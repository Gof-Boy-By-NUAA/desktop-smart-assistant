# A05 Findings Forensic Investigation — SmartAssistant RC1 (2d38cb3)

Investigation only. No source, configuration, harness, build, dependency or VM files were modified. The only artifact created by this investigation is this report.

Investigated tree (frozen RC1 source, read-only):
`C:\Users\UnlimitedPower\Documents\桌面智能助手\SmartAssistant-b01-r2-rc1` (commit `2d38cb386b404554df268b4fef4730a595734237`, installer SHA256 `965598c2…`).

---

# Finding Summary

| ID | Finding | Classification | Severity |
|---|---|---|---|
| R-001 | External PowerShell probe receives HTTP 401 with empty body against the packaged backend | **Acceptance tooling issue — NOT a product defect.** Trusted-transport security working as designed | None (product) |
| R-002 | Cancel evidence `cancellation_requested=1, local_signal=0`: durable cancel intent recorded while the in-process cancel accelerator found no owned token | **Product defect (latent lifecycle/race window)**: cancel can be accepted durably yet deliver no immediate signal to a running turn; interruption then depends on later fence checkpoints only | Medium |
| R-003 | Secret logging exposure | **Low-severity product logging hygiene defects** on two conditional error paths; the known startup config log is properly masked | Low |
| R-004 | `a05-observe.ps1` first run `backend=0, discovery=BLOCKED`, later `backend=1, discovery=PASS` | **Acceptance tooling timing issue** (point-in-time snapshot, no readiness wait) + normal cold-start latency; NOT a product startup defect | None (product) |

---

# Evidence

## R-001 — Desktop Authentication Probe Boundary

- `channel/web/web_channel.py:4014-4033` — in desktop mode (`COW_DESKTOP=1`, always true for the installed app) the entire WSGI app is wrapped OUTERMOST by `DesktopRequestAuthMiddleware`; source comment: *"Keep this wrapper outermost so static assets, health and every API route reject direct loopback callers before application code."*
- `channel/web/desktop_protocol.py:42-47` — the exact four required headers: `X-Cow-Desktop-Launch-Id`, `X-Cow-Desktop-Timestamp`, `X-Cow-Desktop-Nonce`, `X-Cow-Desktop-Mac`.
- `channel/web/desktop_protocol.py:513-560` — `DesktopRequestAuthMiddleware`: `_header_values` requires EXACTLY the four headers (missing or extra → `DesktopProtocolError`); `_deny` answers `401 Unauthorized`, `Content-Length: 0`, empty body — the precise form observed on the VM (`LOGIN_HTTP_STATUS=401`).
- `channel/web/desktop_protocol.py:576-603` — timestamp window, nonce replay cache, launch-id and HMAC-MAC validation.
- `desktop/src/main/python-manager.ts:489-490` — `launchId = crypto.randomBytes(24)`, `secret = crypto.randomBytes(32)`: per-launch 32-byte secret generated only inside the Electron main process.
- `desktop/src/main/python-manager.ts:535-548` — the secret reaches the backend child ONLY via the bootstrap control frame over the inherited fd; per-launch ephemeral TLS certificate pinned by fingerprint.
- `desktop/src/main/python-manager.ts:696-702` — Electron main signs every backend request with the four `X-Cow-Desktop-*` headers (`canonicalRequestMac`) over `https://127.0.0.1:<port>`.
- `channel/web/web_channel.py:4409-4460` — `AuthLoginHandler` has **no 401 path at all** (wrong password → HTTP 200 + `{"status":"error","message":"Wrong password"}`; rate limit → 429; malformed body → 200 + JSON error). Therefore the observed 401 could not have originated in the login handler — it is the middleware.
- `channel/web/web_channel.py:4109-4114` — even `/api/health` is 401 for direct loopback callers in desktop mode (corroborating "every route").
- VM observations (operator transcripts, A05 evidence): run 1 failure = plaintext `http://` against a TLS-only listener (transport error, no HTTP status); run 2 = `https://` + process-scoped trust → `LOGIN_HTTP_STATUS=401`, password never evaluated.

## R-002 — Agent Cancellation Lifecycle

Evidence signature `cancellation_requested=1, local_signal=0` originates from the request-scoped Cancel-button handler (not the `/cancel` text command — that path, `web_channel.py:2936-2966`, derives both counters from the same registry call and cannot produce 1/0):

- `channel/web/web_channel.py:3700-3795` — cancel handler:
  - `durable_store.request_execution_cancellation(request_id, owner_id)` → state `"requested"` ⇒ `cancellation_requested = 1`;
  - `registry.cancel_request_owned(request_id, owner_id)` → miss ⇒ `local_signal_count = 0` (no `threading.Event` set).
- `channel/web/sse_persistence.py` (`request_execution_cancellation`): `queued` → terminally `cancelled`; **`running` → persistent intent only (`requested`)** — *"the live fence holder must acknowledge it at its next safe checkpoint"*; terminal states reject.
- `agent/protocol/cancel.py:18-160` — cooperative model: in-process registry of `threading.Event`s, polled at "safe checkpoints"; entries released after run completion.
- `bridge/agent_bridge.py:886-908` — token registration at turn entry: `token_key = request_id or f"{session_id}:run:{uuid4}"`, `owner_id = context["session_owner_id"]`; registration happens BEFORE the session-run-lock wait so queued turns are cancellable; `web_channel.py:2121/2126` set `session_owner_id`/`request_id` from the authenticated principal — key and owner forms are consistent on the normal Web path.
- `bridge/agent_bridge.py:907-908, 979-980, 1227` — unregister on early-cancel, normal and error paths (idempotent; cleanup present).
- Interruption points actually wired: `agent/protocol/agent_stream.py:393-399` (`_raise_if_cancelled` poll), `agent_stream.py:1645` (raise during LLM streaming — checked **per stream chunk**), `agent/protocol/agent.py:446,463` (post-process tool boundaries), `bridge/agent_bridge.py:121-135` (durable-fence polls around tool execution).
- Blocking characteristics: the agent turn runs on a web.py worker thread; no `asyncio`, `asyncio.to_thread` or `run_in_executor` exists anywhere in `agent/`, `bridge/`, `channel/web/` (grep verified) — the task-hint async concerns reduce to *cooperative polling vs blocking SDK iterators* (a stalled provider stream delays cancel until the next chunk or boundary). The per-session `threading.Lock` (`agent_bridge.py:430-455`) blocks queued turns with no timeout, but queued turns are registered pre-wait and terminally cancelled via the durable store, so queue cancellation is sound.
- Cross-evidence: the A03-RC1/A05-visible error *"执行结果不确定（Agent execution interrupted before durable completion…）"* is produced by `bridge/agent_bridge.py:1066-1073` — ANY non-`AgentCancelledError` exception after `agent_run_entered=True` (including the blank-response guard `RuntimeError("Agent returned no final response after execution")`, `agent_bridge.py:1036-1044`) settles the durable claim as `in_doubt` with exactly that detail string.

## R-003 — Secret Logging Exposure

- `config.py:359-406` — the startup `load config: {...}` log (`config.py:483`) routes through `_mask_sensitive_recursive`: any key containing `key|secret|password|token|credential|bearer` is masked to `first3 + "*****" + last3`. Verified against observed log output (`'zhipu_ai_api_key': '15a*****vZH'`). Masking is by key-name substring and recursive — coverage is good; residual exposure is 3+3 characters per credential (by design).
- **Leakage path 1 (conditional):** `agent/tools/mcp/mcp_oauth.py:457` — `logger.warning(f"[MCP-OAuth:{self.server_name}] token response missing access_token: {resp}")` dumps the **entire token-exchange response dict**. If a provider returns `refresh_token`/`id_token` or partial credential material without `access_token`, raw values land in the log.
- **Leakage path 2 (conditional):** `channel/dingtalk/dingtalk_message.py:168` — `logger.error(f"[DingTalk] Failed to get access token: {token_data}")` dumps the full token JSON on the "missing accessToken" branch (HTTP 200 with unexpected shape).
- Provider request/exception logging: `models` handler logs key NAMES only (`provider zhipu updated: ['zhipu_ai_api_base', 'zhipu_ai_api_key']`, `web_channel.py:6288`); provider SDK exception texts (observed 401 bodies) contain no local key material.
- Desktop main process: backend log lines route to `console.log` (`desktop/src/main/index.ts:689-690`) — process stdout only, not persisted by the app; the only main-process file sinks are the updater log (`updater.ts:108`) and window-state JSON (`index.ts:544`), neither of which carries credentials. The persisted backend log is `.cow/run.log` (the file covered by the two conditional paths above).
- No hardcoded credential literals exist in the tree (consistent with the B01/B01-R2 chain; grep for credential-shaped literals returned none).

## R-004 — A05 Startup Observation

- `C:\A05-Acceptance\Transfer\a05-observe.ps1` (harness, host copy): a **point-in-time snapshot** — one `Get-CimInstance` process enumeration; if `smart-assistant-backend.exe` is absent at that instant, `backend_count=0`, `backend_pid=null` ⇒ `discovery=BLOCKED`. There is no poll/wait loop.
- Product startup sequence (source): Electron main spawns the bundled backend (`python-manager.ts`), which must complete Python interpreter boot, config load, plugin init and the trusted-transport bootstrap, then emit the ready frame before the listener appears; on a clean VM with cold file cache and antivirus scanning the ~325 MB backend bundle, first-launch latency is naturally seconds-to-tens-of-seconds.
- Observed sequence (`backend=0/BLOCKED` → later `backend=1/PASS`) proves the backend DID start; the first observation simply raced the spawn.

---

# Root Cause

## R-001
The installed desktop backend is protected by per-launch TLS + HMAC-authenticated trusted transport (`DesktopRequestAuthMiddleware`, outermost WSGI wrapper). An external raw-HTTP client (PowerShell) cannot possess the per-launch `desktop_secret` — which exists only in Electron main memory and the backend's inherited control pipe — and therefore can never reach `/auth/login`, `/api/sessions`, or any route. The observed 401/empty-body is the middleware's designed rejection of an unsigned loopback caller. **Product security architecture working as intended; the probe's expectation (raw-HTTP login + cookie replay) is architecturally invalid for the packaged desktop mode.** Correct verification boundary for external acceptance: real-UI logout behavior (LoginGate restoration, restart-stays-logged-out, relogin). Low-level old-session 401 semantics are only reachable through the in-renderer trusted channel (as done in A02-RC1 on the same artifact); GUI automation is prohibited in A05, so that sub-probe is `BLOCKED_BY_DESIGN`.

## R-002
The `(cancellation_requested=1, local_signal=0)` signature means: the durable store saw the execution row in state `running` and persisted a cancellation INTENT, while the in-process cancel registry held no token for `(request_id, owner_id)` — so no `threading.Event` was ever set for a live turn. Because queued turns register before waiting and are terminally cancelled, and because key/owner forms are consistent on the normal Web path, the signature arises from a lifecycle mismatch between the durable row and the registry entry:

1. **Settle-lag race** — the turn is finishing: token already unregistered (or about to be) while the row is still `running` (settlement not yet committed). Cancel lands in the gap: accepted durably, delivered to nobody; the turn completes naturally.
2. **Stale `running` row across backend restart** (fence leak) — a crash/relaunch leaves `running` in SQLite; a later cancel targets a row with no live process token: `requested=1, local_signal=0`, and the stale fence later surfaces as `in_doubt` ("interrupted before durable completion") via the BaseException settlement (`agent_bridge.py:1066-1073`) or dispatcher fencing.
3. **Owner/key-form mismatch** on non-Web-originated turns (scheduler-dispatched or plugin-bypassed executions that lack the authenticated `session_owner_id`).

Consequence chain: Cancel button → durable intent recorded (UI: accepted) → **no immediate in-process signal** → interruption of a genuinely running turn depends solely on the next durable-fence checkpoint (tool boundaries, `agent_bridge.py:121-135`) or per-chunk event poll (`agent_stream.py:1645`, which never fires because the event was never set) → if no checkpoint occurs, the cancel is a silent no-op for the live execution; collisions around settlement surface as the user-visible "执行结果不确定（Agent execution interrupted before durable completion）" error. Correctness is preserved (fail-closed `in_doubt` fencing, no history corruption — cleanup paths verified present and idempotent), but cancel reliability and UX are compromised. Contributing amplifier: cooperative polling cannot preempt a blocked provider SDK iterator (no hard stream/socket abort), lengthening the window in which a cancel has no observable effect.

## R-003
Leakage paths are conditional logging of raw credential-bearing response objects on error branches (`mcp_oauth.py:457`, `dingtalk_message.py:168`) — a dict-formatting hygiene gap, not an architectural exposure. The primary startup config log is masked by design; desktop main persists no credentials.

## R-004
Harness timing: the observation script is a single instantaneous snapshot with no readiness wait, executed during the (normal, slower on cold clean VM) backend spawn window. The product's startup itself succeeded.

---

# Affected Files

Product (RC1 source, investigation-identified; **not modified**):
- `channel/web/web_channel.py` — cancel handler semantics (R-002 evidence), middleware wrap (R-001 evidence)
- `channel/web/sse_persistence.py` — `request_execution_cancellation` state machine (R-002)
- `agent/protocol/cancel.py` — cooperative registry (R-002)
- `bridge/agent_bridge.py` — token lifecycle, fence polls, `in_doubt` settlement (R-002)
- `agent/protocol/agent_stream.py` — cancellation checkpoints (R-002)
- `agent/protocol/agent.py` — post-process tool boundary checks (R-002)
- `agent/tools/mcp/mcp_oauth.py:457` — conditional token-response dump (R-003)
- `channel/dingtalk/dingtalk_message.py:168` — conditional token_data dump (R-003)
- `config.py:359-406` — masking (R-003, verified adequate; residual 3+3 chars by design)
- `channel/web/desktop_protocol.py`, `desktop/src/main/python-manager.ts` — R-001 architecture (working as designed; no change warranted)

Acceptance harness (not product):
- `C:\A05-Acceptance\Transfer\a05-session-401-probe.ps1` — R-001: design invalid for desktop trusted transport (VM copy additionally modified during diagnosis; canonical SHA256 `20745b77…`; modified copy + backup preserved as diagnostic evidence)
- `C:\A05-Acceptance\Transfer\a05-observe.ps1` — R-004: missing readiness poll

---

# Risk Assessment

- **R-001** — no product risk. Risk only if remediation tried to extract the transport secret or weaken the middleware: would convert a security feature into a test backdoor. Must not happen.
- **R-002** — Medium. User-facing cancel can be silently ineffective against a running turn; late/stale fences surface as "执行结果不确定" errors (observed in A03-RC1/A05). No data-corruption path found (fail-closed `in_doubt`; cleanup verified). Escalates under slow providers (long blocking iterators widen the no-op window) and after backend crashes (stale `running` fences).
- **R-003** — Low. Conditional paths (malformed OAuth/token responses) in optional subsystems (MCP OAuth, DingTalk channel); requires an unusual provider response to trigger; impact limited to local `.cow/run.log`.
- **R-004** — no product risk; acceptance-run friction only (false BLOCKED on first observation).

---

# Recommended Remediation

(Recommendations only — nothing implemented in this phase.)

1. **R-001 (semantics, not code):** amend A05 acceptance semantics for the revocation sub-gate: record `A05_DIRECT_OLD_SESSION_REPLAY = BLOCKED_BY_DESIGN` with the cross-stage evidence combination (A05 direct real-UI logout verification + A02-RC1 same-artifact in-renderer 401 verification + A04 artifact correspondence binding); promotion decision belongs to the independent final audit. Archive the VM-modified probe script and its backup as diagnostic evidence; restore the canonical probe copy and verify SHA256 equality before final manifest.
2. **R-002 (RC2 candidates, `agent_bridge` / `sse_persistence` / `agent_stream` layer):**
   - When durable state resolves to `requested` but the registry miss occurs, actively drive the fence instead of waiting for the next tool boundary: have the dispatcher/guard re-verify (`verify_now()`-style) and raise `AgentCancelledError` at the earliest safe checkpoint of the executing turn.
   - Close the settle-lag window: order durable settlement strictly before token unregister, and re-check the durable cancellation intent at unregister time (turn a gap-race cancel into a clean terminal state).
   - Reconcile registry entries against durable `running` rows at dispatcher cadence to detect and fence stale rows after restarts (fence-leak hygiene).
   - Evaluate aborting the underlying provider HTTP stream on cancel (close the response/socket) so cooperative polls are not hostage to a stalled iterator.
3. **R-003 (RC2 candidates, logging hygiene):** redact response objects before logging at `mcp_oauth.py:457` and `dingtalk_message.py:168` (log key names/status only, as the models handler already does). Optionally tighten `_mask_value` beyond 3+3 retained characters.
4. **R-004 (harness only):** add a bounded readiness poll (e.g., up to 60 s, 2 s interval) for the backend process/listener in `a05-observe.ps1`; alternatively document "run after UI fully loaded" in operator instructions. No product change.

---

# Files Must Not Be Modified

- The entire frozen RC1 source tree: `C:\Users\UnlimitedPower\Documents\桌面智能助手\SmartAssistant-b01-r2-rc1\` (commit `2d38cb3`; any change requires RC2).
- All sibling worktrees and branches (`SmartAssistant-b01-r2`, `SmartAssistant-fix-current`, `SmartAssistant-product-d60e99f`, acceptance worktrees) and their git history.
- All sealed/historical evidence directories, including but not limited to:
  - `…\SmartAssistant-fix-current\acceptance\evidence\b01-r2-toolchain-freeze-20260925-132630\`
  - `…\b01-r2-e1-evidence-closure-20260926-013717\`
  - `…\b01-r2-e2-classification-closure-20260926-110500\`
  - `…\b01-r2-e2-p0-interface-discovery-20260926-113237\`
  - `…\b01-r2-e2-a1-auditor-semantics-20260926-115200\`
  - `C:\A02-Acceptance\Evidence\`, `C:\A02-RC1-Acceptance\Evidence\`, `C:\A03-Acceptance\Evidence\`, `C:\A03-RC1-Acceptance\Evidence\`
  - `…\acceptance\evidence\a05-external-20260926-140833\` (append-only; subject to its own run's integrity rules)
- The A05 transfer package canonical copies: `C:\A05-Acceptance\Transfer\` (hash-of-record: installer `965598c2…`, probe `20745b77…`).
- All VM-side files and the VM itself (no modification, no cleanup beyond the already-disclosed operator diagnostic edits, which are preserved as evidence).
- The frozen RC1 installer binary and any packaged artifact (no rebuild, no patching).
