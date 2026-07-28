"""R4D active-test harness: runtime-neutral helpers, cleanup traps, no secrets leakage."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from flow_engine.script_sandbox.attestation import (
    PRODUCTION_ATTESTATION_SOURCES,
    SOURCE_CONTAINER_INSPECT,
    SOURCE_DOCKER_INSPECT,
    SOURCE_TESTING_FIXTURE,
    build_attestation_document,
    verify_attestation,
)
from flow_engine.script_sandbox.pins import orch_script_source_digest
from flow_engine.script_sandbox.spool import (
    CANCELS_DIR,
    CLAIMED_DIR,
    PENDING_JOBS_DIR,
    QUARANTINE_DIR,
    RESULTS_DIR,
    SEEN_DIR,
    TMP_DIR,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def test_container_runtime_helper_exists_and_is_runtime_neutral() -> None:
    helper = (SCRIPTS / "lib" / "container_runtime.sh").read_text(encoding="utf-8")
    assert "orch_detect_container_runtime" in helper
    assert "podman" in helper and "docker" in helper
    assert "orch_compose" in helper
    assert "container_inspect" in helper
    assert "never invents" in helper.lower() or "Does not invent" in helper or "not invent" in helper.lower()
    assert "sha256:0000" not in helper


def test_active_test_script_has_fail_safe_cleanup_trap() -> None:
    text = (SCRIPTS / "r4d_active_test.sh").read_text(encoding="utf-8")
    assert "trap cleanup EXIT" in text or "trap cleanup EXIT INT TERM" in text
    assert "down -v" in text
    assert "ORCH_R4D_KEEP" in text
    assert "r4d_generate_ephemeral_env.sh" in text
    assert "build_script_runner_attestation.sh" in text
    assert "r4d_exercise.py" in text
    assert "PRAGMA integrity_check" in text or "integrity_check" in text
    # No real provider credential hooks.
    assert "OPENAI_API_KEY" not in text
    assert "ANTHROPIC_API_KEY" not in text
    assert "paid" not in text.lower() or "never" in text.lower()


def test_ephemeral_env_generator_writes_ignored_temp_file(tmp_path, monkeypatch) -> None:
    run_dir = tmp_path / "r4d-run"
    env_file = run_dir / "env"
    monkeypatch.setenv("ORCH_R4D_RUN_ID", "test-run")
    monkeypatch.setenv("ORCH_R4D_RUN_DIR", str(run_dir))
    monkeypatch.setenv("ORCH_R4D_ENV_FILE", str(env_file))
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "r4d_generate_ephemeral_env.sh")],
        cwd=str(ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "ORCH_R4D_ENV_FILE=" in proc.stdout
    assert env_file.is_file()
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode & 0o077 == 0, f"env file must not be group/world readable: {oct(mode)}"
    text = env_file.read_text(encoding="utf-8")
    for key in (
        "REDIS_PASSWORD=",
        "DJANGO_SECRET_KEY=",
        "ORCH_API_SERVICE_TOKEN=",
        "ORCH_WORKER_SERVICE_TOKEN=",
        "ORCH_TOKEN_FOUNDER=",
        "ORCH_ATTESTATION_HMAC_KEY=",
        "ORCH_SCRIPT_SPOOL_HMAC_KEY=",
        "ORCH_PROVIDER_MODE=mock",
    ):
        assert key in text
    # Generator stdout must not echo secret values.
    for line in text.splitlines():
        if line.startswith("REDIS_PASSWORD="):
            secret = line.split("=", 1)[1]
            assert secret
            assert secret not in proc.stdout


def test_build_attestation_script_is_runtime_neutral() -> None:
    text = (SCRIPTS / "build_script_runner_attestation.sh").read_text(encoding="utf-8")
    assert "container_runtime.sh" in text
    assert "orch_image_build" in text or "ORCH_CONTAINER_RUNTIME" in text
    assert "fails closed" in text.lower() or "fail" in text.lower()
    assert "SOURCE_CONTAINER_INSPECT" in text or "container_inspect" in text


def test_attestation_bootstrap_import_does_not_require_existing_attestation(
    tmp_path,
) -> None:
    """The signer must load before the fail-closed runtime document exists."""
    missing = tmp_path / "not-created-yet.json"
    env = {
        **os.environ,
        "ORCH_SCRIPT_RUNNER_ATTESTATION_FILE": str(missing),
    }
    env.pop("ORCH_TESTING", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from flow_engine.script_sandbox.attestation "
                "import build_attestation_document; "
                "assert callable(build_attestation_document)"
            ),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert not missing.exists()


def test_runtime_allowlist_still_fails_closed_without_attestation(tmp_path) -> None:
    missing = tmp_path / "missing-runtime-attestation.json"
    env = {
        **os.environ,
        "ORCH_SCRIPT_RUNNER_ATTESTATION_FILE": str(missing),
    }
    env.pop("ORCH_TESTING", None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from flow_engine.script_sandbox import SCRIPT_RUNNER_IMAGE_DIGEST",
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0
    assert "attestation missing" in proc.stderr


def test_container_inspect_attestation_source_accepted(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ATTESTATION_HMAC_KEY", "r4d-test-attestation-key")
    monkeypatch.delenv("ORCH_TESTING", raising=False)
    digest = "sha256:" + ("ab" * 32)
    doc = build_attestation_document(
        image_digest=digest,
        image_id=digest,
        executable_digest=orch_script_source_digest(),
        built_at="2026-07-26T00:00:00Z",
        source=SOURCE_CONTAINER_INSPECT,
    )
    verified = verify_attestation(doc)
    assert verified["source"] == SOURCE_CONTAINER_INSPECT
    assert SOURCE_CONTAINER_INSPECT in PRODUCTION_ATTESTATION_SOURCES
    assert SOURCE_DOCKER_INSPECT in PRODUCTION_ATTESTATION_SOURCES
    assert SOURCE_TESTING_FIXTURE not in PRODUCTION_ATTESTATION_SOURCES


def test_docker_inspect_attestation_still_accepted(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_ATTESTATION_HMAC_KEY", "r4d-test-attestation-key")
    monkeypatch.delenv("ORCH_TESTING", raising=False)
    digest = "sha256:" + ("cd" * 32)
    doc = build_attestation_document(
        image_digest=digest,
        image_id=digest,
        executable_digest=orch_script_source_digest(),
        built_at="2026-07-26T00:00:00Z",
        source=SOURCE_DOCKER_INSPECT,
    )
    assert verify_attestation(doc)["source"] == SOURCE_DOCKER_INSPECT


def test_compose_stack_lists_r4d_required_services() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    for svc in (
        "api:",
        "coordinator:",
        "redis:",
        "worker:",
        "script-worker:",
        "script-runner:",
        "scheduler:",
        "mcp-context-assets:",
        "mcp-workflow-control:",
        "mcp-delegation-coordination:",
        "mcp-evidence-governance:",
        "mcp-maintenance:",
    ):
        assert svc in compose
    assert "network_mode:" in compose
    assert "9001:9001" not in compose


def test_compose_external_images_use_fully_qualified_registry_names() -> None:
    """Non-interactive rootless Podman cannot prompt to resolve short names."""
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    image_refs = re.findall(r"^\s+image:\s*([^\s#]+)", compose, flags=re.MULTILINE)
    assert image_refs, "expected at least one external Compose image"
    assert image_refs == [
        "docker.io/library/redis:7-alpine",
        "localhost/orchestrator-script-spool-init:local",
    ]
    assert all(
        ref.startswith(("docker.io/", "quay.io/", "ghcr.io/", "localhost/"))
        for ref in image_refs
    )


def test_compose_rootless_mounts_preserve_security_boundaries() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    redis_block = compose[compose.index("  redis:") : compose.index("  coordinator:")]
    runner_block = compose[
        compose.index("  script-runner:") : compose.index("  # R4C: Celery Beat")
    ]

    # Redis bypasses its root/chown entrypoint path while retaining an ephemeral
    # writable broker data tmpfs under the existing fail-closed hardening.
    assert 'user: "999:1000"' in redis_block
    assert "/data:size=64M,mode=1777" in redis_block
    assert "cap_drop:" in compose and "- ALL" in compose

    # SELinux relabeling is scoped to the signed, non-secret attestation file.
    # The bind remains read-only and the runner remains non-root/networkless.
    attestation_mount = (
        ":/etc/orch/script-runner.attestation.json:ro,z"
    )
    assert compose.count(attestation_mount) == 3
    assert 'network_mode: "none"' in runner_block
    assert "user: root" not in runner_block
    assert "script-spool:/var/orch/spool" in runner_block
    assert "/etc/orch/script-runner.attestation.json:rw" not in compose

    # Durable coordinator state and cross-boundary spool remain named volumes;
    # no broad host directory is made writable.
    assert "orchestrator-data:/data" in compose
    assert "script-spool:/var/orch/spool" in compose


def test_spool_volume_has_bounded_one_shot_ownership_initializer() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    init_block = compose[
        compose.index("  script-spool-init:")
        : compose.index("  # R4C: networked script-worker")
    ]
    worker_block = compose[
        compose.index("  script-worker:") : compose.index("  # R4C: networkless")
    ]
    runner_block = compose[
        compose.index("  script-runner:") : compose.index("  # R4C: Celery Beat")
    ]

    assert 'user: "0:0"' in init_block
    assert "image: localhost/orchestrator-script-spool-init:local" in init_block
    assert 'network_mode: "none"' in init_block
    assert "cap_add:\n      - CHOWN\n      # Required" in init_block
    assert "- DAC_OVERRIDE" in init_block
    assert "cap_drop:" not in init_block  # inherited through the hardening anchor
    assert "chown -R 0:0 /var/orch/spool" in init_block
    assert "chown -R 10001:10001 /var/orch/spool" in init_block
    assert "chmod 0750" in init_block
    assert init_block.index("chown -R 0:0") < init_block.index("mkdir -p")
    assert init_block.index("mkdir -p") < init_block.index("chmod 0750")
    assert init_block.index("chmod 0750") < init_block.index("chown -R 10001:10001")
    assert "chmod 0777" not in init_block
    assert 'restart: "no"' in init_block
    assert "script-spool:/var/orch/spool" in init_block
    canonical_dirs = {
        PENDING_JOBS_DIR,
        RESULTS_DIR,
        CANCELS_DIR,
        SEEN_DIR,
        TMP_DIR,
        CLAIMED_DIR,
        QUARANTINE_DIR,
    }
    for name in canonical_dirs:
        assert f"/var/orch/spool/{name}" in init_block
    assert "/var/orch/spool/control" not in init_block
    assert "/var/orch/spool/claims" not in init_block

    dependency = (
        "script-spool-init:\n        condition: service_completed_successfully"
    )
    assert dependency in worker_block
    assert dependency in runner_block
    assert 'user: "0:0"' not in runner_block
    assert "cap_add:" not in runner_block
    assert 'network_mode: "none"' in runner_block
    assert "DAC_OVERRIDE" not in worker_block
    assert "DAC_OVERRIDE" not in runner_block


def test_dockerfile_copies_r4d_scripts() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY scripts ./scripts" in text
    scripts = (SCRIPTS / "r4d_state_snapshot.py").read_text(encoding="utf-8")
    assert "restart_continuity" in scripts
    assert "redelivery_count" in scripts


def test_active_test_evidence_remediation_markers() -> None:
    harness = (SCRIPTS / "r4d_active_test.sh").read_text(encoding="utf-8")
    exercise = (SCRIPTS / "r4d_exercise.py").read_text(encoding="utf-8")
    assert "chmod 600" in harness and "compose-config.yml" in harness
    assert "teardown-zero-state.json" in harness
    assert "redelivery-start" in harness
    assert "redelivery-finalize" in harness
    assert "restart-pre" in harness and "restart-post" in harness
    assert "08_redelivery_at_loss" in exercise
    assert "unacknowledged_at_loss" in exercise
    assert "exactly_one_terminal_effect" in exercise
    assert "state_identity_preserved" in exercise
    assert "result_continuity" in exercise
    assert (SCRIPTS / "r4d_compose.sh").is_file()
    assert (SCRIPTS / "r4d_compose_exec.sh").is_file()


def test_local_processes_are_bounded_and_read_only_safe() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert '"provider-mock", "--concurrency", "1"' in text
    assert '"--schedule", "/tmp/celerybeat-schedule"' in text
    assert '"--worker-tmp-dir", "/tmp", "--no-control-socket"' in text


def test_runner_seccomp_allows_required_session_isolation_and_safe_errors(
    capsys,
) -> None:
    profile = (ROOT / "deploy/seccomp/script-worker.json").read_text(encoding="utf-8")
    assert '"setsid"' in profile
    assert '"renameat2"' in profile
    assert '"mkdir"' in profile
    assert '"rmdir"' in profile

    from flow_engine.script_sandbox.runner_service import _log_runner_error

    secret = "sensitive-job-payload-must-not-appear"
    _log_runner_error(OSError(1, secret))
    captured = capsys.readouterr()
    event = json.loads(captured.err)
    assert event == {
        "event": "script_runner_job_error",
        "error_class": "PermissionError",
        "errno": 1,
    }
    assert secret not in captured.err
    runner = (ROOT / "src/flow_engine/script_sandbox/runner.py").read_text(
        encoding="utf-8"
    )
    assert 'env[key] = "/usr/local/bin:/usr/bin:/bin"' in runner


def test_podman_health_kick_preserves_compose_health_gates() -> None:
    text = (SCRIPTS / "r4d_active_test.sh").read_text(encoding="utf-8")
    assert '[[ "${RUNTIME}" == "podman" ]]' in text
    assert "podman healthcheck run" in text
    assert 'label=io.podman.compose.project=${PROJECT}' in text
    assert "{{.State.Health.Status}}" in text
    assert '"${health}" == "starting"' in text
    assert '"${health}" == "unhealthy"' in text
    assert '${PROJECT}_script-spool-init_1' in text
    assert '"${exit_code}" == "0"' in text
    assert "COMPOSE_UP_PID" in text
    assert "kill -0" in text
    assert "wait " in text
    assert "deadline=" in text
    assert "health-kick" in text
    # Docker retains its ordinary foreground Compose semantics.
    assert "compose_r4d up -d --build" in text
    # Never mutate health status or turn a failed check into healthy.
    assert "healthcheck run" in text
    assert "healthcheck update" not in text


def test_compose_log_capture_and_worker_healthcheck_are_runtime_safe() -> None:
    harness = (SCRIPTS / "r4d_active_test.sh").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    worker_block = compose[compose.index("  worker:") : compose.index("  # One-shot")]

    assert "compose_r4d --no-ansi logs" in harness
    assert "compose_r4d logs --no-color" in harness
    assert worker_block.count("test:") == 1
    assert 'test: ["CMD-SHELL"' in worker_block
    assert 'celery@$(hostname)' in worker_block
    assert 'celery@$${HOSTNAME}' not in worker_block
    assert '["CMD", "celery"' not in worker_block


def test_gitignore_covers_r4d_temp_and_attestation_artifacts() -> None:
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".tmp/" in gi
    assert ".env" in gi
    # Production attestation must not be force-committed via tracked path alone.
    assert "artifacts/" in gi or "attestations" in gi or ".tmp/" in gi


def test_r4d_exercise_redacts_secret_keys() -> None:
    # Import without requiring Django by loading module text markers.
    text = (SCRIPTS / "r4d_exercise.py").read_text(encoding="utf-8")
    assert "[REDACTED]" in text
    assert "twelve" in text.lower() or "all_twelve_loadout" in text
    assert "cross" in text.lower()
    assert "schedule.manila" in text or "list_schedule_templates" in text
    assert re.search(r"workspace_root|override_argv|inject_env", text)
    assert "redelivery-start" in text
    assert "restart-pre" in text


@pytest.mark.skipif(
    os.environ.get("ORCH_R4D_RUN_SHELL_HELPERS") != "1",
    reason="optional live runtime detection; enable with ORCH_R4D_RUN_SHELL_HELPERS=1",
)
def test_live_runtime_detection_smoke() -> None:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f"source '{SCRIPTS}/lib/container_runtime.sh' && orch_detect_container_runtime",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() in {"podman", "docker"}
