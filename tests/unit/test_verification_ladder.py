from __future__ import annotations

from scripts.verification_ladder import (
    L2_PYTEST_TARGETS,
    default_run_id,
    detect_container_runtime,
    level_record,
)


def test_default_run_id_prefix_and_shape() -> None:
    run_id = default_run_id("verify")
    assert run_id.startswith("verify-")
    assert "T" in run_id


def test_level_record_expected_vs_actual() -> None:
    record = level_record(
        level="L1",
        expected={"ok": True},
        actual={"ok": True, "detail": "flowctl --help"},
        passed=True,
    )
    assert record["level"] == "L1"
    assert record["expected"]["ok"] is True
    assert record["actual"]["detail"] == "flowctl --help"
    assert record["passed"] is True


def test_detect_container_runtime_returns_known_or_none() -> None:
    runtime = detect_container_runtime()
    assert runtime is None or runtime in {"podman", "docker"}


def test_l2_targets_include_api_and_delivery() -> None:
    joined = " ".join(L2_PYTEST_TARGETS)
    assert "test_r4_api_auth.py" in joined
    assert "test_r4_delivery.py" in joined


def test_level_record_failed_when_mismatch() -> None:
    record = level_record(
        level="L3",
        expected={"r4d_verify_exit_zero": True},
        actual={"r4d_verify_exit_zero": False, "exit_code": 1},
        passed=False,
    )
    assert record["passed"] is False
    assert record["actual"]["exit_code"] == 1
