"""Script-worker controller: authenticated spool dispatch to networkless runner."""

from __future__ import annotations

import os
import uuid
from typing import Any

from flow_engine.domain.errors import AuthzDeniedError
from flow_engine.script_sandbox.allowlist import require_allowlist_entry
from flow_engine.script_sandbox.attestation import require_authorized_image_digest
from flow_engine.script_sandbox.pins import (
    assert_script_worker_controller_authority,
    testing_fixtures_enabled,
)
from flow_engine.script_sandbox.runner import (
    ScriptRunRequest,
    ScriptRunResult,
    run_allowlisted_script,
)
from flow_engine.script_sandbox.spool import (
    build_job_envelope,
    is_cancel_published,
    publish_cancel_for_job,
    read_result,
    spool_configured,
    write_job,
)


def execute_script_job(request: ScriptRunRequest) -> ScriptRunResult:
    """Controller entry used by script-worker / testing delivery paths.

    Production: write signed job to spool, await typed result from script-runner.
    When spool is configured, cancel_check publishes an authenticated cancel envelope
    bound to job/execution/nonce for the networkless runner to observe.
    ORCH_TESTING without spool: in-process runner fixtures.
    """
    assert_script_worker_controller_authority()
    entry = require_allowlist_entry(request.script_id)
    expected_image = request.expected_image_digest or entry.image_digest
    require_authorized_image_digest(expected_image)
    if (
        request.expected_executable_digest
        and request.expected_executable_digest != entry.executable_digest
    ):
        raise AuthzDeniedError("executable digest mismatch")
    if request.expected_image_digest and request.expected_image_digest != entry.image_digest:
        raise AuthzDeniedError("image digest mismatch")

    # Prefer spool whenever configured (including ORCH_TESTING integration paths).
    if not spool_configured():
        if testing_fixtures_enabled():
            return run_allowlisted_script(request)
        raise AuthzDeniedError(
            "ORCH_SCRIPT_SPOOL_DIR required for script-worker dispatch "
            "(subprocess must not run in networked controller)"
        )

    job_id = request.job_id or str(uuid.uuid4())
    execution_id = request.execution_id or ""
    envelope = build_job_envelope(
        job_id=job_id,
        execution_id=execution_id,
        script_id=entry.script_id,
        argv=entry.argv,
        input_json=dict(request.input_json or {}),
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=entry.timeout_sec,
    )
    write_job(envelope)

    cancel_published = False

    def _on_poll() -> None:
        nonlocal cancel_published
        if cancel_published:
            return
        if request.cancel_check is not None and request.cancel_check():
            publish_cancel_for_job(
                job_id=job_id,
                execution_id=execution_id,
                job_nonce=str(envelope["nonce"]),
            )
            cancel_published = True

    # Publish immediately if cancel already requested before wait.
    _on_poll()

    wait_timeout = float(entry.timeout_sec) + 30.0
    override = os.environ.get("ORCH_SCRIPT_SPOOL_WAIT_SEC", "").strip()
    if override:
        wait_timeout = float(override)
    result_env = read_result(
        job_id,
        expected_nonce=str(envelope["nonce"]),
        wait_timeout_sec=wait_timeout,
        on_poll=_on_poll,
    )
    result = ScriptRunResult.from_dict(result_env["result"])
    if result.image_digest != entry.image_digest:
        raise AuthzDeniedError("spool result image digest mismatch")
    if result.executable_digest != entry.executable_digest:
        raise AuthzDeniedError("spool result executable digest mismatch")
    # If cancel was durable-published, surface cancelled even if a race result arrived.
    if cancel_published or is_cancel_published(
        job_id=job_id,
        job_nonce=str(envelope["nonce"]),
        execution_id=execution_id,
    ):
        if result.status != "cancelled":
            result = ScriptRunResult(
                script_id=result.script_id,
                status="cancelled",
                argv=result.argv,
                executable_digest=result.executable_digest,
                image_digest=result.image_digest,
                output={},
                redacted_output=result.redacted_output,
                error_code="VALIDATION_FAILED",
                error="cancelled",
                bounded=result.bounded,
                network_attempted=result.network_attempted,
                hardening=result.hardening,
                pgid=result.pgid,
            )
    return result


def process_runner_job(job: dict[str, Any]) -> dict[str, Any]:
    """script-runner side: validate allowlist + execute subprocess for one job."""
    from flow_engine.script_sandbox.runner import run_allowlisted_script as _run
    from flow_engine.script_sandbox.spool import build_result_envelope, write_result

    job_id = str(job["job_id"])
    job_nonce = str(job["nonce"])
    execution_id = str(job.get("execution_id") or "")

    def _cancel_check() -> bool:
        return is_cancel_published(
            job_id=job_id,
            job_nonce=job_nonce,
            execution_id=execution_id,
        )

    request = ScriptRunRequest(
        script_id=str(job["script_id"]),
        input_json=dict(job.get("input_json") or {}),
        expected_executable_digest=str(job["executable_digest"]),
        expected_image_digest=str(job["image_digest"]),
        cancel_check=_cancel_check,
        execution_id=execution_id,
        job_id=job_id,
    )
    result = _run(request)
    result_dict = result.to_dict()
    envelope = build_result_envelope(
        job_id=job_id,
        result=result_dict,
        job_nonce=job_nonce,
    )
    write_result(envelope)
    return envelope
