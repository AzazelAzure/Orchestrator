"""R2 governed runtime lifecycle (runs, attempts, invocations).

Internal persistence helpers. External callers must go through StateCoordinator.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.clock import is_expired, utc_after_seconds, utc_now_iso
from flow_engine.application.credit_service import (
    assert_concurrency_available,
    credit_usage,
    release_credit,
    reserve_credit,
    settle_credit,
)
from flow_engine.application.event_service import append_event
from flow_engine.application.work_service import assert_completion_prerequisites, show_work
from flow_engine.coordinator.audit import append_audit_event
from flow_engine.coordinator.commands import Grant, ResolvedTaskGrant, SystemTestGrant
from flow_engine.domain.credits import (
    ACCEPTANCE_CREDIT_PER_PROVIDER,
    ACCEPTANCE_CREDIT_TOTAL,
    HARD_ATTEMPT_TIMEOUT_SEC,
    HEARTBEAT_INTERVAL_SEC,
    INACTIVITY_THRESHOLD_SEC,
)
from flow_engine.domain.errors import (
    ConflictError,
    NotFoundError,
    OutcomeUnknownError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import (
    AnomalyCode,
    AttemptStatus,
    InvocationStatus,
    ProviderLimitState,
    RunStatus,
    WorkItemStatus,
)
from flow_engine.domain.transitions import (
    assert_attempt_transition,
    assert_run_transition,
    assert_work_transition,
)
from flow_engine.providers.protocol import (
    InvocationRequest,
    ProviderRunner,
    default_mock_registry,
)


def _row_to_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "work_item_id": row["work_item_id"],
        "project_id": row["project_id"],
        "budget_scope_id": row["budget_scope_id"],
        "status": row["status"],
        "provider": row["provider"],
        "provider_limit_state": row["provider_limit_state"],
        "revision": row["revision"],
        "grant": json.loads(row["grant_json"]),
        "policy_snapshot": json.loads(row["policy_snapshot_json"]),
        "gate_snapshot": json.loads(row["gate_snapshot_json"]),
        "credit_budget_total": row["credit_budget_total"],
        "credit_budget_per_provider": row["credit_budget_per_provider"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_attempt(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "attempt_number": row["attempt_number"],
        "status": row["status"],
        "lease_holder": row["lease_holder"],
        "lease_expires_at": row["lease_expires_at"],
        "last_heartbeat_at": row["last_heartbeat_at"],
        "hard_deadline_at": row["hard_deadline_at"],
        "inactivity_deadline_at": row["inactivity_deadline_at"],
        "dispatched_at": row["dispatched_at"],
        "possible_side_effect": bool(row["possible_side_effect"]),
        "revision": row["revision"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_invocation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "attempt_id": row["attempt_id"],
        "run_id": row["run_id"],
        "provider": row["provider"],
        "status": row["status"],
        "request_digest": row["request_digest"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "evidence": json.loads(row["evidence_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM runtime_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"run not found: {run_id}")
    return _row_to_run(row)


def get_attempt(conn: sqlite3.Connection, attempt_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM runtime_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"attempt not found: {attempt_id}")
    return _row_to_attempt(row)


def get_invocation_for_attempt(
    conn: sqlite3.Connection, attempt_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM provider_invocations WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()
    return _row_to_invocation(row) if row else None


def _cas_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    expected_status: RunStatus,
    target_status: RunStatus,
    expected_revision: int | None = None,
    provider: str | None = None,
    provider_limit_state: str | None = None,
) -> dict[str, Any]:
    current = get_run(conn, run_id)
    if RunStatus(current["status"]) != expected_status:
        raise ConflictError(
            f"run status mismatch: expected {expected_status}, got {current['status']}"
        )
    assert_run_transition(RunStatus(current["status"]), target_status)
    if expected_revision is not None and current["revision"] != expected_revision:
        raise ConflictError(
            f"run revision mismatch: expected {expected_revision}, got {current['revision']}"
        )
    new_revision = current["revision"] + 1
    now = utc_now_iso()
    cursor = conn.execute(
        """
        UPDATE runtime_runs
        SET status = ?, revision = ?, updated_at = ?,
            provider = COALESCE(?, provider),
            provider_limit_state = COALESCE(?, provider_limit_state)
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (
            target_status,
            new_revision,
            now,
            provider,
            provider_limit_state,
            run_id,
            expected_status,
            current["revision"],
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"compare-and-set failed for run {run_id}")
    return get_run(conn, run_id)


def _cas_attempt(
    conn: sqlite3.Connection,
    attempt_id: str,
    *,
    expected_status: AttemptStatus,
    target_status: AttemptStatus,
    expected_revision: int | None = None,
    lease_holder: str | None = None,
    lease_expires_at: str | None = None,
    last_heartbeat_at: str | None = None,
    hard_deadline_at: str | None = None,
    inactivity_deadline_at: str | None = None,
    dispatched_at: str | None = None,
    possible_side_effect: int | None = None,
) -> dict[str, Any]:
    current = get_attempt(conn, attempt_id)
    if AttemptStatus(current["status"]) != expected_status:
        raise ConflictError(
            f"attempt status mismatch: expected {expected_status}, got {current['status']}"
        )
    assert_attempt_transition(AttemptStatus(current["status"]), target_status)
    if expected_revision is not None and current["revision"] != expected_revision:
        raise ConflictError(
            f"attempt revision mismatch: expected {expected_revision}, got {current['revision']}"
        )
    new_revision = current["revision"] + 1
    now = utc_now_iso()
    cursor = conn.execute(
        """
        UPDATE runtime_attempts
        SET status = ?, revision = ?, updated_at = ?,
            lease_holder = COALESCE(?, lease_holder),
            lease_expires_at = COALESCE(?, lease_expires_at),
            last_heartbeat_at = COALESCE(?, last_heartbeat_at),
            hard_deadline_at = COALESCE(?, hard_deadline_at),
            inactivity_deadline_at = COALESCE(?, inactivity_deadline_at),
            dispatched_at = COALESCE(?, dispatched_at),
            possible_side_effect = COALESCE(?, possible_side_effect)
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (
            target_status,
            new_revision,
            now,
            lease_holder,
            lease_expires_at,
            last_heartbeat_at,
            hard_deadline_at,
            inactivity_deadline_at,
            dispatched_at,
            possible_side_effect,
            attempt_id,
            expected_status,
            current["revision"],
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"compare-and-set failed for attempt {attempt_id}")
    return get_attempt(conn, attempt_id)


def _sync_work_status(
    conn: sqlite3.Connection,
    work_item_id: str,
    target: WorkItemStatus,
    *,
    actor: str,
) -> None:
    row = conn.execute(
        "SELECT status, revision, claimed_by FROM work_items WHERE id = ?",
        (work_item_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"work item not found: {work_item_id}")
    current = WorkItemStatus(row["status"])
    if current == target:
        return
    assert_work_transition(current, target)
    new_revision = int(row["revision"]) + 1
    claimed_by = actor if target == WorkItemStatus.CLAIMED else row["claimed_by"]
    if target in {
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
        WorkItemStatus.PENDING,
    }:
        claimed_by = None
    cursor = conn.execute(
        """
        UPDATE work_items
        SET status = ?, claimed_by = ?, revision = ?
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (target, claimed_by, new_revision, work_item_id, current, row["revision"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"compare-and-set failed for work item {work_item_id}")


def preview_run(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    provider: str,
    grant: Grant,
    actor: str,
) -> dict[str, Any]:
    if provider not in grant.providers:
        raise ValidationFailedError(f"provider {provider} not in grant")
    work = show_work(conn, work_item_id)
    queue = conn.execute(
        "SELECT project_id FROM queues WHERE id = ?",
        (work["queue_id"],),
    ).fetchone()
    if queue is None:
        raise NotFoundError("queue missing for work item")
    if isinstance(grant, SystemTestGrant):
        loadout_resolution = "refused"
    else:
        loadout_resolution = {
            "mode": "r3_resolved",
            "loadout_id": grant.loadout_id,
            "snapshot_id": grant.snapshot_id,
            "organization_id": grant.organization_id,
        }
    return {
        "work_item_id": work_item_id,
        "work_status": work["status"],
        "project_id": queue["project_id"],
        "provider": provider,
        "grant": grant.to_dict(),
        "credit_budget_total": ACCEPTANCE_CREDIT_TOTAL,
        "credit_budget_per_provider": ACCEPTANCE_CREDIT_PER_PROVIDER,
        "timing": {
            "heartbeat_sec": HEARTBEAT_INTERVAL_SEC,
            "inactivity_sec": INACTIVITY_THRESHOLD_SEC,
            "hard_timeout_sec": HARD_ATTEMPT_TIMEOUT_SEC,
        },
        "loadout_resolution": loadout_resolution,
        "actor": actor,
    }


def create_run(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    provider: str,
    grant: Grant,
    actor: str,
    policy_snapshot: dict[str, Any] | None = None,
    gate_snapshot: dict[str, Any] | None = None,
    packet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    preview = preview_run(
        conn,
        work_item_id=work_item_id,
        provider=provider,
        grant=grant,
        actor=actor,
    )
    work = show_work(conn, work_item_id)
    if WorkItemStatus(work["status"]) not in {
        WorkItemStatus.PENDING,
        WorkItemStatus.CLAIMED,
        WorkItemStatus.FAILED,
    }:
        raise ConflictError(f"work item status {work['status']} cannot start a run")

    now = utc_now_iso()
    run_id = new_id()
    conn.execute(
        """
        INSERT INTO runtime_runs (
            id, work_item_id, project_id, budget_scope_id, status, provider, provider_limit_state,
            revision, grant_json, policy_snapshot_json, gate_snapshot_json,
            credit_budget_total, credit_budget_per_provider, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            work_item_id,
            preview["project_id"],
            grant.budget_scope_id,
            RunStatus.PENDING,
            provider,
            ProviderLimitState.OPEN,
            json.dumps(grant.to_dict()),
            json.dumps(policy_snapshot or {"policy_revision": grant.policy_revision}),
            json.dumps(gate_snapshot or {}),
            ACCEPTANCE_CREDIT_TOTAL,
            ACCEPTANCE_CREDIT_PER_PROVIDER,
            now,
            now,
        ),
    )
    attempt_id = new_id()
    conn.execute(
        """
        INSERT INTO runtime_attempts (
            id, run_id, attempt_number, status, revision, created_at, updated_at
        ) VALUES (?, ?, 1, ?, 0, ?, ?)
        """,
        (attempt_id, run_id, AttemptStatus.PENDING, now, now),
    )
    pin = None
    if isinstance(grant, ResolvedTaskGrant):
        from flow_engine.application.delegation_service import create_dispatch_pin
        from flow_engine.coordinator.commands import stable_digest

        packet_hash = stable_digest(packet or {"work_item_id": work_item_id})
        pin = create_dispatch_pin(
            conn,
            grant=grant,
            packet_hash=packet_hash,
            actor=actor,
            run_id=run_id,
            attempt_id=attempt_id,
        )
    append_audit_event(
        conn,
        event_type="runtime.run_created",
        actor=actor,
        payload={"run_id": run_id, "work_item_id": work_item_id, "provider": provider},
    )
    append_event(
        conn,
        event_type="runtime.run_created",
        actor=actor,
        payload={"run_id": run_id, "attempt_id": attempt_id},
    )
    run = get_run(conn, run_id)
    result = {"run": run, "attempt": get_attempt(conn, attempt_id), "preview": preview}
    if pin is not None:
        result["dispatch_pin"] = pin
    return result


def claim_attempt(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    actor: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    if run["provider_limit_state"] == ProviderLimitState.HALTED:
        raise ConflictError("provider limit halted; continue or reroute required")

    if RunStatus(run["status"]) == RunStatus.PENDING:
        run = _cas_run(
            conn,
            run_id,
            expected_status=RunStatus.PENDING,
            target_status=RunStatus.CLAIMED,
            expected_revision=expected_revision,
        )
        _sync_work_status(
            conn, run["work_item_id"], WorkItemStatus.CLAIMED, actor=actor
        )
    elif RunStatus(run["status"]) == RunStatus.PAUSED:
        run = _cas_run(
            conn,
            run_id,
            expected_status=RunStatus.PAUSED,
            target_status=RunStatus.CLAIMED,
            expected_revision=expected_revision,
        )
        _sync_work_status(
            conn, run["work_item_id"], WorkItemStatus.CLAIMED, actor=actor
        )
    elif RunStatus(run["status"]) != RunStatus.CLAIMED:
        raise ConflictError(f"run status {run['status']} cannot be claimed")

    row = conn.execute(
        """
        SELECT id FROM runtime_attempts
        WHERE run_id = ? AND status = ?
        ORDER BY attempt_number DESC LIMIT 1
        """,
        (run_id, AttemptStatus.PENDING),
    ).fetchone()
    if row is None:
        # Resume paused attempt
        row = conn.execute(
            """
            SELECT id FROM runtime_attempts
            WHERE run_id = ? AND status = ?
            ORDER BY attempt_number DESC LIMIT 1
            """,
            (run_id, AttemptStatus.PAUSED),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"no claimable attempt for run {run_id}")
        expected = AttemptStatus.PAUSED
    else:
        expected = AttemptStatus.PENDING

    now = utc_now_iso()
    attempt = _cas_attempt(
        conn,
        row["id"],
        expected_status=expected,
        target_status=AttemptStatus.CLAIMED,
        lease_holder=actor,
        lease_expires_at=utc_after_seconds(HEARTBEAT_INTERVAL_SEC * 2, from_iso=now),
        last_heartbeat_at=now,
        hard_deadline_at=utc_after_seconds(HARD_ATTEMPT_TIMEOUT_SEC, from_iso=now),
        inactivity_deadline_at=utc_after_seconds(INACTIVITY_THRESHOLD_SEC, from_iso=now),
    )
    append_audit_event(
        conn,
        event_type="runtime.attempt_claimed",
        actor=actor,
        payload={"run_id": run_id, "attempt_id": attempt["id"]},
    )
    return {"run": get_run(conn, run_id), "attempt": attempt}


def heartbeat_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    actor: str,
) -> dict[str, Any]:
    attempt = get_attempt(conn, attempt_id)
    if AttemptStatus(attempt["status"]) != AttemptStatus.CLAIMED:
        raise ConflictError("heartbeat requires claimed attempt")
    if attempt["lease_holder"] != actor:
        raise ConflictError("heartbeat holder mismatch")
    now = utc_now_iso()
    cursor = conn.execute(
        """
        UPDATE runtime_attempts
        SET last_heartbeat_at = ?,
            lease_expires_at = ?,
            inactivity_deadline_at = ?,
            revision = revision + 1,
            updated_at = ?
        WHERE id = ? AND revision = ? AND status = ?
        """,
        (
            now,
            utc_after_seconds(HEARTBEAT_INTERVAL_SEC * 2, from_iso=now),
            utc_after_seconds(INACTIVITY_THRESHOLD_SEC, from_iso=now),
            now,
            attempt_id,
            attempt["revision"],
            AttemptStatus.CLAIMED,
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"heartbeat CAS failed for attempt {attempt_id}")
    return get_attempt(conn, attempt_id)


def dispatch_provider_call(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    runners: dict[str, ProviderRunner] | None = None,
    delivery_mode: str = "inline",
) -> dict[str, Any]:
    attempt = get_attempt(conn, attempt_id)
    if AttemptStatus(attempt["status"]) != AttemptStatus.CLAIMED:
        raise ConflictError("dispatch requires claimed attempt")
    existing = get_invocation_for_attempt(conn, attempt_id)
    if existing is not None:
        raise ConflictError("duplicate dispatch blocked; attempt already has invocation")

    run = get_run(conn, attempt["run_id"])
    if run["provider_limit_state"] == ProviderLimitState.HALTED:
        raise ConflictError("provider limit halted")
    provider = run["provider"]
    if not provider:
        raise ValidationFailedError("run has no provider")

    assert_concurrency_available(
        conn, provider=provider, project_id=run["project_id"], run_id=run["id"]
    )

    invocation_id = new_id()
    from flow_engine.providers.host_runner import canonical_invocation_packet, digest_json

    invocation_packet = canonical_invocation_packet(payload or {})
    request_digest = digest_json(invocation_packet)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO provider_invocations (
            id, attempt_id, run_id, provider, status, request_digest,
            result_json, evidence_json, invocation_packet_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, '{}', ?, ?, ?)
        """,
        (
            invocation_id,
            attempt_id,
            run["id"],
            provider,
            InvocationStatus.RESERVED,
            request_digest,
            json.dumps(invocation_packet, sort_keys=True),
            now,
            now,
        ),
    )
    reserve_credit(
        conn,
        run_id=run["id"],
        provider=provider,
        attempt_id=attempt_id,
        invocation_id=invocation_id,
    )

    if delivery_mode == "async":
        from flow_engine.control_plane.delivery_registry import (
            delivery_idempotency_key,
            register_delivery_job,
        )

        idem = delivery_idempotency_key(invocation_id, attempt_id)
        job = register_delivery_job(
            conn,
            invocation_id=invocation_id,
            attempt_id=attempt_id,
            run_id=run["id"],
            provider=provider,
            idempotency_key=idem,
        )
        append_audit_event(
            conn,
            event_type="runtime.provider_reserved_async",
            actor=actor,
            payload={
                "run_id": run["id"],
                "attempt_id": attempt_id,
                "invocation_id": invocation_id,
                "provider": provider,
                "delivery_job_id": job["id"],
            },
        )
        return {
            "run": get_run(conn, run["id"]),
            "attempt": get_attempt(conn, attempt_id),
            "invocation": get_invocation_for_attempt(conn, attempt_id),
            "delivery": {
                "mode": "async",
                "invocation_id": invocation_id,
                "delivery_job_id": job["id"],
                "delivered": False,
            },
        }

    registry = runners or default_mock_registry()
    runner = registry.get(provider)
    if runner is None:
        raise ValidationFailedError(f"no provider runner registered for {provider}")

    request = InvocationRequest(
        invocation_id=invocation_id,
        attempt_id=attempt_id,
        run_id=run["id"],
        provider=provider,
        payload=payload or {},
    )
    prepared = runner.prepare(request)
    try:
        handle = runner.deliver(prepared)
    except Exception as exc:
        # Adapter exceptions are ambiguous: a provider side effect may have
        # occurred before the local exception surfaced. Persist unknown and
        # consume the reserved credit; never make the attempt retryable.
        unknown_at = utc_now_iso()
        conn.execute(
            """
            UPDATE provider_invocations
            SET status = ?, result_json = ?, evidence_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                InvocationStatus.OUTCOME_UNKNOWN,
                json.dumps({"outcome": "outcome_unknown"}),
                json.dumps({"adapter_error": type(exc).__name__}),
                unknown_at,
                invocation_id,
            ),
        )
        settle_credit(
            conn,
            run_id=run["id"],
            provider=provider,
            attempt_id=attempt_id,
            invocation_id=invocation_id,
        )
        attempt = get_attempt(conn, attempt_id)
        conn.execute(
            """
            UPDATE runtime_attempts
            SET possible_side_effect = 1, dispatched_at = ?, revision = revision + 1,
                updated_at = ?
            WHERE id = ? AND status = ? AND revision = ?
            """,
            (
                unknown_at,
                unknown_at,
                attempt_id,
                AttemptStatus.CLAIMED,
                attempt["revision"],
            ),
        )
        _cas_attempt(
            conn,
            attempt_id,
            expected_status=AttemptStatus.CLAIMED,
            target_status=AttemptStatus.OUTCOME_UNKNOWN,
        )
        _cas_run(
            conn,
            run["id"],
            expected_status=RunStatus.CLAIMED,
            target_status=RunStatus.OUTCOME_UNKNOWN,
        )
        _sync_work_status(
            conn, run["work_item_id"], WorkItemStatus.OUTCOME_UNKNOWN, actor=actor
        )
        append_audit_event(
            conn,
            event_type="runtime.provider_delivery_unknown",
            actor=actor,
            anomaly_code=AnomalyCode.A1,
            payload={
                "run_id": run["id"],
                "attempt_id": attempt_id,
                "invocation_id": invocation_id,
                "provider": provider,
                "error_type": type(exc).__name__,
            },
        )
        return {
            "run": get_run(conn, run["id"]),
            "attempt": get_attempt(conn, attempt_id),
            "invocation": get_invocation_for_attempt(conn, attempt_id),
            "delivery": {"invocation_id": invocation_id, "delivered": False},
            "anomalies": [{"code": "A1", "detail": "provider delivery outcome unknown"}],
            "error_code": "OUTCOME_UNKNOWN",
        }

    conn.execute(
        """
        UPDATE provider_invocations
        SET status = ?, evidence_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            InvocationStatus.DISPATCHED,
            json.dumps({"delivery": handle.delivery_id}),
            utc_now_iso(),
            invocation_id,
        ),
    )
    attempt = get_attempt(conn, attempt_id)
    cursor = conn.execute(
        """
        UPDATE runtime_attempts
        SET dispatched_at = ?,
            possible_side_effect = 1,
            last_heartbeat_at = ?,
            revision = revision + 1,
            updated_at = ?
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (
            now,
            now,
            utc_now_iso(),
            attempt_id,
            AttemptStatus.CLAIMED,
            attempt["revision"],
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"dispatch CAS failed for attempt {attempt_id}")
    append_audit_event(
        conn,
        event_type="runtime.provider_dispatched",
        actor=actor,
        payload={
            "run_id": run["id"],
            "attempt_id": attempt_id,
            "invocation_id": invocation_id,
            "provider": provider,
        },
    )
    return {
        "run": get_run(conn, run["id"]),
        "attempt": get_attempt(conn, attempt_id),
        "invocation": get_invocation_for_attempt(conn, attempt_id),
        "delivery": {
            "invocation_id": handle.invocation_id,
            "provider": handle.provider,
            "delivered": handle.delivered,
        },
    }


def submit_result(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    outcome: str,
    actor: str,
    evidence: dict[str, Any] | None = None,
    anomalies: list[dict[str, Any]] | None = None,
    consume_credit: bool = True,
) -> dict[str, Any]:
    if outcome not in {"complete", "failed", "outcome_unknown"}:
        raise ValidationFailedError(f"invalid outcome: {outcome}")
    # Mandatory anomaly list (empty allowed)
    anomaly_list = anomalies if anomalies is not None else []

    attempt = get_attempt(conn, attempt_id)
    invocation = get_invocation_for_attempt(conn, attempt_id)
    if invocation is None:
        raise ConflictError("no invocation to settle")
    run = get_run(conn, attempt["run_id"])

    if outcome == "complete":
        attempt_target = AttemptStatus.COMPLETE
        run_target = RunStatus.COMPLETE
        work_target = WorkItemStatus.COMPLETE
        inv_status = InvocationStatus.COMPLETE
        assert_completion_prerequisites(conn, run["work_item_id"])
    elif outcome == "failed":
        attempt_target = AttemptStatus.FAILED
        run_target = RunStatus.FAILED
        work_target = WorkItemStatus.FAILED
        inv_status = InvocationStatus.FAILED
    else:
        attempt_target = AttemptStatus.OUTCOME_UNKNOWN
        run_target = RunStatus.OUTCOME_UNKNOWN
        work_target = WorkItemStatus.OUTCOME_UNKNOWN
        inv_status = InvocationStatus.OUTCOME_UNKNOWN

    now = utc_now_iso()
    conn.execute(
        """
        UPDATE provider_invocations
        SET status = ?, result_json = ?, evidence_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            inv_status,
            json.dumps({"outcome": outcome, "anomalies": anomaly_list}),
            json.dumps(evidence or {}),
            now,
            invocation["id"],
        ),
    )
    credit_action = settle_credit if consume_credit else release_credit
    credit_action(
        conn,
        run_id=run["id"],
        provider=invocation["provider"],
        attempt_id=attempt_id,
        invocation_id=invocation["id"],
    )
    _cas_attempt(
        conn,
        attempt_id,
        expected_status=AttemptStatus.CLAIMED,
        target_status=attempt_target,
    )
    _cas_run(
        conn,
        run["id"],
        expected_status=RunStatus.CLAIMED,
        target_status=run_target,
    )
    _sync_work_status(conn, run["work_item_id"], work_target, actor=actor)

    code = AnomalyCode.A1 if outcome == "outcome_unknown" else None
    append_audit_event(
        conn,
        event_type="runtime.result_submitted",
        actor=actor,
        anomaly_code=code,
        payload={
            "run_id": run["id"],
            "attempt_id": attempt_id,
            "invocation_id": invocation["id"],
            "outcome": outcome,
            "anomalies": anomaly_list,
        },
    )
    return {
        "run": get_run(conn, run["id"]),
        "attempt": get_attempt(conn, attempt_id),
        "invocation": get_invocation_for_attempt(conn, attempt_id),
        "credits": credit_usage(conn, run["id"]),
        "anomalies": anomaly_list,
        "halted": outcome == "outcome_unknown",
        "error_code": "OUTCOME_UNKNOWN" if outcome == "outcome_unknown" else None,
    }


def pause_run(
    conn: sqlite3.Connection, *, run_id: str, actor: str
) -> dict[str, Any]:
    run = _cas_run(
        conn, run_id, expected_status=RunStatus.CLAIMED, target_status=RunStatus.PAUSED
    )
    row = conn.execute(
        """
        SELECT id FROM runtime_attempts
        WHERE run_id = ? AND status = ?
        ORDER BY attempt_number DESC LIMIT 1
        """,
        (run_id, AttemptStatus.CLAIMED),
    ).fetchone()
    attempt = None
    if row:
        attempt = _cas_attempt(
            conn,
            row["id"],
            expected_status=AttemptStatus.CLAIMED,
            target_status=AttemptStatus.PAUSED,
        )
    _sync_work_status(conn, run["work_item_id"], WorkItemStatus.PAUSED, actor=actor)
    append_audit_event(
        conn,
        event_type="runtime.paused",
        actor=actor,
        payload={"run_id": run_id},
    )
    return {"run": run, "attempt": attempt}


def resume_run(
    conn: sqlite3.Connection, *, run_id: str, actor: str
) -> dict[str, Any]:
    return claim_attempt(conn, run_id=run_id, actor=actor)


def cancel_run(
    conn: sqlite3.Connection, *, run_id: str, actor: str
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    current = RunStatus(run["status"])
    if current not in {RunStatus.PENDING, RunStatus.CLAIMED, RunStatus.PAUSED}:
        raise ConflictError(f"cannot cancel run in status {current}")
    run = _cas_run(
        conn, run_id, expected_status=current, target_status=RunStatus.CANCELLED
    )
    for row in conn.execute(
        """
        SELECT id, status FROM runtime_attempts
        WHERE run_id = ? AND status IN (?, ?, ?)
        """,
        (
            run_id,
            AttemptStatus.PENDING,
            AttemptStatus.CLAIMED,
            AttemptStatus.PAUSED,
        ),
    ).fetchall():
        _cas_attempt(
            conn,
            row["id"],
            expected_status=AttemptStatus(row["status"]),
            target_status=AttemptStatus.CANCELLED,
        )
        inv = get_invocation_for_attempt(conn, row["id"])
        if inv and inv["status"] == InvocationStatus.RESERVED:
            release_credit(
                conn,
                run_id=run_id,
                provider=inv["provider"],
                attempt_id=row["id"],
                invocation_id=inv["id"],
            )
    _sync_work_status(
        conn, run["work_item_id"], WorkItemStatus.CANCELLED, actor=actor
    )
    append_audit_event(
        conn,
        event_type="runtime.cancelled",
        actor=actor,
        payload={"run_id": run_id},
    )
    return {"run": get_run(conn, run_id)}


def provider_limit_halt(
    conn: sqlite3.Connection, *, run_id: str, actor: str
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    cursor = conn.execute(
        """
        UPDATE runtime_runs
        SET provider_limit_state = ?, revision = revision + 1, updated_at = ?
        WHERE id = ? AND revision = ?
        """,
        (ProviderLimitState.HALTED, utc_now_iso(), run_id, run["revision"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError("provider limit halt CAS failed")
    append_audit_event(
        conn,
        event_type="runtime.provider_limit_halted",
        actor=actor,
        anomaly_code=AnomalyCode.A3,
        payload={"run_id": run_id},
    )
    return get_run(conn, run_id)


def provider_limit_continue(
    conn: sqlite3.Connection, *, run_id: str, actor: str
) -> dict[str, Any]:
    run = get_run(conn, run_id)
    cursor = conn.execute(
        """
        UPDATE runtime_runs
        SET provider_limit_state = ?, revision = revision + 1, updated_at = ?
        WHERE id = ? AND revision = ?
        """,
        (ProviderLimitState.OPEN, utc_now_iso(), run_id, run["revision"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError("provider limit continue CAS failed")
    append_audit_event(
        conn,
        event_type="runtime.provider_limit_continued",
        actor=actor,
        payload={"run_id": run_id},
    )
    return get_run(conn, run_id)


def provider_limit_reroute(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    new_provider: str,
    actor: str,
    grant: Grant,
) -> dict[str, Any]:
    if new_provider not in grant.providers:
        raise ValidationFailedError(f"provider {new_provider} not in grant")
    run = get_run(conn, run_id)
    cursor = conn.execute(
        """
        UPDATE runtime_runs
        SET provider = ?, provider_limit_state = ?, revision = revision + 1, updated_at = ?
        WHERE id = ? AND revision = ?
        """,
        (
            new_provider,
            ProviderLimitState.OPEN,
            utc_now_iso(),
            run_id,
            run["revision"],
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError("provider limit reroute CAS failed")
    append_audit_event(
        conn,
        event_type="runtime.provider_limit_rerouted",
        actor=actor,
        payload={"run_id": run_id, "provider": new_provider},
    )
    return get_run(conn, run_id)


def begin_reconcile(
    conn: sqlite3.Connection, *, run_id: str, actor: str
) -> dict[str, Any]:
    run = _cas_run(
        conn,
        run_id,
        expected_status=RunStatus.OUTCOME_UNKNOWN,
        target_status=RunStatus.RECONCILING,
    )
    row = conn.execute(
        """
        SELECT id FROM runtime_attempts
        WHERE run_id = ? AND status = ?
        ORDER BY attempt_number DESC LIMIT 1
        """,
        (run_id, AttemptStatus.OUTCOME_UNKNOWN),
    ).fetchone()
    if row is None:
        raise NotFoundError("no outcome_unknown attempt to reconcile")
    attempt = _cas_attempt(
        conn,
        row["id"],
        expected_status=AttemptStatus.OUTCOME_UNKNOWN,
        target_status=AttemptStatus.RECONCILING,
    )
    _sync_work_status(
        conn, run["work_item_id"], WorkItemStatus.RECONCILING, actor=actor
    )
    append_audit_event(
        conn,
        event_type="runtime.reconcile_started",
        actor=actor,
        anomaly_code=AnomalyCode.A1,
        payload={"run_id": run_id, "attempt_id": attempt["id"]},
    )
    return {"run": get_run(conn, run_id), "attempt": attempt}


def finish_reconcile(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    outcome: str,
    actor: str,
    evidence: dict[str, Any] | None = None,
    runners: dict[str, ProviderRunner] | None = None,
) -> dict[str, Any]:
    if outcome not in {"complete", "failed", "cancelled"}:
        raise ValidationFailedError(f"invalid reconcile outcome: {outcome}")
    run = get_run(conn, run_id)
    if RunStatus(run["status"]) != RunStatus.RECONCILING:
        raise ConflictError("run is not reconciling")
    row = conn.execute(
        """
        SELECT id FROM runtime_attempts
        WHERE run_id = ? AND status = ?
        ORDER BY attempt_number DESC LIMIT 1
        """,
        (run_id, AttemptStatus.RECONCILING),
    ).fetchone()
    if row is None:
        raise NotFoundError("no reconciling attempt")
    attempt_id = row["id"]
    invocation = get_invocation_for_attempt(conn, attempt_id)
    if invocation is None:
        raise ConflictError("reconcile requires original invocation")

    registry = runners or default_mock_registry()
    runner = registry.get(invocation["provider"])
    reconcile_evidence = evidence or {}
    if runner is not None:
        recon = runner.reconcile(invocation["id"])
        reconcile_evidence = {**reconcile_evidence, **recon.evidence}
        # Trust runner-discovered outcome only for the default reconcile finish path.
        if evidence is None and outcome == "complete":
            if recon.outcome in {"complete", "failed"}:
                outcome = recon.outcome

    evidence_id = new_id()
    conn.execute(
        """
        INSERT INTO reconciliation_evidence (
            id, attempt_id, invocation_id, outcome, evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            attempt_id,
            invocation["id"],
            outcome,
            json.dumps(reconcile_evidence),
            utc_now_iso(),
        ),
    )
    conn.execute(
        """
        UPDATE provider_invocations
        SET status = ?, evidence_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            InvocationStatus.RECONCILED,
            json.dumps(reconcile_evidence),
            utc_now_iso(),
            invocation["id"],
        ),
    )

    if outcome == "complete":
        attempt_target, run_target, work_target = (
            AttemptStatus.COMPLETE,
            RunStatus.COMPLETE,
            WorkItemStatus.COMPLETE,
        )
        assert_completion_prerequisites(conn, run["work_item_id"])
    elif outcome == "failed":
        attempt_target, run_target, work_target = (
            AttemptStatus.FAILED,
            RunStatus.FAILED,
            WorkItemStatus.FAILED,
        )
    else:
        attempt_target, run_target, work_target = (
            AttemptStatus.CANCELLED,
            RunStatus.CANCELLED,
            WorkItemStatus.CANCELLED,
        )

    _cas_attempt(
        conn,
        attempt_id,
        expected_status=AttemptStatus.RECONCILING,
        target_status=attempt_target,
    )
    _cas_run(
        conn,
        run_id,
        expected_status=RunStatus.RECONCILING,
        target_status=run_target,
    )
    _sync_work_status(conn, run["work_item_id"], work_target, actor=actor)
    append_audit_event(
        conn,
        event_type="runtime.reconcile_finished",
        actor=actor,
        payload={
            "run_id": run_id,
            "attempt_id": attempt_id,
            "invocation_id": invocation["id"],
            "outcome": outcome,
            "evidence_id": evidence_id,
        },
    )
    return {
        "run": get_run(conn, run_id),
        "attempt": get_attempt(conn, attempt_id),
        "invocation": get_invocation_for_attempt(conn, attempt_id),
        "evidence_id": evidence_id,
    }


def new_attempt_after_unknown(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    actor: str,
) -> dict[str, Any]:
    """Founder-only paid retry after unknown/reconciled terminal. New attempt identity."""
    run = get_run(conn, run_id)
    status = RunStatus(run["status"])
    if status not in {
        RunStatus.OUTCOME_UNKNOWN,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.COMPLETE,
    }:
        # After reconcile, status is terminal; allow failed/cancelled/complete
        if status != RunStatus.FAILED and status not in {
            RunStatus.CANCELLED,
            RunStatus.COMPLETE,
        }:
            raise ConflictError(
                f"new attempt not allowed from status {status}; reconcile first"
            )

    # Prefer moving failed reconciled runs back to pending then claim path
    if status in {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.COMPLETE}:
        # Reset run to pending for a new paid attempt (does not auto-dispatch)
        if status == RunStatus.FAILED:
            run = _cas_run(
                conn,
                run_id,
                expected_status=RunStatus.FAILED,
                target_status=RunStatus.PENDING,
            )
            _sync_work_status(
                conn, run["work_item_id"], WorkItemStatus.PENDING, actor=actor
            )
        else:
            # For complete/cancelled after unknown path, reopen via direct update with transition check
            # COMPLETE/CANCELLED are terminal in RUN_TRANSITIONS — founder exception creates NEW run attempt
            # by inserting a new attempt and forcing pending via audited exception path.
            now = utc_now_iso()
            cursor = conn.execute(
                """
                UPDATE runtime_runs
                SET status = ?, revision = revision + 1, updated_at = ?
                WHERE id = ? AND revision = ?
                """,
                (RunStatus.PENDING, now, run_id, run["revision"]),
            )
            if cursor.rowcount != 1:
                raise ConflictError("founder reopen CAS failed")
            _sync_work_status(
                conn, run["work_item_id"], WorkItemStatus.PENDING, actor=actor
            )
            run = get_run(conn, run_id)
    elif status == RunStatus.OUTCOME_UNKNOWN:
        raise OutcomeUnknownError("reconcile original invocation before new paid attempt")

    max_row = conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) AS n FROM runtime_attempts WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    next_number = int(max_row["n"]) + 1
    now = utc_now_iso()
    attempt_id = new_id()
    conn.execute(
        """
        INSERT INTO runtime_attempts (
            id, run_id, attempt_number, status, revision, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 0, ?, ?)
        """,
        (attempt_id, run_id, next_number, AttemptStatus.PENDING, now, now),
    )
    append_audit_event(
        conn,
        event_type="runtime.new_attempt_after_unknown",
        actor=actor,
        anomaly_code=AnomalyCode.A2,
        payload={
            "run_id": run_id,
            "attempt_id": attempt_id,
            "attempt_number": next_number,
            "note": "founder step-up authorized; credits and gates not bypassed",
        },
    )
    return {"run": get_run(conn, run_id), "attempt": get_attempt(conn, attempt_id)}


def evaluate_timeouts(
    conn: sqlite3.Connection, *, actor: str = "system"
) -> list[dict[str, Any]]:
    """Mark claimed attempts past hard/inactivity deadlines as outcome_unknown when dispatched."""
    results: list[dict[str, Any]] = []
    now = utc_now_iso()
    rows = conn.execute(
        """
        SELECT * FROM runtime_attempts
        WHERE status = ?
        """,
        (AttemptStatus.CLAIMED,),
    ).fetchall()
    for row in rows:
        attempt = _row_to_attempt(row)
        timed_out = False
        if attempt["hard_deadline_at"] and is_expired(
            attempt["hard_deadline_at"], now_iso=now
        ):
            timed_out = True
        if attempt["inactivity_deadline_at"] and is_expired(
            attempt["inactivity_deadline_at"], now_iso=now
        ):
            timed_out = True
        if attempt["lease_expires_at"] and is_expired(
            attempt["lease_expires_at"], now_iso=now
        ):
            timed_out = True
        if not timed_out:
            continue
        if not attempt["possible_side_effect"] and not attempt["dispatched_at"]:
            # Pre-dispatch timeout → failed without credit consumption if reserved
            inv = get_invocation_for_attempt(conn, attempt["id"])
            if inv and inv["status"] == InvocationStatus.RESERVED:
                release_credit(
                    conn,
                    run_id=attempt["run_id"],
                    provider=inv["provider"],
                    attempt_id=attempt["id"],
                    invocation_id=inv["id"],
                )
            _cas_attempt(
                conn,
                attempt["id"],
                expected_status=AttemptStatus.CLAIMED,
                target_status=AttemptStatus.FAILED,
            )
            _cas_run(
                conn,
                attempt["run_id"],
                expected_status=RunStatus.CLAIMED,
                target_status=RunStatus.FAILED,
            )
            run = get_run(conn, attempt["run_id"])
            _sync_work_status(
                conn, run["work_item_id"], WorkItemStatus.FAILED, actor=actor
            )
            results.append({"attempt_id": attempt["id"], "outcome": "failed"})
            continue

        # Possible dispatch → outcome_unknown + settle credit
        inv = get_invocation_for_attempt(conn, attempt["id"])
        if inv and inv["status"] in {
            InvocationStatus.RESERVED,
            InvocationStatus.DISPATCHED,
        }:
            settle_credit(
                conn,
                run_id=attempt["run_id"],
                provider=inv["provider"],
                attempt_id=attempt["id"],
                invocation_id=inv["id"],
            )
            conn.execute(
                """
                UPDATE provider_invocations
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (InvocationStatus.OUTCOME_UNKNOWN, now, inv["id"]),
            )
        _cas_attempt(
            conn,
            attempt["id"],
            expected_status=AttemptStatus.CLAIMED,
            target_status=AttemptStatus.OUTCOME_UNKNOWN,
        )
        _cas_run(
            conn,
            attempt["run_id"],
            expected_status=RunStatus.CLAIMED,
            target_status=RunStatus.OUTCOME_UNKNOWN,
        )
        run = get_run(conn, attempt["run_id"])
        _sync_work_status(
            conn, run["work_item_id"], WorkItemStatus.OUTCOME_UNKNOWN, actor=actor
        )
        append_audit_event(
            conn,
            event_type="runtime.timeout_unknown",
            actor=actor,
            anomaly_code=AnomalyCode.A1,
            payload={"attempt_id": attempt["id"], "run_id": attempt["run_id"]},
        )
        results.append({"attempt_id": attempt["id"], "outcome": "outcome_unknown"})
    return results
