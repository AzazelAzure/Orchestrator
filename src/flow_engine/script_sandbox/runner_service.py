"""Networkless script-runner service: consume signed spool jobs, emit typed results."""

from __future__ import annotations

import json
import os
import sys
import time

from flow_engine.domain.errors import AuthzDeniedError, FlowError
from flow_engine.script_sandbox.attestation import require_authorized_image_digest
from flow_engine.script_sandbox.controller import process_runner_job
from flow_engine.script_sandbox.pins import testing_fixtures_enabled
from flow_engine.script_sandbox.spool import claim_job, list_pending_jobs, spool_root


def assert_runner_role() -> None:
    role = os.environ.get("ORCH_SCRIPT_ROLE", "").strip()
    if testing_fixtures_enabled():
        return
    if role != "script-runner":
        raise AuthzDeniedError("script-runner service requires ORCH_SCRIPT_ROLE=script-runner")
    # Fail closed if backend credentials were projected into the runner.
    forbidden = (
        "COORDINATOR_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "FLOW_DB_PATH",
        "REDIS_PASSWORD",
        "ORCH_WORKER_SERVICE_TOKEN",
        "ORCH_API_SERVICE_TOKEN",
        "ORCH_TOKEN_FOUNDER",
        "ORCH_TOKEN_WORKER",
    )
    present = [name for name in forbidden if os.environ.get(name)]
    if present:
        raise AuthzDeniedError(
            "script-runner must not receive backend credentials: "
            + ", ".join(present)
        )


def _log_runner_error(exc: FlowError | OSError) -> None:
    """Emit bounded diagnostic metadata; never include payloads or exception text."""
    event: dict[str, object] = {
        "event": "script_runner_job_error",
        "error_class": type(exc).__name__,
    }
    if isinstance(exc, FlowError):
        event["error_code"] = getattr(exc, "code", "FLOW_ERROR")
    else:
        event["errno"] = exc.errno
    print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)


def run_once() -> int:
    assert_runner_role()
    # Confirm authorized digest is available (attestation), without calling Docker.
    from flow_engine.script_sandbox.allowlist import SCRIPT_RUNNER_IMAGE_DIGEST

    require_authorized_image_digest(SCRIPT_RUNNER_IMAGE_DIGEST)
    _ = spool_root()
    processed = 0
    for path in list_pending_jobs():
        try:
            job = claim_job(path)
            process_runner_job(job)
            processed += 1
        except FlowError as exc:
            # Leave poison/quarantined jobs for operator inspection after claim failure;
            # claim_job removes valid claimed jobs and quarantines invalid ones.
            _log_runner_error(exc)
            continue
        except OSError as exc:
            _log_runner_error(exc)
            continue
    return processed


def main() -> None:
    assert_runner_role()
    poll = float(os.environ.get("ORCH_SCRIPT_RUNNER_POLL_SEC", "0.25"))
    while True:
        run_once()
        time.sleep(poll)


if __name__ == "__main__":
    main()
