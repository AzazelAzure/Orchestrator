#!/usr/bin/env bash
# ORCH_PUBLISH_HOST load, validation, and probe helpers for VPS deploy scripts.
# Source from deploy/vps/*.sh — not invoked directly.
set -euo pipefail

orch_publish_host_parse_line() {
  local line="${1#ORCH_PUBLISH_HOST=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

orch_publish_host_from_file() {
  local env_file="$1"
  local line
  if [[ ! -f "$env_file" ]]; then
    return 1
  fi
  ORCH_PUBLISH_HOST=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      ORCH_PUBLISH_HOST=*)
        ORCH_PUBLISH_HOST="$(orch_publish_host_parse_line "$line")"
        ;;
    esac
  done <"$env_file"
  if [[ -n "${ORCH_PUBLISH_HOST:-}" ]]; then
    return 0
  fi
  return 1
}

orch_publish_host_load() {
  if [[ -n "${ORCH_PUBLISH_HOST:-}" ]]; then
    return 0
  fi
  local env_file="${ORCH_PUBLISH_ENV_FILE:-}"
  if [[ -z "$env_file" && -n "${ORCH_ROOT:-}" ]]; then
    env_file="$ORCH_ROOT/.env.vps"
  fi
  if [[ -n "$env_file" ]] && orch_publish_host_from_file "$env_file"; then
    return 0
  fi
  return 1
}

orch_publish_host_validate() {
  local host="$1"
  case "$host" in
    ""|0.0.0.0|0|"*"|::)
      echo "invalid ORCH_PUBLISH_HOST: binds all interfaces or is empty" >&2
      return 1
      ;;
  esac
  if [[ "$host" == *:* ]]; then
    echo "ORCH_PUBLISH_HOST must be a host address, not a port mapping" >&2
    return 1
  fi
  return 0
}

orch_publish_host_require() {
  local env_file="${ORCH_ROOT:-}/.env.vps"
  if ! orch_publish_host_from_file "$env_file"; then
    echo "ORCH_PUBLISH_HOST is required in $env_file (installation bridge/gateway — not 0.0.0.0)" >&2
    return 1
  fi
  orch_publish_host_validate "$ORCH_PUBLISH_HOST"
}

orch_publish_probe_host() {
  if orch_publish_host_load; then
    orch_publish_host_validate "$ORCH_PUBLISH_HOST"
    printf '%s' "$ORCH_PUBLISH_HOST"
    return 0
  fi
  printf '%s' "127.0.0.1"
}

orch_publish_url() {
  local port="$1"
  local path="${2:-/}"
  local host
  host="$(orch_publish_probe_host)"
  printf 'http://%s:%s%s' "$host" "$port" "$path"
}
