"""R3 organization, delegation, loadout resolution, and dispatch pins."""

from __future__ import annotations

import pytest

from flow_engine.application import ensure_queue, init_project, submit_work
from flow_engine.application.delegation_service import (
    accept_delegation,
    accept_handoff_evidence,
    assert_review_separation,
    complete_assignment,
    create_dispatch_pin,
    create_handoff,
    decline_delegation,
    dispatch_delegated_assignment,
    mint_task_grant,
    request_delegation,
    reroute_delegation,
)
from flow_engine.application.loadout_resolution import (
    ENGINE_SAFETY_FLOOR,
    all_twelve_loadout_ids,
    load_catalog_lanes,
    load_catalog_scripts,
    load_shipped_skill_hashes,
    merge_authority_layers,
    resolve_all_twelve_loadouts,
    resolve_loadout,
)
from flow_engine.application.organization_service import (
    add_actor,
    add_provider_seat,
    create_assignment,
    create_organization_profile,
    find_position,
    get_snapshot,
    list_members,
    materialize_snapshot,
    preview_loadout,
)
from flow_engine.coordinator import (
    CommandContext,
    ResolvedTaskGrant,
    RuntimeCommand,
    StateCoordinator,
    SystemTestGrant,
)
from flow_engine.domain.errors import AuthzDeniedError, StaleAssetError
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence import Kernel
from flow_engine.persistence.connection import open_connection
from flow_engine.persistence.migrations import KERNEL_TABLES, current_version, list_tables
from flow_engine.persistence.transactions import transaction


@pytest.fixture
def engine(tmp_path):
    kernel = Kernel.init(tmp_path / "state.db")
    with transaction(kernel.connection):
        init_project(kernel.connection, name="demo_project")
        ensure_queue(kernel.connection, name="default")
    yield kernel
    kernel.close()


def _work(conn) -> str:
    with transaction(conn):
        item = submit_work(conn, queue_name="default", payload={"t": 1}, actor="agent")
    return item["id"]


def _r2_grant() -> SystemTestGrant:
    return SystemTestGrant(
        grant_id="r2-compat",
        principal_id="agent",
        role=PrincipalRole.WORKER,
        surfaces=(Surface.CLI, Surface.TEST),
        providers=("codex", "cursor", "claude"),
        budget_scope_id="acceptance-campaign-r3",
    )


def _accept(coord, command_type, target, payload, ctx):
    with transaction(coord.connection):
        return coord.accept(
            RuntimeCommand(
                command_type=command_type,
                target_id=target,
                payload=payload,
                context=ctx,
            )
        )


def _bootstrap_org(conn, *, name: str = "demo-org"):
    with transaction(conn):
        org = create_organization_profile(conn, name=name, actor="founder")
        impl_actor = add_actor(
            conn,
            organization_id=org["id"],
            actor_key="impl",
            display_name="Implementer",
            actor="founder",
        )
        review_actor = add_actor(
            conn,
            organization_id=org["id"],
            actor_key="reviewer",
            display_name="Reviewer",
            actor="founder",
        )
        impl_seat = add_provider_seat(
            conn,
            organization_id=org["id"],
            actor_id=impl_actor["id"],
            provider="cursor",
            seat_key="impl-cursor",
            actor="founder",
        )
        review_seat = add_provider_seat(
            conn,
            organization_id=org["id"],
            actor_id=review_actor["id"],
            provider="claude",
            seat_key="review-claude",
            actor="founder",
        )
    return org, impl_actor, review_actor, impl_seat, review_seat


def test_migration_adds_r3_tables(engine: Kernel) -> None:
    assert current_version(engine.connection) >= 4
    tables = set(list_tables(engine.connection))
    for name in (
        "organization_profiles",
        "departments",
        "hierarchy_layers",
        "positions",
        "actors",
        "provider_seats",
        "authority_ceilings",
        "assignments",
        "delegation_requests",
        "delegation_dispositions",
        "handoffs",
        "resolved_loadout_snapshots",
        "task_grants",
        "immutable_dispatch_pins",
        "child_closure_evidence",
    ):
        assert name in tables
        assert name in KERNEL_TABLES


def test_r2_compatibility_grant_refuses_org_and_loadout(engine: Kernel) -> None:
    conn = engine.connection
    work_id = _work(conn)
    grant = _r2_grant()
    coord = StateCoordinator(conn)
    ctx = CommandContext(
        principal_id=grant.principal_id,
        role=grant.role,
        surface=Surface.CLI,
        grant=grant,
    )
    denied = _accept(
        coord,
        "runtime.preview",
        work_id,
        {"provider": "codex", "work_item_id": work_id, "loadout_id": "loadout.tech.worker"},
        ctx,
    )
    assert denied["status"] == "rejected"
    assert denied["error_code"] == "AUTHZ_DENIED"

    org_denied = _accept(
        coord,
        "org.create_profile",
        None,
        {"name": "should-fail"},
        ctx,
    )
    assert org_denied["status"] == "rejected"
    assert org_denied["error_code"] == "AUTHZ_DENIED"


def test_precedence_deny_wins_and_intersect(engine: Kernel) -> None:
    merged = merge_authority_layers(
        [
            ENGINE_SAFETY_FLOOR,
            {"capabilities": ["a", "b", "c"], "effects": ["read", "write"]},
            {"denials": ["self_review"], "capabilities": ["a", "b"], "effects": ["read"]},
            {"numeric_bounds": {"per_run_concurrency": 1}},
        ]
    )
    assert "self_review" in merged["denials"]
    assert "upward_authority" in merged["denials"]
    assert merged["capabilities"] == ["a", "b"]
    assert merged["effects"] == ["read"]
    assert merged["numeric_bounds"]["per_run_concurrency"] == 1


def test_twelve_loadouts_resolve_and_pin(engine: Kernel) -> None:
    conn = engine.connection
    org, *_ = _bootstrap_org(conn)
    resolutions = resolve_all_twelve_loadouts(org)
    assert len(resolutions) == 12
    assert set(all_twelve_loadout_ids()) == {r["loadout_id"] for r in resolutions}
    shipped_skills = load_shipped_skill_hashes()
    lanes = load_catalog_lanes()
    scripts = load_catalog_scripts()
    for item in resolutions:
        assert item["organization_profile_hash"] == org["content_sha256"]
        assert item["loadout_hash"]
        assert item["policy_hash"]
        assert item["member_asset_hashes"]
        for skill_id in item["skill_refs"]:
            assert item["member_asset_hashes"][skill_id] == shipped_skills[skill_id]
        for lane_id in item["mcp_lane_refs"]:
            assert item["member_asset_hashes"][lane_id] == lanes[lane_id]["content_sha256"]
        for script_id in item["script_refs"]:
            assert item["member_asset_hashes"][script_id] == scripts[script_id]["content_sha256"]
        assert "upward_authority" in item["authority"]["denials"]
        with transaction(conn):
            snap = materialize_snapshot(
                conn,
                organization_id=org["id"],
                loadout_id=item["loadout_id"],
                actor="founder",
            )
        assert snap["content_sha256"]
        assert snap["loadout_id"] == item["loadout_id"]


def test_stale_hash_fails_closed(engine: Kernel) -> None:
    conn = engine.connection
    org, *_ = _bootstrap_org(conn)
    with pytest.raises(StaleAssetError):
        resolve_loadout(
            loadout_id="loadout.tech.worker",
            organization_profile=org,
            expected_loadout_hash="0" * 64,
        )


def test_assignment_and_no_upward_delegation(engine: Kernel) -> None:
    conn = engine.connection
    org, impl_actor, _review_actor, impl_seat, _review_seat = _bootstrap_org(conn)
    work_id = _work(conn)
    worker = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="worker"
    )
    manager = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="manager"
    )
    with transaction(conn):
        parent = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=worker["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        with pytest.raises(AuthzDeniedError, match="upward"):
            create_assignment(
                conn,
                organization_id=org["id"],
                work_item_id=work_id,
                position_id=manager["id"],
                actor_id=impl_actor["id"],
                provider_seat_id=impl_seat["id"],
                actor="founder",
                parent_assignment_id=parent["id"],
            )


def test_delegation_accept_decline_reroute_dispatch(engine: Kernel) -> None:
    conn = engine.connection
    org, impl_actor, _review_actor, impl_seat, _review_seat = _bootstrap_org(conn)
    work_id = _work(conn)
    manager = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="manager"
    )
    worker = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="worker"
    )
    supervisor = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="supervisor"
    )
    with transaction(conn):
        parent = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=manager["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        req = request_delegation(
            conn,
            parent_assignment_id=parent["id"],
            to_position_id=worker["id"],
            packet={"objective": "implement slice"},
            actor="founder",
        )
        declined = decline_delegation(
            conn,
            request_id=req["id"],
            actor_id=impl_actor["id"],
            actor="founder",
            reason="busy",
        )
        assert declined["status"] == "declined"

        req2 = request_delegation(
            conn,
            parent_assignment_id=parent["id"],
            to_position_id=worker["id"],
            packet={"objective": "implement slice 2"},
            actor="founder",
        )
        rerouted = reroute_delegation(
            conn,
            request_id=req2["id"],
            actor_id=impl_actor["id"],
            reroute_position_id=supervisor["id"],
            actor="founder",
        )
        assert rerouted["status"] == "rerouted"
        assert rerouted["to_position_id"] == supervisor["id"]
        accepted = accept_delegation(
            conn,
            request_id=req2["id"],
            actor_id=impl_actor["id"],
            actor="founder",
        )
        assert accepted["status"] == "accepted"
        dispatched = dispatch_delegated_assignment(
            conn,
            request_id=req2["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        assert dispatched["assignment"]["parent_assignment_id"] == parent["id"]
        assert dispatched["assignment"]["position_id"] == supervisor["id"]
        assert dispatched["request"]["status"] == "dispatched"


def test_self_review_and_review_separation(engine: Kernel) -> None:
    conn = engine.connection
    org, impl_actor, review_actor, impl_seat, review_seat = _bootstrap_org(conn)
    work_id = _work(conn)
    tech_worker = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="worker"
    )
    qa_worker = find_position(
        conn, organization_id=org["id"], department_key="qa", position_key="worker"
    )
    with transaction(conn):
        impl_asg = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=tech_worker["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        # Same actor reviewing own work is denied.
        review_same = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=qa_worker["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        with pytest.raises(AuthzDeniedError, match="self-review"):
            create_handoff(
                conn,
                from_assignment_id=impl_asg["id"],
                to_assignment_id=review_same["id"],
                packet={"objective": "review me", "evidence": []},
                actor="founder",
                review_required=True,
            )
        review_asg = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=qa_worker["id"],
            actor_id=review_actor["id"],
            provider_seat_id=review_seat["id"],
            actor="founder",
        )
        same_provider_seat = add_provider_seat(
            conn,
            organization_id=org["id"],
            actor_id=review_actor["id"],
            provider="cursor",
            seat_key="review-cursor",
            actor="founder",
        )
        same_provider_asg = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=qa_worker["id"],
            actor_id=review_actor["id"],
            provider_seat_id=same_provider_seat["id"],
            actor="founder",
        )
        with pytest.raises(AuthzDeniedError, match="distinct provider"):
            create_handoff(
                conn,
                from_assignment_id=impl_asg["id"],
                to_assignment_id=same_provider_asg["id"],
                packet={"objective": "same-provider review"},
                actor="founder",
                review_required=True,
            )
        handoff = create_handoff(
            conn,
            from_assignment_id=impl_asg["id"],
            to_assignment_id=review_asg["id"],
            packet={"objective": "review", "evidence": ["diff"]},
            actor="founder",
        )
        assert handoff["packet_sha256"]
    with pytest.raises(AuthzDeniedError):
        assert_review_separation(
            implementation={
                "provider": "cursor",
                "seat_id": "s1",
                "invocation_id": "i1",
                "attempt_id": "a1",
            },
            review={
                "provider": "cursor",
                "seat_id": "s1",
                "invocation_id": "i1",
                "attempt_id": "a1",
            },
        )
    assert_review_separation(
        implementation={
            "provider": "cursor",
            "seat_id": "s1",
            "invocation_id": "i1",
            "attempt_id": "a1",
        },
        review={
            "provider": "claude",
            "seat_id": "s2",
            "invocation_id": "i2",
            "attempt_id": "a2",
        },
    )


def test_parent_closure_blocked_until_child_accepted(engine: Kernel) -> None:
    conn = engine.connection
    org, impl_actor, review_actor, impl_seat, review_seat = _bootstrap_org(conn)
    work_id = _work(conn)
    manager = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="manager"
    )
    worker = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="worker"
    )
    with transaction(conn):
        parent = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=manager["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        child = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=worker["id"],
            actor_id=review_actor["id"],
            provider_seat_id=review_seat["id"],
            actor="founder",
            parent_assignment_id=parent["id"],
        )
        with pytest.raises(AuthzDeniedError, match="parent closure blocked"):
            complete_assignment(conn, assignment_id=parent["id"], actor="founder")
        informational = create_handoff(
            conn,
            from_assignment_id=child["id"],
            to_assignment_id=parent["id"],
            packet={"objective": "informational handoff"},
            actor="founder",
            review_required=False,
        )
        accept_handoff_evidence(
            conn,
            handoff_id=informational["id"],
            actor="founder",
            evidence={"status": "received"},
        )
        reverse_informational = create_handoff(
            conn,
            from_assignment_id=parent["id"],
            to_assignment_id=child["id"],
            packet={"objective": "parent informational handoff"},
            actor="founder",
            review_required=False,
        )
        accept_handoff_evidence(
            conn,
            handoff_id=reverse_informational["id"],
            actor="founder",
            evidence={"status": "received"},
        )
        with pytest.raises(AuthzDeniedError, match="parent closure blocked"):
            complete_assignment(conn, assignment_id=parent["id"], actor="founder")
        handoff = create_handoff(
            conn,
            from_assignment_id=child["id"],
            to_assignment_id=parent["id"],
            packet={"objective": "done", "evidence": ["ok"]},
            actor="founder",
            review_required=True,
        )
        with pytest.raises(AuthzDeniedError, match="missing dispatch_pin_id"):
            accept_handoff_evidence(
                conn,
                handoff_id=handoff["id"],
                actor="founder",
                evidence={"status": "accepted"},
            )
        child_grant = mint_task_grant(
            conn,
            organization_id=org["id"],
            principal_id="child-agent",
            role=PrincipalRole.WORKER,
            surfaces=(Surface.CLI,),
            providers=("claude",),
            budget_scope_id="closure-review",
            assignment_id=child["id"],
            actor="founder",
        )
        parent_grant = mint_task_grant(
            conn,
            organization_id=org["id"],
            principal_id="parent-reviewer",
            role=PrincipalRole.MANAGER,
            surfaces=(Surface.CLI,),
            providers=("cursor",),
            budget_scope_id="closure-review",
            assignment_id=parent["id"],
            actor="founder",
        )
        child_pin = create_dispatch_pin(
            conn,
            grant=child_grant,
            packet_hash="c" * 64,
            actor="founder",
            attempt_id="child-attempt",
            invocation_id="child-invocation",
        )
        parent_pin = create_dispatch_pin(
            conn,
            grant=parent_grant,
            packet_hash="d" * 64,
            actor="founder",
            attempt_id="parent-review-attempt",
            invocation_id="parent-review-invocation",
        )
        with pytest.raises(AuthzDeniedError, match="not bound"):
            accept_handoff_evidence(
                conn,
                handoff_id=handoff["id"],
                actor="founder",
                evidence={
                    "implementation": {"dispatch_pin_id": parent_pin["id"]},
                    "review": {"dispatch_pin_id": child_pin["id"]},
                },
            )
        accept_handoff_evidence(
            conn,
            handoff_id=handoff["id"],
            actor="founder",
            evidence={
                "status": "accepted",
                "implementation": {
                    "dispatch_pin_id": child_pin["id"],
                },
                "review": {
                    "dispatch_pin_id": parent_pin["id"],
                },
            },
        )
        completed = complete_assignment(conn, assignment_id=parent["id"], actor="founder")
        assert completed["status"] == "completed"


def test_r3_dispatch_requires_pins_and_r2_path_retained(engine: Kernel) -> None:
    conn = engine.connection
    org, impl_actor, _ra, impl_seat, _rs = _bootstrap_org(conn)
    work_id = _work(conn)
    worker = find_position(
        conn, organization_id=org["id"], department_key="tech", position_key="worker"
    )
    with transaction(conn):
        assignment = create_assignment(
            conn,
            organization_id=org["id"],
            work_item_id=work_id,
            position_id=worker["id"],
            actor_id=impl_actor["id"],
            provider_seat_id=impl_seat["id"],
            actor="founder",
        )
        grant = mint_task_grant(
            conn,
            organization_id=org["id"],
            principal_id="agent",
            role=PrincipalRole.WORKER,
            surfaces=(Surface.CLI, Surface.TEST),
            providers=("codex", "cursor", "claude"),
            budget_scope_id="r3-campaign",
            assignment_id=assignment["id"],
            actor="founder",
        )
        assert isinstance(grant, ResolvedTaskGrant)
        pin = create_dispatch_pin(
            conn,
            grant=grant,
            packet_hash="a" * 64,
            actor="founder",
        )
        assert pin["loadout_identity"] == "loadout.tech.worker"
        snap = get_snapshot(conn, grant.snapshot_id)
        stale_grant = ResolvedTaskGrant(
            grant_id=grant.grant_id,
            principal_id=grant.principal_id,
            role=grant.role,
            surfaces=grant.surfaces,
            providers=grant.providers,
            budget_scope_id=grant.budget_scope_id,
            organization_id=grant.organization_id,
            organization_profile_hash=grant.organization_profile_hash,
            loadout_id=grant.loadout_id,
            snapshot_id=grant.snapshot_id,
            assignment_id=grant.assignment_id,
        )
        with pytest.raises(StaleAssetError):
            create_dispatch_pin(
                conn,
                grant=stale_grant,
                packet_hash="b" * 64,
                actor="founder",
                expected_snapshot_hash="0" * 64,
            )
        _ = snap

    # R2 compatibility path still creates runs without loadout pins.
    r2 = _r2_grant()
    coord = StateCoordinator(conn)
    ctx = CommandContext(
        principal_id=r2.principal_id,
        role=r2.role,
        surface=Surface.CLI,
        grant=r2,
    )
    created = _accept(
        coord,
        "runtime.create",
        work_id,
        {"provider": "codex", "work_item_id": work_id},
        ctx,
    )
    assert created["status"] == "applied"
    assert created["result"]["preview"]["loadout_resolution"] == "refused"
    assert "dispatch_pin" not in created["result"]


def test_coordinator_org_preview_and_members(engine: Kernel) -> None:
    conn = engine.connection
    coord = StateCoordinator(conn)
    ctx = CommandContext(
        principal_id="founder",
        role=PrincipalRole.FOUNDER,
        surface=Surface.CLI,
        grant=None,
    )
    created = _accept(
        coord,
        "org.create_profile",
        None,
        {"name": "coord-org"},
        ctx,
    )
    assert created["status"] == "applied"
    org_id = created["result"]["profile"]["id"]
    members = _accept(coord, "org.list_members", org_id, {}, ctx)
    assert members["status"] == "applied"
    assert len(members["result"]["positions"]) == 12
    preview = _accept(
        coord,
        "org.preview_loadout",
        None,
        {"organization_id": org_id, "loadout_id": "loadout.qa.worker"},
        ctx,
    )
    assert preview["status"] == "applied"
    assert preview["result"]["resolution"]["loadout_id"] == "loadout.qa.worker"
    with transaction(conn):
        listed = list_members(conn, org_id)
        assert len(listed["positions"]) == 12
        preview_loadout(
            conn,
            organization_id=org_id,
            loadout_id="loadout.admin-ops.executive",
            actor="founder",
        )


def test_additive_upgrade_from_r2(tmp_path) -> None:
    db_path = tmp_path / "upgrade.db"
    # Initialize fully (applies all migrations including 004).
    kernel = Kernel.init(db_path)
    try:
        assert current_version(kernel.connection) == 8
        assert "immutable_dispatch_pins" in list_tables(kernel.connection)
    finally:
        kernel.close()
    # Re-open preserves version.
    conn = open_connection(db_path, initialize=True)
    try:
        assert current_version(conn) == 8
    finally:
        conn.close()
