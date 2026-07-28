# Orchestrator VPS bootstrap (shared hosting VPS)

Install Orchestrator on the shared hosting VPS loopback for Cloudflare Tunnel origin routing through the edge proxy.

## Prerequisites

- SSH access as `dev@` on the VPS
- Podman/Docker Compose
- Edge proxy extended with ecosystem vhosts (see host `proxy/ECOSYSTEM_HOSTS.md`)

## Bootstrap

```bash
mkdir -p ~/orchestrator && cd ~/orchestrator
git clone https://github.com/AzazelAzure/Orchestrator.git . 2>/dev/null || git pull origin main
bash scripts/generate_vps_env.sh .env.vps
bash scripts/build_script_runner_attestation.sh
# Update digest after attestation build (compose reads ORCH_SCRIPT_IMAGE_DIGEST from env file)
python3 - <<'PY'
import json, pathlib, re
root = pathlib.Path(".")
att = json.loads((root / "deploy/attestations/script-runner.testing.attestation.json").read_text())
digest = att.get("digest") or att.get("image_digest")
env = root / ".env.vps"
text = env.read_text()
if digest and not digest.startswith("sha256:"):
    digest = f"sha256:{digest}"
if digest:
    if re.search(r"^ORCH_SCRIPT_IMAGE_DIGEST=", text, flags=re.M):
        text = re.sub(r"^ORCH_SCRIPT_IMAGE_DIGEST=.*$", f"ORCH_SCRIPT_IMAGE_DIGEST={digest}", text, flags=re.M)
    else:
        text += f"\nORCH_SCRIPT_IMAGE_DIGEST={digest}\n"
    env.write_text(text)
PY

docker compose -f docker-compose.yml -f deploy/vps/docker-compose.vps.yml --env-file .env.vps up -d --build redis coordinator api worker scheduler script-spool-init script-runner script-worker ops-console
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
