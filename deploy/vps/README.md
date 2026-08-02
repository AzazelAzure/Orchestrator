# Orchestrator VPS bootstrap (shared hosting VPS)

Install Orchestrator on the shared hosting VPS for Cloudflare Tunnel → edge proxy `:8443` routing.

## One-command deploy (from dev machine)

```bash
# From Orchestrator repo root — rsync + remote bootstrap + smokes
bash deploy/vps/deploy_ecosystem.sh
```

Options: `--dry-run`, `--skip-hfm`, `--skip-orch`, `--skip-portfolio`

SSH target: `VPS_SSH_TARGET`, or sibling finance-manager `.env` → `VPS_ORIGIN_IP` / `FM_SPRINT_SSH`.

## What deploy does

1. Rsync edge-proxy ecosystem files (`ecosystem-hosts.conf`, nginx, compose, TLS script)
2. Rsync Orchestrator → `~/orchestrator`, sibling site tree → `~/portfolio`
3. Remote [`vps_bootstrap.sh`](vps_bootstrap.sh):
   - Generate `.env.vps` if missing (`ORCH_API_BIND=8000:8000` for proxy reachability)
   - Build script-runner attestation (sources `.env.vps` first)
   - Start **singleton shared mutation plane** (no tracked-file `sed`)
   - Materialize blue presentation (`api-blue` + ops-console blue)
   - Reload edge proxy (`COMPOSE_PROJECT_NAME=fm-beta`, proxy only)
   - Install systemd user units; **disable** legacy singleton `ops-console.service`
   - Smoke loopback, proxy Host-header, and public ZT URLs

## Blue/green materialization order

See [`BLUEGREEN.md`](BLUEGREEN.md) for the generic edge contract.

1. **Backup** — SQLite integrity + volume identity (`orchestrator-data`); never `down -v`.
2. **Disable singleton console** — `systemctl --user disable --now ops-console.service` (also run by bootstrap).
3. **Daemon-reload** — install units; verify `orchestrator-ecosystem.service` starts shared plane only (no `api`).
4. **Shared refresh** — `bash deploy/vps/vps_bootstrap.sh orch-shared`
5. **Blue presentation** — default public route; `orch_color.sh deploy --color blue`
6. **Idle green** (materialization grant) — `ORCH_COLOR_MATERIALIZE_ONLY=1 bash deploy/vps/orch_color.sh deploy --color green`
7. **Smoke green** — loopback `:8010`/`:8091`, bearer schema/docs, anonymous deny
8. **Sibling regression** — `bash deploy/vps/vps_bootstrap.sh smoke`
9. **Public selector stays blue** — do not run `orch_color.sh switch` without a separate promotion grant

Rollback rehearsal: `orch_color.sh rollback` restores Orchestrator-owned `deploy/vps/.state/orch_active_color.prev` after a staged selector write.

## Reboot persistence

On VPS after first deploy:

```bash
loginctl enable-linger dev   # run user systemd units when not logged in
systemctl --user status orchestrator-ecosystem portfolio-stub
systemctl --user list-timers orchestrator-healthcheck.timer orchestrator-verification-ladder.timer
```

Presentation API/console slots are **script-managed** (`orch_color.sh`); only the shared plane is systemd-managed on boot.

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
```

Authenticated OpenAPI (bearer required; anonymous denied):

```bash
curl -fsS -H "Authorization: Bearer $FOUNDER_API_TOKEN" http://127.0.0.1:8000/api/schema/ -o /dev/null -w '%{http_code}\n'
curl -fsS -H "Authorization: Bearer $FOUNDER_API_TOKEN" http://127.0.0.1:8000/api/docs/ -o /dev/null -w '%{http_code}\n'
```

## Resource profile

MVP shared plane: redis, coordinator, worker, scheduler, script sandbox. Presentation: `api-blue`/`api-green` + per-color ops-console. No MCP lanes or real-provider workers on VPS until authorized.

## Gates

Does not close `G-ORCH-VPS-LIVE` or `G-ORCH-HOSTED-READY` — staging exposure.

## Per-app blue/green

Upstream selection is **per-app**, owned by Orchestrator deploy tooling. See [`BLUEGREEN.md`](BLUEGREEN.md). Off-color smoke uses loopback/origin Host headers only — no DNS/ZT mutation in the materialization grant.
