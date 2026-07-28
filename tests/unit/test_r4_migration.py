"""R4 migration 005 and legacy queue preservation."""

from __future__ import annotations

from flow_engine.application import claim_work, ensure_queue, init_project
from flow_engine.application.clock import utc_now_iso
from flow_engine.control_plane.bootstrap import bootstrap_test_principals
from flow_engine.persistence import Kernel
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.migrations import _load_sql, current_version, list_tables
from flow_engine.persistence.transactions import transaction


def test_migration_005_adds_control_plane_tables(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "r4.db")
    try:
        assert current_version(kernel.connection) == 7
        tables = set(list_tables(kernel.connection))
        assert "control_plane_principals" in tables
        assert "control_plane_delivery_jobs" in tables
        assert "script_executions" in tables
        assert "schedule_runs" in tables
    finally:
        kernel.close()


def test_migration_preserves_legacy_pending_claim_r4(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = open_connection(db_path, initialize=False)
    try:
        conn.executescript(_load_sql("001_initial_schema.sql"))
        conn.executescript(_load_sql("002_governance_invariants.sql"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?), (2, ?)",
            (utc_now_iso(), utc_now_iso()),
        )
        conn.commit()
        with transaction(conn):
            init_project(conn, name="legacy")
            ensure_queue(conn, name="default")
            from flow_engine.domain.models import new_id

            wid = new_id()
            conn.execute(
                """
                INSERT INTO work_items (id, queue_id, status, payload_json, revision)
                SELECT ?, q.id, 'pending', '{}', 0
                FROM queues q
                WHERE q.name = 'default'
                """,
                (wid,),
            )
        conn.commit()
        from flow_engine.persistence.migrations import apply_migrations

        apply_migrations(conn)
        assert current_version(conn) == 7
        row = conn.execute("SELECT status FROM work_items WHERE id = ?", (wid,)).fetchone()
        assert row["status"] == "pending"
        with transaction(conn):
            claimed = claim_work(conn, actor="legacy-r4-agent", work_id=wid)
        assert claimed["status"] == "claimed"
    finally:
        conn.close()


def test_bootstrap_registers_base_and_lane_principals(tmp_path) -> None:
    kernel = Kernel.init(tmp_path / "boot.db")
    try:
        with transaction(kernel.connection):
            created = bootstrap_test_principals(kernel.connection)
        # 8 base/provider-worker principals + 5 lane-scoped MCP principals
        assert len(created) == 13
        count = kernel.connection.execute(
            "SELECT COUNT(*) AS n FROM control_plane_principals"
        ).fetchone()["n"]
        assert count == 13
        kinds = {
            r["kind"]
            for r in kernel.connection.execute(
                "SELECT kind FROM control_plane_principals"
            ).fetchall()
        }
        assert kinds == {
            "founder",
            "scheduler",
            "mcp_service",
            "worker",
            "provider_invocation",
        }
        lane_keys = {
            r["principal_key"]
            for r in kernel.connection.execute(
                "SELECT principal_key FROM control_plane_principals WHERE kind = 'mcp_service'"
            ).fetchall()
        }
        assert "mcp-service" in lane_keys
        assert "mcp.lane.workflow-control" in lane_keys
        assert len(lane_keys) == 6
    finally:
        kernel.close()
