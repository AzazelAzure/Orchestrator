#!/usr/bin/env bash
# Create per-color presentation Podman networks (idempotent).
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"

# shellcheck source=orch_presentation_env.sh
source "${ORCH_ROOT}/deploy/vps/orch_presentation_env.sh"

usage() {
  cat <<'EOF'
usage: ensure_presentation_networks.sh [--color blue|green|all]

Ensures orchestrator-console-{color} networks exist before presentation deploy.
EOF
}

ensure_one() {
  local color="$1"
  local network
  network="$(orch_presentation_network_for_color "$color")"
  if podman network exists "$network" 2>/dev/null; then
    echo "ok: network $network exists"
    return 0
  fi
  podman network create "$network" >/dev/null
  echo "created network $network"
}

COLOR="${1:-all}"
if [[ "${1:-}" == --color ]]; then
  COLOR="${2:-}"
fi

case "$COLOR" in
  blue|green) ensure_one "$COLOR" ;;
  all)
    ensure_one blue
    ensure_one green
    ;;
  -h|--help) usage; exit 0 ;;
  *) echo "invalid color: $COLOR" >&2; usage; exit 1 ;;
esac
