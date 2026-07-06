#!/usr/bin/env python3
"""Glovebox MCP — drive a FLEET of sandboxed app windows.

Each "instance" is its own Xephyr display (:1, :2, :3, …) running one app, rendered as a window on
the host desktop and controlled independently. Separate displays = separate cursors, so multiple
agents can drive different instances IN PARALLEL.

Tools take `instance=N` (default 1 = the start-display.sh sandbox). Launch more with launch_app().
Input is unicode-safe (xdotool); screenshots via mss; grounding backend is selectable:

  GLOVEBOX_VISION = none | basic | local     (env / .vision-mode file / default 'local')
  GLOVEBOX_HOST_DISPLAY = :0                  (host display where new Xephyr windows appear)

Response contract: every tool returns JSON — {"ok": true, "action": ..., "instance": ..., ...} on
success. Failures raise ToolError, so the client sees isError=true with the same JSON shape
({"ok": false, "error": ..., "fix": ...}) embedded in the error text; "fix" names the call that
unblocks the agent. screenshot() returns an image; observe='screenshot'|'parse' returns
[json, image].
"""
import os, sys, io, json, base64, subprocess, time, shlex, shutil, signal

os.environ.setdefault("DISPLAY", ":1")
HERE = os.path.dirname(os.path.abspath(__file__))
OMNI_DIR = os.path.join(HERE, "OmniParser")
sys.path.insert(0, OMNI_DIR)
HOST = os.environ.get("GLOVEBOX_HOST_DISPLAY", ":0")

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.utilities.types import Image as MCPImage
import mss
from PIL import Image, ImageDraw

from _version import __version__


def _vision_mode() -> str:
    m = os.environ.get("GLOVEBOX_VISION")
    if not m:
        f = os.path.join(HERE, ".vision-mode")
        if os.path.exists(f):
            with open(f) as fh:
                m = fh.read().strip()
    m = (m or "local").lower()
    return m if m in ("none", "basic", "local") else "local"


VISION = _vision_mode()
mcp = FastMCP("glovebox")
# FastMCP has no version parameter; set it on the underlying server so the real project
# version (not the MCP SDK's) reaches the client in the initialize handshake.
mcp._mcp_server.version = __version__

INSTANCES = {}   # N -> {name, command, size, xephyr_pid, app_pid}
_LAST = {}       # N -> {element id -> (cx, cy)} from that instance's last parse_screen
_SCAN_MAX = 12   # highest display :N that list_instances probes for already-running sandboxes
PARSE_MAX = 300  # parse_screen element cap per call (flagged via "truncated")
FILES_MAX = 200  # list_files entry cap per call (flagged via "truncated")


# ---------- response contract ----------
def _res(ok: bool, action: str, instance=None, **kw) -> str:
    out = {"ok": ok, "action": action}
    if instance is not None:
        out["instance"] = int(instance)
        out["display"] = _disp(instance)
    out.update(kw)
    return json.dumps(out, ensure_ascii=False)


def _ok(action: str, instance=None, **kw) -> str:
    return _res(True, action, instance, **kw)


def _fail(action: str, error: str, fix: str = "", instance=None, **kw):
    """Raise a ToolError carrying the structured payload — client sees isError=true.
    `fix` always names the call/step that unblocks the agent."""
    if fix:
        kw = {"fix": fix, **kw}
    raise ToolError(_res(False, action, instance, error=error, **kw))


# ---------- display / process helpers ----------
def _disp(n) -> str:
    return f":{int(n)}"


def _env(n) -> dict:
    return {**os.environ, "DISPLAY": _disp(n)}


def _files_dir(n) -> str:
    """Per-instance staging folder inside the install dir (files/<N>/). Auto-created.
    A stable place to drop files for an instance; also readable by snap Chromium (it's under $HOME)."""
    d = os.path.join(HERE, "files", str(int(n)))
    os.makedirs(d, exist_ok=True)
    return d


def _up(n) -> bool:
    return subprocess.run(["xdpyinfo", "-display", _disp(n)], capture_output=True).returncode == 0


def _instance_down_fix(n) -> str:
    return ("list_instances() shows what is running; launch_app() starts a new instance"
            + (" (instance 1 is started by ./start-display.sh on the host)" if int(n) == 1 else ""))


def _xdo(n, action, *args):
    """Run xdotool against instance n's display; on failure raise a structured error
    (a dead display must never come back as a silent success)."""
    r = subprocess.run(["xdotool", *args], env=_env(n), capture_output=True, text=True)
    if r.returncode != 0:
        if not _up(n):
            _fail(action, f"instance {n} is not running (display {_disp(n)} unreachable)",
                  fix=_instance_down_fix(n), instance=n)
        _fail(action, f"xdotool {args[0]} failed: {(r.stderr or r.stdout).strip() or 'unknown error'}",
              instance=n)
    return r


def _grab(n=1) -> Image.Image:
    with mss.mss(display=_disp(n)) as m:
        shot = m.grab(m.monitors[1])
    return Image.frombytes("RGB", shot.size, shot.rgb)


def _grab_checked(n, action) -> Image.Image:
    try:
        return _grab(n)
    except Exception as e:
        if not _up(n):
            _fail(action, f"instance {n} is not running (display {_disp(n)} unreachable)",
                  fix=_instance_down_fix(n), instance=n)
        _fail(action, f"screen capture failed on {_disp(n)}: {e}", instance=n)


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
        _files_dir(1)


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


def _kill_xephyr(n):
    """Kill instance n's Xephyr and WAIT for the display to go down — returning while the old
    server is still dying lets the next launch_app reuse :N and land on a corpse display."""
    subprocess.run(["pkill", "-f", f"Xephyr {_disp(n)} "], capture_output=True)
    for _ in range(10):
        if not _up(n):
            return
        time.sleep(0.25)


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


def _app_env_args(n, command):
    """Env + argv to launch a GUI `command` on instance n's display. Chromium gets X11 flags,
    a per-instance profile, and a CDP debug port; other GTK apps get D-Bus isolation so they
    render ON this display instead of forwarding their window to an app on the host."""
    appenv = _env(n)
    args = shlex.split(command)
    base = os.path.basename(args[0]) if args else ""
    if base in ("chromium", "chromium-browser", "chrome", "google-chrome"):
        appenv.pop("WAYLAND_DISPLAY", None)
        appenv["GDK_BACKEND"] = "x11"
        if "--ozone-platform" not in command:
            prof = os.path.join(HERE, "profiles", f"chromium-{n}")
            os.makedirs(os.path.join(prof, "Default"), exist_ok=True)
            _prefs = os.path.join(prof, "Default", "Preferences")
            if not os.path.exists(_prefs):   # send downloads / "save as" to the instance's files/<N>/ folder
                _dl = _files_dir(n)
                with open(_prefs, "w") as fh:
                    json.dump({"download": {"default_directory": _dl, "prompt_for_download": False},
                               "savefile": {"default_directory": _dl}}, fh)
            args += ["--ozone-platform=x11", "--disable-gpu", "--no-first-run",
                     "--no-default-browser-check", "--start-maximized",
                     f"--remote-debugging-port={_cdp_port(n)}", "--remote-allow-origins=*",
                     f"--user-data-dir={prof}"]
    else:
        appenv.pop("WAYLAND_DISPLAY", None)
        appenv.setdefault("GDK_BACKEND", "x11")
        if shutil.which("dbus-run-session"):
            args = ["dbus-run-session", "--"] + args
    return appenv, args, base


# ---------- instance management ----------
@mcp.tool()
def launch_app(command: str, name: str = "", size: str = "1440x900") -> str:
    """Launch a GUI app in its OWN new Xephyr display/window and return its instance id.

    `command` is any shell command ('chromium', 'xterm', 'gimp', 'inkscape file.svg', …).
    Chromium automatically gets X11 flags, a per-instance profile, downloads routed to
    files/<N>/, and a CDP debug port (needed by upload_file). Other GTK apps get D-Bus
    isolation so they render on this display, not the host.

    Returns {"ok", "instance", "display", "name", "files_dir"} — pass that instance id to every
    control tool. Fails (isError) if Xephyr can't start on the host display, the command doesn't
    exist, or the app exits immediately. Each instance has its own cursor, so separate agents can
    drive separate instances in parallel; never share one instance between agents.
    The window appears on GLOVEBOX_HOST_DISPLAY (default :0). Give the app a moment to draw
    (settle_ms/wait_ms) before the first screenshot."""
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
        _fail("launch_app", f"Xephyr {_disp(n)} did not start",
              fix=f"check that the host display {HOST} is reachable (GLOVEBOX_HOST_DISPLAY) and Xephyr is installed",
              instance=n)
    if shutil.which("openbox"):   # window manager is a nicety — apps still render without one
        subprocess.Popen(["openbox"], env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.5)
    appenv, args, base = _app_env_args(n, command)
    applog = f"/tmp/glovebox_app_{n}.log"   # app stderr — read back if it dies on the spot
    try:
        with open(applog, "wb") as lf:
            app = subprocess.Popen(args, env=appenv, stdout=subprocess.DEVNULL, stderr=lf)
    except FileNotFoundError:
        _kill_xephyr(n)
        _fail("launch_app", f"command not found: {args[0]}",
              fix="use a command that exists in PATH (e.g. 'chromium', 'xmessage hi')", instance=n)
    time.sleep(0.6)
    rc = app.poll()
    if rc is not None and rc != 0:
        _kill_xephyr(n)
        try:
            with open(applog) as lf:
                stderr_tail = lf.read().strip().splitlines()[-1][:200]
        except (OSError, IndexError):
            stderr_tail = ""
        _fail("launch_app", f"'{command}' exited immediately with code {rc}"
                            + (f": {stderr_tail}" if stderr_tail else ""),
              fix="check that the command exists and its arguments are valid; try it manually with "
                  "DISPLAY set to the instance display",
              instance=n)
    # maximize the app window to fill the display once it maps (handles slow-starting apps too)
    subprocess.Popen(
        ["bash", "-c",
         'for i in $(seq 1 25); do W=$(xdotool search --onlyvisible --maxdepth 4 "" 2>/dev/null | tail -1); '
         'if [ -n "$W" ]; then xdotool windowactivate "$W" 2>/dev/null; '
         'wmctrl -i -r "$W" -b add,maximized_vert,maximized_horz 2>/dev/null; break; fi; sleep 1; done'],
        env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    INSTANCES[n] = {"name": name or base, "command": command, "size": size,
                    "xephyr_pid": xeph.pid, "app_pid": app.pid}
    return _ok("launch_app", n, name=INSTANCES[n]["name"], command=command,
               files_dir=_files_dir(n))


@mcp.tool()
def list_instances() -> str:
    """List all instances (running app windows): id, display, name, command, up.

    Returns {"ok", "count", "instances": [{instance, display, name, command, up}, …]}.
    Probes displays :1–:12 for sandboxes started outside this server (e.g. start-display.sh),
    so it also finds instances this process didn't launch (their name/command show as unknown).
    Use it to discover what you can drive and to verify an instance is up before acting."""
    _register_existing()
    seen = set(INSTANCES) | {k for k in range(1, _SCAN_MAX + 1) if _up(k)}
    out = []
    for n in sorted(seen):
        info = INSTANCES.get(n, {})
        out.append({"instance": n, "display": _disp(n), "name": info.get("name", "?"),
                    "command": info.get("command", "(unknown)"), "up": _up(n)})
    return _ok("list_instances", count=len(out), instances=out)


@mcp.tool()
def close_instance(instance: int) -> str:
    """Close an instance: kill its app + its Xephyr display, and verify it actually went down.

    Returns {"ok", "instance", "detail"} only after the display stops responding.
    Fails (isError) if the instance isn't running, if it survives SIGTERM, or for instance 1 —
    the main start-display.sh sandbox is protected (stop it from the host with `pkill Xephyr`)."""
    n = int(instance)
    if n == 1:
        _fail("close_instance", "refusing to close instance 1 (the main start-display.sh sandbox)",
              fix="stop it from the host with `pkill Xephyr`; other instances close by id", instance=1)
    info = INSTANCES.pop(n, None)
    if info is None and not _up(n):
        _fail("close_instance", f"instance {n} is not running — nothing to close",
              fix="list_instances() shows what is running", instance=n)
    if info:
        for pid in (info.get("app_pid"), info.get("xephyr_pid")):
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except Exception:
                    pass
    _kill_xephyr(n)
    _LAST.pop(n, None)
    if not _up(n):
        return _ok("close_instance", n, detail=f"instance {n} closed (display down)")
    _fail("close_instance", f"instance {n} is still up after SIGTERM",
          fix=f"retry close_instance({n}); as a last resort `pkill -f 'Xephyr {_disp(n)}'` on the host",
          instance=n)


# ---------- per-instance control ----------
@mcp.tool()
def get_screen_size(instance: int = 1) -> str:
    """Screen size of an instance + the active vision backend and server version.

    Returns {"ok", "instance", "width", "height", "vision", "version"}. Call it once before
    coordinate math — coordinates are absolute pixels, top-left origin, within THIS instance's
    display. "vision" tells you how to ground: 'basic'/'local' → parse_screen + click_element;
    'none' → screenshot + reason about pixels yourself. Fails (isError) if the instance is down."""
    img = _grab_checked(instance, "get_screen_size")
    return _ok("get_screen_size", instance, width=img.width, height=img.height,
               vision=VISION, version=__version__)


@mcp.tool()
def screenshot(instance: int = 1) -> MCPImage:
    """Screenshot of an instance's window (PNG image, full display).

    The ONLY way to see the screen — always look before you act. For an element list with
    coordinates use parse_screen (basic/local vision). Prefer observe='screenshot' on action
    tools to act and see the result in one call. Fails (isError) if the instance is down."""
    img = _grab_checked(instance, "screenshot")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return MCPImage(data=buf.getvalue(), format="png")


def _do_parse(instance: int = 1, box_threshold: float = 0.05) -> dict:
    if VISION == "none":
        return {"ok": True, "action": "parse_screen", "instance": int(instance), "vision": "none",
                "note": "no local grounding — take screenshot(instance) and reason about pixel "
                        "coords yourself, then click(x, y, instance)."}
    img = _grab_checked(instance, "parse_screen")
    W, H = img.width, img.height
    ann = f"/tmp/glovebox_annotated_{int(instance)}.png"
    if VISION == "basic":
        elems, boxes = _parse_tesseract(img)
        _annotate(img, elems, boxes, ann)
    else:
        elems = _parse_omni(img, box_threshold, ann)
    _LAST[int(instance)] = {e["id"]: tuple(e["center"]) for e in elems}
    total = len(elems)
    truncated = total > PARSE_MAX
    if truncated:
        elems = elems[:PARSE_MAX]
    return {"ok": True, "action": "parse_screen", "instance": int(instance), "vision": VISION,
            "screen": [W, H], "annotated": ann, "count": total,
            "truncated": truncated, "elements": elems}


def _observe(instance, mode, settle_ms, payload: dict):
    """After an action, optionally return the resulting state in the SAME call.
    mode: 'none' (json only), 'screenshot' (json + image), 'parse' (json incl. elements + image).
    Mode is validated by _validate_observe BEFORE the action runs."""
    if settle_ms:
        time.sleep(min(int(settle_ms), 5000) / 1000.0)
    payload = {"ok": True, "action": payload.pop("action"), "instance": int(instance),
               "display": _disp(instance), **payload}
    if mode == "screenshot":
        img = _grab_checked(instance, payload["action"])
        buf = io.BytesIO(); img.save(buf, format="PNG")
        payload["observed"] = "screenshot"
        return [json.dumps(payload, ensure_ascii=False), MCPImage(data=buf.getvalue(), format="png")]
    if mode == "parse":
        pr = _do_parse(instance)
        for k in ("ok", "action", "instance"):
            pr.pop(k, None)
        payload["observed"] = "parse"
        payload["parse"] = pr
        out = [json.dumps(payload, ensure_ascii=False)]
        ann = f"/tmp/glovebox_annotated_{int(instance)}.png"
        if VISION != "none" and os.path.exists(ann):
            with open(ann, "rb") as fh:
                out.append(MCPImage(data=fh.read(), format="png"))
        return out
    return json.dumps(payload, ensure_ascii=False)


def _validate_observe(action, instance, observe):
    """Reject a bad observe mode BEFORE performing the action (never act, then error)."""
    if observe not in ("none", "screenshot", "parse"):
        _fail(action, f"unknown observe mode '{observe}' — the action was not performed",
              fix="use observe='none' | 'screenshot' | 'parse'", instance=instance)


@mcp.tool()
def parse_screen(instance: int = 1, box_threshold: float = 0.05) -> str:
    """Detect on-screen elements and return them with ids + pixel-centers (vision grounding).

    Returns {"ok", "vision", "screen": [W,H], "annotated", "count", "truncated",
    "elements": [{id, type, label, interactive, center: [x,y]}, …]} — then click_element(id).
    Also saves a numbered Set-of-Mark image to /tmp/glovebox_annotated_<N>.png.
    Backend = GLOVEBOX_VISION: 'local' (OmniParser, text + icons, ~2 s on GPU after a ~6 s
    first-call model load), 'basic' (tesseract, text only), 'none' (returns a note telling you
    to screenshot + reason about pixels yourself — this is normal, not an error).
    Elements are capped at 300 per call ("truncated": true when more were detected).
    Ids are only valid until the screen changes — re-parse after navigation."""
    return json.dumps(_do_parse(instance, box_threshold), ensure_ascii=False)


@mcp.tool()
def click(x: int, y: int, instance: int = 1, button: int = 1, observe: str = "none", settle_ms: int = 0):
    """Click at absolute pixel coords (top-left origin) in an instance.

    Returns {"ok", "action", "instance", "detail"}. button: 1=left, 2=middle, 3=right.
    `observe`='screenshot'|'parse' additionally returns the resulting screen state in the SAME
    call (saves a round-trip); `settle_ms` (max 5000) waits for the UI to update first — use
    400–1500 after anything that navigates or submits. Fails (isError) if the instance is down."""
    _validate_observe("click", instance, observe)
    _xdo(instance, "click", "mousemove", "--sync", str(x), str(y))
    _xdo(instance, "click", "click", str(button))
    return _observe(instance, observe, settle_ms,
                    {"action": "click", "detail": f"clicked ({x},{y}) button {button}"})


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, instance: int = 1, button: int = 1,
         observe: str = "none", settle_ms: int = 0):
    """Drag (mouse-down → move → up) from (x1,y1) to (x2,y2) — for drawing, selecting, sliders.

    Returns {"ok", "action", "instance", "detail"}. `observe`/`settle_ms`: see click().
    Fails (isError) if the instance is down."""
    _validate_observe("drag", instance, observe)
    _xdo(instance, "drag", "mousemove", "--sync", str(x1), str(y1))
    _xdo(instance, "drag", "mousedown", str(button))
    _xdo(instance, "drag", "mousemove", "--sync", str(x2), str(y2))
    _xdo(instance, "drag", "mouseup", str(button))
    return _observe(instance, observe, settle_ms,
                    {"action": "drag", "detail": f"dragged ({x1},{y1})->({x2},{y2})"})


@mcp.tool()
def double_click(x: int, y: int, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Double-click at absolute pixel coords in an instance (e.g. open a file icon, select a word).

    Returns {"ok", "action", "instance", "detail"}. `observe`/`settle_ms`: see click().
    Fails (isError) if the instance is down."""
    _validate_observe("double_click", instance, observe)
    _xdo(instance, "double_click", "mousemove", "--sync", str(x), str(y))
    _xdo(instance, "double_click", "click", "--repeat", "2", "1")
    return _observe(instance, observe, settle_ms,
                    {"action": "double_click", "detail": f"double-clicked ({x},{y})"})


@mcp.tool()
def click_element(id: int, instance: int = 1, button: int = 1, observe: str = "none", settle_ms: int = 0):
    """Click an element by id from that instance's most recent parse_screen (no coordinate guessing).

    Returns {"ok", "action", "instance", "detail", "element", "center"}. Fails (isError) with the
    fix named if the id is unknown — run parse_screen(instance) first. Ids go stale when the
    screen changes: after navigation, re-parse before clicking. `observe`/`settle_ms`: see click()."""
    _validate_observe("click_element", instance, observe)
    last = _LAST.get(int(instance), {})
    if id not in last:
        _fail("click_element", f"unknown element id {id} for instance {instance}",
              fix="run parse_screen(instance) first — ids are only valid for the most recent parse",
              instance=instance, known_ids=len(last))
    x, y = last[id]
    _xdo(instance, "click_element", "mousemove", "--sync", str(x), str(y))
    _xdo(instance, "click_element", "click", str(button))
    return _observe(instance, observe, settle_ms,
                    {"action": "click_element", "detail": f"clicked element {id} at ({x},{y})",
                     "element": id, "center": [x, y]})


@mcp.tool()
def type_text(text: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Type into the focused field — unicode-safe. Click the field first to focus it.

    Returns {"ok", "action", "instance", "detail", "method": "xdotool"|"clipboard"}.
    Pure ASCII is typed via xdotool; text with any non-ASCII char (č/š/ž …) is inserted via the
    clipboard + ctrl+v, because xdotool's synthetic unicode keystrokes get silently dropped by
    some toolkits (e.g. Inkscape's GTK canvas) even though they work in browsers. Fails (isError)
    rather than silently losing characters if xclip is missing for non-ASCII text.
    `observe`/`settle_ms`: see click()."""
    _validate_observe("type_text", instance, observe)
    if any(ord(c) > 127 for c in text):
        if not shutil.which("xclip"):
            _fail("type_text", "text contains non-ASCII characters but xclip is not installed — "
                               "typing it via xdotool would silently drop characters in some toolkits",
                  fix="install xclip (e.g. `sudo apt install xclip`) or send ASCII-only text",
                  instance=instance)
        r = subprocess.run(["xclip", "-selection", "clipboard"],
                           input=text.encode("utf-8"), env=_env(instance))
        if r.returncode != 0:
            if not _up(instance):
                _fail("type_text", f"instance {instance} is not running (display {_disp(instance)} unreachable)",
                      fix=_instance_down_fix(instance), instance=instance)
            _fail("type_text", "xclip failed to set the clipboard", instance=instance)
        time.sleep(0.05)
        _xdo(instance, "type_text", "key", "--clearmodifiers", "ctrl+v")
        method = "clipboard"
    else:
        _xdo(instance, "type_text", "type", "--clearmodifiers", "--delay", "15", "--", text)
        method = "xdotool"
    return _observe(instance, observe, settle_ms,
                    {"action": "type_text", "detail": f"typed {len(text)} chars", "method": method})


@mcp.tool()
def upload_file(filepath: str, instance: int = 1, selector: str = "input[type=file]",
                observe: str = "none", settle_ms: int = 0):
    """Attach a local file to a page's <input type=file> via Chrome DevTools Protocol — use this for
    ALL browser uploads (logos, images, docs).

    The sandbox's native GTK file picker is invisible to us AND hangs the renderer, so never click
    an upload button expecting a dialog — call this instead. Works on Chromium started by
    launch_app / start-display.sh (they open a per-instance --remote-debugging-port, 9222+N).
    `selector` targets the file input on the MAIN page (default = first file input). If the site
    only inserts the <input> after you click its 'upload' control, click that first (it won't open
    a dialog we can see, but it wires up the input), then call this.
    Returns {"ok", "action", "instance", "detail"}; fails (isError) with the reason if the file is
    missing, no CDP target answers, or the selector matches nothing.
    NATIVE (non-browser) apps: use open_file() or drive the app's own Open dialog — it IS visible
    here. `observe`/`settle_ms`: see click()."""
    _validate_observe("upload_file", instance, observe)
    if not os.path.exists(filepath):
        _fail("upload_file", f"file not found: {filepath}",
              fix="list_files(instance) shows the staging folder files/<N>/ — drop the file there or pass an absolute path that exists",
              instance=instance)
    try:
        _cdp_set_file_input(_cdp_port(instance), filepath, selector)
    except Exception as e:
        _fail("upload_file", f"upload failed: {e}",
              fix="the browser must be a Chromium started by launch_app/start-display.sh (CDP port "
                  f"{_cdp_port(instance)}); if the page adds the <input> only after clicking its "
                  "upload control, click that control first, then retry",
              instance=instance)
    return _observe(instance, observe, settle_ms,
                    {"action": "upload_file",
                     "detail": f"set '{selector}' = {os.path.basename(filepath)} via CDP"})


@mcp.tool()
def open_file(filepath: str, instance: int = 1, app: str = "") -> str:
    """Open a local file in an app ON an existing instance's display (NOT a new instance).

    If `app` is given, runs `<app> <filepath>` there (e.g. app='gimp'); otherwise tries xdg-open.
    Relative paths resolve against the instance's staging folder files/<N>/ (see list_files).
    Returns {"ok", "action", "instance", "opened", "app"}; fails (isError) if the file or the app
    doesn't exist. The app is started asynchronously — screenshot after a moment to confirm it
    drew. For BROWSER <input type=file> uploads use upload_file() instead. GTK apps get the same
    X11 + D-Bus handling as launch_app (so they render on this display, not the host)."""
    p = filepath if os.path.isabs(filepath) else os.path.join(_files_dir(instance), filepath)
    if not os.path.exists(p):
        _fail("open_file", f"file not found: {p}",
              fix="list_files(instance) shows the staging folder and its contents; drop the file there or pass an absolute path",
              instance=instance)
    if app:
        appenv, args, _ = _app_env_args(instance, app)
        args = args + [p]
    else:
        appenv, args = _env(instance), ["xdg-open", p]
    try:
        subprocess.Popen(args, env=appenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        _fail("open_file", f"command not found: {args[0]}",
              fix="pass an app that exists in PATH (e.g. app='gimp') or omit app to use xdg-open",
              instance=instance)
    return _ok("open_file", instance, opened=p, app=app or "xdg-open")


@mcp.tool()
def list_files(instance: int = 1) -> str:
    """The instance's staging folder (files/<N>/ under the install dir) and its contents.

    Returns {"ok", "instance", "dir", "count", "truncated", "files": […]} (capped at 200 entries,
    sorted). Drop files there (or reference any host path you can read) to open in apps via
    open_file() or an app's Open dialog; launch_app Chromium instances also download / 'save as'
    into it — call this after a download to confirm the file arrived. Browser uploads still go
    through upload_file()."""
    d = _files_dir(instance)
    try:
        files = sorted(os.listdir(d))
    except OSError:
        files = []
    total = len(files)
    truncated = total > FILES_MAX
    if truncated:
        files = files[:FILES_MAX]
    return _ok("list_files", instance, dir=d, count=total, truncated=truncated, files=files)


@mcp.tool()
def press_keys(keys: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Press a key or combo (xdotool syntax): 'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'ctrl+l', 'F5'.

    Returns {"ok", "action", "instance", "detail"}. Fails (isError) on an invalid keysym or a dead
    instance. Browser navigation: 'ctrl+l' → 'ctrl+a' → type_text(url) → 'Return'.
    `observe`/`settle_ms`: see click()."""
    _validate_observe("press_keys", instance, observe)
    r = _xdo(instance, "press_keys", "key", "--clearmodifiers", keys)
    # xdotool exits 0 on unknown keysyms and just ignores them — that's a silent no-op, so flag it
    if "No such key name" in (r.stderr or ""):
        _fail("press_keys", f"invalid key name in '{keys}' — xdotool ignored it, nothing was pressed",
              fix="use xdotool keysym syntax: 'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5'",
              instance=instance)
    return _observe(instance, observe, settle_ms,
                    {"action": "press_keys", "detail": f"pressed {keys}"})


@mcp.tool()
def move_mouse(x: int, y: int, instance: int = 1) -> str:
    """Move the mouse pointer to absolute pixel coords in an instance (no click).

    Returns {"ok", "action", "instance", "detail"}. Useful for hover states (menus, tooltips) —
    follow with screenshot() to see the effect. Fails (isError) if the instance is down."""
    _xdo(instance, "move_mouse", "mousemove", "--sync", str(x), str(y))
    return _ok("move_mouse", instance, detail=f"moved to ({x},{y})")


@mcp.tool()
def scroll(amount: int, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Scroll the wheel at the current mouse position: positive = up, negative = down.

    Returns {"ok", "action", "instance", "detail"}. One unit ≈ one wheel notch (a few lines).
    Scrolling targets the window under the pointer — move_mouse/click there first. Fails (isError)
    on amount=0 (a no-op) or a dead instance. `observe`/`settle_ms`: see click()."""
    _validate_observe("scroll", instance, observe)
    amount = int(amount)
    if amount == 0:
        _fail("scroll", "scroll amount 0 is a no-op — nothing was scrolled",
              fix="pass a positive amount to scroll up or a negative amount to scroll down",
              instance=instance)
    btn = "4" if amount > 0 else "5"
    for _ in range(abs(amount)):
        _xdo(instance, "scroll", "click", btn)
    return _observe(instance, observe, settle_ms,
                    {"action": "scroll", "detail": f"scrolled {amount}"})


@mcp.tool()
def wait_ms(ms: int) -> str:
    """Wait (e.g. for a page to load or an app to draw) — capped at 10000 ms.

    Returns {"ok", "action", "requested_ms", "waited_ms", "clamped"} — "clamped": true means you
    asked for more than the cap and only waited_ms elapsed. Prefer settle_ms on the action itself
    when you also want to observe the result in the same call."""
    ms = int(ms)
    waited = max(0, min(ms, 10000))
    time.sleep(waited / 1000)
    return _ok("wait_ms", requested_ms=ms, waited_ms=waited, clamped=waited != ms)


@mcp.tool()
def status() -> str:
    """Server + sandbox status in one read-only call — run this first, and when filing bug reports.

    Returns {"ok", "version", "vision", "host_display", "instances": [...same as list_instances],
    "deps": {xdotool, xclip, xephyr, wmctrl, tesseract, omniparser_weights: true|false}}.
    "vision" is the grounding backend (none|basic|local); "deps" shows whether each system tool
    is installed and (for 'local') whether the OmniParser weights are present. No side effects."""
    _register_existing()
    seen = set(INSTANCES) | {k for k in range(1, _SCAN_MAX + 1) if _up(k)}
    instances = []
    for n in sorted(seen):
        info = INSTANCES.get(n, {})
        instances.append({"instance": n, "display": _disp(n), "name": info.get("name", "?"),
                          "command": info.get("command", "(unknown)"), "up": _up(n)})
    deps = {"xdotool": bool(shutil.which("xdotool")),
            "xclip": bool(shutil.which("xclip")),
            "xephyr": bool(shutil.which("Xephyr")),
            "wmctrl": bool(shutil.which("wmctrl")),
            "tesseract": bool(shutil.which("tesseract")),
            "omniparser_weights": os.path.exists(os.path.join(OMNI_DIR, "weights", "icon_detect", "model.pt"))}
    return _ok("status", version=__version__, vision=VISION, host_display=HOST,
               instances=instances, deps=deps)


if __name__ == "__main__":
    mcp.run()
