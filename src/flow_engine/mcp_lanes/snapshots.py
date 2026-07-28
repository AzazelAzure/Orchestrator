"""Exact per-lane tool snapshots derived from the locked catalog."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from flow_engine.domain.errors import AuthzDeniedError, StaleAssetError, ValidationFailedError
from flow_engine.mcp_lanes.catalog import load_lane_by_id, principal_key_for_lane
from flow_engine.mcp_lanes.forbidden import FORBIDDEN_OPERATIONS, FORBIDDEN_TOOL_NAMES


def _digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def lane_tool_snapshot(lane_id: str) -> dict[str, Any]:
    """Return the exact immutable tool snapshot for a lane."""
    lane = load_lane_by_id(lane_id)
    tools = list(lane["tools"])
    if len(tools) != len(set(tools)):
        raise StaleAssetError(
            f"lane {lane_id} tool snapshot has duplicate tool names: {tools}"
        )
    forbidden = list(lane.get("forbidden_operations") or [])
    # Fail closed if catalog ever lists a forbidden tool name.
    overlap = set(tools) & FORBIDDEN_TOOL_NAMES
    if overlap:
        raise StaleAssetError(f"lane {lane_id} catalog lists forbidden tools: {sorted(overlap)}")
    missing_forbidden = FORBIDDEN_OPERATIONS - set(forbidden)
    # Catalog may use a subset naming; require core exclusions at minimum.
    core = {
        "waiver",
        "hitm_exception",
        "merge",
        "deploy",
        "publication",
        "credential_projection",
        "direct_database_access",
        "provider_cli_invocation",
    }
    if not core.issubset(set(forbidden)):
        raise StaleAssetError(f"lane {lane_id} missing core forbidden_operations")

    body = {
        "lane_id": lane_id,
        "asset_id": lane["asset_id"],
        "principal_key": principal_key_for_lane(lane_id),
        "tools": tools,
        "tool_count": len(tools),
        "forbidden_operations": forbidden,
        "catalog_content_sha256": lane["content_sha256"],
        "binds_stdio_compat_tools": bool(lane.get("binds_stdio_compat_tools")),
        "stdio_compat_tools": list(lane.get("stdio_compat_tools") or []),
    }
    digest = _digest(body)
    return {**body, "snapshot_digest": digest, "missing_forbidden_extra": sorted(missing_forbidden)}


def verify_tool_in_snapshot(
    *,
    lane_id: str,
    tool_name: str,
    expected_snapshot_digest: str | None = None,
) -> dict[str, Any]:
    """Enforce exact tool membership and optional client snapshot pin."""
    if tool_name in FORBIDDEN_TOOL_NAMES or tool_name in FORBIDDEN_OPERATIONS:
        raise AuthzDeniedError(f"tool {tool_name} is forbidden on all MCP lanes")
    snapshot = lane_tool_snapshot(lane_id)
    if expected_snapshot_digest and expected_snapshot_digest != snapshot["snapshot_digest"]:
        raise StaleAssetError(
            f"tool snapshot digest mismatch for lane {lane_id}: "
            f"expected {expected_snapshot_digest}, got {snapshot['snapshot_digest']}"
        )
    if tool_name not in snapshot["tools"]:
        raise AuthzDeniedError(
            f"tool {tool_name} is not in exact snapshot for lane {lane_id}"
        )
    return snapshot


def assert_lane_service_matches(
    *,
    lane_id: str,
    service_principal_key: str,
) -> None:
    expected = principal_key_for_lane(lane_id)
    if service_principal_key != expected:
        raise AuthzDeniedError(
            f"MCP service principal {service_principal_key} cannot serve lane {lane_id} "
            f"(expected {expected})"
        )


def parse_snapshot_digest(value: str | None) -> str | None:
    if value is None:
        return None
    digest = value.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValidationFailedError("expected_snapshot_digest must be 64-char sha256 hex")
    return digest
