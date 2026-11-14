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
             in-process exactly as before. A service that answers but is slow is reported as busy
  instead: loading a second copy of the models beside a working service is the one thing it
  exists to prevent
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
import hashlib
import io
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from _paths import tmp_dir

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tmp_dir()
DEFAULT_IDLE_MIN = 15.0
START_TIMEOUT_S = 20.0     # how long a client waits for a freshly spawned service's socket
CONNECT_TIMEOUT_S = 2.0
PARSE_TIMEOUT_S = 180.0    # first parse includes the model load (~10 s on a warm disk, far more cold)
DRAIN_TIMEOUT_S = 120.0    # on shutdown/idle-exit: how long to let in-flight parses finish
MAX_HEADER = 64 * 1024     # frame limits: a peer cannot make the service allocate 4 GiB
MAX_PAYLOAD = 64 * 1024 * 1024
MAX_HANDLERS = 16          # concurrent connections; parses are serialized on the GPU anyway


class ServiceTimeout(TimeoutError):
    """The service accepted the request but did not answer in time. NOT 'unreachable': the
    service may still be parsing on the GPU, so the caller must not load the models itself."""


def socket_path() -> str:
    p = os.environ.get("GLOVEBOX_VISION_SOCKET")
    if p:
        return p
    rt = os.environ.get("XDG_RUNTIME_DIR")
    if rt and os.path.isdir(rt):
        return os.path.join(rt, "glovebox-vision.sock")
    return os.path.join(TMP, "vision.sock")


def log_path() -> str:
    return os.path.join(TMP, "vision-service.log")


def lock_path(path: str) -> str:
    """One start-lock per socket path (tests and GLOVEBOX_VISION_SOCKET use several)."""
    return os.path.join(TMP, f"vision-service-{hashlib.sha1(path.encode()).hexdigest()[:8]}.lock")


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
    if hl > MAX_HEADER:
        raise ValueError(f"frame header of {hl} bytes exceeds the {MAX_HEADER} limit")
    header = json.loads(_recv_exact(sock, hl).decode("utf-8"))
    (pl,) = struct.unpack("!I", _recv_exact(sock, 4))
    if pl > MAX_PAYLOAD:
        raise ValueError(f"frame payload of {pl} bytes exceeds the {MAX_PAYLOAD} limit")
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
        self.sock_ino = None
        self.handlers = threading.BoundedSemaphore(MAX_HANDLERS)

    # -- request handling
    def _parse(self, header, payload):
        from PIL import Image
        import vision
        t_q = time.time()
        with self.gpu_lock:
            queued_ms = int((time.time() - t_q) * 1000)
            t0 = time.time()
            ann = os.path.join(TMP, f"service_annotated_{os.getpid()}_{threading.get_ident()}.png")
            try:
                try:
                    img = Image.open(io.BytesIO(payload)).convert("RGB")
                except Exception as e:   # a bad frame is the CLIENT's error, answered as such
                    return {"ok": False, "error": f"service could not decode the frame: {str(e)[:120]}",
                            "fix": "retry; the client must send a PNG"}, b""
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
            finally:
                try:
                    os.unlink(ann)        # sandbox screenshots must not accumulate in the temp dir
                except OSError:
                    pass
        return {"ok": True, "elements": elems, "queued_ms": queued_ms, "parse_ms": parse_ms,
                "detector": getattr(self.parser, "detector", None),
                "load_seconds": getattr(self.parser, "load_seconds", None)}, png

    def status(self) -> dict:
        with self.state_lock:
            return {"running": True, "pid": os.getpid(), "socket": self.path, "fake": self.fake,
                    "loaded": bool(getattr(self.parser, "loaded", False)),
                    "detector": getattr(self.parser, "detector", None),
                    "load_seconds": getattr(self.parser, "load_seconds", None),
                    "served": self.served, "errors": self.errors, "in_flight": self.in_flight,
                    "idle_s": int(time.time() - self.last_activity),
                    "uptime_s": int(time.time() - self.started), "idle_exit_s": int(self.idle_s)}

    def _handle(self, conn):
        """One connection. in_flight was counted at accept time (so a client still uploading its
        frame already keeps the idle watchdog from exiting); only a parse resets the idle clock —
        status polls (ping, the server's status() tool) must not keep the GPU pinned forever."""
        try:
            conn.settimeout(PARSE_TIMEOUT_S)
            header, payload = recv_frame(conn)
            op = header.get("op")
            if op == "parse":
                with self.state_lock:
                    self.last_activity = time.time()
                reply, png = self._parse(header, payload)
                with self.state_lock:
                    self.served += 1
                    if not reply["ok"]:
                        self.errors += 1
                    self.last_activity = time.time()
                send_frame(conn, reply, png)
            elif op == "status":
                send_frame(conn, self.status())
            elif op == "shutdown":
                send_frame(conn, {"ok": True, "stopping": True})
                self.stop_event.set()
            else:
                send_frame(conn, {"ok": False, "error": f"unknown op {op!r}", "fix": "use parse|status|shutdown"})
        except Exception as e:
            _log(f"connection error: {e!r}")
        finally:
            with self.state_lock:
                self.in_flight -= 1
            self.handlers.release()
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
                    self.stop_event.set()   # decided under the lock: no request can slip in between

    def _drain(self):
        """Let in-flight parses finish (bounded) before the process exits — handler threads are
        daemons, so returning from serve_forever mid-parse would cut a client off."""
        deadline = time.time() + DRAIN_TIMEOUT_S
        while time.time() < deadline:
            with self.state_lock:
                if self.in_flight == 0:
                    return
            time.sleep(0.05)
        _log(f"drain timed out with {self.in_flight} request(s) in flight")

    def serve_forever(self):
        """Bind, accept until shutdown/idle, drain, clean up. Returns when the service has stopped."""
        if os.path.exists(self.path):
            if ping(self.path) is not None:
                raise RuntimeError(f"another vision service already answers on {self.path}")
            os.unlink(self.path)   # stale socket from a dead service
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)       # the socket is created 0600: frames are screenshots
        try:
            self.sock.bind(self.path)
        finally:
            os.umask(old_umask)
        self.sock_ino = os.stat(self.path).st_ino
        self.sock.listen(16)
        self.sock.settimeout(0.5)
        threading.Thread(target=self._idle_watchdog, daemon=True).start()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self.stop_event.set())
        _log(f"serving on {self.path} (pid {os.getpid()}, fake={self.fake}, idle exit {int(self.idle_s)} s)")
        try:
            while not self.stop_event.is_set():
                try:
                    conn, _ = self.sock.accept()
                except TimeoutError:
                    continue
                except OSError as e:    # EMFILE, ECONNABORTED, …: log and keep serving
                    _log(f"accept failed: {e!r}")
                    time.sleep(0.1)
                    continue
                if not self.handlers.acquire(timeout=5):
                    _log("too many connections — refusing one")
                    conn.close()
                    continue
                with self.state_lock:
                    self.in_flight += 1
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            self._drain()
        finally:
            try:
                self.sock.close()
            except OSError:
                pass
            try:
                # only OUR socket file: a newer service may already have bound this path
                if os.stat(self.path).st_ino == self.sock_ino:
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
    """Connect/send failures raise OSError (the service is unreachable — callers may fall back);
    a timeout AFTER the request was sent raises ServiceTimeout (it is busy — callers must not)."""
    s = _connect(path or socket_path(), CONNECT_TIMEOUT_S)
    try:
        s.settimeout(timeout)
        send_frame(s, header, payload)
        try:
            return recv_frame(s)
        except TimeoutError:
            raise ServiceTimeout(f"no answer within {timeout:.0f} s") from None
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
    threading.Thread(target=p.wait, daemon=True).start()   # reap: no zombie when it idle-exits
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
    with open(lock_path(path), "w") as lk:
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
    fix — including when the service is reachable but did not answer in time (it is busy; a
    second model load in this process is exactly what the service exists to prevent) and when
    its reply is malformed. Only connect/send-time OSError means 'unreachable' (callers may then
    fall back in-process)."""
    import vision
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    try:
        header, png = request({"op": "parse", "box_threshold": box_threshold, "instance": int(instance)},
                              buf.getvalue(), path=path, timeout=PARSE_TIMEOUT_S)
    except ServiceTimeout as e:
        raise vision.VisionError(f"the vision service is busy: {e} (another agent's parse, or a cold model load)",
                                 "retry in a few seconds; status().vision.omniparser.service shows the queue") from None
    if not header.get("ok"):
        raise vision.VisionError(header.get("error", "service parse failed"),
                                 header.get("fix", "retry, or set GLOVEBOX_VISION_SERVICE=0 to parse in-process"))
    if not isinstance(header.get("elements"), list):
        raise vision.VisionError("the vision service sent a malformed reply (no element list)",
                                 "restart it: python vision_service.py stop (it restarts on the next parse)")
    if png:
        with open(annotated_path, "wb") as fh:
            fh.write(png)
    meta = {k: header[k] for k in ("queued_ms", "parse_ms", "load_seconds", "detector") if k in header}
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
