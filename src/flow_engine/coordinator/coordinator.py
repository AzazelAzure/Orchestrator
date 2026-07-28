"""Sole-writer state coordinator: typed command boundary over SQLite."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.application.credit_service import credit_usage
from flow_engine.application.recovery_service import (
    reconstruct_eligible_deliveries,
    recover_after_restart,
    recover_worker_death,
    replay_delivery_hint,
)
from flow_engine.application.runtime_service import (
    begin_reconcile,
    cancel_run,
    claim_attempt,
    create_run,
    dispatch_provider_call,
    evaluate_timeouts,
    finish_reconcile,
    get_run,
    heartbeat_attempt,
    new_attempt_after_unknown,
    pause_run,
    preview_run,
    provider_limit_continue,
    provider_limit_halt,
    provider_limit_reroute,
    resume_run,
    submit_result,
)
from flow_engine.coordinator.audit import append_audit_event, list_audit_events
from flow_engine.coordinator.authz import authorize_command
from flow_engine.coordinator.commands import Grant, RuntimeCommand
from flow_engine.domain.errors import (
    AuthRequiredError,
    AuthzDeniedError,
    BudgetExhaustedError,
    FlowError,
    IdempotencyReplayError,
    NotFoundError,
    OutcomeUnknownError,
    PersistenceUnavailableError,
    PrerequisiteError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import AnomalyCode, PrincipalRole
from flow_engine.providers.protocol import ProviderRunner, default_mock_registry

# Explicit pure-observation commands whose no-key responses must reflect current
# state.  Mutation commands are intentionally absent and retain stable
# idempotency scopes.  Callers may still request cached observation semantics by
# supplying an explicit idempotency key.
FRESH_OBSERVATION_COMMANDS = frozenset(
    {
        "runtime.credit_usage",
        "runtime.list_audit",
        "runtime.show",
        "delivery.list_eligible",
        "delivery.get_by_invocation",
        "script.list_allowlist",
        "script.show",
        "schedule.list_templates",
        "schedule.status",
        "control_plane.resolve_token",
        "org.get_profile",
        "org.list_profiles",
        "org.list_members",
        "org.find_position",
        "org.preview_loadout",
        "org.get_snapshot",
        "org.get_assignment",
        "delegation.assert_review_separation",
        "delegation.get_request",
        "delegation.get_pin",
    }
)


class StateCoordinator:
    """Only this writer may commit authoritative R2 runtime state."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        runners: dict[str, ProviderRunner] | None = None,
    ) -> None:
        self._conn = conn
        self._runners = runners or default_mock_registry()

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def accept(self, command: RuntimeCommand) -> dict[str, Any]:
        """Durably record and apply a typed command. Returns operation envelope."""
        from flow_engine.coordinator.mcp_enforce import (
            assert_mcp_coordinator_context,
            strip_mcp_payload_audit_fields,
        )

        # Never trust MCP identity from payload audit fields (HTTP + in-process).
        command = RuntimeCommand(
            command_type=command.command_type,
            target_id=command.target_id,
            payload=strip_mcp_payload_audit_fields(command.payload),
            idempotency_key=command.idempotency_key,
            context=command.context,
        )

        operation_id = new_id()
        command_id = new_id()
        created_at = utc_now_iso()
        scope = command.idempotency_scope
        digest = command.request_digest
        # No-key observations are distinct audited reads, not cached snapshots.
        if (
            command.command_type in FRESH_OBSERVATION_COMMANDS
            and command.idempotency_key is None
        ):
            scope = f"{scope}|observation|{operation_id}"

        existing = self._conn.execute(
            """
            SELECT id, operation_id, request_digest, status, result_json, error_code
            FROM runtime_commands WHERE idempotency_scope = ?
            """,
            (scope,),
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != digest:
                raise IdempotencyReplayError(
                    "idempotency scope reused with conflicting digest"
                )
            prior = json.loads(existing["result_json"] or "{}")
            return {
                **prior,
                "operation_id": existing["operation_id"],
                "from_cache": True,
                "command_status": existing["status"],
            }

        self._conn.execute(
            """
            INSERT INTO runtime_commands (
                id, operation_id, command_type, principal_id, surface, target_id,
                request_digest, attempt_id, provider_invocation_id, idempotency_scope,
                status, result_json, error_code, created_at, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', NULL, NULL, ?, NULL)
            """,
            (
                command_id,
                operation_id,
                command.command_type,
                command.context.principal_id,
                str(command.context.surface),
                command.target_id,
                digest,
                command.context.attempt_id,
                command.context.provider_invocation_id,
                scope,
                created_at,
            ),
        )

        self._conn.execute("SAVEPOINT coord_cmd")
        try:
            assert_mcp_coordinator_context(self._conn, command)
            kind, caps = self._lookup_principal_kind(command.context.principal_id)
            authorize_command(command, principal_kind=kind, capabilities=caps)
            result = self._dispatch(command)
            error_code = result.get("error_code")
            status = "applied"
            applied_at = utc_now_iso()
            envelope = {
                "operation_id": operation_id,
                "command_type": command.command_type,
                "status": status,
                "from_cache": False,
                "anomalies": result.get("anomalies") or [],
                "result": result,
                "error_code": error_code,
            }
            self._conn.execute(
                """
                UPDATE runtime_commands
                SET status = ?, result_json = ?, error_code = ?, applied_at = ?
                WHERE id = ?
                """,
                (status, json.dumps(envelope), error_code, applied_at, command_id),
            )
            append_audit_event(
                self._conn,
                event_type="coordinator.command_applied",
                actor=command.context.principal_id,
                command_id=command_id,
                payload={
                    "operation_id": operation_id,
                    "command_type": command.command_type,
                    "error_code": error_code,
                },
            )
            self._conn.execute("RELEASE SAVEPOINT coord_cmd")
            return envelope
        except PersistenceUnavailableError:
            self._conn.execute("ROLLBACK TO SAVEPOINT coord_cmd")
            raise
        except FlowError as exc:
            self._conn.execute("ROLLBACK TO SAVEPOINT coord_cmd")
            error_code = getattr(exc, "code", "FLOW_ERROR")
            anomaly = None
            if isinstance(
                exc,
                (
                    AuthzDeniedError,
                    AuthRequiredError,
                    UnsupportedSurfaceError,
                    PrerequisiteError,
                ),
            ):
                anomaly = AnomalyCode.A2
            elif isinstance(exc, BudgetExhaustedError):
                anomaly = AnomalyCode.A3
            elif isinstance(exc, OutcomeUnknownError):
                anomaly = AnomalyCode.A1
            envelope = {
                "operation_id": operation_id,
                "command_type": command.command_type,
                "status": "rejected",
                "from_cache": False,
                "anomalies": (
                    [{"code": str(anomaly), "detail": str(exc)}] if anomaly else []
                ),
                "result": None,
                "error_code": error_code,
                "error": str(exc),
            }
            self._conn.execute(
                """
                UPDATE runtime_commands
                SET status = 'rejected', result_json = ?, error_code = ?, applied_at = ?
                WHERE id = ?
                """,
                (json.dumps(envelope), error_code, utc_now_iso(), command_id),
            )
            append_audit_event(
                self._conn,
                event_type="coordinator.command_rejected",
                actor=command.context.principal_id,
                anomaly_code=anomaly,
                command_id=command_id,
                payload={
                    "operation_id": operation_id,
                    "command_type": command.command_type,
                    "error_code": error_code,
                },
            )
            return envelope

    def _require_grant(self, command: RuntimeCommand) -> Grant:
        if command.context.grant is None:
            raise AuthzDeniedError("grant required")
        return command.context.grant

    def _lookup_principal_kind(
        self, principal_id: str
    ) -> tuple[str | None, tuple[str, ...]]:
        """Resolve registry kind/capabilities when principal is registered (R4)."""
        import json

        if not principal_id or principal_id == "auth-resolver":
            return None, ()
        row = self._conn.execute(
            """
            SELECT kind, capabilities_json FROM control_plane_principals
            WHERE id = ? AND status = 'active'
            """,
            (principal_id,),
        ).fetchone()
        if row is None:
            row = self._conn.execute(
                """
                SELECT kind, capabilities_json FROM control_plane_principals
                WHERE principal_key = ? AND status = 'active'
                """,
                (principal_id,),
            ).fetchone()
        if row is None:
            return None, ()
        caps = tuple(json.loads(row["capabilities_json"] or "[]"))
        return row["kind"], caps

    def _require_r3_grant(self, command: RuntimeCommand):
        from flow_engine.coordinator.commands import ResolvedTaskGrant

        grant = self._require_grant(command)
        if not isinstance(grant, ResolvedTaskGrant):
            raise AuthzDeniedError("R3 resolved task grant required")
        return grant

    def _dispatch(self, command: RuntimeCommand) -> dict[str, Any]:
        ctype = command.command_type
        payload = command.payload
        actor = command.context.principal_id

        # --- R3 organization / delegation ---
        org_result = self._dispatch_org(command)
        if org_result is not None:
            return org_result
        del_result = self._dispatch_delegation(command)
        if del_result is not None:
            return del_result

        if ctype == "runtime.preview":
            return preview_run(
                self._conn,
                work_item_id=command.target_id or payload["work_item_id"],
                provider=payload["provider"],
                grant=self._require_grant(command),
                actor=actor,
            )
        if ctype == "runtime.create":
            return create_run(
                self._conn,
                work_item_id=command.target_id or payload["work_item_id"],
                provider=payload["provider"],
                grant=self._require_grant(command),
                actor=actor,
                policy_snapshot=payload.get("policy_snapshot"),
                gate_snapshot=payload.get("gate_snapshot"),
                packet=payload.get("packet"),
            )
        if ctype == "runtime.claim":
            return claim_attempt(
                self._conn,
                run_id=command.target_id or payload["run_id"],
                actor=actor,
                expected_revision=command.context.expected_revision,
            )
        if ctype == "runtime.step":
            run_id = command.target_id or payload["run_id"]
            run = get_run(self._conn, run_id)
            if run["status"] in {"pending", "paused"}:
                claim_attempt(self._conn, run_id=run_id, actor=actor)
            attempt_id = payload.get("attempt_id")
            if not attempt_id:
                row = self._conn.execute(
                    """
                    SELECT id FROM runtime_attempts
                    WHERE run_id = ? AND status = 'claimed'
                    ORDER BY attempt_number DESC LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError("no claimed attempt to step")
                attempt_id = row["id"]
            return dispatch_provider_call(
                self._conn,
                attempt_id=attempt_id,
                actor=actor,
                payload=payload.get("invocation_payload"),
                runners=self._runners,
            )
        if ctype == "runtime.run":
            created = create_run(
                self._conn,
                work_item_id=command.target_id or payload["work_item_id"],
                provider=payload["provider"],
                grant=self._require_grant(command),
                actor=actor,
                packet=payload.get("packet"),
            )
            run_id = created["run"]["id"]
            claim_attempt(self._conn, run_id=run_id, actor=actor)
            delivery_mode = "async" if payload.get("async_dispatch") or payload.get("delivery_mode") == "async" else "inline"
            dispatched = dispatch_provider_call(
                self._conn,
                attempt_id=created["attempt"]["id"],
                actor=actor,
                payload=payload.get("invocation_payload"),
                runners=self._runners,
                delivery_mode=delivery_mode,
            )
            return {"created": created, "dispatched": dispatched}
        if ctype == "runtime.heartbeat":
            return {
                "attempt": heartbeat_attempt(
                    self._conn,
                    attempt_id=command.target_id or payload["attempt_id"],
                    actor=actor,
                )
            }
        if ctype == "runtime.result":
            return submit_result(
                self._conn,
                attempt_id=command.target_id or payload["attempt_id"],
                outcome=payload["outcome"],
                actor=actor,
                evidence=payload.get("evidence"),
                anomalies=payload.get("anomalies"),
            )
        if ctype == "runtime.pause":
            return pause_run(
                self._conn, run_id=command.target_id or payload["run_id"], actor=actor
            )
        if ctype == "runtime.resume":
            return resume_run(
                self._conn, run_id=command.target_id or payload["run_id"], actor=actor
            )
        if ctype == "runtime.cancel":
            return cancel_run(
                self._conn, run_id=command.target_id or payload["run_id"], actor=actor
            )
        if ctype == "runtime.reconcile":
            run_id = command.target_id or payload["run_id"]
            if payload.get("finish"):
                return finish_reconcile(
                    self._conn,
                    run_id=run_id,
                    outcome=payload["outcome"],
                    actor=actor,
                    evidence=payload.get("evidence"),
                    runners=self._runners,
                )
            started = begin_reconcile(self._conn, run_id=run_id, actor=actor)
            if payload.get("auto_finish"):
                finished = finish_reconcile(
                    self._conn,
                    run_id=run_id,
                    outcome=payload.get("outcome", "complete"),
                    actor=actor,
                    evidence=payload.get("evidence"),
                    runners=self._runners,
                )
                return {"started": started, "finished": finished}
            return started
        if ctype == "runtime.provider_limit_halt":
            return {
                "run": provider_limit_halt(
                    self._conn,
                    run_id=command.target_id or payload["run_id"],
                    actor=actor,
                )
            }
        if ctype == "runtime.provider_limit_continue":
            return {
                "run": provider_limit_continue(
                    self._conn,
                    run_id=command.target_id or payload["run_id"],
                    actor=actor,
                )
            }
        if ctype == "runtime.provider_limit_reroute":
            return {
                "run": provider_limit_reroute(
                    self._conn,
                    run_id=command.target_id or payload["run_id"],
                    new_provider=payload["provider"],
                    actor=actor,
                    grant=self._require_grant(command),
                )
            }
        if ctype == "runtime.new_attempt_after_unknown":
            return new_attempt_after_unknown(
                self._conn,
                run_id=command.target_id or payload["run_id"],
                actor=actor,
            )
        if ctype == "runtime.recover_restart":
            return recover_after_restart(self._conn, actor=actor)
        if ctype == "runtime.recover_worker_death":
            return recover_worker_death(
                self._conn,
                attempt_id=command.target_id or payload["attempt_id"],
                actor=actor,
            )
        if ctype == "runtime.reconstruct_deliveries":
            return {"deliveries": reconstruct_eligible_deliveries(self._conn)}
        if ctype == "runtime.replay_delivery_hint":
            return replay_delivery_hint(
                self._conn,
                invocation_id=command.target_id or payload["invocation_id"],
            )
        if ctype == "runtime.evaluate_timeouts":
            return {"timeouts": evaluate_timeouts(self._conn, actor=actor)}
        if ctype == "runtime.credit_usage":
            return credit_usage(self._conn, command.target_id or payload["run_id"])
        if ctype == "runtime.list_audit":
            return {
                "events": list_audit_events(
                    self._conn,
                    limit=int(payload.get("limit", 100)),
                    anomaly_code=payload.get("anomaly_code"),
                )
            }
        if ctype == "runtime.show":
            run = get_run(self._conn, command.target_id or payload["run_id"])
            return {"run": run, "credits": credit_usage(self._conn, run["id"])}

        delivery_result = self._dispatch_delivery(command)
        if delivery_result is not None:
            return delivery_result

        script_result = self._dispatch_script(command)
        if script_result is not None:
            return script_result

        schedule_result = self._dispatch_schedule(command)
        if schedule_result is not None:
            return schedule_result

        raise ValidationFailedError(f"unknown command_type: {ctype}")

    def _dispatch_delivery(self, command: RuntimeCommand) -> dict[str, Any] | None:
        from flow_engine.control_plane import delivery_registry as delivery

        ctype = command.command_type
        payload = command.payload
        if not ctype.startswith("delivery.") and ctype not in {
            "runtime.worker_deliver",
            "runtime.worker_prepare",
            "runtime.worker_preflight",
            "runtime.worker_preflight_reject",
            "runtime.worker_snapshot",
            "runtime.worker_settle",
            "runtime.worker_cancel_prepare",
            "runtime.worker_cancel_settle",
        }:
            if ctype.startswith("control_plane."):
                return self._dispatch_control_plane(command)
            return None

        if ctype == "delivery.register":
            return {
                "job": delivery.register_delivery_job(
                    self._conn,
                    invocation_id=payload["invocation_id"],
                    attempt_id=payload["attempt_id"],
                    run_id=payload["run_id"],
                    provider=payload["provider"],
                    idempotency_key=payload["idempotency_key"],
                )
            }
        if ctype == "delivery.claim":
            return {
                "job": delivery.claim_delivery_job(
                    self._conn,
                    job_id=command.target_id or payload["job_id"],
                    worker_principal_id=command.context.principal_id,
                    celery_task_id=payload.get("celery_task_id"),
                    attempt_id=payload.get("attempt_id"),
                    invocation_id=payload.get("invocation_id"),
                )
            }
        if ctype == "delivery.heartbeat":
            return {
                "job": delivery.heartbeat_delivery_job(
                    self._conn,
                    job_id=command.target_id or payload["job_id"],
                    worker_principal_id=command.context.principal_id,
                )
            }
        if ctype == "delivery.complete":
            return {
                "job": delivery.complete_delivery_job(
                    self._conn,
                    job_id=command.target_id or payload["job_id"],
                    outcome=payload["outcome"],
                    result=payload.get("result"),
                )
            }
        if ctype == "delivery.list_eligible":
            return {"jobs": delivery.list_eligible_delivery_jobs(self._conn)}
        if ctype == "delivery.recover_stale":
            return {
                "jobs": delivery.recover_stale_delivery_jobs(
                    self._conn, stale_before_iso=payload["stale_before_iso"]
                )
            }
        if ctype == "delivery.get_by_invocation":
            job = delivery.get_delivery_job_by_invocation(
                self._conn, command.target_id or payload["invocation_id"]
            )
            return {"job": job}
        if ctype == "runtime.worker_deliver":
            # Provider I/O must not run inside the outer accept savepoint/txn.
            # Callers should use accept_worker_deliver via CoordinatorClient/HTTP.
            # When invoked through accept() (legacy), still prepare+execute+settle
            # but execute happens inside savepoint — prefer accept_worker_deliver.
            raise ValidationFailedError(
                "runtime.worker_deliver must use accept_worker_deliver "
                "(provider I/O outside SQLite transaction)"
            )
        if ctype == "runtime.worker_prepare":
            from flow_engine.application.worker_delivery import prepare_worker_delivery

            return {
                "prepared": prepare_worker_delivery(
                    self._conn,
                    attempt_id=command.target_id or payload["attempt_id"],
                    delivery_job_id=payload.get("delivery_job_id"),
                    worker_principal_id=command.context.principal_id,
                    lease_token=payload.get("lease_token"),
                )
            }
        if ctype == "runtime.worker_preflight":
            from flow_engine.application.worker_delivery import preflight_worker_delivery

            return {
                "preflight": preflight_worker_delivery(
                    self._conn,
                    attempt_id=command.target_id or payload["attempt_id"],
                    delivery_job_id=payload["delivery_job_id"],
                    worker_principal_id=command.context.principal_id,
                )
            }
        if ctype == "runtime.worker_preflight_reject":
            return submit_result(
                self._conn,
                attempt_id=command.target_id or payload["attempt_id"],
                outcome="failed",
                actor=command.context.principal_id,
                evidence={"reason": "provider_preflight_rejected"},
                anomalies=[],
                consume_credit=False,
            )
        if ctype == "runtime.worker_snapshot":
            from flow_engine.application.worker_delivery import persist_adapter_snapshot

            return {
                "snapshot": persist_adapter_snapshot(
                    self._conn,
                    invocation_id=command.target_id or payload["invocation_id"],
                    provider=payload["provider"],
                    snapshot=payload["snapshot"],
                    snapshot_digest=payload["snapshot_digest"],
                    actor=command.context.principal_id,
                )
            }
        if ctype == "runtime.worker_settle":
            from flow_engine.application.worker_delivery import (
                settle_external_worker_delivery,
            )

            return settle_external_worker_delivery(
                self._conn,
                prepared=payload["prepared"],
                provider_result=payload.get("provider_result"),
                actor=command.context.principal_id,
            )
        if ctype == "runtime.worker_cancel_prepare":
            from flow_engine.application.runtime_service import get_invocation_for_attempt

            invocation = get_invocation_for_attempt(
                self._conn, command.target_id or payload["attempt_id"]
            )
            if invocation is None:
                raise ValidationFailedError("provider invocation not found")
            expected = f"worker.provider.{invocation['provider']}"
            if command.context.principal_id != expected:
                raise AuthzDeniedError("provider cancel principal mismatch")
            return {"invocation": invocation}
        if ctype == "runtime.worker_cancel_settle":
            from flow_engine.application.worker_delivery import (
                _record_runner_event,
                settle_external_worker_delivery,
            )

            invocation_id = payload["invocation_id"]
            row = self._conn.execute(
                """
                SELECT provider, attempt_id, run_id, adapter_snapshot_digest,
                       binding_digest
                FROM provider_invocations WHERE id = ?
                """,
                (invocation_id,),
            ).fetchone()
            if row is None or command.context.principal_id != f"worker.provider.{row['provider']}":
                raise AuthzDeniedError("provider cancel principal mismatch")
            _record_runner_event(
                self._conn,
                invocation_id=invocation_id,
                event_type="cancel",
                event={
                    "cancelled": bool(payload.get("cancelled")),
                    "ambiguous": bool(payload.get("ambiguous")),
                },
            )
            ambiguous = bool(payload.get("ambiguous"))
            cancelled = bool(payload.get("cancelled"))
            if ambiguous or cancelled:
                settled = settle_external_worker_delivery(
                    self._conn,
                    prepared={
                        "invocation_id": invocation_id,
                        "attempt_id": row["attempt_id"],
                        "run_id": row["run_id"],
                        "provider": row["provider"],
                        "delivery_job_id": None,
                    },
                    provider_result=(
                        None
                        if ambiguous
                        else {
                            "outcome": "failed",
                            "evidence": {"cancelled": True},
                            "anomalies": [],
                            "delivery_id": invocation_id,
                            "provider_call_id": None,
                            "redacted_output": "",
                            "truncated": False,
                            "snapshot_digest": row["adapter_snapshot_digest"],
                            "binding_digest": row["binding_digest"],
                        }
                    ),
                    actor=command.context.principal_id,
                )
                return {"cancel_recorded": True, "settled": settled}
            return {"cancel_recorded": True, "invocation_id": invocation_id}
        raise ValidationFailedError(f"unknown command_type: {ctype}")

    def _dispatch_script(self, command: RuntimeCommand) -> dict[str, Any] | None:
        from flow_engine.script_sandbox import allowlist as script_allowlist
        from flow_engine.script_sandbox import registry as script_registry
        from flow_engine.script_sandbox.classify import reject_repository_script

        ctype = command.command_type
        payload = command.payload
        actor = command.context.principal_id
        if not ctype.startswith("script."):
            return None

        if ctype == "script.list_allowlist":
            return {"scripts": script_allowlist.list_allowlist()}
        if ctype == "script.register":
            script_id = payload.get("script_id") or command.target_id
            reject_repository_script(str(script_id))
            return script_registry.register_script_execution(
                self._conn,
                script_id=str(script_id),
                actor=actor,
                input_json=payload.get("input") or payload.get("input_json") or {},
                idempotency_key=command.idempotency_key
                or payload.get("idempotency_key")
                or f"script|{script_id}|{payload.get('nonce', '')}",
                expected_executable_digest=payload.get("expected_executable_digest"),
                expected_image_digest=payload.get("expected_image_digest"),
                schedule_run_id=payload.get("schedule_run_id"),
            )
        if ctype == "script.start":
            return script_registry.start_script_execution(
                self._conn,
                execution_id=command.target_id or payload["execution_id"],
                actor=actor,
            )
        if ctype == "script.complete":
            return script_registry.complete_script_execution(
                self._conn,
                execution_id=command.target_id or payload["execution_id"],
                actor=actor,
                result=payload.get("result") or {},
            )
        if ctype == "script.execute":
            # Subprocess must not run inside accept()/SQLite txn.
            raise ValidationFailedError(
                "script.execute must use accept_script_execute "
                "(subprocess outside SQLite transaction via script-worker)"
            )
        if ctype == "script.cancel":
            return script_registry.cancel_script_execution(
                self._conn,
                execution_id=command.target_id or payload["execution_id"],
                actor=actor,
            )
        if ctype == "script.show":
            return {
                "execution": script_registry.get_script_execution(
                    self._conn, command.target_id or payload["execution_id"]
                )
            }
        raise ValidationFailedError(f"unknown command_type: {ctype}")

    def _dispatch_schedule(self, command: RuntimeCommand) -> dict[str, Any] | None:
        from flow_engine.schedules import service as schedule_service
        from flow_engine.schedules.templates import list_schedule_templates
        from flow_engine.script_sandbox.classify import reject_repository_script

        ctype = command.command_type
        payload = command.payload
        actor = command.context.principal_id
        if not ctype.startswith("schedule."):
            return None

        if ctype == "schedule.list_templates":
            return {"templates": list_schedule_templates(), "timezone": "Asia/Manila"}
        if ctype == "schedule.status":
            return schedule_service.list_schedule_status(self._conn)
        if ctype == "schedule.tick":
            return schedule_service.claim_schedule_tick(
                self._conn,
                schedule_id=command.target_id or payload["schedule_id"],
                planned_time=payload["planned_time"],
                actor=actor,
                provider_call_budget=payload.get("provider_call_budget"),
            )
        if ctype == "schedule.complete":
            if payload.get("attempt_remediation") or payload.get("remediation"):
                raise UnsupportedSurfaceError(
                    "scheduled results must not remediate or mutate repositories"
                )
            # Reject repository scripts if any were requested in the payload.
            for script_id in payload.get("script_ids") or []:
                reject_repository_script(str(script_id))
            return schedule_service.complete_schedule_run(
                self._conn,
                run_id=command.target_id or payload["run_id"],
                actor=actor,
                effects=payload.get("effects"),
                script_results=payload.get("script_results"),
                attempt_remediation=False,
                provider_calls=int(payload.get("provider_calls") or 0),
            )
        if ctype == "schedule.run_on_demand":
            # Founder-only on-demand: claim tick then optionally register scripts.
            tick = schedule_service.claim_schedule_tick(
                self._conn,
                schedule_id=command.target_id or payload["schedule_id"],
                planned_time=payload["planned_time"],
                actor=actor,
                provider_call_budget=0,
            )
            return {"tick": tick, "on_demand": True}
        raise ValidationFailedError(f"unknown command_type: {ctype}")

    def _dispatch_control_plane(self, command: RuntimeCommand) -> dict[str, Any]:
        from flow_engine.control_plane import principal_registry as principals

        ctype = command.command_type
        payload = command.payload
        if ctype == "control_plane.register_principal":
            return {
                "principal": principals.register_principal(
                    self._conn,
                    principal_key=payload["principal_key"],
                    kind=payload["kind"],
                    role=PrincipalRole(payload["role"]),
                    raw_token=payload["raw_token"],
                    display_name=payload["display_name"],
                    organization_id=payload.get("organization_id"),
                    actor_id=payload.get("actor_id"),
                    provider_seat_id=payload.get("provider_seat_id"),
                    grant_id=payload.get("grant_id"),
                    capabilities=tuple(payload.get("capabilities") or ()),
                ).to_dict()
            }
        if ctype == "control_plane.resolve_token":
            principal = principals.resolve_by_token(self._conn, payload["raw_token"])
            grant = principals.load_grant_for_principal(self._conn, principal)
            return {
                "principal": principal.to_dict(),
                "grant": grant.to_dict() if grant is not None else None,
            }
        if ctype == "control_plane.revoke":
            return {
                "principal": principals.revoke_principal(
                    self._conn,
                    principal_key=payload["principal_key"],
                    actor=command.context.principal_id,
                ).to_dict()
            }
        raise ValidationFailedError(f"unknown command_type: {ctype}")

    def _dispatch_org(self, command: RuntimeCommand) -> dict[str, Any] | None:
        from flow_engine.application import organization_service as org
        from flow_engine.domain.states import PrincipalRole

        ctype = command.command_type
        payload = command.payload
        actor = command.context.principal_id
        if not ctype.startswith("org."):
            return None

        if ctype == "org.create_profile":
            return {
                "profile": org.create_organization_profile(
                    self._conn,
                    name=payload["name"],
                    actor=actor,
                    profile=payload.get("profile"),
                    policy_revision=payload.get("policy_revision", "r3-default"),
                )
            }
        if ctype == "org.get_profile":
            return {
                "profile": org.get_organization_profile(
                    self._conn, command.target_id or payload["organization_id"]
                )
            }
        if ctype == "org.list_profiles":
            return {"profiles": org.list_organization_profiles(self._conn)}
        if ctype == "org.add_actor":
            return {
                "actor": org.add_actor(
                    self._conn,
                    organization_id=payload["organization_id"],
                    actor_key=payload["actor_key"],
                    display_name=payload.get("display_name") or payload["actor_key"],
                    actor=actor,
                )
            }
        if ctype == "org.add_provider_seat":
            return {
                "seat": org.add_provider_seat(
                    self._conn,
                    organization_id=payload["organization_id"],
                    actor_id=payload["actor_id"],
                    provider=payload["provider"],
                    seat_key=payload["seat_key"],
                    actor=actor,
                )
            }
        if ctype == "org.list_members":
            return org.list_members(
                self._conn, command.target_id or payload["organization_id"]
            )
        if ctype == "org.find_position":
            return {
                "position": org.find_position(
                    self._conn,
                    organization_id=payload["organization_id"],
                    department_key=payload["department_key"],
                    position_key=payload["position_key"],
                )
            }
        if ctype == "org.preview_loadout":
            return {
                "resolution": org.preview_loadout(
                    self._conn,
                    organization_id=payload["organization_id"],
                    loadout_id=payload["loadout_id"],
                    actor=actor,
                )
            }
        if ctype == "org.materialize_snapshot":
            return {
                "snapshot": org.materialize_snapshot(
                    self._conn,
                    organization_id=payload["organization_id"],
                    loadout_id=payload["loadout_id"],
                    actor=actor,
                    department_ceiling=payload.get("department_ceiling"),
                    hierarchy_ceiling=payload.get("hierarchy_ceiling"),
                    position_ceiling=payload.get("position_ceiling"),
                    explicit_grant=payload.get("explicit_grant"),
                )
            }
        if ctype == "org.get_snapshot":
            return {
                "snapshot": org.get_snapshot(
                    self._conn, command.target_id or payload["snapshot_id"]
                )
            }
        if ctype == "org.create_assignment":
            return {
                "assignment": org.create_assignment(
                    self._conn,
                    organization_id=payload["organization_id"],
                    work_item_id=payload["work_item_id"],
                    position_id=payload["position_id"],
                    actor_id=payload["actor_id"],
                    provider_seat_id=payload["provider_seat_id"],
                    actor=actor,
                    parent_assignment_id=payload.get("parent_assignment_id"),
                )
            }
        if ctype == "org.get_assignment":
            return {
                "assignment": org.get_assignment(
                    self._conn, command.target_id or payload["assignment_id"]
                )
            }
        if ctype == "org.complete_assignment":
            from flow_engine.application.delegation_service import complete_assignment

            return {
                "assignment": complete_assignment(
                    self._conn,
                    assignment_id=command.target_id or payload["assignment_id"],
                    actor=actor,
                )
            }
        # Silence unused import if role checks added later.
        _ = PrincipalRole
        raise ValidationFailedError(f"unknown command_type: {ctype}")

    def _dispatch_delegation(self, command: RuntimeCommand) -> dict[str, Any] | None:
        from flow_engine.application import delegation_service as dele
        from flow_engine.domain.states import PrincipalRole, Surface

        ctype = command.command_type
        payload = command.payload
        actor = command.context.principal_id
        if not ctype.startswith("delegation."):
            return None

        if ctype == "delegation.request":
            return {
                "request": dele.request_delegation(
                    self._conn,
                    parent_assignment_id=payload["parent_assignment_id"],
                    to_position_id=payload["to_position_id"],
                    packet=payload.get("packet") or {},
                    actor=actor,
                )
            }
        if ctype == "delegation.accept":
            return {
                "request": dele.accept_delegation(
                    self._conn,
                    request_id=command.target_id or payload["request_id"],
                    actor_id=payload["actor_id"],
                    actor=actor,
                )
            }
        if ctype == "delegation.decline":
            return {
                "request": dele.decline_delegation(
                    self._conn,
                    request_id=command.target_id or payload["request_id"],
                    actor_id=payload["actor_id"],
                    actor=actor,
                    reason=payload.get("reason", ""),
                )
            }
        if ctype == "delegation.reroute":
            return {
                "request": dele.reroute_delegation(
                    self._conn,
                    request_id=command.target_id or payload["request_id"],
                    actor_id=payload["actor_id"],
                    reroute_position_id=payload["reroute_position_id"],
                    actor=actor,
                    reason=payload.get("reason", ""),
                )
            }
        if ctype == "delegation.dispatch":
            return dele.dispatch_delegated_assignment(
                self._conn,
                request_id=command.target_id or payload["request_id"],
                actor_id=payload["actor_id"],
                provider_seat_id=payload["provider_seat_id"],
                actor=actor,
            )
        if ctype == "delegation.handoff":
            return {
                "handoff": dele.create_handoff(
                    self._conn,
                    from_assignment_id=payload["from_assignment_id"],
                    to_assignment_id=payload["to_assignment_id"],
                    packet=payload.get("packet") or {},
                    actor=actor,
                    review_required=bool(payload.get("review_required", True)),
                )
            }
        if ctype == "delegation.accept_handoff":
            return {
                "handoff": dele.accept_handoff_evidence(
                    self._conn,
                    handoff_id=command.target_id or payload["handoff_id"],
                    actor=actor,
                    evidence=payload.get("evidence"),
                )
            }
        if ctype == "delegation.mint_grant":
            grant = dele.mint_task_grant(
                self._conn,
                organization_id=payload["organization_id"],
                principal_id=payload.get("principal_id") or actor,
                role=PrincipalRole(payload.get("role", "worker")),
                surfaces=tuple(
                    Surface(s) for s in payload.get("surfaces", ["cli", "test"])
                ),
                providers=tuple(payload.get("providers") or ["codex", "cursor", "claude"]),
                budget_scope_id=payload["budget_scope_id"],
                assignment_id=payload["assignment_id"],
                actor=actor,
                capabilities=tuple(payload.get("capabilities") or ()),
                policy_revision=payload.get("policy_revision"),
            )
            return {"grant": grant.to_dict()}
        if ctype == "delegation.assert_review_separation":
            dele.assert_review_separation(
                implementation=payload["implementation"],
                review=payload["review"],
            )
            return {"ok": True}
        if ctype == "delegation.get_request":
            return {
                "request": dele.get_delegation_request(
                    self._conn, command.target_id or payload["request_id"]
                )
            }
        if ctype == "delegation.get_pin":
            return {
                "pin": dele.get_dispatch_pin(
                    self._conn, command.target_id or payload["pin_id"]
                )
            }
        raise ValidationFailedError(f"unknown command_type: {ctype}")
