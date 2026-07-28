"""Resolve MCP service principal from dedicated header (dual-principal)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rest_framework.request import Request

from flow_engine.control_plane.api.views_helpers import get_client
from flow_engine.coordinator.commands import CommandContext, RuntimeCommand
from flow_engine.domain.errors import AuthRequiredError, AuthzDeniedError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.mcp_lanes.catalog import lane_id_from_principal_key


@dataclass(frozen=True)
class McpServicePrincipal:
    principal_id: str
    principal_key: str
    kind: str
    lane_id: str
    capabilities: tuple[str, ...] = ()


def extract_mcp_service_token(request: Request) -> str | None:
    token = request.META.get("HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN")
    if token:
        return token.strip()
    return None


def extract_mcp_lane_header(request: Request) -> str | None:
    value = request.META.get("HTTP_X_ORCHESTRATOR_MCP_LANE_ID")
    if value:
        return value.strip()
    return None


def resolve_mcp_service_principal(request: Request) -> McpServicePrincipal:
    token = extract_mcp_service_token(request)
    if not token:
        raise AuthRequiredError("X-Orchestrator-MCP-Service-Token required")

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
        raise AuthRequiredError(envelope.get("error") or "MCP service authentication failed")

    result = envelope.get("result") or {}
    principal = result.get("principal") or {}
    if principal.get("status") == "revoked":
        raise AuthRequiredError("MCP service principal revoked")
    if principal.get("kind") != "mcp_service":
        raise AuthzDeniedError("MCP service token must resolve to kind mcp_service")

    principal_key = str(principal.get("principal_key") or "")
    lane_id = lane_id_from_principal_key(principal_key)
    if not lane_id:
        # Legacy generic mcp-service is not a lane identity.
        raise AuthzDeniedError(
            f"MCP service principal {principal_key} is not a lane-scoped identity"
        )

    header_lane = extract_mcp_lane_header(request)
    if header_lane and header_lane != lane_id:
        raise AuthzDeniedError(
            f"X-Orchestrator-MCP-Lane-Id {header_lane} does not match service {lane_id}"
        )

    caps = tuple(principal.get("capabilities") or [])
    return McpServicePrincipal(
        principal_id=str(principal["principal_id"]),
        principal_key=principal_key,
        kind="mcp_service",
        lane_id=lane_id,
        capabilities=caps,
    )


def service_principal_dict(service: McpServicePrincipal) -> dict[str, Any]:
    return {
        "principal_id": service.principal_id,
        "principal_key": service.principal_key,
        "kind": service.kind,
        "lane_id": service.lane_id,
        "capabilities": list(service.capabilities),
    }
