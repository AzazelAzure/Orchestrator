# Changelog

All notable changes to this repository are documented here. The product ships as
the `flow_engine` Python package (`orchestrator` on PyPI metadata).

## [Unreleased]

### Fixed

- **Bootstrap integration follow-up** — `persist_adapter_snapshot` accepts bootstrap
  handshake fields (`cli_version_pin`, `event_schema`, `execution_profile`); durable
  `binding_digest` and coordinator/worker invoke packets carry `execution_profile`
  end-to-end (`runtime.worker_snapshot` path no longer rejects valid handshakes).
  Invalid or provider-incompatible `execution_profile` values raise
  `ValidationFailedError` (with chaining) so `StateCoordinator.accept` rolls back its
  savepoint and a subsequent valid snapshot on the same connection applies. Settlement
  of already-persisted pre-#33 adapter snapshots uses a deterministic legacy binding
  formula keyed only on exact legacy snapshot field sets; new persistence remains
  strict to the bootstrap schema; hybrid or unknown shapes fail closed.

### Added

- **ORCH-PORTFOLIO-SELF-HOSTING-BOOTSTRAP-2026-08-08** — Governed provider execution
  profiles (`acceptance`, `cursor-implementation`, `claude-independent-review-merge`,
  `codex-admin-reconciliation`) with explicit profile in signed invocation packets;
  product-side `REGISTERED_CLI_VERSIONS` registry with installation `ORCH_PROVIDER_CLI_VERSION`
  pins (Cursor `2026.08.04-aaa8809` / `2026.07.23`, Codex `0.146.0` / `0.144.6`, Claude
  `2.1.212`); immutable pre-invocation git baseline with workspace-root-relative `write_set`
  enforcement (commits and HEAD moves cannot bypass declared paths); live API acceptance
  treats delegation `404 NOT_FOUND` as valid negative result for missing assignments.
- **ORCH-VPS-POST-NET-ATTACH-01** — Portable fixes for remaining VPS runtime friction after
  network-attach deploy: compose service discovery via Podman labels (no `podman-compose ps -q
  <service>` shim); `CMD-SHELL` healthchecks compatible with podman-compose 1.0.6; edge proxy
  reload validates/reloads nginx only (no `fm-beta` stack recreation); VPS
  `DJANGO_ALLOWED_HOSTS` generation merges required presentation aliases without dropping
  operator extras (`scripts/orch_vps_allowed_hosts.sh`).
- **ORCH-VPS-POST-NET-ATTACH-01 (review)** — Allowed-host env rewrite uses line-by-line
  replacement (no `sed` substitution from operator values); direct tests for metacharacter
  preservation and idempotency.

### Added

- **ORCH-VPS-NET-ATTACH-01** — Rootless-native presentation routing without host port
  publish: per-color Podman networks (`orchestrator-console-{color}`) with stable DNS
  aliases (`orch-api-{color}`, `orch-console-{color}`); optional loopback-only diagnostics
  via `ORCH_DIAG_BIND=127.0.0.1` + `docker-compose.bluegreen.diag.yml`; in-container and
  in-network health/smoke probes (`orch_presentation_env.sh`, `ensure_presentation_networks.sh`).
  Supersedes nonviable `ORCH_PUBLISH_HOST` bridge-gateway bind contract from
  ORCH-VPS-PUBLISH-HOST-01.
- **ORCH-VPS-NET-ATTACH-01 (review)** — Console in-container probes prefer `wget`/`curl`
  (nginx:alpine-safe) before Python fallback; `healthcheck.sh` restores exact-one/ambiguous
  compose discovery; generic `ORCH_EDGE_PROXY_PRE_RELOAD_CMD` hook runs before edge-proxy
  nginx validation/reload (`orch_color.sh`, `vps_bootstrap.sh`).
- **ORCH-VPS-NET-ATTACH-01 (review F4)** — `vps_bootstrap.sh all` fails closed when
  `reload_hfm_proxy` fails (required pre-reload hook absent/failing no longer exits 0).

### Removed

- **ORCH-VPS-PUBLISH-HOST-01** — `ORCH_PUBLISH_HOST` / `orch_publish_env.sh` bridge-gateway
  host bind contract (failed rootless bind on VPS; see anomaly
  `orchestrator_vps_materialization_2026-08-02` containment attempt).

### Added

- **ORCH-VPS-PUBLISH-HOST-01** — `ORCH_PUBLISH_HOST` binds presentation ports
  (`8000`/`8010`/`8081`/`8091`) to the installation Podman bridge gateway instead
  of `0.0.0.0`; `orch_publish_env.sh` validation; VPS bootstrap and `generate_vps_env.sh`
  fail closed without explicit publish host; `deploy_ecosystem.sh` renders edge-proxy
  ecosystem hosts to a temp staging file (never the tracked sibling canonical) before
  VPS rsync;
  fixes `ORCH_SCRIPT_IMAGE_DIGEST` in `.env.vps.example`.

### Changed

- **VPS blue/green compose** — `api-blue`/`api-green` ports require
  `${ORCH_PUBLISH_HOST}`; health/smoke scripts probe the configured bind address.

### Added

- **ORCH-VPS-REPAIR-01** — VPS materialization repair: Orchestrator rsync defaults to
  **no-delete** with anchored protected excludes (`.env.vps`, `deploy/vps/.state/`,
  attestations, backups); opt-in `--delete` for Orchestrator only. Pinned Compose
  project/CWD (`ORCH_COMPOSE_PROJECT`, `cd "$ORCH_ROOT"`) in `orch_color.sh`,
  `healthcheck.sh`, and `vps_bootstrap.sh`; exact-one container discovery; blue/green
  `build.context: .`; per-color isolated console networks (`orchestrator-console-{color}`)
  with single `api` alias; strict script-runner/spool-init health semantics; guarded
  fake-binary deploy script tests. Does **not** claim redeploy acceptance or gate closure.

### Changed

- **VPS deploy scripts** — `run_ops_console.sh` drops unused `VITE_API_BASE_URL` build-arg
  (ops-console image is static nginx); `orch_color.sh` smoke adds authenticated console
  `/ops/summary/` proxy check; shared-plane bootstrap runs `healthcheck.sh shared` after up.

### Added

- **ORCH-DOCS-VPS-BG-01** — Authenticated OpenAPI schema/docs (`TEMPLATES` +
  `SERVE_PERMISSIONS`); VPS blue/green presentation tier (`api-blue`/`api-green`,
  per-color ops-console, `orch_color.sh`, `healthcheck.sh`, systemd shared-plane-only
  unit, healthcheck timer); `ORCH_API_BIND` single publish; pure-Python Compose contract
  tests; [`deploy/vps/BLUEGREEN.md`](deploy/vps/BLUEGREEN.md) edge selector contract.
  Materialization preserves public blue; `ORCH_COLOR_MATERIALIZE_ONLY=1` blocks
  `switch`. Orchestrator rollback state under `deploy/vps/.state/`.

### Changed

- **OpenAPI auth** — `/api/schema/` and `/api/docs/` deny anonymous callers
  (**401/403**); bearer receives schema and Swagger HTML **200**. `/health/` remains
  the only anonymous non-auth Django surface.
- **VPS bootstrap** — removed tracked-file `patch_orch_base_ports` sed; shared-plane
  refresh separated from color presentation deploy; legacy `ops-console.service` disabled
  on install.

### Added

- **Codex AM-04 bounded acceptance** — `scripts/provider_live_acceptance.py` and
  `scripts/provider_runtime_acceptance.py` now include `codex` with matrix row
  AM-04; installation-local pin example `docs/provider-codex-pins.env.example`
  (`gpt-5.6-sol`). Acceptance mode, read-only isolation, one call per script, no
  automatic retry (live runs require `.local/provider/codex.pins.env`).

- **Deep documentation set (work item `01KZ08CDQ5KT9W1PTYMMJ0E9CV`)** — Replaced
  surface-level onboarding with source-grounded guides under `docs/guides/` and
  `docs/reference/`: domain/lifecycle, architecture/execution paths, auth/security,
  providers, operator runbook, developer/test guide, troubleshooting playbook, and
  API/CLI/MCP surface reference + glossary. `docs/read-the-docs.md` is now the
  mandatory learning-path router. Layer docs (`architecture.md`, `r2`–`r4`,
  `skills.md`) link into deep pages without contradicting sole-writer, fail-closed
  auth, and local-candidate vs live-acceptance scope labels. Added
  `docs/_audit/check_links.py` for relative link validation. Fresh-review fixes:
  anonymous OpenAPI schema/docs behavior (drf-spectacular `SERVE_PERMISSIONS`),
  `founder.ops` vs coordinator founder-role gate, `mcp/profiles` principal kinds,
  migration `findings` attribution, `FLOW_DB_PATH` citation, six-lane wording in
  `r4-control-plane.md`. Added `test_openapi_schema_anonymous` (anonymous schema **200**)
  and `test_openapi_docs_serve_permissions_allow_any` (docs `AllowAny` at permission
  layer only — no HTTP GET that tolerates template **500**). Anonymous `/api/docs/` may
  still **500** locally when `drf_spectacular/swagger_ui.html` is missing; runtime
  hardening/packaging tracked as a separate work item.

### Fixed

- **Local-stack manifest isolation and stress-script founder auth (ORCH-LI)** —
  `scripts/local_stack_helpers.py::refresh_work_item` now persists to one
  explicit manifest path (the same path its caller loaded from), instead of
  re-resolving `ORCH_LOCAL_STACK_MANIFEST` independently of the in-memory
  manifest; concurrent slices with distinct manifest paths no longer collide
  on one file. `scripts/local_delegation_stress.py` and
  `scripts/local_stress_test.py` now send an authenticated
  `Authorization: Bearer <ORCH_TOKEN_FOUNDER>` request for their ops-summary
  check (mirroring `orchestrator_live_acceptance.py`) instead of an anonymous
  `urlopen`; a missing token or HTTP/auth failure is reported as an explicit
  failed `ops_summary_hierarchy` row with a persisted terminal
  `summary.json`, never as an uncaught exception or an anonymous retry.
  Server-side founder-only enforcement is unchanged.

- **Founder registration over coordinator HTTP** — `POST /api/v1/auth/register` with a
  founder bearer now forwards `principal_token` to the coordinator so
  `auth.register_user` resolves founder authority server-side instead of
  unconditionally executing as `SYSTEM`. Serialized `context.role` and payload
  bypass keys remain stripped at the HTTP boundary.

### Added

- **User authentication + CLI login** — human accounts mapped to `human` principals;
  Django password hashes; opaque digest-only access/refresh/PAT credentials with
  rotating refresh and replay-safe family revocation; coordinator-durable login
  throttle (gunicorn multi-worker safe); JSON `/api/v1/auth/*`; gated `/ops/summary/`
  (founder or `ops.read`); ops-console generic bearer token field; `flowctl auth
  login|logout|status|token` with `0600` credential store and `ORCH_USER_TOKEN`
  support (no secret echo by default). Migration `008_user_auth.sql` rebuilds the
  principals CHECK constraint (forward-only; rollback = restore SQLite backup).
  Registration defaults **off** via `ORCH_ALLOW_USER_REGISTRATION=0`. No JWT
  dependency.

- **Compose auth env propagation** — `docker-compose.yml` now projects
  `ORCH_ALLOW_USER_REGISTRATION` into `api` and `coordinator` (default `:-0`) and
  credential TTL / login-throttle settings into `coordinator` only. VPS overlay
  pins registration off with literal `"0"` on both services.

- **skills-scripts MCP lane** — sixth product MCP lane for publication-neutral skill/script catalog reads (`list_skills`, `get_skill`, `list_scripts`, `describe_script`) and `request_script_run` → coordinator `script.register` under initiating-principal dual-auth (never MCP-side shell or direct `script.execute`). Catalog, Compose service, bootstrap token env, and unit coverage included (`agentic/catalogs/mcp_lanes.json`, `src/flow_engine/mcp_lanes/`, `tests/unit/test_skills_scripts_mcp_lane.py`).
- **`scripts/lib/http_wait.sh`** — sourceable HTTP readiness helper (`wait_http` returns nonzero on timeout; `fail` remains the fatal abort path). Optional `ORCH_HTTP_PROBE_SLEEP` (default `2`) for deterministic probes. Covered by `tests/unit/test_local_stack_up.py`.

### Changed

- **`scripts/local_stack_up.sh`** — existing-stack health probe is non-fatal so a stale manifest with an unhealthy API reaches the rebuild path; post-Compose readiness still fails fatally via `wait_http … || fail "…"`. Env/token reuse and Compose semantics unchanged.

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
