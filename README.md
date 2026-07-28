# Orchestrator

Generic SQLite-backed workflow orchestrator core.

This repository is an AGPL-3.0-only public-release **candidate** derived from a
verified generic engine core. The installable Python package remains
`flow_engine` for compatibility; the project/distribution name is
`orchestrator`.

## What this is

- Work items, queues, resources/leases, gates/waivers, findings, artifacts, and policy metadata in SQLite
- CLI (`flowctl`) for core operations
- Optional read-only MCP stdio server (`flowctl-mcp`) over generic project capabilities
- R4A–R4D local control-plane stack: DRF API, coordinator service, Redis/Celery
  mock delivery, five MCP lanes, registered-script sandbox, schedules, and a
  rootless-container active-test harness (see `docs/r4-control-plane.md`)
- Portable repo-local skills under `skills/`

## What this is not

- Not a product adapter for any specific business application
- Not a deployed SaaS or production-hosted control plane (R4 Compose is local
  active-test only)
- Not production-hardened by publication of this candidate alone

## Requirements

- Python 3.12+

## Install (local)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
# Optional MCP transport:
pip install -e '.[mcp]'
```

## Tests

```bash
pytest
```

## CLI

```bash
flowctl --help
```

Database path defaults are resolved by the CLI; override with `FLOW_DB_PATH` when needed.

## Read-only MCP (optional)

```bash
pip install -e '.[mcp]'
flowctl-mcp
```

The MCP server exposes only the approved read-only tools (`repo_health`,
`open_prs`, `ci_status`, `work_lookup`, `session_brief`). Configure logical
projects via `FLOW_PROJECTS_CONFIG` pointing at a `projects.json` (see
`docs/architecture.md`). Default config location is
`~/.config/orchestrator/projects.json` (XDG-style; no product-specific path).

## Skills

Eleven canonical packages live in `skills/`, split into a five-skill default
core bundle and a six-skill opt-in extended bundle. Seventeen positional R3
skills ship in the opt-in `positional` bundle. See `docs/skills.md` for
discovery and install mappings.

## R1 inert catalogs

Portable asset, MCP-lane, loadout, script, and policy contracts live under
`agentic/catalogs/`. They are schema-validated and content-hashed, discoverable
from the agentic manifest, and **inert** (not runtime activation). See
`docs/r1-assets.md`.

## R2 persistent runtime

Governed runs, attempts, credits, mock providers, and recovery are documented in
`docs/r2-runtime.md`.

## R3 organization and delegation

Hierarchy, scoped delegation, twelve resolved positional loadouts, and dispatch
pins are documented in `docs/r3-organization.md`. R2 system-test grants remain
as an explicit compatibility path.

## R4 local control plane (R4A–R4D)

Django/DRF API, coordinator HTTP service, Redis/Celery mock delivery, and
Compose-based MCP, script, and schedule services are documented in
`docs/r4-control-plane.md`. Install optional extras with
`pip install -e '.[control-plane,dev]'`.

The final local active-test functional run
`r4d-20260726T113210Z-2768705` passed all 11 evidence steps under rootless
Podman with runtime fingerprint
`956096e8560ceea53b33e5420c58f4ad6ca7da606c5587475a296e05c39f51cb`.
The full source suite also passed with **316 passed, 1 skipped**. This is
technical evidence only: independent review found the
`G-ORCH-LOCAL-CONTROL-PLANE` acceptance package insufficient, it does not close
any governance gate, and real Codex/Cursor/Claude provider adapters remain out
of scope.

## License

GNU Affero General Public License v3.0 only — see `LICENSE`.
