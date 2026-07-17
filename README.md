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
- Portable repo-local skills under `skills/`

## What this is not

- Not a product adapter for any specific business application
- Not a deployed SaaS, Django app, or container image (none are included yet)
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
core bundle and a six-skill opt-in extended bundle. See `docs/skills.md` for
discovery and install mappings.

## License

GNU Affero General Public License v3.0 only — see `LICENSE`.
