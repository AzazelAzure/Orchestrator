"""Service-layer integration tests."""

from __future__ import annotations

import pytest

from flow_engine.application import (
    claim_resource,
    claim_work,
    complete_work,
    create_gate,
    fail_gate,
    fail_work,
    init_project,
    list_events,
    pass_gate,
    register_artifact,
    release_resource,
    retry_work,
    show_resource,
    submit_work,
    waive_gate,
)
from flow_engine.domain.errors import AdvisoryConflictError, ConflictError
from flow_engine.domain.states import ClaimPolicy
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def engine(tmp_path):
    kernel = Kernel.init(tmp_path / "state.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="test")
    yield kernel
    kernel.close()


def test_work_lifecycle(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={"task": "a"}, actor="agent-1")
        work_id = work["id"]

    with transaction(conn):
        claimed = claim_work(conn, work_id=work_id, actor="agent-1")
        assert claimed["status"] == "claimed"

    with transaction(conn):
        completed = complete_work(conn, work_id=work_id, actor="agent-1")
        assert completed["status"] == "complete"


def test_work_fail_and_retry(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="agent-1")
        work_id = work["id"]
        claim_work(conn, work_id=work_id, actor="agent-1")
        fail_work(conn, work_id=work_id, actor="agent-1", reason="boom")

    with transaction(conn):
        retried = retry_work(conn, work_id=work_id, actor="agent-1")
        assert retried["status"] == "pending"


def test_fifo_claim_from_queue(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        first = submit_work(conn, queue_name="default", payload={"n": 1}, actor="a")
        second = submit_work(conn, queue_name="default", payload={"n": 2}, actor="a")

    with transaction(conn):
        claimed = claim_work(conn, queue_name="default", actor="claimer")
        assert claimed["id"] == first["id"]

    with transaction(conn):
        claimed2 = claim_work(conn, queue_name="default", actor="claimer")
        assert claimed2["id"] == second["id"]


def test_idempotency_returns_cached_result(engine: Kernel) -> None:
    conn = engine.connection
    key = "submit-once"
    with transaction(conn):
        first = submit_work(
            conn,
            queue_name="default",
            payload={"x": 1},
            actor="agent",
            idempotency_key=key,
        )
    with transaction(conn):
        second = submit_work(
            conn,
            queue_name="default",
            payload={"x": 999},
            actor="agent",
            idempotency_key=key,
        )
    assert first["id"] == second["id"]
    assert second["from_cache"] is True


def test_resource_strict_conflict(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        claim_resource(
            conn,
            resource_id="vps",
            holder="agent-1",
            claim_policy=ClaimPolicy.STRICT,
        )

    with transaction(conn):
        with pytest.raises(ConflictError):
            claim_resource(
                conn,
                resource_id="vps",
                holder="agent-2",
                claim_policy=ClaimPolicy.STRICT,
            )


def test_resource_advisory_requires_force(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        claim_resource(
            conn,
            resource_id="ws1",
            holder="agent-1",
            claim_policy=ClaimPolicy.ADVISORY,
        )

    with transaction(conn):
        with pytest.raises(AdvisoryConflictError):
            claim_resource(
                conn,
                resource_id="ws1",
                holder="agent-2",
                claim_policy=ClaimPolicy.ADVISORY,
            )

    with transaction(conn):
        overridden = claim_resource(
            conn,
            resource_id="ws1",
            holder="agent-2",
            claim_policy=ClaimPolicy.ADVISORY,
            force=True,
            reason="maintenance",
        )
        assert overridden["lease"]["holder"] == "agent-2"


def test_resource_release(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        claim_resource(conn, resource_id="ws2", holder="agent-1")
        release_resource(conn, resource_id="ws2", holder="agent-1")

    released = show_resource(conn, "ws2")
    assert released["lease"] is None


def test_gate_lifecycle(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        gate = create_gate(conn, work_item_id=work["id"], gate_type="pre_execution", actor="a")

    with transaction(conn):
        passed = pass_gate(conn, gate_id=gate["id"], actor="a")
        assert passed["status"] == "passed"


def test_gate_fail_and_waive(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        gate = create_gate(conn, work_item_id=work["id"], gate_type="merge_ready", actor="a")
        failed = fail_gate(conn, gate_id=gate["id"], actor="a")
        assert failed["status"] == "failed"

    with transaction(conn):
        artifact = register_artifact(
            conn,
            uri="artifact://waiver/evidence-1",
            artifact_type="note",
            sensitivity="internal",
            retention_class="standard",
            created_by="a",
        )
        work2 = submit_work(conn, queue_name="default", payload={}, actor="a")
        gate2 = create_gate(conn, work_item_id=work2["id"], gate_type="deploy", actor="a")
        waived = waive_gate(
            conn,
            gate_id=gate2["id"],
            actor="a",
            authority="role:governance_reviewer",
            reason="approved exception",
            evidence_artifact_id=artifact["id"],
        )
        assert waived["status"] == "waived"


def test_mutations_emit_events(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        submit_work(conn, queue_name="default", payload={}, actor="a")

    events = list_events(conn, limit=10)
    assert any(event["event_type"] == "work.submitted" for event in events)
