#!/usr/bin/env bash
# R4D local verification — static checks plus optional Compose active-test.
# Usage:
#   bash scripts/r4d_verify.sh           # static only
#   bash scripts/r4d_verify.sh --active  # static + full active-test (podman|docker)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck disable=SC1091
source "$ROOT/scripts/lib/container_runtime.sh"

ACTIVE=0
for arg in "$@"; do
  case "${arg}" in
    --active) ACTIVE=1 ;;
    *) echo "unknown arg: ${arg}" >&2; exit 2 ;;
  esac
done

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi

export ORCH_TESTING="${ORCH_TESTING:-1}"
python -m pip install -e '.[control-plane,dev]'
python scripts/write_testing_attestation.py
python agentic/generate_catalogs.py
python agentic/validate_catalogs.py
ruff check src tests scripts/r4d_exercise.py scripts/r4d_seed_work.py scripts/r4d_state_snapshot.py
pytest -q --cache-clear tests/unit/test_r4d_*.py tests/unit/test_r4c_*.py tests/unit/test_r4b_*.py tests/unit/test_r4_*.py
pytest -q --cache-clear

if command -v git >/dev/null 2>&1; then
  git diff --check || true
fi

if RUNTIME="$(orch_detect_container_runtime 2>/dev/null)"; then
  echo "CONTAINER_RUNTIME=${RUNTIME}"
  # Static compose config with example placeholders (not a secret-bearing run).
  set -a
  if [[ -f "$ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/.env"
  else
    # shellcheck disable=SC1091
    source "$ROOT/.env.example"
  fi
  set +a
  orch_compose -f "$ROOT/docker-compose.yml" config >/dev/null
  echo "COMPOSE_CONFIG_OK"
else
  echo "CONTAINER_RUNTIME_UNAVAILABLE"
fi

if [[ "${ACTIVE}" -eq 1 ]]; then
  bash "$ROOT/scripts/r4d_active_test.sh"
fi

echo "R4D verification finished (gates not closed)"
