"""Resource leases with advisory/strict claim policies and temporal expiry."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.clock import is_expired, utc_after_seconds, utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.application.idempotency import run_idempotent
from flow_engine.domain.errors import AdvisoryConflictError, ConflictError, NotFoundError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import ClaimPolicy, LeaseMode

DEFAULT_LEASE_DURATION_SEC = 3600
SYSTEM_ACTOR = "system"


def _validate_lease_duration(lease_duration_sec: int) -> None:
    if lease_duration_sec <= 0:
        raise ValueError("lease_duration_sec must be positive")


def _lease_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "resource_id": row["resource_id"],
        "holder": row["holder"],
        "mode": row["mode"],
        "acquired_at": row["acquired_at"],
        "expires_at": row["expires_at"],
        "released_at": row["released_at"],
        "revision": row["revision"],
    }


def _resource_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "claim_policy": row["claim_policy"],
        "revision": row["revision"],
    }


def _get_resource(conn: sqlite3.Connection, resource_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT id, kind, claim_policy, revision FROM resources WHERE id = ?",
        (resource_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"resource not found: {resource_id}")
    return _resource_row(row)


def _expire_lease_cas(
    conn: sqlite3.Connection,
    *,
    lease: dict[str, Any],
    actor: str = SYSTEM_ACTOR,
) -> dict[str, Any] | None:
    if lease.get("released_at"):
        return None

    resource = _get_resource(conn, lease["resource_id"])
    now = utc_now_iso()
    lease_revision = lease["revision"]
    resource_revision = resource["revision"]

    lease_cursor = conn.execute(
        """
        UPDATE leases
        SET released_at = ?, revision = revision + 1
        WHERE id = ? AND revision = ? AND released_at IS NULL
        """,
        (now, lease["id"], lease_revision),
    )
    if lease_cursor.rowcount != 1:
        return None

    resource_cursor = conn.execute(
        "UPDATE resources SET revision = revision + 1 WHERE id = ? AND revision = ?",
        (resource["id"], resource_revision),
    )
    if resource_cursor.rowcount != 1:
        raise ConflictError(f"compare-and-set failed for resource {resource['id']}")

    updated_lease = _lease_row(
        conn.execute("SELECT * FROM leases WHERE id = ?", (lease["id"],)).fetchone()
    )
    updated_resource = _get_resource(conn, resource["id"])
    append_event(
        conn,
        event_type="resource.lease_expired",
        actor=actor,
        payload={
            "lease_id": lease["id"],
            "resource_id": resource["id"],
            "prior_lease_revision": lease_revision,
            "resulting_lease_revision": updated_lease["revision"],
            "prior_resource_revision": resource_revision,
            "resulting_resource_revision": updated_resource["revision"],
            "expired_at": now,
        },
    )
    return updated_lease


def _expire_stale_leases(conn: sqlite3.Connection, resource_id: str) -> None:
    rows = conn.execute(
        """
        SELECT id, resource_id, holder, mode, acquired_at, expires_at, released_at, revision
        FROM leases
        WHERE resource_id = ? AND released_at IS NULL
        """,
        (resource_id,),
    ).fetchall()
    now = utc_now_iso()
    for row in rows:
        lease = _lease_row(row)
        if lease["expires_at"] and is_expired(lease["expires_at"], now_iso=now):
            _expire_lease_cas(conn, lease=lease)


def _active_lease(conn: sqlite3.Connection, resource_id: str) -> dict[str, Any] | None:
    _expire_stale_leases(conn, resource_id)
    row = conn.execute(
        """
        SELECT id, resource_id, holder, mode, acquired_at, expires_at, released_at, revision
        FROM leases
        WHERE resource_id = ? AND released_at IS NULL
        ORDER BY acquired_at DESC
        LIMIT 1
        """,
        (resource_id,),
    ).fetchone()
    if row is None:
        return None
    lease = _lease_row(row)
    if lease["expires_at"] and is_expired(lease["expires_at"]):
        return None
    return lease


def _release_lease(
    conn: sqlite3.Connection,
    *,
    lease_id: str,
    actor: str,
    reason: str | None = None,
) -> None:
    now = utc_now_iso()
    conn.execute(
        "UPDATE leases SET released_at = ? WHERE id = ? AND released_at IS NULL",
        (now, lease_id),
    )
    append_event(
        conn,
        event_type="resource.lease_released",
        actor=actor,
        payload={"lease_id": lease_id, "reason": reason},
    )


def ensure_resource(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    kind: str = "generic",
    claim_policy: ClaimPolicy = ClaimPolicy.STRICT,
    actor: str = "system",
) -> dict[str, Any]:
    try:
        return _get_resource(conn, resource_id)
    except NotFoundError:
        pass

    try:
        conn.execute(
            """
            INSERT INTO resources (id, kind, claim_policy, revision)
            VALUES (?, ?, ?, 0)
            """,
            (resource_id, kind, claim_policy),
        )
        append_event(
            conn,
            event_type="resource.registered",
            actor=actor,
            payload={"resource_id": resource_id, "kind": kind, "claim_policy": claim_policy},
        )
    except sqlite3.IntegrityError:
        pass

    return _get_resource(conn, resource_id)


def list_resources(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, kind, claim_policy, revision FROM resources ORDER BY id"
    ).fetchall()
    resources = [_resource_row(row) for row in rows]
    for resource in resources:
        resource["lease"] = _active_lease(conn, resource["id"])
    return resources


def show_resource(conn: sqlite3.Connection, resource_id: str) -> dict[str, Any]:
    resource = _get_resource(conn, resource_id)
    resource["lease"] = _active_lease(conn, resource_id)
    return resource


def claim_resource(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    kind: str = "generic",
    claim_policy: ClaimPolicy = ClaimPolicy.STRICT,
    force: bool = False,
    reason: str = "",
    actor: str | None = None,
    lease_duration_sec: int = DEFAULT_LEASE_DURATION_SEC,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    actor = actor or holder
    _validate_lease_duration(lease_duration_sec)

    def _claim() -> dict[str, Any]:
        resource = ensure_resource(
            conn,
            resource_id=resource_id,
            kind=kind,
            claim_policy=claim_policy,
            actor=actor,
        )
        lease = _active_lease(conn, resource_id)
        resource = _get_resource(conn, resource_id)

        if lease is not None and lease["holder"] != holder:
            policy = ClaimPolicy(resource["claim_policy"])
            if policy == ClaimPolicy.ADVISORY and not force:
                append_event(
                    conn,
                    event_type="resource.claim_rejected",
                    actor=actor,
                    payload={
                        "resource_id": resource_id,
                        "holder": holder,
                        "current_holder": lease["holder"],
                        "reason": "advisory_conflict",
                    },
                )
                raise AdvisoryConflictError(
                    f"resource {resource_id} held by {lease['holder']}; use --force to override"
                )
            if policy == ClaimPolicy.STRICT:
                append_event(
                    conn,
                    event_type="resource.claim_rejected",
                    actor=actor,
                    payload={
                        "resource_id": resource_id,
                        "holder": holder,
                        "current_holder": lease["holder"],
                        "reason": "strict_conflict",
                    },
                )
                raise ConflictError(
                    f"resource {resource_id} already held by {lease['holder']}"
                )
            if not reason.strip():
                raise ValueError("reason is required for advisory force replacement")
            _release_lease(conn, lease_id=lease["id"], actor=actor, reason=reason)

        if lease is not None and lease["holder"] == holder:
            return {**resource, "lease": lease, "renewed": False}

        revision = resource["revision"]
        cursor = conn.execute(
            """
            UPDATE resources SET revision = revision + 1
            WHERE id = ? AND revision = ?
            """,
            (resource_id, revision),
        )
        if cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for resource {resource_id}")

        now = utc_now_iso()
        lease_id = new_id()
        expires_at = utc_after_seconds(lease_duration_sec, from_iso=now)
        conn.execute(
            """
            INSERT INTO leases (
                id, resource_id, holder, mode, acquired_at, expires_at, released_at, revision
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, 0)
            """,
            (lease_id, resource_id, holder, LeaseMode.EXCLUSIVE, now, expires_at),
        )
        new_lease = _lease_row(
            conn.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        )
        append_event(
            conn,
            event_type="resource.claimed",
            actor=actor,
            payload={
                "resource_id": resource_id,
                "holder": holder,
                "force": force,
                "reason": reason,
                "expires_at": expires_at,
            },
            idempotency_key=idempotency_key,
        )
        return {**_get_resource(conn, resource_id), "lease": new_lease, "renewed": False}

    result, from_cache = run_idempotent(conn, idempotency_key, _claim)
    return {**result, "from_cache": from_cache}


def renew_resource(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    actor: str | None = None,
    lease_duration_sec: int = DEFAULT_LEASE_DURATION_SEC,
    expected_lease_revision: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    actor = actor or holder
    _validate_lease_duration(lease_duration_sec)

    def _renew() -> dict[str, Any]:
        resource = _get_resource(conn, resource_id)
        lease = _active_lease(conn, resource_id)
        if lease is None or lease["holder"] != holder:
            raise ConflictError(f"resource {resource_id} is not held by {holder}")

        if expected_lease_revision is not None and lease["revision"] != expected_lease_revision:
            raise ConflictError(
                f"lease revision mismatch for {resource_id}: expected {expected_lease_revision}, got {lease['revision']}"
            )

        resource_revision = resource["revision"]
        cursor = conn.execute(
            "UPDATE resources SET revision = revision + 1 WHERE id = ? AND revision = ?",
            (resource_id, resource_revision),
        )
        if cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for resource {resource_id}")

        expires_at = utc_after_seconds(lease_duration_sec)
        lease_cursor = conn.execute(
            """
            UPDATE leases
            SET expires_at = ?, revision = revision + 1
            WHERE id = ? AND revision = ? AND released_at IS NULL
            """,
            (expires_at, lease["id"], lease["revision"]),
        )
        if lease_cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for lease {lease['id']}")

        renewed = _lease_row(
            conn.execute("SELECT * FROM leases WHERE id = ?", (lease["id"],)).fetchone()
        )
        append_event(
            conn,
            event_type="resource.renewed",
            actor=actor,
            payload={"resource_id": resource_id, "holder": holder, "expires_at": expires_at},
            idempotency_key=idempotency_key,
        )
        return {**_get_resource(conn, resource_id), "lease": renewed, "renewed": True}

    result, from_cache = run_idempotent(conn, idempotency_key, _renew)
    return {**result, "from_cache": from_cache}


def release_resource(
    conn: sqlite3.Connection,
    *,
    resource_id: str,
    holder: str,
    expected_revision: int | None = None,
    expected_lease_revision: int | None = None,
    actor: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    actor = actor or holder

    def _release() -> dict[str, Any]:
        resource = _get_resource(conn, resource_id)
        lease = _active_lease(conn, resource_id)
        if lease is None:
            raise ConflictError(f"resource {resource_id} has no active lease")
        if lease["holder"] != holder:
            raise ConflictError(
                f"resource {resource_id} held by {lease['holder']}, not {holder}"
            )

        if expected_revision is not None and resource["revision"] != expected_revision:
            raise ConflictError(
                f"revision mismatch for {resource_id}: expected {expected_revision}, got {resource['revision']}"
            )
        if expected_lease_revision is not None and lease["revision"] != expected_lease_revision:
            raise ConflictError(
                f"lease revision mismatch for {resource_id}: expected {expected_lease_revision}, got {lease['revision']}"
            )

        cursor = conn.execute(
            "UPDATE resources SET revision = revision + 1 WHERE id = ? AND revision = ?",
            (resource_id, resource["revision"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for resource {resource_id}")

        lease_cursor = conn.execute(
            """
            UPDATE leases
            SET released_at = ?, revision = revision + 1
            WHERE id = ? AND revision = ? AND released_at IS NULL
            """,
            (utc_now_iso(), lease["id"], lease["revision"]),
        )
        if lease_cursor.rowcount != 1:
            raise ConflictError(f"compare-and-set failed for lease {lease['id']}")

        append_event(
            conn,
            event_type="resource.released",
            actor=actor,
            payload={"resource_id": resource_id, "holder": holder},
            idempotency_key=idempotency_key,
        )
        return _get_resource(conn, resource_id)

    result, from_cache = run_idempotent(conn, idempotency_key, _release)
    return {**result, "from_cache": from_cache}
