# API, CLI, and MCP surface reference

**Validated:** `flowctl --help` and subcommand help on branch `docs/deep-reference-2026-08-02`.
REST paths from `control_plane/api/urls.py` and `control_plane/urls.py`.

**Auth column:** `—` = no bearer required; `Bearer` = any authenticated principal
(`Authorization: Bearer` or `X-Orchestrator-Token`); `Founder` = `kind=founder`;
`Worker` = `kind=worker`; `Scheduler` = `kind=scheduler`; `Ops` = founder or
`ops.read`.

---

## Global CLI options

```
flowctl [-h] [--db DB] [--json] <command> ...
```

| Option | Default | Env override |
|--------|---------|--------------|
| `--db` | `.flow/state.db` | `FLOW_DB_PATH` |
| `--json` | off | — |

---

## `flowctl` command tree

### Top-level commands

| Command | Help | Needs initialized DB |
|---------|------|---------------------|
| `init` | Initialize database and default project | No (creates) |
| `status` | Show engine status | Yes |
| `export` | Export full database snapshot | Yes |
| `queue` | Queue operations | Yes |
| `work` | Work item operations | Yes |
| `resource` | Resource lease operations | Yes |
| `gate` | Gate operations | Yes |
| `event` | Event ledger operations | Yes |
| `cap` | Read-only project capabilities | Yes |
| `runtime` | R2 governed runtime controls | Yes |
| `org` | R3 organization / loadout controls | Yes |
| `delegation` | R3 delegation / handoff controls | Yes |
| `auth` | Control-plane user authentication | No (remote API) |

### `flowctl queue`

| Subcommand | Arguments |
|------------|-----------|
| `list` | — |
| `show` | `<name>` |

### `flowctl work`

| Subcommand | Key flags |
|------------|-----------|
| `submit` | `--queue`, `--payload`, `--actor`, `--depends-on`, `--idempotency-key` |
| `list` | `--queue`, `--status` |
| `show` | `<work_id>` |
| `claim` | `[work_id]`, `--queue`, `--actor`, `--revision`, `--idempotency-key` |
| `complete` | `<work_id>`, `--actor`, `--revision`, `--idempotency-key` |
| `fail` | `<work_id>`, `--actor`, `--reason`, `--revision`, `--idempotency-key` |
| `retry` | `<work_id>`, `--actor`, `--revision`, `--idempotency-key` |

### `flowctl resource`

| Subcommand | Key flags |
|------------|-----------|
| `list` | — |
| `show` | `<resource_id>` |
| `claim` | `<resource_id>`, `--holder`, `--kind`, `--policy advisory\|strict`, `--force`, `--reason` |
| `renew` | `<resource_id>`, `--holder` |
| `release` | `<resource_id>`, `--holder`, `--revision` |

### `flowctl gate`

| Subcommand | Key flags |
|------------|-----------|
| `list` | `--work` |
| `pass` | `<gate_id>`, `--actor` |
| `fail` | `<gate_id>`, `--actor` |
| `waive` | `<gate_id>`, `--authority`, `--reason`, `--evidence-artifact-id` (all required) |

### `flowctl event`

| Subcommand | Key flags |
|------------|-----------|
| `list` | `--limit`, `--type` |

### `flowctl cap`

Parent flags: `--projects-config`, `--actor`, `--request-id`, `--timeout`.

| Subcommand | Key flags |
|------------|-----------|
| `repo-health` | `--project` (required) |
| `open-prs` | `--project`, `--github-owner`, `--github-repo` |
| `ci-status` | `--project`, `--github-owner`, `--github-repo`, `--ref` |
| `work-lookup` | `--project`, `--work-id` and/or `--logical-work-id` |
| `session-brief` | `--project`, optional `--work-id`, `--logical-work-id`, `--github-owner`, `--github-repo` |

### `flowctl runtime`

Requires `--budget-scope-id` on grant-bearing commands.

| Subcommand | Purpose |
|------------|---------|
| `preview` | Preview governed run |
| `create` | Create governed run |
| `run` | Create, claim, dispatch one step |
| `step` | Claim if needed, dispatch one call |
| `claim` | Claim run attempt lease |
| `pause` / `resume` / `cancel` | Run lifecycle |
| `result` / `heartbeat` | Attempt reporting |
| `reconcile` | Reconcile original invocation |
| `provider-limit` | Halt / continue / reroute |
| `new-attempt` | Founder-only after unknown |
| `show` | Run + credit usage |
| `recover` | `restart`, `worker-death`, `reconstruct`, `timeouts` subpaths |

### `flowctl org`

`create-profile`, `show-profile`, `list-profiles`, `add-actor`, `add-seat`,
`members`, `find-position`, `loadout-preview`, `snapshot`, `show-snapshot`,
`assign`, `complete-assignment`.

### `flowctl delegation`

`request`, `accept`, `decline`, `reroute`, `dispatch`, `handoff`,
`accept-handoff`, `mint-grant`, `show-request`, `show-pin`.

### `flowctl auth`

| Subcommand | Key flags |
|------------|-----------|
| `login` | `--api-url`, `--username`, `--password`, `--token`, `--token-file`, `--pat` |
| `logout` | `--api-url` |
| `status` | `--api-url`, `--show-token` (unsafe) |
| `token` | `--api-url`, `--label`, `--show-token` (unsafe) |

Credential precedence: see [Auth guide](../guides/auth-and-security.md).

---

## REST API (`/api/v1/`)

Base URL (local stack): `http://127.0.0.1:8000`.

### Auth endpoints

| Method | Path | Auth |
|--------|------|------|
| POST | `/api/v1/auth/register` | — (gated) |
| POST | `/api/v1/auth/login` | — |
| POST | `/api/v1/auth/refresh` | — |
| POST | `/api/v1/auth/logout` | — |
| GET | `/api/v1/auth/me` | Bearer |
| POST | `/api/v1/auth/token` | Bearer |
| POST | `/api/v1/auth/token/<credential_id>/revoke` | Bearer |

### Runtime

| Method | Path | Auth | Coordinator command |
|--------|------|------|---------------------|
| POST | `/api/v1/runtime/preview` | Founder | `runtime.preview` |
| POST | `/api/v1/runtime/run` | Founder | `runtime.run` |
| GET | `/api/v1/runtime/runs/<run_id>` | Founder or Worker | `runtime.show` |
| POST | `/api/v1/runtime/heartbeat` | Worker | `runtime.heartbeat` |
| POST | `/api/v1/runtime/result` | Worker | `runtime.result` |
| POST | `/api/v1/runtime/recover` | Founder | `runtime.recover_restart` |
| POST | `/api/v1/runtime/pause` | Founder | `runtime.pause` |
| POST | `/api/v1/runtime/resume` | Founder | `runtime.resume` |
| POST | `/api/v1/runtime/cancel` | Founder | `runtime.cancel` |

### Delivery

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/v1/delivery/jobs` | Worker |

### MCP lanes (R4B)

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/v1/mcp/profiles` | Founder, Worker, Scheduler, or `mcp_service` (initiating bearer only; no lane dual-identity guard) |
| GET | `/api/v1/mcp/lanes/<lane_id>/snapshot` | Bearer (initiating principal) |
| GET | `/api/v1/mcp/lanes/<lane_id>/tools` | Bearer (initiating principal) |
| POST | `/api/v1/mcp/lanes/<lane_id>/tools/invoke` | Bearer (dual identity) |

### Scripts and schedules (R4C)

| Method | Path | Auth |
|--------|------|------|
| GET | `/api/v1/scripts/allowlist` | Bearer |
| POST | `/api/v1/scripts/execute` | Founder, Worker, or Scheduler (`script.register`) |
| GET | `/api/v1/scripts/executions/<execution_id>` | Bearer |
| POST | `/api/v1/scripts/cancel` | Founder or Worker |
| GET | `/api/v1/schedules/templates` | Bearer |
| GET | `/api/v1/schedules/status` | Bearer |
| POST | `/api/v1/schedules/tick` | Founder or Scheduler |
| POST | `/api/v1/schedules/complete` | Founder or Scheduler |
| POST | `/api/v1/schedules/run` | Founder |

### Ops, schema, and health (outside `/api/v1/`)

| Method | Path | Auth |
|--------|------|------|
| GET | `/health/` | — |
| GET | `/ops/summary/` | Ops |
| GET | `/api/schema/` | Bearer required (anonymous **401/403**) |
| GET | `/api/docs/` | Bearer required; Swagger HTML **200** when authenticated |

OpenAPI schema served by `drf-spectacular` (`control_plane/urls.py`). Schema and
Swagger UI require authentication (`SPECTACULAR_SETTINGS["SERVE_PERMISSIONS"]`).
Anonymous requests receive **401/403** (`test_openapi_schema_anonymous_denied`,
`test_openapi_docs_anonymous_denied`). Authenticated bearer requests receive schema
**200** and rendered Swagger HTML **200** (`test_openapi_docs_authenticated_html`).
Only `GET /health/` is anonymously reachable among Django HTML/JSON surfaces besides
the auth registration/login endpoints.

---

## Stdio MCP (`flowctl-mcp`)

Entry: `flowctl-mcp` (requires `pip install -e '.[mcp]'`).

| Tool | Capability | Mutates state |
|------|------------|---------------|
| `repo_health` | `repo_health` | No |
| `open_prs` | `open_prs` | No |
| `ci_status` | `ci_status` | No |
| `work_lookup` | `work_lookup` | No |
| `session_brief` | `session_brief` | No |

Env: `FLOW_DB_PATH`, `FLOW_PROJECTS_CONFIG`, `FLOW_CAPABILITY_TIMEOUT_SEC`.

---

## R4 MCP lane catalog (HTTP via DRF)

Six lanes from `agentic/catalogs/mcp_lanes.json` (`count`: 6). Catalog
`activation` / `lifecycle_state` are **inert** — lane profiles are design contracts;
runtime enforcement is via DRF + coordinator (`mcp_lanes/`).

| `lane_id` | Tools (`records[].tools`) |
|-----------|---------------------------|
| `context-assets` | `bounded_packets`, `catalog_search_get`, `loadout_resolution`, `repo_ci_pr_reads`, `repo_health`, `open_prs`, `ci_status`, `work_lookup`, `session_brief` |
| `delegation-coordination` | `org_profile_read`, `loadout_preview`, `assignment`, `request`, `disposition`, `dispatch`, `handoff` |
| `evidence-governance` | `artifacts`, `findings_anomalies`, `reports`, `review_acceptance`, `escalation`, `policy_explanation`, `ordinary_authorized_gate_actions` |
| `maintenance` | `schedule_status_run`, `registered_check_execution`, `maintenance_evidence`, `health`, `recovery_test_results` |
| `skills-scripts` | `list_skills`, `get_skill`, `list_scripts`, `describe_script`, `request_script_run` |
| `workflow-control` | `work_status`, `queue_status`, `dependency_status`, `gate_status`, `budget_status`, `preview`, `step`, `run`, `pause`, `resume`, `cancel`, `reconcile` |

`context-assets` also binds the five stdio-compat tools (`stdio_compat_tools` in catalog).
`workflow-control` notes: design-only lane profile; DRF calls in later milestones.

Lane tools invoke DRF only. Forbidden operations listed per lane in catalog JSON
(e.g. `direct_database_access`, `provider_cli_invocation`).

---

## Surface enum reference

| Surface | Typical entry |
|---------|---------------|
| `cli` | `flowctl` kernel / runtime |
| `rest` | DRF API |
| `mcp` | `flowctl-mcp` or lane containers |
| `worker` | Celery |
| `schedule` | Celery Beat |
| `test` | pytest fixtures |

---

## See also

- [Glossary](glossary.md)
- [Auth and security](../guides/auth-and-security.md)
- [Architecture and execution paths](../guides/architecture-and-execution.md)
