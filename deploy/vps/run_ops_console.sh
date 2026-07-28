#!/usr/bin/env bash
# Run ops-console via plain podman (podman-compose breaks on network_mode for this service).
set -euo pipefail
ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
cd "$ORCH_ROOT"
IMAGE="${ORCH_OPS_CONSOLE_IMAGE:-orchestrator-ops-console:vps}"
API_BASE="${VITE_API_BASE_URL:-https://api.thedirectorate.app}"

podman build -t "$IMAGE" ./ops-console --build-arg "VITE_API_BASE_URL=${API_BASE}"
podman rm -f orchestrator_ops-console_1 2>/dev/null || true
podman run -d \
  --name orchestrator_ops-console_1 \
  --restart unless-stopped \
  -p 8081:8081 \
  --user 0:0 \
  "$IMAGE"

for _ in 1 2 3 4 5; do
  if curl -fsS "http://127.0.0.1:8081/health" >/dev/null 2>&1; then
    echo "ops-console running on :8081"
    exit 0
  fi
  sleep 1
done
echo "ops-console failed health check" >&2
podman logs orchestrator_ops-console_1 2>&1 | tail -20
exit 1
