"""Engine work lookup capability."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.gate_service import list_gates
from flow_engine.capabilities.envelope import (
    CapabilityError,
    CapabilityRequest,
    CapabilityResult,
    EvidenceRef,
    ResultCode,
    redact_evidence_refs,
)


def _extract_evidence_refs(payload: dict[str, Any]) -> list[EvidenceRef]:
    raw_refs = payload.get("evidence_refs", [])
    if not isinstance(raw_refs, list):
        return []
    return redact_evidence_refs(raw_refs)


def lookup_work(
    conn: sqlite3.Connection,
    request: CapabilityRequest,
    *,
    engine_project_name: str | None = None,
) -> CapabilityResult:
    validation = request.validate()
    if validation is not None:
        return CapabilityResult.failure(request, validation)

    work_id = str(request.params.get("work_id", "")).strip()
    logical_work_id = str(request.params.get("logical_work_id", "")).strip()
    if not work_id and not logical_work_id:
        return CapabilityResult.failure(
            request,
            CapabilityError(
                ResultCode.INVALID_INPUT,
                "work_id or logical_work_id is required",
            ),
        )

    if work_id:
        rows = conn.execute(
            """
            SELECT w.id, w.queue_id, w.status, w.payload_json, w.claimed_by, w.revision,
                   q.name AS queue_name, q.project_id, p.name AS project_name
            FROM work_items w
            JOIN queues q ON q.id = w.queue_id
            JOIN projects p ON p.id = q.project_id
            WHERE w.id = ?
            """,
            (work_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT w.id, w.queue_id, w.status, w.payload_json, w.claimed_by, w.revision,
                   q.name AS queue_name, q.project_id, p.name AS project_name
            FROM work_items w
            JOIN queues q ON q.id = w.queue_id
            JOIN projects p ON p.id = q.project_id
            WHERE json_extract(w.payload_json, '$.logical_work_id') = ?
            """,
            (logical_work_id,),
        ).fetchall()

    if engine_project_name:
        rows = [row for row in rows if row["project_name"] == engine_project_name]

    if not rows:
        return CapabilityResult.failure(
            request,
            CapabilityError(ResultCode.NOT_FOUND, "work item not found"),
        )
    if len(rows) > 1:
        return CapabilityResult.failure(
            request,
            CapabilityError(
                ResultCode.AMBIGUOUS,
                "multiple work items matched lookup",
                {"matches": [row["id"] for row in rows]},
            ),
        )

    row = rows[0]
    payload = json.loads(row["payload_json"])
    evidence_refs = _extract_evidence_refs(payload)
    if any(ref.redacted for ref in evidence_refs):
        return CapabilityResult.failure(
            request,
            CapabilityError(
                ResultCode.RESTRICTED,
                "work item contains restricted evidence references",
            ),
            data={"work_id": row["id"]},
        )

    deps = conn.execute(
        "SELECT depends_on_id FROM work_dependencies WHERE work_item_id = ?",
        (row["id"],),
    ).fetchall()
    gates = list_gates(conn, work_item_id=row["id"])

    ancestry = {
        "depends_on": [dep["depends_on_id"] for dep in deps],
        "parent_work_item_id": payload.get("parent_work_item_id"),
        "root_work_item_id": payload.get("root_work_item_id"),
    }

    data: dict[str, Any] = {
        "work_id": row["id"],
        "logical_work_id": payload.get("logical_work_id"),
        "project_id": request.project_id,
        "engine_project_name": row["project_name"],
        "queue_name": row["queue_name"],
        "status": row["status"],
        "revision": row["revision"],
        "claimed_by": row["claimed_by"],
        "ancestry": ancestry,
        "gates": gates,
        "payload": {
            key: value
            for key, value in payload.items()
            if key not in {"evidence_refs"}
        },
    }
    return CapabilityResult.success(request, data=data, evidence_refs=tuple(evidence_refs))
