"""Adversarial / negative tests for R4A security review findings."""

from __future__ import annotations

from pathlib import Path

import pytest

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.application.runtime_service import (
    claim_attempt,
    create_run,
    dispatch_provider_call,
)
from flow_engine.application.worker_delivery import (
    prepare_worker_delivery,
    settle_worker_delivery,
)
from flow_engine.control_plane.authz_matrix import assert_command_allowed_for_kind
from flow_engine.control_plane.bootstrap import (
    bootstrap_principals_from_env,
    bootstrap_test_principals,
)
from flow_engine.control_plane.delivery_registry import claim_delivery_job
from flow_engine.control_plane.service_auth import authenticate_service_credential
from flow_engine.coordinator import (
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    SystemTestGrant,
)
from flow_engine.domain.errors import AuthRequiredError, AuthzDeniedError, ConflictError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


def test_f1_service_credentials_must_be_distinct(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_API_SERVICE_TOKEN", "same")
    monkeypatch.setenv("ORCH_WORKER_SERVICE_TOKEN", "same")
    with pytest.raises(AuthzDeniedError):
        authenticate_service_credential(credential="same", claimed_kind="api")


def test_f1_missing_service_credential_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_API_SERVICE_TOKEN", "api-secret")
    monkeypatch.setenv("ORCH_WORKER_SERVICE_TOKEN", "worker-secret")
    with pytest.raises(AuthRequiredError):
        authenticate_service_credential(credential=None, claimed_kind="api")


def test_f1_wrong_service_credential_rejected(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_API_SERVICE_TOKEN", "api-secret")
    monkeypatch.setenv("ORCH_WORKER_SERVICE_TOKEN", "worker-secret")
    with pytest.raises(AuthzDeniedError):
        authenticate_service_credential(credential="nope", claimed_kind="api")


def test_f1_compose_does_not_publish_9001() -> None:
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "9001:9001" not in text
    assert "Never publish 9001" in text or "never publish" in text.lower()


def test_f2_runtime_bootstrap_has_no_fixed_tokens() -> None:
    from flow_engine.control_plane import bootstrap as boot

    assert not hasattr(boot, "DEV_TOKENS") or boot.DEV_TOKENS != {
        "founder": "local-dev-founder"
    }
    assert "local-dev-founder" not in boot.TEST_FIXTURE_TOKENS.values()
    assert boot.TEST_FIXTURE_TOKENS["founder"].startswith("test-fixture-")


def test_f2_bootstrap_from_env_fail_closed(monkeypatch, tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "boot.db")
    try:
        for key in (
            "ORCH_TOKEN_FOUNDER",
            "ORCH_TOKEN_SCHEDULER",
            "ORCH_TOKEN_MCP",
            "ORCH_TOKEN_MCP_CONTEXT_ASSETS",
            "ORCH_TOKEN_MCP_WORKFLOW_CONTROL",
            "ORCH_TOKEN_MCP_DELEGATION_COORDINATION",
            "ORCH_TOKEN_MCP_EVIDENCE_GOVERNANCE",
            "ORCH_TOKEN_MCP_MAINTENANCE",
            "ORCH_TOKEN_MCP_SKILLS_SCRIPTS",
            "ORCH_TOKEN_WORKER",
            "ORCH_TOKEN_PROVIDER_INVOCATION",
        ):
            monkeypatch.delenv(key, raising=False)
        with pytest.raises(RuntimeError, match="failed closed"):
            with transaction(kernel.connection):
                bootstrap_principals_from_env(kernel.connection)
    finally:
        kernel.close()


def test_f2_settings_reject_wildcard_hosts() -> None:
    from flow_engine.control_plane.settings import validate_runtime_settings

    with pytest.raises(RuntimeError, match="wildcard"):
        validate_runtime_settings(
            secret_key="x",
            allowed_hosts="*",
            testing=False,
        )


def test_f2_settings_require_secret() -> None:
    from flow_engine.control_plane.settings import validate_runtime_settings

    with pytest.raises(RuntimeError, match="DJANGO_SECRET_KEY"):
        validate_runtime_settings(
            secret_key="",
            allowed_hosts="localhost",
            testing=False,
        )


def test_f2_api_port_loopback_only() -> None:
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "127.0.0.1:8000:8000" in text
    assert '"8000:8000"' not in text.replace("127.0.0.1:8000:8000", "")


def test_f3_matrix_denies_mcp_recovery() -> None:
    with pytest.raises(AuthzDeniedError):
        assert_command_allowed_for_kind(
            command_type="runtime.recover_restart",
            principal_kind="mcp_service",
        )


def test_f3_matrix_denies_worker_founder_ops() -> None:
    with pytest.raises(AuthzDeniedError):
        assert_command_allowed_for_kind(
            command_type="runtime.waive_gate",
            principal_kind="worker",
        )


def test_f3_matrix_allows_explicit_recovery_capability() -> None:
    assert_command_allowed_for_kind(
        command_type="runtime.recover_restart",
        principal_kind="worker",
        capabilities=("recovery.control_plane",),
    )


def test_f3_coordinator_denies_registered_mcp_recovery(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "matrix.db")
    try:
        with transaction(kernel.connection):
            principals = bootstrap_test_principals(kernel.connection)
            assert "mcp-service" in principals
            row = kernel.connection.execute(
                "SELECT id FROM control_plane_principals WHERE principal_key = 'mcp-service'"
            ).fetchone()
            coord = StateCoordinator(kernel.connection)
            envelope = coord.accept(
                RuntimeCommand(
                    command_type="runtime.recover_restart",
                    target_id=None,
                    payload={},
                    context=CommandContext(
                        principal_id=row["id"],
                        role=PrincipalRole.WORKER,
                        surface=Surface.REST,
                    ),
                )
            )
        assert envelope["status"] == "rejected"
        assert envelope["error_code"] == "AUTHZ_DENIED"
    finally:
        kernel.close()


def test_f4_redis_not_published() -> None:
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "6379:6379" not in text
    assert "requirepass" in text


def test_f4_delivery_ownership_binding(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "own.db")
    try:
        with transaction(kernel.connection):
            init_project(kernel.connection, name="demo")
            ensure_queue(kernel.connection, name="default")
            item = submit_work(kernel.connection, queue_name="default", payload={}, actor="x")
            grant = SystemTestGrant(
                grant_id="g1",
                principal_id="worker",
                role=PrincipalRole.WORKER,
                surfaces=(Surface.WORKER, Surface.TEST),
                providers=("codex",),
                budget_scope_id="acceptance-campaign-r4",
            )
            created = create_run(
                kernel.connection,
                work_item_id=item["id"],
                provider="codex",
                grant=grant,
                actor="worker",
            )
            claim_attempt(kernel.connection, run_id=created["run"]["id"], actor="worker")
            dispatched = dispatch_provider_call(
                kernel.connection,
                attempt_id=created["attempt"]["id"],
                actor="worker",
                delivery_mode="async",
            )
            job_id = dispatched["delivery"]["delivery_job_id"]
            with pytest.raises(ConflictError):
                claim_delivery_job(
                    kernel.connection,
                    job_id=job_id,
                    worker_principal_id="worker-a",
                    attempt_id="wrong-attempt",
                    invocation_id=dispatched["invocation"]["id"],
                )
    finally:
        kernel.close()


def test_f5_ambiguous_delivery_is_outcome_unknown(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "amb.db")
    try:
        with transaction(kernel.connection):
            init_project(kernel.connection, name="demo")
            ensure_queue(kernel.connection, name="default")
            item = submit_work(kernel.connection, queue_name="default", payload={}, actor="x")
            grant = SystemTestGrant(
                grant_id="g1",
                principal_id="worker",
                role=PrincipalRole.WORKER,
                surfaces=(Surface.WORKER, Surface.TEST),
                providers=("codex",),
                budget_scope_id="acceptance-campaign-r4",
            )
            created = create_run(
                kernel.connection,
                work_item_id=item["id"],
                provider="codex",
                grant=grant,
                actor="worker",
            )
            claim_attempt(kernel.connection, run_id=created["run"]["id"], actor="worker")
            dispatched = dispatch_provider_call(
                kernel.connection,
                attempt_id=created["attempt"]["id"],
                actor="worker",
                delivery_mode="async",
            )
            prepared = prepare_worker_delivery(
                kernel.connection,
                attempt_id=dispatched["attempt"]["id"],
                delivery_job_id=dispatched["delivery"]["delivery_job_id"],
                worker_principal_id="worker",
            )
            settled = settle_worker_delivery(
                kernel.connection,
                prepared=prepared,
                provider_result=None,
                actor="worker",
                ambiguous=True,
            )
        assert settled["provider_result"]["outcome"] == "outcome_unknown"
        run = kernel.connection.execute(
            "SELECT status FROM runtime_runs WHERE id = ?",
            (dispatched["run"]["id"],),
        ).fetchone()
        assert run["status"] == "outcome_unknown"
        # Replay blocked
        with transaction(kernel.connection):
            with pytest.raises(ConflictError, match="reconciliation"):
                prepare_worker_delivery(
                    kernel.connection,
                    attempt_id=dispatched["attempt"]["id"],
                    delivery_job_id=dispatched["delivery"]["delivery_job_id"],
                    worker_principal_id="worker",
                )
    finally:
        kernel.close()


def test_f5_accept_worker_deliver_rejects_nested_accept(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "nested.db")
    try:
        with transaction(kernel.connection):
            init_project(kernel.connection, name="demo")
            ensure_queue(kernel.connection, name="default")
            item = submit_work(kernel.connection, queue_name="default", payload={}, actor="x")
            grant = SystemTestGrant(
                grant_id="g1",
                principal_id="worker",
                role=PrincipalRole.WORKER,
                surfaces=(Surface.WORKER, Surface.TEST),
                providers=("codex",),
                budget_scope_id="acceptance-campaign-r4",
            )
            created = create_run(
                kernel.connection,
                work_item_id=item["id"],
                provider="codex",
                grant=grant,
                actor="worker",
            )
            claim_attempt(kernel.connection, run_id=created["run"]["id"], actor="worker")
            dispatched = dispatch_provider_call(
                kernel.connection,
                attempt_id=created["attempt"]["id"],
                actor="worker",
                delivery_mode="async",
            )
            coord = StateCoordinator(kernel.connection)
            envelope = coord.accept(
                RuntimeCommand(
                    command_type="runtime.worker_deliver",
                    target_id=dispatched["attempt"]["id"],
                    payload={
                        "attempt_id": dispatched["attempt"]["id"],
                        "delivery_job_id": dispatched["delivery"]["delivery_job_id"],
                    },
                    context=CommandContext(
                        principal_id="worker",
                        role=PrincipalRole.WORKER,
                        surface=Surface.WORKER,
                        grant=grant,
                    ),
                )
            )
        assert envelope["status"] == "rejected"
        assert "accept_worker_deliver" in (envelope.get("error") or "")
    finally:
        kernel.close()


def test_f6_compose_hardening_markers() -> None:
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "cap_drop:" in text
    assert "no-new-privileges" in text
    assert "read_only: true" in text
    assert "internal: true" in text
    assert "mem_limit:" in text
    dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
    df = dockerfile.read_text(encoding="utf-8")
    assert "USER orch" in df
    assert "useradd" in df


def _compose_service_environment_keys(compose_text: str, service: str) -> set[str]:
    """Extract environment variable names for a top-level Compose service."""
    lines = compose_text.splitlines()
    in_services = False
    in_service = False
    in_environment = False
    keys: set[str] = set()
    for line in lines:
        if line.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if line.startswith("volumes:") or line.startswith("networks:"):
            break
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            name = line.strip().rstrip(":")
            in_service = name == service
            in_environment = False
            continue
        if not in_service:
            continue
        stripped = line.strip()
        if stripped.startswith("environment:"):
            in_environment = True
            continue
        if in_environment:
            if line.startswith("    ") and not line.startswith("      "):
                # Next sibling key under the service (e.g. depends_on, ports).
                if ":" in stripped and not stripped.startswith("-"):
                    in_environment = False
                    continue
            if stripped.startswith("#"):
                continue
            if ":" in stripped:
                key = stripped.split(":", 1)[0].strip()
                if key and key.replace("_", "").isalnum() and key[0].isalpha():
                    keys.add(key)
    return keys


def test_compose_credential_projection_no_cross_service_tokens() -> None:
    """Coordinator gets both service tokens; API/worker must not cross-project."""
    compose = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    coordinator_env = _compose_service_environment_keys(text, "coordinator")
    api_env = _compose_service_environment_keys(text, "api")
    worker_env = _compose_service_environment_keys(text, "worker")

    assert "ORCH_API_SERVICE_TOKEN" in coordinator_env
    assert "ORCH_WORKER_SERVICE_TOKEN" in coordinator_env

    assert "ORCH_API_SERVICE_TOKEN" in api_env
    assert "ORCH_WORKER_SERVICE_TOKEN" not in api_env

    assert "ORCH_WORKER_SERVICE_TOKEN" in worker_env
    assert "ORCH_API_SERVICE_TOKEN" not in worker_env

    # Adversarial: raw service blocks must not assign the peer credential.
    api_start = text.index("\n  api:\n")
    worker_start = text.index("\n  worker:\n")
    worker_end = text.index("\n  script-worker:\n")
    api_block = text[api_start:worker_start]
    worker_block = text[worker_start:worker_end]
    assert "ORCH_API_SERVICE_TOKEN:" in api_block
    assert "ORCH_WORKER_SERVICE_TOKEN:" not in api_block
    assert "ORCH_WORKER_SERVICE_TOKEN:" in worker_block
    assert "ORCH_API_SERVICE_TOKEN:" not in worker_block

    # R4B: each MCP lane gets only its own projected token env (no worker/API/DB).
    for service in (
        "mcp-workflow-control",
        "mcp-context-assets",
        "mcp-maintenance",
        "mcp-delegation-coordination",
        "mcp-evidence-governance",
        "mcp-skills-scripts",
    ):
        env_keys = _compose_service_environment_keys(text, service)
        assert "ORCH_MCP_LANE_TOKEN" in env_keys
        assert "ORCH_API_BASE_URL" in env_keys
        assert "ORCH_MCP_LANE_ID" in env_keys
        assert "ORCH_WORKER_SERVICE_TOKEN" not in env_keys
        assert "ORCH_API_SERVICE_TOKEN" not in env_keys
        assert "FLOW_DB_PATH" not in env_keys
        assert "COORDINATOR_URL" not in env_keys
        assert "REDIS_PASSWORD" not in env_keys
