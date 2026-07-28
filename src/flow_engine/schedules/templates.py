"""Default Asia/Manila schedule templates (HD-ACP-013 / loadout-catalog §6)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

SCHEDULE_TIMEZONE = "Asia/Manila"

DEFAULT_TIMEOUT_SEC = 900
BACKUP_TIMEOUT_SEC = 1800


@dataclass(frozen=True)
class ScheduleTemplate:
    schedule_id: str
    name: str
    cadence: str  # cron-like description
    # day_of_week: 0=Monday … 6=Sunday; None = every day
    day_of_week: int | None
    hour: int
    minute: int
    script_ids: tuple[str, ...]
    timeout_sec: int = DEFAULT_TIMEOUT_SEC
    provider_call_budget: int = 0
    concurrency: int = 1
    no_overlap: bool = True
    dedupe_key: str = "schedule_id+planned_time"
    timezone: str = SCHEDULE_TIMEZONE
    findings_evidence_only: bool = True
    # Wednesday skill-gap is candidate-only (no skill-gap-detection skill run).
    mode: str = "scripts"  # scripts | follow_up_candidate_only

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["script_ids"] = list(self.script_ids)
        return data


_TEMPLATES: dict[str, ScheduleTemplate] = {
    t.schedule_id: t
    for t in (
        ScheduleTemplate(
            schedule_id="schedule.manila.daily.catalog_governance",
            name="Catalog/governance integrity",
            cadence="daily 01:30 Asia/Manila",
            day_of_week=None,
            hour=1,
            minute=30,
            script_ids=(
                "script.generic.catalog_integrity_sweep",
                "script.generic.governance_integrity_sweep",
            ),
        ),
        ScheduleTemplate(
            schedule_id="schedule.manila.daily.documentation",
            name="Documentation sweep",
            cadence="daily 02:00 Asia/Manila",
            day_of_week=None,
            hour=2,
            minute=0,
            script_ids=(
                "script.generic.documentation_link_sweep",
                "script.generic.documentation_metadata_sweep",
            ),
        ),
        ScheduleTemplate(
            schedule_id="schedule.manila.daily.stale_work",
            name="Stale work/gate/lease/attempt",
            cadence="daily 02:30 Asia/Manila",
            day_of_week=None,
            hour=2,
            minute=30,
            script_ids=("script.generic.stale_work_sweep",),
        ),
        ScheduleTemplate(
            schedule_id="schedule.manila.weekly.dependency_inventory",
            name="Dependency inventory",
            cadence="Monday 03:00 Asia/Manila",
            day_of_week=0,
            hour=3,
            minute=0,
            script_ids=("script.generic.dependency_manifest_inventory",),
        ),
        ScheduleTemplate(
            schedule_id="schedule.manila.weekly.secret_sweep",
            name="Security/secret sweep",
            cadence="Tuesday 03:00 Asia/Manila",
            day_of_week=1,
            hour=3,
            minute=0,
            script_ids=("script.generic.secret_pattern_scan",),
        ),
        ScheduleTemplate(
            schedule_id="schedule.manila.weekly.skill_gap_proposal",
            name="Skill-gap proposal (finding/candidate only)",
            cadence="Wednesday 03:00 Asia/Manila",
            day_of_week=2,
            hour=3,
            minute=0,
            script_ids=(),
            mode="follow_up_candidate_only",
        ),
        ScheduleTemplate(
            schedule_id="schedule.manila.weekly.backup_restore",
            name="Isolated backup/restore rehearsal",
            cadence="Sunday 03:00 Asia/Manila",
            day_of_week=6,
            hour=3,
            minute=0,
            script_ids=("script.generic.backup_restore_probe",),
            timeout_sec=BACKUP_TIMEOUT_SEC,
        ),
    )
}


def list_schedule_templates() -> list[dict[str, Any]]:
    return [t.to_dict() for t in sorted(_TEMPLATES.values(), key=lambda x: x.schedule_id)]


def get_schedule_template(schedule_id: str) -> ScheduleTemplate | None:
    return _TEMPLATES.get(schedule_id)


def require_schedule_template(schedule_id: str) -> ScheduleTemplate:
    from flow_engine.domain.errors import NotFoundError

    template = get_schedule_template(schedule_id)
    if template is None:
        raise NotFoundError(f"unknown schedule template: {schedule_id}")
    return template
