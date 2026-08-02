# Authentication and authorization

**Audience:** operators configuring a local stack and contributors wiring clients.

**Scope labels:**

- **Local candidate:** env vars + `flowctl auth` against `http://127.0.0.1:8000`
- **Live acceptance:** Compose-injected ephemeral tokens + human auth migration `008`
- **Production / VPS:** `deploy/` overlay examples only; not validated as landed product

**Source anchors:** `control_plane/user_auth.py`, `control_plane/api/views_auth.py`,
`control_plane/authz_matrix.py`, `control_plane/bootstrap.py`, `control_plane/service_auth.py`,
`control_plane/coordinator_client.py`, `workers/tasks.py`, `docker-compose.yml`,
`cli/auth_cmds.py`, `persistence/migrations/008_user_auth.sql`.

---

## Identity model

Orchestrator separates **principal kinds** (stored in coordinator SQLite) from
**human user accounts** (migration 008). Principals authenticate with **bearer
tokens** (`Authorization: Bearer` on DRF, or `X-Orchestrator-Principal-Token` on
coordinator HTTP). That is distinct from **service transport credentials** used
only on coordinator HTTP (`X-Orchestrator-Service-Token`).

| Kind | Typical use | Principal bearer (bootstrap env) |
|------|-------------|----------------------------------|
| `founder` | Operator step-up, recovery, principal admin | `ORCH_TOKEN_FOUNDER` (or human account with founder role) |
| `human` | End-user accounts | Password login → access/refresh/PAT digests (not `ORCH_TOKEN_*`) |
| `worker` | Celery delivery / provider workers | `ORCH_TOKEN_WORKER`; per-provider: `ORCH_TOKEN_WORKER_CODEX`, `ORCH_TOKEN_WORKER_CURSOR`, `ORCH_TOKEN_WORKER_CLAUDE` |
| `mcp_service` | R4 lane service principal | `ORCH_TOKEN_MCP` (R4A compat) + per-lane keys (see bootstrap table below) |
| `scheduler` | Celery Beat schedule ticks | `ORCH_TOKEN_SCHEDULER` |
| `provider_invocation` | Scoped delivery identity | `ORCH_TOKEN_PROVIDER_INVOCATION` |
| `system` | Internal resolver paths | Service commands only (no bootstrap bearer) |

`PrincipalRole` (`founder`, `worker`, `executive`, `manager`, `system`) layers
on top of kind for command matrix checks.

---

## Registration gate (fail-closed)

User self-registration is **off by default**:

```python
# control_plane/user_auth.py
def registration_allowed() -> bool:
    return os.environ.get("ORCH_ALLOW_USER_REGISTRATION", "0") == "1"
```

| Condition | `POST /api/v1/auth/register` behavior |
|-----------|--------------------------------------|
| `ORCH_ALLOW_USER_REGISTRATION=0` (default) | **403** `"user registration is disabled"` |
| `ORCH_ALLOW_USER_REGISTRATION=1` | System principal executes `auth.register_user` |
| Founder `Authorization: Bearer` present | Coordinator resolves founder via `principal_token`; founder can register users server-side |

Compose projects the flag into **both** `api` and `coordinator` services
(`docker-compose.yml`). VPS overlay pins literal `"0"`.

---

## Login, access, refresh, logout

### HTTP endpoints (`control_plane/api/urls.py`)

| Method | Path | Auth required |
|--------|------|---------------|
| POST | `/api/v1/auth/register` | AllowAny (gated by env / founder bearer) |
| POST | `/api/v1/auth/login` | AllowAny |
| POST | `/api/v1/auth/refresh` | AllowAny (refresh token in body) |
| POST | `/api/v1/auth/logout` | AllowAny (token in body) |
| GET | `/api/v1/auth/me` | Bearer access or PAT |
| POST | `/api/v1/auth/token` | Bearer (issue PAT) |
| POST | `/api/v1/auth/token/<id>/revoke` | Bearer |

### Credential properties

- Opaque secrets (`access`, `refresh`, `pat`); **SHA-256 digests at rest** only.
- No JWT dependency.
- Default TTLs (`user_auth.py`): access 30m, refresh 14d, PAT 365d (env-overridable).
- Refresh rotation with replay-safe family revocation.
- Login throttle durable in SQLite (`auth.throttle_check`); multi-worker safe.

Password hashing uses Django `make_password` / `check_password` only.

---

## Personal access tokens (PAT)

```bash
flowctl auth login --api-url http://127.0.0.1:8000   # password path
flowctl auth token --label my-automation             # issues PAT; no echo by default
flowctl auth token --show-token                      # unsafe: print once
```

Revocation: `POST /api/v1/auth/token/<credential_id>/revoke` with active bearer.

---

## CLI credential storage and precedence

Validated against `flowctl auth --help` and `cli/auth_cmds.py`.

### Storage location

| Priority | Path |
|----------|------|
| 1 | `ORCH_CREDENTIALS_PATH` |
| 2 | `$XDG_CONFIG_HOME/orchestrator/credentials.json` |
| 3 | `~/.config/orchestrator/credentials.json` |

File mode **0600** enforced on write; group/world readable files are refused.

### Bearer resolution (`resolve_bearer_token`)

| Priority | Source |
|----------|--------|
| 1 | `--token` (explicit) |
| 2 | `--token-file` |
| 3 | `ORCH_USER_TOKEN` env |
| 4 | Stored `access_token` or `pat` in credentials file |

`flowctl auth status` never prints secrets unless `--show-token`.

### API URL

`--api-url` → `ORCH_API_URL` → default `http://127.0.0.1:8000`.

---

## Bootstrap principal tokens vs service transport credentials

Bootstrap is **off by default** (`ORCH_BOOTSTRAP_PRINCIPALS=0`). When enabled in
local Compose, `bootstrap_principals_from_env()` registers principals from injected
`ORCH_TOKEN_*` values (`control_plane/bootstrap.py`, `docker-compose.yml`) —
never from source-controlled fixed secrets.

### Principal bearer tokens (`ORCH_TOKEN_*`)

Registered as principal `token_digest` values. Used as:

- DRF: `Authorization: Bearer <token>` or `X-Orchestrator-Token`
- Coordinator HTTP: `X-Orchestrator-Principal-Token` (alongside service transport)
- MCP lane containers: lane service token + initiating principal bearer on DRF invokes

| Env var | Principal key (`bootstrap.py`) |
|---------|-------------------------------|
| `ORCH_TOKEN_FOUNDER` | `founder` |
| `ORCH_TOKEN_SCHEDULER` | `scheduler` |
| `ORCH_TOKEN_MCP` | `mcp-service` (R4A compat) |
| `ORCH_TOKEN_WORKER` | `worker` |
| `ORCH_TOKEN_WORKER_CODEX` | `worker.provider.codex` |
| `ORCH_TOKEN_WORKER_CURSOR` | `worker.provider.cursor` |
| `ORCH_TOKEN_WORKER_CLAUDE` | `worker.provider.claude` |
| `ORCH_TOKEN_PROVIDER_INVOCATION` | `provider-invocation` |
| `ORCH_TOKEN_MCP_CONTEXT_ASSETS` | `mcp.lane.context-assets` |
| `ORCH_TOKEN_MCP_WORKFLOW_CONTROL` | `mcp.lane.workflow-control` |
| `ORCH_TOKEN_MCP_DELEGATION_COORDINATION` | `mcp.lane.delegation-coordination` |
| `ORCH_TOKEN_MCP_EVIDENCE_GOVERNANCE` | `mcp.lane.evidence-governance` |
| `ORCH_TOKEN_MCP_MAINTENANCE` | `mcp.lane.maintenance` |
| `ORCH_TOKEN_MCP_SKILLS_SCRIPTS` | `mcp.lane.skills-scripts` |

Provider delivery tasks pass `ORCH_TOKEN_WORKER_<PROVIDER>` as `principal_token`
on coordinator commands (`workers/tasks.py`). Schedule ticks pass
`ORCH_TOKEN_SCHEDULER` as `principal_token` (`schedule_tick`).

### Service transport credentials (not principal identity)

Distinct secrets for **which service** may call coordinator HTTP
(`control_plane/service_auth.py`, `coordinator_client.py`). Sent as
`X-Orchestrator-Service-Token` with `X-Orchestrator-Service-Kind` (`api` or
`worker`). Must differ from each other (fail closed if equal).

| Env var | Service kind | Typical caller |
|---------|--------------|----------------|
| `ORCH_API_SERVICE_TOKEN` | `api` | DRF API → coordinator; scheduler tick transport (`_scheduler_client` uses `service_kind="api"`) |
| `ORCH_WORKER_SERVICE_TOKEN` | `worker` | Celery worker / script-worker → coordinator |

**Do not conflate:** `ORCH_WORKER_SERVICE_TOKEN` authenticates the worker
**process** to coordinator HTTP. `ORCH_TOKEN_WORKER` / `ORCH_TOKEN_WORKER_*`
identify the **worker principal** for authz on commands (delivery, heartbeat,
result, provider preflight/settle).

Sync principal and service env after rotation: `scripts/local_stack_sync_tokens.py`.

### External consumer pattern (installation-local)

Tools outside this repository that call `GET /ops/summary/` should resolve
credentials with the same fail-closed precedence their maintainers document
(env file → env var → anonymous). Anonymous access to `/ops/summary/` returns
**403** when auth is enabled.

---

## Authorization matrix (deny by default)

`control_plane/authz_matrix.py` maps `command_type` → allowed principal kinds.

**Representative rules:**

| Command family | Allowed kinds |
|----------------|---------------|
| `runtime.preview/run/create/step/...` | `founder` (most); `worker` for claim/heartbeat/result/delivery |
| `runtime.new_attempt_after_unknown`, `runtime.waive_gate` | `founder` only (`founder.ops` widens the matrix pre-check in `authz_matrix.py` but `coordinator/authz.py` still requires `PrincipalRole.FOUNDER` for `FOUNDER_ONLY_COMMANDS`) |
| `runtime.recover_*`, `delivery.recover_stale` | `founder` or `recovery.control_plane` capability |
| `mcp.tool.invoke` | `founder`, `worker`, `scheduler` (initiating principal) |
| `ops.dashboard_read` | `founder`, `system`, `scheduler`, `mcp_service`, or `ops.read` capability |
| Unknown command types | **founder only** |

DRF views attach `command_type` and use `RequireEndpointCapability`
(`control_plane/api/permissions.py`). MCP service principals are denied
founder-only REST endpoints (`DenyMCPService`).

### Capabilities that widen access

| Capability | Effect |
|------------|--------|
| `ops.read` | `GET /ops/summary/` for `human` principals |
| `recovery.control_plane` | Recovery commands for non-founder kinds |
| `founder.ops` | Matrix pre-check for `FOUNDER_OPS_COMMANDS`; does not bypass coordinator founder-role gate |

---

## Anonymous allowlist summary

| Surface | Anonymous allowed |
|---------|-------------------|
| `GET /health/` | Yes |
| Auth register/login/refresh/logout | Yes (register gated) |
| `GET /api/schema/` | **Yes** — `drf-spectacular` `SERVE_PERMISSIONS` defaults to `AllowAny` on `SpectacularAPIView` (`control_plane/urls.py`; `SPECTACULAR_SETTINGS` does not override). Anonymous callers receive **200** (`test_openapi_schema_anonymous`). **Follow-up risk:** full OpenAPI schema without bearer — separate runtime hardening/packaging work item. |
| `GET /api/docs/` | **Yes (permission only)** — `SpectacularSwaggerView` uses the same `SERVE_PERMISSIONS` default (`test_openapi_docs_serve_permissions_allow_any`). Swagger UI HTML needs Django `TEMPLATES` APP_DIRS (not configured today); anonymous requests may **500** locally when `drf_spectacular/swagger_ui.html` is missing — packaging defect, same follow-up work item. |
| All `/api/v1/*` | **No** |
| `GET /ops/summary/` | **No** (requires auth + founder or `ops.read`) |
| Coordinator HTTP | **No** (service tokens only) |
| `flowctl` kernel commands | Yes (local SQLite; no network auth) |
| `flowctl auth *` | Uses stored credentials when calling API |
| `flowctl-mcp` | Yes (read-only local DB; no network) |

`REST_FRAMEWORK.DEFAULT_PERMISSION_CLASSES` (`IsAuthenticated`) applies to DRF
`APIView` routes under `/api/v1/*` only. OpenAPI schema/docs bypass that default
via drf-spectacular's class-level `SERVE_PERMISSIONS`. Treat anonymous schema
exposure as a documented follow-up risk, not a closed security boundary.

---

## Threat and security model (local candidate)

| Threat | Mitigation in tree |
|--------|-------------------|
| Token leakage in logs | Redaction in provider runner, script results, MCP envelopes |
| Stolen refresh token | Family revocation on replay |
| Credential file world-readable | Refused at load (`_assert_private_file`) |
| MCP lane privilege escalation | Dual identity + catalog snapshot enforcement |
| Direct SQLite writes from API | Coordinator sole-writer |
| Script arbitrary execution | Allowlist + attested runner image + network isolation |
| Brute-force login | Durable throttle in coordinator |
| Caller-controlled test hooks in prod schemas | Rejected at DRF/MCP boundary (`r4-control-plane.md`) |

**Out of scope for this repo:** production WAF, multi-tenant isolation, HSM key
storage, and hosted SaaS hardening.

---

## Founder step-up (CLI only)

`flowctl runtime new-attempt` requires founder role plus `StepUpEvidence`
(reauthentication ≤5 minutes, reason, evidence artifact, duplicate-cost ack,
policy revision, new idempotency identity). MCP and schedule surfaces cannot
issue this command (`coordinator/commands.py` → `FOUNDER_ONLY_COMMANDS` /
`MCP_FORBIDDEN_COMMANDS`; enforced in `coordinator/authz.py`).

---

## See also

- [Architecture and execution paths](architecture-and-execution.md)
- [Operator runbook](operator-runbook.md)
- [Troubleshooting](troubleshooting.md) — auth 401/403 section
- [Surface reference](../reference/surfaces.md)
- [`r4-control-plane.md`](../r4-control-plane.md) — Compose auth env block
