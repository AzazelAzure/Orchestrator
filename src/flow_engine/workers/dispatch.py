"""Optional enqueue helpers (non-authoritative broker hints)."""

from __future__ import annotations

import os
from typing import Any


def enqueue_mock_provider_job(
    *,
    job_id: str,
    attempt_id: str,
    worker_principal_id: str | None = None,
) -> dict[str, Any]:
    """Enqueue Celery mock delivery when broker is configured."""
    if not os.environ.get("CELERY_BROKER_URL"):
        return {"enqueued": False, "reason": "CELERY_BROKER_URL not set"}
    broker = os.environ["CELERY_BROKER_URL"]
    if "://" in broker and "@" not in broker.split("://", 1)[1]:
        # redis://host without password — fail closed in non-test.
        if os.environ.get("ORCH_TESTING") != "1":
            return {"enqueued": False, "reason": "broker authentication required"}
    from flow_engine.workers.tasks import execute_mock_provider

    _ = worker_principal_id  # identity derived server-side
    async_result = execute_mock_provider.delay(
        job_id=job_id,
        attempt_id=attempt_id,
    )
    return {"enqueued": True, "task_id": async_result.id}


def enqueue_provider_job(
    *,
    provider: str,
    job_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    """Route async dispatch to its provider queue; mock requires explicit mode."""
    if provider not in {"codex", "cursor", "claude"}:
        return {"enqueued": False, "reason": "unsupported provider"}
    if os.environ.get("ORCH_PROVIDER_MODE") == "mock" or os.environ.get("ORCH_TESTING") == "1":
        return enqueue_mock_provider_job(job_id=job_id, attempt_id=attempt_id)
    if not os.environ.get("CELERY_BROKER_URL"):
        return {"enqueued": False, "reason": "CELERY_BROKER_URL not set"}
    broker = os.environ["CELERY_BROKER_URL"]
    if "://" in broker and "@" not in broker.split("://", 1)[1]:
        return {"enqueued": False, "reason": "broker authentication required"}
    from flow_engine.workers.tasks import (
        execute_provider_claude,
        execute_provider_codex,
        execute_provider_cursor,
    )

    task = {
        "codex": execute_provider_codex,
        "cursor": execute_provider_cursor,
        "claude": execute_provider_claude,
    }[provider]
    result = task.apply_async(
        kwargs={"job_id": job_id, "attempt_id": attempt_id},
        queue=f"provider-{provider}",
    )
    return {"enqueued": True, "task_id": result.id, "queue": f"provider-{provider}"}
