from __future__ import annotations

import json
from pathlib import Path

from scripts.r5_dogfood_evidence import (
    DELEGATION_FALLBACK_NOTE,
    OPEN_LCP_GATE,
    R4D_REFERENCE_RUN_ID,
    build_rows,
    load_r4d_reference,
    load_governed_run_reference,
    probe_delegation,
    probe_hq_supplementary,
    resolve_hq_root,
    row,
)


def test_row_shape() -> None:
    record = row(
        step="Compose stack up",
        expected="all services healthy",
        actual="L1+L2 green",
        passed=True,
        evidence_artifact=".tmp/r5-dogfood/run/l3_health",
    )
    assert record["step"] == "Compose stack up"
    assert record["passed"] is True


def test_build_rows_includes_seven_evidence_plan_steps() -> None:
    rows = build_rows(
        health={"passed": True, "levels": {}},
        delegation={
            "actual": DELEGATION_FALLBACK_NOTE,
            "passed": True,
            "evidence_artifact": ".local/hq-confer/runs",
            "hq_root": "/tmp/HQ",
            "available": False,
            "runs_exist": False,
            "confer_exists": True,
        },
        r4d={
            "reference_run_id": R4D_REFERENCE_RUN_ID,
            "passed": True,
            "reconcile_first_no_duplicate_paid_call": True,
            "redelivery_path": f".tmp/r4d/{R4D_REFERENCE_RUN_ID}/evidence/steps/08_redelivery.json",
        },
        governed={
            "reference_run_id": R4D_REFERENCE_RUN_ID,
            "passed": True,
            "mock_delivery_path": f".tmp/r4d/{R4D_REFERENCE_RUN_ID}/evidence/steps/02_api_worker_mock.json",
            "audit_trail": {"final_status": "complete"},
        },
        gate={
            "passed": True,
            "evidence_artifact": ".tmp/r5-dogfood/test-run/slice_c_gate/gate_block_audit.json",
        },
        supplementary={
            "cross_provider_review_passed": True,
            "conference_path": "discussions/r5-dogfood-proof-2026-07-28/conference.md",
            "handoff_passed": True,
            "handoff_path": "discussions/r5-dogfood-proof-2026-07-28/handoff_contract.md",
        },
        run_dir=Path(".tmp/r5-dogfood/test-run"),
    )
    assert len(rows) == 7
    steps = [item["step"] for item in rows]
    assert "Injected failure handled" in steps
    assert rows[1]["actual"] == DELEGATION_FALLBACK_NOTE
    assert all(item["passed"] for item in rows)


def test_load_governed_run_reference_from_existing_run(tmp_path: Path) -> None:
    run_dir = tmp_path / ".tmp" / "r4d" / R4D_REFERENCE_RUN_ID / "evidence" / "steps"
    run_dir.mkdir(parents=True)
    (run_dir / "02_api_worker_mock.json").write_text(
        json.dumps(
            {
                "enqueue": {"enqueued": True, "task_id": "task-1"},
                "final_status": "complete",
                "run_id": "run-1",
            }
        ),
        encoding="utf-8",
    )
    result = load_governed_run_reference(root=tmp_path, reference_run_id=R4D_REFERENCE_RUN_ID)
    assert result["passed"] is True
    assert result["terminal_complete"] is True


def test_probe_hq_supplementary_detects_conference_and_handoff(tmp_path: Path) -> None:
    discussion = (
        tmp_path
        / "programs/orchestrator-platform/agentic-control-plane/discussions/r5-dogfood-proof-2026-07-28"
    )
    discussion.mkdir(parents=True)
    (discussion / "conference.md").write_text("# conference\n", encoding="utf-8")
    (discussion / "handoff_contract.md").write_text("# handoff\n", encoding="utf-8")
    result = probe_hq_supplementary(hq_root=tmp_path)
    assert result["cross_provider_review_passed"] is True
    assert result["handoff_passed"] is True


def test_load_r4d_reference_from_existing_run(tmp_path: Path) -> None:
    run_dir = tmp_path / ".tmp" / "r4d" / R4D_REFERENCE_RUN_ID / "evidence"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"ok": True}),
        encoding="utf-8",
    )
    (run_dir / "steps").mkdir()
    (run_dir / "steps" / "08_redelivery.json").write_text(
        json.dumps(
            {
                "duplicate_terminal_effect": False,
                "exactly_one_terminal_effect": True,
                "invocation_count": 1,
                "run_status": "complete",
            }
        ),
        encoding="utf-8",
    )
    result = load_r4d_reference(root=tmp_path, reference_run_id=R4D_REFERENCE_RUN_ID)
    assert result["passed"] is True
    assert result["reference_run_id"] == R4D_REFERENCE_RUN_ID


def test_probe_delegation_documents_fallback_when_missing(tmp_path: Path) -> None:
    hq_root = tmp_path / "HQ"
    hq_root.mkdir()
    result = probe_delegation(hq_root=hq_root)
    assert result["available"] is False
    assert result["actual"] == DELEGATION_FALLBACK_NOTE
    assert result["passed"] is True


def test_resolve_hq_root_prefers_env(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom-hq"
    custom.mkdir()
    monkeypatch.setenv("ORCH_HQ_ROOT", str(custom))
    assert resolve_hq_root(tmp_path / "Orchestrator") == custom.resolve()
