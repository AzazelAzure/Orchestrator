"""Deterministic recovery helpers for coordinator restart and delivery replay."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.credit_service import credit_usage
from flow_engine.application.runtime_service import (
    evaluate_timeouts,
    get_attempt,
    get_invocation_for_attempt,
    get_run,
)
from flow_engine.coordinator.audit import append_audit_event
from flow_engine.domain.states import AnomalyCode, AttemptStatus, InvocationStatus, RunStatus


def reconstruct_eligible_deliveries(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Rebuild delivery candidates from SQLite without creating new paid calls."""
    rows = conn.execute(
        """
        SELECT i.id AS invocation_id, i.attempt_id, i.run_id, i.provider, i.status,
               i.request_digest, a.status AS attempt_status, r.status AS run_status
        FROM provider_invocations i
        JOIN runtime_attempts a ON a.id = i.attempt_id
        JOIN runtime_runs r ON r.id = i.run_id
        WHERE i.status IN (?, ?)
        ORDER BY i.created_at ASC
        """,
        (InvocationStatus.RESERVED, InvocationStatus.DISPATCHED),
    ).fetchall()
    deliveries: list[dict[str, Any]] = []
    for row in rows:
        deliveries.append(
            {
                "invocation_id": row["invocation_id"],
                "attempt_id": row["attempt_id"],
                "run_id": row["run_id"],
                "provider": row["provider"],
                "invocation_status": row["status"],
                "attempt_status": row["attempt_status"],
                "run_status": row["run_status"],
                "request_digest": row["request_digest"],
                "action": (
                    "await_callback"
                    if row["status"] == InvocationStatus.DISPATCHED
                    else "deliver_reserved"
                ),
                "duplicate_paid_call": False,
            }
        )
    return deliveries


def recover_after_restart(
    conn: sqlite3.Connection, *, actor: str = "system"
) -> dict[str, Any]:
    """Coordinator restart recovery: timeouts + reconstruct deliveries; no new invocations."""
    timeout_results = evaluate_timeouts(conn, actor=actor)
    deliveries = reconstruct_eligible_deliveries(conn)
    open_unknown = conn.execute(
        """
        SELECT COUNT(*) AS n FROM runtime_runs WHERE status = ?
        """,
        (RunStatus.OUTCOME_UNKNOWN,),
    ).fetchone()["n"]
    append_audit_event(
        conn,
        event_type="runtime.recovery_restart",
        actor=actor,
        anomaly_code=AnomalyCode.A3 if timeout_results else None,
        payload={
            "timeouts": timeout_results,
            "eligible_deliveries": len(deliveries),
            "outcome_unknown_runs": open_unknown,
        },
    )
    return {
        "timeouts": timeout_results,
        "eligible_deliveries": deliveries,
        "outcome_unknown_runs": open_unknown,
        "new_paid_calls": 0,
    }


def recover_worker_death(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    actor: str = "system",
) -> dict[str, Any]:
    """Worker death after possible dispatch → outcome_unknown; never auto paid retry."""
    from flow_engine.application import runtime_service as runtime
    from flow_engine.domain.states import WorkItemStatus

    attempt = get_attempt(conn, attempt_id)
    if AttemptStatus(attempt["status"]) != AttemptStatus.CLAIMED:
        return {"attempt": attempt, "action": "none"}

    if attempt["possible_side_effect"] or attempt["dispatched_at"]:
        result = runtime.submit_result(
            conn,
            attempt_id=attempt_id,
            outcome="outcome_unknown",
            actor=actor,
            evidence={"reason": "worker_death"},
            anomalies=[{"code": "A1", "detail": "worker_death"}],
        )
        return {"action": "outcome_unknown", **result}

    inv = get_invocation_for_attempt(conn, attempt_id)
    if inv is not None:
        result = runtime.submit_result(
            conn,
            attempt_id=attempt_id,
            outcome="failed",
            actor=actor,
            evidence={"reason": "worker_death_pre_dispatch"},
            anomalies=[],
            consume_credit=False,
        )
        return {"action": "failed_pre_dispatch", **result}

    runtime._cas_attempt(
        conn,
        attempt_id,
        expected_status=AttemptStatus.CLAIMED,
        target_status=AttemptStatus.FAILED,
    )
    runtime._cas_run(
        conn,
        attempt["run_id"],
        expected_status=RunStatus.CLAIMED,
        target_status=RunStatus.FAILED,
    )
    run = get_run(conn, attempt["run_id"])
    runtime._sync_work_status(conn, run["work_item_id"], WorkItemStatus.FAILED, actor=actor)
    append_audit_event(
        conn,
        event_type="runtime.worker_death_pre_dispatch",
        actor=actor,
        payload={"attempt_id": attempt_id, "run_id": run["id"]},
    )
    return {
        "action": "failed_pre_dispatch",
        "run": get_run(conn, run["id"]),
        "attempt": get_attempt(conn, attempt_id),
    }


def replay_delivery_hint(
    conn: sqlite3.Connection,
    *,
    invocation_id: str,
) -> dict[str, Any]:
    """Broker/delivery replay is a hint only; SQLite remains authoritative."""
    row = conn.execute(
        "SELECT * FROM provider_invocations WHERE id = ?",
        (invocation_id,),
    ).fetchone()
    if row is None:
        return {"accepted": False, "reason": "unknown_invocation"}
    attempt = get_attempt(conn, row["attempt_id"])
    run = get_run(conn, row["run_id"])
    existing = get_invocation_for_attempt(conn, row["attempt_id"])
    return {
        "accepted": True,
        "duplicate_paid_call": False,
        "authoritative_status": existing["status"] if existing else None,
        "run_status": run["status"],
        "attempt_status": attempt["status"],
        "credits": credit_usage(conn, run["id"]),
        "hint_only": True,
    }
