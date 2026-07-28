"""Celery tasks for mock provider delivery, scripts, and schedules."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from flow_engine.control_plane.coordinator_client import CoordinatorClient
from flow_engine.coordinator.commands import CommandContext, RuntimeCommand
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.schedules.templates import SCHEDULE_TIMEZONE, require_schedule_template
from flow_engine.workers.celery_app import app


def _worker_client() -> CoordinatorClient:
    return CoordinatorClient(
        base_url=os.environ.get("COORDINATOR_URL", "http://coordinator:9001"),
        service_kind="worker",
    )


def _worker_context(principal_id: str = "worker") -> CommandContext:
    return CommandContext(
        principal_id=principal_id,
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=None,
    )


def _accept(
    command: RuntimeCommand, *, principal_token: str | None = None
) -> dict[str, Any]:
    return _worker_client().accept(command, principal_token=principal_token)


def _execute_socket_provider(
    *,
    provider: str,
    job_id: str,
    attempt_id: str,
    celery_task_id: str,
) -> dict[str, Any]:
    """Socket-only provider worker. Coordinator sees typed state commands only."""
    from flow_engine.providers.host_runner import (
        UnixSocketClient,
        authorize_provider_packet,
    )

    principal_id = f"worker.provider.{provider}"
    suffix = provider.upper()
    principal_token = os.environ[f"ORCH_TOKEN_WORKER_{suffix}"]
    host_token = os.environ[f"ORCH_HOST_RUNNER_TOKEN_{suffix}"]
    socket_path = Path(os.environ[f"ORCH_HOST_RUNNER_SOCKET_{suffix}"])
    ctx = _worker_context(principal_id)
    claim = _accept(
        RuntimeCommand(
            command_type="delivery.claim",
            target_id=job_id,
            payload={"job_id": job_id, "attempt_id": attempt_id, "celery_task_id": celery_task_id},
            idempotency_key=f"claim|{job_id}|{celery_task_id}",
            context=ctx,
        ),
        principal_token=principal_token,
    )
    if claim.get("status") == "rejected":
        return claim
    preflight_envelope = _accept(
        RuntimeCommand(
            command_type="runtime.worker_preflight",
            target_id=attempt_id,
            payload={
                "attempt_id": attempt_id,
                "delivery_job_id": job_id,
            },
            idempotency_key=f"preflight|{job_id}|{attempt_id}",
            context=ctx,
        ),
        principal_token=principal_token,
    )
    prepared = preflight_envelope["result"]["preflight"]
    if prepared["provider"] != provider:
        raise PermissionError("provider queue/binding mismatch")
    client = UnixSocketClient(provider, socket_path, host_token)
    try:
        authorize_provider_packet(prepared["payload"], provider)
        handshake = client.request("handshake")
        snapshot_envelope = _accept(
            RuntimeCommand(
                command_type="runtime.worker_snapshot",
                target_id=prepared["invocation_id"],
                payload={
                    "invocation_id": prepared["invocation_id"],
                    "provider": provider,
                    "snapshot": handshake["snapshot"],
                    "snapshot_digest": handshake["snapshot_digest"],
                },
                idempotency_key=f"snapshot|{prepared['invocation_id']}|{handshake['snapshot_digest']}",
                context=ctx,
            ),
            principal_token=principal_token,
        )
        if snapshot_envelope.get("status") != "applied":
            raise RuntimeError("adapter snapshot pin rejected")
    except Exception:
        return _accept(
            RuntimeCommand(
                command_type="runtime.worker_preflight_reject",
                target_id=attempt_id,
                payload={"attempt_id": attempt_id},
                idempotency_key=f"preflight-reject|{job_id}|{attempt_id}",
                context=ctx,
            ),
            principal_token=principal_token,
        )
    pinned = snapshot_envelope["result"]["snapshot"]
    packet_request = {
        "invocation_id": prepared["invocation_id"],
        "attempt_id": prepared["attempt_id"],
        "provider": provider,
        "credit_reservation_id": pinned["binding"]["credit_reservation_id"],
        "packet_digest": pinned["binding"]["packet_digest"],
        "snapshot_digest": handshake["snapshot_digest"],
        "binding_digest": pinned["binding_digest"],
        "task_packet": prepared["payload"],
        "cwd": ".",
    }
    try:
        client.request("validate_packet", **packet_request)
    except Exception:
        return _accept(
            RuntimeCommand(
                command_type="runtime.worker_preflight_reject",
                target_id=attempt_id,
                payload={"attempt_id": attempt_id},
                idempotency_key=f"packet-reject|{job_id}|{attempt_id}",
                context=ctx,
            ),
            principal_token=principal_token,
        )
    intent_envelope = _accept(
        RuntimeCommand(
            command_type="runtime.worker_prepare",
            target_id=attempt_id,
            payload={
                "attempt_id": attempt_id,
                "delivery_job_id": job_id,
                "lease_token": f"lease|{job_id}|{attempt_id}|{principal_id}",
            },
            idempotency_key=f"prepare|{job_id}|{attempt_id}",
            context=ctx,
        ),
        principal_token=principal_token,
    )
    prepared = intent_envelope["result"]["prepared"]
    if (
        prepared["invocation_id"] != pinned["invocation_id"]
        or prepared["provider"] != provider
    ):
        return _settle_provider_unknown(
            prepared=prepared, job_id=job_id, ctx=ctx, principal_token=principal_token
        )
    stop_heartbeat = threading.Event()

    def heartbeat_loop() -> None:
        while not stop_heartbeat.wait(5):
            try:
                host = client.request("heartbeat", invocation_id=prepared["invocation_id"])
                if not host.get("alive"):
                    return
                _accept(
                    RuntimeCommand(
                        command_type="delivery.heartbeat",
                        target_id=job_id,
                        payload={"job_id": job_id},
                        context=ctx,
                    ),
                    principal_token=principal_token,
                )
            except Exception:
                return

    heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    try:
        result = client.request(
            "invoke",
            **packet_request,
        )
    except Exception:
        try:
            result = client.request(
                "reconcile", invocation_id=prepared["invocation_id"]
            )
        except Exception:
            result = {"outcome": "outcome_unknown", "reconciliation_required": True}
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(6)
    provider_result = None if result.get("outcome") == "outcome_unknown" else {
        "outcome": result["outcome"],
        "evidence": {
            "provider_call_id": result.get("provider_call_id"),
            "truncated": bool(result.get("truncated")),
        },
        "anomalies": result.get("anomalies") or [],
        "delivery_id": str(result.get("provider_call_id") or prepared["invocation_id"]),
        "provider_call_id": result.get("provider_call_id"),
        "redacted_output": result.get("redacted_output", ""),
        "truncated": bool(result.get("truncated")),
        "snapshot_digest": handshake["snapshot_digest"],
        "binding_digest": result.get("binding_digest"),
    }
    return _accept(
        RuntimeCommand(
            command_type="runtime.worker_settle",
            target_id=attempt_id,
            payload={"prepared": prepared, "provider_result": provider_result},
            idempotency_key=f"settle|{job_id}|{attempt_id}",
            context=ctx,
        ),
        principal_token=principal_token,
    )


def _settle_provider_unknown(
    *,
    prepared: dict[str, Any],
    job_id: str,
    ctx: CommandContext,
    principal_token: str,
) -> dict[str, Any]:
    """Fail closed after dispatch intent; never leave an accepted attempt hanging."""
    return _accept(
        RuntimeCommand(
            command_type="runtime.worker_settle",
            target_id=prepared["attempt_id"],
            payload={"prepared": prepared, "provider_result": None},
            idempotency_key=f"settle|{job_id}|{prepared['attempt_id']}",
            context=ctx,
        ),
        principal_token=principal_token,
    )


@app.task(name="flow_engine.workers.cancel_provider_invocation", bind=True, max_retries=0)
def cancel_provider_invocation(
    self, *, provider: str, attempt_id: str
) -> dict[str, Any]:
    """Governed provider cancellation; socket ambiguity is recorded explicitly."""
    from flow_engine.providers.host_runner import UnixSocketClient

    _ = self
    if provider not in {"codex", "cursor", "claude"}:
        raise ValueError("unsupported provider")
    suffix = provider.upper()
    principal_id = f"worker.provider.{provider}"
    principal_token = os.environ[f"ORCH_TOKEN_WORKER_{suffix}"]
    ctx = _worker_context(principal_id)
    prepared = _accept(
        RuntimeCommand(
            command_type="runtime.worker_cancel_prepare",
            target_id=attempt_id,
            payload={"attempt_id": attempt_id},
            idempotency_key=f"cancel-prepare|{attempt_id}",
            context=ctx,
        ),
        principal_token=principal_token,
    )
    invocation_id = prepared["result"]["invocation"]["id"]
    client = UnixSocketClient(
        provider,
        Path(os.environ[f"ORCH_HOST_RUNNER_SOCKET_{suffix}"]),
        os.environ[f"ORCH_HOST_RUNNER_TOKEN_{suffix}"],
    )
    ambiguous = False
    try:
        cancelled = bool(
            client.request("cancel", invocation_id=invocation_id).get("cancelled")
        )
    except Exception:
        cancelled = False
        ambiguous = True
    return _accept(
        RuntimeCommand(
            command_type="runtime.worker_cancel_settle",
            target_id=invocation_id,
            payload={
                "invocation_id": invocation_id,
                "cancelled": cancelled,
                "ambiguous": ambiguous,
            },
            idempotency_key=f"cancel-settle|{invocation_id}",
            context=ctx,
        ),
        principal_token=principal_token,
    )


@app.task(name="flow_engine.workers.execute_mock_provider", bind=True, max_retries=3)
def execute_mock_provider(
    self,
    *,
    job_id: str,
    attempt_id: str,
    worker_principal_id: str | None = None,
) -> dict[str, Any]:
    """Idempotent mock provider execution via coordinator command boundary."""
    _ = worker_principal_id
    ctx = _worker_context()
    claim_envelope = _accept(
        RuntimeCommand(
            command_type="delivery.claim",
            target_id=job_id,
            payload={
                "job_id": job_id,
                "attempt_id": attempt_id,
                "celery_task_id": self.request.id,
            },
            idempotency_key=f"claim|{job_id}|{self.request.id}",
            context=ctx,
        )
    )
    if claim_envelope.get("status") == "rejected":
        return claim_envelope

    slow_sec = float(os.environ.get("ORCH_R4D_SLOW_MOCK", "0") or "0")
    if slow_sec > 0:
        time.sleep(slow_sec)

    _accept(
        RuntimeCommand(
            command_type="delivery.heartbeat",
            target_id=job_id,
            payload={"job_id": job_id},
            context=ctx,
        )
    )

    deliver_envelope = _accept(
        RuntimeCommand(
            command_type="runtime.worker_deliver",
            target_id=attempt_id,
            payload={"attempt_id": attempt_id, "delivery_job_id": job_id},
            idempotency_key=f"deliver|{job_id}|{attempt_id}",
            context=ctx,
        )
    )
    return deliver_envelope


@app.task(name="flow_engine.workers.execute_provider_codex", bind=True, max_retries=0)
def execute_provider_codex(self, *, job_id: str, attempt_id: str) -> dict[str, Any]:
    return _execute_socket_provider(
        provider="codex", job_id=job_id, attempt_id=attempt_id,
        celery_task_id=str(self.request.id),
    )


@app.task(name="flow_engine.workers.execute_provider_cursor", bind=True, max_retries=0)
def execute_provider_cursor(self, *, job_id: str, attempt_id: str) -> dict[str, Any]:
    return _execute_socket_provider(
        provider="cursor", job_id=job_id, attempt_id=attempt_id,
        celery_task_id=str(self.request.id),
    )


@app.task(name="flow_engine.workers.execute_provider_claude", bind=True, max_retries=0)
def execute_provider_claude(self, *, job_id: str, attempt_id: str) -> dict[str, Any]:
    return _execute_socket_provider(
        provider="claude", job_id=job_id, attempt_id=attempt_id,
        celery_task_id=str(self.request.id),
    )


@app.task(name="flow_engine.workers.recover_deliveries")
def recover_deliveries(*, stale_before_iso: str) -> dict[str, Any]:
    """Recovery is founder-authorized; workers must not invoke recover commands."""
    return {
        "status": "rejected",
        "error_code": "AUTHZ_DENIED",
        "error": "worker recovery denied; founder recovery.control_plane required",
        "stale_before_iso": stale_before_iso,
    }


@app.task(name="flow_engine.workers.execute_registered_script", bind=True, max_retries=1)
def execute_registered_script(
    self,
    *,
    execution_id: str,
) -> dict[str, Any]:
    """Run allowlisted script in script-worker; state via authenticated coordinator."""
    from flow_engine.application.script_delivery import run_script_worker_cycle

    _ = self
    return run_script_worker_cycle(
        _worker_client(),
        execution_id=execution_id,
        worker_context=_worker_context(),
    )


def _scheduler_client() -> CoordinatorClient:
    return CoordinatorClient(
        base_url=os.environ.get("COORDINATOR_URL", "http://coordinator:9001"),
        service_kind="api",
    )


def _scheduler_token() -> str:
    token = os.environ.get("ORCH_TOKEN_SCHEDULER", "").strip()
    if token:
        return token
    if os.environ.get("ORCH_TESTING") == "1":
        from flow_engine.control_plane.bootstrap import bootstrap_test_token_for

        return bootstrap_test_token_for("scheduler")
    raise RuntimeError("ORCH_TOKEN_SCHEDULER required for schedule ticks")


@app.task(name="flow_engine.workers.schedule_tick")
def schedule_tick(
    *,
    schedule_id: str,
    planned_time: str,
) -> dict[str, Any]:
    """Scheduler principal tick — authenticated with scheduler token."""
    ctx = CommandContext(
        principal_id="scheduler",
        role=PrincipalRole.SYSTEM,
        surface=Surface.SCHEDULE,
        grant=None,
    )
    return _scheduler_client().accept(
        RuntimeCommand(
            command_type="schedule.tick",
            target_id=schedule_id,
            payload={
                "schedule_id": schedule_id,
                "planned_time": planned_time,
                "provider_call_budget": 0,
            },
            idempotency_key=f"tick|{schedule_id}|{planned_time}",
            context=ctx,
        ),
        principal_token=_scheduler_token(),
    )


@app.task(name="flow_engine.workers.schedule_template_tick")
def schedule_template_tick(*, schedule_id: str) -> dict[str, Any]:
    """Beat entrypoint: compute exact Asia/Manila planned_time for a template."""
    template = require_schedule_template(schedule_id)
    tz = ZoneInfo(SCHEDULE_TIMEZONE)
    now = datetime.now(tz)
    if template.day_of_week is not None and now.weekday() != template.day_of_week:
        return {
            "status": "skipped",
            "reason": "weekday_mismatch",
            "schedule_id": schedule_id,
        }
    planned = now.replace(
        hour=template.hour,
        minute=template.minute,
        second=0,
        microsecond=0,
    )
    # Exact window: only fire when wall-clock is on the cadence minute.
    if now.hour != template.hour or now.minute != template.minute:
        return {
            "status": "skipped",
            "reason": "outside_cadence_window",
            "schedule_id": schedule_id,
            "now": now.isoformat(),
            "planned_time": planned.isoformat(),
        }
    return schedule_tick(schedule_id=schedule_id, planned_time=planned.isoformat())


@app.task(name="flow_engine.workers.reject_repository_script")
def reject_repository_script_task(*, script_id: str) -> dict[str, Any]:
    """Celery surface must refuse repository scripts (negative control)."""
    from flow_engine.domain.errors import FlowError
    from flow_engine.script_sandbox.classify import reject_repository_script

    try:
        reject_repository_script(script_id)
    except FlowError as exc:
        return {
            "status": "rejected",
            "error_code": getattr(exc, "code", "UNSUPPORTED_SURFACE"),
            "error": str(exc),
            "script_id": script_id,
            "executable": False,
        }
    return {
        "status": "rejected",
        "error_code": "AUTHZ_DENIED",
        "error": "repository/registry runner must not execute via this task",
        "script_id": script_id,
    }
