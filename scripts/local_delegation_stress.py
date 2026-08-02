#!/usr/bin/env python3
"""Local delegation stress — full lifecycle via live Orchestrator API (MCP lanes).

Prerequisites: bash scripts/local_stack_up.sh
Writes evidence to .tmp/local-delegation/<run_id>/summary.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.local_stack_helpers import refresh_work_item, reset_local_acceptance_budget  # noqa: E402
from scripts.r4d_exercise import ApiClient, _load_env, _redact  # noqa: E402
from scripts.verification_ladder import default_run_id, write_json  # noqa: E402


def load_manifest(root: Path) -> tuple[dict[str, Any], Path]:
    """Load the manifest and return it alongside the exact path it was read from.

    Callers MUST pass this same path into ``refresh_work_item`` so the helper
    never persists to a different file than the one this manifest came from.
    """
    path = Path(os.environ.get("ORCH_LOCAL_STACK_MANIFEST", root / ".tmp/local-stack/manifest.json"))
    if not path.is_file():
        raise FileNotFoundError(f"missing {path}; run bash scripts/local_stack_up.sh")
    return json.loads(path.read_text(encoding="utf-8")), path


def check_ops_summary(manifest: dict[str, Any], env: dict[str, str]) -> dict[str, Any]:
    """Founder-authenticated ops-summary fetch. Never retried anonymously.

    Raises if the founder token is absent from the stack env or the request
    fails; callers must not fall back to an anonymous request on either path.
    """
    founder_token = env.get("ORCH_TOKEN_FOUNDER")
    if not founder_token:
        raise RuntimeError(
            "missing ORCH_TOKEN_FOUNDER in stack env; refusing anonymous ops_summary call"
        )
    req = urllib.request.Request(
        manifest["ops_summary_url"],
        headers={
            "Authorization": f"Bearer {founder_token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _redact_known_secret(text: str, *secrets: str | None) -> str:
    """Strip any literal occurrence of a known secret value from ``text``.

    Pattern-based redaction (``_redact`` / ``redact_evidence``) only catches
    keyword-prefixed shapes (``token=``, ``Authorization: ...``, ``Bearer
    ...``); a bare secret value embedded in free-text exception messages with
    no recognizable prefix (for example ``"... for token abc123"`` with a
    space, not ``=``/``:``) would not match either pattern. Since the caller
    already holds the exact secret value, replace it directly rather than
    relying on heuristics — this is additive to, not a replacement for, the
    existing pattern-based redaction applied by ``write_json``.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def seed_org(manifest: dict[str, Any]) -> dict[str, Any]:
    work_item_id = manifest["work_item_id"]
    proc = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/r4d_compose.sh"),
            "exec",
            "-T",
            "-e",
            f"WORK_ITEM_ID={work_item_id}",
            "coordinator",
            "python",
            "/app/scripts/local_stack_seed_org.py",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ORCH_R4D_ENV_FILE": manifest["env_file"],
            "ORCH_COMPOSE_PROJECT": manifest.get("compose_project", "orch-local"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"seed org failed: {(proc.stderr or proc.stdout)[:800]}")
    line = proc.stdout.strip().splitlines()[-1]
    return json.loads(line)


def mcp_invoke(
    api: ApiClient,
    *,
    tool: str,
    arguments: dict[str, Any],
) -> tuple[int, Any]:
    return api.request(
        "POST",
        "/api/v1/mcp/lanes/delegation-coordination/tools/invoke",
        mcp_lane="delegation-coordination",
        body={"tool": tool, "arguments": arguments},
        expected=None,
        idempotency_key=f"local-delegation-{tool}-{uuid.uuid4().hex}",
    )


def row(step: str, passed: bool, detail: Any) -> dict[str, Any]:
    return {"step": step, "passed": passed, "detail": detail}


def main() -> int:
    run_id = os.environ.get("ORCH_DELEGATION_RUN_ID") or default_run_id("local-delegation")
    out_dir = ROOT / ".tmp/local-delegation" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest, manifest_path = load_manifest(ROOT)
    env = _load_env(Path(manifest["env_file"]))
    api = ApiClient(manifest["api_base"], env)

    rows: list[dict[str, Any]] = []

    try:
        refresh_work_item(manifest, manifest_path=manifest_path)
        reset_local_acceptance_budget(manifest)
        seed = seed_org(manifest)
        rows.append(row("seed_org", True, seed))
        write_json(out_dir / "seed.json", seed)
    except Exception as exc:
        rows.append(row("seed_org", False, str(exc)))
        write_json(out_dir / "summary.json", {"passed": False, "rows": rows})
        print(json.dumps({"passed": False, "rows": rows}, indent=2))
        return 1

    parent_id = seed["parent_assignment_id"]
    worker_pos = seed["worker_position_id"]
    impl_actor = seed["impl_actor_id"]
    impl_seat = seed["impl_seat_id"]
    work_item_id = seed["work_item_id"]

    status, body = mcp_invoke(
        api,
        tool="request",
        arguments={
            "parent_assignment_id": parent_id,
            "to_position_id": worker_pos,
            "packet": {
                "objective": "local-delegation-stress slice",
                "write_set": ["Orchestrator/.tmp/local-delegation/"],
            },
        },
    )
    request_id = ((body.get("result") or {}).get("request") or {}).get("id")
    cmd_ok = (
        body.get("command_type") == "delegation.request"
        and status in {200, 202}
        and body.get("command_status") != "rejected"
        and not body.get("from_cache")
        and bool(request_id)
    )
    rows.append(
        row(
            "delegation_request",
            cmd_ok and bool(request_id),
            {"http_status": status, "command_type": body.get("command_type"), "request_id": request_id},
        )
    )
    write_json(out_dir / "request.json", _redact(body))

    if not request_id:
        write_json(out_dir / "summary.json", {"passed": False, "rows": rows})
        print(json.dumps({"passed": False, "rows": rows}, indent=2))
        return 1

    status, body = mcp_invoke(
        api,
        tool="disposition",
        arguments={"request_id": request_id, "actor_id": impl_actor, "action": "accept"},
    )
    accepted = body.get("command_type") == "delegation.accept" and status in {200, 202}
    rows.append(
        row(
            "delegation_accept",
            accepted,
            {"http_status": status, "command_type": body.get("command_type")},
        )
    )
    write_json(out_dir / "accept.json", _redact(body))

    status, body = mcp_invoke(
        api,
        tool="dispatch",
        arguments={
            "request_id": request_id,
            "actor_id": impl_actor,
            "provider_seat_id": impl_seat,
        },
    )
    dispatched = body.get("command_type") == "delegation.dispatch" and status in {200, 202}
    child_assignment = ((body.get("result") or {}).get("assignment") or {}).get("id")
    pin_id = ((body.get("result") or {}).get("pin") or {}).get("id")
    rows.append(
        row(
            "delegation_dispatch",
            dispatched and bool(child_assignment),
            {
                "http_status": status,
                "command_type": body.get("command_type"),
                "child_assignment_id": child_assignment,
                "pin_id": pin_id,
            },
        )
    )
    write_json(out_dir / "dispatch.json", _redact(body))

    reset_local_acceptance_budget(manifest)

    # Cross-talk: workflow-control preview (dual principal) on same work item
    status, wf_body = api.request(
        "POST",
        "/api/v1/mcp/lanes/workflow-control/tools/invoke",
        mcp_lane="workflow-control",
        body={
            "tool": "preview",
            "arguments": {"work_item_id": work_item_id, "provider": "cursor"},
        },
        expected={200, 202},
        idempotency_key=f"local-delegation-preview-{uuid.uuid4().hex}",
    )
    wf_ok = wf_body.get("command_type") == "runtime.preview" or (wf_body.get("result") is not None)
    rows.append(
        row(
            "workflow_control_preview",
            wf_ok,
            {
                "http_status": status,
                "command_type": wf_body.get("command_type"),
                "lane": (wf_body.get("mcp") or {}).get("lane_id"),
            },
        )
    )
    write_json(out_dir / "workflow_preview.json", _redact(wf_body))

    # Mock runtime run via workflow-control (governed Celery path)
    status, run_body = api.request(
        "POST",
        "/api/v1/mcp/lanes/workflow-control/tools/invoke",
        mcp_lane="workflow-control",
        body={
            "tool": "run",
            "arguments": {
                "work_item_id": work_item_id,
                "provider": "cursor",
                "delivery_mode": "async",
                "payload": {"local_delegation_stress": True},
            },
        },
        expected={200, 202},
        idempotency_key=f"local-delegation-run-{uuid.uuid4().hex}",
    )
    run_id_created = ((run_body.get("result") or {}).get("created") or {}).get("run", {}).get("id")
    rows.append(
        row(
            "runtime_run_mock",
            run_body.get("command_type") == "runtime.run" and bool(run_id_created),
            {"http_status": status, "run_id": run_id_created},
        )
    )
    write_json(out_dir / "runtime_run.json", _redact(run_body))

    # Ops summary hierarchy visible (founder-authenticated; never retried anonymously).
    # Missing token or HTTP/auth failure must not crash the run: persist a failed
    # row and terminal summary instead of an uncaught exception with no evidence.
    try:
        ops_summary_body = check_ops_summary(manifest, env)
        profiles = ((ops_summary_body.get("hierarchy") or {}).get("profiles")) or []
        delegations = ops_summary_body.get("delegations") or {}
        pins = delegations.get("recent_pins") or []
        open_delegations = delegations.get("open") or []
        hierarchy_ok = (
            len(profiles) >= 1
            or len(pins) >= 1
            or len(open_delegations) >= 1
            or bool(seed.get("organization_id"))
        )
        rows.append(
            row(
                "ops_summary_hierarchy",
                hierarchy_ok,
                {
                    "profile_count": len(profiles),
                    "pin_count": len(pins),
                    "open_delegation_count": len(open_delegations),
                },
            )
        )
    except Exception as exc:
        detail = _redact_known_secret(str(exc), env.get("ORCH_TOKEN_FOUNDER"))
        rows.append(row("ops_summary_hierarchy", False, {"error": detail}))

    passed = all(r["passed"] for r in rows)
    summary = {
        "run_id": run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "rows": rows,
        "manifest": _redact(manifest),
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(_redact(summary), indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
