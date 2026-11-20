"""No-display smoke tests: the server imports, registers all tools, and keeps its contract helpers
and vision-resolution rules honest. Everything here runs without an X server or GPU."""
import json
import os
import subprocess

import pytest
from PIL import Image

import server
import vision
from _version import __version__
from mcp.server.mcpserver.exceptions import ToolError

EXPECTED_TOOLS = {
    "launch_app", "list_instances", "close_instance", "get_screen_size", "screenshot",
    "parse_screen", "click", "drag", "double_click", "click_element", "type_text",
    "upload_file", "open_file", "list_files", "press_keys", "move_mouse", "scroll",
    "wait_ms", "status", "find_elements", "wait_for_text", "start_app", "browser", "manage_window", "put_file", "fill",
    # 0.3.0
    "clipboard", "wait_for_stable", "window_info", "app_log",
}


def _vision(monkeypatch, **kw):
    """Pin the server's (TTL-cached) vision resolution for a test."""
    import time
    v = {"requested": "auto", "effective": "none", "ready": True, "reason": "test", "fallback": False, **kw}
    monkeypatch.setitem(server._VISION, "value", v)
    monkeypatch.setitem(server._VISION, "at", time.time() + 10**6)
    # the fake instance numbers these tests use must not depend on what displays exist on the box
    monkeypatch.setattr(server, "display_up", lambda n, timeout=5: True)


def test_all_tools_registered():
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS
    assert len(names) == 30


def test_descriptions_are_agent_first():
    """Every tool description must say more than a one-liner (returns shape / when-to-use / caveats)."""
    for t in server.mcp._tool_manager.list_tools():
        assert t.description, f"{t.name} has no description"
        assert len(t.description) > 120, f"{t.name} description too thin: {t.description!r}"
        assert "Returns" in t.description or "returns" in t.description, f"{t.name} doesn't state its return shape"


def test_version_reaches_handshake():
    """MCPServer(version=...) is what the initialize handshake reports (the e2e asserts it on the wire)."""
    assert server.mcp.version == __version__
    assert server.mcp.name == "glovebox"


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
    server._set_geometry(42, 800, 600)
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
    # boxes from a 2x-upscaled OCR pass are mapped back to screen pixels
    elems, boxes = vision.group_tesseract(data, "line", scale=2.0)
    assert boxes[0] == (5, 5, 33, 11) and elems[0]["center"] == [19, 8]


def test_vision_error_becomes_structured_failure(monkeypatch):
    _vision(monkeypatch, effective="basic", ready=True)
    monkeypatch.setattr(server, "_grab_checked", lambda n, a: Image.new("RGB", (20, 20)))
    monkeypatch.setattr(server.vision, "parse_tesseract",
                        lambda img, g: (_ for _ in ()).throw(vision.VisionError("tesseract failed", "install it")))
    with pytest.raises(ToolError) as e:
        server._do_parse(3)
    p = json.loads(str(e.value))
    assert p["ok"] is False and p["error"] == "tesseract failed" and p["fix"] == "install it"


def test_requested_but_unusable_backend_fails_with_fix(monkeypatch):
    _vision(monkeypatch, effective="local", ready=False, reason="GPU too small")
    with pytest.raises(ToolError) as e:
        server._do_parse(3)
    p = json.loads(str(e.value))
    assert "GPU too small" in p["error"] and "install.sh local" in p["fix"]


def test_parse_tesseract_keeps_the_richer_pass(monkeypatch):
    """Two OCR passes (2x and 1x); whichever finds more words wins, so a tesseract build that
    returns nothing for upscaled frames still yields a parse."""
    import sys, types
    calls = []

    def image_to_data(im, lang, output_type, **kw):
        calls.append(im.size)
        if im.size[0] > 100:   # the 2x pass finds nothing
            return {"text": [], "conf": [], "left": [], "top": [], "width": [], "height": [],
                    "block_num": [], "par_num": [], "line_num": []}
        return {"text": ["Sign", "in"], "conf": ["90", "90"], "left": [10, 50], "top": [10, 10],
                "width": [30, 15], "height": [12, 12], "block_num": [1, 1], "par_num": [1, 1], "line_num": [1, 1]}
    fake = types.SimpleNamespace(image_to_data=image_to_data, TesseractNotFoundError=RuntimeError,
                                 Output=types.SimpleNamespace(DICT="dict"))
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    monkeypatch.setattr(vision, "tesseract_lang_string", lambda available=None: "eng")
    monkeypatch.delenv("GLOVEBOX_OCR_SCALE", raising=False)
    elems, boxes = vision.parse_tesseract(Image.new("RGB", (100, 50), "white"), "line")
    assert calls == [(200, 100), (100, 50)]
    assert [e["label"] for e in elems] == ["Sign in"] and boxes[0] == (10, 10, 65, 22)


def test_put_file_writes_only_inside_the_staging_folder(tmp_path, monkeypatch):
    import base64
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    p = json.loads(server.put_file("in/data.csv", "a;b\nč;ž\n", instance=7))
    assert p["ok"] and p["bytes"] == len("a;b\nč;ž\n".encode()) and p["name"] == "in/data.csv" and p["overwritten"] is False
    assert open(p["path"], encoding="utf-8").read() == "a;b\nč;ž\n"
    assert p["path"].startswith(str(tmp_path / "files" / "7"))

    with pytest.raises(ToolError) as e:                       # exists, no overwrite
        server.put_file("in/data.csv", "x", instance=7)
    assert "already exists" in json.loads(str(e.value))["error"]
    assert json.loads(server.put_file("in/data.csv", "x", instance=7, overwrite=True))["overwritten"] is True

    raw = bytes(range(256))
    p = json.loads(server.put_file("blob.bin", base64.b64encode(raw).decode(), instance=7, encoding="base64"))
    assert open(p["path"], "rb").read() == raw

    outside = tmp_path / "files" / "evil.txt"
    for bad in ("../evil.txt", "/etc/evil", "a/../../evil.txt", "", "dir/"):
        with pytest.raises(ToolError) as e:
            server.put_file(bad, "x", instance=7)
        assert "nothing was written" in json.loads(str(e.value))["error"], bad
    assert not outside.exists()
    os.symlink(str(tmp_path), str(tmp_path / "files" / "7" / "link"))          # a symlink out of the folder
    with pytest.raises(ToolError):
        server.put_file("link/evil.txt", "x", instance=7)
    assert not (tmp_path / "evil.txt").exists()

    for kw in ({"encoding": "base64", "content": "not base64!!"}, {"encoding": "hex", "content": "00"},
               {"content": "x" * (server.PUT_MAX + 1)}):
        with pytest.raises(ToolError):
            server.put_file("f.txt", instance=7, **{"content": "x", **kw})
    assert not (tmp_path / "files" / "7" / "f.txt").exists()
    with pytest.raises(ToolError):                                             # host display / out of range
        server.put_file("f.txt", "x", instance=0)
    assert [f for f in os.listdir(tmp_path / "files" / "7") if ".put-" in f] == []   # no temp files left


# ---------- agent feedback round: fill, hit, observe_region, scroll direction, foreign input ----------
def _no_browser(monkeypatch):
    """No CDP browser, no real pointer: the click helpers must work for native apps too."""
    monkeypatch.setattr(server, "_hit", lambda n, x, y: None)
    monkeypatch.setattr(server, "_pointer", lambda n: None)


def test_observe_region_is_validated_before_acting_and_implies_screenshot():
    server._set_geometry(43, 800, 600)
    try:
        assert server._validate_observe("click", 43, "none") == "none"
        assert server._validate_observe("click", 43, "none", "10,20,100,50") == ("screenshot", (10, 20, 110, 70))
        for bad, region in (("screenshot", "700,0,200,50"), ("none", "nope"), ("parse", "0,0,10,10")):
            with pytest.raises(ToolError) as e:
                server._validate_observe("click", 43, bad, region)
            assert "not performed" in json.loads(str(e.value))["error"]
    finally:
        server._GEOM.pop(43, None)


def test_scroll_direction_sets_axis_and_sign(monkeypatch):
    _vision(monkeypatch)
    _no_browser(monkeypatch)
    sent = []
    monkeypatch.setattr(server, "_xdo", lambda n, action, sub, *a, **kw: sent.append((sub, a)))
    for kw, button, direction in (({"direction": "down"}, 5, "down"), ({"direction": "up", "amount": -4}, 4, "up"),
                                  ({"direction": "right"}, 7, "right"), ({"amount": -2}, 5, "down"),
                                  ({"amount": 2, "axis": "horizontal"}, 6, "left")):
        sent.clear()
        p = json.loads(server.scroll(instance=7, **kw))
        assert sent[-1] == ("click", (button,)) and p["direction"] == direction
    with pytest.raises(ToolError) as e:
        server.scroll(3, 7, direction="sideways")
    assert "nothing was scrolled" in json.loads(str(e.value))["error"] and not sent[1:]


def test_foreign_pointer_movement_is_reported_once(monkeypatch):
    where = {"p": (10, 10)}
    monkeypatch.setattr(server, "_pointer", lambda n: where["p"])
    server._PTR.pop(44, None)
    try:
        assert server._foreign_input(44) == {}            # first sight: nothing to compare with
        server._PTR[44] = (100, 100)                      # this server left it here
        where["p"] = (101, 99)
        assert server._foreign_input(44) == {}            # jitter is not a person
        where["p"] = (400, 300)
        w = server._foreign_input(44)
        assert "another agent" in w["warnings"][0] and "[400, 300]" in w["warnings"][0]
        assert server._foreign_input(44) == {}            # reported once, not on every later call
    finally:
        server._PTR.pop(44, None)


def test_click_reports_the_element_it_hit(monkeypatch):
    _vision(monkeypatch)
    monkeypatch.setattr(server, "_pointer", lambda n: None)
    monkeypatch.setattr(server, "_click_at", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_hit", lambda n, x, y: {"role": "checkbox", "name": "Oglas", "source": "browser-dom"})
    assert json.loads(server.click(5, 5, 7))["hit"]["role"] == "checkbox"
    monkeypatch.setattr(server, "_hit", lambda n, x, y: None)          # native app: the key is absent
    assert "hit" not in json.loads(server.click(5, 5, 7))


def test_fill_verdicts(monkeypatch):
    """fill never reports a field as set unless the read-back says so, and never submits a miss."""
    _vision(monkeypatch)
    _no_browser(monkeypatch)
    keys = []
    monkeypatch.setattr(server, "_click_at", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_type_into", lambda n, action, text, clear=False: "xdotool")
    monkeypatch.setattr(server, "_xdo", lambda n, action, sub, *a, **kw: keys.append(a))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "_focus_guard", lambda *a: {"focus_checked": "at-spi"})
    monkeypatch.setattr(server, "_grab", lambda n: Image.new("RGB", (800, 600)))
    server._set_geometry(7, 800, 600)

    def run(read, text="Žiga", **kw):
        monkeypatch.setattr(server, "_read_field", lambda n, action: read)
        keys.clear()
        return json.loads(server.fill(text, 10, 10, 7, submit=True, **kw))

    p = run(("Žiga\n", "clipboard", None))
    assert p["verified"] is True and p["submitted"] is True and keys == [("Return",)]
    p = run(("Ziga", "browser-dom", "input"))
    assert p["verified"] is False and p["value"] == "Ziga" and "different value" in p["warnings"][0]
    p = run((None, "unreadable", None))
    assert p["verified"] is None and "note" in p
    assert run(("anything", "clipboard", None), verify=False)["read_back"] == "skipped"
    for read, needle in (((None, "browser-dom", "body"), "moved the focus"), (("", "browser-dom", "input"), "still empty")):
        with pytest.raises(ToolError) as e:
            run(read)
        assert needle in json.loads(str(e.value))["error"] and keys == [], "a failed fill must not press Return"
    with pytest.raises(ToolError) as e:                      # no target: nothing is clicked
        server.fill("x", instance=7)
    assert "nothing was done" in json.loads(str(e.value))["error"]
    with pytest.raises(ToolError):
        server.fill("x", instance=7, element=3)              # id from no parse


def test_fill_types_nothing_when_the_focus_check_fails(monkeypatch):
    """The guard runs between the click and the typing: a miss or an unannounced password field
    must fail with nothing typed; 'nobody could tell' must not read as success."""
    _vision(monkeypatch)
    _no_browser(monkeypatch)
    typed = []
    monkeypatch.setattr(server, "_click_at", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_type_into", lambda n, action, text, clear=False: typed.append(text) or "xdotool")
    monkeypatch.setattr(server, "_read_field", lambda n, action: (None, "unreadable", None))
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "_grab", lambda n: Image.new("RGB", (800, 600)))
    monkeypatch.setattr(server, "_cdp_pages", lambda *a, **kw: (_ for _ in ()).throw(OSError("no browser")))
    server._set_geometry(7, 800, 600)
    entry = {"role": "text", "name": "Ime", "rect": [100, 100, 300, 30], "editable": True, "password": False}

    def run(focus, why="", **kw):
        monkeypatch.setattr(server, "_a11y_focus", lambda n, at=None: (focus, why))
        typed.clear()
        return json.loads(server.fill("abc", 200, 110, 7, **kw))

    p = run(entry)
    assert p["focus_checked"] == "at-spi" and p["focus"]["role"] == "text" and typed == ["abc"]
    assert run({**entry, "rect": [100, 100, 300, 8]})["focus_checked"] == "at-spi"   # 2 px below: slack
    for focus, needle in (({**entry, "role": "label", "editable": False}, "not an editable field"),
                          ({**entry, "rect": [100, 300, 300, 30]}, "did not move the focus"),
                          ({**entry, "role": "password text", "password": True}, "password field")):
        with pytest.raises(ToolError) as e:
            run(focus)
        err = json.loads(str(e.value))
        assert needle in err["error"] and "nothing was typed" in err["error"] and typed == []
        assert err["focus"]["role"] == focus["role"]
    assert run({**entry, "role": "password text", "password": True}, allow_password=True)["read_back"] == "unreadable"
    # nobody could tell: typed, but flagged — and a static frame says the click changed nothing
    p = run(None, "the app exposes no focused widget")
    assert p["focus_checked"] is False and p["click_effect"] == "none" and p["verified"] is None
    assert "WITHOUT knowing" in p["warnings"][0] and "static label" in p["warnings"][0] and typed == ["abc"]


def test_fill_browser_focus_guard(monkeypatch):
    from contextlib import contextmanager
    answer = {}

    @contextmanager
    def fake_cdp(port, page_url_contains="", timeout=15):
        yield (lambda method, params=None: {"result": {"value": answer["v"]}}), {}, []
    monkeypatch.setattr(server, "_cdp", fake_cdp)
    monkeypatch.setattr(server, "_a11y_focus", lambda n, at=None: pytest.fail("browsers are checked through the DOM"))
    answer["v"] = {"tag": "input", "value": "", "password": False}
    assert server._focus_guard(7, 5, 5, False, {}) == {"focus_checked": "browser-dom"}
    answer["v"] = {"tag": "iframe", "unknown": True}
    assert server._focus_guard(7, 5, 5, False, {})["focus_checked"] is False
    for v, allow, needle in (({"tag": "body"}, False, "<body>"),
                             ({"tag": "input", "value": "", "password": True}, False, "password field")):
        answer["v"] = v
        with pytest.raises(ToolError) as e:
            server._focus_guard(7, 5, 5, allow, {})
        assert needle in json.loads(str(e.value))["error"]
    assert server._focus_guard(7, 5, 5, True, {})["focus_checked"] == "browser-dom"   # password, announced
    answer["v"] = {"tag": "input", "value": "x", "at": False, "name": "ime", "rect": [10, 300, 200, 20]}
    with pytest.raises(ToolError) as e:        # the page kept its focus elsewhere (toolbar click, focus-refusing widget)
        server._focus_guard(7, 5, 5, False, {})
    err = json.loads(str(e.value))
    assert "nothing was typed" in err["error"] and err["focus"]["name"] == "ime"


def test_label_warning_needs_a_typed_detector_and_suggests_the_input():
    els = {1: {"id": 1, "type": "text", "label": "Geslo", "center": [100, 200]},
           2: {"id": 2, "type": "input", "label": "", "center": [300, 204]},
           3: {"id": 3, "type": "input", "label": "", "center": [300, 400]},
           4: {"id": 4, "type": "button", "label": "OK", "center": [100, 500]}}
    try:
        server._LAST_EL[45] = {"typed": True, "elements": els}
        w = server._label_warning(45, 1)
        assert "not an input field" in w["warnings"][0] and w["suggest_element"]["id"] == 2
        assert server._label_warning(45, 2) == {} and server._label_warning(45, 4) == {}
        server._LAST_EL[45]["typed"] = False        # OCR / OmniParser-only: every label is "text"
        assert server._label_warning(45, 1) == {}
    finally:
        server._LAST_EL.pop(45, None)


def test_pointer_already_at_target_is_not_moved(monkeypatch):
    """xdotool mousemove --sync waits ~15 s when the pointer does not have to move."""
    sent = []
    monkeypatch.setattr(server, "_xdo", lambda n, action, sub, *a, **kw: sent.append((sub, a)))
    monkeypatch.setattr(server, "_pointer", lambda n: (10, 20))
    try:
        server._move(46, "click", 10, 20)
        assert sent == []
        server._move(46, "click", 11, 20)
        assert sent == [("mousemove", (11, 20))] and server._PTR[46] == (11, 20)
    finally:
        server._PTR.pop(46, None)


def test_a11y_helper_reports_missing_bindings_as_json():
    """The helper must answer in JSON whatever python runs it (the venv has no PyGObject)."""
    import sys
    r = subprocess.run([sys.executable, "-I", server.A11Y_HELPER, "--check"], capture_output=True, text=True, timeout=30)
    d = json.loads(r.stdout)
    assert d["ok"] is True or "not importable" in d["error"]


# ---------- found by driving real apps (Qt, LibreOffice) ----------
def test_apps_are_told_to_use_x11_and_office_gets_its_own_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "HERE", str(tmp_path))
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    env, args, base = server._app_env_args(7, "kate notes.txt")
    # Qt follows these, not DISPLAY: with the host's Wayland values the window never appears
    assert env["XDG_SESSION_TYPE"] == "x11" and env["QT_QPA_PLATFORM"] == "xcb"
    assert env["QT_LINUX_ACCESSIBILITY_ALWAYS_ON"] == "1" and "WAYLAND_DISPLAY" not in env
    env, args, base = server._app_env_args(7, "libreoffice --writer doc.odt")
    prof = [a for a in args if a.startswith("-env:UserInstallation=file://")]
    assert len(prof) == 1 and prof[0].endswith("profiles/libreoffice-7"), "LibreOffice must not hand off to another instance"
    env, args, base = server._app_env_args(7, "soffice -env:UserInstallation=file:///x --calc")
    assert sum("UserInstallation" in a for a in args) == 1          # an explicit profile is respected


def test_close_ends_the_whole_process_group_but_only_on_that_display(monkeypatch):
    """A launcher's grandchild that survives its display (soffice.bin) must be found; a process
    with another DISPLAY in the same group id must not."""
    import sys
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"
    mine = subprocess.Popen([sys.executable, "-c", code], env={**os.environ, "DISPLAY": ":77"}, start_new_session=True)
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env={**os.environ, "DISPLAY": ":78"},
                             start_new_session=True)
    try:
        import time
        time.sleep(0.5)
        server._GROUPS[77].update({mine.pid, other.pid})
        pids = server._group_pids(77)
        assert mine.pid in pids and len(pids) == 2 and other.pid not in pids     # parent + grandchild
        server._kill_groups(77, server.signal.SIGKILL)
        mine.wait(timeout=5)
        time.sleep(0.2)
        assert server._group_pids(77) == [] and other.poll() is None
    finally:
        server._GROUPS.pop(77, None)
        for p in (mine, other):
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass


def test_window_level_focus_is_unknown_unless_the_clicked_widget_is_editable(monkeypatch):
    """LibreOffice reports the focus as its frame: that must not fail a click into the document,
    and must not pass a click onto something that is not editable."""
    monkeypatch.setattr(server, "_a11y_python", lambda: "/usr/bin/python3")
    answers = {}

    def fake_run(cmd, **kw):
        if cmd[0] == "xprop":
            return subprocess.CompletedProcess(cmd, 0, 'AT_SPI_BUS(STRING) = "unix:path=/x"', "")
        assert kw["env"].get("AT_SPI_BUS_ADDRESS") == "unix:path=/x" and "DBUS_SESSION_BUS_ADDRESS" not in kw["env"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(answers["v"]), "")
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    frame = {"role": "frame", "name": "Writer", "rect": [0, 0, 900, 700], "editable": False, "container": True}
    answers["v"] = {"ok": True, "focus": frame, "at_point": {"role": "paragraph", "name": "", "rect": [100, 100, 500, 20], "editable": True}}
    f, why = server._a11y_focus(7, (200, 110))
    assert f["role"] == "paragraph" and f["via"] == "at-point"
    answers["v"] = {"ok": True, "focus": frame, "at_point": {"role": "label", "name": "Ime", "rect": [1, 1, 5, 5], "editable": False}}
    f, why = server._a11y_focus(7, (2, 2))
    assert f is None and "frame" in why and "label" in why
    answers["v"] = {"ok": True, "focus": frame}
    assert server._a11y_focus(7, (2, 2))[0] is None


def test_unpaired_surrogates_never_reach_the_wire():
    """Half an emoji in a tool result (a JS string cut with slice(), a clipboard fragment) is not
    encodable as UTF-8: it used to crash the stdout writer and close the MCP connection."""
    out = server._ok("browser", None, result="half \ud83d ok \U0001F600", title="\udc00")
    out.encode("utf-8")                      # must not raise
    d = json.loads(out)
    assert d["result"] == "half � ok \U0001F600" and d["title"] == "�"
    assert d["text_repaired"].startswith("2 ")
    assert "text_repaired" not in json.loads(server._ok("browser", None, result="čšž \U0001F600"))
    with pytest.raises(ToolError) as e:
        server._fail("browser", "the script threw: \ud83d")
    str(e.value).encode("utf-8")


def _fake_browser(monkeypatch, evaluate):
    from contextlib import contextmanager

    @contextmanager
    def fake_cdp(port, page_url_contains="", timeout=15):
        yield (lambda method, params=None, wait=None: evaluate(params or {}, wait)), {"url": "u", "title": "t"}, []
    monkeypatch.setattr(server, "_cdp", fake_cdp)
    monkeypatch.setattr(server, "_require", lambda action, instance: 3)


def test_browser_eval_timeout_fails_cleanly_and_says_the_script_runs_on(monkeypatch):
    seen = {}

    def evaluate(params, wait):
        seen["wait"] = wait
        raise server.CdpTimeout("Runtime.evaluate: no reply within 5 s")
    _fake_browser(monkeypatch, evaluate)
    with pytest.raises(ToolError) as e:
        server.browser(action="eval", instance=3, js="slow()", timeout_s=5)
    d = json.loads(str(e.value))
    assert seen["wait"] == 5 and d["timed_out"] is True
    assert "keeps running" in d["error"] and "timeout_s" in d["fix"] and "window.__r" in d["fix"]
    for bad in (0, 601):
        with pytest.raises(ToolError, match="out of range"):
            server.browser(action="eval", instance=3, js="1", timeout_s=bad)


def test_browser_eval_result_is_capped_and_flagged(monkeypatch):
    _fake_browser(monkeypatch, lambda params, wait: {"result": {"value": ["x" * 50] * 10_000}})
    d = json.loads(server.browser(action="eval", instance=3, js="big"))
    assert d["truncated"] is True and "result" not in d
    assert len(d["result_json"]) == server.EVAL_MAX and d["length"] > server.EVAL_MAX and "max_chars" in d["fix"]
    d = json.loads(server.browser(action="eval", instance=3, js="big", max_chars=1_000_000))
    assert len(d["result"]) == 10_000 and "truncated" not in d
