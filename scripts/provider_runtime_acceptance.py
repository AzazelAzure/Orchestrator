#!/usr/bin/env python3
"""Provider runtime acceptance — AM-05/06 through coordinator/worker_delivery path.

Real Cursor + Claude calls via HostRunner socket bound into StateCoordinator
commands (preflight → snapshot pin → prepare → invoke → settle) with credit
reservation and settlement. Not host-runner-only direct invoke.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from flow_engine.application import ensure_queue, init_project, submit_work  # noqa: E402
from flow_engine.application.credit_service import credit_usage  # noqa: E402
from flow_engine.application.runtime_service import (  # noqa: E402
    claim_attempt,
    create_run,
    dispatch_provider_call,
)
from flow_engine.control_plane.bootstrap import bootstrap_test_principals  # noqa: E402
from flow_engine.coordinator import (  # noqa: E402
    CommandContext,
    RuntimeCommand,
    StateCoordinator,
    SystemTestGrant,
)
from flow_engine.domain.states import PrincipalRole, Surface  # noqa: E402
from flow_engine.persistence import Kernel  # noqa: E402
from flow_engine.persistence.transactions import transaction  # noqa: E402
from flow_engine.providers.host_runner import (  # noqa: E402
    HostRunner,
    HostRunnerServer,
    UnixSocketClient,
    authorize_provider_packet,
    canonical_invocation_packet,
    redact,
)
from scripts.provider_live_acceptance import (  # noqa: E402
    ACCEPTANCE_TOKEN,
    acceptance_checks,
    acceptance_success,
    acceptance_task_packet,
    build_binding,
    load_env_file,
    redact_evidence,
)

DEFAULT_PROVIDERS = ("cursor", "claude")
ACCEPTANCE_MATRIX = {"cursor": "AM-05", "claude": "AM-06"}


def governed_acceptance_packet(provider: str) -> dict[str, Any]:
    base = acceptance_task_packet(provider)
    return canonical_invocation_packet({
        **base,
        "sensitivity": "internal",
        "allowed_providers": [provider],
    })


def short_socket_path(root: Path, provider: str) -> Path:
    directory = root / ".tmp" / "sockets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory / f"{provider[:4]}-{uuid.uuid4().hex[:8]}.sock"


@dataclass(frozen=True)
class ProviderRuntimeOutcome:
    provider: str
    matrix_id: str
    success: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    evidence_dir: Path
    error: str | None = None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(redact_evidence(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def default_run_id() -> str:
    return datetime.now(UTC).strftime("runtime-%Y%m%dT%H%M%SZ")


def expected_am_bar(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "matrix_id": ACCEPTANCE_MATRIX[provider],
        "coordinator_path": True,
        "credit_reserved_before_dispatch": True,
        "credit_settled_after_complete": True,
        "run_status_complete": True,
        "acceptance_checks": {
            "outcome_complete": True,
            "exit_code_zero": True,
            "not_reconciliation_required": True,
            "acceptance_token_present": True,
            "terminal_identity_present": True,
        },
    }


def credit_snapshot(conn, *, run_id: str, invocation_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT kind, units, provider, created_at
        FROM credit_entries
        WHERE invocation_id = ?
        ORDER BY created_at
        """,
        (invocation_id,),
    ).fetchall()
    usage = credit_usage(conn, run_id)
    provider = rows[0]["provider"] if rows else None
    provider_usage = usage["by_provider"].get(
        provider or "",
        {"consumed": 0, "open_reservations": 0},
    )
    return {
        "entries": [dict(row) for row in rows],
        "reservation_count": sum(1 for row in rows if row["kind"] == "reservation"),
        "settlement_count": sum(1 for row in rows if row["kind"] == "settlement"),
        "open_reservations": provider_usage.get("open_reservations", 0),
        "consumed": provider_usage.get("consumed", 0),
    }


def _worker_context(provider: str) -> CommandContext:
    return CommandContext(
        principal_id=f"worker.provider.{provider}",
        role=PrincipalRole.WORKER,
        surface=Surface.WORKER,
        grant=None,
    )


def _accept(coord: StateCoordinator, command: RuntimeCommand) -> dict[str, Any]:
    with transaction(coord.connection):
        return coord.accept(command)


def execute_coordinator_socket_delivery(
    *,
    coord: StateCoordinator,
    provider: str,
    job_id: str,
    attempt_id: str,
    socket_path: Path,
    host_token: str,
) -> dict[str, Any]:
    """Mirror production Celery socket worker using in-process coordinator."""
    ctx = _worker_context(provider)
    task_id = f"acceptance-{uuid.uuid4().hex[:8]}"

    claim = _accept(
        coord,
        RuntimeCommand(
            command_type="delivery.claim",
            target_id=job_id,
            payload={
                "job_id": job_id,
                "attempt_id": attempt_id,
                "celery_task_id": task_id,
            },
            idempotency_key=f"claim|{job_id}|{task_id}",
            context=ctx,
        ),
    )
    if claim.get("status") == "rejected":
        return {"stage": "delivery.claim", "envelope": claim}

    preflight_envelope = _accept(
        coord,
        RuntimeCommand(
            command_type="runtime.worker_preflight",
            target_id=attempt_id,
            payload={"attempt_id": attempt_id, "delivery_job_id": job_id},
            idempotency_key=f"preflight|{job_id}|{attempt_id}",
            context=ctx,
        ),
    )
    prepared = preflight_envelope["result"]["preflight"]
    if prepared["provider"] != provider:
        raise PermissionError("provider queue/binding mismatch")

    client = UnixSocketClient(provider, socket_path, host_token)
    authorize_provider_packet(prepared["payload"], provider)
    handshake = client.request("handshake")

    snapshot_envelope = _accept(
        coord,
        RuntimeCommand(
            command_type="runtime.worker_snapshot",
            target_id=prepared["invocation_id"],
            payload={
                "invocation_id": prepared["invocation_id"],
                "provider": provider,
                "snapshot": handshake["snapshot"],
                "snapshot_digest": handshake["snapshot_digest"],
            },
            idempotency_key=(
                f"snapshot|{prepared['invocation_id']}|{handshake['snapshot_digest']}"
            ),
            context=ctx,
        ),
    )
    if snapshot_envelope.get("status") != "applied":
        return {"stage": "runtime.worker_snapshot", "envelope": snapshot_envelope}

    pinned = snapshot_envelope["result"]["snapshot"]
    packet_request = {
        "invocation_id": prepared["invocation_id"],
        "attempt_id": prepared["attempt_id"],
        "provider": provider,
        "credit_reservation_id": pinned["binding"]["credit_reservation_id"],
        "packet_digest": pinned["binding"]["packet_digest"],
        "snapshot_digest": handshake["snapshot_digest"],
        "binding_digest": pinned["binding_digest"],
        "task_packet": prepared["payload"],
        "cwd": ".",
    }
    client.request("validate_packet", **packet_request)

    intent_envelope = _accept(
        coord,
        RuntimeCommand(
            command_type="runtime.worker_prepare",
            target_id=attempt_id,
            payload={
                "attempt_id": attempt_id,
                "delivery_job_id": job_id,
                "lease_token": f"lease|{job_id}|{attempt_id}|{ctx.principal_id}",
            },
            idempotency_key=f"prepare|{job_id}|{attempt_id}",
            context=ctx,
        ),
    )
    prepared_delivery = intent_envelope["result"]["prepared"]
    result = client.request("invoke", **packet_request)

    provider_result = None
    if result.get("outcome") != "outcome_unknown":
        provider_result = {
            "outcome": result["outcome"],
            "evidence": {
                "provider_call_id": result.get("provider_call_id"),
                "truncated": bool(result.get("truncated")),
            },
            "anomalies": result.get("anomalies") or [],
            "delivery_id": str(result.get("provider_call_id") or prepared_delivery["invocation_id"]),
            "provider_call_id": result.get("provider_call_id"),
            "redacted_output": result.get("redacted_output", ""),
            "truncated": bool(result.get("truncated")),
            "snapshot_digest": handshake["snapshot_digest"],
            "binding_digest": result.get("binding_digest"),
        }

    settle_envelope = _accept(
        coord,
        RuntimeCommand(
            command_type="runtime.worker_settle",
            target_id=attempt_id,
            payload={"prepared": prepared_delivery, "provider_result": provider_result},
            idempotency_key=f"settle|{job_id}|{attempt_id}",
            context=ctx,
        ),
    )
    return {
        "claim": claim,
        "preflight": preflight_envelope,
        "snapshot": snapshot_envelope,
        "prepare": intent_envelope,
        "invoke_result": result,
        "settle": settle_envelope,
        "provider_result": provider_result,
    }


def pins_available(root: Path, provider: str) -> bool:
    return (root / ".local" / "provider" / f"{provider}.pins.env").is_file()


def run_provider_runtime_acceptance(
    provider: str,
    *,
    root: Path,
    run_dir: Path,
) -> ProviderRuntimeOutcome:
    matrix_id = ACCEPTANCE_MATRIX[provider]
    evidence_dir = run_dir / provider
    evidence_dir.mkdir(parents=True, exist_ok=True)
    expected = expected_am_bar(provider)

    pins_path = root / ".local" / "provider" / f"{provider}.pins.env"
    if not pins_path.is_file():
        actual = {"error": f"missing pins: {pins_path}", "passed": False}
        write_json(evidence_dir / "summary.json", {"provider": provider, **actual})
        return ProviderRuntimeOutcome(
            provider=provider,
            matrix_id=matrix_id,
            success=False,
            expected=expected,
            actual=actual,
            evidence_dir=evidence_dir,
            error=f"missing pins: {pins_path}",
        )

    provider_workspace = evidence_dir / "workspace"
    provider_workspace.mkdir(parents=True, exist_ok=True)
    server_thread: threading.Thread | None = None

    try:
        pins = load_env_file(pins_path)
        socket_path = short_socket_path(root, provider)
        binding = build_binding(
            provider,
            root=provider_workspace,
            pins=pins,
            run_dir=evidence_dir,
            acceptance_mode=True,
        )
        binding = replace(binding, socket_path=socket_path)
        runner = HostRunner(binding)
        server = HostRunnerServer(runner)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        time.sleep(0.3)

        db_path = evidence_dir / "runtime.db"
        kernel = Kernel.init(db_path)
        try:
            with transaction(kernel.connection):
                init_project(kernel.connection, name="acceptance", actor="acceptance")
                ensure_queue(kernel.connection, name="default")
                bootstrap_test_principals(kernel.connection)
                item = submit_work(
                    kernel.connection,
                    queue_name="default",
                    payload={"acceptance_probe": True, "provider": provider},
                    actor="acceptance",
                )
                grant = SystemTestGrant(
                    grant_id=f"acceptance-{provider}",
                    principal_id=f"worker.provider.{provider}",
                    role=PrincipalRole.WORKER,
                    surfaces=(Surface.WORKER, Surface.TEST),
                    providers=(provider,),
                    budget_scope_id=f"acceptance-runtime-{provider}",
                )
                task_packet = governed_acceptance_packet(provider)
                created = create_run(
                    kernel.connection,
                    work_item_id=item["id"],
                    provider=provider,
                    grant=grant,
                    actor=f"worker.provider.{provider}",
                    packet=task_packet,
                )
                run_id = created["run"]["id"]
                claim_attempt(
                    kernel.connection,
                    run_id=run_id,
                    actor=f"worker.provider.{provider}",
                )
                dispatched = dispatch_provider_call(
                    kernel.connection,
                    attempt_id=created["attempt"]["id"],
                    actor=f"worker.provider.{provider}",
                    payload=task_packet,
                    delivery_mode="async",
                )

            invocation_id = dispatched["invocation"]["id"]
            attempt_id = dispatched["attempt"]["id"]
            job_id = dispatched["delivery"]["delivery_job_id"]
            credit_before = credit_snapshot(
                kernel.connection, run_id=run_id, invocation_id=invocation_id
            )
            write_json(evidence_dir / "credit_before_dispatch.json", credit_before)

            coord = StateCoordinator(kernel.connection)
            delivery_trace = execute_coordinator_socket_delivery(
                coord=coord,
                provider=provider,
                job_id=job_id,
                attempt_id=attempt_id,
                socket_path=binding.socket_path,
                host_token=binding.auth_token,
            )
            write_json(evidence_dir / "coordinator_delivery.json", delivery_trace)

            credit_after = credit_snapshot(
                kernel.connection, run_id=run_id, invocation_id=invocation_id
            )
            write_json(evidence_dir / "credit_after_settle.json", credit_after)

            run_row = kernel.connection.execute(
                "SELECT status FROM runtime_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            invoke_result = delivery_trace.get("invoke_result") or {}
            checks = acceptance_checks(invoke_result)
            actual = {
                "provider": provider,
                "matrix_id": matrix_id,
                "coordinator_path": delivery_trace.get("settle", {}).get("status") == "applied",
                "credit_reserved_before_dispatch": credit_before["reservation_count"] >= 1,
                "credit_settled_after_complete": credit_after["settlement_count"] >= 1
                and credit_after["open_reservations"] == 0,
                "run_status_complete": run_row["status"] == "complete",
                "acceptance_checks": checks,
                "credit_before": credit_before,
                "credit_after": credit_after,
                "model": binding.model,
            }
            success = (
                actual["coordinator_path"]
                and actual["credit_reserved_before_dispatch"]
                and actual["credit_settled_after_complete"]
                and actual["run_status_complete"]
                and acceptance_success(invoke_result)
            )
            summary = {
                "provider": provider,
                "matrix_id": matrix_id,
                "expected": expected,
                "actual": actual,
                "passed": success,
                "invocation_id": invocation_id,
                "attempt_id": attempt_id,
                "captured_at": datetime.now(UTC).isoformat(),
            }
            write_json(evidence_dir / "summary.json", summary)
            return ProviderRuntimeOutcome(
                provider=provider,
                matrix_id=matrix_id,
                success=success,
                expected=expected,
                actual=actual,
                evidence_dir=evidence_dir,
            )
        finally:
            kernel.close()
    except Exception as exc:  # noqa: BLE001
        error_summary = {
            "provider": provider,
            "matrix_id": matrix_id,
            "expected": expected,
            "actual": {
                "passed": False,
                "error_type": type(exc).__name__,
                "error": redact(str(exc)),
            },
            "passed": False,
            "captured_at": datetime.now(UTC).isoformat(),
        }
        write_json(evidence_dir / "summary.json", error_summary)
        return ProviderRuntimeOutcome(
            provider=provider,
            matrix_id=matrix_id,
            success=False,
            expected=expected,
            actual=error_summary["actual"],
            evidence_dir=evidence_dir,
            error=str(exc),
        )
    finally:
        if server_thread is not None:
            server_thread.join(timeout=1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--run-id", default=None)
    parser.add_argument(
        "--provider",
        choices=[*DEFAULT_PROVIDERS, "all"],
        default="all",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    run_id = args.run_id or default_run_id()
    run_dir = root / ".tmp" / "provider-runtime-acceptance" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    providers = list(DEFAULT_PROVIDERS) if args.provider == "all" else [args.provider]
    outcomes: list[ProviderRuntimeOutcome] = []
    for provider in providers:
        if not pins_available(root, provider):
            outcomes.append(
                ProviderRuntimeOutcome(
                    provider=provider,
                    matrix_id=ACCEPTANCE_MATRIX[provider],
                    success=False,
                    expected=expected_am_bar(provider),
                    actual={"passed": False, "error": "pins unavailable"},
                    evidence_dir=run_dir / provider,
                    error="pins unavailable",
                )
            )
            continue
        outcomes.append(
            run_provider_runtime_acceptance(provider, root=root, run_dir=run_dir)
        )

    acceptance_matrix = {
        outcome.matrix_id: {
            "provider": outcome.provider,
            "expected": outcome.expected,
            "actual": outcome.actual,
            "passed": outcome.success,
            "evidence_dir": str(outcome.evidence_dir),
            "error": redact(outcome.error) if outcome.error else None,
        }
        for outcome in outcomes
    }
    run_summary = {
        "run_id": run_id,
        "root": str(root),
        "providers": {
            outcome.provider: {
                "matrix_id": outcome.matrix_id,
                "success": outcome.success,
                "evidence_dir": str(outcome.evidence_dir),
            }
            for outcome in outcomes
        },
        "acceptance_matrix": acceptance_matrix,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    write_json(run_dir / "run_summary.json", run_summary)
    print(json.dumps(run_summary, indent=2))
    return 0 if all(o.success for o in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
