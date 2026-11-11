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
        monkeypatch.setitem(tool_defs_mod._DEF_BY_NAME, p.name, p)
        monkeypatch.setitem(tool_defs_mod.TOOL_TO_CONNECTOR, p.name, name)
    return d


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class ServerHandle:
    url: str
    token: str
    port: int
    build: Callable[[], Any]
    deletes: int = 0
    _server: Any = None
    _thread: Any = None

    def set_token(self, new: str) -> None:
        self.token = new

    def start(self) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(
                self.build(), host="127.0.0.1", port=self.port, log_level="warning", lifespan="on"
            )
        )
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("test MCP server failed to start")
            time.sleep(0.02)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(10)

    def restart(self) -> None:
        """Same port, fresh MCP app: every session the clients hold is gone."""
        self.stop()
        self.start()


@contextlib.contextmanager
def mcp_http_server(
    *,
    token: str,
    tools: dict[str, Callable[..., Any]],
    json_response: bool = False,
    name: str = "testsrv",
    stale_session_status: int = 404,
) -> Iterator[ServerHandle]:
    """`stale_session_status` replaces the 404 FastMCP gives an unknown session id
    (e.g. 400, as Jarvis's http.js answered before it followed the MCP spec)."""
    port = free_port()
    handle: ServerHandle

    def build():
        mcp = FastMCP(name, json_response=json_response)
        for tool_name, fn in tools.items():
            mcp.add_tool(fn, name=tool_name)
        inner = mcp.streamable_http_app()

        async def app(scope, receive, send):
            if scope["type"] != "http":
                await inner(scope, receive, send)
                return
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

            async def send_with_status(message):
                if message["type"] == "http.response.start" and message["status"] == 404:
                    message = {**message, "status": stale_session_status}
                await send(message)

            await inner(scope, receive, send_with_status)

        return app

    handle = ServerHandle(
        url=f"http://127.0.0.1:{port}/mcp", token=token, port=port, build=build
    )
    handle.start()
    try:
        yield handle
    finally:
        handle.stop()
