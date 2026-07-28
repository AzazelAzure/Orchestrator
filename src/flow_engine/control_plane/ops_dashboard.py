"""Read-only ops dashboard aggregation via coordinator (no direct DRF SQLite access)."""

from __future__ import annotations

import sqlite3
from typing import Any

from flow_engine.application.gate_service import list_gates
from flow_engine.application.organization_service import list_members, list_organization_profiles
from flow_engine.application.queue_service import list_queues
from flow_engine.application.work_service import list_work
from flow_engine.coordinator.audit import list_audit_events

# Installation gate register pointers (evidence paths as strings — no HQ filesystem reads).
OPEN_GATE_EVIDENCE: dict[str, str] = {
    "G-ORCH-LOCAL-CONTROL-PLANE": "programs/orchestrator-platform/agentic-control-plane/r4d_implementation_report.md",
    "G-ORCH-PROOF-GENERIC": "programs/orchestrator-platform/agentic-control-plane/discussions/r5-dogfood-proof-2026-07-28/gate_close_request.md",
    "G-ORCH-PROOF-PORTFOLIO": "programs/orchestrator-platform/agentic-control-plane/discussions/r6-portfolio-proof-2026-07-28/",
    "G-ORCH-VPS-LIVE": "programs/orchestrator-platform/discussions/shared-vps-hosting-2026-07-28/README.md",
    "G-ORCH-HOSTED-READY": "programs/orchestrator-platform/agentic-control-plane/discussions/hosted-entry-directorate-app-2026-07-28/tunnel_runbook.md",
}


def read_ops_dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    profiles = list_organization_profiles(conn)
    organization_id = profiles[0]["id"] if profiles else None
    hierarchy: dict[str, Any] = {"profiles": profiles, "members": {}}
    if organization_id:
        hierarchy["members"] = list_members(conn, organization_id)

    delegation_rows = conn.execute(
        """
        SELECT id, status, to_position_id, parent_assignment_id, created_at
        FROM delegation_requests
        WHERE status NOT IN ('declined', 'dispatched')
        ORDER BY created_at DESC
        LIMIT 25
        """
    ).fetchall()
    delegations = [dict(row) for row in delegation_rows]

    pin_rows = conn.execute(
        """
        SELECT id, run_id, grant_id, snapshot_id, created_at
        FROM immutable_dispatch_pins
        ORDER BY created_at DESC
        LIMIT 10
        """
    ).fetchall()
    dispatch_pins = [dict(row) for row in pin_rows]

    finding_count = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM findings
        WHERE status NOT IN ('resolved', 'closed', 'withdrawn')
        """
    ).fetchone()["cnt"]

    gates = list_gates(conn)
    open_gates = [
        {
            "gate_id": g.get("gate_type") or g.get("id"),
            "status": g.get("status"),
            "work_item_id": g.get("work_item_id"),
            "gate_type": g.get("gate_type"),
            "evidence_ref": OPEN_GATE_EVIDENCE.get(g.get("gate_type") or ""),
        }
        for g in gates
        if g.get("status") in {"open", "blocked", "pending", "required"}
    ]

    queues = list_queues(conn)
    recent_work = list_work(conn)[:15]
    audit = list_audit_events(conn, limit=20)

    return {
        "hierarchy": hierarchy,
        "delegations": {"open": delegations, "recent_pins": dispatch_pins},
        "queues": queues,
        "recent_work": recent_work,
        "findings": {"open_count": int(finding_count)},
        "open_gates": open_gates,
        "recent_audit": audit,
    }
