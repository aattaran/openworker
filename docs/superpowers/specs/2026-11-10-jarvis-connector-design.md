# Jarvis connector: design

Date: 2026-11-10 · Branch: `jarvis-connector` (fork of andrewyng/openworker @ `ddb8b36`)

## Goal

Make the full Jarvis MCP surface (58 tools) available to OpenWorker sessions as a first-class
connector. Each tool keeps OpenWorker's governance: a pinned allowlist, per-tool read/write
approval, per-session connector gating, and per-tool toggles.

Jarvis is a self-hosted MCP server (Streamable HTTP, `POST /mcp`, static
`Authorization: Bearer <MCP_SECRET_TOKEN>`). It runs either on the local PC (`:3100`) or on
Cloud Run.

## Decision: extend the MCP-backed connector path

OpenWorker already ships MCP-backed connectors (jira, monday): `descriptor.mcp_url`, tools
pinned in `tool_defs` as `mcp__<connector>__<tool>`, per-tool approval applied in
`prepare_mcp_tools` (coworker/server/manager.py:2178-2265), and `MCPManager` (official `mcp`
SDK) as the client. That path assumes a vendor URL that is the same for everyone, plus OAuth.
Jarvis has a per-user URL and a static token.

So this change adds a second way to back a connector with MCP, **token auth**, and keeps
everything downstream unchanged. It does not hand-write tool functions and does not add a new
MCP client. Rejected alternatives:

- 47 hand-written functions plus a custom client, which duplicates `MCPManager`.
- A raw `mcp.json` entry, which gives server-level approval only and keeps the token in plain JSON.

## Delivery

Two commits on the fork, so the first can go upstream on its own:

1. **Generic: token-auth MCP-backed connectors and per-tool timeouts.** No Jarvis content.
2. **Jarvis: descriptor and the 58 pinned tool defs.**

## Commit 1: generic changes

### 1a. `ConnectorDescriptor` (descriptors.py)
- New auth value `"mcp_token"`. Connectors that use it carry `url` and `token` fields and leave
  `mcp_url` empty.
- New read-only property `mcp_backed -> bool`, true when `mcp_url` is set or
  `auth == "mcp_token"`.

### 1b. Split the two jobs `mcp_url` does today
Every current `mcp_url` read site has been classified:

| Site | Meaning | After |
|---|---|---|
| setup.py:64 | connected ⇔ OAuth tokens exist | unchanged (`mcp_url`) |
| setup.py:88 | GUI `mcp` flag → one-click OAuth pane | unchanged (`mcp_url`) |
| app.py:1623 | `/mcp-connect` OAuth endpoint | unchanged (`mcp_url`) |
| manager.py:2179 | backed: pin + per-tool approval + category "connector" | `mcp_backed` |
| manager.py:2302 | hide backed servers from the raw MCP tab | `mcp_backed` |
| manager.py:2417 | OAuth one-click connect | unchanged (`mcp_url`) |

Because the GUI `mcp` flag stays false, an `mcp_token` connector renders the standard
paste-fields form (`ConnectSetup`, AddConnectionModal.tsx:121-125). **No GUI code changes.**

### 1c. Connect (setup.py `connect_connector`)
For `auth == "mcp_token"`:
1. Run the descriptor's `validate`, which makes a real MCP `initialize` call (sync httpx,
   `Accept: application/json, text/event-stream`, parsing either reply). Results:
   - 200 → `ValidationResult(ok, identity="<serverInfo.name> v<version>")`
   - 401/403 → `"token rejected"`
   - connect error → `"can't reach <url>"`
2. Store the profile `<name>:default = {url, token, mode: "mcp", enabled: true}` in the
   SecretStore.
3. Seed the global `mcp.json` with `{url, requires_approval: true, include_tools: pinned,
   enabled: true}`. **The token is never written to `mcp.json`.**

On validation failure, nothing is written.

### 1d. Session start (manager.py `prepare_mcp_tools`)
For a backed server whose descriptor auth is `mcp_token`, set
`server.headers["Authorization"] = f"Bearer {profile['token']}"` from the SecretStore and
`server.url = profile["url"]`, then continue through the existing backed path (pin filter, `ensure`,
per-tool approval, category flip, error notices).

If the profile is missing or has no token, skip the server and record
`_mcp_errors[name] = "not connected — reconnect from Connectors"`. Never send an empty header.

### 1e. Disconnect
Delete the profile and the `mcp.json` entry, and revoke `mcp__<name>__*` trust rules. This is
the existing behavior; it is verified by a test, not new code.

### 1f. Per-tool timeout
- `ConnectorToolDef` gains `timeout: Optional[float] = None`.
- `build_callables` takes a `timeout_for(full_tool_name) -> float` in place of the single
  `timeout`, defaulting to the current 120.0.
- `prepare_mcp_tools` passes the catalog value for backed servers.
- Raw servers and the headless runner keep 120 s.

### 1g. Headless runner
No change. It reads raw `mcp.json`, so an `mcp_token` server connects without its header, gets
a 401, and is skipped with a note. This fails closed. Seeding `requires_approval: true` is a
second guard, in case a header is ever supplied that way.

*Upstream observation (out of scope):* OAuth-backed seeds write `requires_approval: false`, so
jira/monday write tools run ungated in headless `--mcp` runs.

## Commit 2: Jarvis

### Descriptor
- `name="jarvis"`, `title="Jarvis"`, `auth="mcp_token"`, `two_way=False`
- hex `brand_color`, `logo=""` (UI fallback badge)
- fields:
  - `url`: "MCP URL", placeholder `http://localhost:3100/mcp`
  - `token`: "MCP token", `secret=True`, help "`MCP_SECRET_TOKEN` from Jarvis's .env"
- `validate` = the generic MCP-initialize validator

### Pinned tools: `mcp__jarvis__<name>`
Sourced from `server.tool(` registrations in Jarvis `src/mcp/server.js` (43) and
`src/mcp/amplifier.js` (15).

**read (22), auto-approved:**
read_file, list_directory, search_code, list_remote_controls, check_delegation, query_memory,
capture_screenshot, list_windows, read_ui_elements, read_clipboard, list_processes, recall,
security_audit, context_snapshot, read_handoff, knowledge_recall, jarvis_status, plan_read,
plan_agents, analyze_traces, review_pr, get_shared_context

**write (36), approval-gated:**
write_file, run_command, verify_work¹, start_remote_control, stop_remote_control, delegate_to_claude, spawn_agents, configure_fleet, launch_fleet,
send_message, click_element, focus_window, send_keystroke, run_in_terminal, scroll_window,
open_file_in_ide, click_at, type_text, manage_window, write_clipboard, launch_app,
kill_process, remember, write_handoff, think_hard, web_search, broadcast_status,
knowledge_store, run_agents, session_start, session_end, plan_write, share_context,
upload_to_drive, mcp_connect², mcp_call²

¹ `verify_work` passes its `command` argument to `execSync`, which makes it equivalent to
`run_command`. As read, it would have bypassed `run_command`'s write gate.
² `default_enabled=False`. They tunnel to any MCP server Jarvis can reach, which would
sidestep the pinned allowlist. The user can enable each one per tool.

**timeout = 900 s** (Jarvis's 15-minute agent limit): delegate_to_claude, think_hard,
verify_work, spawn_agents, launch_fleet, run_agents. All other tools use 120 s. Latency for the
others is measured in the e2e run, and any tool that needs more is raised then.

### Trust boundary
Jarvis tools run on the Jarvis host. OpenWorker's sandbox (OpenShell, macOS sandbox, hidden
Windows account) does not contain them, so the approval gate is the only control. The
read/write split above carries that weight.

## Testing

**Unit tests** (pytest, following test_mcp_connectors.py / test_connector_registry.py):
- The pin set equals the 58 names, kinds match this spec, and `mcp_connect`/`mcp_call` are
  default-off.
- `test_every_mcp_backed_descriptor_pins_tools` covers `mcp_token` connectors.
- `prepare_mcp_tools` injects the bearer from the SecretStore. The raw bytes of `mcp.json`
  never contain the token.
- A missing token skips the server and records an error.
- A pinned timeout reaches `build_callables`, and unpinned tools stay at 120 s.
- Validator: 200 / 401 / unreachable.
- Disconnect removes the profile, config, and trust rules.
- The registry brand test passes for `jarvis`.

**E2E, local real path, no paid calls:**
- Run `node src/mcp/http.js` with a throwaway token, connect through `/v1/connectors/jarvis`,
  and open a session.
- `jarvis_status` runs without approval.
- `share_context` shows the approval card.
- `verify_work` with `node -e "setTimeout(()=>{},150000)"` completes past 120 s.
- Drift check: every pin exists in the live `tools/list`. The check fails on a missing pin and
  reports unpinned live tools.
- `think_hard` and `delegate_to_claude` are never invoked.

**E2E, Cloud Run:**
- Read-only: `initialize`, `tools/list` drift check, `jarvis_status`.
- No write tools run in prod.
- The prod token comes from the environment and is never printed or committed.

**Guard proof:**
- Mutate each of these and confirm a named test fails, with sources restored byte-identical:
  1. bearer injection
  2. the per-tool approval override
  3. timeout threading
  4. the tunnel default-off
  5. `verify_work`'s kind
- Then run the four-dimension adversarial review, plus a re-decomposed completeness pass that
  states its own blind spot.

## Out of scope
- Inbound messaging from Jarvis (`BasePlatformAdapter`).
- A Jarvis logo SVG.
- Auto-reading Jarvis's `.env`.
- Background-mode changes on the Jarvis side.
- Fixing the upstream headless approval observation.
