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

## Port matrix (loopback / host-gateway)

| Slot | Blue (live default) | Green (idle) |
|------|---------------------|--------------|
| API | `127.0.0.1:8000` | `127.0.0.1:8010` |
| Ops console | `127.0.0.1:8081` | `127.0.0.1:8091` |

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
    blue   host.containers.internal:8000;
    green  host.containers.internal:8010;
}

map $orch_active_color $orch_console_loopback {
    blue   host.containers.internal:8081;
    green  host.containers.internal:8091;
}
```

Other products' upstream maps and selectors must remain unchanged when Orchestrator flips color.

## Orchestrator deploy commands

From the Orchestrator repo on the VPS (`~/orchestrator`):

```bash
# Shared plane refresh (preserves orchestrator-data volume; never use down -v)
bash deploy/vps/vps_bootstrap.sh orch-shared

# Materialize idle green (public selector stays blue)
ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh deploy --color green
ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh smoke --color green

# Status and digests per slot
bash deploy/vps/orch_color.sh status
```

**Traffic promotion** (`switch`) requires a separate authorization grant. Initial materialization sets `ORCH_COLOR_MATERIALIZE_ONLY=1` (default) so `orch_color.sh switch` is blocked.

## Selector write and rollback

- `orch_color.sh switch` writes the edge `orch_active_color.conf` map, runs `nginx -t`, then reloads **only** the proxy container.
- Prior selector color is stored under Orchestrator-owned `deploy/vps/.state/orch_active_color.prev` (not in another product's secret directories).
- On reload or post-switch smoke failure, the script restores the prior map and reloads again.

## Off-color smoke (no DNS/ZT mutation)

Validate the idle slot on loopback before any public flip:

```bash
curl -fsS http://127.0.0.1:8010/health/
curl -fsS http://127.0.0.1:8091/
# Bearer schema/docs against idle API (see orch_color.sh smoke)
```

Origin Host-header curls through the edge proxy (`https://127.0.0.1:8443/...`) remain the pre-promotion bar for sibling products.

## Console build-time API URL

Each color console image is built with an explicit `VITE_API_BASE_URL`. Static console delivery on the idle port does **not** prove browser E2E routing to the idle API — test the idle API directly on loopback.

## Guards

- **Inactive-color sibling restart:** presentation deploy uses `--no-deps` and aborts if shared-plane container IDs change.
- **Per-slot identity:** `orch_color.sh status` reports per-color image digest (not checkout HEAD alone).
- **Reboot:** disable legacy `ops-console.service`; shared plane systemd unit excludes presentation API.

See [`README.md`](README.md) for full backup, disable, and evidence order.
