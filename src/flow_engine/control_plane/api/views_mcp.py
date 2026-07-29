"""DRF views for capability-scoped MCP lanes (R4B)."""

from __future__ import annotations

from typing import Any

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from flow_engine.control_plane.api.authentication import OrchestratorUser
from flow_engine.control_plane.api.mcp_service_auth import (
    resolve_mcp_service_principal,
    service_principal_dict,
)
from flow_engine.control_plane.api.permissions import RequireEndpointCapability, RequireSurface
from flow_engine.control_plane.api.serializers import (
    McpInvokeSerializer,
    McpToolResultSerializer,
    OperationResponseSerializer,
)
from flow_engine.control_plane.api.views_helpers import build_context, get_client
from flow_engine.control_plane.authz_matrix import assert_command_allowed_for_kind
from flow_engine.control_plane.errors import http_status_for_envelope
from flow_engine.coordinator.commands import RuntimeCommand
from flow_engine.domain.errors import (
    AuthRequiredError,
    AuthzDeniedError,
    FlowError,
    NotFoundError,
    StaleAssetError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.domain.states import Surface
from flow_engine.mcp_lanes.authz import assert_mcp_invoke_allowed
from flow_engine.mcp_lanes.catalog import LANE_IDS
from flow_engine.mcp_lanes.handlers import (
    delegation_command_for_tool,
    describe_lane,
    handle_read_tool,
    script_command_for_tool,
    workflow_command_for_tool,
)
from flow_engine.mcp_lanes.profiles import profiles_with_snapshots
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot, parse_snapshot_digest


class RequireMcpDualPrincipal(BasePermission):
    """Require authenticated initiating principal plus lane MCP service principal."""

    def has_permission(self, request, view) -> bool:
        user = request.user
        if not isinstance(user, OrchestratorUser):
            return False
        if user.kind == "mcp_service":
            return False
        try:
            service = resolve_mcp_service_principal(request)
        except (AuthRequiredError, AuthzDeniedError):
            return False
        lane_id = getattr(view, "lane_id_from_kwargs", None)
        if callable(lane_id):
            expected = lane_id(view.kwargs)
        else:
            expected = view.kwargs.get("lane_id")
        if expected and service.lane_id != expected:
            return False
        request.mcp_service_principal = service  # type: ignore[attr-defined]
        return True


def _error_response(exc: Exception) -> Response:
    if isinstance(exc, AuthRequiredError):
        code, status = "AUTH_REQUIRED", 401
    elif isinstance(exc, UnsupportedSurfaceError):
        code, status = "UNSUPPORTED_SURFACE", 403
    elif isinstance(exc, AuthzDeniedError):
        code, status = "AUTHZ_DENIED", 403
    elif isinstance(exc, NotFoundError):
        code, status = "NOT_FOUND", 404
    elif isinstance(exc, StaleAssetError):
        code, status = "STALE_ASSET", 409
    elif isinstance(exc, ValidationFailedError):
        code, status = "VALIDATION_FAILED", 400
    elif isinstance(exc, FlowError):
        code, status = getattr(exc, "code", "FLOW_ERROR"), 409
    else:
        code, status = "VALIDATION_FAILED", 400
    return Response(
        {
            "status": "rejected",
            "error_code": code,
            "error": str(exc),
        },
        status=status,
    )


class McpLaneSnapshotView(APIView):
    command_type = "mcp.snapshot.get"
    required_surface = Surface.REST
    permission_classes = [
        IsAuthenticated,
        RequireSurface,
        RequireEndpointCapability,
        RequireMcpDualPrincipal,
    ]

    @extend_schema(responses={200: McpToolResultSerializer})
    def get(self, request, lane_id: str):
        try:
            if lane_id not in LANE_IDS:
                raise NotFoundError(f"unknown lane: {lane_id}")
            service = request.mcp_service_principal  # type: ignore[attr-defined]
            if service.lane_id != lane_id:
                raise AuthzDeniedError("cross-lane snapshot denied")
            body = {
                "status": "ok",
                "lane": describe_lane(lane_id),
                "initiating_principal_id": request.user.principal_id,
                "mcp_service_principal": service_principal_dict(service),
            }
            return Response(body, status=200)
        except Exception as exc:  # noqa: BLE001 — mapped below
            return _error_response(exc)


class McpLaneToolsView(APIView):
    command_type = "mcp.tools.list"
    required_surface = Surface.REST
    permission_classes = [
        IsAuthenticated,
        RequireSurface,
        RequireEndpointCapability,
        RequireMcpDualPrincipal,
    ]

    @extend_schema(responses={200: McpToolResultSerializer})
    def get(self, request, lane_id: str):
        try:
            if lane_id not in LANE_IDS:
                raise NotFoundError(f"unknown lane: {lane_id}")
            service = request.mcp_service_principal  # type: ignore[attr-defined]
            if service.lane_id != lane_id:
                raise AuthzDeniedError("cross-lane tools list denied")
            snapshot = lane_tool_snapshot(lane_id)
            return Response(
                {
                    "status": "ok",
                    "lane_id": lane_id,
                    "tools": snapshot["tools"],
                    "snapshot_digest": snapshot["snapshot_digest"],
                    "initiating_principal_id": request.user.principal_id,
                    "mcp_service_principal": service_principal_dict(service),
                },
                status=200,
            )
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)


class McpLaneInvokeView(APIView):
    command_type = "mcp.tool.invoke"
    required_surface = Surface.REST
    permission_classes = [
        IsAuthenticated,
        RequireSurface,
        RequireEndpointCapability,
        RequireMcpDualPrincipal,
    ]

    @extend_schema(
        request=McpInvokeSerializer,
        responses={200: McpToolResultSerializer, 202: OperationResponseSerializer},
    )
    def post(self, request, lane_id: str):
        try:
            if lane_id not in LANE_IDS:
                raise NotFoundError(f"unknown lane: {lane_id}")
            ser = McpInvokeSerializer(data=request.data)
            ser.is_valid(raise_exception=True)
            data = ser.validated_data
            tool_name = data["tool"]
            service = request.mcp_service_principal  # type: ignore[attr-defined]
            user: OrchestratorUser = request.user  # type: ignore[assignment]

            loadout_id = data.get("loadout_id")
            department = data.get("department")
            if user.grant and user.grant.get("loadout_id") and not loadout_id:
                loadout_id = user.grant.get("loadout_id")

            snapshot = assert_mcp_invoke_allowed(
                lane_id=lane_id,
                tool_name=tool_name,
                service_principal_key=service.principal_key,
                initiating_principal_kind=user.kind,
                initiating_principal_id=user.principal_id,
                service_principal_id=service.principal_id,
                expected_snapshot_digest=parse_snapshot_digest(
                    data.get("expected_snapshot_digest")
                ),
                loadout_id=loadout_id,
                department=department,
            )

            initiating = {
                "principal_id": user.principal_id,
                "principal_key": user.principal_key,
                "kind": user.kind,
            }
            service_dict = service_principal_dict(service)

            command_type = (
                workflow_command_for_tool(tool_name)
                or delegation_command_for_tool(tool_name, data.get("arguments") or {})
                or script_command_for_tool(tool_name)
            )
            if command_type:
                return self._dispatch_workflow(
                    request,
                    command_type=command_type,
                    tool_name=tool_name,
                    arguments=data.get("arguments") or {},
                    snapshot=snapshot,
                    initiating=initiating,
                    service_dict=service_dict,
                )

            result = handle_read_tool(
                lane_id=lane_id,
                tool_name=tool_name,
                arguments=data.get("arguments") or {},
                initiating_principal=initiating,
                service_principal=service_dict,
            )
            result["snapshot_digest"] = snapshot["snapshot_digest"]
            return Response(result, status=200)
        except Exception as exc:  # noqa: BLE001
            return _error_response(exc)

    def _dispatch_workflow(
        self,
        request,
        *,
        command_type: str,
        tool_name: str,
        arguments: dict[str, Any],
        snapshot: dict[str, Any],
        initiating: dict[str, Any],
        service_dict: dict[str, Any],
    ) -> Response:
        user: OrchestratorUser = request.user  # type: ignore[assignment]
        assert_command_allowed_for_kind(
            command_type=command_type,
            principal_kind=user.kind,
            capabilities=user.capabilities,
        )
        payload = dict(arguments)
        for banned in (
            "principal_id",
            "role",
            "grant",
            "step_up",
            "capabilities",
            "worker_principal_id",
            "mcp_lane_id",
            "mcp_service_principal_id",
            "mcp_service_principal_key",
            "initiating_principal_id",
            "mcp_snapshot_digest",
            "tool_snapshot_digest",
            "mcp_tool_name",
            "mcp_tool_snapshot_digest",
            "workspace_root",
            "override_argv",
            "override_cwd",
            "inject_env",
            "force_timeout",
            "simulate_network",
            "cwd",
            "argv",
            "env",
        ):
            payload.pop(banned, None)

        # MCP identity lives only on CommandContext — never payload audit fields.
        target_id = (
            payload.get("work_item_id")
            or payload.get("run_id")
            or payload.get("target_id")
            or payload.get("script_id")
        )
        context = build_context(request, surface=Surface.MCP, command_type=command_type)
        from dataclasses import replace

        context = replace(
            context,
            mcp_service_principal_id=service_dict["principal_id"],
            mcp_lane_id=snapshot["lane_id"],
            mcp_tool_snapshot_digest=snapshot["snapshot_digest"],
            mcp_tool_name=tool_name,
        )
        command = RuntimeCommand(
            command_type=command_type,
            target_id=target_id,
            payload=payload,
            idempotency_key=request.headers.get("Idempotency-Key"),
            context=context,
        )
        envelope = get_client().accept(
            command,
            principal_token=getattr(user, "raw_token", None) or None,
        )
        if isinstance(envelope, dict):
            envelope = {
                **envelope,
                "mcp": {
                    "lane_id": snapshot["lane_id"],
                    "tool_snapshot_digest": snapshot["snapshot_digest"],
                    "initiating_principal_id": initiating["principal_id"],
                    "mcp_service_principal_id": service_dict["principal_id"],
                },
            }
        status = http_status_for_envelope(envelope)
        return Response(envelope, status=status)


class McpDepartmentProfilesView(APIView):
    """Admin/Ops, QA, Tech capability profiles from locked loadouts (read)."""

    command_type = "mcp.profiles.list"
    required_surface = Surface.REST
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability]

    @extend_schema(responses={200: McpToolResultSerializer})
    def get(self, request):
        user: OrchestratorUser = request.user  # type: ignore[assignment]
        return Response(
            {
                "status": "ok",
                "profiles": profiles_with_snapshots(),
                "initiating_principal_id": user.principal_id,
                "authority_note": (
                    "Profiles narrow to locked catalog membership; "
                    "they do not multiply authority."
                ),
            },
            status=200,
        )
