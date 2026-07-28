"""R4 API authn/authz matrix and OpenAPI exposure."""

from __future__ import annotations

import os

import pytest

os.environ["ORCH_TESTING"] = "1"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

pytest.importorskip("django")
pytest.importorskip("rest_framework")

from rest_framework.test import APIClient

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.control_plane.api.views_helpers import set_inprocess_client
from flow_engine.control_plane.bootstrap import (
    bootstrap_test_principals,
    bootstrap_test_token_for,
)
from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.control_plane.principal_registry import register_principal, revoke_principal
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def r4_api(tmp_path):
    import django
    from django.apps import apps
    from django.conf import settings

    os.environ["ORCH_TESTING"] = "1"
    if not settings.configured:
        os.environ.setdefault(
            "DJANGO_SETTINGS_MODULE", "flow_engine.control_plane.settings"
        )
    if not apps.ready:
        django.setup()

    kernel = Kernel.init(tmp_path / "api.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo")
        ensure_queue(kernel.connection, name="default")
        bootstrap_test_principals(kernel.connection)
    client = CoordinatorClient.from_inprocess(kernel)
    set_inprocess_client(client)
    api = APIClient()
    yield api, kernel
    set_inprocess_client(None)
    kernel.close()


def _auth(api: APIClient, key: str) -> None:
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(key)}")


def test_unauthenticated_request_rejected(r4_api) -> None:
    api, _ = r4_api
    resp = api.post(
        "/api/v1/runtime/preview",
        {"work_item_id": "x", "provider": "codex"},
        format="json",
    )
    assert resp.status_code in {401, 403}


def test_founder_preview_accepted(r4_api) -> None:
    api, kernel = r4_api
    with transaction(kernel.connection):
        item = submit_work(kernel.connection, queue_name="default", payload={}, actor="f")
    _auth(api, "founder")
    resp = api.post(
        "/api/v1/runtime/preview",
        {"work_item_id": item["id"], "provider": "codex"},
        format="json",
    )
    assert resp.status_code in {200, 202}
    assert "operation_id" in resp.json()


def test_revoked_grant_denied(r4_api) -> None:
    api, kernel = r4_api
    with transaction(kernel.connection):
        register_principal(
            kernel.connection,
            principal_key="revoked-test",
            kind="worker",
            role=PrincipalRole.WORKER,
            raw_token="revoked-token-xyz",
            display_name="Revoked",
            surfaces=(Surface.REST, Surface.WORKER),
        )
        revoke_principal(kernel.connection, principal_key="revoked-test", actor="founder")
    api.credentials(HTTP_AUTHORIZATION="Bearer revoked-token-xyz")
    resp = api.get("/api/v1/runtime/runs/does-not-exist")
    assert resp.status_code in {401, 403}


def test_mcp_service_denied_recovery(r4_api) -> None:
    api, _ = r4_api
    _auth(api, "mcp-service")
    resp = api.post("/api/v1/runtime/recover", {}, format="json")
    assert resp.status_code == 403


def test_scheduler_denied_recovery(r4_api) -> None:
    api, _ = r4_api
    _auth(api, "scheduler")
    resp = api.post("/api/v1/runtime/recover", {}, format="json")
    assert resp.status_code == 403


def test_worker_denied_recovery(r4_api) -> None:
    api, _ = r4_api
    _auth(api, "worker")
    resp = api.post("/api/v1/runtime/recover", {}, format="json")
    assert resp.status_code == 403


def test_mcp_denied_founder_preview(r4_api) -> None:
    api, _ = r4_api
    _auth(api, "mcp-service")
    resp = api.post(
        "/api/v1/runtime/preview",
        {"work_item_id": "x", "provider": "codex"},
        format="json",
    )
    assert resp.status_code == 403


def test_founder_runtime_control_not_denied(r4_api) -> None:
    """Founder may call pause/resume/cancel; missing run fails after authz."""
    api, _ = r4_api
    _auth(api, "founder")
    for path in ("/api/v1/runtime/pause", "/api/v1/runtime/resume", "/api/v1/runtime/cancel"):
        resp = api.post(path, {"run_id": "nonexistent-run"}, format="json")
        assert resp.status_code != 403


def test_ops_summary_unauthenticated(r4_api) -> None:
    api, _ = r4_api
    resp = api.get("/ops/summary/")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") in {"ok", "degraded"}
    assert "open_gates" in body
    assert "hierarchy" in body
    assert "delegations" in body
    api, _ = r4_api
    _auth(api, "worker")
    resp = api.get("/api/v1/delivery/jobs")
    assert resp.status_code in {200, 202}
    body = resp.json()
    assert body.get("status") in {"applied", "rejected", None} or "operation_id" in body


def test_founder_denied_delivery_list(r4_api) -> None:
    """Founder lacks WORKER surface and delivery.list is worker-kind only."""
    api, _ = r4_api
    _auth(api, "founder")
    resp = api.get("/api/v1/delivery/jobs")
    assert resp.status_code == 403


def test_health_unauthenticated(r4_api) -> None:
    api, _ = r4_api
    resp = api.get("/health/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_openapi_schema_available(r4_api) -> None:
    api, _ = r4_api
    _auth(api, "founder")
    resp = api.get("/api/schema/")
    assert resp.status_code == 200


def test_async_run_returns_operation_id(r4_api) -> None:
    api, kernel = r4_api
    with transaction(kernel.connection):
        item = submit_work(kernel.connection, queue_name="default", payload={}, actor="f")
    _auth(api, "founder")
    resp = api.post(
        "/api/v1/runtime/run",
        {
            "work_item_id": item["id"],
            "provider": "codex",
            "delivery_mode": "async",
        },
        format="json",
    )
    assert resp.status_code in {200, 202}
    body = resp.json()
    assert "operation_id" in body
    result = body.get("result") or {}
    dispatched = result.get("dispatched") or {}
    delivery = dispatched.get("delivery") or {}
    assert delivery.get("mode") == "async" or delivery.get("delivered") is False


def test_caller_supplied_role_ignored(r4_api) -> None:
    api, kernel = r4_api
    with transaction(kernel.connection):
        item = submit_work(kernel.connection, queue_name="default", payload={}, actor="f")
    _auth(api, "mcp-service")
    resp = api.post(
        "/api/v1/runtime/preview",
        {
            "work_item_id": item["id"],
            "provider": "codex",
            "role": "founder",
            "principal_id": "founder",
            "grant": {"role": "founder"},
        },
        format="json",
    )
    assert resp.status_code == 403
