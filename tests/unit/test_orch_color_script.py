"""Static safety checks for deploy/vps/orch_color.sh blue/green orchestration."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/vps/orch_color.sh"
BOOTSTRAP = ROOT / "deploy/vps/vps_bootstrap.sh"
CONSOLE = ROOT / "deploy/vps/run_ops_console.sh"
HEALTH = ROOT / "deploy/vps/healthcheck.sh"
PRESENTATION = ROOT / "deploy/vps/orch_presentation_env.sh"
ECOSYSTEM = ROOT / "deploy/vps/deploy_ecosystem.sh"
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
        'cd "$ORCH_ROOT"',
        "COMPOSE_PROJECT_NAME",
        "ambiguous container count",
        "/ops/summary/",
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
    assert "sed -i" not in text or "thedirectorate" in text
    assert "ops-console.service portfolio-stub" not in text
    assert "disable-singleton-console" in text
    assert "down -v" not in text


def test_bootstrap_pins_compose_and_requires_attestation_file() -> None:
    text = _read(BOOTSTRAP)
    assert 'cd "$ORCH_ROOT"' in text
    assert "COMPOSE_PROJECT_NAME" in text
    assert "attestation path is not a regular JSON file" in text
    assert "ORCH_HEALTH_COLOR=shared" in text


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


def test_ops_console_isolated_per_color_network() -> None:
    text = _read(CONSOLE)
    presentation = _read(PRESENTATION)
    assert "--color" in text
    assert "orchestrator-console-" in text
    assert "network connect --alias api" in text
    assert "orch_resolve_api_cid_for_color" in text
    assert "ambiguous API container count" in presentation
    assert "VITE_API_BASE_URL" not in text
    assert "8081" in text and "8091" in text
    assert "ORCH_COMPOSE_PROJECT" in text
    assert "COMPOSE_PROJECT_NAME" in presentation


def test_healthcheck_strict_script_runner_semantics() -> None:
    text = _read(HEALTH)
    presentation = _read(PRESENTATION)
    assert "orch_presentation_env.sh" in text
    assert "$ORCH_ROOT" in text
    assert "script-spool-init" in text
    assert "script-runner" in text
    assert "ambiguous API container count" in presentation
    assert "healthy" in text


def test_deploy_ecosystem_orchestrator_sync_contract() -> None:
    text = _read(ECOSYSTEM)
    assert "ORCH_PROTECTED_EXCLUDES" in text
    for exclude in (
        "--exclude '/.env.vps'",
        "--exclude '/deploy/vps/.state/'",
        "--exclude '/deploy/attestations/'",
        "--exclude '/backups/'",
    ):
        assert exclude in text, f"missing protected exclude {exclude}"
    assert "ORCH_RSYNC_DELETE=0" in text
    assert "--delete) ORCH_RSYNC_DELETE=1" in text
    assert "rsync_orchestrator" in text
