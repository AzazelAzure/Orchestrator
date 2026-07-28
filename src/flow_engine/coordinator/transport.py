"""Serialize/deserialize RuntimeCommand for HTTP transport."""

from __future__ import annotations

from typing import Any

from flow_engine.coordinator.commands import (
    CommandContext,
    ResolvedTaskGrant,
    RuntimeCommand,
    StepUpEvidence,
    SystemTestGrant,
)
from flow_engine.domain.states import PrincipalRole, Surface


def _grant_from_dict(data: dict[str, Any] | None):
    if data is None:
        return None
    mode = data.get("compatibility_mode", "r2_system_test")
    surfaces = tuple(Surface(s) for s in data.get("surfaces", ["cli"]))
    providers = tuple(data.get("providers") or [])
    if mode == "r3_resolved":
        return ResolvedTaskGrant(
            grant_id=data["grant_id"],
            principal_id=data["principal_id"],
            role=PrincipalRole(data["role"]),
            surfaces=surfaces,
            providers=providers,
            budget_scope_id=data["budget_scope_id"],
            organization_id=data["organization_id"],
            organization_profile_hash=data.get("organization_profile_hash", ""),
            loadout_id=data["loadout_id"],
            snapshot_id=data["snapshot_id"],
            assignment_id=data.get("assignment_id", ""),
            capabilities=tuple(data.get("capabilities") or ()),
            policy_revision=data.get("policy_revision", "r3-default"),
            effect_ceiling=data.get("effect_ceiling", ""),
            compatibility_mode="r3_resolved",
        )
    return SystemTestGrant(
        grant_id=data["grant_id"],
        principal_id=data["principal_id"],
        role=PrincipalRole(data["role"]),
        surfaces=surfaces,
        providers=providers,
        budget_scope_id=data["budget_scope_id"],
        capabilities=tuple(data.get("capabilities") or ()),
        policy_revision=data.get("policy_revision", "system-test"),
        compatibility_mode=data.get("compatibility_mode", "r2_system_test"),
    )


def _step_up_from_dict(data: dict[str, Any] | None) -> StepUpEvidence | None:
    if data is None:
        return None
    return StepUpEvidence(
        reauthenticated_at=data["reauthenticated_at"],
        reason=data["reason"],
        evidence=data["evidence"],
        duplicate_cost_warning_ack=bool(data["duplicate_cost_warning_ack"]),
        policy_revision=data["policy_revision"],
        new_idempotency_identity=data["new_idempotency_identity"],
    )


def command_from_dict(data: dict[str, Any]) -> RuntimeCommand:
    ctx_data = data.get("context") or {}
    return RuntimeCommand(
        command_type=data["command_type"],
        target_id=data.get("target_id"),
        payload=data.get("payload") or {},
        idempotency_key=data.get("idempotency_key"),
        context=CommandContext(
            principal_id=ctx_data["principal_id"],
            role=PrincipalRole(ctx_data.get("role", "worker")),
            surface=Surface(ctx_data.get("surface", "rest")),
            grant=_grant_from_dict(ctx_data.get("grant")),
            step_up=_step_up_from_dict(ctx_data.get("step_up")),
            attempt_id=ctx_data.get("attempt_id"),
            provider_invocation_id=ctx_data.get("provider_invocation_id"),
            expected_revision=ctx_data.get("expected_revision"),
            mcp_service_principal_id=ctx_data.get("mcp_service_principal_id"),
            mcp_lane_id=ctx_data.get("mcp_lane_id"),
            mcp_tool_snapshot_digest=ctx_data.get("mcp_tool_snapshot_digest"),
            mcp_tool_name=ctx_data.get("mcp_tool_name"),
        ),
    )


def command_to_dict(command: RuntimeCommand) -> dict[str, Any]:
    ctx = command.context
    grant_dict = ctx.grant.to_dict() if ctx.grant is not None else None
    step_up_dict = ctx.step_up.to_dict() if ctx.step_up is not None else None
    return {
        "command_type": command.command_type,
        "target_id": command.target_id,
        "payload": command.payload,
        "idempotency_key": command.idempotency_key,
        "context": {
            "principal_id": ctx.principal_id,
            "role": str(ctx.role),
            "surface": str(ctx.surface),
            "grant": grant_dict,
            "step_up": step_up_dict,
            "attempt_id": ctx.attempt_id,
            "provider_invocation_id": ctx.provider_invocation_id,
            "expected_revision": ctx.expected_revision,
            "mcp_service_principal_id": ctx.mcp_service_principal_id,
            "mcp_lane_id": ctx.mcp_lane_id,
            "mcp_tool_snapshot_digest": ctx.mcp_tool_snapshot_digest,
            "mcp_tool_name": ctx.mcp_tool_name,
        },
    }
