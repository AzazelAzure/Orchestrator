# Orchestrator blue/green — installation edge contract

Orchestrator owns **deploy tooling** and **per-app upstream selection** for its API and ops-console hosts. The shared TLS edge (installation-local proxy) terminates HTTPS and routes each product independently.

## Topology

| Tier | Singleton (one active mutation plane) | Color slots (presentation only) |
|------|--------------------------------------|----------------------------------|
| Redis, coordinator, worker, scheduler, script-* | Yes | No |
| DRF API | No | `api-blue` (:8000), `api-green` (:8010) |
| Ops console | No | blue (:8081), green (:8091) via `run_ops_console.sh` |

- **Single SQLite writer:** only the coordinator mounts `orchestrator-data`.
- **No per-color database duplication.**
- Color API containers are stateless frontends (`COORDINATOR_URL` → singleton coordinator).
- **Compose identity:** all VPS compose wrappers `cd "$ORCH_ROOT"` with `COMPOSE_PROJECT_NAME=orchestrator` (override via `ORCH_COMPOSE_PROJECT`).

## Port matrix (`ORCH_PUBLISH_HOST` — not 0.0.0.0)

Presentation API and console ports bind to **`ORCH_PUBLISH_HOST`** in `.env.vps` (installation Podman bridge gateway — reachable from the `fm-beta` proxy container, not routable from the internet). **Only `:8443` is the public entry.** Example installation gateway: `10.89.1.1` (set per host; never commit installation-specific values as portable defaults).

| Slot | Blue (live default) | Green (idle) |
|------|---------------------|--------------|
| API | `${ORCH_PUBLISH_HOST}:8000` | `${ORCH_PUBLISH_HOST}:8010` |
| Ops console | `${ORCH_PUBLISH_HOST}:8081` | `${ORCH_PUBLISH_HOST}:8091` |

HFM ecosystem vhosts must use the same `ORCH_PUBLISH_HOST` (render via `scripts/ops/render_ecosystem_hosts.sh`) — do not point Orchestrator upstreams at `host.containers.internal` when it resolves to the public host address.

## Per-color isolated console networks

`run_ops_console.sh` creates `orchestrator-console-{color}`, connects **exactly one** matching `api-{color}` container with alias `api`, and runs the console on that network. nginx upstream `api:8000` resolves deterministically — no shared `api` alias across both presentation services on `orchestrator_frontend`.

Recreating an API container requires re-running `run_ops_console.sh` for that color to re-attach the alias.

## Installation edge selector (generic contract)

The edge proxy includes a dedicated Orchestrator color map file (separate from other products' selectors):

```nginx
map $request_uri $orch_active_color {
    default blue;
}
```

Orchestrator API and console vhosts use **Orchestrator-only** upstream maps keyed by `$orch_active_color`, for example:

```nginx
map $orch_active_color $orch_api_loopback {
    blue   10.89.1.1:8000;   # ORCH_PUBLISH_HOST — installation-specific
    green  10.89.1.1:8010;
}

map $orch_active_color $orch_console_loopback {
    blue   10.89.1.1:8081;
    green  10.89.1.1:8091;
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

Validate the idle slot on host-published ports before any public flip:

```bash
curl -fsS http://127.0.0.1:8010/health/
curl -fsS http://127.0.0.1:8091/
# Bearer schema/docs + console /ops/summary/ proxy (see orch_color.sh smoke)
```

Origin Host-header curls through the edge proxy (`https://127.0.0.1:8443/...`) remain the pre-promotion bar for sibling products.

## Guards

- **Inactive-color sibling restart:** presentation deploy uses `--no-deps` and aborts if shared-plane container IDs change.
- **Per-slot identity:** `orch_color.sh status` reports per-color image digest; zero or multiple matching containers fail closed.
- **Script-runner:** `healthcheck.sh` requires `script-runner` running and `script-spool-init` successfully exited.
- **Reboot:** disable legacy `ops-console.service`; shared plane systemd unit excludes presentation API.
- **Build context:** `docker-compose.bluegreen.yml` uses `build.context: .` (repo root when CWD is `$ORCH_ROOT`).

## Repair redeploy abort bars

Abort on: selector ≠ blue; coordinator count ≠ 1; volume ≠ `orchestrator_orchestrator-data`; backup integrity failure; protected path deletion during sync; missing/duplicate service discovery; exited script-runner; console cross-color routing; external reachability of `8000/8010/8081/8091`; sibling HTTP regression.

See [`README.md`](README.md) for full backup, sync contract, and evidence order.
