# Provider integration

**Audience:** operators running bounded live acceptance and contributors adding
provider bindings safely.

**Scope:**

| Mode | `ORCH_PROVIDER_MODE` | What runs |
|------|---------------------|-----------|
| Mock (default local) | `mock` | In-process mock runners; no external CLI |
| Host runner (local acceptance) | configured per script | Unix-socket `host_runner.py` invoking installed CLIs |
| Production / VPS | **Not documented as landed** | Overlay env examples only |

**Source anchors:** `providers/host_runner.py`, `providers/protocol.py`,
`persistence/migrations/007_provider_adapters.sql`, `docs/provider-*-pins.env.example`,
`scripts/provider_live_acceptance.py`, `scripts/provider_runtime_acceptance.py`.

---

## Provider protocol overview

```
Coordinator/worker                Host runner (subprocess)           Provider CLI
      │                                  │                              │
      │  prepare / deliver / heartbeat     │                              │
      ├─────────────────────────────────►│  argv + env allowlist        │
      │                                  ├─────────────────────────────►│
      │                                  │◄── stream-json events ───────┤
      │◄──────── signed result ──────────┤                              │
```

Provider I/O runs **outside** the SQLite transaction after durable dispatch intent
is recorded (`application/worker_delivery.py`).

---

## Supported providers (registered CLI versions)

From `providers/cli_registry.py` → `REGISTERED_CLI_VERSIONS`. Installation pins select
**one exact registered version** via `ORCH_PROVIDER_CLI_VERSION` in
`.local/provider/<provider>.pins.env` (examples in `docs/provider-*-pins.env.example`).
Model pins (`ORCH_PROVIDER_MODEL`) remain separate. Handshake requires the probed
`--version` output to match the pin exactly and maps the pin to a registered event schema.

| Provider | Registered CLI versions |
|----------|---------------------------|
| `codex` | `0.144.6`, `0.146.0` |
| `cursor` | `2026.07.23`, `2026.08.04-aaa8809` |
| `claude` | `2.1.212` |

Unknown or mismatched pins fail closed during handshake.

---

## Execution profiles

Profiles are explicit in the host-runner binding (`ORCH_PROVIDER_PROFILE`) and must
match the signed invocation packet `execution_profile` field. Unknown or provider-incompatible
profiles are denied; acceptance is never silently upgraded.

**Adapter snapshot schema (persistence vs settlement):** new `runtime.worker_snapshot`
persistence requires the full bootstrap field set (including `cli_version_pin`,
`event_schema`, and `execution_profile`). Rows already persisted before bootstrap
#33 with only the legacy snapshot keys settle using the pre-#33 binding digest
formula (no `execution_profile` in the binding). Schema choice is deterministic from
the stored snapshot keys: exact bootstrap set, exact legacy set, or fail closed on
hybrid/unknown shapes — no silent default profile.

| Profile | Providers | Behavior |
|---------|-----------|----------|
| `acceptance` (default) | all | Isolated empty read-only workspace; bounded argv |
| `cursor-implementation` | `cursor` | Repository worktree; default write mode + `--force`; requires `write_set` |
| `claude-independent-review-merge` | `claude` | Trusted authorized review role: Bash allowed for tests/gh; disallows Edit/Write only; records git mutations without blocking gh |
| `codex-admin-reconciliation` | `codex` | Read-only sandbox (same argv bounds as acceptance) |

Repository campaigns use clean per-slice worktrees. Profiles that require git evidence
capture an immutable pre-invocation baseline and compare all workspace-root-relative
paths (committed, staged, unstaged, untracked, renames, deletions). `write_set` may
include `.` for the whole confined workspace. HEAD moves and commits cannot bypass
declared paths. Linked worktrees and cwd subdirectories resolve against `workspace_root`;
baseline capture fails closed when git evidence cannot be established.

The Claude independent-review/merge profile is a **trusted authorized role**: Bash remains
available for tests and `gh` review/merge; it is not a filesystem sandbox. Host-runner
invocations pass `--permission-mode bypassPermissions` so authorized Bash/`gh` calls run
non-interactively in headless operation (no interactive approval prompts). The
`acceptance` profile does **not** receive this bypass — it stays tool-disallowed and
isolated. Git baseline diffs are recorded for workspace mutations without failing
authorized `gh` operations.

---

## Bindings and model pins

Installation-local pin files (examples only — copy and customize outside git):

- `docs/provider-cursor-pins.env.example`
- `docs/provider-claude-pins.env.example`

Cursor acceptance loads `CURSOR_API_KEY` from installation-local
`.local/provider/cursor.env` (`providers/cursor_env.py`). Never commit keys.

Model pins are enforced at dispatch; hash mismatch denies fail-closed (R3 dispatch
pins — see [`r3-organization.md`](../r3-organization.md)).

---

## Host runner safety bounds

| Control | Value / behavior | Source |
|---------|------------------|--------|
| Max frame bytes | 1,048,576 | `MAX_FRAME_BYTES` |
| Redacted evidence cap | 262,144 bytes | `DEFAULT_OUTPUT_CAP` / `binding.output_cap` |
| Coordinator provider-result cap | 524,288 bytes | `worker_delivery` settlement validation |
| Stderr cap | 262,144 bytes | `DEFAULT_STDERR_CAP` |
| Max line / event bytes | 65,536 (acceptance, `codex-admin-reconciliation`); 524,288 (`cursor-implementation`, `claude-independent-review-merge` tool-result lines) | `MAX_LINE_BYTES`, `AGENTIC_MAX_EVENT_LINE_BYTES` |
| Max parsed events | 2,000 | `MAX_EVENTS` |
| Env allowlist | `HOME`, `LANG`, `LC_ALL`, `PATH`, `TERM`, `NO_COLOR` (+ `CURSOR_API_KEY` for cursor) | `SAFE_ENV`, `provider_env_allowlist` |
| Secret redaction | Bearer, API keys, private keys | `SECRET_PATTERN`, `PRIVATE_KEY_PATTERN` |
| Event allowlist | Per-provider `EVENT_TYPES` set | Stream parser rejects unknown events |

Stdout is parsed incrementally (newline-delimited JSON). Nonterminal transcript volume
may exceed the evidence cap without terminating the provider; oversized single events,
unknown event types, and event-count overflow after subprocess dispatch terminate the
provider when needed, persist durable `outcome_unknown` with an `A3` anomaly, and never
automatically replay the invocation. Terminal stream events are retained in
`redacted_output` even when earlier evidence is truncated. `truncated=true` records
partial evidence; outcome stays `complete` when stderr is bounded and terminal identity
is present.

Subprocess uses argv arrays only (no shell). Transport uses selector-driven incremental
parse with bounded wall time.

---

## Credit settlement (no silent retry)

1. **Reserve** credits before dispatch (`application/credit_service.py`).
2. Dispatch provider invocation (`InvocationStatus`: `reserved` → `dispatched`).
3. Terminal success/failure or `outcome_unknown` **settles** (consumes) reservation.
4. **No automatic paid retry** after `outcome_unknown`.
5. Founder step-up `runtime.new_attempt_after_unknown` required for a new paid attempt.

Acceptance campaign defaults: 9 total credits, 3 per provider (`domain/credits.py`).
All runs in one campaign share the same explicit `--budget-scope-id`.

---

## Bounded acceptance scripts

| Script | Purpose |
|--------|---------|
| `scripts/provider_live_acceptance.py` | One minimal real CLI call per provider in isolated acceptance mode |
| `scripts/provider_runtime_acceptance.py` | AM-05/06 through coordinator/worker_delivery with credit reserve/settle |
| `scripts/verification_ladder.py` | L1–L4 ladder; writes `.tmp/verification-ladder/<run_id>/summary.json` |

**Local candidate only.** Passing bounded acceptance does not close external
governance gates or assert VPS readiness.

Verified snapshot (CHANGELOG): Cursor (`composer-2.5`) and Claude
(`claude-opus-4-8`) green on 2026-07-28 run; Codex deferred.

---

## Evidence and redaction

- Invocation packets canonicalize without credential keys or private absolute paths
  (`canonical_invocation_packet` in `host_runner.py`).
- Stream events filtered to provider-specific allowlists before persistence.
- Script and schedule result schemas apply recursive secret redaction
  (`script_sandbox/`, `schedules/`).

Evidence artifacts land under `.tmp/` run directories (gitignored). Ops summary
may surface latest ladder/probe JSON read-only (`control_plane/api/ops_urls.py`).

---

## Mock provider path (default Compose)

When `ORCH_PROVIDER_MODE=mock`, Celery worker uses in-tree mock delivery
(`workers/tasks.py`) — no host runner socket, no external API spend.

Use mock mode for:

- `pytest` (deterministic, no credentials)
- R4D active-test harness (`scripts/r4d_active_test.sh`)
- Daily development without provider keys

---

## Safely adding a provider (contributor checklist)

1. Add protocol handler implementing `providers/protocol.py` contracts.
2. Extend `EVENT_TYPES` and `REGISTERED_CLI_VERSIONS` with bounded allowlists.
3. Add migration row types if persistence changes (`007_provider_adapters.sql` pattern).
4. Wire worker delivery path only through coordinator commands.
5. Add unit tests in `tests/unit/test_provider_host_runner.py` (mock subprocess).
6. Add bounded live acceptance script hook — **do not** enable by default in Compose.
7. Document pin env example under `docs/provider-<name>-pins.env.example`.
8. Update CHANGELOG under `[Unreleased]` with verification evidence label.

**Do not:** bypass coordinator, add shell-string invocation, project secrets into
env beyond explicit allowlist, or implement automatic paid retry.

---

## CLI binding notes (by profile)

| Provider | Profile | Notable argv / stream behavior |
|----------|---------|-------------------------------|
| Cursor | `acceptance` | `--mode ask`, `--trust` |
| Cursor | `cursor-implementation` | Default write mode (no `--mode`; CLI permits only `plan`/`ask`); `--force` |
| Claude | `acceptance` | All tools disallowed via `--disallowedTools`; `--max-turns 8`; `--max-budget-usd 1.00` |
| Claude | `claude-independent-review-merge` | Disallows Edit/Write only; `--permission-mode bypassPermissions` (headless authorized Bash/gh); `--max-turns 32`; `--max-budget-usd 4.00` |
| Codex | `acceptance` | `--skip-git-repo-check` (isolated empty non-git workspace), `--sandbox read-only` |
| Codex | `codex-admin-reconciliation` | `--sandbox read-only` (no `--skip-git-repo-check`) |
| Claude | all | `--verbose` stream-json; stdin prompt; terminal `result` subtypes:
  `success` (complete when identity present) or error family
  (`error_during_execution`, `error_max_turns`, `error_max_budget_usd`,
  `error_max_structured_output_retries` — reconciliation required) |
| Cursor | all | Observed `cursor-events-v1` types: `system`, `user`, `assistant`,
  `tool_call`, `result`, `error`, `thinking` |
| Codex | all | `thread.started` / `turn.completed` event family |

Terminal identity accepts `request_id` in stream parser (CHANGELOG 2026-07-28).

---

## See also

- [Domain and lifecycle](domain-and-lifecycle.md) — credits and invocations
- [Architecture and execution paths](architecture-and-execution.md) — worker path
- [Operator runbook](operator-runbook.md) — verification ladder
- [Troubleshooting](troubleshooting.md) — provider timeout / transport
- [`r2-runtime.md`](../r2-runtime.md)
