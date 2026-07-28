"""R4B independent-review: coordinator MCP enforce, secret exclusion, unique tools."""

from __future__ import annotations

import json
import os
from io import BytesIO

import pytest

os.environ["ORCH_TESTING"] = "1"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.control_plane.bootstrap import (
    bootstrap_test_principals,
    bootstrap_test_token_for,
)
from flow_engine.control_plane.principal_registry import resolve_by_key
from flow_engine.coordinator import (
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    SystemTestGrant,
)
from flow_engine.coordinator.http_service import application, reset_coordinator
from flow_engine.coordinator.mcp_enforce import strip_mcp_payload_audit_fields
from flow_engine.coordinator.transport import command_to_dict
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.mcp_lanes.catalog import LANE_IDS, clear_catalog_cache, principal_key_for_lane
from flow_engine.mcp_lanes.server import (
    SECRET_TOOL_ARG_KEYS,
    _tool_schema,
    redact_secret_tool_args,
    resolve_session_initiating_token,
)
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def coord_env(tmp_path, monkeypatch):
    db = tmp_path / "coord.db"
    monkeypatch.setenv("FLOW_DB_PATH", str(db))
    monkeypatch.setenv("ORCH_API_SERVICE_TOKEN", "test-api-service")
    monkeypatch.setenv("ORCH_WORKER_SERVICE_TOKEN", "test-worker-service")
    monkeypatch.setenv("ORCH_TESTING", "1")
    reset_coordinator()
    kernel = Kernel.init(db)
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo")
        ensure_queue(kernel.connection, name="default")
        bootstrap_test_principals(kernel.connection)
        item = submit_work(kernel.connection, queue_name="default", payload={}, actor="f")
    yield kernel, item
    reset_coordinator()
    kernel.close()


def _founder_grant(principal_id: str) -> SystemTestGrant:
    return SystemTestGrant(
        grant_id="test-grant",
        principal_id=principal_id,
        role=PrincipalRole.FOUNDER,
        surfaces=(Surface.REST, Surface.MCP, Surface.CLI, Surface.TEST),
        providers=("codex", "cursor", "claude"),
        budget_scope_id="acceptance-campaign-r4",
        policy_revision="r4-local",
    )


def _mcp_preview_command(
    *,
    founder_id: str,
    service_id: str,
    lane_id: str,
    tool_name: str,
    snapshot_digest: str,
    work_item_id: str,
    payload_extra: dict | None = None,
) -> RuntimeCommand:
    payload = {"work_item_id": work_item_id, "provider": "codex"}
    if payload_extra:
        payload.update(payload_extra)
    return RuntimeCommand(
        command_type="runtime.preview",
        target_id=work_item_id,
        payload=payload,
        context=CommandContext(
            principal_id=founder_id,
            role=PrincipalRole.FOUNDER,
            surface=Surface.MCP,
            grant=_founder_grant(founder_id),
            mcp_service_principal_id=service_id,
            mcp_lane_id=lane_id,
            mcp_tool_snapshot_digest=snapshot_digest,
            mcp_tool_name=tool_name,
        ),
    )


def _wsgi_call(method: str, path: str, body: bytes | None = None, headers: dict | None = None):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "9001",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": BytesIO(body or b""),
        "wsgi.errors": BytesIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(body or b"")),
    }
    for key, value in (headers or {}).items():
        environ[f"HTTP_{key.upper().replace('-', '_')}"] = value
    status_headers: list = []

    def start_response(status, response_headers, exc_info=None):
        status_headers.append((status, response_headers))

    result = b"".join(application(environ, start_response))
    status = status_headers[0][0]
    return status, json.loads(result.decode("utf-8") or "{}")


def test_lane_tool_names_unique_in_every_snapshot() -> None:
    clear_catalog_cache()
    for lane_id in LANE_IDS:
        snap = lane_tool_snapshot(lane_id)
        assert len(snap["tools"]) == len(set(snap["tools"]))
    context = lane_tool_snapshot("context-assets")
    assert context["tools"].count("session_brief") == 1


def test_tool_schema_excludes_initiating_token() -> None:
    schema = _tool_schema("preview")
    props = schema.get("properties") or {}
    assert "initiating_token" not in props
    for key in SECRET_TOOL_ARG_KEYS:
        assert key not in props
    assert "initiating_token" not in (schema.get("required") or [])


def test_secret_args_redacted_from_trace() -> None:
    trace = redact_secret_tool_args(
        {"initiating_token": "super-secret-token", "arguments": {"x": 1}}
    )
    assert trace["initiating_token"] == "[REDACTED]"
    assert "super-secret-token" not in json.dumps(trace)
    assert resolve_session_initiating_token(initiating_token="from-session") == "from-session"


def test_payload_audit_fields_stripped() -> None:
    cleaned = strip_mcp_payload_audit_fields(
        {
            "work_item_id": "w1",
            "mcp_lane_id": "forged",
            "mcp_service_principal_id": "forged",
            "initiating_principal_id": "forged",
            "mcp_snapshot_digest": "0" * 64,
        }
    )
    assert cleaned == {"work_item_id": "w1"}


def test_inprocess_dropped_mcp_context_denied(coord_env) -> None:
    kernel, item = coord_env
    founder = resolve_by_key(kernel.connection, "founder")
    service = resolve_by_key(kernel.connection, principal_key_for_lane("workflow-control"))
    snap = lane_tool_snapshot("workflow-control")
    command = _mcp_preview_command(
        founder_id=founder.principal_id,
        service_id=service.principal_id,
        lane_id="workflow-control",
        tool_name="preview",
        snapshot_digest=snap["snapshot_digest"],
        work_item_id=item["id"],
    )
    from dataclasses import replace

    dropped = RuntimeCommand(
        command_type=command.command_type,
        target_id=command.target_id,
        payload=command.payload,
        context=replace(
            command.context,
            mcp_service_principal_id=None,
            mcp_lane_id=None,
            mcp_tool_snapshot_digest=None,
            mcp_tool_name=None,
        ),
    )
    with transaction(kernel.connection):
        envelope = StateCoordinator(kernel.connection).accept(dropped)
    assert envelope["status"] == "rejected"
    assert envelope["error_code"] == "AUTHZ_DENIED"
    assert "incomplete" in (envelope.get("error") or "").lower() or "dropped" in (
        envelope.get("error") or ""
    ).lower()


def test_inprocess_cross_lane_context_denied(coord_env) -> None:
    kernel, item = coord_env
    founder = resolve_by_key(kernel.connection, "founder")
    # Service bound to context-assets, claim workflow-control lane.
    service = resolve_by_key(kernel.connection, principal_key_for_lane("context-assets"))
    snap = lane_tool_snapshot("workflow-control")
    command = _mcp_preview_command(
        founder_id=founder.principal_id,
        service_id=service.principal_id,
        lane_id="workflow-control",
        tool_name="preview",
        snapshot_digest=snap["snapshot_digest"],
        work_item_id=item["id"],
    )
    with transaction(kernel.connection):
        envelope = StateCoordinator(kernel.connection).accept(command)
    assert envelope["status"] == "rejected"
    assert envelope["error_code"] == "AUTHZ_DENIED"
    assert "cross-lane" in (envelope.get("error") or "").lower()


def test_inprocess_forged_payload_audit_ignored_when_context_valid(coord_env) -> None:
    kernel, item = coord_env
    founder = resolve_by_key(kernel.connection, "founder")
    service = resolve_by_key(kernel.connection, principal_key_for_lane("workflow-control"))
    other = resolve_by_key(kernel.connection, principal_key_for_lane("context-assets"))
    snap = lane_tool_snapshot("workflow-control")
    command = _mcp_preview_command(
        founder_id=founder.principal_id,
        service_id=service.principal_id,
        lane_id="workflow-control",
        tool_name="preview",
        snapshot_digest=snap["snapshot_digest"],
        work_item_id=item["id"],
        payload_extra={
            "mcp_lane_id": "context-assets",
            "mcp_service_principal_id": other.principal_id,
            "initiating_principal_id": "forged-initiator",
            "mcp_snapshot_digest": "0" * 64,
        },
    )
    with transaction(kernel.connection):
        envelope = StateCoordinator(kernel.connection).accept(command)
    assert envelope["status"] == "applied"
    assert envelope["command_type"] == "runtime.preview"


def test_http_dropped_mcp_context_denied(coord_env) -> None:
    kernel, item = coord_env
    founder = resolve_by_key(kernel.connection, "founder")
    body = json.dumps(
        {
            "command_type": "runtime.preview",
            "target_id": item["id"],
            "payload": {"work_item_id": item["id"], "provider": "codex"},
            "context": {
                "principal_id": "forged",
                "role": "founder",
                "surface": "mcp",
            },
        }
    ).encode("utf-8")
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        body,
        {
            "X-Orchestrator-Service-Token": "test-api-service",
            "X-Orchestrator-Service-Kind": "api",
            "X-Orchestrator-Principal-Token": bootstrap_test_token_for("founder"),
            "Content-Type": "application/json",
        },
    )
    assert status.startswith("403") or envelope.get("status") == "rejected"
    assert envelope.get("error_code") == "AUTHZ_DENIED"
    _ = founder  # principal resolved server-side


def test_http_forged_cross_lane_context_denied(coord_env) -> None:
    kernel, item = coord_env
    wrong_service = resolve_by_key(kernel.connection, principal_key_for_lane("context-assets"))
    snap = lane_tool_snapshot("workflow-control")
    body = json.dumps(
        {
            "command_type": "runtime.preview",
            "target_id": item["id"],
            "payload": {
                "work_item_id": item["id"],
                "provider": "codex",
                # Forged audit fields must be ignored / stripped.
                "mcp_lane_id": "workflow-control",
                "mcp_service_principal_id": wrong_service.principal_id,
            },
            "context": {
                "principal_id": "forged",
                "role": "founder",
                "surface": "mcp",
                "mcp_service_principal_id": wrong_service.principal_id,
                "mcp_lane_id": "workflow-control",
                "mcp_tool_snapshot_digest": snap["snapshot_digest"],
                "mcp_tool_name": "preview",
            },
        }
    ).encode("utf-8")
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        body,
        {
            "X-Orchestrator-Service-Token": "test-api-service",
            "X-Orchestrator-Service-Kind": "api",
            "X-Orchestrator-Principal-Token": bootstrap_test_token_for("founder"),
            "Content-Type": "application/json",
        },
    )
    assert "403" in status or envelope.get("status") == "rejected"
    assert envelope.get("error_code") == "AUTHZ_DENIED"
    assert "cross-lane" in (envelope.get("error") or "").lower()


def test_http_and_inprocess_valid_mcp_context_parity(coord_env) -> None:
    kernel, item = coord_env
    founder = resolve_by_key(kernel.connection, "founder")
    service = resolve_by_key(kernel.connection, principal_key_for_lane("workflow-control"))
    snap = lane_tool_snapshot("workflow-control")
    command = _mcp_preview_command(
        founder_id=founder.principal_id,
        service_id=service.principal_id,
        lane_id="workflow-control",
        tool_name="preview",
        snapshot_digest=snap["snapshot_digest"],
        work_item_id=item["id"],
    )
    with transaction(kernel.connection):
        inprocess = StateCoordinator(kernel.connection).accept(command)
    assert inprocess["status"] == "applied"

    # Fresh work item for HTTP path (idempotency).
    with transaction(kernel.connection):
        item2 = submit_work(kernel.connection, queue_name="default", payload={}, actor="f")
    command2 = _mcp_preview_command(
        founder_id=founder.principal_id,
        service_id=service.principal_id,
        lane_id="workflow-control",
        tool_name="preview",
        snapshot_digest=snap["snapshot_digest"],
        work_item_id=item2["id"],
    )
    body = json.dumps(command_to_dict(command2)).encode("utf-8")
    status, http_env = _wsgi_call(
        "POST",
        "/v1/commands",
        body,
        {
            "X-Orchestrator-Service-Token": "test-api-service",
            "X-Orchestrator-Service-Kind": "api",
            "X-Orchestrator-Principal-Token": bootstrap_test_token_for("founder"),
            "Content-Type": "application/json",
        },
    )
    assert status.startswith("202") or status.startswith("200")
    assert http_env["status"] == "applied"
    assert http_env["command_type"] == inprocess["command_type"]
