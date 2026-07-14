# Architecture

## Layers

```
CLI (flowctl) / optional MCP stdio (flowctl-mcp)
        │
        ▼
Application services (work, queue, resource/lease, gate, finding, artifact, policy)
        │
        ▼
SQLite persistence (WAL) + migrations
```

The installable import package is `flow_engine`. The distribution/project name is
`orchestrator`.

## Core concepts

| Concept | Role |
|--------|------|
| Project | Namespace for work and resources |
| Work item | Claimable unit with dependencies and gates |
| Resource lease | Temporal claim (advisory or strict) |
| Gate / waiver | Completion controls with auditable waiver history |
| Finding | Generic issue record with amendment history |
| Artifact / policy | Metadata pointers and versioned policy references |

## Read-only capabilities

Optional project capabilities resolve a logical `project_id` through a local
`projects.json` binding file:

```json
{
  "projects": {
    "demo_project": {
      "checkout_path": "/path/to/checkout",
      "engine_project_name": "demo_project"
    }
  }
}
```

Config resolution order:

1. Explicit CLI / API argument
2. `FLOW_PROJECTS_CONFIG`
3. `~/.config/orchestrator/projects.json`

Capabilities: `repo_health`, `open_prs`, `ci_status`, `work_lookup`,
`session_brief`. MCP tool names match these capability names. Default transport
is read-only.

## Boundaries

- No product-specific adapters ship in this repository.
- No Django app or deploy containers are included in this candidate.
- Engine backup/restore helpers that exist in tests are not a hosted operations platform.

## Verification posture

Local `pytest` coverage exercises kernel concurrency, governance invariants,
CLI, capabilities, and MCP transport. Passing tests does not by itself assert
production readiness.
