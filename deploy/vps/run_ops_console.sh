#!/usr/bin/env bash
# Run ops-console via plain podman (podman-compose breaks on network_mode for this service).
# Per-color builds bake VITE_API_BASE_URL at image build time — static delivery does not
# prove idle API routing in the browser; test idle API directly on loopback.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
cd "$ORCH_ROOT"

COLOR="${ORCH_CONSOLE_COLOR:-blue}"
CONTAINER_NAME="${ORCH_CONSOLE_CONTAINER_NAME:-}"
HOST_PORT="${ORCH_CONSOLE_PORT:-}"
IMAGE_TAG="${ORCH_CONSOLE_IMAGE_TAG:-}"
API_BASE="${VITE_API_BASE_URL:-}"

usage() {
  cat <<'EOF'
usage: run_ops_console.sh [--color blue|green] [--name NAME] [--port PORT]
                          [--image-tag TAG] [--api-base URL]

Build and run one ops-console presentation container. Defaults: blue / :8081 /
orchestrator-ops-console-blue:vps / https://api.thedirectorate.app
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --color) COLOR="$2"; shift 2 ;;
    --name) CONTAINER_NAME="$2"; shift 2 ;;
    --port) HOST_PORT="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    --api-base) API_BASE="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 1 ;;
  esac
done

case "$COLOR" in
  blue)
    HOST_PORT="${HOST_PORT:-8081}"
    CONTAINER_NAME="${CONTAINER_NAME:-orchestrator_ops-console_blue}"
    IMAGE_TAG="${IMAGE_TAG:-orchestrator-ops-console-blue:vps}"
    API_BASE="${API_BASE:-https://api.thedirectorate.app}"
    ;;
  green)
    HOST_PORT="${HOST_PORT:-8091}"
    CONTAINER_NAME="${CONTAINER_NAME:-orchestrator_ops-console_green}"
    IMAGE_TAG="${IMAGE_TAG:-orchestrator-ops-console-green:vps}"
    API_BASE="${API_BASE:-http://127.0.0.1:8010}"
    ;;
  *)
    echo "invalid --color: $COLOR (expected blue or green)" >&2
    exit 1
    ;;
esac

podman build -t "$IMAGE_TAG" ./ops-console --build-arg "VITE_API_BASE_URL=${API_BASE}"
podman rm -f "$CONTAINER_NAME" 2>/dev/null || true
podman run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  -p "${HOST_PORT}:8081" \
  --user 0:0 \
  "$IMAGE_TAG"

for _ in 1 2 3 4 5; do
  if curl -fsS "http://127.0.0.1:${HOST_PORT}/" >/dev/null 2>&1; then
    echo "ops-console ($COLOR) running on :${HOST_PORT} (image=${IMAGE_TAG})"
    exit 0
  fi
  sleep 1
done
echo "ops-console ($COLOR) failed health check on :${HOST_PORT}" >&2
podman logs "$CONTAINER_NAME" 2>&1 | tail -20
exit 1
