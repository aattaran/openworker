"""Operator-run end-to-end checks of the Jarvis connector against a REAL Jarvis MCP
server. Skipped unless JARVIS_E2E_MODE is set, so this is not CI coverage.

local: JARVIS_E2E_JARVIS_DIR points at a Jarvis checkout (use a git worktree, never the
       live checkout: http.js writes and deletes bridge.pid in its own root). The
       fixture launches `node src/mcp/http.js` with a minimal environment (no
       MONGODB_URI, no API keys), a temp cwd (so no .env loads), and a random token.
prod:  JARVIS_E2E_URL + JARVIS_E2E_TOKEN; only tests marked `readonly` run.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets as pysecrets
import shutil
import subprocess
import time

import httpx
import pytest

from coworker.connectors.setup import connect_connector
from coworker.connectors.tool_defs import mcp_pinned_tools
from coworker.risk import RiskClass, classify
from coworker.server.manager import SessionManager
from tests.mcp_http_server import free_port

MODE = os.environ.get("JARVIS_E2E_MODE", "")
pytestmark = pytest.mark.skipif(MODE not in ("local", "prod"), reason="operator-run e2e")
local_only = pytest.mark.skipif(MODE != "local", reason="writes or restarts: local only")

# Paid or agent-spawning tools this suite must never call.
FORBIDDEN = {"think_hard", "delegate_to_claude", "spawn_agents", "launch_fleet", "run_agents"}
_ENV_KEEP = ("PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "APPDATA", "LOCALAPPDATA",
             "USERPROFILE", "HOME", "TEMP", "TMP", "COMSPEC", "PATHEXT")


class _LocalJarvis:
    def __init__(self, jarvis_dir: str, workdir: str) -> None:
        self.dir = jarvis_dir
        self.workdir = workdir
        self.port = free_port()
        self.token = pysecrets.token_hex(16)
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.log = os.path.join(workdir, "jarvis.log")
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
        env["MCP_SECRET_TOKEN"] = self.token
        node = shutil.which("node") or "node"
        with open(self.log, "ab") as log:
            self.proc = subprocess.Popen(
                [node, os.path.join(self.dir, "src", "mcp", "http.js"),
                 "--port", str(self.port), "--project-dir", self.workdir],
                cwd=self.workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 60
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"jarvis exited {self.proc.returncode}; see {self.log}")
            try:
                if httpx.get(f"http://127.0.0.1:{self.port}/", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError as exc:
                logging.warning("jarvis not up yet on :%s: %s", self.port, exc)
                last_error = exc
            time.sleep(0.5)
        raise RuntimeError(f"jarvis did not come up ({last_error}); see {self.log}")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(15)

    def restart(self) -> None:
        self.stop()
        self.start()


@pytest.fixture(scope="module")
def jarvis(tmp_path_factory):
    if MODE == "prod":
        url, token = os.environ["JARVIS_E2E_URL"], os.environ["JARVIS_E2E_TOKEN"]
        yield type("Prod", (), {"url": url, "token": token})()
        return
    server = _LocalJarvis(os.environ["JARVIS_E2E_JARVIS_DIR"], str(tmp_path_factory.mktemp("jarvis")))
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def manager(tmp_path, jarvis):
    m = SessionManager(data_dir=tmp_path / "data")
    result = connect_connector(m.secrets, "jarvis", {"url": jarvis.url, "token": jarvis.token})
    assert result["ok"], result
    m.connect_result = result
    return m


async def _call(tools: dict, short: str, **arguments):
    assert short not in FORBIDDEN, f"e2e must never call {short}"
    return await asyncio.to_thread(tools[f"mcp__jarvis__{short}"], **arguments)


def _run(manager, scenario, session="s1"):
    async def go():
        try:
            tools = await manager.prepare_mcp_tools(session)
            return await scenario({t.__name__: t for t in tools})
        finally:
            await manager.mcp.aclose()

    return asyncio.run(go())


@pytest.mark.readonly
def test_identity(manager):
    assert manager.connect_result["account"].startswith("jarvis v")


@pytest.mark.readonly
def test_connected_jarvis_is_effective(manager):
    # Real session gating, not a monkeypatched set: a connected connector must be live.
    assert "jarvis" in manager.effective_connectors("s1")


@pytest.mark.readonly
def test_drift(jarvis):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def live_names():
        headers = {"Authorization": f"Bearer {jarvis.token}"}
        async with streamablehttp_client(jarvis.url, headers=headers) as (r, w, _):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return {t.name for t in (await session.list_tools()).tools}

    live = asyncio.run(live_names())
    pinned = set(mcp_pinned_tools("jarvis"))
    assert not pinned - live, f"pinned but gone from Jarvis: {sorted(pinned - live)}"
    print(f"live Jarvis tools not pinned: {sorted(live - pinned)}")


@pytest.mark.readonly
def test_status_is_read(manager):
    async def scenario(tools):
        fn = tools["mcp__jarvis__jarvis_status"]
        assert classify(fn.__name__, fn.__aisuite_tool_metadata__) == RiskClass.READ
        assert fn.__aisuite_tool_metadata__.requires_approval is False
        return await _call(tools, "jarvis_status")

    assert "JARVIS STATUS" in str(_run(manager, scenario))


@local_only
def test_share_context_is_gated(manager):
    async def scenario(tools):
        fn = tools["mcp__jarvis__share_context"]
        assert fn.__aisuite_tool_metadata__.requires_approval is True
        assert classify(fn.__name__, fn.__aisuite_tool_metadata__) == RiskClass.EXTERNAL
        return await _call(tools, "share_context", namespace="ow-e2e", key="k", value="v")

    assert "Context stored" in str(_run(manager, scenario))


@local_only
def test_tunnel_tools_absent_by_default(manager):
    async def scenario(tools):
        return set(tools)

    names = _run(manager, scenario)
    assert "mcp__jarvis__mcp_call" not in names and "mcp__jarvis__mcp_connect" not in names
    assert "mcp__jarvis__read_file" in names


@local_only
def test_long_verify_work(manager, jarvis):
    # 150 s: past OpenWorker's old 120 s ceiling. timeoutSeconds lifts Jarvis's own
    # execSync limit (default 120) so only OpenWorker's wait is under test.
    async def scenario(tools):
        started = time.monotonic()
        out = await _call(
            tools, "verify_work",
            command='node -e "setTimeout(()=>{},150000)"',
            projectPath=jarvis.workdir, timeoutSeconds=200,
        )
        return out, time.monotonic() - started

    out, elapsed = _run(manager, scenario)
    assert elapsed >= 150
    assert "VERIFICATION PASSED" in str(out)


@local_only
def test_jarvis_restart_mid_session(manager, jarvis):
    # One long-lived manager across both sessions, as in the desktop app: no aclose()
    # between them, so a dead cached connection would be handed to session s2.
    async def scenario():
        try:
            s1 = {t.__name__: t for t in await manager.prepare_mcp_tools("s1")}
            first = await _call(s1, "jarvis_status")
            jarvis.restart()
            started = time.monotonic()
            with pytest.raises(Exception) as info:
                await asyncio.wait_for(_call(s1, "jarvis_status"), 30)
            assert not isinstance(info.value, asyncio.TimeoutError), "the call hung"
            print(f"in-flight call after restart: {info.value!r} in {time.monotonic() - started:.1f}s")
            s2 = {t.__name__: t for t in await manager.prepare_mcp_tools("s2")}
            return first, await _call(s2, "jarvis_status")
        finally:
            await manager.mcp.aclose()

    first, fresh = asyncio.run(scenario())
    assert "JARVIS STATUS" in str(first)
    assert "JARVIS STATUS" in str(fresh)
