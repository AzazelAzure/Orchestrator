"""Exact endpoint-by-principal-kind capability matrix (DRF + coordinator).

Deny-by-default. MCP/scheduler may not run recovery or founder operations.
Workers may deliver/heartbeat/result but not recover or founder ops unless
an explicit capability is present on the principal record.
"""

from __future__ import annotations

from flow_engine.domain.errors import AuthzDeniedError

# command_type -> allowed principal kinds (exact allowlist)
COMMAND_KIND_MATRIX: dict[str, frozenset[str]] = {
    # Founder workflow
    "runtime.preview": frozenset({"founder"}),
    "runtime.run": frozenset({"founder"}),
    "runtime.create": frozenset({"founder"}),
    "runtime.claim": frozenset({"founder", "worker"}),
    "runtime.step": frozenset({"founder"}),
    "runtime.show": frozenset({"founder", "worker"}),
    "runtime.pause": frozenset({"founder"}),
    "runtime.resume": frozenset({"founder"}),
    "runtime.cancel": frozenset({"founder"}),
    "runtime.reconcile": frozenset({"founder"}),
    "runtime.credit_usage": frozenset({"founder"}),
    "runtime.list_audit": frozenset({"founder"}),
    "runtime.provider_limit_halt": frozenset({"founder"}),
    "runtime.provider_limit_continue": frozenset({"founder"}),
    "runtime.provider_limit_reroute": frozenset({"founder"}),
    # Founder-only escalation
    "runtime.new_attempt_after_unknown": frozenset({"founder"}),
    "runtime.waive_gate": frozenset({"founder"}),
    "runtime.hitm_exception": frozenset({"founder"}),
    # Worker delivery path
    "runtime.heartbeat": frozenset({"worker", "provider_invocation"}),
    "runtime.result": frozenset({"worker", "provider_invocation"}),
    "runtime.worker_deliver": frozenset({"worker"}),
    "runtime.worker_prepare": frozenset({"worker"}),
    "runtime.worker_preflight": frozenset({"worker"}),
    "runtime.worker_preflight_reject": frozenset({"worker"}),
    "runtime.worker_snapshot": frozenset({"worker"}),
    "runtime.worker_settle": frozenset({"worker"}),
    "runtime.worker_cancel_prepare": frozenset({"worker"}),
    "runtime.worker_cancel_settle": frozenset({"worker"}),
    "delivery.register": frozenset({"founder", "worker"}),
    "delivery.claim": frozenset({"worker"}),
    "delivery.heartbeat": frozenset({"worker"}),
    "delivery.complete": frozenset({"worker"}),
    "delivery.list_eligible": frozenset({"worker"}),
    "delivery.get_by_invocation": frozenset({"worker", "founder"}),
    # Recovery — founder only unless explicit capability (checked separately)
    "runtime.recover_restart": frozenset({"founder"}),
    "runtime.recover_worker_death": frozenset({"founder"}),
    "runtime.reconstruct_deliveries": frozenset({"founder"}),
    "runtime.replay_delivery_hint": frozenset({"founder"}),
    "runtime.evaluate_timeouts": frozenset({"founder"}),
    "delivery.recover_stale": frozenset({"founder"}),
    # Control-plane admin
    "control_plane.register_principal": frozenset({"founder"}),
    "control_plane.revoke": frozenset({"founder"}),
    "control_plane.resolve_token": frozenset(
        {"founder", "scheduler", "mcp_service", "worker", "provider_invocation", "system"}
    ),
    # R4B MCP lane gateway (initiating principal; service principal checked separately)
    "mcp.snapshot.get": frozenset({"founder", "worker", "scheduler"}),
    "mcp.tools.list": frozenset({"founder", "worker", "scheduler"}),
    "mcp.tool.invoke": frozenset({"founder", "worker", "scheduler"}),
    "mcp.profiles.list": frozenset({"founder", "worker", "scheduler", "mcp_service"}),
    # R4C scripts / schedules
    "script.list_allowlist": frozenset({"founder", "scheduler", "worker", "mcp_service"}),
    "script.register": frozenset({"founder", "scheduler", "worker"}),
    "script.start": frozenset({"worker"}),
    "script.complete": frozenset({"worker"}),
    "script.execute": frozenset({"founder", "worker"}),
    "script.cancel": frozenset({"founder", "worker"}),
    "script.show": frozenset({"founder", "scheduler", "worker", "mcp_service"}),
    "schedule.list_templates": frozenset({"founder", "scheduler", "worker", "mcp_service"}),
    "schedule.status": frozenset({"founder", "scheduler", "worker", "mcp_service"}),
    "schedule.tick": frozenset({"founder", "scheduler"}),
    "schedule.complete": frozenset({"founder", "scheduler"}),
    "schedule.run_on_demand": frozenset({"founder"}),
    "ops.dashboard_read": frozenset({"founder", "system", "scheduler", "mcp_service"}),
    "delegation.request": frozenset({"founder", "executive", "manager"}),
    "delegation.accept": frozenset({"founder", "executive", "manager"}),
    "delegation.decline": frozenset({"founder", "executive", "manager"}),
    "delegation.reroute": frozenset({"founder", "executive", "manager"}),
    "delegation.dispatch": frozenset({"founder", "executive", "manager"}),
    "delegation.handoff": frozenset({"founder", "executive", "manager"}),
    "delegation.accept_handoff": frozenset({"founder", "executive", "manager"}),
}

# Explicit capability keys that can widen recovery for non-founder kinds.
RECOVERY_CAPABILITY = "recovery.control_plane"
FOUNDER_OPS_CAPABILITY = "founder.ops"

RECOVERY_COMMANDS = frozenset(
    {
        "runtime.recover_restart",
        "runtime.recover_worker_death",
        "runtime.reconstruct_deliveries",
        "runtime.replay_delivery_hint",
        "runtime.evaluate_timeouts",
        "delivery.recover_stale",
    }
)

FOUNDER_OPS_COMMANDS = frozenset(
    {
        "runtime.new_attempt_after_unknown",
        "runtime.waive_gate",
        "runtime.hitm_exception",
    }
)

# REST path -> command_type mapping for DRF enforcement
REST_ENDPOINT_COMMANDS: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/runtime/preview"): "runtime.preview",
    ("POST", "/api/v1/runtime/run"): "runtime.run",
    ("GET", "/api/v1/runtime/runs"): "runtime.show",  # prefix match handled by view
    ("POST", "/api/v1/runtime/heartbeat"): "runtime.heartbeat",
    ("POST", "/api/v1/runtime/result"): "runtime.result",
    ("POST", "/api/v1/runtime/recover"): "runtime.recover_restart",
    ("POST", "/api/v1/runtime/pause"): "runtime.pause",
    ("POST", "/api/v1/runtime/resume"): "runtime.resume",
    ("POST", "/api/v1/runtime/cancel"): "runtime.cancel",
    ("GET", "/api/v1/delivery/jobs"): "delivery.list_eligible",
    ("GET", "/api/v1/mcp/profiles"): "mcp.profiles.list",
    ("GET", "/api/v1/mcp/lanes"): "mcp.snapshot.get",
    ("POST", "/api/v1/mcp/lanes"): "mcp.tool.invoke",
    ("GET", "/api/v1/scripts/allowlist"): "script.list_allowlist",
    ("POST", "/api/v1/scripts/execute"): "script.register",
    ("GET", "/api/v1/scripts/executions"): "script.show",
    ("POST", "/api/v1/scripts/cancel"): "script.cancel",
    ("GET", "/api/v1/schedules/templates"): "schedule.list_templates",
    ("GET", "/api/v1/schedules/status"): "schedule.status",
    ("POST", "/api/v1/schedules/tick"): "schedule.tick",
    ("POST", "/api/v1/schedules/complete"): "schedule.complete",
    ("POST", "/api/v1/schedules/run"): "schedule.run_on_demand",
}


def assert_command_allowed_for_kind(
    *,
    command_type: str,
    principal_kind: str,
    capabilities: tuple[str, ...] | list[str] = (),
) -> None:
    """Deny-by-default matrix check. Capabilities may widen recovery/founder ops."""
    caps = set(capabilities)
    if command_type in FOUNDER_OPS_COMMANDS:
        if principal_kind == "founder" or FOUNDER_OPS_CAPABILITY in caps:
            return
        raise AuthzDeniedError(
            f"command {command_type} denied for principal kind {principal_kind}"
        )
    if command_type in RECOVERY_COMMANDS:
        if principal_kind == "founder" or RECOVERY_CAPABILITY in caps:
            return
        raise AuthzDeniedError(
            f"recovery command {command_type} denied for principal kind {principal_kind}"
        )

    allowed = COMMAND_KIND_MATRIX.get(command_type)
    if allowed is None:
        # Unknown commands: founder only (fail closed for novel verbs)
        if principal_kind != "founder":
            raise AuthzDeniedError(f"unknown command {command_type} denied")
        return
    if principal_kind == "system" and command_type == "control_plane.resolve_token":
        return
    if principal_kind not in allowed:
        raise AuthzDeniedError(
            f"command {command_type} denied for principal kind {principal_kind}"
        )
