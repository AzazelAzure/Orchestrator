#!/usr/bin/env bash
# Generate ephemeral local secrets for R4D Compose active-test into an ignored path.
# Never prints secret values. Never commits. Fail closed if destination is tracked.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

RUN_ID="${ORCH_R4D_RUN_ID:-r4d-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
RUN_DIR="${ORCH_R4D_RUN_DIR:-$ROOT/.tmp/r4d/${RUN_ID}}"
ENV_FILE="${ORCH_R4D_ENV_FILE:-$RUN_DIR/env}"

mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"

if [[ -d "$ROOT/.git" ]] && command -v git >/dev/null 2>&1; then
  if git -C "$ROOT" check-ignore -q "$ENV_FILE" 2>/dev/null; then
    :
  else
    # .tmp/ is gitignored; still refuse if somehow tracked.
    if git -C "$ROOT" ls-files --error-unmatch "$ENV_FILE" >/dev/null 2>&1; then
      echo "ERROR: refusing to write secrets into a tracked path: $ENV_FILE" >&2
      exit 1
    fi
  fi
fi

_rand() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
}

umask 077
{
  echo "# Ephemeral R4D active-test secrets — do not commit"
  echo "# run_id=${RUN_ID}"
  echo "REDIS_PASSWORD=$(_rand)"
  echo "DJANGO_SECRET_KEY=$(_rand)"
  echo "DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1"
  echo "ORCH_API_SERVICE_TOKEN=$(_rand)"
  echo "ORCH_WORKER_SERVICE_TOKEN=$(_rand)"
  echo "ORCH_TOKEN_FOUNDER=$(_rand)"
  echo "ORCH_TOKEN_SCHEDULER=$(_rand)"
  echo "ORCH_TOKEN_MCP=$(_rand)"
  echo "ORCH_TOKEN_MCP_CONTEXT_ASSETS=$(_rand)"
  echo "ORCH_TOKEN_MCP_WORKFLOW_CONTROL=$(_rand)"
  echo "ORCH_TOKEN_MCP_DELEGATION_COORDINATION=$(_rand)"
  echo "ORCH_TOKEN_MCP_EVIDENCE_GOVERNANCE=$(_rand)"
  echo "ORCH_TOKEN_MCP_MAINTENANCE=$(_rand)"
  echo "ORCH_TOKEN_MCP_SKILLS_SCRIPTS=$(_rand)"
  echo "ORCH_TOKEN_WORKER=$(_rand)"
  echo "ORCH_TOKEN_WORKER_CODEX=$(_rand)"
  echo "ORCH_TOKEN_WORKER_CURSOR=$(_rand)"
  echo "ORCH_TOKEN_WORKER_CLAUDE=$(_rand)"
  echo "ORCH_HOST_RUNNER_TOKEN_CODEX=$(_rand)"
  echo "ORCH_HOST_RUNNER_TOKEN_CURSOR=$(_rand)"
  echo "ORCH_HOST_RUNNER_TOKEN_CLAUDE=$(_rand)"
  echo "ORCH_TOKEN_PROVIDER_INVOCATION=$(_rand)"
  echo "ORCH_SCRIPT_SPOOL_HMAC_KEY=$(_rand)"
  echo "ORCH_ATTESTATION_HMAC_KEY=$(_rand)"
  echo "ORCH_PROVIDER_MODE=mock"
  echo "ORCH_R4D_SLOW_MOCK=15"
  # Placeholder until attestation build fills the real digest.
  echo "ORCH_SCRIPT_IMAGE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000"
} >"$ENV_FILE"
chmod 600 "$ENV_FILE"

# Emit only paths/ids — never secret material.
echo "ORCH_R4D_RUN_ID=${RUN_ID}"
echo "ORCH_R4D_RUN_DIR=${RUN_DIR}"
echo "ORCH_R4D_ENV_FILE=${ENV_FILE}"
