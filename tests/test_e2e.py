"""Display-gated e2e: a real stdio round-trip against a real X server on :99.

The fixture prefers Xvfb (what CI has); locally it falls back to Xephyr on the host display.
A window manager (openbox) is started on :99 when available — without one Xephyr ignores
xdotool pointer motion, which is exactly the kind of silent failure these tests exist to catch.
Everything self-skips when no X server can be started, so the smoke tests still run anywhere.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import pytest

from conftest import REPO

DISP = 99          # the test display / instance number (far from the :1.. _SCAN_MAX user range)
DEAD = 98          # never started — for dead-instance error paths
XENV = {**os.environ, "DISPLAY": f":{DISP}"}

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") or shutil.which("Xephyr")) or not shutil.which("xdpyinfo"),
    reason="no X server (Xvfb/Xephyr) or xdpyinfo available",
)
HAS_WM = bool(shutil.which("openbox"))
HAS_XMESSAGE = bool(shutil.which("xmessage"))


def _display_up(disp: str) -> bool:
    return subprocess.run(["xdpyinfo", "-display", disp], capture_output=True).returncode == 0


@pytest.fixture(scope="module")
def x99():
    """A real X server on :99 — Xvfb if present (CI), else Xephyr nested in the host display."""
    if _display_up(f":{DISP}"):
        pytest.skip(f":{DISP} is already in use")
    if shutil.which("Xvfb"):
        proc = subprocess.Popen(["Xvfb", f":{DISP}", "-screen", "0", "1024x768x24"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        host = os.environ.get("GLOVEBOX_HOST_DISPLAY", ":0")
        if not _display_up(host):
            pytest.skip(f"no Xvfb and host display {host} unreachable for Xephyr")
        proc = subprocess.Popen(["Xephyr", f":{DISP}", "-screen", "1024x768", "-ac"],
                                env={**os.environ, "DISPLAY": host},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if _display_up(f":{DISP}"):
            break
        time.sleep(0.25)
    else:
        proc.terminate()
        pytest.skip(f"X server on :{DISP} did not come up")
    wm = None
    if HAS_WM:
        wm = subprocess.Popen(["openbox"], env=XENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(0.6)
    yield f":{DISP}"
    if wm:
        wm.terminate()
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
def xmessage_window(x99):
    """A real window with text on :99 (OCR target, window_info target)."""
    if not HAS_XMESSAGE:
        pytest.skip("xmessage not installed")
    # -fn 10x20: a bitmap font from xfonts-base (CI has no scalable fonts; the 6x13 fallback is unreadable)
    # padded with spaces so the glyphs don't touch the window border (OCR eats edge letters)
    p = subprocess.Popen(["xmessage", "-fn", "10x20", "-center", "    Sign in to continue    "], env=XENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    yield p
    p.terminate()


@asynccontextmanager
async def glovebox(extra_env=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {**os.environ, "GLOVEBOX_VISION": "none", **(extra_env or {})}
    params = StdioServerParameters(command=sys.executable,
                                   args=[os.path.join(REPO, "server.py")], env=env)
    try:
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                yield s, init
    finally:
        # reap the subprocess transport while the loop is still open, or its __del__
        # fires after asyncio.run() closed the loop and pytest flags the teardown
        import gc
        gc.collect()
        await asyncio.sleep(0.05)


def payload(res):
    """Extract the JSON payload — failures arrive as isError text with the JSON embedded."""
    text = res.content[0].text
    return json.loads(text[text.find("{"):])


def pointer():
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"], env=XENV, capture_output=True, text=True).stdout
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    return int(kv["X"]), int(kv["Y"])


def test_handshake_reports_project_version(x99):
    from _version import __version__

    async def run():
        async with glovebox() as (s, init):
            assert init.server_info.name == "glovebox"
            assert init.server_info.version == __version__
            tools = await s.list_tools()
            assert len(tools.tools) == 30
    asyncio.run(run())


def test_control_roundtrip_and_error_paths(x99):
    async def run():
        async with glovebox() as (s, _):
            # happy path against the real display
            r = await s.call_tool("get_screen_size", {"instance": DISP})
            p = payload(r)
            assert not r.is_error and p["ok"] and (p["width"], p["height"]) == (1024, 768)
            assert p["vision"] == "none"

            # screenshot returns [json, image]
            r = await s.call_tool("screenshot", {"instance": DISP})
            p = payload(r)
            assert not r.is_error and p["format"] == "png" and p["width"] == 1024
            assert len(r.content) == 2 and r.content[1].type == "image"

            r = await s.call_tool("click", {"x": 10, "y": 10, "instance": DISP})
            assert not r.is_error and payload(r)["ok"]
            if HAS_WM:
                assert pointer() == (10, 10), "click reported ok but the pointer never moved"

            # observe returns [json, image] in one call; settle_ms is clamped AND flagged
            r = await s.call_tool("click", {"x": 10, "y": 10, "instance": DISP,
                                            "observe": "screenshot", "settle_ms": 6000})
            p = payload(r)
            assert not r.is_error and p["observed"] == "screenshot"
            assert p["settled_ms"] == 5000 and p["settle_clamped"] is True
            assert len(r.content) == 2 and r.content[1].type == "image"

            # vision=none parse is an informative note, not an error
            r = await s.call_tool("parse_screen", {"instance": DISP})
            p = payload(r)
            assert not r.is_error and p["ok"] and p["vision"] == "none" and "note" in p

            # every lie the old server told must now be an isError with a named fix
            r = await s.call_tool("click", {"x": 1, "y": 1, "instance": DEAD})
            assert r.is_error and payload(r)["ok"] is False and "fix" in payload(r)

            for bad in ({"x": 9999, "y": 5}, {"x": -3, "y": 5}, {"x": 5, "y": 768}):
                r = await s.call_tool("click", {**bad, "instance": DISP})
                assert r.is_error and "outside" in payload(r)["error"], bad

            r = await s.call_tool("click_element", {"id": 0, "instance": DISP})
            assert r.is_error and "parse_screen" in payload(r)["fix"]

            r = await s.call_tool("close_instance", {"instance": DEAD})
            assert r.is_error and payload(r)["ok"] is False

            r = await s.call_tool("close_instance", {"instance": 1})
            assert r.is_error  # instance 1 is protected

            r = await s.call_tool("scroll", {"amount": 0, "instance": DISP})
            assert r.is_error  # zero scroll is a no-op, not a success

            r = await s.call_tool("press_keys", {"keys": "NoSuchKey", "instance": DISP})
            assert r.is_error  # xdotool silently ignores bad keysyms; the server must not

            r = await s.call_tool("press_keys", {"keys": "-h", "instance": DISP})
            assert r.is_error and "nothing was pressed" in payload(r)["error"]

            r = await s.call_tool("press_keys", {"keys": "ctrl+a Delete", "instance": DISP})
            assert not r.is_error and payload(r)["keys"] == ["ctrl+a", "Delete"]

            r = await s.call_tool("click", {"x": 1, "y": 1, "instance": DISP, "observe": "bogus"})
            assert r.is_error and "not performed" in payload(r)["error"]

            r = await s.call_tool("upload_file", {"filepath": "/does/not/exist", "instance": DISP})
            assert r.is_error and "not found" in payload(r)["error"]

            r = await s.call_tool("wait_ms", {"ms": 20000})
            p = payload(r)
            assert p["clamped"] is True and p["waited_ms"] == 10000

            r = await s.call_tool("drag", {"x1": 10, "y1": 10, "x2": 100, "y2": 100, "instance": DISP, "steps": 4})
            assert not r.is_error and payload(r)["steps"] == 4
            if HAS_WM:
                assert pointer() == (100, 100)

            r = await s.call_tool("status", {})
            p = payload(r)
            assert p["ok"] and p["vision"]["effective"] == "none" and "deps" in p
            assert "openbox" in p["deps"] and "gpu" in p["vision"] and "omniparser" in p["vision"]
    asyncio.run(run())


def test_screenshot_region_scale_format(x99):
    async def run():
        async with glovebox() as (s, _):
            r = await s.call_tool("screenshot", {"instance": DISP, "region": "0,0,200,100",
                                                 "scale": 0.5, "format": "jpeg"})
            p = payload(r)
            assert not r.is_error and p["region"] == [0, 0, 200, 100] and p["image_size"] == [100, 50]
            assert p["format"] == "jpeg" and r.content[1].mime_type == "image/jpeg"

            r = await s.call_tool("screenshot", {"instance": DISP, "region": "900,0,200,100"})
            assert r.is_error and "exceeds" in payload(r)["error"]

            r = await s.call_tool("screenshot", {"instance": DISP, "format": "gif"})
            assert r.is_error
    asyncio.run(run())


def test_wait_for_stable_on_static_screen(x99):
    async def run():
        async with glovebox() as (s, _):
            r = await s.call_tool("wait_for_stable", {"instance": DISP, "timeout_ms": 3000, "interval_ms": 150})
            p = payload(r)
            assert not r.is_error and p["stable"] is True and p["timed_out"] is False and p["frames"] >= 2

            r = await s.call_tool("wait_for_stable", {"instance": DEAD})
            assert r.is_error
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("xclip"), reason="xclip not installed")
def test_clipboard_roundtrip_and_type_text_unicode(x99):
    async def run():
        async with glovebox() as (s, _):
            text = "Nadja Kovačič — čšž"
            r = await s.call_tool("clipboard", {"action": "set", "text": text, "instance": DISP})
            assert not r.is_error and payload(r)["length"] == len(text)
            r = await s.call_tool("clipboard", {"action": "get", "instance": DISP})
            p = payload(r)
            assert p["text"] == text and p["empty"] is False

            r = await s.call_tool("clipboard", {"action": "paste", "instance": DISP})
            assert r.is_error

            r = await s.call_tool("type_text", {"text": "plain ascii", "instance": DISP})
            assert payload(r)["method"] == "xdotool"

            text2 = "Črnomelj — šž"
            r = await s.call_tool("type_text", {"text": text2, "instance": DISP})
            assert payload(r)["method"] == "clipboard"
            # the previous clipboard text is restored after the paste (so agent text does not leak
            # to the host clipboard through the bridge); the paste itself is what ctrl+v received
            out = subprocess.run(["xclip", "-o", "-selection", "clipboard"], env=XENV, capture_output=True, timeout=5)
            assert out.stdout.decode("utf-8") == text
    asyncio.run(run())


def test_window_info_and_app_log(x99, xmessage_window):
    async def run():
        async with glovebox() as (s, _):
            r = await s.call_tool("window_info", {"instance": DISP})
            p = payload(r)
            assert not r.is_error and any(w["title"] == "xmessage" for w in p["windows"])
            if HAS_WM:
                assert p["active"] and p["active"]["title"] == "xmessage" and p["active"]["width"] > 0

            r = await s.call_tool("app_log", {"instance": DEAD})
            assert r.is_error and "no app log" in payload(r)["error"]

            r = await s.call_tool("open_file", {"filepath": "/etc/hostname", "instance": DISP, "app": "no-such-app"})
            assert r.is_error and "command not found" in payload(r)["error"]   # named, even behind dbus-run-session

            if HAS_XMESSAGE:
                r = await s.call_tool("open_file", {"filepath": "/etc/hostname", "instance": DISP, "app": "xmessage"})
                p = payload(r)
                assert not r.is_error and p["app"] == "xmessage" and p["pid"] > 0
                r = await s.call_tool("app_log", {"instance": DISP})
                assert not r.is_error and "lines" in payload(r)
                subprocess.run(["pkill", "-f", "^xmessage /etc/hostname"], capture_output=True)
    asyncio.run(run())


def test_list_files_entries(x99):
    d = os.path.join(REPO, "files", str(DISP))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "done.txt"), "w") as fh:
        fh.write("x" * 10)
    with open(os.path.join(d, "big.zip.crdownload"), "w") as fh:
        fh.write("y")
    try:
        async def run():
            async with glovebox() as (s, _):
                r = await s.call_tool("list_files", {"instance": DISP})
                p = payload(r)
                by = {f["name"]: f for f in p["files"]}
                assert p["count"] == 2 and by["done.txt"]["bytes"] == 10 and by["done.txt"]["partial"] is False
                assert by["big.zip.crdownload"]["partial"] is True
        asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.mark.skipif(not (shutil.which("tesseract") and HAS_XMESSAGE), reason="tesseract or xmessage not installed")
def test_basic_vision_groups_lines(x99, xmessage_window):
    pytest.importorskip("pytesseract")

    async def run():
        async with glovebox({"GLOVEBOX_VISION": "basic"}) as (s, _):
            r = await s.call_tool("parse_screen", {"instance": DISP})
            p = payload(r)
            assert not r.is_error and p["vision"] == "basic" and p["granularity"] == "line"
            labels = [e["label"] for e in p["elements"]]
            merged = [l for l in labels if "in to continue" in l]
            if not merged:
                _ocr_diagnostics()                  # what did the runner actually show? (CI artifact)
            # three words merged into ONE line target proves the grouping; tesseract's reading of
            # the first glyphs of this bitmap font varies ("Sign"/"gn"), so don't assert on them
            assert len(merged) == 1, labels
            assert all("confidence" in e for e in p["elements"])
            r = await s.call_tool("click_element", {"id": 0, "instance": DISP})
            assert not r.is_error
            r = await s.call_tool("parse_screen", {"instance": DISP, "granularity": "bogus"})
            assert r.is_error
    asyncio.run(run())


def _ocr_diagnostics():
    """Dump what the X server shows + what tesseract sees, so a CI failure is debuggable from the
    artifact instead of guessed at (fonts, rendering and OCR all differ between Xvfb and Xephyr)."""
    import tempfile
    import mss
    import pytesseract
    from PIL import Image
    from pytesseract import Output
    out = os.path.join(tempfile.gettempdir(), f"glovebox-{os.getuid()}")
    os.makedirs(out, exist_ok=True)
    with (getattr(mss, "MSS", None) or mss.mss)(display=f":{DISP}") as m:
        shot = m.grab(m.monitors[1])
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.save(os.path.join(out, "diag_frame.png"))
    print("xlsfonts 10x20:", subprocess.run(["xlsfonts", "-fn", "10x20"], env=XENV, capture_output=True, text=True))
    print("windows:", subprocess.run(["xdotool", "search", "--name", ".*", "getwindowname", "%@"], env=XENV,
                                     capture_output=True, text=True).stdout)
    print("frame colors:", img.getcolors(8) or "more than 8 colors")
    for scale in (1, 2):
        im = img.resize((img.width * scale, img.height * scale)) if scale > 1 else img
        d = pytesseract.image_to_data(im, lang="eng", output_type=Output.DICT)
        words = [(t, c) for t, c in zip(d["text"], d["conf"]) if t.strip()]
        print(f"tesseract words @ {scale}x:", words[:30])


def test_explicit_local_without_omniparser_is_an_honest_error(x99):
    """GLOVEBOX_VISION=local on a box without OmniParser must fail with the fix, never a bare
    'No module named util' (what 0.2 returned) and never a silent fallback."""
    if os.path.isdir(os.path.join(REPO, "OmniParser")):
        pytest.skip("OmniParser is installed here")

    async def run():
        async with glovebox({"GLOVEBOX_VISION": "local"}) as (s, _):
            r = await s.call_tool("parse_screen", {"instance": DISP})
            p = payload(r)
            assert r.is_error and p["ok"] is False and "install.sh local" in p["fix"]
            r = await s.call_tool("status", {})
            v = payload(r)["vision"]
            assert v["requested"] == "local" and v["ready"] is False and v["omniparser"]["code"] is False
    asyncio.run(run())


@pytest.mark.skipif(not (shutil.which("Xephyr") and HAS_XMESSAGE), reason="Xephyr or xmessage not installed")
def test_launch_app_lifecycle(x99):
    """launch_app nests a Xephyr inside :99 (so this works headless in CI), then close it."""
    async def run():
        async with glovebox({"GLOVEBOX_HOST_DISPLAY": f":{DISP}"}) as (s, _):
            r = await s.call_tool("launch_app", {"command": "no-such-binary-xyz"})
            assert r.is_error and payload(r)["ok"] is False

            r = await s.call_tool("launch_app",
                                  {"command": "xmessage -center glovebox-e2e", "size": "640x480"})
            p = payload(r)
            assert not r.is_error and p["ok"]
            n = p["instance"]
            assert n != 1 and p["width"] == 640 and isinstance(p["warnings"], list)
            if HAS_WM:
                assert p["warnings"] == []

            r = await s.call_tool("get_screen_size", {"instance": n})
            assert payload(r)["width"] == 640

            r = await s.call_tool("list_instances", {})
            row = [i for i in payload(r)["instances"] if i["instance"] == n][0]
            assert row["up"] and row["launched_here"] is True

            r = await s.call_tool("app_log", {"instance": n})
            assert not r.is_error

            r = await s.call_tool("close_instance", {"instance": n})
            assert not r.is_error and payload(r)["ok"]

            r = await s.call_tool("close_instance", {"instance": n})
            assert r.is_error  # closing twice must not pretend success
    asyncio.run(run())


@pytest.mark.skipif(not (shutil.which("Xephyr") and HAS_XMESSAGE and shutil.which("xkbcomp")
                         and shutil.which("setxkbmap") and shutil.which("xmodmap")),
                    reason="Xephyr/xmessage/x11-xkb-utils/xmodmap not installed")
def test_launch_app_bridges_keyboard_and_clipboard(x99):
    """A launched instance must carry the HOST keymap (Xephyr alone files AltGr under Mod1, so
    AltGr+V is Alt+v and no '@' on Slovenian/German/... layouts) and mirror the host clipboard."""
    import host_bridge
    host = f":{DISP}"
    subprocess.run(["setxkbmap", "-display", host, "-layout", "si"], check=True, timeout=10)
    try:
        # the bug, reproduced on a bare Xephyr: AltGr (keycode 108 = <RALT>) lands in Mod1
        bare = subprocess.Popen(["Xephyr", ":97", "-screen", "320x200", "-ac"], env=XENV,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                if _display_up(":97"):
                    break
                time.sleep(0.25)
            pm = subprocess.run(["xmodmap", "-display", ":97", "-pm"], capture_output=True, text=True, timeout=10).stdout
            assert re.search(r"mod1.*\(0x6c\)", pm), pm
        finally:
            bare.terminate()
            bare.wait(timeout=5)

        async def run():
            async with glovebox({"GLOVEBOX_HOST_DISPLAY": host}) as (s, _):
                r = await s.call_tool("launch_app", {"command": "xmessage -center bridge-e2e", "size": "640x480"})
                p = payload(r)
                assert not r.is_error, p
                n, disp = p["instance"], p["display"]
                try:
                    assert p["keyboard"]["method"] == "host-keymap", p["keyboard"]
                    pm = subprocess.run(["xmodmap", "-display", disp, "-pm"], capture_output=True, text=True, timeout=10).stdout
                    assert not re.search(r"mod1.*\(0x6c\)", pm), pm          # AltGr is no longer plain Alt
                    pke = subprocess.run(["xmodmap", "-display", disp, "-pke"], capture_output=True, text=True, timeout=10).stdout
                    assert re.search(r"^keycode +108 = ISO_Level3_Shift", pke, re.M), pke

                    assert p["clipboard_sync"] is True and host_bridge.running(n)
                    r = await s.call_tool("list_instances", {})
                    assert [i for i in payload(r)["instances"] if i["instance"] == n][0]["clipboard_sync"] is True

                    def host_clip():
                        return subprocess.run(["xclip", "-o", "-selection", "clipboard"], env=XENV,
                                              capture_output=True, timeout=5).stdout.decode("utf-8", "replace")
                    subprocess.run(["xclip", "-i", "-selection", "clipboard"], env=XENV, input="from host čž".encode(),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    for _ in range(40):                                        # host -> instance
                        r = await s.call_tool("clipboard", {"action": "get", "instance": n})
                        if payload(r)["text"] == "from host čž":
                            break
                        await asyncio.sleep(0.2)
                    assert payload(r)["text"] == "from host čž", payload(r)
                    await s.call_tool("clipboard", {"action": "set", "text": "from instance", "instance": n})
                    for _ in range(40):                                        # instance -> host
                        if host_clip() == "from instance":
                            break
                        await asyncio.sleep(0.2)
                    assert host_clip() == "from instance"
                finally:
                    r = await s.call_tool("close_instance", {"instance": n})
                for _ in range(100):                                           # the bridge exits with the display (<= ~4 s)
                    if not host_bridge.running(n):
                        break
                    await asyncio.sleep(0.2)
                assert not host_bridge.running(n)
        asyncio.run(run())
    finally:
        subprocess.run(["setxkbmap", "-display", host, "-layout", "us"], timeout=10)


@pytest.mark.skipif(not (shutil.which("Xephyr") and HAS_XMESSAGE), reason="Xephyr or xmessage not installed")
def test_cleanup_on_exit_closes_launched_instances(x99):
    async def run():
        async with glovebox({"GLOVEBOX_HOST_DISPLAY": f":{DISP}", "GLOVEBOX_CLEANUP_ON_EXIT": "1"}) as (s, _):
            r = await s.call_tool("launch_app", {"command": "xmessage -center bye", "size": "320x240"})
            return payload(r)["instance"]
    n = asyncio.run(run())
    for _ in range(20):
        if not _display_up(f":{n}"):
            break
        time.sleep(0.25)
    assert not _display_up(f":{n}"), "instance survived server exit despite GLOVEBOX_CLEANUP_ON_EXIT=1"


def test_concurrent_calls_overlap_and_launches_do_not_collide(x99):
    """MCP SDK 2.x runs sync tools on a worker thread, so calls on one server can overlap.
    Document that (two 1.5 s waits finish in well under 3 s) and prove the allocation lock holds:
    two simultaneous launch_app calls must get two different displays."""
    async def run():
        async with glovebox({"GLOVEBOX_HOST_DISPLAY": f":{DISP}"}) as (s, _):
            t0 = time.time()
            a, b = await asyncio.gather(s.call_tool("wait_ms", {"ms": 1500}), s.call_tool("wait_ms", {"ms": 1500}))
            elapsed = time.time() - t0
            assert payload(a)["waited_ms"] == 1500 and payload(b)["waited_ms"] == 1500
            assert elapsed < 2.6, f"calls were serialized ({elapsed:.1f}s) — 2.x should overlap them"

            # :99 is the HOST display in this session, so it must not be driven; overlap two
            # read-only calls instead
            r1, r2 = await asyncio.gather(s.call_tool("status", {}), s.call_tool("list_instances", {}))
            assert not r1.is_error and not r2.is_error

            if not (shutil.which("Xephyr") and HAS_XMESSAGE):
                return
            l1, l2 = await asyncio.gather(
                s.call_tool("launch_app", {"command": "xmessage -center one", "size": "320x240"}),
                s.call_tool("launch_app", {"command": "xmessage -center two", "size": "320x240"}))
            p1, p2 = payload(l1), payload(l2)
            assert p1["ok"] and p2["ok"]
            assert p1["instance"] != p2["instance"], (p1, p2)
            for n in (p1["instance"], p2["instance"]):
                r = await s.call_tool("close_instance", {"instance": n})
                assert not r.is_error, payload(r)
    asyncio.run(run())


def test_hardening_and_new_tools(x99, xmessage_window):
    """The review round: host display refused, scroll clamp reported, pre-validated keys, typed
    click options, start_app, manage_window, find_elements/wait_for_text honesty in 'none' mode,
    browser() without a browser, close_instance(all_launched) with nothing launched."""
    async def run():
        async with glovebox({"GLOVEBOX_HOST_DISPLAY": ":0"}) as (s, _):
            # instance 0 = the host display: refused before anything happens
            for tool, args in (("screenshot", {}), ("click", {"x": 1, "y": 1}), ("press_keys", {"keys": "Return"})):
                r = await s.call_tool(tool, {**args, "instance": 0})
                assert r.is_error and "range" in payload(r)["error"] or "host" in payload(r)["error"], tool

            r = await s.call_tool("scroll", {"amount": 500, "instance": DISP, "x": 50, "y": 50})
            p = payload(r)
            assert not r.is_error and p["clamped"] is True and p["applied"] == 100
            r = await s.call_tool("scroll", {"amount": -2, "instance": DISP, "axis": "sideways"})
            assert r.is_error and "axis" in payload(r)["error"]
            r = await s.call_tool("scroll", {"amount": 2, "instance": DISP, "axis": "horizontal"})
            assert not r.is_error and payload(r)["axis"] == "horizontal"

            # an invalid token anywhere in a sequence: nothing pressed, named
            r = await s.call_tool("press_keys", {"keys": "ctrl+a NoSuchKeyXYZ", "instance": DISP})
            assert r.is_error and "nothing was pressed" in payload(r)["error"]
            r = await s.call_tool("press_keys", {"keys": "ctrl+", "instance": DISP})
            assert r.is_error

            r = await s.call_tool("click", {"x": 20, "y": 20, "instance": DISP, "count": 2, "modifiers": "ctrl"})
            p = payload(r)
            assert not r.is_error and p["count"] == 2 and p["modifiers"] == "ctrl"
            if HAS_WM:
                assert pointer() == (20, 20)
            r = await s.call_tool("click", {"x": 20, "y": 20, "instance": DISP, "modifiers": "hyperdrive"})
            assert r.is_error and "nothing was done" in payload(r)["error"]

            r = await s.call_tool("type_text", {"text": "abc", "instance": DISP, "clear": True, "submit": True})
            p = payload(r)
            assert not r.is_error and p["cleared"] is True and p["submitted"] is True

            # start_app: a second xmessage on the same display, visible in window_info
            r = await s.call_tool("window_info", {"instance": DISP})
            before = payload(r)["count"]
            assert payload(r)["cursor"] is not None
            r = await s.call_tool("start_app", {"command": "xmessage -center second-window", "instance": DISP})
            assert not r.is_error and payload(r)["pid"] > 0
            for _ in range(20):
                await asyncio.sleep(0.25)
                r = await s.call_tool("window_info", {"instance": DISP})
                if payload(r)["count"] > before:
                    break
            assert payload(r)["count"] > before
            r = await s.call_tool("start_app", {"command": "no-such-binary-abc", "instance": DISP})
            assert r.is_error and "not found" in payload(r)["error"]
            r = await s.call_tool("start_app", {"command": "xmessage 'unbalanced", "instance": DISP})
            assert r.is_error and "nothing was started" in payload(r)["error"]

            # manage_window on the active window
            r = await s.call_tool("window_info", {"instance": DISP})
            active = payload(r)["active"]
            if active and HAS_WM:
                r = await s.call_tool("manage_window", {"action": "move", "instance": DISP, "window_id": active["id"],
                                                        "x": 30, "y": 40})
                assert not r.is_error and payload(r)["window"] == active["id"]
                r = await s.call_tool("manage_window", {"action": "resize", "instance": DISP, "window_id": active["id"],
                                                        "width": 300, "height": 200, "observe": "screenshot"})
                assert not r.is_error and payload(r)["observed"] == "screenshot"
                r = await s.call_tool("manage_window", {"action": "activate", "instance": DISP})
                assert not r.is_error
            r = await s.call_tool("manage_window", {"action": "resize", "instance": DISP, "window_id": 1})
            assert r.is_error and "unknown window" in payload(r)["error"]
            r = await s.call_tool("manage_window", {"action": "explode", "instance": DISP})
            assert r.is_error

            # vision 'none': find/wait fail with the fix instead of pretending
            r = await s.call_tool("find_elements", {"query": "ok", "instance": DISP})
            assert r.is_error and "screenshot" in payload(r)["fix"]
            r = await s.call_tool("wait_for_text", {"text": "ok", "instance": DISP, "timeout_ms": 500})
            assert r.is_error and "wait_for_stable" in payload(r)["fix"]

            r = await s.call_tool("browser", {"action": "info", "instance": DISP})
            assert r.is_error and "Chromium" in payload(r)["fix"]
            r = await s.call_tool("browser", {"action": "navigate", "url": "not a url", "instance": DISP})
            assert r.is_error and "nothing was navigated" in payload(r)["error"]

            r = await s.call_tool("close_instance", {"all_launched": True})
            assert not r.is_error and payload(r)["closed"] == []

            r = await s.call_tool("list_instances", {})
            rows = payload(r)["instances"]
            assert all("title" in row and "clipboard_sync" in row for row in rows)
    asyncio.run(run())


GTK_FORM = """
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk
w = Gtk.Window(title="form"); w.set_default_size(500, 220)
b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10); w.add(b)
e = Gtk.Entry(); b.pack_start(e, False, False, 10)
p = Gtk.Entry(); p.set_visibility(False); b.pack_start(p, False, False, 10)
b.pack_start(Gtk.Label(label="just a label"), True, True, 10)
w.connect("destroy", Gtk.main_quit); w.show_all(); Gtk.main()
"""


def _system_gtk_atspi():
    py = "/usr/bin/python3"
    if not os.path.exists(py):
        return None
    r = subprocess.run([py, "-c", "import gi; gi.require_version('Gtk','3.0'); gi.require_version('Atspi','2.0')"],
                       capture_output=True)
    return py if r.returncode == 0 else None


@pytest.mark.skipif(not (shutil.which("Xephyr") and shutil.which("xprop") and shutil.which("dbus-run-session")
                         and _system_gtk_atspi()), reason="Xephyr, xprop, dbus-run-session or system PyGObject (Gtk+Atspi) missing")
def test_fill_checks_the_focus_of_a_native_app(x99, tmp_path):
    """fill() on a real GTK window: the entry is filled and verified; a click on a label (the
    focus stays in the entry) and an unannounced password field fail with nothing typed."""
    script = tmp_path / "form.py"
    script.write_text(GTK_FORM)

    async def run():
        async with glovebox({"GLOVEBOX_HOST_DISPLAY": f":{DISP}"}) as (s, _):
            p = payload(await s.call_tool("launch_app", {"command": f"/usr/bin/python3 {script}", "size": "700x400"}))
            n = p["instance"]
            try:
                win = None
                for _i in range(40):
                    w = payload(await s.call_tool("window_info", {"instance": n}))
                    if w.get("active") and w["active"]["title"] == "form":
                        win = w["active"]
                        break
                    await asyncio.sleep(0.5)
                assert win, "the GTK window never appeared"
                await asyncio.sleep(1.0)
                cx = win["x"] + win["width"] // 2
                r = await s.call_tool("fill", {"text": "abc", "x": cx, "y": win["y"] + 27, "instance": n})
                p = payload(r)
                if p.get("focus_checked") is False:
                    pytest.skip("no AT-SPI bus in this environment: " + p.get("focus_unchecked_reason", ""))
                assert not r.is_error and p["focus_checked"] == "at-spi" and p["focus"]["role"] == "text"
                if shutil.which("xclip"):
                    assert p["verified"] is True and p["read_back"] == "clipboard"
                r = await s.call_tool("fill", {"text": "oops", "x": cx, "y": win["y"] + win["height"] - 40, "instance": n})
                p = payload(r)
                assert r.is_error and "nothing was typed" in p["error"] and p["focus"]["role"] == "text"
                r = await s.call_tool("fill", {"text": "secret", "x": cx, "y": win["y"] + 71, "instance": n})
                p = payload(r)
                assert r.is_error and "password field" in p["error"] and "allow_password" in p["fix"]
                r = await s.call_tool("fill", {"text": "secret", "x": cx, "y": win["y"] + 71, "instance": n,
                                               "allow_password": True})
                p = payload(r)
                assert not r.is_error and p["focus"]["role"] == "password text" and p["verified"] is None
                # clicking the same spot again must not hang (mousemove --sync with nothing to move)
                t0 = time.time()
                r = await s.call_tool("click", {"x": cx, "y": win["y"] + 71, "instance": n})
                assert not r.is_error and time.time() - t0 < 5
            finally:
                await s.call_tool("close_instance", {"instance": n})
    asyncio.run(run())
