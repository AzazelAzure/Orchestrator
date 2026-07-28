"""Token/header authentication resolving server-side principals."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from flow_engine.control_plane.api.views_helpers import get_client
from flow_engine.coordinator.commands import CommandContext, RuntimeCommand
from flow_engine.domain.states import PrincipalRole, Surface


@dataclass
class OrchestratorUser:
    """DRF user backed by resolved control-plane principal."""

    principal_id: str
    principal_key: str
    kind: str
    role: PrincipalRole
    display_name: str
    grant: dict[str, Any] | None
    surfaces: tuple[Surface, ...]
    capabilities: tuple[str, ...] = ()
    raw_token: str = ""
    is_authenticated: bool = True
    is_active: bool = True

    @property
    def pk(self) -> str:
        return self.principal_id


class OrchestratorPrincipalAuthentication(BaseAuthentication):
    """Accept Bearer token or X-Orchestrator-Token. No fixed dev-token fallback."""

    keyword = "Bearer"
    token_header = "HTTP_X_ORCHESTRATOR_TOKEN"

    def authenticate(self, request):
        token = self._extract_token(request)
        if not token:
            return None

        client = get_client()
        envelope = client.accept(
            RuntimeCommand(
                command_type="control_plane.resolve_token",
                target_id=None,
                payload={"raw_token": token},
                context=CommandContext(
                    principal_id="auth-resolver",
                    role=PrincipalRole.SYSTEM,
                    surface=Surface.REST,
                ),
            )
        )
        if envelope.get("status") == "rejected":
            raise AuthenticationFailed(envelope.get("error", "authentication failed"))

        result = envelope.get("result") or {}
        principal = result.get("principal") or {}
        grant = result.get("grant")
        if principal.get("status") == "revoked":
            raise AuthenticationFailed("principal revoked")

        caps = tuple(principal.get("capabilities") or [])
        if grant and grant.get("capabilities"):
            caps = tuple(grant.get("capabilities") or caps)

        user = OrchestratorUser(
            principal_id=principal["principal_id"],
            principal_key=principal["principal_key"],
            kind=principal["kind"],
            role=PrincipalRole(principal["role"]),
            display_name=principal["display_name"],
            grant=grant,
            surfaces=tuple(Surface(s) for s in principal.get("surfaces", ["rest"])),
            capabilities=caps,
            raw_token=token,
        )
        return (user, token)

    def _extract_token(self, request) -> str | None:
        auth = request.META.get("HTTP_AUTHORIZATION", "")
        if auth.startswith(f"{self.keyword} "):
            return auth[len(self.keyword) + 1 :].strip()
        token = request.META.get(self.token_header)
        if token:
            return token.strip()
        return None
