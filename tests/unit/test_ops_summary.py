from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("ORCH_TESTING", "1")
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

pytest.importorskip("django")
pytest.importorskip("rest_framework")

from rest_framework.test import APIClient


@pytest.fixture
def client() -> APIClient:
    import django
    from django.apps import apps
    from django.conf import settings

    os.environ["ORCH_TESTING"] = "1"
    if not settings.configured:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flow_engine.control_plane.settings")
    if not apps.ready:
        django.setup()
    return APIClient()


def test_ops_summary_requires_authentication(client: APIClient) -> None:
    with patch(
        "flow_engine.control_plane.api.authentication.OrchestratorPrincipalAuthentication.authenticate",
        return_value=None,
    ):
        response = client.get("/ops/summary/")
    assert response.status_code in {401, 403}


def test_ops_summary_authenticated_founder_ok(client: APIClient) -> None:
    from flow_engine.control_plane.api.authentication import OrchestratorUser
    from flow_engine.domain.states import PrincipalRole, Surface

    user = OrchestratorUser(
        principal_id="founder-id",
        principal_key="founder",
        kind="founder",
        role=PrincipalRole.FOUNDER,
        display_name="Founder",
        grant=None,
        surfaces=(Surface.REST,),
        capabilities=(),
        raw_token="test-founder",
    )
    mock_health = {"status": "ok", "mode": "inprocess", "schema_version": 8}
    mock_dashboard = {
        "open_gates": [
            {"gate_id": "G-ORCH-LOCAL-CONTROL-PLANE", "status": "open"},
        ],
    }
    with (
        patch(
            "flow_engine.control_plane.api.authentication.OrchestratorPrincipalAuthentication.authenticate",
            return_value=(user, "test-founder"),
        ),
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
    assert body["stack_health"]["schema_version"] == 8
    gate_ids = [g if isinstance(g, str) else g.get("gate_id") for g in body["open_gates"]]
    assert "G-ORCH-LOCAL-CONTROL-PLANE" in gate_ids


def test_ops_summary_degraded_when_coordinator_unavailable(client: APIClient) -> None:
    from flow_engine.control_plane.api.authentication import OrchestratorUser
    from flow_engine.domain.states import PrincipalRole, Surface

    user = OrchestratorUser(
        principal_id="founder-id",
        principal_key="founder",
        kind="founder",
        role=PrincipalRole.FOUNDER,
        display_name="Founder",
        grant=None,
        surfaces=(Surface.REST,),
        capabilities=(),
        raw_token="test-founder",
    )
    with (
        patch(
            "flow_engine.control_plane.api.authentication.OrchestratorPrincipalAuthentication.authenticate",
            return_value=(user, "test-founder"),
        ),
        patch(
            "flow_engine.control_plane.api.ops_urls.get_client",
            side_effect=RuntimeError("coordinator down"),
        ),
        patch(
            "flow_engine.control_plane.api.ops_urls.fetch_dashboard_payload",
            return_value={"error": "coordinator down"},
        ),
        patch(
            "flow_engine.control_plane.api.ops_urls.fetch_schedule_status",
            return_value=None,
        ),
    ):
        response = client.get("/ops/summary/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert "coordinator down" in body["stack_health"]["detail"]
