"""Helpers for unauthenticated ops summary aggregation."""

from __future__ import annotations

import os
from typing import Any

from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.coordinator.commands import CommandContext, RuntimeCommand
from flow_engine.domain.states import PrincipalRole, Surface


def _ops_client() -> CoordinatorClient:
    from flow_engine.control_plane.api.views_helpers import get_client

    return get_client()


def _founder_context() -> tuple[CommandContext, str | None]:
    token = (os.environ.get("ORCH_TOKEN_FOUNDER") or "").strip()
    if not token:
        return (
            CommandContext(
                principal_id="ops-summary",
                role=PrincipalRole.SYSTEM,
                surface=Surface.REST,
            ),
            None,
        )
    client = _ops_client()
    envelope = client.accept(
        RuntimeCommand(
            command_type="control_plane.resolve_token",
            target_id=None,
            payload={"raw_token": token},
            context=CommandContext(
                principal_id="auth-resolver",
                role=PrincipalRole.SYSTEM,
                surface=Surface.REST,
            ),
        ),
    )
    principal = (envelope.get("result") or {}).get("principal") or {}
    return (
        CommandContext(
            principal_id=principal.get("principal_id", "ops-summary"),
            role=PrincipalRole(principal.get("role", "founder")),
            surface=Surface.REST,
        ),
        token,
    )


def _system_read(command_type: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    client = _ops_client()
    ctx, token = _founder_context()
    command = RuntimeCommand(
        command_type=command_type,
        target_id=None,
        payload=payload or {},
        context=ctx,
    )
    return client.accept(command, principal_token=token)


def fetch_dashboard_payload() -> dict[str, Any]:
    try:
        envelope = _system_read("ops.dashboard_read")
        if envelope.get("status") == "applied":
            return envelope.get("result") or {}
        if envelope.get("status") == "rejected":
            return {"error": envelope.get("error"), "error_code": envelope.get("error_code")}
        return envelope
    except Exception as exc:
        return {"error": str(exc)}


def fetch_schedule_status() -> dict[str, Any] | None:
    try:
        envelope = _system_read("schedule.status")
        if envelope.get("status") == "applied":
            return envelope.get("result")
        return envelope
    except Exception:
        return None
