#!/usr/bin/env python3
"""Bounded live provider acceptance — one minimal real CLI call per provider.

Uses HostRunner acceptance_mode (isolated empty read-only workspace, no tools).
Writes redacted evidence under .tmp/provider-acceptance/<run_id>/.
No automatic retry. Codex excluded.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flow_engine.providers.cursor_env import bootstrap_cursor_host_runner_env  # noqa: E402
from flow_engine.providers.host_runner import (  # noqa: E402
    HostRunner,
    ProviderBinding,
    canonical_invocation_packet,
    digest_json,
    redact,
)

ACCEPTANCE_TOKEN = "ACCEPTANCE_OK"
DEFAULT_PROVIDERS = ("cursor", "claude")
PROVIDER_EXECUTABLES = {
    "cursor": "cursor-agent",
    "claude": "claude",
}


@dataclass(frozen=True)
class ProviderOutcome:
    provider: str
    success: bool
    checks: dict[str, bool]
    evidence_dir: Path
    error: str | None = None


def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines from an installation-local env file."""
    if not path.is_file():
        raise FileNotFoundError(f"missing env file: {path}")
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key] = value
    return out


def acceptance_task_packet(provider: str) -> dict[str, Any]:
    return canonical_invocation_packet({
        "acceptance_probe": True,
        "provider": provider,
        "instruction": (
            "Return only a structured terminal result event whose text or result "
            f"field contains the exact token {ACCEPTANCE_TOKEN}. Do not use tools."
        ),
    })


def build_binding(
    provider: str,
    *,
    root: Path,
    pins: dict[str, str],
    run_dir: Path,
    acceptance_mode: bool = True,
) -> ProviderBinding:
    executable_name = PROVIDER_EXECUTABLES[provider]
    executable = Path(
        os.environ.get(
            f"ORCH_PROVIDER_EXECUTABLE_{provider.upper()}",
            shutil.which(executable_name) or "",
        )
    )
    if not executable.is_absolute() or not os.access(executable, os.X_OK):
        raise RuntimeError(f"{executable_name} executable not found or not executable")
    model = pins["ORCH_PROVIDER_MODEL"]
    allowed = tuple(
        item.strip()
        for item in pins["ORCH_PROVIDER_ALLOWED_MODELS"].split(",")
        if item.strip()
    )
    if provider == "cursor":
        bootstrap_cursor_host_runner_env(root)
    return ProviderBinding(
        provider=provider,
        executable=executable,
        model=model,
        workspace_root=root.resolve(),
        socket_path=run_dir / "sockets" / f"{provider}.sock",
        auth_token=secrets.token_hex(32),
        allowed_models=allowed,
        acceptance_mode=acceptance_mode,
    )


def build_invoke_packet(
    runner: HostRunner,
    provider: str,
    *,
    invocation_id: str,
) -> dict[str, Any]:
    handshake = runner.handshake()
    snapshot_digest = handshake["snapshot_digest"]
    task_packet = acceptance_task_packet(provider)
    packet_digest = digest_json(task_packet)
    attempt_id = f"accept-{provider}-{invocation_id[:8]}"
    binding_fields = {
        "provider": provider,
        "attempt_id": attempt_id,
        "invocation_id": invocation_id,
        "credit_reservation_id": f"credit-{invocation_id[:8]}",
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot_digest,
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    }
    return {
        **binding_fields,
        "binding_digest": digest_json(binding_fields),
        "task_packet": task_packet,
        "cwd": ".",
    }


def acceptance_checks(result: dict[str, Any]) -> dict[str, bool]:
    output = str(result.get("redacted_output") or "")
    checks = {
        "outcome_complete": result.get("outcome") == "complete",
        "exit_code_zero": result.get("exit_code") == 0,
        "not_reconciliation_required": not bool(result.get("reconciliation_required")),
        "acceptance_token_present": ACCEPTANCE_TOKEN in output,
        "terminal_identity_present": bool(result.get("provider_call_id")),
    }
    return checks


def acceptance_success(result: dict[str, Any]) -> bool:
    checks = acceptance_checks(result)
    return all(checks.values())


def _is_secret_evidence_key(key: str) -> bool:
    lk = key.lower()
    if lk in {
        "auth_token",
        "token",
        "secret",
        "password",
        "authorization",
        "signature",
        "nonce",
        "credential",
        "api_key",
        "apikey",
    }:
        return True
    return lk.endswith(
        ("_token", "_secret", "_password", "_api_key", "_apikey", "_credential")
    )


def redact_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if _is_secret_evidence_key(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact_evidence(item)
        return redacted
    if isinstance(value, list):
        return [redact_evidence(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_evidence(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def run_provider_acceptance(
    provider: str,
    *,
    root: Path,
    run_dir: Path,
) -> ProviderOutcome:
    evidence_dir = run_dir / provider
    evidence_dir.mkdir(parents=True, exist_ok=True)
    pins_path = root / ".local" / "provider" / f"{provider}.pins.env"
    try:
        pins = load_env_file(pins_path)
        binding = build_binding(provider, root=root, pins=pins, run_dir=run_dir)
        runner = HostRunner(binding)
        handshake = runner.handshake()
        write_json(evidence_dir / "handshake.json", handshake)
        invocation_id = f"live-{provider}-{uuid.uuid4()}"
        packet = build_invoke_packet(runner, provider, invocation_id=invocation_id)
        write_json(evidence_dir / "invoke_packet.json", packet)
        result = runner.invoke(packet)
        write_json(evidence_dir / "invoke_result.json", result)
        checks = acceptance_checks(result)
        summary = {
            "provider": provider,
            "invocation_id": invocation_id,
            "model": binding.model,
            "acceptance_mode": binding.acceptance_mode,
            "checks": checks,
            "success": acceptance_success(result),
            "captured_at": datetime.now(UTC).isoformat(),
        }
        write_json(evidence_dir / "summary.json", summary)
        return ProviderOutcome(
            provider=provider,
            success=bool(summary["success"]),
            checks=checks,
            evidence_dir=evidence_dir,
        )
    except Exception as exc:  # noqa: BLE001 — capture bounded evidence on failure
        error_summary = {
            "provider": provider,
            "success": False,
            "error_type": type(exc).__name__,
            "error": redact(str(exc)),
            "captured_at": datetime.now(UTC).isoformat(),
        }
        write_json(evidence_dir / "summary.json", error_summary)
        return ProviderOutcome(
            provider=provider,
            success=False,
            checks={},
            evidence_dir=evidence_dir,
            error=str(exc),
        )


def default_run_id() -> str:
    return datetime.now(UTC).strftime("accept-%Y%m%dT%H%M%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Orchestrator installation root",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Evidence run id (default: UTC timestamp accept-YYYYMMDDTHHMMSSZ)",
    )
    parser.add_argument(
        "--provider",
        choices=[*DEFAULT_PROVIDERS, "all"],
        default="all",
        help="Provider to exercise (default: all except codex)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    run_id = args.run_id or default_run_id()
    run_dir = root / ".tmp" / "provider-acceptance" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    providers = list(DEFAULT_PROVIDERS) if args.provider == "all" else [args.provider]
    outcomes: list[ProviderOutcome] = []
    for provider in providers:
        outcomes.append(run_provider_acceptance(provider, root=root, run_dir=run_dir))

    run_summary = {
        "run_id": run_id,
        "root": str(root),
        "providers": {
            outcome.provider: {
                "success": outcome.success,
                "checks": outcome.checks,
                "evidence_dir": str(outcome.evidence_dir),
                "error": redact(outcome.error) if outcome.error else None,
            }
            for outcome in outcomes
        },
        "captured_at": datetime.now(UTC).isoformat(),
    }
    write_json(run_dir / "run_summary.json", run_summary)
    print(json.dumps(run_summary, indent=2))
    return 0 if all(o.success for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
