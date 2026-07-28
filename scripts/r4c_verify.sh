#!/usr/bin/env bash
# R4C local verification — run from Orchestrator repo root.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi
export ORCH_TESTING="${ORCH_TESTING:-1}"
python -m pip install -e '.[control-plane,dev]'
python scripts/write_testing_attestation.py
python agentic/generate_catalogs.py
python agentic/validate_catalogs.py
ruff check src tests
pytest -q --cache-clear tests/unit/test_r4c_*.py tests/unit/test_r4b_*.py tests/unit/test_r4_*.py
pytest -q --cache-clear
if command -v docker >/dev/null 2>&1; then
  set -a
  if [[ -f "$ROOT/.env" ]]; then
    # shellcheck disable=SC1091
    source "$ROOT/.env"
  else
    # shellcheck disable=SC1091
    source "$ROOT/.env.example"
  fi
  set +a
  docker compose config
else
  echo "DOCKER_UNAVAILABLE"
fi
if command -v git >/dev/null 2>&1; then
  git diff --check || true
fi
echo "R4C verification finished"
