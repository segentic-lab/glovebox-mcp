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
  GLOVEBOX_CLIPBOARD_SYNC = 1                     (per-instance host bridge: clipboard text mirrored both ways
                                                   + keymap kept equal to the host's. See host_bridge.py)

Response contract: every tool returns JSON — {"ok": true, "action": ..., "instance": ..., ...} on
success. Failures raise ToolError, so the client sees isError=true with the same JSON shape
({"ok": false, "error": ..., "fix": ...}) embedded in the error text; "fix" names the call that
unblocks the agent. Image-returning tools (screenshot, observe='screenshot'|'parse') return
[json, image]. An action that DID happen is never reported as failed because the observation
after it failed: "observed": false + "observe_error" instead.

Concurrency: MCP SDK 2.x runs sync tools on worker threads, so calls can overlap. Every tool that
touches an instance's cursor, keyboard or clipboard runs under that instance's lock (one cursor =
one sequence at a time); different instances proceed in parallel. Module state (INSTANCES, _LAST,
_GEOM, _LAUNCHED) is guarded by _LOCK or is a single atomic dict operation.

Isolation is DISPLAY-level: the agent's input and vision are confined to the Xephyr window, but
the apps run as your user with your filesystem. For OS-level isolation run glovebox in a VM/container.
"""
import atexit
import base64
import binascii
import ctypes
import ctypes.util
import fcntl
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

os.environ.setdefault("DISPLAY", ":1")
HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("GLOVEBOX_HOST_DISPLAY", ":0")

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as MCPImage
import mss
from PIL import Image, ImageChops, ImageDraw

import host_bridge
import vision
import vision_service
from _paths import disp as _disp, display_up, tmp_dir
from _version import __version__

TMP = tmp_dir()   # annotated frames + app/Xephyr logs; 0700, ownership-checked (frames are screenshots)

mcp = MCPServer("glovebox", version=__version__,
                instructions="Sandboxed computer use: drive real apps inside nested X11 windows. Call status() "
                             "first; every tool returns JSON with ok/fix; see AGENTS.md in the repo.")

INSTANCES = {}   # N -> {name, command, size, xephyr_pid, app_pid}
_LAST = {}       # N -> {element id -> (cx, cy)} from that instance's last parse_screen
_GEOM = {}       # N -> (W, H, read_at) cached display geometry (bounds checks; refreshed after GEOM_TTL)
_LAUNCHED = set()  # instances started by THIS process (for optional cleanup on exit)
_LOCK = threading.RLock()
_ILOCKS = defaultdict(threading.RLock)   # per-instance: one cursor/keyboard/clipboard = one sequence at a time
_SCAN_MAX = 24   # highest display :N that list_instances probes for already-running sandboxes
INSTANCE_MAX = 255   # X display numbers; tests use :99
GEOM_TTL = 1.0   # seconds; the windows are resizeable, so bounds must not trust an old size
PARSE_MAX = 300  # parse_screen element cap per call (flagged via "truncated")
FILES_MAX = 200  # list_files entry cap per call (flagged via "truncated")
WINDOWS_MAX = 50  # window_info visible-window cap
CLIP_MAX = 100_000  # clipboard(get) characters per call (flagged via "truncated")
TEXT_MAX = 20_000   # browser(action='text') characters per call
PUT_MAX = 10 * 1024 * 1024   # put_file bytes per call
SETTLE_MAX_MS = 5000
WAIT_MAX_MS = 10000
SCROLL_MAX = 100
XDO_TIMEOUT = 20  # seconds; a frozen display must not hang a tool call forever
BROWSERS = ("chromium", "chromium-browser", "chrome", "google-chrome", "google-chrome-stable",
            "brave", "brave-browser", "microsoft-edge", "microsoft-edge-stable", "vivaldi",
            "vivaldi-stable", "opera")
MODIFIERS = {"ctrl", "control", "alt", "shift", "super", "meta", "hyper", "lock",
             "mod1", "mod2", "mod3", "mod4", "mod5"}

USE_SERVICE = os.environ.get("GLOVEBOX_VISION_SERVICE", "1").strip().lower() not in ("0", "false", "no", "off")
# Host <-> instance bridge: one detached process per instance mirrors the clipboard and keeps the
# keymap equal to the host's (see host_bridge.py). The env var keeps its historical name.
CLIP_SYNC = os.environ.get("GLOVEBOX_CLIPBOARD_SYNC", "1").strip().lower() not in ("0", "false", "no", "off")
_OMNI = vision.OmniParser()        # in-process fallback when the shared service is unavailable
_SERVICE_FAILED_AT = [0.0]         # back off re-spawn attempts after a failure
_VISION = {"at": 0.0, "value": None}
VISION_TTL = 20.0


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _vision() -> dict:
    """The effective backend, re-resolved every VISION_TTL seconds — installing OmniParser or
    freeing the GPU takes effect without a server restart (it used to be fixed at import)."""
    now = time.time()
    if _VISION["value"] is None or now - _VISION["at"] > VISION_TTL:
        svc = vision_service.ping() if USE_SERVICE else None
        _VISION["value"] = vision.resolve(service_loaded=bool(svc and svc.get("loaded")))
        _VISION["at"] = now
    return _VISION["value"]


def _parse_local(img, box_threshold, ann, instance):
    """OmniParser via the shared vision service (one GPU load per machine, see vision_service.py);
    falls back to loading the models in THIS process only when the service cannot be reached or
    started — a reachable-but-busy service is reported as such (VisionError), never duplicated.
    Returns (elements, meta): which path answered, how long it queued, why it fell back."""
    if USE_SERVICE and time.time() - _SERVICE_FAILED_AT[0] > 60:
        reason = None
        try:
            if vision_service.ensure_running() is not None:
                elems, meta = vision_service.parse(img, box_threshold, ann, instance)
                return elems, {"via": "service", **meta}
            reason = "the vision service could not be started (see vision-service.log in the temp dir)"
        except vision.VisionError:
            raise                      # a real parse/preflight/busy error from the service — report it
        except (OSError, ValueError, ConnectionError) as e:
            reason = f"the vision service is unreachable: {e!r}"
        _SERVICE_FAILED_AT[0] = time.time()
        print(f"[glovebox] {reason}; parsing in-process for 60 s", file=sys.stderr)
        elems = _OMNI.parse(img, box_threshold, ann)
        return elems, {"via": "in-process", "fallback_reason": reason, "detector": _OMNI.detector}
    elems = _OMNI.parse(img, box_threshold, ann)
    meta = {"via": "in-process", "detector": _OMNI.detector}
    if USE_SERVICE:
        meta["fallback_reason"] = "the vision service failed less than 60 s ago (retrying after that)"
    return elems, meta


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


def _error_payload(e: ToolError) -> dict:
    try:
        return json.loads(str(e))
    except ValueError:
        return {"error": str(e)}


# ---------- display / process helpers ----------
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


def _xephyr_log(n) -> str:
    return os.path.join(TMP, f"xephyr_{int(n)}.log")


def _annotated(n) -> str:
    return os.path.join(TMP, f"annotated_{int(n)}.png")


def _up(n) -> bool:
    return bool(display_up(n))


def _instance_down_fix(n) -> str:
    return ("list_instances() shows what is running; launch_app() starts a new instance"
            + (" (instance 1 is started by ./start-display.sh on the host)" if int(n) == 1 else ""))


def _fail_down(action, n):
    _fail(action, f"instance {n} is not running (display {_disp(n)} unreachable)",
          fix=_instance_down_fix(n), instance=n)


def _require(action, n, up: bool = True) -> int:
    """Validate an instance id at the top of every per-instance tool: an integer in
    1..INSTANCE_MAX, never the host display (instance=0 used to drive the real desktop), and —
    unless up=False — a display that answers. Returns the int."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        _fail(action, f"instance must be an integer, got {n!r}", fix="pass the id launch_app/list_instances returned")
    if n < 1 or n > INSTANCE_MAX:
        _fail(action, f"instance {n} is out of range 1..{INSTANCE_MAX}",
              fix="instances are 1 (start-display.sh) or the ids launch_app returns; the host display is never driven")
    if _disp(n) == HOST:
        _fail(action, f"instance {n} is the host display {HOST} — glovebox never drives the host desktop",
              fix="use an instance from list_instances() or launch_app()", instance=n)
    if up:
        st = display_up(n)
        if st is None:
            _fail(action, "xdpyinfo is not installed — displays cannot be probed", fix="sudo apt install x11-utils")
        if not st:
            _fail_down(action, n)
    return n


@contextmanager
def _ilock(n):
    with _LOCK:
        lk = _ILOCKS[int(n)]
    with lk:
        yield


def _run(action, n, cmd, timeout=XDO_TIMEOUT, env=None, **kw):
    """subprocess.run that turns a missing binary or a hang into a structured error instead of a
    raw exception (every tool used to leak FileNotFoundError when xdotool/xclip/pkill was absent)."""
    try:
        return subprocess.run(cmd, env=env if env is not None else _env(n), capture_output=True,
                              timeout=timeout, **kw)
    except FileNotFoundError:
        _fail(action, f"{cmd[0]} is not installed", fix="install it (./install.sh lists the packages); status().deps shows what is present",
              instance=n)
    except subprocess.TimeoutExpired:
        _fail(action, f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} timed out after {timeout} s on {_disp(n)} — the display or app is frozen",
              fix=f"screenshot({n}) to check; close_instance({n}) and launch_app() again if it stays frozen",
              instance=n)


def _xdo(n, action, sub, *args, opts=(), timeout=XDO_TIMEOUT):
    """Run `xdotool <sub> <opts> -- <args>` against instance n's display. The `--` keeps
    user-supplied values (negative numbers, '-h') from being parsed as xdotool options.
    On failure raise a structured error (a dead display must never come back as a silent success)."""
    cmd = ["xdotool", sub, *opts]
    if args:
        cmd += ["--", *[str(a) for a in args]]
    r = _run(action, n, cmd, timeout=timeout, text=True)
    if r.returncode != 0:
        if not _up(n):
            _fail_down(action, n)
        _fail(action, f"xdotool {sub} failed: {(r.stderr or r.stdout).strip()[:300] or 'unknown error'}",
              instance=n)
    return r


def _xdo_soft(n, action, *cmd):
    """xdotool whose non-zero exit is an answer, not an error (e.g. 'no active window')."""
    return _run(action, n, ["xdotool", *cmd], text=True)


def _geometry(n) -> tuple:
    """(W, H) of the instance display, cached for GEOM_TTL seconds (the windows are resizeable)."""
    n = int(n)
    g = _GEOM.get(n)
    if g is None or time.time() - g[2] > GEOM_TTL:
        r = _xdo(n, "get_screen_size", "getdisplaygeometry")
        w, h = r.stdout.split()[:2]
        g = (int(w), int(h), time.time())
        _GEOM[n] = g
    return g[0], g[1]


def _set_geometry(n, W, H):
    _GEOM[int(n)] = (W, H, time.time())


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
        img = _grab(n)
    except Exception as e:
        if not _up(n):
            _fail_down(action, n)
        _fail(action, f"screen capture failed on {_disp(n)}: {e}", instance=n)
    _set_geometry(n, img.width, img.height)
    return img


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


def _reap(proc):
    """Wait for a detached child in the background so it never lingers as a zombie."""
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


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


def _bridge_host(n) -> tuple:
    """Make instance n usable by a PERSON too: copy the host keymap (AltGr characters) and start
    the clipboard bridge. Returns (keyboard dict, clipboard_sync bool, warnings list). Never
    raises — a failed bridge is reported, the instance is still usable by the agent."""
    warnings = []
    try:
        kb = host_bridge.apply_keymap(_disp(n), HOST, _host_env())
    except Exception as e:   # noqa: BLE001 — bridging must never take the launch down
        kb = {"method": "xephyr-default", "host_layout": "", "verified": False,
              "warning": f"keymap copy failed: {e!r}"}
    if kb.get("warning"):
        warnings.append(kb["warning"])
    clip = False
    if CLIP_SYNC:
        if not shutil.which("xclip"):
            warnings.append("xclip is not installed: no clipboard sync with the host (and no unicode type_text)")
        else:
            try:
                clip = host_bridge.ensure_running(n, HOST, _host_env())
                if not clip:
                    warnings.append(f"the host bridge did not start for {_disp(n)} (no clipboard sync); "
                                    f"see {host_bridge.log_path(n)}")
            except Exception as e:   # noqa: BLE001
                warnings.append(f"the host bridge failed to start: {e!r}")
    return kb, clip, warnings


def _register_existing():
    if 1 not in INSTANCES and _disp(1) != HOST and _up(1):
        INSTANCES[1] = {"name": "sandbox", "command": "(start-display.sh)", "size": "?",
                        "xephyr_pid": None, "app_pid": None}
        _files_dir(1)
        _bridge_host(1)   # idempotent: the keymap copy is exact, the bridge is lock-guarded


def _title(n) -> str:
    r = _xdo_soft(n, "list_instances", "getactivewindow", "getwindowname")
    return r.stdout.strip() if r.returncode == 0 else ""


def _instance_rows():
    """Every instance that answers on :1..:_SCAN_MAX plus the ones this process knows. Entries
    whose display has gone (the person closed the window) are forgotten here, so their numbers
    can be reused and list_instances never shows a corpse as 'known'."""
    _register_existing()
    with _LOCK:
        for n in [k for k in INSTANCES if not _up(k)]:
            _forget(n)
        seen = set(INSTANCES) | {k for k in range(1, _SCAN_MAX + 1) if _disp(k) != HOST and _up(k)}
        rows = []
        for n in sorted(seen):
            info = INSTANCES.get(n, {})
            rows.append({"instance": n, "display": _disp(n), "name": info.get("name", "?"),
                         "command": info.get("command", "(unknown)"), "up": True,
                         "title": _title(n), "launched_here": n in _LAUNCHED,
                         "clipboard_sync": host_bridge.running(n)})
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


def _cdp_pages(port):
    import urllib.request
    # ProxyHandler({}) — never route the 127.0.0.1 CDP call through an http_proxy from the env.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    info = json.loads(opener.open(f"http://127.0.0.1:{port}/json", timeout=5).read().decode())
    return [t for t in info if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            and not t.get("url", "").startswith("devtools://")]


@contextmanager
def _cdp(port, page_url_contains=""):
    """A CDP session on one page target of the browser listening on 127.0.0.1:port. Yields
    (cmd, target, pages): cmd(method, params) -> result. Targets are listed most-recently-active
    first, so the first one is the tab being looked at unless `page_url_contains` picks."""
    import websocket
    pages = _cdp_pages(port)
    if page_url_contains:
        pages = [t for t in pages if page_url_contains in t.get("url", "")]
        if not pages:
            raise RuntimeError(f"no open tab whose URL contains '{page_url_contains}'")
    if not pages:
        raise RuntimeError(f"no CDP page target on 127.0.0.1:{port} — is Chromium started with --remote-debugging-port?")
    target = pages[0]
    # suppress_origin: Chrome rejects CDP WS handshakes that carry a non-allowlisted Origin header;
    # sending none sidesteps that without opening the port to every origin (--remote-allow-origins).
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=15, suppress_origin=True)
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
        yield cmd, target, pages
    finally:
        ws.close()


def _cdp_set_file_input(port, filepath, selector, page_url_contains=""):
    """Attach a file to a page's <input type=file> over CDP (no native dialog, no renderer hang)."""
    with _cdp(port, page_url_contains) as (cmd, target, _pages):
        cmd("DOM.enable")
        root = cmd("DOM.getDocument", {"depth": 1})["root"]["nodeId"]
        nid = cmd("DOM.querySelector", {"nodeId": root, "selector": selector}).get("nodeId")
        if not nid:
            raise RuntimeError(f"no element matches selector '{selector}' on the page")
        cmd("DOM.setFileInputFiles", {"files": [os.path.abspath(filepath)], "nodeId": nid})
        return target.get("url", "")


def _next_free() -> int:
    n = 2
    while _disp(n) == HOST or _up(n) or (n in INSTANCES and _up(n)):
        n += 1
    return n


def _kill_xephyr(n):
    """Kill instance n's Xephyr and WAIT for the display to go down — returning while the old
    server is still dying lets the next launch_app reuse :N and land on a corpse display.
    The pattern is anchored: an unanchored `pkill -f 'Xephyr :N '` also matches any shell/editor
    whose command line merely CONTAINS that string."""
    try:
        subprocess.run(["pkill", "-f", f"^Xephyr {_disp(n)} "], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for _ in range(10):
        if not _up(n):
            break
        time.sleep(0.25)
    _GEOM.pop(int(n), None)


def _forget(n):
    INSTANCES.pop(n, None)
    _LAST.pop(n, None)
    _GEOM.pop(n, None)
    _LAUNCHED.discard(n)


def _terminate(info: dict):
    """SIGTERM the app and Xephyr pids recorded at launch — only while they still are those
    processes (a recycled pid must not hit an unrelated process)."""
    for pid, needle in ((info.get("app_pid"), None), (info.get("xephyr_pid"), b"Xephyr")):
        if not pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read()
            if needle and needle not in cmdline:
                continue
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _close(n) -> bool:
    """Kill app + Xephyr of instance n; True when the display is down afterwards."""
    info = INSTANCES.get(n)
    if info:
        _terminate(info)
    _kill_xephyr(n)
    if _up(n):
        return False
    _forget(n)
    return True


def _cleanup_on_exit(*_):
    for n in sorted(_LAUNCHED):
        _close(n)


if _truthy(os.environ.get("GLOVEBOX_CLEANUP_ON_EXIT", "")):
    atexit.register(_cleanup_on_exit)

    def _on_signal(signum, frame):
        _cleanup_on_exit()
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass


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
    they render ON this display instead of forwarding their window to an app on the host.
    Raises ValueError for an unparsable/empty command (checked BEFORE a display is started)."""
    appenv = _env(n)
    args = shlex.split(command)
    if not args:
        raise ValueError("empty command")
    base = os.path.basename(args[0])
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
            # the CDP port stays loopback-only; we connect without an Origin header, so the
            # browser does not need --remote-allow-origins=* (which lets ANY local page drive it)
            args += ["--ozone-platform=x11", "--disable-gpu", "--no-first-run",
                     "--no-default-browser-check", "--start-maximized",
                     f"--remote-debugging-port={_cdp_port(n)}", f"--user-data-dir={prof}"]
    else:
        appenv.pop("WAYLAND_DISPLAY", None)
        appenv.setdefault("GDK_BACKEND", "x11")
        if shutil.which("dbus-run-session"):
            args = ["dbus-run-session", "--"] + args
    return appenv, args, base


def _spawn_checked(n, args, appenv, logpath, fix_hint, append=False):
    """Start a GUI process, give it 0.6 s, and fail (with its stderr tail) if it already died —
    a 'launched' that drew nothing is the most expensive lie a computer-use server can tell.
    Returns (proc, error, fix); error is None on success."""
    real = args[2] if args[:2] == ["dbus-run-session", "--"] and len(args) > 2 else args[0]
    if not shutil.which(real):     # checked here: behind dbus-run-session the exec failure would be code 1
        return None, f"command not found: {real}", fix_hint
    try:
        with open(logpath, "ab" if append else "wb") as lf:
            proc = subprocess.Popen(args, env=appenv, stdout=subprocess.DEVNULL, stderr=lf)
    except FileNotFoundError:
        return None, f"command not found: {args[0]}", fix_hint
    except OSError as e:
        return None, f"could not start '{args[0]}': {e}", fix_hint
    time.sleep(0.6)
    rc = proc.poll()
    if rc is not None and rc != 0:
        try:
            with open(logpath, errors="replace") as lf:
                tail = lf.read().strip().splitlines()[-1][:200]
        except (OSError, IndexError):
            tail = ""
        return proc, f"'{' '.join(args)}' exited immediately with code {rc}" + (f": {tail}" if tail else ""), None
    _reap(proc)
    return proc, None, None


def _maximize_async(n):
    # maximize the app window to fill the display once it maps (handles slow-starting apps too)
    _reap(subprocess.Popen(
        ["bash", "-c",
         'for i in $(seq 1 25); do W=$(xdotool search --onlyvisible --maxdepth 4 "" 2>/dev/null | tail -1); '
         'if [ -n "$W" ]; then xdotool windowactivate "$W" 2>/dev/null; '
         'wmctrl -i -r "$W" -b add,maximized_vert,maximized_horz 2>/dev/null; break; fi; sleep 1; done'],
        env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def _resolve_path(action, n, filepath):
    """Relative paths resolve against the instance's staging folder files/<N>/; must exist."""
    p = filepath if os.path.isabs(filepath) else os.path.join(_files_dir(n), filepath)
    if not os.path.exists(p):
        _fail(action, f"file not found: {p}",
              fix="list_files(instance) shows the staging folder files/<N>/ — drop the file there or pass an absolute path that exists",
              instance=n)
    return p


# ---------- instance management ----------
@mcp.tool()
def launch_app(command: str, name: str = "", size: str = "1440x900") -> str:
    """Launch a GUI app in its OWN new Xephyr display/window and return its instance id.

    `command` is any shell command ('chromium', 'xterm', 'gimp', 'inkscape file.svg', …).
    Chromium-family browsers (chromium, google-chrome, brave, microsoft-edge, vivaldi, opera)
    automatically get X11 flags, a per-instance profile, downloads routed to files/<N>/, and a CDP
    debug port (needed by upload_file and browser()). Other GTK apps get D-Bus isolation so they
    render on this display, not the host.

    Returns {"ok", "instance", "display", "name", "files_dir", "width", "height", "warnings": [...],
    "keyboard": {method, host_layout, verified}, "clipboard_sync": bool} — pass that instance id to every
    control tool. "keyboard" says how the window got the host's keyboard layout ("host-keymap" =
    exact copy, verified against the host; Xephyr alone maps AltGr as plain Alt, so a person could
    not type @ { } [ ] on Slovenian/German/… layouts); "clipboard_sync" is true when the per-instance
    host bridge runs (it mirrors text between the host and instance clipboards and keeps the
    keymap equal to the host's; GLOVEBOX_CLIPBOARD_SYNC=0 disables it — note that whatever the
    person copies on the host then becomes readable inside the sandbox). Neither affects the
    agent's own type_text/clipboard. Fails (isError) BEFORE anything starts if the command is
    empty/unparsable or `size` is not WxH; after that if Xephyr can't start on the host display
    (its log tail is quoted), the command doesn't exist, or the app exits immediately (its stderr
    tail is quoted; app_log(instance) has more) — the display is closed again in those cases.
    "warnings" lists what degraded: window manager (openbox) missing (Xephyr then ignores pointer
    motion, so clicks would land in the screen center), keymap copy or clipboard bridge failed.
    Each instance has its own cursor, so separate agents can drive separate instances in parallel;
    display numbers are allocated under a cross-process lock, so parallel launches don't collide.
    Never share one instance between agents. The window appears on GLOVEBOX_HOST_DISPLAY
    (default :0). Give the app a moment to draw (wait_for_stable / settle_ms) before the first
    screenshot. Instances outlive this server process unless GLOVEBOX_CLEANUP_ON_EXIT=1."""
    if not re.fullmatch(r"\d{2,5}x\d{2,5}", size or ""):
        _fail("launch_app", f"bad size '{size}' — nothing was started", fix="use size='WIDTHxHEIGHT', e.g. '1440x900'")
    try:
        if not shlex.split(command):
            raise ValueError("empty command")
    except ValueError as e:
        _fail("launch_app", f"cannot parse command {command!r}: {e} — nothing was started",
              fix="pass a shell-quoted command such as 'chromium' or \"inkscape 'my file.svg'\"")
    with _LOCK, _launch_lock():
        _register_existing()
        n = _next_free()
        try:
            with open(_xephyr_log(n), "wb") as lf:
                xeph = subprocess.Popen(
                    ["Xephyr", _disp(n), "-screen", size, "-resizeable", "-ac",
                     "-title", f"Glovebox {_disp(n)} — {name or command[:28]}"],
                    env=_host_env(), stdout=subprocess.DEVNULL, stderr=lf)
        except FileNotFoundError:
            _fail("launch_app", "Xephyr is not installed", fix="sudo apt install xserver-xephyr (./install.sh does it)")
        except OSError as e:
            _fail("launch_app", f"could not start Xephyr: {e}", fix="check the host display and permissions")
        _reap(xeph)
        for _ in range(40):
            if _up(n):
                break
            if xeph.poll() is not None:
                break
            time.sleep(0.25)
        if not _up(n):
            try:
                xeph.kill()
            except OSError:
                pass
            try:
                with open(_xephyr_log(n), errors="replace") as lf:
                    tail = " | ".join(lf.read().strip().splitlines()[-3:])[:300]
            except OSError:
                tail = ""
            _fail("launch_app", f"Xephyr {_disp(n)} did not start" + (f": {tail}" if tail else ""),
                  fix=f"check that the host display {HOST} is reachable (GLOVEBOX_HOST_DISPLAY) and the size "
                      f"is valid; the full log is {_xephyr_log(n)}",
                  instance=n)
        INSTANCES[n] = {"name": name or command, "command": command, "size": size,
                        "xephyr_pid": xeph.pid, "app_pid": None}
        _LAUNCHED.add(n)
    try:
        warnings = []
        if shutil.which("openbox"):
            _reap(subprocess.Popen(["openbox"], env=_env(n), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        else:
            warnings.append("openbox (window manager) is not installed: Xephyr ignores xdotool pointer motion "
                            "without a WM, so clicks land in the screen center — install openbox")
        time.sleep(0.5)
        keyboard, clip_sync, bridge_warnings = _bridge_host(n)
        warnings += bridge_warnings
        appenv, args, base = _app_env_args(n, command)
        app, err, fix = _spawn_checked(n, args, appenv, _app_log(n),
                                       "use a command that exists in PATH (e.g. 'chromium', 'xmessage hi')")
        if err:
            _close(n)
            _fail("launch_app", err,
                  fix=fix or "check that the command exists and its arguments are valid; try it manually with "
                             "DISPLAY set to the instance display; app_log() would show its stderr",
                  instance=n)
        _maximize_async(n)
        INSTANCES[n].update(name=name or base, app_pid=app.pid)
        W, H = _geometry(n)
    except ToolError:
        raise
    except Exception as e:   # noqa: BLE001 — never leave a live display behind a raw exception
        _close(n)
        _fail("launch_app", f"launch failed after the display started (closed again): {e!r}",
              fix="retry; if it persists report status() output", instance=n)
    return _ok("launch_app", n, name=INSTANCES[n]["name"], command=command,
               files_dir=_files_dir(n), width=W, height=H, warnings=warnings,
               keyboard=keyboard, clipboard_sync=clip_sync)


@mcp.tool()
def list_instances() -> str:
    """List all running instances (app windows): id, display, name, command, title, launched_here.

    Returns {"ok", "count", "instances": [{instance, display, name, command, up, title,
    launched_here, clipboard_sync}, …]}. "title" is the active window's title (a browser's
    follows the page) — the way to tell instances apart that another agent's server launched
    (their name/command show as unknown, launched_here=false). Probes displays :1–:24 for
    sandboxes started outside this server (start-display.sh, other agents). Instances whose
    window was closed by hand disappear from the list and their number is reused."""
    rows = _instance_rows()
    return _ok("list_instances", count=len(rows), instances=rows)


@mcp.tool()
def close_instance(instance: int = 0, all_launched: bool = False) -> str:
    """Close an instance: kill its app + its Xephyr display, and verify it actually went down.

    Returns {"ok", "instance", "detail"} only after the display stops responding. With
    all_launched=true closes EVERY instance this server process launched (instance is ignored)
    and returns {"ok", "closed": [...], "failed": [...]} — "ok" is false if any survived.
    Fails (isError) if the instance isn't running, if it survives SIGTERM, or for instance 1 —
    the main start-display.sh sandbox is protected (stop it from the host with `pkill Xephyr`).
    Closing an instance another agent launched is allowed but visible to it (launched_here)."""
    if all_launched:
        closed, failed = [], []
        with _LOCK:
            for n in sorted(_LAUNCHED):
                (closed if _close(n) else failed).append(n)
        if failed:
            _fail("close_instance", f"instances {failed} are still up after SIGTERM",
                  fix="retry close_instance(all_launched=true); as a last resort pkill -f '^Xephyr :N ' on the host",
                  closed=closed, failed=failed)
        return _ok("close_instance", closed=closed, failed=[], detail=f"closed {len(closed)} instance(s)")
    n = _require("close_instance", instance, up=False)
    if n == 1:
        _fail("close_instance", "refusing to close instance 1 (the main start-display.sh sandbox)",
              fix="stop it from the host with `pkill Xephyr`; other instances close by id", instance=1)
    with _LOCK:
        if n not in INSTANCES and not _up(n):
            _fail("close_instance", f"instance {n} is not running — nothing to close",
                  fix="list_instances() shows what is running", instance=n)
        if _close(n):
            return _ok("close_instance", n, detail=f"instance {n} closed (display down)")
    _fail("close_instance", f"instance {n} is still up after SIGTERM",
          fix=f"retry close_instance({n}); as a last resort `pkill -f '^Xephyr {_disp(n)} '` on the host",
          instance=n)


@mcp.tool()
def start_app(command: str, instance: int = 1) -> str:
    """Start ANOTHER app on an existing instance's display (same window/cursor) — e.g. a second
    tool next to the browser, or a restart of an app that quit. For a new, separate window use
    launch_app; to open a file in an app use open_file.

    Returns {"ok", "instance", "command", "pid"}; fails (isError) if the instance is down, the
    command is unparsable/missing, or the app exits immediately (stderr tail quoted;
    app_log(instance) has more — logs of apps started here are appended to it). Browsers get the
    same X11/profile/CDP handling as launch_app. The app starts asynchronously: wait_for_stable()
    or screenshot() after a moment to confirm it drew."""
    n = _require("start_app", instance)
    try:
        appenv, args, base = _app_env_args(n, command)
    except ValueError as e:
        _fail("start_app", f"cannot parse command {command!r}: {e} — nothing was started",
              fix="pass a shell-quoted command such as 'gimp' or \"inkscape 'my file.svg'\"", instance=n)
    proc, err, fix = _spawn_checked(n, args, appenv, _app_log(n),
                                    "use a command that exists in PATH (e.g. 'gimp', 'xmessage hi')", append=True)
    if err:
        _fail("start_app", err, fix=fix or "check the command; app_log(instance) shows its stderr", instance=n)
    return _ok("start_app", n, command=command, pid=proc.pid)


# ---------- per-instance control ----------
@mcp.tool()
def get_screen_size(instance: int = 1) -> str:
    """Screen size of an instance + the active vision backend and server version.

    Returns {"ok", "instance", "width", "height", "vision", "version"}. Call it once before
    coordinate math — coordinates are absolute pixels, top-left origin, within THIS instance's
    display (0 <= x < width, 0 <= y < height; out-of-range coordinates are rejected). "vision"
    is the EFFECTIVE grounding backend: 'basic'/'local' → parse_screen + click_element;
    'none' → screenshot + reason about pixels yourself. Fails (isError) if the instance is down."""
    n = _require("get_screen_size", instance)
    _GEOM.pop(n, None)   # a resized Xephyr window changes the geometry — re-read
    W, H = _geometry(n)
    return _ok("get_screen_size", n, width=W, height=H, vision=_vision()["effective"],
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
    n = _require("screenshot", instance)
    fmt = (format or "png").lower()
    if fmt == "jpg":
        fmt = "jpeg"
    if fmt not in ("png", "jpeg"):
        _fail("screenshot", f"unknown format '{format}'", fix="use format='png' or 'jpeg'", instance=n)
    if not (0.1 <= float(scale) <= 1.0):
        _fail("screenshot", f"scale {scale} out of range", fix="use 0.1 <= scale <= 1.0", instance=n)
    img = _grab_checked(n, "screenshot")
    W, H = img.width, img.height
    box = None
    if region:
        try:
            x, y, w, h = [int(v) for v in region.split(",")]
            assert w > 0 and h > 0
        except (ValueError, AssertionError):
            _fail("screenshot", f"bad region '{region}'", fix="use region='x,y,w,h' with positive w,h", instance=n)
        if x < 0 or y < 0 or x + w > W or y + h > H:
            _fail("screenshot", f"region {region} exceeds the {W}x{H} display",
                  fix=f"keep 0 <= x, x+w <= {W} and 0 <= y, y+h <= {H}", instance=n, width=W, height=H)
        box = (x, y, x + w, y + h)
        img = img.crop(box)
    if float(scale) < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    im = _encode(img, fmt, quality)
    meta = _ok("screenshot", n, width=W, height=H, region=list(box) if box else None,
               scale=float(scale), format=fmt, bytes=len(im.data),
               image_size=[img.width, img.height])
    return [meta, im]


def _do_parse(instance: int = 1, box_threshold: float = 0.05, granularity: str = "line") -> dict:
    """One parse of an instance's screen → the parse_screen payload (dict). Runs under the
    instance lock: the annotated image and _LAST ids belong to one parse at a time."""
    n = _require("parse_screen", instance)
    v = _vision()
    mode = v["effective"]
    base = {"ok": True, "action": "parse_screen", "instance": n, "vision": mode}
    if mode == "none":
        base["note"] = ("no local grounding — take screenshot(instance) and reason about pixel coords "
                        "yourself, then click(x, y, instance)."
                        + (f" (auto fallback: {v['reason']})" if v["fallback"] else ""))
        return base
    if not v["ready"]:
        _fail("parse_screen", f"vision backend '{mode}' was requested but is not usable: {v['reason']}",
              fix="run ./install.sh " + mode + " to set it up, or set GLOVEBOX_VISION=auto|none and restart "
                  "the server; status() shows the full vision diagnosis", instance=n, vision=mode)
    if granularity not in ("line", "word"):
        _fail("parse_screen", f"unknown granularity '{granularity}'", fix="use granularity='line' or 'word'",
              instance=n)
    with _ilock(n):
        img = _grab_checked(n, "parse_screen")
        W, H = img.width, img.height
        ann = _annotated(n)
        try:
            if mode == "basic":
                elems, boxes = vision.parse_tesseract(img, granularity)
                _annotate(img, elems, boxes, ann)
            else:
                elems, meta = _parse_local(img, box_threshold, ann, n)
                base.update(meta)
        except vision.VisionError as e:
            _fail("parse_screen", e.error, fix=e.fix, instance=n, vision=mode)
        _LAST[n] = {e["id"]: tuple(e["center"]) for e in elems}
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
    Mode is validated by _validate_observe BEFORE the action runs. The action already happened
    when this runs, so a failed observation is reported inside an ok:true payload ("observed":
    false, "observe_error") — never as a failed action, which would make the agent repeat it."""
    settle_ms = int(settle_ms or 0)
    if settle_ms:
        waited = max(0, min(settle_ms, SETTLE_MAX_MS))
        time.sleep(waited / 1000.0)
        payload["settled_ms"] = waited
        if waited != settle_ms:
            payload["settle_clamped"] = True
    payload = {"ok": True, "action": payload.pop("action"), "instance": int(instance),
               "display": _disp(instance), **payload}
    try:
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
            if pr.get("elements") is not None and os.path.exists(ann):
                with open(ann, "rb") as fh:
                    out.append(MCPImage(data=fh.read(), format="png"))
            return out
    except ToolError as e:
        err = _error_payload(e)
        payload["observed"] = False
        payload["observe_error"] = err.get("error", str(e))
        if err.get("fix"):
            payload["observe_fix"] = err["fix"]
        payload["note"] = f"the {payload['action']} itself was performed; only the {mode} after it failed"
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
    Backend = the effective GLOVEBOX_VISION: 'local' (OmniParser on the GPU, ~2 s after a ~10 s
    first-call model load; served by ONE shared vision-service process per machine so parallel
    agents don't each load the models — "via": "service"|"in-process", "queued_ms" says how long
    the GPU queue was, "detector" which detector answered, "fallback_reason" why the service was
    not used. "type" is "text"|"icon" with OmniParser's own detector; with the optional
    ScreenParser detector (status().vision.omniparser.detector) it is one of text, button, link,
    tab, input, checkbox, radio, switch, select, slider, stepper, rating, icon, image, item,
    indicator and each element also carries "class", the detector's raw class name; buttons/tabs/
    links carry their visible text as "label", icons/images a short caption. `box_threshold`
    raises the detector's confidence floor to cut noise), 'basic' (tesseract, text only;
    granularity='line' merges words on one line into one target like "Sign in" — default,
    split at wide gaps so a menu bar is one target per item — or 'word' for single words; the
    frame is OCR'd at 2× (GLOVEBOX_OCR_SCALE) because UI text is too small for tesseract at 1×;
    each element also carries "confidence" 0–100), 'none' (returns a note telling you to
    screenshot + reason about pixels yourself — this is normal, not an error; status() says why
    if 'auto' fell back to it). Fails (isError) with the fix named when an explicitly requested
    backend is not usable (OmniParser not installed, no/too-small GPU, GPU out of memory, the
    vision service busy, tesseract missing its language data). Elements are capped at 300 per
    call ("truncated": true when more were detected) — use find_elements to search instead of
    paging. Ids are only valid until the screen changes — re-parse after navigation."""
    return json.dumps(_do_parse(instance, box_threshold, granularity), ensure_ascii=False)


@mcp.tool()
def find_elements(query: str, instance: int = 1, type: str = "", limit: int = 20) -> str:
    """Parse the screen and return only the elements whose label contains `query` (case-insensitive
    substring; empty query = all), optionally filtered by `type` (button, link, text, icon, …).
    One call instead of parse_screen + scanning 300 elements. The ids are valid for
    click_element(id) right away (this IS the most recent parse).

    Returns {"ok", "instance", "vision", "query", "type", "count": total matches, "elements":
    [{id, type, label, interactive, center}, …] (first `limit`, 1–100), "truncated"}. "count": 0
    is an honest answer (the text is not on screen, or OCR did not read it — screenshot to check).
    Fails (isError) like parse_screen; in vision mode 'none' it fails with the fix (no grounding
    backend: screenshot and reason yourself)."""
    n = _require("find_elements", instance)
    limit = max(1, min(int(limit), 100))
    pr = _do_parse(n)
    if pr.get("elements") is None:
        _fail("find_elements", "no grounding backend (vision mode 'none') — nothing can be searched",
              fix="screenshot(instance) and reason about the pixels yourself, or install a vision backend "
                  "(./install.sh basic|local); status() shows the vision diagnosis", instance=n, vision=pr["vision"])
    q = (query or "").lower()
    t = (type or "").lower()
    hits = [e for e in pr["elements"] if (not q or q in (e.get("label") or "").lower())
            and (not t or (e.get("type") or "").lower() == t)]
    return _ok("find_elements", n, vision=pr["vision"], query=query, type=type, count=len(hits),
               elements=hits[:limit], truncated=len(hits) > limit, screen=pr.get("screen"))


@mcp.tool()
def wait_for_text(text: str, instance: int = 1, timeout_ms: int = 10000, interval_ms: int = 1000,
                  appear: bool = True) -> str:
    """Wait until `text` appears on screen (or, appear=false, disappears) — by OCR/parse polling.
    The honest replacement for "sleep and hope" after navigation or a submit: returns as soon as
    the text is (not) there, or reports "timed_out": true.

    Returns {"ok", "instance", "found": bool, "satisfied": bool, "elapsed_ms", "polls", "element":
    {id, type, label, center} | null, "timed_out"}. "satisfied" is what you waited for;
    "timed_out": true with satisfied: false is reported, not hidden — screenshot to see what IS
    there. timeout_ms max 60000, interval_ms 300–5000. Fails (isError) if the instance is down or
    there is no grounding backend (vision mode 'none')."""
    n = _require("wait_for_text", instance)
    timeout_ms = max(0, min(int(timeout_ms), 60000))
    interval = max(300, min(int(interval_ms), 5000)) / 1000.0
    q = (text or "").lower()
    if not q:
        _fail("wait_for_text", "empty text", fix="pass the text to wait for", instance=n)
    t0 = time.time()
    polls = 0
    while True:
        pr = _do_parse(n)
        if pr.get("elements") is None:
            _fail("wait_for_text", "no grounding backend (vision mode 'none') — cannot read the screen",
                  fix="use wait_for_stable() + screenshot() instead, or install a vision backend", instance=n)
        polls += 1
        hit = next((e for e in pr["elements"] if q in (e.get("label") or "").lower()), None)
        found = hit is not None
        satisfied = found if appear else not found
        elapsed = int((time.time() - t0) * 1000)
        if satisfied or elapsed >= timeout_ms:
            return _ok("wait_for_text", n, text=text, appear=appear, found=found, satisfied=satisfied,
                       elapsed_ms=elapsed, polls=polls, timed_out=not satisfied,
                       element={k: hit[k] for k in ("id", "type", "label", "center")} if hit else None)
        time.sleep(interval)


def _modifier_keys(modifiers: str, action, n) -> list:
    keys = [m.strip().lower() for m in re.split(r"[+\s,]+", modifiers or "") if m.strip()]
    bad = [k for k in keys if k not in MODIFIERS]
    if bad:
        _fail(action, f"unknown modifier '{bad[0]}' — nothing was done",
              fix="use modifiers='ctrl' | 'shift' | 'alt' | 'super' (combine with +)", instance=n)
    return keys


def _click_at(n, action, x, y, button=1, count=1, modifiers=""):
    """mousemove + click, optionally with modifier keys held; the keys are released even when
    the click fails (a stuck ctrl would turn every later click into a ctrl-click)."""
    mods = _modifier_keys(modifiers, action, n)
    _check_xy(action, n, (x, y))
    _xdo(n, action, "mousemove", x, y, opts=("--sync",))
    for m in mods:
        _xdo(n, action, "keydown", m)
    try:
        opts = ("--repeat", str(count), "--delay", "80") if count > 1 else ()
        _xdo(n, action, "click", button, opts=opts)
    finally:
        for m in reversed(mods):
            try:
                _xdo(n, action, "keyup", m)
            except ToolError:
                pass


@mcp.tool()
def click(x: int, y: int, instance: int = 1, button: int = 1, count: int = 1, modifiers: str = "",
          observe: str = "none", settle_ms: int = 0):
    """Click at absolute pixel coords (top-left origin) in an instance.

    Returns {"ok", "action", "instance", "detail"}. button: 1=left, 2=middle, 3=right.
    count: 1–3 clicks (2 = double-click, 3 = triple-click selects a line/paragraph).
    modifiers: keys held during the click — 'ctrl' (open in new tab / multi-select), 'shift'
    (range select), 'alt', 'super', combinable 'ctrl+shift'; released afterwards whatever happens.
    `observe`='screenshot'|'parse' additionally returns the resulting screen state in the SAME
    call (saves a round-trip); `settle_ms` (max 5000, "settle_clamped": true if you asked for
    more) waits for the UI to update first — use 400–1500 after anything that navigates or
    submits, or wait_for_stable() when you don't know how long. Fails (isError) if the instance
    is down, the coordinates are outside the display or a modifier name is unknown (nothing is
    clicked then). A failed observation after a successful click is reported as "observed":
    false inside an ok result — the click is not repeated."""
    n = _require("click", instance)
    _validate_observe("click", n, observe)
    count = max(1, min(int(count), 3))
    with _ilock(n):
        _click_at(n, "click", x, y, button, count, modifiers)
        return _observe(n, observe, settle_ms,
                        {"action": "click", "detail": f"clicked ({x},{y}) button {button}" + (f" x{count}" if count > 1 else "")
                                                       + (f" with {modifiers}" if modifiers else ""),
                         "count": count, "modifiers": modifiers})


@mcp.tool()
def drag(x1: int, y1: int, x2: int, y2: int, instance: int = 1, button: int = 1, steps: int = 10,
         observe: str = "none", settle_ms: int = 0):
    """Drag (mouse-down → move → up) from (x1,y1) to (x2,y2) — for drawing, selecting, sliders.

    Returns {"ok", "action", "instance", "detail", "steps"}. `steps` (default 10, 1–100) is the
    number of intermediate motion events: canvases, sliders and drag-and-drop targets usually
    need a few to register the drag (1 = jump straight to the end point). The button is released
    even if a motion step fails (a stuck button would turn every later click into a drag).
    `observe`/`settle_ms`: see click(). Fails (isError) if the instance is down or either point
    is off-screen."""
    n = _require("drag", instance)
    _validate_observe("drag", n, observe)
    _check_xy("drag", n, (x1, y1), (x2, y2))
    steps = max(1, min(int(steps), 100))
    with _ilock(n):
        _xdo(n, "drag", "mousemove", x1, y1, opts=("--sync",))
        _xdo(n, "drag", "mousedown", button)
        try:
            for i in range(1, steps + 1):
                xi = round(x1 + (x2 - x1) * i / steps)
                yi = round(y1 + (y2 - y1) * i / steps)
                _xdo(n, "drag", "mousemove", xi, yi, opts=("--sync",))
                if steps > 1:
                    time.sleep(0.01)
        finally:
            try:
                _xdo(n, "drag", "mouseup", button)
            except ToolError:
                pass
        return _observe(n, observe, settle_ms,
                        {"action": "drag", "detail": f"dragged ({x1},{y1})->({x2},{y2})", "steps": steps})


@mcp.tool()
def double_click(x: int, y: int, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Double-click at absolute pixel coords in an instance (e.g. open a file icon, select a word).
    Same as click(count=2).

    Returns {"ok", "action", "instance", "detail"}. `observe`/`settle_ms`: see click().
    Fails (isError) if the instance is down or the coordinates are off-screen."""
    n = _require("double_click", instance)
    _validate_observe("double_click", n, observe)
    with _ilock(n):
        _click_at(n, "double_click", x, y, 1, 2)
        return _observe(n, observe, settle_ms,
                        {"action": "double_click", "detail": f"double-clicked ({x},{y})"})


@mcp.tool()
def click_element(id: int, instance: int = 1, button: int = 1, modifiers: str = "", observe: str = "none",
                  settle_ms: int = 0):
    """Click an element by id from that instance's most recent parse_screen/find_elements (no
    coordinate guessing).

    Returns {"ok", "action", "instance", "detail", "element", "center"}. Fails (isError) with the
    fix named if the id is unknown — run parse_screen(instance) first. Ids go stale when the
    screen changes: after navigation, re-parse before clicking. `modifiers`/`observe`/`settle_ms`:
    see click()."""
    n = _require("click_element", instance)
    _validate_observe("click_element", n, observe)
    last = _LAST.get(n, {})
    if id not in last:
        _fail("click_element", f"unknown element id {id} for instance {n}",
              fix="run parse_screen(instance) first — ids are only valid for the most recent parse",
              instance=n, known_ids=len(last))
    x, y = last[id]
    with _ilock(n):
        _click_at(n, "click_element", x, y, button, 1, modifiers)
        return _observe(n, observe, settle_ms,
                        {"action": "click_element", "detail": f"clicked element {id} at ({x},{y})",
                         "element": id, "center": [x, y]})


def _xclip(n, action, *args, input=None):
    # Setting (-i) forks a daemon that owns the selection and inherits stdout — capturing it would
    # block until that daemon exits, so only reads (-o) capture output.
    capture = "-o" in args
    cmd = ["xclip", "-selection", "clipboard", *args]
    try:
        return subprocess.run(cmd, input=input, env=_env(n),
                              stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.PIPE if capture else subprocess.DEVNULL, timeout=5)
    except FileNotFoundError:
        _fail(action, "xclip is not installed", fix="sudo apt install xclip", instance=n)
    except subprocess.TimeoutExpired:
        _fail(action, "xclip timed out — the clipboard owner on this display is not responding",
              fix="retry; if it persists the owning app is hung (screenshot it)", instance=n)


def _xclip_read(n, action):
    """(text|None, error|None): None text = empty/non-text clipboard; error = xclip really failed."""
    r = _xclip(n, action, "-o")
    if r.returncode == 0:
        return r.stdout.decode("utf-8", errors="replace"), None
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    if "not available" in err or "no selection" in err.lower() or not err:
        return None, None
    return None, err


@mcp.tool()
def type_text(text: str, instance: int = 1, clear: bool = False, submit: bool = False,
              observe: str = "none", settle_ms: int = 0):
    """Type into the focused field — unicode-safe. Click the field first to focus it.

    Returns {"ok", "action", "instance", "detail", "method": "xdotool"|"clipboard"}.
    clear=true selects the field's content first (ctrl+a) so the text REPLACES it;
    submit=true presses Return afterwards — one call for "fill and submit".
    Pure ASCII is typed via xdotool (timeout scales with length); text with any non-ASCII char
    (č/š/ž …) is inserted via the clipboard + ctrl+v, because xdotool's synthetic unicode
    keystrokes get silently dropped by some toolkits (e.g. Inkscape's GTK canvas) even though
    they work in browsers. The clipboard is put back as it was afterwards (previous text, or
    cleared), best-effort: an app that reads the selection late (> 0.3 s) can paste the restored
    text instead, and the host bridge may have mirrored the typed text in that window — see
    clipboard(). Terminals that paste with shift+Insert instead of ctrl+v need press_keys after
    clipboard(action='set'). Fails (isError) rather than silently losing characters if xclip is
    missing for non-ASCII text. `observe`/`settle_ms`: see click()."""
    n = _require("type_text", instance)
    _validate_observe("type_text", n, observe)
    with _ilock(n):
        if clear:
            _xdo(n, "type_text", "key", "ctrl+a", opts=("--clearmodifiers",))
        if any(ord(c) > 127 for c in text):
            if not shutil.which("xclip"):
                _fail("type_text", "text contains non-ASCII characters but xclip is not installed — "
                                   "typing it via xdotool would silently drop characters in some toolkits",
                      fix="install xclip (e.g. `sudo apt install xclip`) or send ASCII-only text",
                      instance=n)
            prev, _err = _xclip_read(n, "type_text")        # restored below: with the host clipboard
            r = _xclip(n, "type_text", "-i", input=text.encode("utf-8"))   # bridge, agent text must not linger
            if r.returncode != 0:
                if not _up(n):
                    _fail_down("type_text", n)
                _fail("type_text", "xclip failed to set the clipboard", instance=n)
            time.sleep(0.05)
            _xdo(n, "type_text", "key", "ctrl+v", opts=("--clearmodifiers",))
            time.sleep(0.3)   # let the app finish reading the selection before it changes again
            try:
                _xclip(n, "type_text", "-i", input=(prev or "").encode("utf-8"))
            except ToolError:
                pass          # best-effort: the text was typed; a failed restore must not fail the call
            method = "clipboard"
        else:
            _xdo(n, "type_text", "type", text, opts=("--clearmodifiers", "--delay", "15"),
                 timeout=XDO_TIMEOUT + 0.03 * len(text))
            method = "xdotool"
        if submit:
            _xdo(n, "type_text", "key", "Return", opts=("--clearmodifiers",))
        return _observe(n, observe, settle_ms,
                        {"action": "type_text", "detail": f"typed {len(text)} chars" + (" + Return" if submit else ""),
                         "method": method, "cleared": bool(clear), "submitted": bool(submit)})


@mcp.tool()
def clipboard(action: str = "get", text: str = "", instance: int = 1) -> str:
    """Read or set the instance's clipboard — the cheapest way to get EXACT text out of an app.

    action='get' returns {"ok", "instance", "text", "length", "empty", "truncated"}: select text
    in the app (click/drag, or press_keys('ctrl+a')) then press_keys('ctrl+c') and read it here
    — no OCR, no screenshot. "empty": true means nothing (or non-text data) is on the clipboard;
    text is capped at 100000 characters ("truncated": true). action='set' puts `text` on the
    clipboard (then press_keys('ctrl+v') or 'shift+Insert' in terminals) and returns {"ok",
    "instance", "length"}. Each instance has its own clipboard (separate X display); the bridge
    started by launch_app mirrors TEXT between it and the host's clipboard within ~1 s (so a
    person can copy/paste across — and whatever they copy on the host is readable here;
    GLOVEBOX_CLIPBOARD_SYNC=0 turns it off). Fails (isError) if xclip is missing, the instance
    is down, or xclip itself fails."""
    if action not in ("get", "set"):
        _fail("clipboard", f"unknown action '{action}'", fix="use action='get' or 'set'", instance=instance)
    n = _require("clipboard", instance)
    if not shutil.which("xclip"):
        _fail("clipboard", "xclip is not installed", fix="sudo apt install xclip", instance=n)
    with _ilock(n):
        if action == "set":
            r = _xclip(n, "clipboard", "-i", input=text.encode("utf-8"))
            if r.returncode != 0:
                _fail("clipboard", "xclip failed to set the clipboard", instance=n)
            return _ok("clipboard", n, set=True, length=len(text))
        out, err = _xclip_read(n, "clipboard")
    if err:
        _fail("clipboard", f"xclip failed to read the clipboard: {err[:200]}",
              fix="retry; if it persists the owning app is hung (screenshot it)", instance=n)
    if out is None:
        return _ok("clipboard", n, text="", length=0, empty=True, truncated=False,
                   note="clipboard is empty or holds non-text data")
    return _ok("clipboard", n, text=out[:CLIP_MAX], length=len(out), empty=(out == ""),
               truncated=len(out) > CLIP_MAX)


@mcp.tool()
def upload_file(filepath: str, instance: int = 1, selector: str = "input[type=file]",
                page_url_contains: str = "", observe: str = "none", settle_ms: int = 0):
    """Attach a local file to a page's <input type=file> via Chrome DevTools Protocol — use this for
    ALL browser uploads (logos, images, docs).

    The sandbox's native GTK file picker is invisible to us AND hangs the renderer, so never click
    an upload button expecting a dialog — call this instead. Works on Chromium-family browsers
    started by launch_app / start-display.sh (they open a per-instance --remote-debugging-port,
    9222+N). `selector` targets the file input on the MAIN page (default = first file input).
    With several tabs open the most recently active page target is used unless `page_url_contains`
    picks the tab by a URL fragment. If the site only inserts the <input> after you click its
    'upload' control, click that first (it won't open a dialog we can see, but it wires up the
    input), then call this. Relative paths resolve against files/<N>/.
    Returns {"ok", "action", "instance", "detail", "page"}; fails (isError) with the reason if the
    file is missing, no CDP target answers, or the selector matches nothing.
    NATIVE (non-browser) apps: use open_file() or drive the app's own Open dialog — it IS visible
    here. `observe`/`settle_ms`: see click()."""
    n = _require("upload_file", instance)
    _validate_observe("upload_file", n, observe)
    p = _resolve_path("upload_file", n, filepath)
    try:
        page = _cdp_set_file_input(_cdp_port(n), p, selector, page_url_contains)
    except Exception as e:
        _fail("upload_file", f"upload failed: {e}",
              fix="the browser must be a Chromium-family browser started by launch_app/start-display.sh (CDP port "
                  f"{_cdp_port(n)}); if the page adds the <input> only after clicking its "
                  "upload control, click that control first, then retry",
              instance=n)
    return _observe(n, observe, settle_ms,
                    {"action": "upload_file", "page": page,
                     "detail": f"set '{selector}' = {os.path.basename(p)} via CDP"})


@mcp.tool()
def browser(action: str = "info", instance: int = 1, url: str = "", js: str = "",
            page_url_contains: str = "", max_chars: int = TEXT_MAX) -> str:
    """Talk to the instance's Chromium-family browser over the Chrome DevTools Protocol — exact
    navigation and page text without keystrokes or OCR.

    action='info'      → {"ok", "url", "title", "pages": [{url, title}, …]} of the active tab + all tabs
    action='navigate'  → loads `url` in the active tab (waits for the load event, max 30 s);
                         returns {"ok", "url", "title", "loaded": bool}
    action='text'      → {"ok", "url", "title", "text", "length", "truncated"}: the rendered page
                         text (document.body.innerText), capped at `max_chars` (default 20000)
    action='eval'      → {"ok", "result"}: the JSON value of the JavaScript expression `js`
                         (e.g. "document.querySelector('h1').innerText"); errors in the page
                         script come back as a failure with the message
    `page_url_contains` picks a tab by URL fragment (default: the most recently active page).
    Works only for browsers started by launch_app/start-display.sh (CDP port 9222+N); fails
    (isError) with the fix otherwise, or when the tab, URL or script is bad. The page state you
    see in screenshots is this same tab."""
    n = _require("browser", instance)
    if action not in ("info", "navigate", "text", "eval"):
        _fail("browser", f"unknown action '{action}'", fix="use action='info' | 'navigate' | 'text' | 'eval'", instance=n)
    if action == "navigate" and not re.match(r"^[a-z][a-z0-9+.-]*:", url or ""):
        _fail("browser", f"bad url {url!r} — nothing was navigated", fix="pass an absolute URL such as 'https://example.com'",
              instance=n)
    if action == "eval" and not (js or "").strip():
        _fail("browser", "empty js expression", fix="pass e.g. js=\"document.title\"", instance=n)
    try:
        with _cdp(_cdp_port(n), page_url_contains) as (cmd, target, pages):
            if action == "info":
                return _ok("browser", n, url=target.get("url", ""), title=target.get("title", ""),
                           pages=[{"url": t.get("url", ""), "title": t.get("title", "")} for t in pages])
            if action == "navigate":
                cmd("Page.enable")
                r = cmd("Page.navigate", {"url": url})
                if r.get("errorText"):
                    _fail("browser", f"navigation failed: {r['errorText']}", fix="check the URL (DNS, scheme)", instance=n)
                loaded = False
                deadline = time.time() + 30
                while time.time() < deadline:
                    st = cmd("Runtime.evaluate", {"expression": "document.readyState", "returnByValue": True})
                    if st.get("result", {}).get("value") == "complete":
                        loaded = True
                        break
                    time.sleep(0.2)
                t = cmd("Runtime.evaluate", {"expression": "[location.href, document.title]", "returnByValue": True})
                href, title = (t.get("result", {}).get("value") or ["", ""])[:2]
                return _ok("browser", n, url=href, title=title, loaded=loaded,
                           note=None if loaded else "the load event did not fire within 30 s (the page may still be usable)")
            expr = "document.body ? document.body.innerText : ''" if action == "text" else js
            r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
            if r.get("exceptionDetails"):
                ex = r["exceptionDetails"]
                msg = (ex.get("exception") or {}).get("description") or ex.get("text") or "script error"
                _fail("browser", f"the script threw: {msg[:300]}", fix="fix the expression; it runs in the page's main frame",
                      instance=n)
            val = r.get("result", {}).get("value")
            if action == "text":
                text = val or ""
                cap = max(100, min(int(max_chars), 200_000))
                t = cmd("Runtime.evaluate", {"expression": "[location.href, document.title]", "returnByValue": True})
                href, title = (t.get("result", {}).get("value") or ["", ""])[:2]
                return _ok("browser", n, url=href, title=title, text=text[:cap], length=len(text),
                           truncated=len(text) > cap)
            return _ok("browser", n, result=val)
    except ToolError:
        raise
    except Exception as e:
        _fail("browser", f"CDP call failed: {e}",
              fix="the browser must be a Chromium-family browser started by launch_app/start-display.sh "
                  f"(CDP port {_cdp_port(n)}); window_info() shows whether a browser window exists", instance=n)


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
    n = _require("open_file", instance)
    p = _resolve_path("open_file", n, filepath)
    try:
        appenv, args, _ = _app_env_args(n, app or "xdg-open")
    except ValueError as e:
        _fail("open_file", f"cannot parse app {app!r}: {e}", fix="pass a program name such as app='gimp'", instance=n)
    args = args + [p]
    proc, err, fix = _spawn_checked(n, args, appenv, _app_log(n),
                                    "pass an app that exists in PATH (e.g. app='gimp') or omit app to use xdg-open",
                                    append=True)
    if err:
        if not app and "code 3" in err:
            fix = fix or "no default application is registered for this file type — pass app='<program>' explicitly"
        _fail("open_file", err,
              fix=fix or "check the app and file; app_log(instance) shows the handler's stderr",
              instance=n)
    return _ok("open_file", n, opened=p, app=app or "xdg-open", pid=proc.pid)


@mcp.tool()
def list_files(instance: int = 1) -> str:
    """The instance's staging folder (files/<N>/ under the install dir) and its contents.

    Returns {"ok", "instance", "dir", "count", "truncated", "files": [{name, bytes, modified,
    is_dir, partial}, …]} (capped at 200 entries, newest first). "partial": true marks an
    in-progress browser download (.crdownload/.part/.tmp) — wait for it to disappear before
    using the file. Drop files there (or reference any host path you can read) to open in apps via
    open_file() or an app's Open dialog; launch_app browser instances also download / 'save as'
    into it — call this after a download to confirm the file arrived. Browser uploads still go
    through upload_file(). Fails (isError) if the folder cannot be read."""
    n = _require("list_files", instance, up=False)
    d = _files_dir(n)
    rows = []
    try:
        names = os.listdir(d)
    except OSError as e:
        _fail("list_files", f"cannot read {d}: {e}", fix="check permissions on the files/ folder", instance=n)
    for name in names:
        p = os.path.join(d, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        rows.append({"name": name, "bytes": st.st_size, "mtime": st.st_mtime,
                     "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
                     "is_dir": os.path.isdir(p),
                     "partial": name.endswith((".crdownload", ".part", ".tmp", ".download"))})
    rows.sort(key=lambda r: r.pop("mtime"), reverse=True)
    total = len(rows)
    truncated = total > FILES_MAX
    if truncated:
        rows = rows[:FILES_MAX]
    return _ok("list_files", n, dir=d, count=total, truncated=truncated, files=rows)


@mcp.tool()
def put_file(name: str, content: str, instance: int = 1, encoding: str = "text", overwrite: bool = False) -> str:
    """Write a file into the instance's staging folder files/<N>/ — the way to hand the sandbox a
    file you produced (a CSV to import, an SVG to open, a text to upload) without shell access.
    Then open_file(name) / upload_file(name) / an app's Open dialog can use it by that name.

    `name` is a relative path inside the staging folder ('data.csv', 'in/logo.svg'; parent
    folders are created); absolute paths and '..' are refused — nothing outside files/<N>/ can be
    written. `content` is text (written as UTF-8) or, with encoding='base64', binary data.
    Returns {"ok", "instance", "path", "name", "bytes", "overwritten"}. Fails (isError), writing
    nothing, if the file exists and overwrite is false, the name escapes the folder or names a
    directory, the base64 is invalid, or the content exceeds 10 MB. The instance does not have
    to be running (stage files first, launch after)."""
    n = _require("put_file", instance, up=False)
    if encoding not in ("text", "base64"):
        _fail("put_file", f"unknown encoding '{encoding}' — nothing was written", fix="use encoding='text' or 'base64'", instance=n)
    root = os.path.realpath(_files_dir(n))
    if not name or os.path.isabs(name) or name.endswith(("/", os.sep)):
        _fail("put_file", f"bad name {name!r} — nothing was written",
              fix="pass a relative file path inside the staging folder, e.g. 'data.csv' or 'in/logo.svg'", instance=n)
    target = os.path.realpath(os.path.join(root, name))
    if os.path.commonpath([root, target]) != root or target == root:
        _fail("put_file", f"{name!r} is outside the staging folder — nothing was written",
              fix=f"files can only be written under {root} (no '..', no symlinks out of it)", instance=n)
    if encoding == "base64":
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as e:
            _fail("put_file", f"invalid base64: {e} — nothing was written", fix="send standard base64 without line breaks",
                  instance=n)
    else:
        data = content.encode("utf-8")
    if len(data) > PUT_MAX:
        _fail("put_file", f"{len(data)} bytes exceeds the {PUT_MAX} byte limit — nothing was written",
              fix="copy large files into the staging folder on the host instead (list_files shows its path)", instance=n)
    if os.path.isdir(target):
        _fail("put_file", f"{name!r} is a directory — nothing was written", fix="pass a file name inside it", instance=n)
    existed = os.path.exists(target)
    if existed and not overwrite:
        _fail("put_file", f"{name!r} already exists — nothing was written",
              fix="pass overwrite=true to replace it, or pick another name (list_files shows what is there)", instance=n)
    tmp = f"{target}.put-{os.getpid()}-{threading.get_ident()}"
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, target)       # atomic: a reader never sees a half-written file
    except OSError as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        _fail("put_file", f"could not write {target}: {e}", fix="check permissions and free space on the files/ folder", instance=n)
    return _ok("put_file", n, path=target, name=os.path.relpath(target, root), bytes=len(data), overwritten=existed)


_X11 = None


def _keysym_known(name: str):
    """True/False via libX11's XStringToKeysym; None when libX11 cannot be loaded (then the
    after-the-fact stderr check is all we have)."""
    global _X11
    if _X11 is None:
        lib = ctypes.util.find_library("X11")
        try:
            _X11 = ctypes.CDLL(lib) if lib else False
        except OSError:
            _X11 = False
        if _X11:
            _X11.XStringToKeysym.restype = ctypes.c_ulong
            _X11.XStringToKeysym.argtypes = [ctypes.c_char_p]
    if not _X11:
        return None
    return _X11.XStringToKeysym(name.encode("utf-8", "replace")) != 0


def _validate_keys(action, n, toks):
    """Reject option-like tokens and unknown keysyms BEFORE pressing anything: a sequence like
    'ctrl+a Foo' must not press ctrl+a and then complain about Foo."""
    for t in toks:
        if t.startswith("-"):
            _fail(action, f"'{t}' is not a key name — nothing was pressed",
                  fix="keysyms never start with '-': use 'minus' for the - key, 'KP_Subtract' on the keypad",
                  instance=n)
        for part in t.split("+"):
            if not part:
                _fail(action, f"'{t}' has an empty key part — nothing was pressed",
                      fix="write combos as 'ctrl+a' (no leading/trailing +)", instance=n)
            if part.lower() in MODIFIERS:
                continue
            if _keysym_known(part) is False:
                _fail(action, f"'{part}' in '{t}' is not an X keysym — nothing was pressed",
                      fix="use xdotool keysym names: 'Return', 'Tab', 'Escape', 'F5', 'ctrl+a', 'Page_Down', "
                          "'KP_Enter', 'space', 'minus', 'at'; sequences are space-separated", instance=n)


@mcp.tool()
def press_keys(keys: str, instance: int = 1, observe: str = "none", settle_ms: int = 0):
    """Press a key, a combo, or a SEQUENCE (xdotool keysym syntax, space-separated for sequences):
    'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5', 'ctrl+a Delete', 'alt+F4', 'shift+Insert'.

    Returns {"ok", "action", "instance", "detail", "keys": [...]}. Every token is validated
    against the X keysym table BEFORE anything is pressed, so an invalid name fails with nothing
    pressed — also in the middle of a sequence. Fails (isError) on a dead instance or a frozen
    display. Browser navigation: 'ctrl+l' → type_text(url) → 'Return' (or browser(action=
    'navigate')). `observe`/`settle_ms`: see click()."""
    n = _require("press_keys", instance)
    _validate_observe("press_keys", n, observe)
    toks = keys.split()
    if not toks:
        _fail("press_keys", "no keys given", fix="pass e.g. 'Return' or 'ctrl+a Delete'", instance=n)
    _validate_keys("press_keys", n, toks)
    with _ilock(n):
        r = _xdo(n, "press_keys", "key", *toks, opts=("--clearmodifiers", "--delay", "40"))
        err = r.stderr or ""
        # second line of defence (libX11 missing): xdotool exits 0 on unknown keysyms
        if "No such key name" in err or "Invalid key sequence" in err:
            bad = [l.strip() for l in err.splitlines() if "key" in l][:2]
            _fail("press_keys", f"xdotool ignored part of '{keys}': {' | '.join(bad)[:200]} — the valid tokens before it WERE pressed",
                  fix="use xdotool keysym syntax: 'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5'; sequences are space-separated",
                  instance=n)
        return _observe(n, observe, settle_ms,
                        {"action": "press_keys", "detail": f"pressed {keys}", "keys": toks})


@mcp.tool()
def move_mouse(x: int, y: int, instance: int = 1) -> str:
    """Move the mouse pointer to absolute pixel coords in an instance (no click).

    Returns {"ok", "action", "instance", "detail"}. Useful for hover states (menus, tooltips) —
    follow with screenshot() to see the effect. Fails (isError) if the instance is down or the
    coordinates are off-screen."""
    n = _require("move_mouse", instance)
    _check_xy("move_mouse", n, (x, y))
    with _ilock(n):
        _xdo(n, "move_mouse", "mousemove", x, y, opts=("--sync",))
    return _ok("move_mouse", n, detail=f"moved to ({x},{y})")


@mcp.tool()
def scroll(amount: int, instance: int = 1, x: int = -1, y: int = -1, axis: str = "vertical",
           observe: str = "none", settle_ms: int = 0):
    """Scroll the wheel: positive = up (or left), negative = down (or right).

    Returns {"ok", "action", "instance", "detail", "applied", "clamped"}. One unit ≈ one wheel
    notch (a few lines); at most 100 notches per call ("clamped": true when you asked for more,
    "applied" says how many were sent). Scrolling targets the window under the pointer — pass
    x,y to move there first (both >= 0), otherwise the current pointer position is used.
    axis='horizontal' scrolls sideways (wheel buttons 6/7). Fails (isError) on amount=0 (a
    no-op), a dead instance, off-screen x,y or an unknown axis. `observe`/`settle_ms`: see click()."""
    n = _require("scroll", instance)
    _validate_observe("scroll", n, observe)
    amount = int(amount)
    if amount == 0:
        _fail("scroll", "scroll amount 0 is a no-op — nothing was scrolled",
              fix="pass a positive amount to scroll up or a negative amount to scroll down", instance=n)
    if axis not in ("vertical", "horizontal"):
        _fail("scroll", f"unknown axis '{axis}'", fix="use axis='vertical' or 'horizontal'", instance=n)
    if x >= 0 or y >= 0:
        _check_xy("scroll", n, (x, y))
    applied = min(abs(amount), SCROLL_MAX)
    if axis == "vertical":
        btn = 4 if amount > 0 else 5
    else:
        btn = 6 if amount > 0 else 7
    with _ilock(n):
        if x >= 0 or y >= 0:
            _xdo(n, "scroll", "mousemove", x, y, opts=("--sync",))
        _xdo(n, "scroll", "click", btn, opts=("--repeat", str(applied), "--delay", "20"))
        return _observe(n, observe, settle_ms,
                        {"action": "scroll", "detail": f"scrolled {amount:+d} {axis}" + (f" (applied {applied})" if applied != abs(amount) else ""),
                         "applied": applied, "clamped": applied != abs(amount), "axis": axis})


@mcp.tool()
def wait_ms(ms: int) -> str:
    """Wait (e.g. for a page to load or an app to draw) — capped at 10000 ms.

    Returns {"ok", "action", "requested_ms", "waited_ms", "clamped"} — "clamped": true means you
    asked for more than the cap and only waited_ms elapsed. Prefer wait_for_stable() (waits until
    the screen stops changing), wait_for_text(), or settle_ms on the action itself when you also
    want to observe the result in the same call."""
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
    n = _require("wait_for_stable", instance)
    _validate_observe("wait_for_stable", n, observe)
    timeout_ms = max(0, min(int(timeout_ms), 30000))
    interval = max(100, min(int(interval_ms), 2000)) / 1000.0
    t0 = time.time()
    prev = _grab_checked(n, "wait_for_stable")
    frames, change, stable = 1, 1.0, False
    while True:
        time.sleep(interval)
        cur = _grab_checked(n, "wait_for_stable")
        frames += 1
        change = _changed_fraction(prev, cur)
        prev = cur
        if change <= float(threshold):
            stable = True
            break
        if (time.time() - t0) * 1000 >= timeout_ms:
            break
    elapsed = int((time.time() - t0) * 1000)
    return _observe(n, observe, 0,
                    {"action": "wait_for_stable", "stable": stable, "elapsed_ms": elapsed, "frames": frames,
                     "change": round(change, 4), "timed_out": not stable})


def _active_window(n, action):
    """({id, title, x, y, width, height, pid} | None, failure | None). No focused window is an
    answer (None, None); any other xdotool failure is reported in `failure`."""
    r = _xdo_soft(n, action, "getactivewindow", "getwindowname", "getwindowgeometry", "--shell")
    if r.returncode != 0 or not r.stdout.strip():
        err = (r.stderr or "").strip()
        return None, (None if r.returncode == 1 or "active window" in err.lower() else err or None)
    lines = r.stdout.splitlines()
    kv = dict(l.split("=", 1) for l in lines[1:] if "=" in l)
    active = {"id": int(kv.get("WINDOW", 0)), "title": lines[0], "x": int(kv.get("X", 0)), "y": int(kv.get("Y", 0)),
              "width": int(kv.get("WIDTH", 0)), "height": int(kv.get("HEIGHT", 0)), "pid": None}
    rp = _xdo_soft(n, action, "getwindowpid", str(active["id"]))
    if rp.returncode == 0 and rp.stdout.strip().isdigit():
        active["pid"] = int(rp.stdout.strip())
    return active, None


@mcp.tool()
def window_info(instance: int = 1) -> str:
    """The active window's title + geometry, the visible windows and the pointer position on an
    instance — a cheap, image-free way to confirm navigation (browser titles follow the page) or
    that an app opened.

    Returns {"ok", "instance", "active": {id, title, x, y, width, height, pid} | null,
    "windows": [{id, title}, …] (visible, capped at 50, unnamed ones included with title ""),
    "count", "truncated", "cursor": [x, y]}. "active" is null when no window has focus yet
    (fresh instance) — that is reported, not an error. Window ids are what manage_window()
    takes. Fails (isError) if the instance is down or xdotool is missing/fails."""
    n = _require("window_info", instance)
    active, failure = _active_window(n, "window_info")
    if failure:
        _fail("window_info", f"xdotool failed: {failure[:200]}", instance=n)
    rs = _xdo_soft(n, "window_info", "search", "--onlyvisible", "--maxdepth", "3", "--name", ".*")
    ids = [w for w in rs.stdout.split() if w.isdigit()] if rs.returncode == 0 else []
    windows = []
    for w in ids[:WINDOWS_MAX]:
        rn = _xdo_soft(n, "window_info", "getwindowname", w)
        windows.append({"id": int(w), "title": rn.stdout.strip() if rn.returncode == 0 else ""})
    cursor = None
    rm = _xdo_soft(n, "window_info", "getmouselocation", "--shell")
    if rm.returncode == 0:
        kv = dict(l.split("=", 1) for l in rm.stdout.splitlines() if "=" in l)
        cursor = [int(kv.get("X", 0)), int(kv.get("Y", 0))]
    return _ok("window_info", n, active=active, windows=windows, count=len(ids),
               truncated=len(ids) > WINDOWS_MAX, cursor=cursor)


@mcp.tool()
def manage_window(action: str, instance: int = 1, window_id: int = 0, width: int = 0, height: int = 0,
                  x: int = 0, y: int = 0, observe: str = "none", settle_ms: int = 0):
    """Act on a window of an instance: action='activate' (focus/raise — switch between an app's
    windows or dialogs), 'close' (asks the app to close it — a save prompt may appear),
    'minimize', 'maximize', 'restore' (un-maximize), 'move' (to x,y), 'resize' (to width x
    height). window_id=0 means the active window (window_info lists ids).

    Returns {"ok", "action", "instance", "window", "detail"} plus `observe`/`settle_ms` like
    click(). Fails (isError) if the window id is unknown, the action is unknown, move/resize
    sizes are missing, or the instance is down. maximize/restore need wmctrl (status().deps)."""
    n = _require("manage_window", instance)
    _validate_observe("manage_window", n, observe)
    actions = ("activate", "close", "minimize", "maximize", "restore", "move", "resize")
    if action not in actions:
        _fail("manage_window", f"unknown action '{action}'", fix=f"use one of {', '.join(actions)}", instance=n)
    with _ilock(n):
        wid = int(window_id)
        if wid == 0:
            active, failure = _active_window(n, "manage_window")
            if not active:
                _fail("manage_window", "no active window on this instance" + (f" ({failure})" if failure else ""),
                      fix="window_info() lists windows — pass one as window_id", instance=n)
            wid = active["id"]
        else:
            r = _xdo_soft(n, "manage_window", "getwindowname", str(wid))
            if r.returncode != 0:
                _fail("manage_window", f"unknown window id {wid}", fix="window_info() lists the visible windows", instance=n)
        if action == "activate":
            _xdo(n, "manage_window", "windowactivate", wid, opts=("--sync",))
        elif action == "close":
            _xdo(n, "manage_window", "windowclose", wid)
        elif action == "minimize":
            _xdo(n, "manage_window", "windowminimize", wid)
        elif action in ("maximize", "restore"):
            flag = "add" if action == "maximize" else "remove"
            r = _run("manage_window", n, ["wmctrl", "-i", "-r", str(wid), "-b", f"{flag},maximized_vert,maximized_horz"],
                     text=True)
            if r.returncode != 0:
                _fail("manage_window", f"wmctrl failed: {(r.stderr or r.stdout).strip()[:200]}",
                      fix="install wmctrl and a window manager (openbox)", instance=n)
        elif action == "move":
            _xdo(n, "manage_window", "windowmove", wid, x, y)
        else:
            if width <= 0 or height <= 0:
                _fail("manage_window", "resize needs width and height > 0", fix="pass width= and height= in pixels", instance=n)
            _xdo(n, "manage_window", "windowsize", wid, width, height)
        return _observe(n, observe, settle_ms,
                        {"action": "manage_window", "window": wid, "detail": f"{action} window {wid}"})


@mcp.tool()
def app_log(instance: int = 1, lines: int = 50) -> str:
    """Tail of the stderr log of the apps launched on an instance (launch_app / start_app /
    open_file) — read it when an app crashed, drew nothing, or misbehaves.

    Returns {"ok", "instance", "path", "lines": [...], "total_lines", "truncated"} (max 500 lines
    per call). The log is per instance: launch_app starts it fresh, start_app/open_file append.
    Instance 1 (start-display.sh) logs to /tmp/chromium-x1.log instead. Fails (isError) if no log
    exists for that instance yet."""
    n = _require("app_log", instance, up=False)
    p = _app_log(n)
    if not os.path.exists(p):
        _fail("app_log", f"no app log for instance {n} (nothing launched there by this server yet)",
              fix="launch_app()/start_app()/open_file() write it; instance 1's browser logs to /tmp/chromium-x1.log",
              instance=n)
    lines = max(1, min(int(lines), 500))
    with open(p, errors="replace") as fh:
        all_lines = fh.read().splitlines()
    tail = all_lines[-lines:]
    return _ok("app_log", n, path=p, lines=tail, total_lines=len(all_lines),
               truncated=len(all_lines) > len(tail))


@mcp.tool()
def status() -> str:
    """Server + sandbox status in one read-only call — run this first, and when filing bug reports.

    Returns {"ok", "version", "host_display", "tmp_dir", "cleanup_on_exit",
    "vision": {requested, effective, ready, reason, fallback, gpu: {present, name, vram_total_mb,
    vram_free_mb, enough, note}, omniparser: {code, weights, torch, ready, loadable, reason,
    detector: {requested, effective, screenparser_weights, reason}, loaded,
    service: {running, pid, loaded, detector, served, in_flight, idle_s, …} — the shared
    per-machine OmniParser process (one GPU load for all agents; starts on first use, idle-exits)},
    tesseract: {binary, pytesseract, langs, lang, ready, reason}},
    "instances": [...same as list_instances], "keyboard": {host_layout, override}, "clipboard_sync":
    {enabled, poll_s, note}, "deps": {xdotool, xclip, xephyr, openbox, wmctrl, xdpyinfo, xkbcomp,
    setxkbmap, dbus_run_session, tesseract, nvidia_smi: true|false}, "notes": [...]}.
    "vision.effective" is the grounding backend actually in use (none|basic|local), re-resolved
    every 20 s (installing OmniParser takes effect without a restart); when GLOVEBOX_VISION=auto it
    is the best READY one and "reason" says why (e.g. OmniParser weights missing, GPU too small).
    "notes" lists things worth knowing (e.g. the service runs a different detector than this
    server would pick). No side effects."""
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
    notes = []
    if svc and svc.get("loaded") and svc.get("detector") and svc["detector"] != omni["detector"]["effective"]:
        notes.append(f"the vision service runs the '{svc['detector']}' detector; this server would pick "
                     f"'{omni['detector']['effective']}' — restart it to switch: python vision_service.py stop")
    if CLIP_SYNC:
        notes.append("clipboard sync is on: text copied on the host is readable inside every instance "
                     "(clipboard(get), pages in the sandbox browser) — GLOVEBOX_CLIPBOARD_SYNC=0 turns it off")
    tess = vision.tesseract_info()
    deps = {k: bool(shutil.which(b)) for k, b in (
        ("xdotool", "xdotool"), ("xclip", "xclip"), ("xephyr", "Xephyr"), ("openbox", "openbox"),
        ("wmctrl", "wmctrl"), ("xdpyinfo", "xdpyinfo"), ("xkbcomp", "xkbcomp"), ("setxkbmap", "setxkbmap"),
        ("dbus_run_session", "dbus-run-session"), ("tesseract", "tesseract"), ("nvidia_smi", "nvidia-smi"))}
    keyboard = {"host_layout": host_bridge.display_layout(HOST, _host_env()),
                "override": os.environ.get("GLOVEBOX_KEYBOARD", "")}
    v = vision.resolve(omni=omni, tess=tess, service_loaded=bool(svc and svc.get("loaded")))
    _VISION.update(value=v, at=time.time())
    return _ok("status", version=__version__, host_display=HOST, tmp_dir=TMP,
               cleanup_on_exit=_truthy(os.environ.get("GLOVEBOX_CLEANUP_ON_EXIT", "")),
               vision={**v, "gpu": gpu, "omniparser": omni, "tesseract": tess},
               instances=_instance_rows(), keyboard=keyboard,
               clipboard_sync={"enabled": CLIP_SYNC, "poll_s": host_bridge.POLL_S,
                               "note": "host clipboard text is mirrored into every instance" if CLIP_SYNC else "off"},
               deps=deps, notes=notes)


if __name__ == "__main__":
    mcp.run()
