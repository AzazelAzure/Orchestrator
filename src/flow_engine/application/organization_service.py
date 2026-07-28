"""R3 organization profiles, members, assignments, and snapshot persistence."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.application.loadout_resolution import (
    all_twelve_loadout_ids,
    resolve_loadout,
)
from flow_engine.coordinator.audit import append_audit_event
from flow_engine.coordinator.commands import stable_digest
from flow_engine.domain.errors import (
    AuthzDeniedError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import AnomalyCode

DEFAULT_DEPARTMENTS = (
    ("admin-ops", "Admin/Ops"),
    ("qa", "QA"),
    ("tech", "Tech"),
)

DEFAULT_LAYERS = (
    ("executive", 40),
    ("manager", 30),
    ("supervisor", 20),
    ("worker", 10),
)

POSITION_RANK = {
    "executive": 40,
    "manager": 30,
    "supervisor": 20,
    "worker": 10,
}


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def create_organization_profile(
    conn: sqlite3.Connection,
    *,
    name: str,
    actor: str,
    profile: dict[str, Any] | None = None,
    policy_revision: str = "r3-default",
) -> dict[str, Any]:
    if not name.strip():
        raise ValidationFailedError("organization profile name is required")
    now = utc_now_iso()
    profile_id = new_id()
    body = profile or {"name": name, "departments": [d[0] for d in DEFAULT_DEPARTMENTS]}
    digest = stable_digest(body)
    try:
        conn.execute(
            """
            INSERT INTO organization_profiles (
                id, name, version, content_sha256, policy_revision,
                profile_json, created_at, updated_at
            ) VALUES (?, ?, '0.1.0', ?, ?, ?, ?, ?)
            """,
            (profile_id, name, digest, policy_revision, json.dumps(body), now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"organization profile name exists: {name}") from exc

    for key, label in DEFAULT_DEPARTMENTS:
        conn.execute(
            """
            INSERT INTO departments (
                id, organization_id, department_key, name,
                authority_ceiling_json, created_at
            ) VALUES (?, ?, ?, ?, '{}', ?)
            """,
            (new_id(), profile_id, key, label, now),
        )
    for key, rank in DEFAULT_LAYERS:
        conn.execute(
            """
            INSERT INTO hierarchy_layers (
                id, organization_id, layer_key, rank,
                authority_ceiling_json, created_at
            ) VALUES (?, ?, ?, ?, '{}', ?)
            """,
            (new_id(), profile_id, key, rank, now),
        )

    # Materialize twelve department×position seats bound to catalog loadouts.
    for dept_key, _label in DEFAULT_DEPARTMENTS:
        dept = conn.execute(
            """
            SELECT id FROM departments
            WHERE organization_id = ? AND department_key = ?
            """,
            (profile_id, dept_key),
        ).fetchone()
        for position_key, _rank in DEFAULT_LAYERS:
            layer = conn.execute(
                """
                SELECT id FROM hierarchy_layers
                WHERE organization_id = ? AND layer_key = ?
                """,
                (profile_id, position_key),
            ).fetchone()
            loadout_id = f"loadout.{dept_key}.{position_key}"
            if loadout_id not in all_twelve_loadout_ids():
                raise ValidationFailedError(f"catalog missing loadout {loadout_id}")
            conn.execute(
                """
                INSERT INTO positions (
                    id, organization_id, department_id, hierarchy_layer_id,
                    position_key, loadout_id, authority_ceiling_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?)
                """,
                (new_id(), profile_id, dept["id"], layer["id"], position_key, loadout_id, now),
            )

    append_audit_event(
        conn,
        event_type="org.profile_created",
        actor=actor,
        payload={"organization_id": profile_id, "name": name},
    )
    return get_organization_profile(conn, profile_id)


def get_organization_profile(conn: sqlite3.Connection, organization_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM organization_profiles WHERE id = ?",
        (organization_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"organization profile not found: {organization_id}")
    data = _row_to_dict(row)
    data["profile"] = json.loads(data.pop("profile_json"))
    return data


def list_organization_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, name, version, content_sha256, policy_revision, created_at FROM organization_profiles ORDER BY name"
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def add_actor(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    actor_key: str,
    display_name: str,
    actor: str,
) -> dict[str, Any]:
    get_organization_profile(conn, organization_id)
    actor_id = new_id()
    now = utc_now_iso()
    try:
        conn.execute(
            """
            INSERT INTO actors (id, organization_id, actor_key, display_name, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (actor_id, organization_id, actor_key, display_name, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"actor exists: {actor_key}") from exc
    append_audit_event(
        conn,
        event_type="org.actor_added",
        actor=actor,
        payload={"organization_id": organization_id, "actor_id": actor_id},
    )
    return get_actor(conn, actor_id)


def get_actor(conn: sqlite3.Connection, actor_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM actors WHERE id = ?", (actor_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"actor not found: {actor_id}")
    return _row_to_dict(row)


def add_provider_seat(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    actor_id: str,
    provider: str,
    seat_key: str,
    actor: str,
) -> dict[str, Any]:
    get_organization_profile(conn, organization_id)
    bound_actor = get_actor(conn, actor_id)
    if bound_actor["organization_id"] != organization_id:
        raise AuthzDeniedError("actor organization mismatch")
    # Provider identity never grants authority — seat is a binding only.
    seat_id = new_id()
    now = utc_now_iso()
    try:
        conn.execute(
            """
            INSERT INTO provider_seats (
                id, organization_id, actor_id, provider, seat_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (seat_id, organization_id, actor_id, provider, seat_key, now),
        )
    except sqlite3.IntegrityError as exc:
        raise ConflictError(f"provider seat exists: {seat_key}") from exc
    append_audit_event(
        conn,
        event_type="org.provider_seat_added",
        actor=actor,
        payload={
            "organization_id": organization_id,
            "seat_id": seat_id,
            "provider": provider,
            "note": "provider identity is not authority",
        },
    )
    return get_provider_seat(conn, seat_id)


def get_provider_seat(conn: sqlite3.Connection, seat_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM provider_seats WHERE id = ?", (seat_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"provider seat not found: {seat_id}")
    return _row_to_dict(row)


def get_position(conn: sqlite3.Connection, position_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"position not found: {position_id}")
    return _row_to_dict(row)


def find_position(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    department_key: str,
    position_key: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT p.* FROM positions p
        JOIN departments d ON d.id = p.department_id
        WHERE p.organization_id = ? AND d.department_key = ? AND p.position_key = ?
        """,
        (organization_id, department_key, position_key),
    ).fetchone()
    if row is None:
        raise NotFoundError(
            f"position not found: {department_key}/{position_key}"
        )
    return _row_to_dict(row)


def list_members(conn: sqlite3.Connection, organization_id: str) -> dict[str, Any]:
    get_organization_profile(conn, organization_id)
    actors = [
        _row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM actors WHERE organization_id = ? ORDER BY actor_key",
            (organization_id,),
        ).fetchall()
    ]
    seats = [
        _row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM provider_seats WHERE organization_id = ? ORDER BY seat_key",
            (organization_id,),
        ).fetchall()
    ]
    positions = [
        _row_to_dict(r)
        for r in conn.execute(
            "SELECT * FROM positions WHERE organization_id = ? ORDER BY loadout_id",
            (organization_id,),
        ).fetchall()
    ]
    return {"actors": actors, "provider_seats": seats, "positions": positions}


def preview_loadout(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    loadout_id: str,
    actor: str,
) -> dict[str, Any]:
    org = get_organization_profile(conn, organization_id)
    resolution = resolve_loadout(loadout_id=loadout_id, organization_profile=org)
    append_audit_event(
        conn,
        event_type="org.loadout_preview",
        actor=actor,
        payload={"organization_id": organization_id, "loadout_id": loadout_id},
    )
    return resolution


def materialize_snapshot(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    loadout_id: str,
    actor: str,
    department_ceiling: dict[str, Any] | None = None,
    hierarchy_ceiling: dict[str, Any] | None = None,
    position_ceiling: dict[str, Any] | None = None,
    explicit_grant: dict[str, Any] | None = None,
) -> dict[str, Any]:
    org = get_organization_profile(conn, organization_id)
    resolution = resolve_loadout(
        loadout_id=loadout_id,
        organization_profile=org,
        department_ceiling=department_ceiling,
        hierarchy_ceiling=hierarchy_ceiling,
        position_ceiling=position_ceiling,
        explicit_grant=explicit_grant,
    )
    snapshot_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO resolved_loadout_snapshots (
            id, organization_id, loadout_id, organization_profile_hash,
            loadout_hash, policy_identity, policy_hash, member_asset_hashes_json,
            resolution_json, content_sha256, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot_id,
            organization_id,
            loadout_id,
            resolution["organization_profile_hash"],
            resolution["loadout_hash"],
            resolution["policy_identity"],
            resolution["policy_hash"],
            json.dumps(resolution["member_asset_hashes"], sort_keys=True),
            json.dumps(resolution, sort_keys=True),
            resolution["content_sha256"],
            now,
        ),
    )
    append_audit_event(
        conn,
        event_type="org.snapshot_materialized",
        actor=actor,
        payload={"snapshot_id": snapshot_id, "loadout_id": loadout_id},
    )
    return get_snapshot(conn, snapshot_id)


def get_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM resolved_loadout_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"resolved loadout snapshot not found: {snapshot_id}")
    data = _row_to_dict(row)
    data["member_asset_hashes"] = json.loads(data.pop("member_asset_hashes_json"))
    data["resolution"] = json.loads(data.pop("resolution_json"))
    return data


def create_assignment(
    conn: sqlite3.Connection,
    *,
    organization_id: str,
    work_item_id: str,
    position_id: str,
    actor_id: str,
    provider_seat_id: str,
    actor: str,
    parent_assignment_id: str | None = None,
) -> dict[str, Any]:
    get_organization_profile(conn, organization_id)
    position = get_position(conn, position_id)
    bound_actor = get_actor(conn, actor_id)
    seat = get_provider_seat(conn, provider_seat_id)
    if position["organization_id"] != organization_id:
        raise AuthzDeniedError("position organization mismatch")
    if bound_actor["organization_id"] != organization_id:
        raise AuthzDeniedError("actor organization mismatch")
    if seat["organization_id"] != organization_id:
        raise AuthzDeniedError("provider seat organization mismatch")
    if seat["actor_id"] != actor_id:
        raise AuthzDeniedError("provider seat is not bound to actor")
    if parent_assignment_id:
        parent = get_assignment(conn, parent_assignment_id)
        parent_pos = get_position(conn, parent["position_id"])
        if POSITION_RANK[position["position_key"]] >= POSITION_RANK[parent_pos["position_key"]]:
            append_audit_event(
                conn,
                event_type="org.assignment_denied_upward",
                actor=actor,
                anomaly_code=AnomalyCode.A2,
                payload={
                    "parent_assignment_id": parent_assignment_id,
                    "requested_position": position["position_key"],
                },
            )
            raise AuthzDeniedError("no upward authority / delegation")

    work = conn.execute(
        "SELECT id FROM work_items WHERE id = ?", (work_item_id,)
    ).fetchone()
    if work is None:
        raise NotFoundError(f"work item not found: {work_item_id}")

    assignment_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO assignments (
            id, organization_id, work_item_id, position_id, actor_id,
            provider_seat_id, loadout_id, parent_assignment_id, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
        """,
        (
            assignment_id,
            organization_id,
            work_item_id,
            position_id,
            actor_id,
            provider_seat_id,
            position["loadout_id"],
            parent_assignment_id,
            now,
            now,
        ),
    )
    if parent_assignment_id:
        conn.execute(
            """
            INSERT INTO child_closure_evidence (
                id, parent_assignment_id, child_assignment_id, status,
                evidence_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', '{}', ?, ?)
            """,
            (new_id(), parent_assignment_id, assignment_id, now, now),
        )
    append_audit_event(
        conn,
        event_type="org.assignment_created",
        actor=actor,
        payload={"assignment_id": assignment_id, "loadout_id": position["loadout_id"]},
    )
    return get_assignment(conn, assignment_id)


def get_assignment(conn: sqlite3.Connection, assignment_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"assignment not found: {assignment_id}")
    return _row_to_dict(row)


def assert_parent_closure_allowed(conn: sqlite3.Connection, assignment_id: str) -> None:
    """Parent assignment cannot complete while child evidence is unaccepted."""
    rows = conn.execute(
        """
        SELECT child_assignment_id, status FROM child_closure_evidence
        WHERE parent_assignment_id = ? AND status != 'accepted'
        """,
        (assignment_id,),
    ).fetchall()
    if rows:
        pending = [r["child_assignment_id"] for r in rows]
        raise AuthzDeniedError(
            f"parent closure blocked until child evidence accepted: {pending}"
        )
