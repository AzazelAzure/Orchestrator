"""R4C scripts and schedules DRF views."""

from __future__ import annotations

import os

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from flow_engine.control_plane.api.permissions import (
    DenyMCPService,
    RequireEndpointCapability,
    RequireSurface,
)
from flow_engine.control_plane.api.serializers import (
    ScheduleCompleteSerializer,
    ScheduleTickSerializer,
    ScriptCancelSerializer,
    ScriptExecuteSerializer,
)
from flow_engine.control_plane.api.views_helpers import submit_command
from flow_engine.domain.states import Surface

_BASE_PERMS = [IsAuthenticated, RequireSurface, RequireEndpointCapability, DenyMCPService]


class ScriptAllowlistView(APIView):
    command_type = "script.list_allowlist"
    permission_classes = _BASE_PERMS

    def get(self, request: Request) -> Response:
        return submit_command(request, command_type="script.list_allowlist", payload={})


class ScriptExecuteView(APIView):
    """Register allowlisted script; enqueue script-worker (or in-process test path)."""

    command_type = "script.register"
    permission_classes = _BASE_PERMS

    def post(self, request: Request) -> Response:
        ser = ScriptExecuteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        # Strip any smuggled test-hook keys from raw body.
        for banned in (
            "workspace_root",
            "simulate_network",
            "force_timeout",
            "inject_env",
            "override_argv",
            "override_cwd",
        ):
            data.pop(banned, None)
        register = submit_command(
            request,
            command_type="script.register",
            target_id=data["script_id"],
            payload=data,
            idempotency_key=data.get("idempotency_key"),
        )
        if register.status_code >= 400:
            return register
        envelope = register.data
        if envelope.get("status") == "rejected":
            return register
        execution = (envelope.get("result") or {}).get("execution") or {}
        execution_id = execution.get("id")
        if not execution_id:
            return register

        # Prefer Celery script-worker; ORCH_TESTING may run three-phase in-process.
        if os.environ.get("CELERY_BROKER_URL") and os.environ.get("ORCH_TESTING") != "1":
            from flow_engine.workers.tasks import execute_registered_script

            execute_registered_script.delay(execution_id=execution_id)
            return Response(
                {
                    **envelope,
                    "result": {
                        **(envelope.get("result") or {}),
                        "queued": True,
                        "queue": "script-sandbox",
                    },
                },
                status=202,
            )

        return submit_command(
            request,
            command_type="script.execute",
            target_id=execution_id,
            payload={"execution_id": execution_id},
            idempotency_key=f"exec|{execution_id}",
        )


class ScriptShowView(APIView):
    command_type = "script.show"
    permission_classes = _BASE_PERMS

    def get(self, request: Request, execution_id: str) -> Response:
        return submit_command(
            request,
            command_type="script.show",
            target_id=execution_id,
            payload={"execution_id": execution_id},
        )


class ScriptCancelView(APIView):
    command_type = "script.cancel"
    permission_classes = _BASE_PERMS

    def post(self, request: Request) -> Response:
        ser = ScriptCancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        return submit_command(
            request,
            command_type="script.cancel",
            target_id=data["execution_id"],
            payload=data,
        )


class ScheduleTemplatesView(APIView):
    command_type = "schedule.list_templates"
    permission_classes = _BASE_PERMS

    def get(self, request: Request) -> Response:
        return submit_command(
            request, command_type="schedule.list_templates", payload={}
        )


class ScheduleStatusView(APIView):
    command_type = "schedule.status"
    permission_classes = _BASE_PERMS

    def get(self, request: Request) -> Response:
        return submit_command(request, command_type="schedule.status", payload={})


class ScheduleTickView(APIView):
    command_type = "schedule.tick"
    permission_classes = _BASE_PERMS

    def post(self, request: Request) -> Response:
        ser = ScheduleTickSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        surface = (
            Surface.SCHEDULE
            if getattr(request.user, "kind", None) == "scheduler"
            else Surface.REST
        )
        return submit_command(
            request,
            command_type="schedule.tick",
            target_id=data["schedule_id"],
            payload=data,
            surface=surface,
            idempotency_key=data.get("idempotency_key")
            or f"tick|{data['schedule_id']}|{data['planned_time']}",
        )


class ScheduleCompleteView(APIView):
    command_type = "schedule.complete"
    permission_classes = _BASE_PERMS

    def post(self, request: Request) -> Response:
        ser = ScheduleCompleteSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        surface = (
            Surface.SCHEDULE
            if getattr(request.user, "kind", None) == "scheduler"
            else Surface.REST
        )
        return submit_command(
            request,
            command_type="schedule.complete",
            target_id=data["run_id"],
            payload=data,
            surface=surface,
        )


class ScheduleRunOnDemandView(APIView):
    command_type = "schedule.run_on_demand"
    permission_classes = _BASE_PERMS

    def post(self, request: Request) -> Response:
        ser = ScheduleTickSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = dict(ser.validated_data)
        return submit_command(
            request,
            command_type="schedule.run_on_demand",
            target_id=data["schedule_id"],
            payload=data,
            idempotency_key=data.get("idempotency_key")
            or f"ondemand|{data['schedule_id']}|{data['planned_time']}",
        )
