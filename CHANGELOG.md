# Changelog

All notable changes to this repository are documented here. The product ships as
the `flow_engine` Python package (`orchestrator` on PyPI metadata).

## [Unreleased]

### Added

- **skills-scripts MCP lane** — sixth product MCP lane for publication-neutral skill/script catalog reads (`list_skills`, `get_skill`, `list_scripts`, `describe_script`) and `request_script_run` → coordinator `script.register` under initiating-principal dual-auth (never MCP-side shell or direct `script.execute`). Catalog, Compose service, bootstrap token env, and unit coverage included (`agentic/catalogs/mcp_lanes.json`, `src/flow_engine/mcp_lanes/`, `tests/unit/test_skills_scripts_mcp_lane.py`).

### Changed

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

### Added (continued)

- **R5 dogfood evidence** — `scripts/r5_dogfood_evidence.py` captures L5 generic proof slices (delegation, mechanical gate, conference references).
- **R6 external adapter proof evidence** — external read-only status stub script; writes `.tmp/r6-external-adapter/<run_id>/`.

### Verified (2026-07-28, R5/R6)

- R5 dogfood run `r5-20260728T041448Z`: 7/7 evidence rows pass (supplementary capture).
- R6 external adapter run `r6-external-adapter-20260728T041642Z`: adapter stub contract green.

### Open / not claimed

- **G-ORCH-PROOF-GENERIC** and **G-ORCH-PROOF-PORTFOLIO** — gate close requests prepared; HitM disposition required.
- **G-ORCH-LOCAL-CONTROL-PLANE** — open (AM-10 security review).
