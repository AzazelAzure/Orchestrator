"""Concurrency tests for compare-and-set claim semantics."""

from __future__ import annotations

import threading
from pathlib import Path

from flow_engine.application import claim_work, init_project, submit_work
from flow_engine.domain.errors import ConflictError
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.transactions import transaction


def _setup_db(db_path: Path) -> str:
    conn = open_connection(db_path, initialize=True)
    try:
        with transaction(conn):
            init_project(conn, name="contention")
            work = submit_work(conn, queue_name="default", payload={"task": "race"}, actor="setup")
            return work["id"]
    finally:
        conn.close()


def test_parallel_work_claim_single_winner(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    work_id = _setup_db(db_path)

    results: list[tuple[str, str | None]] = []
    lock = threading.Lock()

    def claimant(name: str) -> None:
        conn = open_connection(db_path)
        try:
            with transaction(conn):
                claimed = claim_work(conn, work_id=work_id, actor=name)
                with lock:
                    results.append(("ok", claimed["claimed_by"]))
        except ConflictError:
            with lock:
                results.append(("conflict", None))
        finally:
            conn.close()

    threads = [threading.Thread(target=claimant, args=(f"agent-{i}",)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [result for result in results if result[0] == "ok"]
    conflicts = [result for result in results if result[0] == "conflict"]

    assert len(successes) == 1
    assert len(conflicts) == 9

    conn = open_connection(db_path)
    try:
        row = conn.execute(
            "SELECT status, claimed_by FROM work_items WHERE id = ?",
            (work_id,),
        ).fetchone()
        assert row["status"] == "claimed"
        assert row["claimed_by"] == successes[0][1]
    finally:
        conn.close()


def test_parallel_fifo_claims_are_unique(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = open_connection(db_path, initialize=True)
    try:
        with transaction(conn):
            init_project(conn, name="fifo")
            for index in range(5):
                submit_work(conn, queue_name="default", payload={"n": index}, actor="setup")
    finally:
        conn.close()

    claimed_ids: list[str] = []
    ids_lock = threading.Lock()

    def pop() -> None:
        for _ in range(10):
            local = open_connection(db_path)
            try:
                with transaction(local):
                    item = claim_work(local, queue_name="default", actor="popper")
                    with ids_lock:
                        claimed_ids.append(item["id"])
                    return
            except ConflictError:
                continue
            except Exception:
                return
            finally:
                local.close()

    threads = [threading.Thread(target=pop) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(claimed_ids) == 5
    assert len(set(claimed_ids)) == 5
