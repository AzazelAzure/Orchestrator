"""Script execution delivery: state transitions in txn, subprocess outside txn.

Mirrors provider accept_worker_deliver: coordinator never runs subprocesses inside
SQLite transactions. Actual execution is script-worker authority only.
"""

from __future__ import annotations

from typing import Any

from flow_engine.application.worker_delivery import (
    _finalize_command,
    _lookup_cached_command,
    _ReservationConflict,
    _reserve_command,
)
from flow_engine.coordinator.authz import authorize_command
from flow_engine.coordinator.commands import RuntimeCommand
from flow_engine.coordinator.coordinator import StateCoordinator
from flow_engine.domain.errors import FlowError
from flow_engine.persistence.transactions import transaction
from flow_engine.script_sandbox.controller import execute_script_job
from flow_engine.script_sandbox.registry import (
    complete_script_execution,
    is_cancel_requested,
    start_script_execution,
)
from flow_engine.script_sandbox.runner import ScriptRunRequest


def accept_script_execute(
    coordinator: StateCoordinator,
    command: RuntimeCommand,
) -> dict[str, Any]:
    """Three-phase: start (txn) → controller/spool execute (outside) → complete (txn)."""
    execution_id = command.target_id or command.payload["execution_id"]
    actor = command.context.principal_id
    scope = command.idempotency_scope
    digest = command.request_digest

    started: dict[str, Any] | None = None
    command_id: str | None = None
    operation_id: str | None = None

    with transaction(coordinator.connection):
        cached = _lookup_cached_command(
            coordinator.connection, scope=scope, digest=digest
        )
        if cached is not None:
            return cached

        kind, caps = coordinator._lookup_principal_kind(actor)
        authorize_command(command, principal_kind=kind, capabilities=caps)

        try:
            operation_id, command_id = _reserve_command(
                coordinator.connection, command
            )
        except _ReservationConflict as exc:
            return exc.envelope

        try:
            started = start_script_execution(
                coordinator.connection,
                execution_id=execution_id,
                actor=actor,
            )
        except FlowError as exc:
            envelope = {
                "operation_id": operation_id,
                "command_type": "script.execute",
                "status": "rejected",
                "from_cache": False,
                "anomalies": [],
                "result": None,
                "error_code": getattr(exc, "code", "FLOW_ERROR"),
                "error": str(exc),
            }
            assert command_id is not None
            _finalize_command(
                coordinator.connection,
                command_id=command_id,
                envelope=envelope,
                status="rejected",
            )
            return envelope

        if started.get("cancelled_before_start") or started.get("already_terminal"):
            envelope = {
                "operation_id": operation_id,
                "command_type": "script.execute",
                "status": "applied",
                "from_cache": False,
                "anomalies": [],
                "result": started,
                "error_code": None,
            }
            assert command_id is not None
            _finalize_command(
                coordinator.connection,
                command_id=command_id,
                envelope=envelope,
                status="applied",
            )
            return envelope

    # Phase 2 — controller/spool OUTSIDE SQLite transaction (never inline on API).
    assert started is not None
    execution = started["execution"]

    def _cancel_check() -> bool:
        with transaction(coordinator.connection):
            return is_cancel_requested(coordinator.connection, execution_id)

    try:
        result = execute_script_job(
            ScriptRunRequest(
                script_id=execution["script_id"],
                input_json=started.get("input") or execution.get("input") or {},
                expected_executable_digest=execution["executable_digest"],
                expected_image_digest=execution["image_digest"],
                cancel_check=_cancel_check,
                execution_id=execution_id,
            )
        )
        result_dict = result.to_dict()
    except FlowError as exc:
        result_dict = {
            "script_id": execution["script_id"],
            "status": "rejected",
            "argv": [],
            "executable_digest": execution["executable_digest"],
            "image_digest": execution["image_digest"],
            "output": {},
            "redacted_output": "",
            "error_code": getattr(exc, "code", "FLOW_ERROR"),
            "error": str(exc),
            "bounded": True,
            "network_attempted": False,
            "hardening": {},
            "pgid": None,
        }

    # Phase 3 — settle in a fresh transaction.
    with transaction(coordinator.connection):
        completed = complete_script_execution(
            coordinator.connection,
            execution_id=execution_id,
            actor=actor,
            result=result_dict,
        )
        envelope = {
            "operation_id": operation_id,
            "command_type": "script.execute",
            "status": "applied",
            "from_cache": False,
            "anomalies": [],
            "result": completed,
            "error_code": None,
        }
        assert command_id is not None
        _finalize_command(
            coordinator.connection,
            command_id=command_id,
            envelope=envelope,
            status="applied",
        )
        return envelope


def run_script_worker_cycle(
    coordinator_client: Any,
    *,
    execution_id: str,
    worker_context: Any,
) -> dict[str, Any]:
    """Script-worker entry: start via transport, spool/execute, complete via transport."""
    start_envelope = coordinator_client.accept(
        RuntimeCommand(
            command_type="script.start",
            target_id=execution_id,
            payload={"execution_id": execution_id},
            idempotency_key=f"script-start|{execution_id}",
            context=worker_context,
        )
    )
    if start_envelope.get("status") == "rejected":
        return start_envelope
    started = start_envelope.get("result") or {}
    if started.get("cancelled_before_start") or started.get("already_terminal"):
        return start_envelope
    execution = started.get("execution") or {}
    if not execution:
        return start_envelope

    def _cancel_check() -> bool:
        show = coordinator_client.accept(
            RuntimeCommand(
                command_type="script.show",
                target_id=execution_id,
                payload={"execution_id": execution_id},
                context=worker_context,
            )
        )
        result = show.get("result") or {}
        body = result.get("execution") or result
        return bool(body.get("cancel_requested")) or body.get("status") == "cancelled"

    try:
        run_result = execute_script_job(
            ScriptRunRequest(
                script_id=execution["script_id"],
                input_json=started.get("input") or execution.get("input") or {},
                expected_executable_digest=execution["executable_digest"],
                expected_image_digest=execution["image_digest"],
                cancel_check=_cancel_check,
                execution_id=execution_id,
            )
        )
        result_dict = run_result.to_dict()
    except FlowError as exc:
        result_dict = {
            "script_id": execution["script_id"],
            "status": "rejected",
            "argv": [],
            "executable_digest": execution["executable_digest"],
            "image_digest": execution["image_digest"],
            "output": {},
            "redacted_output": "",
            "error_code": getattr(exc, "code", "FLOW_ERROR"),
            "error": str(exc),
            "bounded": True,
            "network_attempted": False,
            "hardening": {},
            "pgid": None,
        }

    return coordinator_client.accept(
        RuntimeCommand(
            command_type="script.complete",
            target_id=execution_id,
            payload={"execution_id": execution_id, "result": result_dict},
            idempotency_key=f"script-complete|{execution_id}",
            context=worker_context,
        )
    )
