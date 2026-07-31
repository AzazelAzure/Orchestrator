"""HTTP boundary tests for auth.register_user principal resolution."""

from __future__ import annotations

import json
import os
import uuid
from io import BytesIO

import pytest

os.environ["ORCH_TESTING"] = "1"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

from flow_engine.application import ensure_queue, init_project
from flow_engine.control_plane.bootstrap import bootstrap_test_principals, bootstrap_test_token_for
from flow_engine.coordinator.http_service import application, reset_coordinator
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def coord_env(tmp_path, monkeypatch):
    db = tmp_path / "auth-register-boundary.db"
    monkeypatch.setenv("FLOW_DB_PATH", str(db))
    monkeypatch.setenv("ORCH_API_SERVICE_TOKEN", "test-api-service")
    monkeypatch.setenv("ORCH_WORKER_SERVICE_TOKEN", "test-worker-service")
    monkeypatch.setenv("ORCH_ALLOW_USER_REGISTRATION", "0")
    monkeypatch.setenv("ORCH_TESTING", "1")
    reset_coordinator()
    kernel = Kernel.init(db)
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo")
        ensure_queue(kernel.connection, name="default")
        bootstrap_test_principals(kernel.connection)
    yield kernel
    reset_coordinator()
    kernel.close()


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


def _api_headers(*, principal_token: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "X-Orchestrator-Service-Token": "test-api-service",
        "X-Orchestrator-Service-Kind": "api",
        "Content-Type": "application/json",
    }
    if principal_token is not None:
        headers["X-Orchestrator-Principal-Token"] = principal_token
    if extra:
        headers.update(extra)
    return headers


def _register_body(
    username: str,
    *,
    context_role: str | None = None,
    founder_authorized: bool | None = None,
) -> bytes:
    payload: dict = {"username": username, "password": "password123"}
    if founder_authorized is not None:
        payload["founder_authorized"] = founder_authorized
    body: dict = {
        "command_type": "auth.register_user",
        "target_id": None,
        "payload": payload,
    }
    if context_role is not None:
        body["context"] = {
            "principal_id": "forged",
            "role": context_role,
            "surface": "rest",
        }
    return json.dumps(body).encode("utf-8")


def _user_exists(kernel, username: str) -> bool:
    row = kernel.connection.execute(
        "SELECT 1 FROM control_plane_user_accounts WHERE username = ?",
        (username,),
    ).fetchone()
    return row is not None


def test_http_founder_register_succeeds_flag_off(coord_env) -> None:
    kernel = coord_env
    username = f"founder-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username),
        _api_headers(principal_token=bootstrap_test_token_for("founder")),
    )
    assert status.startswith("202") or envelope.get("status") == "applied"
    assert envelope.get("status") == "applied"
    assert _user_exists(kernel, username)


def test_http_anonymous_register_denied_flag_off(coord_env) -> None:
    kernel = coord_env
    username = f"anon-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username),
        _api_headers(),
    )
    assert status.startswith("403")
    assert envelope.get("status") == "rejected"
    assert envelope.get("error_code") == "AUTHZ_DENIED"
    assert "registration is disabled" in (envelope.get("error") or "")
    assert not _user_exists(kernel, username)


def test_http_non_founder_bearer_denied_flag_off(coord_env) -> None:
    kernel = coord_env
    username = f"worker-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username),
        _api_headers(principal_token=bootstrap_test_token_for("worker")),
    )
    assert status.startswith("403")
    assert envelope.get("status") == "rejected"
    assert envelope.get("error_code") == "AUTHZ_DENIED"
    assert not _user_exists(kernel, username)


def test_http_smuggled_context_founder_ignored(coord_env) -> None:
    kernel = coord_env
    username = f"smuggle-ctx-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username, context_role="founder"),
        _api_headers(),
    )
    assert status.startswith("403")
    assert envelope.get("status") == "rejected"
    assert not _user_exists(kernel, username)


def test_http_smuggled_payload_founder_authorized_ignored(coord_env) -> None:
    kernel = coord_env
    username = f"smuggle-payload-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username, founder_authorized=True),
        _api_headers(),
    )
    assert status.startswith("403")
    assert envelope.get("status") == "rejected"
    assert not _user_exists(kernel, username)


def test_http_open_registration_anonymous_ok(coord_env, monkeypatch) -> None:
    kernel = coord_env
    monkeypatch.setenv("ORCH_ALLOW_USER_REGISTRATION", "1")
    reset_coordinator()
    username = f"open-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username),
        _api_headers(),
    )
    assert status.startswith("202") or envelope.get("status") == "applied"
    assert envelope.get("status") == "applied"
    assert _user_exists(kernel, username)


def test_http_non_api_caller_denied(coord_env) -> None:
    kernel = coord_env
    username = f"worker-svc-{uuid.uuid4().hex[:8]}"
    status, envelope = _wsgi_call(
        "POST",
        "/v1/commands",
        _register_body(username),
        {
            "X-Orchestrator-Service-Token": "test-worker-service",
            "X-Orchestrator-Service-Kind": "worker",
            "Content-Type": "application/json",
            "X-Orchestrator-Principal-Token": bootstrap_test_token_for("founder"),
        },
    )
    assert status.startswith("403")
    assert envelope.get("error_code") == "AUTHZ_DENIED"
    assert "API service credential" in (envelope.get("error") or "")
    assert not _user_exists(kernel, username)
