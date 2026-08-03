#!/usr/bin/env bash
# Render ecosystem-hosts.conf from ORCH_PUBLISH_HOST (installation bridge/gateway).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEMPLATE="${REPO_ROOT}/proxy/conf.d/ecosystem-hosts.conf.template"
CANONICAL="${REPO_ROOT}/proxy/conf.d/ecosystem-hosts.conf"
DEPLOY_ARTIFACT="${REPO_ROOT}/proxy/conf.d/ecosystem-hosts.deploy.conf"
OUT="${1:-$DEPLOY_ARTIFACT}"

load_publish_host() {
  if [[ -n "${ORCH_PUBLISH_HOST:-}" ]]; then
    return 0
  fi
  local env_file="${ORCH_PUBLISH_ENV_FILE:-}"
  if [[ -z "$env_file" && -f "${REPO_ROOT}/.env" ]]; then
    env_file="${REPO_ROOT}/.env"
  fi
  if [[ -z "$env_file" && -f "${REPO_ROOT}/.secrets/server.env" ]]; then
    env_file="${REPO_ROOT}/.secrets/server.env"
  fi
  if [[ -n "$env_file" && -f "$env_file" ]]; then
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        ORCH_PUBLISH_HOST=*)
          ORCH_PUBLISH_HOST="${line#ORCH_PUBLISH_HOST=}"
          ORCH_PUBLISH_HOST="${ORCH_PUBLISH_HOST%\"}"
          ORCH_PUBLISH_HOST="${ORCH_PUBLISH_HOST#\"}"
          return 0
        ;;
      esac
    done <"$env_file"
  fi
  return 1
}

validate_host() {
  case "${ORCH_PUBLISH_HOST:-}" in
    ""|0.0.0.0|0|"*"|::)
      echo "invalid ORCH_PUBLISH_HOST: must be a non-public bind address (not 0.0.0.0)" >&2
      return 1
      ;;
  esac
  if [[ "$ORCH_PUBLISH_HOST" == *:* ]]; then
    echo "ORCH_PUBLISH_HOST must be a host address only" >&2
    return 1
  fi
  return 0
}

usage() {
  cat <<'EOF'
usage: render_ecosystem_hosts.sh [OUTPUT_PATH]

Renders ecosystem-hosts.conf.template to a deployment artifact (default:
proxy/conf.d/ecosystem-hosts.deploy.conf). Does not write the tracked local
canonical proxy/conf.d/ecosystem-hosts.conf.
Requires ORCH_PUBLISH_HOST in the environment or repo .env / .secrets/server.env.
EOF
}

if [[ "${1:-}" == -h || "${1:-}" == --help ]]; then
  usage
  exit 0
fi

[[ -f "$TEMPLATE" ]] || { echo "missing template: $TEMPLATE" >&2; exit 1; }

if ! load_publish_host; then
  echo "ORCH_PUBLISH_HOST is required (env, .env, or .secrets/server.env)" >&2
  exit 1
fi
validate_host

if [[ "$OUT" == "$CANONICAL" ]]; then
  echo "refusing to render over tracked canonical file: $CANONICAL" >&2
  echo "use default deploy artifact or an explicit staging path" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUT")"
sed "s/@ORCH_PUBLISH_HOST@/${ORCH_PUBLISH_HOST}/g" "$TEMPLATE" >"$OUT"
echo "rendered $OUT (ORCH_PUBLISH_HOST=$ORCH_PUBLISH_HOST)"
