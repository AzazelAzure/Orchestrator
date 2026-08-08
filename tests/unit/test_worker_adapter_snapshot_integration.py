"""Coordinator-path adapter snapshot and binding integration (bootstrap follow-up)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from tests.unit.test_provider_host_runner import _binding, _invoke_packet

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.application.runtime_service import (
    claim_attempt,
    create_run,
    dispatch_provider_call,
)
from flow_engine.application.worker_delivery import (
    ADAPTER_SNAPSHOT_FIELDS,
    LEGACY_ADAPTER_SNAPSHOT_FIELDS,
    _legacy_invocation_binding_fields,
    persist_adapter_snapshot,
    preflight_worker_delivery,
    prepare_worker_delivery,
    settle_external_worker_delivery,
)
from flow_engine.control_plane.bootstrap import bootstrap_test_principals
from flow_engine.coordinator import (
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    SystemTestGrant,
)
from flow_engine.domain.errors import ConflictError, ValidationFailedError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence.transactions import transaction
from flow_engine.providers.cli_registry import (
    EXECUTION_PROFILE_ACCEPTANCE,
    EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE,
    EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
)
from flow_engine.providers.host_runner import HostRunner, digest_json


def _fake_cli(tmp_path: Path, provider: str) -> Path:
    versions = {
        "codex": "0.146.0",
        "cursor": "2026.08.04-aaa8809",
        "claude": "2.1.212",
    }
    path = tmp_path / provider
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        f" print('{provider} {versions[provider]}'); raise SystemExit(0)\n"
        "if 'status' in sys.argv or 'auth' in sys.argv or 'login' in sys.argv:\n"
        " raise SystemExit(0)\n"
        "print(json.dumps({'type':'result','subtype':'success','provider_call_id':'call-1','result':'ok'}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _dispatch_invocation(kernel_db, provider: str) -> dict[str, str]:
    with transaction(kernel_db.connection):
        init_project(kernel_db.connection, name="adapter-snapshot", actor="test")
        ensure_queue(kernel_db.connection, name="default")
        bootstrap_test_principals(kernel_db.connection)
        item = submit_work(
            kernel_db.connection,
            queue_name="default",
            payload={"acceptance_probe": True, "provider": provider},
            actor="test",
        )
        grant = SystemTestGrant(
            grant_id=f"grant-{provider}",
            principal_id=f"worker.provider.{provider}",
            role=PrincipalRole.WORKER,
            surfaces=(Surface.WORKER, Surface.TEST),
            providers=(provider,),
            budget_scope_id=f"acceptance-adapter-{provider}",
        )
        created = create_run(
            kernel_db.connection,
            work_item_id=item["id"],
            provider=provider,
            grant=grant,
            actor=f"worker.provider.{provider}",
            packet={"acceptance_probe": True, "provider": provider},
        )
        claim_attempt(
            kernel_db.connection,
            run_id=created["run"]["id"],
            actor=f"worker.provider.{provider}",
        )
        dispatched = dispatch_provider_call(
            kernel_db.connection,
            attempt_id=created["attempt"]["id"],
            actor=f"worker.provider.{provider}",
            delivery_mode="async",
        )
    return {
        "invocation_id": dispatched["invocation"]["id"],
        "attempt_id": created["attempt"]["id"],
        "job_id": dispatched["delivery"]["delivery_job_id"],
        "run_id": created["run"]["id"],
    }


def _handshake_snapshot(tmp_path: Path, provider: str) -> dict[str, object]:
    runner = HostRunner(_binding(tmp_path, provider))
    return runner.handshake()


def _legacy_snapshot(provider: str) -> dict[str, object]:
    return {
        "protocol_version": 1,
        "provider": provider,
        "adapter_version": "1",
        "executable_name": provider,
        "executable_digest": "abc123",
        "cli_version": "0.144.6",
        "auth_ready": True,
        "structured_output": "jsonl",
        "resolved_model": f"{provider}-test-model",
        "model_resolution": "installation_allowed_pin",
        "acceptance_policy": "isolated-empty-read-only-no-tool",
        "binding_digest": "legacy-inner-binding-digest",
    }


def _worker_snapshot_command(
    coord: StateCoordinator,
    ctx: CommandContext,
    ids: dict[str, str],
    provider: str,
    snapshot: dict[str, object],
    snapshot_digest: str,
    *,
    idempotency_key: str,
) -> dict[str, object]:
    return coord.accept(
        RuntimeCommand(
            command_type="runtime.worker_snapshot",
            target_id=ids["invocation_id"],
            payload={
                "invocation_id": ids["invocation_id"],
                "provider": provider,
                "snapshot": snapshot,
                "snapshot_digest": snapshot_digest,
            },
            idempotency_key=idempotency_key,
            context=ctx,
        )
    )


def _pin_legacy_snapshot_row(
    conn,
    *,
    invocation_id: str,
    provider: str,
    attempt_id: str,
    packet_digest: str,
    credit_reservation_id: str,
) -> tuple[str, str]:
    legacy = _legacy_snapshot(provider)
    snapshot_digest = digest_json(legacy)
    binding = _legacy_invocation_binding_fields(
        provider=provider,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
        credit_reservation_id=credit_reservation_id,
        packet_digest=packet_digest,
        snapshot_digest=snapshot_digest,
        resolved_model=str(legacy["resolved_model"]),
        adapter_version=str(legacy["adapter_version"]),
    )
    binding_digest = digest_json(binding)
    conn.execute(
        """
        UPDATE provider_invocations
        SET adapter_snapshot_json = ?, adapter_snapshot_digest = ?, binding_digest = ?
        WHERE id = ?
        """,
        (
            json.dumps(legacy, sort_keys=True),
            snapshot_digest,
            binding_digest,
            invocation_id,
        ),
    )
    return snapshot_digest, binding_digest


@pytest.mark.parametrize("provider", ["codex", "cursor", "claude"])
def test_persist_adapter_snapshot_accepts_bootstrap_handshake(
    kernel_db, tmp_path: Path, provider: str
) -> None:
    ids = _dispatch_invocation(kernel_db, provider)
    handshake = _handshake_snapshot(tmp_path, provider)
    snapshot = handshake["snapshot"]
    assert set(snapshot) == ADAPTER_SNAPSHOT_FIELDS
    with transaction(kernel_db.connection):
        pinned = persist_adapter_snapshot(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider=provider,
            snapshot=snapshot,
            snapshot_digest=handshake["snapshot_digest"],
            actor=f"worker.provider.{provider}",
        )
    assert pinned["binding"]["execution_profile"] == EXECUTION_PROFILE_ACCEPTANCE
    assert pinned["binding_digest"] == digest_json(pinned["binding"])


@pytest.mark.parametrize(
    ("provider", "profile"),
    [
        ("cursor", EXECUTION_PROFILE_CURSOR_IMPLEMENTATION),
        ("claude", EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE),
    ],
)
def test_persist_adapter_snapshot_accepts_non_acceptance_profiles(
    kernel_db, tmp_path: Path, provider: str, profile: str
) -> None:
    ids = _dispatch_invocation(kernel_db, provider)
    binding = _binding(tmp_path, provider, execution_profile=profile)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    with transaction(kernel_db.connection):
        pinned = persist_adapter_snapshot(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider=provider,
            snapshot=handshake["snapshot"],
            snapshot_digest=handshake["snapshot_digest"],
            actor=f"worker.provider.{provider}",
        )
    assert pinned["binding"]["execution_profile"] == profile


def test_persist_adapter_snapshot_rejects_stale_field_set(kernel_db, tmp_path: Path) -> None:
    ids = _dispatch_invocation(kernel_db, "codex")
    handshake = _handshake_snapshot(tmp_path, "codex")
    stale = dict(handshake["snapshot"])
    stale.pop("execution_profile")
    with transaction(kernel_db.connection):
        with pytest.raises(ValidationFailedError, match="fields mismatch"):
            persist_adapter_snapshot(
                kernel_db.connection,
                invocation_id=ids["invocation_id"],
                provider="codex",
                snapshot=stale,
                snapshot_digest=digest_json(stale),
                actor="worker.provider.codex",
            )


def test_persist_adapter_snapshot_replay_is_immutable(kernel_db, tmp_path: Path) -> None:
    ids = _dispatch_invocation(kernel_db, "codex")
    handshake = _handshake_snapshot(tmp_path, "codex")
    with transaction(kernel_db.connection):
        first = persist_adapter_snapshot(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider="codex",
            snapshot=handshake["snapshot"],
            snapshot_digest=handshake["snapshot_digest"],
            actor="worker.provider.codex",
        )
        second = persist_adapter_snapshot(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider="codex",
            snapshot=handshake["snapshot"],
            snapshot_digest=handshake["snapshot_digest"],
            actor="worker.provider.codex",
        )
    assert first["binding_digest"] == second["binding_digest"]
    with transaction(kernel_db.connection):
        tampered = dict(handshake["snapshot"])
        tampered["resolved_model"] = "other-model"
        with pytest.raises(ConflictError, match="immutable adapter snapshot"):
            persist_adapter_snapshot(
                kernel_db.connection,
                invocation_id=ids["invocation_id"],
                provider="codex",
                snapshot=tampered,
                snapshot_digest=digest_json(tampered),
                actor="worker.provider.codex",
            )


def test_coordinator_worker_snapshot_command_applies(kernel_db, tmp_path: Path) -> None:
    provider = "cursor"
    ids = _dispatch_invocation(kernel_db, provider)
    handshake = _handshake_snapshot(tmp_path, provider)
    coord = StateCoordinator(kernel_db.connection)
    ctx = CommandContext(
        principal_id=f"worker.provider.{provider}",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=None,
    )
    envelope = coord.accept(
        RuntimeCommand(
            command_type="runtime.worker_snapshot",
            target_id=ids["invocation_id"],
            payload={
                "invocation_id": ids["invocation_id"],
                "provider": provider,
                "snapshot": handshake["snapshot"],
                "snapshot_digest": handshake["snapshot_digest"],
            },
            idempotency_key=f"snapshot|{ids['invocation_id']}",
            context=ctx,
        )
    )
    assert envelope["status"] == "applied"
    pinned = envelope["result"]["snapshot"]
    assert pinned["binding"]["execution_profile"] == EXECUTION_PROFILE_ACCEPTANCE


def test_host_runner_validate_packet_matches_pinned_binding(kernel_db, tmp_path: Path) -> None:
    provider = "claude"
    ids = _dispatch_invocation(kernel_db, provider)
    runner = HostRunner(_binding(tmp_path, provider))
    handshake = runner.handshake()
    with transaction(kernel_db.connection):
        pinned = persist_adapter_snapshot(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider=provider,
            snapshot=handshake["snapshot"],
            snapshot_digest=handshake["snapshot_digest"],
            actor=f"worker.provider.{provider}",
        )
        preflight = preflight_worker_delivery(
            kernel_db.connection,
            attempt_id=ids["attempt_id"],
            delivery_job_id=ids["job_id"],
            worker_principal_id=f"worker.provider.{provider}",
        )
    packet = {
        "invocation_id": ids["invocation_id"],
        "attempt_id": ids["attempt_id"],
        "provider": provider,
        "credit_reservation_id": pinned["binding"]["credit_reservation_id"],
        "packet_digest": pinned["binding"]["packet_digest"],
        "snapshot_digest": handshake["snapshot_digest"],
        "binding_digest": pinned["binding_digest"],
        "execution_profile": pinned["binding"]["execution_profile"],
        "task_packet": preflight["payload"],
        "cwd": ".",
    }
    validated = runner.validate_packet(packet)
    assert validated["execution_profile"] == EXECUTION_PROFILE_ACCEPTANCE


def test_validate_packet_rejects_binding_digest_without_execution_profile(
    tmp_path: Path,
) -> None:
    runner = HostRunner(_binding(tmp_path, "codex"))
    handshake = runner.handshake()
    packet = _invoke_packet(runner, handshake)
    stale_digest = digest_json({
        "provider": "codex",
        "attempt_id": packet["attempt_id"],
        "invocation_id": packet["invocation_id"],
        "credit_reservation_id": packet["credit_reservation_id"],
        "packet_digest": packet["packet_digest"],
        "snapshot_digest": packet["snapshot_digest"],
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    })
    packet["binding_digest"] = stale_digest
    with pytest.raises(PermissionError, match="binding digest mismatch"):
        runner.validate_packet(packet)


def test_coordinator_rejects_unknown_profile_then_accepts_valid_snapshot(
    kernel_db, tmp_path: Path
) -> None:
    provider = "codex"
    ids = _dispatch_invocation(kernel_db, provider)
    handshake = _handshake_snapshot(tmp_path, provider)
    coord = StateCoordinator(kernel_db.connection)
    ctx = CommandContext(
        principal_id=f"worker.provider.{provider}",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=None,
    )
    bad = dict(handshake["snapshot"])
    bad["execution_profile"] = "unknown-profile"
    rejected = _worker_snapshot_command(
        coord,
        ctx,
        ids,
        provider,
        bad,
        digest_json(bad),
        idempotency_key="snapshot-bad-profile",
    )
    assert rejected["status"] == "rejected"
    assert rejected["error_code"] == "VALIDATION_FAILED"

    applied = _worker_snapshot_command(
        coord,
        ctx,
        ids,
        provider,
        handshake["snapshot"],
        handshake["snapshot_digest"],
        idempotency_key="snapshot-good-profile",
    )
    assert applied["status"] == "applied"
    assert applied["result"]["snapshot"]["binding"]["execution_profile"] == EXECUTION_PROFILE_ACCEPTANCE


def test_coordinator_rejects_incompatible_profile_then_accepts_valid_snapshot(
    kernel_db, tmp_path: Path
) -> None:
    provider = "codex"
    ids = _dispatch_invocation(kernel_db, provider)
    handshake = _handshake_snapshot(tmp_path, provider)
    coord = StateCoordinator(kernel_db.connection)
    ctx = CommandContext(
        principal_id=f"worker.provider.{provider}",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=None,
    )
    bad = dict(handshake["snapshot"])
    bad["execution_profile"] = EXECUTION_PROFILE_CURSOR_IMPLEMENTATION
    rejected = _worker_snapshot_command(
        coord,
        ctx,
        ids,
        provider,
        bad,
        digest_json(bad),
        idempotency_key="snapshot-incompatible-profile",
    )
    assert rejected["status"] == "rejected"
    assert rejected["error_code"] == "VALIDATION_FAILED"

    applied = _worker_snapshot_command(
        coord,
        ctx,
        ids,
        provider,
        handshake["snapshot"],
        handshake["snapshot_digest"],
        idempotency_key="snapshot-compatible-profile",
    )
    assert applied["status"] == "applied"


def test_legacy_pinned_snapshot_settles_after_upgrade(kernel_db) -> None:
    provider = "codex"
    ids = _dispatch_invocation(kernel_db, provider)
    with transaction(kernel_db.connection):
        row = kernel_db.connection.execute(
            "SELECT request_digest FROM provider_invocations WHERE id = ?",
            (ids["invocation_id"],),
        ).fetchone()
        credit = kernel_db.connection.execute(
            """
            SELECT id FROM credit_entries
            WHERE invocation_id = ? AND kind = 'reservation'
            ORDER BY created_at LIMIT 1
            """,
            (ids["invocation_id"],),
        ).fetchone()
        snapshot_digest, binding_digest = _pin_legacy_snapshot_row(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider=provider,
            attempt_id=ids["attempt_id"],
            packet_digest=row["request_digest"],
            credit_reservation_id=credit["id"],
        )
        prepared = prepare_worker_delivery(
            kernel_db.connection,
            attempt_id=ids["attempt_id"],
            delivery_job_id=ids["job_id"],
            worker_principal_id=f"worker.provider.{provider}",
        )
        settled = settle_external_worker_delivery(
            kernel_db.connection,
            prepared=prepared,
            provider_result={
                "outcome": "complete",
                "evidence": {"mock": True},
                "anomalies": [],
                "delivery_id": "call-legacy-1",
                "provider_call_id": "call-legacy-1",
                "snapshot_digest": snapshot_digest,
                "binding_digest": binding_digest,
            },
            actor=f"worker.provider.{provider}",
        )
    assert settled["provider_result"]["outcome"] == "complete"


def test_legacy_settlement_rejects_tampered_binding_digest(kernel_db) -> None:
    provider = "codex"
    ids = _dispatch_invocation(kernel_db, provider)
    with transaction(kernel_db.connection):
        row = kernel_db.connection.execute(
            "SELECT request_digest FROM provider_invocations WHERE id = ?",
            (ids["invocation_id"],),
        ).fetchone()
        credit = kernel_db.connection.execute(
            """
            SELECT id FROM credit_entries
            WHERE invocation_id = ? AND kind = 'reservation'
            ORDER BY created_at LIMIT 1
            """,
            (ids["invocation_id"],),
        ).fetchone()
        snapshot_digest, binding_digest = _pin_legacy_snapshot_row(
            kernel_db.connection,
            invocation_id=ids["invocation_id"],
            provider=provider,
            attempt_id=ids["attempt_id"],
            packet_digest=row["request_digest"],
            credit_reservation_id=credit["id"],
        )
        prepared = prepare_worker_delivery(
            kernel_db.connection,
            attempt_id=ids["attempt_id"],
            delivery_job_id=ids["job_id"],
            worker_principal_id=f"worker.provider.{provider}",
        )
        with pytest.raises(ConflictError, match="provider callback binding digest mismatch"):
            settle_external_worker_delivery(
                kernel_db.connection,
                prepared=prepared,
                provider_result={
                    "outcome": "complete",
                    "evidence": {},
                    "anomalies": [],
                    "delivery_id": "call-legacy-2",
                    "provider_call_id": "call-legacy-2",
                    "snapshot_digest": snapshot_digest,
                    "binding_digest": "tampered-binding-digest",
                },
                actor=f"worker.provider.{provider}",
            )


def test_hybrid_snapshot_schema_rejected_at_settlement(kernel_db) -> None:
    provider = "codex"
    ids = _dispatch_invocation(kernel_db, provider)
    hybrid = _legacy_snapshot(provider)
    hybrid["execution_profile"] = EXECUTION_PROFILE_ACCEPTANCE
    snapshot_digest = digest_json(hybrid)
    with transaction(kernel_db.connection):
        row = kernel_db.connection.execute(
            "SELECT request_digest FROM provider_invocations WHERE id = ?",
            (ids["invocation_id"],),
        ).fetchone()
        credit = kernel_db.connection.execute(
            """
            SELECT id FROM credit_entries
            WHERE invocation_id = ? AND kind = 'reservation'
            ORDER BY created_at LIMIT 1
            """,
            (ids["invocation_id"],),
        ).fetchone()
        binding_digest = digest_json(
            _legacy_invocation_binding_fields(
                provider=provider,
                attempt_id=ids["attempt_id"],
                invocation_id=ids["invocation_id"],
                credit_reservation_id=credit["id"],
                packet_digest=row["request_digest"],
                snapshot_digest=snapshot_digest,
                resolved_model=str(hybrid["resolved_model"]),
                adapter_version=str(hybrid["adapter_version"]),
            )
        )
        kernel_db.connection.execute(
            """
            UPDATE provider_invocations
            SET adapter_snapshot_json = ?, adapter_snapshot_digest = ?, binding_digest = ?
            WHERE id = ?
            """,
            (
                json.dumps(hybrid, sort_keys=True),
                snapshot_digest,
                binding_digest,
                ids["invocation_id"],
            ),
        )
        prepared = prepare_worker_delivery(
            kernel_db.connection,
            attempt_id=ids["attempt_id"],
            delivery_job_id=ids["job_id"],
            worker_principal_id=f"worker.provider.{provider}",
        )
        with pytest.raises(ValidationFailedError, match="hybrid or unknown"):
            settle_external_worker_delivery(
                kernel_db.connection,
                prepared=prepared,
                provider_result={
                    "outcome": "complete",
                    "evidence": {},
                    "anomalies": [],
                    "delivery_id": "call-hybrid",
                    "provider_call_id": "call-hybrid",
                    "snapshot_digest": snapshot_digest,
                    "binding_digest": binding_digest,
                },
                actor=f"worker.provider.{provider}",
            )


def test_legacy_schema_fields_are_exact(kernel_db) -> None:
    assert set(_legacy_snapshot("codex")) == LEGACY_ADAPTER_SNAPSHOT_FIELDS
