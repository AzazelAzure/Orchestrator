"""Idempotency key handling for agent-safe retries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def run_idempotent(
    conn: sqlite3.Connection,
    key: str | None,
    operation: Callable[[], T],
) -> tuple[T, bool]:
    """Run *operation* once per idempotency key.

    Returns ``(result, from_cache)``. When *key* is None the operation always
    runs and nothing is stored.
    """
    if not key:
        return operation(), False

    row = conn.execute(
        "SELECT result_json FROM idempotency_results WHERE key = ?",
        (key,),
    ).fetchone()
    if row is not None:
        return json.loads(row[0]), True

    result = operation()
    conn.execute(
        "INSERT INTO idempotency_results (key, result_json) VALUES (?, ?)",
        (key, json.dumps(result)),
    )
    return result, False
