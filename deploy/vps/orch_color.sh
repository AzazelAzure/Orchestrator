#!/usr/bin/env bash
# Orchestrator blue/green presentation deploy, smoke, selector switch, and rollback.
# Shared mutation plane (coordinator, worker, scheduler, Redis, script-worker) is singleton.
# Materialization grant: deploy idle color + smoke only — public selector stays blue.
set -euo pipefail

ORCH_ROOT="${ORCH_ROOT:-$HOME/orchestrator}"
FM_ROOT="${FM_ROOT:-$HOME/finance_manager}"
COMPOSE="${COMPOSE:-podman-compose}"
ORCH_COMPOSE_PROJECT="${ORCH_COMPOSE_PROJECT:-orchestrator}"
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

# shellcheck source=orch_presentation_env.sh
source "$ORCH_ROOT/deploy/vps/orch_presentation_env.sh"

orch_compose_files_bg() {
  local -a files=(
    docker-compose.yml
    deploy/vps/docker-compose.vps.yml
    deploy/vps/docker-compose.bluegreen.yml
  )
  if orch_diag_bind_enabled; then
    files+=(deploy/vps/docker-compose.bluegreen.diag.yml)
  fi
  printf '%s\n' "${files[@]}"
}

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
    local -a args=()
    local file
    while IFS= read -r file; do
      args+=(-f "$file")
    done < <(orch_compose_files_bg)
    COMPOSE_PROJECT_NAME="$ORCH_COMPOSE_PROJECT" ${COMPOSE} \
      "${args[@]}" \
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
    cid="$(compose_service_cids 0 "$svc" | head -n1 || true)"
    if [[ -n "$cid" ]]; then
      echo "$svc $cid" >>"$STATE_DIR/shared_ids.before"
    fi
  done
}

assert_shared_ids_unchanged() {
  local svc cid before after
  for svc in "${SHARED_SERVICES[@]}"; do
    before="$(awk -v s="$svc" '$1==s {print $2}' "$STATE_DIR/shared_ids.before" 2>/dev/null || true)"
    cid="$(compose_service_cids 0 "$svc" | head -n1 || true)"
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
    set -a
    # shellcheck disable=SC1091
    source "$FM_ROOT/.secrets/server.env"
    set +a
  elif [[ -f "$FM_ROOT/.env" ]]; then
    env_args=(--env-file "$FM_ROOT/.env")
    set -a
    # shellcheck disable=SC1091
    source "$FM_ROOT/.env"
    set +a
  fi
  orch_run_edge_proxy_pre_reload_hook || die "edge proxy pre-reload hook failed"
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
  local -a cids=()
  svc="$(api_service_for_color "$color")"
  mapfile -t cids < <(compose_service_cids 1 "$svc")
  if [[ ${#cids[@]} -eq 0 ]]; then
    echo "missing"
    return 0
  fi
  if [[ ${#cids[@]} -gt 1 ]]; then
    die "ambiguous container count (${#cids[@]}) for $svc"
  fi
  cid="${cids[0]}"
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
  bash "$ORCH_ROOT/deploy/vps/ensure_presentation_networks.sh" --color "$color"
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
  local api_cid network api_url console_url bearer
  api_cid="$(orch_resolve_api_cid_for_color "$color")" || die "API container missing for color=$color"
  network="$(orch_presentation_network_for_color "$color")"
  api_url="$(orch_presentation_api_url_in_network "$color" /health/)"
  console_url="$(orch_presentation_console_url_in_network "$color" /)"
  orch_presentation_probe_api "$color" "$api_cid" || die "in-container API health for color=$color"
  orch_presentation_probe_console "$color" || die "in-container console static for color=$color"
  orch_presentation_network_probe_api "$color" || die "in-network API probe on $network"
  orch_presentation_network_probe_console "$color" || die "in-network console probe on $network"
  if orch_diag_bind_enabled; then
    local api_port console_port
    api_port="$(api_loopback_port "$color")"
    console_port="$(console_loopback_port "$color")"
    curl -fsS "http://127.0.0.1:${api_port}/health/" >/dev/null || die "loopback diag API :${api_port}"
    curl -fsS "http://127.0.0.1:${console_port}/" >/dev/null || die "loopback diag console :${console_port}"
  fi
  if [[ -f "$ORCH_ROOT/.env.vps" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$ORCH_ROOT/.env.vps"
    set +a
    bearer="${FOUNDER_API_TOKEN:-${ORCH_TOKEN_FOUNDER:-}}"
    if [[ -n "$bearer" ]]; then
      local code
      code="$(podman run --rm --network "$network" docker.io/curlimages/curl:8.5.0 \
        -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${bearer}" \
        "$(orch_presentation_api_url_in_network "$color" /api/schema/)")"
      [[ "$code" == "200" ]] || die "authenticated schema on $network returned $code"
      code="$(podman run --rm --network "$network" docker.io/curlimages/curl:8.5.0 \
        -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${bearer}" \
        "$(orch_presentation_api_url_in_network "$color" /api/docs/)")"
      [[ "$code" == "200" ]] || die "authenticated docs on $network returned $code"
      code="$(podman run --rm --network "$network" docker.io/curlimages/curl:8.5.0 \
        -s -o /dev/null -w '%{http_code}' \
        "$(orch_presentation_api_url_in_network "$color" /api/schema/)")"
      [[ "$code" == "401" || "$code" == "403" ]] || die "anonymous schema on $network returned $code"
      code="$(podman run --rm --network "$network" docker.io/curlimages/curl:8.5.0 \
        -s -o /dev/null -w '%{http_code}' \
        "$(orch_presentation_api_url_in_network "$color" /api/docs/)")"
      [[ "$code" == "401" || "$code" == "403" ]] || die "anonymous docs on $network returned $code"
      code="$(podman run --rm --network "$network" docker.io/curlimages/curl:8.5.0 \
        -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer ${bearer}" \
        "$(orch_presentation_console_url_in_network "$color" /ops/summary/)")"
      [[ "$code" == "200" ]] || die "authenticated console /ops/summary/ on $network returned $code"
      log "ok: bearer schema/docs, console proxy, and anonymous deny on $network"
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
  smoke --color blue|green   In-network + in-container probes; optional loopback diag
  switch --color blue|green  Update edge selector + reload (blocked when materialize-only)
  rollback [--color blue|green]  Restore prior selector from .state rollback file

environment:
  ORCH_COLOR_MATERIALIZE_ONLY=1  Default — blocks switch (initial materialization grant)
  ORCH_COMPOSE_PROJECT=orchestrator  Compose project name (pinned CWD=$ORCH_ROOT)
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
