#!/usr/bin/env bash
# Idempotent VPS-side bootstrap for Orchestrator + Portfolio + HFM proxy ecosystem files.
# Invoked locally by deploy/vps/deploy_ecosystem.sh over SSH.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
PORT_ROOT="${PORT_ROOT:-$HOME/portfolio}"
FM_ROOT="${FM_ROOT:-$HOME/finance_manager}"
COMPOSE="${COMPOSE:-podman-compose}"
FM_PROJECT="${FM_PROJECT:-fm-beta}"

orch_compose() {
  ${COMPOSE} -f "$ORCH_ROOT/docker-compose.yml" -f "$ORCH_ROOT/deploy/vps/docker-compose.vps.yml" --env-file "$ORCH_ROOT/.env.vps" "$@"
}

log() { printf '[vps-bootstrap] %s\n' "$*"; }

ensure_orch_env() {
  if [[ ! -f "$ORCH_ROOT/.env.vps" ]]; then
    log "generating $ORCH_ROOT/.env.vps"
    bash "$ORCH_ROOT/scripts/generate_vps_env.sh" "$ORCH_ROOT/.env.vps"
  fi
}

patch_orch_base_ports() {
  # podman-compose merges port lists; strip loopback-only API bind from base file on VPS.
  if grep -q '127.0.0.1:8000:8000' "$ORCH_ROOT/docker-compose.yml" 2>/dev/null; then
    sed -i 's/"127.0.0.1:8000:8000"/"8000:8000"/' "$ORCH_ROOT/docker-compose.yml"
    log "patched base api port binding for proxy upstream reachability"
  fi
}

ensure_attestation() {
  local att_dir="$ORCH_ROOT/deploy/attestations/script-runner.attestation.json"
  if [[ -d "$att_dir" ]]; then
    rm -rf "$att_dir"
    log "removed mistaken attestation directory"
  fi
  set -a
  # shellcheck disable=SC1091
  source "$ORCH_ROOT/.env.vps"
  set +a
  (cd "$ORCH_ROOT" && bash scripts/build_script_runner_attestation.sh)
  python3 - <<'PY' "$ORCH_ROOT"
import json, pathlib, re, sys
root = pathlib.Path(sys.argv[1])
att_path = root / "deploy/attestations/script-runner.attestation.json"
if not att_path.is_file():
    att_path = root / "deploy/attestations/script-runner.testing.attestation.json"
att = json.loads(att_path.read_text())
digest = att.get("image_digest") or att.get("digest")
if digest and not str(digest).startswith("sha256:"):
    digest = f"sha256:{digest}"
env = root / ".env.vps"
text = env.read_text()
if digest:
    if re.search(r"^ORCH_SCRIPT_IMAGE_DIGEST=", text, flags=re.M):
        text = re.sub(r"^ORCH_SCRIPT_IMAGE_DIGEST=.*$", f"ORCH_SCRIPT_IMAGE_DIGEST={digest}", text, flags=re.M)
    else:
        text += f"\nORCH_SCRIPT_IMAGE_DIGEST={digest}\n"
    env.write_text(text)
PY
}

up_orchestrator() {
  patch_orch_base_ports
  orch_compose up -d --build \
    redis coordinator api worker scheduler \
    script-spool-init script-runner script-worker
  bash "$ORCH_ROOT/deploy/vps/run_ops_console.sh"
}

up_portfolio() {
  ${COMPOSE} -f "$PORT_ROOT/deploy/docker-compose.vps.yml" up -d --build
}

reload_hfm_proxy() {
  if [[ ! -f "$FM_ROOT/docker-compose.bluegreen.yml" ]]; then
    log "skip HFM proxy reload — $FM_ROOT/docker-compose.bluegreen.yml missing"
    return 0
  fi
  local env_args=()
  if [[ -f "$FM_ROOT/.secrets/server.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.secrets/server.env")
  elif [[ -f "$FM_ROOT/.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.env")
  fi
  (
    cd "$FM_ROOT"
    COMPOSE_PROJECT_NAME="$FM_PROJECT" ${COMPOSE} -f docker-compose.bluegreen.yml "${env_args[@]}" up -d proxy --force-recreate --no-deps
  )
}

install_systemd_user_units() {
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  local units=(
    orchestrator-ecosystem.service
    ops-console.service
    portfolio-stub.service
    orchestrator-verification-ladder.service
    orchestrator-verification-ladder.timer
  )
  for unit in "${units[@]}"; do
    if [[ -f "$ORCH_ROOT/deploy/vps/systemd/$unit" ]]; then
      cp "$ORCH_ROOT/deploy/vps/systemd/$unit" "$unit_dir/"
    fi
  done
  chmod +x "$ORCH_ROOT/deploy/vps/run_ops_console.sh" 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user enable orchestrator-ecosystem.service ops-console.service portfolio-stub.service orchestrator-verification-ladder.timer 2>/dev/null || true
  log "systemd user units installed (enable linger: loginctl enable-linger \$USER)"
}

smoke_local() {
  curl -fsS "http://127.0.0.1:8000/health/" >/dev/null || { log "FAIL orchestrator api loopback"; return 1; }
  log "smoke ok: orchestrator api loopback"
  curl -fsS "http://127.0.0.1:8081/" >/dev/null || { log "FAIL ops console loopback"; return 1; }
  log "smoke ok: ops console loopback"
  curl -fsS "http://127.0.0.1:3000/health" >/dev/null || { log "FAIL portfolio loopback"; return 1; }
  log "smoke ok: portfolio loopback"
  curl -kfsS -H "Host: api.thedirectorate.dev" "https://127.0.0.1:8443/health/" >/dev/null || { log "FAIL api via proxy"; return 1; }
  log "smoke ok: api via proxy"
  curl -kfsS -H "Host: www.thedirectorate.dev" "https://127.0.0.1:8443/" >/dev/null || { log "FAIL console via proxy"; return 1; }
  log "smoke ok: console via proxy"
  curl -kfsS -H "Host: www.pproctor.com" "https://127.0.0.1:8443/health" >/dev/null || { log "FAIL portfolio via proxy"; return 1; }
  log "smoke ok: portfolio via proxy"
  curl -kfsS -H "Host: thehivemanager.com" "https://127.0.0.1:8443/" -o /dev/null || { log "FAIL HFM regression"; return 1; }
  log "smoke ok: HFM regression via proxy"
}

smoke_public() {
  curl -fsS "https://api.thedirectorate.dev/health/" >/dev/null && log "smoke ok: api public ZT"
  curl -fsS "https://www.thedirectorate.dev/" >/dev/null && log "smoke ok: console public ZT"
  curl -fsS "https://www.pproctor.com/health" >/dev/null && log "smoke ok: portfolio public ZT"
}

main() {
  ensure_orch_env
  ensure_attestation
  up_orchestrator || log "orchestrator up had errors"
  up_portfolio || log "portfolio up had errors"
  reload_hfm_proxy || log "proxy reload had errors"
  install_systemd_user_units
  smoke_local || log "local smokes had failures"
  smoke_public || log "public ZT smokes skipped or failed"
}

case "${1:-all}" in
  orch) ensure_orch_env; ensure_attestation; up_orchestrator ;;
  portfolio) up_portfolio ;;
  proxy) reload_hfm_proxy ;;
  systemd) install_systemd_user_units ;;
  smoke) smoke_local; smoke_public || true ;;
  all) main ;;
  *) echo "usage: $0 [all|orch|portfolio|proxy|systemd|smoke]" >&2; exit 1 ;;
esac
