"""Read-only ops summary aggregation for the thin ops console."""

from __future__ import annotations

import json
from pathlib import Path

from django.urls import path
from rest_framework.response import Response
from rest_framework.views import APIView

from flow_engine.control_plane.api.views_helpers import get_client

ROOT = Path(__file__).resolve().parents[4]


def _latest_json_summary(glob_pattern: str) -> dict | None:
    base = ROOT / ".tmp"
    if not base.is_dir():
        return None
    candidates = sorted(base.glob(glob_pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return None


class OpsSummaryView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request):
        stack_health: dict = {"status": "unknown"}
        try:
            stack_health = {"status": "ok", **get_client().health()}
        except Exception as exc:
            stack_health = {"status": "degraded", "detail": str(exc)}

        ladder = _latest_json_summary("verification-ladder/*/summary.json")
        delegate_probe = _latest_json_summary("hq-delegate-probe/*/summary.json")

        open_gates = [
            "G-ORCH-LOCAL-CONTROL-PLANE",
            "G-ORCH-PROOF-GENERIC",
            "G-ORCH-PROOF-PORTFOLIO",
            "G-ORCH-VPS-LIVE",
            "G-ORCH-HOSTED-READY",
        ]

        return Response(
            {
                "status": stack_health.get("status", "unknown"),
                "stack_health": stack_health,
                "verification_ladder": {
                    "latest_run_id": ladder.get("run_id") if ladder else None,
                    "passed": ladder.get("passed") if ladder else None,
                    "levels": ladder.get("levels") if ladder else None,
                },
                "credit_envelope": {
                    "campaign": "acceptance-campaign-r4",
                    "note": "Read-only summary; founder mutations require authenticated DRF paths",
                },
                "open_gates": open_gates,
                "delegate_probe": delegate_probe,
                "findings": {
                    "surface": "dashboard-only-v1",
                    "open_count": None,
                    "note": "External alerting deferred; ops console shows ladder and gate status",
                },
            }
        )


urlpatterns = [
    path("", OpsSummaryView.as_view(), name="ops-summary"),
]
