"""Distinct API/worker service credentials for coordinator transport."""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from enum import Enum

from flow_engine.domain.errors import AuthRequiredError, AuthzDeniedError


class ServiceCallerKind(str, Enum):
    API = "api"
    WORKER = "worker"
    INTERNAL = "internal"  # in-process / test only


@dataclass(frozen=True)
class AuthenticatedServiceCaller:
    kind: ServiceCallerKind


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AuthRequiredError(f"{name} is required (fail closed)")
    return value


def api_service_credential() -> str:
    return _required_env("ORCH_API_SERVICE_TOKEN")


def worker_service_credential() -> str:
    return _required_env("ORCH_WORKER_SERVICE_TOKEN")


def optional_api_service_credential() -> str | None:
    value = os.environ.get("ORCH_API_SERVICE_TOKEN", "").strip()
    return value or None


def optional_worker_service_credential() -> str | None:
    value = os.environ.get("ORCH_WORKER_SERVICE_TOKEN", "").strip()
    return value or None


def authenticate_service_credential(
    *,
    credential: str | None,
    claimed_kind: str | None,
) -> AuthenticatedServiceCaller:
    """Authenticate coordinator callers. Rejects missing/mismatched credentials."""
    if not credential or not credential.strip():
        raise AuthRequiredError("service credential required")
    if claimed_kind not in {ServiceCallerKind.API.value, ServiceCallerKind.WORKER.value}:
        raise AuthzDeniedError("unknown service caller kind")

    api_token = optional_api_service_credential()
    worker_token = optional_worker_service_credential()
    if api_token is None or worker_token is None:
        raise AuthRequiredError(
            "ORCH_API_SERVICE_TOKEN and ORCH_WORKER_SERVICE_TOKEN must be configured"
        )
    if hmac.compare_digest(api_token, worker_token):
        raise AuthzDeniedError("API and worker service credentials must be distinct")

    if claimed_kind == ServiceCallerKind.API.value:
        if not hmac.compare_digest(credential.strip(), api_token):
            raise AuthzDeniedError("invalid API service credential")
        return AuthenticatedServiceCaller(kind=ServiceCallerKind.API)

    if not hmac.compare_digest(credential.strip(), worker_token):
        raise AuthzDeniedError("invalid worker service credential")
    return AuthenticatedServiceCaller(kind=ServiceCallerKind.WORKER)


def require_configured_service_credentials() -> None:
    """Fail closed at process start when HTTP coordinator mode is used."""
    api_token = optional_api_service_credential()
    worker_token = optional_worker_service_credential()
    if not api_token or not worker_token:
        raise RuntimeError(
            "ORCH_API_SERVICE_TOKEN and ORCH_WORKER_SERVICE_TOKEN are required"
        )
    if hmac.compare_digest(api_token, worker_token):
        raise RuntimeError("API and worker service credentials must be distinct")
