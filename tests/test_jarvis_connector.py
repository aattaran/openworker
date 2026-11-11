"""The Jarvis connector's pinned MCP catalog: exactly Jarvis's 58 tools, each
classified read/write, the MCP tunnel off by default, and the long-running
tools on a 900 s timeout. Sets are copied from the design spec."""

from __future__ import annotations

from coworker.connectors.descriptors import MCP_TOKEN_AUTH, get_descriptor
from coworker.connectors.tool_defs import mcp_pinned_tools, mcp_tool_defs

# search_code, capture_screenshot, read_ui_elements and review_pr only read, but
# Jarvis interpolates their arguments into shell commands unquoted, so they can run
# arbitrary commands: they ask first, like run_command, until Jarvis quotes them.
SHELL_INTERPOLATING = {"search_code", "capture_screenshot", "read_ui_elements", "review_pr"}
READ = {
    "read_file", "list_directory", "list_remote_controls",
    "check_delegation", "query_memory", "list_windows",
    "read_clipboard", "list_processes", "recall",
    "security_audit", "context_snapshot", "read_handoff", "knowledge_recall",
    "jarvis_status", "plan_read", "plan_agents", "analyze_traces",
    "get_shared_context",
}
WRITE = SHELL_INTERPOLATING | {
    "write_file", "run_command", "verify_work", "start_remote_control",
    "stop_remote_control", "delegate_to_claude", "spawn_agents", "configure_fleet",
    "launch_fleet", "send_message", "click_element", "focus_window", "send_keystroke",
    "run_in_terminal", "scroll_window", "open_file_in_ide", "click_at", "type_text",
    "manage_window", "write_clipboard", "launch_app", "kill_process", "remember",
    "write_handoff", "think_hard", "web_search", "broadcast_status", "knowledge_store",
    "run_agents", "session_start", "session_end", "plan_write", "share_context",
    "upload_to_drive", "mcp_connect", "mcp_call",
}
LONG = {
    "delegate_to_claude", "think_hard", "verify_work", "spawn_agents",
    "launch_fleet", "run_agents",
}
TUNNEL = {"mcp_connect", "mcp_call"}


def _defs():
    return {d.name.removeprefix("mcp__jarvis__"): d for d in mcp_tool_defs("jarvis")}


def test_jarvis_pins_exactly_58():
    assert len(READ) == 18 and len(WRITE) == 40 and not READ & WRITE
    assert set(mcp_pinned_tools("jarvis")) == READ | WRITE
    assert len(mcp_pinned_tools("jarvis")) == 58


def test_jarvis_kinds():
    defs = _defs()
    assert {n for n, d in defs.items() if d.kind == "read"} == READ
    assert {n for n, d in defs.items() if d.kind == "write"} == WRITE
    assert defs["verify_work"].kind == "write"
    for name in SHELL_INTERPOLATING:
        assert defs[name].kind == "write", name


def test_jarvis_tunnel_default_off():
    defs = _defs()
    assert {n for n, d in defs.items() if not d.default_enabled} == TUNNEL


def test_jarvis_timeouts():
    defs = _defs()
    for name in READ | WRITE:
        expected = 900.0 if name in LONG else None
        assert defs[name].timeout == expected, name


def test_jarvis_descriptor():
    d = get_descriptor("jarvis")
    assert d is not None
    assert d.mcp_backed is True
    assert d.auth == MCP_TOKEN_AUTH
    assert d.mcp_url == ""
    assert [f.key for f in d.fields] == ["url", "token"]
    assert d.fields[1].secret is True
    assert d.validate is not None
