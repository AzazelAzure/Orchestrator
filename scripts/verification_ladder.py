#!/usr/bin/env python3
"""Verification ladder L1–L4 with expected-vs-actual evidence.

L1: flowctl / CLI smoke
L2: DRF API pytest subset
L3: r4d_verify static (skip with reason when no container runtime)
L4: provider runtime acceptance envelope (AM-05/06 path)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.provider_live_acceptance import redact_evidence  # noqa: E402

L2_PYTEST_TARGETS = (
    "tests/unit/test_r4_api_auth.py",
    "tests/unit/test_r4_delivery.py",
    "tests/unit/test_provider_live_acceptance.py",
)


def default_run_id(prefix: str = "ladder") -> str:
    return f"{prefix}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def level_record(
    *,
    level: str,
    expected: dict[str, Any],
    actual: dict[str, Any],
    passed: bool,
) -> dict[str, Any]:
    return {
        "level": level,
        "expected": expected,
        "actual": actual,
        "passed": passed,
    }


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(
        json.dumps(redact_evidence(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def detect_container_runtime() -> str | None:
    override = os.environ.get("ORCH_CONTAINER_RUNTIME", "").strip().lower()
    if override in {"podman", "docker"}:
        return override
    podman = shutil.which("podman")
    docker = shutil.which("docker")
    if podman and not docker:
        return "podman"
    if docker:
        try:
            probe = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if probe.returncode == 0:
                return "docker"
        except (OSError, subprocess.TimeoutExpired):
            pass
        if podman:
            return "podman"
        return "docker"
    if podman:
        return "podman"
    return None


def run_command(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout_sec: int = 900,
) -> dict[str, Any]:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
        return {
            "argv": argv,
            "exit_code": completed.returncode,
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "exit_code": 124,
            "stdout_tail": (exc.stdout or b"").decode()[-4000:],
            "stderr_tail": (exc.stderr or b"").decode()[-4000:],
            "timed_out": True,
        }


def run_l1(*, root: Path, run_dir: Path) -> dict[str, Any]:
    expected = {
        "flowctl_help_exit_zero": True,
        "flowctl_init_exit_zero": True,
        "flowctl_status_exit_zero": True,
    }
    db_path = run_dir / "l1-state.db"
    help_run = run_command(
        [sys.executable, "-m", "flow_engine.cli.app", "--help"],
        cwd=root,
        env={"PYTHONPATH": str(root / "src")},
    )
    init_run = run_command(
        [
            sys.executable,
            "-m",
            "flow_engine.cli.app",
            "--db",
            str(db_path),
            "init",
            "--project",
            "ladder",
            "--queue",
            "default",
        ],
        cwd=root,
        env={"PYTHONPATH": str(root / "src"), "ORCH_TESTING": "1"},
    )
    status_run = run_command(
        [
            sys.executable,
            "-m",
            "flow_engine.cli.app",
            "--db",
            str(db_path),
            "status",
        ],
        cwd=root,
        env={"PYTHONPATH": str(root / "src"), "ORCH_TESTING": "1"},
    )
    actual = {
        "flowctl_help_exit_zero": help_run["exit_code"] == 0,
        "flowctl_init_exit_zero": init_run["exit_code"] == 0,
        "flowctl_status_exit_zero": status_run["exit_code"] == 0,
        "runs": {
            "help": help_run,
            "init": init_run,
            "status": status_run,
        },
    }
    passed = all(actual[key] for key in expected)
    return level_record(level="L1", expected=expected, actual=actual, passed=passed)


def run_l25(*, root: Path) -> dict[str, Any]:
    """Optional installation checkpoint: external bridge summary exists."""
    expect = os.environ.get("ORCH_BRIDGE_EXPECT", "").strip() == "1"
    expected = {"bridge_summary_exists": True}
    if not expect:
        actual = {"skipped": True, "reason": "ORCH_BRIDGE_EXPECT not set"}
        return level_record(level="L2.5", expected=expected, actual=actual, passed=True)

    summary_path = os.environ.get("ORCH_BRIDGE_SUMMARY_PATH", "").strip()
    path: Path | None
    if summary_path:
        path = Path(summary_path)
    else:
        candidates: list[Path] = []
        tmp = root / ".tmp"
        if tmp.is_dir():
            for pattern in ("orch-bridge/*/summary.json", "hq-orch-bridge/*/summary.json"):
                candidates.extend(tmp.glob(pattern))
        path = (
            max(candidates, key=lambda p: p.stat().st_mtime)
            if candidates
            else None
        )

    exists = path is not None and path.is_file()
    parsed: dict[str, Any] | None = None
    if exists and path is not None:
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            exists = False

    actual = {
        "skipped": False,
        "bridge_summary_exists": exists,
        "summary_path": str(path) if path else None,
        "valid_json_object": isinstance(parsed, dict),
    }
    passed = actual["bridge_summary_exists"] and actual["valid_json_object"]
    return level_record(level="L2.5", expected=expected, actual=actual, passed=passed)


def run_l2(*, root: Path) -> dict[str, Any]:
    expected = {"pytest_exit_zero": True, "targets": list(L2_PYTEST_TARGETS)}
    python_bin = str(root / ".venv/bin/python") if (root / ".venv/bin/python").is_file() else sys.executable
    pytest_argv = [
        python_bin,
        "-m",
        "pytest",
        "-q",
        "--cache-clear",
        *L2_PYTEST_TARGETS,
    ]
    completed = run_command(
        pytest_argv,
        cwd=root,
        env={
            "PYTHONPATH": str(root / "src"),
            "ORCH_TESTING": "1",
            "DJANGO_SETTINGS_MODULE": "flow_engine.control_plane.settings",
        },
        timeout_sec=600,
    )
    actual = {
        "pytest_exit_zero": completed["exit_code"] == 0,
        "exit_code": completed["exit_code"],
        "stdout_tail": completed["stdout_tail"],
        "stderr_tail": completed["stderr_tail"],
    }
    return level_record(
        level="L2",
        expected=expected,
        actual=actual,
        passed=actual["pytest_exit_zero"],
    )


def run_l3(*, root: Path) -> dict[str, Any]:
    runtime = detect_container_runtime()
    expected = {
        "container_runtime_available": True,
        "r4d_verify_exit_zero": True,
    }
    if runtime is None:
        actual = {
            "skipped": True,
            "reason": "no podman or docker available",
            "container_runtime_available": False,
        }
        return level_record(
            level="L3",
            expected=expected,
            actual=actual,
            passed=False,
        )
    verify = run_command(
        ["bash", str(root / "scripts" / "r4d_verify.sh")],
        cwd=root,
        env={
            "ORCH_TESTING": "1",
            "ORCH_PROVIDER_MODE": "mock",
            "ORCH_CONTAINER_RUNTIME": runtime,
        },
        timeout_sec=1200,
    )
    actual = {
        "skipped": False,
        "container_runtime": runtime,
        "container_runtime_available": True,
        "r4d_verify_exit_zero": verify["exit_code"] == 0,
        "exit_code": verify["exit_code"],
        "stdout_tail": verify["stdout_tail"],
        "stderr_tail": verify["stderr_tail"],
    }
    return level_record(
        level="L3",
        expected=expected,
        actual=actual,
        passed=actual["r4d_verify_exit_zero"],
    )


def run_l4(*, root: Path, run_dir: Path, skip_live: bool) -> dict[str, Any]:
    expected = {
        "provider_runtime_acceptance_exit_zero": True,
        "am05_passed": True,
        "am06_passed": True,
    }
    if skip_live:
        actual = {"skipped": True, "reason": "--skip-l4 set"}
        return level_record(level="L4", expected=expected, actual=actual, passed=False)

    l4_run_id = f"runtime-{uuid.uuid4().hex[:8]}"
    completed = run_command(
        [
            sys.executable,
            str(root / "scripts" / "provider_runtime_acceptance.py"),
            "--root",
            str(root),
            "--run-id",
            l4_run_id,
        ],
        cwd=root,
        env={
            "PYTHONPATH": str(root / "src"),
            "ORCH_TESTING": "1",
        },
        timeout_sec=900,
    )
    summary_path = root / ".tmp" / "provider-runtime-acceptance" / l4_run_id / "run_summary.json"
    summary: dict[str, Any] = {}
    if summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    am05 = (summary.get("acceptance_matrix") or {}).get("AM-05", {})
    am06 = (summary.get("acceptance_matrix") or {}).get("AM-06", {})
    actual = {
        "skipped": False,
        "provider_runtime_acceptance_exit_zero": completed["exit_code"] == 0,
        "exit_code": completed["exit_code"],
        "evidence_run_id": l4_run_id,
        "evidence_path": str(summary_path.parent),
        "am05_passed": bool(am05.get("passed")),
        "am06_passed": bool(am06.get("passed")),
        "stdout_tail": completed["stdout_tail"],
        "stderr_tail": completed["stderr_tail"],
        "run_summary": summary,
    }
    passed = (
        actual["provider_runtime_acceptance_exit_zero"]
        and actual["am05_passed"]
        and actual["am06_passed"]
    )
    write_json(run_dir / "l4_run_summary.json", actual)
    return level_record(level="L4", expected=expected, actual=actual, passed=passed)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--levels",
        default="L1,L2,L3,L4",
        help="Comma-separated subset (default: L1,L2,L3,L4)",
    )
    parser.add_argument(
        "--skip-l4",
        action="store_true",
        help="Skip live provider runtime acceptance (L4)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    run_id = args.run_id or default_run_id()
    run_dir = root / ".tmp" / "verification-ladder" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    selected = {item.strip().upper() for item in args.levels.split(",") if item.strip()}
    levels: dict[str, dict[str, Any]] = {}

    if "L1" in selected:
        levels["L1"] = run_l1(root=root, run_dir=run_dir)
    if "L2" in selected:
        levels["L2"] = run_l2(root=root)
    if "L2.5" in selected:
        levels["L2.5"] = run_l25(root=root)
    if "L3" in selected:
        levels["L3"] = run_l3(root=root)
    if "L4" in selected:
        levels["L4"] = run_l4(root=root, run_dir=run_dir, skip_live=args.skip_l4)

    summary = {
        "run_id": run_id,
        "root": str(root),
        "levels": levels,
        "passed": all(level["passed"] for level in levels.values()),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    write_json(run_dir / "summary.json", summary)
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
