"""Celery application configuration with Asia/Manila Beat entries."""

from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

from flow_engine.schedules.templates import list_schedule_templates

broker_url = os.environ.get("CELERY_BROKER_URL", "")
result_backend = os.environ.get("CELERY_RESULT_BACKEND", "")

if not broker_url:
    # Allow import for unit tests without broker; tasks fail closed at enqueue.
    broker_url = "memory://"
    result_backend = "cache+memory://"

app = Celery("orchestrator_workers", broker=broker_url, backend=result_backend or None)


def _beat_schedule() -> dict:
    """Exact Celery Beat/cron entries for all 7 Asia/Manila templates."""
    entries: dict[str, dict] = {}
    for template in list_schedule_templates():
        schedule_id = template["schedule_id"]
        day_of_week = template.get("day_of_week")
        # Celery crontab day_of_week: 0=Sunday … 6=Saturday; templates use 0=Monday.
        celery_dow = None
        if day_of_week is not None:
            celery_dow = str((int(day_of_week) + 1) % 7)
        entries[schedule_id] = {
            "task": "flow_engine.workers.schedule_template_tick",
            "schedule": crontab(
                minute=int(template["minute"]),
                hour=int(template["hour"]),
                day_of_week=celery_dow if celery_dow is not None else "*",
            ),
            "kwargs": {"schedule_id": schedule_id},
            "options": {"queue": "scheduler"},
        }
    return entries


app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Manila",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="provider-mock",
    task_routes={
        "flow_engine.workers.execute_mock_provider": {"queue": "provider-mock"},
        "flow_engine.workers.execute_provider_codex": {"queue": "provider-codex"},
        "flow_engine.workers.execute_provider_cursor": {"queue": "provider-cursor"},
        "flow_engine.workers.execute_provider_claude": {"queue": "provider-claude"},
        "flow_engine.workers.cancel_provider_invocation": {"queue": "provider-control"},
        "flow_engine.workers.execute_registered_script": {"queue": "script-sandbox"},
        "flow_engine.workers.schedule_tick": {"queue": "scheduler"},
        "flow_engine.workers.schedule_template_tick": {"queue": "scheduler"},
        "flow_engine.workers.reject_repository_script": {"queue": "script-sandbox"},
    },
    beat_schedule=_beat_schedule(),
    broker_connection_retry_on_startup=True,
)
if os.environ.get("ORCH_R4D_SLOW_MOCK"):
    # R4D worker-loss probe: avoid hour-long Redis visibility hiding killed tasks.
    app.conf.broker_transport_options = {"visibility_timeout": 60}
app.autodiscover_tasks(["flow_engine.workers"], related_name="tasks")
