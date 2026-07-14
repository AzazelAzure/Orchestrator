"""Transaction helpers for atomic state changes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside a single commit/rollback transaction."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
