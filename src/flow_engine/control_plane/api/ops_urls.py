"""Read-only ops summary aggregation for the ops console."""

from __future__ import annotations

import json
import os
from pathlib import Path

from django.urls import path
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from flow_engine.control_plane.api.authentication import OrchestratorPrincipalAuthentication
from flow_engine.control_plane.api.ops_aggregate import (
    fetch_dashboard_payload,
    fetch_schedule_status,
)
from flow_engine.control_plane.api.permissions import RequireOpsReadOrFounder
from flow_engine.control_plane.api.views_helpers import get_client

ROOT = Path(__file__).resolve().parents[4]


def _latest_json_summary(glob_pattern: str) -> dict | None:
    base = ROOT / ".tmp"
    if not base.is_dir():
        return None
    candidates = sorted(base.glob(glob_pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None


class OpsSummaryView(APIView):
    authentication_classes = [OrchestratorPrincipalAuthentication]
    permission_classes = [IsAuthenticated, RequireOpsReadOrFounder]

    def get(self, request):
        stack_health: dict = {"status": "unknown"}
        try:
            stack_health = {"status": "ok", **get_client().health()}
        except Exception as exc:
            stack_health = {"status": "degraded", "detail": str(exc)}

        ladder = _latest_json_summary("verification-ladder/*/summary.json")
        delegate_probe = _latest_json_summary("hq-delegate-probe/*/summary.json")
        bridge_probe = _latest_json_summary("hq-orch-bridge/*/summary.json")

        dashboard = fetch_dashboard_payload(request)
        schedule = fetch_schedule_status(request)

        open_gates = dashboard.get("open_gates")
        if not open_gates:
            open_gates = []

        findings = dashboard.get("findings") or {
            "surface": "dashboard-v1",
            "open_count": None,
        }

        status = stack_health.get("status", "unknown")
        if dashboard.get("error"):
            status = "degraded"

        return Response(
            {
                "status": status,
                "stack_health": stack_health,
                "verification_ladder": {
                    "latest_run_id": ladder.get("run_id") if ladder else None,
                    "passed": ladder.get("passed") if ladder else None,
                    "levels": ladder.get("levels") if ladder else None,
                },
                "credit_envelope": {
                    "campaign": "acceptance-campaign-r4",
                    "provider_mode": os.environ.get("ORCH_PROVIDER_MODE", "mock"),
                    "note": "Read-only summary; founder mutations require authenticated DRF paths",
                },
                "hierarchy": dashboard.get("hierarchy"),
                "delegations": dashboard.get("delegations"),
                "queues": dashboard.get("queues"),
                "recent_work": dashboard.get("recent_work"),
                "open_gates": open_gates,
                "recent_audit": dashboard.get("recent_audit"),
                "schedules": schedule,
                "delegate_probe": delegate_probe,
                "bridge_probe": bridge_probe,
                "findings": findings,
                "settings": {
                    "allowed_hosts": os.environ.get("DJANGO_ALLOWED_HOSTS", ""),
                    "provider_mode": os.environ.get("ORCH_PROVIDER_MODE", "mock"),
                    "coordinator_url": os.environ.get("COORDINATOR_URL", ""),
                },
            }
        )


urlpatterns = [
    path("", OpsSummaryView.as_view(), name="ops-summary"),
]
