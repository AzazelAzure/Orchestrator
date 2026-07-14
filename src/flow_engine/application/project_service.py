"""Project bootstrap and status."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from flow_engine.application.event_service import append_event
from flow_engine.domain.errors import NotFoundError
from flow_engine.domain.models import new_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


def init_project(
    conn: sqlite3.Connection,
    *,
    name: str,
    actor: str = "system",
) -> dict[str, Any]:
    project_id = new_id()
    created_at = _now()
    conn.execute(
        "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
        (project_id, name, created_at),
    )
    append_event(
        conn,
        event_type="project.created",
        actor=actor,
        payload={"project_id": project_id, "name": name},
    )
    return {"id": project_id, "name": name, "created_at": created_at}


def get_project(conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, name, created_at FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"project not found: {project_id}")
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}


def get_default_project(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, name, created_at FROM projects ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if row is None:
        raise NotFoundError("no project initialized")
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"]}


def status(conn: sqlite3.Connection) -> dict[str, Any]:
    project = conn.execute(
        "SELECT id, name, created_at FROM projects ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    counts = {
        "queues": conn.execute("SELECT COUNT(*) FROM queues").fetchone()[0],
        "work_items": conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0],
        "resources": conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0],
        "leases": conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0],
        "gates": conn.execute("SELECT COUNT(*) FROM gates").fetchone()[0],
        "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
    }
    pending = conn.execute(
        "SELECT COUNT(*) FROM work_items WHERE status = 'pending'"
    ).fetchone()[0]
    claimed = conn.execute(
        "SELECT COUNT(*) FROM work_items WHERE status = 'claimed'"
    ).fetchone()[0]
    return {
        "project": dict(project) if project else None,
        "counts": counts,
        "work": {"pending": pending, "claimed": claimed},
    }


def export_all(conn: sqlite3.Connection) -> dict[str, Any]:
    table_order = {
        "projects": "created_at",
        "queues": "name",
        "work_items": "rowid",
        "work_dependencies": "work_item_id",
        "resources": "id",
        "leases": "id",
        "gates": "id",
        "gate_actions": "created_at",
        "artifacts": "created_at",
        "policy_versions": "effective_at",
        "findings": "created_at",
        "finding_actions": "created_at",
        "finding_evidence": "finding_id",
        "events": "created_at",
        "idempotency_results": "key",
    }

    def rows(table: str) -> list[dict[str, Any]]:
        order_by = table_order[table]
        result = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
        return [dict(row) for row in result]

    return {table: rows(table) for table in table_order}
