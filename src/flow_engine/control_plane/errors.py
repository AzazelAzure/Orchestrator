"""Map interface-contract error codes to HTTP status."""

from __future__ import annotations

ERROR_HTTP_STATUS = {
    "AUTH_REQUIRED": 401,
    "AUTHZ_DENIED": 403,
    "UNSUPPORTED_SURFACE": 403,
    "NOT_FOUND": 404,
    "VALIDATION_FAILED": 400,
    "CONFLICT_CAS": 409,
    "IDEMPOTENCY_REPLAY": 409,
    "GATE_OPEN": 409,
    "BUDGET_EXHAUSTED": 429,
    "OUTCOME_UNKNOWN": 409,
    "STALE_ASSET": 409,
}


def http_status_for_envelope(envelope: dict) -> int:
    if envelope.get("from_cache"):
        return 200
    if envelope.get("status") == "rejected":
        code = envelope.get("error_code", "FLOW_ERROR")
        return ERROR_HTTP_STATUS.get(code, 409)
    return 202
