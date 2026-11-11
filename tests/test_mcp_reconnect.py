"""A restarted (or session-expiring) MCP server must not wedge OpenWorker: a call in
flight when its connection dies fails promptly instead of waiting out the tool
timeout, and a session the server has ended is dropped so the next call reconnects."""

from __future__ import annotations

import asyncio

import pytest

from coworker.mcp.client import MCPManager
from coworker.mcp.config import MCPServerDef
from tests.mcp_http_server import mcp_http_server


def _ping() -> str:
    return "pong"


async def _fails_without_hanging(call) -> None:
    """The call must raise on its own; hitting the 10 s guard means it hung."""
    with pytest.raises(Exception) as info:
        await asyncio.wait_for(call, 10)
    assert not isinstance(info.value, asyncio.TimeoutError), "the call hung"


def _server(srv) -> MCPServerDef:
    return MCPServerDef(
        name="srv",
        transport="http",
        url=srv.url,
        headers={"Authorization": f"Bearer {srv.token}"},
    )


@pytest.mark.parametrize("stale_status", [400, 404])
def test_call_after_server_restart_fails_promptly(stale_status):
    with mcp_http_server(token="t", tools={"ping": _ping}, stale_session_status=stale_status) as srv:

        async def scenario():
            mgr = MCPManager()
            try:
                await mgr.ensure(_server(srv))
                assert "pong" in str(await mgr.call("srv", "ping", {}))
                srv.restart()
                await _fails_without_hanging(mgr.call("srv", "ping", {}))
            finally:
                await mgr.aclose()

        asyncio.run(scenario())


@pytest.mark.parametrize("stale_status", [400, 404])
def test_next_call_reconnects_after_server_restart(stale_status):
    with mcp_http_server(token="t", tools={"ping": _ping}, stale_session_status=stale_status) as srv:

        async def scenario():
            mgr = MCPManager()
            try:
                await mgr.ensure(_server(srv))
                await mgr.call("srv", "ping", {})
                srv.restart()
                await _fails_without_hanging(mgr.call("srv", "ping", {}))
                # A later session asks ensure() again: it must not get the dead conn.
                await asyncio.wait_for(mgr.ensure(_server(srv)), 10)
                return await asyncio.wait_for(mgr.call("srv", "ping", {}), 10)
            finally:
                await mgr.aclose()

        assert "pong" in str(asyncio.run(scenario()))
