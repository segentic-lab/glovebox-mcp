#!/usr/bin/env python3
"""Glovebox MCP — drive a FLEET of sandboxed app windows.

Each "instance" is its own Xephyr display (:1, :2, :3, …) running one app, rendered as a window on
the host desktop and controlled independently. Separate displays = separate cursors, so multiple
agents can drive different instances IN PARALLEL.

Tools take `instance=N` (default 1 = the start-display.sh sandbox). Launch more with launch_app().
Input is unicode-safe (xdotool); screenshots via mss; grounding backend is selectable:

  GLOVEBOX_VISION = auto | none | basic | local   (env / .vision-mode file / default 'auto')
  GLOVEBOX_HOST_DISPLAY = :0                      (host display where new Xephyr windows appear)
  GLOVEBOX_CLEANUP_ON_EXIT = 1                    (close the instances THIS process launched when it exits;
                                                   default off — instances outlive the server so another
                                                   agent/session can pick them up)
  GLOVEBOX_VISION_SERVICE = 1                     (local mode: parse through ONE shared OmniParser process
                                                   per machine instead of loading the models in every agent's
                                                   server; 0 = always in-process. See vision_service.py)

Response contract: every tool returns JSON — {"ok": true, "action": ..., "instance": ..., ...} on
success. Failures raise ToolError, so the client sees isError=true with the same JSON shape
({"ok": false, "error": ..., "fix": ...}) embedded in the error text; "fix" names the call that
unblocks the agent. Image-returning tools (screenshot, observe='screenshot'|'parse') return
[json, image].

Isolation is DISPLAY-level: the agent's input and vision are confined to the Xephyr window, but
the apps run as your user with your filesystem. For OS-level isolation run glovebox in a VM/container.
"""
import atexit
import fcntl
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

os.environ.setdefault("DISPLAY", ":1")
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("GLOVEBOX_HOST_DISPLAY", ":0")
TMP = os.path.join(tempfile.gettempdir(), f"glovebox-{os.getuid()}")   # annotated frames + app logs
os.makedirs(TMP, exist_ok=True)

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as MCPImage
import mss
from PIL import Image, ImageChops, ImageDraw

import vision
import vision_service
from _version import __version__

# MCP SDK 2.x: the project version goes straight into the initialize handshake. Sync tool
# functions run on a worker thread in 2.x, so calls on one server process may overlap — module
# state that is mutated (INSTANCES, _LAST, _GEOM, _LAUNCHED) is guarded by _LOCK or is a single
# atomic dict operation; the OmniParser loader has its own lock.
mcp = MCPServer("glovebox", version=__version__,
                instructions="Sandboxed computer use: drive real apps inside nested X11 windows. Call status() "
                             "first; every tool returns JSON with ok/fix; see AGENTS.md in the repo.")

INSTANCES = {}   # N -> {name, command, size, xephyr_pid, app_pid}
_LAST = {}       # N -> {element id -> (cx, cy)} from that instance's last parse_screen
_GEOM = {}       # N -> (W, H) cached display geometry (bounds checks)
_LAUNCHED = set()  # instances started by THIS process (for optional cleanup on exit)
_LOCK = threading.RLock()
_SCAN_MAX = 12   # highest display :N that list_instances probes for already-running sandboxes
PARSE_MAX = 300  # parse_screen element cap per call (flagged via "truncated")
FILES_MAX = 200  # list_files entry cap per call (flagged via "truncated")
WINDOWS_MAX = 50  # window_info visible-window cap
SETTLE_MAX_MS = 5000
WAIT_MAX_MS = 10000
XDO_TIMEOUT = 20  # seconds; a frozen display must not hang a tool call forever
UP_TIMEOUT = 5
BROWSERS = ("chromium", "chromium-browser", "chrome", "google-chrome", "google-chrome-stable",
            "brave", "brave-browser", "microsoft-edge", "microsoft-edge-stable", "vivaldi",
            "vivaldi-stable", "opera")

VISION = vision.resolve()          # {requested, effective, ready, reason, fallback}
_OMNI = vision.OmniParser()        # in-process fallback when the shared service is unavailable
USE_SERVICE = os.environ.get("GLOVEBOX_VISION_SERVICE", "1").strip().lower() not in ("0", "false", "no", "off")
_SERVICE_FAILED_AT = [0.0]         # back off re-spawn attempts after a failure


def _parse_local(img, box_threshold, ann, instance):
    """OmniParser via the shared vision service (one GPU load per machine, see vision_service.py);
    falls back to loading the models in THIS process when the service can't be reached or started.
    Returns (elements, meta) — meta says which path answered and how long it queued."""
    if USE_SERVICE and time.time() - _SERVICE_FAILED_AT[0] > 60:
        try:
            if vision_service.ensure_running() is not None:
                elems, meta = vision_service.parse(img, box_threshold, ann, instance)
                return elems, {"via": "service", **meta}
            _SERVICE_FAILED_AT[0] = time.time()
        except vision.VisionError:
            raise                      # a real parse/preflight error from the service — report it
        except (OSError, ValueError, ConnectionError):
            _SERVICE_FAILED_AT[0] = time.time()   # unreachable: fall through to in-process
    elems = _OMNI.parse(img, box_threshold, ann)
    return elems, {"via": "in-process"}


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


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


def _app_log(n) -> str:
    return os.path.join(TMP, f"app_{int(n)}.log")


def _annotated(n) -> str:
    return os.path.join(TMP, f"annotated_{int(n)}.png")


def _up(n) -> bool:
    try:
        return subprocess.run(["xdpyinfo", "-display", _disp(n)], capture_output=True,
                              timeout=UP_TIMEOUT).returncode == 0
    except subprocess.TimeoutExpired:
        return False   # a display that can't answer xdpyinfo in 5 s is not usable


def _instance_down_fix(n) -> str:
    return ("list_instances() shows what is running; launch_app() starts a new instance"
            + (" (instance 1 is started by ./start-display.sh on the host)" if int(n) == 1 else ""))


def _fail_down(action, n):
    _fail(action, f"instance {n} is not running (display {_disp(n)} unreachable)",
          fix=_instance_down_fix(n), instance=n)


def _xdo(n, action, sub, *args, opts=()):
    """Run `xdotool <sub> <opts> -- <args>` against instance n's display. The `--` keeps
    user-supplied values (negative numbers, '-h') from being parsed as xdotool options.
    On failure raise a structured error (a dead display must never come back as a silent success)."""
    cmd = ["xdotool", sub, *opts]
    if args:
        cmd += ["--", *[str(a) for a in args]]
    try:
        r = subprocess.run(cmd, env=_env(n), capture_output=True, text=True, timeout=XDO_TIMEOUT)
    except subprocess.TimeoutExpired:
        _fail(action, f"xdotool {sub} timed out after {XDO_TIMEOUT} s on {_disp(n)} — the display or app is frozen",
              fix=f"screenshot({n}) to check; close_instance({n}) and launch_app() again if it stays frozen",
              instance=n)
    if r.returncode != 0:
        if not _up(n):
            _fail_down(action, n)
        _fail(action, f"xdotool {sub} failed: {(r.stderr or r.stdout).strip()[:300] or 'unknown error'}",
              instance=n)
    return r


def _geometry(n) -> tuple:
    """(W, H) of the instance display, cached per instance (invalidated on launch/close)."""
    n = int(n)
    if n not in _GEOM:
        r = _xdo(n, "get_screen_size", "getdisplaygeometry")
        w, h = r.stdout.split()[:2]
        _GEOM[n] = (int(w), int(h))
    return _GEOM[n]


def _check_xy(action, n, *points):
    """Reject coordinates outside the display — xdotool silently clamps them to the edge, which
    turned an off-target click into a reported success."""
    W, H = _geometry(n)
    for x, y in points:
        if not (0 <= int(x) < W and 0 <= int(y) < H):
            _fail(action, f"({x},{y}) is outside the {W}x{H} display of instance {n} — nothing was done",
                  fix=f"use 0 <= x < {W} and 0 <= y < {H} (get_screen_size({n}) reports the size); "
                      "re-run parse_screen/screenshot if the coordinates came from a stale view",
                  instance=n, width=W, height=H)


def _mss():
    cls = getattr(mss, "MSS", None) or mss.mss   # mss >= 10 deprecates the lowercase factory
    return cls


def _grab(n=1) -> Image.Image:
    with _mss()(display=_disp(n)) as m:
        shot = m.grab(m.monitors[1])
    return Image.frombytes("RGB", shot.size, shot.rgb)


def _grab_checked(n, action) -> Image.Image:
    try:
        return _grab(n)
    except Exception as e:
        if not _up(n):
            _fail_down(action, n)
        _fail(action, f"screen capture failed on {_disp(n)}: {e}", instance=n)


def _encode(img: Image.Image, fmt: str = "png", quality: int = 80) -> MCPImage:
    buf = io.BytesIO()
    if fmt == "jpeg":
        img.convert("RGB").save(buf, format="JPEG", quality=max(1, min(int(quality), 95)))
    else:
        img.save(buf, format="PNG")
    return MCPImage(data=buf.getvalue(), format=fmt)


def _changed_fraction(a: Image.Image, b: Image.Image) -> float:
    """Fraction of pixels that differ noticeably between two same-size frames."""
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 16 else 0)
    changed = diff.histogram()[255]
    return changed / float(a.width * a.height)


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


def _instance_rows():
    _register_existing()
    seen = set(INSTANCES) | {k for k in range(1, _SCAN_MAX + 1) if _up(k)}
    rows = []
    for n in sorted(seen):
        info = INSTANCES.get(n, {})
        rows.append({"instance": n, "display": _disp(n), "name": info.get("name", "?"),
                     "command": info.get("command", "(unknown)"), "up": _up(n),
                     "launched_here": n in _LAUNCHED})
    return rows


@contextmanager
def _launch_lock():
    """Serialize display-number allocation ACROSS server processes (each agent runs its own
    server), so two agents launching at once can't both pick :2."""
    path = os.path.join(HERE, "files", ".launch.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ---------- Chrome DevTools Protocol (file uploads bypass the broken native GTK picker) ----------
def _cdp_port(n) -> int:
    return 9222 + int(n)


def _cdp_set_file_input(port, filepath, selector, page_url_contains=""):
    """Attach a file to a page's <input type=file> over CDP (no native dialog, no renderer hang)."""
    import urllib.request
    import websocket
    # ProxyHandler({}) — never route the 127.0.0.1 CDP call through an http_proxy from the env.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    info = json.loads(opener.open(f"http://127.0.0.1:{port}/json", timeout=5).read().decode())
    pages = [t for t in info if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
             and not t.get("url", "").startswith("devtools://")]
    if page_url_contains:
        pages = [t for t in pages if page_url_contains in t.get("url", "")]
        if not pages:
            raise RuntimeError(f"no open tab whose URL contains '{page_url_contains}'")
    if not pages:
        raise RuntimeError(f"no CDP page target on 127.0.0.1:{port} — is Chromium started with --remote-debugging-port?")
    target = pages[0]
    # suppress_origin: Chrome rejects CDP WS handshakes that carry a non-allowlisted Origin header;
    # sending none sidesteps that without needing --remote-allow-origins on the browser.
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=10, suppress_origin=True)
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
    return target.get("url", "")


def _next_free() -> int:
    n = 2
    while _up(n) or n in INSTANCES:
        n += 1
    return n


def _kill_xephyr(n):
    """Kill instance n's Xephyr and WAIT for the display to go down — returning while the old
    server is still dying lets the next launch_app reuse :N and land on a corpse display.
    The pattern is anchored: an unanchored `pkill -f 'Xephyr :N '` also matches any shell/editor
    whose command line merely CONTAINS that string."""
    subprocess.run(["pkill", "-f", f"^Xephyr {_disp(n)} "], capture_output=True, timeout=5)
    for _ in range(10):
        if not _up(n):
            break
        time.sleep(0.25)
    _GEOM.pop(n, None)


def _forget(n):
    INSTANCES.pop(n, None)
    _LAST.pop(n, None)
    _GEOM.pop(n, None)
    _LAUNCHED.discard(n)


def _cleanup_on_exit():
    for n in sorted(_LAUNCHED):
        info = INSTANCES.get(n, {})
        for pid in (info.get("app_pid"), info.get("xephyr_pid")):
            if pid:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        _kill_xephyr(n)


if _truthy(os.environ.get("GLOVEBOX_CLEANUP_ON_EXIT", "")):
    atexit.register(_cleanup_on_exit)


# ---------- vision ----------
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


def _app_env_args(n, command):
    """Env + argv to launch a GUI `command` on instance n's display. Chromium-family browsers get
    X11 flags, a per-instance profile, and a CDP debug port; other GTK apps get D-Bus isolation so
    they render ON this display instead of forwarding their window to an app on the host."""
    appenv = _env(n)
    args = shlex.split(command)
    base = os.path.basename(args[0]) if args else ""
    if base in BROWSERS:
        appenv.pop("WAYLAND_DISPLAY", None)
        appenv["GDK_BACKEND"] = "x11"
        if "--ozone-platform" not in command:
            prof = os.path.join(HERE, "profiles", f"{base}-{n}")
            legacy = os.path.join(HERE, "profiles", f"chromium-{n}")   # pre-0.3 profile dir name
            if base != "chromium" and os.path.isdir(legacy) and not os.path.isdir(prof):
                prof = legacy
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


def _spawn_checked(action, n, args, appenv, logpath, fix_hint):
    """Start a GUI process, give it 0.6 s, and fail (with its stderr tail) if it already died —
    a 'launched' that drew nothing is the most expensive lie a computer-use server can tell."""
    try:
        with open(logpath, "wb") as lf:
            proc = subprocess.Popen(args, env=appenv, stdout=subprocess.DEVNULL, stderr=lf)
    except FileNotFoundError:
        return None, f"command not found: {args[0]}", fix_hint
    time.sleep(0.6)
    rc = proc.poll()
    if rc is not None and rc != 0:
        try:
            with open(logpath) as lf:
                tail = lf.read().strip().splitlines()[-1][:200]
        except (OSError, IndexError):
            tail = ""
        return proc, f"'{' '.join(args)}' exited immediately with code {rc}" + (f": {tail}" if tail else ""), None
    return proc, None, None


def _maximize_async(n):
    # maximize the app window to fill the display once it maps (handles slow-starting apps too)
    subprocess.Popen(
        ["bash", "-c",
         'for i in $(seq 1 25); do W=$(xdotool search --onlyvisible --maxdepth 4 "" 2>/dev/null | tail -1); '
         'if [ -n "$W" ]; then xdotool windowactivate "$W" 2>/dev/null; '
         'wmctrl -i -r "$W" -b add,maximized_vert,maximized_horz 2>/dev/null; break; fi; sleep 1; done'],
        env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- instance management ----------
@mcp.tool()
def launch_app(command: str, name: str = "", size: str = "1440x900") -> str:
    """Launch a GUI app in its OWN new Xephyr display/window and return its instance id.

    `command` is any shell command ('chromium', 'xterm', 'gimp', 'inkscape file.svg', …).
    Chromium-family browsers (chromium, google-chrome, brave, microsoft-edge, vivaldi, opera)
    automatically get X11 flags, a per-instance profile, downloads routed to files/<N>/, and a CDP
    debug port (needed by upload_file). Other GTK apps get D-Bus isolation so they render on this
    display, not the host.

    Returns {"ok", "instance", "display", "name", "files_dir", "width", "height", "warnings": [...]}
    — pass that instance id to every control tool. Fails (isError) if Xephyr can't start on the
    host display, the command doesn't exist, or the app exits immediately (its stderr tail is
    quoted; app_log(instance) has more). "warnings" is non-empty when the window manager (openbox)
    is missing — Xephyr then ignores pointer motion, so clicks would land in the screen center.
    Each instance has its own cursor, so separate agents can drive separate instances in parallel;
    display numbers are allocated under a cross-process lock, so parallel launches don't collide.
    Never share one instance between agents. The window appears on GLOVEBOX_HOST_DISPLAY
    (default :0). Give the app a moment to draw (wait_for_stable / settle_ms) before the first
    screenshot. Instances outlive this server process unless GLOVEBOX_CLEANUP_ON_EXIT=1."""
    with _LOCK, _launch_lock():
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
            try:
                xeph.kill()
                xeph.wait(timeout=3)
            except Exception:
                pass
            _fail("launch_app", f"Xephyr {_disp(n)} did not start",
                  fix=f"check that the host display {HOST} is reachable (GLOVEBOX_HOST_DISPLAY) and Xephyr is installed",
                  instance=n)
        INSTANCES[n] = {"name": name or command, "command": command, "size": size,
                        "xephyr_pid": xeph.pid, "app_pid": None}
        _LAUNCHED.add(n)
    warnings = []
    if shutil.which("openbox"):
        subprocess.Popen(["openbox"], env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        warnings.append("openbox (window manager) is not installed: Xephyr ignores xdotool pointer motion "
                        "without a WM, so clicks land in the screen center — install openbox")
    time.sleep(0.5)
    appenv, args, base = _app_env_args(n, command)
    app, err, fix = _spawn_checked("launch_app", n, args, appenv, _app_log(n),
                                   "use a command that exists in PATH (e.g. 'chromium', 'xmessage hi')")
    if err:
        _kill_xephyr(n)
        _forget(n)
        _fail("launch_app", err,
              fix=fix or "check that the command exists and its arguments are valid; try it manually with "
                         "DISPLAY set to the instance display; app_log() would show its stderr",
              instance=n)
    _maximize_async(n)
    INSTANCES[n].update(name=name or base, app_pid=app.pid)
    W, H = _geometry(n)
    return _ok("launch_app", n, name=INSTANCES[n]["name"], command=command,
               files_dir=_files_dir(n), width=W, height=H, warnings=warnings)


@mcp.tool()
def list_instances() -> str:
    """List all instances (running app windows): id, display, name, command, up, launched_here.

    Returns {"ok", "count", "instances": [{instance, display, name, command, up, launched_here}, …]}.
    Probes displays :1–:12 for sandboxes started outside this server (e.g. start-display.sh or
    another agent's server), so it also finds instances this process didn't launch (their
    name/command show as unknown, launched_here=false). Use it to discover what you can drive
    and to verify an instance is up before acting."""
    rows = _instance_rows()
    return _ok("list_instances", count=len(rows), instances=rows)


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
    with _LOCK:
        info = INSTANCES.get(n)
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
        if not _up(n):
            _forget(n)
            return _ok("close_instance", n, detail=f"instance {n} closed (display down)")
    _fail("close_instance", f"instance {n} is still up after SIGTERM",
          fix=f"retry close_instance({n}); as a last resort `pkill -f '^Xephyr {_disp(n)} '` on the host",
          instance=n)


# ---------- per-instance control ----------
@mcp.tool()
def get_screen_size(instance: int = 1) -> str:
    """Screen size of an instance + the active vision backend and server version.

    Returns {"ok", "instance", "width", "height", "vision", "version"}. Call it once before
    coordinate math — coordinates are absolute pixels, top-left origin, within THIS instance's
    display (0 <= x < width, 0 <= y < height; out-of-range coordinates are rejected). "vision"
    is the EFFECTIVE grounding backend: 'basic'/'local' → parse_screen + click_element;
    'none' → screenshot + reason about pixels yourself. Fails (isError) if the instance is down."""
    if not _up(instance):
        _fail_down("get_screen_size", instance)
    _GEOM.pop(int(instance), None)   # a resized Xephyr window changes the geometry — re-read
    W, H = _geometry(instance)
    return _ok("get_screen_size", instance, width=W, height=H, vision=VISION["effective"],
               version=__version__)


@mcp.tool()
def screenshot(instance: int = 1, region: str = "", scale: float = 1.0, format: str = "png",
               quality: int = 80):
    """Screenshot of an instance's display — the ONLY way to see the screen; always look before you act.

    Returns [json, image]: {"ok", "instance", "width", "height", "region", "scale", "format",
    "bytes"} plus the image. Options to cut payload or zoom:
      region="x,y,w,h"  crop to a rectangle (zoom into small text/icons; coordinates stay absolute —
                        add x,y back when you click), default = full display;
      scale=0.5         downscale (0.1–1.0) for cheap overview shots;
      format="jpeg"     smaller than PNG for photos/video, quality 1–95 (default png, lossless).
    For an element list with coordinates use parse_screen (basic/local vision). Prefer
    observe='screenshot' on action tools to act and see the result in one call. Fails (isError)
    if the instance is down, the region is outside the display, or the format is unknown."""
    fmt = (format or "png").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("png", "jpeg"):
        _fail("screenshot", f"unknown format '{format}'", fix="use format='png' or 'jpeg'", instance=instance)
    if not (0.1 <= float(scale) <= 1.0):
        _fail("screenshot", f"scale {scale} out of range", fix="use 0.1 <= scale <= 1.0", instance=instance)
    img = _grab_checked(instance, "screenshot")
    W, H = img.width, img.height
    _GEOM[int(instance)] = (W, H)
    box = None
    if region:
        try:
            x, y, w, h = [int(v) for v in region.split(",")]
            assert w > 0 and h > 0
        except (ValueError, AssertionError):
            _fail("screenshot", f"bad region '{region}'", fix="use region='x,y,w,h' with positive w,h", instance=instance)
        if x < 0 or y < 0 or x + w > W or y + h > H:
            _fail("screenshot", f"region {region} exceeds the {W}x{H} display",
                  fix=f"keep 0 <= x, x+w <= {W} and 0 <= y, y+h <= {H}", instance=instance, width=W, height=H)
        box = (x, y, x + w, y + h)
        img = img.crop(box)
    if float(scale) < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    im = _encode(img, fmt, quality)
    meta = _ok("screenshot", instance, width=W, height=H, region=list(box) if box else None,
               scale=float(scale), format=fmt, bytes=len(im.data),
               image_size=[img.width, img.height])
    return [meta, im]


def _do_parse(instance: int = 1, box_threshold: float = 0.05, granularity: str = "line") -> dict:
    mode = VISION["effective"]
    base = {"ok": True, "action": "parse_screen", "instance": int(instance), "vision": mode}
    if mode == "none":
        base["note"] = ("no local grounding — take screenshot(instance) and reason about pixel coords "
                        "yourself, then click(x, y, instance)."
                        + (f" (auto fallback: {VISION['reason']})" if VISION["fallback"] else ""))
        return base
    if not VISION["ready"]:
        _fail("parse_screen", f"vision backend '{mode}' was requested but is not usable: {VISION['reason']}",
              fix="run ./install.sh " + mode + " to set it up, or set GLOVEBOX_VISION=auto|none and restart "
                  "the server; status() shows the full vision diagnosis", instance=instance, vision=mode)
    if granularity not in ("line", "word"):
        _fail("parse_screen", f"unknown granularity '{granularity}'", fix="use granularity='line' or 'word'",
              instance=instance)
    img = _grab_checked(instance, "parse_screen")
    W, H = img.width, img.height
    _GEOM[int(instance)] = (W, H)
    ann = _annotated(instance)
    try:
        if mode == "basic":
            elems, boxes = vision.parse_tesseract(img, granularity)
            _annotate(img, elems, boxes, ann)
        else:
            elems, meta = _parse_local(img, box_threshold, ann, instance)
            base.update(meta)
    except vision.VisionError as e:
        _fail("parse_screen", e.error, fix=e.fix, instance=instance, vision=mode)
    _LAST[int(instance)] = {e["id"]: tuple(e["center"]) for e in elems}
    total = len(elems)
    truncated = total > PARSE_MAX
    if truncated:
        elems = elems[:PARSE_MAX]
    base.update(screen=[W, H], annotated=ann, count=total, truncated=truncated, elements=elems)
    if mode == "basic":
        base["granularity"] = granularity
    return base


def _observe(instance, mode, settle_ms, payload: dict):
    """After an action, optionally return the resulting state in the SAME call.
    mode: 'none' (json only), 'screenshot' (json + image), 'parse' (json incl. elements + image).
    Mode is validated by _validate_observe BEFORE the action runs."""
    settle_ms = int(settle_ms or 0)
    if settle_ms:
        waited = max(0, min(settle_ms, SETTLE_MAX_MS))
        time.sleep(waited / 1000.0)
        payload["settled_ms"] = waited
        if waited != settle_ms:
            payload["settle_clamped"] = True
    payload = {"ok": True, "action": payload.pop("action"), "instance": int(instance),
               "display": _disp(instance), **payload}
    if mode == "screenshot":
        img = _grab_checked(instance, payload["action"])
        payload["observed"] = "screenshot"
        return [json.dumps(payload, ensure_ascii=False), _encode(img)]
    if mode == "parse":
        pr = _do_parse(instance)
        for k in ("ok", "action", "instance"):
            pr.pop(k, None)
        payload["observed"] = "parse"
        payload["parse"] = pr
        out = [json.dumps(payload, ensure_ascii=False)]
        ann = _annotated(instance)
        if VISION["effective"] != "none" and os.path.exists(ann):
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
def parse_screen(instance: int = 1, box_threshold: float = 0.05, granularity: str = "line") -> str:
    """Detect on-screen elements and return them with ids + pixel-centers (vision grounding).

    Returns {"ok", "vision", "screen": [W,H], "annotated", "count", "truncated",
    "elements": [{id, type, label, interactive, center: [x,y]}, …]} — then click_element(id).
    Also saves a numbered Set-of-Mark image (path in "annotated").
    Backend = the effective GLOVEBOX_VISION: 'local' (OmniParser on the GPU, text + icons, ~2 s
    after a ~6 s first-call model load; served by ONE shared vision-service process per machine so
    parallel agents don't each load the models — "via": "service"|"in-process", "queued_ms" says
    how long the GPU queue was), 'basic' (tesseract, text only; granularity='line'
    merges words on one line into one target like "Sign in" — default — or 'word' for single
    words; the frame is OCR'd at 2× (GLOVEBOX_OCR_SCALE) because UI text is too small for
    tesseract at 1×; each element also carries "confidence" 0–100), 'none' (returns a note telling you to
    screenshot + reason about pixels yourself — this is normal, not an error; status() says why
    if 'auto' fell back to it). Fails (isError) with the fix named when an explicitly requested
    backend is not usable (OmniParser not installed, no/too-small GPU, GPU out of memory,
    tesseract missing its language data). Elements are capped at 300 per call ("truncated":
    true when more were detected). Ids are only valid until the screen changes — re-parse after
    navigation."""
    return json.dumps(_do_parse(instance, box_threshold, granularity), ensure_ascii=False)


@mcp.tool()
def click(x: int, y: int, instance: int = 1, button: int = 1, observe: str = "none", settle_ms: int = 0):
    """Click at absolute pixel coords (top-left origin) in an instance.

    Returns {"ok", "action", "instance", "detail"}. button: 1=left, 2=middle, 3=right.
    `observe`='screenshot'|'parse' additionally returns the resulting screen state in the SAME
    call (saves a round-trip); `settle_ms` (max 5000, "settle_clamped": true if you asked for
    more) waits for the UI to update first — use 400–1500 after anything that navigates or
    submits, or wait_for_stable() when you don't know how long. Fails (isError) if the instance
    is down or the coordinates are outside the display (nothing is clicked then)."""
    _validate_observe("click", instance, observe)
    _check_xy("click", instance, (x, y))
    _xdo(instance, "click", "mousemove", x, y, opts=("--sync",))
    _xdo(instance, "click", "click", button)
    return _observe(instance, observe, settle_ms,
                    {"action": "click", "detail": f"clicked ({x},{y}) button {button}"})


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, instance: int = 1, button: int = 1, steps: int = 10,
         observe: str = "none", settle_ms: int = 0):
    """Drag (mouse-down → move → up) from (x1,y1) to (x2,y2) — for drawing, selecting, sliders.

    Returns {"ok", "action", "instance", "detail", "steps"}. `steps` (default 10, 1–100) is the
    number of intermediate motion events: canvases, sliders and drag-and-drop targets usually
    need a few to register the drag (1 = jump straight to the end point). `observe`/`settle_ms`:
    see click(). Fails (isError) if the instance is down or either point is off-screen."""
    _validate_observe("drag", instance, observe)
    _check_xy("drag", instance, (x1, y1), (x2, y2))
    steps = max(1, min(int(steps), 100))
    _xdo(instance, "drag", "mousemove", x1, y1, opts=("--sync",))
    _xdo(instance, "drag", "mousedown", button)
    for i in range(1, steps + 1):
        xi = round(x1 + (x2 - x1) * i / steps)
        yi = round(y1 + (y2 - y1) * i / steps)
        _xdo(instance, "drag", "mousemove", xi, yi, opts=("--sync",))
        if steps > 1:
            time.sleep(0.01)
    _xdo(instance, "drag", "mouseup", button)
    return _observe(instance, observe, settle_ms,
                    {"action": "drag", "detail": f"dragged ({x1},{y1})->({x2},{y2})", "steps": steps})


@mcp.tool()
def double_click(x: int, y: int, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Double-click at absolute pixel coords in an instance (e.g. open a file icon, select a word).

    Returns {"ok", "action", "instance", "detail"}. `observe`/`settle_ms`: see click().
    Fails (isError) if the instance is down or the coordinates are off-screen."""
    _validate_observe("double_click", instance, observe)
    _check_xy("double_click", instance, (x, y))
    _xdo(instance, "double_click", "mousemove", x, y, opts=("--sync",))
    _xdo(instance, "double_click", "click", 1, opts=("--repeat", "2"))
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
    _xdo(instance, "click_element", "mousemove", x, y, opts=("--sync",))
    _xdo(instance, "click_element", "click", button)
    return _observe(instance, observe, settle_ms,
                    {"action": "click_element", "detail": f"clicked element {id} at ({x},{y})",
                     "element": id, "center": [x, y]})


def _xclip(n, action, *args, input=None):
    # Setting (-i) forks a daemon that owns the selection and inherits stdout — capturing it would
    # block until that daemon exits, so only reads (-o) capture output.
    capture = "-o" in args
    try:
        r = subprocess.run(["xclip", "-selection", "clipboard", *args], input=input, env=_env(n),
                           stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                           stderr=subprocess.PIPE if capture else subprocess.DEVNULL, timeout=5)
    except subprocess.TimeoutExpired:
        _fail(action, "xclip timed out — the clipboard owner on this display is not responding",
              fix="retry; if it persists the owning app is hung (screenshot it)", instance=n)
    return r


@mcp.tool()
def type_text(text: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Type into the focused field — unicode-safe. Click the field first to focus it.

    Returns {"ok", "action", "instance", "detail", "method": "xdotool"|"clipboard"}.
    Pure ASCII is typed via xdotool; text with any non-ASCII char (č/š/ž …) is inserted via the
    clipboard + ctrl+v, because xdotool's synthetic unicode keystrokes get silently dropped by
    some toolkits (e.g. Inkscape's GTK canvas) even though they work in browsers (that route
    leaves the text in the instance's clipboard — see clipboard()). Terminals that paste with
    shift+Insert instead of ctrl+v need press_keys after clipboard(action='set'). Fails (isError)
    rather than silently losing characters if xclip is missing for non-ASCII text.
    `observe`/`settle_ms`: see click()."""
    _validate_observe("type_text", instance, observe)
    if any(ord(c) > 127 for c in text):
        if not shutil.which("xclip"):
            _fail("type_text", "text contains non-ASCII characters but xclip is not installed — "
                               "typing it via xdotool would silently drop characters in some toolkits",
                  fix="install xclip (e.g. `sudo apt install xclip`) or send ASCII-only text",
                  instance=instance)
        r = _xclip(instance, "type_text", "-i", input=text.encode("utf-8"))
        if r.returncode != 0:
            if not _up(instance):
                _fail_down("type_text", instance)
            _fail("type_text", "xclip failed to set the clipboard", instance=instance)
        time.sleep(0.05)
        _xdo(instance, "type_text", "key", "ctrl+v", opts=("--clearmodifiers",))
        method = "clipboard"
    else:
        _xdo(instance, "type_text", "type", text, opts=("--clearmodifiers", "--delay", "15"))
        method = "xdotool"
    return _observe(instance, observe, settle_ms,
                    {"action": "type_text", "detail": f"typed {len(text)} chars", "method": method})


@mcp.tool()
def clipboard(action: str = "get", text: str = "", instance: int = 1) -> str:
    """Read or set the instance's clipboard — the cheapest way to get EXACT text out of an app.

    action='get' returns {"ok", "instance", "text", "length", "empty"}: select text in the app
    (click/drag, or press_keys('ctrl+a')) then press_keys('ctrl+c') and read it here — no OCR,
    no screenshot. "empty": true means nothing (or non-text data) is on the clipboard.
    action='set' puts `text` on the clipboard (then press_keys('ctrl+v') or 'shift+Insert' in
    terminals) and returns {"ok", "instance", "length"}. Each instance has its own clipboard
    (separate X display). Fails (isError) if xclip is missing or the instance is down."""
    if action not in ("get", "set"):
        _fail("clipboard", f"unknown action '{action}'", fix="use action='get' or 'set'", instance=instance)
    if not shutil.which("xclip"):
        _fail("clipboard", "xclip is not installed", fix="sudo apt install xclip", instance=instance)
    if not _up(instance):
        _fail_down("clipboard", instance)
    if action == "set":
        r = _xclip(instance, "clipboard", "-i", input=text.encode("utf-8"))
        if r.returncode != 0:
            _fail("clipboard", "xclip failed to set the clipboard", instance=instance)
        return _ok("clipboard", instance, set=True, length=len(text))
    r = _xclip(instance, "clipboard", "-o")
    if r.returncode != 0:
        # "target STRING not available" = nothing on the clipboard (or an image/non-text owner)
        return _ok("clipboard", instance, text="", length=0, empty=True,
                   note="clipboard is empty or holds non-text data")
    out = r.stdout.decode("utf-8", errors="replace")
    return _ok("clipboard", instance, text=out, length=len(out), empty=(out == ""))


@mcp.tool()
def upload_file(filepath: str, instance: int = 1, selector: str = "input[type=file]",
                page_url_contains: str = "", observe: str = "none", settle_ms: int = 0):
    """Attach a local file to a page's <input type=file> via Chrome DevTools Protocol — use this for
    ALL browser uploads (logos, images, docs).

    The sandbox's native GTK file picker is invisible to us AND hangs the renderer, so never click
    an upload button expecting a dialog — call this instead. Works on Chromium-family browsers
    started by launch_app / start-display.sh (they open a per-instance --remote-debugging-port,
    9222+N). `selector` targets the file input on the MAIN page (default = first file input).
    With several tabs open the first page target is used unless `page_url_contains` picks the
    tab by a URL fragment. If the site only inserts the <input> after you click its 'upload'
    control, click that first (it won't open a dialog we can see, but it wires up the input),
    then call this. Relative paths resolve against files/<N>/.
    Returns {"ok", "action", "instance", "detail", "page"}; fails (isError) with the reason if the
    file is missing, no CDP target answers, or the selector matches nothing.
    NATIVE (non-browser) apps: use open_file() or drive the app's own Open dialog — it IS visible
    here. `observe`/`settle_ms`: see click()."""
    _validate_observe("upload_file", instance, observe)
    p = filepath if os.path.isabs(filepath) else os.path.join(_files_dir(instance), filepath)
    if not os.path.exists(p):
        _fail("upload_file", f"file not found: {p}",
              fix="list_files(instance) shows the staging folder files/<N>/ — drop the file there or pass an absolute path that exists",
              instance=instance)
    try:
        page = _cdp_set_file_input(_cdp_port(instance), p, selector, page_url_contains)
    except Exception as e:
        _fail("upload_file", f"upload failed: {e}",
              fix="the browser must be a Chromium-family browser started by launch_app/start-display.sh (CDP port "
                  f"{_cdp_port(instance)}); if the page adds the <input> only after clicking its "
                  "upload control, click that control first, then retry",
              instance=instance)
    return _observe(instance, observe, settle_ms,
                    {"action": "upload_file", "page": page,
                     "detail": f"set '{selector}' = {os.path.basename(p)} via CDP"})


@mcp.tool()
def open_file(filepath: str, instance: int = 1, app: str = "") -> str:
    """Open a local file in an app ON an existing instance's display (NOT a new instance).

    If `app` is given, runs `<app> <filepath>` there (e.g. app='gimp'); otherwise xdg-open picks
    the default handler. Relative paths resolve against the instance's staging folder files/<N>/
    (see list_files). Returns {"ok", "action", "instance", "opened", "app"}; fails (isError) if
    the file or the app doesn't exist, or if the handler exits immediately (e.g. xdg-open finds
    no application for that file type — then pass app=). The app is started asynchronously —
    wait_for_stable()/screenshot after a moment to confirm it drew. For BROWSER <input type=file>
    uploads use upload_file() instead. Both paths get the same X11 + D-Bus handling as launch_app
    (so the app renders on this display, not in an already-running window on the host)."""
    if not _up(instance):
        _fail_down("open_file", instance)
    p = filepath if os.path.isabs(filepath) else os.path.join(_files_dir(instance), filepath)
    if not os.path.exists(p):
        _fail("open_file", f"file not found: {p}",
              fix="list_files(instance) shows the staging folder and its contents; drop the file there or pass an absolute path",
              instance=instance)
    appenv, args, _ = _app_env_args(instance, app or "xdg-open")
    args = args + [p]
    proc, err, fix = _spawn_checked("open_file", instance, args, appenv, _app_log(instance),
                                    "pass an app that exists in PATH (e.g. app='gimp') or omit app to use xdg-open")
    if err:
        if not app and "code 3" in err:
            fix = fix or "no default application is registered for this file type — pass app='<program>' explicitly"
        _fail("open_file", err,
              fix=fix or "check the app and file; app_log(instance) shows the handler's stderr",
              instance=instance)
    return _ok("open_file", instance, opened=p, app=app or "xdg-open", pid=proc.pid)


@mcp.tool()
def list_files(instance: int = 1) -> str:
    """The instance's staging folder (files/<N>/ under the install dir) and its contents.

    Returns {"ok", "instance", "dir", "count", "truncated", "files": [{name, bytes, modified,
    is_dir, partial}, …]} (capped at 200 entries, newest first). "partial": true marks an
    in-progress browser download (.crdownload/.part/.tmp) — wait for it to disappear before
    using the file. Drop files there (or reference any host path you can read) to open in apps via
    open_file() or an app's Open dialog; launch_app browser instances also download / 'save as'
    into it — call this after a download to confirm the file arrived. Browser uploads still go
    through upload_file()."""
    d = _files_dir(instance)
    rows = []
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            rows.append({"name": name, "bytes": st.st_size,
                         "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                         "is_dir": os.path.isdir(p),
                         "partial": name.endswith((".crdownload", ".part", ".tmp", ".download"))})
    except OSError:
        pass
    rows.sort(key=lambda r: r["modified"], reverse=True)
    total = len(rows)
    truncated = total > FILES_MAX
    if truncated:
        rows = rows[:FILES_MAX]
    return _ok("list_files", instance, dir=d, count=total, truncated=truncated, files=rows)


@mcp.tool()
def press_keys(keys: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Press a key, a combo, or a SEQUENCE (xdotool keysym syntax, space-separated for sequences):
    'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5', 'ctrl+a Delete', 'alt+F4', 'shift+Insert'.

    Returns {"ok", "action", "instance", "detail", "keys": [...]}. Fails (isError) on an invalid
    keysym (nothing is pressed), a dead instance, or a frozen display. Browser navigation:
    'ctrl+l' → type_text(url) → 'Return'. `observe`/`settle_ms`: see click()."""
    _validate_observe("press_keys", instance, observe)
    toks = keys.split()
    if not toks:
        _fail("press_keys", "no keys given", fix="pass e.g. 'Return' or 'ctrl+a Delete'", instance=instance)
    bad = [t for t in toks if t.startswith("-")]
    if bad:
        _fail("press_keys", f"'{bad[0]}' is not a key name — nothing was pressed",
              fix="keysyms never start with '-': use 'minus' for the - key, 'KP_Subtract' on the keypad",
              instance=instance)
    r = _xdo(instance, "press_keys", "key", *toks, opts=("--clearmodifiers", "--delay", "40"))
    err = r.stderr or ""
    # xdotool exits 0 on unknown keysyms ("No such key name … Ignoring it") and on option-like
    # tokens ("Invalid key sequence") — both are silent no-ops, so flag them
    if "No such key name" in err or "Invalid key sequence" in err:
        bad = [l.strip() for l in err.splitlines() if "key" in l][:2]
        _fail("press_keys", f"invalid key name in '{keys}' — xdotool ignored it: {' | '.join(bad)[:200]}",
              fix="use xdotool keysym syntax: 'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5'; sequences are space-separated",
              instance=instance)
    return _observe(instance, observe, settle_ms,
                    {"action": "press_keys", "detail": f"pressed {keys}", "keys": toks})


@mcp.tool()
def move_mouse(x: int, y: int, instance: int = 1) -> str:
    """Move the mouse pointer to absolute pixel coords in an instance (no click).

    Returns {"ok", "action", "instance", "detail"}. Useful for hover states (menus, tooltips) —
    follow with screenshot() to see the effect. Fails (isError) if the instance is down or the
    coordinates are off-screen."""
    _check_xy("move_mouse", instance, (x, y))
    _xdo(instance, "move_mouse", "mousemove", x, y, opts=("--sync",))
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
    btn = 4 if amount > 0 else 5
    _xdo(instance, "scroll", "click", btn, opts=("--repeat", str(min(abs(amount), 100)), "--delay", "20"))
    return _observe(instance, observe, settle_ms,
                    {"action": "scroll", "detail": f"scrolled {amount}"})


@mcp.tool()
def wait_ms(ms: int) -> str:
    """Wait (e.g. for a page to load or an app to draw) — capped at 10000 ms.

    Returns {"ok", "action", "requested_ms", "waited_ms", "clamped"} — "clamped": true means you
    asked for more than the cap and only waited_ms elapsed. Prefer wait_for_stable() (waits until
    the screen stops changing) or settle_ms on the action itself when you also want to observe
    the result in the same call."""
    ms = int(ms)
    waited = max(0, min(ms, WAIT_MAX_MS))
    time.sleep(waited / 1000)
    return _ok("wait_ms", requested_ms=ms, waited_ms=waited, clamped=waited != ms)


@mcp.tool()
def wait_for_stable(instance: int = 1, timeout_ms: int = 5000, interval_ms: int = 300,
                    threshold: float = 0.002, observe: str = "none"):
    """Wait until the screen stops changing (page loaded, app drew, animation ended) — replaces
    guessing a settle_ms.

    Samples the display every `interval_ms` (100–2000) and returns as soon as two consecutive
    frames differ by at most `threshold` (fraction of pixels, default 0.2%), or after `timeout_ms`
    (max 30000). Returns {"ok", "instance", "stable", "elapsed_ms", "frames", "change": last
    changed fraction, "timed_out"}. "stable": false with "timed_out": true means the screen was
    still changing (video, spinner, blinking cursor — raise the threshold to ~0.01 for those); it
    is reported, not hidden. `observe`='screenshot'|'parse' returns the final state in the same
    call. Fails (isError) if the instance is down."""
    _validate_observe("wait_for_stable", instance, observe)
    timeout_ms = max(0, min(int(timeout_ms), 30000))
    interval = max(100, min(int(interval_ms), 2000)) / 1000.0
    t0 = time.time()
    prev = _grab_checked(instance, "wait_for_stable")
    frames, change, stable = 1, 1.0, False
    while True:
        time.sleep(interval)
        cur = _grab_checked(instance, "wait_for_stable")
        frames += 1
        change = _changed_fraction(prev, cur)
        prev = cur
        if change <= float(threshold):
            stable = True
            break
        if (time.time() - t0) * 1000 >= timeout_ms:
            break
    elapsed = int((time.time() - t0) * 1000)
    return _observe(instance, observe, 0,
                    {"action": "wait_for_stable", "stable": stable, "elapsed_ms": elapsed, "frames": frames,
                     "change": round(change, 4), "timed_out": not stable})


@mcp.tool()
def window_info(instance: int = 1) -> str:
    """The active window's title + geometry and the list of visible windows on an instance — a cheap,
    image-free way to confirm navigation (browser titles follow the page) or that an app opened.

    Returns {"ok", "instance", "active": {id, title, x, y, width, height, pid} | null,
    "windows": [{id, title}, …] (visible, capped at 50), "count"}. "active" is null when no
    window has focus yet (fresh instance) — that is reported, not an error. Fails (isError) if
    the instance is down."""
    if not _up(instance):
        _fail_down("window_info", instance)
    active = None
    r = subprocess.run(["xdotool", "getactivewindow", "getwindowname", "getwindowgeometry", "--shell"],
                       env=_env(instance), capture_output=True, text=True, timeout=XDO_TIMEOUT)
    if r.returncode == 0 and r.stdout.strip():
        lines = r.stdout.splitlines()
        title = lines[0]
        kv = dict(l.split("=", 1) for l in lines[1:] if "=" in l)
        active = {"id": int(kv.get("WINDOW", 0)), "title": title, "x": int(kv.get("X", 0)), "y": int(kv.get("Y", 0)),
                  "width": int(kv.get("WIDTH", 0)), "height": int(kv.get("HEIGHT", 0)), "pid": None}
        rp = subprocess.run(["xdotool", "getwindowpid", str(active["id"])], env=_env(instance),
                            capture_output=True, text=True, timeout=XDO_TIMEOUT)
        if rp.returncode == 0 and rp.stdout.strip().isdigit():
            active["pid"] = int(rp.stdout.strip())
    rs = subprocess.run(["xdotool", "search", "--onlyvisible", "--maxdepth", "3", "--name", ".*"],
                        env=_env(instance), capture_output=True, text=True, timeout=XDO_TIMEOUT)
    ids = [w for w in rs.stdout.split() if w.isdigit()] if rs.returncode == 0 else []
    windows = []
    for w in ids[:WINDOWS_MAX]:
        rn = subprocess.run(["xdotool", "getwindowname", w], env=_env(instance), capture_output=True,
                            text=True, timeout=XDO_TIMEOUT)
        name = rn.stdout.strip() if rn.returncode == 0 else ""
        if name:
            windows.append({"id": int(w), "title": name})
    return _ok("window_info", instance, active=active, windows=windows, count=len(windows),
               truncated=len(ids) > WINDOWS_MAX)


@mcp.tool()
def app_log(instance: int = 1, lines: int = 50) -> str:
    """Tail of the stderr log of the app launched on an instance (launch_app / open_file) — read it
    when an app crashed, drew nothing, or misbehaves.

    Returns {"ok", "instance", "path", "lines": [...], "total_lines", "truncated"} (max 500 lines
    per call). The log is per instance and overwritten by each launch on that instance. Instance 1
    (start-display.sh) logs to /tmp/chromium-x1.log instead. Fails (isError) if no log exists for
    that instance yet."""
    p = _app_log(instance)
    if not os.path.exists(p):
        _fail("app_log", f"no app log for instance {instance} (nothing launched there by this server yet)",
              fix="launch_app()/open_file() write it; instance 1's browser logs to /tmp/chromium-x1.log",
              instance=instance)
    lines = max(1, min(int(lines), 500))
    with open(p, errors="replace") as fh:
        all_lines = fh.read().splitlines()
    tail = all_lines[-lines:]
    return _ok("app_log", instance, path=p, lines=tail, total_lines=len(all_lines),
               truncated=len(all_lines) > len(tail))


@mcp.tool()
def status() -> str:
    """Server + sandbox status in one read-only call — run this first, and when filing bug reports.

    Returns {"ok", "version", "host_display", "tmp_dir", "cleanup_on_exit",
    "vision": {requested, effective, ready, reason, fallback, gpu: {present, name, vram_total_mb,
    vram_free_mb, enough, note}, omniparser: {code, weights, torch, ready, reason, loaded,
    service: {running, pid, loaded, served, in_flight, idle_s, …} — the shared per-machine
    OmniParser process (one GPU load for all agents; starts on first use, idle-exits)},
    tesseract: {binary, pytesseract, langs, lang, ready, reason}},
    "instances": [...same as list_instances], "deps": {xdotool, xclip, xephyr, openbox, wmctrl,
    xdpyinfo, dbus_run_session, tesseract, nvidia_smi: true|false}}.
    "vision.effective" is the grounding backend actually in use (none|basic|local); when
    GLOVEBOX_VISION=auto it is the best READY one and "reason" says why (e.g. OmniParser weights
    missing, GPU too small). The GPU check is the same preflight parse_screen uses. No side effects."""
    gpu = vision.gpu_info()
    omni = vision.omniparser_info(gpu)
    omni["loaded"] = _OMNI.loaded
    if _OMNI.load_seconds is not None:
        omni["load_seconds"] = _OMNI.load_seconds
    svc = vision_service.ping() if USE_SERVICE else None
    omni["service"] = svc or {"running": False, "socket": vision_service.socket_path(),
                              "enabled": USE_SERVICE,
                              "note": "starts on the first local-mode parse_screen" if USE_SERVICE
                                      else "disabled (GLOVEBOX_VISION_SERVICE=0): models load per server process"}
    tess = vision.tesseract_info()
    deps = {k: bool(shutil.which(b)) for k, b in (
        ("xdotool", "xdotool"), ("xclip", "xclip"), ("xephyr", "Xephyr"), ("openbox", "openbox"),
        ("wmctrl", "wmctrl"), ("xdpyinfo", "xdpyinfo"), ("dbus_run_session", "dbus-run-session"),
        ("tesseract", "tesseract"), ("nvidia_smi", "nvidia-smi"))}
    return _ok("status", version=__version__, host_display=HOST, tmp_dir=TMP,
               cleanup_on_exit=_truthy(os.environ.get("GLOVEBOX_CLEANUP_ON_EXIT", "")),
               vision={**VISION, "gpu": gpu, "omniparser": omni, "tesseract": tess},
               instances=_instance_rows(), deps=deps)


if __name__ == "__main__":
    mcp.run()
