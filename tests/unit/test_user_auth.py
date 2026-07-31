"""User auth: accounts, opaque credentials, throttle, ops gating, CLI store."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["ORCH_TESTING"] = "1"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

pytest.importorskip("django")
pytest.importorskip("rest_framework")

from rest_framework.test import APIClient

from flow_engine.application import ensure_queue, init_project
from flow_engine.application.clock import clear_clock, utc_now_iso
from flow_engine.cli import auth_cmds
from flow_engine.control_plane.api.views_helpers import set_inprocess_client
from flow_engine.control_plane.authz_matrix import (
    OPS_READ_CAPABILITY,
    assert_command_allowed_for_kind,
)
from flow_engine.control_plane.bootstrap import bootstrap_test_principals, bootstrap_test_token_for
from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.control_plane.principal_registry import (
    register_principal,
    resolve_by_token,
    token_digest,
)
from flow_engine.control_plane.user_auth import (
    login_user,
    refresh_session,
    register_user,
    throttle_check_and_bump,
)
from flow_engine.domain.errors import AuthRequiredError, AuthzDeniedError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence import Kernel
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.migrations import (
    KERNEL_TABLES,
    _load_sql,
    apply_migrations,
    current_version,
    list_tables,
)
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def auth_api(tmp_path, monkeypatch):
    import django
    from django.apps import apps
    from django.conf import settings

    monkeypatch.setenv("ORCH_TESTING", "1")
    monkeypatch.setenv("ORCH_ALLOW_USER_REGISTRATION", "0")
    if not settings.configured:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flow_engine.control_plane.settings")
    if not apps.ready:
        django.setup()

    kernel = Kernel.init(tmp_path / "auth.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo")
        ensure_queue(kernel.connection, name="default")
        bootstrap_test_principals(kernel.connection)
    client = CoordinatorClient.from_inprocess(kernel)
    set_inprocess_client(client)
    api = APIClient()
    yield api, kernel
    set_inprocess_client(None)
    clear_clock()
    kernel.close()


def _auth(api: APIClient, key: str) -> None:
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(key)}")


def test_migration_008_rebuild_preserves_principals_and_allows_human(tmp_path) -> None:
    db_path = tmp_path / "pre8.db"
    conn = open_connection(db_path, initialize=False)
    try:
        for name in (
            "001_initial_schema.sql",
            "002_governance_invariants.sql",
            "003_r2_runtime.sql",
            "004_r3_organization.sql",
            "005_r4_control_plane.sql",
            "006_r4c_scripts_schedules.sql",
            "007_provider_adapters.sql",
        ):
            conn.executescript(_load_sql(name))
        now = utc_now_iso()
        for version in range(1, 8):
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, now),
            )
        conn.execute(
            """
            INSERT INTO control_plane_principals (
                id, principal_key, kind, role, display_name, status, token_digest,
                capabilities_json, surfaces_json, created_at
            ) VALUES
            ('p-founder', 'founder', 'founder', 'founder', 'Founder', 'active', ?, '[]', '["rest"]', ?),
            ('p-worker', 'worker', 'worker', 'worker', 'Worker', 'active', ?, '[]', '["rest"]', ?),
            ('p-sched', 'scheduler', 'scheduler', 'system', 'Sched', 'active', ?, '[]', '["rest"]', ?),
            ('p-svc', 'mcp-service', 'mcp_service', 'worker', 'MCP', 'active', ?, '[]', '["rest"]', ?)
            """,
            (
                token_digest("pre-founder"),
                now,
                token_digest("pre-worker"),
                now,
                token_digest("pre-sched"),
                now,
                token_digest("pre-mcp"),
                now,
            ),
        )
        conn.commit()
        assert current_version(conn) == 7
        apply_migrations(conn)
        assert current_version(conn) == 8
        tables = set(list_tables(conn))
        assert set(KERNEL_TABLES).issubset(tables)
        assert "control_plane_user_accounts" in tables
        assert "control_plane_credentials" in tables
        assert "control_plane_auth_throttle" in tables
        kinds = {
            r["kind"] for r in conn.execute("SELECT kind FROM control_plane_principals").fetchall()
        }
        assert kinds == {"founder", "worker", "scheduler", "mcp_service"}
        assert resolve_by_token(conn, "pre-founder").kind == "founder"
        with transaction(conn):
            register_principal(
                conn,
                principal_key="human.alice",
                kind="human",
                role=PrincipalRole.MANAGER,
                raw_token="unused-human-legacy",
                display_name="Alice",
                surfaces=(Surface.REST, Surface.CLI),
            )
        assert (
            conn.execute(
                "SELECT kind FROM control_plane_principals WHERE principal_key = 'human.alice'"
            ).fetchone()["kind"]
            == "human"
        )
    finally:
        conn.close()


def test_register_fail_closed_without_flag(auth_api) -> None:
    api, _ = auth_api
    resp = api.post(
        "/api/v1/auth/register",
        {"username": "alice", "password": "password123"},
        format="json",
    )
    assert resp.status_code == 403


def test_register_and_login_lifecycle(auth_api, monkeypatch) -> None:
    api, kernel = auth_api
    monkeypatch.setenv("ORCH_ALLOW_USER_REGISTRATION", "1")
    resp = api.post(
        "/api/v1/auth/register",
        {"username": "alice", "password": "password123", "display_name": "Alice"},
        format="json",
    )
    assert resp.status_code in {200, 202}
    body = resp.json()
    assert body["status"] == "applied"
    assert body["result"]["principal"]["kind"] == "human"
    assert "ops.read" not in body["result"]["principal"]["capabilities"]

    resp = api.post(
        "/api/v1/auth/login",
        {"username": "alice", "password": "password123"},
        format="json",
    )
    assert resp.status_code in {200, 202}
    session = resp.json()["result"]
    access = session["access"]["token"]
    refresh = session["refresh"]["token"]
    assert access and refresh
    dig = token_digest(access)
    row = kernel.connection.execute(
        "SELECT token_digest FROM control_plane_credentials WHERE token_digest = ?",
        (dig,),
    ).fetchone()
    assert row is not None

    api.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
    me = api.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["kind"] == "human"
    assert me.json()["principal_key"] == "human.alice"

    denied = api.post(
        "/api/v1/runtime/preview",
        {"work_item_id": "x", "provider": "codex"},
        format="json",
    )
    assert denied.status_code == 403

    rotated = api.post("/api/v1/auth/refresh", {"refresh_token": refresh}, format="json")
    assert rotated.status_code in {200, 202}
    new_refresh = rotated.json()["result"]["refresh"]["token"]
    replay = api.post("/api/v1/auth/refresh", {"refresh_token": refresh}, format="json")
    assert replay.status_code in {401, 409}
    assert replay.json().get("status") == "rejected" or replay.status_code == 401

    api.credentials(HTTP_AUTHORIZATION=f"Bearer {rotated.json()['result']['access']['token']}")
    logout = api.post("/api/v1/auth/logout", {"token": new_refresh}, format="json")
    assert logout.status_code in {200, 202}


def test_founder_can_register_when_flag_off(auth_api) -> None:
    api, _ = auth_api
    _auth(api, "founder")
    resp = api.post(
        "/api/v1/auth/register",
        {"username": "bob", "password": "password123"},
        format="json",
    )
    assert resp.status_code in {200, 202}
    assert resp.json()["status"] == "applied"


def test_legacy_service_token_still_resolves(auth_api) -> None:
    api, kernel = auth_api
    principal = resolve_by_token(kernel.connection, bootstrap_test_token_for("worker"))
    assert principal.kind == "worker"
    _auth(api, "worker")
    resp = api.get("/api/v1/delivery/jobs")
    assert resp.status_code in {200, 202}


def test_ops_summary_requires_auth(auth_api) -> None:
    api, _ = auth_api
    resp = api.get("/ops/summary/")
    assert resp.status_code in {401, 403}


def test_ops_summary_founder_ok(auth_api) -> None:
    api, _ = auth_api
    _auth(api, "founder")
    resp = api.get("/ops/summary/")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") in {"ok", "degraded"}
    assert "hierarchy" in body


def test_ops_summary_human_needs_ops_read(auth_api, monkeypatch) -> None:
    api, kernel = auth_api
    monkeypatch.setenv("ORCH_ALLOW_USER_REGISTRATION", "1")
    with transaction(kernel.connection):
        register_user(
            kernel.connection,
            username="carol",
            password="password123",
            allow_registration=True,
        )
        login = login_user(kernel.connection, username="carol", password="password123")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']['token']}")
    denied = api.get("/ops/summary/")
    assert denied.status_code == 403

    with transaction(kernel.connection):
        kernel.connection.execute(
            """
            UPDATE control_plane_principals
            SET capabilities_json = ?
            WHERE principal_key = 'human.carol'
            """,
            (json.dumps([OPS_READ_CAPABILITY]),),
        )
    with transaction(kernel.connection):
        login2 = login_user(kernel.connection, username="carol", password="password123")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {login2['access']['token']}")
    ok = api.get("/ops/summary/")
    assert ok.status_code == 200


def test_health_still_anonymous(auth_api) -> None:
    api, _ = auth_api
    resp = api.get("/health/")
    assert resp.status_code == 200


def test_pat_issue_and_resolve(auth_api, monkeypatch) -> None:
    api, kernel = auth_api
    monkeypatch.setenv("ORCH_ALLOW_USER_REGISTRATION", "1")
    with transaction(kernel.connection):
        register_user(
            kernel.connection,
            username="dave",
            password="password123",
            allow_registration=True,
        )
        login = login_user(kernel.connection, username="dave", password="password123")
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']['token']}")
    resp = api.post("/api/v1/auth/token", {"label": "ci"}, format="json")
    assert resp.status_code in {200, 202}
    pat = resp.json()["result"]["pat"]["token"]
    principal = resolve_by_token(kernel.connection, pat)
    assert principal.principal_key == "human.dave"


def test_refresh_reuse_revokes_family(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "reuse.db")
    try:
        with transaction(kernel.connection):
            register_user(
                kernel.connection,
                username="eve",
                password="password123",
                allow_registration=True,
            )
            session = login_user(kernel.connection, username="eve", password="password123")
            old_refresh = session["refresh"]["token"]
            refresh_session(kernel.connection, refresh_token=old_refresh)
        with pytest.raises(AuthRequiredError, match="reuse"):
            with transaction(kernel.connection):
                refresh_session(kernel.connection, refresh_token=old_refresh)
    finally:
        kernel.close()


def test_throttle_durable_across_connections(tmp_path) -> None:
    db = tmp_path / "throttle.db"
    k1 = Kernel.init(db)
    try:
        with transaction(k1.connection):
            for _ in range(10):
                result = throttle_check_and_bump(
                    k1.connection,
                    action="auth.login",
                    subject_key="ip:1.2.3.4",
                    max_hits=10,
                    window_sec=900,
                )
                assert result["allowed"] is True
        k1.connection.commit()
    finally:
        k1.close()

    k2 = Kernel.open(db)
    try:
        with transaction(k2.connection):
            blocked = throttle_check_and_bump(
                k2.connection,
                action="auth.login",
                subject_key="ip:1.2.3.4",
                max_hits=10,
                window_sec=900,
            )
        assert blocked["allowed"] is False
    finally:
        k2.close()


def test_human_denied_founder_matrix() -> None:
    with pytest.raises(AuthzDeniedError):
        assert_command_allowed_for_kind(
            command_type="runtime.preview",
            principal_kind="human",
            capabilities=(),
        )


def test_cli_credentials_mode_0600_no_secret_echo(tmp_path, monkeypatch, capsys) -> None:
    cred_path = tmp_path / "credentials.json"
    monkeypatch.setenv("ORCH_CREDENTIALS_PATH", str(cred_path))
    auth_cmds.save_stored_credentials(
        {
            "api_url": "http://127.0.0.1:8000",
            "access_token": "secret-access-token",
            "username": "alice",
        }
    )
    mode = cred_path.stat().st_mode
    assert mode & (stat.S_IRGRP | stat.S_IROTH) == 0
    assert mode & stat.S_IRUSR

    class Args:
        api_url = "http://127.0.0.1:8000"
        show_token = False
        json = True

    with patch.object(
        auth_cmds,
        "_http_json",
        return_value=(
            200,
            {
                "principal_key": "human.alice",
                "kind": "human",
                "role": "manager",
                "capabilities": [],
            },
        ),
    ):
        rc = auth_cmds._cmd_status(Args(), "http://127.0.0.1:8000")
    assert rc == 0
    out = capsys.readouterr().out
    assert "secret-access-token" not in out


def test_ops_console_load_summary_sends_bearer() -> None:
    app_js = Path(__file__).resolve().parents[2] / "ops-console" / "public" / "app.js"
    text = app_js.read_text(encoding="utf-8")
    assert 'apiFetch("/ops/summary/")' in text
    assert "orch_api_token" in text
    assert "Authorization" in text


def test_ops_summary_anonymous_rejected() -> None:
    import django
    from django.apps import apps
    from django.conf import settings

    if not settings.configured:
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "flow_engine.control_plane.settings")
    if not apps.ready:
        django.setup()
    client = APIClient()
    with patch(
        "flow_engine.control_plane.api.authentication.OrchestratorPrincipalAuthentication.authenticate",
        return_value=None,
    ):
        response = client.get("/ops/summary/")
    assert response.status_code in {401, 403}
