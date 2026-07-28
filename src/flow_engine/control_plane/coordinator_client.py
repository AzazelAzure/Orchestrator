"""HTTP client for the sole-writer state coordinator."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from flow_engine.coordinator.commands import RuntimeCommand
from flow_engine.coordinator.coordinator import StateCoordinator
from flow_engine.coordinator.transport import command_to_dict
from flow_engine.domain.errors import FlowError
from flow_engine.persistence.connection import Kernel
from flow_engine.persistence.transactions import transaction


class CoordinatorClient:
    """Thin adapter: API/workers never write SQLite directly."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        coordinator: StateCoordinator | None = None,
        service_kind: str = "api",
        service_token: str | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get("COORDINATOR_URL", "")).rstrip("/")
        self._coordinator = coordinator
        self._service_kind = service_kind
        if service_token is not None:
            self._service_token = service_token
        elif service_kind == "worker":
            self._service_token = os.environ.get("ORCH_WORKER_SERVICE_TOKEN", "")
        else:
            self._service_token = os.environ.get("ORCH_API_SERVICE_TOKEN", "")

    @classmethod
    def from_inprocess(cls, kernel: Kernel) -> CoordinatorClient:
        return cls(coordinator=StateCoordinator(kernel.connection), service_kind="api")

    def accept(
        self,
        command: RuntimeCommand,
        *,
        principal_token: str | None = None,
    ) -> dict[str, Any]:
        if command.command_type == "runtime.worker_deliver" and self._coordinator is not None:
            from flow_engine.application.worker_delivery import accept_worker_deliver

            return accept_worker_deliver(self._coordinator, command)

        if command.command_type == "script.execute" and self._coordinator is not None:
            from flow_engine.application.script_delivery import accept_script_execute

            return accept_script_execute(self._coordinator, command)

        if self._coordinator is not None:
            with transaction(self._coordinator.connection):
                return self._coordinator.accept(command)
        if not self._base_url:
            raise RuntimeError("COORDINATOR_URL not configured and no in-process coordinator")
        url = f"{self._base_url}/v1/commands"
        body = json.dumps(command_to_dict(command)).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Orchestrator-Service-Kind": self._service_kind,
            "X-Orchestrator-Service-Token": self._service_token,
        }
        if principal_token:
            headers["X-Orchestrator-Principal-Token"] = principal_token
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
            except json.JSONDecodeError:
                raise FlowError(
                    "coordinator returned a non-JSON error response",
                    code="PERSISTENCE_UNAVAILABLE",
                ) from exc
            # The coordinator's HTTP status mirrors a typed rejected envelope.
            # Preserve that interface contract for network clients just as the
            # in-process client does; DRF maps the envelope and must not turn an
            # expected domain rejection into an HTML 500.
            if (
                isinstance(payload, dict)
                and payload.get("status") == "rejected"
                and payload.get("error_code")
            ):
                return payload
            code = payload.get("error_code", "FLOW_ERROR")
            raise FlowError(payload.get("error", str(exc)), code=code) from exc

    def health(self) -> dict[str, Any]:
        if self._coordinator is not None:
            version = self._coordinator.connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()[0]
            return {"status": "ok", "mode": "inprocess", "schema_version": version}
        url = f"{self._base_url}/v1/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
