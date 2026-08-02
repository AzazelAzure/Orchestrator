#!/usr/bin/env bash
# Run ops-console via plain podman (podman-compose breaks on network_mode for this service).
# Each color console joins an isolated orchestrator-console-{color} network with exactly one
# matching API container aliased as api:8000 for nginx upstream resolution.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
COMPOSE="${COMPOSE:-podman-compose}"
ORCH_COMPOSE_PROJECT="${ORCH_COMPOSE_PROJECT:-orchestrator}"

COLOR="${ORCH_CONSOLE_COLOR:-blue}"
CONTAINER_NAME="${ORCH_CONSOLE_CONTAINER_NAME:-}"
HOST_PORT="${ORCH_CONSOLE_PORT:-}"
IMAGE_TAG="${ORCH_CONSOLE_IMAGE_TAG:-}"

usage() {
  cat <<'EOF'
usage: run_ops_console.sh [--color blue|green] [--name NAME] [--port PORT] [--image-tag TAG]

Build and run one ops-console presentation container on an isolated per-color network
with the matching api-{color} container aliased as api. Defaults: blue / :8081 /
orchestrator-ops-console-blue:vps
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

resolve_api_cid_for_color() {
  local color="$1"
  local svc
  case "$color" in
    blue) svc="api-blue" ;;
    green) svc="api-green" ;;
    *) echo "invalid color: $color" >&2; return 1 ;;
  esac
  local -a cids=()
  mapfile -t cids < <(
    COMPOSE_PROJECT_NAME="$ORCH_COMPOSE_PROJECT" ${COMPOSE} \
      -f docker-compose.yml \
      -f deploy/vps/docker-compose.vps.yml \
      -f deploy/vps/docker-compose.bluegreen.yml \
      --env-file .env.vps ps -q "$svc" 2>/dev/null | sed '/^$/d'
  )
  if [[ ${#cids[@]} -eq 0 ]]; then
    echo "no running API container for color=$color" >&2
    return 1
  fi
  if [[ ${#cids[@]} -gt 1 ]]; then
    echo "ambiguous API container count (${#cids[@]}) for color=$color" >&2
    return 1
  fi
  echo "${cids[0]}"
}

CONSOLE_NETWORK="orchestrator-console-${COLOR}"
API_CID="$(resolve_api_cid_for_color "$COLOR")"

if ! podman network exists "$CONSOLE_NETWORK" 2>/dev/null; then
  podman network create "$CONSOLE_NETWORK" >/dev/null
fi

podman network disconnect "$CONSOLE_NETWORK" "$API_CID" 2>/dev/null || true
podman network connect --alias api "$CONSOLE_NETWORK" "$API_CID"

podman build -t "$IMAGE_TAG" ./ops-console
podman rm -f "$CONTAINER_NAME" 2>/dev/null || true
podman run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network "$CONSOLE_NETWORK" \
  -p "${HOST_PORT}:8081" \
  --user 0:0 \
  "$IMAGE_TAG"

for _ in 1 2 3 4 5; do
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/" >/dev/null 2>&1; then
    echo "ops-console ($COLOR) running on :${HOST_PORT} (image=${IMAGE_TAG}, network=${CONSOLE_NETWORK})"
    exit 0
  fi
  sleep 1
done
echo "ops-console ($COLOR) failed health check on :${HOST_PORT}" >&2
podman logs "$CONTAINER_NAME" 2>&1 | tail -20
exit 1
