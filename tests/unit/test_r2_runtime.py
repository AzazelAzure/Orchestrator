"""R2 persistent runtime: migrations, transitions, credits, recovery, authz."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from flow_engine.application import (
    claim_work,
    complete_work,
    create_gate,
    ensure_queue,
    init_project,
    submit_work,
)
from flow_engine.application.clock import clear_clock, set_clock, utc_now_iso
from flow_engine.application.credit_service import credit_usage, reserve_credit
from flow_engine.application.recovery_service import (
    reconstruct_eligible_deliveries,
    recover_after_restart,
    recover_worker_death,
)
from flow_engine.application.runtime_service import evaluate_timeouts, get_run
from flow_engine.coordinator import (
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    StepUpEvidence,
    SystemTestGrant,
    list_audit_events,
)
from flow_engine.domain.credits import (
    ACCEPTANCE_CREDIT_PER_PROVIDER,
    ACCEPTANCE_CREDIT_TOTAL,
    GLOBAL_PROVIDER_CONCURRENCY,
    PER_PROVIDER_CONCURRENCY,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import (
    LEGACY_WORK_ITEM_STATUSES,
    PrincipalRole,
    Surface,
    WorkItemStatus,
)
from flow_engine.domain.transitions import WORK_ITEM_TRANSITIONS
from flow_engine.persistence import Kernel
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.migrations import (
    KERNEL_TABLES,
    _load_sql,
    current_version,
    list_tables,
)
from flow_engine.persistence.transactions import transaction
from flow_engine.providers import MockProviderRunner, default_mock_registry


@pytest.fixture(autouse=True)
def _reset_clock() -> None:
    clear_clock()
    yield
    clear_clock()


@pytest.fixture
def engine(tmp_path):
    kernel = Kernel.init(tmp_path / "state.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo_project")
        ensure_queue(kernel.connection, name="default")
    yield kernel
    kernel.close()


def _grant(
    *,
    principal: str = "agent",
    role: PrincipalRole = PrincipalRole.WORKER,
    providers: tuple[str, ...] = ("codex", "cursor", "claude"),
    surfaces: tuple[Surface, ...] = (Surface.CLI, Surface.TEST),
) -> SystemTestGrant:
    return SystemTestGrant(
        grant_id="test-grant",
        principal_id=principal,
        role=role,
        surfaces=surfaces,
        providers=providers,
        budget_scope_id="acceptance-campaign-test",
    )


def _ctx(
    grant: SystemTestGrant,
    *,
    role: PrincipalRole | None = None,
    surface: Surface = Surface.CLI,
    step_up: StepUpEvidence | None = None,
) -> CommandContext:
    return CommandContext(
        principal_id=grant.principal_id,
        role=role or grant.role,
        surface=surface,
        grant=grant,
        step_up=step_up,
    )


def _accept(
    coord: StateCoordinator,
    command_type: str,
    target: str | None,
    payload: dict,
    ctx: CommandContext,
    *,
    idempotency_key: str | None = None,
):
    with transaction(coord.connection):
        return coord.accept(
            RuntimeCommand(
                command_type=command_type,
                target_id=target,
                payload=payload,
                idempotency_key=idempotency_key,
                context=ctx,
            )
        )


def _work(conn, *, queue: str = "default") -> str:
    with transaction(conn):
        item = submit_work(conn, queue_name=queue, payload={"t": 1}, actor="agent")
    return item["id"]


def test_migration_preserves_legacy_pending_claim(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = open_connection(db_path, initialize=False)
    try:
        conn.executescript(_load_sql("001_initial_schema.sql"))
        conn.executescript(_load_sql("002_governance_invariants.sql"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?), (2, ?)",
            (utc_now_iso(), utc_now_iso()),
        )
        conn.commit()
        with transaction(conn):
            project = init_project(conn, name="legacy")
            from flow_engine.domain.models import new_id

            queue_id = new_id()
            conn.execute(
                "INSERT INTO queues (id, project_id, name) VALUES (?, ?, ?)",
                (queue_id, project["id"], "default"),
            )
            work_id = new_id()
            conn.execute(
                """
                INSERT INTO work_items (id, queue_id, status, payload_json, revision)
                VALUES (?, ?, ?, '{}', 0)
                """,
                (work_id, queue_id, WorkItemStatus.PENDING),
            )
        assert current_version(conn) == 2
    finally:
        conn.close()

    upgraded = open_connection(db_path, initialize=True)
    try:
        assert current_version(upgraded) == 8
        assert set(KERNEL_TABLES).issubset(set(list_tables(upgraded)))
        row = upgraded.execute(
            "SELECT status FROM work_items WHERE id = ?",
            (work_id,),
        ).fetchone()
        assert row["status"] == WorkItemStatus.PENDING
        with transaction(upgraded):
            claimed = claim_work(upgraded, actor="legacy-agent", work_id=work_id)
        assert claimed["status"] == WorkItemStatus.CLAIMED
    finally:
        upgraded.close()


def test_legacy_four_state_edges_preserved() -> None:
    assert LEGACY_WORK_ITEM_STATUSES <= set(WORK_ITEM_TRANSITIONS)
    assert WorkItemStatus.CLAIMED in WORK_ITEM_TRANSITIONS[WorkItemStatus.PENDING]
    assert WorkItemStatus.COMPLETE in WORK_ITEM_TRANSITIONS[WorkItemStatus.CLAIMED]
    assert WorkItemStatus.FAILED in WORK_ITEM_TRANSITIONS[WorkItemStatus.CLAIMED]
    assert WorkItemStatus.PENDING in WORK_ITEM_TRANSITIONS[WorkItemStatus.FAILED]


def test_r2_lifecycle_happy_path(engine: Kernel) -> None:
    conn = engine.connection
    work_id = _work(conn)
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)

    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    assert created["status"] == "applied"
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]

    claimed = _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    assert claimed["result"]["run"]["status"] == "claimed"

    stepped = _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    assert stepped["status"] == "applied"
    inv = stepped["result"]["invocation"]
    assert inv["status"] == "dispatched"

    done = _accept(
        coord,
        "runtime.result",
        attempt_id,
        {"attempt_id": attempt_id, "outcome": "complete", "anomalies": []},
        ctx,
    )
    assert done["status"] == "applied"
    assert done["result"]["run"]["status"] == "complete"
    assert done["result"]["credits"]["totals"]["settled"] == 1


def test_credit_envelopes_and_duplicate_dispatch(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)

    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    dup = _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id, "extra": "dup"},
        ctx,
    )
    assert dup["status"] == "rejected"
    assert dup["error_code"] == "CONFLICT_CAS"

    usage = credit_usage(conn, run_id)
    assert usage["budget_total"] == ACCEPTANCE_CREDIT_TOTAL
    assert usage["budget_per_provider"] == ACCEPTANCE_CREDIT_PER_PROVIDER
    assert usage["totals"]["open_reservations"] == 1


def test_per_provider_concurrency(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)

    run_ids = []
    for i in range(PER_PROVIDER_CONCURRENCY + 1):
        work_id = _work(conn)
        created = _accept(
            coord,
            "runtime.create",
            work_id,
            {"provider": "codex", "work_item_id": work_id},
            ctx,
        )
        run_id = created["result"]["run"]["id"]
        attempt_id = created["result"]["attempt"]["id"]
        _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
        result = _accept(
            coord,
            "runtime.step",
            run_id,
            {"run_id": run_id, "attempt_id": attempt_id, "n": i},
            ctx,
        )
        run_ids.append((run_id, result))

    assert run_ids[0][1]["status"] == "applied"
    assert run_ids[1][1]["status"] == "rejected"
    assert run_ids[1][1]["error_code"] == "BUDGET_EXHAUSTED"


def test_idempotent_command_replay(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    payload = {"provider": "cursor", "work_item_id": work_id}
    first = _accept(coord, "runtime.preview", work_id, payload, ctx)
    second = _accept(coord, "runtime.preview", work_id, payload, ctx)
    assert first["status"] == "applied"
    assert second["from_cache"] is True
    assert second["operation_id"] == first["operation_id"]


def test_outcome_unknown_reconcile_and_no_auto_retry(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "claude", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    unknown = _accept(
        coord,
        "runtime.result",
        attempt_id,
        {
            "attempt_id": attempt_id,
            "outcome": "outcome_unknown",
            "anomalies": [{"code": "A1"}],
        },
        ctx,
    )
    assert unknown["status"] == "applied"
    assert unknown["result"]["halted"] is True
    assert get_run(conn, run_id)["status"] == "outcome_unknown"
    assert unknown["result"]["credits"]["totals"]["settled"] == 1

    # Worker path cannot open new attempt without founder step-up
    denied = _accept(
        coord,
        "runtime.new_attempt_after_unknown",
        run_id,
        {"run_id": run_id},
        ctx,
    )
    assert denied["status"] == "rejected"
    assert denied["error_code"] == "AUTHZ_DENIED"

    recon = _accept(
        coord,
        "runtime.reconcile",
        run_id,
        {"run_id": run_id, "auto_finish": True, "outcome": "failed"},
        ctx,
    )
    assert recon["status"] == "applied"
    assert recon["result"]["finished"]["run"]["status"] == "failed"


def test_founder_new_attempt_requires_step_up(engine: Kernel) -> None:
    conn = engine.connection
    work_id = _work(conn)
    grant = _grant(role=PrincipalRole.FOUNDER)
    coord = StateCoordinator(conn)
    worker_ctx = _ctx(grant, role=PrincipalRole.WORKER)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        worker_ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, worker_ctx)
    _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        worker_ctx,
    )
    _accept(
        coord,
        "runtime.result",
        attempt_id,
        {"attempt_id": attempt_id, "outcome": "outcome_unknown", "anomalies": []},
        worker_ctx,
    )
    _accept(
        coord,
        "runtime.reconcile",
        run_id,
        {"run_id": run_id, "auto_finish": True, "outcome": "failed"},
        worker_ctx,
    )

    now = datetime.now(UTC).replace(microsecond=0)
    step_up = StepUpEvidence(
        reauthenticated_at=now.isoformat(),
        reason="retry after reconcile",
        evidence="ticket-1",
        duplicate_cost_warning_ack=True,
        policy_revision="system-test",
        new_idempotency_identity="new-attempt-1",
    )
    founder_ctx = _ctx(grant, role=PrincipalRole.FOUNDER, step_up=step_up)
    ok = _accept(
        coord,
        "runtime.new_attempt_after_unknown",
        run_id,
        {"run_id": run_id},
        founder_ctx,
    )
    assert ok["status"] == "applied"
    assert ok["result"]["attempt"]["attempt_number"] == 2

    mcp_ctx = _ctx(
        grant,
        role=PrincipalRole.FOUNDER,
        surface=Surface.MCP,
        step_up=step_up,
    )
    mcp_denied = _accept(
        coord,
        "runtime.new_attempt_after_unknown",
        run_id,
        {"run_id": run_id, "again": True},
        mcp_ctx,
    )
    assert mcp_denied["status"] == "rejected"
    assert mcp_denied["error_code"] == "UNSUPPORTED_SURFACE"


def test_loadout_resolution_refused(engine: Kernel) -> None:
    conn = engine.connection
    work_id = _work(conn)
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    denied = _accept(
        coord,
        "runtime.preview",
        work_id,
        {
            "provider": "codex",
            "work_item_id": work_id,
            "loadout_id": "tech.worker",
        },
        ctx,
    )
    assert denied["status"] == "rejected"
    assert denied["error_code"] == "AUTHZ_DENIED"


def test_recovery_restart_no_duplicate_paid_calls(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    stepped = _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    invocation_id = stepped["result"]["invocation"]["id"]
    before = conn.execute("SELECT COUNT(*) AS n FROM provider_invocations").fetchone()["n"]

    with transaction(conn):
        recovery = recover_after_restart(conn)
    assert recovery["new_paid_calls"] == 0
    deliveries = reconstruct_eligible_deliveries(conn)
    assert any(d["invocation_id"] == invocation_id for d in deliveries)
    after = conn.execute("SELECT COUNT(*) AS n FROM provider_invocations").fetchone()["n"]
    assert after == before


def test_timeout_after_dispatch_marks_unknown(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    future = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0).isoformat()
    set_clock(future)
    timed = _accept(coord, "runtime.evaluate_timeouts", None, {}, ctx)
    assert timed["status"] == "applied"
    assert any(t["outcome"] == "outcome_unknown" for t in timed["result"]["timeouts"])
    assert get_run(conn, run_id)["status"] == "outcome_unknown"


def test_provider_limit_halt_blocks_claim(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    _accept(coord, "runtime.provider_limit_halt", run_id, {"run_id": run_id}, ctx)
    blocked = _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    assert blocked["status"] == "rejected"
    _accept(coord, "runtime.provider_limit_continue", run_id, {"run_id": run_id}, ctx)
    ok = _accept(
        coord,
        "runtime.claim",
        run_id,
        {"run_id": run_id},
        ctx,
        idempotency_key="claim-after-provider-limit-continue",
    )
    assert ok["status"] == "applied"


def test_mock_provider_protocol_contract() -> None:
    runner = MockProviderRunner("codex")
    from flow_engine.providers import InvocationRequest

    req = InvocationRequest(
        invocation_id="inv1",
        attempt_id="att1",
        run_id="run1",
        provider="codex",
        payload={"x": 1},
        cwd_policy="workspace-root",
        timeout_sec=123,
        env_allowlist=("SAFE_NAME",),
    )
    prepared = runner.prepare(req)
    assert prepared.argv[0] == "mock-codex"
    assert prepared.cwd == "workspace-root"
    assert prepared.timeout_sec == 123
    assert prepared.env_allowlist == ("SAFE_NAME",)
    assert prepared.heartbeat_interval_sec == 60
    handle = runner.deliver(prepared)
    assert handle.delivered
    assert runner.heartbeat(handle).alive
    result = runner.collect(handle)
    assert result.outcome == "complete"
    recon = runner.reconcile("inv1")
    assert recon.outcome == "complete"
    assert runner.delivery_count == 1


def test_credit_budget_is_shared_across_runs_in_one_grant(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant(providers=("codex",))
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    run_ids: list[str] = []
    for index in range(4):
        work_id = _work(conn, queue="default")
        created = _accept(
            coord,
            "runtime.create",
            work_id,
            {"provider": "codex", "work_item_id": work_id, "index": index},
            ctx,
        )
        run_id = created["result"]["run"]["id"]
        attempt_id = created["result"]["attempt"]["id"]
        run_ids.append(run_id)
        _accept(
            coord,
            "runtime.claim",
            run_id,
            {"run_id": run_id},
            ctx,
            idempotency_key=f"claim-{index}",
        )
        stepped = _accept(
            coord,
            "runtime.step",
            run_id,
            {"run_id": run_id, "attempt_id": attempt_id},
            ctx,
            idempotency_key=f"step-{index}",
        )
        if index < ACCEPTANCE_CREDIT_PER_PROVIDER:
            assert stepped["status"] == "applied"
            _accept(
                coord,
                "runtime.result",
                attempt_id,
                {"attempt_id": attempt_id, "outcome": "complete", "anomalies": []},
                ctx,
                idempotency_key=f"result-{index}",
            )
        else:
            assert stepped["status"] == "rejected"
            assert stepped["error_code"] == "BUDGET_EXHAUSTED"
    usage = credit_usage(conn, run_ids[0])
    assert usage["totals"]["consumed"] == ACCEPTANCE_CREDIT_PER_PROVIDER


def test_provider_delivery_exception_becomes_audited_unknown(engine: Kernel) -> None:
    conn = engine.connection
    runner = MockProviderRunner("codex", fail_deliver=True)
    coord = StateCoordinator(conn, runners={"codex": runner})
    grant = _grant(providers=("codex",))
    ctx = _ctx(grant)
    work_id = _work(conn)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    result = _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    assert result["status"] == "applied"
    assert result["error_code"] == "OUTCOME_UNKNOWN"
    assert result["result"]["run"]["status"] == "outcome_unknown"
    assert credit_usage(conn, run_id)["totals"]["consumed"] == 1
    events = list_audit_events(conn)
    assert any(
        event["event_type"] == "runtime.provider_delivery_unknown" and event["anomaly_code"] == "A1"
        for event in events
    )


def test_gate_blocked_completion_emits_a2_anomaly(engine: Kernel) -> None:
    conn = engine.connection
    coord = StateCoordinator(conn)
    grant = _grant(providers=("codex",))
    ctx = _ctx(grant)
    work_id = _work(conn)
    with transaction(conn):
        create_gate(conn, work_item_id=work_id, gate_type="required", actor="test")
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    attempt_id = created["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    _accept(
        coord,
        "runtime.step",
        run_id,
        {"run_id": run_id, "attempt_id": attempt_id},
        ctx,
    )
    result = _accept(
        coord,
        "runtime.result",
        attempt_id,
        {"attempt_id": attempt_id, "outcome": "complete", "anomalies": []},
        ctx,
    )
    assert result["status"] == "rejected"
    assert result["error_code"] == "GATE_OPEN"
    assert result["anomalies"][0]["code"] == "A2"


def test_total_credit_budget_spans_providers_and_runs(engine: Kernel) -> None:
    conn = engine.connection
    providers = ("codex", "cursor", "claude", "other")
    runners = default_mock_registry()
    runners["other"] = MockProviderRunner("other")
    coord = StateCoordinator(conn, runners=runners)
    grant = _grant(providers=providers)
    ctx = _ctx(grant)
    for index in range(10):
        provider = providers[min(index // 3, 3)]
        work_id = _work(conn)
        created = _accept(
            coord,
            "runtime.create",
            work_id,
            {"provider": provider, "work_item_id": work_id, "index": index},
            ctx,
            idempotency_key=f"total-create-{index}",
        )
        run_id = created["result"]["run"]["id"]
        attempt_id = created["result"]["attempt"]["id"]
        _accept(
            coord,
            "runtime.claim",
            run_id,
            {"run_id": run_id},
            ctx,
            idempotency_key=f"total-claim-{index}",
        )
        step = _accept(
            coord,
            "runtime.step",
            run_id,
            {"run_id": run_id, "attempt_id": attempt_id},
            ctx,
            idempotency_key=f"total-step-{index}",
        )
        if index < ACCEPTANCE_CREDIT_TOTAL:
            assert step["status"] == "applied"
            _accept(
                coord,
                "runtime.result",
                attempt_id,
                {"attempt_id": attempt_id, "outcome": "complete", "anomalies": []},
                ctx,
                idempotency_key=f"total-result-{index}",
            )
        else:
            assert step["status"] == "rejected"
            assert step["error_code"] == "BUDGET_EXHAUSTED"


def test_worker_death_consumes_after_dispatch_and_releases_before(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant(providers=("codex",))
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)

    dispatched_work = _work(conn)
    dispatched = _accept(
        coord,
        "runtime.create",
        dispatched_work,
        {"provider": "codex", "work_item_id": dispatched_work},
        ctx,
    )
    dispatched_run = dispatched["result"]["run"]["id"]
    dispatched_attempt = dispatched["result"]["attempt"]["id"]
    _accept(coord, "runtime.claim", dispatched_run, {"run_id": dispatched_run}, ctx)
    _accept(
        coord,
        "runtime.step",
        dispatched_run,
        {"run_id": dispatched_run, "attempt_id": dispatched_attempt},
        ctx,
    )
    with transaction(conn):
        after = recover_worker_death(conn, attempt_id=dispatched_attempt)
    assert after["action"] == "outcome_unknown"
    assert credit_usage(conn, dispatched_run)["totals"]["consumed"] == 1

    pre_work = _work(conn)
    pre = _accept(
        coord,
        "runtime.create",
        pre_work,
        {"provider": "codex", "work_item_id": pre_work, "phase": "pre"},
        ctx,
    )
    pre_run = pre["result"]["run"]["id"]
    pre_attempt = pre["result"]["attempt"]["id"]
    _accept(
        coord,
        "runtime.claim",
        pre_run,
        {"run_id": pre_run},
        ctx,
        idempotency_key="worker-death-pre-claim",
    )
    invocation_id = new_id()
    now = utc_now_iso()
    with transaction(conn):
        conn.execute(
            """INSERT INTO provider_invocations
               (id, attempt_id, run_id, provider, status, request_digest,
                result_json, evidence_json, created_at, updated_at)
               VALUES (?, ?, ?, 'codex', 'reserved', '{}', NULL, '{}', ?, ?)""",
            (invocation_id, pre_attempt, pre_run, now, now),
        )
        reserve_credit(
            conn,
            run_id=pre_run,
            provider="codex",
            attempt_id=pre_attempt,
            invocation_id=invocation_id,
        )
        before = recover_worker_death(conn, attempt_id=pre_attempt)
    assert before["action"] == "failed_pre_dispatch"
    usage = credit_usage(conn, pre_run)["totals"]
    assert usage["open_reservations"] == 0
    assert usage["released"] == 1


def test_attempt_lease_expiry_alone_triggers_recovery(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant(providers=("codex",))
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    run_id = created["result"]["run"]["id"]
    _accept(coord, "runtime.claim", run_id, {"run_id": run_id}, ctx)
    set_clock((datetime.now(UTC) + timedelta(seconds=121)).isoformat())
    with transaction(conn):
        results = evaluate_timeouts(conn)
    assert results[0]["outcome"] == "failed"
    assert get_run(conn, run_id)["status"] == "failed"


def test_audit_append_only_and_mandatory_anomalies(engine: Kernel) -> None:
    conn = engine.connection
    grant = _grant()
    coord = StateCoordinator(conn)
    ctx = _ctx(grant)
    work_id = _work(conn)
    _accept(
        coord,
        "runtime.preview",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    events = _accept(coord, "runtime.list_audit", None, {"limit": 10}, ctx)
    assert events["status"] == "applied"
    assert isinstance(events["result"]["events"], list)
    assert events["anomalies"] == []
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_events")


def test_legacy_work_path_still_works(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        item = submit_work(conn, queue_name="default", payload={}, actor="a")
        claimed = claim_work(conn, actor="a", work_id=item["id"])
        done = complete_work(conn, work_id=item["id"], actor="a")
    assert claimed["status"] == "claimed"
    assert done["status"] == "complete"


def test_schema_version_is_current(engine: Kernel) -> None:
    assert engine.schema_version == 8
    assert engine.has_kernel_tables()


def test_global_concurrency_constant() -> None:
    assert GLOBAL_PROVIDER_CONCURRENCY == 3
    assert len(default_mock_registry()) == 3
