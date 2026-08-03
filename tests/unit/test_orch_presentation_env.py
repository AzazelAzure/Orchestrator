"""Tests for deploy/vps/orch_presentation_env.sh helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRESENTATION_ENV = ROOT / "deploy/vps/orch_presentation_env.sh"


def _run(body: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = f"""
    set -euo pipefail
    source "{PRESENTATION_ENV}"
    {body}
    """
    merged = os.environ.copy()
    if env:
        merged.update(env)
    return subprocess.run(
        ["/bin/bash", "-c", script],
        text=True,
        capture_output=True,
        env=merged,
    )


def test_presentation_network_names_are_stable() -> None:
    proc = _run(
        """
        orch_presentation_network_for_color blue
        printf '\\n'
        orch_api_alias_for_color green
        printf '\\n'
        orch_console_alias_for_color blue
        """
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines == [
        "orchestrator-console-blue",
        "orch-api-green",
        "orch-console-blue",
    ]


def test_diag_bind_rejects_non_loopback(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_DIAG_BIND=0.0.0.0\n", encoding="utf-8")
    proc = _run(
        f"ORCH_ROOT={orch}\norch_diag_bind_enabled",
    )
    assert proc.returncode != 0
    assert "ORCH_DIAG_BIND" in proc.stderr


def test_diag_bind_allows_loopback(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_DIAG_BIND=127.0.0.1\n", encoding="utf-8")
    proc = _run(
        f"ORCH_ROOT={orch}\norch_diag_bind_enabled && orch_diag_publish_args 8081 8081",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "-p 127.0.0.1:8081:8081"


def test_diag_bind_disabled_by_default(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_TOKEN_FOUNDER=test\n", encoding="utf-8")
    proc = _run(
        f"ORCH_ROOT={orch}\norch_diag_bind_enabled || true; orch_diag_publish_args 8000 8000",
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == ""


def test_presentation_urls_use_color_aliases() -> None:
    proc = _run(
        """
        orch_presentation_api_url_in_network blue /health/
        printf '\\n'
        orch_presentation_console_url_in_network green /ops/summary/
        """
    )
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert lines == [
        "http://orch-api-blue:8000/health/",
        "http://orch-console-green:8081/ops/summary/",
    ]


def test_generate_vps_env_omits_publish_host() -> None:
    gen = ROOT / "scripts/generate_vps_env.sh"
    assert gen.is_file()
    text = gen.read_text(encoding="utf-8")
    assert "ORCH_PUBLISH_HOST" not in text
    assert "ORCH_SCRIPT_IMAGE_DIGEST" in text
