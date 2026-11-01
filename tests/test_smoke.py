"""No-display smoke tests: the server imports, registers all tools, and keeps its contract helpers
and vision-resolution rules honest. Everything here runs without an X server or GPU."""
import json
import subprocess

import pytest
from PIL import Image

import server
import vision
from _version import __version__
from mcp.server.fastmcp.exceptions import ToolError

EXPECTED_TOOLS = {
    "launch_app", "list_instances", "close_instance", "get_screen_size", "screenshot",
    "parse_screen", "click", "drag", "double_click", "click_element", "type_text",
    "upload_file", "open_file", "list_files", "press_keys", "move_mouse", "scroll",
    "wait_ms", "status",
    # 0.3.0
    "clipboard", "wait_for_stable", "window_info", "app_log",
}


def test_all_tools_registered():
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS
    assert len(names) == 23


def test_descriptions_are_agent_first():
    """Every tool description must say more than a one-liner (returns shape / when-to-use / caveats)."""
    for t in server.mcp._tool_manager.list_tools():
        assert t.description, f"{t.name} has no description"
        assert len(t.description) > 120, f"{t.name} description too thin: {t.description!r}"
        assert "Returns" in t.description or "returns" in t.description, f"{t.name} doesn't state its return shape"


def test_version_reaches_handshake():
    assert server.mcp._mcp_server.version == __version__
    opts = server.mcp._mcp_server.create_initialization_options()
    assert opts.server_version == __version__


def test_fail_raises_structured_toolerror():
    with pytest.raises(ToolError) as e:
        server._fail("click", "it broke", fix="run list_instances()", instance=3)
    payload = json.loads(str(e.value))
    assert payload == {"ok": False, "action": "click", "instance": 3, "display": ":3",
                       "fix": "run list_instances()", "error": "it broke"}


def test_ok_payload_shape():
    payload = json.loads(server._ok("wait_ms", requested_ms=5, waited_ms=5, clamped=False))
    assert payload["ok"] is True and payload["action"] == "wait_ms"
    assert payload["clamped"] is False


def test_observe_mode_validated_before_acting():
    with pytest.raises(ToolError) as e:
        server._validate_observe("click", 1, "screenshoot")
    payload = json.loads(str(e.value))
    assert payload["ok"] is False and "not performed" in payload["error"]


# ---------- 0.3.0 fixes ----------
def test_xdotool_args_are_option_safe(monkeypatch):
    """User values go after `--` so '-5' or '-h' can never be parsed as xdotool options."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server._xdo(7, "click", "mousemove", -5, 10, opts=("--sync",))
    assert seen["cmd"] == ["xdotool", "mousemove", "--sync", "--", "-5", "10"]
    assert "timeout" in server._xdo.__code__.co_names or True  # timeout is passed (see below)


def test_xdotool_calls_have_a_timeout(monkeypatch):
    def fake_run(cmd, **kw):
        assert kw.get("timeout"), "xdotool must never be allowed to hang a tool call forever"
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server._xdo(7, "click", "click", 1)


def test_coordinates_outside_display_are_rejected():
    server._GEOM[42] = (800, 600)
    server._check_xy("click", 42, (0, 0), (799, 599))   # corners are fine
    for bad in ((800, 10), (10, 600), (-1, 10), (10, -1), (9999, 9999)):
        with pytest.raises(ToolError) as e:
            server._check_xy("click", 42, bad)
        p = json.loads(str(e.value))
        assert p["ok"] is False and "nothing was done" in p["error"] and p["width"] == 800
    server._GEOM.pop(42, None)


def test_kill_pattern_is_anchored(monkeypatch):
    """An unanchored `pkill -f 'Xephyr :2 '` also kills any shell whose command line contains it."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server, "_up", lambda n: False)
    server._kill_xephyr(2)
    pkill = [c for c in calls if c[0] == "pkill"][0]
    assert pkill[-1].startswith("^Xephyr :2 ")


def test_changed_fraction():
    a = Image.new("RGB", (100, 100), "white")
    b = a.copy()
    assert server._changed_fraction(a, b) == 0.0
    b.paste("black", (0, 0, 10, 100))   # 10% of the pixels
    assert abs(server._changed_fraction(a, b) - 0.10) < 0.001
    assert server._changed_fraction(a, Image.new("RGB", (50, 50))) == 1.0   # size mismatch = changed


def test_browser_family_gets_cdp_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    for b in ("chromium", "google-chrome-stable", "brave-browser", "microsoft-edge"):
        env, args, base = server._app_env_args(5, f"{b} https://example.org")
        assert base == b
        assert any(a.startswith("--remote-debugging-port=9227") for a in args), b
        assert env["GDK_BACKEND"] == "x11" and "WAYLAND_DISPLAY" not in env
    env, args, base = server._app_env_args(5, "gimp")
    assert "--remote-debugging-port" not in " ".join(args)


# ---------- vision resolution ----------
def test_requested_mode_defaults_to_auto(monkeypatch):
    monkeypatch.setenv("GLOVEBOX_VISION", "basic")
    assert vision.requested_mode() == "basic"
    monkeypatch.setenv("GLOVEBOX_VISION", "bogus")
    assert vision.requested_mode() == "auto"   # unknown values -> auto (never a hard-coded GPU mode)


READY = {"ready": True, "reason": "ok"}
NOT_READY = {"ready": False, "reason": "OmniParser weights missing"}
TESS_READY = {"ready": True, "reason": "ok"}
TESS_MISSING = {"ready": False, "reason": "tesseract binary not installed"}


def test_resolve_auto_picks_best_ready_backend():
    assert vision.resolve("auto", READY, TESS_READY)["effective"] == "local"
    r = vision.resolve("auto", NOT_READY, TESS_READY)
    assert r["effective"] == "basic" and r["fallback"] and "weights missing" in r["reason"]
    r = vision.resolve("auto", NOT_READY, TESS_MISSING)
    assert r["effective"] == "none" and r["ready"] and r["fallback"]


def test_resolve_explicit_backend_is_strict():
    """An explicitly requested backend that isn't usable must surface as not-ready (parse_screen
    then fails with the fix named) — never silently fall back."""
    r = vision.resolve("local", NOT_READY, TESS_READY)
    assert r["effective"] == "local" and r["ready"] is False and not r["fallback"]
    r = vision.resolve("basic", READY, TESS_MISSING)
    assert r["effective"] == "basic" and r["ready"] is False
    assert vision.resolve("none", NOT_READY, TESS_MISSING)["ready"] is True


def test_gpu_preflight_thresholds(monkeypatch):
    monkeypatch.setattr(vision.shutil, "which", lambda b: "/usr/bin/nvidia-smi")

    def smi(total, free):
        return lambda *a, **k: subprocess.CompletedProcess(a, 0, f"Some GPU, {total}, {free}\n", "")
    monkeypatch.setattr(vision.subprocess, "run", smi(4096, 4000))
    g = vision.gpu_info()
    assert g["present"] and g["enough"] is False and "6000" in g["note"]
    monkeypatch.setattr(vision.subprocess, "run", smi(16384, 12000))
    g = vision.gpu_info()
    assert g["enough"] and g["note"] == "ok" and g["name"] == "Some GPU"
    monkeypatch.setattr(vision.subprocess, "run", smi(16384, 1000))
    assert "free" in vision.gpu_info()["note"]
    monkeypatch.setattr(vision.shutil, "which", lambda b: None)
    assert vision.gpu_info()["present"] is False


def test_omniparser_info_names_the_missing_piece(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "OMNI_DIR", str(tmp_path / "OmniParser"))
    gpu = {"present": True, "enough": True, "note": "ok", "vram_free_mb": 9000}
    info = vision.omniparser_info(gpu)
    assert info["ready"] is False and "code not found" in info["reason"]
    gpu_small = {"present": True, "enough": False, "note": "4096 MiB VRAM < 6000 MiB needed", "vram_free_mb": 4000}
    monkeypatch.setattr(vision.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(vision.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(vision.importlib.util, "find_spec", lambda n: object())
    assert vision.omniparser_info(gpu_small)["reason"].startswith("4096 MiB")
    assert vision.omniparser_info(gpu)["ready"] is True


def test_tesseract_lang_is_filtered_to_installed(monkeypatch):
    monkeypatch.delenv("GLOVEBOX_TESS_LANG", raising=False)
    monkeypatch.delenv("LC_TESS_LANG", raising=False)
    assert vision.tesseract_lang_string(["eng", "osd"]) == "eng"
    assert vision.tesseract_lang_string(["slv", "eng"]) == "slv+eng"
    assert vision.tesseract_lang_string(["deu"]) == "eng"
    monkeypatch.setenv("GLOVEBOX_TESS_LANG", "deu")
    assert vision.tesseract_lang_string(["eng"]) == "deu"


def test_group_tesseract_lines_and_words():
    data = {"text": ["Sign", "in", "", "Cancel"], "conf": ["90", "85", "-1", "30"],
            "left": [10, 50, 0, 10], "top": [10, 10, 0, 50], "width": [30, 15, 0, 40], "height": [12, 12, 0, 12],
            "block_num": [1, 1, 1, 1], "par_num": [1, 1, 1, 1], "line_num": [1, 1, 1, 2]}
    elems, boxes = vision.group_tesseract(data, "line")
    assert [e["label"] for e in elems] == ["Sign in"]          # low-confidence "Cancel" dropped
    assert boxes[0] == (10, 10, 65, 22) and elems[0]["center"] == [37, 16]
    assert elems[0]["confidence"] == 88
    elems, _ = vision.group_tesseract(data, "word")
    assert [e["label"] for e in elems] == ["Sign", "in"]


def test_vision_error_becomes_structured_failure(monkeypatch):
    monkeypatch.setitem(server.VISION, "effective", "basic")
    monkeypatch.setitem(server.VISION, "ready", True)
    monkeypatch.setattr(server, "_grab_checked", lambda n, a: Image.new("RGB", (20, 20)))
    monkeypatch.setattr(server.vision, "parse_tesseract",
                        lambda img, g: (_ for _ in ()).throw(vision.VisionError("tesseract failed", "install it")))
    with pytest.raises(ToolError) as e:
        server._do_parse(3)
    p = json.loads(str(e.value))
    assert p["ok"] is False and p["error"] == "tesseract failed" and p["fix"] == "install it"


def test_requested_but_unusable_backend_fails_with_fix(monkeypatch):
    monkeypatch.setitem(server.VISION, "effective", "local")
    monkeypatch.setitem(server.VISION, "ready", False)
    monkeypatch.setitem(server.VISION, "reason", "GPU too small")
    with pytest.raises(ToolError) as e:
        server._do_parse(3)
    p = json.loads(str(e.value))
    assert "GPU too small" in p["error"] and "install.sh local" in p["fix"]
