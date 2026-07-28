"""R3 delegation requests, dispositions, handoffs, grants, and dispatch pins."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.application.loadout_resolution import resolve_loadout
from flow_engine.application.organization_service import (
    POSITION_RANK,
    assert_parent_closure_allowed,
    get_assignment,
    get_organization_profile,
    get_position,
    get_provider_seat,
    get_snapshot,
    materialize_snapshot,
)
from flow_engine.coordinator.audit import append_audit_event
from flow_engine.coordinator.commands import ResolvedTaskGrant, stable_digest
from flow_engine.domain.errors import (
    AuthzDeniedError,
    ConflictError,
    NotFoundError,
    PrerequisiteError,
    StaleAssetError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import AnomalyCode, PrincipalRole, Surface


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def request_delegation(
    conn: sqlite3.Connection,
    *,
    parent_assignment_id: str,
    to_position_id: str,
    packet: dict[str, Any],
    actor: str,
) -> dict[str, Any]:
    parent = get_assignment(conn, parent_assignment_id)
    if parent["status"] != "active":
        raise AuthzDeniedError("parent assignment is not active")
    from_pos = get_position(conn, parent["position_id"])
    to_pos = get_position(conn, to_position_id)
    if to_pos["organization_id"] != parent["organization_id"]:
        raise AuthzDeniedError("cross-organization delegation denied")
    if POSITION_RANK[to_pos["position_key"]] >= POSITION_RANK[from_pos["position_key"]]:
        append_audit_event(
            conn,
            event_type="delegation.upward_denied",
            actor=actor,
            anomaly_code=AnomalyCode.A2,
            payload={
                "parent_assignment_id": parent_assignment_id,
                "to_position": to_pos["position_key"],
            },
        )
        raise AuthzDeniedError("no upward authority")

    if not isinstance(packet, dict) or not packet.get("objective"):
        raise ValidationFailedError("delegation packet requires objective")
    packet_hash = stable_digest(packet)
    request_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO delegation_requests (
            id, organization_id, parent_assignment_id, from_position_id,
            to_position_id, work_item_id, packet_json, packet_sha256,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?, ?)
        """,
        (
            request_id,
            parent["organization_id"],
            parent_assignment_id,
            from_pos["id"],
            to_position_id,
            parent["work_item_id"],
            json.dumps(packet, sort_keys=True),
            packet_hash,
            now,
            now,
        ),
    )
    append_audit_event(
        conn,
        event_type="delegation.requested",
        actor=actor,
        payload={"request_id": request_id, "packet_sha256": packet_hash},
    )
    return get_delegation_request(conn, request_id)


def get_delegation_request(conn: sqlite3.Connection, request_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM delegation_requests WHERE id = ?", (request_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"delegation request not found: {request_id}")
    data = _row_to_dict(row)
    data["packet"] = json.loads(data.pop("packet_json"))
    return data


def _append_disposition(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    disposition: str,
    actor_id: str,
    reason: str = "",
    reroute_position_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO delegation_dispositions (
            id, request_id, disposition, actor_id, reason,
            reroute_position_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            new_id(),
            request_id,
            disposition,
            actor_id,
            reason,
            reroute_position_id,
            utc_now_iso(),
        ),
    )


def accept_delegation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    actor_id: str,
    actor: str,
) -> dict[str, Any]:
    req = get_delegation_request(conn, request_id)
    if req["status"] not in {"requested", "rerouted"}:
        raise ConflictError(f"delegation request not in requested state: {req['status']}")
    _append_disposition(
        conn, request_id=request_id, disposition="accepted", actor_id=actor_id
    )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE delegation_requests SET status = 'accepted', updated_at = ?
        WHERE id = ?
        """,
        (now, request_id),
    )
    append_audit_event(
        conn,
        event_type="delegation.accepted",
        actor=actor,
        payload={"request_id": request_id},
    )
    return get_delegation_request(conn, request_id)


def decline_delegation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    actor_id: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    req = get_delegation_request(conn, request_id)
    if req["status"] != "requested":
        raise ConflictError(f"delegation request not in requested state: {req['status']}")
    _append_disposition(
        conn,
        request_id=request_id,
        disposition="declined",
        actor_id=actor_id,
        reason=reason,
    )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE delegation_requests SET status = 'declined', updated_at = ?
        WHERE id = ?
        """,
        (now, request_id),
    )
    append_audit_event(
        conn,
        event_type="delegation.declined",
        actor=actor,
        payload={"request_id": request_id, "reason": reason},
    )
    return get_delegation_request(conn, request_id)


def reroute_delegation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    actor_id: str,
    reroute_position_id: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    req = get_delegation_request(conn, request_id)
    if req["status"] not in {"requested", "accepted"}:
        raise AuthzDeniedError(f"cannot reroute request in state {req['status']}")
    parent = get_assignment(conn, req["parent_assignment_id"])
    from_pos = get_position(conn, parent["position_id"])
    to_pos = get_position(conn, reroute_position_id)
    if POSITION_RANK[to_pos["position_key"]] >= POSITION_RANK[from_pos["position_key"]]:
        raise AuthzDeniedError("no upward authority on reroute")
    _append_disposition(
        conn,
        request_id=request_id,
        disposition="rerouted",
        actor_id=actor_id,
        reason=reason,
        reroute_position_id=reroute_position_id,
    )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE delegation_requests
        SET status = 'rerouted', to_position_id = ?, updated_at = ?
        WHERE id = ?
        """,
        (reroute_position_id, now, request_id),
    )
    append_audit_event(
        conn,
        event_type="delegation.rerouted",
        actor=actor,
        payload={"request_id": request_id, "to_position_id": reroute_position_id},
    )
    return get_delegation_request(conn, request_id)


def create_handoff(
    conn: sqlite3.Connection,
    *,
    from_assignment_id: str,
    to_assignment_id: str,
    packet: dict[str, Any],
    actor: str,
    review_required: bool = True,
) -> dict[str, Any]:
    """Packet-only handoff between assignments (no ambient conversation context)."""
    src = get_assignment(conn, from_assignment_id)
    dst = get_assignment(conn, to_assignment_id)
    if src["work_item_id"] != dst["work_item_id"]:
        raise AuthzDeniedError("handoff work items must match")
    if src["actor_id"] == dst["actor_id"] and review_required:
        append_audit_event(
            conn,
            event_type="delegation.self_review_denied",
            actor=actor,
            anomaly_code=AnomalyCode.A2,
            payload={
                "from_assignment_id": from_assignment_id,
                "to_assignment_id": to_assignment_id,
            },
        )
        raise AuthzDeniedError("self-review forbidden")
    src_seat = get_provider_seat(conn, src["provider_seat_id"])
    dst_seat = get_provider_seat(conn, dst["provider_seat_id"])
    if review_required and src_seat["provider"] == dst_seat["provider"]:
        raise AuthzDeniedError(
            "independent review requires a distinct provider principal"
        )
    if not isinstance(packet, dict):
        raise ValidationFailedError("handoff packet must be an object")
    # Packet-only: reject keys that smuggle ambient conversation context.
    forbidden_keys = {"conversation_id", "chat_history", "ambient_context"}
    if forbidden_keys & set(packet):
        raise ValidationFailedError("handoff must be packet-only")
    packet_hash = stable_digest(packet)
    handoff_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO handoffs (
            id, organization_id, from_assignment_id, to_assignment_id,
            work_item_id, packet_json, packet_sha256, review_required, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            handoff_id,
            src["organization_id"],
            from_assignment_id,
            to_assignment_id,
            src["work_item_id"],
            json.dumps(packet, sort_keys=True),
            packet_hash,
            1 if review_required else 0,
            now,
        ),
    )
    append_audit_event(
        conn,
        event_type="delegation.handoff_created",
        actor=actor,
        payload={"handoff_id": handoff_id, "packet_sha256": packet_hash},
    )
    return get_handoff(conn, handoff_id)


def get_handoff(conn: sqlite3.Connection, handoff_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"handoff not found: {handoff_id}")
    data = _row_to_dict(row)
    data["packet"] = json.loads(data.pop("packet_json"))
    data["review_required"] = bool(data["review_required"])
    return data


def accept_handoff_evidence(
    conn: sqlite3.Connection,
    *,
    handoff_id: str,
    actor: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    handoff = get_handoff(conn, handoff_id)
    evidence = evidence or {}
    if handoff["review_required"]:
        src = get_assignment(conn, handoff["from_assignment_id"])
        dst = get_assignment(conn, handoff["to_assignment_id"])
        src_seat = get_provider_seat(conn, src["provider_seat_id"])
        dst_seat = get_provider_seat(conn, dst["provider_seat_id"])
        implementation = _review_identity_from_pin(
            conn,
            pin_id=(evidence.get("implementation") or {}).get("dispatch_pin_id"),
            assignment_id=src["id"],
            provider=src_seat["provider"],
            seat_id=src_seat["id"],
        )
        review = _review_identity_from_pin(
            conn,
            pin_id=(evidence.get("review") or {}).get("dispatch_pin_id"),
            assignment_id=dst["id"],
            provider=dst_seat["provider"],
            seat_id=dst_seat["id"],
        )
        assert_review_separation(implementation=implementation, review=review)
    now = utc_now_iso()
    conn.execute(
        "UPDATE handoffs SET accepted_at = ? WHERE id = ?",
        (now, handoff_id),
    )
    # Only a reviewed child-to-its-parent handoff can satisfy the canonical
    # parent/child closure row. Other handoffs remain informational.
    if handoff["review_required"] and src["parent_assignment_id"] == dst["id"]:
        conn.execute(
            """
            UPDATE child_closure_evidence
            SET status = 'accepted', handoff_id = ?, evidence_json = ?, updated_at = ?
            WHERE parent_assignment_id = ? AND child_assignment_id = ?
            """,
            (
                handoff_id,
                json.dumps(evidence, sort_keys=True),
                now,
                handoff["to_assignment_id"],
                handoff["from_assignment_id"],
            ),
        )
    append_audit_event(
        conn,
        event_type="delegation.handoff_accepted",
        actor=actor,
        payload={"handoff_id": handoff_id},
    )
    return get_handoff(conn, handoff_id)


def _review_identity_from_pin(
    conn: sqlite3.Connection,
    *,
    pin_id: str | None,
    assignment_id: str,
    provider: str,
    seat_id: str,
) -> dict[str, Any]:
    if not pin_id:
        raise AuthzDeniedError("review separation missing dispatch_pin_id")
    row = conn.execute(
        """
        SELECT p.attempt_id, p.invocation_id
        FROM immutable_dispatch_pins p
        JOIN task_grants g ON g.grant_id = p.grant_id
        WHERE p.id = ? AND g.assignment_id = ?
        """,
        (pin_id, assignment_id),
    ).fetchone()
    if row is None:
        raise AuthzDeniedError("dispatch pin is not bound to review assignment")
    if not row["attempt_id"] or not row["invocation_id"]:
        raise AuthzDeniedError("dispatch pin lacks attempt or invocation identity")
    return {
        "provider": provider,
        "seat_id": seat_id,
        "attempt_id": row["attempt_id"],
        "invocation_id": row["invocation_id"],
    }


def mint_task_grant(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    principal_id: str,
    role: PrincipalRole,
    surfaces: tuple[Surface, ...],
    providers: tuple[str, ...],
    budget_scope_id: str,
    assignment_id: str,
    actor: str,
    capabilities: tuple[str, ...] = (),
    policy_revision: str | None = None,
) -> ResolvedTaskGrant:
    org = get_organization_profile(conn, organization_id)
    assignment = get_assignment(conn, assignment_id)
    if assignment["organization_id"] != organization_id:
        raise AuthzDeniedError("assignment organization mismatch")
    snapshot = materialize_snapshot(
        conn,
        organization_id=organization_id,
        loadout_id=assignment["loadout_id"],
        actor=actor,
    )
    grant_id = new_id()
    now = utc_now_iso()
    revision = policy_revision or org["policy_revision"]
    conn.execute(
        """
        INSERT INTO task_grants (
            id, organization_id, grant_id, principal_id, role, surfaces_json,
            providers_json, budget_scope_id, assignment_id, loadout_id,
            snapshot_id, capabilities_json, effect_ceiling, policy_revision,
            compatibility_mode, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'r3_resolved', ?)
        """,
        (
            new_id(),
            organization_id,
            grant_id,
            principal_id,
            str(role),
            json.dumps([str(s) for s in surfaces]),
            json.dumps(list(providers)),
            budget_scope_id,
            assignment_id,
            assignment["loadout_id"],
            snapshot["id"],
            json.dumps(list(capabilities)),
            snapshot["resolution"].get("effect_ceiling") or "",
            revision,
            now,
        ),
    )
    grant = ResolvedTaskGrant(
        grant_id=grant_id,
        principal_id=principal_id,
        role=role,
        surfaces=surfaces,
        providers=providers,
        budget_scope_id=budget_scope_id,
        organization_id=organization_id,
        organization_profile_hash=org["content_sha256"],
        loadout_id=assignment["loadout_id"],
        snapshot_id=snapshot["id"],
        assignment_id=assignment_id,
        capabilities=capabilities,
        policy_revision=revision,
        effect_ceiling=str(snapshot["resolution"].get("effect_ceiling") or ""),
    )
    append_audit_event(
        conn,
        event_type="delegation.task_grant_minted",
        actor=actor,
        payload={"grant_id": grant_id, "snapshot_id": snapshot["id"]},
    )
    return grant


def assert_review_separation(
    *,
    implementation: dict[str, Any],
    review: dict[str, Any],
) -> None:
    """Require distinct provider principal, seat, invocation, and attempt."""
    checks = (
        ("provider", implementation.get("provider"), review.get("provider")),
        ("seat_id", implementation.get("seat_id"), review.get("seat_id")),
        ("invocation_id", implementation.get("invocation_id"), review.get("invocation_id")),
        ("attempt_id", implementation.get("attempt_id"), review.get("attempt_id")),
    )
    for label, left, right in checks:
        if not left or not right:
            raise AuthzDeniedError(f"review separation missing {label}")
        if left == right:
            raise AuthzDeniedError(f"self-review / shared {label} forbidden")


def create_dispatch_pin(
    conn: sqlite3.Connection,
    *,
    grant: ResolvedTaskGrant,
    packet_hash: str,
    actor: str,
    run_id: str | None = None,
    attempt_id: str | None = None,
    invocation_id: str | None = None,
    expected_snapshot_hash: str | None = None,
) -> dict[str, Any]:
    snapshot = get_snapshot(conn, grant.snapshot_id)
    org = get_organization_profile(conn, grant.organization_id)
    # Fail closed if org or snapshot drifted.
    if org["content_sha256"] != grant.organization_profile_hash:
        raise StaleAssetError("organization profile hash drifted")
    if expected_snapshot_hash and snapshot["content_sha256"] != expected_snapshot_hash:
        raise StaleAssetError("resolved loadout snapshot stale")
    # Re-resolve and compare loadout/member hashes.
    live = resolve_loadout(
        loadout_id=grant.loadout_id,
        organization_profile=org,
        expected_loadout_hash=snapshot["loadout_hash"],
        expected_member_hashes=snapshot["member_asset_hashes"],
    )
    if live["policy_hash"] != snapshot["policy_hash"]:
        raise StaleAssetError("policy hash drifted")

    pin = {
        "policy_identity": snapshot["policy_identity"],
        "policy_hash": snapshot["policy_hash"],
        "organization_profile_identity": grant.organization_id,
        "organization_profile_hash": grant.organization_profile_hash,
        "loadout_identity": grant.loadout_id,
        "loadout_hash": snapshot["loadout_hash"],
        "member_asset_hashes": snapshot["member_asset_hashes"],
        "packet_hash": packet_hash,
        "budget_identity": grant.budget_scope_id,
        "grant_identity": grant.grant_id,
        "attempt_id": attempt_id,
        "invocation_id": invocation_id,
        "snapshot_id": grant.snapshot_id,
    }
    pin_hash = stable_digest(pin)
    pin_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO immutable_dispatch_pins (
            id, run_id, attempt_id, invocation_id, grant_id, snapshot_id,
            policy_identity, policy_hash, organization_profile_identity,
            organization_profile_hash, loadout_identity, loadout_hash,
            member_asset_hashes_json, packet_hash, budget_identity,
            grant_identity, pin_json, content_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pin_id,
            run_id,
            attempt_id,
            invocation_id,
            grant.grant_id,
            grant.snapshot_id,
            pin["policy_identity"],
            pin["policy_hash"],
            pin["organization_profile_identity"],
            pin["organization_profile_hash"],
            pin["loadout_identity"],
            pin["loadout_hash"],
            json.dumps(pin["member_asset_hashes"], sort_keys=True),
            packet_hash,
            grant.budget_scope_id,
            grant.grant_id,
            json.dumps(pin, sort_keys=True),
            pin_hash,
            now,
        ),
    )
    append_audit_event(
        conn,
        event_type="delegation.dispatch_pin_created",
        actor=actor,
        payload={"pin_id": pin_id, "grant_id": grant.grant_id},
    )
    return get_dispatch_pin(conn, pin_id)


def get_dispatch_pin(conn: sqlite3.Connection, pin_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM immutable_dispatch_pins WHERE id = ?", (pin_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"dispatch pin not found: {pin_id}")
    data = _row_to_dict(row)
    data["member_asset_hashes"] = json.loads(data.pop("member_asset_hashes_json"))
    data["pin"] = json.loads(data.pop("pin_json"))
    return data


def complete_assignment(
    conn: sqlite3.Connection,
    *,
    assignment_id: str,
    actor: str,
) -> dict[str, Any]:
    assert_parent_closure_allowed(conn, assignment_id)
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE assignments SET status = 'completed', updated_at = ?
        WHERE id = ? AND status = 'active'
        """,
        (now, assignment_id),
    )
    append_audit_event(
        conn,
        event_type="delegation.assignment_completed",
        actor=actor,
        payload={"assignment_id": assignment_id},
    )
    return get_assignment(conn, assignment_id)


def dispatch_delegated_assignment(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    actor_id: str,
    provider_seat_id: str,
    actor: str,
) -> dict[str, Any]:
    """Accept path: create child assignment from an accepted/rerouted request."""
    from flow_engine.application.organization_service import create_assignment

    req = get_delegation_request(conn, request_id)
    if req["status"] not in {"accepted", "rerouted"}:
        raise PrerequisiteError("delegation must be accepted or rerouted before dispatch")
    child = create_assignment(
        conn,
        organization_id=req["organization_id"],
        work_item_id=req["work_item_id"],
        position_id=req["to_position_id"],
        actor_id=actor_id,
        provider_seat_id=provider_seat_id,
        actor=actor,
        parent_assignment_id=req["parent_assignment_id"],
    )
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE delegation_requests SET status = 'dispatched', updated_at = ?
        WHERE id = ?
        """,
        (now, request_id),
    )
    append_audit_event(
        conn,
        event_type="delegation.dispatched",
        actor=actor,
        payload={"request_id": request_id, "assignment_id": child["id"]},
    )
    return {"request": get_delegation_request(conn, request_id), "assignment": child}
