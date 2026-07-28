"""Runtime workflow API views — thin coordinator adapters."""

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from flow_engine.control_plane.api.permissions import (
    DenyMCPService,
    RequireEndpointCapability,
    RequireSurface,
)
from flow_engine.control_plane.api.serializers import (
    HeartbeatSerializer,
    OperationResponseSerializer,
    ResultSubmitSerializer,
    RuntimePreviewSerializer,
    RuntimeRunControlSerializer,
    RuntimeRunSerializer,
)
from flow_engine.control_plane.api.views_helpers import submit_command
from flow_engine.domain.states import Surface


class RuntimePreviewView(APIView):
    command_type = "runtime.preview"
    permission_classes = [
        IsAuthenticated,
        RequireSurface,
        RequireEndpointCapability,
        DenyMCPService,
    ]

    @extend_schema(request=RuntimePreviewSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = RuntimePreviewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        return submit_command(
            request,
            command_type="runtime.preview",
            target_id=data["work_item_id"],
            payload={
                "work_item_id": data["work_item_id"],
                "provider": data["provider"],
                **data.get("payload", {}),
            },
        )


class RuntimeRunView(APIView):
    command_type = "runtime.run"
    permission_classes = [
        IsAuthenticated,
        RequireSurface,
        RequireEndpointCapability,
        DenyMCPService,
    ]

    @extend_schema(request=RuntimeRunSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = RuntimeRunSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        payload = {
            "work_item_id": data["work_item_id"],
            "provider": data["provider"],
            "invocation_payload": data.get("payload", {}),
        }
        if data.get("delivery_mode") == "async":
            payload["delivery_mode"] = "async"
            response = submit_command(
                request,
                command_type="runtime.run",
                target_id=data["work_item_id"],
                payload={**payload, "async_dispatch": True},
            )
            if response.status_code in {200, 202}:
                body = response.data
                result = (body or {}).get("result") or {}
                dispatched = result.get("dispatched") or {}
                delivery = dispatched.get("delivery") or {}
                job_id = delivery.get("delivery_job_id")
                attempt_id = (dispatched.get("attempt") or {}).get("id") or (
                    (result.get("created") or {}).get("attempt") or {}
                ).get("id")
                if job_id and attempt_id:
                    from flow_engine.workers.dispatch import enqueue_provider_job

                    enqueue_info = enqueue_provider_job(
                        provider=data["provider"],
                        job_id=job_id,
                        attempt_id=attempt_id,
                    )
                    if isinstance(body, dict):
                        body.setdefault("delivery_enqueue", enqueue_info)
                        response.data = body
            return response
        return submit_command(
            request,
            command_type="runtime.run",
            target_id=data["work_item_id"],
            payload=payload,
        )


class RuntimeShowView(APIView):
    command_type = "runtime.show"
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability]

    @extend_schema(responses={202: OperationResponseSerializer})
    def get(self, request, run_id: str):
        return submit_command(
            request,
            command_type="runtime.show",
            target_id=run_id,
            payload={"run_id": run_id},
        )


class RuntimeHeartbeatView(APIView):
    command_type = "runtime.heartbeat"
    required_surface = Surface.WORKER
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability]

    @extend_schema(request=HeartbeatSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = HeartbeatSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        attempt_id = ser.validated_data["attempt_id"]
        return submit_command(
            request,
            command_type="runtime.heartbeat",
            target_id=attempt_id,
            payload={"attempt_id": attempt_id},
            surface=Surface.WORKER,
        )


class RuntimeResultView(APIView):
    command_type = "runtime.result"
    required_surface = Surface.WORKER
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability]

    @extend_schema(request=ResultSubmitSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = ResultSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        return submit_command(
            request,
            command_type="runtime.result",
            target_id=data["attempt_id"],
            payload=dict(data),
            surface=Surface.WORKER,
        )


class RuntimeRecoverView(APIView):
    command_type = "runtime.recover_restart"
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability]

    @extend_schema(responses={202: OperationResponseSerializer})
    def post(self, request):
        return submit_command(request, command_type="runtime.recover_restart", payload={})


class RuntimePauseView(APIView):
    command_type = "runtime.pause"
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability, DenyMCPService]

    @extend_schema(request=RuntimeRunControlSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = RuntimeRunControlSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run_id = ser.validated_data["run_id"]
        return submit_command(
            request,
            command_type="runtime.pause",
            target_id=run_id,
            payload={"run_id": run_id},
        )


class RuntimeResumeView(APIView):
    command_type = "runtime.resume"
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability, DenyMCPService]

    @extend_schema(request=RuntimeRunControlSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = RuntimeRunControlSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run_id = ser.validated_data["run_id"]
        return submit_command(
            request,
            command_type="runtime.resume",
            target_id=run_id,
            payload={"run_id": run_id},
        )


class RuntimeCancelView(APIView):
    command_type = "runtime.cancel"
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability, DenyMCPService]

    @extend_schema(request=RuntimeRunControlSerializer, responses={202: OperationResponseSerializer})
    def post(self, request):
        ser = RuntimeRunControlSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        run_id = ser.validated_data["run_id"]
        return submit_command(
            request,
            command_type="runtime.cancel",
            target_id=run_id,
            payload={"run_id": run_id},
        )


class DeliveryListView(APIView):
    command_type = "delivery.list_eligible"
    required_surface = Surface.WORKER
    permission_classes = [IsAuthenticated, RequireSurface, RequireEndpointCapability]

    @extend_schema(responses={202: OperationResponseSerializer})
    def get(self, request):
        return submit_command(
            request,
            command_type="delivery.list_eligible",
            payload={},
            surface=Surface.WORKER,
        )
