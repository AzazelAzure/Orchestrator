"""DRF permissions enforcing fail-closed authz matrix."""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from flow_engine.control_plane.api.authentication import OrchestratorUser
from flow_engine.control_plane.authz_matrix import assert_command_allowed_for_kind
from flow_engine.domain.errors import AuthzDeniedError
from flow_engine.domain.states import PrincipalRole, Surface


class RequireSurface(BasePermission):
    surface = Surface.REST

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not isinstance(user, OrchestratorUser):
            return False
        required = getattr(view, "required_surface", self.surface)
        return required in user.surfaces


class RequireFounder(BasePermission):
    def has_permission(self, request, view) -> bool:
        user = request.user
        return isinstance(user, OrchestratorUser) and user.role == PrincipalRole.FOUNDER


class RequireEndpointCapability(BasePermission):
    """Enforce exact endpoint → principal-kind matrix (deny by default)."""

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not isinstance(user, OrchestratorUser):
            return False
        command_type = getattr(view, "command_type", None)
        if not command_type:
            return False
        try:
            assert_command_allowed_for_kind(
                command_type=command_type,
                principal_kind=user.kind,
                capabilities=user.grant.get("capabilities", []) if user.grant else (),
            )
        except AuthzDeniedError:
            return False
        # MCP/scheduler never get founder-only or recovery via REST unless capability.
        if user.kind in {"mcp_service", "scheduler"}:
            if command_type.startswith("runtime.recover") or command_type in {
                "runtime.new_attempt_after_unknown",
                "runtime.waive_gate",
                "runtime.hitm_exception",
                "delivery.recover_stale",
            }:
                caps = (
                    set(user.grant.get("capabilities") or [])
                    if user.grant
                    else set()
                )
                if user.kind == "mcp_service" and "recovery.control_plane" not in caps:
                    return False
                if user.kind == "scheduler" and "recovery.control_plane" not in caps:
                    return False
        return True


class DenyMCPService(BasePermission):
    """MCP-service principals may not invoke founder-only REST endpoints."""

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not isinstance(user, OrchestratorUser):
            return False
        if user.kind == "mcp_service" and getattr(view, "founder_only", False):
            return False
        return True
