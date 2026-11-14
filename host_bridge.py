"""Host ↔ instance bridges: the keyboard layout and the clipboard.

Each instance is a separate X server (Xephyr), and two things a person expects from "a window
on my desktop" are NOT automatic across that boundary:

* **Keyboard layout.** Xephyr copies the host's keysyms but builds its own modifier map, and
  it files the AltGr key (keycode 108) under Mod1 (plain Alt) instead of making it the level-3
  switch. On any layout that reaches characters through AltGr (Slovenian/Croatian/German/…:
  `@ { } [ ] \\ | ~ € ^`), AltGr+V arrives in the app as Alt+v and nothing is typed. Confirmed
  with a key logger inside the instance: unpatched → `keycode=108 keysym=Alt_R`, `keycode=55
  state=Mod1 keysym=v`; after the copy → `ISO_Level3_Shift`, `state=Mod5 keysym=at`.
  `apply_keymap()` copies the host's complete keymap (`xkbcomp HOST INSTANCE`) — exact, every
  layout, no detection needed — or, when xkbcomp is missing, sets the detected layout with
  setxkbmap. The agent's own typing (xdotool) addresses keysyms and works either way; this is
  for the person typing into the window.

* **Clipboard.** An X selection lives in one X server; the host's clipboard is invisible inside
  an instance and vice versa. `ClipboardSync` polls both CLIPBOARD selections with xclip and
  copies text whichever way it changed (a VM-style shared clipboard: text only, CLIPBOARD only,
  not PRIMARY). The host side is an Xwayland display on Wayland desktops, whose compositor
  bridges the X clipboard to native apps (verified with KDE's klipper).

One detached **bridge process per instance** (`spawn`/`ensure_running`) does both: it mirrors
the clipboard every POLL_S and, every KEYMAP_CHECK_S, re-copies the keymap when the instance's
no longer equals the host's (Xephyr reloading its own map, or the person switching the host
layout). A file lock keeps several agents' servers from starting duplicates; the bridge exits
when the instance's display goes down.

CLI:  python host_bridge.py keymap :N [--host :0]            (one-off copy + verification)
      python host_bridge.py serve :N [--host :0]             (the bridge, in the foreground)
      python host_bridge.py spawn :N [--host :0]             (detached; prints the pid)
      python host_bridge.py status :N [--host :0]
"""
import fcntl
import os
import re
import shutil
import subprocess
import sys
import time

from _paths import disp as _disp, display_up, tmp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tmp_dir()
POLL_S = float(os.environ.get("GLOVEBOX_CLIPBOARD_POLL", "0.5"))
KEYMAP_CHECK_S = 5.0
LIVENESS_CHECK_S = 5.0     # xdpyinfo probe cadence; two consecutive failures = the display is gone
XCLIP_TIMEOUT = 3


# ---------------------------------------------------------------- keyboard
def _run(cmd, env, timeout=10):
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)


def layout_from_dump(dump: str) -> str:
    """Layout name out of an xkbcomp keymap dump's symbols name: `pc+si+inet(evdev)` (a server's
    own naming) or `pc_si_inet(evdev)` (after a keymap was loaded from a dump) → "si";
    `pc+si(nodeadkeys)+inet(evdev)` → "si(nodeadkeys)". Empty when not recognisable."""
    m = re.search(r'xkb_symbols\s+"pc[+_]([^+_"]+)', dump)
    return m.group(1) if m else ""


def display_layout(disp: str, env: dict = None) -> str:
    """Best-effort name of the layout a display serves, for reporting: the live keymap's symbols
    name (what Xwayland and a copied keymap carry), else setxkbmap's rules property (what a
    real X server updated by setxkbmap carries). '' when neither says. Functional checks (does
    AltGr switch levels) do not go through this."""
    env = env or {**os.environ, "DISPLAY": disp}
    lay = ""
    if shutil.which("xkbcomp"):
        try:
            r = _run(["xkbcomp", disp, "-"], env)
            lay = layout_from_dump(r.stdout) if r.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            lay = ""
    if not lay and shutil.which("setxkbmap"):
        try:
            m = re.search(r"layout:\s*(\S+)", _run(["setxkbmap", "-display", disp, "-query"], env).stdout)
            lay = m.group(1) if m else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    return lay


def requested_layout() -> tuple:
    """(layout, variant) from GLOVEBOX_KEYBOARD ('si', 'si(nodeadkeys)', 'de,us'); ('', '') if unset."""
    v = os.environ.get("GLOVEBOX_KEYBOARD", "").strip()
    m = re.fullmatch(r"([A-Za-z,]+)(?:\((\w+)\))?", v)
    return (m.group(1), m.group(2) or "") if m else ("", "")


def keymap_signature(disp: str, env: dict = None):
    """What a person's keys do on a display: keysyms per keycode + the modifier map (xmodmap).
    Two displays with equal signatures type the same. None when xmodmap is missing/unreachable."""
    if not shutil.which("xmodmap"):
        return None
    env = env or {**os.environ, "DISPLAY": disp}
    try:
        a = _run(["xmodmap", "-display", disp, "-pke"], env)
        b = _run(["xmodmap", "-display", disp, "-pm"], env)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if a.returncode or b.returncode:
        return None
    return " ".join(a.stdout.split()) + "|" + " ".join(b.stdout.split())


def copy_keymap(disp: str, host: str, host_env: dict) -> str:
    """xkbcomp HOST DISP. Returns '' on success, else the reason."""
    if not shutil.which("xkbcomp"):
        return "xkbcomp is not installed (package x11-xkb-utils)"
    try:
        # the geometry warnings xkbcomp prints on Xwayland hosts are harmless (fonts/colours of
        # the on-screen keyboard picture), hence -w 0
        r = _run(["xkbcomp", "-w", "0", host, disp], host_env, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"xkbcomp failed: {e!r}"
    return "" if r.returncode == 0 else f"xkbcomp {host} {disp} failed: {(r.stderr or '').strip()[:120]}"


def apply_keymap(disp: str, host: str, host_env: dict = None) -> dict:
    """Make the instance's keyboard behave like the host's. Returns
    {"method": "host-keymap"|"setxkbmap"|"xephyr-default", "host_layout": str, "verified": bool|None,
    "warning"?: str}. "verified" is whether the instance's keymap signature now equals the host's
    (None when xmodmap is missing). "host_layout" is the host's layout NAME as far as it can be
    read (Xwayland and Xephyr do not always update the name when the map changes; the verified
    flag is the functional truth)."""
    disp, host = _disp(disp), _disp(host)
    host_env = host_env or {**os.environ, "DISPLAY": host}
    inst_env = {**os.environ, "DISPLAY": disp}
    layout, variant = requested_layout()
    if layout and shutil.which("setxkbmap"):
        cmd = ["setxkbmap", "-display", disp, "-layout", layout] + (["-variant", variant] if variant else [])
        try:
            r = _run(cmd, inst_env)
            if r.returncode == 0:
                return {"method": "setxkbmap", "layout": layout + (f"({variant})" if variant else ""),
                        "host_layout": display_layout(host, host_env), "source": "GLOVEBOX_KEYBOARD",
                        "verified": None}
            warn = f"setxkbmap {layout} failed: {(r.stderr or '').strip()[:120]}"
        except (OSError, subprocess.TimeoutExpired) as e:
            warn = f"setxkbmap failed: {e!r}"
        return {"method": "xephyr-default", "host_layout": display_layout(host, host_env), "verified": False,
                "warning": warn}
    warn = copy_keymap(disp, host, host_env)
    if not warn:
        hs, is_ = keymap_signature(host, host_env), keymap_signature(disp, inst_env)
        verified = None if hs is None or is_ is None else hs == is_
        out = {"method": "host-keymap", "host_layout": display_layout(host, host_env), "source": host,
               "verified": verified}
        if verified is False:
            out["warning"] = ("the keymap copied from the host does not match it (xmodmap) — the "
                              "bridge re-copies every 5 s; if AltGr characters still fail, report "
                              "status() output")
        return out
    return {"method": "xephyr-default", "host_layout": display_layout(host, host_env), "verified": False,
            "warning": warn + " — AltGr characters (@ { } [ ] | ~ €) typed by a PERSON into this "
                              "window will not work; the agent's type_text is unaffected"}


# ---------------------------------------------------------------- clipboard
def read_clipboard(env: dict):
    """Text on the CLIPBOARD selection of env['DISPLAY'], None when empty/non-text/unreachable."""
    try:
        r = subprocess.run(["xclip", "-o", "-selection", "clipboard", "-t", "UTF8_STRING"], env=env,
                           capture_output=True, timeout=XCLIP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.decode("utf-8", "replace")


def write_clipboard(env: dict, text: str) -> bool:
    # xclip -i forks a daemon that owns the selection; it must not inherit our pipes or a
    # captured stdout blocks until the daemon exits (= until someone else takes the selection)
    try:
        r = subprocess.run(["xclip", "-i", "-selection", "clipboard"], env=env, input=text.encode("utf-8"),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=XCLIP_TIMEOUT)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class ClipboardSync:
    """One step = read both sides, propagate the side that changed. The IO is injectable so the
    policy is testable without an X server.

    Policy: the first step seeds from the host (the person's current clipboard flows in). After
    that, a change on the instance (the app or the agent copied) goes to the host, else a change
    on the host goes to the instance. Non-text/empty clipboards (None) are never propagated and
    never overwrite the other side."""

    def __init__(self, read_host, read_inst, write_host, write_inst):
        self.read_host, self.read_inst = read_host, read_inst
        self.write_host, self.write_inst = write_host, write_inst
        self.last = None          # the last text both sides agreed on
        self.seeded = False
        self.copied = {"to_instance": 0, "to_host": 0}

    def step(self) -> str:
        """Returns what happened: 'seed' | 'to_host' | 'to_instance' | 'idle'."""
        h, i = self.read_host(), self.read_inst()
        if not self.seeded:
            self.seeded = True
            if h is not None:
                self.last = h
                if h != i:
                    self.write_inst(h)
                    self.copied["to_instance"] += 1
                    return "seed"
            elif i is not None:
                self.last = i
            return "idle"
        if i is not None and i != self.last and i != h:
            self.write_host(i)
            self.last = i
            self.copied["to_host"] += 1
            return "to_host"
        if h is not None and h != self.last and h != i:
            self.write_inst(h)
            self.last = h
            self.copied["to_instance"] += 1
            return "to_instance"
        if h is not None and h == i:
            self.last = h
        return "idle"


def lock_path(n) -> str:
    return os.path.join(TMP, f"bridge_{_disp(n)[1:]}.lock")


def log_path(n) -> str:
    return os.path.join(TMP, f"bridge_{_disp(n)[1:]}.log")


def running(n) -> bool:
    """True when a bridge process holds the instance's lock (probing never creates the file).
    The probe takes the lock for a few microseconds; a bridge starting at that instant retries
    (see run_bridge), so a probe cannot make it give up."""
    if not os.path.exists(lock_path(n)):
        return False
    try:
        with open(lock_path(n), "r+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def run_bridge(n, host: str, interval: float = POLL_S, log=None, host_env: dict = None) -> int:
    """Foreground loop until the instance display goes away: clipboard mirroring every `interval`
    and a keymap check every KEYMAP_CHECK_S. Returns an exit code: 0 normal, 2 another bridge
    already holds the lock."""
    disp, host = _disp(n), _disp(host)
    log = log or (lambda m: None)
    lk = open(lock_path(n), "a+")
    for attempt in range(5):             # a running() probe may hold the lock for an instant
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            time.sleep(0.05)
    else:
        lk.close()
        log(f"another bridge already serves {disp}")
        return 2
    henv = host_env or {**os.environ, "DISPLAY": host}
    ienv = {**os.environ, "DISPLAY": disp}
    clip = shutil.which("xclip") is not None
    sync = ClipboardSync(lambda: read_clipboard(henv), lambda: read_clipboard(ienv),
                         lambda t: write_clipboard(henv, t), lambda t: write_clipboard(ienv, t))
    keep_keymap = not requested_layout()[0] and shutil.which("xkbcomp") and shutil.which("xmodmap")
    log(f"bridging {host} <-> {disp}: clipboard={'every %ss' % interval if clip else 'off (no xclip)'}, "
        f"keymap={'checked every %ss' % KEYMAP_CHECK_S if keep_keymap else 'not enforced'} (pid {os.getpid()})")
    down = 0
    next_kb = 0.0
    next_live = time.time() + LIVENESS_CHECK_S
    try:
        while True:
            ev = sync.step() if clip else "idle"
            if ev != "idle":
                log(f"{ev} ({len(sync.last or '')} chars)")
            now = time.time()
            if keep_keymap and now >= next_kb:
                next_kb = now + KEYMAP_CHECK_S
                hs, is_ = keymap_signature(host, henv), keymap_signature(disp, ienv)
                if hs and is_ and hs != is_:
                    err = copy_keymap(disp, host, henv)
                    log("keymap diverged from the host — re-copied" if not err else f"keymap re-copy failed: {err}")
            if now >= next_live:
                next_live = now + LIVENESS_CHECK_S
                up = display_up(disp)
                if up is None:            # xdpyinfo missing: cannot tell, keep serving
                    down = 0
                else:
                    down = down + 1 if not up else 0
                if down >= 2:
                    log(f"{disp} is gone — exiting")
                    return 0
            time.sleep(interval)
    finally:
        # the lock file stays (unlinking it while another bridge opens it is the classic
        # unlink race that ends with two bridges); releasing the lock is what matters
        try:
            fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()
        except OSError:
            pass


def spawn(n, host: str, host_env: dict = None):
    """Start a detached bridge for instance n unless one already runs. Returns the pid, or None
    when a bridge is already running."""
    if running(n):
        return None
    cmd = [sys.executable, os.path.join(HERE, "host_bridge.py"), "serve", _disp(n), "--host", _disp(host)]
    with open(log_path(n), "ab") as lf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf, env=host_env,
                             start_new_session=True, cwd=HERE)
    return p.pid


def ensure_running(n, host: str, host_env: dict = None, wait_s: float = 2.0) -> bool:
    """spawn + wait until the lock is held. True when a bridge is running."""
    if running(n):
        return True
    if spawn(n, host, host_env) is None:
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if running(n):
            return True
        time.sleep(0.1)
    return running(n)


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("keymap", "serve", "spawn", "status"):
        p = sub.add_parser(name)
        p.add_argument("display")
        p.add_argument("--host", default=os.environ.get("GLOVEBOX_HOST_DISPLAY", ":0"))
        if name == "serve":
            p.add_argument("--interval", type=float, default=POLL_S)
    a = ap.parse_args(argv)
    if a.cmd == "keymap":
        r = apply_keymap(a.display, a.host)
        print(r)
        return 0 if r["method"] != "xephyr-default" else 1
    if a.cmd == "serve":
        def log(m):
            print(time.strftime("%H:%M:%S"), m, flush=True)
        return run_bridge(a.display, a.host, a.interval, log)
    if a.cmd == "spawn":
        pid = spawn(a.display, a.host)
        print(pid if pid else "already running")
        return 0
    henv = {**os.environ, "DISPLAY": _disp(a.host)}
    hs, is_ = keymap_signature(_disp(a.host), henv), keymap_signature(_disp(a.display))
    print({"display": _disp(a.display), "host_layout": display_layout(_disp(a.host), henv),
           "keymap_equals_host": None if hs is None or is_ is None else hs == is_,
           "bridge_running": running(a.display)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
