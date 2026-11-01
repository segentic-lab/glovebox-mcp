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
DEFAULT_TESS_LANGS = ("slv", "eng")


class VisionError(Exception):
    """A grounding failure with the fix named — the server turns it into a structured error."""

    def __init__(self, error: str, fix: str):
        super().__init__(error)
        self.error, self.fix = error, fix


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
    return {"code": code, "weights": weights, "torch": torch_ok, "gpu": gpu, "ready": ready, "reason": reason}


def resolve(requested: str = None, omni: dict = None, tess: dict = None) -> dict:
    """Pick the effective backend. Returns {requested, effective, ready, reason, fallback}.
    `ready` is False only when an explicitly requested backend is unusable (auto always lands
    on something usable, 'none' at worst)."""
    requested = requested or requested_mode()
    omni = omni if omni is not None else omniparser_info()
    tess = tess if tess is not None else tesseract_info()
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
def group_tesseract(data: dict, granularity: str = "line", min_conf: int = OCR_MIN_CONF):
    """Turn pytesseract image_to_data output into elements. 'line' merges words that tesseract
    put on the same line ("Sign" + "in" -> "Sign in", one target); 'word' keeps single words.
    Returns (elems, boxes) with ids assigned in reading order."""
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
        x, y, w, h = int(data["left"][i]), int(data["top"][i]), int(data["width"][i]), int(data["height"][i])
        key = (i,) if granularity == "word" else (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        if key not in groups:
            groups[key] = {"words": [], "confs": [], "box": [x, y, x + w, y + h]}
            order.append(key)
        g = groups[key]
        g["words"].append(txt)
        g["confs"].append(conf)
        b = g["box"]
        g["box"] = [min(b[0], x), min(b[1], y), max(b[2], x + w), max(b[3], y + h)]
    elems, boxes = [], {}
    for key in order:
        g = groups[key]
        x0, y0, x1, y1 = g["box"]
        eid = len(elems)
        elems.append({"id": eid, "type": "text", "label": " ".join(g["words"]), "interactive": None,
                      "center": [(x0 + x1) // 2, (y0 + y1) // 2],
                      "confidence": round(sum(g["confs"]) / len(g["confs"]))})
        boxes[eid] = (x0, y0, x1, y1)
    return elems, boxes


def parse_tesseract(img, granularity: str = "line"):
    try:
        import pytesseract
        from pytesseract import Output
    except ImportError:
        raise VisionError("pytesseract is not installed",
                          "pip install pytesseract (or ./install.sh basic), or set GLOVEBOX_VISION=none")
    lang = tesseract_lang_string()
    try:
        data = pytesseract.image_to_data(img, lang=lang, output_type=Output.DICT)
    except pytesseract.TesseractNotFoundError:
        raise VisionError("tesseract binary not found",
                          "install tesseract-ocr (./install.sh basic) or set GLOVEBOX_VISION=none")
    except Exception as e:   # TesseractError: bad language data etc.
        raise VisionError(f"tesseract failed (lang={lang}): {str(e)[:200]}",
                          "install the language data (apt install tesseract-ocr-<lang>) or set "
                          "GLOVEBOX_TESS_LANG to an installed language (`tesseract --list-langs`)")
    return group_tesseract(data, granularity)


# ---------- OmniParser backend ----------
class OmniParser:
    """Lazy-loaded OmniParser (YOLO icon detector + Florence-2 captioner) with a GPU preflight."""

    def __init__(self):
        self._yolo = None
        self._cap = None
        self._lock = threading.Lock()
        self.load_seconds = None

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
        if gpu["vram_free_mb"] is not None and gpu["vram_free_mb"] < MIN_FREE_VRAM_MB:
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
            try:
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
        self.load()
        from util.utils import check_ocr_box, get_som_labeled_img
        import base64
        W, H = img.width, img.height
        frame = annotated_path.replace("annotated", "frame")
        img.save(frame)
        try:
            (ocr_text, ocr_bbox), _ = check_ocr_box(frame, display_img=False, output_bb_format="xyxy",
                                                    easyocr_args={"paragraph": False, "text_threshold": 0.8},
                                                    use_paddleocr=False)
            b64, _lc, parsed = get_som_labeled_img(frame, self._yolo, BOX_TRESHOLD=box_threshold,
                                                   output_coord_in_ratio=True, ocr_bbox=ocr_bbox,
                                                   caption_model_processor=self._cap, ocr_text=ocr_text,
                                                   iou_threshold=0.7, imgsz=640)
        except Exception as e:
            if "out of memory" in str(e).lower():
                raise VisionError(f"GPU out of memory during parse: {str(e)[:160]}",
                                  "close other GPU applications, or parse a smaller instance (launch_app size=), "
                                  "or set GLOVEBOX_VISION=basic")
            raise VisionError(f"OmniParser parse failed: {str(e)[:200]}",
                              "retry once; if it persists, re-run ./install.sh local")
        with open(annotated_path, "wb") as f:
            f.write(base64.b64decode(b64))
        elems = []
        for e in parsed:
            bb = e.get("bbox") or e.get("box")
            if not bb or len(bb) < 4:
                continue
            elems.append({"id": len(elems), "type": e.get("type"), "label": e.get("content"),
                          "interactive": e.get("interactivity"),
                          "center": [int((bb[0] + bb[2]) / 2 * W), int((bb[1] + bb[3]) / 2 * H)]})
        return elems


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
    # OmniParser imports PaddleOCR unconditionally; we use easyocr, so make it optional (idempotent).
    u = os.path.join(OMNI_DIR, "util", "utils.py")
    if os.path.isfile(u):
        s = open(u).read()
        if "from paddleocr import PaddleOCR\n" in s:
            s = s.replace("from paddleocr import PaddleOCR\n",
                          "try:\n    from paddleocr import PaddleOCR\nexcept Exception:\n    PaddleOCR = None\n")
            s = re.sub(r"(rec_batch_num=1024\))\n", r"\1 if PaddleOCR is not None else None\n", s, count=1)
            open(u, "w").write(s)
            log("▶ patched OmniParser util/utils.py (PaddleOCR optional)")
    info = omniparser_info()
    log(f"✓ OmniParser installed — ready: {info['ready']} ({info['reason']})")
    return OMNI_DIR


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
    else:
        print(__doc__)
        sys.exit(2)
