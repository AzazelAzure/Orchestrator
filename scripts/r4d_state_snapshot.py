#!/usr/bin/env python3
"""Authoritative SQLite snapshots from the coordinator container (sole writer).

Reads a JSON spec from stdin; prints one JSON object. No secrets.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

for candidate in (Path("/app/src"), Path(__file__).resolve().parents[1] / "src"):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
        break

from flow_engine.persistence import Kernel  # noqa: E402


def _delivery_job(conn, job_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, status, redelivery_count, run_id, attempt_id, invocation_id,
               worker_principal_id, celery_task_id, idempotency_key
        FROM control_plane_delivery_jobs WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


def _run_bundle(conn, run_id: str) -> dict[str, Any] | None:
    run = conn.execute(
        """
        SELECT id, work_item_id, status, revision, provider
        FROM runtime_runs WHERE id = ?
        """,
        (run_id,),
    ).fetchone()
    if run is None:
        return None
    attempts = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, status, revision, attempt_number
            FROM runtime_attempts WHERE run_id = ? ORDER BY attempt_number
            """,
            (run_id,),
        ).fetchall()
    ]
    invocations = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, status, attempt_id
            FROM provider_invocations WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
    ]
    delivery_jobs = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, status, redelivery_count, idempotency_key
            FROM control_plane_delivery_jobs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchall()
    ]
    terminal_runs = conn.execute(
        """
        SELECT COUNT(*) AS n FROM runtime_runs
        WHERE work_item_id = ? AND status IN ('complete', 'failed', 'outcome_unknown', 'cancelled')
        """,
        (run["work_item_id"],),
    ).fetchone()["n"]
    return {
        "run": dict(run),
        "attempts": attempts,
        "invocations": invocations,
        "delivery_jobs": delivery_jobs,
        "terminal_run_count_for_work_item": terminal_runs,
        "invocation_count": len(invocations),
    }


def _work_item(conn, work_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, status, revision, claimed_by
        FROM work_items WHERE id = ?
        """,
        (work_id,),
    ).fetchone()
    return dict(row) if row else None


def _restart_continuity(
    conn,
    *,
    work_item_ids: list[str],
    run_ids: list[str],
) -> dict[str, Any]:
    work_items = {wid: _work_item(conn, wid) for wid in work_item_ids}
    runs = {rid: _run_bundle(conn, rid) for rid in run_ids}
    return {
        "work_items": work_items,
        "runs": runs,
        "sqlite_user_version": conn.execute("PRAGMA user_version").fetchone()[0],
    }


def main() -> int:
    spec = json.load(sys.stdin)
    db_path = Path(os.environ.get("FLOW_DB_PATH", "/data/state.db"))
    kernel = Kernel.init(db_path)
    try:
        out: dict[str, Any] = {}
        for query in spec.get("queries", []):
            qtype = query["type"]
            if qtype == "delivery_job":
                out.setdefault("delivery_jobs", {})[query["id"]] = _delivery_job(
                    kernel.connection, query["id"]
                )
            elif qtype == "run_bundle":
                out.setdefault("run_bundles", {})[query["run_id"]] = _run_bundle(
                    kernel.connection, query["run_id"]
                )
            elif qtype == "work_item":
                out.setdefault("work_items", {})[query["id"]] = _work_item(
                    kernel.connection, query["id"]
                )
            elif qtype == "restart_continuity":
                out["restart_continuity"] = _restart_continuity(
                    kernel.connection,
                    work_item_ids=query.get("work_item_ids", []),
                    run_ids=query.get("run_ids", []),
                )
            else:
                raise SystemExit(f"unknown query type: {qtype}")
        print(json.dumps(out, sort_keys=True))
        return 0
    finally:
        kernel.close()


if __name__ == "__main__":
    raise SystemExit(main())
