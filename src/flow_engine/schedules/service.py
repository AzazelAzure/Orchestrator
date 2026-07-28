"""Schedule tick/dedupe/no-overlap persistence and effect gates."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from flow_engine.application.artifact_service import register_artifact
from flow_engine.application.clock import utc_now_iso
from flow_engine.application.event_service import append_event
from flow_engine.application.finding_service import create_finding
from flow_engine.domain.errors import (
    BudgetExhaustedError,
    ConflictError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import FindingSeverity
from flow_engine.schedules.templates import (
    SCHEDULE_TIMEZONE,
    ScheduleTemplate,
    list_schedule_templates,
    require_schedule_template,
)
from flow_engine.script_sandbox.effects import (
    FORBIDDEN_SCRIPT_EFFECTS,
    assert_allowed_effects,
)
from flow_engine.script_sandbox.results_schema import validate_and_redact_script_results

# Exact cadence window: planned_time must match template minute; allow ±window.
PLANNED_TIME_WINDOW = timedelta(seconds=0)


def assert_schedule_effects(effects: list[dict[str, Any]] | None) -> None:
    assert_allowed_effects(effects)
    for item in effects or ():
        effect_type = str(item.get("type") or "")
        if effect_type in FORBIDDEN_SCRIPT_EFFECTS:
            raise UnsupportedSurfaceError(
                f"schedule must not produce {effect_type}"
            )


def planned_times_for_day(
    template: ScheduleTemplate,
    *,
    day: date,
    tz_name: str = SCHEDULE_TIMEZONE,
) -> list[str]:
    """Return ISO planned_time values for a calendar day in the schedule TZ."""
    if template.day_of_week is not None and day.weekday() != template.day_of_week:
        return []
    tz = ZoneInfo(tz_name)
    local = datetime.combine(day, time(template.hour, template.minute), tzinfo=tz)
    return [local.isoformat()]


def assert_planned_time_matches_cadence(
    template: ScheduleTemplate,
    planned_time: str,
    *,
    tz_name: str = SCHEDULE_TIMEZONE,
) -> datetime:
    """Validate planned_time against exact Asia/Manila template cadence/window."""
    tz = ZoneInfo(tz_name)
    try:
        parsed = datetime.fromisoformat(planned_time)
    except ValueError as exc:
        raise ValidationFailedError(f"invalid planned_time: {planned_time}") from exc
    if parsed.tzinfo is None:
        raise ValidationFailedError("planned_time must be timezone-aware")
    local = parsed.astimezone(tz)
    if template.day_of_week is not None and local.weekday() != template.day_of_week:
        raise ValidationFailedError(
            f"planned_time weekday mismatch for {template.schedule_id}"
        )
    expected = datetime.combine(
        local.date(), time(template.hour, template.minute), tzinfo=tz
    )
    delta = abs(local - expected)
    if delta > PLANNED_TIME_WINDOW:
        raise ValidationFailedError(
            f"planned_time {planned_time} outside exact cadence window "
            f"for {template.schedule_id} (expected {expected.isoformat()})"
        )
    # Canonical form: exact template local iso.
    return expected


def _active_run(conn: sqlite3.Connection, schedule_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM schedule_runs
        WHERE schedule_id = ? AND status IN ('claimed', 'running')
        ORDER BY claimed_at DESC LIMIT 1
        """,
        (schedule_id,),
    ).fetchone()


def claim_schedule_tick(
    conn: sqlite3.Connection,
    *,
    schedule_id: str,
    planned_time: str,
    actor: str,
    provider_call_budget: int | None = None,
) -> dict[str, Any]:
    template = require_schedule_template(schedule_id)
    if template.provider_call_budget != 0:
        raise BudgetExhaustedError("schedule templates must have zero provider-call budget")
    if provider_call_budget is not None and provider_call_budget != 0:
        raise BudgetExhaustedError("schedule provider-call budget must be zero")

    canonical = assert_planned_time_matches_cadence(template, planned_time)
    planned_time = canonical.isoformat()

    # Dedupe: (schedule_id, planned_time)
    existing = conn.execute(
        """
        SELECT * FROM schedule_runs
        WHERE schedule_id = ? AND planned_time = ?
        """,
        (schedule_id, planned_time),
    ).fetchone()
    if existing is not None:
        return {
            "run": _run_row(existing),
            "deduped": True,
            "from_cache": True,
        }

    # No-overlap / concurrency one
    if template.no_overlap or template.concurrency == 1:
        active = _active_run(conn, schedule_id)
        if active is not None:
            raise ConflictError(
                f"schedule {schedule_id} already has an active run (no-overlap)"
            )
        any_active = conn.execute(
            """
            SELECT id FROM schedule_runs
            WHERE status IN ('claimed', 'running')
            LIMIT 1
            """
        ).fetchone()
        if any_active is not None:
            raise ConflictError("global schedule concurrency is one; another run is active")

    run_id = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO schedule_runs (
            id, schedule_id, planned_time, status, actor, provider_call_budget,
            script_ids_json, result_json, claimed_at, completed_at, timezone
        ) VALUES (?, ?, ?, 'claimed', ?, 0, ?, NULL, ?, NULL, ?)
        """,
        (
            run_id,
            schedule_id,
            planned_time,
            actor,
            json.dumps(list(template.script_ids)),
            now,
            template.timezone,
        ),
    )
    append_event(
        conn,
        event_type="schedule.tick_claimed",
        actor=actor,
        payload={
            "schedule_id": schedule_id,
            "planned_time": planned_time,
            "run_id": run_id,
        },
    )
    row = conn.execute("SELECT * FROM schedule_runs WHERE id = ?", (run_id,)).fetchone()
    return {"run": _run_row(row), "deduped": False, "from_cache": False, "template": template.to_dict()}


def complete_schedule_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    actor: str,
    effects: list[dict[str, Any]] | None = None,
    script_results: list[dict[str, Any]] | None = None,
    attempt_remediation: bool = False,
    provider_calls: int = 0,
) -> dict[str, Any]:
    if attempt_remediation:
        raise UnsupportedSurfaceError(
            "scheduled results must not remediate or mutate repositories"
        )
    if provider_calls != 0:
        raise BudgetExhaustedError("schedule provider-call budget is zero")

    row = conn.execute("SELECT * FROM schedule_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValidationFailedError(f"unknown schedule run: {run_id}")
    if row["status"] not in {"claimed", "running"}:
        raise ConflictError(f"schedule run {run_id} is not active")

    template = require_schedule_template(row["schedule_id"])
    effects = list(effects or [])
    cleaned_results = validate_and_redact_script_results(script_results)

    if template.mode == "follow_up_candidate_only" and not effects:
        effects.append(
            {
                "type": "follow_up_work_candidate",
                "summary": "Skill-gap proposal candidate (not skill-gap-detection skill run)",
            }
        )

    assert_schedule_effects(effects)

    evidence_ids: list[str] = []
    finding_ids: list[str] = []
    for effect in effects:
        et = effect["type"]
        if et == "evidence":
            art = register_artifact(
                conn,
                uri=str(effect.get("uri") or f"orch://schedule/{run_id}/evidence"),
                artifact_type="schedule_evidence",
                sensitivity="internal",
                retention_class="standard",
                created_by=actor,
                content_hash=None,
            )
            evidence_ids.append(art["id"])
        elif et == "finding":
            finding = create_finding(
                conn,
                summary=str(effect.get("summary") or "schedule finding"),
                severity=FindingSeverity(str(effect.get("severity") or "low")),
                actor=actor,
            )
            finding_ids.append(finding["id"])
        elif et == "anomaly":
            append_event(
                conn,
                event_type="schedule.anomaly",
                actor=actor,
                payload={"run_id": run_id, "detail": effect.get("summary")},
            )
        elif et == "follow_up_work_candidate":
            append_event(
                conn,
                event_type="schedule.follow_up_candidate",
                actor=actor,
                payload={
                    "run_id": run_id,
                    "summary": effect.get("summary"),
                    "remediation": False,
                    "provider_calls": 0,
                },
            )

    result = {
        "effects": effects,
        "script_results": cleaned_results,
        "evidence_ids": evidence_ids,
        "finding_ids": finding_ids,
        "provider_calls": 0,
        "remediation": False,
    }
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE schedule_runs
        SET status = 'complete', result_json = ?, completed_at = ?
        WHERE id = ?
        """,
        (json.dumps(result), now, run_id),
    )
    append_event(
        conn,
        event_type="schedule.run_complete",
        actor=actor,
        payload={"run_id": run_id, "schedule_id": row["schedule_id"]},
    )
    updated = conn.execute("SELECT * FROM schedule_runs WHERE id = ?", (run_id,)).fetchone()
    return {"run": _run_row(updated), "result": result}


def list_schedule_status(conn: sqlite3.Connection) -> dict[str, Any]:
    templates = list_schedule_templates()
    runs = [
        _run_row(r)
        for r in conn.execute(
            """
            SELECT * FROM schedule_runs
            ORDER BY claimed_at DESC LIMIT 100
            """
        ).fetchall()
    ]
    return {
        "timezone": SCHEDULE_TIMEZONE,
        "templates": templates,
        "recent_runs": runs,
        "provider_call_budget": 0,
        "concurrency": 1,
        "no_overlap": True,
    }


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "schedule_id": row["schedule_id"],
        "planned_time": row["planned_time"],
        "status": row["status"],
        "actor": row["actor"],
        "provider_call_budget": row["provider_call_budget"],
        "script_ids": json.loads(row["script_ids_json"] or "[]"),
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "claimed_at": row["claimed_at"],
        "completed_at": row["completed_at"],
        "timezone": row["timezone"],
    }
