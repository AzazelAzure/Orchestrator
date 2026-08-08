# Troubleshooting playbook

**Audience:** operators and agents diagnosing failures from observable symptoms.

Each section lists **likely causes**, **authoritative checks**, and **safe next
steps** grounded in this branch's source. Placeholders only — never paste secrets.

---

## Auth 401 / 403 on API or ops summary

### Symptoms

- `curl /ops/summary/` returns 403
- DRF returns `{"detail":"Authentication credentials were not provided."}`
- Live acceptance `ops_summary_hierarchy` row fails with auth error

### Causes

| Cause | Evidence |
|-------|----------|
| Missing bearer | `OrchestratorPrincipalAuthentication` returns None → 401 |
| Anonymous ops summary | `RequireOpsReadOrFounder` denies without founder or `ops.read` |
| Revoked principal | `authentication.py` → `"principal revoked"` |
| Registration disabled | `POST /auth/register` → 403 when `ORCH_ALLOW_USER_REGISTRATION=0` |
| Wrong token for command | `authz_matrix.py` deny |

### Checks

```bash
curl -s http://127.0.0.1:8000/health/ | jq .status
flowctl auth status --api-url http://127.0.0.1:8000
# Founder path (stack env file — do not commit):
# curl -H "Authorization: Bearer <founder-token>" http://127.0.0.1:8000/ops/summary/
```

### Fix

1. `flowctl auth login` or export `ORCH_USER_TOKEN` from secure store.
2. `python3 scripts/local_stack_sync_tokens.py` after Compose env rotation.
3. Grant `ops.read` capability to human principal if not founder.
4. For stress scripts: ensure manifest env includes founder token (ORCH-LI behavior).

**Source:** `control_plane/api/ops_urls.py`, `permissions.py`, `tests/unit/test_ops_summary.py`.

---

## Ops summary degraded / empty ladder

### Symptoms

- `"status": "degraded"` in ops summary JSON
- `verification_ladder.latest_run_id` is null

### Causes

- Coordinator unreachable from API (`get_client().health()` exception)
- No `.tmp/verification-ladder/*/summary.json` yet
- Dashboard fetch error in `fetch_dashboard_payload`

### Checks

```bash
curl -s http://127.0.0.1:8000/health/
ls -lt .tmp/verification-ladder/*/summary.json 2>/dev/null | head -3
python3 scripts/verification_ladder.py
```

---

## Stale Compose images / unhealthy stack

### Symptoms

- `local_stack_up.sh` rebuilds unexpectedly
- `wait_http` timeout after compose up
- API 503 on `/health/`

### Causes

- Stale manifest pointing at dead containers (`local_stack_up.sh` non-fatal stale probe)
- Image digest drift vs attestation
- Port 8000 conflict

### Checks

```bash
ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
  bash scripts/r4d_compose.sh ps
bash scripts/local_stack_up.sh    # observe rebuild path
curl -v http://127.0.0.1:8000/health/
```

### Fix

1. Stop the persistent stack, then rebuild:

   ```bash
   ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
     bash scripts/r4d_compose.sh down
   bash scripts/local_stack_up.sh
   ```

   (`down` preserves volumes unless you explicitly add volume-removal flags.)
2. Re-run the persistent stack (rebuilds script-runner attestation under
   `.tmp/local-stack/attestations/` and reloads stack env — preferred):

   ```bash
   bash scripts/local_stack_up.sh
   ```

   Do **not** run `scripts/build_script_runner_attestation.sh` bare: it requires
   `ORCH_ATTESTATION_HMAC_KEY` and defaults output to tracked
   `deploy/attestations/`. Only use a fully explicit `.tmp` invocation if you must
   run it outside `local_stack_up.sh` (source `ORCH_ATTESTATION_HMAC_KEY` from
   `.tmp/local-stack/env` first; set `ORCH_SCRIPT_RUNNER_ATTESTATION_OUT` under
   `.tmp/local-stack/attestations/`).
3. Sync tokens after rebuild: `python3 scripts/local_stack_sync_tokens.py`.

**Source:** `scripts/local_stack_up.sh`, `tests/unit/test_local_stack_up.py`.

---

## Provider timeout / transport hang

### Symptoms

- Attempt stuck in `dispatched` or moves to `outcome_unknown`
- Host runner wall clock exceeded
- Partial stream with no terminal event

### Causes

- CLI version pin mismatch (`REGISTERED_CLI_VERSIONS` / `ORCH_PROVIDER_CLI_VERSION`)
- Missing API key in allowlisted env (Cursor)
- Network blocked in acceptance environment
- Event not in `EVENT_TYPES` allowlist

### Checks

```bash
# Mock path (isolate coordinator):
ORCH_PROVIDER_MODE=mock pytest tests/unit/test_r2_runtime.py -q
# Host runner unit tests (no real CLI):
pytest tests/unit/test_provider_host_runner.py -q
```

### Fix

1. Verify pin files match supported CLI versions.
2. Run bounded live acceptance with an explicit provider (`cursor` or `claude`;
   `codex` is excluded):

   ```bash
   python3 scripts/provider_live_acceptance.py --provider cursor
   ```

3. Reconcile before new paid attempt: `flowctl runtime reconcile …` (founder).
4. Do **not** auto-retry paid calls — use founder step-up `new-attempt` if policy allows.

**Source:** `providers/host_runner.py`, `domain/credits.py` timeouts.

---

## Credit reservation failures

### Symptoms

- Coordinator rejects dispatch with credit/budget error
- Acceptance campaign exhausted

### Causes

- Wrong or per-item `budget_scope_id` (campaign split unintentionally)
- Prior invocation not settled
- Concurrency envelope exceeded (`GLOBAL_PROVIDER_CONCURRENCY`, etc.)

### Checks

```bash
flowctl runtime show <run_id> --budget-scope-id <campaign-id>
# Credit ledger: coordinator/API envelopes and runtime.show only.
# flowctl export emits kernel tables (projects, queues, work_items, …) — not credits.
```

### Fix

1. Use one stable `--budget-scope-id` per acceptance campaign.
2. Settle or reconcile outstanding invocations.
3. Wait for concurrency slots to free.

---

## Worker / coordinator delivery failures

### Symptoms

- Celery task errors in worker logs
- `delivery.claim` rejected
- Heartbeat gaps → inactivity timeout

### Causes

- `ORCH_WORKER_SERVICE_TOKEN` mismatch (coordinator HTTP transport)
- `ORCH_TOKEN_WORKER` / `ORCH_TOKEN_WORKER_<PROVIDER>` mismatch (principal identity on delivery commands)
- Coordinator unreachable on backend network
- Redis auth failure

### Checks

```bash
ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
  bash scripts/r4d_compose.sh logs worker --tail 50
ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
  bash scripts/r4d_compose.sh logs coordinator --tail 50
pytest tests/unit/test_r4_delivery.py -q
```

### Fix

1. `python3 scripts/local_stack_sync_tokens.py`.
2. Verify coordinator health from API container (not host port 9001).
3. Restart worker after token sync:

   ```bash
   ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
     bash scripts/r4d_compose.sh restart worker
   ```

---

## MCP lane auth / invoke failures

### Symptoms

- Lane container 403 on DRF invoke
- `mcp.tool.invoke` rejected in envelope
- Catalog snapshot mismatch

### Causes

- Missing initiating bearer or MCP service token
- Tool not in lane catalog snapshot
- Cross-lane crafted call blocked (`mcp_enforce.py`)

### Checks

```bash
pytest tests/unit/test_r4b_mcp_lanes.py tests/unit/test_skills_scripts_mcp_lane.py -q
bash scripts/r4b_verify.sh
```

### Fix

1. Verify per-lane principal env keys from `bootstrap.py` / `docker-compose.yml`
   (e.g. `ORCH_TOKEN_MCP_CONTEXT_ASSETS`, `ORCH_TOKEN_MCP_SKILLS_SCRIPTS`).
2. Regenerate lane catalog hash if tools changed (`agentic/catalogs/`).
3. Never point lanes at coordinator URL directly.

---

## MCP stdio (`flowctl-mcp`) issues

### Symptoms

- Import error on startup
- Tool timeout
- Empty project binding

### Causes

- Missing `pip install -e '.[mcp]'`
- `FLOW_PROJECTS_CONFIG` points at wrong `projects.json`
- `FLOW_CAPABILITY_TIMEOUT_SEC` too low

### Checks

```bash
pip show mcp
flowctl cap repo-health --projects-config /path/to/projects.json --project demo_project
pytest tests/mcp/ -q
```

---

## SQLite locking / single-writer violations

### Symptoms

- `database is locked`
- Divergent state between API read and CLI write

### Causes

- Direct SQLite write bypassing coordinator while API stack running
- Two processes writing same DB file
- WAL checkpoint under heavy contention

### Fix

1. **Stop** writing via raw SQLite while Compose is up — use coordinator path only.
2. Use separate `FLOW_DB_PATH` for kernel-only experiments.
3. Only one coordinator instance per database file.

**Source:** `coordinator/coordinator.py`, `tests/unit/test_r4_coordinator_boundary.py`.

---

## Local stack manifest collision

### Symptoms

- Wrong `work_item_id` after parallel script runs
- Stress test overwrites another slice's manifest

### Cause

Shared `ORCH_LOCAL_STACK_MANIFEST` default path across concurrent processes.

### Fix

```bash
export ORCH_LOCAL_STACK_MANIFEST=.tmp/local-stack/manifest-slice-a.json
# one distinct path per concurrent slice
```

**Source:** `scripts/local_stack_helpers.py`, CHANGELOG ORCH-LI.

---

## Evidence path confusion

### Symptoms

- Cannot find acceptance run output
- Ops summary missing ladder ID

### Standard locations (gitignored)

| Artifact | Path pattern |
|----------|----------------|
| Verification ladder | `.tmp/verification-ladder/<run_id>/summary.json` |
| R4D evidence | `.tmp/r4d-active-test/<run_id>/` |
| Provider live acceptance | `.tmp/provider-live-acceptance/<run_id>/` |
| Local stack manifest | `.tmp/local-stack/manifest.json` (or custom) |
| Delegate/bridge probes | `.tmp/hq-delegate-probe/*/summary.json` (installation-local naming) |

Probe glob patterns in ops summary are optional aggregates — absence is not failure.

---

## CLI exit codes

| Code | Meaning (`cli/app.py`) |
|------|------------------------|
| 0 | Success |
| 1 | Advisory conflict |
| 2 | Conflict, invalid transition, not found, flow error |
| 3 | Unexpected exception |

`flowctl auth` login failures return **2**.

---

## When to stop and escalate

- Suspected credential compromise → rotate tokens, revoke credentials via API.
- Data corruption suspicion → stop the Compose stack cleanly, follow [operator runbook](operator-runbook.md) backup/restore procedure (no live `cp` of SQLite/WAL files), file finding with evidence paths.
- Repeated `outcome_unknown` on paid providers → reconcile before any new attempt.

---

## See also

- [Operator runbook](operator-runbook.md)
- [Auth and security](auth-and-security.md)
- [Providers](providers.md)
- [Glossary](../reference/glossary.md)
