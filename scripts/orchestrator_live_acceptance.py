#!/usr/bin/env python3
"""Live API acceptance against persistent local stack (.tmp/local-stack/manifest.json).

Runs R4D primary exercises (health, mock runtime, MCP lanes, scripts, schedules)
plus ops summary and delegation-coordination invoke wiring checks.
Requires: bash scripts/local_stack_up.sh first.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.auth_founder_register_acceptance import check_auth_founder_register  # noqa: E402
from scripts.local_stack_helpers import (  # noqa: E402
    refresh_work_item,
    reset_local_acceptance_budget,
)
from scripts.r4d_exercise import ApiClient, _load_env, _redact, run_primary_exercises  # noqa: E402
from scripts.verification_ladder import default_run_id, write_json  # noqa: E402


def load_manifest(root: Path) -> dict[str, Any]:
    path = Path(os.environ.get("ORCH_LOCAL_STACK_MANIFEST", root / ".tmp/local-stack/manifest.json"))
    if not path.is_file():
        raise FileNotFoundError(
            f"missing {path}; run: bash scripts/local_stack_up.sh"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def check_ops_summary(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ok = body.get("status") in {"ok", "degraded"}
    return {"passed": ok, "status": body.get("status"), "url": url}


def check_delegation_invoke(api: ApiClient) -> dict[str, Any]:
    status, body = api.request(
        "POST",
        "/api/v1/mcp/lanes/delegation-coordination/tools/invoke",
        mcp_lane="delegation-coordination",
        body={
            "tool": "request",
            "arguments": {
                "parent_assignment_id": "acceptance-missing-assignment",
                "to_position_id": "acceptance-missing-position",
            },
        },
        expected={200, 400, 422},
    )
    mode = (body.get("result") or {}).get("mode")
    command_type = body.get("command_type")
    passed = mode != "delegation_read" and command_type == "delegation.request"
    return {
        "passed": passed,
        "http_status": status,
        "command_type": command_type,
        "mode": mode,
    }


def main() -> int:
    manifest = load_manifest(ROOT)
    env_file = Path(manifest["env_file"])
    api_base = manifest["api_base"].rstrip("/")
    evidence_dir = ROOT / ".tmp/local-stack/acceptance" / default_run_id("live-api")
    evidence_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("ORCH_R4D_ENV_FILE", str(env_file))
    os.environ.setdefault("ORCH_R4D_EVIDENCE_DIR", str(evidence_dir))
    os.environ.setdefault("ORCH_R4D_API_BASE", api_base)
    os.environ.setdefault("ORCH_R4D_WORK_ITEM_ID", manifest["work_item_id"])

    rows: list[dict[str, Any]] = []

    ops = check_ops_summary(manifest["ops_summary_url"])
    rows.append({"step": "ops_summary", **ops})
    write_json(evidence_dir / "ops_summary.json", ops)

    reset_local_acceptance_budget(manifest)
    refresh_work_item(manifest)
    os.environ["ORCH_R4D_WORK_ITEM_ID"] = manifest["work_item_id"]
    r4d_rc = run_primary_exercises()
    r4d_summary_path = evidence_dir / "summary.json"
    r4d_summary = (
        json.loads(r4d_summary_path.read_text(encoding="utf-8"))
        if r4d_summary_path.is_file()
        else {"ok": r4d_rc == 0}
    )
    rows.append(
        {
            "step": "r4d_primary_exercises",
            "passed": r4d_rc == 0 and r4d_summary.get("ok", False),
            "steps": r4d_summary.get("steps"),
        }
    )

    env = _load_env(env_file)
    api = ApiClient(api_base, env)

    auth_register = check_auth_founder_register(api, env, evidence_dir=evidence_dir)
    rows.append({"step": "auth_founder_register", **auth_register})
    write_json(evidence_dir / "auth_founder_register.json", auth_register)

    delegation = check_delegation_invoke(api)
    rows.append({"step": "mcp_delegation_invoke", **delegation})
    write_json(evidence_dir / "delegation_invoke.json", delegation)

    passed = all(r.get("passed") for r in rows)
    summary = {
        "run_id": evidence_dir.name,
        "captured_at": datetime.now(UTC).isoformat(),
        "manifest": _redact(manifest),
        "passed": passed,
        "rows": rows,
    }
    write_json(evidence_dir / "acceptance_summary.json", summary)
    print(json.dumps(_redact(summary), indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
