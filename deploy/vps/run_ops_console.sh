#!/usr/bin/env bash
# Run ops-console via plain podman (podman-compose breaks on network_mode for this service).
# Each color console joins an isolated orchestrator-console-{color} network with exactly one
# matching API container aliased as api (console upstream) and orch-api-{color} (edge routing).
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
COMPOSE="${COMPOSE:-podman-compose}"
ORCH_COMPOSE_PROJECT="${ORCH_COMPOSE_PROJECT:-orchestrator}"

# shellcheck source=orch_presentation_env.sh
source "${ORCH_ROOT}/deploy/vps/orch_presentation_env.sh"

COLOR="${ORCH_CONSOLE_COLOR:-blue}"
CONTAINER_NAME="${ORCH_CONSOLE_CONTAINER_NAME:-}"
HOST_PORT="${ORCH_CONSOLE_PORT:-}"
IMAGE_TAG="${ORCH_CONSOLE_IMAGE_TAG:-}"

usage() {
  cat <<'EOF'
usage: run_ops_console.sh [--color blue|green] [--name NAME] [--port PORT] [--image-tag TAG]

Build and run one ops-console presentation container on an isolated per-color network
with the matching api-{color} container aliased as api and orch-api-{color}.
Optional loopback diagnostics: ORCH_DIAG_BIND=127.0.0.1 publishes 127.0.0.1:PORT only.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --color) COLOR="$2"; shift 2 ;;
    --name) CONTAINER_NAME="$2"; shift 2 ;;
    --port) HOST_PORT="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

case "$COLOR" in
  blue)
    HOST_PORT="${HOST_PORT:-8081}"
    CONTAINER_NAME="${CONTAINER_NAME:-orchestrator_ops-console_blue}"
    IMAGE_TAG="${IMAGE_TAG:-orchestrator-ops-console-blue:vps}"
  ;;
  green)
    HOST_PORT="${HOST_PORT:-8091}"
    CONTAINER_NAME="${CONTAINER_NAME:-orchestrator_ops-console_green}"
    IMAGE_TAG="${IMAGE_TAG:-orchestrator-ops-console-green:vps}"
  ;;
  *)
    echo "invalid --color: $COLOR (expected blue or green)" >&2
    exit 1
    ;;
esac

cd "$ORCH_ROOT"
bash "$ORCH_ROOT/deploy/vps/ensure_presentation_networks.sh" --color "$COLOR"

API_CID="$(orch_resolve_api_cid_for_color "$COLOR")"
CONSOLE_NETWORK="$(orch_presentation_network_for_color "$COLOR")"
API_EDGE_ALIAS="$(orch_api_alias_for_color "$COLOR")"
CONSOLE_ALIAS="$(orch_console_alias_for_color "$COLOR")"

if ! podman network exists "$CONSOLE_NETWORK" 2>/dev/null; then
  echo "missing presentation network: $CONSOLE_NETWORK" >&2
  exit 1
fi

podman network disconnect "$CONSOLE_NETWORK" "$API_CID" 2>/dev/null || true
podman network connect --alias api --alias "$API_EDGE_ALIAS" "$CONSOLE_NETWORK" "$API_CID"

podman build -t "$IMAGE_TAG" ./ops-console
podman rm -f "$CONTAINER_NAME" 2>/dev/null || true

publish_args=()
if orch_diag_bind_enabled; then
  # shellcheck disable=SC2206
  publish_args=($(orch_diag_publish_args "$HOST_PORT" 8081))
fi

podman run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network "${CONSOLE_NETWORK}:alias=${CONSOLE_ALIAS}" \
  "${publish_args[@]}" \
  --user 0:0 \
  "$IMAGE_TAG"

for _ in 1 2 3 4 5; do
  if orch_presentation_probe_console "$COLOR"; then
    if orch_presentation_network_probe_api "$COLOR"; then
      echo "ops-console ($COLOR) healthy on network=${CONSOLE_NETWORK} aliases=${API_EDGE_ALIAS},${CONSOLE_ALIAS}"
      exit 0
    fi
  fi
  sleep 1
done
echo "ops-console ($COLOR) failed in-network health check on ${CONSOLE_NETWORK}" >&2
podman logs "$CONTAINER_NAME" 2>&1 | tail -20
exit 1
