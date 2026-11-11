"""Connecting a token-auth MCP connector: validate, store the token ONLY in the
SecretStore, seed mcp.json with url + pinned include_tools + approval on."""

from __future__ import annotations

import pytest

from coworker.connectors.setup import connect_connector, disconnect_connector
from coworker.connectors.tool_defs import ConnectorToolDef
from coworker.mcp.config import global_mcp_path, read_global
from coworker.secrets import SecretStore
from tests.mcp_http_server import mcp_http_server, register_token_connector

SECRET = "s3cr3t-token-value"


def _ping() -> str:
    return "pong"


@pytest.fixture
def tok(monkeypatch):
    register_token_connector(
        monkeypatch, "tok", [ConnectorToolDef("tok", "mcp__tok__ping", "Ping", "read", "Ping.")]
    )
    with mcp_http_server(token=SECRET, tools={"ping": _ping}) as srv:
        yield srv


def test_connect_seeds_config_without_token(tok):
    secrets = SecretStore()
    result = connect_connector(secrets, "tok", {"url": tok.url, "token": SECRET})
    assert result["ok"] is True
    entry = read_global()["tok"]
    assert entry["url"] == tok.url
    assert entry["requires_approval"] is True
    assert entry["include_tools"] == ["ping"]
    raw = global_mcp_path().read_text(encoding="utf-8")
    assert SECRET not in raw
    assert "Authorization" not in raw
    profile = secrets.get("tok:default")
    assert profile["mode"] == "mcp"
    assert profile["token"] == SECRET
    assert profile["url"] == tok.url


def test_connect_failure_writes_nothing(tok):
    secrets = SecretStore()
    result = connect_connector(secrets, "tok", {"url": tok.url, "token": "wrong"})
    assert result["ok"] is False
    assert "tok" not in read_global()
    assert secrets.get("tok:default") is None


def test_connect_strips_whitespace(tok):
    secrets = SecretStore()
    result = connect_connector(secrets, "tok", {"url": f" {tok.url}\n", "token": f"{SECRET}\n"})
    assert result["ok"] is True
    profile = secrets.get("tok:default")
    assert profile["url"] == tok.url
    assert profile["token"] == SECRET
    assert read_global()["tok"]["url"] == tok.url


def test_reconnect_blank_token_keeps_stored(tok):
    secrets = SecretStore()
    assert connect_connector(secrets, "tok", {"url": tok.url, "token": SECRET})["ok"]
    result = connect_connector(secrets, "tok", {"url": tok.url, "token": ""})
    assert result["ok"] is True
    assert secrets.get("tok:default")["token"] == SECRET


def test_disconnect_removes_config_and_profile(tok):
    secrets = SecretStore()
    assert connect_connector(secrets, "tok", {"url": tok.url, "token": SECRET})["ok"]
    disconnect_connector(secrets, "tok")
    assert "tok" not in read_global()
    assert secrets.get("tok:default") is None
