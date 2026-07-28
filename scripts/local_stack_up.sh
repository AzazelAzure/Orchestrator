#!/usr/bin/env bash
# Bring up a persistent local Orchestrator Compose stack for daily agent use.
# Writes .tmp/local-stack/manifest.json (env path, work_item_id, API URLs).
# Does not tear down on exit. Does not close gates.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/container_runtime.sh"

STACK_DIR="${ORCH_LOCAL_STACK_DIR:-$ROOT/.tmp/local-stack}"
ENV_FILE="${STACK_DIR}/env"
MANIFEST="${STACK_DIR}/manifest.json"
COMPOSE_FILE="${ORCH_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
PROJECT="${ORCH_COMPOSE_PROJECT:-orch-local}"
API_BASE="${ORCH_LOCAL_API_BASE:-http://127.0.0.1:8000}"
FORCE="${ORCH_LOCAL_STACK_FORCE:-0}"

log() { printf '[local-stack] %s\n' "$*"; }
fail() { printf '[local-stack] ERROR: %s\n' "$*" >&2; exit 1; }

wait_http() {
  local url="$1" name="$2" attempts="${3:-90}"
  local i
  for i in $(seq 1 "${attempts}"); do
    if python3 - <<PY
import urllib.request
try:
    urllib.request.urlopen("${url}", timeout=3)
except Exception:
    raise SystemExit(1)
PY
    then
      log "healthy: ${name}"
      return 0
    fi
    sleep 2
  done
  fail "timed out waiting for ${name} at ${url}"
}

if [[ "${FORCE}" != "1" && -f "${MANIFEST}" ]]; then
  if wait_http "${API_BASE}/health/" "api (existing)" 3; then
    log "stack already healthy — manifest ${MANIFEST}"
    cat "${MANIFEST}"
    exit 0
  fi
  log "manifest present but API unhealthy — rebuilding stack"
fi

RUNTIME="$(orch_detect_container_runtime)" || fail "container runtime unavailable"
log "runtime: ${RUNTIME}"

mkdir -p "${STACK_DIR}/attestations" "${STACK_DIR}/logs"
chmod 700 "${STACK_DIR}"

export ORCH_R4D_RUN_DIR="${STACK_DIR}"
export ORCH_R4D_ENV_FILE="${ENV_FILE}"
RESEED="${ORCH_LOCAL_STACK_RESEED:-0}"
if [[ "${RESEED}" == "1" || ! -f "${ENV_FILE}" ]]; then
  eval "$(bash "$ROOT/scripts/r4d_generate_ephemeral_env.sh")"
else
  log "reusing existing env ${ENV_FILE} (set ORCH_LOCAL_STACK_RESEED=1 to rotate tokens)"
fi
[[ -f "${ENV_FILE}" ]] || fail "env file missing"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

ATTESTATION_OUT="${STACK_DIR}/attestations/script-runner.attestation.json"
export ORCH_SCRIPT_RUNNER_ATTESTATION_OUT="${ATTESTATION_OUT}"
export ORCH_SCRIPT_RUNNER_ATTESTATION_HOST_PATH="${ATTESTATION_OUT}"
export ORCH_ATTESTATION_HMAC_KEY

log "building script-runner attestation"
bash "$ROOT/scripts/build_script_runner_attestation.sh" \
  >"${STACK_DIR}/logs/attestation-build.txt" 2>&1
[[ -f "${ATTESTATION_OUT}" ]] || fail "attestation missing"

python3 - <<PY
import json
from pathlib import Path
att = json.loads(Path("${ATTESTATION_OUT}").read_text(encoding="utf-8"))
digest = att["image_digest"]
path = Path("${ENV_FILE}")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("ORCH_SCRIPT_IMAGE_DIGEST="):
        lines.append(f"ORCH_SCRIPT_IMAGE_DIGEST={digest}")
    else:
        lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
export ORCH_PROVIDER_MODE="${ORCH_PROVIDER_MODE:-mock}"
export ORCH_TESTING="${ORCH_TESTING:-1}"
export ORCH_SCRIPT_RUNNER_ATTESTATION_HOST_PATH="${ATTESTATION_OUT}"
export CORS_ALLOWED_ORIGINS="${CORS_ALLOWED_ORIGINS:-http://127.0.0.1:8081,http://localhost:8081}"

compose_envfile_ok=0
if orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" --env-file "${ENV_FILE}" config >/dev/null 2>&1; then
  compose_envfile_ok=1
fi

compose_local() {
  if [[ "${compose_envfile_ok}" == "1" ]]; then
    orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" --env-file "${ENV_FILE}" "$@"
  else
    orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" "$@"
  fi
}

log "compose up (project=${PROJECT})"
compose_local up -d --build >"${STACK_DIR}/logs/compose-up.txt" 2>&1

wait_http "${API_BASE}/health/" "api" 90

log "seeding work item"
SEED_JSON="$(compose_local exec -T coordinator python /app/scripts/r4d_seed_work.py)"
WORK_ITEM_ID="$(python3 -c "import json,sys; print(json.load(sys.stdin)['work_item_id'])" <<<"${SEED_JSON}")"
[[ -n "${WORK_ITEM_ID}" ]] || fail "seed missing work_item_id"

python3 - <<PY
import json
from datetime import datetime, timezone
from pathlib import Path

payload = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "api_base": "${API_BASE}",
    "ops_summary_url": "${API_BASE}/ops/summary/",
    "ops_console_url": "http://127.0.0.1:8081/",
    "compose_project": "${PROJECT}",
    "env_file": "${ENV_FILE}",
    "work_item_id": "${WORK_ITEM_ID}",
    "runtime": "${RUNTIME}",
    "provider_mode": "${ORCH_PROVIDER_MODE:-mock}",
}
path = Path("${MANIFEST}")
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
path.chmod(0o600)
print(json.dumps(payload, indent=2))
PY

log "stack ready — run: python3 scripts/orchestrator_live_acceptance.py"
