#!/usr/bin/env python3
"""Validate R1 inert catalogs: structure, hashes, memberships, exclusions."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTIC = Path(__file__).resolve().parent
CATALOGS = ROOT / "agentic" / "catalogs"
if str(AGENTIC) not in sys.path:
    sys.path.insert(0, str(AGENTIC))

from catalog_defs import (  # noqa: E402
    DISPATCH_PIN_FIELDS,
    FORBIDDEN_MCP_AND_SCHEDULE_OPS,
    PRECEDENCE_LAYERS,
    STDIO_COMPAT_TOOLS,
)
from catalog_hash import content_sha256  # noqa: E402
from generate_catalogs import build_all  # noqa: E402

PRIVATE_PATH_RE = re.compile(
    r"(/home/[A-Za-z0-9._-]+|/Users/[A-Za-z0-9._-]+|\\\\Users\\\\|[A-Za-z]:\\\\Users\\\\)"
)
SECRET_HINT_RE = re.compile(
    r"(?i)(api[_-]?key|secret[_-]?key|begin\s+private\s+key|password\s*=\s*\S+|token\s*=\s*[A-Za-z0-9]{12,})"
)

REQUIRED_FILES = (
    "assets.json",
    "mcp_lanes.json",
    "loadouts.json",
    "scripts.json",
    "policy.json",
)

COMMON_ASSET_FIELDS = (
    "asset_id",
    "kind",
    "version",
    "content_sha256",
    "owner",
    "source",
    "compatibility",
    "sensitivity",
    "lifecycle_state",
    "provenance",
)


class CatalogValidationError(Exception):
    """Raised when catalog validation fails."""


def _load(name: str) -> dict[str, Any]:
    path = CATALOGS / name
    if not path.is_file():
        raise CatalogValidationError(f"missing catalog file: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _require_fields(record: dict[str, Any], fields: tuple[str, ...], *, where: str) -> None:
    missing = [field for field in fields if field not in record]
    if missing:
        raise CatalogValidationError(f"{where}: missing fields {missing}")


def _check_hash(record: dict[str, Any], *, where: str) -> None:
    expected = content_sha256(record)
    actual = record.get("content_sha256")
    if actual != expected:
        raise CatalogValidationError(f"{where}: content_sha256 mismatch")


def _scan_text_exclusions(payload: Any, *, where: str) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if PRIVATE_PATH_RE.search(text):
        raise CatalogValidationError(f"{where}: private absolute path detected")
    if SECRET_HINT_RE.search(text):
        raise CatalogValidationError(f"{where}: secret-like material detected")


def validate_catalogs(*, require_committed: bool = True) -> dict[str, Any]:
    """Validate committed catalogs (and optionally regenerate expectations)."""
    expected = build_all()
    report: dict[str, Any] = {"ok": True, "files": {}, "checks": []}

    if require_committed:
        for name in REQUIRED_FILES:
            committed = _load(name)
            if committed != expected[name]:
                raise CatalogValidationError(
                    f"{name}: committed catalog differs from deterministic generator output"
                )
            report["files"][name] = {"path": f"agentic/catalogs/{name}", "matched_generator": True}
    else:
        for name, payload in expected.items():
            report["files"][name] = {"generated_only": True, "count": payload.get("count")}

    assets = expected["assets.json"]
    lanes = expected["mcp_lanes.json"]
    loadouts = expected["loadouts.json"]
    scripts = expected["scripts.json"]
    policy_doc = expected["policy.json"]

    for name, payload in expected.items():
        _check_hash(payload, where=name)
        _scan_text_exclusions(payload, where=name)

    for record in assets["records"]:
        _require_fields(record, COMMON_ASSET_FIELDS, where=record["asset_id"])
        _check_hash(record, where=record["asset_id"])
        if record["lifecycle_state"] != "inert":
            raise CatalogValidationError(f"{record['asset_id']}: lifecycle must be inert")

    lane_records = lanes["records"]
    if len(lane_records) != 5:
        raise CatalogValidationError(f"expected 5 MCP lanes, got {len(lane_records)}")
    lane_ids = {item["lane_id"] for item in lane_records}
    if lane_ids != {
        "workflow-control",
        "delegation-coordination",
        "evidence-governance",
        "context-assets",
        "maintenance",
    }:
        raise CatalogValidationError(f"unexpected MCP lane set: {sorted(lane_ids)}")

    context = next(item for item in lane_records if item["lane_id"] == "context-assets")
    if not context.get("binds_stdio_compat_tools"):
        raise CatalogValidationError("context-assets must bind stdio compat tools")
    if set(context.get("stdio_compat_tools", [])) != set(STDIO_COMPAT_TOOLS):
        raise CatalogValidationError("context-assets stdio tool set mismatch")
    for item in lane_records:
        _check_hash(item, where=item["asset_id"])
        if set(item.get("forbidden_operations", [])) != set(FORBIDDEN_MCP_AND_SCHEDULE_OPS):
            raise CatalogValidationError(f"{item['asset_id']}: forbidden ops mismatch")
        if item.get("executable") is not False:
            raise CatalogValidationError(f"{item['asset_id']}: lanes must be non-executable at R1")
        tools = list(item.get("tools") or [])
        if len(tools) != len(set(tools)):
            raise CatalogValidationError(
                f"{item['asset_id']}: duplicate tool names in lane snapshot: {tools}"
            )

    loadout_records = loadouts["records"]
    if len(loadout_records) != 12:
        raise CatalogValidationError(f"expected 12 loadouts, got {len(loadout_records)}")
    loadout_ids = {item["loadout_id"] for item in loadout_records}
    if len(loadout_ids) != 12:
        raise CatalogValidationError("loadout IDs are not unique")
    for item in loadout_records:
        _require_fields(item, COMMON_ASSET_FIELDS, where=item["loadout_id"])
        _check_hash(item, where=item["loadout_id"])
        if item["lifecycle_state"] != "inert":
            raise CatalogValidationError(f"{item['loadout_id']}: must be inert")
        if item.get("executable") is not False:
            raise CatalogValidationError(f"{item['loadout_id']}: must be non-executable")
        asset_ids = {record["asset_id"] for record in assets["records"]}
        lane_asset_ids = {record["asset_id"] for record in lane_records}
        script_asset_ids = {record["asset_id"] for record in scripts["records"]}
        for field, allowed in (
            ("skill_refs", asset_ids),
            ("mcp_lane_refs", lane_asset_ids),
            ("script_refs", script_asset_ids),
        ):
            dangling = set(item.get(field, [])) - allowed
            if dangling:
                raise CatalogValidationError(
                    f"{item['loadout_id']}: dangling {field} {sorted(dangling)}"
                )

    script_records = scripts["records"]
    if len(script_records) != 12:
        raise CatalogValidationError(f"expected 12 scripts, got {len(script_records)}")
    for item in script_records:
        _check_hash(item, where=item["script_id"])
        if item.get("executable") is not False:
            raise CatalogValidationError(f"{item['script_id']}: repository scripts must be non-executable")
        if item.get("repository_script") is not True:
            raise CatalogValidationError(f"{item['script_id']}: must be catalog-only repository script")

    policy = policy_doc["record"]
    _check_hash(policy, where="policy.record")
    if policy.get("deny_wins") is not True:
        raise CatalogValidationError("policy must encode deny-wins")
    if policy.get("precedence") != list(PRECEDENCE_LAYERS):
        raise CatalogValidationError("policy precedence mismatch")
    if policy.get("immutable_dispatch_pin_fields") != list(DISPATCH_PIN_FIELDS):
        raise CatalogValidationError("dispatch pin fields mismatch")
    anomaly = policy.get("mandatory_anomaly_reporting") or {}
    if anomaly.get("required") is not True or anomaly.get("omission_invalid") is not True:
        raise CatalogValidationError("mandatory anomaly reporting not encoded")
    review = policy.get("independent_review_separation") or {}
    for key in (
        "distinct_provider_principal",
        "distinct_seat",
        "distinct_invocation",
        "distinct_attempt",
    ):
        if review.get(key) is not True:
            raise CatalogValidationError(f"independent review separation missing {key}")
    stdio = policy.get("stdio_mcp_compatibility") or {}
    if set(stdio.get("tools", [])) != set(STDIO_COMPAT_TOOLS):
        raise CatalogValidationError("policy stdio compatibility tool set mismatch")
    activation = policy.get("r1_activation") or {}
    if activation.get("assets_inert") is not True or activation.get("runtime_enforcement") is not False:
        raise CatalogValidationError("R1 assets must remain inert / non-enforcing")

    report["checks"] = [
        "hashes",
        "memberships",
        "forbidden_operations",
        "private_path_exclusions",
        "secret_exclusions",
        "stdio_compat",
        "deny_wins_precedence_dispatch_pin_anomaly_review",
        "inert_non_executable",
    ]
    return report


def main() -> None:
    for name in REQUIRED_FILES:
        if not (CATALOGS / name).is_file():
            from generate_catalogs import main as generate_main

            generate_main()
            break
    try:
        report = validate_catalogs(require_committed=True)
    except CatalogValidationError as exc:
        print(f"VALIDATION FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(report, indent=2, sort_keys=True))
    # Rebuild manifest when catalogs were just materialized.
    manifest_path = ROOT / "agentic" / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        inert = [
            e
            for e in manifest.get("entities", [])
            if e.get("kind") == "binding"
            and e.get("id", "").startswith("catalog.orchestrator.r1.")
        ]
        if len(inert) != 5:
            from build_manifest import main as build_manifest_main

            build_manifest_main()
            print("rebuilt agentic/manifest.json")


if __name__ == "__main__":
    main()
