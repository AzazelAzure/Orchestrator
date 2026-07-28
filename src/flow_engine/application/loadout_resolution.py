"""R3 loadout resolution with deny-wins precedence and fail-closed hashes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from flow_engine.coordinator.commands import stable_digest
from flow_engine.domain.errors import StaleAssetError

PRECEDENCE_LAYERS = (
    "engine_safety_floor",
    "handbook_version",
    "installation_policy",
    "orchestrator_base",
    "department",
    "hierarchy_layer",
    "position",
    "project_repo_extension",
    "task_class",
    "explicit_task_grant",
)

# Non-configurable engine safety floor (design-contract §9).
ENGINE_SAFETY_FLOOR: dict[str, Any] = {
    "denials": [
        "upward_authority",
        "self_review",
        "silent_waiver",
        "silent_exception",
        "authority_broadening",
        "provider_identity_as_authority",
    ],
    "mandatory_controls": [
        "anomaly_reporting",
        "append_only_audit",
        "independent_review_separation",
        "idempotency",
        "unknown_outcome_halt",
    ],
    "capabilities": None,  # unrestricted until narrowed by later layers
    "effects": None,
    "numeric_bounds": {
        "global_provider_concurrency": 3,
        "per_provider_concurrency": 1,
        "per_project_concurrency": 3,
        "per_run_concurrency": 2,
        "per_attempt_provider_calls": 1,
    },
    "path_bounds": None,
    "network_bounds": {"default": "deny"},
    "secret_bounds": {"projection": "deny"},
}


def _agentic_root() -> Path:
    # Prefer in-repo agentic catalogs relative to this package's repo root.
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "agentic"
        if (candidate / "catalogs" / "loadouts.json").is_file():
            return candidate
    raise StaleAssetError("R1 loadout catalog not discoverable")


def load_catalog_loadouts() -> dict[str, dict[str, Any]]:
    path = _agentic_root() / "catalogs" / "loadouts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["loadout_id"]: item for item in payload["records"]}


def load_catalog_assets() -> dict[str, dict[str, Any]]:
    path = _agentic_root() / "catalogs" / "assets.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["asset_id"]: item for item in payload["records"]}


def load_catalog_lanes() -> dict[str, dict[str, Any]]:
    path = _agentic_root() / "catalogs" / "mcp_lanes.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["asset_id"]: item for item in payload["records"]}


def load_catalog_scripts() -> dict[str, dict[str, Any]]:
    path = _agentic_root() / "catalogs" / "scripts.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["script_id"]: item for item in payload["records"]}


def load_shipped_skill_hashes() -> dict[str, str]:
    skills_root = _agentic_root().parent / "skills"
    hashes: dict[str, str] = {}
    for manifest_path in sorted(skills_root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        skill_id = manifest.get("skill_id")
        content_hash = manifest.get("content_sha256")
        if not skill_id or not content_hash or len(str(content_hash)) != 64:
            raise StaleAssetError(f"invalid shipped skill manifest: {manifest_path}")
        digest = hashlib.sha256()
        package_files = sorted(
            path
            for path in manifest_path.parent.rglob("*")
            if path.is_file()
            and path.relative_to(manifest_path.parent).as_posix()
            not in {"manifest.json", ".hq-managed-skill.json"}
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
        for package_path in package_files:
            relative = package_path.relative_to(manifest_path.parent).as_posix()
            data = package_path.read_bytes()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(len(data)).encode("ascii"))
            digest.update(b"\0")
            digest.update(data)
        if digest.hexdigest() != content_hash:
            raise StaleAssetError(f"shipped skill package hash drifted: {skill_id}")
        if skill_id in hashes:
            raise StaleAssetError(f"duplicate shipped skill identity: {skill_id}")
        hashes[str(skill_id)] = str(content_hash)
    return hashes


def load_catalog_policy() -> dict[str, Any]:
    path = _agentic_root() / "catalogs" / "policy.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["record"]


def load_catalog_loadout(loadout_id: str) -> dict[str, Any]:
    loadouts = load_catalog_loadouts()
    if loadout_id not in loadouts:
        raise StaleAssetError(f"unknown loadout: {loadout_id}")
    return loadouts[loadout_id]


def _intersect_sets(
    left: set[str] | None, right: set[str] | None
) -> set[str] | None:
    if left is None:
        return set(right) if right is not None else None
    if right is None:
        return set(left)
    return left & right


def _most_restrictive_numeric(
    left: dict[str, int] | None, right: dict[str, int] | None
) -> dict[str, int]:
    out = dict(left or {})
    for key, value in (right or {}).items():
        if key not in out:
            out[key] = value
        else:
            out[key] = min(out[key], value)
    return out


def _merge_bounds(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> dict[str, Any]:
    """Most restrictive wins: explicit deny overrides allow; missing keys inherit."""
    if left is None and right is None:
        return {}
    if left is None:
        return dict(right or {})
    if right is None:
        return dict(left)
    out = dict(left)
    for key, value in right.items():
        if key not in out:
            out[key] = value
        elif value == "deny" or out[key] == "deny":
            out[key] = "deny"
        elif isinstance(value, (int, float)) and isinstance(out[key], (int, float)):
            out[key] = min(out[key], value)
        else:
            # Conflicting non-numeric bounds fail closed.
            if out[key] != value:
                raise StaleAssetError(f"conflicting bound for {key}")
    return out


def merge_authority_layers(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply precedence merge rules across ordered authority layers."""
    denials: set[str] = set()
    mandatory: set[str] = set()
    capabilities: set[str] | None = None
    effects: set[str] | None = None
    numeric = dict(ENGINE_SAFETY_FLOOR["numeric_bounds"])
    path_bounds: dict[str, Any] = {}
    network_bounds: dict[str, Any] = dict(ENGINE_SAFETY_FLOOR["network_bounds"])
    secret_bounds: dict[str, Any] = dict(ENGINE_SAFETY_FLOOR["secret_bounds"])

    for layer in layers:
        denials |= set(layer.get("denials") or [])
        mandatory |= set(layer.get("mandatory_controls") or [])
        capabilities = _intersect_sets(
            capabilities,
            set(layer["capabilities"]) if layer.get("capabilities") is not None else None,
        )
        effects = _intersect_sets(
            effects,
            set(layer["effects"]) if layer.get("effects") is not None else None,
        )
        numeric = _most_restrictive_numeric(numeric, layer.get("numeric_bounds"))
        path_bounds = _merge_bounds(path_bounds, layer.get("path_bounds"))
        network_bounds = _merge_bounds(network_bounds, layer.get("network_bounds"))
        secret_bounds = _merge_bounds(secret_bounds, layer.get("secret_bounds"))

    return {
        "denials": sorted(denials),
        "mandatory_controls": sorted(mandatory),
        "capabilities": sorted(capabilities) if capabilities is not None else None,
        "effects": sorted(effects) if effects is not None else None,
        "numeric_bounds": numeric,
        "path_bounds": path_bounds,
        "network_bounds": network_bounds,
        "secret_bounds": secret_bounds,
    }


def verify_asset_hash(asset: dict[str, Any], *, expected_hash: str | None = None) -> str:
    actual = asset.get("content_sha256")
    if not actual or len(str(actual)) != 64:
        raise StaleAssetError(
            f"missing or invalid content_sha256 for {asset.get('asset_id') or asset.get('loadout_id')}"
        )
    if expected_hash is not None and actual != expected_hash:
        raise StaleAssetError(
            f"stale asset hash for {asset.get('asset_id') or asset.get('loadout_id')}"
        )
    return str(actual)


def resolve_loadout(
    *,
    loadout_id: str,
    organization_profile: dict[str, Any],
    department_ceiling: dict[str, Any] | None = None,
    hierarchy_ceiling: dict[str, Any] | None = None,
    position_ceiling: dict[str, Any] | None = None,
    project_extension: dict[str, Any] | None = None,
    task_class: dict[str, Any] | None = None,
    explicit_grant: dict[str, Any] | None = None,
    handbook: dict[str, Any] | None = None,
    installation_policy: dict[str, Any] | None = None,
    product_base: dict[str, Any] | None = None,
    expected_loadout_hash: str | None = None,
    expected_member_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Resolve a positional loadout with full precedence and pin material."""
    org_hash = organization_profile.get("content_sha256")
    if not org_hash:
        raise StaleAssetError("organization profile hash missing")

    loadout = load_catalog_loadout(loadout_id)
    loadout_hash = verify_asset_hash(loadout, expected_hash=expected_loadout_hash)
    assets = load_catalog_assets()
    lanes = load_catalog_lanes()
    scripts = load_catalog_scripts()
    shipped_skill_hashes = load_shipped_skill_hashes()
    policy = load_catalog_policy()
    policy_hash = verify_asset_hash(policy)

    member_hashes: dict[str, str] = {}
    for skill_id in loadout.get("skill_refs") or []:
        if skill_id not in assets:
            raise StaleAssetError(f"dangling skill ref: {skill_id}")
        if not assets[skill_id].get("package_shipped"):
            raise StaleAssetError(f"skill package is not shipped: {skill_id}")
        if skill_id not in shipped_skill_hashes:
            raise StaleAssetError(f"shipped skill manifest missing: {skill_id}")
        member_hashes[skill_id] = shipped_skill_hashes[skill_id]
    for lane_id in loadout.get("mcp_lane_refs") or []:
        if lane_id not in lanes:
            raise StaleAssetError(f"dangling MCP lane ref: {lane_id}")
        member_hashes[lane_id] = verify_asset_hash(lanes[lane_id])
    for script_id in loadout.get("script_refs") or []:
        if script_id not in scripts:
            raise StaleAssetError(f"dangling script ref: {script_id}")
        member_hashes[script_id] = verify_asset_hash(scripts[script_id])

    if expected_member_hashes:
        for key, expected in expected_member_hashes.items():
            if key not in member_hashes:
                raise StaleAssetError(f"expected member missing from resolution: {key}")
            if member_hashes[key] != expected:
                raise StaleAssetError(f"stale member asset hash: {key}")

    layers = [
        ENGINE_SAFETY_FLOOR,
        handbook or {},
        installation_policy or {},
        product_base or {},
        department_ceiling or {},
        hierarchy_ceiling or {},
        position_ceiling or {},
        project_extension or {},
        task_class or {},
        explicit_grant or {},
    ]
    # Repo extensions may only add assets or narrow authority — never remove floor denials.
    merged = merge_authority_layers(layers)
    for denial in ENGINE_SAFETY_FLOOR["denials"]:
        if denial not in merged["denials"]:
            raise StaleAssetError("resolution dropped engine safety floor denial")

    # Loadout forbidden effects are denials.
    for forbidden in loadout.get("forbidden") or []:
        if forbidden not in merged["denials"]:
            merged["denials"] = sorted(set(merged["denials"]) | {forbidden})

    resolution = {
        "loadout_id": loadout_id,
        "loadout_hash": loadout_hash,
        "organization_profile_identity": organization_profile["id"],
        "organization_profile_hash": org_hash,
        "policy_identity": policy.get("policy_id") or policy.get("asset_id"),
        "policy_hash": policy_hash,
        "department": loadout.get("department"),
        "position": loadout.get("position"),
        "skill_refs": list(loadout.get("skill_refs") or []),
        "mcp_lane_refs": list(loadout.get("mcp_lane_refs") or []),
        "script_refs": list(loadout.get("script_refs") or []),
        "member_asset_hashes": member_hashes,
        "effect_ceiling": loadout.get("effect_ceiling"),
        "budget_ceiling": loadout.get("budget_ceiling"),
        "forbidden": list(loadout.get("forbidden") or []),
        "authority": merged,
        "precedence": list(PRECEDENCE_LAYERS),
    }
    resolution["content_sha256"] = stable_digest(resolution)
    return resolution


def all_twelve_loadout_ids() -> list[str]:
    return sorted(load_catalog_loadouts().keys())


def resolve_all_twelve_loadouts(organization_profile: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        resolve_loadout(loadout_id=loadout_id, organization_profile=organization_profile)
        for loadout_id in all_twelve_loadout_ids()
    ]
