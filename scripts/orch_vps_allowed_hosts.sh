#!/usr/bin/env bash
# Merge required VPS presentation aliases into DJANGO_ALLOWED_HOSTS without dropping extras.
set -euo pipefail

ORCH_VPS_REQUIRED_ALLOWED_HOSTS=(
  api.thedirectorate.app
  127.0.0.1
  localhost
  www.thedirectorate.app
  orch-api-blue
  orch-api-green
  api-blue
  api-green
  orch-console-blue
  orch-console-green
)

orch_vps_default_allowed_hosts() {
  local IFS=,
  printf '%s' "${ORCH_VPS_REQUIRED_ALLOWED_HOSTS[*]}"
}

orch_merge_django_allowed_hosts() {
  local current="${1:-}"
  local -a merged=()
  local -A seen=()
  local host part

  for host in "${ORCH_VPS_REQUIRED_ALLOWED_HOSTS[@]}"; do
    if [[ -z "${seen[$host]+x}" ]]; then
      merged+=("$host")
      seen[$host]=1
    fi
  done

  if [[ -n "$current" ]]; then
    IFS=',' read -ra parts <<< "$current"
    for part in "${parts[@]}"; do
      host="${part#"${part%%[![:space:]]*}"}"
      host="${host%"${host##*[![:space:]]}"}"
      [[ -z "$host" ]] && continue
      if [[ -z "${seen[$host]+x}" ]]; then
        merged+=("$host")
        seen[$host]=1
      fi
    done
  fi

  local IFS=,
  printf '%s' "${merged[*]}"
}

orch_ensure_env_allowed_hosts_line() {
  local env_file="$1"
  local current merged
  if [[ ! -f "$env_file" ]]; then
    return 0
  fi
  current="$(grep -E '^DJANGO_ALLOWED_HOSTS=' "$env_file" | tail -n1 | cut -d= -f2- || true)"
  merged="$(orch_merge_django_allowed_hosts "$current")"
  if [[ "$merged" == "$current" ]]; then
    return 0
  fi
  if grep -qE '^DJANGO_ALLOWED_HOSTS=' "$env_file"; then
    sed -i "s|^DJANGO_ALLOWED_HOSTS=.*|DJANGO_ALLOWED_HOSTS=${merged}|" "$env_file"
  else
    printf '\nDJANGO_ALLOWED_HOSTS=%s\n' "$merged" >>"$env_file"
  fi
}
