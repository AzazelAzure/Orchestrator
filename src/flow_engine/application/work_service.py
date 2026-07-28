"""Work item queue operations with CAS transitions."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.event_service import append_event
from flow_engine.application.idempotency import run_idempotent
from flow_engine.application.queue_service import ensure_queue, get_queue
from flow_engine.domain.errors import ConflictError, NotFoundError, PrerequisiteError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import GateRequirement, GateStatus, WorkItemStatus
from flow_engine.domain.transitions import assert_work_transition


def _work_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "queue_id": row["queue_id"],
        "status": row["status"],
        "payload": json.loads(row["payload_json"]),
        "claimed_by": row["claimed_by"],
        "revision": row["revision"],
    }


def _get_work(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, queue_id, status, payload_json, claimed_by, revision
        FROM work_items WHERE id = ?
        """,
        (work_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"work item not found: {work_id}")
    return _work_row(row)


def _cas_update(
    conn: sqlite3.Connection,
    work_id: str,
    *,
    expected_revision: int | None,
    expected_status: WorkItemStatus,
    target_status: WorkItemStatus,
    claimed_by: str | None = None,
) -> dict[str, Any]:
    current = _get_work(conn, work_id)

    if WorkItemStatus(current["status"]) != expected_status:
        raise ConflictError(
            f"status mismatch for {work_id}: expected {expected_status}, got {current['status']}"
        )

    assert_work_transition(WorkItemStatus(current["status"]), target_status)

    if expected_revision is not None and current["revision"] != expected_revision:
        raise ConflictError(
            f"revision mismatch for {work_id}: expected {expected_revision}, got {current['revision']}"
        )

    new_revision = current["revision"] + 1
    cursor = conn.execute(
        """
        UPDATE work_items
        SET status = ?, claimed_by = ?, revision = ?
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (
            target_status,
            claimed_by,
            new_revision,
            work_id,
            expected_status,
            current["revision"],
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"compare-and-set failed for work item {work_id}")

    return _get_work(conn, work_id)


def submit_work(
    conn: sqlite3.Connection,
    *,
    queue_name: str,
    payload: dict[str, Any],
    actor: str,
    depends_on: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _submit() -> dict[str, Any]:
        queue = ensure_queue(conn, name=queue_name, actor=actor)
        work_id = new_id()
        conn.execute(
            """
            INSERT INTO work_items (id, queue_id, status, payload_json, revision)
            VALUES (?, ?, ?, ?, 0)
            """,
            (work_id, queue["id"], WorkItemStatus.PENDING, json.dumps(payload)),
        )
        for dep_id in depends_on or []:
            conn.execute(
                """
                INSERT INTO work_dependencies (work_item_id, depends_on_id)
                VALUES (?, ?)
                """,
                (work_id, dep_id),
            )
        result = _get_work(conn, work_id)
        append_event(
            conn,
            event_type="work.submitted",
            actor=actor,
            payload={"work_id": work_id, "queue": queue_name, "payload": payload},
            idempotency_key=idempotency_key,
        )
        return result

    result, from_cache = run_idempotent(conn, idempotency_key, _submit)
    return {**result, "from_cache": from_cache}


def list_work(
    conn: sqlite3.Connection,
    *,
    queue_name: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    query = """
        SELECT w.id, w.queue_id, w.status, w.payload_json, w.claimed_by, w.revision, q.name AS queue_name
        FROM work_items w
        JOIN queues q ON q.id = w.queue_id
        WHERE 1=1
    """
    params: list[Any] = []
    if queue_name:
        query += " AND q.name = ?"
        params.append(queue_name)
    if status:
        query += " AND w.status = ?"
        params.append(status)
    query += " ORDER BY w.rowid"

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "queue_id": row["queue_id"],
            "queue_name": row["queue_name"],
            "status": row["status"],
            "payload": json.loads(row["payload_json"]),
            "claimed_by": row["claimed_by"],
            "revision": row["revision"],
        }
        for row in rows
    ]


def show_work(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    work = _get_work(conn, work_id)
    queue = conn.execute(
        "SELECT name FROM queues WHERE id = ?",
        (work["queue_id"],),
    ).fetchone()
    work["queue_name"] = queue["name"] if queue else None

    deps = conn.execute(
        "SELECT depends_on_id FROM work_dependencies WHERE work_item_id = ?",
        (work_id,),
    ).fetchall()
    work["depends_on"] = [row["depends_on_id"] for row in deps]
    return work


def claim_work(
    conn: sqlite3.Connection,
    *,
    actor: str,
    work_id: str | None = None,
    queue_name: str | None = None,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _claim() -> dict[str, Any]:
        target_id = work_id
        if target_id is None:
            if not queue_name:
                raise ValueError("work_id or queue_name is required to claim work")
            queue = get_queue(conn, queue_name)
            row = conn.execute(
                """
                SELECT id FROM work_items
                WHERE queue_id = ? AND status = ?
                ORDER BY rowid ASC
                LIMIT 1
                """,
                (queue["id"], WorkItemStatus.PENDING),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"no pending work in queue {queue_name}")
            target_id = row["id"]

        updated = _cas_update(
            conn,
            target_id,
            expected_revision=expected_revision,
            expected_status=WorkItemStatus.PENDING,
            target_status=WorkItemStatus.CLAIMED,
            claimed_by=actor,
        )
        append_event(
            conn,
            event_type="work.claimed",
            actor=actor,
            payload={"work_id": target_id},
            idempotency_key=idempotency_key,
        )
        return updated

    result, from_cache = run_idempotent(conn, idempotency_key, _claim)
    return {**result, "from_cache": from_cache}


def _assert_completion_prerequisites(conn: sqlite3.Connection, work_id: str) -> None:
    deps = conn.execute(
        "SELECT depends_on_id FROM work_dependencies WHERE work_item_id = ?",
        (work_id,),
    ).fetchall()
    for row in deps:
        dep = _get_work(conn, row["depends_on_id"])
        if WorkItemStatus(dep["status"]) != WorkItemStatus.COMPLETE:
            raise PrerequisiteError(
                f"dependency {row['depends_on_id']} is not complete (status={dep['status']})"
            )

    for row in conn.execute(
        """
        SELECT id, status, requirement
        FROM gates
        WHERE work_item_id = ?
        """,
        (work_id,),
    ).fetchall():
        if GateRequirement(row["requirement"]) != GateRequirement.REQUIRED:
            continue
        status = GateStatus(row["status"])
        if status not in {GateStatus.PASSED, GateStatus.WAIVED}:
            raise PrerequisiteError(
                f"required gate {row['id']} is unresolved (status={row['status']})"
            )


def assert_completion_prerequisites(conn: sqlite3.Connection, work_id: str) -> None:
    _assert_completion_prerequisites(conn, work_id)


def complete_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _complete() -> dict[str, Any]:
        _assert_completion_prerequisites(conn, work_id)
        updated = _cas_update(
            conn,
            work_id,
            expected_revision=expected_revision,
            expected_status=WorkItemStatus.CLAIMED,
            target_status=WorkItemStatus.COMPLETE,
            claimed_by=actor,
        )
        append_event(
            conn,
            event_type="work.completed",
            actor=actor,
            payload={"work_id": work_id},
            idempotency_key=idempotency_key,
        )
        return updated

    result, from_cache = run_idempotent(conn, idempotency_key, _complete)
    return {**result, "from_cache": from_cache}


def fail_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    reason: str = "",
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _fail() -> dict[str, Any]:
        updated = _cas_update(
            conn,
            work_id,
            expected_revision=expected_revision,
            expected_status=WorkItemStatus.CLAIMED,
            target_status=WorkItemStatus.FAILED,
            claimed_by=None,
        )
        append_event(
            conn,
            event_type="work.failed",
            actor=actor,
            payload={"work_id": work_id, "reason": reason},
            idempotency_key=idempotency_key,
        )
        return updated

    result, from_cache = run_idempotent(conn, idempotency_key, _fail)
    return {**result, "from_cache": from_cache}


def retry_work(
    conn: sqlite3.Connection,
    *,
    work_id: str,
    actor: str,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _retry() -> dict[str, Any]:
        updated = _cas_update(
            conn,
            work_id,
            expected_revision=expected_revision,
            expected_status=WorkItemStatus.FAILED,
            target_status=WorkItemStatus.PENDING,
            claimed_by=None,
        )
        append_event(
            conn,
            event_type="work.retried",
            actor=actor,
            payload={"work_id": work_id},
            idempotency_key=idempotency_key,
        )
        return updated

    result, from_cache = run_idempotent(conn, idempotency_key, _retry)
    return {**result, "from_cache": from_cache}
