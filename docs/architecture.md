# Architecture

## Layers

```
CLI (flowctl) / optional MCP stdio (flowctl-mcp)
        │
        ▼
DRF API (R4A) ──HTTP──► state-coordinator (sole SQLite writer)
Celery worker ──HTTP──► state-coordinator
        │
        ▼
Application services (runtime, org, delegation)
        │
        ▼
SQLite persistence (WAL) + migrations
Redis (Celery broker only; non-authoritative)
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
| Runtime run / attempt | R2 governed execution with credits, leases, invocations |
| Organization / assignment | R3 hierarchy seats, delegation, resolved loadouts |
| State coordinator | Sole SQLite writer for R2/R3 command ledger + transitions |

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

## R1 inert catalogs

Versioned asset/loadout/MCP-lane/script/policy catalogs live under
`agentic/catalogs/` and are documented in `docs/r1-assets.md`. They are
discoverable via `agentic/manifest.json` but remain inert contracts: not
runtime enforcement, not installation-policy activation, and not executable
repository scripts.

## R2 persistent runtime

Additive migrations and the state coordinator implement governed runs, attempts,
provider invocations, credit reservations, audit events, and recovery without
organization/loadout semantics on the R2 compatibility grant path. Mock provider
runners satisfy the host-runner protocol; real installation bindings stay
untracked. See `docs/r2-runtime.md`.

## R3 organization and loadouts

Additive migration 004 and coordinator commands implement organization profiles,
assignments, scoped delegation, twelve positional loadout resolutions, and
immutable dispatch pins. See `docs/r3-organization.md`. Catalog JSON remains a
discoverable inert contract; R3 services enforce pins at dispatch.

## R4 local control plane (R4A–R4D)

Additive migration 005, Django/DRF API, coordinator HTTP service, Redis/Celery
mock delivery, and Docker Compose implement the service boundary and mock
delivery slice. See `docs/r4-control-plane.md`. MCP lanes, script sandbox,
schedules, and Compose active-test harness (R4D) are present. Rootless-Podman
run `r4d-20260726T113210Z-2768705` passed all 11 evidence steps with fingerprint
`956096e8560ceea53b33e5420c58f4ad6ca7da606c5587475a296e05c39f51cb`;
the full source suite passed with 316 passed and 1 skipped. Independent review
found the local-control-plane acceptance evidence insufficient, including gaps
in redelivery proof, restart continuity, post-teardown zero-state capture, and
credential-safe compose-config evidence. No gate is closed; real provider
adapters are not implemented.

Security posture for R4A: coordinator is network-internal (9001 unpublished);
API and worker use distinct service credentials; principal/role/grant are
resolved server-side; Redis requires authentication and is unpublished;
provider I/O runs outside the SQLite transaction after durable intent;
containers run non-root with `cap_drop: ALL`, read-only root, and an internal
backend network.

## Boundaries

- No product-specific adapters ship in this repository.
- R4 control-plane slices R4A–R4D may be present in-tree; none of them close
  `G-ORCH-LOCAL-CONTROL-PLANE` without independent review + operator active-test evidence.
- Django/DRF and Compose are included for local control-plane testing only; not production hosting.
- R2 `SystemTestGrant` compatibility path continues to refuse org/loadout semantics.

## Verification posture

Local `pytest` coverage exercises kernel concurrency, governance invariants,
CLI, capabilities, MCP transport, R2 runtime controls, R3
organization/delegation, and R4A control-plane boundaries. Passing tests does
not by itself assert production readiness or close Headquarters gates.
