#!/usr/bin/env bash
# Deterministic container health probes for Orchestrator VPS shared + presentation tiers.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
COMPOSE="${COMPOSE:-podman-compose}"
ORCH_COMPOSE_PROJECT="${ORCH_COMPOSE_PROJECT:-orchestrator}"
COLOR="${ORCH_HEALTH_COLOR:-all}"

# shellcheck source=orch_publish_env.sh
source "$ORCH_ROOT/deploy/vps/orch_publish_env.sh"

SHARED_SERVICES=(redis coordinator worker scheduler script-spool-init script-runner script-worker)
BLUE_API_SERVICE=api-blue
GREEN_API_SERVICE=api-green
BLUE_CONSOLE_NAME=orchestrator_ops-console_blue
GREEN_CONSOLE_NAME=orchestrator_ops-console_green

log() { printf '[orch-health] %s\n' "$*"; }
fail() { log "FAIL: $*"; exit 1; }

orch_compose() {
  (
    cd "$ORCH_ROOT"
    COMPOSE_PROJECT_NAME="$ORCH_COMPOSE_PROJECT" ${COMPOSE} \
      -f docker-compose.yml \
      -f deploy/vps/docker-compose.vps.yml \
      --env-file .env.vps "$@"
  )
}

orch_compose_bg() {
  (
    cd "$ORCH_ROOT"
    COMPOSE_PROJECT_NAME="$ORCH_COMPOSE_PROJECT" ${COMPOSE} \
      -f docker-compose.yml \
      -f deploy/vps/docker-compose.vps.yml \
      -f deploy/vps/docker-compose.bluegreen.yml \
      --env-file .env.vps "$@"
  )
}

compose_service_cids() {
  local use_bg="$1"
  local svc="$2"
  if [[ "$use_bg" == "1" ]]; then
    orch_compose_bg ps -q "$svc" 2>/dev/null | sed '/^$/d'
  else
    orch_compose ps -q "$svc" 2>/dev/null | sed '/^$/d'
  fi
}

exact_one_compose_cid() {
  local use_bg="$1"
  local svc="$2"
  local -a cids=()
  mapfile -t cids < <(compose_service_cids "$use_bg" "$svc")
  if [[ ${#cids[@]} -eq 0 ]]; then
    return 1
  fi
  if [[ ${#cids[@]} -gt 1 ]]; then
    echo "ambiguous container count (${#cids[@]}) for $svc" >&2
    return 1
  fi
  echo "${cids[0]}"
}

service_health_ok() {
  local cname="$1"
  local svc="$2"
  local status health_status exit_code

  status="$(podman inspect --format '{{.State.Status}}' "$cname" 2>/dev/null || echo missing)"
  case "$svc" in
    script-spool-init)
      exit_code="$(podman inspect --format '{{.State.ExitCode}}' "$cname" 2>/dev/null || echo -1)"
      [[ "$status" == "exited" && "$exit_code" == "0" ]]
      ;;
    script-runner)
      [[ "$status" == "running" ]]
      ;;
    *)
      health_status="$(podman inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$cname" 2>/dev/null || true)"
      if [[ -n "$health_status" ]]; then
        [[ "$health_status" == "healthy" ]]
      else
        [[ "$status" == "running" ]]
      fi
      ;;
  esac
}

check_shared_plane() {
  local svc
  for svc in "${SHARED_SERVICES[@]}"; do
    local cid cname
    cid="$(exact_one_compose_cid 0 "$svc")" || fail "no running container for $svc"
    cname="$(podman inspect --format '{{.Name}}' "$cid" | sed 's#^/##')"
    service_health_ok "$cname" "$svc" || fail "shared service $svc ($cname) unhealthy"
    log "ok: shared $svc ($cname)"
  done
}

check_presentation_color() {
  local color="$1"
  local api_svc api_port console_name console_port
  case "$color" in
    blue)
      api_svc="$BLUE_API_SERVICE"
      api_port=8000
      console_name="$BLUE_CONSOLE_NAME"
      console_port=8081
      ;;
    green)
      api_svc="$GREEN_API_SERVICE"
      api_port=8010
      console_name="$GREEN_CONSOLE_NAME"
      console_port=8091
      ;;
    *) fail "unknown color: $color" ;;
  esac

  local cid cname
  cid="$(exact_one_compose_cid 1 "$api_svc")" || fail "presentation api $api_svc is not running"
  cname="$(podman inspect --format '{{.Name}}' "$cid" | sed 's#^/##')"
  service_health_ok "$cname" "$api_svc" || fail "presentation $api_svc ($cname) unhealthy"
  curl -fsS "$(orch_publish_url "$api_port" /health/)" >/dev/null || fail "host-published API :${api_port}/health/"
  log "ok: api-$color host-published :${api_port}/health/"

  if podman container exists "$console_name" 2>/dev/null; then
    curl -fsS "$(orch_publish_url "$console_port" /)" >/dev/null || fail "host-published console :${console_port}/"
    log "ok: console-$color host-published :${console_port}/"
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
      if compose_service_cids 1 "$GREEN_API_SERVICE" | grep -q .; then
        check_presentation_color green
      fi
      ;;
    *) fail "usage: $0 [shared|blue|green|all]" ;;
  esac
  log "health probe complete"
}

main "$@"
