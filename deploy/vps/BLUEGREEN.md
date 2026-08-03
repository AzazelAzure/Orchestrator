# Orchestrator blue/green — installation edge contract

Orchestrator owns **deploy tooling** and **per-app upstream selection** for its API and ops-console hosts. The shared TLS edge (installation-local proxy) terminates HTTPS and routes each product independently.

## Topology

| Tier | Singleton (one active mutation plane) | Color slots (presentation only) |
|------|--------------------------------------|----------------------------------|
| Redis, coordinator, worker, scheduler, script-* | Yes | No |
| DRF API | No | `api-blue`, `api-green` (in-container :8000) |
| Ops console | No | blue/green via `run_ops_console.sh` (in-container :8081) |

- **Single SQLite writer:** only the coordinator mounts `orchestrator-data`.
- **No per-color database duplication.**
- Color API containers are stateless frontends (`COORDINATOR_URL` → singleton coordinator).
- **Compose identity:** all VPS compose wrappers `cd "$ORCH_ROOT"` with `COMPOSE_PROJECT_NAME=orchestrator` (override via `ORCH_COMPOSE_PROJECT`).

## Presentation routing (no host port publish by default)

Presentation API and console tiers **do not publish host ports** on VPS by default. External traffic enters only through the installation edge proxy (`:8443`). Each color uses an isolated Podman network with stable DNS aliases:

| Color | Network | API alias | Console alias |
|-------|---------|-----------|---------------|
| blue | `orchestrator-console-blue` | `orch-api-blue:8000` | `orch-console-blue:8081` |
| green | `orchestrator-console-green` | `orch-api-green:8000` | `orch-console-green:8081` |

The installation edge proxy attaches to both networks (owned by installation deploy tooling) and routes via `$orch_active_color`-keyed nginx maps to those aliases. Do not rely on `host.containers.internal` or bridge-gateway host binds for Orchestrator presentation.

**Optional loopback diagnostics:** set `ORCH_DIAG_BIND=127.0.0.1` in `.env.vps` to publish `127.0.0.1:8000/8010/8081/8091` only (merge `docker-compose.bluegreen.diag.yml`). Never use `0.0.0.0` or installation bridge gateways for presentation publish.

## Per-color isolated console networks

`ensure_presentation_networks.sh` creates `orchestrator-console-{color}` when missing. `run_ops_console.sh` connects **exactly one** matching `api-{color}` container with aliases `api` (console upstream) and `orch-api-{color}` (edge routing), then runs the console with alias `orch-console-{color}`.

Recreating an API container requires re-running `run_ops_console.sh` for that color to re-attach aliases.

## Installation edge selector (generic contract)

The edge proxy includes a dedicated Orchestrator color map file (separate from other products' selectors):

```nginx
map $request_uri $orch_active_color {
    default blue;
}
```

Orchestrator API and console vhosts use **Orchestrator-only** upstream maps keyed by `$orch_active_color`, for example:

```nginx
map $orch_active_color $orch_api_upstream {
    blue   orch-api-blue:8000;
    green  orch-api-green:8000;
}

map $orch_active_color $orch_console_upstream {
    blue   orch-console-blue:8081;
    green  orch-console-green:8081;
}
```

Other products' upstream maps and selectors must remain unchanged when Orchestrator flips color.

## Orchestrator deploy commands

From the Orchestrator repo on the VPS (`~/orchestrator`):

```bash
# Shared plane refresh (preserves orchestrator-data volume; never use down -v)
bash deploy/vps/vps_bootstrap.sh orch-shared

# Materialize idle green first (repair grant; public selector stays blue)
ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh deploy --color green
ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh smoke --color green

# Refresh live blue presentation
ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh deploy --color blue

# Status and digests per slot (exact-one discovery; fails on ambiguous containers)
bash deploy/vps/orch_color.sh status
```

**Traffic promotion** (`switch`) requires a separate authorization grant. Initial materialization sets `ORCH_COLOR_MATERIALIZE_ONLY=1` (default) so `orch_color.sh switch` is blocked.

## Selector write and rollback

- `orch_color.sh switch` writes the edge `orch_active_color.conf` map, runs `nginx -t`, then reloads **only** the proxy container.
- Prior selector color is stored under Orchestrator-owned `deploy/vps/.state/orch_active_color.prev` (not in another product's secret directories).
- On reload or post-switch smoke failure, the script restores the prior map and reloads again.

## Off-color smoke (no DNS/ZT mutation)

Validate the idle slot on the presentation network before any public flip:

```bash
bash deploy/vps/orch_color.sh smoke --color green
bash deploy/vps/healthcheck.sh green
```

Probes use `podman exec` and ephemeral in-network curls — not host-wide port assumptions.

Origin Host-header curls through the edge proxy (`https://127.0.0.1:8443/...`) remain the pre-promotion bar for sibling products.

## Guards

- `ORCH_COLOR_MATERIALIZE_ONLY=1` (default) blocks `switch` until a separate grant authorizes traffic promotion.
- Shared-plane identity guard: `deploy --color` captures shared container IDs before presentation recreate and aborts if they change.
- `orch_color.sh status` fails closed when zero presentation API slots are running.
