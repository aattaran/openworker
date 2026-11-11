"""validate_mcp_token: a real MCP `initialize` against a token-guarded server, which
must parse SSE or JSON replies and leave no session behind on the server."""

from __future__ import annotations

from coworker.connectors.descriptors import validate_mcp_token

from tests.mcp_http_server import free_port, mcp_http_server


def _ping() -> str:
    return "pong"


def test_validate_ok_sse():
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        result = validate_mcp_token({"url": srv.url, "token": "t"})
    assert result.ok is True
    assert result.identity.startswith("testsrv v")


def test_validate_ok_json():
    with mcp_http_server(token="t", tools={"ping": _ping}, json_response=True) as srv:
        result = validate_mcp_token({"url": srv.url, "token": "t"})
    assert result.ok is True
    assert result.identity.startswith("testsrv v")


def test_validate_wrong_token():
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        result = validate_mcp_token({"url": srv.url, "token": "nope"})
    assert result.ok is False
    assert result.error == "token rejected"


def test_validate_unreachable():
    url = f"http://127.0.0.1:{free_port()}/mcp"
    result = validate_mcp_token({"url": url, "token": "t"})
    assert result.ok is False
    assert result.error.startswith("can't reach")


def test_validate_missing_mcp_path():
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        base = srv.url.removesuffix("/mcp")
        result = validate_mcp_token({"url": base, "token": "t"})
    assert result.ok is False
    assert "did you mean" in result.error
    assert result.error.endswith("/mcp?")


def test_validate_leaves_no_session():
    with mcp_http_server(token="t", tools={"ping": _ping}) as srv:
        assert validate_mcp_token({"url": srv.url, "token": "t"}).ok
        assert srv.deletes == 1
