"""Immutable artifact metadata registration (GOV-ARTIFACT-001)."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.domain.errors import NotFoundError
from flow_engine.domain.models import new_id


def _artifact_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "uri": row["uri"],
        "artifact_type": row["artifact_type"],
        "content_hash": row["content_hash"],
        "sensitivity": row["sensitivity"],
        "retention_class": row["retention_class"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def register_artifact(
    conn: sqlite3.Connection,
    *,
    uri: str,
    artifact_type: str,
    sensitivity: str,
    retention_class: str,
    created_by: str,
    content_hash: str | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    if not uri.strip():
        raise ValueError("uri is required")
    if not sensitivity.strip() or not retention_class.strip():
        raise ValueError("sensitivity and retention_class are required")

    artifact_id = artifact_id or new_id()
    created_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO artifacts (
            id, uri, artifact_type, content_hash, sensitivity,
            retention_class, created_by, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            artifact_id,
            uri,
            artifact_type,
            content_hash,
            sensitivity,
            retention_class,
            created_by,
            created_at,
        ),
    )
    artifact = _artifact_row(
        conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    )
    append_event(
        conn,
        event_type="artifact.registered",
        actor=created_by,
        payload={"artifact_id": artifact_id, "uri": uri, "artifact_type": artifact_type},
    )
    return artifact


def get_artifact(conn: sqlite3.Connection, artifact_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"artifact not found: {artifact_id}")
    return _artifact_row(row)
