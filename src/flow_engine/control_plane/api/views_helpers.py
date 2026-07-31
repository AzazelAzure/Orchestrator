"""Shared view helpers."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from django.conf import settings
from rest_framework.request import Request
from rest_framework.response import Response

from flow_engine.control_plane.authz_matrix import assert_command_allowed_for_kind
from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.control_plane.errors import http_status_for_envelope
from flow_engine.control_plane.principal_registry import local_budget_scope_id
from flow_engine.coordinator.commands import (
    CommandContext,
    ResolvedTaskGrant,
    RuntimeCommand,
    SystemTestGrant,
)
from flow_engine.domain.states import Surface

if TYPE_CHECKING:
    from flow_engine.control_plane.api.authentication import OrchestratorUser

_inprocess_client: CoordinatorClient | None = None


def _local_budget_scope() -> str:
    return local_budget_scope_id()


def set_inprocess_client(client: CoordinatorClient | None) -> None:
    global _inprocess_client
    _inprocess_client = client


def get_client() -> CoordinatorClient:
    if _inprocess_client is not None:
        return _inprocess_client
    if os.environ.get("COORDINATOR_INPROCESS") == "1":
        raise RuntimeError("COORDINATOR_INPROCESS=1 but in-process client not initialized")
    return CoordinatorClient(
        base_url=getattr(settings, "COORDINATOR_URL", None),
        service_kind="api",
    )


def build_context(
    request: Request,
    *,
    surface: Surface = Surface.REST,
    command_type: str | None = None,
) -> CommandContext:
    """Build command context from server-resolved principal only.

    Caller-supplied role/grant/principal fields in the request body are ignored.
    Org/delegation commands use role authority without R2 compatibility grants.
    """
    user: OrchestratorUser = request.user  # type: ignore[assignment]
    grant = None
    hierarchy_without_grant = bool(
        command_type and (command_type.startswith("delegation.") or command_type.startswith("org."))
    )
    if user.grant:
        if user.grant.get("compatibility_mode") == "r3_resolved":
            grant = ResolvedTaskGrant(
                grant_id=user.grant["grant_id"],
                principal_id=user.grant["principal_id"],
                role=user.role,
                surfaces=tuple(Surface(s) for s in user.grant.get("surfaces", ["rest"])),
                providers=tuple(user.grant.get("providers") or []),
                budget_scope_id=user.grant["budget_scope_id"],
                organization_id=user.grant["organization_id"],
                organization_profile_hash=user.grant.get("organization_profile_hash", ""),
                loadout_id=user.grant["loadout_id"],
                snapshot_id=user.grant["snapshot_id"],
                assignment_id=user.grant.get("assignment_id", ""),
                capabilities=tuple(user.grant.get("capabilities") or ()),
                policy_revision=user.grant.get("policy_revision", "r3-default"),
                effect_ceiling=user.grant.get("effect_ceiling", ""),
            )
        else:
            grant = SystemTestGrant(
                grant_id=user.grant["grant_id"],
                principal_id=user.grant["principal_id"],
                role=user.role,
                surfaces=tuple(Surface(s) for s in user.grant.get("surfaces", ["rest"])),
                providers=tuple(user.grant.get("providers") or []),
                budget_scope_id=user.grant["budget_scope_id"],
                capabilities=tuple(user.grant.get("capabilities") or ()),
                policy_revision=user.grant.get("policy_revision", "r4-local"),
                compatibility_mode=user.grant.get("compatibility_mode", "r2_system_test"),
            )
    elif user.kind == "founder":
        if not hierarchy_without_grant:
            grant = SystemTestGrant(
                grant_id=f"api-grant-{user.principal_key}",
                principal_id=user.principal_id,
                role=user.role,
                surfaces=user.surfaces,
                providers=("codex", "cursor", "claude"),
                budget_scope_id=_local_budget_scope(),
                policy_revision="r4-local",
                capabilities=user.capabilities,
            )
    elif user.kind == "worker":
        grant = SystemTestGrant(
            grant_id=f"api-grant-{user.principal_key}",
            principal_id=user.principal_id,
            role=user.role,
            surfaces=user.surfaces,
            providers=("codex", "cursor", "claude"),
            budget_scope_id=_local_budget_scope(),
            policy_revision="r4-local",
            capabilities=user.capabilities,
        )
    elif user.kind == "scheduler":
        grant = SystemTestGrant(
            grant_id=f"api-grant-{user.principal_key}",
            principal_id=user.principal_id,
            role=user.role,
            surfaces=user.surfaces,
            providers=(),
            budget_scope_id="schedule-zero-provider-budget",
            policy_revision="r4-local",
            capabilities=user.capabilities,
        )
    elif user.kind == "human":
        grant = SystemTestGrant(
            grant_id=f"api-grant-{user.principal_key}",
            principal_id=user.principal_id,
            role=user.role,
            surfaces=user.surfaces,
            providers=(),
            budget_scope_id=_local_budget_scope(),
            policy_revision="r4-local",
            capabilities=user.capabilities,
        )
    if hierarchy_without_grant and not (
        user.grant and user.grant.get("compatibility_mode") == "r3_resolved"
    ):
        grant = None
    return CommandContext(
        principal_id=user.principal_id,
        role=user.role,
        surface=surface,
        grant=grant,
        # Never take authority from the request body.
        expected_revision=None,
    )


def submit_command(
    request: Request,
    *,
    command_type: str,
    target_id: str | None = None,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    surface: Surface = Surface.REST,
) -> Response:
    user: OrchestratorUser = request.user  # type: ignore[assignment]
    assert_command_allowed_for_kind(
        command_type=command_type,
        principal_kind=user.kind,
        capabilities=user.capabilities,
    )
    # Strip any caller-supplied authority keys from payload.
    clean_payload = dict(payload or {})
    for banned in (
        "principal_id",
        "role",
        "grant",
        "step_up",
        "capabilities",
        "worker_principal_id",
        "founder_authorized",
        "allow_registration",
        # R4C: never accept caller-controlled script path/test hooks via any surface.
        "workspace_root",
        "simulate_network",
        "force_timeout",
        "inject_env",
        "override_argv",
        "override_cwd",
    ):
        clean_payload.pop(banned, None)

    command = RuntimeCommand(
        command_type=command_type,
        target_id=target_id or clean_payload.get("target_id"),
        payload=clean_payload,
        idempotency_key=idempotency_key or request.headers.get("Idempotency-Key"),
        context=build_context(request, surface=surface),
    )
    envelope = get_client().accept(
        command,
        principal_token=getattr(user, "raw_token", None) or None,
    )
    status = http_status_for_envelope(envelope)
    return Response(envelope, status=status)
