#!/usr/bin/env python3
"""Seed local-stress org hierarchy inside coordinator container (idempotent).

Prints JSON with org_id, assignment ids, position ids, actor/seat ids.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

for candidate in (Path("/app/src"), Path(__file__).resolve().parents[1] / "src"):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
        break

from flow_engine.application.organization_service import (  # noqa: E402
    add_actor,
    add_provider_seat,
    create_assignment,
    create_organization_profile,
    find_position,
    list_organization_profiles,
)
from flow_engine.persistence import Kernel  # noqa: E402
from flow_engine.persistence.transactions import transaction  # noqa: E402

ORG_NAME = "local-stress-org"


def _existing_org(conn):
    for profile in list_organization_profiles(conn):
        if profile.get("name") == ORG_NAME:
            return profile
    return None


def main() -> int:
    db_path = Path(os.environ.get("FLOW_DB_PATH", "/data/state.db"))
    work_item_id = os.environ.get("WORK_ITEM_ID", "").strip()
    if not work_item_id:
        print(json.dumps({"status": "error", "detail": "WORK_ITEM_ID required"}))
        return 1

    kernel = Kernel.init(db_path)
    try:
        conn = kernel.connection
        with transaction(conn):
            org = _existing_org(conn)
            if org is None:
                org = create_organization_profile(
                    conn, name=ORG_NAME, actor="local-seed"
                )
                impl_actor = add_actor(
                    conn,
                    organization_id=org["id"],
                    actor_key="impl",
                    display_name="Implementer",
                    actor="local-seed",
                )
                review_actor = add_actor(
                    conn,
                    organization_id=org["id"],
                    actor_key="reviewer",
                    display_name="Reviewer",
                    actor="local-seed",
                )
                impl_seat = add_provider_seat(
                    conn,
                    organization_id=org["id"],
                    actor_id=impl_actor["id"],
                    provider="cursor",
                    seat_key="impl-cursor",
                    actor="local-seed",
                )
                review_seat = add_provider_seat(
                    conn,
                    organization_id=org["id"],
                    actor_id=review_actor["id"],
                    provider="claude",
                    seat_key="review-claude",
                    actor="local-seed",
                )
            else:
                members = conn.execute(
                    """
                    SELECT a.id AS actor_id, a.actor_key, s.id AS seat_id, s.provider, s.seat_key
                    FROM actors a
                    LEFT JOIN provider_seats s ON s.actor_id = a.id
                    WHERE a.organization_id = ?
                    """,
                    (org["id"],),
                ).fetchall()
                impl_actor = {"id": None}
                review_actor = {"id": None}
                impl_seat = {"id": None}
                review_seat = {"id": None}
                for row in members:
                    if row["actor_key"] == "impl":
                        impl_actor["id"] = row["actor_id"]
                        if row["provider"] == "cursor":
                            impl_seat["id"] = row["seat_id"]
                    if row["actor_key"] == "reviewer":
                        review_actor["id"] = row["actor_id"]
                        if row["provider"] == "claude":
                            review_seat["id"] = row["seat_id"]

            manager = find_position(
                conn,
                organization_id=org["id"],
                department_key="tech",
                position_key="manager",
            )
            worker = find_position(
                conn,
                organization_id=org["id"],
                department_key="tech",
                position_key="worker",
            )
            supervisor = find_position(
                conn,
                organization_id=org["id"],
                department_key="tech",
                position_key="supervisor",
            )

            existing_assignment = conn.execute(
                """
                SELECT id FROM assignments
                WHERE organization_id = ? AND work_item_id = ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (org["id"], work_item_id),
            ).fetchone()

            if existing_assignment:
                parent_assignment_id = existing_assignment["id"]
            else:
                parent = create_assignment(
                    conn,
                    organization_id=org["id"],
                    work_item_id=work_item_id,
                    position_id=manager["id"],
                    actor_id=impl_actor["id"],
                    provider_seat_id=impl_seat["id"],
                    actor="local-seed",
                )
                parent_assignment_id = parent["id"]

        payload = {
            "status": "ok",
            "organization_id": org["id"],
            "organization_name": ORG_NAME,
            "work_item_id": work_item_id,
            "parent_assignment_id": parent_assignment_id,
            "manager_position_id": manager["id"],
            "worker_position_id": worker["id"],
            "supervisor_position_id": supervisor["id"],
            "impl_actor_id": impl_actor["id"],
            "impl_seat_id": impl_seat["id"],
            "review_actor_id": review_actor["id"],
            "review_seat_id": review_seat["id"],
            "departments": 3,
            "layers": 4,
        }
        print(json.dumps(payload, sort_keys=True))
        return 0
    finally:
        kernel.close()


if __name__ == "__main__":
    raise SystemExit(main())
