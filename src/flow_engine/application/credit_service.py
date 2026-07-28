"""Credit reservation and settlement for R2 runs."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.domain.credits import (
    ACTIVE_ATTEMPT_STATUSES,
    ACTIVE_INVOCATION_STATUSES,
    ACTIVE_RUN_STATUSES,
    GLOBAL_PROVIDER_CONCURRENCY,
    PER_PROJECT_CONCURRENCY,
    PER_PROVIDER_CONCURRENCY,
    PER_RUN_CONCURRENCY,
)
from flow_engine.domain.errors import BudgetExhaustedError, ConflictError
from flow_engine.domain.models import new_id


def _sum_credits(
    conn: sqlite3.Connection,
    budget_scope_id: str,
    provider: str | None = None,
    invocation_id: str | None = None,
) -> dict[str, int]:
    query = """
        SELECT e.kind, e.provider, e.units
        FROM credit_entries e
        JOIN runtime_runs r ON r.id = e.run_id
        WHERE r.budget_scope_id = ?
    """
    params: list[Any] = [budget_scope_id]
    if provider is not None:
        query += " AND e.provider = ?"
        params.append(provider)
    if invocation_id is not None:
        query += " AND e.invocation_id = ?"
        params.append(invocation_id)
    reserved = 0
    settled = 0
    released = 0
    for row in conn.execute(query, params).fetchall():
        if row["kind"] == "reservation":
            reserved += int(row["units"])
        elif row["kind"] == "settlement":
            settled += int(row["units"])
        elif row["kind"] == "release":
            released += int(row["units"])
    return {
        "reserved": reserved,
        "settled": settled,
        "released": released,
        "consumed": settled,
        "open_reservations": reserved - settled - released,
        "net_spent": settled,
    }


def credit_usage(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    run = conn.execute(
        """SELECT budget_scope_id, credit_budget_total, credit_budget_per_provider
           FROM runtime_runs WHERE id = ?""",
        (run_id,),
    ).fetchone()
    if run is None:
        raise ConflictError(f"run not found for credit usage: {run_id}")
    budget_scope_id = run["budget_scope_id"]
    totals = _sum_credits(conn, budget_scope_id)
    by_provider: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        """SELECT DISTINCT e.provider FROM credit_entries e
           JOIN runtime_runs r ON r.id = e.run_id
           WHERE r.budget_scope_id = ?""",
        (budget_scope_id,),
    ).fetchall():
        by_provider[row["provider"]] = _sum_credits(
            conn, budget_scope_id, row["provider"]
        )
    return {
        "budget_total": run["credit_budget_total"],
        "budget_per_provider": run["credit_budget_per_provider"],
        "budget_scope_id": budget_scope_id,
        "totals": totals,
        "by_provider": by_provider,
    }


def _count_active_invocations(
    conn: sqlite3.Connection,
    *,
    provider: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
) -> int:
    placeholders = ",".join("?" for _ in ACTIVE_INVOCATION_STATUSES)
    query = f"""
        SELECT COUNT(*) AS n
        FROM provider_invocations i
        JOIN runtime_runs r ON r.id = i.run_id
        WHERE i.status IN ({placeholders})
    """
    params: list[Any] = list(ACTIVE_INVOCATION_STATUSES)
    if provider is not None:
        query += " AND i.provider = ?"
        params.append(provider)
    if project_id is not None:
        query += " AND r.project_id = ?"
        params.append(project_id)
    if run_id is not None:
        query += " AND i.run_id = ?"
        params.append(run_id)
    return int(conn.execute(query, params).fetchone()["n"])


def assert_concurrency_available(
    conn: sqlite3.Connection,
    *,
    provider: str,
    project_id: str,
    run_id: str,
) -> None:
    if _count_active_invocations(conn) >= GLOBAL_PROVIDER_CONCURRENCY:
        raise BudgetExhaustedError("global provider concurrency exhausted")
    if _count_active_invocations(conn, provider=provider) >= PER_PROVIDER_CONCURRENCY:
        raise BudgetExhaustedError(f"per-provider concurrency exhausted for {provider}")
    if (
        _count_active_invocations(conn, project_id=project_id)
        >= PER_PROJECT_CONCURRENCY
    ):
        raise BudgetExhaustedError("per-project concurrency exhausted")
    if _count_active_invocations(conn, run_id=run_id) >= PER_RUN_CONCURRENCY:
        raise BudgetExhaustedError("per-run concurrency exhausted")


def assert_credit_available(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    provider: str,
    units: int = 1,
) -> None:
    usage = credit_usage(conn, run_id)
    totals = usage["totals"]
    if totals["consumed"] + totals["open_reservations"] + units > usage["budget_total"]:
        raise BudgetExhaustedError("acceptance credit total exhausted")
    provider_usage = usage["by_provider"].get(
        provider, {"consumed": 0, "open_reservations": 0}
    )
    if (
        provider_usage["consumed"] + provider_usage["open_reservations"] + units
        > usage["budget_per_provider"]
    ):
        raise BudgetExhaustedError(f"per-provider credit exhausted for {provider}")


def reserve_credit(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    provider: str,
    attempt_id: str,
    invocation_id: str,
    units: int = 1,
) -> dict[str, Any]:
    assert_credit_available(conn, run_id=run_id, provider=provider, units=units)
    entry_id = new_id()
    created_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO credit_entries (
            id, run_id, provider, kind, units, attempt_id, invocation_id, created_at
        ) VALUES (?, ?, ?, 'reservation', ?, ?, ?, ?)
        """,
        (entry_id, run_id, provider, units, attempt_id, invocation_id, created_at),
    )
    return {
        "id": entry_id,
        "run_id": run_id,
        "provider": provider,
        "kind": "reservation",
        "units": units,
        "attempt_id": attempt_id,
        "invocation_id": invocation_id,
        "created_at": created_at,
    }


def settle_credit(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    provider: str,
    attempt_id: str,
    invocation_id: str,
    units: int = 1,
) -> dict[str, Any]:
    """Consume a reserved credit (terminal or outcome_unknown)."""
    scope = credit_usage(conn, run_id)["budget_scope_id"]
    open_res = _sum_credits(
        conn, scope, provider, invocation_id=invocation_id
    )["open_reservations"]
    if open_res < units:
        raise BudgetExhaustedError("no open reservation to settle")
    entry_id = new_id()
    created_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO credit_entries (
            id, run_id, provider, kind, units, attempt_id, invocation_id, created_at
        ) VALUES (?, ?, ?, 'settlement', ?, ?, ?, ?)
        """,
        (entry_id, run_id, provider, units, attempt_id, invocation_id, created_at),
    )
    return {
        "id": entry_id,
        "kind": "settlement",
        "units": units,
        "invocation_id": invocation_id,
        "created_at": created_at,
    }


def release_credit(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    provider: str,
    attempt_id: str,
    invocation_id: str,
    units: int = 1,
) -> dict[str, Any]:
    """Release reservation without consumption (pre-dispatch abort only)."""
    scope = credit_usage(conn, run_id)["budget_scope_id"]
    open_res = _sum_credits(
        conn, scope, provider, invocation_id=invocation_id
    )["open_reservations"]
    if open_res < units:
        raise BudgetExhaustedError("no open reservation to release")
    entry_id = new_id()
    created_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO credit_entries (
            id, run_id, provider, kind, units, attempt_id, invocation_id, created_at
        ) VALUES (?, ?, ?, 'release', ?, ?, ?, ?)
        """,
        (entry_id, run_id, provider, units, attempt_id, invocation_id, created_at),
    )
    return {
        "id": entry_id,
        "kind": "release",
        "units": units,
        "invocation_id": invocation_id,
        "created_at": created_at,
    }


def count_active_attempts_for_run(conn: sqlite3.Connection, run_id: str) -> int:
    placeholders = ",".join("?" for _ in ACTIVE_ATTEMPT_STATUSES)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM runtime_attempts
        WHERE run_id = ? AND status IN ({placeholders})
        """,
        (run_id, *ACTIVE_ATTEMPT_STATUSES),
    ).fetchone()
    return int(row["n"])


def count_active_runs(conn: sqlite3.Connection) -> int:
    placeholders = ",".join("?" for _ in ACTIVE_RUN_STATUSES)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM runtime_runs WHERE status IN ({placeholders})",
        tuple(ACTIVE_RUN_STATUSES),
    ).fetchone()
    return int(row["n"])
