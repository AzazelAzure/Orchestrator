"""Resource lease contention tests."""

from __future__ import annotations

import threading

from flow_engine.application import claim_resource, init_project
from flow_engine.domain.errors import AdvisoryConflictError, ConflictError
from flow_engine.domain.states import ClaimPolicy
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.transactions import transaction


def test_parallel_strict_resource_claim_single_winner(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = open_connection(db_path, initialize=True)
    try:
        with transaction(conn):
            init_project(conn, name="resource-race")
    finally:
        conn.close()

    results: list[str] = []
    lock = threading.Lock()

    def claimant(name: str) -> None:
        local = open_connection(db_path)
        try:
            with transaction(local):
                claim_resource(
                    local,
                    resource_id="vps",
                    holder=name,
                    claim_policy=ClaimPolicy.STRICT,
                )
                with lock:
                    results.append("ok")
        except ConflictError:
            with lock:
                results.append("conflict")
        finally:
            local.close()

    threads = [threading.Thread(target=claimant, args=(f"agent-{i}",)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("ok") == 1
    assert results.count("conflict") == 9


def test_advisory_resource_blocks_without_force(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    conn = open_connection(db_path, initialize=True)
    try:
        with transaction(conn):
            init_project(conn, name="advisory")
            claim_resource(
                conn,
                resource_id="ws1",
                holder="agent-1",
                claim_policy=ClaimPolicy.ADVISORY,
            )
    finally:
        conn.close()

    local = open_connection(db_path)
    try:
        with transaction(local):
            try:
                claim_resource(
                    local,
                    resource_id="ws1",
                    holder="agent-2",
                    claim_policy=ClaimPolicy.ADVISORY,
                )
                raise AssertionError("expected AdvisoryConflictError")
            except AdvisoryConflictError:
                pass
    finally:
        local.close()

    verify = open_connection(db_path)
    try:
        row = verify.execute(
            "SELECT holder FROM leases WHERE resource_id = 'ws1'"
        ).fetchone()
        assert row["holder"] == "agent-1"
    finally:
        verify.close()
