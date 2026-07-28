"""R4B MCP lane snapshots, dual-principal authz, cross-lane denial, profiles."""

from __future__ import annotations

import os
from pathlib import Path

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
from flow_engine.mcp_lanes.catalog import LANE_IDS, principal_key_for_lane
from flow_engine.mcp_lanes.forbidden import FORBIDDEN_TOOL_NAMES
from flow_engine.mcp_lanes.profiles import department_capability_profiles
from flow_engine.mcp_lanes.server import assert_lane_runtime_safe
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot, verify_tool_in_snapshot
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def r4b_api(tmp_path):
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

    kernel = Kernel.init(tmp_path / "r4b.db")
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


def _dual(api: APIClient, *, initiating: str, lane_id: str) -> None:
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(initiating)}",
        HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN=bootstrap_test_token_for(
            principal_key_for_lane(lane_id)
        ),
        HTTP_X_ORCHESTRATOR_MCP_LANE_ID=lane_id,
    )


def test_all_lane_snapshots_exact() -> None:
    digests = set()
    for lane_id in LANE_IDS:
        snap = lane_tool_snapshot(lane_id)
        assert snap["lane_id"] == lane_id
        assert snap["tools"]
        assert snap["snapshot_digest"]
        assert len(snap["snapshot_digest"]) == 64
        digests.add(snap["snapshot_digest"])
        for tool in snap["tools"]:
            assert tool not in FORBIDDEN_TOOL_NAMES
            verify_tool_in_snapshot(lane_id=lane_id, tool_name=tool)
    assert len(digests) == 5


def test_department_profiles_narrow_only() -> None:
    profiles = department_capability_profiles()
    assert set(profiles) == {"admin-ops", "qa", "tech"}
    for department, profile in profiles.items():
        assert profile["department"] == department
        assert profile["allowed_lane_ids"]
        assert "multiply" not in profile["authority_note"].lower() or "not" in profile["authority_note"]
        for lane_id in profile["allowed_lane_ids"]:
            assert lane_id in LANE_IDS


def test_snapshot_requires_dual_principal(r4b_api) -> None:
    api, _ = r4b_api
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for('founder')}")
    resp = api.get("/api/v1/mcp/lanes/workflow-control/snapshot")
    assert resp.status_code in {401, 403}


def test_snapshot_ok_with_dual_principal(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="workflow-control")
    resp = api.get("/api/v1/mcp/lanes/workflow-control/snapshot")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["lane"]["snapshot"]["lane_id"] == "workflow-control"
    assert body["initiating_principal_id"]
    assert body["mcp_service_principal"]["principal_key"] == "mcp.lane.workflow-control"
    assert body["initiating_principal_id"] != body["mcp_service_principal"]["principal_id"]


def test_cross_lane_crafted_call_denied(r4b_api) -> None:
    api, _ = r4b_api
    # Service identity is context-assets but path claims workflow-control.
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for('founder')}",
        HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN=bootstrap_test_token_for(
            principal_key_for_lane("context-assets")
        ),
        HTTP_X_ORCHESTRATOR_MCP_LANE_ID="workflow-control",
    )
    resp = api.post(
        "/api/v1/mcp/lanes/workflow-control/tools/invoke",
        {"tool": "preview", "arguments": {"work_item_id": "x", "provider": "codex"}},
        format="json",
    )
    assert resp.status_code in {401, 403}


def test_cross_lane_tool_not_in_snapshot_denied(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="workflow-control")
    resp = api.post(
        "/api/v1/mcp/lanes/workflow-control/tools/invoke",
        {"tool": "artifacts", "arguments": {}},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json().get("error_code") == "AUTHZ_DENIED"


def test_forbidden_tool_denied(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="evidence-governance")
    resp = api.post(
        "/api/v1/mcp/lanes/evidence-governance/tools/invoke",
        {"tool": "waiver", "arguments": {}},
        format="json",
    )
    assert resp.status_code == 403
    assert resp.json().get("error_code") in {"AUTHZ_DENIED", "UNSUPPORTED_SURFACE"}


def test_mcp_service_cannot_be_initiating(r4b_api) -> None:
    api, _ = r4b_api
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(principal_key_for_lane('workflow-control'))}",
        HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN=bootstrap_test_token_for(
            principal_key_for_lane("workflow-control")
        ),
    )
    resp = api.get("/api/v1/mcp/lanes/workflow-control/tools")
    assert resp.status_code in {401, 403}


def test_context_assets_stdio_compat_tools_present() -> None:
    snap = lane_tool_snapshot("context-assets")
    for tool in ("repo_health", "open_prs", "ci_status", "work_lookup", "session_brief"):
        assert tool in snap["tools"]
    assert snap["tools"].count("session_brief") == 1
    assert len(snap["tools"]) == len(set(snap["tools"]))


def test_invoke_context_tool_via_drf(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="context-assets")
    snap = lane_tool_snapshot("context-assets")
    resp = api.post(
        "/api/v1/mcp/lanes/context-assets/tools/invoke",
        {
            "tool": "repo_health",
            "expected_snapshot_digest": snap["snapshot_digest"],
            "arguments": {"project_id": "demo", "actor": "founder"},
        },
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["sqlite_from_mcp"] is False
    assert body["result"]["provider_calls"] is False
    assert body["initiating_principal_id"]
    assert body["mcp_service_principal_key"] == "mcp.lane.context-assets"


def test_invoke_workflow_preview_preserves_both_principals(r4b_api) -> None:
    api, kernel = r4b_api
    with transaction(kernel.connection):
        item = submit_work(kernel.connection, queue_name="default", payload={}, actor="f")
    _dual(api, initiating="founder", lane_id="workflow-control")
    resp = api.post(
        "/api/v1/mcp/lanes/workflow-control/tools/invoke",
        {
            "tool": "preview",
            "arguments": {"work_item_id": item["id"], "provider": "codex"},
        },
        format="json",
    )
    assert resp.status_code in {200, 202}
    body = resp.json()
    assert body.get("mcp", {}).get("lane_id") == "workflow-control"
    assert body.get("mcp", {}).get("initiating_principal_id")
    assert body.get("mcp", {}).get("mcp_service_principal_id")
    assert (
        body["mcp"]["initiating_principal_id"]
        != body["mcp"]["mcp_service_principal_id"]
    )


def test_maintenance_script_execution_not_available(r4b_api) -> None:
    """MCP may catalog allowlisted checks but must reject repository scripts."""
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="maintenance")
    resp = api.post(
        "/api/v1/mcp/lanes/maintenance/tools/invoke",
        {"tool": "registered_check_execution", "arguments": {}},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["repository_scripts_executable"] is False

    denied = api.post(
        "/api/v1/mcp/lanes/maintenance/tools/invoke",
        {
            "tool": "registered_check_execution",
            "arguments": {"script_id": "script.repository.custom_hook"},
        },
        format="json",
    )
    assert denied.status_code == 200
    denied_body = denied.json()
    assert denied_body["status"] == "rejected"
    assert denied_body["result"]["executable"] is False


def test_department_profile_denies_lane(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="maintenance")
    # QA worker loadouts do not include maintenance.
    resp = api.post(
        "/api/v1/mcp/lanes/maintenance/tools/invoke",
        {
            "tool": "health",
            "department": "qa",
            "arguments": {},
        },
        format="json",
    )
    assert resp.status_code == 403


def test_profiles_endpoint(r4b_api) -> None:
    api, _ = r4b_api
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for('founder')}")
    resp = api.get("/api/v1/mcp/profiles")
    assert resp.status_code == 200
    body = resp.json()
    assert "admin-ops" in body["profiles"]["departments"]
    assert "qa" in body["profiles"]["departments"]
    assert "tech" in body["profiles"]["departments"]


def test_lane_runtime_refuses_sqlite_env(monkeypatch) -> None:
    monkeypatch.setenv("FLOW_DB_PATH", "/tmp/nope.db")
    with pytest.raises(RuntimeError, match="FLOW_DB_PATH"):
        assert_lane_runtime_safe()


def test_compose_mcp_lanes_frontend_only_no_db() -> None:
    import yaml

    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)
    for name in (
        "mcp-context-assets",
        "mcp-workflow-control",
        "mcp-delegation-coordination",
        "mcp-evidence-governance",
        "mcp-maintenance",
    ):
        assert f"  {name}:" in text
        service = payload["services"][name]
        environment = service.get("environment") or {}
        volumes = service.get("volumes") or []
        assert "FLOW_DB_PATH" not in environment
        assert "COORDINATOR_URL" not in environment
        assert "ORCH_WORKER_SERVICE_TOKEN" not in environment
        assert "ORCH_API_SERVICE_TOKEN" not in environment
        assert "REDIS_PASSWORD" not in environment
        assert all("orchestrator-data" not in str(volume) for volume in volumes)
    assert "ORCH_TOKEN_MCP_WORKFLOW_CONTROL" in text
    # R4A hardening preserved.
    assert "no-new-privileges:true" in text
    assert "127.0.0.1:8000:8000" in text
    assert "9001:9001" not in text


def test_compose_mcp_token_no_cross_lane_projection() -> None:
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    wf_start = text.index("\n  mcp-workflow-control:\n")
    del_start = text.index("\n  mcp-delegation-coordination:\n")
    wf_block = text[wf_start:del_start]
    assert "ORCH_TOKEN_MCP_WORKFLOW_CONTROL" in wf_block
    assert "ORCH_TOKEN_MCP_CONTEXT_ASSETS" not in wf_block
    assert "ORCH_TOKEN_MCP_DELEGATION_COORDINATION" not in wf_block


def test_snapshot_digest_mismatch_denied(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="context-assets")
    resp = api.post(
        "/api/v1/mcp/lanes/context-assets/tools/invoke",
        {
            "tool": "session_brief",
            "expected_snapshot_digest": "0" * 64,
            "arguments": {},
        },
        format="json",
    )
    assert resp.status_code == 409
    assert resp.json().get("error_code") == "STALE_ASSET"


def test_delegation_command_mapping() -> None:
    from flow_engine.mcp_lanes.handlers import delegation_command_for_tool

    assert delegation_command_for_tool("request") == "delegation.request"
    assert delegation_command_for_tool("dispatch") == "delegation.dispatch"
    assert delegation_command_for_tool("handoff") == "delegation.handoff"
    assert (
        delegation_command_for_tool("disposition", {"action": "accept"})
        == "delegation.accept"
    )
    assert (
        delegation_command_for_tool("disposition", {"action": "decline"})
        == "delegation.decline"
    )
    assert (
        delegation_command_for_tool("disposition", {"action": "reroute"})
        == "delegation.reroute"
    )


def test_invoke_delegation_request_dispatches_command(r4b_api) -> None:
    """Mutating delegation tools must use coordinator dispatch, not read stubs."""
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="delegation-coordination")
    resp = api.post(
        "/api/v1/mcp/lanes/delegation-coordination/tools/invoke",
        {
            "tool": "request",
            "arguments": {
                "parent_assignment_id": "missing-assignment",
                "to_position_id": "missing-position",
            },
        },
        format="json",
    )
    body = resp.json()
    assert (body.get("result") or {}).get("mode") != "delegation_read"
    assert body.get("mcp", {}).get("lane_id") == "delegation-coordination"
    assert body.get("command_type") == "delegation.request"


def test_invoke_delegation_disposition_dispatches_command(r4b_api) -> None:
    api, _ = r4b_api
    _dual(api, initiating="founder", lane_id="delegation-coordination")
    resp = api.post(
        "/api/v1/mcp/lanes/delegation-coordination/tools/invoke",
        {
            "tool": "disposition",
            "arguments": {"request_id": "missing", "actor_id": "missing", "action": "accept"},
        },
        format="json",
    )
    body = resp.json()
    assert (body.get("result") or {}).get("mode") != "delegation_read"
    assert body.get("mcp", {}).get("lane_id") == "delegation-coordination"
    assert body.get("command_type") == "delegation.accept"
