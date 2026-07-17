#!/usr/bin/env python3
"""Materialize Orchestrator's repository-owned agentic manifest."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "agentic/manifest.json"
TOOLS = ("ci_status", "open_prs", "repo_health", "session_brief", "work_lookup")


def verification(source: str, *, valid: bool | None = None) -> dict:
    return {
        "source_present": (ROOT / source).exists(),
        "package_valid": valid,
        "binding_configured": None,
        "executable_resolves": None,
        "smoke_pass": None,
        "captured_at": None,
        "method": "tracked-source inspection; live binding and smoke are installation-local",
    }


def main() -> None:
    entities: list[dict] = []
    relationships: list[dict] = []
    skill_ids: set[str] = set()
    for skill_dir in sorted(ROOT.glob("skills/*")):
        manifest_path = skill_dir / "manifest.json"
        if not manifest_path.is_file():
            continue
        package = json.loads(manifest_path.read_text(encoding="utf-8"))
        skill_id = package["skill_id"]
        skill_ids.add(skill_id)
        entities.append({
            "id": skill_id,
            "kind": "skill_package",
            "name": skill_dir.name,
            "owner": "platform.orchestrator",
            "domain": "orchestrator",
            "status": "active",
            "source": skill_dir.relative_to(ROOT).as_posix(),
            "version": package["version"],
            "content_sha256": package["content_sha256"],
            "authority": "repository-package-owner",
            "sensitivity": "public",
            "mutation_class": "local" if package.get("write_set") else "none",
            "interface": {"activation_state": package["activation_state"], "scheduling_ref": package["scheduling_ref"]},
            "notes": package.get("notes", ""),
            "verification": verification(skill_dir.relative_to(ROOT).as_posix(), valid=True),
        })
    for bundle_path in sorted((ROOT / "skills/bundles").glob("*.json")):
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle_id = bundle["bundle_id"]
        entities.append({
            "id": bundle_id,
            "kind": "skill_bundle",
            "name": bundle_path.stem,
            "owner": "platform.orchestrator",
            "domain": "orchestrator",
            "status": "active",
            "source": bundle_path.relative_to(ROOT).as_posix(),
            "version": "1",
            "content_sha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            "authority": "repository-package-owner",
            "sensitivity": "public",
            "mutation_class": "none",
            "interface": {"activation": bundle["activation"]},
            "notes": "References packages by stable skill ID; contains no copied skill body.",
            "verification": verification(bundle_path.relative_to(ROOT).as_posix(), valid=True),
        })
        for member in bundle["members"]:
            if member not in skill_ids:
                raise SystemExit(f"unknown bundle member: {member}")
            relationships.append({"from": member, "type": "member_of", "to": bundle_id})
    server_id = "mcp.server.orchestrator.read-only"
    entities.append({
        "id": server_id,
        "kind": "mcp_server",
        "name": "flowctl-mcp",
        "owner": "platform.orchestrator",
        "domain": "orchestrator",
        "status": "available",
        "source": "src/flow_engine/mcp/server.py",
        "version": "0.1.0",
        "content_sha256": None,
        "authority": "read-only-default",
        "sensitivity": "public",
        "mutation_class": "none",
        "interface": {"transport": "stdio", "entrypoint": "flowctl-mcp", "tool_count": len(TOOLS)},
        "notes": "Agent invocation is not exposed; Orchestrator workers retain agent authority.",
        "verification": verification("src/flow_engine/mcp/server.py"),
    })
    for name in TOOLS:
        capability_id = f"capability.orchestrator.{name.replace('_', '-')}"
        tool_id = f"mcp.tool.orchestrator.{name.replace('_', '-')}"
        entities.extend([
            {"id": capability_id, "kind": "capability", "name": name, "owner": "platform.orchestrator", "domain": "orchestrator", "status": "active", "source": "src/flow_engine/capabilities/transport.py", "version": "0.1.0", "content_sha256": None, "authority": "read-only-default", "sensitivity": "public", "mutation_class": "none", "interface": {"logical_name": name}, "notes": "Shared application capability used by CLI and MCP transports.", "verification": verification("src/flow_engine/capabilities/transport.py")},
            {"id": tool_id, "kind": "mcp_tool", "name": name, "owner": "platform.orchestrator", "domain": "orchestrator", "status": "available", "source": "src/flow_engine/capabilities/transport.py", "version": "0.1.0", "content_sha256": None, "authority": "read-only-default", "sensitivity": "public", "mutation_class": "none", "interface": {"transport": "stdio"}, "notes": "Typed MCP exposure of the corresponding logical capability.", "verification": verification("src/flow_engine/capabilities/transport.py")},
        ])
        relationships.extend([
            {"from": server_id, "type": "exposes", "to": tool_id},
            {"from": tool_id, "type": "wraps", "to": capability_id},
        ])
    data = {
        "schema_version": 1,
        "repository": {"id": "orchestrator", "domain": "orchestrator", "manifest_path": "agentic/manifest.json", "revision": None},
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entities": sorted(entities, key=lambda item: item["id"]),
        "relationships": sorted(relationships, key=lambda item: (item["from"], item["type"], item["to"])),
    }
    OUT.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
