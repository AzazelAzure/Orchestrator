"""JSON auth endpoints: register, login, refresh, logout, me, PAT."""

from __future__ import annotations

from typing import Any

from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from flow_engine.control_plane.api.authentication import OrchestratorUser
from flow_engine.control_plane.api.views_helpers import get_client, submit_command
from flow_engine.control_plane.errors import http_status_for_envelope
from flow_engine.control_plane.user_auth import registration_allowed
from flow_engine.coordinator.commands import CommandContext, RuntimeCommand
from flow_engine.domain.states import PrincipalRole, Surface


def _client_ip(request: Request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.META.get("REMOTE_ADDR")


def _system_auth_command(
    command_type: str,
    payload: dict[str, Any],
) -> Response:
    envelope = get_client().accept(
        RuntimeCommand(
            command_type=command_type,
            target_id=None,
            payload=payload,
            context=CommandContext(
                principal_id="auth-resolver",
                role=PrincipalRole.SYSTEM,
                surface=Surface.REST,
            ),
        )
    )
    return Response(envelope, status=http_status_for_envelope(envelope))


class AuthRegisterView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        payload = {
            "username": body.get("username", ""),
            "password": body.get("password", ""),
            "display_name": body.get("display_name"),
        }
        # Bearer present: coordinator resolves founder authority via principal_token only.
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
            if token:
                envelope = get_client().accept(
                    RuntimeCommand(
                        command_type="auth.register_user",
                        target_id=None,
                        payload=payload,
                        context=CommandContext(
                            principal_id="auth-resolver",
                            role=PrincipalRole.SYSTEM,
                            surface=Surface.REST,
                        ),
                    ),
                    principal_token=token,
                )
                return Response(envelope, status=http_status_for_envelope(envelope))
        if not registration_allowed():
            return Response(
                {
                    "status": "rejected",
                    "error_code": "AUTHZ_DENIED",
                    "error": "user registration is disabled",
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        return _system_auth_command("auth.register_user", payload)


class AuthLoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        return _system_auth_command(
            "auth.login",
            {
                "username": body.get("username", ""),
                "password": body.get("password", ""),
                "client_ip": _client_ip(request),
            },
        )


class AuthRefreshView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        return _system_auth_command(
            "auth.refresh",
            {"refresh_token": body.get("refresh_token", "")},
        )


class AuthLogoutView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        raw = body.get("token") or body.get("refresh_token") or ""
        if not raw:
            auth = request.META.get("HTTP_AUTHORIZATION", "")
            if auth.startswith("Bearer "):
                raw = auth[7:].strip()
        return _system_auth_command("auth.logout", {"raw_token": raw})


class AuthMeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user: OrchestratorUser = request.user  # type: ignore[assignment]
        return Response(
            {
                "principal_id": user.principal_id,
                "principal_key": user.principal_key,
                "kind": user.kind,
                "role": str(user.role),
                "display_name": user.display_name,
                "capabilities": list(user.capabilities),
                "surfaces": [str(s) for s in user.surfaces],
            }
        )


class AuthTokenView(APIView):
    """Issue a personal access token (PAT) for the authenticated principal."""

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        body = request.data if isinstance(request.data, dict) else {}
        return submit_command(
            request,
            command_type="auth.issue_pat",
            payload={
                "label": body.get("label", ""),
                "scopes": body.get("scopes") or [],
            },
        )


class AuthTokenRevokeView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request: Request, credential_id: str) -> Response:
        return submit_command(
            request,
            command_type="auth.revoke_credential",
            payload={"credential_id": credential_id},
        )
