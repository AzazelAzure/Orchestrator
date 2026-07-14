"""Immutable policy version registration (GOV-POLICY-001)."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.domain.errors import NotFoundError
from flow_engine.domain.models import new_id


def _policy_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "policy_id": row["policy_id"],
        "version": row["version"],
        "content_hash": row["content_hash"],
        "canonical_uri": row["canonical_uri"],
        "created_by": row["created_by"],
        "effective_at": row["effective_at"],
    }


def register_policy_version(
    conn: sqlite3.Connection,
    *,
    policy_id: str,
    version: str,
    content_hash: str,
    canonical_uri: str,
    created_by: str,
    effective_at: str | None = None,
    policy_version_id: str | None = None,
) -> dict[str, Any]:
    policy_version_id = policy_version_id or new_id()
    effective_at = effective_at or utc_now_iso()
    conn.execute(
        """
        INSERT INTO policy_versions (
            id, policy_id, version, content_hash, canonical_uri, created_by, effective_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            policy_version_id,
            policy_id,
            version,
            content_hash,
            canonical_uri,
            created_by,
            effective_at,
        ),
    )
    policy = _policy_row(
        conn.execute(
            "SELECT * FROM policy_versions WHERE id = ?", (policy_version_id,)
        ).fetchone()
    )
    append_event(
        conn,
        event_type="policy_version.registered",
        actor=created_by,
        payload={"policy_version_id": policy_version_id, "policy_id": policy_id, "version": version},
    )
    return policy


def get_policy_version(conn: sqlite3.Connection, policy_version_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM policy_versions WHERE id = ?", (policy_version_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"policy version not found: {policy_version_id}")
    return _policy_row(row)
