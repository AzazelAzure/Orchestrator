"""CLI commands for read-only capabilities."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from flow_engine.capabilities.service import CapabilityService
from flow_engine.capabilities.transport import (
    CAPABILITY_CI_STATUS,
    CAPABILITY_OPEN_PRS,
    CAPABILITY_REPO_HEALTH,
    CAPABILITY_SESSION_BRIEF,
    CAPABILITY_WORK_LOOKUP,
    DEFAULT_CAPABILITY_TIMEOUT_SEC,
    build_request,
    capability_exit_code,
    dispatch_with_timeout,
    serialize_result,
)
from flow_engine.cli.output import emit_result


def add_capability_parser(sub: argparse._SubParsersAction) -> None:
    cap = sub.add_parser("cap", help="Read-only project capabilities")
    cap.add_argument("--projects-config", help="Path to projects.json")
    cap.add_argument("--actor", default="cli:user", help="Actor identifier")
    cap.add_argument("--request-id", help="Optional request correlation id")
    cap.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CAPABILITY_TIMEOUT_SEC,
        help="Capability timeout in seconds",
    )
    cap_sub = cap.add_subparsers(dest="cap_command", required=True)

    repo = cap_sub.add_parser("repo-health", help="Repository health for a logical project")
    repo.add_argument("--project", required=True, help="Logical project id")

    prs = cap_sub.add_parser("open-prs", help="Open pull request status")
    prs.add_argument("--project", required=True)
    prs.add_argument("--github-owner", required=True)
    prs.add_argument("--github-repo", required=True)

    ci = cap_sub.add_parser("ci-status", help="CI status for a repository ref")
    ci.add_argument("--project", required=True)
    ci.add_argument("--github-owner", required=True)
    ci.add_argument("--github-repo", required=True)
    ci.add_argument("--ref", required=True)

    work = cap_sub.add_parser("work-lookup", help="Engine work lookup")
    work.add_argument("--project", required=True)
    work.add_argument("--work-id")
    work.add_argument("--logical-work-id")

    brief = cap_sub.add_parser("session-brief", help="Session brief projection")
    brief.add_argument("--project", required=True)
    brief.add_argument("--work-id")
    brief.add_argument("--logical-work-id")
    brief.add_argument("--github-owner")
    brief.add_argument("--github-repo")


def run_capability_command(args: argparse.Namespace, db_path: Path) -> int:
    service = CapabilityService(
        db_path=db_path,
        projects_config=args.projects_config,
        provider_timeout_sec=min(args.timeout, DEFAULT_CAPABILITY_TIMEOUT_SEC),
    )

    try:
        request = _build_cli_request(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    result = dispatch_with_timeout(service, request, timeout_sec=args.timeout)
    emit_result(serialize_result(result), as_json=True)
    return capability_exit_code(result)


def _build_cli_request(args: argparse.Namespace):
    common = {
        "project_id": args.project,
        "actor": args.actor,
        "request_id": args.request_id,
    }
    if args.cap_command == "repo-health":
        return build_request(CAPABILITY_REPO_HEALTH, **common)
    if args.cap_command == "open-prs":
        return build_request(
            CAPABILITY_OPEN_PRS,
            **common,
            params={"github_owner": args.github_owner, "github_repo": args.github_repo},
        )
    if args.cap_command == "ci-status":
        return build_request(
            CAPABILITY_CI_STATUS,
            **common,
            params={
                "github_owner": args.github_owner,
                "github_repo": args.github_repo,
                "ref": args.ref,
            },
        )
    if args.cap_command == "work-lookup":
        if not args.work_id and not args.logical_work_id:
            raise ValueError("work-id or logical-work-id is required")
        params: dict[str, Any] = {}
        if args.work_id:
            params["work_id"] = args.work_id
        if args.logical_work_id:
            params["logical_work_id"] = args.logical_work_id
        return build_request(CAPABILITY_WORK_LOOKUP, **common, params=params)
    if args.cap_command == "session-brief":
        params = {}
        if args.work_id:
            params["work_id"] = args.work_id
        if args.logical_work_id:
            params["logical_work_id"] = args.logical_work_id
        if args.github_owner:
            params["github_owner"] = args.github_owner
        if args.github_repo:
            params["github_repo"] = args.github_repo
        return build_request(CAPABILITY_SESSION_BRIEF, **common, params=params)
    raise RuntimeError(f"unknown cap command: {args.cap_command}")
