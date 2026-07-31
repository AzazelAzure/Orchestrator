#!/usr/bin/env bash
# Sourceable HTTP readiness helpers for local-stack scripts.
# Function-only: no side effects on source. Timeout returns nonzero (never exits).
#
# shellcheck shell=bash

fail() {
  printf '[local-stack] ERROR: %s\n' "$*" >&2
  exit 1
}

wait_http() {
  local url="$1" name="$2" attempts="${3:-90}"
  local i
  local sleep_s="${ORCH_HTTP_PROBE_SLEEP:-2}"
  for i in $(seq 1 "${attempts}"); do
    if python3 - <<PY
import urllib.request
try:
    urllib.request.urlopen("${url}", timeout=3)
except Exception:
    raise SystemExit(1)
PY
    then
      printf '[local-stack] %s\n' "healthy: ${name}"
      return 0
    fi
    sleep "${sleep_s}"
  done
  return 1
}
