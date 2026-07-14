"""Queue management."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.event_service import append_event
from flow_engine.application.project_service import get_default_project
from flow_engine.domain.errors import NotFoundError
from flow_engine.domain.models import new_id


def ensure_queue(
    conn: sqlite3.Connection,
    *,
    name: str,
    project_id: str | None = None,
    actor: str = "system",
) -> dict[str, Any]:
    if project_id is None:
        project_id = get_default_project(conn)["id"]

    row = conn.execute(
        "SELECT id, project_id, name FROM queues WHERE project_id = ? AND name = ?",
        (project_id, name),
    ).fetchone()
    if row is not None:
        return {"id": row["id"], "project_id": row["project_id"], "name": row["name"]}

    queue_id = new_id()
    conn.execute(
        "INSERT INTO queues (id, project_id, name) VALUES (?, ?, ?)",
        (queue_id, project_id, name),
    )
    append_event(
        conn,
        event_type="queue.created",
        actor=actor,
        payload={"queue_id": queue_id, "name": name},
    )
    return {"id": queue_id, "project_id": project_id, "name": name}


def list_queues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, project_id, name FROM queues ORDER BY name"
    ).fetchall()
    return [
        {"id": row["id"], "project_id": row["project_id"], "name": row["name"]}
        for row in rows
    ]


def get_queue(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, project_id, name FROM queues WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"queue not found: {name}")
    return {"id": row["id"], "project_id": row["project_id"], "name": row["name"]}


def show_queue(conn: sqlite3.Connection, name: str) -> dict[str, Any]:
    queue = get_queue(conn, name)
    items = conn.execute(
        """
        SELECT id, status, claimed_by, revision
        FROM work_items
        WHERE queue_id = ?
        ORDER BY rowid
        """,
        (queue["id"],),
    ).fetchall()
    queue["work_items"] = [dict(row) for row in items]
    return queue
