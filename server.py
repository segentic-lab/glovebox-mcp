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
from mcp.types import ToolAnnotations
from mcp.server.mcpserver.utilities.types import Image as MCPImage
import mss
from PIL import Image, ImageChops, ImageDraw

import a11y
import host_bridge
import vision
import vision_service
from _paths import disp as _disp, display_up, tmp_dir
from _version import __version__

TMP = tmp_dir()   # annotated frames + app/Xephyr logs; 0700, ownership-checked (frames are screenshots)

mcp = MCPServer("glovebox", version=__version__,
                instructions="Sandboxed computer use: drive real apps inside nested X11 windows. Call status() "
                             "first; every tool returns JSON with ok/fix. Prefer labels over pixel guesses: "
                             "find_elements('Save') -> click_element(id) or act(id) (the app's accessibility "
                             "information is asked first, vision second), wait_for_text('Done') instead of "
                             "sleeping, fill(text, x, y) to set a form field and read it back, "
                             "browser(action='text') for exact page text. click() reports the element it "
                             "hit in browsers. See AGENTS.md in the repo.")

INSTANCES = {}   # N -> {name, command, size, xephyr_pid, app_pid}
_LAST = {}       # N -> {element id -> (cx, cy)} from that instance's last parse_screen
_GEOM = {}       # N -> (W, H, read_at) cached display geometry (bounds checks; refreshed after GEOM_TTL)
_LAUNCHED = set()  # instances started by THIS process (for optional cleanup on exit)
_LAST_EL = {}    # N -> {"typed": bool, "elements": {id: element}} from the last parse (fill's label check)
_SNAP = defaultdict(int)   # N -> id of that instance's latest accessibility snapshot
A11Y_MIN = 3     # source='auto': fewer accessibility elements than this -> use vision instead
_A11Y = {"at": 0.0, "python": None}   # interpreter that can import Atspi (re-probed every 60 s)
A11Y_HELPER = os.path.join(HERE, "a11y_focus.py")
A11Y_APT = "sudo apt install at-spi2-core gir1.2-atspi-2.0 python3-gi"
FOCUS_SLACK = 8  # px: toolkits accept clicks slightly outside the rectangle they report over AT-SPI
_PTR = {}        # N -> (x, y) where THIS server last left the pointer (foreign-input detection)
_GROUPS = defaultdict(set)   # N -> process groups this server started on that display (killed on close)
OFFICE = ("libreoffice", "soffice", "lowriter", "localc", "loimpress", "lodraw", "lobase", "lomath")
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
EVAL_MAX = 100_000  # browser(action='eval') characters of result JSON per call
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


# ---------- tool annotations (clients use them to decide what needs approval) ----------
# openWorldHint is true throughout: the apps being driven reach the web and the user's files.
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)                 # look, wait, list
_ADDITIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)   # start, move, stage
_ACTS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)        # input into a live app


# ---------- response contract ----------
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def _dumps(payload: dict) -> str:
    """json.dumps for everything that leaves a tool. A str holding an unpaired UTF-16 surrogate
    (half an emoji: a JS string cut with slice(), a clipboard fragment, a window title) cannot be
    encoded as UTF-8 — it used to kill the server's stdout writer, and with it the whole MCP
    connection, on the reply. Such characters become U+FFFD and the reply says how many."""
    out = json.dumps(payload, ensure_ascii=False)
    bad = len(_LONE_SURROGATE.findall(out))
    if bad:
        payload = {**payload, "text_repaired": f"{bad} unpaired UTF-16 surrogate(s) (half of an emoji or "
                                               "other astral character) replaced with U+FFFD"}
        out = _LONE_SURROGATE.sub("\ufffd", json.dumps(payload, ensure_ascii=False))
    return out


def _res(ok: bool, action: str, instance=None, **kw) -> str:
    out = {"ok": ok, "action": action}
    if instance is not None:
        out["instance"] = int(instance)
        out["display"] = _disp(instance)
    out.update(kw)
    return _dumps(out)


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


def _cdp_pages(port, timeout=5):
    import urllib.request
    # ProxyHandler({}) — never route the 127.0.0.1 CDP call through an http_proxy from the env.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    info = json.loads(opener.open(f"http://127.0.0.1:{port}/json", timeout=timeout).read().decode())
    return [t for t in info if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
            and not t.get("url", "").startswith("devtools://")]


class CdpTimeout(RuntimeError):
    """The browser did not answer one CDP command in time (the page script may still be running)."""


@contextmanager
def _cdp(port, page_url_contains="", timeout=15):
    """A CDP session on one page target of the browser listening on 127.0.0.1:port. Yields
    (cmd, target, pages): cmd(method, params) -> result. Targets are listed most-recently-active
    first, so the first one is the tab being looked at unless `page_url_contains` picks."""
    import websocket
    pages = _cdp_pages(port, min(5, timeout))
    if page_url_contains:
        pages = [t for t in pages if page_url_contains in t.get("url", "")]
        if not pages:
            raise RuntimeError(f"no open tab whose URL contains '{page_url_contains}'")
    if not pages:
        raise RuntimeError(f"no CDP page target on 127.0.0.1:{port} — is Chromium started with --remote-debugging-port?")
    target = pages[0]
    # suppress_origin: Chrome rejects CDP WS handshakes that carry a non-allowlisted Origin header;
    # sending none sidesteps that without opening the port to every origin (--remote-allow-origins).
    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=timeout, suppress_origin=True)
    seq = {"n": 0}

    def cmd(method, params=None, wait=None):
        """`wait` = seconds this one command may take (default: the session timeout). It is a
        deadline for the whole reply, not per socket read, so a chatty page cannot extend it."""
        seq["n"] += 1
        mid = seq["n"]
        deadline = time.monotonic() + (wait or timeout)
        ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CdpTimeout(f"{method}: no reply within {wait or timeout:g} s")
            ws.settimeout(left)
            try:
                msg = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                raise CdpTimeout(f"{method}: no reply within {wait or timeout:g} s") from None
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
    _LAST_EL.pop(n, None)
    _PTR.pop(n, None)
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


def _group_pids(n) -> list:
    """Live processes that belong to a process group this server started for instance n AND
    still have DISPLAY=:n in their environment — both conditions, so a recycled group id can
    never point the kill at an unrelated process."""
    groups = _GROUPS.get(int(n)) or set()
    if not groups:
        return []
    want = f"DISPLAY={_disp(n)}".encode()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as fh:
                pgrp = int(fh.read().rsplit(b")", 1)[1].split()[2])
            if pgrp not in groups:
                continue
            with open(f"/proc/{name}/environ", "rb") as fh:
                if want in fh.read().split(b"\0"):
                    out.append(int(name))
        except (OSError, ValueError, IndexError):
            continue
    return out


def _kill_groups(n, sig):
    for pid in _group_pids(n):
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def _close(n) -> bool:
    """Kill the apps + Xephyr of instance n; True when the display is down afterwards. Apps are
    ended as whole process groups: some survive the loss of their display (LibreOffice's
    soffice.bin kept running, and every later `libreoffice` launch handed its document to that
    windowless leftover instead of opening a window)."""
    info = INSTANCES.get(n)
    _kill_groups(n, signal.SIGTERM)
    if info:
        _terminate(info)
    _kill_xephyr(n)
    if _up(n):
        return False
    if _group_pids(n):
        time.sleep(0.5)
        _kill_groups(n, signal.SIGKILL)     # the display is gone; whatever ignored SIGTERM has no purpose left
    _GROUPS.pop(int(n), None)
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
    # The host session is often Wayland; every toolkit must be told to draw on THIS X display.
    # Qt goes by XDG_SESSION_TYPE / QT_QPA_PLATFORM, not by DISPLAY: with the host's values it
    # picks its Wayland backend and — WAYLAND_DISPLAY being removed — never shows a window
    # (Kate, any PyQt/KDE app stayed a black screen). The accessibility switches make Qt
    # register on the instance's AT-SPI bus without a screen reader running (fill's focus check).
    appenv.update(XDG_SESSION_TYPE="x11", QT_QPA_PLATFORM="xcb", SDL_VIDEODRIVER="x11", CLUTTER_BACKEND="x11",
                  MOZ_ENABLE_WAYLAND="0", QT_ACCESSIBILITY="1", QT_LINUX_ACCESSIBILITY_ALWAYS_ON="1")
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
        if base in OFFICE and "UserInstallation" not in command:
            # LibreOffice is single-instance per PROFILE: without its own, the launch is handed to
            # a LibreOffice already running — on the host desktop or another instance — and the
            # document opens THERE, outside this sandbox.
            prof = os.path.join(HERE, "profiles", f"libreoffice-{n}")
            os.makedirs(prof, exist_ok=True)
            args.append(f"-env:UserInstallation=file://{prof}")
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
            # its own session = its own process group: close_instance can then end the whole tree
            # (dbus-run-session -> launcher script -> the real binary), not just the first pid
            proc = subprocess.Popen(args, env=appenv, stdout=subprocess.DEVNULL, stderr=lf,
                                    start_new_session=True)
        _GROUPS[int(n)].add(proc.pid)
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
@mcp.tool(annotations=_ADDITIVE)
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


@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_ACTS)
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


@mcp.tool(annotations=_ADDITIVE)
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
@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_READ_ONLY)
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
        box = _parse_region("screenshot", n, region, W, H)
        img = img.crop(box)
    if float(scale) < 1.0:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
    im = _encode(img, fmt, quality)
    meta = _ok("screenshot", n, width=W, height=H, region=list(box) if box else None,
               scale=float(scale), format=fmt, bytes=len(im.data),
               image_size=[img.width, img.height])
    return [meta, im]


def _a11y_env(n):
    """(python, env) for running an AT-SPI helper against instance n's OWN accessibility bus, or
    (None, why). The bus address is a property of the display's root window; the host's session
    bus is removed from the environment so the host's widgets can never be reported."""
    py = _a11y_python()
    if not py:
        return None, f"AT-SPI bindings are not installed on the host ({A11Y_APT})"
    try:
        r = subprocess.run(["xprop", "-root", "-display", _disp(n), "AT_SPI_BUS"], capture_output=True,
                           text=True, timeout=3)
    except Exception as e:
        return None, f"the accessibility bus lookup failed ({type(e).__name__})"
    m = re.search(r'=\s*"(.+)"', r.stdout or "")
    if not m:
        return None, "no accessibility bus on this display (the app does not speak AT-SPI)"
    env = {**_env(n), "AT_SPI_BUS_ADDRESS": m.group(1)}
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    return py, env


def _a11y_parse(n):
    """(elements, meta) from the app's own accessibility information, or (None, why).
    A Chromium-family browser answers over CDP (the active tab's DOM); anything else is asked
    over the display's AT-SPI bus. Elements carry private keys (box, path) the caller strips."""
    snap = _SNAP[n] = _SNAP[n] + 1
    browser_why = None
    try:
        with _cdp(_cdp_port(n), timeout=8) as (cmd, _target, _pages):
            d = a11y.browser_snapshot(cmd, snap)
        meta = {"source": "browser-dom", "snapshot": snap, "page": {"url": d.get("url", ""), "title": d.get("title", "")}}
        if d.get("frames_skipped"):
            meta["frames_skipped"] = d["frames_skipped"]
        if d.get("truncated"):
            meta["a11y_truncated"] = True
        if d["elements"]:
            return d["elements"], meta
        browser_why = "the page exposes no elements"
    except CdpTimeout:
        browser_why = "the page did not answer in time (its main thread is busy)"
    except Exception:
        pass                                         # no CDP browser on this instance: a native app
    py, env = _a11y_env(n)
    if not py:
        return None, browser_why or env
    try:
        d = a11y.atspi_call(py, env, "dump")
    except RuntimeError as e:
        return None, browser_why or str(e)
    if not d.get("elements"):
        return None, browser_why or "the app exposes no elements over AT-SPI (canvas, game or Xlib-only app)"
    meta = {"source": "at-spi", "snapshot": snap, "apps": d.get("apps", [])}
    if d.get("truncated"):
        meta["a11y_truncated"] = True
    return d["elements"], meta


_EL_PRIVATE = ("box", "path")


def _public(e: dict) -> dict:
    out = {k: v for k, v in e.items() if k not in _EL_PRIVATE}
    if out.get("onscreen"):
        out.pop("onscreen")                         # only the exceptions are flagged
    return out


def _do_parse(instance: int = 1, box_threshold: float = 0.05, granularity: str = "line", source: str = "auto",
              keep_all: bool = False) -> dict:
    """One parse of an instance's screen → the parse_screen payload (dict). Runs under the
    instance lock: the annotated image and _LAST ids belong to one parse at a time.
    source='auto' asks the app first (accessibility: exact labels, no GPU) and falls back to
    vision; keep_all adds "_all" (every accessibility element, off-screen ones included)."""
    n = _require("parse_screen", instance)
    if source not in ("auto", "a11y", "vision"):
        _fail("parse_screen", f"unknown source '{source}'", fix="use source='auto' | 'a11y' | 'vision'", instance=n)
    v = _vision()
    mode = v["effective"]
    base = {"ok": True, "action": "parse_screen", "instance": n, "vision": mode}
    if source != "vision":
        with _ilock(n):
            elems, meta = _a11y_parse(n)
            if elems is not None:
                shown = [e for e in elems if e.get("onscreen")]
                if source == "auto" and len(elems) < A11Y_MIN and mode != "none":
                    elems, meta = None, (f"only {len(elems)} element(s) are exposed through accessibility "
                                         "(a canvas or a custom-drawn app)")
            if elems is not None:
                img = _grab_checked(n, "parse_screen")
                ann = _annotated(n)
                _annotate(img, shown, {e["id"]: tuple(e["box"]) for e in shown if e.get("box")}, ann)
                _LAST[n] = {e["id"]: tuple(e["center"]) for e in shown}
                _LAST_EL[n] = {"typed": True, "source": meta["source"], "snapshot": meta["snapshot"],
                               "elements": {e["id"]: _public(e) for e in elems},
                               "handles": {e["id"]: e.get("path") for e in elems}}
                base.update(meta, screen=[img.width, img.height], annotated=ann, count=len(shown),
                            truncated=len(shown) > PARSE_MAX, offscreen=len(elems) - len(shown),
                            elements=[_public(e) for e in shown[:PARSE_MAX]])
                if keep_all:
                    base["_all"] = [_public(e) for e in shown] + [_public(e) for e in elems if not e.get("onscreen")]
                return base
        if source == "a11y":
            _fail("parse_screen", f"no accessibility information for instance {n}: {meta}",
                  fix="use source='vision' (or 'auto'), or screenshot(instance) and click(x, y)", instance=n, vision=mode)
        base["a11y_unavailable"] = meta
    base["source"] = "vision"
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
        _LAST_EL[n] = {"typed": base.get("detector") == "screenparser", "elements": {e["id"]: e for e in elems}}
    total = len(elems)
    truncated = total > PARSE_MAX
    if truncated:
        elems = elems[:PARSE_MAX]
    base.update(screen=[W, H], annotated=ann, count=total, truncated=truncated, elements=elems)
    if mode == "basic":
        base["granularity"] = granularity
    return base


def _parse_region(action, n, region, W, H, undone=""):
    """'x,y,w,h' -> (x1, y1, x2, y2) inside the WxH display, or a structured failure."""
    try:
        x, y, w, h = [int(v) for v in region.split(",")]
        assert w > 0 and h > 0
    except (ValueError, AssertionError):
        _fail(action, f"bad region '{region}'{undone}", fix="use 'x,y,w,h' with positive w,h", instance=n)
    if x < 0 or y < 0 or x + w > W or y + h > H:
        _fail(action, f"region {region} exceeds the {W}x{H} display{undone}",
              fix=f"keep 0 <= x, x+w <= {W} and 0 <= y, y+h <= {H}", instance=n, width=W, height=H)
    return (x, y, x + w, y + h)


def _observe(instance, mode, settle_ms, payload: dict):
    """After an action, optionally return the resulting state in the SAME call.
    mode: 'none' (json only), 'screenshot' (json + image), 'parse' (json incl. elements + image).
    Mode is validated by _validate_observe BEFORE the action runs. The action already happened
    when this runs, so a failed observation is reported inside an ok:true payload ("observed":
    false, "observe_error") — never as a failed action, which would make the agent repeat it."""
    box = None
    if isinstance(mode, tuple):
        mode, box = mode
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
            if box:   # the display may have been resized since validation: clamp, never fail here
                box = (min(box[0], img.width - 1), min(box[1], img.height - 1),
                       min(box[2], img.width), min(box[3], img.height))
                img = img.crop(box)
                payload["observed_region"] = list(box)
            return [_dumps(payload), _encode(img)]
        if mode in ("parse", "elements"):
            pr = _do_parse(instance)
            for k in ("ok", "action", "instance"):
                pr.pop(k, None)
            payload["observed"] = mode
            payload["parse"] = pr
            if mode == "elements":                   # the element list alone: no image tokens
                return _dumps(payload)
            out = [_dumps(payload)]
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
    return _dumps(payload)


def _validate_observe(action, instance, observe, region=""):
    """Reject a bad observe mode/region BEFORE performing the action (never act, then error).
    Returns what _observe takes as `mode`: the mode, or (mode, crop box) with observe_region —
    which implies observe='screenshot'."""
    if observe not in ("none", "screenshot", "parse", "elements"):
        _fail(action, f"unknown observe mode '{observe}' — the action was not performed",
              fix="use observe='none' | 'screenshot' | 'parse' | 'elements'", instance=instance)
    if not region:
        return observe
    if observe in ("parse", "elements"):
        _fail(action, f"observe_region crops a screenshot; it cannot be combined with observe='{observe}' — "
                      "the action was not performed",
              fix="drop observe_region, or use observe='screenshot'", instance=instance)
    W, H = _geometry(instance)
    return ("screenshot", _parse_region(action, instance, region, W, H, " — the action was not performed"))


@mcp.tool(annotations=_READ_ONLY)
def parse_screen(instance: int = 1, box_threshold: float = 0.05, granularity: str = "line",
                 source: str = "auto") -> str:
    """List the on-screen elements with ids + pixel-centers — then click_element(id), act(id) or
    fill(element=id).

    Returns {"ok", "source", "vision", "screen": [W,H], "annotated", "count", "truncated",
    "elements": [{id, type, label, interactive, center: [x,y]}, …]}. Also saves a numbered
    Set-of-Mark image (path in "annotated").
    `source` picks who is asked. 'auto' (default) asks the APP first — accessibility: exact
    labels and real element types, no GPU, tens of milliseconds — and falls back to vision when
    the app exposes fewer than 3 elements (a canvas, a game, a Tk app;
    "a11y_unavailable" says why). "source" in the reply names what answered:
      'browser-dom'  the active tab of a Chromium-family browser (roles, accessible names and
                     states from the DOM, open shadow roots and same-origin iframes included;
                     "frames_skipped": cross-origin iframes that could not be read — the
                     browser's own toolbar is never listed). Elements also carry "role",
                     "states" (disabled, checked, expanded, focused, required, readonly,
                     password, …), "value" (never a password's) and a select's "options";
                     "offscreen" counts the elements outside the viewport, which
                     find_elements can still find and click_element/act scroll into view.
      'at-spi'       a native toolkit app (GTK, Qt, LibreOffice) over the display's own
                     accessibility bus; elements carry "role", "states", "value", "actions".
      'vision'       pixels, as described below.
    source='a11y' fails (isError) instead of falling back; source='vision' skips the app.
    Vision backend = the effective GLOVEBOX_VISION: 'local' (OmniParser on the GPU, ~2 s after a ~10 s
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
    return _dumps(_do_parse(instance, box_threshold, granularity, source))


@mcp.tool(annotations=_READ_ONLY)
def find_elements(query: str, instance: int = 1, type: str = "", limit: int = 20, source: str = "auto") -> str:
    """Parse the screen and return only the elements whose label contains `query` (case-insensitive
    substring; empty query = all), optionally filtered by `type` (button, link, text, icon, …).
    One call instead of parse_screen + scanning 300 elements. The ids are valid for
    click_element(id) right away (this IS the most recent parse).

    Returns {"ok", "instance", "source", "vision", "query", "type", "count": total matches,
    "elements": [{id, type, label, interactive, center}, …] (first `limit`, 1–100), "truncated"}.
    `source`: see parse_screen ('auto' asks the app's accessibility information first, then
    vision). With an accessibility source the search also covers what is scrolled out of view:
    those hits come after the visible ones with "onscreen": false and "center": null (or
    "covered": true under an overlay) — click_element(id), fill(element=id) and act(id) scroll
    them into view themselves. "count": 0 is an honest answer (the text is not there, or OCR did
    not read it — screenshot to check). Fails (isError) like parse_screen; with no accessibility
    information and vision mode 'none' it fails with the fix (screenshot and reason yourself)."""
    n = _require("find_elements", instance)
    limit = max(1, min(int(limit), 100))
    pr = _do_parse(n, source=source, keep_all=True)
    if pr.get("elements") is None:
        _fail("find_elements", "no grounding backend (vision mode 'none') — nothing can be searched",
              fix="screenshot(instance) and reason about the pixels yourself, or install a vision backend "
                  "(./install.sh basic|local); status() shows the vision diagnosis", instance=n, vision=pr["vision"])
    q = (query or "").lower()
    t = (type or "").lower()
    hits = [e for e in pr.get("_all", pr["elements"]) if (not q or q in (e.get("label") or "").lower())
            and (not t or (e.get("type") or "").lower() == t)]
    return _ok("find_elements", n, source=pr.get("source"), vision=pr["vision"], query=query, type=type,
               count=len(hits), elements=hits[:limit], truncated=len(hits) > limit, screen=pr.get("screen"),
               **({"a11y_unavailable": pr["a11y_unavailable"]} if pr.get("a11y_unavailable") else {}),
               **({"note": "nothing in the app's accessibility information matches; text inside an image, a "
                           "canvas or a cross-origin iframe is only visible to source='vision'"}
                  if not hits and q and pr.get("source") in ("browser-dom", "at-spi") and source == "auto" else {}))


@mcp.tool(annotations=_READ_ONLY)
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
        pr = _do_parse(n, keep_all=True)
        if pr.get("elements") is None:
            _fail("wait_for_text", "no grounding backend (vision mode 'none') — cannot read the screen",
                  fix="use wait_for_stable() + screenshot() instead, or install a vision backend", instance=n)
        polls += 1
        hit = next((e for e in pr.get("_all", pr["elements"]) if q in (e.get("label") or "").lower()
                    or q in str(e.get("value") or "").lower()), None)
        found = hit is not None
        satisfied = found if appear else not found
        elapsed = int((time.time() - t0) * 1000)
        if satisfied or elapsed >= timeout_ms:
            return _ok("wait_for_text", n, text=text, appear=appear, found=found, satisfied=satisfied,
                       elapsed_ms=elapsed, polls=polls, timed_out=not satisfied, source=pr.get("source"),
                       element={k: hit.get(k) for k in ("id", "type", "label", "center")} if hit else None)
        time.sleep(interval)


def _pointer(n):
    """(x, y) of the instance's pointer, or None when it cannot be read."""
    try:
        r = _xdo_soft(n, "pointer", "getmouselocation")
    except ToolError:
        return None
    m = re.match(r"x:(-?\d+) y:(-?\d+)", r.stdout or "") if r.returncode == 0 else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def _move(n, action, x, y, at=None):
    """Move the pointer to (x, y) and wait until it is there. `xdotool mousemove --sync` waits
    for the pointer to MOVE — when it already is at the target it waits ~15 s for nothing (a
    second click on the same spot used to hang that long), so that case is skipped. `at`: the
    position the caller already knows (drag steps), to save a lookup."""
    x, y = int(x), int(y)
    if (at or _pointer(n)) != (x, y):
        _xdo(n, action, "mousemove", x, y, opts=("--sync",))
    _PTR[n] = (x, y)
    return (x, y)


def _foreign_input(n) -> dict:
    """{"warnings": [...]} when the pointer is not where this server left it — the person (the
    window is theirs too) or another agent is using the instance, so earlier coordinates and the
    keyboard focus cannot be trusted. Only pointer movement is visible; typing is not. {} otherwise."""
    now = _pointer(n)
    last = _PTR.get(n)
    if now is None:
        return {}
    _PTR[n] = now
    if last and (abs(now[0] - last[0]) > 3 or abs(now[1] - last[1]) > 3):
        return {"warnings": [f"the pointer moved since this server's last action (left at {list(last)}, found at "
                             f"{list(now)}): a person or another agent is using instance {n} — focus and "
                             "earlier coordinates may be stale; screenshot before the next step"]}
    return {}


_HIT_JS = r"""(function(X, Y) {
  var z = window.devicePixelRatio || 1;
  var side = Math.max(0, (window.outerWidth - window.innerWidth * z) / 2);
  var top = Math.max(0, window.outerHeight - window.innerHeight * z - side);
  var vx = (X - window.screenX - side) / z, vy = (Y - window.screenY - top) / z;
  if (vx < 0 || vy < 0 || vx >= window.innerWidth || vy >= window.innerHeight) return {role: "browser-ui"};
  var e = document.elementFromPoint(vx, vy);
  while (e && e.shadowRoot) { var i = e.shadowRoot.elementFromPoint(vx, vy); if (!i || i === e) break; e = i; }
  if (!e) return null;
  var t = e.closest('a[href],button,input,select,textarea,label,summary,[role],[contenteditable=""],[contenteditable="true"]') || e;
  var c = (t.tagName === "LABEL" && t.control) ? t.control : t;
  var tag = c.tagName.toLowerCase(), ty = (c.type || "").toLowerCase();
  var role = c.getAttribute("role") || ({a: "link", button: "button", select: "combobox", textarea: "textbox", summary: "button"})[tag]
    || (tag === "input" ? (({checkbox: "checkbox", radio: "radio", submit: "button", button: "button", reset: "button",
                             range: "slider", file: "file input", image: "button"})[ty] || "textbox")
        : (c.isContentEditable ? "textbox" : tag));
  var lab = (c.labels && c.labels[0]) ? c.labels[0].innerText : "";
  var name = c.getAttribute("aria-label") || lab || c.placeholder
    || ((tag === "input" && /^(button|submit|reset)$/.test(ty)) ? c.value : "")
    || ((tag === "input" || tag === "textarea" || tag === "select") ? "" : c.innerText) || c.alt || c.title || c.name || "";
  var out = {role: role, name: String(name).replace(/\s+/g, " ").trim().slice(0, 80), tag: tag};
  if (ty === "checkbox" || ty === "radio") out.checked_before = !!c.checked;
  if (c.disabled) out.disabled = true;
  if (tag === "a" && c.href) out.href = String(c.href).slice(0, 200);
  if (tag === "iframe") out.note = "inside an iframe - its content is not inspected";
  return out;
})"""

_FOCUS_JS = r"""(function() {
  var e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return null;
  var tag = e.tagName.toLowerCase();
  if (tag === "iframe") return {tag: tag, unknown: true};
  if (tag === "input" || tag === "textarea")
    return {tag: tag, value: String(e.value), password: (e.type || "").toLowerCase() === "password"};
  if (e.isContentEditable) return {tag: tag, value: String(e.innerText)};
  return {tag: tag};
})()"""


_FOCUS_AT_JS = r"""(function(X, Y) {
  var e = document.activeElement;
  while (e && e.shadowRoot && e.shadowRoot.activeElement) e = e.shadowRoot.activeElement;
  if (!e) return null;
  var tag = e.tagName.toLowerCase(), out = {tag: tag};
  if (tag === "iframe") return {tag: tag, unknown: true};
  if (tag === "input" || tag === "textarea") { out.value = String(e.value); out.password = (e.type || "").toLowerCase() === "password"; }
  else if (e.isContentEditable) out.value = String(e.innerText);
  if (out.value === undefined) return out;
  var z = window.devicePixelRatio || 1;
  var side = Math.max(0, (window.outerWidth - window.innerWidth * z) / 2);
  var top = Math.max(0, window.outerHeight - window.innerHeight * z - side);
  var vx = (X - window.screenX - side) / z, vy = (Y - window.screenY - top) / z, S = 8 / z;
  var r = e.getBoundingClientRect();
  var at = vx >= r.left - S && vx < r.right + S && vy >= r.top - S && vy < r.bottom + S;
  if (!at && vx >= 0 && vy >= 0 && vx < window.innerWidth && vy < window.innerHeight) {
    var p = document.elementFromPoint(vx, vy);
    while (p && p.shadowRoot) { var i = p.shadowRoot.elementFromPoint(vx, vy); if (!i || i === p) break; p = i; }
    if (p) {
      var lab = p.closest ? p.closest("label") : null, pr = p.getBoundingClientRect();
      at = p === e || e.contains(p) || (lab && lab.control === e)
        || (p.contains(e) && p !== document.body && p !== document.documentElement
            && pr.height <= Math.max(120, r.height * 4));
    }
  }
  out.at = !!at;
  out.name = String(e.getAttribute("aria-label") || e.name || e.id || e.placeholder || "").slice(0, 60);
  out.rect = [Math.round(window.screenX + side + r.left * z), Math.round(window.screenY + top + r.top * z),
              Math.round(r.width * z), Math.round(r.height * z)];
  return out;
})"""


def _hit(n, x, y):
    """What is under (x, y) BEFORE a click, for a Chromium-family browser on this instance:
    {role, name, tag, checked_before?, disabled?, href?, source} from the page's DOM, or
    {role: 'browser-ui'} outside the page area. None when there is no CDP browser (native apps)
    or the lookup fails — a click must never fail or wait long because of it."""
    try:
        with _cdp(_cdp_port(n), timeout=2) as (cmd, _target, _pages):
            r = cmd("Runtime.evaluate", {"expression": f"{_HIT_JS}({int(x)},{int(y)})", "returnByValue": True})
        v = r.get("result", {}).get("value")
        if isinstance(v, dict):
            v["source"] = "browser-dom"
            return v
    except Exception:
        pass
    return None


def _hit_kw(n, x, y) -> dict:
    h = _hit(n, x, y)
    return {"hit": h} if h else {}


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
    _move(n, action, x, y)
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


@mcp.tool(annotations=_ACTS)
def click(x: int, y: int, instance: int = 1, button: int = 1, count: int = 1, modifiers: str = "",
          observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Click at absolute pixel coords (top-left origin) in an instance.

    For anything with a label, find_elements('Save') -> click_element(id) beats guessing pixels.
    Returns {"ok", "action", "instance", "detail", "hit"?, "warnings"?}. "hit" (Chromium-family
    browsers only, read from the page's DOM just before the click) names what was under the
    point: {"role": "checkbox", "name": "Remember me", "tag", "checked_before", "disabled",
    "href", …} or {"role": "browser-ui"} for the toolbar/tab strip — compare it with what you
    meant to click; it is absent for native apps. "warnings" appears when the pointer was not
    where this server left it (a person or another agent is using the instance — re-screenshot).
    button: 1=left, 2=middle, 3=right.
    count: 1–3 clicks (2 = double-click, 3 = triple-click selects a line/paragraph).
    modifiers: keys held during the click — 'ctrl' (open in new tab / multi-select), 'shift'
    (range select), 'alt', 'super', combinable 'ctrl+shift'; released afterwards whatever happens.
    `observe`='screenshot'|'parse'|'elements' additionally returns the resulting screen state in
    the SAME call (saves a round-trip): the image, the image plus the parsed elements, or the
    element list alone (no image tokens — cheapest when the app exposes accessibility);
    `settle_ms` (max 5000, "settle_clamped": true if you asked for
    more) waits for the UI to update first — use 400–1500 after anything that navigates or
    submits, or wait_for_stable() when you don't know how long. `observe_region`='x,y,w,h' crops
    that screenshot to the part you care about (implies observe='screenshot'; far fewer tokens
    than the full display; validated before the action). Fails (isError) if the instance
    is down, the coordinates are outside the display or a modifier name is unknown (nothing is
    clicked then). A failed observation after a successful click is reported as "observed":
    false inside an ok result — the click is not repeated."""
    n = _require("click", instance)
    observe = _validate_observe("click", n, observe, observe_region)
    count = max(1, min(int(count), 3))
    with _ilock(n):
        extra = {**_foreign_input(n), **_hit_kw(n, x, y)}
        _click_at(n, "click", x, y, button, count, modifiers)
        return _observe(n, observe, settle_ms,
                        {"action": "click", **extra, "detail": f"clicked ({x},{y}) button {button}" + (f" x{count}" if count > 1 else "")
                                                       + (f" with {modifiers}" if modifiers else ""),
                         "count": count, "modifiers": modifiers})


@mcp.tool(annotations=_ACTS)
def drag(x1: int, y1: int, x2: int, y2: int, instance: int = 1, button: int = 1, steps: int = 10,
         observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Drag (mouse-down → move → up) from (x1,y1) to (x2,y2) — for drawing, selecting, sliders.

    Returns {"ok", "action", "instance", "detail", "steps"}. `steps` (default 10, 1–100) is the
    number of intermediate motion events: canvases, sliders and drag-and-drop targets usually
    need a few to register the drag (1 = jump straight to the end point). The button is released
    even if a motion step fails (a stuck button would turn every later click into a drag).
    `observe`/`settle_ms`: see click(). Fails (isError) if the instance is down or either point
    is off-screen."""
    n = _require("drag", instance)
    observe = _validate_observe("drag", n, observe, observe_region)
    _check_xy("drag", n, (x1, y1), (x2, y2))
    steps = max(1, min(int(steps), 100))
    with _ilock(n):
        extra = _foreign_input(n)
        at = _move(n, "drag", x1, y1)
        _xdo(n, "drag", "mousedown", button)
        try:
            for i in range(1, steps + 1):
                xi = round(x1 + (x2 - x1) * i / steps)
                yi = round(y1 + (y2 - y1) * i / steps)
                at = _move(n, "drag", xi, yi, at)
                if steps > 1:
                    time.sleep(0.01)
        finally:
            try:
                _xdo(n, "drag", "mouseup", button)
            except ToolError:
                pass
            _PTR.pop(n, None)   # re-read on the next action: a failed step leaves it mid-way
        return _observe(n, observe, settle_ms,
                        {"action": "drag", **extra, "detail": f"dragged ({x1},{y1})->({x2},{y2})", "steps": steps})


@mcp.tool(annotations=_ACTS)
def double_click(x: int, y: int, instance: int = 1, observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Double-click at absolute pixel coords in an instance (e.g. open a file icon, select a word).
    Same as click(count=2).

    Returns {"ok", "action", "instance", "detail", "hit"?, "warnings"?} — see click().
    `observe`/`settle_ms`/`observe_region`: see click(). Fails (isError) if the instance is down
    or the coordinates are off-screen."""
    n = _require("double_click", instance)
    observe = _validate_observe("double_click", n, observe, observe_region)
    with _ilock(n):
        extra = {**_foreign_input(n), **_hit_kw(n, x, y)}
        _click_at(n, "double_click", x, y, 1, 2)
        return _observe(n, observe, settle_ms,
                        {"action": "double_click", **extra, "detail": f"double-clicked ({x},{y})"})


@mcp.tool(annotations=_ACTS)
def click_element(id: int, instance: int = 1, button: int = 1, modifiers: str = "", observe: str = "none",
                  settle_ms: int = 0, observe_region: str = ""):
    """Click an element by id from that instance's most recent parse_screen/find_elements (no
    coordinate guessing).

    Returns {"ok", "action", "instance", "detail", "element", "center", "hit"?, "warnings"?}
    ("hit"/"warnings": see click()). Fails (isError) with the
    fix named if the id is unknown — run parse_screen(instance) first. Ids from a vision parse
    are remembered pixels: they go stale when the screen changes, so re-parse after navigation.
    Ids from an accessibility source (parse "source": browser-dom | at-spi) are re-located just
    before the click: an element that moved is clicked where it is NOW ("moved_from"), one that
    is scrolled out of view is scrolled in first ("scrolled": true), and one that is gone fails
    with "stale": true and nothing clicked. `modifiers`/`observe`/`settle_ms`: see click()."""
    n = _require("click_element", instance)
    observe = _validate_observe("click_element", n, observe, observe_region)
    with _ilock(n):
        x, y, found = _element_target(n, "click_element", id)
        extra = {**_foreign_input(n), **_hit_kw(n, x, y)}
        if found.get("warnings"):
            extra["warnings"] = found.pop("warnings") + extra.get("warnings", [])
        extra.update(found)
        _click_at(n, "click_element", x, y, button, 1, modifiers)
        return _observe(n, observe, settle_ms,
                        {"action": "click_element", **extra, "detail": f"clicked element {id} at ({x},{y})",
                         "element": id, "center": [x, y]})


@mcp.tool(annotations=_ACTS)
def act(element: int, action: str = "press", value: str = "", instance: int = 1, allow_password: bool = False,
        observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Operate an element through the app's own accessibility interface — no pointer, no
    keyboard, no coordinates. `element` is an id from a parse_screen/find_elements whose
    "source" was browser-dom or at-spi (not vision).

    action='press'            activate it (button, link, checkbox, tab, menu item). Browser: a
                              real, trusted click delivered to the page without moving the X
                              pointer ("method": "cdp-input"); when the element is covered or
                              out of view its click handler is invoked directly instead
                              ("method": "dom-click", "synthetic": true — some pages ignore
                              those). Native: the element's AT-SPI action ("at_spi_action").
    action='set_value'        set `value`: text for a field (browser: trusted insert-text
                              events, then a direct set if the page reformatted it), an
                              option's text for a <select>, 'true'/'false' for a checkbox or
                              radio, a number for a slider. Replaces the old content.
    action='focus'            give it the keyboard focus (then type_text/press_keys).
    action='scroll_into_view' bring an off-screen element into the viewport.
    Returns {"ok", "action", "instance", "element", "label", "type", "did", "method",
    "before": {states, value}, "after": {states, value}, "center"?, "verified"?: true|false|null
    (set_value: the value read back equals `value`; null = unreadable, e.g. a password),
    "scrolled"?, "synthetic"?, "note"?}. Compare before/after to see the effect (e.g. "checked"
    appearing). Fails (isError), nothing done, when: the id is unknown or from a vision parse;
    the element is gone ("stale": true — find_elements again); it is disabled or read-only; a
    password field without allow_password=true; the app exposes no such action for it (then
    click_element(id) / fill(element=id) use the pointer and keyboard instead); a <select> has
    no such option ("options" lists them). Prefer fill() when the page must see real
    keystrokes (autocomplete, per-key validation). `observe`/`settle_ms`/`observe_region`: see
    click()."""
    n = _require("act", instance)
    observe = _validate_observe("act", n, observe, observe_region)
    if action not in a11y.ACTIONS:
        _fail("act", f"unknown action '{action}' — nothing was done",
              fix="use action='press' | 'set_value' | 'focus' | 'scroll_into_view'", instance=n)
    last = _LAST_EL.get(n) or {}
    els = last.get("elements") or {}
    el = els.get(element)
    if el is None:
        _fail("act", f"unknown element id {element} for instance {n} — nothing was done",
              fix="run find_elements/parse_screen first — ids are only valid for the most recent parse",
              instance=n, known_ids=len(els))
    src = last.get("source")
    label = (el.get("label") or "")[:60]
    if not src:
        _fail("act", f"element {element} comes from a vision parse — it is a position on screen, not a handle "
                     "into the app; nothing was done",
              fix="click_element(id) / fill(element=id) work on vision ids; for act() list the elements with "
                  "find_elements(..., source='a11y')", instance=n)
    if action == "set_value" and "password" in (el.get("states") or []) and not allow_password:
        _fail("act", f"element {element} ('{label}') is a password field — nothing was set",
              fix="pass allow_password=true if this is the field you mean", instance=n)
    with _ilock(n):
        try:
            if src == "browser-dom":
                with _cdp(_cdp_port(n), timeout=8) as (cmd, _target, _pages):
                    r = a11y.browser_act(cmd, last["snapshot"], element, action, value)
            else:
                py, env = _a11y_env(n)
                if not py:
                    raise RuntimeError(env)
                r = a11y.atspi_call(py, env, "act", json.dumps(last["handles"].get(element)), el.get("role", ""),
                                    el.get("label", ""), action, value)
                r.pop("ok", None)
        except a11y.Stale as e:
            _fail("act", f"element {element} ('{label}') is stale: {e} — nothing was done",
                  fix="find_elements/parse_screen again and use the new id", instance=n, stale=True)
        except Exception as e:
            kw = {"options": e.options} if getattr(e, "options", None) else {}
            _fail("act", f"could not {action} element {element} ('{label}'): {e} — nothing was done",
                  fix="click_element(id) / fill(element=id) use the pointer and keyboard instead"
                      if getattr(e, "no_action", False) else
                      "check the element's \"states\" (disabled, readonly) and \"options\"; find_elements again "
                      "if the screen changed", instance=n, **kw)
        res = {"action": "act", "element": element, "label": label, "type": el.get("type"), "did": action, **r}
        if action == "set_value":
            after = (r.get("after") or {})
            if "password" in (el.get("states") or []) or after.get("value") is None:
                res["verified"] = None if el.get("type") not in ("checkbox", "radio", "switch") else \
                    (("checked" in (after.get("states") or [])) == (value.strip().lower() in ("true", "1", "on", "yes", "checked")))
            else:
                got, want = str(after["value"]), value
                try:                                 # sliders and spin buttons: 5 == 5.0
                    res["verified"] = got == want or float(got.replace(",", ".")) == float(want.replace(",", "."))
                except ValueError:
                    res["verified"] = got == want or got.strip().lower() == want.strip().lower()
        res["detail"] = f"{action} on element {element} ('{label}') via {r.get('method', src)}"
        return _observe(n, observe, settle_ms, res)


def _element_target(n, action, id):
    """(x, y, info) — where to point at element `id` of instance n's latest parse, right now.
    Vision ids are remembered pixels. Accessibility ids are looked up again in the app: the
    element may have moved (info.moved_from), be scrolled out of view (scrolled in first,
    info.scrolled) or be gone (fails with stale: true — nothing was done)."""
    last = _LAST_EL.get(n) or {}
    els = last.get("elements") or {}
    el = els.get(id)
    src = last.get("source")
    if el is None:
        _fail(action, f"unknown element id {id} for instance {n} — nothing was done",
              fix="run parse_screen/find_elements first — ids are only valid for the most recent parse",
              instance=n, known_ids=len(els))
    if not src:
        return (*el["center"], {})
    label = (el.get("label") or "")[:40]

    def stale(why):
        _fail(action, f"element {id} ('{label}') is stale: {why} — nothing was done",
              fix="find_elements/parse_screen again and use the new id", instance=n, stale=True)

    info, p = {}, None
    try:
        if src == "browser-dom":
            with _cdp(_cdp_port(n), timeout=5) as (cmd, _target, _pages):
                p = a11y.browser_locate(cmd, last["snapshot"], id)
            if p.get("covered"):
                info["warnings"] = [f"element {id} ('{label}') is covered by <{p.get('by', '?')}> at its centre — the "
                                    "click goes to whatever is on top (see \"hit\"); act(id) invokes the element itself"]
        else:
            py, env = _a11y_env(n)
            if not py:
                raise RuntimeError(env)
            p = a11y.atspi_call(py, env, "locate", json.dumps(last["handles"].get(id)), el.get("role", ""),
                                el.get("label", ""))
    except a11y.Stale as e:
        stale(str(e))
    except ToolError:
        raise
    except Exception as e:                           # the lookup itself failed: fall back to where it was
        if not el.get("center"):
            _fail(action, f"element {id} ('{label}') could not be located ({e}) — nothing was done",
                  fix="find_elements/parse_screen again", instance=n)
        return (*el["center"], {"warnings": [f"element {id} could not be re-located ({str(e)[:80]}); "
                                              "used the position from the parse"]})
    if not p.get("center"):
        _fail(action, f"element {id} ('{label}') is not visible (hidden, zero-sized or in a closed menu) — "
                      "nothing was done",
              fix="open the menu/section that contains it, or act(id) to invoke it without the pointer", instance=n)
    x, y = (int(v) for v in p["center"])
    if p.get("scrolled"):
        info["scrolled"] = True
        time.sleep(0.15)                             # let the scroll paint before the pointer moves
    elif el.get("center") and (abs(x - el["center"][0]) > 3 or abs(y - el["center"][1]) > 3):
        info["moved_from"] = list(el["center"])
    return x, y, info


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


def _type_into(n, action, text, clear=False) -> str:
    """Type `text` into the focused field (see type_text for why non-ASCII goes via the
    clipboard). Returns the method used. Caller holds the instance lock."""
    if clear:
        _xdo(n, action, "key", "ctrl+a", opts=("--clearmodifiers",))
    if any(ord(c) > 127 for c in text):
        if not shutil.which("xclip"):
            _fail(action, "text contains non-ASCII characters but xclip is not installed — "
                               "typing it via xdotool would silently drop characters in some toolkits",
                  fix="install xclip (e.g. `sudo apt install xclip`) or send ASCII-only text",
                  instance=n)
        prev, _err = _xclip_read(n, action)        # restored below: with the host clipboard
        r = _xclip(n, action, "-i", input=text.encode("utf-8"))   # bridge, agent text must not linger
        if r.returncode != 0:
            if not _up(n):
                _fail_down(action, n)
            _fail(action, "xclip failed to set the clipboard", instance=n)
        time.sleep(0.05)
        _xdo(n, action, "key", "ctrl+v", opts=("--clearmodifiers",))
        time.sleep(0.3)   # let the app finish reading the selection before it changes again
        try:
            _xclip(n, action, "-i", input=(prev or "").encode("utf-8"))
        except ToolError:
            pass          # best-effort: the text was typed; a failed restore must not fail the call
        method = "clipboard"
    else:
        _xdo(n, action, "type", text, opts=("--clearmodifiers", "--delay", "15"),
             timeout=XDO_TIMEOUT + 0.03 * len(text))
        method = "xdotool"
    return method


@mcp.tool(annotations=_ACTS)
def type_text(text: str, instance: int = 1, clear: bool = False, submit: bool = False,
              observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Type into the focused field — unicode-safe. Click the field first to focus it.

    To set a form field in one verified call use fill() instead (click + replace + read back).
    Returns {"ok", "action", "instance", "detail", "method": "xdotool"|"clipboard", "warnings"?}.
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
    observe = _validate_observe("type_text", n, observe, observe_region)
    with _ilock(n):
        extra = _foreign_input(n)
        method = _type_into(n, "type_text", text, clear)
        if submit:
            _xdo(n, "type_text", "key", "Return", opts=("--clearmodifiers",))
        return _observe(n, observe, settle_ms,
                        {"action": "type_text", **extra, "detail": f"typed {len(text)} chars" + (" + Return" if submit else ""),
                         "method": method, "cleared": bool(clear), "submitted": bool(submit)})


def _a11y_python():
    """An interpreter that can import Atspi — normally the SYSTEM python3 (the venv has no
    PyGObject) — or None. Probed at most once a minute, so installing the packages takes effect
    without a restart."""
    if time.time() - _A11Y["at"] < 60:
        return _A11Y["python"]
    found = None
    for c in dict.fromkeys(x for x in (sys.executable, "/usr/bin/python3", shutil.which("python3")) if x):
        try:
            r = subprocess.run([c, "-I", A11Y_HELPER, "--check"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and json.loads(r.stdout or "{}").get("ok"):
                found = c
                break
        except Exception:
            continue
    _A11Y.update(at=time.time(), python=found)
    return found


def _a11y_focus(n, at=None):
    """(focus | None, why): the widget with keyboard focus on instance n, asked from THAT
    display's accessibility bus (its address is a property of the display's root window; the
    host's session bus is removed from the helper's environment so the host's focus can never be
    reported). focus = {role, name, rect, editable, password, app}; None + why when it cannot be
    known (packages missing, no bus on the display, the app exposes nothing)."""
    py = _a11y_python()
    if not py:
        return None, f"AT-SPI bindings are not installed on the host ({A11Y_APT})"
    try:
        r = subprocess.run(["xprop", "-root", "-display", _disp(n), "AT_SPI_BUS"], capture_output=True,
                           text=True, timeout=3)
        m = re.search(r'=\s*"(.+)"', r.stdout or "")
        if not m:
            return None, "no accessibility bus on this display (the app does not speak AT-SPI)"
        env = {**_env(n), "AT_SPI_BUS_ADDRESS": m.group(1)}
        env.pop("DBUS_SESSION_BUS_ADDRESS", None)
        out = subprocess.run([py, "-I", A11Y_HELPER] + ([str(int(at[0])), str(int(at[1]))] if at else []),
                             env=env, capture_output=True, text=True, timeout=5)
        d = json.loads(out.stdout or "{}")
    except Exception as e:
        return None, f"the accessibility lookup failed ({type(e).__name__})"
    if not d.get("ok"):
        return None, f"the accessibility lookup failed ({str(d.get('error'))[:120]})"
    if not d.get("focus"):
        return None, "the app exposes no focused widget over AT-SPI (canvas, game or Xlib-only app)"
    f = d["focus"]
    if f.get("container"):
        # the app only says "the focus is in this window" (LibreOffice). If the widget it reports
        # AT the clicked point is editable, that is where the keys go; anything else is unknown.
        ap = d.get("at_point") or {}
        if ap.get("editable"):
            return {**ap, "via": "at-point"}, ""
        return None, (f"the app reports the focus only as its {f.get('role')} — not which widget inside it"
                      + (f" (at the point: {ap.get('role')} '{(ap.get('name') or '')[:30]}')" if ap else ""))
    return f, ""


def _focus_guard(n, x, y, allow_password, extra) -> dict:
    """Between fill's click and its typing: is the keyboard focus on an editable field AT the
    clicked point? Raises (nothing typed yet) when it provably is not, or when it is a password
    field the caller did not announce. Returns {"focus_checked": "browser-dom" | "at-spi" | False,
    "focus"?, "focus_unchecked_reason"?} — False means nobody could tell, never "it was fine"."""
    pw_fix = ("pass allow_password=true if you mean to type a password here; otherwise the click landed on "
              "the wrong field — screenshot and target the right one")
    v = None
    try:
        with _cdp(_cdp_port(n), timeout=3) as (cmd, _target, _pages):
            v = cmd("Runtime.evaluate", {"expression": f"{_FOCUS_AT_JS}({int(x)},{int(y)})",
                                         "returnByValue": True}).get("result", {}).get("value")
    except Exception:
        pass
    if isinstance(v, dict):
        if v.get("unknown"):
            return {"focus_checked": False, "focus_unchecked_reason": "the focus is inside an iframe"}
        if not isinstance(v.get("value"), str):
            _fail("fill", f"after the click the browser's focus is on <{v.get('tag')}>, not a text field — the click "
                          f"at ({x},{y}) missed; nothing was typed",
                  fix="screenshot / find_elements to locate the field, then fill() again; for a custom widget "
                      "use click + type_text", instance=n, **extra)
        if v.get("at") is False:
            # the page kept its focus: the click hit the browser's own toolbar, or an element that
            # refuses focus — typing now would land in a field somewhere else on the page
            focus = {"role": v.get("tag"), "name": v.get("name"), "rect": v.get("rect")}
            _fail("fill", f"the click at ({x},{y}) did not put the focus there: it is on the <{v.get('tag')}>"
                          + (f" '{v['name']}'" if v.get("name") else "") + f" at {v.get('rect')} (x,y,w,h), which is not "
                          "at that point — nothing was typed",
                  fix="for the address bar use browser(action='navigate'); for a page field, screenshot / "
                      "find_elements to locate it; for a custom widget use click + type_text",
                  instance=n, focus=focus, **extra)
        if v.get("password") and not allow_password:
            _fail("fill", f"the field focused by the click at ({x},{y}) is a password field — nothing was typed",
                  fix=pw_fix, instance=n, **extra)
        return {"focus_checked": "browser-dom"}
    f, why = _a11y_focus(n, (x, y))
    if f is None:
        return {"focus_checked": False, "focus_unchecked_reason": why}
    focus = {"role": f.get("role"), "name": f.get("name"), "rect": f.get("rect")}
    if f.get("via"):
        focus["via"] = f["via"]     # weaker evidence: the editable widget under the click, focus in its window
    what = f"{focus['role']}" + (f" '{focus['name']}'" if focus["name"] else "")
    if not f.get("editable"):
        _fail("fill", f"after the click the keyboard focus is on a {what}, not an editable field — the click at "
                      f"({x},{y}) missed; nothing was typed",
              fix="screenshot to locate the field, then fill() again", instance=n, focus=focus, **extra)
    r = f.get("rect") or []
    if len(r) == 4 and r[2] > 0 and r[3] > 0 and not (
            r[0] - FOCUS_SLACK <= x < r[0] + r[2] + FOCUS_SLACK and r[1] - FOCUS_SLACK <= y < r[1] + r[3] + FOCUS_SLACK):
        _fail("fill", f"the click at ({x},{y}) did not move the focus: it is still on the {what} at {r} (x,y,w,h), "
                      "which does not contain that point — nothing was typed",
              fix=f"you clicked something that takes no focus (a label?); click inside {r} to fill that field, "
                  "or screenshot to find the one you meant", instance=n, focus=focus, **extra)
    if f.get("password") and not allow_password:
        _fail("fill", f"the field focused by the click at ({x},{y}) is a password field — nothing was typed",
              fix=pw_fix, instance=n, focus=focus, **extra)
    return {"focus_checked": "at-spi", "focus": focus}


def _label_warning(n, element):
    """With a detector that knows element types (ScreenParser): warn when fill() targets a text
    label / image, and name the nearest input to its right or below. {} otherwise — OCR-only
    backends call everything 'text', so they cannot tell."""
    last = _LAST_EL.get(n) or {}
    els = last.get("elements") or {}
    el = els.get(element)
    if not last.get("typed") or not el or el.get("type") not in vision.SP_NON_INTERACTIVE:
        return {}
    if not el.get("center"):
        return {}
    ex, ey = el["center"]
    best = None
    for c in els.values():
        if c.get("type") != "input" or not c.get("center"):
            continue
        dx, dy = c["center"][0] - ex, c["center"][1] - ey
        if (dx > 0 and abs(dy) <= 25) or (dy > 0 and abs(dx) <= 250):
            d = (dx * dx + dy * dy) ** 0.5
            if best is None or d < best[0]:
                best = (d, c)
    out = {"warnings": [f"element {element} is a {el.get('type')} label ('{(el.get('label') or '')[:40]}'), not an "
                        "input field" + (f" — the nearest input is element {best[1]['id']}" if best else "")]}
    if best:
        out["suggest_element"] = {"id": best[1]["id"], "label": best[1].get("label"), "center": list(best[1]["center"])}
    return out


def _click_effect(n, x, y, before):
    """'changed' | 'none' | None: did anything within ~150x50 px of the click point change?
    Two frames after the click, so a caret that happens to be in its 'off' blink phase in one of
    them is not read as 'nothing happened'. Evidence only — a click on an already-focused field
    changes nothing either."""
    if before is None:
        return None
    try:
        for i in range(2):
            if i:
                time.sleep(0.15)
            if _changed_fraction(before, _crop_around(_grab(n), x, y)) > 0.0005:
                return "changed"
        return "none"
    except Exception:
        return None


def _crop_around(img, x, y):
    return img.crop((max(0, x - 150), max(0, y - 50), min(img.width, x + 150), min(img.height, y + 50)))


def _read_field(n, action):
    """Read back the focused field after a fill → (value | None, how, focus_tag | None).
    Browser: the focused element's value over CDP (exact, clipboard untouched); a focus that is
    not a text field comes back as (None, 'browser-dom', tag). Anything else: select-all + copy
    through the clipboard behind a sentinel, so 'nothing copyable' cannot be mistaken for the
    text we just pasted; (None, 'unreadable', None) then. The clipboard is restored."""
    try:
        with _cdp(_cdp_port(n), timeout=3) as (cmd, _target, _pages):
            r = cmd("Runtime.evaluate", {"expression": _FOCUS_JS, "returnByValue": True})
        v = r.get("result", {}).get("value")
        if isinstance(v, dict) and not v.get("unknown"):
            return (v["value"] if isinstance(v.get("value"), str) else None), "browser-dom", v.get("tag")
    except Exception:
        pass
    if not shutil.which("xclip"):
        return None, "unreadable", None
    sentinel = f"{host_bridge.PRIVATE_PREFIX}{os.getpid()}-{time.time_ns()}"
    try:
        prev, _err = _xclip_read(n, action)
        _xclip(n, action, "-i", input=sentinel.encode())
        time.sleep(0.05)
        _xdo(n, action, "key", "ctrl+a", "ctrl+c", opts=("--clearmodifiers", "--delay", "40"))
        time.sleep(0.2)
        got, _err = _xclip_read(n, action)
        _xclip(n, action, "-i", input=(prev or "").encode("utf-8"))
    except ToolError:
        return None, "unreadable", None
    if got is None or got == sentinel:
        return None, "unreadable", None
    _xdo_soft(n, action, "key", "--clearmodifiers", "End")   # collapse the selection we made
    return got, "clipboard", None


@mcp.tool(annotations=_ACTS)
def fill(text: str, x: int = -1, y: int = -1, instance: int = 1, element: int = -1, submit: bool = False,
         verify: bool = True, allow_password: bool = False, observe: str = "none", settle_ms: int = 0,
         observe_region: str = ""):
    """Set a form field in ONE verified call: click it, check the focus, replace its content
    with `text`, read the value back. Use it instead of click + type_text + screenshot for every
    field of a form.

    Target the field with x,y (absolute pixels) or `element` (an id from parse_screen/
    find_elements). Steps and what each can tell you:
      1. click, then a FOCUS CHECK before anything is typed — browsers through the page's DOM,
         native GTK/Qt/LibreOffice apps through the accessibility bus (AT-SPI). If the focus is
         not on an editable field, or is on a field whose rectangle does not contain the click
         (you clicked a label; the focus stayed where it was), the call FAILS with nothing typed
         and "focus" naming what has it. A password field is refused the same way unless
         allow_password=true — landing in one by accident is the case worth stopping.
      2. the old content is replaced (select-all, then type; text="" empties the field);
      3. the value is read back; submit=true presses Return only if nothing failed.
    Returns {"ok", "action", "instance", "detail", "method", "center",
     "focus_checked": "browser-dom"|"at-spi"|false, "focus"?: {role, name, rect},
     "verified": true|false|null, "read_back": "browser-dom"|"clipboard"|"unreadable"|"skipped",
     "value"?, "click_effect"?: "changed"|"none", "suggest_element"?, "hit"?, "warnings"?}:
      focus_checked false — nobody could tell which widget had focus (canvas, game, Xlib-only
        app, iframe, or AT-SPI not installed: "focus_unchecked_reason"). The text was typed
        blind; "warnings" says so and "click_effect" reports whether the click changed any pixel
        near the target ("none" = probably a static label). Treat such a fill as UNCONFIRMED
        and screenshot.
      verified true  — the focused field holds exactly `text` (trailing newline ignored);
      verified false — it holds something else ("value", first 200 chars): the field reformats
        or limits input (dates, phone masks, maxlength);
      verified null  — the value could not be read (native password field, canvas).
    With `element`, an accessibility source or a typed detector (ScreenParser) warns when the
    element is a text label and names the nearest input ("suggest_element"); accessibility ids
    are re-located first (see click_element). Also fails (isError) when the field is still
    empty after typing; repeating fill() is always safe (it replaces). Native read-back goes
    through select-all + copy (the clipboard is restored). `observe`/`settle_ms`/
    `observe_region`: see click()."""
    n = _require("fill", instance)
    observe = _validate_observe("fill", n, observe, observe_region)
    extra = {}
    if int(element) >= 0:
        x, y, found = _element_target(n, "fill", element)
        extra = _label_warning(n, element)
        if found.get("warnings"):
            extra["warnings"] = extra.get("warnings", []) + found.pop("warnings")
        extra.update(found)
    elif x < 0 or y < 0:
        _fail("fill", "no target — nothing was done", fix="pass x and y (pixels) or element=<id from find_elements>",
              instance=n)
    x, y = int(x), int(y)
    with _ilock(n):
        warnings = extra.pop("warnings", []) + _foreign_input(n).get("warnings", [])
        extra.update(_hit_kw(n, x, y))
        before = None
        if "hit" not in extra:            # no browser: keep a frame for the click_effect fallback
            _check_xy("fill", n, (x, y))
            try:
                before = _crop_around(_grab(n), x, y)
            except Exception:
                pass
        _click_at(n, "fill", x, y)
        time.sleep(0.15)                  # focus handlers run before we look
        if warnings:
            extra["warnings"] = warnings
        res = {"center": [x, y], **_focus_guard(n, x, y, allow_password, extra)}
        if res["focus_checked"] is False:
            effect = _click_effect(n, x, y, before)
            if effect:
                res["click_effect"] = effect
            warnings.append("typed WITHOUT knowing which field had focus (" + res["focus_unchecked_reason"] + ")"
                            + (" and the click changed nothing near the target — it may have hit a static label"
                               if effect == "none" else "") + "; screenshot to confirm where the text went")
        if text == "":
            _xdo(n, "fill", "key", "ctrl+a", "BackSpace", opts=("--clearmodifiers", "--delay", "40"))
            method = "xdotool"
        else:
            method = _type_into(n, "fill", text, clear=True)
        res.update(method=method, verified=None, read_back="skipped")
        if verify and (res.get("focus") or {}).get("role") == "password text":
            # toolkits refuse to copy out of a password entry (and GTK makes the attempt slow)
            res.update(read_back="unreadable", note="a native password field cannot be read back; the focus "
                                                    "check confirmed the field")
        elif verify:
            time.sleep(0.15)
            got, how, tag = _read_field(n, "fill")
            res["read_back"] = how
            norm = lambda v: v.replace("\r\n", "\n").rstrip("\n")
            if got is None and how == "browser-dom":
                _fail("fill", f"after typing, the browser's focus is on <{tag}>, not a text field — the page moved "
                              "the focus while typing; the text may be incomplete (nothing was submitted)",
                      fix="screenshot to see the field's state, then fill() again", instance=n, **extra)
            if got is None:
                res["note"] = "the value could not be read back — screenshot to confirm"
            elif norm(got) == norm(text):
                res["verified"] = True
            elif got == "" and text != "":
                _fail("fill", f"the field at ({x},{y}) is still empty after typing {len(text)} chars (nothing was submitted)",
                      fix="the field may be read-only or not focused — screenshot, then click it and retry",
                      instance=n, read_back=how, **extra)
            else:
                res.update(verified=False, value=got[:200])
                warnings.append("the field holds a different value than the text sent — it reformats or limits "
                                "input, or the paste partly missed; check \"value\"")
        if submit:
            _xdo(n, "fill", "key", "Return", opts=("--clearmodifiers",))
        if warnings:
            extra["warnings"] = warnings
        return _observe(n, observe, settle_ms,
                        {"action": "fill", **extra, **res, "submitted": bool(submit),
                         "detail": f"filled {len(text)} chars at ({x},{y})" + (" + Return" if submit else "")})


@mcp.tool(annotations=_ACTS)
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


@mcp.tool(annotations=_ACTS)
def upload_file(filepath: str, instance: int = 1, selector: str = "input[type=file]",
                page_url_contains: str = "", observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
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
    observe = _validate_observe("upload_file", n, observe, observe_region)
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


_VIEWPORT_JS = r"""(function() {
  var z = window.devicePixelRatio || 1;
  var side = Math.max(0, (window.outerWidth - window.innerWidth * z) / 2);
  var top = Math.max(0, window.outerHeight - window.innerHeight * z - side);
  return {dpr: z, left: Math.round(window.screenX + side), top: Math.round(window.screenY + top),
          css_width: window.innerWidth, css_height: window.innerHeight};
})()"""


@mcp.tool(annotations=_ACTS)
def browser(action: str = "info", instance: int = 1, url: str = "", js: str = "",
            page_url_contains: str = "", max_chars: int = 0, timeout_s: float = 30) -> str:
    """Talk to the instance's Chromium-family browser over the Chrome DevTools Protocol — exact
    navigation and page text without keystrokes or OCR.

    action='info'      → {"ok", "url", "title", "pages": [{url, title}, …], "viewport"} of the
                         active tab + all tabs. `viewport` = {dpr, left, top, css_width,
                         css_height} maps the page's CSS pixels (getBoundingClientRect) to the
                         pixels click()/screenshot() use: x = left + css_x*dpr, y = top + css_y*dpr
                         (dpr is not 1 on a zoomed page; top is the toolbar height)
    action='navigate'  → loads `url` in the active tab (waits for the load event, max 30 s);
                         returns {"ok", "url", "title", "loaded": bool}
    action='text'      → {"ok", "url", "title", "text", "length", "truncated"}: the rendered page
                         text (document.body.innerText), capped at `max_chars` (default 20000,
                         max 200000)
    action='eval'      → {"ok", "result"}: the JSON value of the JavaScript expression `js`
                         (e.g. "document.querySelector('h1').innerText"); a Promise is awaited.
                         A result whose JSON is longer than `max_chars` (default 100000, max
                         1000000) comes back as {"ok", "truncated": true, "length", "result_json":
                         the first max_chars characters} — raise max_chars or return less
                         (slice/map in the script). Errors in the page script come back as a
                         failure with the message
    `timeout_s` (1–600, default 30) bounds an eval or text call. On a timeout the call fails
    (isError, "timed_out": true) and the connection stays usable, but the script KEEPS RUNNING
    in the page — for long work store the outcome on window (js="fetchAll().then(r => {
    window.__r = r }); 'started'") and poll with short evals.
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
    if not (1 <= float(timeout_s) <= 600):
        _fail("browser", f"timeout_s {timeout_s} out of range — nothing was run", fix="use 1 <= timeout_s <= 600", instance=n)
    if int(max_chars) < 0:
        _fail("browser", f"max_chars {max_chars} is negative — nothing was run", fix="omit max_chars or pass a positive number",
              instance=n)
    wait = float(timeout_s)
    try:
        with _cdp(_cdp_port(n), page_url_contains) as (cmd, target, pages):
            if action == "info":
                viewport = None
                try:   # a busy page must not cost `info` its answer
                    viewport = cmd("Runtime.evaluate", {"expression": _VIEWPORT_JS, "returnByValue": True},
                                   wait=3).get("result", {}).get("value")
                except Exception:
                    pass
                return _ok("browser", n, url=target.get("url", ""), title=target.get("title", ""),
                           pages=[{"url": t.get("url", ""), "title": t.get("title", "")} for t in pages],
                           viewport=viewport)
            if action == "navigate":
                cmd("Page.enable")
                r = cmd("Page.navigate", {"url": url}, wait=30)
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
            r = cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}, wait=wait)
            if r.get("exceptionDetails"):
                ex = r["exceptionDetails"]
                msg = (ex.get("exception") or {}).get("description") or ex.get("text") or "script error"
                _fail("browser", f"the script threw: {msg[:300]}", fix="fix the expression; it runs in the page's main frame",
                      instance=n)
            val = r.get("result", {}).get("value")
            if action == "text":
                text = val or ""
                cap = max(100, min(int(max_chars) or TEXT_MAX, 200_000))
                t = cmd("Runtime.evaluate", {"expression": "[location.href, document.title]", "returnByValue": True})
                href, title = (t.get("result", {}).get("value") or ["", ""])[:2]
                return _ok("browser", n, url=href, title=title, text=text[:cap], length=len(text),
                           truncated=len(text) > cap)
            cap = max(100, min(int(max_chars) or EVAL_MAX, 1_000_000))
            as_json = json.dumps(val, ensure_ascii=False)
            if len(as_json) > cap:
                return _ok("browser", n, truncated=True, length=len(as_json), result_json=as_json[:cap],
                           note=f"the result's JSON is {len(as_json)} characters; only the first {cap} are in "
                                "result_json (cut mid-value, so not parseable)",
                           fix=f"pass max_chars up to 1000000, or return less from the script (slice/map), or "
                               "keep it in the page (window.__r = …) and read it in pieces")
            return _ok("browser", n, result=val)
    except ToolError:
        raise
    except CdpTimeout:
        _fail("browser", f"the {'script' if action == 'eval' else action} did not finish within {wait:g} s — "
                         "no result was returned; a script already started keeps running in the page",
              fix="pass a larger timeout_s (max 600), or start the work without awaiting it and poll: "
                  "js=\"work().then(r => { window.__r = r }); 'started'\" then js=\"window.__r\"; if even "
                  "js=\"1\" times out the page's main thread is blocked — screenshot to see why",
              instance=n, timed_out=True)
    except Exception as e:
        _fail("browser", f"CDP call failed: {e}",
              fix="the browser must be a Chromium-family browser started by launch_app/start-display.sh "
                  f"(CDP port {_cdp_port(n)}); window_info() shows whether a browser window exists", instance=n)


@mcp.tool(annotations=_ADDITIVE)
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


@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_ADDITIVE)
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


@mcp.tool(annotations=_ACTS)
def press_keys(keys: str, instance: int = 1, observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Press a key, a combo, or a SEQUENCE (xdotool keysym syntax, space-separated for sequences):
    'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5', 'ctrl+a Delete', 'alt+F4', 'shift+Insert'.

    Returns {"ok", "action", "instance", "detail", "keys": [...]}. Every token is validated
    against the X keysym table BEFORE anything is pressed, so an invalid name fails with nothing
    pressed — also in the middle of a sequence. Fails (isError) on a dead instance or a frozen
    display. Browser navigation: 'ctrl+l' → type_text(url) → 'Return' (or browser(action=
    'navigate')). `observe`/`settle_ms`: see click()."""
    n = _require("press_keys", instance)
    observe = _validate_observe("press_keys", n, observe, observe_region)
    toks = keys.split()
    if not toks:
        _fail("press_keys", "no keys given", fix="pass e.g. 'Return' or 'ctrl+a Delete'", instance=n)
    _validate_keys("press_keys", n, toks)
    with _ilock(n):
        extra = _foreign_input(n)
        r = _xdo(n, "press_keys", "key", *toks, opts=("--clearmodifiers", "--delay", "40"))
        err = r.stderr or ""
        # second line of defence (libX11 missing): xdotool exits 0 on unknown keysyms
        if "No such key name" in err or "Invalid key sequence" in err:
            bad = [l.strip() for l in err.splitlines() if "key" in l][:2]
            _fail("press_keys", f"xdotool ignored part of '{keys}': {' | '.join(bad)[:200]} — the valid tokens before it WERE pressed",
                  fix="use xdotool keysym syntax: 'Return', 'ctrl+a', 'Tab', 'ctrl+t', 'F5'; sequences are space-separated",
                  instance=n)
        return _observe(n, observe, settle_ms,
                        {"action": "press_keys", **extra, "detail": f"pressed {keys}", "keys": toks})


@mcp.tool(annotations=_ADDITIVE)
def move_mouse(x: int, y: int, instance: int = 1) -> str:
    """Move the mouse pointer to absolute pixel coords in an instance (no click).

    Returns {"ok", "action", "instance", "detail"}. Useful for hover states (menus, tooltips) —
    follow with screenshot() to see the effect. Fails (isError) if the instance is down or the
    coordinates are off-screen."""
    n = _require("move_mouse", instance)
    _check_xy("move_mouse", n, (x, y))
    with _ilock(n):
        _move(n, "move_mouse", x, y)
    return _ok("move_mouse", n, detail=f"moved to ({x},{y})")


@mcp.tool(annotations=_ACTS)
def scroll(amount: int = 3, instance: int = 1, x: int = -1, y: int = -1, axis: str = "vertical",
           direction: str = "", observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Scroll the wheel. Say where: direction='down'|'up'|'left'|'right' with `amount` notches
    (sign ignored) — or the signed form: positive = up (or left), negative = down (or right).

    Returns {"ok", "action", "instance", "detail", "applied", "clamped", "axis", "direction"}. One unit ≈ one wheel
    notch (a few lines); at most 100 notches per call ("clamped": true when you asked for more,
    "applied" says how many were sent). Scrolling targets the window under the pointer — pass
    x,y to move there first (both >= 0), otherwise the current pointer position is used.
    axis='horizontal' scrolls sideways (wheel buttons 6/7). Fails (isError) on amount=0 (a
    no-op), a dead instance, off-screen x,y or an unknown axis. `observe`/`settle_ms`: see click()."""
    n = _require("scroll", instance)
    observe = _validate_observe("scroll", n, observe, observe_region)
    amount = int(amount)
    if direction:
        d = direction.strip().lower()
        if d not in ("up", "down", "left", "right"):
            _fail("scroll", f"unknown direction '{direction}' — nothing was scrolled",
                  fix="use direction='up' | 'down' | 'left' | 'right'", instance=n)
        axis = "vertical" if d in ("up", "down") else "horizontal"
        amount = abs(amount) if d in ("up", "left") else -abs(amount)
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
        extra = _foreign_input(n)
        if x >= 0 or y >= 0:
            _move(n, "scroll", x, y)
        _xdo(n, "scroll", "click", btn, opts=("--repeat", str(applied), "--delay", "20"))
        return _observe(n, observe, settle_ms,
                        {"action": "scroll", **extra, "detail": f"scrolled {amount:+d} {axis}" + (f" (applied {applied})" if applied != abs(amount) else ""),
                         "applied": applied, "clamped": applied != abs(amount), "axis": axis,
                         "direction": {4: "up", 5: "down", 6: "left", 7: "right"}[btn]})


@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_READ_ONLY)
def wait_for_stable(instance: int = 1, timeout_ms: int = 5000, interval_ms: int = 300,
                    threshold: float = 0.002, observe: str = "none", observe_region: str = ""):
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
    observe = _validate_observe("wait_for_stable", n, observe, observe_region)
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


@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_ACTS)
def manage_window(action: str, instance: int = 1, window_id: int = 0, width: int = 0, height: int = 0,
                  x: int = 0, y: int = 0, observe: str = "none", settle_ms: int = 0, observe_region: str = ""):
    """Act on a window of an instance: action='activate' (focus/raise — switch between an app's
    windows or dialogs), 'close' (asks the app to close it — a save prompt may appear),
    'minimize', 'maximize', 'restore' (un-maximize), 'move' (to x,y), 'resize' (to width x
    height). window_id=0 means the active window (window_info lists ids).

    Returns {"ok", "action", "instance", "window", "detail"} plus `observe`/`settle_ms` like
    click(). Fails (isError) if the window id is unknown, the action is unknown, move/resize
    sizes are missing, or the instance is down. maximize/restore need wmctrl (status().deps)."""
    n = _require("manage_window", instance)
    observe = _validate_observe("manage_window", n, observe, observe_region)
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


@mcp.tool(annotations=_READ_ONLY)
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


@mcp.tool(annotations=_READ_ONLY)
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
    setxkbmap, dbus_run_session, tesseract, nvidia_smi, xprop, atspi: true|false}, "notes": [...], "tips": [...]}
    ("tips": the tools that save the most round-trips on this setup — read them once).
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
    tips = ["form fields: fill(text, x, y) clicks, replaces and reads the value back in one call",
            "after an action pass observe_region='x,y,w,h' to see only the part that changed (cheaper than a full screenshot)",
            "browsers: click() returns \"hit\" (the element under the point); browser(action='text') gives exact page text"]
    deps = {k: bool(shutil.which(b)) for k, b in (
        ("xdotool", "xdotool"), ("xclip", "xclip"), ("xephyr", "Xephyr"), ("openbox", "openbox"),
        ("wmctrl", "wmctrl"), ("xdpyinfo", "xdpyinfo"), ("xkbcomp", "xkbcomp"), ("setxkbmap", "setxkbmap"),
        ("dbus_run_session", "dbus-run-session"), ("tesseract", "tesseract"), ("nvidia_smi", "nvidia-smi"),
        ("xprop", "xprop"))}
    deps["atspi"] = bool(_a11y_python())
    if not deps["atspi"]:
        notes.append("AT-SPI bindings are missing: native apps cannot be read through accessibility "
                     "(find_elements falls back to vision, act() is unavailable) and fill() types blind there "
                     f"(focus_checked: false) — {A11Y_APT}")
    keyboard = {"host_layout": host_bridge.display_layout(HOST, _host_env()),
                "override": os.environ.get("GLOVEBOX_KEYBOARD", "")}
    v = vision.resolve(omni=omni, tess=tess, service_loaded=bool(svc and svc.get("loaded")))
    _VISION.update(value=v, at=time.time())
    tips.insert(0, "labelled UI: find_elements('Save') -> click_element(id) / act(id) / fill(element=id) instead of "
                   "guessing pixels — the app's accessibility information is asked first (exact labels, no GPU), "
                   "vision second; wait_for_text('Done') instead of sleeping"
                   + ("" if v["effective"] != "none" else
                      ". No vision backend is installed: apps without accessibility (canvases, games, Tk) need "
                      "screenshot(region=…) and click(x, y); ./install.sh basic adds OCR for them"))
    tips.append("after an action pass observe='elements' for the element list alone (no image), and act(id) to "
                "press or set a control without moving the pointer")
    return _ok("status", version=__version__, host_display=HOST, tmp_dir=TMP,
               cleanup_on_exit=_truthy(os.environ.get("GLOVEBOX_CLEANUP_ON_EXIT", "")),
               vision={**v, "gpu": gpu, "omniparser": omni, "tesseract": tess},
               instances=_instance_rows(), keyboard=keyboard,
               clipboard_sync={"enabled": CLIP_SYNC, "poll_s": host_bridge.POLL_S,
                               "note": "host clipboard text is mirrored into every instance" if CLIP_SYNC else "off"},
               deps=deps, notes=notes, tips=tips)


if __name__ == "__main__":
    mcp.run()
