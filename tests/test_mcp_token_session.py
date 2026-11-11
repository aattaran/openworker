"""Session start for token-auth MCP connectors: the bearer comes from the SecretStore
(never mcp.json), the per-tool read/write classification gates calls, catalog
timeouts reach the callables, and a bad or missing token fails closed with a
reason on the Connectors page."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from coworker.connectors.setup import connect_connector
from coworker.connectors.tool_defs import ConnectorToolDef
from coworker.mcp.config import load_mcp_servers, put_global_server
from coworker.server import manager as manager_mod
from coworker.server.manager import SessionManager
from tests.mcp_http_server import mcp_http_server, register_token_connector

PINS = [
    ConnectorToolDef("tok", "mcp__tok__ping", "Ping", "read", "Ping."),
    ConnectorToolDef("tok", "mcp__tok__share", "Share", "write", "Share.", timeout=900.0),
]


def _ping() -> str:
    return "pong"


def _share(text: str) -> str:
    return f"stored {text}"


def _fake_tool(name):
    return SimpleNamespace(name=name, description=name, inputSchema={"type": "object"})


def _manager(tmp_path, monkeypatch) -> SessionManager:
    register_token_connector(monkeypatch, "tok", PINS)
    manager = SessionManager(data_dir=tmp_path / "data")
    monkeypatch.setattr(manager, "effective_connectors", lambda sid, agent=None: {"tok"})
    return manager


def _seed(manager, *, token="t", url="http://127.0.0.1:9/mcp"):
    profile = {"type": "token", "enabled": True, "url": url, "mode": "mcp"}
    if token:
        profile["token"] = token
    manager.secrets.put("tok:default", profile)
    put_global_server(
        "tok",
        {
            "url": "http://stale.invalid/mcp",
            "requires_approval": True,
            "include_tools": ["ping", "share"],
            "enabled": True,
        },
    )


def _capture_ensure(manager, monkeypatch, seen):
    async def fake_ensure(server, *, interactive=False):
        seen["server"] = server
        allow = set(server.include_tools or [])
        return SimpleNamespace(tools=[_fake_tool(n) for n in ("ping", "share") if n in allow])

    monkeypatch.setattr(manager.mcp, "ensure", fake_ensure)


def test_prepare_injects_bearer_from_secrets(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _seed(manager, url="http://127.0.0.1:9/mcp")
    seen: dict = {}
    _capture_ensure(manager, monkeypatch, seen)
    asyncio.run(manager.prepare_mcp_tools("s1"))
    assert seen["server"].headers["Authorization"] == "Bearer t"
    assert seen["server"].url == "http://127.0.0.1:9/mcp"


def test_prepare_missing_token_skips_with_error(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _seed(manager, token="")
    seen: dict = {}
    _capture_ensure(manager, monkeypatch, seen)
    assert asyncio.run(manager.prepare_mcp_tools("s1")) == []
    assert "server" not in seen
    assert manager._mcp_errors["tok"].startswith("not connected")


def test_prepare_per_tool_approval_and_category(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _seed(manager)
    _capture_ensure(manager, monkeypatch, {})
    tools = asyncio.run(manager.prepare_mcp_tools("s1"))
    meta = {t.__aisuite_tool_metadata__.name: t.__aisuite_tool_metadata__ for t in tools}
    assert meta["mcp__tok__ping"].requires_approval is False
    assert meta["mcp__tok__share"].requires_approval is True
    assert meta["mcp__tok__ping"].category == "connector"
    assert meta["mcp__tok__share"].category == "connector"


def test_prepare_passes_catalog_timeout(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    _seed(manager)
    _capture_ensure(manager, monkeypatch, {})
    captured: dict = {}
    real = manager_mod.build_callables

    def spy(*args, **kwargs):
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(manager_mod, "build_callables", spy)
    asyncio.run(manager.prepare_mcp_tools("s1"))
    timeout_for = captured["timeout_for"]
    assert timeout_for("mcp__tok__share") == 900.0
    assert timeout_for("mcp__tok__ping") == 120.0
    assert timeout_for("mcp__tok__unpinned") == 120.0


def test_real_path_call_through_mcpmanager(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with mcp_http_server(token="t", tools={"ping": _ping, "share": _share}) as srv:
        assert connect_connector(manager.secrets, "tok", {"url": srv.url, "token": "t"})["ok"]

        async def scenario():
            try:
                tools = await manager.prepare_mcp_tools("s1")
                by_name = {t.__name__: t for t in tools}
                return await asyncio.to_thread(by_name["mcp__tok__ping"])
            finally:
                await manager.mcp.aclose()

        out = asyncio.run(scenario())
    assert "pong" in str(out)


def test_rotated_token_skips_with_error(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        assert connect_connector(manager.secrets, "tok", {"url": srv.url, "token": "t"})["ok"]
        srv.set_token("rotated")

        async def scenario():
            try:
                return await asyncio.wait_for(manager.prepare_mcp_tools("s1"), 30)
            finally:
                await manager.mcp.aclose()

        assert asyncio.run(scenario()) == []
    assert manager._mcp_errors["tok"] == "token rejected — reconnect from Connectors"


def _slow(seconds: float) -> str:
    import time

    time.sleep(seconds)
    return "done"


def test_long_tool_outlives_default_sse_read_timeout(tmp_path, monkeypatch):
    # A tool that legitimately runs past the SDK's SSE read timeout (300 s by default;
    # 1 s here) sends no events while it works, so the stream read times out and the
    # call is never answered. Backed servers must read for as long as their slowest pin.
    from coworker.mcp import client as client_mod

    register_token_connector(
        monkeypatch,
        "tok",
        [ConnectorToolDef("tok", "mcp__tok__slow", "Slow", "read", "Slow.", timeout=8.0)],
    )
    manager = SessionManager(data_dir=tmp_path / "data")
    monkeypatch.setattr(manager, "effective_connectors", lambda sid, agent=None: {"tok"})
    monkeypatch.setattr(client_mod, "_SSE_READ_TIMEOUT", 1.0)
    with mcp_http_server(token="t", tools={"slow": _slow}) as srv:
        assert connect_connector(manager.secrets, "tok", {"url": srv.url, "token": "t"})["ok"]

        async def scenario():
            try:
                tools = {t.__name__: t for t in await manager.prepare_mcp_tools("s1")}
                return await asyncio.to_thread(tools["mcp__tok__slow"], seconds=2.5)
            finally:
                await manager.mcp.aclose()

        assert "done" in str(asyncio.run(scenario()))


def _two_sessions(manager, srv, between):
    """Session s1 makes a call; `between` runs (a restart, a rotation); then s2 starts
    on the SAME long-lived manager, as in the desktop app."""

    async def scenario():
        try:
            s1 = {t.__name__: t for t in await manager.prepare_mcp_tools("s1")}
            await asyncio.to_thread(s1["mcp__tok__ping"])
            between()
            s2 = await asyncio.wait_for(manager.prepare_mcp_tools("s2"), 30)
            s2 = {t.__name__: t for t in s2}
            out = await asyncio.to_thread(s2["mcp__tok__ping"]) if s2 else None
            return s2, out
        finally:
            await manager.mcp.aclose()

    return asyncio.run(scenario())


def test_idle_restart_next_session_reconnects(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with mcp_http_server(token="t", tools={"ping": _ping, "share": _share}) as srv:
        assert connect_connector(manager.secrets, "tok", {"url": srv.url, "token": "t"})["ok"]
        tools, out = _two_sessions(manager, srv, srv.restart)
    assert "mcp__tok__ping" in tools
    assert "pong" in str(out)


def test_rotated_token_while_idle_skips_next_session(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with mcp_http_server(token="t", tools={"ping": _ping, "share": _share}) as srv:
        assert connect_connector(manager.secrets, "tok", {"url": srv.url, "token": "t"})["ok"]
        tools, _ = _two_sessions(manager, srv, lambda: srv.set_token("rotated"))
    assert tools == {}
    assert manager._mcp_errors["tok"] == "token rejected — reconnect from Connectors"


def test_reconnect_drops_the_cached_connection(tmp_path, monkeypatch):
    # Re-pointing a connected token server (e.g. local -> Cloud Run) must not leave
    # later sessions on the old host's live connection.
    manager = _manager(tmp_path, monkeypatch)
    stale = SimpleNamespace(shutdown=asyncio.Event(), tools=[])
    manager.mcp._conns["tok"] = stale
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        assert manager.connect_connector("tok", {"url": srv.url, "token": "t"})["ok"]
    assert "tok" not in manager.mcp._conns
    assert stale.shutdown.is_set()


def test_headless_load_fails_closed(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        assert connect_connector(manager.secrets, "tok", {"url": srv.url, "token": "t"})["ok"]
    (server,) = [s for s in load_mcp_servers(None, secrets=manager.secrets) if s.name == "tok"]
    assert server.headers == {}
    assert server.requires_approval is True
