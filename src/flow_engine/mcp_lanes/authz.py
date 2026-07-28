"""Server-side MCP lane capability checks (deny-wins)."""

from __future__ import annotations

from typing import Any

from flow_engine.domain.errors import AuthzDeniedError, UnsupportedSurfaceError
from flow_engine.mcp_lanes.forbidden import FORBIDDEN_OPERATIONS, FORBIDDEN_TOOL_NAMES
from flow_engine.mcp_lanes.profiles import (
    assert_department_may_use_lane,
    assert_loadout_may_use_lane,
)
from flow_engine.mcp_lanes.snapshots import (
    assert_lane_service_matches,
    verify_tool_in_snapshot,
)


def assert_not_forbidden_tool(tool_name: str) -> None:
    if tool_name in FORBIDDEN_TOOL_NAMES or tool_name in FORBIDDEN_OPERATIONS:
        raise UnsupportedSurfaceError(
            f"tool {tool_name} is excluded from all MCP lanes"
        )


def assert_mcp_invoke_allowed(
    *,
    lane_id: str,
    tool_name: str,
    service_principal_key: str,
    initiating_principal_kind: str,
    initiating_principal_id: str,
    service_principal_id: str,
    expected_snapshot_digest: str | None = None,
    loadout_id: str | None = None,
    department: str | None = None,
) -> dict[str, Any]:
    """Fail closed on cross-lane, forbidden, snapshot, and profile mismatches."""
    if not initiating_principal_id or not service_principal_id:
        raise AuthzDeniedError("both initiating and MCP service principals are required")
    if initiating_principal_id == service_principal_id:
        raise AuthzDeniedError("initiating and MCP service principals must be distinct")
    if initiating_principal_kind == "mcp_service":
        raise AuthzDeniedError(
            "MCP service principal cannot act as initiating principal"
        )

    assert_not_forbidden_tool(tool_name)
    assert_lane_service_matches(
        lane_id=lane_id, service_principal_key=service_principal_key
    )
    snapshot = verify_tool_in_snapshot(
        lane_id=lane_id,
        tool_name=tool_name,
        expected_snapshot_digest=expected_snapshot_digest,
    )
    assert_loadout_may_use_lane(loadout_id=loadout_id, lane_id=lane_id)
    assert_department_may_use_lane(department=department, lane_id=lane_id)
    return snapshot
