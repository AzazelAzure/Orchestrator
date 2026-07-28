#!/usr/bin/env bash
# Host-side Compose helper for R4D scripts (same env contract as r4d_compose_exec.sh).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/container_runtime.sh"

COMPOSE_FILE="${ORCH_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
PROJECT="${ORCH_COMPOSE_PROJECT:-orch-r4d}"
ENV_FILE="${ORCH_R4D_ENV_FILE:?ORCH_R4D_ENV_FILE required}"

COMPOSE_ENVFILE_OK=0
if orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" --env-file "${ENV_FILE}" config >/dev/null 2>&1; then
  COMPOSE_ENVFILE_OK=1
fi

if [[ "${COMPOSE_ENVFILE_OK}" == "1" ]]; then
  orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" --env-file "${ENV_FILE}" "$@"
else
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
  orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" "$@"
fi
