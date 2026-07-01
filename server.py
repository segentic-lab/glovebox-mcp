#!/usr/bin/env python3
"""Glovebox MCP — drive a FLEET of sandboxed app windows.

Each "instance" is its own Xephyr display (:1, :2, :3, …) running one app, rendered as a window on
the host desktop and controlled independently. Separate displays = separate cursors, so multiple
agents can drive different instances IN PARALLEL.

Tools take `instance=N` (default 1 = the start-display.sh sandbox). Launch more with launch_app().
Input is unicode-safe (xdotool); screenshots via mss; grounding backend is selectable:

  GLOVEBOX_VISION = claude | basic | local   (env / .vision-mode file / default 'local')
  GLOVEBOX_HOST_DISPLAY = :0                  (host display where new Xephyr windows appear)
"""
import os, sys, io, json, base64, subprocess, time, shlex, signal

os.environ.setdefault("DISPLAY", ":1")
HERE = os.path.dirname(os.path.abspath(__file__))
OMNI_DIR = os.path.join(HERE, "OmniParser")
sys.path.insert(0, OMNI_DIR)
HOST = os.environ.get("GLOVEBOX_HOST_DISPLAY", ":0")

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image as MCPImage
import mss
from PIL import Image, ImageDraw


def _vision_mode() -> str:
    m = os.environ.get("GLOVEBOX_VISION")
    if not m:
        f = os.path.join(HERE, ".vision-mode")
        if os.path.exists(f):
            with open(f) as fh:
                m = fh.read().strip()
    m = (m or "local").lower()
    return m if m in ("claude", "basic", "local") else "local"


VISION = _vision_mode()
mcp = FastMCP("glovebox")

INSTANCES = {}   # N -> {name, command, size, xephyr_pid, app_pid}
_LAST = {}       # N -> {element id -> (cx, cy)} from that instance's last parse_screen
_SCAN_MAX = 12   # highest display :N that list_instances probes for already-running sandboxes


# ---------- display / process helpers ----------
def _disp(n) -> str:
    return f":{int(n)}"


def _env(n) -> dict:
    return {**os.environ, "DISPLAY": _disp(n)}


def _xdotool(n, *args):
    return subprocess.run(["xdotool", *args], env=_env(n), capture_output=True, text=True)


def _grab(n=1) -> Image.Image:
    with mss.mss(display=_disp(n)) as m:
        shot = m.grab(m.monitors[1])
    return Image.frombytes("RGB", shot.size, shot.rgb)


def _up(n) -> bool:
    return subprocess.run(["xdpyinfo", "-display", _disp(n)], capture_output=True).returncode == 0


def _host_env() -> dict:
    """Env for spawning Xephyr on the host display — discovers the (session-specific) XAUTHORITY."""
    e = {**os.environ, "DISPLAY": HOST}
    xa = e.get("XAUTHORITY", "")
    if not xa or not os.path.exists(xa):
        import glob
        uid = os.getuid()
        cands = (glob.glob(f"/run/user/{uid}/xauth_*")
                 + glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*")
                 + [os.path.expanduser("~/.Xauthority")])
        for c in cands:
            if os.path.exists(c):
                e["XAUTHORITY"] = c
                break
    return e


def _register_existing():
    if _up(1) and 1 not in INSTANCES:
        INSTANCES[1] = {"name": "sandbox", "command": "(start-display.sh)", "size": "?",
                        "xephyr_pid": None, "app_pid": None}


# ---------- Chrome DevTools Protocol (file uploads bypass the broken native GTK picker) ----------
def _cdp_port(n) -> int:
    return 9222 + int(n)


def _cdp_set_file_input(port, filepath, selector):
    """Attach a file to a page's <input type=file> over CDP (no native dialog, no renderer hang)."""
    import urllib.request, websocket
    info = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5).read().decode())
    pages = [t for t in info if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
             and not t.get("url", "").startswith("devtools://")]
    if not pages:
        raise RuntimeError(f"no CDP page target on 127.0.0.1:{port} — is Chromium started with --remote-debugging-port?")
    # suppress_origin: Chrome rejects CDP WS handshakes that carry a non-allowlisted Origin header;
    # sending none sidesteps that without needing --remote-allow-origins on the browser.
    ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
    seq = {"n": 0}

    def cmd(method, params=None):
        seq["n"] += 1
        mid = seq["n"]
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error'].get('message')}")
                return msg.get("result", {})

    try:
        cmd("DOM.enable")
        root = cmd("DOM.getDocument", {"depth": 1})["root"]["nodeId"]
        nid = cmd("DOM.querySelector", {"nodeId": root, "selector": selector}).get("nodeId")
        if not nid:
            raise RuntimeError(f"no element matches selector '{selector}' on the page")
        cmd("DOM.setFileInputFiles", {"files": [os.path.abspath(filepath)], "nodeId": nid})
    finally:
        ws.close()


def _next_free() -> int:
    n = 2
    while _up(n) or n in INSTANCES:
        n += 1
    return n


# ---------- vision backends ----------
def _annotate(img, elems, boxes, path):
    im = img.copy()
    d = ImageDraw.Draw(im)
    for e in elems:
        cx, cy = e["center"]
        bb = (boxes or {}).get(e["id"])
        if bb:
            d.rectangle(bb, outline=(255, 60, 0), width=2)
        d.text((cx + 3, cy - 8), str(e["id"]), fill=(255, 60, 0))
    im.save(path)


def _parse_tesseract(img):
    import pytesseract
    from pytesseract import Output
    data = pytesseract.image_to_data(img, lang=os.environ.get("LC_TESS_LANG", "slv+eng"),
                                     output_type=Output.DICT)
    elems, boxes = [], {}
    for i in range(len(data["text"])):
        txt = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1
        if not txt or conf < 45:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        eid = len(elems)
        elems.append({"id": eid, "type": "text", "label": txt, "interactive": None,
                      "center": [x + w // 2, y + h // 2]})
        boxes[eid] = (x, y, x + w, y + h)
    return elems, boxes


_yolo = None
_cap = None


def _load_omni():
    global _yolo, _cap
    if _yolo is not None:
        return
    from util.utils import get_yolo_model, get_caption_model_processor
    _yolo = get_yolo_model(os.path.join(OMNI_DIR, "weights/icon_detect/model.pt"))
    _cap = get_caption_model_processor(model_name="florence2",
                                       model_name_or_path=os.path.join(OMNI_DIR, "weights/icon_caption_florence"))


def _parse_omni(img, box_threshold, annotated_path):
    _load_omni()
    from util.utils import check_ocr_box, get_som_labeled_img
    W, H = img.width, img.height
    frame = annotated_path.replace("annotated", "frame")
    img.save(frame)
    (ocr_text, ocr_bbox), _ = check_ocr_box(frame, display_img=False, output_bb_format="xyxy",
                                            easyocr_args={"paragraph": False, "text_threshold": 0.8},
                                            use_paddleocr=False)
    b64, _lc, parsed = get_som_labeled_img(frame, _yolo, BOX_TRESHOLD=box_threshold,
                                           output_coord_in_ratio=True, ocr_bbox=ocr_bbox,
                                           caption_model_processor=_cap, ocr_text=ocr_text,
                                           iou_threshold=0.7, imgsz=640)
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


# ---------- instance management ----------
@mcp.tool()
def launch_app(command: str, name: str = "", size: str = "1440x900") -> str:
    """Launch a GUI app in its OWN new Xephyr display/window; returns its instance id.
    `command` is any shell command (e.g. 'chromium', 'xterm', 'gimp'). For Chromium, the X11
    flags + a per-instance profile are added automatically. Control it with instance=<id>."""
    _register_existing()
    n = _next_free()
    xeph = subprocess.Popen(
        ["Xephyr", _disp(n), "-screen", size, "-resizeable", "-ac",
         "-title", f"Glovebox {_disp(n)} — {name or command[:28]}"],
        env=_host_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if _up(n):
            break
        time.sleep(0.25)
    else:
        return json.dumps({"error": f"Xephyr {_disp(n)} did not start (host display {HOST} reachable?)"})
    subprocess.Popen(["openbox"], env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    appenv = _env(n)
    args = shlex.split(command)
    base = os.path.basename(args[0]) if args else ""
    if base in ("chromium", "chromium-browser", "chrome", "google-chrome"):
        appenv.pop("WAYLAND_DISPLAY", None)
        appenv["GDK_BACKEND"] = "x11"
        if "--ozone-platform" not in command:
            prof = os.path.join(HERE, "profiles", f"chromium-{n}")
            os.makedirs(prof, exist_ok=True)
            args += ["--ozone-platform=x11", "--disable-gpu", "--no-first-run",
                     "--no-default-browser-check", "--start-maximized",
                     f"--remote-debugging-port={_cdp_port(n)}", "--remote-allow-origins=*",
                     f"--user-data-dir={prof}"]
    else:
        # Force X11 + isolate the D-Bus session so GTK single-instance apps (inkscape, gimp, …)
        # open ON THIS display instead of forwarding their window to an instance on the host.
        appenv.pop("WAYLAND_DISPLAY", None)
        appenv.setdefault("GDK_BACKEND", "x11")
        import shutil
        if shutil.which("dbus-run-session"):
            args = ["dbus-run-session", "--"] + args
    app = subprocess.Popen(args, env=appenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # maximize the app window to fill the display once it maps (handles slow-starting apps too)
    subprocess.Popen(
        ["bash", "-c",
         'for i in $(seq 1 25); do W=$(xdotool search --onlyvisible --maxdepth 4 "" 2>/dev/null | tail -1); '
         'if [ -n "$W" ]; then xdotool windowactivate "$W" 2>/dev/null; '
         'wmctrl -i -r "$W" -b add,maximized_vert,maximized_horz 2>/dev/null; break; fi; sleep 1; done'],
        env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    INSTANCES[n] = {"name": name or base, "command": command, "size": size,
                    "xephyr_pid": xeph.pid, "app_pid": app.pid}
    return json.dumps({"instance": n, "display": _disp(n), "name": INSTANCES[n]["name"]})


@mcp.tool()
def list_instances() -> str:
    """List all instances (running app windows) with their display + name."""
    _register_existing()
    seen = set(INSTANCES) | {k for k in range(1, _SCAN_MAX + 1) if _up(k)}
    out = []
    for n in sorted(seen):
        info = INSTANCES.get(n, {})
        out.append({"instance": n, "display": _disp(n), "name": info.get("name", "?"),
                    "command": info.get("command", "(unknown)"), "up": _up(n)})
    return json.dumps(out)


@mcp.tool()
def close_instance(instance: int) -> str:
    """Close an instance (kills its app + Xephyr display). Refuses instance 1 (the main sandbox)."""
    if int(instance) == 1:
        return "refusing to close instance 1 (the main sandbox); use pkill Xephyr to stop everything"
    info = INSTANCES.pop(int(instance), None)
    if info:
        for pid in (info.get("app_pid"), info.get("xephyr_pid")):
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
    subprocess.run(["pkill", "-f", f"Xephyr {_disp(instance)} "], capture_output=True)
    _LAST.pop(int(instance), None)
    return f"closed instance {instance}"


# ---------- per-instance control ----------
@mcp.tool()
def get_screen_size(instance: int = 1) -> str:
    """Screen size of an instance + active vision backend."""
    img = _grab(instance)
    return json.dumps({"instance": instance, "width": img.width, "height": img.height, "vision": VISION})


@mcp.tool()
def screenshot(instance: int = 1) -> MCPImage:
    """Screenshot of an instance's window."""
    img = _grab(instance)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return MCPImage(data=buf.getvalue(), format="png")


def _do_parse(instance: int = 1, box_threshold: float = 0.05) -> str:
    if VISION == "claude":
        return json.dumps({"vision": "claude", "instance": instance,
                           "note": "grounding disabled — use screenshot(instance) and reason about coords, then click(instance,x,y)."})
    img = _grab(instance)
    W, H = img.width, img.height
    ann = f"/tmp/glovebox_annotated_{int(instance)}.png"
    if VISION == "basic":
        elems, boxes = _parse_tesseract(img)
        _annotate(img, elems, boxes, ann)
    else:
        elems = _parse_omni(img, box_threshold, ann)
    _LAST[int(instance)] = {e["id"]: tuple(e["center"]) for e in elems}
    return json.dumps({"vision": VISION, "instance": instance, "screen": [W, H],
                       "annotated": ann, "count": len(elems), "elements": elems}, ensure_ascii=False)


def _observe(instance, mode, settle_ms, text):
    """After an action, optionally return the resulting state in the SAME call.
    mode: 'none' (text only), 'screenshot' (text + image), 'parse' (text + elements + image)."""
    if settle_ms:
        time.sleep(min(int(settle_ms), 5000) / 1000.0)
    if mode == "screenshot":
        img = _grab(instance)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        return [text, MCPImage(data=buf.getvalue(), format="png")]
    if mode == "parse":
        pj = _do_parse(instance)
        out = [text, pj]
        ann = f"/tmp/glovebox_annotated_{int(instance)}.png"
        if os.path.exists(ann):
            with open(ann, "rb") as fh:
                out.append(MCPImage(data=fh.read(), format="png"))
        return out
    return text


@mcp.tool()
def parse_screen(instance: int = 1, box_threshold: float = 0.05) -> str:
    """Detect on-screen elements (id, type, label, interactive, pixel-center) for an instance
    (backend = GLOVEBOX_VISION). Saves a numbered image to /tmp/glovebox_annotated_<N>.png."""
    return _do_parse(instance, box_threshold)


@mcp.tool()
def click(x: int, y: int, instance: int = 1, button: int = 1, observe: str = "none", settle_ms: int = 0):
    """Click at absolute coords. `observe`='screenshot'|'parse' returns the resulting screen state
    in the SAME call (saves a round-trip); `settle_ms` waits for the UI to update first. Default 'none'."""
    _xdotool(instance, "mousemove", "--sync", str(x), str(y))
    _xdotool(instance, "click", str(button))
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] clicked ({x},{y})")


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, instance: int = 1, button: int = 1,
         observe: str = "none", settle_ms: int = 0):
    """Drag (mouse-down → move → up) from (x1,y1) to (x2,y2) — for drawing shapes / selecting."""
    _xdotool(instance, "mousemove", "--sync", str(x1), str(y1))
    _xdotool(instance, "mousedown", str(button))
    _xdotool(instance, "mousemove", "--sync", str(x2), str(y2))
    _xdotool(instance, "mouseup", str(button))
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] dragged ({x1},{y1})->({x2},{y2})")


@mcp.tool()
def double_click(x: int, y: int, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Double-click at absolute coords in an instance."""
    _xdotool(instance, "mousemove", "--sync", str(x), str(y))
    _xdotool(instance, "click", "--repeat", "2", "1")
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] double-clicked ({x},{y})")


@mcp.tool()
def click_element(id: int, instance: int = 1, button: int = 1, observe: str = "none", settle_ms: int = 0):
    """Click an element id from the most recent parse_screen. `observe`/`settle_ms`: see click()."""
    last = _LAST.get(int(instance), {})
    if id not in last:
        return f"unknown element id {id} for instance {instance} — run parse_screen(instance) first"
    x, y = last[id]
    _xdotool(instance, "mousemove", "--sync", str(x), str(y))
    _xdotool(instance, "click", str(button))
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] clicked element {id} at ({x},{y})")


@mcp.tool()
def type_text(text: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Type at the focused field — unicode-safe. Pure ASCII is typed via xdotool; text with any
    non-ASCII char (č/š/ž …) is inserted via the clipboard + ctrl+v, because xdotool's synthetic
    unicode keystrokes get silently dropped by some toolkits (e.g. Inkscape's GTK canvas) even
    though they work in browsers. `observe`/`settle_ms`: see click()."""
    import shutil
    if any(ord(c) > 127 for c in text) and shutil.which("xclip"):
        subprocess.run(["xclip", "-selection", "clipboard"],
                       input=text.encode("utf-8"), env=_env(instance))
        time.sleep(0.05)
        _xdotool(instance, "key", "--clearmodifiers", "ctrl+v")
    else:
        _xdotool(instance, "type", "--clearmodifiers", "--delay", "15", "--", text)
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] typed {len(text)} chars")


@mcp.tool()
def upload_file(filepath: str, instance: int = 1, selector: str = "input[type=file]",
                observe: str = "none", settle_ms: int = 0):
    """Attach a local file to a page's <input type=file> via Chrome DevTools Protocol — use this for
    ALL browser uploads (logos, images, docs). The sandbox's native GTK file picker is invisible to us
    AND hangs the renderer, so never click an upload button expecting a dialog — call this instead.
    Works on Chromium started by launch_app / start-display.sh (they open a per-instance
    --remote-debugging-port). `selector` targets the file input on the MAIN page (default = first file
    input). If the site only inserts the <input> after you click its 'upload' control, click that first
    (it won't open a dialog we can see, but it wires up the input), then call this. `observe`/`settle_ms`: see click()."""
    if not os.path.exists(filepath):
        return json.dumps({"error": f"file not found: {filepath}"})
    try:
        _cdp_set_file_input(_cdp_port(instance), filepath, selector)
    except Exception as e:
        return json.dumps({"error": f"upload failed: {e}"})
    return _observe(instance, observe, settle_ms,
                    f"[{_disp(instance)}] set '{selector}' = {os.path.basename(filepath)} via CDP")


@mcp.tool()
def press_keys(keys: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Press a key/combo (xdotool): 'Return','ctrl+a','Tab','ctrl+t'. `observe`/`settle_ms`: see click()."""
    _xdotool(instance, "key", "--clearmodifiers", keys)
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] pressed {keys}")


@mcp.tool()
def move_mouse(x: int, y: int, instance: int = 1) -> str:
    """Move the mouse in an instance."""
    _xdotool(instance, "mousemove", "--sync", str(x), str(y))
    return f"[{_disp(instance)}] moved to ({x},{y})"


@mcp.tool()
def scroll(amount: int, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Scroll: positive = up, negative = down. `observe`/`settle_ms`: see click()."""
    btn = "4" if amount > 0 else "5"
    for _ in range(abs(amount)):
        _xdotool(instance, "click", btn)
    return _observe(instance, observe, settle_ms, f"[{_disp(instance)}] scrolled {amount}")


@mcp.tool()
def wait_ms(ms: int) -> str:
    """Wait (e.g. for a page to load). Capped at 10000 ms."""
    ms = max(0, min(int(ms), 10000))
    time.sleep(ms / 1000)
    return f"waited {ms}ms"


if __name__ == "__main__":
    mcp.run()
