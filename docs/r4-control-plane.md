# R4 local control plane

R4A adds a Django REST Framework API, sole-writer coordinator HTTP service,
Redis/Celery mock provider delivery, and Docker Compose for local active testing.

R4B adds five capability-scoped MCP lane services with distinct identities.
Every lane tool calls DRF with both initiating-principal and MCP service-principal
identity, enforces exact catalog tool snapshots, and denies cross-lane crafted
calls. MCP containers never open SQLite or call providers.

R4C adds the generic registered-script allowlist sandbox and Asia/Manila
findings/evidence-only schedule templates. Repository scripts remain catalog-only
and are rejected on API, MCP, Celery, schedule, and registry-runner surfaces.

This document does **not** claim any gate close. R4D provides a local
Compose active-test harness; running it produces evidence only. The final
rootless-Podman evidence run is recorded below.

## Architecture

```
MCP lane containers (×6, frontend only)
  ──HTTP + initiating Bearer + MCP service token──► DRF API
Scheduler (Celery Beat) / script-worker controller / provider-worker
  ──HTTP + service credential──► state-coordinator (internal)
script-worker ──signed spool──► script-runner (network_mode: none)
DRF API (authn/authz) ──HTTP + API service credential──► state-coordinator
Redis ◄── Celery broker (authenticated, non-published, non-authoritative)
```

Rules:

- All authoritative mutations go through `StateCoordinator.accept` via the coordinator service.
- The API never opens SQLite for writes.
- MCP lane services never set `FLOW_DB_PATH`, `COORDINATOR_URL`, worker/API service
  tokens, or Redis credentials; they call DRF only.
- Script execution: coordinator may only register/authorize/state-transition. The networked
  `script-worker` controller dispatches signed job envelopes over a bounded HMAC spool.
  Subprocess execution runs exclusively in `script-runner` with `network_mode: none` and
  **no** DB/Redis/coordinator credentials. Results return as typed signed envelopes;
  worker settles via authenticated coordinator transport (`script.start` /
  `script.complete` or `accept_script_execute`). Coordinator/API set `ORCH_SCRIPT_ROLE=control`.
- Image authority is a deployment/build attestation (`scripts/build_script_runner_attestation.sh`)
  that captures the immutable image `Id` or `RepoDigest` via podman/docker
  `image inspect`, signs it (HMAC), and persists it for coordinator/worker/runner
  configuration. Runtime never calls the container engine and never treats a
  self-referential pin-manifest hash as an image digest. Missing/wrong
  attestation fails closed outside `ORCH_TESTING`. Allowed production sources:
  `container_inspect` (preferred) or legacy `docker_inspect`.
- Script execution uses argv arrays only (never shell strings), pinned executable digest,
  authorized attested image digest, JSON input/output schemas, server-resolved cwd/path policy,
  minimal env allowlist, no secret projection, streaming stdout/stderr byte caps with redaction
  before persistence, durable cancel + process-group termination, concurrency one, and idempotency.
- Durable cancel crosses the networkless spool: the controller publishes an authenticated cancel
  envelope bound to job/execution/nonce; the runner polls it during process execution and
  terminates the process group; coordinator settlement makes cancellation win over completed/failed.
- Spool discovery/claim never follows symlinks: paths are confined and lstat'd, pending entries
  must be regular files under the jobs directory, filename `job_id` must equal the signed
  envelope `job_id`, claims are atomic move-first (no-replace to a unique claimed path) with
  post-move validation/inode binding, nonce consumption only after successful claim+validation,
  and invalid claimed files quarantined with recoverable audit state.
- Public DRF/MCP/schedule schemas reject caller-controlled test hooks (`workspace_root`,
  `simulate_network`, `force_timeout`, `inject_env`, `override_argv`/`cwd`).
- Script-runner container: non-root, read-only root, tmpfs, no-new-privileges, seccomp,
  cap_drop ALL, `network_mode: none`.
- Schedules: Asia/Manila Celery Beat entries for all 7 templates; scheduler token auth;
  planned_time validated against exact cadence; zero provider-call budget; concurrency one;
  dedupe on `(schedule_id, planned_time)`; no overlap. `script_results` validated/redacted
  against exact typed evidence/finding/anomaly/follow-up schemas with per-field and aggregate
  byte/count bounds, recursive secret redaction, and forbidden-effect vocabulary rejection —
  never remediation, repo mutation, merge/deploy, policy/gate changes, or provider calls.
- Coordinator port **9001 is never published**; it is reachable only on the Compose backend network.
- Dual identity on MCP invokes is preserved (R4B).

## Principals (local)

Bootstrap is **off by default** (`ORCH_BOOTSTRAP_PRINCIPALS=0`). When enabled in Compose,
principals are registered from injected env tokens (`ORCH_TOKEN_*`) — never from
source-controlled fixed secrets. Tests use deterministic fixtures in
`bootstrap_test_principals()` only.

Authenticate API callers with `Authorization: Bearer <token>` or `X-Orchestrator-Token`.
Coordinator transport requires distinct `ORCH_API_SERVICE_TOKEN` /
`ORCH_WORKER_SERVICE_TOKEN` headers.

## User authentication (human accounts)

Human end-user identity is separate from founder/service/MCP/worker bootstrap
tokens:

- Accounts live in coordinator SQLite (`control_plane_user_accounts`) mapped to
  `kind=human` principals. Passwords use Django `make_password` /
  `check_password` only; password digests are never stored on
  `control_plane_principals.token_digest`.
- Issued credentials are opaque secrets (`access` / `refresh` / `pat`) with
  SHA-256 digests at rest, short-lived access, rotating refresh with
  replay-safe family revocation, and independently revocable PATs. No JWT
  package is required.
- Registration is fail-closed: `ORCH_ALLOW_USER_REGISTRATION` defaults to `0`
  in every environment. Compose projects the flag into **both** `api` and
  `coordinator` (dual gate — either service missing the opt-in breaks open
  signup). Credential TTL and login-throttle settings are projected into
  **coordinator only** (`ORCH_ACCESS_TOKEN_TTL_SEC`, `ORCH_REFRESH_TOKEN_TTL_SEC`,
  `ORCH_PAT_TTL_SEC`, `ORCH_AUTH_THROTTLE_WINDOW_SEC`, `ORCH_AUTH_THROTTLE_MAX`).
  Enable registration explicitly in Compose `.env` for local self-signup, or
  create accounts with a founder bearer. Founder-authenticated registration over
  the API-to-coordinator HTTP boundary requires the API service to forward the
  raw bearer as `X-Orchestrator-Principal-Token`; the coordinator resolves role
  server-side and never trusts serialized `context.role` or payload authority
  fields (`founder_authorized`, `allow_registration`). New humans get least-privilege
  capabilities (no `ops.read` until granted).
  The VPS overlay pins `ORCH_ALLOW_USER_REGISTRATION: "0"` on `api` and
  `coordinator` so a hand-edited `.env.vps` cannot enable signup without an
  overlay edit.
- Login throttling uses coordinator-durable counters
  (`control_plane_auth_throttle`) so limits hold under gunicorn `--workers 2`.
- JSON endpoints: `POST /api/v1/auth/register|login|refresh|logout`,
  `GET /api/v1/auth/me`, `POST /api/v1/auth/token`,
  `POST /api/v1/auth/token/<id>/revoke`.
- Anonymous allowlist is `/health/` only. `/ops/summary/` requires founder or
  capability `ops.read`. Ops-console uses a generic API bearer field and sends
  `Authorization` on summary fetch.
- CLI: `flowctl auth login|logout|status|token` talks to `ORCH_API_URL`, stores
  credentials under `~/.config/orchestrator/credentials.json` at mode `0600`,
  supports `ORCH_USER_TOKEN` / `--token-file`, and never prints secrets unless
  `--show-token` is set. Local SQLite `flowctl` DB commands remain unchanged.

### Migration `008_user_auth.sql` and rollback

SQLite cannot alter CHECK constraints in place. Migration 008 rebuilds
`control_plane_principals` (copy → drop → rename → recreate active digest
index), then creates accounts/credentials/throttle tables. The migration runner
is forward-only. **Before applying on a durable DB, take a SQLite backup.**
Rollback is restore-from-backup (HitM/ops-approved), not an automatic down
migration — a silent CHECK reverse would orphan `human` rows.

Deploy note (no VPS mutation in this slice): keep registration off, migrate,
provision the first human via founder-authorized register or flag-gated local
signup, and update any summary consumers to send a bearer (founder or
`ops.read` human/PAT).

## Install

```bash
pip install -e '.[control-plane,dev]'
```

## Run (Compose)

```bash
# Preferred: R4D ephemeral secrets + attestation + full active-test (podman|docker)
bash scripts/r4d_active_test.sh

# Or manual:
bash scripts/r4d_generate_ephemeral_env.sh   # writes ignored .tmp/r4d/*/env
set -a && source .tmp/r4d/<run>/env && set +a
bash scripts/build_script_runner_attestation.sh
# copy printed ORCH_SCRIPT_IMAGE_DIGEST into env
# docker compose / podman-compose:
orch_compose() { /* see scripts/lib/container_runtime.sh */ }
bash -c 'source scripts/lib/container_runtime.sh && orch_compose config'
bash -c 'source scripts/lib/container_runtime.sh && orch_compose up --build'
```

Services:

- `api` — DRF on **127.0.0.1:8000** only
- `coordinator` — sole writer on internal network only (not published)
- `redis` — authenticated Celery broker (not published)
- `worker` — mock provider delivery tasks
- `script-worker` — networked controller for allowlisted scripts (`script-sandbox` queue)
- `script-runner` — networkless executor (`network_mode: none`, spool only)
- `scheduler` — schedule tick queue (Asia/Manila TZ)
- six MCP lane services — frontend only

## API (versioned)

| Method | Path | Description |
|---|---|---|
| POST | `/api/v1/runtime/preview` | Preview run (founder) |
| POST | `/api/v1/runtime/run` | Create + dispatch |
| GET | `/api/v1/runtime/runs/{run_id}` | Run status |
| POST | `/api/v1/runtime/heartbeat` | Attempt heartbeat (worker) |
| POST | `/api/v1/runtime/result` | Submit attempt result (worker) |
| POST | `/api/v1/runtime/recover` | Coordinator restart recovery (founder) |
| GET | `/api/v1/delivery/jobs` | List eligible delivery jobs (worker) |
| GET | `/api/v1/mcp/profiles` | Admin/Ops, QA, Tech capability profiles |
| GET | `/api/v1/mcp/lanes/{lane_id}/snapshot` | Exact tool snapshot (dual principal) |
| GET | `/api/v1/mcp/lanes/{lane_id}/tools` | Lane tool list (dual principal) |
| POST | `/api/v1/mcp/lanes/{lane_id}/tools/invoke` | Invoke lane tool (dual principal) |
| GET | `/api/v1/scripts/allowlist` | Generic registered-script allowlist |
| POST | `/api/v1/scripts/execute` | Register + execute allowlisted script |
| GET | `/api/v1/scripts/executions/{id}` | Script execution status |
| POST | `/api/v1/scripts/cancel` | Cancel script execution |
| GET | `/api/v1/schedules/templates` | Asia/Manila schedule templates |
| GET | `/api/v1/schedules/status` | Schedule status / recent runs |
| POST | `/api/v1/schedules/tick` | Claim schedule tick (scheduler/founder) |
| POST | `/api/v1/schedules/complete` | Complete schedule run (effects-only) |
| POST | `/api/v1/schedules/run` | Founder on-demand schedule run |

## Migration 006

Additive tables: `script_executions`, `schedule_runs`.
Legacy queue claim eligibility unchanged. R4A/R4B tables preserved.

## Tests

```bash
pip install -e '.[control-plane,dev]'
ORCH_TESTING=1 python scripts/write_testing_attestation.py
pytest tests/unit/test_r4d_*.py tests/unit/test_r4c_*.py
bash scripts/r4d_verify.sh           # static
bash scripts/r4d_verify.sh --active  # static + full Compose active-test
```

The full source suite at final verification reported **316 passed, 1 skipped**.

## Final local active-test evidence

The 2026-07-26 rootless-Podman run
`r4d-20260726T113210Z-2768705` completed successfully. Its ignored,
repo-local evidence bundle is
`.tmp/r4d/r4d-20260726T113210Z-2768705/`, with runtime fingerprint
`956096e8560ceea53b33e5420c58f4ad6ca7da606c5587475a296e05c39f51cb`.
`evidence/summary.json` records `ok: true`, `runtime: podman`, and
`gates_closed: false`.

All 11 evidence steps passed:

1. sole-writer seed;
2. API/coordinator health;
3. authenticated API-to-worker mock-provider completion;
4. all six MCP lane snapshots with crafted cross-lane invocation denied
   (`403`);
5. all 12 department-by-position loadouts resolved and hashed;
6. registered generic script completion, escape-hook rejection (`400`), and
   repository-script denial (`403`);
7. all seven Asia/Manila schedule templates ticked and completed with
   remediation denied;
8. worker restart recovery;
9. redelivery enqueue;
10. coordinator restart recovery; and
11. SQLite integrity (`ok`).

Final-run remediations included rootless volume ownership and health handling,
real image-inspect attestation bootstrap, fresh no-key observation reads,
typed HTTP rejection transport, and seccomp additions proven necessary by the
networkless runner (`setsid`, `renameat2`, `mkdir`, and `rmdir`). These changes
preserve fail-closed attestation and sandbox behavior. Earlier diagnostic runs
also exposed rootless Podman health/teardown friction; the final run completed
the scripted evidence sequence and teardown.

Independent evidence review nevertheless rated
`G-ORCH-LOCAL-CONTROL-PLANE` evidence insufficient:

- the redelivery probe does not prove a task was unacknowledged at worker loss,
  redelivered, completed exactly once, and produced no duplicate terminal
  effect;
- restart evidence lacks authoritative pre/post state identity, revision, and
  result-continuity capture;
- teardown lacks a post-cleanup zero-container/volume/network capture; and
- `logs/compose-config.yml` is mode `0644` and contains expanded ephemeral
  credentials (the run directory is `0700`, but exporting the evidence bundle
  would create credential risk).

The script-sandbox and scheduled-maintenance evidence support HitM
consideration, but the functional-green run is not sufficient
local-control-plane acceptance evidence.

### Provider host-runner credentials and model pins

Real-provider workers receive only their matching Unix-socket mount, worker
principal token, and scoped host-runner HMAC key. The HMAC key authenticates
Orchestrator envelopes; it is **not** a Codex, Cursor, or Claude credential.
Provider authentication remains in the installed host CLI process. Neither key
class may appear in product state, logs, evidence, or an expanded Compose
configuration.

The supported CLIs do not all provide a no-paid-call model-enumeration command.
The host runner therefore fails closed on an installation-local exact
allowed-model pin, records `model_resolution: installation_allowed_pin`, and
requires the configured resolved model to match that allowlist. This is a
configuration authorization and immutable pin, not proof that a provider will
accept the model; only the separately authorized bounded call can supply that
evidence.

Provider acceptance runs use a fresh, empty, non-symlink mode-`0700`
disposable workspace that is removed on every exit, including validation and
process-launch failures. Claude receives an empty allowed-tool set and explicit
denials for read/search/edit/write/shell/network tools. Codex uses its
`read-only` sandbox and Cursor uses `ask` mode without force; because those CLIs
do not expose a literal universal no-tool switch in the supported interface,
the acceptance policy also supplies an explicit no-tool instruction and the
empty disposable workspace. This constrained acceptance profile is distinct
from a separately configured implementation-capable profile.

### Provider-adapter verification and blocked live acceptance

Provider-adapter implementation and independent pre-live review passed. The
latest authoritative source verification is **334 passed, 1 skipped**; Ruff,
catalog validation, and `git diff --check` also passed.

The authorized live acceptance stopped before dispatch with **zero provider
calls and zero retries** because exact model pins could not be established
without guessing. Codex authentication was ready but had no exact configured
model ID. Cursor `status` reported login while both model-list surfaces
reported authentication required. Claude authentication was ready but
configuration supplied only the `opus` alias. No acceptance runner, container,
socket, or temporary workspace was created. All related gates remain open.

HitM must provide explicit exact model IDs for all three providers and resolve
Cursor automation authentication/model discovery. A narrowly authorized alias
plus post-handshake pin exception is possible but not recommended because the
immutable exact identity would be learned only after dispatch.

## Out of scope

- Real Codex/Cursor/Claude host-runner provider adapters (the active test uses
  the mock provider), hosting, merge/deploy, publication
- Closing `G-ORCH-SCRIPT-SANDBOX`, `G-ORCH-SCHEDULED-MAINTENANCE`, or
  `G-ORCH-LOCAL-CONTROL-PLANE` (R4D evidence does not close gates)
