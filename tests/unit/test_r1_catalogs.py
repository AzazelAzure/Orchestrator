"""R1 inert catalog validation, hashes, memberships, and MCP compatibility."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
AGENTIC = ROOT / "agentic"
CATALOGS = AGENTIC / "catalogs"

if str(AGENTIC) not in sys.path:
    sys.path.insert(0, str(AGENTIC))

from catalog_defs import (  # noqa: E402
    FORBIDDEN_MCP_AND_SCHEDULE_OPS,
    STDIO_COMPAT_TOOLS,
)
from catalog_hash import content_sha256  # noqa: E402
from generate_catalogs import build_all  # noqa: E402
from validate_catalogs import CatalogValidationError, validate_catalogs  # noqa: E402

SCHEMA_MAP = {
    "assets.json": "portable-asset-index.schema.json",
    "mcp_lanes.json": "mcp-lane-catalog.schema.json",
    "loadouts.json": "loadout-catalog.schema.json",
    "scripts.json": "script-catalog.schema.json",
    "policy.json": "policy-contract.schema.json",
}


def _ensure_committed_catalogs() -> None:
    expected = build_all()
    stale = False
    for name in SCHEMA_MAP:
        path = CATALOGS / name
        if not path.is_file():
            stale = True
            break
        committed = json.loads(path.read_text(encoding="utf-8"))
        if committed != expected[name]:
            stale = True
            break
    if stale:
        from generate_catalogs import main as generate_main

        generate_main()
    manifest_path = AGENTIC / "manifest.json"
    needs_manifest = not manifest_path.is_file()
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        inert = [
            e
            for e in manifest.get("entities", [])
            if e.get("kind") == "binding"
            and e.get("id", "").startswith("catalog.orchestrator.r1.")
        ]
        skill_packages = [
            e for e in manifest.get("entities", []) if e.get("kind") == "skill_package"
        ]
        needs_manifest = len(inert) != 5 or len(skill_packages) != 28
    if needs_manifest:
        from build_manifest import main as build_manifest_main

        build_manifest_main()


_ensure_committed_catalogs()


def _structural_schema_check(payload: dict, schema: dict, *, where: str) -> None:
    """Minimal required-field / const checks without external jsonschema."""
    for key in schema.get("required", []):
        assert key in payload, f"{where}: missing required {key}"
    for key, rules in schema.get("properties", {}).items():
        if key not in payload:
            continue
        if "const" in rules:
            assert payload[key] == rules["const"], f"{where}.{key}: const mismatch"
        if rules.get("type") == "integer" and "const" in rules:
            assert payload[key] == rules["const"]
        if key == "content_sha256":
            assert isinstance(payload[key], str) and len(payload[key]) == 64


def test_generate_catalogs_is_deterministic() -> None:
    first = build_all()
    second = build_all()
    assert first == second
    for _name, payload in first.items():
        assert payload["content_sha256"] == content_sha256(payload)
        assert payload["lifecycle_state"] == "inert"
        assert payload["activation"] == "inert"


def test_committed_catalogs_match_generator_and_validate() -> None:
    report = validate_catalogs(require_committed=True)
    assert report["ok"] is True
    assert "hashes" in report["checks"]
    assert set(report["files"]) == set(SCHEMA_MAP)


def test_catalog_schemas_exist_and_cover_required_shape() -> None:
    generated = build_all()
    for name, schema_name in SCHEMA_MAP.items():
        schema_path = CATALOGS / "schemas" / schema_name
        assert schema_path.is_file(), schema_name
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        _structural_schema_check(generated[name], schema, where=name)
        committed = json.loads((CATALOGS / name).read_text(encoding="utf-8"))
        _structural_schema_check(committed, schema, where=f"committed:{name}")


def test_memberships_five_lanes_twelve_loadouts_twelve_scripts() -> None:
    data = build_all()
    assert data["mcp_lanes.json"]["count"] == 5
    assert data["loadouts.json"]["count"] == 12
    assert data["scripts.json"]["count"] == 12
    assert len(data["mcp_lanes.json"]["records"]) == 5
    assert len(data["loadouts.json"]["records"]) == 12
    assert len(data["scripts.json"]["records"]) == 12


def test_loadout_references_resolve_and_script_memberships_are_pinned() -> None:
    data = build_all()
    assets = {item["asset_id"] for item in data["assets.json"]["records"]}
    lanes = {item["asset_id"] for item in data["mcp_lanes.json"]["records"]}
    scripts = {item["asset_id"] for item in data["scripts.json"]["records"]}
    expected_script_refs = {
        "loadout.qa.executive": {"script.generic.repository_health"},
        "loadout.qa.worker": {"script.generic.repository_health"},
        "loadout.tech.supervisor": {
            "script.generic.repository_health",
            "script.generic.queue_worker_heartbeat_health",
        },
    }
    for loadout in data["loadouts.json"]["records"]:
        assert set(loadout["skill_refs"]) <= assets
        assert set(loadout["mcp_lane_refs"]) <= lanes
        assert set(loadout["script_refs"]) <= scripts
        if loadout["loadout_id"] in expected_script_refs:
            assert set(loadout["script_refs"]) == expected_script_refs[loadout["loadout_id"]]


def test_forbidden_operations_encoded_on_every_lane() -> None:
    for lane in build_all()["mcp_lanes.json"]["records"]:
        assert set(lane["forbidden_operations"]) == set(FORBIDDEN_MCP_AND_SCHEDULE_OPS)
        assert lane["executable"] is False


def test_repository_scripts_are_catalog_only_non_executable() -> None:
    for script in build_all()["scripts.json"]["records"]:
        assert script["repository_script"] is True
        assert script["executable"] is False
        assert script["network_policy"] == "deny_by_default"


def test_policy_encodes_precedence_deny_wins_dispatch_pin_anomaly_review() -> None:
    policy = build_all()["policy.json"]["record"]
    assert policy["deny_wins"] is True
    assert policy["precedence"][0] == "engine_safety_floor"
    assert "policy_hash" in policy["immutable_dispatch_pin_fields"]
    assert policy["mandatory_anomaly_reporting"]["required"] is True
    assert policy["independent_review_separation"]["distinct_attempt"] is True
    assert policy["r1_activation"]["assets_inert"] is True
    assert policy["r1_activation"]["runtime_enforcement"] is False
    assert policy["r1_activation"]["installation_policy_active"] is False


def test_private_path_and_secret_exclusions() -> None:
    validate_catalogs(require_committed=True)
    for path in CATALOGS.rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "/home/" not in text
        assert "/Users/" not in text
        assert "BEGIN PRIVATE KEY" not in text


def test_stdio_five_tool_compatibility_retained_and_bound() -> None:
    from flow_engine.capabilities.transport import APPROVED_MCP_TOOL_NAMES

    assert set(APPROVED_MCP_TOOL_NAMES) == set(STDIO_COMPAT_TOOLS)
    lanes = build_all()["mcp_lanes.json"]["records"]
    context = next(item for item in lanes if item["lane_id"] == "context-assets")
    assert context["binds_stdio_compat_tools"] is True
    assert set(context["stdio_compat_tools"]) == set(STDIO_COMPAT_TOOLS)
    policy = build_all()["policy.json"]["record"]
    assert policy["stdio_mcp_compatibility"]["silent_removal_forbidden"] is True
    assert set(policy["stdio_mcp_compatibility"]["tools"]) == set(STDIO_COMPAT_TOOLS)


def test_hash_tamper_is_detected() -> None:
    path = CATALOGS / "loadouts.json"
    original = path.read_text(encoding="utf-8")
    try:
        tampered = json.loads(original)
        tampered["records"][0]["content_sha256"] = "0" * 64
        path.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with pytest.raises(CatalogValidationError, match="differs from deterministic"):
            validate_catalogs(require_committed=True)
    finally:
        path.write_text(original, encoding="utf-8")


def test_manifest_discovers_inert_catalogs_without_activation() -> None:
    manifest = json.loads((AGENTIC / "manifest.json").read_text(encoding="utf-8"))
    inert_catalogs = [
        e
        for e in manifest["entities"]
        if e.get("kind") == "binding"
        and e["id"].startswith("catalog.orchestrator.r1.")
    ]
    assert len(inert_catalogs) == 5
    for entity in inert_catalogs:
        assert entity["status"] == "inert"
        assert entity["interface"]["executable"] is False
        assert entity["interface"]["runtime_enforcement"] is False
    loadouts = [
        e
        for e in manifest["entities"]
        if e.get("kind") == "binding" and e["id"].startswith("loadout.")
    ]
    assert len(loadouts) == 12
    assert all(e["status"] == "inert" for e in loadouts)
    # Stdio MCP surface remains present.
    tool_names = {e["name"] for e in manifest["entities"] if e.get("kind") == "mcp_tool"}
    assert tool_names == set(STDIO_COMPAT_TOOLS)
