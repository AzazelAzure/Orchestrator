# Orchestrator VPS bootstrap (shared hosting VPS)

Install Orchestrator on the shared hosting VPS loopback for Cloudflare Tunnel origin routing through the edge proxy.

## Prerequisites

- SSH access as `dev@` on the VPS
- Podman/Docker Compose
- Edge proxy extended with ecosystem vhosts (see host `proxy/ECOSYSTEM_HOSTS.md`)

## Bootstrap

```bash
mkdir -p ~/orchestrator && cd ~/orchestrator
git clone https://github.com/AzazelAzure/Orchestrator.git .
cp deploy/vps/.env.vps.example .env.vps
# Edit .env.vps — set REDIS_PASSWORD, DJANGO_SECRET_KEY, FOUNDER_API_TOKEN

./scripts/build_script_runner_attestation.sh
export ORCH_SCRIPT_ATTESTATION_DIGEST=$(jq -r .digest deploy/attestations/script-runner.testing.attestation.json)

docker compose -f docker-compose.yml -f deploy/vps/docker-compose.vps.yml --env-file .env.vps up -d --build
```

## Verify

```bash
curl -sS http://127.0.0.1:8000/health/
curl -sS http://127.0.0.1:8081/
curl -kfsS -H "Host: api.thedirectorate.dev" https://127.0.0.1:8443/health/
curl -kfsS -H "Host: www.thedirectorate.dev" https://127.0.0.1:8443/
```

## Resource profile

MVP subset: redis, coordinator, api, worker, scheduler, ops-console. Defer MCP lane fanout and real-provider workers until authorized.

## Gates

Does not close `G-ORCH-VPS-LIVE` or `G-ORCH-HOSTED-READY` — staging/stub exposure only.
