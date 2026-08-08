# Orchestrator VPS bootstrap (shared hosting VPS)

Install Orchestrator on the shared hosting VPS for Cloudflare Tunnel → edge proxy `:8443` routing.

## One-command deploy (from dev machine)

```bash
# From Orchestrator repo root — rsync + remote bootstrap + smokes
bash deploy/vps/deploy_ecosystem.sh
```

Options: `--dry-run`, `--delete` (Orchestrator tree only; opt-in), `--skip-hfm`, `--skip-orch`, `--skip-portfolio`

SSH target: `VPS_SSH_TARGET`, or sibling finance-manager `.env` → `VPS_ORIGIN_IP` / `FM_SPRINT_SSH`.

## Safe Orchestrator sync contract

| Phase | Behavior |
|-------|----------|
| **Default / first repair sync** | `rsync -az` **without** `--delete` |
| **Later routine deploys** | Pass `--delete` explicitly for Orchestrator only after pre-sync backup/integrity/key-parity bars |

**Never delete** on the VPS Orchestrator tree (anchored excludes always applied):

- `/.env.vps`
- `/deploy/vps/.state/`
- `/deploy/attestations/`
- `/backups/`

Durable SQLite backups must live **outside** `~/orchestrator/` (e.g. `~/backups/orchestrator/`).

## What deploy does

1. Rsync installation edge-proxy tooling from the sibling ecosystem checkout (ecosystem template, render/attach scripts, nginx, compose, TLS script) — render + proxy network attach are owned by that checkout on VPS
2. Rsync Orchestrator → `~/orchestrator` (protected excludes; no-delete by default), sibling site tree → `~/portfolio`
3. Remote [`vps_bootstrap.sh`](vps_bootstrap.sh):
   - Generate `.env.vps` if missing (secrets only)
   - Presentation tier uses per-color Podman networks — **no host port publish** by default (optional `ORCH_DIAG_BIND=127.0.0.1`)
   - Build script-runner attestation; require regular JSON attestation file
   - Start **singleton shared mutation plane** (pinned Compose project `orchestrator`, CWD `$ORCH_ROOT`)
   - Run `healthcheck.sh shared` (strict script-runner / spool-init semantics)
   - Materialize blue presentation (`api-blue` + isolated console network)
   - Reload edge proxy (`COMPOSE_PROJECT_NAME=fm-beta`, proxy only; runs `ORCH_EDGE_PROXY_PRE_RELOAD_CMD` when configured)
   - Install systemd user units; **disable** legacy singleton `ops-console.service`
   - Smoke in-network presentation probes, proxy Host-header, and public ZT URLs

## Blue/green materialization order

See [`BLUEGREEN.md`](BLUEGREEN.md) for the generic edge contract.

1. **Backup** — SQLite integrity + volume identity (`orchestrator-data`); never `down -v`. Verify durable backup outside `~/orchestrator/`.
2. **Pre-sync bars** — selector blue; one coordinator; `.env.vps` mode 0600 + key-name parity; firewall + external negative port probes on `8000/8010/8081/8091`.
3. **Disable singleton console** — `systemctl --user disable --now ops-console.service` (also run by bootstrap).
4. **Daemon-reload** — install units; verify `orchestrator-ecosystem.service` starts shared plane only (no `api`).
5. **Shared refresh** — `bash deploy/vps/vps_bootstrap.sh orch-shared`
6. **Green presentation** (repair grant) — `ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh deploy --color green`
7. **Blue presentation** — `ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh deploy --color blue`
8. **Smoke both colors** — static console `/`, authenticated `/ops/summary/` via matching-color console network, bearer schema/docs, anonymous deny
9. **Sibling regression** — `bash deploy/vps/vps_bootstrap.sh smoke`
10. **Public selector stays blue** — do not run `orch_color.sh switch` without a separate promotion grant

Rollback rehearsal: `orch_color.sh rollback` restores Orchestrator-owned `deploy/vps/.state/orch_active_color.prev` after a staged selector write.

## Reboot persistence

On VPS after first deploy:

```bash
loginctl enable-linger dev   # run user systemd units when not logged in
systemctl --user status orchestrator-ecosystem portfolio-stub
systemctl --user list-timers orchestrator-healthcheck.timer orchestrator-verification-ladder.timer
```

Presentation API/console slots are **script-managed** (`orch_color.sh`, `run_ops_console.sh`); only the shared plane is systemd-managed on boot.

## Manual bootstrap (VPS only)

```bash
bash ~/orchestrator/deploy/vps/vps_bootstrap.sh all
# or: orch-shared | orch | orch-color green | portfolio | proxy | systemd | smoke
```

## Verify

```bash
curl -sS https://api.thedirectorate.app/health/
curl -sS https://www.thedirectorate.app/
curl -sS https://www.pproctor.com/health
curl -kfsS -H "Host: thehivemanager.com" https://127.0.0.1:8443/ -o /dev/null -w '%{http_code}\n'
bash deploy/vps/healthcheck.sh all
bash deploy/vps/orch_color.sh status
```

Authenticated OpenAPI (bearer required; anonymous denied):

```bash
curl -fsS -H "Authorization: Bearer $FOUNDER_API_TOKEN" http://127.0.0.1:8000/api/schema/ -o /dev/null -w '%{http_code}\n'
curl -fsS -H "Authorization: Bearer $FOUNDER_API_TOKEN" http://127.0.0.1:8000/api/docs/ -o /dev/null -w '%{http_code}\n'
```

## Resource profile

MVP shared plane: redis, coordinator, worker, scheduler, script sandbox. Presentation: `api-blue`/`api-green` + per-color ops-console on isolated `orchestrator-console-{color}` networks. No MCP lanes or real-provider workers on VPS until authorized.

## Gates

Does not close `G-ORCH-VPS-LIVE` or `G-ORCH-HOSTED-READY` — staging exposure; redeploy acceptance pending.

## Per-app blue/green

Upstream selection is **per-app**, owned by Orchestrator deploy tooling. See [`BLUEGREEN.md`](BLUEGREEN.md). Host-published ports `8000/8010/8081/8091` are firewall-contained; public entry is `:8443` only.
