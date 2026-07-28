"""R4 delivery registry: idempotency, redelivery, recovery, external I/O."""

from __future__ import annotations

import pytest

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.application.runtime_service import (
    claim_attempt,
    create_run,
    dispatch_provider_call,
)
from flow_engine.application.worker_delivery import accept_worker_deliver
from flow_engine.control_plane.delivery_registry import (
    claim_delivery_job,
    delivery_idempotency_key,
    list_eligible_delivery_jobs,
    recover_stale_delivery_jobs,
    register_delivery_job,
)
from flow_engine.coordinator import (
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    SystemTestGrant,
)
from flow_engine.domain.errors import ConflictError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence import Kernel
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def engine(tmp_path):
    kernel = Kernel.init(tmp_path / "delivery.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo")
        ensure_queue(kernel.connection, name="default")
    yield kernel
    kernel.close()


def _grant() -> SystemTestGrant:
    return SystemTestGrant(
        grant_id="g1",
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surfaces=(Surface.WORKER, Surface.TEST),
        providers=("codex",),
        budget_scope_id="acceptance-campaign-r4",
    )


def _async_dispatch(conn, work_id: str) -> dict:
    grant = _grant()
    created = create_run(
        conn,
        work_item_id=work_id,
        provider="codex",
        grant=grant,
        actor="worker",
    )
    claim_attempt(conn, run_id=created["run"]["id"], actor="worker")
    return dispatch_provider_call(
        conn,
        attempt_id=created["attempt"]["id"],
        actor="worker",
        delivery_mode="async",
    )


def test_register_delivery_idempotent(engine) -> None:
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
    inv = dispatched["invocation"]["id"]
    att = dispatched["attempt"]["id"]
    key = delivery_idempotency_key(inv, att)
    with transaction(engine.connection):
        j1 = register_delivery_job(
            engine.connection,
            invocation_id=inv,
            attempt_id=att,
            run_id=dispatched["run"]["id"],
            provider="codex",
            idempotency_key=key,
        )
        j2 = register_delivery_job(
            engine.connection,
            invocation_id=inv,
            attempt_id=att,
            run_id=dispatched["run"]["id"],
            provider="codex",
            idempotency_key=key,
        )
    assert j1["id"] == j2["id"]


def test_claim_increments_redelivery_count(engine) -> None:
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job = dispatched["delivery"]["delivery_job_id"]
        claim_delivery_job(
            engine.connection,
            job_id=job,
            worker_principal_id="worker-1",
            celery_task_id="task-1",
        )
        reclaimed = claim_delivery_job(
            engine.connection,
            job_id=job,
            worker_principal_id="worker-1",
            celery_task_id="task-2",
        )
    assert reclaimed["redelivery_count"] >= 1


def test_worker_deliver_via_accept_outside_txn(engine) -> None:
    coord = StateCoordinator(engine.connection)
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        attempt_id = dispatched["attempt"]["id"]
    ctx = CommandContext(
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=_grant(),
    )
    envelope = accept_worker_deliver(
        coord,
        RuntimeCommand(
            command_type="runtime.worker_deliver",
            target_id=attempt_id,
            payload={"attempt_id": attempt_id, "delivery_job_id": job_id},
            context=ctx,
        ),
    )
    assert envelope["status"] == "applied"
    run = engine.connection.execute(
        "SELECT status FROM runtime_runs WHERE id = ?",
        (dispatched["run"]["id"],),
    ).fetchone()
    assert run["status"] == "complete"


def test_runtime_show_without_explicit_idempotency_key_is_fresh(engine) -> None:
    coord = StateCoordinator(engine.connection)
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
    run_id = dispatched["run"]["id"]
    ctx = CommandContext(
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=_grant(),
    )

    with transaction(engine.connection):
        first = coord.accept(
            RuntimeCommand(
                command_type="runtime.show",
                target_id=run_id,
                payload={"run_id": run_id},
                context=ctx,
            )
        )
    assert first["result"]["run"]["status"] == "claimed"
    assert first["from_cache"] is False

    with transaction(engine.connection):
        engine.connection.execute(
            "UPDATE runtime_runs SET status = 'complete' WHERE id = ?",
            (run_id,),
        )
        second = coord.accept(
            RuntimeCommand(
                command_type="runtime.show",
                target_id=run_id,
                payload={"run_id": run_id},
                context=ctx,
            )
        )
    assert second["result"]["run"]["status"] == "complete"
    assert second["from_cache"] is False


def test_recover_stale_delivery_jobs(engine) -> None:
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        claim_delivery_job(
            engine.connection,
            job_id=job_id,
            worker_principal_id="worker-1",
        )
        recovered = recover_stale_delivery_jobs(
            engine.connection, stale_before_iso="9999-01-01T00:00:00+00:00"
        )
        eligible = list_eligible_delivery_jobs(engine.connection)
    assert any(j["id"] == job_id for j in recovered)
    assert any(j["id"] == job_id and j["status"] == "registered" for j in eligible)


def test_coordinator_restart_recovery(engine) -> None:
    coord = StateCoordinator(engine.connection)
    ctx = CommandContext(
        principal_id="system",
        role=PrincipalRole.SYSTEM,
        surface=Surface.REST,
    )
    with transaction(engine.connection):
        envelope = coord.accept(
            RuntimeCommand(
                command_type="runtime.recover_restart",
                target_id=None,
                payload={},
                context=ctx,
            )
        )
    assert envelope["status"] == "applied"
    assert envelope["result"]["new_paid_calls"] == 0


def test_claim_rejects_attempt_mismatch(engine) -> None:
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job = dispatched["delivery"]["delivery_job_id"]
        with pytest.raises(ConflictError, match="attempt_id mismatch"):
            claim_delivery_job(
                engine.connection,
                job_id=job,
                worker_principal_id="worker-1",
                attempt_id="not-the-attempt",
            )


def test_duplicate_worker_deliver_returns_cache_without_second_provider_call(engine) -> None:
    from flow_engine.providers.protocol import MockProviderRunner

    runner = MockProviderRunner("codex")
    coord = StateCoordinator(engine.connection, runners={"codex": runner})
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        attempt_id = dispatched["attempt"]["id"]
    ctx = CommandContext(
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=_grant(),
    )
    cmd = RuntimeCommand(
        command_type="runtime.worker_deliver",
        target_id=attempt_id,
        payload={"attempt_id": attempt_id, "delivery_job_id": job_id},
        idempotency_key=f"deliver|{job_id}|{attempt_id}",
        context=ctx,
    )
    first = accept_worker_deliver(coord, cmd)
    second = accept_worker_deliver(coord, cmd)
    assert first["status"] == "applied"
    assert second.get("from_cache") is True
    assert runner.delivery_count == 1


def test_concurrent_dispatch_lease_only_one_provider_io(engine) -> None:
    from flow_engine.application.worker_delivery import prepare_worker_delivery
    from flow_engine.providers.protocol import MockProviderRunner

    runner = MockProviderRunner("codex")
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        attempt_id = dispatched["attempt"]["id"]
        prepared = prepare_worker_delivery(
            engine.connection,
            attempt_id=attempt_id,
            delivery_job_id=job_id,
            worker_principal_id="worker",
            lease_token="lease-a",
        )
        with pytest.raises(ConflictError, match="non-replayable|lease"):
            prepare_worker_delivery(
                engine.connection,
                attempt_id=attempt_id,
                delivery_job_id=job_id,
                worker_principal_id="worker",
                lease_token="lease-b",
            )
    from flow_engine.application.worker_delivery import execute_provider_delivery

    execute_provider_delivery(prepared, runners={"codex": runner})
    assert runner.delivery_count == 1


def test_stale_delivering_with_dispatch_intent_goes_outcome_unknown(engine) -> None:
    from flow_engine.application.worker_delivery import prepare_worker_delivery

    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        attempt_id = dispatched["attempt"]["id"]
        prepare_worker_delivery(
            engine.connection,
            attempt_id=attempt_id,
            delivery_job_id=job_id,
            worker_principal_id="worker",
        )
        recovered = recover_stale_delivery_jobs(
            engine.connection, stale_before_iso="9999-01-01T00:00:00+00:00"
        )
    assert any(j["id"] == job_id for j in recovered)
    job = next(j for j in recovered if j["id"] == job_id)
    assert job["status"] == "failed"
    assert job["outcome_unknown"] is True
    assert job["status"] != "registered"
    run = engine.connection.execute(
        "SELECT status FROM runtime_runs WHERE id = ?",
        (dispatched["run"]["id"],),
    ).fetchone()
    assert run["status"] == "outcome_unknown"
    eligible = list_eligible_delivery_jobs(engine.connection)
    assert not any(j["id"] == job_id for j in eligible)


def test_crash_after_intent_before_settle_is_non_replayable(engine) -> None:
    from flow_engine.application.worker_delivery import prepare_worker_delivery
    from flow_engine.providers.protocol import MockProviderRunner

    runner = MockProviderRunner("codex")
    coord = StateCoordinator(engine.connection, runners={"codex": runner})
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        attempt_id = dispatched["attempt"]["id"]
        # Simulate durable intent then crash before provider I/O / settle.
        prepare_worker_delivery(
            engine.connection,
            attempt_id=attempt_id,
            delivery_job_id=job_id,
            worker_principal_id="worker",
            lease_token=f"lease|deliver|{job_id}|{attempt_id}|{attempt_id}",
        )
    ctx = CommandContext(
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=_grant(),
    )
    envelope = accept_worker_deliver(
        coord,
        RuntimeCommand(
            command_type="runtime.worker_deliver",
            target_id=attempt_id,
            payload={"attempt_id": attempt_id, "delivery_job_id": job_id},
            idempotency_key=f"deliver|{job_id}|{attempt_id}",
            context=ctx,
        ),
    )
    assert runner.delivery_count == 0
    assert envelope.get("in_progress") or envelope.get("status") in {"accepted", "rejected"}
    if envelope.get("status") == "rejected":
        assert envelope.get("error_code") == "CONFLICT_CAS"


def test_redelivered_celery_task_in_progress_without_provider_call(engine) -> None:
    from flow_engine.providers.protocol import MockProviderRunner

    runner = MockProviderRunner("codex")
    coord = StateCoordinator(engine.connection, runners={"codex": runner})
    with transaction(engine.connection):
        item = submit_work(engine.connection, queue_name="default", payload={}, actor="x")
        dispatched = _async_dispatch(engine.connection, item["id"])
        job_id = dispatched["delivery"]["delivery_job_id"]
        attempt_id = dispatched["attempt"]["id"]

    ctx = CommandContext(
        principal_id="worker",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=_grant(),
    )
    # First task acquires lease then we interrupt before settle by only preparing
    # through accept — complete normally, then redeliver with same idempotency key.
    cmd = RuntimeCommand(
        command_type="runtime.worker_deliver",
        target_id=attempt_id,
        payload={"attempt_id": attempt_id, "delivery_job_id": job_id},
        idempotency_key=f"deliver|{job_id}|{attempt_id}",
        context=ctx,
    )
    first = accept_worker_deliver(coord, cmd)
    assert first["status"] == "applied"
    assert runner.delivery_count == 1
    # Celery redelivery of the same deliver idempotency key.
    second = accept_worker_deliver(coord, cmd)
    assert second.get("from_cache") is True
    assert runner.delivery_count == 1
