"""CLI helpers for R2 runtime commands via the state coordinator."""

from __future__ import annotations

import argparse
import json
from typing import Any

from flow_engine.cli.output import emit_result
from flow_engine.coordinator import (
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    StepUpEvidence,
    SystemTestGrant,
)
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence.transactions import transaction


def _parse_json_obj(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def add_runtime_parser(sub: argparse._SubParsersAction) -> None:
    runtime = sub.add_parser("runtime", help="R2 governed runtime controls")
    runtime_sub = runtime.add_subparsers(dest="runtime_command", required=True)

    def _common_grant(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--actor", default="agent")
        parser.add_argument("--role", choices=[r.value for r in PrincipalRole], default="worker")
        parser.add_argument("--grant-id", default="system-test-grant")
        parser.add_argument(
            "--budget-scope-id",
            required=True,
            help="Stable identity shared by every run in one acceptance campaign",
        )
        parser.add_argument(
            "--providers",
            default="codex,cursor,claude",
            help="Comma-separated providers allowed by system test grant",
        )
        parser.add_argument("--policy-revision", default="system-test")
        parser.add_argument("--idempotency-key", help="Optional extra idempotency salt")

    def _step_up(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--step-up-reauth-at", help="ISO timestamp of reauthentication")
        parser.add_argument("--step-up-reason")
        parser.add_argument("--step-up-evidence")
        parser.add_argument(
            "--step-up-ack-duplicate-cost",
            action="store_true",
            help="Acknowledge possible duplicate paid-call cost",
        )
        parser.add_argument("--step-up-new-idempotency-id")

    preview = runtime_sub.add_parser("preview", help="Preview a governed run")
    preview.add_argument("work_item_id")
    preview.add_argument("--provider", required=True)
    _common_grant(preview)

    create = runtime_sub.add_parser("create", help="Create a governed run")
    create.add_argument("work_item_id")
    create.add_argument("--provider", required=True)
    _common_grant(create)

    run_cmd = runtime_sub.add_parser("run", help="Create, claim, and dispatch one step")
    run_cmd.add_argument("work_item_id")
    run_cmd.add_argument("--provider", required=True)
    run_cmd.add_argument("--payload", type=_parse_json_obj, default={})
    _common_grant(run_cmd)

    step = runtime_sub.add_parser("step", help="Claim if needed and dispatch one provider call")
    step.add_argument("run_id")
    step.add_argument("--attempt-id")
    step.add_argument("--payload", type=_parse_json_obj, default={})
    _common_grant(step)

    claim = runtime_sub.add_parser("claim", help="Claim run attempt lease")
    claim.add_argument("run_id")
    claim.add_argument("--revision", type=int)
    _common_grant(claim)

    pause = runtime_sub.add_parser("pause", help="Pause a claimed run")
    pause.add_argument("run_id")
    _common_grant(pause)

    resume = runtime_sub.add_parser("resume", help="Resume a paused run")
    resume.add_argument("run_id")
    _common_grant(resume)

    cancel = runtime_sub.add_parser("cancel", help="Cancel a run")
    cancel.add_argument("run_id")
    _common_grant(cancel)

    result = runtime_sub.add_parser("result", help="Submit attempt result")
    result.add_argument("attempt_id")
    result.add_argument(
        "--outcome",
        required=True,
        choices=["complete", "failed", "outcome_unknown"],
    )
    result.add_argument("--evidence", type=_parse_json_obj, default={})
    result.add_argument(
        "--anomalies",
        type=json.loads,
        default=[],
        help="JSON list of anomaly objects (required field; empty list allowed)",
    )
    _common_grant(result)

    heartbeat = runtime_sub.add_parser("heartbeat", help="Renew attempt lease heartbeat")
    heartbeat.add_argument("attempt_id")
    _common_grant(heartbeat)

    reconcile = runtime_sub.add_parser("reconcile", help="Reconcile original invocation")
    reconcile.add_argument("run_id")
    reconcile.add_argument("--finish", action="store_true")
    reconcile.add_argument("--auto-finish", action="store_true")
    reconcile.add_argument(
        "--outcome",
        choices=["complete", "failed", "cancelled"],
        default="complete",
    )
    reconcile.add_argument("--evidence", type=_parse_json_obj, default={})
    _common_grant(reconcile)

    limit = runtime_sub.add_parser("provider-limit", help="Provider limit halt/continue/reroute")
    limit.add_argument("run_id")
    limit.add_argument("action", choices=["halt", "continue", "reroute"])
    limit.add_argument("--provider", help="Required for reroute")
    _common_grant(limit)

    new_attempt = runtime_sub.add_parser(
        "new-attempt",
        help="Founder-only new paid attempt after unknown/reconciled terminal",
    )
    new_attempt.add_argument("run_id")
    _common_grant(new_attempt)
    _step_up(new_attempt)

    show = runtime_sub.add_parser("show", help="Show run and credit usage")
    show.add_argument("run_id")
    _common_grant(show)

    recover = runtime_sub.add_parser("recover", help="Recovery operations")
    recover.add_argument(
        "action",
        choices=["restart", "worker-death", "reconstruct", "timeouts"],
    )
    recover.add_argument("--attempt-id")
    _common_grant(recover)


def _grant_from_args(args: argparse.Namespace) -> SystemTestGrant:
    providers = tuple(p.strip() for p in args.providers.split(",") if p.strip())
    role = PrincipalRole(args.role)
    return SystemTestGrant(
        grant_id=args.grant_id,
        principal_id=args.actor,
        role=role,
        surfaces=(Surface.CLI, Surface.TEST),
        providers=providers,
        budget_scope_id=args.budget_scope_id,
        policy_revision=args.policy_revision,
    )


def _step_up_from_args(args: argparse.Namespace) -> StepUpEvidence | None:
    if not getattr(args, "step_up_reauth_at", None):
        return None
    return StepUpEvidence(
        reauthenticated_at=args.step_up_reauth_at,
        reason=args.step_up_reason or "",
        evidence=args.step_up_evidence or "",
        duplicate_cost_warning_ack=bool(args.step_up_ack_duplicate_cost),
        policy_revision=args.policy_revision,
        new_idempotency_identity=args.step_up_new_idempotency_id or "",
    )


def _context_from_args(args: argparse.Namespace) -> CommandContext:
    grant = _grant_from_args(args)
    return CommandContext(
        principal_id=args.actor,
        role=PrincipalRole(args.role),
        surface=Surface.CLI,
        grant=grant,
        step_up=_step_up_from_args(args),
        expected_revision=getattr(args, "revision", None),
        attempt_id=getattr(args, "attempt_id", None),
    )


def run_runtime_command(args: argparse.Namespace, conn) -> int:
    coordinator = StateCoordinator(conn)
    ctx = _context_from_args(args)
    cmd = args.runtime_command

    def _accept(command_type: str, target_id: str | None, payload: dict[str, Any]) -> dict:
        if getattr(args, "idempotency_key", None):
            payload = {**payload, "_idem_salt": args.idempotency_key}
        with transaction(conn):
            return coordinator.accept(
                RuntimeCommand(
                    command_type=command_type,
                    target_id=target_id,
                    payload=payload,
                    context=ctx,
                )
            )

    if cmd == "preview":
        result = _accept(
            "runtime.preview",
            args.work_item_id,
            {"provider": args.provider, "work_item_id": args.work_item_id},
        )
    elif cmd == "create":
        result = _accept(
            "runtime.create",
            args.work_item_id,
            {"provider": args.provider, "work_item_id": args.work_item_id},
        )
    elif cmd == "run":
        result = _accept(
            "runtime.run",
            args.work_item_id,
            {
                "provider": args.provider,
                "work_item_id": args.work_item_id,
                "invocation_payload": args.payload,
            },
        )
    elif cmd == "step":
        result = _accept(
            "runtime.step",
            args.run_id,
            {
                "run_id": args.run_id,
                "attempt_id": args.attempt_id,
                "invocation_payload": args.payload,
            },
        )
    elif cmd == "claim":
        result = _accept("runtime.claim", args.run_id, {"run_id": args.run_id})
    elif cmd == "pause":
        result = _accept("runtime.pause", args.run_id, {"run_id": args.run_id})
    elif cmd == "resume":
        result = _accept("runtime.resume", args.run_id, {"run_id": args.run_id})
    elif cmd == "cancel":
        result = _accept("runtime.cancel", args.run_id, {"run_id": args.run_id})
    elif cmd == "result":
        result = _accept(
            "runtime.result",
            args.attempt_id,
            {
                "attempt_id": args.attempt_id,
                "outcome": args.outcome,
                "evidence": args.evidence,
                "anomalies": args.anomalies,
            },
        )
    elif cmd == "heartbeat":
        result = _accept(
            "runtime.heartbeat",
            args.attempt_id,
            {"attempt_id": args.attempt_id},
        )
    elif cmd == "reconcile":
        result = _accept(
            "runtime.reconcile",
            args.run_id,
            {
                "run_id": args.run_id,
                "finish": args.finish,
                "auto_finish": args.auto_finish,
                "outcome": args.outcome,
                "evidence": args.evidence,
            },
        )
    elif cmd == "provider-limit":
        mapping = {
            "halt": "runtime.provider_limit_halt",
            "continue": "runtime.provider_limit_continue",
            "reroute": "runtime.provider_limit_reroute",
        }
        payload: dict[str, Any] = {"run_id": args.run_id}
        if args.action == "reroute":
            if not args.provider:
                raise SystemExit("--provider is required for reroute")
            payload["provider"] = args.provider
        result = _accept(mapping[args.action], args.run_id, payload)
    elif cmd == "new-attempt":
        # Force founder role for this command surface
        founder_ctx = CommandContext(
            principal_id=args.actor,
            role=PrincipalRole.FOUNDER,
            surface=Surface.CLI,
            grant=SystemTestGrant(
                grant_id=args.grant_id,
                principal_id=args.actor,
                role=PrincipalRole.FOUNDER,
                surfaces=(Surface.CLI, Surface.TEST),
                providers=tuple(p.strip() for p in args.providers.split(",") if p.strip()),
                budget_scope_id=args.budget_scope_id,
                policy_revision=args.policy_revision,
            ),
            step_up=_step_up_from_args(args),
        )
        with transaction(conn):
            result = coordinator.accept(
                RuntimeCommand(
                    command_type="runtime.new_attempt_after_unknown",
                    target_id=args.run_id,
                    payload={"run_id": args.run_id},
                    context=founder_ctx,
                )
            )
    elif cmd == "show":
        result = _accept("runtime.show", args.run_id, {"run_id": args.run_id})
    elif cmd == "recover":
        if args.action == "restart":
            result = _accept("runtime.recover_restart", None, {})
        elif args.action == "worker-death":
            if not args.attempt_id:
                raise SystemExit("--attempt-id is required for worker-death")
            result = _accept(
                "runtime.recover_worker_death",
                args.attempt_id,
                {"attempt_id": args.attempt_id},
            )
        elif args.action == "reconstruct":
            result = _accept("runtime.reconstruct_deliveries", None, {})
        else:
            result = _accept("runtime.evaluate_timeouts", None, {})
    else:
        raise RuntimeError(f"unknown runtime command: {cmd}")

    emit_result(result, as_json=args.json)
    if result.get("status") == "rejected":
        return 2
    return 0
