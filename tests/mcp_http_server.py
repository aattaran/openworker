"""In-process streamable-HTTP MCP server for tests: real protocol, bearer-guarded.

Replies use SSE by default, as a Node `StreamableHTTPServerTransport` does; pass
`json_response=True` to exercise the JSON reply branch instead.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator

import uvicorn
from mcp.server.fastmcp import FastMCP

from coworker.connectors import descriptors as descriptors_mod
from coworker.connectors import tool_defs as tool_defs_mod
from coworker.connectors.descriptors import (
    MCP_TOKEN_AUTH,
    ConnectorDescriptor,
    Field,
    validate_mcp_token,
)
from coworker.connectors.tool_defs import ConnectorToolDef


def register_token_connector(
    monkeypatch, name: str, pins: list[ConnectorToolDef]
) -> ConnectorDescriptor:
    """Register a temporary token-auth MCP connector and its pins for one test."""
    d = ConnectorDescriptor(
        name=name,
        title=name.title(),
        icon="",
        blurb="",
        auth=MCP_TOKEN_AUTH,
        two_way=False,
        fields=[Field("url", "MCP URL"), Field("token", "Token", secret=True)],
        instructions=[],
        validate=validate_mcp_token,
    )
    monkeypatch.setitem(descriptors_mod._BY_NAME, name, d)
    monkeypatch.setitem(tool_defs_mod.TOOLS_BY_CONNECTOR, name, list(pins))
    for p in pins:
        monkeypatch.setitem(tool_defs_mod._KIND_BY_NAME, p.name, p.kind)
        monkeypatch.setitem(tool_defs_mod.TOOL_TO_CONNECTOR, p.name, name)
    return d


@dataclass
class ServerHandle:
    url: str
    token: str
    deletes: int = 0

    def set_token(self, new: str) -> None:
        self.token = new


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def mcp_http_server(
    *,
    token: str,
    tools: dict[str, Callable[..., Any]],
    json_response: bool = False,
    name: str = "testsrv",
) -> Iterator[ServerHandle]:
    mcp = FastMCP(name, json_response=json_response)
    for tool_name, fn in tools.items():
        mcp.add_tool(fn, name=tool_name)
    inner = mcp.streamable_http_app()
    port = free_port()
    handle = ServerHandle(url=f"http://127.0.0.1:{port}/mcp", token=token)

    async def app(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope["headers"]).get(b"authorization")
            if auth != f"Bearer {handle.token}".encode():
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"Unauthorized"}'})
                return
            if scope["method"] == "DELETE":
                handle.deletes += 1
        await inner(scope, receive, send)

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("test MCP server failed to start")
        time.sleep(0.02)
    try:
        yield handle
    finally:
        server.should_exit = True
        thread.join(10)
