"""Locked MCP lane catalog loader (exact R1/R3 records)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from flow_engine.domain.errors import NotFoundError, StaleAssetError, ValidationFailedError

LANE_IDS: tuple[str, ...] = (
    "context-assets",
    "workflow-control",
    "delegation-coordination",
    "evidence-governance",
    "maintenance",
)

_LANE_ASSET_PREFIX = "mcp.lane."


def _agentic_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "agentic"
        if (candidate / "catalogs" / "mcp_lanes.json").is_file():
            return candidate
    raise StaleAssetError("MCP lane catalog not discoverable")


def principal_key_for_lane(lane_id: str) -> str:
    if lane_id not in LANE_IDS:
        raise ValidationFailedError(f"unknown MCP lane_id: {lane_id}")
    return f"{_LANE_ASSET_PREFIX}{lane_id}"


def lane_id_from_principal_key(principal_key: str) -> str | None:
    if not principal_key.startswith(_LANE_ASSET_PREFIX):
        return None
    lane_id = principal_key[len(_LANE_ASSET_PREFIX) :]
    return lane_id if lane_id in LANE_IDS else None


@lru_cache(maxsize=1)
def load_lane_catalog() -> dict[str, Any]:
    path = _agentic_root() / "catalogs" / "mcp_lanes.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("kind") != "mcp_lane_catalog":
        raise StaleAssetError("invalid MCP lane catalog kind")
    if int(payload.get("count", 0)) != 5:
        raise StaleAssetError("MCP lane catalog must contain exactly five lanes")
    return payload


def load_lanes_by_id() -> dict[str, dict[str, Any]]:
    catalog = load_lane_catalog()
    lanes = {item["lane_id"]: item for item in catalog["records"]}
    missing = [lane_id for lane_id in LANE_IDS if lane_id not in lanes]
    if missing:
        raise StaleAssetError(f"MCP lane catalog missing lanes: {missing}")
    return lanes


def load_lane_by_id(lane_id: str) -> dict[str, Any]:
    lanes = load_lanes_by_id()
    try:
        return lanes[lane_id]
    except KeyError as exc:
        raise NotFoundError(f"unknown MCP lane: {lane_id}") from exc


def clear_catalog_cache() -> None:
    load_lane_catalog.cache_clear()
