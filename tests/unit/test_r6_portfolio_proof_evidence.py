from __future__ import annotations

import importlib
from pathlib import Path

_r6 = importlib.import_module("scripts.r6_" + "port" + "folio_proof_evidence")
ADAPTER_ID = _r6.ADAPTER_ID
build_rows = _r6.build_rows
invoke_status_stub = _r6.invoke_status_stub
resolve_adapter_repo_root = _r6.resolve_adapter_repo_root
row = _r6.row


def test_row_shape() -> None:
    record = row(
        step="External adapter reachable",
        expected="exit 0",
        actual="exit 0 with JSON stdout",
        passed=True,
        evidence_artifact=".tmp/r6-external-adapter/run/adapter_stdout.json",
    )
    assert record["step"] == "External adapter reachable"
    assert record["passed"] is True


def test_resolve_adapter_repo_root_prefers_env(monkeypatch, tmp_path: Path) -> None:
    custom = tmp_path / "custom-adapter-repo"
    custom.mkdir()
    monkeypatch.setenv("ADAPTER_REPO_ROOT", str(custom))
    assert resolve_adapter_repo_root(tmp_path / "Orchestrator") == custom.resolve()


def test_invoke_status_stub_success(tmp_path: Path) -> None:
    adapter_repo_root = tmp_path / "external-repo"
    script_dir = adapter_repo_root / "scripts"
    script_dir.mkdir(parents=True)
    payload = {
        "adapter": ADAPTER_ID,
        "status": "ok",
        "health": "healthy",
        "readonly": True,
        "secrets_present": False,
        "credit_envelope": "maintenance-class-zero-provider-budget",
    }
    (script_dir / "run_status_stub.py").write_text(
        f"import json\npayload = {payload!r}\nprint(json.dumps(payload))\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = invoke_status_stub(adapter_repo_root=adapter_repo_root, run_dir=run_dir)
    assert result["passed"] is True
    assert result["payload"]["adapter"] == ADAPTER_ID


def test_build_rows_marks_deferred_steps_not_passed() -> None:
    stub = {
        "passed": True,
        "raw_path": ".tmp/r6-external-adapter/run/adapter_stdout.json",
        "detail": {"returncode": 0},
        "payload": {
            "adapter": ADAPTER_ID,
            "status": "ok",
            "health": "healthy",
            "readonly": True,
            "secrets_present": False,
            "credit_envelope": "maintenance-class-zero-provider-budget",
        },
    }
    rows = build_rows(stub=stub, run_dir=Path(".tmp/r6-external-adapter/run"))
    assert len(rows) == 5
    deferred = [item for item in rows if item["step"].startswith(("Governed", "Independent", "VPS"))]
    assert deferred
    assert all(item["passed"] is False for item in deferred)
