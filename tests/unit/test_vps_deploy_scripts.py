"""Executable harness tests for VPS deploy scripts with guarded fake binaries."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy/vps"


def _stub_sh(name: str, body: str) -> str:
    return textwrap.dedent(
        f"""\
        #!/bin/bash
        set -euo pipefail
        MARKER="${{ORCH_TEST_MARKER:?ORCH_TEST_MARKER required}}"
        printf '%s\\n' "$MARKER" >> "${{MARKER}}.log"
        printf '%s argv=' "$0" >> "${{MARKER}}.log"
        printf ' %q' "$@" >> "${{MARKER}}.log"
        printf '\\n' >> "${{MARKER}}.log"
        printf 'cwd=%s\\n' "$PWD" >> "${{MARKER}}.log"
        {body}
        """
    )


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(_stub_sh(name, body), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_script(
    script: Path,
    *,
    env: dict[str, str],
    args: list[str],
    cwd: Path,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    merged = os.environ.copy()
    merged.update(env)
    return subprocess.run(
        ["/bin/bash", str(script), *args],
        cwd=cwd,
        env=merged,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _exclusive_path(bin_dir: Path) -> str:
    return str(bin_dir)


def _fixture_orch_root(tmp_path: Path) -> Path:
    orch = tmp_path / "orchestrator"
    for rel in (
        "docker-compose.yml",
        "deploy/vps/docker-compose.vps.yml",
        "deploy/vps/docker-compose.bluegreen.yml",
        ".env.vps",
        "deploy/vps/orch_color.sh",
        "deploy/vps/healthcheck.sh",
        "deploy/vps/run_ops_console.sh",
        "deploy/vps/vps_bootstrap.sh",
        "deploy/vps/orch_presentation_env.sh",
        "deploy/vps/ensure_presentation_networks.sh",
        "deploy/attestations/script-runner.attestation.json",
        "ops-console/Dockerfile",
    ):
        path = orch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel.endswith(".yml"):
            path.write_text("services: {}\nvolumes: {}\nnetworks: {}\n", encoding="utf-8")
        elif rel.endswith(".env.vps"):
            path.write_text(
                "ORCH_TOKEN_FOUNDER=test-token\n",
                encoding="utf-8",
            )
        elif rel.endswith(".json"):
            path.write_text('{"image_digest":"sha256:deadbeef"}\n', encoding="utf-8")
        elif rel.endswith("Dockerfile"):
            path.write_text("FROM scratch\n", encoding="utf-8")
        else:
            src = ROOT / rel
            path.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            if src.suffix == ".sh":
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return orch


def _write_utility_stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            set -euo pipefail
            {body}
            """
        ),
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_sed_stub(bin_dir: Path) -> None:
    _write_utility_stub(
        bin_dir,
        "sed",
        textwrap.dedent(
            """
            if [[ "${1:-}" == "/^$/d" ]]; then
              while IFS= read -r line; do [[ -n "$line" ]] && printf '%s\\n' "$line"; done
              exit 0
            fi
            if [[ "${1:-}" == "s#^/##" ]]; then
              while IFS= read -r line; do printf '%s\\n' "${line#/}"; done
              exit 0
            fi
            exit 0
            """
        ),
    )


class TestDeployEcosystemHarness:
    def test_orchestrator_sync_no_delete_with_protected_excludes(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "rsync.marker"
        _write_stub(
            bin_dir,
            "rsync",
            'if [[ " $* " == *" --delete "* ]]; then echo "unexpected --delete" >&2; exit 9; fi; exit 0',
        )
        _write_stub(bin_dir, "ssh", "exit 0")

        env = {
            "PATH": _exclusive_path(bin_dir),
            "ORCH_TEST_MARKER": str(marker),
            "VPS_SSH_TARGET": "test-invalid.invalid",
            "HFM_ROOT": str(tmp_path / "missing-hfm"),
            "PORT_ROOT": str(tmp_path / "missing-port"),
        }
        result = _run_script(
            DEPLOY / "deploy_ecosystem.sh",
            env=env,
            args=["--skip-hfm", "--skip-portfolio"],
            cwd=Path("/tmp"),
        )
        assert result.returncode == 0, result.stderr
        log = Path(f"{marker}.log").read_text(encoding="utf-8")
        assert "--delete" not in log
        for exclude in (
            "/.env.vps",
            "/deploy/vps/.state/",
            "/deploy/attestations/",
            "/backups/",
        ):
            assert exclude in log, f"missing exclude {exclude}"

    def test_orchestrator_sync_opt_in_delete(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "rsync-delete.marker"
        _write_stub(
            bin_dir,
            "rsync",
            'if [[ " $* " != *" --delete "* ]]; then echo "expected --delete" >&2; exit 8; fi; exit 0',
        )
        _write_stub(bin_dir, "ssh", "exit 0")

        env = {
            "PATH": _exclusive_path(bin_dir),
            "ORCH_TEST_MARKER": str(marker),
            "VPS_SSH_TARGET": "test-invalid.invalid",
            "HFM_ROOT": str(tmp_path / "missing-hfm"),
            "PORT_ROOT": str(tmp_path / "missing-port"),
        }
        result = _run_script(
            DEPLOY / "deploy_ecosystem.sh",
            env=env,
            args=["--delete", "--skip-hfm", "--skip-portfolio"],
            cwd=Path("/tmp"),
        )
        assert result.returncode == 0, result.stderr
        log = Path(f"{marker}.log").read_text(encoding="utf-8")
        assert "--delete" in log

    def test_hfm_sync_rsyncs_network_attach_tooling(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        hfm = tmp_path / "hfm"
        for rel in (
            "proxy/conf.d/ecosystem-hosts.conf.template",
            "scripts/ops/render_ecosystem_hosts.sh",
            "scripts/ops/attach_orchestrator_proxy_networks.sh",
            "proxy/nginx.bluegreen.conf",
            "docker-compose.bluegreen.yml",
            "proxy/certs/generate-ecosystem-certs.sh",
        ):
            dest = hfm / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(f"stub:{rel}\n", encoding="utf-8")
            if dest.suffix == ".sh":
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR)

        marker = tmp_path / "hfm-sync.marker"
        _write_stub(bin_dir, "rsync", "exit 0")
        _write_stub(bin_dir, "ssh", "exit 0")

        env = {
            "PATH": f"{_exclusive_path(bin_dir)}:/usr/bin:/bin",
            "ORCH_TEST_MARKER": str(marker),
            "VPS_SSH_TARGET": "test-invalid.invalid",
            "HFM_ROOT": str(hfm),
            "PORT_ROOT": str(tmp_path / "missing-port"),
        }
        result = _run_script(
            DEPLOY / "deploy_ecosystem.sh",
            env=env,
            args=["--skip-orch", "--skip-portfolio"],
            cwd=Path("/tmp"),
        )
        assert result.returncode == 0, result.stderr
        log = Path(f"{marker}.log").read_text(encoding="utf-8")
        assert "ecosystem-hosts.conf.template" in log
        assert "attach_orchestrator_proxy_networks.sh" in log
        assert "ORCH_PUBLISH_HOST" not in log


class TestRunOpsConsoleHarness:
    def test_blue_console_uses_blue_api_only(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "console.marker"
        orch = _fixture_orch_root(tmp_path)

        _write_stub(
            bin_dir,
            "podman-compose",
            textwrap.dedent(
                """
                if [[ " $* " == *" ps "* && " $* " == *" api-blue "* ]]; then echo cid-blue; exit 0; fi
                if [[ " $* " == *" ps "* && " $* " == *" api-green "* ]]; then echo cid-green; exit 0; fi
                exit 0
                """
            ),
        )
        _write_stub(
            bin_dir,
            "podman",
            textwrap.dedent(
                """
                case "$1" in
                  network)
                    if [[ "$2" == "exists" ]]; then exit 0; fi
                    if [[ "$2" == "create" ]]; then exit 0; fi
                    if [[ "$2" == "disconnect" ]]; then exit 0; fi
                    if [[ "$2" == "connect" ]]; then
                      if [[ "$8" != "cid-blue" ]]; then echo "wrong api cid: $8" >&2; exit 3; fi
                      if [[ "$7" != *"orchestrator-console-blue" ]]; then echo "wrong network: $7" >&2; exit 4; fi
                      exit 0
                    fi
                    ;;
                  build) exit 0 ;;
                  rm) exit 0 ;;
                  run) exit 0 ;;
                  logs) exit 0 ;;
                  exec) exit 0 ;;
                  container)
                    if [[ "$2" == "exists" ]]; then exit 0; fi
                    ;;
                esac
                exit 0
                """
            ),
        )
        _write_stub(bin_dir, "bash", "exit 0")
        _write_sed_stub(bin_dir)

        env = {
            "PATH": f"{_exclusive_path(bin_dir)}:/usr/bin:/bin",
            "ORCH_TEST_MARKER": str(marker),
            "ORCH_ROOT": str(orch),
            "COMPOSE": "podman-compose",
        }
        result = _run_script(
            orch / "deploy/vps/run_ops_console.sh",
            env=env,
            args=["--color", "blue"],
            cwd=Path("/tmp"),
        )
        assert result.returncode == 0, result.stderr

    def test_missing_api_fails_closed(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        orch = _fixture_orch_root(tmp_path)
        _write_stub(bin_dir, "podman-compose", "exit 0")
        _write_stub(bin_dir, "podman", "exit 0")
        _write_sed_stub(bin_dir)

        env = {
            "PATH": f"{_exclusive_path(bin_dir)}:/usr/bin:/bin",
            "ORCH_TEST_MARKER": str(tmp_path / "missing.marker"),
            "ORCH_ROOT": str(orch),
            "COMPOSE": "podman-compose",
        }
        result = _run_script(
            orch / "deploy/vps/run_ops_console.sh",
            env=env,
            args=["--color", "green"],
            cwd=Path("/tmp"),
        )
        assert result.returncode != 0
        assert "no running API container" in result.stderr


class TestHealthcheckHarness:
    def test_exited_script_runner_fails(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        orch = _fixture_orch_root(tmp_path)

        compose_body = textwrap.dedent(
            """
            if [[ " $* " != *" ps "* ]]; then exit 0; fi
            case "$*" in
              *script-spool-init*) echo cid-spool-init ;;
              *script-runner*) echo cid-runner ;;
              *script-worker*) echo cid-worker-script ;;
              *scheduler*) echo cid-sched ;;
              *coordinator*) echo cid-coord ;;
              *redis*) echo cid-redis ;;
              *worker*) echo cid-worker ;;
            esac
            exit 0
            """
        )
        _write_stub(bin_dir, "podman-compose", compose_body)

        podman_body = textwrap.dedent(
            r"""
            if [[ "$1" != "inspect" ]]; then exit 0; fi
            fmt="$3"
            target="$4"
            name_hint="$target"
            if [[ "$fmt" == *Name* ]]; then
              case "$target" in
                cid-redis) echo "/orchestrator_redis_1" ;;
                cid-coord) echo "/orchestrator_coordinator_1" ;;
                cid-worker) echo "/orchestrator_worker_1" ;;
                cid-sched) echo "/orchestrator_scheduler_1" ;;
                cid-spool-init) echo "/orchestrator_script-spool-init_1" ;;
                cid-runner) echo "/orchestrator_script-runner_1" ;;
                cid-worker-script) echo "/orchestrator_script-worker_1" ;;
                *) echo "/orchestrator_unknown_1" ;;
              esac
              exit 0
            fi
            case "$target" in
              *redis*)
                [[ "$fmt" == *Status* && "$fmt" != *Health* && "$fmt" != *Exit* ]] && echo running && exit 0
                [[ "$fmt" == *Health* ]] && echo healthy && exit 0
                ;;
              *coordinator*|*worker_1*|*worker\"*)
                [[ "$fmt" == *Status* && "$fmt" != *Health* && "$fmt" != *Exit* ]] && echo running && exit 0
                [[ "$fmt" == *Health* ]] && echo healthy && exit 0
                ;;
              *scheduler*)
                [[ "$fmt" == *Status* && "$fmt" != *Health* && "$fmt" != *Exit* ]] && echo running && exit 0
                [[ "$fmt" == *Health* ]] && echo healthy && exit 0
                ;;
              *script-spool-init*)
                [[ "$fmt" == *Status* && "$fmt" != *Health* && "$fmt" != *Exit* ]] && echo running && exit 0
                [[ "$fmt" == *Health* ]] && echo healthy && exit 0
                ;;
              *script-runner*)
                [[ "$fmt" == *Status* && "$fmt" != *Health* && "$fmt" != *Exit* ]] && echo exited && exit 0
                ;;
              *script-worker*)
                [[ "$fmt" == *Status* && "$fmt" != *Health* ]] && echo running && exit 0
                ;;
            esac
            echo missing >&2
            exit 0
            """
        )
        _write_stub(bin_dir, "podman", podman_body)
        _write_stub(bin_dir, "curl", "exit 0")
        _write_sed_stub(bin_dir)

        env = {
            "PATH": f"{_exclusive_path(bin_dir)}:/usr/bin:/bin",
            "ORCH_TEST_MARKER": str(tmp_path / "health.marker"),
            "ORCH_ROOT": str(orch),
            "ORCH_HEALTH_COLOR": "shared",
            "COMPOSE": "podman-compose",
        }
        result = _run_script(
            orch / "deploy/vps/healthcheck.sh",
            env=env,
            args=[],
            cwd=Path("/tmp"),
        )
        assert result.returncode != 0
        combined = result.stdout + result.stderr
        assert "script-runner" in combined

    def test_compose_invoked_from_unrelated_cwd(self, tmp_path: Path) -> None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        marker = tmp_path / "compose.marker"
        orch = _fixture_orch_root(tmp_path)
        _write_stub(
            bin_dir,
            "podman-compose",
            'if [[ "$PWD" != *"orchestrator" ]]; then echo "wrong cwd: $PWD" >&2; exit 7; fi; exit 0',
        )
        _write_stub(
            bin_dir,
            "podman",
            textwrap.dedent(
                """
                if [[ "$1" == "inspect" ]]; then
                  case "$3" in
                    *Name*) echo "/orchestrator_redis_1" ;;
                    *Status*) echo "healthy" ;;
                    *Health*) echo "healthy" ;;
                  esac
                  exit 0
                fi
                exit 0
                """
            ),
        )
        _write_stub(bin_dir, "curl", "exit 0")
        _write_sed_stub(bin_dir)

        env = {
            "PATH": _exclusive_path(bin_dir),
            "ORCH_TEST_MARKER": str(marker),
            "ORCH_ROOT": str(orch),
            "ORCH_HEALTH_COLOR": "shared",
            "COMPOSE": "podman-compose",
        }
        _run_script(
            orch / "deploy/vps/healthcheck.sh",
            env=env,
            args=[],
            cwd=Path("/tmp"),
        )
        log = Path(f"{marker}.log").read_text(encoding="utf-8")
        assert "cwd=" in log
        assert "orchestrator" in log
