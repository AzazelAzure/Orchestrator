"""Department capability profiles (Admin/Ops, QA, Tech) from locked loadouts.

Profiles narrow lane access to catalog intersections. They never add tools or
authority beyond the locked MCP lane catalog and loadout mcp_lane_refs.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from flow_engine.domain.errors import AuthzDeniedError, NotFoundError, StaleAssetError
from flow_engine.mcp_lanes.catalog import LANE_IDS, load_lanes_by_id, principal_key_for_lane
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot

DEPARTMENTS: tuple[str, ...] = ("admin-ops", "qa", "tech")


def _agentic_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "agentic"
        if (candidate / "catalogs" / "loadouts.json").is_file():
            return candidate
    raise StaleAssetError("loadout catalog not discoverable")


@lru_cache(maxsize=1)
def _load_loadouts() -> list[dict[str, Any]]:
    path = _agentic_root() / "catalogs" / "loadouts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["records"])


def _lane_id_from_ref(ref: str) -> str:
    prefix = "mcp.lane."
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref


def department_capability_profiles() -> dict[str, dict[str, Any]]:
    """Build Admin/Ops, QA, and Tech profiles from locked loadout lane refs."""
    lanes = load_lanes_by_id()
    profiles: dict[str, dict[str, Any]] = {}
    for department in DEPARTMENTS:
        loadouts = [item for item in _load_loadouts() if item.get("department") == department]
        if not loadouts:
            raise StaleAssetError(f"no loadouts for department {department}")
        lane_refs: set[str] = set()
        position_lanes: dict[str, list[str]] = {}
        for item in loadouts:
            refs = [str(r) for r in (item.get("mcp_lane_refs") or [])]
            position_lanes[str(item.get("position"))] = refs
            lane_refs.update(refs)
        allowed_lane_ids = sorted(
            {
                _lane_id_from_ref(ref)
                for ref in lane_refs
                if _lane_id_from_ref(ref) in LANE_IDS
            }
        )
        # Tools = union of tools for allowed lanes only (catalog-bound).
        tools_by_lane: dict[str, list[str]] = {}
        for lane_id in allowed_lane_ids:
            tools_by_lane[lane_id] = list(lanes[lane_id]["tools"])
        profiles[department] = {
            "department": department,
            "display_name": {
                "admin-ops": "Admin/Ops",
                "qa": "QA",
                "tech": "Tech",
            }[department],
            "loadout_ids": [item["loadout_id"] for item in loadouts],
            "allowed_lane_refs": sorted(lane_refs),
            "allowed_lane_ids": allowed_lane_ids,
            "position_lane_refs": position_lanes,
            "tools_by_lane": tools_by_lane,
            "authority_note": (
                "Profile narrows to locked catalog lane/tool membership only; "
                "does not multiply authority."
            ),
        }
    return profiles


def profile_for_department(department: str) -> dict[str, Any]:
    profiles = department_capability_profiles()
    try:
        return profiles[department]
    except KeyError as exc:
        raise NotFoundError(f"unknown department profile: {department}") from exc


def assert_department_may_use_lane(*, department: str | None, lane_id: str) -> None:
    """When a department is known, enforce profile lane membership."""
    if not department:
        return
    profile = profile_for_department(department)
    if lane_id not in profile["allowed_lane_ids"]:
        raise AuthzDeniedError(
            f"department {department} profile does not include lane {lane_id}"
        )


def assert_loadout_may_use_lane(*, loadout_id: str | None, lane_id: str) -> None:
    if not loadout_id:
        return
    expected_ref = principal_key_for_lane(lane_id)
    for item in _load_loadouts():
        if item.get("loadout_id") == loadout_id:
            refs = set(item.get("mcp_lane_refs") or [])
            if expected_ref not in refs and f"mcp.lane.{lane_id}" not in refs:
                raise AuthzDeniedError(
                    f"loadout {loadout_id} does not grant lane {lane_id}"
                )
            return
    raise NotFoundError(f"unknown loadout_id: {loadout_id}")


def profiles_with_snapshots() -> dict[str, Any]:
    """Profiles plus exact lane snapshots for allowed lanes only."""
    out: dict[str, Any] = {"departments": {}}
    for department, profile in department_capability_profiles().items():
        snapshots = {
            lane_id: lane_tool_snapshot(lane_id) for lane_id in profile["allowed_lane_ids"]
        }
        out["departments"][department] = {**profile, "lane_snapshots": snapshots}
    return out
