"""DRF-side MCP tool handlers. MCP containers never import this with a DB path."""

from __future__ import annotations

from typing import Any

from flow_engine.application.loadout_resolution import (
    load_catalog_assets,
    load_catalog_lanes,
    load_catalog_loadouts,
    load_catalog_scripts,
    load_shipped_skill_hashes,
)
from flow_engine.domain.errors import UnsupportedSurfaceError, ValidationFailedError
from flow_engine.mcp_lanes.catalog import load_lane_by_id
from flow_engine.mcp_lanes.forbidden import FORBIDDEN_OPERATIONS
from flow_engine.mcp_lanes.profiles import department_capability_profiles, profile_for_department
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot

# Tools that map to coordinator runtime commands (initiating principal authz).
WORKFLOW_TOOL_TO_COMMAND: dict[str, str] = {
    "preview": "runtime.preview",
    "step": "runtime.step",
    "run": "runtime.run",
    "pause": "runtime.pause",
    "resume": "runtime.resume",
    "cancel": "runtime.cancel",
    "reconcile": "runtime.reconcile",
}

DELEGATION_TOOL_TO_COMMAND: dict[str, str] = {
    "request": "delegation.request",
    "dispatch": "delegation.dispatch",
    "handoff": "delegation.handoff",
}

# Skills/scripts lane — register via coordinator under initiating principal authz.
SCRIPT_TOOL_TO_COMMAND: dict[str, str] = {
    "request_script_run": "script.register",
}

SCRIPT_READ_TOOLS = frozenset(
    {
        "list_skills",
        "get_skill",
        "list_scripts",
        "describe_script",
    }
)

# Keys stripped from request_script_run arguments (mirror ScriptExecuteView bans).
SCRIPT_SMUGGLING_KEYS = frozenset(
    {
        "workspace_root",
        "override_argv",
        "override_cwd",
        "inject_env",
        "force_timeout",
        "simulate_network",
        "cwd",
        "argv",
        "env",
    }
)

# Maintenance tools — status/run via coordinator; never remediation.
MAINTENANCE_TOOLS = frozenset(
    {
        "schedule_status_run",
        "registered_check_execution",
        "maintenance_evidence",
        "health",
        "recovery_test_results",
    }
)


def _bounded_args(arguments: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(arguments or {})
    for banned in (
        "principal_id",
        "role",
        "grant",
        "step_up",
        "capabilities",
        "worker_principal_id",
        "raw_token",
        "service_token",
        *SCRIPT_SMUGGLING_KEYS,
    ):
        raw.pop(banned, None)
    return raw


def describe_lane(lane_id: str) -> dict[str, Any]:
    lane = load_lane_by_id(lane_id)
    snapshot = lane_tool_snapshot(lane_id)
    return {
        "lane_id": lane_id,
        "asset_id": lane["asset_id"],
        "snapshot": snapshot,
        "forbidden_operations": sorted(FORBIDDEN_OPERATIONS),
        "notes": lane.get("notes") or "",
    }


def handle_read_tool(
    *,
    lane_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    initiating_principal: dict[str, Any],
    service_principal: dict[str, Any],
) -> dict[str, Any]:
    """Catalog/status handlers — no provider CLI, no script execution."""
    args = _bounded_args(arguments)
    base = {
        "lane_id": lane_id,
        "tool": tool_name,
        "authority": "drf",
        "initiating_principal_id": initiating_principal.get("principal_id"),
        "initiating_principal_key": initiating_principal.get("principal_key"),
        "mcp_service_principal_id": service_principal.get("principal_id"),
        "mcp_service_principal_key": service_principal.get("principal_key"),
        "arguments": args,
    }

    if tool_name in FORBIDDEN_OPERATIONS:
        raise UnsupportedSurfaceError(f"{tool_name} forbidden on MCP")

    if lane_id == "maintenance" and tool_name in MAINTENANCE_TOOLS:
        return _maintenance_tool_result(
            lane_id=lane_id,
            tool_name=tool_name,
            base=base,
            args=args,
        )

    if lane_id == "skills-scripts" and tool_name in SCRIPT_READ_TOOLS:
        return _skills_scripts_read_result(
            lane_id=lane_id,
            tool_name=tool_name,
            base=base,
            args=args,
        )

    if tool_name in {
        "catalog_search_get",
        "loadout_resolution",
        "loadout_preview",
        "org_profile_read",
        "bounded_packets",
        "session_brief",
        "repo_ci_pr_reads",
        "repo_health",
        "open_prs",
        "ci_status",
        "work_lookup",
        "work_status",
        "queue_status",
        "dependency_status",
        "gate_status",
        "budget_status",
        "artifacts",
        "findings_anomalies",
        "reports",
        "review_acceptance",
        "escalation",
        "policy_explanation",
        "ordinary_authorized_gate_actions",
        "assignment",
    }:
        return _catalog_or_status_result(lane_id=lane_id, tool_name=tool_name, base=base, args=args)

    if tool_name in DELEGATION_TOOL_TO_COMMAND:
        raise ValidationFailedError(
            f"tool {tool_name} requires delegation command dispatch, not read handler"
        )

    if tool_name in WORKFLOW_TOOL_TO_COMMAND:
        raise ValidationFailedError(
            f"tool {tool_name} requires workflow command dispatch, not read handler"
        )

    if tool_name in SCRIPT_TOOL_TO_COMMAND:
        raise ValidationFailedError(
            f"tool {tool_name} requires script command dispatch, not read handler"
        )

    raise ValidationFailedError(f"no handler for tool {tool_name} on lane {lane_id}")


def _skills_scripts_read_result(
    *,
    lane_id: str,
    tool_name: str,
    base: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    """Publication-neutral skill/script catalog reads — no activation or writes."""
    if tool_name == "list_skills":
        hashes = load_shipped_skill_hashes()
        skills = [
            {"skill_id": skill_id, "content_sha256": digest}
            for skill_id, digest in sorted(hashes.items())
        ]
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "skills_catalog",
                "skills": skills,
                "count": len(skills),
                "publication_candidate": False,
                "activation": False,
            },
        }

    if tool_name == "get_skill":
        skill_id = str(args.get("skill_id") or "")
        if not skill_id:
            raise ValidationFailedError("skill_id is required")
        hashes = load_shipped_skill_hashes()
        digest = hashes.get(skill_id)
        if digest is None:
            raise ValidationFailedError(f"unknown skill_id: {skill_id}")
        # Metadata only — do not mutate manifests or activate packages.
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "skill_metadata",
                "skill_id": skill_id,
                "content_sha256": digest,
                "publication_candidate": False,
                "activation": False,
            },
        }

    if tool_name == "list_scripts":
        scripts = load_catalog_scripts()
        rows = [
            {
                "script_id": sid,
                "executable": bool(rec.get("executable")),
                "kind": rec.get("kind"),
                "content_sha256": rec.get("content_sha256"),
            }
            for sid, rec in sorted(scripts.items())
        ]
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "scripts_catalog",
                "scripts": rows,
                "count": len(rows),
                "mcp_direct_execution": False,
            },
        }

    if tool_name == "describe_script":
        script_id = str(args.get("script_id") or "")
        if not script_id:
            raise ValidationFailedError("script_id is required")
        scripts = load_catalog_scripts()
        rec = scripts.get(script_id)
        if rec is None:
            raise ValidationFailedError(f"unknown script_id: {script_id}")
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "script_describe",
                "script_id": script_id,
                "executable": bool(rec.get("executable")),
                "kind": rec.get("kind"),
                "content_sha256": rec.get("content_sha256"),
                "notes": rec.get("notes") or "",
                "mcp_direct_execution": False,
                "executable_via": "drf_script_worker",
            },
        }

    raise ValidationFailedError(f"no skills-scripts read handler for {tool_name}")


def _catalog_or_status_result(
    *,
    lane_id: str,
    tool_name: str,
    base: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    if tool_name in {"catalog_search_get", "bounded_packets"}:
        assets = load_catalog_assets()
        query = str(args.get("query") or args.get("asset_id") or "")
        matches = []
        for asset_id, record in assets.items():
            if not query or query in asset_id or query in str(record.get("kind", "")):
                matches.append(
                    {
                        "asset_id": asset_id,
                        "kind": record.get("kind"),
                        "content_sha256": record.get("content_sha256"),
                    }
                )
            if len(matches) >= 25:
                break
        return {**base, "status": "ok", "result": {"matches": matches, "count": len(matches)}}

    if tool_name in {"loadout_resolution", "loadout_preview"}:
        loadouts = load_catalog_loadouts()
        loadout_id = args.get("loadout_id")
        if loadout_id:
            record = loadouts.get(str(loadout_id))
            if record is None:
                raise ValidationFailedError(f"unknown loadout_id: {loadout_id}")
            return {
                **base,
                "status": "ok",
                "result": {
                    "loadout_id": loadout_id,
                    "mcp_lane_refs": list(record.get("mcp_lane_refs") or []),
                    "department": record.get("department"),
                    "position": record.get("position"),
                    "forbidden": list(record.get("forbidden") or []),
                },
            }
        department = args.get("department")
        if department:
            return {
                **base,
                "status": "ok",
                "result": profile_for_department(str(department)),
            }
        return {
            **base,
            "status": "ok",
            "result": {
                "departments": department_capability_profiles(),
                "loadout_ids": sorted(loadouts),
            },
        }

    if tool_name == "org_profile_read":
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "catalog_bound",
                "detail": "Organization profile reads go through DRF; no MCP SQLite.",
            },
        }

    if tool_name in {
        "repo_health",
        "open_prs",
        "ci_status",
        "work_lookup",
        "session_brief",
        "repo_ci_pr_reads",
    }:
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "drf_proxy",
                "stdio_compat": tool_name
                in {"repo_health", "open_prs", "ci_status", "work_lookup", "session_brief"},
                "provider_calls": False,
                "sqlite_from_mcp": False,
                "detail": (
                    "Context tool served by DRF authority only; "
                    "MCP lane did not open SQLite or call providers."
                ),
            },
        }

    if tool_name in {
        "work_status",
        "queue_status",
        "dependency_status",
        "gate_status",
        "budget_status",
    }:
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "status",
                "tool": tool_name,
                "target_id": args.get("target_id") or args.get("run_id") or args.get("work_item_id"),
            },
        }

    if lane_id == "evidence-governance":
        if tool_name in {"review_acceptance", "ordinary_authorized_gate_actions"}:
            # Ordinary gate actions allowed as status/ack surface; waiver still forbidden.
            return {
                **base,
                "status": "ok",
                "result": {
                    "mode": "governance_read",
                    "waiver_available": False,
                    "exception_available": False,
                    "merge_available": False,
                },
            }
        return {
            **base,
            "status": "ok",
            "result": {"mode": "evidence_read", "tool": tool_name, "append_only": True},
        }

    if lane_id == "delegation-coordination":
        scripts = load_catalog_scripts()
        # Repository scripts remain catalog-only / non-executable.
        repo_scripts = [
            {
                "script_id": sid,
                "executable": bool(rec.get("executable")),
            }
            for sid, rec in scripts.items()
            if rec.get("executable") is False or "repository" in sid
        ]
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "delegation_read",
                "tool": tool_name,
                "repository_scripts_executable": False,
                "catalog_only_scripts_sample": repo_scripts[:5],
            },
        }

    # Fallback: lane metadata only.
    lanes = load_catalog_lanes()
    return {
        **base,
        "status": "ok",
        "result": {
            "mode": "lane_metadata",
            "tool": tool_name,
            "lane_asset": lanes.get(f"mcp.lane.{lane_id}", {}).get("asset_id"),
        },
    }


def workflow_command_for_tool(tool_name: str) -> str | None:
    return WORKFLOW_TOOL_TO_COMMAND.get(tool_name)


def script_command_for_tool(tool_name: str) -> str | None:
    return SCRIPT_TOOL_TO_COMMAND.get(tool_name)


def delegation_command_for_tool(tool_name: str, arguments: dict[str, Any] | None = None) -> str | None:
    if tool_name == "disposition":
        args = arguments or {}
        action = str(args.get("action") or args.get("disposition") or "accept").lower()
        if action in {"decline", "reject", "deny"}:
            return "delegation.decline"
        if action in {"reroute", "route"}:
            return "delegation.reroute"
        return "delegation.accept"
    return DELEGATION_TOOL_TO_COMMAND.get(tool_name)


def _maintenance_tool_result(
    *,
    lane_id: str,
    tool_name: str,
    base: dict[str, Any],
    args: dict[str, Any],
) -> dict[str, Any]:
    from flow_engine.domain.errors import FlowError
    from flow_engine.schedules.templates import list_schedule_templates
    from flow_engine.script_sandbox.allowlist import list_allowlist
    from flow_engine.script_sandbox.classify import (
        ScriptClass,
        classify_script,
        reject_repository_script,
    )

    if tool_name == "health":
        return {
            **base,
            "status": "ok",
            "result": {"surface": "mcp.maintenance", "ready": True, "r4c": True},
        }

    if tool_name == "schedule_status_run":
        # Status only via MCP — activation remains forbidden. Tick/run go through DRF.
        templates = list_schedule_templates()
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "schedule_status",
                "timezone": "Asia/Manila",
                "templates": templates,
                "activation_available": False,
                "provider_call_budget": 0,
                "concurrency": 1,
                "remediation_available": False,
            },
        }

    if tool_name == "registered_check_execution":
        script_id = str(args.get("script_id") or "")
        if not script_id:
            return {
                **base,
                "status": "ok",
                "result": {
                    "mode": "allowlist_catalog",
                    "scripts": [
                        {"script_id": s["script_id"], "executable": True}
                        for s in list_allowlist()
                    ],
                    "repository_scripts_executable": False,
                },
            }
        kind = classify_script(script_id)
        if kind != ScriptClass.GENERIC_ALLOWLISTED:
            try:
                reject_repository_script(script_id)
            except FlowError as exc:
                return {
                    **base,
                    "status": "rejected",
                    "error_code": getattr(exc, "code", "UNSUPPORTED_SURFACE"),
                    "error": str(exc),
                    "result": {
                        "executable": False,
                        "repository_script": True,
                        "script_id": script_id,
                    },
                }
        # MCP may describe / request registration identity only — execution via DRF/worker.
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "registered_check",
                "script_id": script_id,
                "executable_via": "drf_script_worker",
                "mcp_direct_execution": False,
                "repository_script": False,
            },
        }

    if tool_name == "maintenance_evidence":
        return {
            **base,
            "status": "ok",
            "result": {
                "mode": "evidence_read",
                "allowed_effects": sorted(
                    {"evidence", "finding", "anomaly", "follow_up_work_candidate"}
                ),
                "forbidden_effects": sorted(
                    {
                        "repair",
                        "remediation",
                        "repository_mutation",
                        "merge",
                        "deploy",
                        "provider_call",
                    }
                ),
            },
        }

    return {
        **base,
        "status": "ok",
        "result": {"tool": tool_name, "mode": "status_only", "executable": False},
    }

