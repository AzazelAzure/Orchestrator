#!/usr/bin/env bash
# Presentation-tier network naming, optional loopback diagnostics, and in-container probes.
# Source from deploy/vps/*.sh — not invoked directly.
set -euo pipefail

ORCH_PRESENTATION_NETWORK_BLUE="${ORCH_PRESENTATION_NETWORK_BLUE:-orchestrator-console-blue}"
ORCH_PRESENTATION_NETWORK_GREEN="${ORCH_PRESENTATION_NETWORK_GREEN:-orchestrator-console-green}"
ORCH_API_ALIAS_BLUE="${ORCH_API_ALIAS_BLUE:-orch-api-blue}"
ORCH_API_ALIAS_GREEN="${ORCH_API_ALIAS_GREEN:-orch-api-green}"
ORCH_CONSOLE_ALIAS_BLUE="${ORCH_CONSOLE_ALIAS_BLUE:-orch-console-blue}"
ORCH_CONSOLE_ALIAS_GREEN="${ORCH_CONSOLE_ALIAS_GREEN:-orch-console-green}"

orch_presentation_network_for_color() {
  case "$1" in
    blue) printf '%s' "$ORCH_PRESENTATION_NETWORK_BLUE" ;;
    green) printf '%s' "$ORCH_PRESENTATION_NETWORK_GREEN" ;;
    *) echo "invalid presentation color: $1" >&2; return 1 ;;
  esac
}

orch_api_alias_for_color() {
  case "$1" in
    blue) printf '%s' "$ORCH_API_ALIAS_BLUE" ;;
    green) printf '%s' "$ORCH_API_ALIAS_GREEN" ;;
    *) echo "invalid presentation color: $1" >&2; return 1 ;;
  esac
}

orch_console_alias_for_color() {
  case "$1" in
    blue) printf '%s' "$ORCH_CONSOLE_ALIAS_BLUE" ;;
    green) printf '%s' "$ORCH_CONSOLE_ALIAS_GREEN" ;;
    *) echo "invalid presentation color: $1" >&2; return 1 ;;
  esac
}

orch_console_container_for_color() {
  case "$1" in
    blue) printf '%s' "orchestrator_ops-console_blue" ;;
    green) printf '%s' "orchestrator_ops-console_green" ;;
    *) echo "invalid presentation color: $1" >&2; return 1 ;;
  esac
}

orch_diag_bind_load() {
  if [[ -n "${ORCH_DIAG_BIND:-}" ]]; then
    return 0
  fi
  local env_file="${ORCH_DIAG_ENV_FILE:-}"
  if [[ -z "$env_file" && -n "${ORCH_ROOT:-}" ]]; then
    env_file="$ORCH_ROOT/.env.vps"
  fi
  if [[ -n "$env_file" && -f "$env_file" ]]; then
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        ORCH_DIAG_BIND=*)
          ORCH_DIAG_BIND="${line#ORCH_DIAG_BIND=}"
          ORCH_DIAG_BIND="${ORCH_DIAG_BIND%\"}"
          ORCH_DIAG_BIND="${ORCH_DIAG_BIND#\"}"
          ORCH_DIAG_BIND="${ORCH_DIAG_BIND%\'}"
          ORCH_DIAG_BIND="${ORCH_DIAG_BIND#\'}"
          return 0
          ;;
      esac
    done <"$env_file"
  fi
  return 1
}

orch_diag_bind_validate() {
  local bind="${1:-}"
  case "$bind" in
    ""|127.0.0.1) return 0 ;;
    *)
      echo "invalid ORCH_DIAG_BIND: only 127.0.0.1 loopback diagnostics are permitted (got: $bind)" >&2
      return 1
      ;;
  esac
}

orch_diag_bind_enabled() {
  orch_diag_bind_load || true
  orch_diag_bind_validate "${ORCH_DIAG_BIND:-}"
  [[ "${ORCH_DIAG_BIND:-}" == "127.0.0.1" ]]
}

orch_diag_publish_args() {
  local host_port="$1"
  local container_port="$2"
  if orch_diag_bind_enabled; then
    printf '%s %s' "-p" "127.0.0.1:${host_port}:${container_port}"
  fi
}

orch_exec_http_ok() {
  local cid="$1"
  local url="$2"
  if podman exec "$cid" wget -q -O /dev/null "$url" >/dev/null 2>&1; then
    return 0
  fi
  if podman exec "$cid" curl -fsS --max-time 10 "$url" >/dev/null 2>&1; then
    return 0
  fi
  podman exec "$cid" python -c \
    "import urllib.request; urllib.request.urlopen('${url}')" >/dev/null 2>&1
}

orch_edge_proxy_pre_reload_load() {
  if [[ -n "${ORCH_EDGE_PROXY_PRE_RELOAD_CMD:-}" ]]; then
    return 0
  fi
  local env_file="${ORCH_EDGE_PROXY_ENV_FILE:-}"
  if [[ -z "$env_file" && -n "${ORCH_ROOT:-}" ]]; then
    env_file="$ORCH_ROOT/.env.vps"
  fi
  if [[ -n "$env_file" && -f "$env_file" ]]; then
    local line key val
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in
        ORCH_EDGE_PROXY_PRE_RELOAD_CMD=*|ORCH_EDGE_PROXY_PRE_RELOAD_REQUIRED=*)
          key="${line%%=*}"
          val="${line#*=}"
          val="${val%\"}"
          val="${val#\"}"
          val="${val%\'}"
          val="${val#\'}"
          printf -v "$key" '%s' "$val"
          ;;
      esac
    done <"$env_file"
  fi
}

orch_run_edge_proxy_pre_reload_hook() {
  orch_edge_proxy_pre_reload_load || true
  local hook="${ORCH_EDGE_PROXY_PRE_RELOAD_CMD:-}"
  if [[ -z "$hook" ]]; then
    if [[ "${ORCH_EDGE_PROXY_PRE_RELOAD_REQUIRED:-0}" == "1" ]]; then
      echo "ORCH_EDGE_PROXY_PRE_RELOAD_REQUIRED=1 but ORCH_EDGE_PROXY_PRE_RELOAD_CMD is unset" >&2
      return 1
    fi
    return 0
  fi
  bash -lc "$hook"
}

orch_presentation_probe_api() {
  local color="$1"
  local cid="${2:-}"
  if [[ -z "$cid" ]]; then
    cid="$(orch_resolve_api_cid_for_color "$color")" || return 1
  fi
  orch_exec_http_ok "$cid" "http://127.0.0.1:8000/health/"
}

orch_presentation_probe_console() {
  local color="$1"
  local name
  name="$(orch_console_container_for_color "$color")"
  if ! podman container exists "$name" 2>/dev/null; then
    echo "console container missing: $name" >&2
    return 1
  fi
  orch_exec_http_ok "$name" "http://127.0.0.1:8081/"
}

orch_presentation_api_url_in_network() {
  local color="$1"
  local path="${2:-/health/}"
  local alias
  alias="$(orch_api_alias_for_color "$color")"
  printf 'http://%s:8000%s' "$alias" "$path"
}

orch_presentation_console_url_in_network() {
  local color="$1"
  local path="${2:-/}"
  local alias
  alias="$(orch_console_alias_for_color "$color")"
  printf 'http://%s:8081%s' "$alias" "$path"
}

orch_curl_from_network() {
  local network="$1"
  local url="$2"
  podman run --rm --network "$network" docker.io/curlimages/curl:8.5.0 \
    -fsS --max-time 10 "$url" >/dev/null
}

orch_presentation_network_probe_api() {
  local color="$1"
  local network url
  network="$(orch_presentation_network_for_color "$color")"
  url="$(orch_presentation_api_url_in_network "$color" /health/)"
  orch_curl_from_network "$network" "$url"
}

orch_presentation_network_probe_console() {
  local color="$1"
  local network url
  network="$(orch_presentation_network_for_color "$color")"
  url="$(orch_presentation_console_url_in_network "$color" /)"
  orch_curl_from_network "$network" "$url"
}

orch_resolve_api_cid_for_color() {
  local color="$1"
  local svc compose="${COMPOSE:-podman-compose}"
  local project="${ORCH_COMPOSE_PROJECT:-orchestrator}"
  case "$color" in
    blue) svc="api-blue" ;;
    green) svc="api-green" ;;
    *) echo "invalid color: $color" >&2; return 1 ;;
  esac
  local -a cids=()
  mapfile -t cids < <(
    COMPOSE_PROJECT_NAME="$project" ${compose} \
      -f "${ORCH_ROOT}/docker-compose.yml" \
      -f "${ORCH_ROOT}/deploy/vps/docker-compose.vps.yml" \
      -f "${ORCH_ROOT}/deploy/vps/docker-compose.bluegreen.yml" \
      --env-file "${ORCH_ROOT}/.env.vps" ps -q "$svc" 2>/dev/null | sed '/^$/d'
  )
  if [[ ${#cids[@]} -eq 0 ]]; then
    echo "no running API container for color=$color" >&2
    return 1
  fi
  if [[ ${#cids[@]} -gt 1 ]]; then
    echo "ambiguous API container count (${#cids[@]}) for color=$color" >&2
    return 1
  fi
  printf '%s' "${cids[0]}"
}
