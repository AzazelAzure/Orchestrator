"""Tests for the SQLite kernel initialization and schema contract."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from flow_engine.domain import ClaimPolicy, LeaseMode, WorkItemStatus, new_id
from flow_engine.persistence import Kernel
from flow_engine.persistence.connection import DEFAULT_BUSY_TIMEOUT_MS, open_connection
from flow_engine.persistence.migrations import apply_migrations
from flow_engine.persistence.transactions import transaction


def test_kernel_init_creates_all_tables(kernel_db: Kernel) -> None:
    assert kernel_db.has_kernel_tables()
    assert "schema_migrations" in kernel_db.tables


def test_kernel_uses_wal_mode(kernel_db: Kernel) -> None:
    assert kernel_db.journal_mode().lower() == "wal"


def test_kernel_enables_foreign_keys(kernel_db: Kernel) -> None:
    assert kernel_db.foreign_keys_enabled()


def test_kernel_sets_busy_timeout(kernel_db: Kernel) -> None:
    assert kernel_db.busy_timeout_ms() == DEFAULT_BUSY_TIMEOUT_MS


def test_migrations_are_idempotent(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    kernel = Kernel.init(db_path)
    version_after_first = kernel.schema_version
    applied = apply_migrations(kernel.connection)
    assert applied == 0
    assert kernel.schema_version == version_after_first
    kernel.close()


def test_events_table_is_append_only(kernel_db: Kernel) -> None:
    conn = kernel_db.connection
    event_id = new_id()
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO events (id, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_id, "test.created", "pytest", "{}", now),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE events SET actor = 'mutator' WHERE id = ?",
            (event_id,),
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


def test_schema_enforces_claim_policy_check(kernel_db: Kernel) -> None:
    conn = kernel_db.connection
    resource_id = new_id()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO resources (id, kind, claim_policy, revision) VALUES (?, ?, ?, ?)",
            (resource_id, "workspace", "shared", 0),
        )


def test_schema_enforces_lease_mode_check(kernel_db: Kernel) -> None:
    conn = kernel_db.connection
    resource_id = new_id()
    conn.execute(
        "INSERT INTO resources (id, kind, claim_policy, revision) VALUES (?, ?, ?, ?)",
        (resource_id, "workspace", ClaimPolicy.ADVISORY, 0),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO leases (id, resource_id, holder, mode) VALUES (?, ?, ?, ?)",
            (new_id(), resource_id, "agent-1", "shared"),
        )


def test_foreign_keys_reject_orphan_work_item(kernel_db: Kernel) -> None:
    conn = kernel_db.connection
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO work_items (id, queue_id, status, payload_json, revision) VALUES (?, ?, ?, ?, ?)",
            (new_id(), new_id(), WorkItemStatus.PENDING, "{}", 0),
        )


def test_end_to_end_row_insertion(kernel_db: Kernel) -> None:
    conn = kernel_db.connection
    project_id = new_id()
    queue_id = new_id()
    work_id = new_id()
    resource_id = new_id()
    lease_id = new_id()
    gate_id = new_id()
    event_id = new_id()
    now = datetime.now(UTC).isoformat()

    with transaction(conn):
        conn.execute(
            "INSERT INTO projects (id, name, created_at) VALUES (?, ?, ?)",
            (project_id, "demo", now),
        )
        conn.execute(
            "INSERT INTO queues (id, project_id, name) VALUES (?, ?, ?)",
            (queue_id, project_id, "default"),
        )
        conn.execute(
            "INSERT INTO work_items (id, queue_id, status, payload_json, revision) VALUES (?, ?, ?, ?, ?)",
            (work_id, queue_id, WorkItemStatus.PENDING, '{"task": "demo"}', 0),
        )
        conn.execute(
            "INSERT INTO resources (id, kind, claim_policy, revision) VALUES (?, ?, ?, ?)",
            (resource_id, "workspace", ClaimPolicy.STRICT, 0),
        )
        conn.execute(
            "INSERT INTO leases (id, resource_id, holder, mode) VALUES (?, ?, ?, ?)",
            (lease_id, resource_id, "agent-1", LeaseMode.EXCLUSIVE),
        )
        conn.execute(
            "INSERT INTO gates (id, work_item_id, gate_type, status) VALUES (?, ?, ?, ?)",
            (gate_id, work_id, "pre_execution", "open"),
        )
        conn.execute(
            "INSERT INTO events (id, event_type, actor, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (event_id, "work.submitted", "agent-1", "{}", now),
        )
        conn.execute(
            "INSERT INTO idempotency_results (key, result_json) VALUES (?, ?)",
            ("idem-1", '{"ok": true}'),
        )

    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM work_items").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 1


def test_open_connection_without_initialize_does_not_create_schema(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = open_connection(db_path, initialize=False)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert tables == []
    finally:
        conn.close()


def test_new_id_is_26_char_ulid() -> None:
    identifier = new_id()
    assert len(identifier) == 26
    assert all(ch in "0123456789ABCDEFGHJKMNPQRSTVWXYZ" for ch in identifier)
