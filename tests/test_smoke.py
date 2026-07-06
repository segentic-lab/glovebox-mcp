"""No-display smoke tests: the server imports, registers all tools, and keeps its contract helpers honest."""
import json

import pytest

import server
from _version import __version__
from mcp.server.fastmcp.exceptions import ToolError

EXPECTED_TOOLS = {
    "launch_app", "list_instances", "close_instance", "get_screen_size", "screenshot",
    "parse_screen", "click", "drag", "double_click", "click_element", "type_text",
    "upload_file", "open_file", "list_files", "press_keys", "move_mouse", "scroll",
    "wait_ms", "status",
}


def test_all_tools_registered():
    names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert names == EXPECTED_TOOLS
    assert len(names) == 19


def test_descriptions_are_agent_first():
    """Every tool description must say more than a one-liner (returns shape / when-to-use / caveats)."""
    for t in server.mcp._tool_manager.list_tools():
        assert t.description, f"{t.name} has no description"
        assert len(t.description) > 120, f"{t.name} description too thin: {t.description!r}"


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


def test_vision_mode_resolution(monkeypatch):
    monkeypatch.setenv("GLOVEBOX_VISION", "basic")
    assert server._vision_mode() == "basic"
    monkeypatch.setenv("GLOVEBOX_VISION", "bogus")
    assert server._vision_mode() == "local"   # unknown values fall back to the default


def test_observe_mode_validated_before_acting():
    with pytest.raises(ToolError) as e:
        server._validate_observe("click", 1, "screenshoot")
    payload = json.loads(str(e.value))
    assert payload["ok"] is False and "not performed" in payload["error"]
