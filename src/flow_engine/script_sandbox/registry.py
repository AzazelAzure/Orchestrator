"""Durable script execution registry (coordinator-owned state transitions only).

Subprocess execution never runs here. Workers call start → run outside txn → complete.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.artifact_service import register_artifact
from flow_engine.application.clock import utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.application.finding_service import create_finding
from flow_engine.domain.errors import (
    AuthzDeniedError,
    ConflictError,
    NotFoundError,
    UnsupportedSurfaceError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import FindingSeverity
from flow_engine.script_sandbox.allowlist import require_allowlist_entry
from flow_engine.script_sandbox.classify import reject_repository_script
from flow_engine.script_sandbox.effects import assert_allowed_effects
from flow_engine.script_sandbox.runner import ScriptRunResult


def register_script_execution(
    conn: sqlite3.Connection,
    *,
    script_id: str,
    actor: str,
    input_json: dict[str, Any] | None = None,
    idempotency_key: str,
    expected_executable_digest: str | None = None,
    expected_image_digest: str | None = None,
    schedule_run_id: str | None = None,
) -> dict[str, Any]:
    reject_repository_script(script_id)
    entry = require_allowlist_entry(script_id)

    from flow_engine.script_sandbox.attestation import require_authorized_image_digest

    authorized = require_authorized_image_digest(entry.image_digest)
    if expected_image_digest and expected_image_digest != authorized:
        raise AuthzDeniedError("expected_image_digest not authorized by attestation")
    if expected_executable_digest and expected_executable_digest != entry.executable_digest:
        raise AuthzDeniedError("expected_executable_digest mismatch")

    existing = conn.execute(
        "SELECT * FROM script_executions WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if existing is not None:
        return {"execution": _exec_row(existing), "from_cache": True}

    active = conn.execute(
        """
        SELECT id FROM script_executions
        WHERE status IN ('registered', 'running')
        LIMIT 1
        """
    ).fetchone()
    if active is not None:
        raise ConflictError("script concurrency is one; another execution is active")

    exec_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO script_executions (
            id, script_id, status, actor, idempotency_key,
            executable_digest, image_digest, input_json, output_json,
            schedule_run_id, registered_at, started_at, completed_at, error_code,
            cancel_requested
        ) VALUES (?, ?, 'registered', ?, ?, ?, ?, ?, NULL, ?, ?, NULL, NULL, NULL, 0)
        """,
        (
            exec_id,
            script_id,
            actor,
            idempotency_key,
            expected_executable_digest or entry.executable_digest,
            expected_image_digest or authorized,
            json.dumps(input_json or {}),
            schedule_run_id,
            now,
        ),
    )
    append_event(
        conn,
        event_type="script.execution_registered",
        actor=actor,
        payload={"execution_id": exec_id, "script_id": script_id},
    )
    row = conn.execute("SELECT * FROM script_executions WHERE id = ?", (exec_id,)).fetchone()
    return {"execution": _exec_row(row), "from_cache": False, "entry": entry.to_dict()}


def start_script_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    actor: str,
) -> dict[str, Any]:
    """Authorize + transition registered → running. No subprocess."""
    row = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"script execution not found: {execution_id}")
    if row["status"] == "complete":
        return {"execution": _exec_row(row), "from_cache": True, "already_terminal": True}
    if int(row["cancel_requested"] or 0) == 1:
        conn.execute(
            """
            UPDATE script_executions
            SET status = 'cancelled', completed_at = ?, error_code = 'VALIDATION_FAILED'
            WHERE id = ?
            """,
            (utc_now_iso(), execution_id),
        )
        updated = conn.execute(
            "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
        ).fetchone()
        return {
            "execution": _exec_row(updated),
            "from_cache": False,
            "cancelled_before_start": True,
        }
    if row["status"] == "running":
        return {"execution": _exec_row(row), "from_cache": True, "already_running": True}
    if row["status"] != "registered":
        raise ConflictError(f"execution {execution_id} not startable: {row['status']}")

    reject_repository_script(row["script_id"])
    entry = require_allowlist_entry(row["script_id"])
    conn.execute(
        """
        UPDATE script_executions
        SET status = 'running', started_at = ?
        WHERE id = ?
        """,
        (utc_now_iso(), execution_id),
    )
    append_event(
        conn,
        event_type="script.execution_started",
        actor=actor,
        payload={"execution_id": execution_id, "script_id": row["script_id"]},
    )
    updated = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    return {
        "execution": _exec_row(updated),
        "from_cache": False,
        "entry": entry.to_dict(),
        "input": json.loads(row["input_json"] or "{}"),
    }


def complete_script_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    actor: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Persist typed worker result + effects. No subprocess."""
    row = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"script execution not found: {execution_id}")
    if row["status"] in {"complete", "failed", "cancelled", "timeout", "rejected"}:
        return {"execution": _exec_row(row), "from_cache": True, "result": json.loads(row["output_json"] or "{}")}

    typed = ScriptRunResult.from_dict(result)
    if typed.script_id != row["script_id"]:
        raise ConflictError("result script_id does not match execution")
    if typed.executable_digest != row["executable_digest"]:
        raise ConflictError("result executable digest does not match execution")
    if typed.image_digest != row["image_digest"]:
        raise ConflictError("result image digest does not match execution")

    from flow_engine.script_sandbox.results_schema import redact_failure_output

    # Redact failure/cap stdout/stderr before persistence.
    result = dict(result)
    if result.get("redacted_output"):
        result["redacted_output"] = redact_failure_output(str(result["redacted_output"]))
    typed = ScriptRunResult.from_dict(result)

    status = typed.status if typed.status in {
        "complete", "failed", "cancelled", "timeout", "rejected"
    } else "failed"
    # Durable cancel wins over complete/failed/timeout/rejected at settlement.
    if int(row["cancel_requested"] or 0) == 1 and status != "cancelled":
        status = "cancelled"
        result = {
            **typed.to_dict(),
            "status": "cancelled",
            "error_code": typed.error_code or "VALIDATION_FAILED",
            "error": typed.error or "cancelled",
            "output": {},
        }
        typed = ScriptRunResult.from_dict(result)

    effects = list((typed.output or {}).get("effects") or [])
    if typed.status == "complete":
        assert_allowed_effects(effects)
        _persist_effects(conn, actor=actor, effects=effects, execution_id=execution_id)

    conn.execute(
        """
        UPDATE script_executions
        SET status = ?, output_json = ?, completed_at = ?, error_code = ?
        WHERE id = ?
        """,
        (
            status,
            json.dumps(typed.to_dict()),
            utc_now_iso(),
            typed.error_code,
            execution_id,
        ),
    )
    append_event(
        conn,
        event_type="script.execution_finished",
        actor=actor,
        payload={
            "execution_id": execution_id,
            "status": status,
            "script_id": row["script_id"],
        },
    )
    updated = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    entry = require_allowlist_entry(row["script_id"])
    return {
        "execution": _exec_row(updated),
        "result": typed.to_dict(),
        "from_cache": False,
        "hardening": entry.hardening,
    }


def cancel_script_execution(
    conn: sqlite3.Connection,
    *,
    execution_id: str,
    actor: str,
) -> dict[str, Any]:
    """Durable cancel request observable by workers; terminate when still registered."""
    row = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"script execution not found: {execution_id}")
    if row["status"] in {"complete", "failed", "cancelled", "timeout", "rejected"}:
        return {"execution": _exec_row(row), "from_cache": True}

    conn.execute(
        """
        UPDATE script_executions
        SET cancel_requested = 1
        WHERE id = ?
        """,
        (execution_id,),
    )
    if row["status"] == "registered":
        conn.execute(
            """
            UPDATE script_executions
            SET status = 'cancelled', completed_at = ?, error_code = 'VALIDATION_FAILED'
            WHERE id = ?
            """,
            (utc_now_iso(), execution_id),
        )
    append_event(
        conn,
        event_type="script.execution_cancel_requested",
        actor=actor,
        payload={"execution_id": execution_id, "prior_status": row["status"]},
    )
    updated = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    return {"execution": _exec_row(updated), "cancel_requested": True}


def is_cancel_requested(conn: sqlite3.Connection, execution_id: str) -> bool:
    row = conn.execute(
        "SELECT cancel_requested, status FROM script_executions WHERE id = ?",
        (execution_id,),
    ).fetchone()
    if row is None:
        return False
    return int(row["cancel_requested"] or 0) == 1 or row["status"] == "cancelled"


def get_script_execution(conn: sqlite3.Connection, execution_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM script_executions WHERE id = ?", (execution_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"script execution not found: {execution_id}")
    return _exec_row(row)


def _persist_effects(
    conn: sqlite3.Connection,
    *,
    actor: str,
    effects: list[dict[str, Any]],
    execution_id: str,
) -> None:
    for effect in effects:
        et = effect["type"]
        if et in {
            "repair",
            "remediation",
            "repository_mutation",
            "merge",
            "deploy",
            "provider_call",
        }:
            raise UnsupportedSurfaceError(f"forbidden script effect: {et}")
        if et == "evidence":
            register_artifact(
                conn,
                uri=str(effect.get("uri") or f"orch://script/{execution_id}/evidence"),
                artifact_type="script_evidence",
                sensitivity="internal",
                retention_class="standard",
                created_by=actor,
            )
        elif et == "finding":
            create_finding(
                conn,
                summary=str(effect.get("summary") or "script finding"),
                severity=FindingSeverity(str(effect.get("severity") or "low")),
                actor=actor,
            )
        elif et == "anomaly":
            append_event(
                conn,
                event_type="script.anomaly",
                actor=actor,
                payload={"execution_id": execution_id, "detail": effect.get("summary")},
            )
        elif et == "follow_up_work_candidate":
            append_event(
                conn,
                event_type="script.follow_up_candidate",
                actor=actor,
                payload={"execution_id": execution_id, "summary": effect.get("summary")},
            )


def _exec_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = set(row.keys())
    return {
        "id": row["id"],
        "script_id": row["script_id"],
        "status": row["status"],
        "actor": row["actor"],
        "idempotency_key": row["idempotency_key"],
        "executable_digest": row["executable_digest"],
        "image_digest": row["image_digest"],
        "input": json.loads(row["input_json"] or "{}"),
        "output": json.loads(row["output_json"]) if row["output_json"] else None,
        "schedule_run_id": row["schedule_run_id"],
        "registered_at": row["registered_at"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "error_code": row["error_code"],
        "cancel_requested": bool(int(row["cancel_requested"])) if "cancel_requested" in keys else False,
    }
