#!/usr/bin/env bash
# Idempotent VPS-side bootstrap for Orchestrator + Portfolio + edge proxy ecosystem files.
# Invoked locally by deploy/vps/deploy_ecosystem.sh over SSH.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
PORT_ROOT="${PORT_ROOT:-$HOME/portfolio}"
FM_ROOT="${FM_ROOT:-$HOME/finance_manager}"
COMPOSE="${COMPOSE:-podman-compose}"
FM_PROJECT="${FM_PROJECT:-fm-beta}"

SHARED_SERVICES=(redis coordinator worker scheduler script-spool-init script-runner script-worker)
ORCH_COMPOSE_PROJECT="${ORCH_COMPOSE_PROJECT:-orchestrator}"

orch_compose() {
  (
    cd "$ORCH_ROOT"
    COMPOSE_PROJECT_NAME="$ORCH_COMPOSE_PROJECT" ${COMPOSE} \
      -f docker-compose.yml \
      -f deploy/vps/docker-compose.vps.yml \
      --env-file .env.vps "$@"
  )
}

log() { printf '[vps-bootstrap] %s\n' "$*"; }

# shellcheck source=orch_presentation_env.sh
source "$ORCH_ROOT/deploy/vps/orch_presentation_env.sh"

ensure_orch_env() {
  if [[ ! -f "$ORCH_ROOT/.env.vps" ]]; then
    log "generating $ORCH_ROOT/.env.vps"
    bash "$ORCH_ROOT/scripts/generate_vps_env.sh" "$ORCH_ROOT/.env.vps"
  fi
  if grep -q 'thedirectorate\.dev' "$ORCH_ROOT/.env.vps" 2>/dev/null; then
    sed -i 's/thedirectorate\.dev/thedirectorate.app/g' "$ORCH_ROOT/.env.vps"
    log "patched .env.vps hostnames to thedirectorate.app"
  fi
  if orch_diag_bind_load 2>/dev/null; then
    orch_diag_bind_validate "${ORCH_DIAG_BIND:-}" || exit 1
    log "ORCH_DIAG_BIND=${ORCH_DIAG_BIND:-} (loopback diagnostics enabled)"
  else
    log "presentation tier: no host port publish (edge routes via per-color Podman networks)"
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
  local att_path="$ORCH_ROOT/deploy/attestations/script-runner.attestation.json"
  if [[ ! -f "$att_path" ]]; then
    att_path="$ORCH_ROOT/deploy/attestations/script-runner.testing.attestation.json"
  fi
  if [[ ! -f "$att_path" || -d "$att_path" ]]; then
    log "ERROR: attestation path is not a regular JSON file: $att_path"
    exit 1
  fi
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

up_shared_plane() {
  log "starting singleton shared mutation plane (no presentation api)"
  bash "$ORCH_ROOT/deploy/vps/orch_color.sh" deploy shared
  ORCH_HEALTH_COLOR=shared bash "$ORCH_ROOT/deploy/vps/healthcheck.sh"
}

materialize_presentation() {
  local color="${1:-blue}"
  log "materializing presentation color=$color (public selector unchanged)"
  ORCH_COLOR_MATERIALIZE_ONLY=1 bash "$ORCH_ROOT/deploy/vps/orch_color.sh" deploy --color "$color"
}

up_orchestrator() {
  up_shared_plane
  materialize_presentation blue
  # Idle green slot is operator-driven: ORCH_COLOR_MATERIALIZE_ONLY=1 orch_color.sh deploy --color green
}

up_portfolio() {
  ${COMPOSE} -f "$PORT_ROOT/deploy/docker-compose.vps.yml" up -d --build
}

reload_hfm_proxy() {
  if [[ ! -f "$FM_ROOT/docker-compose.bluegreen.yml" ]]; then
    log "skip edge proxy reload — $FM_ROOT/docker-compose.bluegreen.yml missing"
    return 0
  fi
  local env_args=()
  if [[ -f "$FM_ROOT/.secrets/server.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.secrets/server.env")
    set -a
    # shellcheck disable=SC1091
    source "$FM_ROOT/.secrets/server.env"
    set +a
  elif [[ -f "$FM_ROOT/.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.env")
    set -a
    # shellcheck disable=SC1091
    source "$FM_ROOT/.env"
    set +a
  fi
  (
    cd "$FM_ROOT"
    COMPOSE_PROJECT_NAME="$FM_PROJECT" ${COMPOSE} -f docker-compose.bluegreen.yml "${env_args[@]}" up -d proxy --force-recreate --no-deps
  )
  orch_run_edge_proxy_pre_reload_hook || {
    log "ERROR: edge proxy pre-reload hook failed"
    return 1
  }
  (
    cd "$FM_ROOT"
    COMPOSE_PROJECT_NAME="$FM_PROJECT" ${COMPOSE} -f docker-compose.bluegreen.yml "${env_args[@]}" \
      exec -T proxy nginx -t
  )
}

install_systemd_user_units() {
  local unit_dir="$HOME/.config/systemd/user"
  mkdir -p "$unit_dir"
  local units=(
    orchestrator-ecosystem.service
    orchestrator-healthcheck.service
    orchestrator-healthcheck.timer
    portfolio-stub.service
    orchestrator-verification-ladder.service
    orchestrator-verification-ladder.timer
  )
  for unit in "${units[@]}"; do
    if [[ -f "$ORCH_ROOT/deploy/vps/systemd/$unit" ]]; then
      cp "$ORCH_ROOT/deploy/vps/systemd/$unit" "$unit_dir/"
    fi
  done
  if [[ -f "$ORCH_ROOT/deploy/vps/systemd/ops-console.service" ]]; then
    cp "$ORCH_ROOT/deploy/vps/systemd/ops-console.service" "$unit_dir/"
  fi
  chmod +x "$ORCH_ROOT/deploy/vps/run_ops_console.sh" 2>/dev/null || true
  chmod +x "$ORCH_ROOT/deploy/vps/orch_color.sh" 2>/dev/null || true
  chmod +x "$ORCH_ROOT/deploy/vps/healthcheck.sh" 2>/dev/null || true
  chmod +x "$ORCH_ROOT/deploy/vps/orch_presentation_env.sh" 2>/dev/null || true
  chmod +x "$ORCH_ROOT/deploy/vps/ensure_presentation_networks.sh" 2>/dev/null || true
  systemctl --user daemon-reload
  bash "$ORCH_ROOT/deploy/vps/orch_color.sh" disable-singleton-console
  systemctl --user enable orchestrator-ecosystem.service portfolio-stub.service orchestrator-healthcheck.timer orchestrator-verification-ladder.timer 2>/dev/null || true
  log "systemd user units installed (enable linger: loginctl enable-linger \$USER)"
}

smoke_local() {
  bash "$ORCH_ROOT/deploy/vps/orch_color.sh" smoke --color blue || {
    log "FAIL orchestrator presentation in-network smoke (blue)"
    return 1
  }
  log "smoke ok: orchestrator presentation in-network (blue)"
  curl -fsS "http://127.0.0.1:3000/health" >/dev/null || { log "FAIL portfolio loopback"; return 1; }
  log "smoke ok: portfolio loopback"
  curl -kfsS -H "Host: api.thedirectorate.app" "https://127.0.0.1:8443/health/" >/dev/null || { log "FAIL api via proxy"; return 1; }
  log "smoke ok: api via proxy"
  curl -kfsS -H "Host: www.thedirectorate.app" "https://127.0.0.1:8443/" >/dev/null || { log "FAIL console via proxy"; return 1; }
  log "smoke ok: console via proxy"
  curl -kfsS -H "Host: www.pproctor.com" "https://127.0.0.1:8443/health" >/dev/null || { log "FAIL portfolio via proxy"; return 1; }
  log "smoke ok: portfolio via proxy"
  curl -kfsS -H "Host: thehivemanager.com" "https://127.0.0.1:8443/" -o /dev/null || { log "FAIL sibling regression"; return 1; }
  log "smoke ok: sibling regression via proxy"
}

smoke_public() {
  curl -fsS "https://api.thedirectorate.app/health/" >/dev/null && log "smoke ok: api public ZT"
  curl -fsS "https://www.thedirectorate.app/" >/dev/null && log "smoke ok: console public ZT"
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
  orch-shared) ensure_orch_env; ensure_attestation; up_shared_plane ;;
  orch) ensure_orch_env; ensure_attestation; up_orchestrator ;;
  orch-color)
    ensure_orch_env
    ensure_attestation
    up_shared_plane
    ORCH_COLOR_MATERIALIZE_ONLY=1 bash "$ORCH_ROOT/deploy/vps/orch_color.sh" deploy --color "${2:-green}"
    ;;
  portfolio) up_portfolio ;;
  proxy) reload_hfm_proxy ;;
  systemd) install_systemd_user_units ;;
  smoke) smoke_local; smoke_public || true ;;
  all) main ;;
  *) echo "usage: $0 [all|orch|orch-shared|orch-color COLOR|portfolio|proxy|systemd|smoke]" >&2; exit 1 ;;
esac
