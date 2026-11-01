"""Shared vision service: ONE OmniParser load per machine instead of one per agent process.

Every agent runs its own glovebox server over stdio, so without this each agent that calls
parse_screen in `local` mode loads YOLO + Florence-2 + easyocr into the GPU again (1.5–2.5 GB and a
CUDA context apiece). This module is a small process that owns the models and answers parse
requests over a Unix socket; the servers are thin clients.

  socket     $XDG_RUNTIME_DIR/glovebox-vision.sock  (else $TMPDIR/glovebox-<uid>/vision.sock)
             — override with GLOVEBOX_VISION_SOCKET
  lifecycle  the first server that needs it starts it (under a file lock); it exits after
             GLOVEBOX_VISION_IDLE_MIN minutes without a request (default 15) so a forgotten
             service never pins the GPU
  fallback   if the socket can't be reached or the service can't be started, the server parses
             in-process exactly as before — the service is an optimization, never a new failure mode
  queueing   parses are serialized on the GPU; each reply carries queued_ms

Wire format (both directions): [u32 len][JSON header][u32 len][binary payload]. A request header is
{"op": "parse"|"status"|"shutdown", "box_threshold": f, "instance": n} with the PNG frame as the
payload; a parse reply is {"ok": true, "elements": [...], "queued_ms", "parse_ms", ...} with the
annotated PNG as payload, or {"ok": false, "error", "fix"} (a VisionError on the client).

CLI:  python vision_service.py serve [--fake] [--idle-min N]   # --fake: no GPU, one canned element (tests)
      python vision_service.py status | stop
"""
import argparse
import fcntl
import io
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(tempfile.gettempdir(), f"glovebox-{os.getuid()}")
DEFAULT_IDLE_MIN = 15.0
START_TIMEOUT_S = 20.0     # how long a client waits for a freshly spawned service's socket
CONNECT_TIMEOUT_S = 2.0
PARSE_TIMEOUT_S = 180.0    # first parse includes the model load (~6 s on a warm disk, far more cold)


def socket_path() -> str:
    p = os.environ.get("GLOVEBOX_VISION_SOCKET")
    if p:
        return p
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt):
        return os.path.join(rt, "glovebox-vision.sock")
    os.makedirs(TMP, exist_ok=True)
    return os.path.join(TMP, "vision.sock")


def log_path() -> str:
    os.makedirs(TMP, exist_ok=True)
    return os.path.join(TMP, "vision-service.log")


# ---------- framing ----------
def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(1 << 20, n - len(buf)))
        if not chunk:
            raise ConnectionError("peer closed the connection mid-frame")
        buf += chunk
    return bytes(buf)


def send_frame(sock, header: dict, payload: bytes = b""):
    h = json.dumps(header, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack("!I", len(h)) + h + struct.pack("!I", len(payload)) + payload)


def recv_frame(sock):
    (hl,) = struct.unpack("!I", _recv_exact(sock, 4))
    header = json.loads(_recv_exact(sock, hl).decode("utf-8"))
    (pl,) = struct.unpack("!I", _recv_exact(sock, 4))
    payload = _recv_exact(sock, pl) if pl else b""
    return header, payload


# ---------- the service ----------
class FakeParser:
    """Stand-in for OmniParser when there is no GPU (tests, `--fake`): one canned element."""
    loaded = True
    load_seconds = 0.0

    def load(self):
        pass

    def parse(self, img, box_threshold, annotated_path):
        img.save(annotated_path)
        return [{"id": 0, "type": "text", "label": "fake element", "interactive": True,
                 "center": [img.width // 2, img.height // 2]}]


class Service:
    def __init__(self, path: str, parser=None, idle_s: float = DEFAULT_IDLE_MIN * 60, fake: bool = False):
        self.path = path
        self.fake = fake or parser is not None and isinstance(parser, FakeParser)
        if parser is None:
            if fake:
                parser = FakeParser()
            else:
                import vision
                parser = vision.OmniParser()
        self.parser = parser
        self.idle_s = idle_s
        self.gpu_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.started = time.time()
        self.last_activity = time.time()
        self.served = 0
        self.in_flight = 0
        self.errors = 0
        self.stop_event = threading.Event()
        self.sock = None

    # -- request handling
    def _parse(self, header, payload):
        from PIL import Image
        import vision
        t_q = time.time()
        with self.gpu_lock:
            queued_ms = int((time.time() - t_q) * 1000)
            t0 = time.time()
            img = Image.open(io.BytesIO(payload)).convert("RGB")
            ann = os.path.join(TMP, f"service_annotated_{os.getpid()}_{threading.get_ident()}.png")
            try:
                elems = self.parser.parse(img, float(header.get("box_threshold", 0.05)), ann)
            except vision.VisionError as e:
                return {"ok": False, "error": e.error, "fix": e.fix}, b""
            except Exception as e:  # never let one bad frame take the service down
                return {"ok": False, "error": f"service parse failed: {str(e)[:200]}",
                        "fix": "retry; if it persists restart the service (python vision_service.py stop)"}, b""
            parse_ms = int((time.time() - t0) * 1000)
        try:
            with open(ann, "rb") as fh:
                png = fh.read()
        except OSError:
            png = b""
        return {"ok": True, "elements": elems, "queued_ms": queued_ms, "parse_ms": parse_ms,
                "load_seconds": getattr(self.parser, "load_seconds", None)}, png

    def status(self) -> dict:
        with self.state_lock:
            return {"running": True, "pid": os.getpid(), "socket": self.path, "fake": self.fake,
                    "loaded": bool(getattr(self.parser, "loaded", False)),
                    "load_seconds": getattr(self.parser, "load_seconds", None),
                    "served": self.served, "errors": self.errors, "in_flight": self.in_flight,
                    "idle_s": int(time.time() - self.last_activity),
                    "uptime_s": int(time.time() - self.started), "idle_exit_s": int(self.idle_s)}

    def _handle(self, conn):
        try:
            conn.settimeout(PARSE_TIMEOUT_S)
            header, payload = recv_frame(conn)
            op = header.get("op")
            with self.state_lock:
                self.last_activity = time.time()
                self.in_flight += 1
            try:
                if op == "parse":
                    reply, png = self._parse(header, payload)
                    with self.state_lock:
                        self.served += 1
                        if not reply["ok"]:
                            self.errors += 1
                    send_frame(conn, reply, png)
                elif op == "status":
                    send_frame(conn, self.status())
                elif op == "shutdown":
                    send_frame(conn, {"ok": True, "stopping": True})
                    self.stop_event.set()
                else:
                    send_frame(conn, {"ok": False, "error": f"unknown op {op!r}", "fix": "use parse|status|shutdown"})
            finally:
                with self.state_lock:
                    self.in_flight -= 1
                    self.last_activity = time.time()
        except Exception as e:
            _log(f"connection error: {e!r}")
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # -- lifecycle
    def _idle_watchdog(self):
        while not self.stop_event.wait(1.0):
            with self.state_lock:
                idle = time.time() - self.last_activity
                busy = self.in_flight > 0
            if not busy and idle >= self.idle_s:
                _log(f"idle for {int(idle)} s — exiting to free the GPU")
                self.stop_event.set()

    def serve_forever(self):
        """Bind, accept until shutdown/idle, clean up. Returns when the service has stopped."""
        if os.path.exists(self.path):
            if ping(self.path) is not None:
                raise RuntimeError(f"another vision service already answers on {self.path}")
            os.unlink(self.path)   # stale socket from a dead service
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._idle_watchdog, daemon=True).start()
        _log(f"serving on {self.path} (pid {os.getpid()}, fake={self.fake}, idle exit {int(self.idle_s)} s)")
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.sock.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
            if os.path.exists(self.path):
                try:
                    os.unlink(self.path)
                except OSError:
                    pass
            _log("stopped")


def _log(msg: str):
    try:
        with open(log_path(), "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


# ---------- client ----------
def _connect(path: str, timeout: float):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(path)
    return s


def ping(path: str = None, timeout: float = CONNECT_TIMEOUT_S):
    """Service status dict if something answers on the socket, else None (dead/stale/no socket)."""
    path = path or socket_path()
    if not os.path.exists(path):
        return None
    try:
        s = _connect(path, timeout)
        try:
            send_frame(s, {"op": "status"})
            header, _ = recv_frame(s)
            return header
        finally:
            s.close()
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def request(header: dict, payload: bytes = b"", path: str = None, timeout: float = PARSE_TIMEOUT_S):
    s = _connect(path or socket_path(), CONNECT_TIMEOUT_S)
    try:
        s.settimeout(timeout)
        send_frame(s, header, payload)
        return recv_frame(s)
    finally:
        s.close()


def spawn(path: str = None, fake: bool = False, idle_min: float = None) -> int:
    """Start a detached service process (survives the caller). Returns its pid."""
    path = path or socket_path()
    cmd = [sys.executable, os.path.join(HERE, "vision_service.py"), "serve", "--socket", path]
    if fake:
        cmd.append("--fake")
    if idle_min is not None:
        cmd += ["--idle-min", str(idle_min)]
    with open(log_path(), "ab") as lf:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=lf, stderr=lf,
                             start_new_session=True, cwd=HERE)
    return p.pid


def ensure_running(path: str = None, fake: bool = False, idle_min: float = None,
                   start_timeout: float = START_TIMEOUT_S):
    """Status of a live service, starting one if needed (serialized across processes by a file lock
    so two servers racing to start it don't both spawn one). Returns None when it can't be had —
    callers then parse in-process."""
    path = path or socket_path()
    st = ping(path)
    if st:
        return st
    lockfile = os.path.join(TMP, "vision-service.lock")
    os.makedirs(TMP, exist_ok=True)
    with open(lockfile, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        try:
            st = ping(path)
            if st:
                return st
            try:
                spawn(path, fake=fake, idle_min=idle_min)
            except OSError as e:
                _log(f"spawn failed: {e!r}")
                return None
            deadline = time.time() + start_timeout
            while time.time() < deadline:
                st = ping(path)
                if st:
                    return st
                time.sleep(0.2)
            return None
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def parse(img, box_threshold: float, annotated_path: str, instance: int = 0, path: str = None):
    """Parse through the service. Returns (elements, meta) or raises VisionError with the service's
    fix. ConnectionError/OSError mean 'service unreachable' — callers fall back in-process."""
    import vision
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    header, png = request({"op": "parse", "box_threshold": box_threshold, "instance": int(instance)},
                          buf.getvalue(), path=path)
    if not header.get("ok"):
        raise vision.VisionError(header.get("error", "service parse failed"),
                                 header.get("fix", "retry, or set GLOVEBOX_VISION_SERVICE=0 to parse in-process"))
    if png:
        with open(annotated_path, "wb") as fh:
            fh.write(png)
    meta = {k: header[k] for k in ("queued_ms", "parse_ms", "load_seconds") if k in header}
    return header["elements"], meta


def stop(path: str = None) -> bool:
    path = path or socket_path()
    try:
        header, _ = request({"op": "shutdown"}, path=path, timeout=5)
        return bool(header.get("stopping"))
    except (OSError, ValueError):
        return False


# ---------- CLI ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", default="status", choices=("serve", "status", "stop"))
    ap.add_argument("--socket", default=None)
    ap.add_argument("--fake", action="store_true", help="no GPU: answer with one canned element (tests)")
    ap.add_argument("--idle-min", type=float, default=None)
    a = ap.parse_args(argv)
    path = a.socket or socket_path()
    if a.cmd == "serve":
        idle_min = a.idle_min if a.idle_min is not None else float(os.environ.get("GLOVEBOX_VISION_IDLE_MIN", DEFAULT_IDLE_MIN))
        Service(path, fake=a.fake, idle_s=idle_min * 60).serve_forever()
    elif a.cmd == "status":
        st = ping(path)
        print(json.dumps(st or {"running": False, "socket": path}, indent=2))
        return 0 if st else 1
    elif a.cmd == "stop":
        ok = stop(path)
        print("stopped" if ok else f"nothing running on {path}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
