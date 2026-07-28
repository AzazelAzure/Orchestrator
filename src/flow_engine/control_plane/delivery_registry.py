"""Async delivery job registry — coordinator-owned, SQLite authoritative."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.domain.errors import ConflictError, NotFoundError, ValidationFailedError
from flow_engine.domain.models import new_id

DELIVERY_ACTIVE = frozenset({"registered", "delivering", "delivered"})
DELIVERY_TERMINAL = frozenset({"completed", "failed", "stale"})


def register_delivery_job(
    conn: sqlite3.Connection,
    *,
    invocation_id: str,
    attempt_id: str,
    run_id: str,
    provider: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Idempotent delivery registration. Same key returns prior job."""
    existing = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return _row_to_job(existing)

    job_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO control_plane_delivery_jobs (
            id, idempotency_key, invocation_id, attempt_id, run_id, provider,
            status, registered_at, redelivery_count
        ) VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, 0)
        """,
        (job_id, idempotency_key, invocation_id, attempt_id, run_id, provider, now),
    )
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row is not None
    return _row_to_job(row)


def claim_delivery_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    worker_principal_id: str,
    celery_task_id: str | None = None,
    attempt_id: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, Any]:
    """Claim a registered job for delivery. Redelivery increments counter."""
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    status = row["status"]
    now = utc_now_iso()

    job = _row_to_job(row)
    if attempt_id or invocation_id:
        assert_delivery_ownership(
            conn,
            job,
            worker_principal_id=worker_principal_id,
            attempt_id=attempt_id or job["attempt_id"],
            invocation_id=invocation_id or job["invocation_id"],
            require_worker_match=False,
        )
    if job.get("outcome_unknown"):
        raise ConflictError("outcome_unknown delivery requires reconciliation before replay")

    if status == "registered":
        conn.execute(
            """
            UPDATE control_plane_delivery_jobs
            SET status = 'delivering', worker_principal_id = ?,
                celery_task_id = ?, heartbeat_at = ?
            WHERE id = ? AND status = 'registered'
            """,
            (worker_principal_id, celery_task_id, now, job_id),
        )
    elif status == "delivering":
        if row["worker_principal_id"] and row["worker_principal_id"] != worker_principal_id:
            raise ConflictError("delivery job held by different worker")
        conn.execute(
            """
            UPDATE control_plane_delivery_jobs
            SET redelivery_count = redelivery_count + 1,
                celery_task_id = COALESCE(?, celery_task_id),
                heartbeat_at = ?
            WHERE id = ?
            """,
            (celery_task_id, now, job_id),
        )
    elif status in DELIVERY_TERMINAL:
        return _row_to_job(row)
    else:
        raise ConflictError(f"delivery job in unexpected status: {status}")

    updated = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert updated is not None
    return _row_to_job(updated)


def acquire_exclusive_dispatch_lease(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    worker_principal_id: str,
    lease_token: str,
) -> dict[str, Any]:
    """CAS: only one provider-I/O execution may hold the dispatch lease.

    Returns the updated job when this caller acquired (or already holds) the lease.
    Raises ConflictError with code CONFLICT_CAS when another holder owns it or
    the job is terminal / outcome_unknown.
    """
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    job = _row_to_job(row)
    if job.get("outcome_unknown"):
        raise ConflictError("outcome_unknown delivery requires reconciliation before replay")
    if job["status"] in DELIVERY_TERMINAL or job["status"] == "delivered":
        raise ConflictError("delivery job already terminal")

    existing_lease = None
    if isinstance(job.get("result_json"), dict):
        existing_lease = job["result_json"].get("dispatch_lease")

    if existing_lease and existing_lease != lease_token:
        raise ConflictError(
            "dispatch lease held by another execution",
            code="CONFLICT_CAS",
        )
    if existing_lease == lease_token:
        return job

    now = utc_now_iso()
    lease_payload = {
        "dispatch_lease": lease_token,
        "lease_held": True,
        "lease_worker_principal_id": worker_principal_id,
        "leased_at": now,
    }
    # Merge any prior non-lease result_json keys (should be empty pre-settle).
    prior = job.get("result_json") if isinstance(job.get("result_json"), dict) else {}
    merged = {**prior, **lease_payload}

    if job["status"] == "registered":
        cursor = conn.execute(
            """
            UPDATE control_plane_delivery_jobs
            SET status = 'delivering',
                worker_principal_id = ?,
                heartbeat_at = ?,
                result_json = ?
            WHERE id = ? AND status = 'registered'
              AND (result_json IS NULL OR result_json = '' OR result_json = '{}')
            """,
            (worker_principal_id, now, json.dumps(merged), job_id),
        )
    else:
        # delivering without a lease yet — exclusive CAS on empty/null result_json
        cursor = conn.execute(
            """
            UPDATE control_plane_delivery_jobs
            SET worker_principal_id = COALESCE(worker_principal_id, ?),
                heartbeat_at = ?,
                result_json = ?
            WHERE id = ? AND status = 'delivering'
              AND (result_json IS NULL OR result_json = '' OR result_json = '{}')
            """,
            (worker_principal_id, now, json.dumps(merged), job_id),
        )
    if cursor.rowcount != 1:
        # Re-read: lost race or lease already taken.
        refreshed = get_delivery_job(conn, job_id)
        held = None
        if isinstance(refreshed.get("result_json"), dict):
            held = refreshed["result_json"].get("dispatch_lease")
        if held == lease_token:
            return refreshed
        raise ConflictError(
            "dispatch lease held by another execution",
            code="CONFLICT_CAS",
        )
    return get_delivery_job(conn, job_id)


def _worker_principal_allowed(
    conn: sqlite3.Connection,
    *,
    worker_principal_id: str,
    provider: str,
) -> bool:
    expected_key = f"worker.provider.{provider}"
    if worker_principal_id in {expected_key, "worker"}:
        return True
    row = conn.execute(
        "SELECT principal_key FROM control_plane_principals WHERE id = ?",
        (worker_principal_id,),
    ).fetchone()
    if row is None:
        return False
    return row["principal_key"] in {"worker", expected_key}


def assert_delivery_ownership(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    *,
    worker_principal_id: str,
    attempt_id: str,
    invocation_id: str,
    require_worker_match: bool = True,
) -> None:
    """Bind delivery ownership to job/attempt/invocation (+ worker when claimed)."""
    if job["attempt_id"] != attempt_id:
        raise ConflictError("delivery job attempt_id mismatch")
    if job["invocation_id"] != invocation_id:
        raise ConflictError("delivery job invocation_id mismatch")
    if not _worker_principal_allowed(
        conn,
        worker_principal_id=worker_principal_id,
        provider=job["provider"],
    ):
        raise ConflictError("delivery worker principal/provider mismatch")
    owner = job.get("worker_principal_id")
    if require_worker_match and owner and owner != worker_principal_id:
        raise ConflictError("delivery job held by different worker")


def get_delivery_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    return _row_to_job(row)


def mark_delivery_outcome_unknown(
    conn: sqlite3.Connection,
    *,
    job_id: str,
) -> dict[str, Any]:
    """Mark job as failed with outcome_unknown flag; blocks replay until reconcile."""
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    now = utc_now_iso()
    prior = json.loads(row["result_json"]) if row["result_json"] else {}
    if not isinstance(prior, dict):
        prior = {}
    result = {
        **prior,
        "outcome_unknown": True,
        "requires_reconciliation": True,
    }
    # Drop live lease marker; keep token for audit if present.
    result.pop("lease_held", None)
    conn.execute(
        """
        UPDATE control_plane_delivery_jobs
        SET status = 'failed', completed_at = ?, result_json = ?
        WHERE id = ?
        """,
        (now, json.dumps(result), job_id),
    )
    updated = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert updated is not None
    return _row_to_job(updated)


def heartbeat_delivery_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    worker_principal_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    if row["worker_principal_id"] and row["worker_principal_id"] != worker_principal_id:
        raise ConflictError("delivery job held by different worker")
    now = utc_now_iso()
    conn.execute(
        "UPDATE control_plane_delivery_jobs SET heartbeat_at = ? WHERE id = ?",
        (now, job_id),
    )
    event = {"alive": True, "worker_principal_id": worker_principal_id}
    event_json = json.dumps(event, sort_keys=True)
    event_digest = hashlib.sha256(event_json.encode()).hexdigest()
    conn.execute(
        """
        UPDATE provider_invocations SET heartbeat_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, now, row["invocation_id"]),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO provider_runner_events
        (id, invocation_id, event_type, event_digest, redacted_event_json, created_at)
        VALUES (?, ?, 'heartbeat', ?, ?, ?)
        """,
        (new_id(), row["invocation_id"], event_digest, event_json, now),
    )
    updated = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert updated is not None
    return _row_to_job(updated)


def mark_delivery_delivered(
    conn: sqlite3.Connection,
    *,
    job_id: str,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE control_plane_delivery_jobs
        SET status = 'delivered', delivered_at = ?
        WHERE id = ? AND status IN ('registered', 'delivering')
        """,
        (now, job_id),
    )
    updated = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert updated is not None
    return _row_to_job(updated)


def complete_delivery_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    outcome: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError("delivery job not found")
    if row["status"] in DELIVERY_TERMINAL:
        return _row_to_job(row)
    status = "completed" if outcome == "complete" else "failed"
    now = utc_now_iso()
    prior = json.loads(row["result_json"]) if row["result_json"] else {}
    if not isinstance(prior, dict):
        prior = {}
    merged = {**prior, **(result or {})}
    merged.pop("lease_held", None)
    conn.execute(
        """
        UPDATE control_plane_delivery_jobs
        SET status = ?, completed_at = ?, result_json = ?
        WHERE id = ?
        """,
        (status, now, json.dumps(merged), job_id),
    )
    updated = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert updated is not None
    return _row_to_job(updated)


def list_eligible_delivery_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM control_plane_delivery_jobs
        WHERE status IN ('registered', 'delivering')
        ORDER BY registered_at ASC
        """
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def recover_stale_delivery_jobs(
    conn: sqlite3.Connection,
    *,
    stale_before_iso: str,
) -> list[dict[str, Any]]:
    """Recover stale delivering jobs.

    Jobs with possible_side_effect / dispatch intent → outcome_unknown (never
    auto-reset to registered). Pre-intent claims may return to registered.
    """
    from flow_engine.application.runtime_service import get_attempt, submit_result
    from flow_engine.domain.states import AttemptStatus

    rows = conn.execute(
        """
        SELECT * FROM control_plane_delivery_jobs
        WHERE status = 'delivering'
          AND (heartbeat_at IS NULL OR heartbeat_at < ?)
        """,
        (stale_before_iso,),
    ).fetchall()
    recovered: list[dict[str, Any]] = []
    for row in rows:
        job = _row_to_job(row)
        attempt = get_attempt(conn, job["attempt_id"])
        has_dispatch_intent = bool(
            attempt.get("possible_side_effect")
            or attempt.get("dispatched_at")
            or (
                isinstance(job.get("result_json"), dict)
                and job["result_json"].get("dispatch_lease")
            )
        )
        if has_dispatch_intent:
            mark_delivery_outcome_unknown(conn, job_id=job["id"])
            if AttemptStatus(attempt["status"]) == AttemptStatus.CLAIMED:
                submit_result(
                    conn,
                    attempt_id=job["attempt_id"],
                    outcome="outcome_unknown",
                    actor="system",
                    evidence={"reason": "stale_delivery_with_dispatch_intent"},
                    anomalies=[
                        {
                            "code": "A1",
                            "detail": "stale delivering job with dispatch intent",
                        }
                    ],
                )
            updated = get_delivery_job(conn, job["id"])
            recovered.append(updated)
            continue

        conn.execute(
            """
            UPDATE control_plane_delivery_jobs
            SET status = 'registered',
                redelivery_count = redelivery_count + 1,
                worker_principal_id = NULL,
                celery_task_id = NULL,
                result_json = NULL
            WHERE id = ? AND status = 'delivering'
            """,
            (job["id"],),
        )
        updated_row = conn.execute(
            "SELECT * FROM control_plane_delivery_jobs WHERE id = ?", (job["id"],)
        ).fetchone()
        if updated_row is not None:
            recovered.append(_row_to_job(updated_row))
    return recovered


def get_delivery_job_by_invocation(
    conn: sqlite3.Connection, invocation_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM control_plane_delivery_jobs WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()
    return _row_to_job(row) if row else None


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    result_json = json.loads(row["result_json"]) if row["result_json"] else None
    outcome_unknown = bool(
        isinstance(result_json, dict) and result_json.get("outcome_unknown")
    )
    return {
        "id": row["id"],
        "idempotency_key": row["idempotency_key"],
        "invocation_id": row["invocation_id"],
        "attempt_id": row["attempt_id"],
        "run_id": row["run_id"],
        "provider": row["provider"],
        "celery_task_id": row["celery_task_id"],
        "status": row["status"],
        "registered_at": row["registered_at"],
        "delivered_at": row["delivered_at"],
        "completed_at": row["completed_at"],
        "redelivery_count": row["redelivery_count"],
        "heartbeat_at": row["heartbeat_at"],
        "result_json": result_json,
        "worker_principal_id": row["worker_principal_id"],
        "outcome_unknown": outcome_unknown,
    }


def delivery_idempotency_key(invocation_id: str, attempt_id: str) -> str:
    if not invocation_id or not attempt_id:
        raise ValidationFailedError("invocation_id and attempt_id required")
    return f"delivery|{invocation_id}|{attempt_id}"
