#!/usr/bin/env bash
# Local driver: rsync ecosystem artifacts to the HFM VPS and run vps_bootstrap.sh.
#
# Orchestrator sync contract (ORCH-VPS-REPAIR-01):
#   - Default: rsync -az WITHOUT --delete (first repair sync and routine default).
#   - Opt-in: pass --delete for Orchestrator tree only after pre-sync backup/integrity bars.
#   - Never delete server-local: .env.vps, deploy/vps/.state/, deploy/attestations/, backups/.
#   - Durable SQLite backups must live outside ~/orchestrator/ (e.g. ~/backups/orchestrator/).
#
# Usage:
#   ./deploy/vps/deploy_ecosystem.sh [--dry-run] [--delete] [--skip-hfm] [--skip-orch] [--skip-portfolio]
#
# Reads VPS SSH target from:
#   VPS_SSH_TARGET, FM_SPRINT_SSH, or HFM repo .env VPS_ORIGIN_IP (dev@IP).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HFM_ROOT="${HFM_ROOT:-$HOME/Projects/HiveSolutions/Finance_Manager/HFM}"
PORT_ROOT="${PORT_ROOT:-$HOME/Projects/Portfolio}"
DRY_RUN=0
ORCH_RSYNC_DELETE=0
SKIP_HFM=0
SKIP_ORCH=0
SKIP_PORT=0

ORCH_PROTECTED_EXCLUDES=(
  --exclude '/.env.vps'
  --exclude '/deploy/vps/.state/'
  --exclude '/deploy/attestations/'
  --exclude '/backups/'
)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --delete) ORCH_RSYNC_DELETE=1; shift ;;
    --skip-hfm) SKIP_HFM=1; shift ;;
    --skip-orch) SKIP_ORCH=1; shift ;;
    --skip-portfolio) SKIP_PORT=1; shift ;;
    -h|--help)
      sed -n '1,25p' "$0"
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

load_vps_ssh() {
  if [[ -n "${VPS_SSH_TARGET:-}" ]]; then
    return 0
  fi
  if [[ -f "$HFM_ROOT/.env" ]]; then
    local ip ssh
    ip="$(grep -E '^VPS_ORIGIN_IP=' "$HFM_ROOT/.env" | head -1 | cut -d= -f2- | tr -d "\"'" || true)"
    ssh="$(grep -E '^FM_SPRINT_SSH=' "$HFM_ROOT/.env" | head -1 | cut -d= -f2- | tr -d "\"'" || true)"
    VPS_SSH_TARGET="${ssh:-${ip:+dev@$ip}}"
  fi
  VPS_SSH_TARGET="${VPS_SSH_TARGET:-dev@159.198.75.194}"
}

run() {
  if [[ "$DRY_RUN" -eq 1 ]]; then
    printf '[dry-run]'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

rsync_to() {
  local src="$1" dest="$2"
  shift 2
  run rsync -az --delete "${@}" "$src" "${VPS_SSH_TARGET}:$dest"
}

rsync_orchestrator() {
  local delete_args=()
  if [[ "$ORCH_RSYNC_DELETE" -eq 1 ]]; then
    delete_args=(--delete)
  fi
  run rsync -az "${delete_args[@]}" \
    --exclude '.git' --exclude '.venv' --exclude '.tmp' --exclude '__pycache__' --exclude '.pytest_cache' \
    "${ORCH_PROTECTED_EXCLUDES[@]}" \
    "$ROOT/" "${VPS_SSH_TARGET}:~/orchestrator/"
}

log() { printf '[deploy-ecosystem] %s\n' "$*"; }

load_vps_ssh
log "target=$VPS_SSH_TARGET orch_delete=${ORCH_RSYNC_DELETE}"

run ssh -o BatchMode=yes "$VPS_SSH_TARGET" 'mkdir -p ~/orchestrator ~/portfolio ~/finance_manager/proxy/conf.d ~/finance_manager/proxy/certs'

if [[ "$SKIP_HFM" -eq 0 ]]; then
  log "render HFM ecosystem-hosts.conf from ORCH_PUBLISH_HOST"
  if [[ -z "${ORCH_PUBLISH_HOST:-}" && -f "$HFM_ROOT/.env" ]]; then
    ORCH_PUBLISH_HOST="$(grep -E '^ORCH_PUBLISH_HOST=' "$HFM_ROOT/.env" | tail -1 | cut -d= -f2- | tr -d "\"'" || true)"
  fi
  if [[ -z "${ORCH_PUBLISH_HOST:-}" ]]; then
    log "ERROR: ORCH_PUBLISH_HOST required for ecosystem-hosts render (HFM .env or env)"
    exit 1
  fi
  ORCH_PUBLISH_HOST="$ORCH_PUBLISH_HOST" bash "$HFM_ROOT/scripts/ops/render_ecosystem_hosts.sh" \
    "$HFM_ROOT/proxy/conf.d/ecosystem-hosts.conf"
  log "sync HFM proxy ecosystem files"
  rsync_to "$HFM_ROOT/proxy/conf.d/ecosystem-hosts.conf" '~/finance_manager/proxy/conf.d/'
  rsync_to "$HFM_ROOT/proxy/nginx.bluegreen.conf" '~/finance_manager/proxy/'
  rsync_to "$HFM_ROOT/docker-compose.bluegreen.yml" '~/finance_manager/'
  rsync_to "$HFM_ROOT/proxy/certs/generate-ecosystem-certs.sh" '~/finance_manager/proxy/certs/'
  run ssh "$VPS_SSH_TARGET" 'bash ~/finance_manager/proxy/certs/generate-ecosystem-certs.sh 2>/dev/null || true'
fi

if [[ "$SKIP_ORCH" -eq 0 ]]; then
  log "sync Orchestrator (protected excludes; delete=${ORCH_RSYNC_DELETE})"
  rsync_orchestrator
fi

if [[ "$SKIP_PORT" -eq 0 ]]; then
  log "sync Portfolio"
  rsync_to "$PORT_ROOT/" '~/portfolio/' \
    --exclude '.git' --exclude '__pycache__' --exclude '.pytest_cache'
fi

log "run remote bootstrap"
run ssh "$VPS_SSH_TARGET" 'chmod +x ~/orchestrator/deploy/vps/vps_bootstrap.sh ~/orchestrator/deploy/vps/run_ops_console.sh 2>/dev/null; bash ~/orchestrator/deploy/vps/vps_bootstrap.sh all'

log "done"
