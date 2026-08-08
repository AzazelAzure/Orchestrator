"""Worker-side mock provider delivery (coordinator-only).

Provider I/O runs outside the SQLite transaction. Durable dispatch intent is
recorded first via an exclusive CAS lease bound to the command idempotency key;
ambiguous completion becomes outcome_unknown and blocks replay until
reconciliation. Duplicate/redelivered tasks return cached or in-progress
envelopes without another provider call.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.application.runtime_service import (
    get_attempt,
    get_invocation_for_attempt,
    get_run,
    submit_result,
)
from flow_engine.control_plane.delivery_registry import (
    acquire_exclusive_dispatch_lease,
    assert_delivery_ownership,
    complete_delivery_job,
    get_delivery_job,
    mark_delivery_delivered,
    mark_delivery_outcome_unknown,
)
from flow_engine.coordinator.audit import append_audit_event
from flow_engine.coordinator.commands import RuntimeCommand
from flow_engine.coordinator.coordinator import StateCoordinator
from flow_engine.domain.errors import (
    ConflictError,
    IdempotencyReplayError,
    NotFoundError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import AttemptStatus, InvocationStatus, RunStatus
from flow_engine.persistence.transactions import transaction
from flow_engine.providers.cli_registry import validate_execution_profile
from flow_engine.providers.host_runner import digest_json
from flow_engine.providers.protocol import (
    InvocationRequest,
    ProviderRunner,
    default_mock_registry,
)

ADAPTER_SNAPSHOT_FIELDS = frozenset({
    "protocol_version",
    "provider",
    "adapter_version",
    "executable_name",
    "executable_digest",
    "cli_version",
    "cli_version_pin",
    "event_schema",
    "auth_ready",
    "structured_output",
    "resolved_model",
    "model_resolution",
    "acceptance_policy",
    "execution_profile",
    "binding_digest",
})


def _invocation_binding_fields(
    *,
    provider: str,
    attempt_id: str,
    invocation_id: str,
    credit_reservation_id: str,
    packet_digest: str,
    snapshot_digest: str,
    resolved_model: str,
    adapter_version: str,
    execution_profile: str,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "attempt_id": attempt_id,
        "invocation_id": invocation_id,
        "credit_reservation_id": credit_reservation_id,
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot_digest,
        "resolved_model": resolved_model,
        "adapter_version": adapter_version,
        "execution_profile": execution_profile,
    }


def _record_runner_event(
    conn: sqlite3.Connection,
    *,
    invocation_id: str,
    event_type: str,
    event: dict[str, Any],
) -> None:
    encoded = json.dumps(event, sort_keys=True)
    if len(encoded.encode()) > 32_768:
        raise ValidationFailedError("runner event exceeds cap")
    digest = digest_json({"event_type": event_type, "event": event})
    conn.execute(
        """
        INSERT OR IGNORE INTO provider_runner_events
        (id, invocation_id, event_type, event_digest, redacted_event_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (new_id(), invocation_id, event_type, digest, encoded, utc_now_iso()),
    )


def persist_adapter_snapshot(
    conn: sqlite3.Connection,
    *,
    invocation_id: str,
    provider: str,
    snapshot: dict[str, Any],
    snapshot_digest: str,
    actor: str,
) -> dict[str, Any]:
    """Pin a non-secret handshake before provider dispatch."""
    if set(snapshot) != ADAPTER_SNAPSHOT_FIELDS:
        raise ValidationFailedError("adapter snapshot fields mismatch")
    if snapshot["provider"] != provider or not snapshot["auth_ready"]:
        raise ValidationFailedError("adapter snapshot provider/auth mismatch")
    validate_execution_profile(provider, str(snapshot["execution_profile"]))
    if digest_json(snapshot) != snapshot_digest:
        raise ValidationFailedError("adapter snapshot digest mismatch")
    encoded = json.dumps(snapshot, sort_keys=True)
    if len(encoded.encode()) > 16_384:
        raise ValidationFailedError("adapter snapshot exceeds cap")
    row = conn.execute(
        """
        SELECT provider, attempt_id, request_digest, adapter_snapshot_digest
        FROM provider_invocations WHERE id = ?
        """,
        (invocation_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError("provider invocation not found")
    if row["provider"] != provider:
        raise ValidationFailedError("provider binding mismatch")
    if actor != f"worker.provider.{provider}":
        raise ValidationFailedError("provider worker principal mismatch")
    credit = conn.execute(
        """
        SELECT id FROM credit_entries
        WHERE invocation_id = ? AND kind = 'reservation'
        ORDER BY created_at LIMIT 1
        """,
        (invocation_id,),
    ).fetchone()
    if credit is None:
        raise ConflictError("open credit reservation not found")
    binding = _invocation_binding_fields(
        provider=provider,
        attempt_id=row["attempt_id"],
        invocation_id=invocation_id,
        credit_reservation_id=credit["id"],
        packet_digest=row["request_digest"],
        snapshot_digest=snapshot_digest,
        resolved_model=snapshot["resolved_model"],
        adapter_version=snapshot["adapter_version"],
        execution_profile=str(snapshot["execution_profile"]),
    )
    binding_digest = digest_json(binding)
    if row["adapter_snapshot_digest"]:
        if row["adapter_snapshot_digest"] != snapshot_digest:
            raise ConflictError("immutable adapter snapshot conflict")
        return {
            "invocation_id": invocation_id,
            "snapshot_digest": snapshot_digest,
            "binding": binding,
            "binding_digest": binding_digest,
        }
    conn.execute(
        """
        UPDATE provider_invocations
        SET adapter_snapshot_json = ?, adapter_snapshot_digest = ?,
            binding_digest = ?, updated_at = ?
        WHERE id = ? AND adapter_snapshot_digest IS NULL
        """,
        (encoded, snapshot_digest, binding_digest, utc_now_iso(), invocation_id),
    )
    _record_runner_event(
        conn,
        invocation_id=invocation_id,
        event_type="snapshot",
        event={"snapshot_digest": snapshot_digest},
    )
    append_audit_event(
        conn,
        event_type="runtime.provider_adapter_pinned",
        actor=actor,
        payload={
            "invocation_id": invocation_id,
            "provider": provider,
            "snapshot_digest": snapshot_digest,
            "resolved_model": snapshot["resolved_model"],
        },
    )
    return {
        "invocation_id": invocation_id,
        "snapshot_digest": snapshot_digest,
        "binding": binding,
        "binding_digest": binding_digest,
    }


def preflight_worker_delivery(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    delivery_job_id: str,
    worker_principal_id: str,
) -> dict[str, Any]:
    """Read-only authorization/binding preflight; no dispatch intent or CAS."""
    job = get_delivery_job(conn, delivery_job_id)
    attempt = get_attempt(conn, attempt_id)
    invocation = get_invocation_for_attempt(conn, attempt_id)
    if invocation is None:
        raise NotFoundError("no invocation for attempt")
    assert_delivery_ownership(
        conn,
        job,
        worker_principal_id=worker_principal_id,
        attempt_id=attempt_id,
        invocation_id=invocation["id"],
    )
    if AttemptStatus(attempt["status"]) != AttemptStatus.CLAIMED:
        raise ConflictError("preflight requires claimed attempt")
    credit = conn.execute(
        """
        SELECT id FROM credit_entries
        WHERE invocation_id = ? AND kind = 'reservation'
        ORDER BY created_at LIMIT 1
        """,
        (invocation["id"],),
    ).fetchone()
    if credit is None:
        raise ConflictError("open credit reservation not found")
    return {
        "attempt_id": attempt_id,
        "invocation_id": invocation["id"],
        "run_id": attempt["run_id"],
        "provider": job["provider"],
        "delivery_job_id": delivery_job_id,
        "payload": json.loads(invocation.get("invocation_packet_json") or "{}"),
        "packet_digest": invocation["request_digest"],
        "credit_reservation_id": credit["id"],
    }


def settle_external_worker_delivery(
    conn: sqlite3.Connection,
    *,
    prepared: dict[str, Any],
    provider_result: dict[str, Any] | None,
    actor: str,
) -> dict[str, Any]:
    """Typed external settlement; missing/ambiguous results fail to unknown."""
    row = conn.execute(
        """
        SELECT provider, attempt_id, request_digest, adapter_snapshot_json,
               adapter_snapshot_digest, binding_digest
        FROM provider_invocations WHERE id = ?
        """,
        (prepared["invocation_id"],),
    ).fetchone()
    if row is None:
        raise NotFoundError("provider invocation not found")
    if actor != f"worker.provider.{row['provider']}":
        raise ConflictError("provider worker principal mismatch")
    if prepared["provider"] != row["provider"] or prepared["attempt_id"] != row["attempt_id"]:
        raise ConflictError("settlement ownership mismatch")
    if provider_result is not None and not row["adapter_snapshot_digest"]:
        raise ConflictError("paid dispatch requires immutable adapter snapshot")
    if provider_result is not None:
        allowed = {
            "outcome", "evidence", "anomalies", "delivery_id",
            "provider_call_id", "redacted_output", "truncated",
            "snapshot_digest",
            "binding_digest",
        }
        if not set(provider_result) <= allowed:
            raise ValidationFailedError("provider result fields mismatch")
        if provider_result.get("outcome") not in {
            "complete", "failed", "outcome_unknown"
        }:
            raise ValidationFailedError("invalid provider outcome")
        if provider_result.get("snapshot_digest") != row["adapter_snapshot_digest"]:
            raise ConflictError("settlement adapter snapshot mismatch")
        snapshot = json.loads(row["adapter_snapshot_json"])
        credit = conn.execute(
            "SELECT id FROM credit_entries WHERE invocation_id = ? AND kind = 'reservation' "
            "ORDER BY created_at LIMIT 1",
            (prepared["invocation_id"],),
        ).fetchone()
        binding = _invocation_binding_fields(
            provider=row["provider"],
            attempt_id=row["attempt_id"],
            invocation_id=prepared["invocation_id"],
            credit_reservation_id=credit["id"] if credit else None,
            packet_digest=row["request_digest"],
            snapshot_digest=row["adapter_snapshot_digest"],
            resolved_model=snapshot.get("resolved_model", ""),
            adapter_version=snapshot.get("adapter_version", ""),
            execution_profile=str(snapshot.get("execution_profile", "")),
        )
        if digest_json(binding) != row["binding_digest"]:
            raise ConflictError("authoritative provider binding digest mismatch")
        if provider_result.get("binding_digest") != row["binding_digest"]:
            raise ConflictError("provider callback binding digest mismatch")
        encoded = json.dumps(provider_result, sort_keys=True)
        if len(encoded.encode()) > 524_288:
            raise ValidationFailedError("provider result exceeds cap")
        if provider_result.get("outcome") == "outcome_unknown":
            provider_result = None
    if provider_result is not None:
        conn.execute(
            """
            UPDATE provider_invocations
            SET provider_call_id = ?, reconciliation_required = 0, updated_at = ?
            WHERE id = ?
            """,
            (provider_result.get("provider_call_id"), utc_now_iso(), prepared["invocation_id"]),
        )
    else:
        conn.execute(
            """
            UPDATE provider_invocations
            SET reconciliation_required = 1, updated_at = ? WHERE id = ?
            """,
            (utc_now_iso(), prepared["invocation_id"]),
        )
    _record_runner_event(
        conn,
        invocation_id=prepared["invocation_id"],
        event_type="terminal_callback" if provider_result is not None else "reconcile_required",
        event={
            "outcome": (provider_result or {}).get("outcome", "outcome_unknown"),
            "provider_call_id": (provider_result or {}).get("provider_call_id"),
        },
    )
    return settle_worker_delivery(
        conn,
        prepared=prepared,
        provider_result=provider_result,
        actor=actor,
        ambiguous=provider_result is None,
    )


def _lookup_cached_command(
    conn: sqlite3.Connection, *, scope: str, digest: str
) -> dict[str, Any] | None:
    existing = conn.execute(
        """
        SELECT id, operation_id, request_digest, status, result_json, error_code
        FROM runtime_commands WHERE idempotency_scope = ?
        """,
        (scope,),
    ).fetchone()
    if existing is None:
        return None
    if existing["request_digest"] != digest:
        raise IdempotencyReplayError(
            "idempotency scope reused with conflicting digest"
        )
    prior = json.loads(existing["result_json"] or "{}")
    if existing["status"] == "applied":
        return {
            **prior,
            "operation_id": existing["operation_id"],
            "from_cache": True,
            "command_status": existing["status"],
        }
    if existing["status"] == "rejected":
        return {
            **prior,
            "operation_id": existing["operation_id"],
            "from_cache": True,
            "command_status": existing["status"],
        }
    # Durable reservation exists but not yet settled → in-progress.
    return {
        "operation_id": existing["operation_id"],
        "command_type": "runtime.worker_deliver",
        "status": "accepted",
        "from_cache": True,
        "in_progress": True,
        "anomalies": [],
        "result": {
            "delivery": {
                "delivered": False,
                "in_progress": True,
                "requires_reconciliation": False,
            }
        },
        "error_code": None,
        "command_status": existing["status"],
    }


def _reserve_command(
    conn: sqlite3.Connection,
    command: RuntimeCommand,
) -> tuple[str, str]:
    """Insert durable accepted reservation for the idempotency scope. Returns ids."""
    operation_id = new_id()
    command_id = new_id()
    created_at = utc_now_iso()
    scope = command.idempotency_scope
    digest = command.request_digest
    try:
        conn.execute(
            """
            INSERT INTO runtime_commands (
                id, operation_id, command_type, principal_id, surface, target_id,
                request_digest, attempt_id, provider_invocation_id, idempotency_scope,
                status, result_json, error_code, created_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', NULL, NULL, ?, NULL)
            """,
            (
                command_id,
                operation_id,
                command.command_type,
                command.context.principal_id,
                str(command.context.surface),
                command.target_id,
                digest,
                command.context.attempt_id,
                command.context.provider_invocation_id,
                scope,
                created_at,
            ),
        )
    except sqlite3.IntegrityError:
        cached = _lookup_cached_command(conn, scope=scope, digest=digest)
        if cached is not None:
            raise _ReservationConflict(cached) from None
        raise
    return operation_id, command_id


class _ReservationConflict(Exception):
    def __init__(self, envelope: dict[str, Any]) -> None:
        super().__init__("idempotency reservation conflict")
        self.envelope = envelope


def _finalize_command(
    conn: sqlite3.Connection,
    *,
    command_id: str,
    envelope: dict[str, Any],
    status: str = "applied",
) -> None:
    conn.execute(
        """
        UPDATE runtime_commands
        SET status = ?, result_json = ?, error_code = ?, applied_at = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps(envelope),
            envelope.get("error_code"),
            utc_now_iso(),
            command_id,
        ),
    )


def prepare_worker_delivery(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    delivery_job_id: str | None,
    worker_principal_id: str,
    lease_token: str | None = None,
) -> dict[str, Any]:
    """Durable pre-delivery intent inside a short transaction.

    Exclusive CAS on ``possible_side_effect`` (and optional job dispatch lease)
    ensures only one execution may proceed to provider I/O.
    """
    job = get_delivery_job(conn, delivery_job_id) if delivery_job_id else None
    if job is not None and job.get("outcome_unknown"):
        raise ConflictError("outcome_unknown delivery requires reconciliation before replay")

    attempt = get_attempt(conn, attempt_id)
    if AttemptStatus(attempt["status"]) != AttemptStatus.CLAIMED:
        raise ConflictError("worker deliver requires claimed attempt")

    run = get_run(conn, attempt["run_id"])
    if RunStatus(run["status"]) == RunStatus.OUTCOME_UNKNOWN:
        raise ConflictError("outcome_unknown requires reconciliation before replay")

    invocation = get_invocation_for_attempt(conn, attempt_id)
    if invocation is None:
        raise NotFoundError("no invocation for attempt")
    inv_status = InvocationStatus(invocation["status"])
    if inv_status not in {InvocationStatus.RESERVED, InvocationStatus.DISPATCHED}:
        raise ConflictError(f"invocation not deliverable: {inv_status}")

    provider = run["provider"]
    if not provider:
        raise ValidationFailedError("run has no provider")

    if job is not None:
        assert_delivery_ownership(
            conn,
            job,
            worker_principal_id=worker_principal_id,
            attempt_id=attempt_id,
            invocation_id=invocation["id"],
        )
        if job.get("status") in {"completed", "failed", "delivered", "stale"}:
            raise ConflictError("delivery job already terminal")

        token = lease_token or f"lease|{job['id']}|{attempt_id}|{worker_principal_id}"
        job = acquire_exclusive_dispatch_lease(
            conn,
            job_id=job["id"],
            worker_principal_id=worker_principal_id,
            lease_token=token,
        )

    # Exclusive CAS: first writer of possible_side_effect wins the provider-I/O lease.
    now = utc_now_iso()
    cursor = conn.execute(
        """
        UPDATE runtime_attempts
        SET possible_side_effect = 1, last_heartbeat_at = ?,
            revision = revision + 1, updated_at = ?
        WHERE id = ? AND status = ? AND possible_side_effect = 0
        """,
        (now, now, attempt_id, AttemptStatus.CLAIMED),
    )
    if cursor.rowcount != 1:
        # Already has dispatch intent — non-replayable without reconciliation /
        # terminal settle. Callers must not perform another provider call.
        raise ConflictError(
            "dispatch lease already held; delivery non-replayable until settlement "
            "or reconciliation",
            code="CONFLICT_CAS",
        )

    job_result = (job or {}).get("result_json") if job else None
    if not isinstance(job_result, dict):
        job_result = {}
    lease_held = lease_token or job_result.get("dispatch_lease")
    append_audit_event(
        conn,
        event_type="runtime.worker_deliver_intent",
        actor=worker_principal_id,
        payload={
            "attempt_id": attempt_id,
            "invocation_id": invocation["id"],
            "delivery_job_id": job["id"] if job else delivery_job_id,
            "dispatch_lease": lease_held,
        },
    )
    return {
        "attempt_id": attempt_id,
        "invocation_id": invocation["id"],
        "run_id": run["id"],
        "provider": provider,
        "delivery_job_id": job["id"] if job else delivery_job_id,
        "payload": json.loads(invocation.get("invocation_packet_json") or "{}"),
        "lease_token": lease_held,
    }


def execute_provider_delivery(
    prepared: dict[str, Any],
    *,
    runners: dict[str, ProviderRunner] | None = None,
) -> dict[str, Any]:
    """Run provider I/O outside any SQLite transaction."""
    registry = runners or default_mock_registry()
    runner = registry.get(prepared["provider"])
    if runner is None:
        raise ValidationFailedError(f"no provider runner registered for {prepared['provider']}")
    request = InvocationRequest(
        invocation_id=prepared["invocation_id"],
        attempt_id=prepared["attempt_id"],
        run_id=prepared["run_id"],
        provider=prepared["provider"],
        payload=prepared.get("payload") or {},
    )
    prepared_req = runner.prepare(request)
    handle = runner.deliver(prepared_req)
    result = runner.collect(handle)
    return {
        "outcome": result.outcome,
        "evidence": result.evidence,
        "anomalies": result.anomalies,
        "delivery_id": handle.delivery_id,
    }


def settle_worker_delivery(
    conn: sqlite3.Connection,
    *,
    prepared: dict[str, Any],
    provider_result: dict[str, Any] | None,
    actor: str,
    ambiguous: bool = False,
) -> dict[str, Any]:
    """Persist delivery outcome. Ambiguous → outcome_unknown (no auto-replay)."""
    attempt_id = prepared["attempt_id"]
    invocation_id = prepared["invocation_id"]
    delivery_job_id = prepared.get("delivery_job_id")
    now = utc_now_iso()

    if ambiguous or provider_result is None:
        conn.execute(
            """
            UPDATE provider_invocations
            SET status = ?, result_json = ?, evidence_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                InvocationStatus.OUTCOME_UNKNOWN,
                json.dumps({"ambiguous": True}),
                json.dumps({"reason": "ambiguous_external_delivery"}),
                now,
                invocation_id,
            ),
        )
        settled = submit_result(
            conn,
            attempt_id=attempt_id,
            outcome="outcome_unknown",
            actor=actor,
            evidence={"reason": "ambiguous_external_delivery"},
            anomalies=[{"code": "A1", "detail": "ambiguous external delivery"}],
        )
        if delivery_job_id:
            mark_delivery_outcome_unknown(conn, job_id=delivery_job_id)
        append_audit_event(
            conn,
            event_type="runtime.worker_deliver_ambiguous",
            actor=actor,
            payload={"attempt_id": attempt_id, "invocation_id": invocation_id},
        )
        return {
            "delivery": {"invocation_id": invocation_id, "delivered": False, "ambiguous": True},
            "provider_result": {"outcome": "outcome_unknown"},
            **settled,
        }

    conn.execute(
        """
        UPDATE provider_invocations
        SET status = ?, evidence_json = ?, updated_at = ?
        WHERE id = ? AND status = ?
        """,
        (
            InvocationStatus.DISPATCHED,
            json.dumps({"delivery": provider_result.get("delivery_id") or "delivered"}),
            now,
            invocation_id,
            InvocationStatus.RESERVED,
        ),
    )
    attempt = get_attempt(conn, attempt_id)
    conn.execute(
        """
        UPDATE runtime_attempts
        SET dispatched_at = COALESCE(dispatched_at, ?),
            possible_side_effect = 1,
            last_heartbeat_at = ?, revision = revision + 1, updated_at = ?
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (now, now, now, attempt_id, AttemptStatus.CLAIMED, attempt["revision"]),
    )

    job = None
    if delivery_job_id:
        job = mark_delivery_delivered(conn, job_id=delivery_job_id)

    settled = submit_result(
        conn,
        attempt_id=attempt_id,
        outcome=provider_result["outcome"],
        actor=actor,
        evidence=provider_result.get("evidence"),
        anomalies=provider_result.get("anomalies"),
    )
    if job is not None:
        complete_delivery_job(
            conn,
            job_id=job["id"],
            outcome=provider_result["outcome"],
            result={"provider_result": provider_result.get("evidence")},
        )

    append_audit_event(
        conn,
        event_type="runtime.worker_deliver",
        actor=actor,
        payload={
            "attempt_id": attempt_id,
            "invocation_id": invocation_id,
            "delivery_job_id": job["id"] if job else None,
            "outcome": provider_result["outcome"],
        },
    )
    return {
        "delivery": {"invocation_id": invocation_id, "delivered": True},
        "provider_result": {
            "outcome": provider_result["outcome"],
            "evidence": provider_result.get("evidence"),
            "anomalies": provider_result.get("anomalies"),
        },
        **settled,
    }


def worker_deliver_mock(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    delivery_job_id: str | None = None,
    actor: str,
    runners: dict[str, ProviderRunner] | None = None,
) -> dict[str, Any]:
    """Legacy single-connection path for tests that already manage transactions.

    Prefer accept_worker_deliver() so provider I/O is outside the SQLite txn.
    """
    prepared = prepare_worker_delivery(
        conn,
        attempt_id=attempt_id,
        delivery_job_id=delivery_job_id,
        worker_principal_id=actor,
    )
    try:
        provider_result = execute_provider_delivery(prepared, runners=runners)
        ambiguous = False
    except Exception:
        provider_result = None
        ambiguous = True
    return settle_worker_delivery(
        conn,
        prepared=prepared,
        provider_result=provider_result,
        actor=actor,
        ambiguous=ambiguous,
    )


def accept_worker_deliver(
    coordinator: StateCoordinator,
    command: RuntimeCommand,
) -> dict[str, Any]:
    """Three-phase delivery: reserve+lease (txn) → provider (outside) → settle (txn).

    Idempotency: durable reservation on first accept; duplicates return
    cached terminal or in-progress envelopes without another provider call.
    """
    attempt_id = command.target_id or command.payload["attempt_id"]
    delivery_job_id = command.payload.get("delivery_job_id")
    actor = command.context.principal_id
    scope = command.idempotency_scope
    digest = command.request_digest
    lease_token = f"lease|{command.idempotency_key or digest}|{attempt_id}"

    command_id: str | None = None
    operation_id: str | None = None
    prepared: dict[str, Any] | None = None

    with transaction(coordinator.connection):
        from flow_engine.coordinator.authz import authorize_command

        cached = _lookup_cached_command(
            coordinator.connection, scope=scope, digest=digest
        )
        if cached is not None:
            return cached

        kind, caps = coordinator._lookup_principal_kind(actor)
        authorize_command(command, principal_kind=kind, capabilities=caps)

        try:
            operation_id, command_id = _reserve_command(
                coordinator.connection, command
            )
        except _ReservationConflict as exc:
            return exc.envelope

        try:
            prepared = prepare_worker_delivery(
                coordinator.connection,
                attempt_id=attempt_id,
                delivery_job_id=delivery_job_id,
                worker_principal_id=actor,
                lease_token=lease_token,
            )
        except ConflictError as exc:
            # Lease lost / already held / outcome_unknown → in-progress or reject.
            # Do not proceed to provider I/O.
            in_progress = "non-replayable" in str(exc) or "lease" in str(exc).lower()
            if in_progress and "outcome_unknown" not in str(exc):
                envelope = {
                    "operation_id": operation_id,
                    "command_type": "runtime.worker_deliver",
                    "status": "accepted",
                    "from_cache": False,
                    "in_progress": True,
                    "anomalies": [],
                    "result": {
                        "delivery": {
                            "delivered": False,
                            "in_progress": True,
                            "attempt_id": attempt_id,
                            "delivery_job_id": delivery_job_id,
                        }
                    },
                    "error_code": None,
                }
                # Leave reservation as accepted (no applied_at) so peers see in-progress.
                return envelope
            envelope = {
                "operation_id": operation_id,
                "command_type": "runtime.worker_deliver",
                "status": "rejected",
                "from_cache": False,
                "anomalies": [],
                "result": None,
                "error_code": getattr(exc, "code", "CONFLICT_CAS"),
                "error": str(exc),
            }
            assert command_id is not None
            _finalize_command(
                coordinator.connection,
                command_id=command_id,
                envelope=envelope,
                status="rejected",
            )
            return envelope

    assert prepared is not None
    assert operation_id is not None
    assert command_id is not None

    ambiguous = False
    provider_result: dict[str, Any] | None
    try:
        provider_result = execute_provider_delivery(
            prepared, runners=coordinator._runners
        )
    except Exception:
        ambiguous = True
        provider_result = None

    with transaction(coordinator.connection):
        result = settle_worker_delivery(
            coordinator.connection,
            prepared=prepared,
            provider_result=provider_result,
            actor=actor,
            ambiguous=ambiguous,
        )
        envelope = {
            "operation_id": operation_id,
            "command_type": "runtime.worker_deliver",
            "status": "applied",
            "from_cache": False,
            "anomalies": result.get("anomalies") or [],
            "result": result,
            "error_code": result.get("error_code"),
        }
        _finalize_command(
            coordinator.connection,
            command_id=command_id,
            envelope=envelope,
            status="applied",
        )
        return envelope
