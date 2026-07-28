"""CLI helpers for R3 organization and delegation via the state coordinator."""

from __future__ import annotations

import argparse
import json
from typing import Any

from flow_engine.cli.output import emit_result
from flow_engine.coordinator import CommandContext, RuntimeCommand, StateCoordinator
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.persistence.transactions import transaction


def _parse_json_obj(value: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return parsed


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--actor", default="agent")
    parser.add_argument(
        "--role",
        choices=[r.value for r in PrincipalRole],
        default="executive",
    )
    parser.add_argument("--idempotency-key")


def _ctx(args: argparse.Namespace) -> CommandContext:
    return CommandContext(
        principal_id=args.actor,
        role=PrincipalRole(args.role),
        surface=Surface.CLI,
        grant=None,
    )


def add_org_parser(sub: argparse._SubParsersAction) -> None:
    org = sub.add_parser("org", help="R3 organization / loadout controls")
    org_sub = org.add_subparsers(dest="org_command", required=True)

    create = org_sub.add_parser("create-profile", help="Create organization profile")
    create.add_argument("--name", required=True)
    create.add_argument("--policy-revision", default="r3-default")
    create.add_argument("--profile", type=_parse_json_obj)
    _common(create)

    show = org_sub.add_parser("show-profile", help="Show organization profile")
    show.add_argument("organization_id")
    _common(show)

    list_p = org_sub.add_parser("list-profiles", help="List organization profiles")
    _common(list_p)

    add_actor = org_sub.add_parser("add-actor", help="Add organization actor")
    add_actor.add_argument("organization_id")
    add_actor.add_argument("--actor-key", required=True)
    add_actor.add_argument("--display-name")
    _common(add_actor)

    add_seat = org_sub.add_parser("add-seat", help="Add provider seat binding")
    add_seat.add_argument("organization_id")
    add_seat.add_argument("--actor-id", required=True)
    add_seat.add_argument("--provider", required=True)
    add_seat.add_argument("--seat-key", required=True)
    _common(add_seat)

    members = org_sub.add_parser("members", help="List actors, seats, positions")
    members.add_argument("organization_id")
    _common(members)

    position = org_sub.add_parser("find-position", help="Find department×position")
    position.add_argument("organization_id")
    position.add_argument("--department", required=True)
    position.add_argument("--position", required=True)
    _common(position)

    preview = org_sub.add_parser("loadout-preview", help="Preview resolved loadout")
    preview.add_argument("organization_id")
    preview.add_argument("--loadout-id", required=True)
    _common(preview)

    snap = org_sub.add_parser("snapshot", help="Materialize resolved-loadout snapshot")
    snap.add_argument("organization_id")
    snap.add_argument("--loadout-id", required=True)
    _common(snap)

    show_snap = org_sub.add_parser("show-snapshot", help="Inspect resolved snapshot")
    show_snap.add_argument("snapshot_id")
    _common(show_snap)

    assign = org_sub.add_parser("assign", help="Create positional assignment")
    assign.add_argument("organization_id")
    assign.add_argument("--work-item-id", required=True)
    assign.add_argument("--position-id", required=True)
    assign.add_argument("--actor-id", required=True)
    assign.add_argument("--provider-seat-id", required=True)
    assign.add_argument("--parent-assignment-id")
    _common(assign)

    complete = org_sub.add_parser("complete-assignment", help="Complete assignment")
    complete.add_argument("assignment_id")
    _common(complete)


def add_delegation_parser(sub: argparse._SubParsersAction) -> None:
    dele = sub.add_parser("delegation", help="R3 delegation / handoff controls")
    dele_sub = dele.add_subparsers(dest="delegation_command", required=True)

    request = dele_sub.add_parser("request", help="Request scoped delegation")
    request.add_argument("--parent-assignment-id", required=True)
    request.add_argument("--to-position-id", required=True)
    request.add_argument("--packet", type=_parse_json_obj, required=True)
    _common(request)

    accept = dele_sub.add_parser("accept", help="Accept delegation request")
    accept.add_argument("request_id")
    accept.add_argument("--actor-id", required=True)
    _common(accept)

    decline = dele_sub.add_parser("decline", help="Decline delegation request")
    decline.add_argument("request_id")
    decline.add_argument("--actor-id", required=True)
    decline.add_argument("--reason", default="")
    _common(decline)

    reroute = dele_sub.add_parser("reroute", help="Reroute delegation request")
    reroute.add_argument("request_id")
    reroute.add_argument("--actor-id", required=True)
    reroute.add_argument("--to-position-id", required=True)
    reroute.add_argument("--reason", default="")
    _common(reroute)

    dispatch = dele_sub.add_parser("dispatch", help="Dispatch accepted delegation")
    dispatch.add_argument("request_id")
    dispatch.add_argument("--actor-id", required=True)
    dispatch.add_argument("--provider-seat-id", required=True)
    _common(dispatch)

    handoff = dele_sub.add_parser("handoff", help="Packet-only handoff")
    handoff.add_argument("--from-assignment-id", required=True)
    handoff.add_argument("--to-assignment-id", required=True)
    handoff.add_argument("--packet", type=_parse_json_obj, required=True)
    handoff.add_argument("--review-required", action="store_true", default=True)
    handoff.add_argument("--no-review-required", action="store_true")
    _common(handoff)

    accept_h = dele_sub.add_parser("accept-handoff", help="Accept handoff evidence")
    accept_h.add_argument("handoff_id")
    accept_h.add_argument("--evidence", type=_parse_json_obj, default={})
    _common(accept_h)

    mint = dele_sub.add_parser("mint-grant", help="Mint R3 resolved task grant")
    mint.add_argument("--organization-id", required=True)
    mint.add_argument("--assignment-id", required=True)
    mint.add_argument("--budget-scope-id", required=True)
    mint.add_argument("--providers", default="codex,cursor,claude")
    _common(mint)

    show_req = dele_sub.add_parser("show-request", help="Show delegation request")
    show_req.add_argument("request_id")
    _common(show_req)

    show_pin = dele_sub.add_parser("show-pin", help="Inspect immutable dispatch pin")
    show_pin.add_argument("pin_id")
    _common(show_pin)


def run_org_command(args: argparse.Namespace, conn) -> int:
    ctx = _ctx(args)
    coord = StateCoordinator(conn)
    cmd = args.org_command
    mapping = {
        "create-profile": (
            "org.create_profile",
            None,
            {
                "name": args.name,
                "policy_revision": args.policy_revision,
                "profile": getattr(args, "profile", None),
            },
        ),
        "show-profile": ("org.get_profile", args.organization_id, {}),
        "list-profiles": ("org.list_profiles", None, {}),
        "add-actor": (
            "org.add_actor",
            None,
            {
                "organization_id": args.organization_id,
                "actor_key": args.actor_key,
                "display_name": args.display_name,
            },
        ),
        "add-seat": (
            "org.add_provider_seat",
            None,
            {
                "organization_id": args.organization_id,
                "actor_id": args.actor_id,
                "provider": args.provider,
                "seat_key": args.seat_key,
            },
        ),
        "members": ("org.list_members", args.organization_id, {}),
        "find-position": (
            "org.find_position",
            None,
            {
                "organization_id": args.organization_id,
                "department_key": args.department,
                "position_key": args.position,
            },
        ),
        "loadout-preview": (
            "org.preview_loadout",
            None,
            {
                "organization_id": args.organization_id,
                "loadout_id": args.loadout_id,
            },
        ),
        "snapshot": (
            "org.materialize_snapshot",
            None,
            {
                "organization_id": args.organization_id,
                "loadout_id": args.loadout_id,
            },
        ),
        "show-snapshot": ("org.get_snapshot", args.snapshot_id, {}),
        "assign": (
            "org.create_assignment",
            None,
            {
                "organization_id": args.organization_id,
                "work_item_id": args.work_item_id,
                "position_id": args.position_id,
                "actor_id": args.actor_id,
                "provider_seat_id": args.provider_seat_id,
                "parent_assignment_id": args.parent_assignment_id,
            },
        ),
        "complete-assignment": ("org.complete_assignment", args.assignment_id, {}),
    }
    command_type, target, payload = mapping[cmd]
    with transaction(coord.connection):
        result = coord.accept(
            RuntimeCommand(
                command_type=command_type,
                target_id=target,
                payload={k: v for k, v in payload.items() if v is not None},
                idempotency_key=getattr(args, "idempotency_key", None),
                context=ctx,
            )
        )
    emit_result(result, as_json=args.json)
    return 0 if result.get("status") == "applied" else 1


def run_delegation_command(args: argparse.Namespace, conn) -> int:
    ctx = _ctx(args)
    coord = StateCoordinator(conn)
    cmd = args.delegation_command
    mapping: dict[str, tuple[str, str | None, dict[str, Any]]] = {
        "request": (
            "delegation.request",
            None,
            {
                "parent_assignment_id": args.parent_assignment_id,
                "to_position_id": args.to_position_id,
                "packet": args.packet,
            },
        ),
        "accept": (
            "delegation.accept",
            args.request_id,
            {"actor_id": args.actor_id},
        ),
        "decline": (
            "delegation.decline",
            args.request_id,
            {"actor_id": args.actor_id, "reason": args.reason},
        ),
        "reroute": (
            "delegation.reroute",
            args.request_id,
            {
                "actor_id": args.actor_id,
                "reroute_position_id": args.to_position_id,
                "reason": args.reason,
            },
        ),
        "dispatch": (
            "delegation.dispatch",
            args.request_id,
            {
                "actor_id": args.actor_id,
                "provider_seat_id": args.provider_seat_id,
            },
        ),
        "handoff": (
            "delegation.handoff",
            None,
            {
                "from_assignment_id": args.from_assignment_id,
                "to_assignment_id": args.to_assignment_id,
                "packet": args.packet,
                "review_required": not args.no_review_required,
            },
        ),
        "accept-handoff": (
            "delegation.accept_handoff",
            args.handoff_id,
            {"evidence": args.evidence},
        ),
        "mint-grant": (
            "delegation.mint_grant",
            None,
            {
                "organization_id": args.organization_id,
                "assignment_id": args.assignment_id,
                "budget_scope_id": args.budget_scope_id,
                "providers": [p.strip() for p in args.providers.split(",") if p.strip()],
                "role": args.role,
                "principal_id": args.actor,
            },
        ),
        "show-request": ("delegation.get_request", args.request_id, {}),
        "show-pin": ("delegation.get_pin", args.pin_id, {}),
    }
    command_type, target, payload = mapping[cmd]
    with transaction(coord.connection):
        result = coord.accept(
            RuntimeCommand(
                command_type=command_type,
                target_id=target,
                payload=payload,
                idempotency_key=getattr(args, "idempotency_key", None),
                context=ctx,
            )
        )
    emit_result(result, as_json=args.json)
    return 0 if result.get("status") == "applied" else 1
