"""Gate lifecycle for work items."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.artifact_service import get_artifact
from flow_engine.application.clock import utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.application.idempotency import run_idempotent
from flow_engine.application.policy_service import get_policy_version
from flow_engine.application.work_service import show_work
from flow_engine.domain.errors import ConflictError, NotFoundError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import GateRequirement, GateStatus
from flow_engine.domain.transitions import assert_gate_transition


def _gate_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "work_item_id": row["work_item_id"],
        "gate_type": row["gate_type"],
        "status": row["status"],
        "requirement": row["requirement"],
        "revision": row["revision"],
        "created_at": row["created_at"],
    }


def _get_gate(conn: sqlite3.Connection, gate_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, work_item_id, gate_type, status, requirement, revision, created_at
        FROM gates WHERE id = ?
        """,
        (gate_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"gate not found: {gate_id}")
    return _gate_row(row)


def _validate_requirement(requirement: str) -> GateRequirement:
    try:
        return GateRequirement(requirement)
    except ValueError as exc:
        raise ValueError(f"invalid gate requirement: {requirement}") from exc


def create_gate(
    conn: sqlite3.Connection,
    *,
    work_item_id: str,
    gate_type: str,
    actor: str,
    requirement: GateRequirement = GateRequirement.REQUIRED,
) -> dict[str, Any]:
    show_work(conn, work_item_id)
    gate_id = new_id()
    created_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO gates (
            id, work_item_id, gate_type, status, requirement, revision, created_at
        ) VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (gate_id, work_item_id, gate_type, GateStatus.OPEN, requirement, created_at),
    )
    gate = _get_gate(conn, gate_id)
    append_event(
        conn,
        event_type="gate.created",
        actor=actor,
        payload={
            "gate_id": gate_id,
            "work_item_id": work_item_id,
            "gate_type": gate_type,
            "requirement": requirement,
        },
    )
    return gate


def list_gates(
    conn: sqlite3.Connection,
    *,
    work_item_id: str | None = None,
) -> list[dict[str, Any]]:
    if work_item_id:
        rows = conn.execute(
            """
            SELECT id, work_item_id, gate_type, status, requirement, revision, created_at
            FROM gates WHERE work_item_id = ?
            ORDER BY id
            """,
            (work_item_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, work_item_id, gate_type, status, requirement, revision, created_at
            FROM gates ORDER BY id
            """
        ).fetchall()
    return [_gate_row(row) for row in rows]


def _record_gate_action(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    action_type: str,
    actor: str,
    gate_revision: int,
    authority: str | None = None,
    reason: str | None = None,
    evidence_artifact_id: str | None = None,
    policy_version_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO gate_actions (
            id, gate_id, action_type, actor, authority, reason,
            evidence_artifact_id, gate_revision, policy_version_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            gate_id,
            action_type,
            actor,
            authority,
            reason,
            evidence_artifact_id,
            gate_revision,
            policy_version_id,
            utc_now_iso(),
        ),
    )


def _transition_gate(
    conn: sqlite3.Connection,
    gate_id: str,
    target: GateStatus,
    actor: str,
    event_type: str,
    *,
    expected_revision: int | None = None,
    authority: str | None = None,
    reason: str | None = None,
    evidence_artifact_id: str | None = None,
    policy_version_id: str | None = None,
) -> dict[str, Any]:
    gate = _get_gate(conn, gate_id)
    current = GateStatus(gate["status"])
    assert_gate_transition(current, target)

    if expected_revision is not None and gate["revision"] != expected_revision:
        raise ConflictError(
            f"revision mismatch for gate {gate_id}: expected {expected_revision}, got {gate['revision']}"
        )

    if policy_version_id:
        get_policy_version(conn, policy_version_id)
    if evidence_artifact_id:
        get_artifact(conn, evidence_artifact_id)

    cursor = conn.execute(
        """
        UPDATE gates
        SET status = ?, revision = revision + 1
        WHERE id = ? AND status = ? AND revision = ?
        """,
        (target, gate_id, current, gate["revision"]),
    )
    if cursor.rowcount != 1:
        raise ConflictError(f"gate transition failed for {gate_id}")

    updated = _get_gate(conn, gate_id)
    action_type = {
        GateStatus.PASSED: "passed",
        GateStatus.FAILED: "failed",
        GateStatus.WAIVED: "waived",
    }[target]
    _record_gate_action(
        conn,
        gate_id=gate_id,
        action_type=action_type,
        actor=actor,
        gate_revision=updated["revision"],
        authority=authority,
        reason=reason,
        evidence_artifact_id=evidence_artifact_id,
        policy_version_id=policy_version_id,
    )
    append_event(
        conn,
        event_type=event_type,
        actor=actor,
        payload={"gate_id": gate_id, "status": target, "revision": updated["revision"]},
    )
    return updated


def pass_gate(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    actor: str,
    expected_revision: int | None = None,
    policy_version_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _pass() -> dict[str, Any]:
        return _transition_gate(
            conn,
            gate_id,
            GateStatus.PASSED,
            actor,
            "gate.passed",
            expected_revision=expected_revision,
            policy_version_id=policy_version_id,
        )

    result, from_cache = run_idempotent(conn, idempotency_key, _pass)
    return {**result, "from_cache": from_cache}


def fail_gate(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    actor: str,
    expected_revision: int | None = None,
    policy_version_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _fail() -> dict[str, Any]:
        return _transition_gate(
            conn,
            gate_id,
            GateStatus.FAILED,
            actor,
            "gate.failed",
            expected_revision=expected_revision,
            policy_version_id=policy_version_id,
        )

    result, from_cache = run_idempotent(conn, idempotency_key, _fail)
    return {**result, "from_cache": from_cache}


def waive_gate(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    actor: str,
    authority: str,
    reason: str,
    evidence_artifact_id: str,
    expected_revision: int | None = None,
    policy_version_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not authority.strip():
        raise ValueError("authority is required for gate waiver")
    if not reason.strip():
        raise ValueError("reason is required for gate waiver")
    if not evidence_artifact_id.strip():
        raise ValueError("evidence_artifact_id is required for gate waiver")

    def _waive() -> dict[str, Any]:
        return _transition_gate(
            conn,
            gate_id,
            GateStatus.WAIVED,
            actor,
            "gate.waived",
            expected_revision=expected_revision,
            authority=authority,
            reason=reason,
            evidence_artifact_id=evidence_artifact_id,
            policy_version_id=policy_version_id,
        )

    result, from_cache = run_idempotent(conn, idempotency_key, _waive)
    return {**result, "from_cache": from_cache}
