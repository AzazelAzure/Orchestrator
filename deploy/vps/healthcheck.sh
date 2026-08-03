#!/usr/bin/env bash
# Deterministic container health probes for Orchestrator VPS shared + presentation tiers.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
COMPOSE="${COMPOSE:-podman-compose}"
COLOR="${ORCH_HEALTH_COLOR:-all}"

SHARED_SERVICES=(redis coordinator worker scheduler script-spool-init script-runner script-worker)
BLUE_API_SERVICE=api-blue
GREEN_API_SERVICE=api-green
BLUE_CONSOLE_NAME=orchestrator_ops-console_blue
GREEN_CONSOLE_NAME=orchestrator_ops-console_green

log() { printf '[orch-health] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

# shellcheck source=orch_presentation_env.sh
source "$ORCH_ROOT/deploy/vps/orch_presentation_env.sh"

orch_compose() {
  ${COMPOSE} -f "$ORCH_ROOT/docker-compose.yml" \
    -f "$ORCH_ROOT/deploy/vps/docker-compose.vps.yml" \
    --env-file "$ORCH_ROOT/.env.vps" "$@"
}

orch_compose_bg() {
  local -a args=()
  local file
  while IFS= read -r file; do
    args+=(-f "$ORCH_ROOT/$file")
  done < <(cd "$ORCH_ROOT" && orch_compose_files_bg)
  ${COMPOSE} "${args[@]}" --env-file "$ORCH_ROOT/.env.vps" "$@"
}

container_health() {
  local name="$1"
  local status
  status="$(podman inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$name" 2>/dev/null || echo missing)"
  case "$status" in
    healthy|running) return 0 ;;
    *) return 1 ;;
  esac
}

check_shared_plane() {
  local svc
  for svc in "${SHARED_SERVICES[@]}"; do
    local cid
    cid="$(orch_compose ps -q "$svc" 2>/dev/null | head -n1 || true)"
    if [[ -z "$cid" ]]; then
      fail "shared service $svc is not running"
    fi
    local cname
    cname="$(podman inspect --format '{{.Name}}' "$cid" | sed 's#^/##')"
    container_health "$cname" || fail "shared service $svc ($cname) unhealthy"
    log "ok: shared $svc ($cname)"
  done
}

check_presentation_color() {
  local color="$1"
  local api_svc
  case "$color" in
    blue) api_svc="$BLUE_API_SERVICE" ;;
    green) api_svc="$GREEN_API_SERVICE" ;;
    *) fail "unknown color: $color" ;;
  esac

  local cid
  cid="$(orch_compose_bg ps -q "$api_svc" 2>/dev/null | head -n1 || true)"
  [[ -n "$cid" ]] || fail "presentation api $api_svc is not running"
  local cname
  cname="$(podman inspect --format '{{.Name}}' "$cid" | sed 's#^/##')"
  container_health "$cname" || fail "presentation $api_svc ($cname) unhealthy"
  orch_presentation_probe_api "$color" "$cid" || fail "in-container API health for color=$color"
  log "ok: api-$color in-container /health/"
  orch_presentation_network_probe_api "$color" || fail "in-network API probe for color=$color"
  log "ok: api-$color in-network probe on $(orch_presentation_network_for_color "$color")"

  local console_name
  console_name="$(orch_console_container_for_color "$color")"
  if podman container exists "$console_name" 2>/dev/null; then
    orch_presentation_probe_console "$color" || fail "in-container console probe for color=$color"
    orch_presentation_network_probe_console "$color" || fail "in-network console probe for color=$color"
    log "ok: console-$color in-network probe"
  else
    log "skip: console container $console_name not present"
  fi
}

main() {
  [[ -f "$ORCH_ROOT/.env.vps" ]] || fail "missing $ORCH_ROOT/.env.vps"
  case "$COLOR" in
    shared) check_shared_plane ;;
    blue|green) check_shared_plane; check_presentation_color "$COLOR" ;;
    all)
      check_shared_plane
      check_presentation_color blue
      if orch_compose_bg ps -q "$GREEN_API_SERVICE" 2>/dev/null | grep -q .; then
        check_presentation_color green
      fi
      ;;
    *) fail "usage: $0 [shared|blue|green|all]" ;;
  esac
  log "health probe complete"
}

main "$@"
