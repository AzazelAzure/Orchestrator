"""Authorization checks for coordinator commands."""

from __future__ import annotations

from datetime import UTC, datetime

from flow_engine.application.clock import utc_now_iso
from flow_engine.coordinator.commands import (
    FOUNDER_ONLY_COMMANDS,
    MCP_FORBIDDEN_COMMANDS,
    ResolvedTaskGrant,
    RuntimeCommand,
    StepUpEvidence,
    SystemTestGrant,
)
from flow_engine.domain.credits import STEP_UP_MAX_AGE_SEC
from flow_engine.domain.errors import (
    AuthRequiredError,
    AuthzDeniedError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.domain.states import PrincipalRole, Surface


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def validate_step_up(step_up: StepUpEvidence | None, *, now_iso: str | None = None) -> None:
    if step_up is None:
        raise AuthzDeniedError("founder step-up evidence required")
    if not step_up.reason.strip():
        raise ValidationFailedError("step-up reason is required")
    if not step_up.evidence.strip():
        raise ValidationFailedError("step-up evidence is required")
    if not step_up.duplicate_cost_warning_ack:
        raise AuthzDeniedError("duplicate-cost warning must be acknowledged")
    if not step_up.policy_revision.strip():
        raise ValidationFailedError("step-up policy_revision is required")
    if not step_up.new_idempotency_identity.strip():
        raise ValidationFailedError("new idempotency identity is required")

    now = _parse_iso(now_iso or utc_now_iso())
    reauth = _parse_iso(step_up.reauthenticated_at)
    age = (now - reauth).total_seconds()
    if age < 0 or age > STEP_UP_MAX_AGE_SEC:
        raise AuthzDeniedError("step-up reauthentication older than five minutes")


def authorize_command(
    command: RuntimeCommand,
    *,
    principal_kind: str | None = None,
    capabilities: tuple[str, ...] | list[str] | None = None,
) -> None:
    ctx = command.context
    if not ctx.principal_id:
        raise AuthRequiredError("principal_id is required")

    # Exact endpoint-by-kind matrix when kind is known (R4 registry / HTTP path).
    # Legacy R1–R3 in-process callers without a kind skip this layer.
    if principal_kind is not None and command.command_type != "control_plane.resolve_token":
        from flow_engine.control_plane.authz_matrix import assert_command_allowed_for_kind

        assert_command_allowed_for_kind(
            command_type=command.command_type,
            principal_kind=principal_kind,
            capabilities=capabilities
            or (ctx.grant.capabilities if ctx.grant is not None else ()),
        )

    if command.command_type in MCP_FORBIDDEN_COMMANDS and ctx.surface == Surface.MCP:
        raise UnsupportedSurfaceError(
            f"{command.command_type} is forbidden on MCP surface"
        )
    if command.command_type in FOUNDER_ONLY_COMMANDS and ctx.surface == Surface.SCHEDULE:
        raise UnsupportedSurfaceError(
            f"{command.command_type} is forbidden on schedule surface"
        )

    if command.command_type in FOUNDER_ONLY_COMMANDS:
        if ctx.role != PrincipalRole.FOUNDER:
            raise AuthzDeniedError("founder role required")
        validate_step_up(ctx.step_up)

    if ctx.grant is None and command.command_type.startswith("runtime."):
        if command.command_type in {
            "runtime.recover_restart",
            "runtime.recover_worker_death",
            "runtime.reconstruct_deliveries",
            "runtime.replay_delivery_hint",
            "runtime.evaluate_timeouts",
            "runtime.list_audit",
            "runtime.worker_deliver",
            "runtime.worker_prepare",
            "runtime.worker_preflight",
            "runtime.worker_preflight_reject",
            "runtime.worker_snapshot",
            "runtime.worker_settle",
            "runtime.worker_cancel_prepare",
            "runtime.worker_cancel_settle",
            "runtime.heartbeat",
            "runtime.result",
            "runtime.show",
        }:
            # Matrix already enforced; grant optional for these verbs.
            pass
        else:
            raise AuthzDeniedError("grant required for runtime commands")

    if ctx.grant is None and command.command_type.startswith("delivery."):
        # Matrix already enforced for delivery verbs.
        pass

    if ctx.grant is None and command.command_type.startswith("script."):
        # Script verbs rely on kind matrix + allowlist; grant optional.
        pass

    if ctx.grant is None and command.command_type.startswith("schedule."):
        # Schedule verbs rely on kind matrix + template constraints; grant optional.
        if command.command_type in FOUNDER_ONLY_COMMANDS:
            pass
        elif (
            command.command_type == "schedule.run_on_demand"
            and ctx.role != PrincipalRole.FOUNDER
        ):
            raise AuthzDeniedError("schedule on-demand run requires founder")

    if ctx.grant is None and command.command_type.startswith("control_plane."):
        if command.command_type == "control_plane.resolve_token":
            pass  # token resolve is authenticated at the HTTP/service layer
        elif ctx.role not in {PrincipalRole.FOUNDER, PrincipalRole.SYSTEM}:
            raise AuthzDeniedError("control_plane admin commands require founder/system role")
    # Org/delegation bootstrap may proceed for authorized hierarchy roles without a
    # resolved grant (chicken-and-egg: profiles must exist before R3 grants).
    if ctx.grant is None and (
        command.command_type.startswith("org.")
        or command.command_type.startswith("delegation.")
    ):
        if ctx.role not in {
            PrincipalRole.FOUNDER,
            PrincipalRole.EXECUTIVE,
            PrincipalRole.MANAGER,
            PrincipalRole.SYSTEM,
        }:
            raise AuthzDeniedError(
                "org/delegation commands require founder/executive/manager role or grant"
            )

    if ctx.grant is not None:
        if not ctx.grant.budget_scope_id.strip():
            raise AuthzDeniedError("explicit acceptance budget_scope_id is required")
        if ctx.principal_id != ctx.grant.principal_id:
            raise AuthzDeniedError("principal does not match grant")
        if ctx.surface not in ctx.grant.surfaces:
            raise AuthzDeniedError(f"surface {ctx.surface} not permitted by grant")

        # R2 compatibility path: SystemTestGrant refuses org/loadout semantics.
        if isinstance(ctx.grant, SystemTestGrant):
            if "loadout_id" in command.payload and command.payload.get("loadout_id"):
                raise AuthzDeniedError(
                    "R2 compatibility grant refuses organization/loadout resolution"
                )
            if "organization_profile_id" in command.payload and command.payload.get(
                "organization_profile_id"
            ):
                raise AuthzDeniedError(
                    "R2 compatibility grant refuses organization/loadout resolution"
                )
            if "organization_id" in command.payload and command.payload.get("organization_id"):
                raise AuthzDeniedError(
                    "R2 compatibility grant refuses organization/loadout resolution"
                )
            if command.command_type.startswith("org.") or command.command_type.startswith(
                "delegation."
            ):
                raise AuthzDeniedError(
                    "R2 compatibility grant cannot authorize org/delegation commands"
                )

        if isinstance(ctx.grant, ResolvedTaskGrant):
            if not ctx.grant.snapshot_id or not ctx.grant.loadout_id:
                raise AuthzDeniedError("R3 grant requires pinned loadout snapshot")
            if not ctx.grant.organization_profile_hash:
                raise AuthzDeniedError("R3 grant requires organization profile hash")
            # Provider identity never grants authority beyond explicit grant providers.
            provider = command.payload.get("provider")
            if provider and provider not in ctx.grant.providers:
                raise AuthzDeniedError("provider not permitted by resolved grant")
