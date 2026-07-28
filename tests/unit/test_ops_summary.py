from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    return APIClient()


def test_ops_summary_read_only_no_auth(client: APIClient) -> None:
    mock_health = {"status": "ok", "mode": "inprocess", "schema_version": 7}
    mock_dashboard = {
        "open_gates": [
            {"gate_id": "G-ORCH-LOCAL-CONTROL-PLANE", "status": "open"},
        ],
    }
    with (
        patch(
            "flow_engine.control_plane.api.ops_urls.get_client",
            return_value=MagicMock(health=MagicMock(return_value=mock_health)),
        ),
        patch(
            "flow_engine.control_plane.api.ops_urls.fetch_dashboard_payload",
            return_value=mock_dashboard,
        ),
        patch(
            "flow_engine.control_plane.api.ops_urls.fetch_schedule_status",
            return_value=None,
        ),
    ):
        response = client.get("/ops/summary/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["stack_health"]["schema_version"] == 7
    gate_ids = [
        g if isinstance(g, str) else g.get("gate_id") for g in body["open_gates"]
    ]
    assert "G-ORCH-LOCAL-CONTROL-PLANE" in gate_ids


def test_ops_summary_degraded_when_coordinator_unavailable(client: APIClient) -> None:
    with patch(
        "flow_engine.control_plane.api.ops_urls.get_client",
        side_effect=RuntimeError("coordinator down"),
    ):
        response = client.get("/ops/summary/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert "coordinator down" in body["stack_health"]["detail"]
