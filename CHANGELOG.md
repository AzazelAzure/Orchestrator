# Changelog

All notable changes to this repository are documented here. The product ships as
the `flow_engine` Python package (`orchestrator` on PyPI metadata).

## [Unreleased]

### Added

- **R1 catalogs** — agentic catalog generation/validation, loadout bundles, and baseline activation assets (`agentic/`, `docs/r1-assets.md`).
- **R2 runtime** — persistent worker delivery, credit reservations, recovery paths, and migration `003_r2_runtime.sql` (`docs/r2-runtime.md`).
- **R3 organization** — delegation, loadout resolution, org CLI, migration `004_r3_organization.sql` (`docs/r3-organization.md`).
- **R4 local control plane** — DRF API, sole-writer coordinator, Redis/Celery mock delivery, five MCP lanes, script sandbox, Manila schedules, Compose/Podman active-test harness (`docs/r4-control-plane.md`, `deploy/`, `docker-compose.yml`).
- **Provider host-runner adapters** — authenticated Unix-socket host runner for installed Codex/Cursor/Claude CLIs; migration `007_provider_adapters.sql`; installation-local pin examples (`docs/provider-*-pins.env.example`).
- **Bounded live acceptance** — `scripts/provider_live_acceptance.py` for one minimal real CLI call per provider in isolated acceptance mode.
- **Orchestrator executive skills** — positional skill packages under `skills/` (admin-ops, admin-tech, admin-qa, and worker-loop skills).

### Changed

- **Provider CLI bindings** — Cursor acceptance adds `--trust`; Claude uses `--verbose` stream-json with stdin prompt; expanded stream event allowlist; terminal identity accepts `request_id`.
- **Cursor env bootstrap** — `CURSOR_API_KEY` loaded from installation-local `.local/provider/cursor.env`; `HOME` added to subprocess allowlist.
- **Claude CLI pin** — supported CLI version updated to `2.1.212`.
- **R4D evidence harness** — redelivery at-loss/finalize snapshots, restart continuity pre/post, teardown zero-state capture, compose-config rendered at mode `0600`.

### Verified (2026-07-28)

- Full suite: **353 passed, 1 skipped**.
- Provider bounded live acceptance run `accept-20260728T003246Z`: **Cursor** (`composer-2.5`) and **Claude** (`claude-opus-4-8`) green on all checks.
- R4D remediated active test run `r4d-20260728T012703Z-852954`: **13 steps green**, teardown `zero_state: true`, remediated redelivery/restart evidence captured.
- Codex live acceptance: **not run** (deferred).

- **Verification ladder** — `scripts/verification_ladder.py` runs L1 (flowctl), L2 (DRF/delivery pytest subset), L3 (`r4d_verify`), L4 (provider runtime envelope); writes `.tmp/verification-ladder/<run_id>/summary.json`.
- **Coordinator-path runtime acceptance** — `scripts/provider_runtime_acceptance.py` exercises AM-05/06 through coordinator/worker_delivery with credit reserve/settle (not host-runner-only).

### Changed

- **`runtime_service._row_to_invocation`** — exposes `invocation_packet_json` for worker preflight payload authorization.

### Verified (2026-07-28, continued)

- Verification ladder L1–L3 green; L4 runtime acceptance `runtime-20260728T033356Z` — AM-05/06 passed with credit ledger.
- `tests/unit/test_verification_ladder.py`: 5 passed.

### Open / not claimed

- **Gates (HitM 2026-07-28):** provider adapters **partial close** (Cursor+Claude; Codex waived NLT review 2026-08-05); script sandbox and scheduled maintenance **closed**; local control plane **open** (AM-10 security review).
- **Codex** — pin and live call deferred; review NLT 2026-08-05.
