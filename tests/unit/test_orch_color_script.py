"""Static safety checks for deploy/vps/orch_color.sh blue/green orchestration."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/vps/orch_color.sh"
BOOTSTRAP = ROOT / "deploy/vps/vps_bootstrap.sh"
ECOSYSTEM_UNIT = ROOT / "deploy/vps/systemd/orchestrator-ecosystem.service"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_orch_color_script_exists_and_is_executable_contract() -> None:
    assert SCRIPT.is_file()
    text = _read(SCRIPT)
    for needle in (
        "deploy shared",
        "deploy --color",
        "--no-deps",
        "ORCH_COLOR_MATERIALIZE_ONLY",
        "switch blocked",
        "shared_ids.before",
        "container ID changed",
        "nginx -t",
        "deploy/vps/.state",
        "orch_active_color.prev",
        "api-blue",
        "api-green",
        "write_selector_map",
        "reload_edge_proxy",
    ):
        assert needle in text, f"missing {needle!r} in orch_color.sh"


def test_orch_color_does_not_write_hfm_secrets_dir() -> None:
    text = _read(SCRIPT)
    assert ".secrets/last_orch" not in text
    assert "last_orch_active_color" not in text
    assert "deploy/vps/.state" in text


def test_orch_color_materialize_blocks_switch() -> None:
    text = _read(SCRIPT)
    assert 'ORCH_COLOR_MATERIALIZE_ONLY" == "1"' in text
    assert "switch blocked" in text


def test_bootstrap_drops_tracked_sed_and_singleton_console_enable() -> None:
    text = _read(BOOTSTRAP)
    assert "patch_orch_base_ports" not in text
    assert "sed -i" not in text or "thedirectorate" in text  # hostname patch only
    assert "ops-console.service portfolio-stub" not in text
    assert "disable-singleton-console" in text
    assert "down -v" not in text


def test_ecosystem_unit_shared_plane_only_no_api() -> None:
    text = _read(ECOSYSTEM_UNIT)
    assert "api-blue" not in text
    assert " api " not in f" {text} "
    for svc in (
        "redis",
        "coordinator",
        "worker",
        "scheduler",
        "script-spool-init",
        "script-runner",
        "script-worker",
    ):
        assert svc in text


def test_ops_console_script_color_ports_and_build_arg() -> None:
    text = _read(ROOT / "deploy/vps/run_ops_console.sh")
    assert "--color" in text
    assert "VITE_API_BASE_URL" in text
    assert "8081" in text and "8091" in text
    assert "idle API routing" in text or "does not prove" in text
