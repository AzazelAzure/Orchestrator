"""governance invariant tests."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path

import pytest

from flow_engine.application import (
    amend_finding,
    claim_resource,
    claim_work,
    complete_work,
    create_finding,
    create_gate,
    fail_gate,
    init_project,
    list_events,
    pass_gate,
    register_artifact,
    register_policy_version,
    renew_resource,
    show_finding,
    submit_work,
    transition_finding,
    waive_gate,
)
from flow_engine.application.clock import clear_clock, set_clock, utc_now_iso
from flow_engine.domain.errors import ConflictError, PrerequisiteError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import (
    ClaimPolicy,
    FindingSeverity,
    FindingStatus,
    GateRequirement,
    GateStatus,
    LeaseMode,
    WorkItemStatus,
)
from flow_engine.persistence import Kernel
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.migrations import (
    KERNEL_TABLES,
    _load_sql,
    current_version,
    list_tables,
)
from flow_engine.persistence.transactions import transaction


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
    yield kernel
    kernel.close()


def _artifact(conn, suffix: str = "1"):
    return register_artifact(
        conn,
        uri=f"artifact://evidence/{suffix}",
        artifact_type="note",
        sensitivity="internal",
        retention_class="standard",
        created_by="actor:test",
    )


def test_migration_from_populated_v001_to_v002(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    conn = open_connection(db_path, initialize=False)
    try:
        conn.executescript(_load_sql("001_initial_schema.sql"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (1, utc_now_iso()),
        )
        conn.commit()
        with transaction(conn):
            project = init_project(conn, name="legacy")
            queue_id = new_id()
            conn.execute(
                "INSERT INTO queues (id, project_id, name) VALUES (?, ?, ?)",
                (queue_id, project["id"], "default"),
            )
            work_id = new_id()
            conn.execute(
                """
                INSERT INTO work_items (id, queue_id, status, payload_json, revision)
                VALUES (?, ?, ?, ?, 0)
                """,
                (work_id, queue_id, WorkItemStatus.PENDING, '{"legacy": true}'),
            )
            resource_id = "shared"
            conn.execute(
                """
                INSERT INTO resources (id, kind, claim_policy, revision)
                VALUES (?, ?, ?, 0)
                """,
                (resource_id, "workspace", ClaimPolicy.STRICT),
            )
            conn.execute(
                """
                INSERT INTO leases (id, resource_id, holder, mode)
                VALUES (?, ?, ?, ?)
                """,
                (new_id(), resource_id, "holder-a", LeaseMode.EXCLUSIVE),
            )
            conn.execute(
                """
                INSERT INTO gates (id, work_item_id, gate_type, status)
                VALUES (?, ?, ?, ?)
                """,
                (new_id(), work_id, "review", GateStatus.OPEN),
            )
        assert current_version(conn) == 1
    finally:
        conn.close()

    upgraded = open_connection(db_path, initialize=True)
    try:
        assert current_version(upgraded) == 2
        assert set(KERNEL_TABLES).issubset(set(list_tables(upgraded)))
        gate = upgraded.execute(
            "SELECT requirement, revision, created_at FROM gates LIMIT 1"
        ).fetchone()
        assert gate["requirement"] == "required"
        assert gate["created_at"]
        lease = upgraded.execute(
            "SELECT acquired_at, expires_at, released_at FROM leases LIMIT 1"
        ).fetchone()
        assert lease["acquired_at"]
        assert lease["expires_at"]
        assert lease["released_at"] is None
    finally:
        upgraded.close()

    fresh = Kernel.init(tmp_path / "fresh.db")
    try:
        assert fresh.schema_version == 2
        assert set(KERNEL_TABLES).issubset(set(fresh.tables))
    finally:
        fresh.close()


def test_complete_work_blocks_open_required_gate(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        work_id = work["id"]
        claim_work(conn, work_id=work_id, actor="a")
        create_gate(conn, work_item_id=work_id, gate_type="review", actor="a")

    with transaction(conn):
        with pytest.raises(PrerequisiteError, match="required gate"):
            complete_work(conn, work_id=work_id, actor="a")


def test_complete_work_blocks_incomplete_dependency(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        blocker = submit_work(conn, queue_name="default", payload={}, actor="a")
        dependent = submit_work(
            conn,
            queue_name="default",
            payload={},
            actor="a",
            depends_on=[blocker["id"]],
        )
        claim_work(conn, work_id=dependent["id"], actor="a")

    with transaction(conn):
        with pytest.raises(PrerequisiteError, match="dependency"):
            complete_work(conn, work_id=dependent["id"], actor="a")


def test_complete_work_allows_waived_required_gate(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        work_id = work["id"]
        claim_work(conn, work_id=work_id, actor="a")
        gate = create_gate(conn, work_item_id=work_id, gate_type="review", actor="a")
        artifact = _artifact(conn)
        waive_gate(
            conn,
            gate_id=gate["id"],
            actor="a",
            authority="role:governance_reviewer",
            reason="approved",
            evidence_artifact_id=artifact["id"],
        )
        completed = complete_work(conn, work_id=work_id, actor="a")
        assert completed["status"] == "complete"


def test_waive_gate_requires_audit_fields(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        gate = create_gate(conn, work_item_id=work["id"], gate_type="review", actor="a")
        artifact = _artifact(conn)
        with pytest.raises(ValueError, match="reason is required"):
            waive_gate(
                conn,
                gate_id=gate["id"],
                actor="a",
                authority="role:governance_reviewer",
                reason="",
                evidence_artifact_id=artifact["id"],
            )


def test_gate_waiver_is_append_only(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        gate = create_gate(conn, work_item_id=work["id"], gate_type="review", actor="a")
        artifact = _artifact(conn)
        waive_gate(
            conn,
            gate_id=gate["id"],
            actor="a",
            authority="role:governance_reviewer",
            reason="approved",
            evidence_artifact_id=artifact["id"],
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE gate_actions SET reason = 'mutated' WHERE gate_id = ?",
            (gate["id"],),
        )


def test_expired_lease_allows_new_claim(engine: Kernel) -> None:
    conn = engine.connection
    set_clock("2026-07-13T10:00:00+00:00")
    with transaction(conn):
        claim_resource(
            conn,
            resource_id="shared",
            holder="agent-1",
            lease_duration_sec=60,
        )

    set_clock("2026-07-13T10:05:00+00:00")
    with transaction(conn):
        claimed = claim_resource(
            conn,
            resource_id="shared",
            holder="agent-2",
            claim_policy=ClaimPolicy.STRICT,
        )
        assert claimed["lease"]["holder"] == "agent-2"

    expired_lease = conn.execute(
        """
        SELECT revision, released_at FROM leases
        WHERE holder = 'agent-1'
        """
    ).fetchone()
    assert expired_lease["released_at"]
    assert expired_lease["revision"] == 1

    events = list_events(conn, event_type="resource.lease_expired")
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["resulting_lease_revision"] == 1
    assert payload["prior_lease_revision"] == 0
    assert payload["resulting_resource_revision"] >= 1
    assert events[0]["actor"] == "system"


def test_lease_duration_must_be_positive(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        with pytest.raises(ValueError, match="lease_duration_sec must be positive"):
            claim_resource(conn, resource_id="shared", holder="agent-1", lease_duration_sec=0)
        with pytest.raises(ValueError, match="lease_duration_sec must be positive"):
            renew_resource(conn, resource_id="shared", holder="agent-1", lease_duration_sec=-1)


def test_stale_lease_revision_rejected_after_expiry(engine: Kernel) -> None:
    conn = engine.connection
    set_clock("2026-07-13T10:00:00+00:00")
    with transaction(conn):
        claimed = claim_resource(
            conn,
            resource_id="shared",
            holder="agent-1",
            lease_duration_sec=60,
        )
        stale_revision = claimed["lease"]["revision"]

    set_clock("2026-07-13T10:05:00+00:00")
    with transaction(conn):
        with pytest.raises(ConflictError, match="is not held by agent-1"):
            renew_resource(
                conn,
                resource_id="shared",
                holder="agent-1",
                expected_lease_revision=stale_revision,
            )

    expired = conn.execute(
        "SELECT revision FROM leases WHERE holder = 'agent-1'"
    ).fetchone()
    assert expired["revision"] == stale_revision + 1


def test_renew_rejects_stale_lease_revision(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        claimed = claim_resource(
            conn,
            resource_id="shared",
            holder="agent-2",
            lease_duration_sec=60,
        )
        renewed = renew_resource(
            conn,
            resource_id="shared",
            holder="agent-2",
            lease_duration_sec=120,
            expected_lease_revision=claimed["lease"]["revision"],
        )
        assert renewed["lease"]["revision"] == claimed["lease"]["revision"] + 1

    with transaction(conn):
        with pytest.raises(ConflictError, match="lease revision mismatch"):
            renew_resource(
                conn,
                resource_id="shared",
                holder="agent-2",
                expected_lease_revision=claimed["lease"]["revision"],
            )


def test_renew_extends_lease_with_cas(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        claimed = claim_resource(
            conn,
            resource_id="shared",
            holder="agent-1",
            lease_duration_sec=60,
        )
        old_expiry = claimed["lease"]["expires_at"]
        renewed = renew_resource(conn, resource_id="shared", holder="agent-1", lease_duration_sec=120)
        assert renewed["lease"]["expires_at"] > old_expiry


def test_parallel_strict_claim_single_winner(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    conn = open_connection(db_path, initialize=True)
    try:
        with transaction(conn):
            init_project(conn, name="race")
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
                    resource_id="shared",
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

    threads = [threading.Thread(target=claimant, args=(f"agent-{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results.count("ok") == 1
    assert results.count("conflict") == 7


def test_finding_lifecycle_and_amendment_history(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        artifact = _artifact(conn)
        finding = create_finding(
            conn,
            summary="unexpected contention",
            severity=FindingSeverity.HIGH,
            actor="actor:reviewer",
            evidence_artifact_ids=[artifact["id"]],
        )
        triaged = transition_finding(
            conn,
            finding_id=finding["id"],
            target_status=FindingStatus.TRIAGED,
            actor="actor:reviewer",
            reason="investigating",
            expected_revision=0,
        )
        assert triaged["status"] == FindingStatus.TRIAGED
        assert triaged["revision"] == 1

        amended = amend_finding(
            conn,
            finding_id=finding["id"],
            actor="actor:reviewer",
            reason="severity lowered after triage",
            summary="contention on shared resource",
            severity=FindingSeverity.MEDIUM,
            expected_revision=1,
            evidence_artifact_id=artifact["id"],
        )
        assert amended["summary"] == "contention on shared resource"
        assert amended["severity"] == FindingSeverity.MEDIUM
        assert amended["revision"] == 2

    actions = conn.execute(
        "SELECT action_type FROM finding_actions WHERE finding_id = ? ORDER BY created_at",
        (finding["id"],),
    ).fetchall()
    assert [row["action_type"] for row in actions] == ["created", "transition", "amended"]

    shown = show_finding(conn, finding["id"])
    assert shown["evidence_artifact_ids"] == [artifact["id"]]


def test_amend_finding_requires_reason_and_field(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        finding = create_finding(
            conn,
            summary="initial",
            severity=FindingSeverity.LOW,
            actor="actor:reviewer",
        )
        with pytest.raises(ValueError, match="reason is required"):
            amend_finding(
                conn,
                finding_id=finding["id"],
                actor="actor:reviewer",
                reason="",
                summary="updated",
            )
        with pytest.raises(ValueError, match="summary and/or severity"):
            amend_finding(
                conn,
                finding_id=finding["id"],
                actor="actor:reviewer",
                reason="noop",
            )


def test_amend_finding_stale_cas(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        finding = create_finding(
            conn,
            summary="initial",
            severity=FindingSeverity.LOW,
            actor="actor:reviewer",
        )
        with pytest.raises(ConflictError, match="revision mismatch"):
            amend_finding(
                conn,
                finding_id=finding["id"],
                actor="actor:reviewer",
                reason="stale",
                summary="updated",
                expected_revision=99,
            )


def test_finding_actions_are_append_only(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        finding = create_finding(
            conn,
            summary="immutable history",
            severity=FindingSeverity.LOW,
            actor="actor:reviewer",
        )

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(
            "UPDATE finding_actions SET reason = 'mutated' WHERE finding_id = ?",
            (finding["id"],),
        )


def test_transition_evidence_accumulates_idempotently(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        artifact_a = _artifact(conn, "a")
        artifact_b = _artifact(conn, "b")
        finding = create_finding(
            conn,
            summary="evidence trail",
            severity=FindingSeverity.MEDIUM,
            actor="actor:reviewer",
            evidence_artifact_ids=[artifact_a["id"]],
        )
        transition_finding(
            conn,
            finding_id=finding["id"],
            target_status=FindingStatus.TRIAGED,
            actor="actor:reviewer",
            reason="reviewing",
            evidence_artifact_id=artifact_b["id"],
            expected_revision=0,
        )
        transition_finding(
            conn,
            finding_id=finding["id"],
            target_status=FindingStatus.RESOLVED,
            actor="actor:reviewer",
            reason="done",
            evidence_artifact_id=artifact_a["id"],
            expected_revision=1,
        )

    shown = show_finding(conn, finding["id"])
    assert set(shown["evidence_artifact_ids"]) == {artifact_a["id"], artifact_b["id"]}
    evidence_count = conn.execute(
        "SELECT COUNT(*) AS count FROM finding_evidence WHERE finding_id = ?",
        (finding["id"],),
    ).fetchone()["count"]
    assert evidence_count == 2


def test_pass_and_fail_gate_record_gate_actions(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        pass_gate_work = submit_work(conn, queue_name="default", payload={}, actor="a")
        pass_gate_obj = create_gate(
            conn, work_item_id=pass_gate_work["id"], gate_type="review", actor="a"
        )
        fail_gate_obj = create_gate(
            conn, work_item_id=work["id"], gate_type="review", actor="a"
        )
        passed = pass_gate(conn, gate_id=pass_gate_obj["id"], actor="a")
        failed = fail_gate(conn, gate_id=fail_gate_obj["id"], actor="a")
        assert passed["status"] == GateStatus.PASSED
        assert failed["status"] == GateStatus.FAILED

    actions = conn.execute(
        "SELECT action_type, gate_revision FROM gate_actions ORDER BY created_at"
    ).fetchall()
    action_types = [row["action_type"] for row in actions]
    assert "passed" in action_types
    assert "failed" in action_types
    for row in actions:
        assert row["gate_revision"] >= 1


def test_idempotent_gate_waiver_replay(engine: Kernel) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        gate = create_gate(conn, work_item_id=work["id"], gate_type="review", actor="a")
        artifact = _artifact(conn)
        key = "waive-once"
        first = waive_gate(
            conn,
            gate_id=gate["id"],
            actor="a",
            authority="role:governance_reviewer",
            reason="approved",
            evidence_artifact_id=artifact["id"],
            idempotency_key=key,
        )
    with transaction(conn):
        second = waive_gate(
            conn,
            gate_id=gate["id"],
            actor="a",
            authority="role:governance_reviewer",
            reason="approved",
            evidence_artifact_id=artifact["id"],
            idempotency_key=key,
        )
    assert first["id"] == second["id"]
    assert second["from_cache"] is True
    action_count = conn.execute("SELECT COUNT(*) AS count FROM gate_actions").fetchone()["count"]
    assert action_count == 1


def test_backup_restore_preserves_governed_records(engine: Kernel, tmp_path: Path) -> None:
    conn = engine.connection
    with transaction(conn):
        work = submit_work(conn, queue_name="default", payload={}, actor="a")
        work_id = work["id"]
        claim_work(conn, work_id=work_id, actor="a")
        gate = create_gate(
            conn,
            work_item_id=work_id,
            gate_type="review",
            actor="a",
            requirement=GateRequirement.REQUIRED,
        )
        artifact = _artifact(conn)
        policy = register_policy_version(
            conn,
            policy_id="gov-work",
            version="1.0.0",
            content_hash="abc123",
            canonical_uri="policy://gov-work/1.0.0",
            created_by="actor:test",
        )
        waive_gate(
            conn,
            gate_id=gate["id"],
            actor="a",
            authority="role:governance_reviewer",
            reason="approved",
            evidence_artifact_id=artifact["id"],
            policy_version_id=policy["id"],
        )
        claim_resource(conn, resource_id="shared", holder="agent-1", lease_duration_sec=120)
        create_finding(
            conn,
            summary="post-restore check",
            severity=FindingSeverity.LOW,
            actor="actor:reviewer",
        )

    backup_path = tmp_path / "backup.db"
    backup_conn = sqlite3.connect(backup_path)
    conn.backup(backup_conn)
    backup_conn.close()

    restored_path = tmp_path / "restored.db"
    shutil.copyfile(backup_path, restored_path)
    restored = open_connection(restored_path)
    try:
        assert current_version(restored) == 2
        assert restored.execute("SELECT COUNT(*) FROM gate_actions").fetchone()[0] == 1
        assert restored.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert restored.execute("SELECT COUNT(*) FROM policy_versions").fetchone()[0] == 1
        assert restored.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 1
        assert restored.execute(
            "SELECT COUNT(*) FROM leases WHERE released_at IS NULL"
        ).fetchone()[0] == 1
        assert restored.execute("SELECT COUNT(*) FROM idempotency_results").fetchone()[0] >= 0
    finally:
        restored.close()
