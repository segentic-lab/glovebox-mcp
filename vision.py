"""Vision grounding for glovebox: backend selection, GPU preflight, and the OmniParser lifecycle.

The server never guesses here. Each backend has a readiness check that says *why* it is or isn't
usable, and the resolution rules are:

  GLOVEBOX_VISION = auto  (default)  best READY backend: local > basic > none, reason recorded
                  = local            OmniParser on the GPU; if not ready, parse_screen fails
                                     with the fix named (it never silently falls back)
                  = basic            Tesseract OCR; same strictness
                  = none             no local grounding — the agent reads screenshots itself

Readiness for `local` means: the OmniParser code is vendored next to this file, the weights are
downloaded, torch is importable, an NVIDIA GPU is visible and has >= MIN_VRAM_MB of memory.
CUDA itself is verified when the models are first loaded (importing torch costs ~1-2 s, so the
cheap nvidia-smi check is what status() reports).

CLI (used by install.sh, also handy by hand):
    python vision.py check              # JSON: requested/effective backend, GPU, deps, reasons
    python vision.py install-omniparser # clone OmniParser + download the v2 weights + patch
    python vision.py install-screenparser # optional typed detector inside the local backend (GLOVEBOX_DETECTOR)
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
OMNI_DIR = os.path.join(HERE, "OmniParser")
OMNI_REPO = "https://github.com/microsoft/OmniParser.git"
OMNI_HF_REPO = "microsoft/OmniParser-v2.0"
OMNI_WEIGHTS = os.path.join(OMNI_DIR, "weights")
OMNI_DETECT = os.path.join(OMNI_WEIGHTS, "icon_detect", "model.pt")
OMNI_CAPTION = os.path.join(OMNI_WEIGHTS, "icon_caption_florence")
OMNI_FILES = ("icon_detect/train_args.yaml", "icon_detect/model.pt", "icon_detect/model.yaml",
              "icon_caption/config.json", "icon_caption/generation_config.json",
              "icon_caption/model.safetensors")

MODES = ("auto", "none", "basic", "local")
MIN_VRAM_MB = 6000        # OmniParser (YOLO + Florence-2) needs ~6 GB; below this it will not load
TIGHT_VRAM_MB = 8000      # works, but close other GPU apps
MIN_FREE_VRAM_MB = 4500   # free memory needed at model-load time
OCR_MIN_CONF = 45         # tesseract word confidence floor
GAP_FACTOR = 1.5          # 'line' grouping splits at gaps wider than this x the word height (menu bars)
OCR_TIMEOUT_S = 30        # a hung tesseract must not hang the tool call
OCR_SCALE_DEFAULT = 2.0   # UI text is ~12-16 px; tesseract wants ~2x that (measured: 1x found nothing, 2x read it at 96%)
DEFAULT_TESS_LANGS = ("slv", "eng")


class VisionError(Exception):
    """A grounding failure with the fix named — the server turns it into a structured error."""

    def __init__(self, error: str, fix: str):
        super().__init__(error)
        self.error, self.fix = error, fix


# ---------- detector choice inside the local backend ----------
# OmniParser's own detector (icon_detect, YOLOv8) finds "interactable icons" and nothing else: every
# box it emits is type "icon". ScreenParser (docling-project, ICML 2026) is a YOLO11-L trained on
# 1.45M screenshots with 55 element classes; swapped into the same pipeline (OCR + Florence-2
# captions) it yields typed elements (button/link/tab/input/checkbox/...) and misses fewer targets
# (the paper: ScreenSpot action accuracy 77.8 -> 80.1 with a fixed Qwen3-VL-8B agent). Apache-2.0,
# 153 MB. Opt in by installing it: `python vision.py install-screenparser`; GLOVEBOX_DETECTOR forces.
SCREENPARSER_HF = "docling-project/ScreenParser"
SCREENPARSER_FILE = "best.pt"
SCREENPARSER_WEIGHTS = os.path.join(OMNI_WEIGHTS, "screenparser", SCREENPARSER_FILE)
SCREENPARSER_IMGSZ = 1280   # the model card's inference settings
SCREENPARSER_CONF = 0.10
SCREENPARSER_IOU = 0.10
DETECTORS = ("auto", "omniparser", "screenparser")

# ScreenParser class -> glovebox element type. Classes NOT listed are layout containers (Window,
# Screen, Side Bar, Toolbar, Navigation Bar, Tab Bar, Status Bar, Table, List, Menu, Scroll,
# Carousel, Column/Browser, Alert, Notification, Tooltip, Video, Chart, Calendar, Breadcrumb,
# Pagination, Bottom navigation, Page control, Search Bar, ContextMenu/DockMenu/EditMenu/PopUp Menu):
# a container box would swallow every OCR line inside it into one element, so they are dropped.
SP_TYPES = {
    "Button": "button", "Utility Button": "button", "Link": "link", "Tab": "tab",
    "Text Input": "input", "Search Field": "input", "Checkbox": "checkbox", "Radiobox": "radio",
    "Switch": "switch", "Toggles": "switch", "Select": "select", "Picker": "select",
    "Date-Time picker": "select", "Slider": "slider", "Steppers": "stepper",
    "Rating Indicator": "rating", "App Icon": "icon", "File Icon": "icon",
    "Image": "image", "Avatar": "image", "Logo": "image", "List Item": "item",
    "Badge": "indicator", "Progress bar": "indicator",
    "Text": "text", "Heading": "text", "Code snippet": "text",
}
SP_NON_INTERACTIVE = {"image", "indicator", "text"}


def requested_detector() -> str:
    d = os.environ.get("GLOVEBOX_DETECTOR", "auto").strip().lower() or "auto"
    return d if d in DETECTORS else "auto"


def detector_info() -> dict:
    """Which detector the local backend uses. Returns {requested, effective, screenparser_weights,
    reason}. auto = ScreenParser when its weights are installed, else OmniParser's own detector.
    An explicit 'screenparser' without weights is reported here and fails parse_screen honestly."""
    req = requested_detector()
    have = os.path.isfile(SCREENPARSER_WEIGHTS)
    if req == "screenparser" and not have:
        eff, reason = "screenparser", f"ScreenParser weights missing at {SCREENPARSER_WEIGHTS} (run `python vision.py install-screenparser`)"
    elif req == "omniparser" or (req == "auto" and not have):
        eff, reason = "omniparser", "OmniParser icon detector" + ("" if req == "omniparser" else " (install ScreenParser for typed elements: `python vision.py install-screenparser`)")
    else:
        eff, reason = "screenparser", "ScreenParser (55 element classes)" + (" — auto-selected because its weights are installed" if req == "auto" else "")
    return {"requested": req, "effective": eff, "screenparser_weights": have, "reason": reason}


def _box_area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a, b):
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _overlap(a, b) -> float:
    """OmniParser's overlap measure: max(IoU, fraction of a covered, fraction of b covered)."""
    i = _inter(a, b)
    aa, ab = _box_area(a), _box_area(b)
    if aa <= 0 or ab <= 0:
        return 0.0
    return max(i / (aa + ab - i + 1e-6), i / aa, i / ab)


def _iou(a, b) -> float:
    i = _inter(a, b)
    u = _box_area(a) + _box_area(b) - i
    return i / u if u > 0 else 0.0


def _inside(a, b) -> bool:
    aa = _box_area(a)
    return aa > 0 and _inter(a, b) / aa > 0.80


def _clamp_box(b, size=None):
    """Box clamped to the image; None when degenerate (under 2 px a side when `size` is known —
    OmniParser's captioner silently skips crops it cannot make, which would shift every caption
    after it onto the wrong element)."""
    x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in b)
    if x2 <= x1 or y2 <= y1:
        return None
    if size and ((x2 - x1) * size[0] < 2 or (y2 - y1) * size[1] < 2):
        return None
    return [x1, y1, x2, y2]


def _duplicate(d, e, iou_threshold) -> bool:
    """Is detection d redundant next to e? Same class: OmniParser's rule (a box that mostly
    covers or is covered by another of its kind is a duplicate; the smaller one survives, ties
    go to the higher confidence). Different classes: only a near-identical box (IoU) — a Text
    Input must keep its clear-icon child, a List Item its Button."""
    a, b = d["bbox"], e["bbox"]
    if d["cls"] == e["cls"]:
        if _overlap(a, b) <= iou_threshold:
            return False
        aa, ab = _box_area(a), _box_area(b)
        if aa != ab:
            return aa > ab
        return (d["conf"], -d["_i"]) < (e["conf"], -e["_i"])
    return _iou(a, b) > iou_threshold and (d["conf"], -d["_i"]) < (e["conf"], -e["_i"])


def merge_detections(dets: list, ocr: list, iou_threshold: float = 0.7, size=None) -> list:
    """Combine ScreenParser detections with OCR lines into glovebox elements (no GPU; pure).

    dets: [{"bbox": [x1,y1,x2,y2] in 0..1, "cls": ScreenParser class, "conf": float}]
    ocr:  [{"bbox": [...], "content": str}]      size: (W, H) pixels, for the degenerate-box filter
    Rules (OmniParser's merge, made class-aware):
      * boxes are clamped to the image; degenerate ones (< 2 px) are dropped;
      * container classes are dropped (see SP_TYPES);
      * duplicates: same-class boxes that mostly overlap keep the smaller (ties: the more
        confident); different classes are duplicates only when near-identical (IoU) — so a
        Text Input keeps the icon inside it;
      * a Text/Heading detection that contains OCR lines is dropped — the lines are the better
        targets; one with no OCR inside stays (type "text", captioned later);
      * any other detection absorbs the OCR lines inside it as its label (a button gets its
        caption text); a detection that lies inside an OCR line is dropped (OmniParser's rule);
      * remaining OCR lines become type "text".
    Returns elements [{"type", "bbox", "content", "interactive", "class", "conf"}] with the
    caption-needing ones (content None) LAST, as OmniParser's captioner expects."""
    cand = []
    for i, d in enumerate(dets):
        if d["cls"] not in SP_TYPES:
            continue
        bb = _clamp_box(d["bbox"], size)
        if bb:
            cand.append({**d, "bbox": bb, "_i": i})
    kept = [d for d in cand if not any(e is not d and _duplicate(d, e, iou_threshold) for e in cand)]
    kept.sort(key=lambda d: _box_area(d["bbox"]))          # small boxes claim their text first
    pool = []
    for o in ocr:
        bb = _clamp_box(o["bbox"], size)
        if bb:
            pool.append({**o, "bbox": bb})
    out = []
    for d in kept:
        typ = SP_TYPES[d["cls"]]
        inside = [o for o in pool if _inside(o["bbox"], d["bbox"])]
        if typ == "text":
            if inside:
                continue
            out.append({"type": "text", "bbox": d["bbox"], "content": None, "interactive": False,
                        "class": d["cls"], "conf": round(d["conf"], 3)})
            continue
        if not inside and any(_inside(d["bbox"], o["bbox"]) for o in pool):
            continue
        for o in inside:
            pool.remove(o)
        label = " ".join(o["content"] for o in inside).strip() or None
        out.append({"type": typ, "bbox": d["bbox"], "content": label, "interactive": typ not in SP_NON_INTERACTIVE,
                    "class": d["cls"], "conf": round(d["conf"], 3)})
    for o in pool:
        out.append({"type": "text", "bbox": o["bbox"], "content": o["content"], "interactive": False,
                    "class": "OCR", "conf": None})
    out.sort(key=lambda e: e["content"] is None)
    return out


# ---------- requested mode ----------
def requested_mode() -> str:
    """GLOVEBOX_VISION env, else the .vision-mode file written by install.sh, else 'auto'."""
    m = os.environ.get("GLOVEBOX_VISION")
    if not m:
        f = os.path.join(HERE, ".vision-mode")
        if os.path.exists(f):
            with open(f) as fh:
                m = fh.read().strip()
    m = (m or "auto").lower()
    return m if m in MODES else "auto"


# ---------- probes ----------
def gpu_info() -> dict:
    """NVIDIA GPU presence + memory via nvidia-smi (fast, no torch import).
    Returns {present, name, vram_total_mb, vram_free_mb, enough, note}."""
    out = {"present": False, "name": None, "vram_total_mb": None, "vram_free_mb": None,
           "enough": False, "note": "nvidia-smi not found — no NVIDIA GPU (or driver) visible"}
    if not shutil.which("nvidia-smi"):
        return out
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError) as e:
        out["note"] = f"nvidia-smi failed: {e}"
        return out
    line = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not line:
        out["note"] = f"nvidia-smi failed: {(r.stderr or r.stdout).strip()[:120] or 'no output'}"
        return out
    parts = [p.strip() for p in line[0].split(",")]
    try:
        name, total, free = parts[0], int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        out["note"] = f"could not parse nvidia-smi output: {line[0][:120]}"
        return out
    out.update(present=True, name=name, vram_total_mb=total, vram_free_mb=free,
               enough=total >= MIN_VRAM_MB)
    if total < MIN_VRAM_MB:
        out["note"] = f"{total} MiB VRAM < {MIN_VRAM_MB} MiB needed by OmniParser"
    elif total < TIGHT_VRAM_MB:
        out["note"] = f"{total} MiB VRAM is tight — close other GPU apps before parsing"
    elif free < MIN_FREE_VRAM_MB:
        out["note"] = f"only {free} MiB VRAM free (need ~{MIN_FREE_VRAM_MB} MiB to load the models)"
    else:
        out["note"] = "ok"
    return out


def tesseract_langs() -> list:
    """Installed tesseract languages (empty if the binary is missing)."""
    if not shutil.which("tesseract"):
        return []
    try:
        r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        return []
    langs = [l.strip() for l in (r.stdout + r.stderr).splitlines()]
    return sorted(l for l in langs if re.fullmatch(r"[a-z_]{3,}", l) and l != "osd")


def tesseract_lang_string(available=None) -> str:
    """The `lang` passed to tesseract: GLOVEBOX_TESS_LANG (or legacy LC_TESS_LANG) if set, else
    the default languages FILTERED to what is installed (a missing language used to be a hard
    tesseract error), falling back to 'eng'."""
    env = os.environ.get("GLOVEBOX_TESS_LANG") or os.environ.get("LC_TESS_LANG")
    if env:
        return env
    available = tesseract_langs() if available is None else available
    picked = [l for l in DEFAULT_TESS_LANGS if l in available]
    return "+".join(picked) if picked else "eng"


def tesseract_info() -> dict:
    binary = bool(shutil.which("tesseract"))
    pyt = importlib.util.find_spec("pytesseract") is not None
    langs = tesseract_langs() if binary else []
    ready = binary and pyt and bool(langs)
    reason = ("ok" if ready else
              "tesseract binary not installed" if not binary else
              "pytesseract not installed (pip install pytesseract)" if not pyt else
              "tesseract has no language data installed")
    return {"binary": binary, "pytesseract": pyt, "langs": langs,
            "lang": tesseract_lang_string(langs) if binary else None, "ready": ready, "reason": reason}


def omniparser_info(gpu: dict = None) -> dict:
    """Is the OmniParser backend usable? Checks code, weights, torch, GPU memory — without
    importing torch. Returns {code, weights, torch, gpu, ready, reason}."""
    code = os.path.isfile(os.path.join(OMNI_DIR, "util", "utils.py"))
    weights = os.path.isfile(OMNI_DETECT) and os.path.isdir(OMNI_CAPTION)
    torch_ok = importlib.util.find_spec("torch") is not None
    gpu = gpu or gpu_info()
    if not code:
        reason = f"OmniParser code not found at {OMNI_DIR} (run ./install.sh local or `python vision.py install-omniparser`)"
    elif not weights:
        reason = f"OmniParser weights missing under {OMNI_WEIGHTS} (run `python vision.py install-omniparser`)"
    elif not torch_ok:
        reason = "torch not installed in this environment (run ./install.sh local)"
    elif not gpu["present"]:
        reason = gpu["note"]
    elif not gpu["enough"]:
        reason = gpu["note"]
    else:
        reason = "ok" if gpu["note"] == "ok" else gpu["note"]
    ready = code and weights and torch_ok and gpu["present"] and gpu["enough"]
    # loadable: enough FREE memory to load the models now (irrelevant once a service holds them)
    loadable = ready and (gpu["vram_free_mb"] is None or gpu["vram_free_mb"] >= MIN_FREE_VRAM_MB)
    return {"code": code, "weights": weights, "torch": torch_ok, "gpu": gpu, "ready": ready, "loadable": loadable,
            "reason": reason, "detector": detector_info()}


def resolve(requested: str = None, omni: dict = None, tess: dict = None, service_loaded: bool = False) -> dict:
    """Pick the effective backend. Returns {requested, effective, ready, reason, fallback}.
    `ready` is False only when an explicitly requested backend is unusable (auto always lands
    on something usable, 'none' at worst). `service_loaded`: a vision service already holds
    the models, so free-VRAM at load time no longer matters for auto."""
    requested = requested or requested_mode()
    omni = omni if omni is not None else omniparser_info()
    tess = tess if tess is not None else tesseract_info()
    if omni.get("ready") and not omni.get("loadable", True) and not service_loaded:
        # usable hardware, but not enough free memory to load right now: auto must not pick it
        omni = {**omni, "ready": False, "reason": omni["gpu"]["note"]}
    if requested == "local":
        return {"requested": "local", "effective": "local", "ready": omni["ready"],
                "reason": omni["reason"], "fallback": False}
    if requested == "basic":
        return {"requested": "basic", "effective": "basic", "ready": tess["ready"],
                "reason": tess["reason"], "fallback": False}
    if requested == "none":
        return {"requested": "none", "effective": "none", "ready": True,
                "reason": "no local grounding requested", "fallback": False}
    if omni["ready"]:
        return {"requested": "auto", "effective": "local", "ready": True,
                "reason": "OmniParser ready" + ("" if omni["reason"] == "ok" else f" ({omni['reason']})"),
                "fallback": False}
    if tess["ready"]:
        return {"requested": "auto", "effective": "basic", "ready": True,
                "reason": f"OmniParser not usable: {omni['reason']}; tesseract ready", "fallback": True}
    return {"requested": "auto", "effective": "none", "ready": True,
            "reason": f"OmniParser not usable: {omni['reason']}; tesseract not usable: {tess['reason']}",
            "fallback": True}


# ---------- tesseract backend ----------
def ocr_scale() -> float:
    """Upscale factor applied before OCR (GLOVEBOX_OCR_SCALE, 1-4, default 2)."""
    try:
        return max(1.0, min(float(os.environ.get("GLOVEBOX_OCR_SCALE", OCR_SCALE_DEFAULT)), 4.0))
    except ValueError:
        return OCR_SCALE_DEFAULT


def group_tesseract(data: dict, granularity: str = "line", min_conf: int = OCR_MIN_CONF, scale: float = 1.0):
    """Turn pytesseract image_to_data output into elements. 'line' merges words that tesseract
    put on the same line ("Sign" + "in" -> "Sign in", one target) but SPLITS a line where the
    gap between two words is wider than GAP_FACTOR x the line's word height — a menu bar
    "File   Edit   View" is three targets, not one whose centre is blank space; 'word' keeps
    single words. `scale` is the upscale factor the image was OCR'd at; boxes are mapped back
    to screen pixels. Returns (elems, boxes), ids in tesseract's block/line order."""
    groups = {}
    order = []
    n = len(data["text"])
    for i in range(n):
        txt = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not txt or conf < min_conf:
            continue
        x, y, w, h = (int(round(int(data[k][i]) / scale)) for k in ("left", "top", "width", "height"))
        key = (i,) if granularity == "word" else (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((x, y, w, h, txt, conf))
    elems, boxes = [], {}
    for key in order:
        words = sorted(groups[key], key=lambda t: t[0])
        heights = sorted(t[3] for t in words)
        gap_limit = max(8, GAP_FACTOR * heights[len(heights) // 2])
        runs, run = [], [words[0]]
        for prev, cur in zip(words, words[1:]):
            if cur[0] - (prev[0] + prev[2]) > gap_limit:
                runs.append(run)
                run = []
            run.append(cur)
        runs.append(run)
        for run in runs:
            x0, y0 = min(t[0] for t in run), min(t[1] for t in run)
            x1, y1 = max(t[0] + t[2] for t in run), max(t[1] + t[3] for t in run)
            eid = len(elems)
            elems.append({"id": eid, "type": "text", "label": " ".join(t[4] for t in run), "interactive": None,
                          "center": [(x0 + x1) // 2, (y0 + y1) // 2],
                          "confidence": round(sum(t[5] for t in run) / len(run))})
            boxes[eid] = (x0, y0, x1, y1)
    return elems, boxes


def parse_tesseract(img, granularity: str = "line"):
    """The `basic` backend: tesseract over a grayscale, auto-contrasted, upscaled frame.
    Returns (elems, boxes) like group_tesseract; raises VisionError with the fix named."""
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError:
        raise VisionError("pytesseract is not installed",
                          "pip install pytesseract (or ./install.sh basic), or set GLOVEBOX_VISION=none") from None
    lang = tesseract_lang_string()
    from PIL import Image, ImageOps
    # Grayscale + autocontrast first: measured on a CI frame (small white window on a black root)
    # the raw RGB frame yielded nothing, the preprocessed one every word at 95%+, in half the time.
    gray = ImageOps.autocontrast(ImageOps.grayscale(img))
    # Two passes, keep the richer one: UI text is usually too small at 1x (2x reads it), but some
    # tesseract builds return NOTHING for an upscaled frame they read fine at 1x (seen on 5.3.4 in CI
    # vs 5.5.0 locally). Costs one extra ~0.2 s pass; never returns an empty parse one scale would
    # have filled. Coordinates are mapped back to screen pixels either way.
    best = None
    for scale in dict.fromkeys((ocr_scale(), 1.0)):
        im = gray if scale == 1.0 else gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
        try:
            data = pytesseract.image_to_data(im, lang=lang, output_type=Output.DICT, timeout=OCR_TIMEOUT_S)
        except pytesseract.TesseractNotFoundError:
            raise VisionError("tesseract binary not found",
                              "install tesseract-ocr (./install.sh basic) or set GLOVEBOX_VISION=none") from None
        except RuntimeError as e:   # pytesseract raises RuntimeError on its timeout
            raise VisionError(f"tesseract did not finish within {OCR_TIMEOUT_S} s: {str(e)[:120]}",
                              "retry; lower GLOVEBOX_OCR_SCALE, or parse a smaller instance") from None
        except Exception as e:   # TesseractError: bad language data etc.
            raise VisionError(f"tesseract failed (lang={lang}): {str(e)[:200]}",
                              "install the language data (apt install tesseract-ocr-<lang>) or set "
                              "GLOVEBOX_TESS_LANG to an installed language (`tesseract --list-langs`)") from None
        elems, boxes = group_tesseract(data, granularity, scale=scale)
        words = sum(len(e["label"].split()) for e in elems)
        if best is None or words > best[0]:
            best = (words, elems, boxes)
    return best[1], best[2]


# ---------- OmniParser backend ----------
def _center(bb, W, H) -> list:
    """Pixel centre of a ratio box, clamped into the display (a box that spills past 1.0 must
    not yield an off-screen click target)."""
    cx = int((float(bb[0]) + float(bb[2])) / 2 * W)
    cy = int((float(bb[1]) + float(bb[3])) / 2 * H)
    return [min(max(cx, 0), max(W - 1, 0)), min(max(cy, 0), max(H - 1, 0))]


class OmniParser:
    """Lazy-loaded OmniParser (YOLO icon detector + Florence-2 captioner) with a GPU preflight."""

    def __init__(self):
        self._yolo = None
        self._cap = None
        self._lock = threading.Lock()
        self.load_seconds = None
        self.detector = detector_info()["effective"]   # omniparser | screenparser (fixed at load)

    @property
    def loaded(self) -> bool:
        return self._yolo is not None

    def _preflight(self):
        info = omniparser_info()
        if not info["ready"]:
            raise VisionError(f"OmniParser not usable: {info['reason']}",
                              "run ./install.sh local (needs an NVIDIA GPU with >= 6 GB VRAM), or set "
                              "GLOVEBOX_VISION=basic / none")
        gpu = info["gpu"]
        if not info["loadable"]:
            raise VisionError(f"not enough free GPU memory to load OmniParser: {gpu['vram_free_mb']} MiB free, "
                              f"~{MIN_FREE_VRAM_MB} MiB needed ({gpu['name']})",
                              "close other GPU applications and retry, or set GLOVEBOX_VISION=basic")

    def load(self):
        with self._lock:
            if self._yolo is not None:
                return
            self._preflight()
            import time
            t0 = time.time()
            if OMNI_DIR not in sys.path:
                sys.path.insert(0, OMNI_DIR)
            try:
                import torch
                from util.utils import get_yolo_model, get_caption_model_processor
            except ImportError as e:
                raise VisionError(f"OmniParser import failed: {e}",
                                  "re-run ./install.sh local to install its Python dependencies")
            if not torch.cuda.is_available():
                raise VisionError("torch cannot see CUDA (driver/toolkit mismatch?) — OmniParser needs the GPU",
                                  "fix the NVIDIA driver (`nvidia-smi` works but torch.cuda.is_available() is False), "
                                  "or set GLOVEBOX_VISION=basic")
            det = detector_info()
            self.detector = det["effective"]
            if self.detector == "screenparser" and not det["screenparser_weights"]:
                raise VisionError(det["reason"], "run `python vision.py install-screenparser`, or unset GLOVEBOX_DETECTOR")
            try:
                if self.detector == "screenparser":
                    from ultralytics import YOLO
                    self._yolo = YOLO(SCREENPARSER_WEIGHTS)
                else:
                    self._yolo = get_yolo_model(OMNI_DETECT)
                self._cap = get_caption_model_processor(model_name="florence2", model_name_or_path=OMNI_CAPTION)
            except Exception as e:
                self._yolo = self._cap = None
                if "out of memory" in str(e).lower():
                    raise VisionError(f"GPU out of memory while loading OmniParser: {str(e)[:160]}",
                                      "close other GPU applications and retry, or set GLOVEBOX_VISION=basic")
                raise VisionError(f"OmniParser model load failed: {str(e)[:200]}",
                                  "re-run `python vision.py install-omniparser` to repair the weights")
            self.load_seconds = round(time.time() - t0, 1)

    def parse(self, img, box_threshold: float, annotated_path: str):
        """Elements for one frame; writes the numbered Set-of-Mark image to annotated_path.
        Raises VisionError (fix named) for every failure; never returns a partial list."""
        self.load()
        from util.utils import check_ocr_box, get_som_labeled_img
        import base64
        import tempfile
        W, H = img.width, img.height
        # OmniParser's OCR wants a file; a private temp file, removed afterwards (frames are
        # screenshots of the sandbox — they must not pile up in the temp dir)
        fd, frame = tempfile.mkstemp(prefix="glovebox-frame-", suffix=".png")
        os.close(fd)
        try:
            img.save(frame)
            try:
                (ocr_text, ocr_bbox), _ = check_ocr_box(frame, display_img=False, output_bb_format="xyxy",
                                                        easyocr_args={"paragraph": False, "text_threshold": 0.8},
                                                        use_paddleocr=False)
                if self.detector == "screenparser":
                    return self._parse_screenparser(img, box_threshold, ocr_text, ocr_bbox, annotated_path)
                b64, _lc, parsed = get_som_labeled_img(frame, self._yolo, BOX_TRESHOLD=box_threshold,
                                                       output_coord_in_ratio=True, ocr_bbox=ocr_bbox,
                                                       caption_model_processor=self._cap, ocr_text=ocr_text,
                                                       iou_threshold=0.7, imgsz=640)
            except VisionError:
                raise
            except Exception as e:
                if "out of memory" in str(e).lower():
                    raise VisionError(f"GPU out of memory during parse: {str(e)[:160]}",
                                      "close other GPU applications, or parse a smaller instance (launch_app size=), "
                                      "or set GLOVEBOX_VISION=basic") from None
                raise VisionError(f"OmniParser parse failed: {str(e)[:200]}",
                                  "retry once; if it persists, re-run ./install.sh local") from None
        finally:
            try:
                os.unlink(frame)
            except OSError:
                pass
        with open(annotated_path, "wb") as f:
            f.write(base64.b64decode(b64))
        # ids must match the numbers drawn on the annotated image: keep OmniParser's order 1:1
        return [{"id": i, "type": e.get("type"), "label": e.get("content"), "interactive": e.get("interactivity"),
                 "center": _center(e.get("bbox") or e.get("box") or [0, 0, 0, 0], W, H)}
                for i, e in enumerate(parsed)]

    def _parse_screenparser(self, img, box_threshold, ocr_text, ocr_bbox, annotated_path):
        """ScreenParser detector + OmniParser's OCR/captioner/annotator. Same element shape as the
        default path plus "class" (the ScreenParser class) and typed "type" values. The detector
        confidence floor is max(box_threshold, SCREENPARSER_CONF): the agent can raise it to cut
        noise, never lower it below the model card's setting."""
        import numpy as np
        import torch
        from torchvision.ops import box_convert
        from util.utils import annotate, get_parsed_content_icon
        W, H = img.width, img.height
        res = self._yolo.predict(source=img, conf=max(float(box_threshold), SCREENPARSER_CONF), iou=SCREENPARSER_IOU,
                                 imgsz=SCREENPARSER_IMGSZ, verbose=False)[0]
        dets = [{"bbox": [x1 / W, y1 / H, x2 / W, y2 / H], "cls": self._yolo.names[int(c)], "conf": float(p)}
                for (x1, y1, x2, y2), c, p in zip(res.boxes.xyxy.tolist(), res.boxes.cls.tolist(), res.boxes.conf.tolist())]
        ocr = [{"bbox": [b[0] / W, b[1] / H, b[2] / W, b[3] / H], "content": t}
               for b, t in zip(ocr_bbox or [], ocr_text or [])]
        elems = merge_detections(dets, ocr, size=(W, H))
        start = next((i for i, e in enumerate(elems) if e["content"] is None), -1)
        np_img = np.asarray(img.convert("RGB"))
        boxes = torch.tensor([e["bbox"] for e in elems], dtype=torch.float32) if elems else torch.zeros((0, 4))
        if start >= 0:
            # captions for boxes without text (icons, images, inputs): OmniParser's Florence-2 call;
            # it crops boxes[start:] — when start == 0 it falls back to all boxes, which is right.
            # It silently skips crops it cannot make; merge_detections drops degenerate boxes so
            # that cannot happen, and if the counts still differ no caption is assigned rather
            # than shifting every caption onto the wrong element.
            caps = get_parsed_content_icon(boxes, start, np_img, self._cap)
            need = elems[start:]
            if len(caps) == len(need):
                for e, c in zip(need, caps):
                    e["content"] = c
            else:
                for e in need:
                    e["content"] = None
        if elems:
            ann, _ = annotate(np_img, box_convert(boxes, "xyxy", "cxcywh"), torch.ones(len(elems)),
                              list(range(len(elems))), text_scale=0.4, text_padding=5)
            from PIL import Image as _I
            _I.fromarray(ann).save(annotated_path)
        else:
            img.save(annotated_path)
        return [{"id": i, "type": e["type"], "label": e["content"], "interactive": e["interactive"],
                 "center": _center(e["bbox"], W, H), "class": e["class"]}
                for i, e in enumerate(elems)]


# ---------- install helper (moved out of install.sh so the server owns the OmniParser lifecycle) ----------
def install_omniparser(log=print) -> str:
    """Clone OmniParser next to the server, download the v2 weights, make PaddleOCR optional.
    Idempotent. Needs git + huggingface_hub (installed by ./install.sh local)."""
    if not os.path.isdir(OMNI_DIR):
        log(f"▶ cloning OmniParser into {OMNI_DIR} …")
        subprocess.run(["git", "clone", "--depth", "1", OMNI_REPO, OMNI_DIR], check=True)
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit("huggingface_hub is not installed — run ./install.sh local (it installs the "
                         "Python deps first) or `pip install huggingface_hub`")
    os.makedirs(OMNI_WEIGHTS, exist_ok=True)
    for f in OMNI_FILES:
        log(f"▶ weights: {f}")
        hf_hub_download(repo_id=OMNI_HF_REPO, filename=f, local_dir=OMNI_WEIGHTS)
    cap = os.path.join(OMNI_WEIGHTS, "icon_caption")
    if os.path.isdir(cap) and not os.path.isdir(OMNI_CAPTION):
        os.rename(cap, OMNI_CAPTION)
    # OmniParser's util/utils.py imports PaddleOCR (we use easyocr) and the OpenAI SDK (for its
    # GPT captioning mode, which we never call) unconditionally — make both optional (idempotent),
    # so the local backend does not drag in packages it never uses.
    u = os.path.join(OMNI_DIR, "util", "utils.py")
    if os.path.isfile(u):
        s = open(u).read()
        patched = []
        # anchored at column 0: once wrapped, the import is indented and must not be wrapped again
        if re.search(r"^from paddleocr import PaddleOCR\n", s, re.M):
            s = re.sub(r"^from paddleocr import PaddleOCR\n",
                       "try:\n    from paddleocr import PaddleOCR\nexcept Exception:\n    PaddleOCR = None\n", s, count=1, flags=re.M)
            s = re.sub(r"(rec_batch_num=1024\))\n", r"\1 if PaddleOCR is not None else None\n", s, count=1)
            patched.append("PaddleOCR")
        if re.search(r"^from openai import AzureOpenAI\n", s, re.M):
            s = re.sub(r"^from openai import AzureOpenAI\n",
                       "try:\n    from openai import AzureOpenAI\nexcept Exception:\n    AzureOpenAI = None\n", s, count=1, flags=re.M)
            patched.append("openai")
        if patched:
            open(u, "w").write(s)
            log(f"▶ patched OmniParser util/utils.py ({', '.join(patched)} optional)")
    info = omniparser_info()
    log(f"✓ OmniParser installed — ready: {info['ready']} ({info['reason']})")
    return OMNI_DIR


def install_screenparser(log=print) -> str:
    """Download the ScreenParser detector weights (153 MB, Apache-2.0) next to OmniParser's.
    Idempotent. OmniParser (the OCR + captioner) must be installed as well."""
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        raise SystemExit("huggingface_hub is not installed — run ./install.sh local first")
    d = os.path.dirname(SCREENPARSER_WEIGHTS)
    os.makedirs(d, exist_ok=True)
    log(f"▶ weights: {SCREENPARSER_HF}/{SCREENPARSER_FILE} → {d}")
    hf_hub_download(repo_id=SCREENPARSER_HF, filename=SCREENPARSER_FILE, local_dir=d)
    info = detector_info()
    log(f"✓ ScreenParser installed — detector now: {info['effective']} ({info['reason']})")
    if not omniparser_info()["ready"]:
        log("⚠ OmniParser itself is not ready; ScreenParser runs inside its pipeline — run `python vision.py install-omniparser`")
    return SCREENPARSER_WEIGHTS


def check() -> dict:
    gpu = gpu_info()
    omni = omniparser_info(gpu)
    tess = tesseract_info()
    return {"resolution": resolve(None, omni, tess), "gpu": gpu, "omniparser": omni, "tesseract": tess}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        print(json.dumps(check(), indent=2))
    elif cmd == "install-omniparser":
        install_omniparser()
    elif cmd == "install-screenparser":
        install_screenparser()
    else:
        print(__doc__)
        sys.exit(2)
