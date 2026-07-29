#!/usr/bin/env bash
# Generate VPS Compose secrets into an ignored .env.vps file.
# Never prints secret values. Fail closed if destination is tracked.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${1:-$ROOT/.env.vps}"

if [[ -d "$ROOT/.git" ]] && command -v git >/dev/null 2>&1; then
  if git -C "$ROOT" ls-files --error-unmatch "$ENV_FILE" >/dev/null 2>&1; then
    echo "ERROR: refusing to write secrets into a tracked path: $ENV_FILE" >&2
    exit 1
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
  echo "# Generated VPS secrets — do not commit"
  echo "REDIS_PASSWORD=$(_rand)"
  echo "DJANGO_SECRET_KEY=$(_rand)"
  echo "DJANGO_ALLOWED_HOSTS=api.thedirectorate.app,127.0.0.1,localhost"
  echo "CORS_ALLOWED_ORIGINS=https://www.thedirectorate.app"
  echo "CSRF_TRUSTED_ORIGINS=https://www.thedirectorate.app,https://api.thedirectorate.app"
  echo "COORDINATOR_URL=http://coordinator:9001"
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
  echo "ORCH_SCRIPT_IMAGE_DIGEST=sha256:0000000000000000000000000000000000000000000000000000000000000000"
  echo "VITE_API_BASE_URL=https://api.thedirectorate.app"
} >"$ENV_FILE"
chmod 600 "$ENV_FILE"

echo "Wrote $ENV_FILE (secrets not printed)"
