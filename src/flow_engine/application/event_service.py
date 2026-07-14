"""Append-only event ledger operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from flow_engine.domain.models import new_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


def append_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    actor: str,
    payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    event_id = new_id()
    payload_json = json.dumps(payload or {})
    created_at = _now()
    conn.execute(
        """
        INSERT INTO events (id, event_type, actor, payload_json, idempotency_key, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (event_id, event_type, actor, payload_json, idempotency_key, created_at),
    )
    return {
        "id": event_id,
        "event_type": event_type,
        "actor": actor,
        "payload": payload or {},
        "idempotency_key": idempotency_key,
        "created_at": created_at,
    }


def list_events(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    event_type: str | None = None,
) -> list[dict[str, Any]]:
    query = "SELECT id, event_type, actor, payload_json, idempotency_key, created_at FROM events"
    params: list[Any] = []
    if event_type:
        query += " WHERE event_type = ?"
        params.append(event_type)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": row["id"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "payload": json.loads(row["payload_json"]),
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
