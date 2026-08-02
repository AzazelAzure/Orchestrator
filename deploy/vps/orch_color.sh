#!/usr/bin/env bash
# Orchestrator blue/green presentation deploy, smoke, selector switch, and rollback.
# Shared mutation plane (coordinator, worker, scheduler, Redis, script-worker) is singleton.
# Materialization grant: deploy idle color + smoke only — public selector stays blue.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
FM_ROOT="${FM_ROOT:-$HOME/finance_manager}"
COMPOSE="${COMPOSE:-podman-compose}"
FM_PROJECT="${FM_PROJECT:-fm-beta}"
STATE_DIR="$ORCH_ROOT/deploy/vps/.state"
ROLLBACK_FILE="$STATE_DIR/orch_active_color.prev"
SELECTOR_REL="proxy/conf.d/orch_active_color.conf"

SHARED_SERVICES=(redis coordinator worker scheduler script-spool-init script-runner script-worker)
BLUE_API_SERVICE=api-blue
GREEN_API_SERVICE=api-green

# Materialization-only mode: refuse traffic switch (public blue preserved).
ORCH_COLOR_MATERIALIZE_ONLY="${ORCH_COLOR_MATERIALIZE_ONLY:-1}"

log() { printf '[orch-color] %s\n' "$*"; }
die() { log "ERROR: $*"; exit 1; }

orch_compose() {
  ${COMPOSE} -f "$ORCH_ROOT/docker-compose.yml" \
    -f "$ORCH_ROOT/deploy/vps/docker-compose.vps.yml" \
    --env-file "$ORCH_ROOT/.env.vps" "$@"
}

orch_compose_bg() {
  ${COMPOSE} -f "$ORCH_ROOT/docker-compose.yml" \
    -f "$ORCH_ROOT/deploy/vps/docker-compose.vps.yml" \
    -f "$ORCH_ROOT/deploy/vps/docker-compose.bluegreen.yml" \
    --env-file "$ORCH_ROOT/.env.vps" "$@"
}

selector_path() {
  echo "$FM_ROOT/$SELECTOR_REL"
}

read_active_color() {
  local path
  path="$(selector_path)"
  if [[ ! -f "$path" ]]; then
    echo blue
    return 0
  fi
  if grep -q 'default green' "$path"; then
    echo green
  else
    echo blue
  fi
}

write_selector_map() {
  local color="$1"
  local dest
  dest="$(selector_path)"
  mkdir -p "$(dirname "$dest")"
  cat >"$dest" <<EOF
map \$request_uri \$orch_active_color {
    default ${color};
}
EOF
}

capture_shared_ids() {
  mkdir -p "$STATE_DIR"
  : >"$STATE_DIR/shared_ids.before"
  local svc cid
  for svc in "${SHARED_SERVICES[@]}"; do
    cid="$(orch_compose ps -q "$svc" 2>/dev/null | head -n1 || true)"
    if [[ -n "$cid" ]]; then
      echo "$svc $cid" >>"$STATE_DIR/shared_ids.before"
    fi
  done
}

assert_shared_ids_unchanged() {
  local svc cid before after
  for svc in "${SHARED_SERVICES[@]}"; do
    before="$(awk -v s="$svc" '$1==s {print $2}' "$STATE_DIR/shared_ids.before" 2>/dev/null || true)"
    cid="$(orch_compose ps -q "$svc" 2>/dev/null | head -n1 || true)"
    after="${cid:-}"
    if [[ -n "$before" && -n "$after" && "$before" != "$after" ]]; then
      die "shared service $svc container ID changed ($before -> $after) — abort (shared-plane identity guard)"
    fi
  done
}

reload_edge_proxy() {
  if [[ ! -f "$FM_ROOT/docker-compose.bluegreen.yml" ]]; then
    log "skip proxy reload — $FM_ROOT/docker-compose.bluegreen.yml missing"
    return 0
  fi
  local env_args=()
  if [[ -f "$FM_ROOT/.secrets/server.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.secrets/server.env")
  elif [[ -f "$FM_ROOT/.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.env")
  fi
  (
    cd "$FM_ROOT"
    COMPOSE_PROJECT_NAME="$FM_PROJECT" ${COMPOSE} -f docker-compose.bluegreen.yml "${env_args[@]}" \
      exec -T proxy nginx -t
    COMPOSE_PROJECT_NAME="$FM_PROJECT" ${COMPOSE} -f docker-compose.bluegreen.yml "${env_args[@]}" \
      exec -T proxy nginx -s reload
  )
}

api_service_for_color() {
  case "$1" in
    blue) echo "$BLUE_API_SERVICE" ;;
    green) echo "$GREEN_API_SERVICE" ;;
    *) die "invalid color: $1" ;;
  esac
}

api_loopback_port() {
  case "$1" in
    blue) echo 8000 ;;
    green) echo 8010 ;;
    *) die "invalid color: $1" ;;
  esac
}

console_loopback_port() {
  case "$1" in
    blue) echo 8081 ;;
    green) echo 8091 ;;
    *) die "invalid color: $1" ;;
  esac
}

slot_image_digest() {
  local color="$1"
  local svc cid digest
  svc="$(api_service_for_color "$color")"
  cid="$(orch_compose_bg ps -q "$svc" 2>/dev/null | head -n1 || true)"
  if [[ -z "$cid" ]]; then
    echo "missing"
    return 0
  fi
  digest="$(podman inspect --format '{{.ImageDigest}}' "$cid" 2>/dev/null || true)"
  if [[ -z "$digest" || "$digest" == "<no value>" ]]; then
    digest="$(podman inspect --format '{{.Image}}' "$cid" 2>/dev/null || echo unknown)"
  fi
  echo "$digest"
}

deploy_shared_plane() {
  [[ -f "$ORCH_ROOT/.env.vps" ]] || die "missing $ORCH_ROOT/.env.vps"
  log "deploy shared mutation plane (no presentation api)"
  orch_compose up -d --build "${SHARED_SERVICES[@]}"
}

deploy_color_cmd() {
  local color="${1:-}"
  [[ "$color" == blue || "$color" == green ]] || die "deploy requires --color blue|green"
  capture_shared_ids
  local svc
  svc="$(api_service_for_color "$color")"
  log "deploy presentation $svc (--no-deps)"
  orch_compose_bg up -d --build --no-deps "$svc"
  bash "$ORCH_ROOT/deploy/vps/run_ops_console.sh" --color "$color"
  assert_shared_ids_unchanged
  log "deploy complete for color=$color digest=$(slot_image_digest "$color")"
}

status_cmd() {
  local active
  active="$(read_active_color)"
  log "active selector color: $active (materialize-only=$ORCH_COLOR_MATERIALIZE_ONLY)"
  local color digest
  for color in blue green; do
    digest="$(slot_image_digest "$color")"
    log "slot $color api digest: $digest"
  done
  if [[ "$(slot_image_digest blue)" == missing && "$(slot_image_digest green)" == missing ]]; then
    die "no presentation API slots running"
  fi
}

smoke_color_cmd() {
  local color="${1:-green}"
  [[ "$color" == blue || "$color" == green ]] || die "smoke requires --color blue|green"
  local api_port console_port bearer
  api_port="$(api_loopback_port "$color")"
  console_port="$(console_loopback_port "$color")"
  curl -fsS "http://127.0.0.1:${api_port}/health/" >/dev/null || die "API health :${api_port}"
  curl -fsS "http://127.0.0.1:${console_port}/" >/dev/null || die "console static :${console_port}"
  if [[ -f "$ORCH_ROOT/.env.vps" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ORCH_ROOT/.env.vps"
    set +a
    bearer="${FOUNDER_API_TOKEN:-${ORCH_TOKEN_FOUNDER:-}}"
    if [[ -n "$bearer" ]]; then
      local code
      code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${bearer}" \
        "http://127.0.0.1:${api_port}/api/schema/")"
      [[ "$code" == "200" ]] || die "authenticated schema on :${api_port} returned $code"
      code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${bearer}" \
        "http://127.0.0.1:${api_port}/api/docs/")"
      [[ "$code" == "200" ]] || die "authenticated docs on :${api_port} returned $code"
      code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${api_port}/api/schema/")"
      [[ "$code" == "401" || "$code" == "403" ]] || die "anonymous schema on :${api_port} returned $code"
      code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${api_port}/api/docs/")"
      [[ "$code" == "401" || "$code" == "403" ]] || die "anonymous docs on :${api_port} returned $code"
      log "ok: bearer schema/docs and anonymous deny on :${api_port}"
    else
      log "skip bearer schema/docs — no FOUNDER_API_TOKEN in .env.vps"
    fi
  fi
  log "smoke ok for color=$color"
}

switch_cmd() {
  local to_color="${1:-}"
  [[ "$to_color" == blue || "$to_color" == green ]] || die "switch requires --color blue|green"
  if [[ "$ORCH_COLOR_MATERIALIZE_ONLY" == "1" ]]; then
    die "switch blocked — ORCH_COLOR_MATERIALIZE_ONLY=1 (materialization grant; public blue preserved)"
  fi
  local from_color
  from_color="$(read_active_color)"
  mkdir -p "$STATE_DIR"
  echo "$from_color" >"$ROLLBACK_FILE"
  local tmp
  tmp="$(mktemp)"
  write_selector_map "$to_color"
  cp "$(selector_path)" "$tmp"
  if ! reload_edge_proxy; then
    write_selector_map "$from_color"
    die "proxy reload failed — restored selector to $from_color"
  fi
  smoke_color_cmd "$to_color" || {
    write_selector_map "$from_color"
    reload_edge_proxy || true
    die "post-switch smoke failed — rolled back selector to $from_color"
  }
  log "switch complete: $from_color -> $to_color"
}

rollback_cmd() {
  local prior="${1:-}"
  if [[ -z "$prior" && -f "$ROLLBACK_FILE" ]]; then
    prior="$(tr -d '[:space:]' <"$ROLLBACK_FILE")"
  fi
  [[ "$prior" == blue || "$prior" == green ]] || die "rollback requires prior color in $ROLLBACK_FILE or --color"
  if [[ "$ORCH_COLOR_MATERIALIZE_ONLY" == "1" ]]; then
    # Rehearsal: restore staged selector without leaving green on public routes.
    log "rollback rehearsal (materialize-only): restoring selector to $prior"
  fi
  write_selector_map "$prior"
  reload_edge_proxy || die "rollback proxy reload failed"
  smoke_color_cmd "$prior" || die "rollback smoke failed for color=$prior"
  log "rollback complete: active selector=$prior"
}

disable_singleton_console_unit() {
  if systemctl --user is-enabled ops-console.service >/dev/null 2>&1; then
    log "disabling legacy singleton ops-console.service"
    systemctl --user disable --now ops-console.service || true
  fi
}

usage() {
  cat <<'EOF'
usage: orch_color.sh <command> [options]

commands:
  deploy shared              Start singleton shared mutation plane only
  deploy --color blue|green  Start one presentation API slot + console (--no-deps)
  status                     Report selector color and per-slot image digests
  smoke --color blue|green   Loopback health, console static, bearer schema/docs
  switch --color blue|green  Update edge selector + reload (blocked when materialize-only)
  rollback [--color blue|green]  Restore prior selector from .state rollback file

environment:
  ORCH_COLOR_MATERIALIZE_ONLY=1  Default — blocks switch (initial materialization grant)
EOF
}

main() {
  local cmd="${1:-}"
  shift || true
  case "$cmd" in
    deploy)
      if [[ "${1:-}" == shared ]]; then
        shift
        deploy_shared_plane
      elif [[ "${1:-}" == --color ]]; then
        deploy_color_cmd "${2:-}"
      else
        usage
        exit 1
      fi
      ;;
    status) status_cmd ;;
    smoke)
      [[ "${1:-}" == --color ]] || die "smoke requires --color blue|green"
      smoke_color_cmd "${2:-}"
      ;;
    switch)
      [[ "${1:-}" == --color ]] || die "switch requires --color blue|green"
      switch_cmd "${2:-}"
      ;;
    rollback)
      local prior=""
      if [[ "${1:-}" == --color ]]; then
        prior="${2:-}"
      fi
      rollback_cmd "$prior"
      ;;
    disable-singleton-console) disable_singleton_console_unit ;;
    "") usage; exit 1 ;;
    *) die "unknown command: $cmd" ;;
  esac
}

main "$@"
