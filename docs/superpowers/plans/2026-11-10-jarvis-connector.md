# Jarvis Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Jarvis's 58 MCP tools in OpenWorker as an MCP-backed connector that
authenticates with a static token and gates each tool on its own read/write approval.

**Architecture:** This extends OpenWorker's existing MCP-backed connector path (jira, monday),
which today assumes OAuth and a fixed vendor URL. It adds a second auth mode, `mcp_token`: the
user pastes a URL and a token, the token lives only in the SecretStore, and the token is
injected as a header at session start. A per-tool timeout is added to the catalog.

Tasks 1–5 are generic and contain no Jarvis strings, so they can go upstream as a unit.
Tasks 6–8 add Jarvis.

**Tech Stack:** Python ≥3.10, `mcp>=1.28.1,<2` (`MCPManager`, FastMCP for test servers),
httpx ≥0.27, pytest with `asyncio_mode = "auto"`, and Node for the Jarvis E2E server.

**Spec:** `docs/superpowers/specs/2026-11-10-jarvis-connector-design.md`

## Global Constraints

- No new runtime dependencies. Use only `httpx`, `mcp` and what is already in `pyproject.toml`.
- The token is **never** written to `mcp.json`. It lives only in the SecretStore profile `<name>:default`.
- The default MCP tool timeout stays `120.0` s. Jarvis's long tools use `900.0` s.
- Pinned tool names are exactly `mcp__jarvis__<jarvis tool name>`.
- Leave the existing `mcp_url` sites that mean OAuth unchanged: setup.py:64, setup.py:88,
  app.py:1623 and manager.py:2417. Only manager.py:2179 and manager.py:2302 switch to `mcp_backed`.
- No GUI code changes. `mcp_token` connectors render through the existing `ConnectSetup` paste form.
- Tasks 1–5 must contain no `jarvis` string in either code or tests.
- E2E Jarvis runs only from a git worktree, with a temp working directory, no `MONGODB_URI`,
  a throwaway token and port 3199. Never run it from the main Jarvis checkout.
- Never print or commit a real token. The Cloud Run token is read from the `JARVIS_E2E_TOKEN` env var only.
- Each commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

These inputs are not covered by the spec's tests but are likely to hit a real user. Each gets
a test in the task named.

1. **The URL is pasted without `/mcp`** (`http://localhost:3100`). The user should get one
   clear refusal that suggests `<url>/mcp`, not a raw HTTP 404. → Task 3
2. **The URL or token is pasted with surrounding whitespace or a trailing newline.** It should
   be stripped before validating and before storing. → Task 4
3. **Reconnecting with the secret field left blank** (for example, only the URL changed). The
   stored token should be kept and used to validate the new URL. → Task 4
4. **The token is rotated in Jarvis after connecting.** At the next session start, the server
   should be skipped with the error `token rejected — reconnect from Connectors`. No hang, and
   no OAuth browser. → Task 5
5. **Jarvis restarts mid-session**, so the server forgets the session (Jarvis returns 400
   "Invalid session"). The in-flight tool call should return an error promptly instead of
   hanging, and the next session should work. → Task 7

---

### Task 0: Dev environment and baseline

**Files:** none committed. `.venv/` is already ignored; check that `git status` stays clean.

- [ ] **Step 1: Create the venv.** Run
  `py -3 -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[messaging,dev]"`.
  Expected: it installs without errors.
  If a dependency fails to build on Python 3.14: run `uv python install 3.12` and recreate the
  venv with `uv venv -p 3.12 .venv`. CI uses 3.12. Record which interpreter was used.
- [ ] **Step 2: Baseline.** Run
  `.venv/Scripts/python.exe -m pytest tests/test_mcp_connectors.py tests/test_connector_registry.py tests/test_mcp.py tests/test_mcp_tool_selection.py tests/test_headless_mcp.py -q`.
  Expected: all pass. Record the count.
  Investigate any failure that is already present at `ddb8b36` (Windows-only, for example)
  before writing code, and note it in the final report. Do not "fix" it in this branch.

### Task 1: Per-tool MCP timeout

**Files:**
- Modify: `coworker/connectors/tool_defs.py`
  - `ConnectorToolDef` (lines 11-30): add the field last.
  - Add `tool_timeout` next to `approval_for_tool` (~1197).
- Modify: `coworker/mcp/tools.py`: `build_callables` (line 57) and `_make_invoke`.
- Test: `tests/test_mcp_tool_selection.py`

**Interfaces:**
- Produces:
  - `ConnectorToolDef.timeout: Optional[float] = None`
  - `tool_timeout(name: str, default: float = 120.0) -> float`. Returns the catalog
    `timeout` when it is set, otherwise `default`. Unknown names also get `default`.
  - `build_callables(server, mcp_tools, call_async, loop, *, timeout: float = 120.0, timeout_for: Optional[Callable[[str], float]] = None)`.
    When `timeout_for` is given, `future.result(timeout_for(full_name))` is used, where
    `full_name` is the `mcp__<server>__<tool>` name the callable is registered under.

- [ ] **Step 1: Failing tests.**
  - `test_build_callables_uses_per_tool_timeout`: build two fake tools, `fast` and `slow`,
    with `timeout_for=lambda n: 0.05 if n.endswith("__fast") else 5.0`. `call_async` is a
    coroutine that sleeps 0.2 s on a running loop thread. Calling `fast` must return the
    existing timeout error shape (assert whatever `_make_invoke` returns today on
    `TimeoutError`, read from the code). Calling `slow` returns the result.
  - `test_build_callables_default_timeout_unchanged`: with no `timeout_for`, the timeout
    passed to `future.result` is `120.0`. Monkeypatch a fake future that records its argument.
  - `test_tool_timeout_reads_catalog`: `tool_timeout("unknown_tool") == 120.0`.
- [ ] **Step 2:** Run
  `.venv/Scripts/python.exe -m pytest tests/test_mcp_tool_selection.py -q`.
  Expected: the new tests FAIL (`unexpected keyword 'timeout_for'` / `ImportError tool_timeout`).
- [ ] **Step 3:** Implement the field, `tool_timeout` and `timeout_for` as specified above.
- [ ] **Step 4:** Run the Step 2 command plus `tests/test_mcp.py`. Expected: everything passes.
- [ ] **Step 5:** Commit: `feat(mcp): optional per-tool timeout for MCP tool calls`.

### Task 2: `mcp_backed` descriptor property

**Files:**
- Modify: `coworker/connectors/descriptors.py`, `ConnectorDescriptor` (~line 46).
- Modify: `coworker/server/manager.py:2179` and `:2302`, changing `bool(descriptor.mcp_url)` /
  `d.mcp_url` to `.mcp_backed`.
- Test: `tests/test_mcp_connectors.py`

**Interfaces:**
- Produces:
  - `ConnectorDescriptor.mcp_backed -> bool` (a property): `bool(self.mcp_url) or self.auth == "mcp_token"`.
  - A module constant `MCP_TOKEN_AUTH = "mcp_token"` in descriptors.py. Every later task uses
    this constant, never the literal string.

- [ ] **Step 1: Failing tests.**
  - `test_mcp_backed_covers_token_auth`: build a descriptor
    `ConnectorDescriptor(name="tok", title="T", icon="", blurb="", auth=MCP_TOKEN_AUTH, two_way=False, fields=[], instructions=[])`.
    Assert `.mcp_backed is True`. A plain `auth="token"` descriptor gives `False`. Monday gives `True`.
  - Change `test_every_mcp_backed_descriptor_pins_tools` (line 38) to filter on `d.mcp_backed`.
  - `test_token_backed_server_hidden_from_mcp_tab`: `register_descriptor` a temporary
    `mcp_token` descriptor (remove it in a `finally`), `put_global_server` it, and assert it is
    absent from the manager's MCP-tab list (the method at manager.py ~2299).
- [ ] **Step 2:** Run `pytest tests/test_mcp_connectors.py -q`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement the property and the constant, and change the two manager sites.
- [ ] **Step 4:** Run `pytest tests/test_mcp_connectors.py tests/test_connector_registry.py -q`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(connectors): mcp_backed covers token-auth MCP connectors`.

### Task 3: In-process MCP HTTP test server and the `initialize` validator

**Files:**
- Create: `tests/mcp_http_server.py`, a test helper and not a test module.
- Modify: `coworker/connectors/descriptors.py`: add the validator in the validators section
  (~line 96+).
- Test: `tests/test_mcp_token_validate.py`

**Interfaces:**
- Produces (test helper):
  `mcp_http_server(*, token: str, tools: dict[str, Callable[..., Any]], json_response: bool = False) -> ContextManager[ServerHandle]`.
  - `ServerHandle` has `.url` (`http://127.0.0.1:<free port>/mcp`), `.set_token(new: str)`
    and `.restart()`. `restart()` drops all sessions while keeping the same port.
  - Build it on FastMCP `streamable_http_app()`, with Starlette middleware that returns `401`
    unless `Authorization == f"Bearer {current_token}"`. Serve it with `uvicorn.Server` on a
    daemon thread, and wait until the server is started.
  - Default replies are SSE, matching Jarvis. `json_response=True` exercises the JSON branch.
- Produces (code): `validate_mcp_token(creds: dict) -> ValidationResult`, which reads
  `creds["url"]` and `creds["token"]`.
  - It POSTs a JSON-RPC `initialize` (protocolVersion taken from `mcp.types.LATEST_PROTOCOL_VERSION`)
    with `Accept: application/json, text/event-stream`, using httpx with `timeout=15`.
  - It parses a JSON body or the first SSE `data:` event, then sends `DELETE` with the
    `mcp-session-id` it got back (best effort; a failed DELETE does not fail validation).
  - Results:
    - Success → `ValidationResult(True, identity=f"{serverInfo.name} v{serverInfo.version}")`
    - 401/403 → error `"token rejected"`
    - `httpx.ConnectError` → `f"can't reach {url}"`
    - 404/405 when the path does not end in `/mcp` → `f"no MCP endpoint at {url} — did you mean {url.rstrip('/')}/mcp?"`
    - Any other status or a malformed body → `f"not an MCP server: HTTP {status}"`

- [ ] **Step 1: Failing tests** in `tests/test_mcp_token_validate.py`, each against
  `mcp_http_server(token="t", tools={"ping": lambda: "pong"})`:
  - `test_validate_ok_sse`: the identity starts with the FastMCP server name and contains ` v`.
  - `test_validate_ok_json`: the same, with `json_response=True`.
  - `test_validate_wrong_token`: `.ok is False` and `.error == "token rejected"`.
  - `test_validate_unreachable`: on a closed port, the error starts with `"can't reach"`.
  - `test_validate_missing_mcp_path` *(Review Focus 1)*: passing `handle.url.removesuffix("/mcp")`
    gives an error containing `"did you mean"` and ending `"/mcp?"`.
  - `test_validate_leaves_no_session`: spy on the DELETE request through a middleware counter
    in the helper (expose `handle.deletes: int`), and assert `== 1` after a successful validate.
- [ ] **Step 2:** Run `pytest tests/test_mcp_token_validate.py -q`. Expected: FAIL (`ImportError validate_mcp_token`).
- [ ] **Step 3:** Implement `validate_mcp_token` and the helper.
- [ ] **Step 4:** Run the same command. Expected: 6 passed.
- [ ] **Step 5:** Commit: `feat(connectors): MCP initialize validator for token-auth servers`.

### Task 4: Connecting a token-auth connector

**Files:**
- Modify: `coworker/connectors/setup.py`, `connect_connector`, before `secrets.put` (line ~382).
- Test: `tests/test_mcp_token_connect.py`

**Interfaces:**
- Consumes: `MCP_TOKEN_AUTH`, `validate_mcp_token` (Task 3), `mcp_pinned_tools` (existing), and
  `put_global_server` / `read_global` (existing).
- Produces: for `d.auth == MCP_TOKEN_AUTH`, `connect_connector`:
  - strips `url` and `token`;
  - stores the profile `{"type": "token", "enabled": True, "url", "token", "mode": "mcp", "account": identity}`;
  - calls
    `put_global_server(name, {"url": url, "requires_approval": True, "include_tools": mcp_pinned_tools(name), "enabled": True})`.
  On validation failure, nothing is written: no profile and no `mcp.json` entry.

Each test registers a temporary `mcp_token` descriptor `"tok"` with fields `url` and `token`
(secret), `validate=validate_mcp_token`, and two `ConnectorToolDef("tok", "mcp__tok__ping", ..., "read")`
pins. Pins are added by monkeypatching `TOOL_DEFS`-derived indexes; use a fixture that restores them.

- [ ] **Step 1: Failing tests.**
  - `test_connect_seeds_config_without_token`:
    - the `mcp.json` entry has `url`, `requires_approval is True` and `include_tools == ["ping"]`;
    - `"t" not in global_mcp_path().read_text()` and no `Authorization` key appears;
    - the profile has `mode == "mcp"` and `token == "t"`.
  - `test_connect_failure_writes_nothing`: with a wrong token, `ok is False`,
    `read_global()` has no `"tok"` and `secrets.get("tok:default") is None`.
  - `test_connect_strips_whitespace` *(Review Focus 2)*: fields
    `{"url": f" {url}\n", "token": "t\n"}` connect OK, and the stored values are stripped.
  - `test_reconnect_blank_token_keeps_stored` *(Review Focus 3)*: connect, then reconnect with
    `{"url": url, "token": ""}`. The result is `ok`, and the stored token is still `"t"`.
  - `test_disconnect_removes_config_and_profile`: after `disconnect_connector(secrets, "tok")`,
    `"tok" not in read_global()` and the profile is gone.
- [ ] **Step 2:** Run `pytest tests/test_mcp_token_connect.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement the `mcp_token` branch.
  Check first how `_resolved` already treats blank secrets (setup.py ~333), and reuse that path
  rather than duplicating it.
- [ ] **Step 4:** Run the same command plus `tests/test_connectors.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(connectors): connect token-auth MCP connectors without writing the token to mcp.json`.

### Task 5: Session-start token injection and catalog timeouts

**Files:**
- Modify: `coworker/server/manager.py`, `prepare_mcp_tools`, in the `if backed:` branch
  (2180-2194) and at the `build_callables` call (2243).
- Test: `tests/test_mcp_token_session.py`

**Interfaces:**
- Consumes: `MCP_TOKEN_AUTH`, `tool_timeout`, `build_callables(..., timeout_for=)` (Task 1),
  and the Task 3 helper.
- Produces: `SessionManager._inject_mcp_token(server: MCPServerDef, descriptor) -> Optional[str]`.
  - When `descriptor.auth == MCP_TOKEN_AUTH`, it sets `server.url = profile["url"]` and
    `server.headers["Authorization"] = f"Bearer {profile['token']}"`, and returns `None`.
  - When the profile or token is missing, it returns `"not connected — reconnect from Connectors"`.
    The caller records that in `_mcp_errors[name]` and skips the server.
  - Backed servers call `build_callables(..., timeout_for=tool_timeout)`.

- [ ] **Step 1: Failing tests.** Use the `test_prepare_mcp_tools_gates_by_session_pin_and_toggles`
  pattern: `SessionManager(data_dir=tmp_path/"data")`, a `fake_ensure` capturing `server`, and
  `effective_connectors` monkeypatched to `{"tok"}`.
  - `test_prepare_injects_bearer_from_secrets`: `seen.headers["Authorization"] == "Bearer t"`
    and `seen.url == profile url`.
  - `test_prepare_missing_token_skips_with_error`: the profile has no token, so `ensure` is
    never called and `manager._mcp_errors["tok"]` starts with `"not connected"`.
  - `test_prepare_per_tool_approval_and_category`: a read pin gives
    `requires_approval is False`, a write pin gives `True`, and both have `category == "connector"`.
  - `test_prepare_passes_catalog_timeout`: monkeypatch `build_callables` in the manager module
    to capture `timeout_for`. Assert `timeout_for("mcp__tok__slow") == 900.0` when that pin has
    `timeout=900.0`, and `== 120.0` for an unpinned name.
  - `test_real_path_call_through_mcpmanager`: no `fake_ensure`. Run the real `MCPManager`
    against `mcp_http_server(token="t", tools={"ping": ...})` after connecting through
    `connect_connector`. Calling the `mcp__tok__ping` callable returns `"pong"`.
    Close with `asyncio.run(manager.mcp.aclose())`.
  - `test_rotated_token_skips_with_error` *(Review Focus 4)*: connect, then `handle.set_token("new")`,
    then run `prepare_mcp_tools` on a new session. The tool list is empty,
    `_mcp_errors["tok"]` is set, and the call returns within 30 s (wrap it in `asyncio.wait_for`).
    Then change the error text for a 401 on a token-auth server to
    `"token rejected — reconnect from Connectors"`. Find how the existing error path formats a
    401 (manager.py 2204-2228) and add that branch.
  - `test_headless_load_fails_closed` (spec 1g): after `connect_connector`, the `"tok"` entry
    from `load_mcp_servers(None, secrets=secrets)` has `headers == {}` and
    `requires_approval is True`.
- [ ] **Step 2:** Run `pytest tests/test_mcp_token_session.py -q`. Expected: FAIL. (The
  headless test may already pass after Task 4; that's fine. It pins the behavior.)
- [ ] **Step 3:** Implement the changes.
- [ ] **Step 4:** Run the Step 2 command plus `tests/test_mcp_connectors.py tests/test_mcp.py tests/test_headless_mcp.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(mcp): inject token auth and catalog timeouts for token-backed servers`.

### Task 6: Jarvis descriptor and pinned catalog

**Files:**
- Modify: `coworker/connectors/descriptors.py`, adding to `DESCRIPTORS` (ends at ~line 1443).
- Modify: `coworker/connectors/tool_defs.py`, adding to `TOOL_DEFS` before its closing paren at line 1187.
- Modify: `tests/test_connector_registry.py`, adding `"jarvis"` to the brand-color map (line ~29).
- Test: `tests/test_jarvis_connector.py`

**Interfaces:**
- Consumes: `MCP_TOKEN_AUTH`, `validate_mcp_token`, `ConnectorToolDef.timeout`.
- Produces: descriptor `"jarvis"` with:
  - `title="Jarvis"`, `auth=MCP_TOKEN_AUTH`, `two_way=False`, a hex `brand_color`, `logo=""`
  - `fields=[Field("url", "MCP URL", placeholder="http://localhost:3100/mcp"), Field("token", "MCP token", secret=True, help="MCP_SECRET_TOKEN from Jarvis's .env")]`
  - `validate=validate_mcp_token`
  - 2-3 `instructions` lines: start Jarvis's MCP server, paste its URL ending in `/mcp`, paste the token.

  It also produces 58 `ConnectorToolDef("jarvis", "mcp__jarvis__<tool>", <label>, <kind>, <description>, ...)`
  entries. Labels and descriptions come from the Section 2 table and the Jarvis `server.tool(`
  description strings. The kinds, default-off tools and timeouts are those in the spec's
  "Pinned tools" section.

- [ ] **Step 1: Failing tests.**
  - `test_jarvis_pins_exactly_58`: `set(mcp_pinned_tools("jarvis"))` equals the literal 58-name
    set, copied from the spec.
  - `test_jarvis_kinds`: the literal 22-name read set maps to `"read"` and the 36-name write set
    to `"write"`. Assert `verify_work` is `"write"`.
  - `test_jarvis_tunnel_default_off`: `mcp_connect` and `mcp_call` have `default_enabled is False`,
    and every other pin has `True`.
  - `test_jarvis_timeouts`: `tool_timeout("mcp__jarvis__<t>") == 900.0` for exactly
    `{delegate_to_claude, think_hard, verify_work, spawn_agents, launch_fleet, run_agents}`,
    and `120.0` for the rest.
  - `test_jarvis_descriptor`: `mcp_backed is True`, `auth == MCP_TOKEN_AUTH`, the field keys are
    `["url", "token"]`, and the token field has `secret is True`.
- [ ] **Step 2:** Run `pytest tests/test_jarvis_connector.py tests/test_connector_registry.py -q`. Expected: FAIL.
- [ ] **Step 3:** Add the descriptor and the 58 defs.
- [ ] **Step 4:** Run the Step 2 command plus `tests/test_mcp_connectors.py`. Expected: PASS.
- [ ] **Step 5:** Commit: `feat(connectors): Jarvis connector with 58 pinned MCP tools`.

### Task 7: E2E against a real local Jarvis, plus a GUI check

**Files:**
- Create: `tests/test_jarvis_e2e.py`. Every test is marked
  `pytest.mark.skipif(not os.environ.get("JARVIS_E2E_URL"), reason="operator-run e2e")`.
  This is an operator-run gate, not CI coverage; say so in the final report.

**Interfaces:**
- Consumes: everything above. `JARVIS_E2E_URL`, `JARVIS_E2E_TOKEN` and
  `JARVIS_E2E_MODE in {"local", "prod"}`. `prod` runs only the tests marked `readonly`.

- [ ] **Step 1: Start the Jarvis test instance safely.** From the Jarvis repo, run
  `git worktree add ../Jarvis-e2e main` (if it doesn't exist) and `npm ci` there. Then start it
  in the background from a temp dir:
  `cd "$SCRATCH/jarvis-e2e" && env -u MONGODB_URI MCP_SECRET_TOKEN=<random 32 hex> node <Jarvis-e2e>/src/mcp/http.js --port 3199 --project-dir "$SCRATCH/jarvis-e2e"`.
  Check that `curl -s http://127.0.0.1:3199/` returns `{"status":"ok"...}`, and that the main
  checkout's `bridge.pid` is unchanged (compare its mtime before and after).
- [ ] **Step 2: Write the tests.** Each one connects through `manager.connect_connector("jarvis", {...})`
  and runs `prepare_mcp_tools` with the real `MCPManager`.
  - `test_identity` (readonly): the connect result's `account == "jarvis v1.0.0"`.
  - `test_drift` (readonly): a raw MCP `tools/list` over httpx. Every pin is present (fail on
    any missing), and unpinned live tools are printed.
  - `test_status_is_read` (readonly): `risk.classify` of `mcp__jarvis__jarvis_status` is READ,
    and calling it returns text containing `"JARVIS STATUS"`.
  - `test_share_context_is_gated` (local): `requires_approval is True`, and classify gives
    EXTERNAL. Calling it with `namespace="ow-e2e"` returns text containing `"Context stored"`.
  - `test_long_verify_work` (local): call `verify_work` with
    `command='node -e "setTimeout(()=>{},150000)"'`. It must return a PASSED result after
    150 s or more without a timeout error. Allow up to 200 s.
  - `test_jarvis_restart_mid_session` *(Review Focus 5)*, local, kept in a separate run:
    1. Make one successful call.
    2. Have the operator-side fixture restart the Jarvis process (kill it and relaunch it with
       the same command).
    3. Call again. It must return an error within 30 s.
    4. A fresh `prepare_mcp_tools` session then succeeds.
    If the second call hangs: stop. That's a real defect in session handling. Report it, and
    fix it in `MCPManager` reconnect handling as an extra task.
  - `think_hard` and `delegate_to_claude` never appear in this file. A test asserts that by
    reading its own source.
- [ ] **Step 3: Run.**
  `JARVIS_E2E_MODE=local JARVIS_E2E_URL=http://127.0.0.1:3199/mcp JARVIS_E2E_TOKEN=<token> .venv/Scripts/python.exe -m pytest tests/test_jarvis_e2e.py -v`.
  Expected: everything passes, including the restart test in its own run.
- [ ] **Step 4: GUI check.** Start the OpenWorker agent server and the Vite UI as described in
  CONTRIBUTING. Then, with the Chrome tools:
  - open Connectors, find Jarvis by searching, and open it;
  - check that the paste form shows the "MCP URL" and "MCP token" fields;
  - connect to `:3199`, and check the card reads "jarvis v1.0.0";
  - in a session, ask it to run `jarvis_status` (no approval card) and then `share_context`
    (an approval card appears, and approving it returns "Context stored").
  Save screenshots in the scratchpad.
- [ ] **Step 5: Stop the instance.** Stop the test Jarvis and remove the worktree with
  `git worktree remove ../Jarvis-e2e`. Commit the tests:
  `test: operator-run Jarvis e2e (local + read-only prod)`.

### Task 8: Read-only E2E against Cloud Run

- [ ] **Step 1:** The operator puts the prod token into `JARVIS_E2E_TOKEN` (an env var only,
  never echoed). Set `JARVIS_E2E_URL=https://<cloud run host>/mcp`. Read the host from
  `gcloud run services describe jarvis --region us-central1 --format='value(status.url)'`.
- [ ] **Step 2:** Run
  `JARVIS_E2E_MODE=prod ... pytest tests/test_jarvis_e2e.py -m readonly -v`.
  Expected: `test_identity`, `test_drift` and `test_status_is_read` pass. Record the
  cold-start latency of the first call. No write tool runs.

### Task 9: Guard proof, review and CodeQL

- [ ] **Step 1: Mutation proof.** Apply each mutation, run the suite, record which named test
  fails, then restore and check `git diff --exit-code`.
  1. Remove the Bearer injection → `test_prepare_injects_bearer_from_secrets`.
  2. Make backed tools keep server-level approval → `test_prepare_per_tool_approval_and_category`.
  3. Drop `timeout_for` at the manager call → `test_prepare_passes_catalog_timeout`.
  4. Set the tunnel pins `default_enabled=True` → `test_jarvis_tunnel_default_off`.
  5. Make `verify_work` read → `test_jarvis_kinds`.
  6. Write the token into the `mcp.json` seed → `test_connect_seeds_config_without_token`.
  A mutation that no test catches is a gap: add the test before going on.
- [ ] **Step 2: Full suite.** Run `.venv/Scripts/python.exe -m pytest -q`.
  Expected: the Task 0 baseline count plus the new tests, with no new failures.
- [ ] **Step 3: Adversarial review.** Use the four code-role dimensions: functional gaps on the
  real path, data-flow wiring, dead code and dead params.
  Then run a second completeness pass cut along a different axis: trust boundary (who can
  write `mcp.json` or the profile, and what an attacker-controlled URL or Jarvis response can
  do). That pass must state its own blind spot. Verify each finding against the code before
  fixing it.
- [ ] **Step 4: CodeQL.** Run `~/.claude/scripts/codeql-agent-gate.cmd` from the repo.
  Expected: clean, or each finding triaged.
- [ ] **Step 5: Publish (ask the operator first).** It's an external action, so get approval
  before running `gh repo fork andrewyng/openworker --remote` and
  `git push -u <fork> jarvis-connector`. Report the commit range for Tasks 1–5 as the upstream
  candidate.
