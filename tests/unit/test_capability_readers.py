"""Tests for read-only project capabilities."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.capabilities.envelope import CapabilityRequest, ResultCode
from flow_engine.capabilities.providers import (
    ProviderPullRequest,
    ProviderResponse,
)
from flow_engine.capabilities.repo_health import read_repo_health
from flow_engine.capabilities.service import CapabilityService, UnconfiguredProvider
from flow_engine.capabilities.work_lookup import lookup_work
from flow_engine.persistence.transactions import transaction


def _request(capability: str, project_id: str = "demo_project", **params) -> CapabilityRequest:
    return CapabilityRequest(
        capability=capability,
        request_id="req-test",
        actor="actor:reviewer",
        project_id=project_id,
        params=params,
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    sample = path / "README.md"
    sample.write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True)


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


class MockOpenPRProvider:
    def __init__(self, response: ProviderResponse) -> None:
        self._response = response

    def list_open_prs(self, *, owner: str, repo: str, timeout_sec: float) -> ProviderResponse:
        return self._response


class MockCIProvider:
    def __init__(self, response: ProviderResponse) -> None:
        self._response = response

    def get_ci_status(self, *, owner: str, repo: str, ref: str, timeout_sec: float) -> ProviderResponse:
        return self._response


def test_repo_health_clean_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    result = read_repo_health(_request("repo_health"), checkout_path=repo)
    assert result.code == ResultCode.OK
    assert result.data["dirty"] is False
    assert result.data["branch"] == "main" or result.data["branch"] == "master"
    assert result.data["logical_project_id"] == "demo_project"


def test_repo_health_dirty_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    (repo / "README.md").write_text("changed\n", encoding="utf-8")
    result = read_repo_health(_request("repo_health"), checkout_path=repo)
    assert result.code == ResultCode.OK
    assert result.data["dirty"] is True
    assert result.data["dirty_file_count"] == 1


def test_repo_health_missing_repository(tmp_path: Path) -> None:
    result = read_repo_health(_request("repo_health"), checkout_path=tmp_path / "missing")
    assert result.code == ResultCode.UNAVAILABLE


def test_repo_health_no_commits(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
    result = read_repo_health(_request("repo_health"), checkout_path=repo)
    assert result.code == ResultCode.NOT_FOUND


def test_open_prs_unconfigured_provider(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        open_pr_provider=UnconfiguredProvider(),
    )
    result = service.open_prs(
        _request("open_prs", github_owner="org", github_repo="demo")
    )
    assert result.code == ResultCode.UNAVAILABLE


def test_open_prs_mock_provider_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    provider = MockOpenPRProvider(
        ProviderResponse(
            available=True,
            data=[ProviderPullRequest(number=1, title="Demo", url="https://example/pr/1", state="open")],
        )
    )
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        open_pr_provider=provider,
    )
    result = service.open_prs(
        _request("open_prs", github_owner="org", github_repo="demo")
    )
    assert result.code == ResultCode.OK
    assert result.data["pull_requests"][0]["number"] == 1


def test_ci_status_missing_auth_is_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        ci_provider=UnconfiguredProvider("missing auth"),
    )
    result = service.ci_status(
        _request("ci_status", github_owner="org", github_repo="demo", ref="abc123")
    )
    assert result.code == ResultCode.UNAVAILABLE
    assert "missing auth" in (result.error.message if result.error else "")


def test_work_lookup_found(kernel_db, tmp_path: Path) -> None:
    conn = kernel_db.connection
    with transaction(conn):
        init_project(conn, name="demo_project")
        ensure_queue(conn, name="default")
        submitted = submit_work(
            conn,
            queue_name="default",
            payload={
                "logical_work_id": "TASK-1",
                "evidence_refs": [
                    {"ref_id": "ev-1", "kind": "note", "uri": "file:///tmp/note", "sensitivity": "internal"}
                ],
            },
            actor="actor:planner",
        )
    result = lookup_work(conn, _request("work_lookup", logical_work_id="TASK-1"))
    assert result.code == ResultCode.OK
    assert result.data["work_id"] == submitted["id"]
    assert result.data["gates"] == []
    assert result.data["ancestry"]["depends_on"] == []


def test_work_lookup_not_found(kernel_db) -> None:
    conn = kernel_db.connection
    with transaction(conn):
        init_project(conn, name="demo_project")
        ensure_queue(conn, name="default")
    result = lookup_work(conn, _request("work_lookup", work_id="missing"))
    assert result.code == ResultCode.NOT_FOUND


def test_work_lookup_ambiguous(kernel_db) -> None:
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
        submit_work(
            conn,
            queue_name="default",
            payload={"logical_work_id": "TASK-1"},
            actor="actor:planner",
        )
    result = lookup_work(conn, _request("work_lookup", logical_work_id="TASK-1"))
    assert result.code == ResultCode.AMBIGUOUS


def test_work_lookup_restricted_evidence(kernel_db) -> None:
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
                    {"ref_id": "ev-secret", "kind": "artifact", "uri": "file:///secret", "sensitivity": "restricted"}
                ],
            },
            actor="actor:planner",
        )
    result = lookup_work(conn, _request("work_lookup", logical_work_id="TASK-SECRET"))
    assert result.code == ResultCode.RESTRICTED


def test_session_brief_deterministic_snapshot(kernel_db, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)

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
        projects_config=config,
        open_pr_provider=UnconfiguredProvider(),
        ci_provider=UnconfiguredProvider(),
    )
    request = _request("session_brief", logical_work_id="TASK-1")
    result = service.session_brief(request)
    assert result.code == ResultCode.OK
    assert result.status.value == "degraded"
    assert "Session brief for project demo_project" in result.data["text"]
    assert "open_prs=unavailable" in result.data["text"]
    assert result.data["sections"]["repository_health"]["code"] == ResultCode.OK.value


def test_repo_health_missing_git_binary(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("flow_engine.capabilities.repo_health.subprocess.run", missing_git)
    result = read_repo_health(_request("repo_health"), checkout_path=repo)
    assert result.code == ResultCode.UNAVAILABLE
    assert result.error is not None
    assert "git is unavailable" in result.error.message


class ExplodingOpenPRProvider:
    def list_open_prs(self, *, owner: str, repo: str, timeout_sec: float):
        raise RuntimeError("network down")


class ExplodingCIProvider:
    def get_ci_status(self, *, owner: str, repo: str, ref: str, timeout_sec: float):
        raise RuntimeError("provider crashed")


def test_open_prs_isolates_provider_exception(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        open_pr_provider=ExplodingOpenPRProvider(),
    )
    result = service.open_prs(_request("open_prs", github_owner="org", github_repo="demo"))
    assert result.code == ResultCode.UNAVAILABLE
    assert result.status.value == "degraded"
    assert result.error is not None
    assert "network down" in result.error.message


def test_ci_status_isolates_provider_exception(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(
        db_path=tmp_path / "state.db",
        projects_config=config,
        ci_provider=ExplodingCIProvider(),
    )
    result = service.ci_status(
        _request("ci_status", github_owner="org", github_repo="demo", ref="abc123")
    )
    assert result.code == ResultCode.UNAVAILABLE
    assert result.status.value == "degraded"
    assert result.error is not None
    assert "provider crashed" in result.error.message


def test_session_brief_isolates_provider_exception(kernel_db, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(
        db_path=kernel_db.db_path,
        projects_config=config,
        open_pr_provider=ExplodingOpenPRProvider(),
        ci_provider=ExplodingCIProvider(),
    )
    result = service.session_brief(
        _request("session_brief", github_owner="org", github_repo="demo")
    )
    assert result.code == ResultCode.OK
    assert result.status.value == "degraded"
    assert "open_prs" in result.degraded_components
    assert result.data["provider_availability"]["open_prs"]["status"] == "degraded"


def test_capability_service_closes_connection_after_work_lookup(
    kernel_db, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    conn = kernel_db.connection
    with transaction(conn):
        init_project(conn, name="demo_project")
        ensure_queue(conn, name="default")
        submit_work(
            conn,
            queue_name="default",
            payload={"logical_work_id": "TASK-CLOSE"},
            actor="actor:planner",
        )

    closed: list[bool] = []
    original_open = __import__(
        "flow_engine.persistence.connection", fromlist=["open_connection"]
    ).open_connection

    class CloseTrackingConnection:
        def __init__(self, inner):
            self._inner = inner

        def close(self) -> None:
            closed.append(True)
            self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def tracking_open(db_path, **kwargs):
        return CloseTrackingConnection(original_open(db_path, **kwargs))

    monkeypatch.setattr("flow_engine.capabilities.service.open_connection", tracking_open)
    service = CapabilityService(db_path=kernel_db.db_path, projects_config=config)
    result = service.work_lookup(_request("work_lookup", logical_work_id="TASK-CLOSE"))
    assert result.code == ResultCode.OK
    assert closed == [True]


def test_capability_service_closes_connection_after_session_brief(
    kernel_db, tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)

    closed: list[bool] = []
    original_open = __import__(
        "flow_engine.persistence.connection", fromlist=["open_connection"]
    ).open_connection

    class CloseTrackingConnection:
        def __init__(self, inner):
            self._inner = inner

        def close(self) -> None:
            closed.append(True)
            self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def tracking_open(db_path, **kwargs):
        return CloseTrackingConnection(original_open(db_path, **kwargs))

    monkeypatch.setattr("flow_engine.capabilities.service.open_connection", tracking_open)
    service = CapabilityService(
        db_path=kernel_db.db_path,
        projects_config=config,
        open_pr_provider=UnconfiguredProvider(),
        ci_provider=UnconfiguredProvider(),
    )
    result = service.session_brief(_request("session_brief"))
    assert result.code == ResultCode.OK
    assert closed == [True]
