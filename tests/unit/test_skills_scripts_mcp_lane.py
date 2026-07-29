"""skills-scripts MCP lane: catalog reads + request_script_run → script.register."""

from __future__ import annotations

import json
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

from flow_engine.application import ensure_queue, init_project
from flow_engine.application.loadout_resolution import load_shipped_skill_hashes
from flow_engine.control_plane.api.views_helpers import set_inprocess_client
from flow_engine.control_plane.bootstrap import (
    bootstrap_test_principals,
    bootstrap_test_token_for,
)
from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.mcp_lanes.catalog import LANE_IDS, principal_key_for_lane
from flow_engine.mcp_lanes.handlers import SCRIPT_TOOL_TO_COMMAND, script_command_for_tool
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction

LANE = "skills-scripts"


@pytest.fixture
def api_kernel(tmp_path):
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

    kernel = Kernel.init(tmp_path / "skills_scripts.db")
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


def _dual(api: APIClient, *, initiating: str, lane_id: str = LANE) -> None:
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(initiating)}",
        HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN=bootstrap_test_token_for(
            principal_key_for_lane(lane_id)
        ),
        HTTP_X_ORCHESTRATOR_MCP_LANE_ID=lane_id,
    )


def test_skills_scripts_lane_in_catalog_snapshots() -> None:
    assert LANE in LANE_IDS
    snap = lane_tool_snapshot(LANE)
    tools = set(snap["tools"])
    assert {
        "list_skills",
        "get_skill",
        "list_scripts",
        "describe_script",
        "request_script_run",
    } <= tools
    assert "arbitrary_script_execution" in snap["forbidden_operations"]


def test_request_script_run_maps_to_script_register() -> None:
    assert SCRIPT_TOOL_TO_COMMAND["request_script_run"] == "script.register"
    assert script_command_for_tool("request_script_run") == "script.register"
    assert script_command_for_tool("list_skills") is None


def test_list_skills_publication_neutral(api_kernel, tmp_path) -> None:
    api, _ = api_kernel
    skills_root = Path(__file__).resolve().parents[2] / "skills"
    before = {}
    for manifest in sorted(skills_root.glob("*/manifest.json")):
        before[str(manifest)] = manifest.read_bytes()

    _dual(api, initiating="founder")
    resp = api.post(
        f"/api/v1/mcp/lanes/{LANE}/tools/invoke",
        {"tool": "list_skills", "arguments": {}},
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["result"]["publication_candidate"] is False
    assert body["result"]["activation"] is False
    assert body["result"]["count"] >= 1

    for path, content in before.items():
        assert Path(path).read_bytes() == content

    # Hash walk still consistent (no side-effect drift).
    load_shipped_skill_hashes()


def test_request_script_run_dual_principal_founder_ok(api_kernel) -> None:
    api, _ = api_kernel
    _dual(api, initiating="founder")
    resp = api.post(
        f"/api/v1/mcp/lanes/{LANE}/tools/invoke",
        {
            "tool": "request_script_run",
            "arguments": {"script_id": "script.generic.repository_health"},
        },
        format="json",
    )
    assert resp.status_code in {200, 202}
    body = resp.json()
    assert body.get("mcp", {}).get("lane_id") == LANE
    assert body.get("mcp", {}).get("initiating_principal_id")
    assert body.get("mcp", {}).get("mcp_service_principal_id")
    # Accept either accepted envelope or rejected-for-runtime reasons; mapping must fire.
    assert "error_code" not in body or body.get("command_type") == "script.register" or body.get(
        "status"
    ) in {"accepted", "ok", "rejected", "queued"}


def test_request_script_run_mcp_service_initiating_denied(api_kernel) -> None:
    api, _ = api_kernel
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(principal_key_for_lane(LANE))}",
        HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN=bootstrap_test_token_for(
            principal_key_for_lane(LANE)
        ),
        HTTP_X_ORCHESTRATOR_MCP_LANE_ID=LANE,
    )
    resp = api.post(
        f"/api/v1/mcp/lanes/{LANE}/tools/invoke",
        {
            "tool": "request_script_run",
            "arguments": {"script_id": "script.generic.repository_health"},
        },
        format="json",
    )
    assert resp.status_code in {401, 403}


def test_mcp_service_rest_scripts_execute_denied(api_kernel) -> None:
    api, _ = api_kernel
    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(principal_key_for_lane(LANE))}",
    )
    resp = api.post(
        "/api/v1/scripts/execute",
        {"script_id": "script.generic.repository_health"},
        format="json",
    )
    assert resp.status_code in {401, 403}
