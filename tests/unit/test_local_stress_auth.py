"""Founder-bearer propagation for scripts.local_delegation_stress and
scripts.local_stress_test ops-summary checks (ORCH-LI / LI-5).

Both callers must send an authenticated Authorization: Bearer request to
ops_summary_url, must never fall back to an anonymous request when the
founder token is missing, and local_delegation_stress.py must not crash on a
missing token or HTTP/auth failure: it must persist a failed
ops_summary_hierarchy row, write a terminal summary.json, and return 1.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.local_delegation_stress import ROOT as DELEGATION_ROOT
from scripts.local_delegation_stress import check_ops_summary as delegation_check_ops_summary
from scripts.local_delegation_stress import main as delegation_main
from scripts.local_stress_test import check_ops_summary as stress_check_ops_summary


def _write_manifest(tmp_path: Path, *, env_lines: list[str]) -> tuple[Path, Path]:
    env_file = tmp_path / "stack.env"
    env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest = {
        "env_file": str(env_file),
        "api_base": "http://127.0.0.1:8000",
        "ops_summary_url": "http://127.0.0.1:8000/ops/summary/",
        "work_item_id": "work-existing",
        "compose_project": "orch-local-test",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path, env_file


def _http_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode("utf-8")
    resp.__enter__.return_value = resp
    return resp


# --- scripts.local_delegation_stress.check_ops_summary -----------------------


def test_delegation_check_ops_summary_sends_founder_bearer(tmp_path: Path) -> None:
    manifest_path, _ = _write_manifest(tmp_path, env_lines=["ORCH_TOKEN_FOUNDER=founder-secret"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    env = {"ORCH_TOKEN_FOUNDER": "founder-secret"}

    with patch("scripts.local_delegation_stress.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _http_response({"status": "ok", "hierarchy": {"profiles": []}})
        body = delegation_check_ops_summary(manifest, env)

    assert body["status"] == "ok"
    req = urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer founder-secret"
    assert req.full_url == manifest["ops_summary_url"]


def test_delegation_check_ops_summary_missing_token_raises_without_request() -> None:
    manifest = {"ops_summary_url": "http://127.0.0.1:8000/ops/summary/"}
    env: dict[str, str] = {}

    with patch("scripts.local_delegation_stress.urllib.request.urlopen") as urlopen:
        try:
            delegation_check_ops_summary(manifest, env)
        except RuntimeError as exc:
            assert "ORCH_TOKEN_FOUNDER" in str(exc)
        else:
            raise AssertionError("expected RuntimeError for missing founder token")
    urlopen.assert_not_called()


# --- scripts.local_delegation_stress.main end-to-end --------------------------


def _run_delegation_main_with_mocks(
    manifest_path: Path,
    *,
    ops_summary_urlopen_mock=None,
) -> tuple[int, Path]:
    run_id = f"test-{uuid.uuid4().hex[:10]}"
    out_dir = DELEGATION_ROOT / ".tmp" / "local-delegation" / run_id

    responses = [
        (
            200,
            {
                "command_type": "delegation.request",
                "command_status": "accepted",
                "from_cache": False,
                "result": {"request": {"id": "req-1"}},
            },
        ),
        (200, {"command_type": "delegation.accept"}),
        (
            200,
            {
                "command_type": "delegation.dispatch",
                "result": {"assignment": {"id": "asg-1"}, "pin": {"id": "pin-1"}},
            },
        ),
        (200, {"command_type": "runtime.preview", "result": {}, "mcp": {"lane_id": "workflow-control"}}),
        (200, {"command_type": "runtime.run", "result": {"created": {"run": {"id": "run-1"}}}}),
    ]
    api_instance = MagicMock()
    api_instance.request.side_effect = responses

    patches = [
        patch("scripts.local_delegation_stress.refresh_work_item", return_value=None),
        patch("scripts.local_delegation_stress.reset_local_acceptance_budget", return_value=None),
        patch(
            "scripts.local_delegation_stress.seed_org",
            return_value={
                "parent_assignment_id": "parent-1",
                "worker_position_id": "worker-1",
                "impl_actor_id": "actor-1",
                "impl_seat_id": "seat-1",
                "work_item_id": "work-1",
                "organization_id": "org-1",
            },
        ),
        patch("scripts.local_delegation_stress.ApiClient", return_value=api_instance),
        patch.dict(
            "os.environ",
            {"ORCH_LOCAL_STACK_MANIFEST": str(manifest_path), "ORCH_DELEGATION_RUN_ID": run_id},
        ),
    ]
    if ops_summary_urlopen_mock is not None:
        patches.append(patch("scripts.local_delegation_stress.urllib.request.urlopen", ops_summary_urlopen_mock))

    try:
        for p in patches:
            p.start()
        rc = delegation_main()
    finally:
        for p in reversed(patches):
            p.stop()
    return rc, out_dir


def test_delegation_main_missing_token_returns_1_and_persists_failed_summary(tmp_path: Path) -> None:
    manifest_path, _ = _write_manifest(tmp_path, env_lines=["OTHER_VAR=1"])  # no ORCH_TOKEN_FOUNDER
    urlopen_mock = MagicMock()

    rc, out_dir = _run_delegation_main_with_mocks(manifest_path, ops_summary_urlopen_mock=urlopen_mock)
    try:
        assert rc == 1
        urlopen_mock.assert_not_called()  # never retry anonymously
        summary_path = out_dir / "summary.json"
        assert summary_path.is_file()
        persisted = json.loads(summary_path.read_text(encoding="utf-8"))
        assert persisted["passed"] is False
        ops_row = next(r for r in persisted["rows"] if r["step"] == "ops_summary_hierarchy")
        assert ops_row["passed"] is False
        assert "ORCH_TOKEN_FOUNDER" in json.dumps(ops_row["detail"])
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_delegation_main_redacts_token_embedded_in_exception_message(tmp_path: Path) -> None:
    """Failure-path regression: a raw token in the exception message itself.

    Pattern-based redaction only recognizes keyword-prefixed shapes
    (``token=``, ``Bearer ...``); a bare token following a space (no
    ``=``/``:``) is deliberately used here so this test only passes if the
    known-secret value is scrubbed directly, not merely "probably" caught by
    a generic regex.
    """
    synthetic_token = "SYNTHETIC-FOUNDER-TOKEN-IN-EXC-MSG-def456"  # pragma: allowlist secret
    manifest_path, _ = _write_manifest(tmp_path, env_lines=[f"ORCH_TOKEN_FOUNDER={synthetic_token}"])

    def _urlopen(req, timeout=15):  # noqa: ARG001
        raise RuntimeError(f"connection reset while calling ops summary for token {synthetic_token}")

    rc, out_dir = _run_delegation_main_with_mocks(manifest_path, ops_summary_urlopen_mock=_urlopen)
    try:
        assert rc == 1
        summary_path = out_dir / "summary.json"
        raw_text = summary_path.read_text(encoding="utf-8")
        assert synthetic_token not in raw_text
        persisted = json.loads(raw_text)
        ops_row = next(r for r in persisted["rows"] if r["step"] == "ops_summary_hierarchy")
        assert ops_row["passed"] is False
        # Useful error reporting preserved: the non-secret context survives redaction.
        assert "connection reset while calling ops summary" in ops_row["detail"]["error"]
        assert "[REDACTED]" in ops_row["detail"]["error"]
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def test_delegation_main_redacts_founder_token_from_persisted_summary(tmp_path: Path) -> None:
    synthetic_token = "SYNTHETIC-FOUNDER-TOKEN-DO-NOT-LEAK-abc123"  # pragma: allowlist secret
    manifest_path, _ = _write_manifest(tmp_path, env_lines=[f"ORCH_TOKEN_FOUNDER={synthetic_token}"])

    def _urlopen(req, timeout=15):  # noqa: ARG001
        assert req.get_header("Authorization") == f"Bearer {synthetic_token}"
        return _http_response({"status": "ok", "hierarchy": {"profiles": [{"id": "p1"}]}})

    rc, out_dir = _run_delegation_main_with_mocks(manifest_path, ops_summary_urlopen_mock=_urlopen)
    try:
        assert rc == 0
        summary_path = out_dir / "summary.json"
        raw_text = summary_path.read_text(encoding="utf-8")
        assert synthetic_token not in raw_text
        persisted = json.loads(raw_text)
        ops_row = next(r for r in persisted["rows"] if r["step"] == "ops_summary_hierarchy")
        assert ops_row["passed"] is True
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# --- scripts.local_stress_test.check_ops_summary ------------------------------


def test_stress_test_check_ops_summary_sends_founder_bearer(tmp_path: Path) -> None:
    manifest_path, _ = _write_manifest(tmp_path, env_lines=["ORCH_TOKEN_FOUNDER=founder-secret"])

    with (
        patch.dict("os.environ", {"ORCH_LOCAL_STACK_MANIFEST": str(manifest_path)}),
        patch("urllib.request.urlopen") as urlopen,
    ):
        urlopen.return_value = _http_response({"status": "ok"})
        ok, detail = stress_check_ops_summary()

    assert ok is True
    req = urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer founder-secret"


def test_stress_test_check_ops_summary_missing_token_returns_false_without_request(tmp_path: Path) -> None:
    manifest_path, _ = _write_manifest(tmp_path, env_lines=["OTHER_VAR=1"])  # no ORCH_TOKEN_FOUNDER

    with (
        patch.dict("os.environ", {"ORCH_LOCAL_STACK_MANIFEST": str(manifest_path)}),
        patch("urllib.request.urlopen") as urlopen,
    ):
        ok, detail = stress_check_ops_summary()

    assert ok is False
    assert "ORCH_TOKEN_FOUNDER" in detail
    urlopen.assert_not_called()


def test_stress_test_check_ops_summary_redacts_token_embedded_in_exception_message(
    tmp_path: Path,
) -> None:
    """Symmetric failure-path regression for local_stress_test.check_ops_summary.

    Mirrors the local_delegation_stress regression: a bare token following a
    space (no ``=``/``:`` prefix) is deliberately used so this only passes if
    the known-secret value is scrubbed directly, not merely "probably" caught
    by the generic SECRET_PATTERN regex.
    """
    synthetic_token = "SYNTHETIC-FOUNDER-TOKEN-STRESS-EXC-MSG-ghi789"  # pragma: allowlist secret
    manifest_path, _ = _write_manifest(tmp_path, env_lines=[f"ORCH_TOKEN_FOUNDER={synthetic_token}"])

    def _raise_with_token(req, timeout=10):  # noqa: ARG001
        raise RuntimeError(f"connection reset while calling ops summary for token {synthetic_token}")

    with (
        patch.dict("os.environ", {"ORCH_LOCAL_STACK_MANIFEST": str(manifest_path)}),
        patch("urllib.request.urlopen", _raise_with_token),
    ):
        ok, detail = stress_check_ops_summary()

    assert ok is False
    assert synthetic_token not in detail
    # Useful non-secret context is retained after redaction.
    assert "connection reset while calling ops summary" in detail
    assert "[REDACTED]" in detail


def test_stress_test_check_ops_summary_missing_manifest_returns_false(tmp_path: Path) -> None:
    missing_path = tmp_path / "does-not-exist.json"

    with patch.dict("os.environ", {"ORCH_LOCAL_STACK_MANIFEST": str(missing_path)}):
        ok, detail = stress_check_ops_summary()

    assert ok is False
    assert "missing" in detail
