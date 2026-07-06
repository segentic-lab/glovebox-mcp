"""Display-gated e2e: a real stdio round-trip against a real X server on :99.

The fixture prefers Xvfb (what CI has); locally it falls back to Xephyr on the host display.
Everything self-skips when no X server can be started, so the smoke tests still run anywhere.
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import pytest

from conftest import REPO

DISP = 99          # the test display / instance number (far from the :1.. _SCAN_MAX user range)
DEAD = 98          # never started — for dead-instance error paths

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") or shutil.which("Xephyr")) or not shutil.which("xdpyinfo"),
    reason="no X server (Xvfb/Xephyr) or xdpyinfo available",
)


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
    yield f":{DISP}"
    proc.terminate()
    proc.wait(timeout=5)


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


def test_handshake_reports_project_version(x99):
    from _version import __version__

    async def run():
        async with glovebox() as (s, init):
            assert init.serverInfo.name == "glovebox"
            assert init.serverInfo.version == __version__
            tools = await s.list_tools()
            assert len(tools.tools) == 19
    asyncio.run(run())


def test_control_roundtrip_and_error_paths(x99):
    async def run():
        async with glovebox() as (s, _):
            # happy path against the real display
            r = await s.call_tool("get_screen_size", {"instance": DISP})
            p = payload(r)
            assert not r.isError and p["ok"] and (p["width"], p["height"]) == (1024, 768)
            assert p["vision"] == "none"

            r = await s.call_tool("screenshot", {"instance": DISP})
            assert not r.isError and r.content[0].type == "image"

            r = await s.call_tool("click", {"x": 10, "y": 10, "instance": DISP})
            assert not r.isError and payload(r)["ok"]

            # observe returns [json, image] in one call
            r = await s.call_tool("click", {"x": 10, "y": 10, "instance": DISP,
                                            "observe": "screenshot", "settle_ms": 100})
            assert not r.isError and payload(r)["observed"] == "screenshot"
            assert len(r.content) == 2 and r.content[1].type == "image"

            # vision=none parse is an informative note, not an error
            r = await s.call_tool("parse_screen", {"instance": DISP})
            p = payload(r)
            assert not r.isError and p["ok"] and p["vision"] == "none" and "note" in p

            # every lie the old server told must now be an isError with a named fix
            r = await s.call_tool("click", {"x": 1, "y": 1, "instance": DEAD})
            assert r.isError and payload(r)["ok"] is False and "fix" in payload(r)

            r = await s.call_tool("click_element", {"id": 0, "instance": DISP})
            assert r.isError and "parse_screen" in payload(r)["fix"]

            r = await s.call_tool("close_instance", {"instance": DEAD})
            assert r.isError and payload(r)["ok"] is False

            r = await s.call_tool("close_instance", {"instance": 1})
            assert r.isError  # instance 1 is protected

            r = await s.call_tool("scroll", {"amount": 0, "instance": DISP})
            assert r.isError  # zero scroll is a no-op, not a success

            r = await s.call_tool("press_keys", {"keys": "NoSuchKey", "instance": DISP})
            assert r.isError  # xdotool silently ignores bad keysyms; the server must not

            r = await s.call_tool("click", {"x": 1, "y": 1, "instance": DISP, "observe": "bogus"})
            assert r.isError and "not performed" in payload(r)["error"]

            r = await s.call_tool("upload_file", {"filepath": "/does/not/exist", "instance": DISP})
            assert r.isError and "not found" in payload(r)["error"]

            r = await s.call_tool("wait_ms", {"ms": 20000})
            p = payload(r)
            assert p["clamped"] is True and p["waited_ms"] == 10000

            r = await s.call_tool("status", {})
            p = payload(r)
            assert p["ok"] and p["vision"] == "none" and "deps" in p
    asyncio.run(run())


@pytest.mark.skipif(not shutil.which("xclip"), reason="xclip not installed")
def test_type_text_unicode_goes_via_clipboard(x99):
    async def run():
        async with glovebox() as (s, _):
            r = await s.call_tool("type_text", {"text": "plain ascii", "instance": DISP})
            assert payload(r)["method"] == "xdotool"

            text = "Nadja Kovačič — čšž"
            r = await s.call_tool("type_text", {"text": text, "instance": DISP})
            assert payload(r)["method"] == "clipboard"
            # the clipboard on :99 must hold the exact unicode text (this is what ctrl+v pastes)
            out = subprocess.run(["xclip", "-o", "-selection", "clipboard"],
                                 env={**os.environ, "DISPLAY": f":{DISP}"},
                                 capture_output=True, timeout=5)
            assert out.stdout.decode("utf-8") == text
    asyncio.run(run())


@pytest.mark.skipif(not (shutil.which("Xephyr") and shutil.which("xmessage")),
                    reason="Xephyr or xmessage not installed")
def test_launch_app_lifecycle(x99):
    """launch_app nests a Xephyr inside :99 (so this works headless in CI), then close it."""
    async def run():
        async with glovebox({"GLOVEBOX_HOST_DISPLAY": f":{DISP}"}) as (s, _):
            r = await s.call_tool("launch_app", {"command": "no-such-binary-xyz"})
            assert r.isError and payload(r)["ok"] is False

            r = await s.call_tool("launch_app",
                                  {"command": "xmessage -center glovebox-e2e", "size": "640x480"})
            p = payload(r)
            assert not r.isError and p["ok"]
            n = p["instance"]
            assert n != 1

            r = await s.call_tool("get_screen_size", {"instance": n})
            assert payload(r)["width"] == 640

            r = await s.call_tool("close_instance", {"instance": n})
            assert not r.isError and payload(r)["ok"]

            r = await s.call_tool("close_instance", {"instance": n})
            assert r.isError  # closing twice must not pretend success
    asyncio.run(run())
