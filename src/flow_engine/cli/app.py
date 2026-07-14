"""flowctl command-line interface."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from flow_engine.application import (
    claim_resource,
    claim_work,
    complete_work,
    ensure_queue,
    export_all,
    fail_gate,
    fail_work,
    init_project,
    list_events,
    list_gates,
    list_queues,
    list_resources,
    list_work,
    pass_gate,
    release_resource,
    renew_resource,
    retry_work,
    show_queue,
    show_resource,
    show_work,
    status,
    submit_work,
    waive_gate,
)
from flow_engine.cli.capability_cmds import add_capability_parser, run_capability_command
from flow_engine.cli.context import db_session, require_initialized, resolve_db_path
from flow_engine.cli.output import emit_result
from flow_engine.domain.errors import (
    AdvisoryConflictError,
    ConflictError,
    FlowError,
    InvalidTransitionError,
    NotFoundError,
)
from flow_engine.domain.states import ClaimPolicy
from flow_engine.persistence.transactions import transaction


def _parse_json(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("payload must be a JSON object")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flowctl", description="Workflow engine CLI")
    parser.add_argument("--db", help="Path to SQLite database (default: .flow/state.db)")
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout")

    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="Initialize database and default project")
    init_cmd.add_argument("--project", default="default", help="Project name")
    init_cmd.add_argument("--queue", action="append", default=[], help="Queue to create")

    sub.add_parser("status", help="Show engine status")
    sub.add_parser("export", help="Export full database snapshot")

    queue = sub.add_parser("queue", help="Queue operations")
    queue_sub = queue.add_subparsers(dest="queue_command", required=True)
    queue_sub.add_parser("list", help="List queues")
    queue_show = queue_sub.add_parser("show", help="Show queue details")
    queue_show.add_argument("name")

    work = sub.add_parser("work", help="Work item operations")
    work_sub = work.add_subparsers(dest="work_command", required=True)
    work_submit = work_sub.add_parser("submit", help="Submit work to a queue")
    work_submit.add_argument("--queue", required=True)
    work_submit.add_argument("--payload", type=_parse_json, default={})
    work_submit.add_argument("--actor", default="agent")
    work_submit.add_argument("--depends-on", action="append", default=[])
    work_submit.add_argument("--idempotency-key")
    work_list = work_sub.add_parser("list", help="List work items")
    work_list.add_argument("--queue")
    work_list.add_argument("--status")
    work_show = work_sub.add_parser("show", help="Show work item")
    work_show.add_argument("work_id")
    work_claim = work_sub.add_parser("claim", help="Claim work item")
    work_claim.add_argument("work_id", nargs="?")
    work_claim.add_argument("--queue")
    work_claim.add_argument("--actor", default="agent")
    work_claim.add_argument("--revision", type=int)
    work_claim.add_argument("--idempotency-key")
    work_complete = work_sub.add_parser("complete", help="Complete work item")
    work_complete.add_argument("work_id")
    work_complete.add_argument("--actor", default="agent")
    work_complete.add_argument("--revision", type=int)
    work_complete.add_argument("--idempotency-key")
    work_fail = work_sub.add_parser("fail", help="Fail work item")
    work_fail.add_argument("work_id")
    work_fail.add_argument("--actor", default="agent")
    work_fail.add_argument("--reason", default="")
    work_fail.add_argument("--revision", type=int)
    work_fail.add_argument("--idempotency-key")
    work_retry = work_sub.add_parser("retry", help="Retry failed work item")
    work_retry.add_argument("work_id")
    work_retry.add_argument("--actor", default="agent")
    work_retry.add_argument("--revision", type=int)
    work_retry.add_argument("--idempotency-key")

    resource = sub.add_parser("resource", help="Resource lease operations")
    resource_sub = resource.add_subparsers(dest="resource_command", required=True)
    resource_sub.add_parser("list", help="List resources")
    resource_show = resource_sub.add_parser("show", help="Show resource")
    resource_show.add_argument("resource_id")
    resource_claim = resource_sub.add_parser("claim", help="Claim resource")
    resource_claim.add_argument("resource_id")
    resource_claim.add_argument("--holder", required=True)
    resource_claim.add_argument("--kind", default="generic")
    resource_claim.add_argument(
        "--policy",
        choices=[ClaimPolicy.ADVISORY, ClaimPolicy.STRICT],
        default=ClaimPolicy.STRICT,
    )
    resource_claim.add_argument("--force", action="store_true")
    resource_claim.add_argument("--reason", default="")
    resource_claim.add_argument("--idempotency-key")
    resource_renew = resource_sub.add_parser("renew", help="Renew resource lease")
    resource_renew.add_argument("resource_id")
    resource_renew.add_argument("--holder", required=True)
    resource_renew.add_argument("--idempotency-key")
    resource_release = resource_sub.add_parser("release", help="Release resource lease")
    resource_release.add_argument("resource_id")
    resource_release.add_argument("--holder", required=True)
    resource_release.add_argument("--revision", type=int)
    resource_release.add_argument("--idempotency-key")

    gate = sub.add_parser("gate", help="Gate operations")
    gate_sub = gate.add_subparsers(dest="gate_command", required=True)
    gate_list = gate_sub.add_parser("list", help="List gates")
    gate_list.add_argument("--work")
    gate_pass = gate_sub.add_parser("pass", help="Pass gate")
    gate_pass.add_argument("gate_id")
    gate_pass.add_argument("--actor", default="agent")
    gate_pass.add_argument("--idempotency-key")
    gate_fail = gate_sub.add_parser("fail", help="Fail gate")
    gate_fail.add_argument("gate_id")
    gate_fail.add_argument("--actor", default="agent")
    gate_fail.add_argument("--idempotency-key")
    gate_waive = gate_sub.add_parser("waive", help="Waive gate")
    gate_waive.add_argument("gate_id")
    gate_waive.add_argument("--actor", default="agent")
    gate_waive.add_argument("--authority", required=True)
    gate_waive.add_argument("--reason", required=True)
    gate_waive.add_argument("--evidence-artifact-id", required=True)
    gate_waive.add_argument("--revision", type=int)
    gate_waive.add_argument("--policy-version-id")
    gate_waive.add_argument("--idempotency-key")

    event = sub.add_parser("event", help="Event ledger operations")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    event_list = event_sub.add_parser("list", help="List events")
    event_list.add_argument("--limit", type=int, default=100)
    event_list.add_argument("--type")

    add_capability_parser(sub)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    db_path = resolve_db_path(args.db)

    try:
        if args.command == "init":
            return _cmd_init(args, db_path)
        if args.command == "cap":
            return run_capability_command(args, db_path)
        with db_session(db_path) as conn:
            require_initialized(conn)
            if args.command == "status":
                emit_result(status(conn), as_json=args.json)
            elif args.command == "export":
                emit_result(export_all(conn), as_json=True)
            elif args.command == "queue":
                return _cmd_queue(args, conn)
            elif args.command == "work":
                return _cmd_work(args, conn)
            elif args.command == "resource":
                return _cmd_resource(args, conn)
            elif args.command == "gate":
                return _cmd_gate(args, conn)
            elif args.command == "event":
                return _cmd_event(args, conn)
        return 0
    except AdvisoryConflictError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ConflictError, InvalidTransitionError, NotFoundError, FlowError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 3


def _cmd_init(args: argparse.Namespace, db_path) -> int:
    with db_session(db_path, initialize=True) as conn:
        with transaction(conn):
            project = init_project(conn, name=args.project)
            queues = []
            for name in args.queue or ["default"]:
                queues.append(ensure_queue(conn, name=name))
        emit_result({"project": project, "queues": queues, "db": str(db_path)}, as_json=args.json)
    return 0


def _cmd_queue(args: argparse.Namespace, conn) -> int:
    if args.queue_command == "list":
        emit_result(list_queues(conn), as_json=args.json)
    elif args.queue_command == "show":
        emit_result(show_queue(conn, args.name), as_json=args.json)
    return 0


def _cmd_work(args: argparse.Namespace, conn) -> int:
    with transaction(conn):
        if args.work_command == "submit":
            result = submit_work(
                conn,
                queue_name=args.queue,
                payload=args.payload,
                actor=args.actor,
                depends_on=args.depends_on,
                idempotency_key=args.idempotency_key,
            )
        elif args.work_command == "list":
            result = list_work(conn, queue_name=args.queue, status=args.status)
        elif args.work_command == "show":
            result = show_work(conn, args.work_id)
        elif args.work_command == "claim":
            result = claim_work(
                conn,
                actor=args.actor,
                work_id=args.work_id,
                queue_name=args.queue,
                expected_revision=args.revision,
                idempotency_key=args.idempotency_key,
            )
        elif args.work_command == "complete":
            result = complete_work(
                conn,
                work_id=args.work_id,
                actor=args.actor,
                expected_revision=args.revision,
                idempotency_key=args.idempotency_key,
            )
        elif args.work_command == "fail":
            result = fail_work(
                conn,
                work_id=args.work_id,
                actor=args.actor,
                reason=args.reason,
                expected_revision=args.revision,
                idempotency_key=args.idempotency_key,
            )
        elif args.work_command == "retry":
            result = retry_work(
                conn,
                work_id=args.work_id,
                actor=args.actor,
                expected_revision=args.revision,
                idempotency_key=args.idempotency_key,
            )
        else:
            raise RuntimeError(f"unknown work command: {args.work_command}")
    emit_result(result, as_json=args.json)
    return 0


def _cmd_resource(args: argparse.Namespace, conn) -> int:
    with transaction(conn):
        if args.resource_command == "list":
            result = list_resources(conn)
        elif args.resource_command == "show":
            result = show_resource(conn, args.resource_id)
        elif args.resource_command == "claim":
            result = claim_resource(
                conn,
                resource_id=args.resource_id,
                holder=args.holder,
                kind=args.kind,
                claim_policy=ClaimPolicy(args.policy),
                force=args.force,
                reason=args.reason,
                actor=args.holder,
                idempotency_key=args.idempotency_key,
            )
        elif args.resource_command == "renew":
            result = renew_resource(
                conn,
                resource_id=args.resource_id,
                holder=args.holder,
                idempotency_key=args.idempotency_key,
            )
        elif args.resource_command == "release":
            result = release_resource(
                conn,
                resource_id=args.resource_id,
                holder=args.holder,
                expected_revision=args.revision,
                idempotency_key=args.idempotency_key,
            )
        else:
            raise RuntimeError(f"unknown resource command: {args.resource_command}")
    emit_result(result, as_json=args.json)
    return 0


def _cmd_gate(args: argparse.Namespace, conn) -> int:
    with transaction(conn):
        if args.gate_command == "list":
            result = list_gates(conn, work_item_id=args.work)
        elif args.gate_command == "pass":
            result = pass_gate(
                conn, gate_id=args.gate_id, actor=args.actor, idempotency_key=args.idempotency_key
            )
        elif args.gate_command == "fail":
            result = fail_gate(
                conn, gate_id=args.gate_id, actor=args.actor, idempotency_key=args.idempotency_key
            )
        elif args.gate_command == "waive":
            result = waive_gate(
                conn,
                gate_id=args.gate_id,
                actor=args.actor,
                authority=args.authority,
                reason=args.reason,
                evidence_artifact_id=args.evidence_artifact_id,
                expected_revision=args.revision,
                policy_version_id=args.policy_version_id,
                idempotency_key=args.idempotency_key,
            )
        else:
            raise RuntimeError(f"unknown gate command: {args.gate_command}")
    emit_result(result, as_json=args.json)
    return 0


def _cmd_event(args: argparse.Namespace, conn) -> int:
    if args.event_command == "list":
        emit_result(
            list_events(conn, limit=args.limit, event_type=args.type),
            as_json=args.json,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
