# Developer and test guide

**Audience:** contributors extending the engine, control plane, or documentation.

**Source of truth:** this branch's `src/flow_engine/`, `tests/`, `pyproject.toml`,
and `scripts/`.

---

## Repository map

```
Orchestrator/
├── src/flow_engine/          # Installable package (import: flow_engine)
├── tests/                    # pytest suite
├── scripts/                  # Operator / acceptance automation (not imported)
├── agentic/                  # R1 inert catalogs + manifest
├── skills/                   # Repo-local skill packages
├── deploy/                   # VPS overlay examples (not production claims)
├── docker-compose.yml        # Local control-plane stack
├── docs/                     # Documentation (start: read-the-docs.md)
├── AGENTS.md                 # Agent operating rules
└── CHANGELOG.md              # Material changes (CPPRD)
```

**Boundary:** do not import `scripts/` from package code. Do not add product-specific
adapters or private installation paths to `src/flow_engine/`.

---

## Extension points

| Extend | Hook | Constraints |
|--------|------|-------------|
| Kernel work/gates | `application/*_service.py` | Transactions via `persistence/transactions.py` |
| R2 runtime commands | `coordinator/commands.py` + handler | Register in authz matrix |
| REST endpoints | `control_plane/api/views_*.py` + `urls.py` | Coordinator client only for writes |
| MCP read tools | `capabilities/` + `mcp/server.py` | Read-only default |
| R4 MCP lane tools | `mcp_lanes/handlers.py` + catalog JSON | Dual identity; catalog snapshot |
| Provider binding | `providers/host_runner.py` | Bounded env, event allowlist, no shell |
| Script sandbox | `script_sandbox/` + allowlist registry | Attested runner image required |
| Schedules | `schedules/templates.py` | Asia/Manila; zero provider budget |
| Migrations | `persistence/migrations/NNN_*.sql` | Forward-only SQL files |

New coordinator commands **must** appear in `COMMAND_KIND_MATRIX` or they deny
non-founder principals (`authz_matrix.py`).

---

## Migration rules

1. Add numbered SQL file under `persistence/migrations/`.
2. Register in `persistence/migrations/__init__.py` apply order.
3. Add unit test covering apply + invariant (`tests/unit/test_r4_migration.py` pattern).
4. Document rollback as **restore from a verified backup** (see [operator runbook](operator-runbook.md)) when DDL is destructive.
5. Never edit applied migration content in place — add a new migration.

---

## Test taxonomy

| Directory / file | Covers |
|------------------|--------|
| `tests/unit/test_kernel.py` | Core queue/work/resource concurrency |
| `tests/contention/` | Work and resource claim races |
| `tests/unit/test_services.py` | Application services |
| `tests/unit/test_cli.py` | `flowctl` parser and kernel commands |
| `tests/unit/test_t03_governance.py` | Gates, findings, waivers |
| `tests/unit/test_r2_runtime.py` | Coordinator runtime lifecycle |
| `tests/unit/test_r3_organization.py` | Org, delegation, loadouts |
| `tests/unit/test_r4_api_auth.py` | DRF authn/authz |
| `tests/unit/test_r4_security.py` | Security boundaries |
| `tests/unit/test_r4_coordinator_boundary.py` | API never writes SQLite |
| `tests/unit/test_r4_delivery.py` | Celery delivery path |
| `tests/unit/test_r4b_mcp_lanes.py` | MCP lane gateway |
| `tests/unit/test_skills_scripts_mcp_lane.py` | Sixth lane |
| `tests/unit/test_r4c_scripts_schedules.py` | Script sandbox + schedules |
| `tests/unit/test_user_auth.py` | Human auth + credentials |
| `tests/unit/test_provider_host_runner.py` | Host runner protocol |
| `tests/unit/test_ops_summary.py` | Ops summary auth |
| `tests/unit/test_orchestrator_live_acceptance.py` | Live acceptance script |
| `tests/unit/test_local_stack_*.py` | Local stack helpers |
| `tests/mcp/` | Stdio MCP integration |

### Focused runs

```bash
pytest tests/unit/test_kernel.py -q
pytest tests/unit/test_r4_api_auth.py tests/unit/test_user_auth.py -q
pytest tests/unit/test_provider_host_runner.py -q
```

Full suite (local candidate baseline):

```bash
pytest    # expect 350+ passed, 1 skipped (see CHANGELOG for pinned counts)
```

---

## CI expectations

CI runs `pytest` on push/PR (see `.github/workflows/` if present). Contributors should:

1. Run focused tests for touched modules.
2. Run full `pytest` before merge claims.
3. Label partial runs explicitly in handoffs.

Passing CI does **not** close external governance gates or assert VPS readiness.

---

## Changelog and CPPRD

Material changes require a complete `[Unreleased]` entry in `CHANGELOG.md`:

- **Added** / **Changed** / **Fixed** / **Verified** sections as appropriate
- No empty stubs
- Verification claims include run IDs or counts when asserting test state

CPPRD (this repo): commit + push (when remote exists) + PR when review required +
docs in same pass. Do not invent production readiness.

---

## Debugging tips

| Symptom | First checks |
|---------|--------------|
| CLI exit 2 on transition | `domain/transitions.py` — invalid state change |
| Coordinator reject envelope | JSON `error_code`, `error` in response body |
| DRF 403 | `authz_matrix.py` command vs principal kind |
| Worker idle | Redis connectivity; `ORCH_WORKER_SERVICE_TOKEN` (transport) and `ORCH_TOKEN_WORKER*` (principal) alignment |
| Provider hang | Host runner selector timeout; see troubleshooting guide |
| SQLite locked | Single-writer violation — find direct write bypass |

Enable structured JSON CLI output: `flowctl --json …`.

Django debug: only in local dev settings; not for production claims.

Coordinator logs: Compose service `coordinator` stdout.

---

## Documentation contributions

Authorized doc paths: `docs/**`, `README.md`, `CHANGELOG.md`.

Rules:

1. Every behavioral claim must trace to source, config, or test on this branch.
2. Distinguish kernel / local candidate / live acceptance / VPS.
3. No private absolute paths, secrets, or installation-specific branding.
4. Validate CLI flags against `flowctl --help`.
5. Check relative links (`python3 docs/_audit/check_links.py` if present).

Start edits from [`read-the-docs.md`](../read-the-docs.md) — mandatory entrypoint.

---

## See also

- [Architecture and execution paths](architecture-and-execution.md)
- [Operator runbook](operator-runbook.md)
- [Troubleshooting](troubleshooting.md)
- [`AGENTS.md`](../../AGENTS.md)
- [`skills.md`](../skills.md)
