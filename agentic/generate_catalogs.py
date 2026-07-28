#!/usr/bin/env python3
"""Deterministically generate R1 inert catalog JSON artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTIC = Path(__file__).resolve().parent
if str(AGENTIC) not in sys.path:
    sys.path.insert(0, str(AGENTIC))

from catalog_defs import (  # noqa: E402
    CATALOG_SCHEMA_VERSION,
    CATALOG_VERSION,
    loadout_defs,
    mcp_lane_defs,
    policy_contract_def,
    portable_asset_index_entries,
    script_defs,
)
from catalog_hash import attach_content_hash  # noqa: E402

OUT_DIR = ROOT / "agentic" / "catalogs"


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _wrap(kind: str, records: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    hashed = [attach_content_hash(record) for record in records]
    hashed.sort(key=lambda item: item.get("asset_id") or item.get("loadout_id") or "")
    catalog = {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_version": CATALOG_VERSION,
        "kind": kind,
        "lifecycle_state": "inert",
        "activation": "inert",
        "count": len(hashed),
        "records": hashed,
        **extra,
    }
    return attach_content_hash(catalog)


def build_all() -> dict[str, dict[str, Any]]:
    assets = _wrap("portable_asset_index", portable_asset_index_entries())
    lanes = _wrap(
        "mcp_lane_catalog",
        mcp_lane_defs(),
        stdio_compat_tools_bound_into="mcp.lane.context-assets",
    )
    loadouts = _wrap("loadout_catalog", loadout_defs(), expected_count=12)
    scripts = _wrap(
        "registered_script_catalog",
        script_defs(),
        expected_count=12,
        repository_scripts_executable=False,
    )
    policy = attach_content_hash(policy_contract_def())
    policy_doc = attach_content_hash(
        {
            "schema_version": CATALOG_SCHEMA_VERSION,
            "catalog_version": CATALOG_VERSION,
            "kind": "policy_contract",
            "lifecycle_state": "inert",
            "activation": "inert",
            "record": policy,
        }
    )
    return {
        "assets.json": assets,
        "mcp_lanes.json": lanes,
        "loadouts.json": loadouts,
        "scripts.json": scripts,
        "policy.json": policy_doc,
    }


def main() -> None:
    payloads = build_all()
    for name, payload in payloads.items():
        _dump(OUT_DIR / name, payload)
    print(f"wrote {len(payloads)} catalogs under {OUT_DIR.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
