"""Shared CLI context and database path resolution."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.migrations import current_version

DEFAULT_DB_PATH = Path(".flow/state.db")


def resolve_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    env_path = os.environ.get("FLOW_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


@contextmanager
def db_session(db_path: Path, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
    conn = open_connection(db_path, initialize=initialize)
    try:
        yield conn
    finally:
        conn.close()


def require_initialized(conn: sqlite3.Connection) -> None:
    if current_version(conn) == 0:
        raise RuntimeError("database not initialized; run `flowctl init` first")


def commit(conn: sqlite3.Connection) -> None:
    conn.commit()
