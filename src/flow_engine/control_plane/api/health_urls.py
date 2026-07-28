"""Health endpoints (no auth)."""

from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

from flow_engine.control_plane.api.views_helpers import get_client


class HealthView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        try:
            coord_health = get_client().health()
            return Response({"status": "ok", "api": "orchestrator-control-plane", **coord_health})
        except Exception as exc:
            return Response({"status": "degraded", "detail": str(exc)}, status=503)


urlpatterns = [
    path("", HealthView.as_view(), name="health"),
]
