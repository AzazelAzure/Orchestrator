"""Tests for deploy/vps/orch_publish_env.sh validation helpers."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLISH_ENV = ROOT / "deploy/vps/orch_publish_env.sh"


def _run_publish_env(body: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    script = f"""
    set -euo pipefail
    source "{PUBLISH_ENV}"
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


def test_publish_host_require_fails_when_missing(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_TOKEN_FOUNDER=test\n", encoding="utf-8")
    proc = _run_publish_env(
        f"ORCH_ROOT={orch}\norch_publish_host_require",
    )
    assert proc.returncode != 0
    assert "ORCH_PUBLISH_HOST" in proc.stderr


def test_publish_host_rejects_all_interfaces(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_PUBLISH_HOST=0.0.0.0\n", encoding="utf-8")
    proc = _run_publish_env(
        f"ORCH_ROOT={orch}\norch_publish_host_require",
    )
    assert proc.returncode != 0
    assert "binds all interfaces" in proc.stderr or "0.0.0.0" in proc.stderr


def test_publish_probe_host_reads_env_file(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_PUBLISH_HOST=10.89.1.1\n", encoding="utf-8")
    proc = _run_publish_env(
        f"ORCH_ROOT={orch}\norch_publish_probe_host",
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "10.89.1.1"


def test_publish_url_builds_probe_target(tmp_path: Path) -> None:
    orch = tmp_path / "orchestrator"
    orch.mkdir()
    (orch / ".env.vps").write_text("ORCH_PUBLISH_HOST=10.89.1.1\n", encoding="utf-8")
    proc = _run_publish_env(
        f"ORCH_ROOT={orch}\norch_publish_url 8010 /health/",
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "http://10.89.1.1:8010/health/"


def test_generate_vps_env_omits_publish_host() -> None:
    gen = ROOT / "scripts/generate_vps_env.sh"
    assert gen.is_file()
    text = gen.read_text(encoding="utf-8")
    assert "ORCH_PUBLISH_HOST" not in text
