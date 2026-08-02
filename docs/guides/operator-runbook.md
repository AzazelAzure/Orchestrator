# Operator runbook

**Audience:** a human operator bringing up, exercising, observing, and tearing down
a local Orchestrator stack.

**Scope:** commands validated against this branch's `scripts/` and `flowctl --help`.
Production/VPS steps reference `deploy/` overlays only — not claimed as landed.

---

## Modes of operation

| Mode | When to use | Persistence |
|------|-------------|-------------|
| **Minimal kernel** | Unit tests, kernel-only CLI work | `.flow/state.db` (default) |
| **One-shot R4D** | CI-style evidence capture | Ephemeral Compose project |
| **Persistent local stack** | Daily agent / acceptance work | Compose volumes + `.tmp/local-stack/manifest.json` |
| **Verification ladder** | Structured L1–L4 check | `.tmp/verification-ladder/<run_id>/` |

---

## 1. Minimal kernel

```bash
cd /path/to/Orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest -q                              # baseline before claims
flowctl init --project default
flowctl status
flowctl --help
```

Override DB: `FLOW_DB_PATH=/path/to/state.db` or `flowctl --db /path/to/state.db`.

Optional read-only MCP:

```bash
pip install -e '.[mcp]'
export FLOW_PROJECTS_CONFIG=/path/to/projects.json   # optional
flowctl-mcp
```

---

## 2. Persistent local Compose stack

```bash
pip install -e '.[control-plane,dev]'
bash scripts/local_stack_up.sh
# Writes .tmp/local-stack/manifest.json; keeps running
```

After first bring-up or env rotation:

```bash
python3 scripts/local_stack_sync_tokens.py
```

### Compose helper (Docker or Podman)

`scripts/local_stack_up.sh` sets `ORCH_COMPOSE_PROJECT=orch-local` (default) and writes
`ORCH_R4D_ENV_FILE` to `.tmp/local-stack/env`. For post-up operations, use the
runtime-agnostic wrapper (same contract as `local_stack_helpers.py`):

```bash
ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
  bash scripts/r4d_compose.sh ps

ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
  bash scripts/r4d_compose.sh logs worker --tail 50
```

If you customized `ORCH_LOCAL_STACK_DIR`, read `compose_project` and `env_file` from
`.tmp/local-stack/manifest.json` (or your manifest path) instead of the defaults above.

### Endpoints (default manifest)

| Service | URL | Notes |
|---------|-----|-------|
| DRF API | `http://127.0.0.1:8000` | Bearer for authenticated routes |
| Health | `http://127.0.0.1:8000/health/` | No auth |
| Ops summary | `http://127.0.0.1:8000/ops/summary/` | **Auth required** (founder or `ops.read`) |
| OpenAPI schema | `http://127.0.0.1:8000/api/schema/` | **Bearer required** (anonymous **401/403**) |
| OpenAPI UI | `http://127.0.0.1:8000/api/docs/` | **Bearer required**; Swagger HTML **200** when authenticated |
| Coordinator | `http://coordinator:9001` (internal) | **Not published** to host |

Human login:

```bash
flowctl auth login --api-url http://127.0.0.1:8000
flowctl auth status
```

Founder bootstrap token (when bootstrap enabled): read from stack env file
referenced in manifest — never paste into chat or commits.

### Concurrent slices

Default manifest `.tmp/local-stack/manifest.json` is for **sequential
single-operator** use. Concurrent governed dispatch requires distinct
`ORCH_LOCAL_STACK_MANIFEST` per slice. The manifest is a local cache; coordinator
`work_item_id` is authoritative.

### Interim work-item creation

No dedicated authenticated work-submit API yet. Supported interim path:
`scripts/r4d_seed_work.py` via `refresh_work_item()` in `scripts/local_stack_helpers.py`.

---

## 3. Live acceptance and stress ladder

Run with stack up and tokens synced:

```bash
python3 scripts/orchestrator_live_acceptance.py
python3 scripts/local_delegation_stress.py
python3 scripts/local_stress_test.py        # L1/L2 + live + delegation + bridge probe
python3 scripts/verification_ladder.py      # structured L1–L4 summary.json
```

**Auth requirement:** live acceptance and stress scripts send
`Authorization: Bearer <founder token>` for ops-summary checks. Missing token
or HTTP 403 is an explicit failure — no anonymous retry
(`scripts/orchestrator_live_acceptance.py`, CHANGELOG ORCH-LI entry).

### Verification ladder levels

| Level | Script / target |
|-------|-----------------|
| L1 | `flowctl` kernel checks |
| L2 | DRF/delivery pytest subset |
| L3 | `scripts/r4d_verify.sh` |
| L4 | Provider runtime envelope |

Output: `.tmp/verification-ladder/<run_id>/summary.json`.

### One-shot R4D harness

```bash
bash scripts/r4d_active_test.sh    # tears down by default
```

Produces remediated redelivery/restart/teardown evidence (see CHANGELOG 2026-07-28).

### Bounded provider live acceptance

```bash
python3 scripts/provider_live_acceptance.py --provider cursor
python3 scripts/provider_live_acceptance.py --provider claude
python3 scripts/provider_runtime_acceptance.py # coordinator/worker credit path
```

Requires installation-local provider credentials and pin files. **Local acceptance only.**

---

## 4. Ops console and observation

Browser ops dashboard: served by DRF when stack is up (see `control_plane/ops_dashboard.py`).

Aggregated read model: `GET /ops/summary/` returns stack health, verification ladder
snapshot, delegation/queue projections, schedule status, and settings metadata
(`control_plane/api/ops_urls.py`).

Authenticate before calling:

```bash
# After flowctl auth login, use stored token or ORCH_USER_TOKEN
curl -s -H "Authorization: Bearer $ORCH_USER_TOKEN" \
  http://127.0.0.1:8000/ops/summary/ | jq .
```

---

## 5. MCP lanes (R4B + skills-scripts)

Six lane containers defined in `agentic/catalogs/mcp_lanes.json`. Each lane:

- Calls DRF with initiating bearer + MCP service token
- Never opens SQLite or invokes provider CLIs directly

Stdio read-only MCP (`flowctl-mcp`) is separate — five capability tools, no network.

Lane verification: `bash scripts/r4b_verify.sh` (local candidate).

---

## 6. Backup, migrations, and recovery

### Minimal kernel (`.flow/state.db`)

SQLite runs in **WAL mode** (`persistence/connection.py`). Copying `state.db` with
`cp` while a writer is active can produce an inconsistent backup (WAL/SHM not
captured atomically with the main file).

**Before backup:** ensure **no** `flowctl` process, test fixture, or other writer
holds the database open.

**Physical backup (restorable):** use SQLite's online backup API, not raw `cp`:

```bash
# Example: Python sqlite3.Connection.backup (stdlib)
python3 - <<'PY'
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

src = Path(".flow/state.db")
dst = Path(f".flow/state.db.bak-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
with sqlite3.connect(src) as source, sqlite3.connect(dst) as dest:
    source.backup(dest)
print(f"backup written: {dst}")
PY
```

Equivalent: `sqlite3 .flow/state.db ".backup '/path/to/backup.db'"` from an idle DB.

**Logical snapshot (not a restorable DB file):** `flowctl export` emits JSON rows
for kernel tables only (`application/project_service.py` → `export_all`) — useful
for inspection/diff, **not** a substitute for `Connection.backup` or volume restore.

### Persistent Compose stack (`orch-local`)

The coordinator holds the sole writer on `/data/state.db` inside the
`orchestrator-data` named volume (`docker-compose.yml`). **Do not** open, copy, or
edit that SQLite file while the stack is running.

**Procedure:**

1. Stop the stack cleanly (preserves volumes by default):

   ```bash
   ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
     bash scripts/r4d_compose.sh down
   ```

2. Archive the **stopped** volume using your container runtime's documented
   volume-backup/export mechanism. The Compose volume name is `orchestrator-data`;
   with project `orch-local`, the runtime typically materializes it as
   `orch-local_orchestrator-data` (exact name depends on Docker vs Podman and
   rootless mapping).

3. **No portable automated volume-backup command is landed in this repository.**
   This doc intentionally does not prescribe runtime-specific `docker`/`podman`
   incantations — use the procedure documented for your selected runtime.

4. Preserve ownership/permissions per runtime guidance when restoring archives.

5. Restart: `bash scripts/local_stack_up.sh`. If env tokens rotated independently
   of the volume, run `python3 scripts/local_stack_sync_tokens.py`.

6. **Verify** restoration in a **disposable** stack (or isolated project/volume)
   before relying on the archive for production-like recovery.

### Migrations and rollback

Forward-only SQL migrations live under `persistence/migrations/`. Migration
`008_user_auth.sql` rebuilds the principals CHECK constraint.

**Rollback:** restore from a **verified** backup taken with the procedures above —
not an automatic down migration. For Compose data, that means a tested volume
archive; for kernel mode, a `Connection.backup` file from an idle DB.

### Coordinator recovery (founder CLI)

```bash
flowctl runtime recover restart|worker-death|reconstruct|timeouts ...
```

Founder role required (`authz_matrix.py`). Reconstructs eligible delivery without
duplicating paid calls. This is **runtime state recovery**, not a substitute for
filesystem/volume backup.

### Stack rebuild path

If `local_stack_up.sh` detects unhealthy API on existing manifest, it proceeds to
rebuild (non-fatal stale probe). Post-Compose readiness still fails fatally via
`scripts/lib/http_wait.sh` → `wait_http`.

---

## 7. VPS / blue-green boundaries

`deploy/` contains VPS overlay and env generation examples (`scripts/generate_vps_env.sh`).
This repository documents **local active-test** Compose; it does **not** claim:

- Blue-green deployment automation is landed
- Production gate closure
- Hosted multi-tenant operation

VPS overlay pins `ORCH_ALLOW_USER_REGISTRATION="0"` on api and coordinator.

---

## 8. Clean shutdown

### Persistent stack

```bash
ORCH_R4D_ENV_FILE=.tmp/local-stack/env ORCH_COMPOSE_PROJECT=orch-local \
  bash scripts/r4d_compose.sh down
```

`down` stops containers and preserves volumes by default (no `-v`). Bring the stack
back with `bash scripts/local_stack_up.sh`. To rotate tokens without tearing down,
use `python3 scripts/local_stack_sync_tokens.py`.

### R4D one-shot

`r4d_active_test.sh` tears down Compose project by default and captures
`zero_state` evidence.

### Local manifest cleanup

```bash
rm -f .tmp/local-stack/manifest.json   # only when intentional; re-seed after
```

---

## Pre-flight checklist

- [ ] `pytest` green for your change scope
- [ ] `flowctl --help` matches docs if CLI touched
- [ ] Tokens synced after env rotation
- [ ] Distinct manifest path if concurrent slices
- [ ] Ops summary called with auth (not anonymous)
- [ ] No secrets in `.tmp/` evidence committed to git

---

## See also

- [Auth and security](auth-and-security.md)
- [Providers](providers.md)
- [Troubleshooting](troubleshooting.md)
- [`r4-control-plane.md`](../r4-control-plane.md)
- [Developer guide](developer-guide.md)
