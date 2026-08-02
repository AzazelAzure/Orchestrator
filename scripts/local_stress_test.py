#!/usr/bin/env python3
"""Local-use convergence stress test harness (6 slices, real-provider ready).

Writes expected-vs-actual rows to .tmp/local-stress/<run_id>/summary.json.
Does not close MVP or hosted gates.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.r4d_exercise import _load_env  # noqa: E402
from scripts.verification_ladder import default_run_id, run_l1, run_l2, write_json  # noqa: E402


def resolve_hq_root(root: Path) -> Path:
    override = os.environ.get("ORCH_HQ_ROOT", "").strip()
    if override:
        return Path(override).resolve()
    for candidate in (
        root.parent / "Headquarters",
        Path.home() / "Headquarters",
    ):
        resolved = candidate.resolve()
        if resolved.is_dir():
            return resolved
    return (root.parent / "Headquarters").resolve()


HQ_ROOT = resolve_hq_root(ROOT)


def row(step: str, expected: str, actual: str, passed: bool, evidence: str) -> dict:
    return {
        "step": step,
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "evidence_artifact": evidence,
    }


def check_hq_bridge() -> tuple[bool, str]:
    bridge = HQ_ROOT / ".local/hq-orch-bridge/summary.json"
    if bridge.is_file():
        return True, str(bridge)
    return False, "missing .local/hq-orch-bridge/summary.json (run bin/hq-orch-bridge)"


def _redact_known_secret(text: str, *secrets: str | None) -> str:
    """Strip any literal occurrence of a known secret value from ``text``.

    Pattern-based redaction (``redact_evidence`` / ``SECRET_PATTERN``) only
    catches keyword-prefixed shapes (``token=``, ``Authorization: ...``,
    ``Bearer ...``); a bare secret value embedded in free-text exception
    messages with no recognizable prefix would not match either pattern.
    Since the caller already holds the exact secret value, replace it
    directly rather than relying on heuristics.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def check_ops_summary() -> tuple[bool, str]:
    """Founder-authenticated ops-summary reachability check.

    Never falls back to an anonymous request: a missing manifest, missing
    env file, or missing ``ORCH_TOKEN_FOUNDER`` is a clear failure, and an
    HTTP 403 from an unauthenticated call must never count as reachable.
    """
    import urllib.request

    manifest_path = Path(
        os.environ.get("ORCH_LOCAL_STACK_MANIFEST", ROOT / ".tmp/local-stack/manifest.json")
    )
    if not manifest_path.is_file():
        return False, f"missing {manifest_path} (run bash scripts/local_stack_up.sh)"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"{manifest_path} ({exc})"

    url = os.environ.get("ORCH_SUMMARY_URL") or manifest.get(
        "ops_summary_url", "http://127.0.0.1:8000/ops/summary/"
    )
    try:
        env = _load_env(Path(manifest["env_file"]))
    except Exception as exc:
        return False, f"{url} (env load failed: {exc})"

    founder_token = env.get("ORCH_TOKEN_FOUNDER")
    if not founder_token:
        return False, f"{url} (missing ORCH_TOKEN_FOUNDER in stack env; refusing anonymous request)"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {founder_token}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        ok = body.get("status") in {"ok", "degraded"}
        return ok, url
    except Exception as exc:
        detail = _redact_known_secret(str(exc), founder_token)
        return False, f"{url} ({detail})"


def check_local_delegation() -> tuple[bool, str]:
    manifest = ROOT / ".tmp/local-stack/manifest.json"
    if not manifest.is_file():
        return False, "missing .tmp/local-stack/manifest.json (run bash scripts/local_stack_up.sh)"
    delegation_root = ROOT / ".tmp/local-delegation"
    delegation_dirs = sorted(
        delegation_root.glob("local-delegation-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for run_dir in delegation_dirs:
        summary_path = run_dir / "summary.json"
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("passed"):
                return True, str(summary_path)
            return False, f"{summary_path} passed=false"
    return False, "missing delegation summary (run python3 scripts/local_delegation_stress.py)"


def check_live_acceptance() -> tuple[bool, str]:
    manifest_path = ROOT / ".tmp/local-stack/manifest.json"
    if not manifest_path.is_file():
        return False, "missing .tmp/local-stack/manifest.json (run bash scripts/local_stack_up.sh)"
    acceptance_root = ROOT / ".tmp/local-stack/acceptance"
    acceptance_dirs = sorted(
        acceptance_root.glob("live-api-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for run_dir in acceptance_dirs:
        summary_path = run_dir / "acceptance_summary.json"
        if summary_path.is_file():
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            if summary.get("passed"):
                return True, str(summary_path)
            return False, f"{summary_path} passed=false"
    return False, "missing acceptance summary (run python3 scripts/orchestrator_live_acceptance.py)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Local stress test harness")
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args()
    run_id = args.run_id or f"local-stress-{default_run_id()}"

    out_dir = ROOT / ".tmp/local-stress" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []

    l1 = run_l1(root=ROOT, run_dir=out_dir)
    rows.append(
        row(
            "L1_flowctl_health",
            "flowctl status succeeds",
            "passed" if l1.get("passed") else "failed",
            bool(l1.get("passed")),
            str(out_dir / "l1.json"),
        )
    )
    write_json(out_dir / "l1.json", l1)

    l2 = run_l2(root=ROOT)
    rows.append(
        row(
            "L2_pytest_subset",
            "control-plane pytest subset green",
            "passed" if l2.get("passed") else "failed",
            bool(l2.get("passed")),
            str(out_dir / "l2.json"),
        )
    )
    write_json(out_dir / "l2.json", l2)

    ok_summary, summary_ev = check_ops_summary()
    rows.append(
        row(
            "ops_summary",
            "GET /ops/summary/ reachable",
            summary_ev,
            ok_summary,
            summary_ev,
        )
    )

    ok_live, live_ev = check_live_acceptance()
    rows.append(
        row(
            "live_api_acceptance",
            "orchestrator_live_acceptance.py green",
            live_ev,
            ok_live,
            live_ev,
        )
    )

    ok_delegation, delegation_ev = check_local_delegation()
    rows.append(
        row(
            "local_delegation_stress",
            "MCP delegation lifecycle green",
            delegation_ev,
            ok_delegation,
            delegation_ev,
        )
    )

    ok_bridge, bridge_ev = check_hq_bridge()
    rows.append(
        row(
            "hq_orch_bridge",
            "HQ bridge summary exists",
            bridge_ev,
            ok_bridge,
            bridge_ev,
        )
    )

    hub = HQ_ROOT / "programs/orchestrator-platform/discussions/local-stress-2026-07-28/README.md"
    rows.append(
        row(
            "stress_hub",
            "HQ stress hub present",
            str(hub),
            hub.is_file(),
            str(hub),
        )
    )

    audit = HQ_ROOT / "programs/orchestrator-platform/discussions/local-stress-2026-07-28/ops_integration_audit.md"
    rows.append(
        row(
            "framework_audit",
            "ops integration audit recorded",
            str(audit),
            audit.is_file(),
            str(audit),
        )
    )

    passed = all(r["passed"] for r in rows)
    summary = {
        "run_id": run_id,
        "captured_at": datetime.now(UTC).isoformat(),
        "passed": passed,
        "rows": rows,
        "hq_root": str(HQ_ROOT),
        "note": "Requires live stack (bash scripts/local_stack_up.sh). Full real-provider slices need real-providers profile + auth.",
    }
    write_json(out_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
