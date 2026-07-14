"""SQLite connection management with WAL mode and kernel initialization."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from flow_engine.persistence.migrations import (
    KERNEL_TABLES,
    apply_migrations,
    current_version,
    list_tables,
)

DEFAULT_BUSY_TIMEOUT_MS = 5000


def configure_connection(conn: sqlite3.Connection) -> None:
    """Apply required pragmas for concurrent CLI access."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={DEFAULT_BUSY_TIMEOUT_MS}")


def open_connection(
    db_path: Path | str,
    *,
    initialize: bool = False,
) -> sqlite3.Connection:
    """Open a SQLite connection with kernel pragmas.

    When *initialize* is True the parent directory is created (if needed) and
  pending schema migrations are applied.
    """
    path = Path(db_path)
    if initialize:
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    configure_connection(conn)

    if initialize:
        apply_migrations(conn)

    return conn


@dataclass(frozen=True)
class Kernel:
    """Handle for an initialized flow-engine SQLite database."""

    db_path: Path
    connection: sqlite3.Connection

    @classmethod
    def init(cls, db_path: Path | str) -> Kernel:
        """Create (or open) a database and apply schema migrations."""
        path = Path(db_path)
        conn = open_connection(path, initialize=True)
        return cls(db_path=path, connection=conn)

    @classmethod
    def open(cls, db_path: Path | str) -> Kernel:
        """Open an existing initialized database."""
        path = Path(db_path)
        conn = open_connection(path, initialize=False)
        return cls(db_path=path, connection=conn)

    @property
    def schema_version(self) -> int:
        return current_version(self.connection)

    @property
    def tables(self) -> list[str]:
        return list_tables(self.connection)

    def journal_mode(self) -> str:
        row = self.connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0])

    def foreign_keys_enabled(self) -> bool:
        row = self.connection.execute("PRAGMA foreign_keys").fetchone()
        return bool(row[0])

    def busy_timeout_ms(self) -> int:
        row = self.connection.execute("PRAGMA busy_timeout").fetchone()
        return int(row[0])

    def has_kernel_tables(self) -> bool:
        present = set(self.tables)
        return all(table in present for table in KERNEL_TABLES)

    def close(self) -> None:
        self.connection.close()
