#!/usr/bin/env bash
# R4A local verification — run from Orchestrator repo root with network for pip.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# Prefer project venv when present.
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
fi
export ORCH_TESTING="${ORCH_TESTING:-1}"
python -m pip install -e '.[control-plane,dev]'
python agentic/validate_catalogs.py
ruff check src tests
pytest -q --cache-clear
if command -v docker >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  set -a
  # Prefer .env when present; otherwise load example placeholders for config validation only.
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
echo "R4A verification finished"
