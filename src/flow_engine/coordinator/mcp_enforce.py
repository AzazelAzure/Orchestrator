"""Coordinator-side MCP identity enforcement (R4B independent-review).

Payload audit fields are never authoritative. Authenticated transport must
preserve MCP service principal, lane, tool name, and immutable snapshot digest
on CommandContext; the coordinator independently verifies them before dispatch.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.domain.errors import AuthzDeniedError, StaleAssetError
from flow_engine.domain.states import Surface
from flow_engine.mcp_lanes.catalog import lane_id_from_principal_key, principal_key_for_lane
from flow_engine.mcp_lanes.handlers import delegation_command_for_tool, workflow_command_for_tool
from flow_engine.mcp_lanes.snapshots import verify_tool_in_snapshot

# Never trust these when present on the command payload.
MCP_PAYLOAD_AUDIT_FIELDS: tuple[str, ...] = (
    "mcp_lane_id",
    "mcp_service_principal_id",
    "mcp_service_principal_key",
    "initiating_principal_id",
    "mcp_snapshot_digest",
    "tool_snapshot_digest",
    "mcp_tool_name",
    "mcp_tool_snapshot_digest",
)


def strip_mcp_payload_audit_fields(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    for key in MCP_PAYLOAD_AUDIT_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def extract_mcp_context_claims(ctx_data: dict[str, Any] | None) -> dict[str, str | None]:
    """Read MCP identity claims from transport context (to be verified)."""
    data = ctx_data or {}
    return {
        "mcp_service_principal_id": _optional_str(data.get("mcp_service_principal_id")),
        "mcp_lane_id": _optional_str(data.get("mcp_lane_id")),
        "mcp_tool_snapshot_digest": _optional_str(data.get("mcp_tool_snapshot_digest")),
        "mcp_tool_name": _optional_str(data.get("mcp_tool_name")),
    }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def mcp_identity_present(claims: dict[str, str | None]) -> bool:
    return any(claims.values())


def assert_mcp_coordinator_context(
    conn: sqlite3.Connection,
    command: Any,
) -> None:
    """Independently verify MCP identity before dispatch. Deny-wins."""
    from flow_engine.coordinator.commands import MCP_FORBIDDEN_COMMANDS, RuntimeCommand

    if not isinstance(command, RuntimeCommand):
        raise TypeError("RuntimeCommand required")

    ctx = command.context
    claims = {
        "mcp_service_principal_id": ctx.mcp_service_principal_id,
        "mcp_lane_id": ctx.mcp_lane_id,
        "mcp_tool_snapshot_digest": ctx.mcp_tool_snapshot_digest,
        "mcp_tool_name": ctx.mcp_tool_name,
    }
    has_claims = mcp_identity_present(claims)
    surface_is_mcp = ctx.surface == Surface.MCP
    if not surface_is_mcp and not has_claims:
        return

    # Preserve pre-R4B MCP-surface denials for founder-only verbs when no
    # lane identity was supplied (authorize_command raises UNSUPPORTED_SURFACE).
    if (
        surface_is_mcp
        and command.command_type in MCP_FORBIDDEN_COMMANDS
        and not has_claims
    ):
        return

    if not surface_is_mcp:
        raise AuthzDeniedError("MCP identity requires MCP surface")

    missing = [key for key, value in claims.items() if not value]
    if missing:
        raise AuthzDeniedError(
            "MCP coordinator context incomplete; dropped MCP identity denied "
            f"(missing: {', '.join(missing)})"
        )

    service_id = claims["mcp_service_principal_id"]
    lane_id = claims["mcp_lane_id"]
    tool_name = claims["mcp_tool_name"]
    snapshot_digest = claims["mcp_tool_snapshot_digest"]
    assert service_id and lane_id and tool_name and snapshot_digest

    if service_id == ctx.principal_id:
        raise AuthzDeniedError("initiating and MCP service principals must be distinct")

    service = _resolve_active_principal(conn, service_id)
    if service["kind"] != "mcp_service":
        raise AuthzDeniedError("MCP service principal kind mismatch")
    principal_key = service["principal_key"]
    bound_lane = lane_id_from_principal_key(principal_key)
    if not bound_lane:
        raise AuthzDeniedError(
            f"MCP service principal {principal_key} is not lane-scoped"
        )
    if bound_lane != lane_id:
        raise AuthzDeniedError(
            f"cross-lane MCP context denied: service {principal_key} "
            f"cannot serve lane {lane_id}"
        )
    if principal_key_for_lane(lane_id) != principal_key:
        raise AuthzDeniedError("service↔lane binding mismatch")

    initiating = _resolve_active_principal(conn, ctx.principal_id)
    if initiating["kind"] == "mcp_service":
        raise AuthzDeniedError(
            "MCP service principal cannot act as initiating principal"
        )

    snapshot = verify_tool_in_snapshot(
        lane_id=lane_id,
        tool_name=tool_name,
        expected_snapshot_digest=snapshot_digest,
    )

    if snapshot["snapshot_digest"] != snapshot_digest:
        raise StaleAssetError("MCP tool snapshot digest mismatch")

    mapped = workflow_command_for_tool(tool_name) or delegation_command_for_tool(
        tool_name, command.payload
    )
    if mapped != command.command_type:
        raise AuthzDeniedError(
            f"tool {tool_name} does not authorize command {command.command_type}"
        )


def _resolve_active_principal(
    conn: sqlite3.Connection, principal_id: str
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, principal_key, kind, capabilities_json
        FROM control_plane_principals
        WHERE id = ? AND status = 'active'
        """,
        (principal_id,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT id, principal_key, kind, capabilities_json
            FROM control_plane_principals
            WHERE principal_key = ? AND status = 'active'
            """,
            (principal_id,),
        ).fetchone()
    if row is None:
        raise AuthzDeniedError(f"unknown or inactive principal: {principal_id}")
    return {
        "principal_id": row["id"],
        "principal_key": row["principal_key"],
        "kind": row["kind"],
        "capabilities": tuple(json.loads(row["capabilities_json"] or "[]")),
    }
