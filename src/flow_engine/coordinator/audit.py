"""Append-only audit and mandatory anomaly emission."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.domain.errors import PersistenceUnavailableError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import AnomalyCode


def append_audit_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    anomaly_code: AnomalyCode | str | None = None,
    command_id: str | None = None,
) -> dict[str, Any]:
    """Persist an audit/anomaly event or fail closed."""
    event_id = new_id()
    created_at = utc_now_iso()
    code = str(anomaly_code) if anomaly_code is not None else None
    try:
        conn.execute(
            """
            INSERT INTO audit_events (
                id, event_type, actor, anomaly_code, command_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                event_type,
                actor,
                code,
                command_id,
                json.dumps(payload or {}),
                created_at,
            ),
        )
    except sqlite3.Error as exc:
        raise PersistenceUnavailableError(
            f"audit persistence unavailable: {exc}"
        ) from exc

    return {
        "id": event_id,
        "event_type": event_type,
        "actor": actor,
        "anomaly_code": code,
        "command_id": command_id,
        "payload": payload or {},
        "created_at": created_at,
    }


def list_audit_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    anomaly_code: str | None = None,
) -> list[dict[str, Any]]:
    query = (
        "SELECT id, event_type, actor, anomaly_code, command_id, payload_json, created_at "
        "FROM audit_events"
    )
    params: list[Any] = []
    if anomaly_code:
        query += " WHERE anomaly_code = ?"
        params.append(anomaly_code)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "anomaly_code": row["anomaly_code"],
            "command_id": row["command_id"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }
        for row in rows
    ]
