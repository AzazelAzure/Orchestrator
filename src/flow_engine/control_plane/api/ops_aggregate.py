"""Helpers for authenticated ops summary aggregation."""

from __future__ import annotations

from typing import Any

from rest_framework.request import Request

from flow_engine.control_plane.api.authentication import OrchestratorUser
from flow_engine.control_plane.api.views_helpers import build_context, get_client
from flow_engine.coordinator.commands import RuntimeCommand


def _authenticated_read(
    request: Request,
    command_type: str,
    *,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user: OrchestratorUser = request.user  # type: ignore[assignment]
    command = RuntimeCommand(
        command_type=command_type,
        target_id=None,
        payload=payload or {},
        context=build_context(request, command_type=command_type),
    )
    return get_client().accept(
        command,
        principal_token=getattr(user, "raw_token", None) or None,
    )


def fetch_dashboard_payload(request: Request) -> dict[str, Any]:
    try:
        envelope = _authenticated_read(request, "ops.dashboard_read")
        if envelope.get("status") == "applied":
            return envelope.get("result") or {}
        if envelope.get("status") == "rejected":
            return {"error": envelope.get("error"), "error_code": envelope.get("error_code")}
        return envelope
    except Exception as exc:
        return {"error": str(exc)}


def fetch_schedule_status(request: Request) -> dict[str, Any] | None:
    try:
        envelope = _authenticated_read(request, "schedule.status")
        if envelope.get("status") == "applied":
            return envelope.get("result")
        if envelope.get("status") == "rejected":
            return None
        return envelope
    except Exception:
        return None
