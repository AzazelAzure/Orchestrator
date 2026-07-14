"""transport tests: shared CLI/MCP dispatch, bounds, redaction, degraded mode."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.capabilities.envelope import CapabilityRequest, ResultCode
from flow_engine.capabilities.providers import ProviderResponse
from flow_engine.capabilities.service import CapabilityService, UnconfiguredProvider
from flow_engine.capabilities.transport import (
    CAPABILITY_REPO_HEALTH,
    CAPABILITY_SESSION_BRIEF,
    CAPABILITY_WORK_LOOKUP,
    MAX_LIST_ITEMS,
    MAX_TEXT_LENGTH,
    MCP_TOOL_OPEN_PRS,
    MCP_TOOL_REPO_HEALTH,
    MCP_TOOL_SESSION_BRIEF,
    MCP_TOOL_WORK_LOOKUP,
    bound_result,
    build_request,
    build_request_from_tool,
    dispatch_capability,
    dispatch_with_timeout,
    serialize_result,
)
from flow_engine.mcp.server import handle_tool_call
from flow_engine.persistence.transactions import transaction


def _write_projects_config(path: Path, checkout: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo_project": {
                        "checkout_path": str(checkout),
                        "engine_project_name": "demo_project",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    sample = path / "README.md"
    sample.write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True)


def _service(tmp_path: Path, repo: Path | None = None) -> CapabilityService:
    config = tmp_path / "projects.json"
    checkout = repo or (tmp_path / "repo")
    if repo is None:
        checkout.mkdir()
        _init_git_repo(checkout)
    _write_projects_config(config, checkout)
    return CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        open_pr_provider=UnconfiguredProvider(),
        ci_provider=UnconfiguredProvider(),
    )


def _cli_cap(db_path: Path, projects_config: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "flow_engine.cli.app",
            "--db",
            str(db_path),
            "--json",
            "cap",
            "--projects-config",
            str(projects_config),
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_and_transport_repo_health_parity(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = build_request(
        CAPABILITY_REPO_HEALTH,
        project_id="demo_project",
        actor="actor:test",
        request_id="parity-1",
    )
    direct = serialize_result(dispatch_capability(service, request))

    proc = _cli_cap(
        tmp_path / "state.db",
        tmp_path / "projects.json",
        "--actor",
        "actor:test",
        "--request-id",
        "parity-1",
        "repo-health",
        "--project",
        "demo_project",
    )
    assert proc.returncode == 0, proc.stderr
    cli = json.loads(proc.stdout)
    assert cli["code"] == direct["code"] == ResultCode.OK.value
    assert cli["request_id"] == direct["request_id"] == "parity-1"
    assert cli["data"]["dirty"] == direct["data"]["dirty"]
    assert cli["status"] == direct["status"]
    assert "captured_at" in cli and "captured_at" in direct


def test_mcp_and_transport_repo_health_parity(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    service = _service(tmp_path)
    request = build_request(
        CAPABILITY_REPO_HEALTH,
        project_id="demo_project",
        actor="actor:test",
        request_id="parity-mcp",
    )
    direct = serialize_result(dispatch_capability(service, request))
    mcp = handle_tool_call(
        service,
        MCP_TOOL_REPO_HEALTH,
        {"project_id": "demo_project", "actor": "actor:test", "request_id": "parity-mcp"},
    )
    assert mcp["code"] == direct["code"] == ResultCode.OK.value
    assert mcp["request_id"] == direct["request_id"]
    assert mcp["data"]["branch"] == direct["data"]["branch"]


def test_invalid_input_unknown_project(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = build_request(
        CAPABILITY_REPO_HEALTH,
        project_id="missing-project",
        actor="actor:test",
    )
    result = dispatch_capability(service, request)
    assert result.code == ResultCode.UNAVAILABLE


def test_invalid_input_mcp_rejects_extra_arguments(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    service = _service(tmp_path)
    payload = handle_tool_call(
        service,
        MCP_TOOL_REPO_HEALTH,
        {
            "project_id": "demo_project",
            "actor": "actor:test",
            "checkout_path": "/etc/passwd",
        },
    )
    assert payload["code"] == ResultCode.INVALID_INPUT.value


def test_work_lookup_requires_selector(kernel_db, tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = build_request(
        CAPABILITY_WORK_LOOKUP,
        project_id="demo_project",
        actor="actor:test",
    )
    result = dispatch_capability(service, request)
    assert result.code == ResultCode.INVALID_INPUT


def test_unavailable_checkout_degrades_repo_health(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    missing = tmp_path / "missing-checkout"
    _write_projects_config(config, missing)
    service = CapabilityService(db_path=tmp_path / "state.db", projects_config=config)
    request = build_request(
        CAPABILITY_REPO_HEALTH,
        project_id="demo_project",
        actor="actor:test",
    )
    result = dispatch_capability(service, request)
    assert result.code == ResultCode.UNAVAILABLE


def test_degraded_without_github_credentials(kernel_db, tmp_path: Path) -> None:
    service = _service(tmp_path)
    conn = kernel_db.connection
    with transaction(conn):
        init_project(conn, name="demo_project")
        ensure_queue(conn, name="default")
        submit_work(
            conn,
            queue_name="default",
            payload={"logical_work_id": "TASK-1"},
            actor="actor:planner",
        )
    service = CapabilityService(
        db_path=kernel_db.db_path,
        projects_config=tmp_path / "projects.json",
        open_pr_provider=UnconfiguredProvider("credentials not configured"),
        ci_provider=UnconfiguredProvider("credentials not configured"),
    )
    request = build_request(
        CAPABILITY_SESSION_BRIEF,
        project_id="demo_project",
        actor="actor:test",
        params={"logical_work_id": "TASK-1"},
    )
    result = dispatch_capability(service, request)
    payload = serialize_result(result)
    assert payload["code"] == ResultCode.OK.value
    assert payload["status"] == "degraded"
    assert payload["data"]["sections"]["work"]["code"] == ResultCode.OK.value
    assert payload["data"]["provider_availability"]["open_prs"]["status"] == "unavailable"


def test_provider_exception_returns_degraded_not_crash(tmp_path: Path) -> None:
    class BoomProvider:
        def list_open_prs(self, *, owner: str, repo: str, timeout_sec: float):
            raise RuntimeError("network down")

    config = tmp_path / "projects.json"
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _write_projects_config(config, checkout)
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        open_pr_provider=BoomProvider(),
    )
    request = build_request(
        "open_prs",
        project_id="demo_project",
        actor="actor:test",
        params={"github_owner": "org", "github_repo": "demo"},
    )
    payload = serialize_result(dispatch_capability(service, request))
    assert payload["code"] == ResultCode.UNAVAILABLE.value
    assert payload["status"] == "degraded"
    assert "network down" in payload["error"]["message"]


def test_transport_timeout(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)

    def slow_repo_health(request: CapabilityRequest):
        time.sleep(0.2)
        return service.repo_health(request)

    monkeypatch.setattr(service, "repo_health", slow_repo_health)
    request = build_request(
        CAPABILITY_REPO_HEALTH,
        project_id="demo_project",
        actor="actor:test",
    )
    result = dispatch_with_timeout(service, request, timeout_sec=0.05)
    payload = serialize_result(result)
    assert payload["code"] == ResultCode.TIMEOUT.value


def test_bounded_output_truncates_large_text_and_lists() -> None:
    from flow_engine.capabilities.envelope import CapabilityResult, CapabilityStatus

    request = CapabilityRequest(
        capability="session_brief",
        request_id="req-bound",
        actor="actor:test",
        project_id="demo_project",
    )
    result = CapabilityResult.success(
        request,
        data={
            "text": "x" * (MAX_TEXT_LENGTH + 50),
            "items": [{"n": i} for i in range(MAX_LIST_ITEMS + 5)],
        },
        status=CapabilityStatus.READY,
    )
    payload = serialize_result(result)
    assert len(payload["data"]["text"]) <= MAX_TEXT_LENGTH + len("…[truncated]")
    assert len(payload["data"]["items"]) == MAX_LIST_ITEMS + 1
    assert payload["data"]["items"][-1]["_truncated_items"] == 5


def test_redaction_preserved_in_serialized_work_lookup(kernel_db, tmp_path: Path) -> None:
    service = _service(tmp_path)
    conn = kernel_db.connection
    with transaction(conn):
        init_project(conn, name="demo_project")
        ensure_queue(conn, name="default")
        submit_work(
            conn,
            queue_name="default",
            payload={
                "logical_work_id": "TASK-SECRET",
                "evidence_refs": [
                    {
                        "ref_id": "ev-secret",
                        "kind": "artifact",
                        "uri": "file:///secret",
                        "sensitivity": "restricted",
                    }
                ],
            },
            actor="actor:planner",
        )
    service = CapabilityService(
        db_path=kernel_db.db_path,
        projects_config=tmp_path / "projects.json",
    )
    request = build_request(
        CAPABILITY_WORK_LOOKUP,
        project_id="demo_project",
        actor="actor:test",
        params={"logical_work_id": "TASK-SECRET"},
    )
    payload = serialize_result(dispatch_capability(service, request))
    assert payload["code"] == ResultCode.RESTRICTED.value


def test_build_request_from_tool_work_lookup_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="work_id or logical_work_id"):
        build_request_from_tool(
            MCP_TOOL_WORK_LOOKUP,
            {"project_id": "demo_project", "actor": "actor:test"},
        )


def test_build_request_from_tool_rejects_non_string_project_id() -> None:
    with pytest.raises(ValueError, match="project_id must be a string"):
        build_request_from_tool(
            MCP_TOOL_REPO_HEALTH,
            {"project_id": 42, "actor": "actor:test"},
        )


def test_build_request_from_tool_rejects_oversized_actor() -> None:
    with pytest.raises(ValueError, match="actor exceeds maximum length"):
        build_request_from_tool(
            MCP_TOOL_REPO_HEALTH,
            {"project_id": "demo_project", "actor": "a" * 200},
        )


def test_build_request_from_tool_rejects_oversized_github_owner() -> None:
    with pytest.raises(ValueError, match="github_owner exceeds maximum length"):
        build_request_from_tool(
            MCP_TOOL_OPEN_PRS,
            {
                "project_id": "demo_project",
                "actor": "actor:test",
                "github_owner": "o" * 150,
                "github_repo": "demo",
            },
        )


def test_handle_tool_call_rejects_invalid_type_via_transport(tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    from flow_engine.mcp.server import handle_tool_call

    service = _service(tmp_path)
    payload = handle_tool_call(
        service,
        MCP_TOOL_REPO_HEALTH,
        {"project_id": ["demo_project"], "actor": "mcp:test"},
    )
    assert payload["code"] == "invalid_input"
    assert "must be a string" in payload["error"]["message"]


def test_bound_result_is_idempotent() -> None:
    from flow_engine.capabilities.envelope import CapabilityResult

    request = CapabilityRequest(
        capability="repo_health",
        request_id="req-1",
        actor="actor:test",
        project_id="demo_project",
    )
    result = CapabilityResult.success(request, data={"branch": "main"})
    once = bound_result(result)
    twice = bound_result(once)
    assert once.to_dict() == twice.to_dict()


class UnavailablePRProvider:
    def list_open_prs(self, *, owner: str, repo: str, timeout_sec: float) -> ProviderResponse:
        return ProviderResponse(available=False, degraded=True, reason="provider timed out")


def test_open_prs_provider_unavailable_degraded(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _write_projects_config(config, checkout)
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        open_pr_provider=UnavailablePRProvider(),
    )
    request = build_request(
        "open_prs",
        project_id="demo_project",
        actor="actor:test",
        params={"github_owner": "org", "github_repo": "demo"},
    )
    payload = serialize_result(dispatch_capability(service, request))
    assert payload["code"] == ResultCode.UNAVAILABLE.value
    assert payload["status"] == "degraded"


def test_mcp_session_brief_parity(kernel_db, tmp_path: Path) -> None:
    pytest.importorskip("mcp")
    service = _service(tmp_path)
    conn = kernel_db.connection
    with transaction(conn):
        init_project(conn, name="demo_project")
        ensure_queue(conn, name="default")
        submit_work(
            conn,
            queue_name="default",
            payload={"logical_work_id": "TASK-1"},
            actor="actor:planner",
        )
    service = CapabilityService(
        db_path=kernel_db.db_path,
        projects_config=tmp_path / "projects.json",
        open_pr_provider=UnconfiguredProvider(),
        ci_provider=UnconfiguredProvider(),
    )
    args = {
        "logical_work_id": "TASK-1",
    }
    direct = serialize_result(
        dispatch_capability(
            service,
            build_request(
                CAPABILITY_SESSION_BRIEF,
                project_id="demo_project",
                actor="actor:test",
                params=args,
            ),
        )
    )
    mcp = handle_tool_call(
        service,
        MCP_TOOL_SESSION_BRIEF,
        {"project_id": "demo_project", "actor": "actor:test", **args},
    )
    assert mcp["code"] == direct["code"]
    assert mcp["status"] == direct["status"]
    assert mcp["data"]["text"] == direct["data"]["text"]
