"""Authoritative R1 catalog definitions (inert contracts; not runtime).

Memberships mirror the Headquarters loadout catalog design direction, encoded
as portable product contracts without private paths or installation policy.
"""

from __future__ import annotations

from typing import Any

CATALOG_SCHEMA_VERSION = 1
CATALOG_VERSION = "0.1.0"
OWNER = "platform.orchestrator"
COMPATIBILITY = "orchestrator>=0.1.0"
SENSITIVITY = "public"
LIFECYCLE_INERT = "inert"
PROVENANCE = "r1-design-contract"

# Always-on floor skills (logical package IDs match skills/*/manifest.json).
ALWAYS_ON_FLOOR = (
    "skill.session-orientation",
    "skill.design-first-gate",
    "skill.investigation-report",
    "skill.handoff-contract",
    "skill.trust-but-verify",
)

# Existing eleven portable skills shipped under skills/.
EXISTING_PORTABLE_SKILLS = (
    "skill.session-orientation",
    "skill.design-first-gate",
    "skill.investigation-report",
    "skill.handoff-contract",
    "skill.trust-but-verify",
    "skill.skill-gap-detection",
    "skill.ci-test-triage",
    "skill.code-review-risk-triage",
    "skill.security-audit-procedure",
    "skill.cpprd-changelog-authoring",
    "skill.repo-exploration-briefing",
)

# R3 portable skill packages (shipped under skills/; referenced by loadouts).
R3_PORTABLE_SKILLS = (
    "skill.anomaly-handling",
    "skill.task-decomposition",
    "skill.dependency-planning",
    "skill.loadout-selection",
    "skill.execution-supervision",
    "skill.report-acceptance",
    "skill.recovery-triage",
    "skill.task-execution",
    "skill.implementation-loop",
    "skill.review-gate",
    "skill.governance-audit",
    "skill.documentation-maintenance",
    "skill.conference-facilitation",
    "skill.schedule-operations",
    "skill.orchestrator-admin-ops-exec",
    "skill.orchestrator-admin-qa-exec",
    "skill.orchestrator-admin-tech-exec",
)

# Backward-compatible alias used by older validators/docs.
PLANNED_SKILL_IDS = R3_PORTABLE_SKILLS


STDIO_COMPAT_TOOLS = (
    "repo_health",
    "open_prs",
    "ci_status",
    "work_lookup",
    "session_brief",
)

FORBIDDEN_MCP_AND_SCHEDULE_OPS = (
    "waiver",
    "hitm_exception",
    "paid_retry_after_unknown",
    "merge",
    "deploy",
    "publication",
    "schedule_activation",
    "arbitrary_script_execution",
    "policy_profile_activation",
    "credential_projection",
    "unrestricted_state_mutation",
    "direct_database_access",
    "provider_cli_invocation",
)

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

DISPATCH_PIN_FIELDS = (
    "policy_identity",
    "policy_hash",
    "organization_profile_identity",
    "organization_profile_hash",
    "loadout_identity",
    "loadout_hash",
    "member_asset_hashes",
    "packet_hash",
    "budget_identity",
    "grant_identity",
    "attempt_id",
    "invocation_id",
)

ANOMALY_CODES = (
    {"code": "A0", "class": "integrity_security", "behavior": "stop_mutation_critical_finding"},
    {"code": "A1", "class": "uncertain_side_effect", "behavior": "stop_mark_unknown_reconcile"},
    {"code": "A2", "class": "authority_scope_gate", "behavior": "deny_high_finding_revoke_grant"},
    {"code": "A3", "class": "runtime_resource", "behavior": "pause_preserve_recovery"},
    {"code": "A4", "class": "evidence_report", "behavior": "reject_completion_require_correction"},
    {"code": "A5", "class": "quality_maintenance", "behavior": "record_route_gate_conditional"},
)


def _asset_base(
    *,
    asset_id: str,
    kind: str,
    source: str,
    notes: str = "",
) -> dict[str, Any]:
    return {
        "asset_id": asset_id,
        "kind": kind,
        "version": CATALOG_VERSION,
        "owner": OWNER,
        "source": source,
        "compatibility": COMPATIBILITY,
        "sensitivity": SENSITIVITY,
        "lifecycle_state": LIFECYCLE_INERT,
        "provenance": PROVENANCE,
        "activation": "inert",
        "executable": False,
        "notes": notes,
    }


def mcp_lane_defs() -> list[dict[str, Any]]:
    lanes = [
        {
            **_asset_base(
                asset_id="mcp.lane.workflow-control",
                kind="mcp_lane_profile",
                source="agentic/catalogs/mcp_lanes.json",
                notes="Design-only lane profile; calls DRF only in later milestones.",
            ),
            "lane_id": "workflow-control",
            "tools": [
                "work_status",
                "queue_status",
                "dependency_status",
                "gate_status",
                "budget_status",
                "preview",
                "step",
                "run",
                "pause",
                "resume",
                "cancel",
                "reconcile",
            ],
            "binds_stdio_compat_tools": False,
            "forbidden_operations": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        },
        {
            **_asset_base(
                asset_id="mcp.lane.delegation-coordination",
                kind="mcp_lane_profile",
                source="agentic/catalogs/mcp_lanes.json",
            ),
            "lane_id": "delegation-coordination",
            "tools": [
                "org_profile_read",
                "loadout_preview",
                "assignment",
                "request",
                "disposition",
                "dispatch",
                "handoff",
            ],
            "binds_stdio_compat_tools": False,
            "forbidden_operations": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        },
        {
            **_asset_base(
                asset_id="mcp.lane.evidence-governance",
                kind="mcp_lane_profile",
                source="agentic/catalogs/mcp_lanes.json",
            ),
            "lane_id": "evidence-governance",
            "tools": [
                "artifacts",
                "findings_anomalies",
                "reports",
                "review_acceptance",
                "escalation",
                "policy_explanation",
                "ordinary_authorized_gate_actions",
            ],
            "binds_stdio_compat_tools": False,
            "forbidden_operations": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        },
        {
            **_asset_base(
                asset_id="mcp.lane.context-assets",
                kind="mcp_lane_profile",
                source="agentic/catalogs/mcp_lanes.json",
                notes="Also binds the existing five read-only stdio MCP tools.",
            ),
            "lane_id": "context-assets",
            "tools": [
                "bounded_packets",
                "catalog_search_get",
                "loadout_resolution",
                "repo_ci_pr_reads",
                *STDIO_COMPAT_TOOLS,
            ],
            "binds_stdio_compat_tools": True,
            "stdio_compat_tools": list(STDIO_COMPAT_TOOLS),
            "forbidden_operations": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        },
        {
            **_asset_base(
                asset_id="mcp.lane.maintenance",
                kind="mcp_lane_profile",
                source="agentic/catalogs/mcp_lanes.json",
            ),
            "lane_id": "maintenance",
            "tools": [
                "schedule_status_run",
                "registered_check_execution",
                "maintenance_evidence",
                "health",
                "recovery_test_results",
            ],
            "binds_stdio_compat_tools": False,
            "forbidden_operations": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        },
        {
            **_asset_base(
                asset_id="mcp.lane.skills-scripts",
                kind="mcp_lane_profile",
                source="agentic/catalogs/mcp_lanes.json",
                notes=(
                    "Catalog reads for shipped skills and allowlisted scripts; "
                    "request_script_run dispatches script.register via DRF dual-principal "
                    "invoke (never MCP-side shell or direct script.execute)."
                ),
            ),
            "lane_id": "skills-scripts",
            "tools": [
                "list_skills",
                "get_skill",
                "list_scripts",
                "describe_script",
                "request_script_run",
            ],
            "binds_stdio_compat_tools": False,
            "forbidden_operations": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        },
    ]
    for record in lanes:
        tools = list(record.get("tools") or [])
        if len(tools) != len(set(tools)):
            raise ValueError(
                f"{record.get('lane_id')}: duplicate tool names in lane snapshot: {tools}"
            )
    return lanes


def script_defs() -> list[dict[str, Any]]:
    scripts = [
        ("script.generic.repository_health", "Repository health", "read_only"),
        ("script.generic.git_diff_summary", "Git diff summary", "read_only"),
        ("script.generic.repository_inventory", "Repository inventory", "read_only"),
        (
            "script.generic.documentation_link_sweep",
            "Documentation link sweep",
            "evidence_producing",
        ),
        (
            "script.generic.documentation_metadata_sweep",
            "Documentation metadata sweep",
            "evidence_producing",
        ),
        (
            "script.generic.governance_integrity_sweep",
            "Governance integrity sweep",
            "evidence_producing",
        ),
        (
            "script.generic.secret_pattern_scan",
            "Secret-pattern scan",
            "evidence_producing",
        ),
        (
            "script.generic.dependency_manifest_inventory",
            "Dependency-manifest inventory",
            "evidence_producing",
        ),
        (
            "script.generic.catalog_integrity_sweep",
            "Catalog integrity sweep",
            "evidence_producing",
        ),
        (
            "script.generic.stale_work_sweep",
            "Stale work/gate/lease/attempt sweep",
            "evidence_producing",
        ),
        (
            "script.generic.queue_worker_heartbeat_health",
            "Queue/worker/heartbeat health",
            "read_only",
        ),
        (
            "script.generic.backup_restore_probe",
            "Backup/restore probe",
            "evidence_producing",
        ),
    ]
    out: list[dict[str, Any]] = []
    for script_id, name, mutation_class in scripts:
        out.append(
            {
                **_asset_base(
                    asset_id=script_id,
                    kind="registered_generic_script",
                    source="agentic/catalogs/scripts.json",
                    notes="Catalog-only at R1; registry presence is not authority.",
                ),
                "script_id": script_id,
                "name": name,
                "mutation_class": mutation_class,
                "repository_script": True,
                "executable": False,
                "network_policy": "deny_by_default",
                "argv_only": True,
                "hardening": {
                    "non_root": True,
                    "read_only_root_fs": True,
                    "no_new_privileges": True,
                    "seccomp": True,
                    "ephemeral_tmp": True,
                },
            }
        )
    return out


def loadout_defs() -> list[dict[str, Any]]:
    """Twelve department × position loadouts."""

    def loadout(
        *,
        loadout_id: str,
        department: str,
        position: str,
        extra_skills: list[str],
        mcp_lanes: list[str],
        scripts: list[str],
        forbidden: list[str],
        effect_ceiling: str,
        budget_ceiling: str,
    ) -> dict[str, Any]:
        skill_refs = list(ALWAYS_ON_FLOOR) + extra_skills
        return {
            **_asset_base(
                asset_id=loadout_id,
                kind="loadout",
                source="agentic/catalogs/loadouts.json",
                notes="Inert positional loadout design; native enforcement is R3.",
            ),
            "loadout_id": loadout_id,
            "department": department,
            "position": position,
            "skill_refs": skill_refs,
            "capability_refs": [],
            "mcp_lane_refs": [f"mcp.lane.{lane}" for lane in mcp_lanes],
            "script_refs": scripts,
            "effect_ceiling": effect_ceiling,
            "budget_ceiling": budget_ceiling,
            "forbidden": forbidden,
            "precedence_inputs": list(PRECEDENCE_LAYERS),
        }

    return [
        loadout(
            loadout_id="loadout.admin-ops.executive",
            department="admin-ops",
            position="executive",
            extra_skills=[
                "skill.orchestrator-admin-ops-exec",
                "skill.task-decomposition",
                "skill.loadout-selection",
                "skill.conference-facilitation",
                "skill.governance-audit",
                "skill.anomaly-handling",
            ],
            mcp_lanes=[
                "workflow-control",
                "delegation-coordination",
                "evidence-governance",
                "context-assets",
                "skills-scripts",
            ],
            scripts=["script.generic.repository_health", "script.generic.git_diff_summary"],
            forbidden=[
                "deep_implementation",
                "self_review",
                "merge_deploy",
                "waiver_via_mcp",
            ],
            effect_ceiling="read_heavy_coordination",
            budget_ceiling="coordination_default",
        ),
        loadout(
            loadout_id="loadout.admin-ops.manager",
            department="admin-ops",
            position="manager",
            extra_skills=[
                "skill.task-decomposition",
                "skill.dependency-planning",
                "skill.report-acceptance",
                "skill.loadout-selection",
                "skill.conference-facilitation",
            ],
            mcp_lanes=[
                "delegation-coordination",
                "evidence-governance",
                "context-assets",
                "workflow-control",
            ],
            scripts=["script.generic.repository_health"],
            forbidden=["implementation_binding", "upward_authority", "founder_ops"],
            effect_ceiling="coordination_limited",
            budget_ceiling="coordination_default",
        ),
        loadout(
            loadout_id="loadout.admin-ops.supervisor",
            department="admin-ops",
            position="supervisor",
            extra_skills=[
                "skill.execution-supervision",
                "skill.report-acceptance",
                "skill.recovery-triage",
                "skill.anomaly-handling",
                "skill.ci-test-triage",
            ],
            mcp_lanes=[
                "workflow-control",
                "evidence-governance",
                "context-assets",
                "maintenance",
            ],
            scripts=[
                "script.generic.repository_health",
                "script.generic.queue_worker_heartbeat_health",
            ],
            forbidden=["implementation", "waiver", "schedule_activation"],
            effect_ceiling="supervision",
            budget_ceiling="supervision_default",
        ),
        loadout(
            loadout_id="loadout.admin-ops.worker",
            department="admin-ops",
            position="worker",
            extra_skills=[
                "skill.task-execution",
                "skill.documentation-maintenance",
                "skill.conference-facilitation",
                "skill.schedule-operations",
                "skill.cpprd-changelog-authoring",
            ],
            mcp_lanes=[
                "context-assets",
                "evidence-governance",
                "maintenance",
                "skills-scripts",
            ],
            scripts=[
                "script.generic.documentation_link_sweep",
                "script.generic.documentation_metadata_sweep",
            ],
            forbidden=[
                "implementation_code_writes",
                "review_of_own_work",
                "founder_ops",
            ],
            effect_ceiling="assigned_read_or_evidence",
            budget_ceiling="worker_default",
        ),
        loadout(
            loadout_id="loadout.qa.executive",
            department="qa",
            position="executive",
            extra_skills=[
                "skill.orchestrator-admin-qa-exec",
                "skill.review-gate",
                "skill.governance-audit",
                "skill.loadout-selection",
                "skill.anomaly-handling",
            ],
            mcp_lanes=[
                "evidence-governance",
                "delegation-coordination",
                "context-assets",
                "workflow-control",
            ],
            scripts=["script.generic.repository_health"],
            forbidden=[
                "implementation",
                "reuse_implementation_context_for_review_or_merge",
            ],
            effect_ceiling="review_coordination",
            budget_ceiling="coordination_default",
        ),
        loadout(
            loadout_id="loadout.qa.manager",
            department="qa",
            position="manager",
            extra_skills=[
                "skill.review-gate",
                "skill.report-acceptance",
                "skill.dependency-planning",
                "skill.security-audit-procedure",
            ],
            mcp_lanes=[
                "evidence-governance",
                "delegation-coordination",
                "context-assets",
            ],
            scripts=["script.generic.repository_health"],
            forbidden=["implementation", "self_merge"],
            effect_ceiling="review_coordination",
            budget_ceiling="coordination_default",
        ),
        loadout(
            loadout_id="loadout.qa.supervisor",
            department="qa",
            position="supervisor",
            extra_skills=[
                "skill.execution-supervision",
                "skill.code-review-risk-triage",
                "skill.ci-test-triage",
                "skill.security-audit-procedure",
                "skill.report-acceptance",
                "skill.anomaly-handling",
            ],
            mcp_lanes=["evidence-governance", "workflow-control", "context-assets"],
            scripts=[
                "script.generic.secret_pattern_scan",
                "script.generic.catalog_integrity_sweep",
            ],
            forbidden=["implementation_of_reviewed_work"],
            effect_ceiling="review_supervision",
            budget_ceiling="supervision_default",
        ),
        loadout(
            loadout_id="loadout.qa.worker",
            department="qa",
            position="worker",
            extra_skills=[
                "skill.review-gate",
                "skill.code-review-risk-triage",
                "skill.security-audit-procedure",
                "skill.ci-test-triage",
                "skill.governance-audit",
            ],
            mcp_lanes=["evidence-governance", "context-assets"],
            scripts=["script.generic.repository_health"],
            forbidden=[
                "any_implementation_of_item_under_review",
                "same_invocation_as_implementer",
            ],
            effect_ceiling="independent_review_only",
            budget_ceiling="worker_default",
        ),
        loadout(
            loadout_id="loadout.tech.executive",
            department="tech",
            position="executive",
            extra_skills=[
                "skill.orchestrator-admin-tech-exec",
                "skill.task-decomposition",
                "skill.loadout-selection",
                "skill.dependency-planning",
                "skill.anomaly-handling",
            ],
            mcp_lanes=[
                "workflow-control",
                "delegation-coordination",
                "context-assets",
                "evidence-governance",
            ],
            scripts=["script.generic.repository_health"],
            forbidden=["deep_implementation_in_executive_context", "self_review"],
            effect_ceiling="tech_coordination",
            budget_ceiling="coordination_default",
        ),
        loadout(
            loadout_id="loadout.tech.manager",
            department="tech",
            position="manager",
            extra_skills=[
                "skill.task-decomposition",
                "skill.dependency-planning",
                "skill.report-acceptance",
                "skill.repo-exploration-briefing",
            ],
            mcp_lanes=[
                "delegation-coordination",
                "workflow-control",
                "context-assets",
                "evidence-governance",
            ],
            scripts=["script.generic.repository_health", "script.generic.repository_inventory"],
            forbidden=["self_review", "founder_ops"],
            effect_ceiling="tech_coordination",
            budget_ceiling="coordination_default",
        ),
        loadout(
            loadout_id="loadout.tech.supervisor",
            department="tech",
            position="supervisor",
            extra_skills=[
                "skill.execution-supervision",
                "skill.ci-test-triage",
                "skill.code-review-risk-triage",
                "skill.recovery-triage",
                "skill.report-acceptance",
                "skill.anomaly-handling",
            ],
            mcp_lanes=[
                "workflow-control",
                "evidence-governance",
                "context-assets",
                "maintenance",
            ],
            scripts=[
                "script.generic.queue_worker_heartbeat_health",
                "script.generic.repository_health",
            ],
            forbidden=["self_merge", "waiver"],
            effect_ceiling="tech_supervision",
            budget_ceiling="supervision_default",
        ),
        loadout(
            loadout_id="loadout.tech.worker",
            department="tech",
            position="worker",
            extra_skills=[
                "skill.task-execution",
                "skill.implementation-loop",
                "skill.repo-exploration-briefing",
                "skill.ci-test-triage",
                "skill.cpprd-changelog-authoring",
            ],
            mcp_lanes=["context-assets", "workflow-control", "evidence-governance"],
            scripts=[
                "script.generic.git_diff_summary",
                "script.generic.dependency_manifest_inventory",
            ],
            forbidden=[
                "independent_self_review_of_own_implementation",
                "merge_deploy",
            ],
            effect_ceiling="implementation_within_grant",
            budget_ceiling="worker_default",
        ),
    ]


def policy_contract_def() -> dict[str, Any]:
    return {
        **_asset_base(
            asset_id="policy.contract.r1.governance",
            kind="policy",
            source="agentic/catalogs/policy.json",
            notes=(
                "Inert governance contract for R1. Does not activate installation "
                "policy or make any Headquarters project contract executable."
            ),
        ),
        "policy_id": "policy.contract.r1.governance",
        "deny_wins": True,
        "mandatory_controls_union": True,
        "capabilities_intersect_parent_ceilings": True,
        "most_restrictive_numeric_wins": True,
        "fail_closed_on_hash_mismatch": True,
        "precedence": list(PRECEDENCE_LAYERS),
        "immutable_dispatch_pin_fields": list(DISPATCH_PIN_FIELDS),
        "mandatory_anomaly_reporting": {
            "required": True,
            "omission_invalid": True,
            "stop_mutation_if_persistence_unavailable": True,
            "taxonomy": list(ANOMALY_CODES),
            "terminal_report_requires": [
                "anomalies_or_findings",
                "gaps",
                "evidence",
                "terminal_status",
            ],
        },
        "independent_review_separation": {
            "required": True,
            "distinct_provider_principal": True,
            "distinct_seat": True,
            "distinct_invocation": True,
            "distinct_attempt": True,
            "defaults": {
                "admin_ops": "codex",
                "tech_implementation": "cursor",
                "qa_independent_review": "claude",
            },
        },
        "stdio_mcp_compatibility": {
            "retain_five_tool_stdio_surface": True,
            "tools": list(STDIO_COMPAT_TOOLS),
            "also_bound_into_lane": "mcp.lane.context-assets",
            "silent_removal_forbidden": True,
        },
        "forbidden_on_mcp_and_schedules": list(FORBIDDEN_MCP_AND_SCHEDULE_OPS),
        "r1_activation": {
            "runtime_enforcement": False,
            "installation_policy_active": False,
            "project_contract_executable": False,
            "assets_inert": True,
        },
        "r3_activation": {
            "hierarchy_delegation_enforcement": True,
            "resolved_loadout_pins_required": True,
            "r2_system_test_compatibility_retained": True,
            "mcp_lane_containers": False,
            "schedules_active": False,
            "scripts_executable": False,
        },
    }


def portable_asset_index_entries() -> list[dict[str, Any]]:
    """Logical portable asset index (skills + catalogs)."""
    entries: list[dict[str, Any]] = []
    for skill_id in EXISTING_PORTABLE_SKILLS + R3_PORTABLE_SKILLS:
        dirname = skill_id.removeprefix("skill.")
        entries.append(
            {
                **_asset_base(
                    asset_id=skill_id,
                    kind="skill_package",
                    source=f"skills/{dirname}/",
                    notes="Portable skill package under skills/.",
                ),
                "package_shipped": True,
                "scheduling_ref": None,
            }
        )
    return entries
