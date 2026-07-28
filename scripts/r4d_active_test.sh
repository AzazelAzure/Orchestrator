#!/usr/bin/env bash
# R4D local active-test proof harness (runtime-neutral: podman | docker).
# Generates ephemeral secrets, builds attestation from real image inspect,
# validates Compose, starts the full stack, exercises proofs, tears down.
# Never uses real provider credentials/calls. Does not close gates / commit / PR.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/container_runtime.sh"

ORCH_R4D_KEEP="${ORCH_R4D_KEEP:-0}"
ORCH_R4D_SKIP_BUILD="${ORCH_R4D_SKIP_BUILD:-0}"
COMPOSE_FILE="${ORCH_COMPOSE_FILE:-$ROOT/docker-compose.yml}"
PROJECT="${ORCH_COMPOSE_PROJECT:-orch-r4d}"
API_BASE="${ORCH_R4D_API_BASE:-http://127.0.0.1:8000}"

CLEANED=0
COMPOSE_UP_PID=""
RUN_DIR=""
ENV_FILE=""
RUNTIME=""

log() { printf '[r4d] %s\n' "$*"; }
fail() { printf '[r4d] ERROR: %s\n' "$*" >&2; exit 1; }

# Prefer exported env (podman-compose + docker compose). Optionally pass --env-file.
COMPOSE_ENVFILE_OK=""
compose_r4d() {
  if [[ "${COMPOSE_ENVFILE_OK}" == "1" ]]; then
    orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" --env-file "${ENV_FILE}" "$@"
  else
    orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" "$@"
  fi
}

cleanup() {
  local ec=$?
  if [[ "${CLEANED}" -eq 1 ]]; then
    return 0
  fi
  CLEANED=1
  log "cleanup trap (exit=${ec}, keep=${ORCH_R4D_KEEP})"
  if [[ -n "${COMPOSE_UP_PID}" ]] && kill -0 "${COMPOSE_UP_PID}" 2>/dev/null; then
    kill "${COMPOSE_UP_PID}" 2>/dev/null || true
    wait "${COMPOSE_UP_PID}" 2>/dev/null || true
  fi
  if [[ -n "${RUN_DIR}" ]]; then
    mkdir -p "${RUN_DIR}/logs" || true
    if [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
      set +e
      set -a
      # shellcheck disable=SC1090
      source "${ENV_FILE}"
      set +a
      compose_r4d ps >"${RUN_DIR}/logs/compose-ps-final.txt" 2>&1 || true
      if [[ "${RUNTIME}" == "podman" ]]; then
        compose_r4d --no-ansi logs >"${RUN_DIR}/logs/compose-logs.txt" 2>&1 || true
      else
        compose_r4d logs --no-color >"${RUN_DIR}/logs/compose-logs.txt" 2>&1 || true
      fi
      set -e
    fi
  fi
  if [[ "${ORCH_R4D_KEEP}" != "1" ]]; then
    set +e
    if [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "${ENV_FILE}"
      set +a
      compose_r4d down -v --remove-orphans \
        >"${RUN_DIR:-/tmp}/r4d-teardown.log" 2>&1 || true
    else
      orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" down -v --remove-orphans >/dev/null 2>&1 || true
    fi
    # Post-teardown zero-state capture (containers/volumes/networks for this project).
    if [[ -n "${RUN_DIR}" ]]; then
      mkdir -p "${RUN_DIR}/logs" || true
      zero_state="${RUN_DIR}/logs/teardown-zero-state.json"
      python3 - <<PY
import json
import os
import subprocess

project = "${PROJECT}"
runtime = "${RUNTIME}"
run_dir = "${RUN_DIR}"

def run_cmd(argv):
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)

containers = []
volumes = []
networks = []
if runtime == "podman":
    rc, out, _ = run_cmd([
        "podman", "ps", "-a",
        "--filter", f"label=io.podman.compose.project={project}",
        "--format", "{{.Names}}",
    ])
    containers = [ln for ln in out.splitlines() if ln.strip()]
    rc_v, out_v, _ = run_cmd(["podman", "volume", "ls", "--format", "{{.Name}}"])
    volumes = [ln for ln in out_v.splitlines() if ln.startswith(f"{project}_")]
    rc_n, out_n, _ = run_cmd(["podman", "network", "ls", "--format", "{{.Name}}"])
    networks = [ln for ln in out_n.splitlines() if ln.startswith(f"{project}_")]
else:
    rc, out, _ = run_cmd([
        "docker", "ps", "-a",
        "--filter", f"label=com.docker.compose.project={project}",
        "--format", "{{.Names}}",
    ])
    containers = [ln for ln in out.splitlines() if ln.strip()]
    rc_v, out_v, _ = run_cmd(["docker", "volume", "ls", "--format", "{{.Name}}"])
    volumes = [ln for ln in out_v.splitlines() if ln.startswith(f"{project}_")]
    rc_n, out_n, _ = run_cmd(["docker", "network", "ls", "--format", "{{.Name}}"])
    networks = [ln for ln in out_n.splitlines() if ln.startswith(f"{project}_")]

payload = {
    "project": project,
    "runtime": runtime,
    "containers": containers,
    "volumes": volumes,
    "networks": networks,
    "zero_containers": len(containers) == 0,
    "zero_volumes": len(volumes) == 0,
    "zero_networks": len(networks) == 0,
    "zero_state": len(containers) == 0 and len(volumes) == 0 and len(networks) == 0,
}
path = os.path.join(run_dir, "logs", "teardown-zero-state.json")
with open(path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2, sort_keys=True)
    fh.write("\n")
if not payload["zero_state"]:
    raise SystemExit(f"teardown zero-state failed: {payload}")
# Refresh evidence summary with teardown capture when present.
evidence_dir = os.path.join(run_dir, "evidence")
summary_path = os.path.join(evidence_dir, "summary.json")
if os.path.isfile(summary_path):
    with open(summary_path, encoding="utf-8") as fh:
        summary = json.load(fh)
    summary["teardown_zero_state"] = payload
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
PY
    fi
    set -e
  else
    log "ORCH_R4D_KEEP=1 — leaving stack running (project=${PROJECT})"
  fi
  # Never leave env secrets world-readable.
  if [[ -n "${ENV_FILE}" && -f "${ENV_FILE}" ]]; then
    chmod 600 "${ENV_FILE}" 2>/dev/null || true
  fi
  return "${ec}"
}
trap cleanup EXIT INT TERM HUP

RUNTIME="$(orch_detect_container_runtime)" || fail "container runtime unavailable"
log "container runtime: ${RUNTIME}"
log "compose: $(orch_compose_cmd)"

# --- ephemeral secrets ---
eval "$(bash "$ROOT/scripts/r4d_generate_ephemeral_env.sh")"
[[ -n "${ORCH_R4D_ENV_FILE:-}" && -f "${ORCH_R4D_ENV_FILE}" ]] || fail "ephemeral env missing"
ENV_FILE="${ORCH_R4D_ENV_FILE}"
RUN_DIR="${ORCH_R4D_RUN_DIR}"
EVIDENCE_DIR="${RUN_DIR}/evidence"
mkdir -p "${EVIDENCE_DIR}/steps" "${RUN_DIR}/logs" "${RUN_DIR}/attestations"
chmod 700 "${RUN_DIR}"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a

ATTESTATION_OUT="${RUN_DIR}/attestations/script-runner.attestation.json"
export ORCH_SCRIPT_RUNNER_ATTESTATION_OUT="${ATTESTATION_OUT}"
export ORCH_SCRIPT_RUNNER_ATTESTATION_HOST_PATH="${ATTESTATION_OUT}"
export ORCH_ATTESTATION_HMAC_KEY

# --- build + attest script-runner ---
if [[ "${ORCH_R4D_SKIP_BUILD}" != "1" ]]; then
  log "building script-runner attestation via ${RUNTIME} inspect"
  bash "$ROOT/scripts/build_script_runner_attestation.sh" \
    | tee "${RUN_DIR}/logs/attestation-build.txt"
else
  [[ -f "${ATTESTATION_OUT}" ]] || fail "ORCH_R4D_SKIP_BUILD=1 but attestation missing"
fi
[[ -f "${ATTESTATION_OUT}" ]] || fail "attestation file missing after build"

# Update env file digest from attestation JSON (never invent; never print digest in CI logs optionally).
python3 - <<PY
import json
from pathlib import Path
att = json.loads(Path("${ATTESTATION_OUT}").read_text(encoding="utf-8"))
digest = att["image_digest"]
assert digest.startswith("sha256:") and len(digest) == 71, digest
path = Path("${ENV_FILE}")
lines = []
for line in path.read_text(encoding="utf-8").splitlines():
    if line.startswith("ORCH_SCRIPT_IMAGE_DIGEST="):
        lines.append(f"ORCH_SCRIPT_IMAGE_DIGEST={digest}")
    else:
        lines.append(line)
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"updated env digest from attestation source={att.get('source')}")
PY

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
export ORCH_SCRIPT_RUNNER_ATTESTATION_HOST_PATH="${ATTESTATION_OUT}"

# Detect whether this compose CLI accepts --env-file (docker compose yes; some podman-compose too).
COMPOSE_ENVFILE_OK=0
if orch_compose -f "${COMPOSE_FILE}" -p "${PROJECT}" --env-file "${ENV_FILE}" config >/dev/null 2>&1; then
  COMPOSE_ENVFILE_OK=1
  log "compose --env-file supported"
else
  log "compose --env-file unsupported; using exported environment only"
fi

# --- compose config validation ---
log "validating compose config"
compose_r4d config >"${RUN_DIR}/logs/compose-config.yml"
chmod 600 "${RUN_DIR}/logs/compose-config.yml" 2>/dev/null || true
log "compose config ok (mode 0600)"

# --- bring up full stack ---
log "compose up --build (project=${PROJECT})"
if [[ "${RUNTIME}" == "podman" ]]; then
  # Rootless Podman may not schedule OCI healthchecks itself.  Keep Compose's
  # health-gated dependency graph authoritative while driving each declared
  # check in this exact Compose project as its container appears.  Failed checks
  # remain failed; the loop never marks or overrides container health.
  compose_r4d up -d --build >"${RUN_DIR}/logs/compose-up.txt" 2>&1 &
  COMPOSE_UP_PID=$!
  health_log="${RUN_DIR}/logs/podman-health-kick.txt"
  : >"${health_log}"
  deadline=$((SECONDS + 240))
  while kill -0 "${COMPOSE_UP_PID}" 2>/dev/null; do
    while IFS= read -r container; do
      [[ -n "${container}" ]] || continue
      state="$(
        podman inspect --format '{{.State.Status}}' "${container}" 2>/dev/null || true
      )"
      [[ -n "${state}" ]] || continue
      health="$(
        podman inspect \
          --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' \
          "${container}" 2>/dev/null || true
      )"
      if [[ "${state}" == "exited" ]]; then
        exit_code="$(podman inspect --format '{{.State.ExitCode}}' "${container}")"
        if [[ "${container}" == "${PROJECT}_script-spool-init_1" && "${exit_code}" == "0" ]]; then
          printf '%s %s completed-successfully\n' \
            "$(date -u +%FT%TZ)" "${container}" >>"${health_log}"
        fi
        continue
      fi
      [[ -n "${health}" ]] || continue
      if [[ "${health}" == "unhealthy" ]]; then
        fail "Podman healthcheck became unhealthy: ${container}"
      fi
      [[ "${state}" == "running" && "${health}" == "starting" ]] || continue
      if podman healthcheck run "${container}" >>"${health_log}" 2>&1; then
        printf '%s %s healthy\n' \
          "$(date -u +%FT%TZ)" "${container}" >>"${health_log}"
      else
        rc=$?
        printf '%s %s pending-or-failed rc=%s\n' \
          "$(date -u +%FT%TZ)" "${container}" "${rc}" >>"${health_log}"
      fi
    done < <(
      podman ps -a \
        --filter "label=io.podman.compose.project=${PROJECT}" \
        --format '{{.Names}}'
    )
    if (( SECONDS >= deadline )); then
      kill "${COMPOSE_UP_PID}" 2>/dev/null || true
      wait "${COMPOSE_UP_PID}" 2>/dev/null || true
      COMPOSE_UP_PID=""
      fail "Podman Compose up timed out while preserving health gates"
    fi
    sleep 2
  done
  if ! wait "${COMPOSE_UP_PID}"; then
    COMPOSE_UP_PID=""
    fail "Podman Compose up failed; see compose-up and health-kick logs"
  fi
  COMPOSE_UP_PID=""
else
  compose_r4d up -d --build >"${RUN_DIR}/logs/compose-up.txt" 2>&1
fi

wait_http() {
  local url="$1" name="$2" attempts="${3:-60}"
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

wait_http "${API_BASE}/health/" "api" 90

# Wait for MCP lane containers / worker via compose ps.
log "waiting for compose services"
for i in $(seq 1 60); do
  if compose_r4d ps >"${RUN_DIR}/logs/compose-ps.txt" 2>&1; then
    if grep -Eqi 'unhealthy|Exit|Error' "${RUN_DIR}/logs/compose-ps.txt"; then
      sleep 2
      continue
    fi
    break
  fi
  sleep 2
done

# --- seed work item via sole-writer coordinator ---
log "seeding work item via coordinator"
SEED_JSON="$(
  compose_r4d exec -T coordinator python /app/scripts/r4d_seed_work.py
)"
echo "${SEED_JSON}" >"${EVIDENCE_DIR}/steps/00_seed.json"
WORK_ITEM_ID="$(python3 -c "import json,sys; print(json.load(sys.stdin)['work_item_id'])" <<<"${SEED_JSON}")"
[[ -n "${WORK_ITEM_ID}" ]] || fail "seed did not return work_item_id"

# --- primary exercises ---
log "running API/MCP/script/schedule/loadout exercises"
export ORCH_R4D_ENV_FILE="${ENV_FILE}"
export ORCH_R4D_EVIDENCE_DIR="${EVIDENCE_DIR}"
export ORCH_R4D_API_BASE="${API_BASE}"
export ORCH_R4D_WORK_ITEM_ID="${WORK_ITEM_ID}"
export ORCH_COMPOSE_FILE="${COMPOSE_FILE}"
export ORCH_COMPOSE_PROJECT="${PROJECT}"
python3 "$ROOT/scripts/r4d_exercise.py" | tee "${RUN_DIR}/logs/exercise.txt"
MOCK_RUN_ID="$(
  python3 -c "import json; print(json.load(open('${EVIDENCE_DIR}/steps/02_api_worker_mock.json'))['run_id'])"
)"

# --- Redis redelivery / worker loss ---
log "worker-loss + redis redelivery probe"
SEED2="$(
  compose_r4d exec -T coordinator python /app/scripts/r4d_seed_work.py
)"
WORK2="$(python3 -c "import json,sys; print(json.load(sys.stdin)['work_item_id'])" <<<"${SEED2}")"
export ORCH_R4D_WORK_ITEM_ID="${WORK2}"
export ORCH_R4D_STOP_WORKER_ON_AT_LOSS=1
REDELIVERY_JSON="$(
  python3 "$ROOT/scripts/r4d_exercise.py" redelivery-start
)"
REDELIVERY_RUN_ID="$(python3 -c "import json,sys; print(json.load(sys.stdin)['run_id'])" <<<"${REDELIVERY_JSON}")"
REDELIVERY_JOB_ID="$(python3 -c "import json,sys; print(json.load(sys.stdin)['delivery_job_id'])" <<<"${REDELIVERY_JSON}")"
unset ORCH_R4D_STOP_WORKER_ON_AT_LOSS

compose_r4d start worker >"${RUN_DIR}/logs/worker-start.txt" 2>&1
sleep 5
wait_http "${API_BASE}/health/" "api-after-worker-loss" 30

export ORCH_R4D_REDELIVERY_RUN_ID="${REDELIVERY_RUN_ID}"
export ORCH_R4D_REDELIVERY_JOB_ID="${REDELIVERY_JOB_ID}"
python3 "$ROOT/scripts/r4d_exercise.py" redelivery-finalize | tee "${RUN_DIR}/logs/redelivery-finalize.txt"

# --- coordinator restart + SQLite integrity ---
log "coordinator restart + sqlite integrity"
export ORCH_R4D_CONTINUITY_WORK_IDS="${WORK_ITEM_ID},${WORK2}"
export ORCH_R4D_CONTINUITY_RUN_IDS="${MOCK_RUN_ID},${REDELIVERY_RUN_ID}"
python3 "$ROOT/scripts/r4d_exercise.py" restart-pre | tee "${RUN_DIR}/logs/restart-pre.txt"

compose_r4d restart coordinator >"${RUN_DIR}/logs/coordinator-restart.txt" 2>&1
sleep 5
wait_http "${API_BASE}/health/" "api-after-coordinator-restart" 60

RECOVER_JSON="$(
  python3 - <<PY
import json, os, urllib.request
env = {}
for line in open(os.environ["ORCH_R4D_ENV_FILE"], encoding="utf-8"):
    line=line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k,v=line.split("=",1); env[k]=v
req = urllib.request.Request(
    os.environ["ORCH_R4D_API_BASE"].rstrip("/") + "/api/v1/runtime/recover",
    data=b"{}",
    headers={
        "Authorization": f"Bearer {env['ORCH_TOKEN_FOUNDER']}",
        "Content-Type": "application/json",
    },
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    body = json.load(resp)
print(json.dumps({"http_status": resp.status, "status": body.get("status")}, sort_keys=True))
PY
)"
export ORCH_R4D_RECOVER_HTTP="$(python3 -c "import json,sys; print(json.load(sys.stdin)['http_status'])" <<<"${RECOVER_JSON}")"
export ORCH_R4D_RECOVER_STATUS="$(python3 -c "import json,sys; print(json.load(sys.stdin).get('status') or '')" <<<"${RECOVER_JSON}")"
python3 "$ROOT/scripts/r4d_exercise.py" restart-post | tee "${RUN_DIR}/logs/restart-post.txt"

INTEGRITY_JSON="$(
  compose_r4d exec -T coordinator python -c 'import json,os,sqlite3; p=os.environ.get("FLOW_DB_PATH","/data/state.db"); c=sqlite3.connect(p); row=c.execute("PRAGMA integrity_check").fetchone(); ver=c.execute("PRAGMA user_version").fetchone(); print(json.dumps({"integrity_check": row[0] if row else None, "user_version": ver[0] if ver else None})); c.close()'
)"
printf '%s\n' "${INTEGRITY_JSON}" >"${EVIDENCE_DIR}/steps/10_sqlite_integrity.json"
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("integrity_check")=="ok", d' <<<"${INTEGRITY_JSON}"
log "sqlite integrity ok"

# --- final evidence summary ---
python3 - <<PY
import json
from pathlib import Path
evidence = Path("${EVIDENCE_DIR}")
steps = sorted(p.name for p in (evidence / "steps").glob("*.json"))
summary = {
    "ok": True,
    "runtime": "${RUNTIME}",
    "project": "${PROJECT}",
    "run_dir": "${RUN_DIR}",
    "steps": steps,
    "gates_closed": False,
    "note": "R4D active-test evidence only; gates remain open",
}
(evidence / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True)+"\n", encoding="utf-8")
print(json.dumps(summary, sort_keys=True))
PY

log "R4D active-test finished OK"
log "evidence: ${EVIDENCE_DIR}"
# cleanup trap performs deterministic teardown unless ORCH_R4D_KEEP=1
