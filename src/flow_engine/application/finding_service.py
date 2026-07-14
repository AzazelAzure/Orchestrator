"""Generic finding lifecycle (GOV-FINDING-001)."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.artifact_service import get_artifact
from flow_engine.application.clock import utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.application.idempotency import run_idempotent
from flow_engine.application.policy_service import get_policy_version
from flow_engine.domain.errors import ConflictError, NotFoundError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import FindingSeverity, FindingStatus
from flow_engine.domain.transitions import assert_finding_transition


def _finding_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "work_item_id": row["work_item_id"],
        "severity": row["severity"],
        "status": row["status"],
        "summary": row["summary"],
        "revision": row["revision"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _get_finding(conn: sqlite3.Connection, finding_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"finding not found: {finding_id}")
    return _finding_row(row)


def _attach_evidence(
    conn: sqlite3.Connection,
    finding_id: str,
    artifact_id: str,
) -> None:
    get_artifact(conn, artifact_id)
    conn.execute(
        "INSERT OR IGNORE INTO finding_evidence (finding_id, artifact_id) VALUES (?, ?)",
        (finding_id, artifact_id),
    )


def create_finding(
    conn: sqlite3.Connection,
    *,
    summary: str,
    severity: FindingSeverity,
    actor: str,
    project_id: str | None = None,
    work_item_id: str | None = None,
    evidence_artifact_ids: list[str] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _create() -> dict[str, Any]:
        if not summary.strip():
            raise ValueError("summary is required")
        finding_id = new_id()
        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO findings (
                id, project_id, work_item_id, severity, status, summary,
                revision, created_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                finding_id,
                project_id,
                work_item_id,
                severity,
                FindingStatus.OPEN,
                summary,
                actor,
                now,
                now,
            ),
        )
        for artifact_id in evidence_artifact_ids or []:
            _attach_evidence(conn, finding_id, artifact_id)
        conn.execute(
            """
            INSERT INTO finding_actions (
                id, finding_id, action_type, actor, from_status, to_status,
                reason, evidence_artifact_id, policy_version_id, finding_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                finding_id,
                "created",
                actor,
                None,
                FindingStatus.OPEN,
                None,
                (evidence_artifact_ids or [None])[0] if evidence_artifact_ids else None,
                None,
                0,
                now,
            ),
        )
        append_event(
            conn,
            event_type="finding.created",
            actor=actor,
            payload={"finding_id": finding_id, "severity": severity},
        )
        return _get_finding(conn, finding_id)

    result, from_cache = run_idempotent(conn, idempotency_key, _create)
    return {**result, "from_cache": from_cache}


def transition_finding(
    conn: sqlite3.Connection,
    *,
    finding_id: str,
    target_status: FindingStatus,
    actor: str,
    reason: str = "",
    evidence_artifact_id: str | None = None,
    policy_version_id: str | None = None,
    expected_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    def _transition() -> dict[str, Any]:
        finding = _get_finding(conn, finding_id)
        current = FindingStatus(finding["status"])
        assert_finding_transition(current, target_status)

        if expected_revision is not None and finding["revision"] != expected_revision:
            raise ConflictError(
                f"revision mismatch for finding {finding_id}: expected {expected_revision}, got {finding['revision']}"
            )

        if evidence_artifact_id:
            _attach_evidence(conn, finding_id, evidence_artifact_id)
        if policy_version_id:
            get_policy_version(conn, policy_version_id)

        now = utc_now_iso()
        cursor = conn.execute(
            """
            UPDATE findings
            SET status = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND status = ? AND revision = ?
            """,
            (target_status, now, finding_id, current, finding["revision"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for finding {finding_id}")

        updated = _get_finding(conn, finding_id)
        conn.execute(
            """
            INSERT INTO finding_actions (
                id, finding_id, action_type, actor, from_status, to_status,
                reason, evidence_artifact_id, policy_version_id, finding_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                finding_id,
                "transition",
                actor,
                current,
                target_status,
                reason or None,
                evidence_artifact_id,
                policy_version_id,
                updated["revision"],
                now,
            ),
        )
        append_event(
            conn,
            event_type="finding.transitioned",
            actor=actor,
            payload={
                "finding_id": finding_id,
                "from_status": current,
                "to_status": target_status,
                "revision": updated["revision"],
            },
        )
        return updated

    result, from_cache = run_idempotent(conn, idempotency_key, _transition)
    return {**result, "from_cache": from_cache}


def amend_finding(
    conn: sqlite3.Connection,
    *,
    finding_id: str,
    actor: str,
    reason: str,
    expected_revision: int | None = None,
    summary: str | None = None,
    severity: FindingSeverity | None = None,
    evidence_artifact_id: str | None = None,
    policy_version_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("reason is required for finding amendment")
    if summary is None and severity is None:
        raise ValueError("summary and/or severity must be supplied for amendment")
    if summary is not None and not summary.strip():
        raise ValueError("summary must be non-empty when supplied")

    def _amend() -> dict[str, Any]:
        finding = _get_finding(conn, finding_id)
        current_status = FindingStatus(finding["status"])

        if expected_revision is not None and finding["revision"] != expected_revision:
            raise ConflictError(
                f"revision mismatch for finding {finding_id}: expected {expected_revision}, got {finding['revision']}"
            )

        if policy_version_id:
            get_policy_version(conn, policy_version_id)
        if evidence_artifact_id:
            _attach_evidence(conn, finding_id, evidence_artifact_id)

        new_summary = summary if summary is not None else finding["summary"]
        new_severity = severity if severity is not None else FindingSeverity(finding["severity"])
        now = utc_now_iso()
        cursor = conn.execute(
            """
            UPDATE findings
            SET summary = ?, severity = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND revision = ?
            """,
            (new_summary, new_severity, now, finding_id, finding["revision"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for finding {finding_id}")

        updated = _get_finding(conn, finding_id)
        conn.execute(
            """
            INSERT INTO finding_actions (
                id, finding_id, action_type, actor, from_status, to_status,
                reason, evidence_artifact_id, policy_version_id, finding_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_id(),
                finding_id,
                "amended",
                actor,
                current_status,
                current_status,
                reason,
                evidence_artifact_id,
                policy_version_id,
                updated["revision"],
                now,
            ),
        )
        append_event(
            conn,
            event_type="finding.amended",
            actor=actor,
            payload={
                "finding_id": finding_id,
                "from_summary": finding["summary"],
                "to_summary": new_summary,
                "from_severity": finding["severity"],
                "to_severity": new_severity,
                "revision": updated["revision"],
                "reason": reason,
            },
        )
        return updated

    result, from_cache = run_idempotent(conn, idempotency_key, _amend)
    return {**result, "from_cache": from_cache}


def show_finding(conn: sqlite3.Connection, finding_id: str) -> dict[str, Any]:
    finding = _get_finding(conn, finding_id)
    evidence = conn.execute(
        "SELECT artifact_id FROM finding_evidence WHERE finding_id = ?",
        (finding_id,),
    ).fetchall()
    finding["evidence_artifact_ids"] = [row["artifact_id"] for row in evidence]
    return finding
