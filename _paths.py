"""Shared by server.py, vision_service.py and host_bridge.py: the per-user temp dir (created
0700 and ownership-checked — it holds screenshots of the sandbox), display naming, and the
display liveness probe. No third-party imports: host_bridge runs on plain python3."""
import os
import subprocess
import tempfile

UP_TIMEOUT = 5   # a display that cannot answer xdpyinfo in 5 s is not usable


def tmp_dir() -> str:
    """$TMPDIR/glovebox-<uid>/, mode 0700, owned by us. Refuses a directory someone else
    pre-created (symlink/hijack on a multi-user host) rather than writing frames into it."""
    d = os.path.join(tempfile.gettempdir(), f"glovebox-{os.getuid()}")
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
    except OSError as e:
        raise RuntimeError(f"cannot create {d}: {e}") from None
    st = os.lstat(d)
    if os.path.islink(d) or st.st_uid != os.getuid():
        raise RuntimeError(f"{d} is not a directory owned by uid {os.getuid()} — remove it (or set TMPDIR)")
    if st.st_mode & 0o077:
        os.chmod(d, 0o700)
    return d


def disp(n) -> str:
    """':N' from an int or an already-formatted ':N'."""
    return n if isinstance(n, str) and n.startswith(":") else f":{int(n)}"


def display_up(d, timeout: float = UP_TIMEOUT):
    """True/False, or None when xdpyinfo itself is missing (the caller decides what that means)."""
    try:
        return subprocess.run(["xdpyinfo", "-display", disp(d)], capture_output=True, timeout=timeout).returncode == 0
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return None
