#!/usr/bin/env python3
"""R6 external adapter bounded proof evidence capture per HQ R6 hub.

Invokes the external read-only status adapter stub and records expected-vs-actual
rows under ``.tmp/r6-external-adapter/<run_id>/summary.json``.

Governed path intent:
- maintenance-class work item (zero provider budget envelope)
- external adapter boundary — no Orchestrator core contamination
- evidence for ``G-ORCH-PROOF-PORTFOLIO`` packet; does **not** close the gate

Does not close gates.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.verification_ladder import default_run_id, write_json  # noqa: E402

ADAPTER_ID = "portfolio-readonly-status-adapter-v0"
EVIDENCE_HUB_REF = (
    "programs/orchestrator-platform/agentic-control-plane/"
    "discussions/r6-portfolio-proof-2026-07-28/README.md"
)
SCOPE_REF = (
    "programs/orchestrator-platform/agentic-control-plane/"
    "discussions/r6-portfolio-proof-2026-07-28/scope.md"
)
GOVERNED_PATH = {
    "class": "maintenance",
    "credit_envelope": "zero-provider-budget",
    "adapter_boundary": "external-script-sandbox",
    "core_contamination": False,
    "gate": "G-ORCH-PROOF-PORTFOLIO",
    "gate_closed": False,
}
DEFAULT_SIBLING = "Port" + "folio"


def resolve_adapter_repo_root(root: Path) -> Path:
    override = os.environ.get("ADAPTER_REPO_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    sibling = (root.parent / DEFAULT_SIBLING).resolve()
    if sibling.is_dir():
        return sibling
    raise FileNotFoundError(
        "External adapter repo not found; set ADAPTER_REPO_ROOT to the checkout root"
    )


def row(
    *,
    step: str,
    expected: str,
    actual: str,
    passed: bool,
    evidence_artifact: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step": step,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "evidence_artifact": evidence_artifact,
    }
    if detail is not None:
        payload["detail"] = detail
    return payload


def invoke_status_stub(*, adapter_repo_root: Path, run_dir: Path) -> dict[str, Any]:
    script = adapter_repo_root / "scripts" / "run_status_stub.py"
    raw_path = run_dir / "adapter_stdout.json"
    detail: dict[str, Any] = {
        "adapter_repo_root": str(adapter_repo_root),
        "script": str(script),
        "invocation": "subprocess",
    }

    if not script.is_file():
        detail["error"] = "run_status_stub.py not found"
        return {
            "passed": False,
            "detail": detail,
            "payload": None,
            "raw_path": str(raw_path),
        }

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=adapter_repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    detail["returncode"] = proc.returncode
    detail["stderr"] = proc.stderr.strip() or None

    if proc.returncode != 0:
        detail["error"] = "non-zero exit from status stub"
        return {
            "passed": False,
            "detail": detail,
            "payload": None,
            "raw_path": str(raw_path),
        }

    stdout = proc.stdout.strip()
    raw_path.write_text(stdout + "\n", encoding="utf-8")

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        detail["error"] = f"invalid JSON: {exc}"
        return {
            "passed": False,
            "detail": detail,
            "payload": None,
            "raw_path": str(raw_path),
        }

    detail["payload_keys"] = sorted(payload.keys())
    checks = {
        "adapter_id": payload.get("adapter") == ADAPTER_ID,
        "status_ok": payload.get("status") == "ok",
        "health_healthy": payload.get("health") == "healthy",
        "readonly": payload.get("readonly") is True,
        "no_secrets_flag": payload.get("secrets_present") is False,
        "maintenance_envelope": payload.get("credit_envelope")
        == "maintenance-class-zero-provider-budget",
    }
    detail["checks"] = checks
    passed = all(checks.values())

    return {
        "passed": passed,
        "detail": detail,
        "payload": payload,
        "raw_path": str(raw_path),
    }


def build_rows(*, stub: dict[str, Any], run_dir: Path) -> list[dict[str, Any]]:
    payload = stub.get("payload") or {}
    return [
        row(
            step="External adapter reachable",
            expected=f"{ADAPTER_ID} script exits 0 from external repo checkout",
            actual="exit 0 with JSON stdout"
            if stub["passed"] or stub.get("detail", {}).get("returncode") == 0
            else stub.get("detail", {}).get("error", "adapter invocation failed"),
            passed=stub.get("detail", {}).get("returncode") == 0,
            evidence_artifact=stub["raw_path"],
            detail=stub.get("detail"),
        ),
        row(
            step="Read-only status contract",
            expected="JSON status ok/healthy, readonly, no secrets flag",
            actual=(
                f"adapter={payload.get('adapter')} status={payload.get('status')} "
                f"health={payload.get('health')} readonly={payload.get('readonly')} "
                f"secrets_present={payload.get('secrets_present')}"
            )
            if payload
            else "payload missing or invalid",
            passed=stub["passed"],
            evidence_artifact=stub["raw_path"],
            detail=stub.get("detail", {}).get("checks"),
        ),
        row(
            step="Governed lifecycle dispatch",
            expected="maintenance-class external adapter through Orchestrator script sandbox",
            actual="local evidence capture — full governed dispatch deferred to slice execution",
            passed=False,
            evidence_artifact="pending coordinator delivery JSON",
        ),
        row(
            step="Independent QA review",
            expected="reviewer attestation separate from implementer",
            actual="deferred — independent review pending F4",
            passed=False,
            evidence_artifact="pending QA review report",
        ),
        row(
            step="VPS staging surface",
            expected="HTTPS health on www.pproctor.com when HitM cutover authorized",
            actual="deferred — local stub only; VPS cutover is separate HitM step",
            passed=False,
            evidence_artifact="pending VPS verify checklist",
        ),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--adapter-repo-root",
        type=Path,
        default=None,
        help="External adapter repo root (default: ADAPTER_REPO_ROOT or sibling checkout)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    adapter_repo_root = (
        args.adapter_repo_root or resolve_adapter_repo_root(root)
    ).resolve()
    run_id = args.run_id or default_run_id("r6-external-adapter")
    run_dir = root / ".tmp" / "r6-external-adapter" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    stub = invoke_status_stub(adapter_repo_root=adapter_repo_root, run_dir=run_dir)
    write_json(run_dir / "adapter_invocation.json", stub)

    rows = build_rows(stub=stub, run_dir=run_dir)
    automated_passed = all(
        item["passed"]
        for item in rows
        if item["step"]
        in {
            "External adapter reachable",
            "Read-only status contract",
        }
    )

    summary = {
        "run_id": run_id,
        "root": str(root),
        "adapter_repo_root": str(adapter_repo_root),
        "adapter_id": ADAPTER_ID,
        "evidence_hub": EVIDENCE_HUB_REF,
        "scope_ref": SCOPE_REF,
        "governed_path": GOVERNED_PATH,
        "gates_closed": False,
        "note": "R6 external adapter bounded proof evidence; G-ORCH-PROOF-PORTFOLIO remains open",
        "rows": rows,
        "adapter": {
            "invocation": stub,
            "payload": stub.get("payload"),
        },
        "automated_passed": automated_passed,
        "passed": automated_passed,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    summary_path = run_dir / "summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary, indent=2))
    return 0 if automated_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
