"""Shared vision service — protocol, lifecycle and the server's client path, all without a GPU
(the parser is faked; the real OmniParser path is the same code with a different parser object)."""
import json
import os
import socket
import threading
import time

import pytest
from PIL import Image

import server
import vision
import vision_service as vs
from mcp.server.mcpserver.exceptions import ToolError


@pytest.fixture
def sock_path(tmp_path, monkeypatch):
    p = str(tmp_path / "vision.sock")
    monkeypatch.setenv("GLOVEBOX_VISION_SOCKET", p)
    monkeypatch.setattr(vs, "TMP", str(tmp_path))   # lock + log + annotated frames stay in tmp
    return p


@pytest.fixture
def service(sock_path):
    """A service running in a thread of THIS process with the fake parser."""
    svc = vs.Service(sock_path, parser=vs.FakeParser(), idle_s=60)
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    for _ in range(50):
        if vs.ping(sock_path):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("service did not come up")
    yield svc
    svc.stop_event.set()
    t.join(timeout=5)


def test_framing_roundtrip():
    a, b = socket.socketpair()
    vs.send_frame(a, {"op": "x", "n": 1, "s": "čšž"}, b"\x00\x01" * 1000)
    header, payload = vs.recv_frame(b)
    assert header == {"op": "x", "n": 1, "s": "čšž"} and payload == b"\x00\x01" * 1000
    vs.send_frame(a, {"empty": True})
    assert vs.recv_frame(b) == ({"empty": True}, b"")


def test_parse_status_and_shutdown(service, sock_path, tmp_path):
    st = vs.ping(sock_path)
    assert st["running"] and st["fake"] and st["served"] == 0 and st["pid"] == os.getpid()

    ann = str(tmp_path / "ann.png")
    elems, meta = vs.parse(Image.new("RGB", (300, 200), "white"), 0.05, ann, instance=3, path=sock_path)
    assert elems[0]["label"] == "fake element" and elems[0]["center"] == [150, 100]
    assert set(meta) >= {"queued_ms", "parse_ms"} and os.path.getsize(ann) > 0

    assert vs.ping(sock_path)["served"] == 1
    assert vs.stop(sock_path) is True
    for _ in range(50):
        if vs.ping(sock_path) is None:
            break
        time.sleep(0.05)
    assert vs.ping(sock_path) is None and not os.path.exists(sock_path)   # socket cleaned up


def test_parser_errors_cross_the_wire_as_vision_errors(sock_path):
    class Boom(vs.FakeParser):
        def parse(self, img, box_threshold, annotated_path):
            raise vision.VisionError("GPU out of memory", "close other GPU apps")
    svc = vs.Service(sock_path, parser=Boom(), idle_s=60)
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    while not vs.ping(sock_path):
        time.sleep(0.05)
    try:
        with pytest.raises(vision.VisionError) as e:
            vs.parse(Image.new("RGB", (10, 10)), 0.05, str(sock_path) + ".png", path=sock_path)
        assert e.value.error == "GPU out of memory" and e.value.fix == "close other GPU apps"
        assert vs.ping(sock_path)["errors"] == 1 and svc.stop_event.is_set() is False   # service survives
    finally:
        svc.stop_event.set()
        t.join(timeout=5)


def test_idle_exit(sock_path):
    svc = vs.Service(sock_path, parser=vs.FakeParser(), idle_s=0.3)
    t = threading.Thread(target=svc.serve_forever, daemon=True)
    t.start()
    while not vs.ping(sock_path):
        time.sleep(0.05)
    t.join(timeout=5)
    assert not t.is_alive() and not os.path.exists(sock_path)


def test_ensure_running_spawns_once_and_reuses(sock_path):
    """The real subprocess path: spawn a detached --fake service, a second call must reuse it."""
    st = vs.ensure_running(sock_path, fake=True, idle_min=0.5)
    assert st and st["running"] and st["fake"] and st["pid"] != os.getpid()
    try:
        assert vs.ensure_running(sock_path)["pid"] == st["pid"]
        elems, meta = vs.parse(Image.new("RGB", (64, 64)), 0.05, sock_path + ".png", path=sock_path)
        assert elems and meta["queued_ms"] >= 0
    finally:
        assert vs.stop(sock_path)
    for _ in range(50):
        if vs.ping(sock_path) is None:
            break
        time.sleep(0.05)
    assert vs.ping(sock_path) is None


# ---------- the server's client path ----------
@pytest.fixture
def local_mode(monkeypatch):
    monkeypatch.setitem(server.VISION, "effective", "local")
    monkeypatch.setitem(server.VISION, "ready", True)
    monkeypatch.setattr(server, "_grab_checked", lambda n, a: Image.new("RGB", (400, 300), "white"))
    monkeypatch.setattr(server, "TMP", str(server.TMP))
    monkeypatch.setattr(server, "USE_SERVICE", True)
    server._SERVICE_FAILED_AT[0] = 0.0


def test_server_parses_via_service(service, local_mode, sock_path):
    p = server._do_parse(5)
    assert p["ok"] and p["via"] == "service" and p["elements"][0]["label"] == "fake element"
    assert "queued_ms" in p and p["count"] == 1
    assert server._LAST[5][0] == (200, 150)          # click_element works off service results
    assert vs.ping(sock_path)["served"] == 1


def test_server_falls_back_in_process_when_service_unreachable(local_mode, sock_path, monkeypatch):
    """No service and spawning disabled -> in-process parse, never an error for the agent."""
    monkeypatch.setattr(vs, "ensure_running", lambda *a, **k: None)
    monkeypatch.setattr(server._OMNI, "parse", lambda img, bt, ann: [{"id": 0, "type": "icon", "label": "local",
                                                                       "interactive": True, "center": [1, 2]}])
    p = server._do_parse(6)
    assert p["via"] == "in-process" and p["elements"][0]["label"] == "local"
    assert server._SERVICE_FAILED_AT[0] > 0                       # back-off recorded


def test_server_reports_service_errors_structured(service, local_mode, sock_path, monkeypatch):
    monkeypatch.setattr(service.parser, "parse",
                        lambda img, bt, ann: (_ for _ in ()).throw(vision.VisionError("GPU too small", "use basic")))
    with pytest.raises(ToolError) as e:
        server._do_parse(7)
    payload = json.loads(str(e.value))
    assert payload["ok"] is False and payload["error"] == "GPU too small" and payload["fix"] == "use basic"


def test_service_disabled_env(local_mode, monkeypatch):
    monkeypatch.setattr(server, "USE_SERVICE", False)
    monkeypatch.setattr(server._OMNI, "parse", lambda img, bt, ann: [])
    assert server._do_parse(8)["via"] == "in-process"
