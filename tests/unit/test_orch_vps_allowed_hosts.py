"""Executable tests for scripts/orch_vps_allowed_hosts.sh merge and env rewrite."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/orch_vps_allowed_hosts.sh"
REQUIRED = [
    "api.thedirectorate.app",
    "127.0.0.1",
    "localhost",
    "www.thedirectorate.app",
    "orch-api-blue",
    "orch-api-green",
    "api-blue",
    "api-green",
    "orch-console-blue",
    "orch-console-green",
]


def _source_prefix() -> str:
    return f'source "{SCRIPT}"; '


def _run_merge(current: str) -> list[str]:
    proc = subprocess.run(
        [
            "/bin/bash",
            "-c",
            _source_prefix() + f'orch_merge_django_allowed_hosts "{current}"',
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return [host for host in proc.stdout.strip().split(",") if host]


def _run_ensure(env_file: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            _source_prefix() + f'orch_ensure_env_allowed_hosts_line "{env_file}"',
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _allowed_hosts_lines(env_file: Path) -> list[str]:
    return [
        line
        for line in env_file.read_text(encoding="utf-8").splitlines()
        if line.startswith("DJANGO_ALLOWED_HOSTS=")
    ]


def _allowed_hosts_value(env_file: Path) -> str:
    lines = _allowed_hosts_lines(env_file)
    assert len(lines) == 1, lines
    return lines[0].split("=", 1)[1]


def test_script_avoids_sed_for_env_rewrite() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "sed" not in text


def test_merge_includes_all_required_aliases() -> None:
    merged = _run_merge("")
    for host in REQUIRED:
        assert host in merged


def test_merge_removes_duplicate_hosts() -> None:
    merged = _run_merge(
        "api.thedirectorate.app,orch-api-blue,api.thedirectorate.app,orch-api-blue"
    )
    assert merged.count("api.thedirectorate.app") == 1
    assert merged.count("orch-api-blue") == 1


def test_merge_preserves_normal_operator_extra() -> None:
    merged = _run_merge("api.thedirectorate.app,custom.operator.example")
    assert "custom.operator.example" in merged
    assert merged.index("api.thedirectorate.app") < merged.index("orch-api-blue")


def test_ensure_env_preserves_ampersand_in_operator_extra(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.vps"
    env_file.write_text(
        "REDIS_PASSWORD=secret\n"
        "DJANGO_ALLOWED_HOSTS=api.thedirectorate.app,weird&value\n",
        encoding="utf-8",
    )
    proc = _run_ensure(env_file)
    assert proc.returncode == 0, proc.stderr
    value = _allowed_hosts_value(env_file)
    assert "weird&value" in value
    assert "orch-api-blue" in value
    assert "weirdDJANGO_ALLOWED_HOSTS=" not in value
    assert len(_allowed_hosts_lines(env_file)) == 1
    after_first = env_file.read_text(encoding="utf-8")
    proc2 = _run_ensure(env_file)
    assert proc2.returncode == 0, proc2.stderr
    assert env_file.read_text(encoding="utf-8") == after_first


def test_ensure_env_preserves_pipe_in_operator_extra(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.vps"
    env_file.write_text(
        "DJANGO_ALLOWED_HOSTS=api.thedirectorate.app,weird|value\n",
        encoding="utf-8",
    )
    proc = _run_ensure(env_file)
    assert proc.returncode == 0, proc.stderr
    value = _allowed_hosts_value(env_file)
    assert "weird|value" in value
    assert "orch-console-green" in value


def test_ensure_env_is_idempotent(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.vps"
    env_file.write_text(
        "DJANGO_ALLOWED_HOSTS=api.thedirectorate.app,127.0.0.1,localhost,custom.operator.example\n",
        encoding="utf-8",
    )
    proc1 = _run_ensure(env_file)
    assert proc1.returncode == 0, proc1.stderr
    first = env_file.read_text(encoding="utf-8")
    proc2 = _run_ensure(env_file)
    assert proc2.returncode == 0, proc2.stderr
    assert env_file.read_text(encoding="utf-8") == first


def test_ensure_env_collapses_duplicate_allowed_host_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.vps"
    env_file.write_text(
        "DJANGO_ALLOWED_HOSTS=api.thedirectorate.app\n"
        "DJANGO_ALLOWED_HOSTS=api.thedirectorate.app,custom.operator.example\n",
        encoding="utf-8",
    )
    proc = _run_ensure(env_file)
    assert proc.returncode == 0, proc.stderr
    lines = _allowed_hosts_lines(env_file)
    assert len(lines) == 1
    value = lines[0].split("=", 1)[1]
    assert "custom.operator.example" in value
    assert "orch-api-green" in value
