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

## Supported providers (pinned CLI versions)

From `providers/host_runner.py` → `SUPPORTED_CLI_VERSIONS`:

| Provider | Pinned version (acceptance) |
|----------|----------------------------|
| `codex` | `0.144.6` |
| `cursor` | `2026.07.23` |
| `claude` | `2.1.212` |

Version mismatch fails closed during preflight.

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
| Output cap | 262,144 bytes | `DEFAULT_OUTPUT_CAP` |
| Max line bytes | 65,536 | `MAX_LINE_BYTES` |
| Max parsed events | 2,000 | `MAX_EVENTS` |
| Env allowlist | `HOME`, `LANG`, `LC_ALL`, `PATH`, `TERM`, `NO_COLOR` (+ `CURSOR_API_KEY` for cursor) | `SAFE_ENV`, `provider_env_allowlist` |
| Secret redaction | Bearer, API keys, private keys | `SECRET_PATTERN`, `PRIVATE_KEY_PATTERN` |
| Event allowlist | Per-provider `EVENT_TYPES` set | Stream parser rejects unknown events |

Subprocess uses argv arrays only (no shell). Transport uses selector-driven
binary capture with bounded wall time.

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
2. Extend `EVENT_TYPES` and `SUPPORTED_CLI_VERSIONS` with bounded allowlists.
3. Add migration row types if persistence changes (`007_provider_adapters.sql` pattern).
4. Wire worker delivery path only through coordinator commands.
5. Add unit tests in `tests/unit/test_provider_host_runner.py` (mock subprocess).
6. Add bounded live acceptance script hook — **do not** enable by default in Compose.
7. Document pin env example under `docs/provider-<name>-pins.env.example`.
8. Update CHANGELOG under `[Unreleased]` with verification evidence label.

**Do not:** bypass coordinator, add shell-string invocation, project secrets into
env beyond explicit allowlist, or implement automatic paid retry.

---

## CLI binding notes (acceptance)

| Provider | Notable argv / stream behavior |
|----------|-------------------------------|
| Cursor | `--trust` flag for acceptance |
| Claude | `--verbose` stream-json; stdin prompt |
| Codex | `thread.started` / `turn.completed` event family |

Terminal identity accepts `request_id` in stream parser (CHANGELOG 2026-07-28).

---

## See also

- [Domain and lifecycle](domain-and-lifecycle.md) — credits and invocations
- [Architecture and execution paths](architecture-and-execution.md) — worker path
- [Operator runbook](operator-runbook.md) — verification ladder
- [Troubleshooting](troubleshooting.md) — provider timeout / transport
- [`r2-runtime.md`](../r2-runtime.md)
