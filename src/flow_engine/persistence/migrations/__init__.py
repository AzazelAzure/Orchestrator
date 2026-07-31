"""Schema migration runner."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from importlib import resources

MIGRATIONS_PACKAGE = "flow_engine.persistence.migrations"

KERNEL_TABLES = (
    "projects",
    "queues",
    "work_items",
    "work_dependencies",
    "resources",
    "leases",
    "gates",
    "gate_actions",
    "events",
    "idempotency_results",
    "artifacts",
    "policy_versions",
    "findings",
    "finding_actions",
    "finding_evidence",
    "runtime_runs",
    "runtime_attempts",
    "provider_invocations",
    "provider_runner_events",
    "runtime_commands",
    "credit_entries",
    "audit_events",
    "reconciliation_evidence",
    "organization_profiles",
    "departments",
    "hierarchy_layers",
    "positions",
    "actors",
    "provider_seats",
    "authority_ceilings",
    "assignments",
    "delegation_requests",
    "delegation_dispositions",
    "handoffs",
    "resolved_loadout_snapshots",
    "task_grants",
    "immutable_dispatch_pins",
    "child_closure_evidence",
    "control_plane_principals",
    "control_plane_delivery_jobs",
    "control_plane_user_accounts",
    "control_plane_credentials",
    "control_plane_auth_throttle",
    "script_executions",
    "schedule_runs",
)


def _migration_files() -> list[tuple[int, str]]:
    """Return sorted (version, filename) pairs for bundled SQL migrations."""
    root = resources.files(MIGRATIONS_PACKAGE)
    migrations: list[tuple[int, str]] = []
    for entry in root.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        version = int(entry.name.split("_", 1)[0])
        migrations.append((version, entry.name))
    return sorted(migrations, key=lambda item: item[0])


def _load_sql(filename: str) -> str:
    return resources.files(MIGRATIONS_PACKAGE).joinpath(filename).read_text(encoding="utf-8")


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied migration version, or 0 if none."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
    return int(row[0])


def apply_migrations(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the number of migrations applied."""
    applied = 0
    for version, filename in _migration_files():
        if version <= current_version(conn):
            continue
        sql = _load_sql(filename)
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(UTC).isoformat()),
        )
        applied += 1
    conn.commit()
    return applied


def list_tables(conn: sqlite3.Connection) -> list[str]:
    """Return user table names (excludes sqlite internal tables)."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]
