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
   - Generate `.env.vps` if missing
   - Build script-runner attestation (sources `.env.vps` first)
   - Patch base API port bind for podman-compose merge behavior
   - `up` Orchestrator MVP + ops-console, sibling site stub
   - Reload edge proxy (`COMPOSE_PROJECT_NAME=fm-beta`, proxy only)
   - Install systemd **user** units for reboot persistence
   - Smoke loopback, proxy Host-header, and public ZT URLs

## Reboot persistence

On VPS after first deploy:

```bash
loginctl enable-linger dev   # run user systemd units when not logged in
systemctl --user status orchestrator-ecosystem portfolio-stub
systemctl --user list-timers orchestrator-verification-ladder.timer
```

## Manual bootstrap (VPS only)

```bash
bash ~/orchestrator/deploy/vps/vps_bootstrap.sh all
# or: orch | portfolio | proxy | systemd | smoke
```

## Verify

```bash
curl -sS https://api.thedirectorate.app/health/
curl -sS https://www.thedirectorate.app/
curl -sS https://www.pproctor.com/health
curl -kfsS -H "Host: thehivemanager.com" https://127.0.0.1:8443/ -o /dev/null -w '%{http_code}\n'
```

## Resource profile

MVP: redis, coordinator, api, worker, scheduler, script sandbox, ops-console. No MCP lanes or real-provider workers on VPS until authorized.

## Gates

Does not close `G-ORCH-VPS-LIVE` or `G-ORCH-HOSTED-READY` — staging exposure.

## Per-app blue/green (pointer)

Upstream selection (blue vs green) is **per-app**, owned by Orchestrator deploy tooling for directorate hosts. Shared edge proxy terminates TLS; one color flip must not change other products' upstreams. Design home (Headquarters): `programs/orchestrator-platform/discussions/bluegreen-fleet-2026-07-29/`. Off-color smoke hostnames are HitM-gated drafts — do not enable without HitM authorization.
