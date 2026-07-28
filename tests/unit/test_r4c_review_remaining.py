"""R4C remaining-review adversarial coverage: attestation, spool, schemas."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

os.environ["ORCH_TESTING"] = "1"
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-insecure-secret")
os.environ.setdefault("DJANGO_ALLOWED_HOSTS", "testserver,localhost")
os.environ.setdefault("DJANGO_DEBUG", "0")

from flow_engine.domain.errors import (
    AuthzDeniedError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.script_sandbox.allowlist import (
    ORCH_SCRIPT_EXECUTABLE_DIGEST,
    SCRIPT_RUNNER_IMAGE_DIGEST,
    get_allowlist_entry,
)
from flow_engine.script_sandbox.attestation import (
    SOURCE_DOCKER_INSPECT,
    SOURCE_TESTING_FIXTURE,
    TESTING_IMAGE_DIGEST,
    build_attestation_document,
    ensure_testing_attestation_file,
    load_verified_attestation,
    require_authorized_image_digest,
    verify_attestation,
    write_attestation,
)
from flow_engine.script_sandbox.controller import execute_script_job, process_runner_job
from flow_engine.script_sandbox.results_schema import (
    redact_failure_output,
    validate_and_redact_script_results,
)
from flow_engine.script_sandbox.runner import (
    ScriptRunRequest,
    ScriptRunResult,
    _InternalTestHooks,
    set_testing_hooks,
)
from flow_engine.script_sandbox.runner_service import assert_runner_role
from flow_engine.script_sandbox.spool import (
    QUARANTINE_DIR,
    _atomic_move_noreplace,
    build_job_envelope,
    build_result_envelope,
    claim_job,
    confine_spool_path,
    ensure_spool_layout,
    is_cancel_published,
    list_pending_jobs,
    publish_cancel_for_job,
    read_result,
    sign_envelope,
    validate_job_envelope,
    write_job,
    write_result,
)


@pytest.fixture(autouse=True)
def _testing_attestation(tmp_path, monkeypatch):
    os.environ["ORCH_TESTING"] = "1"
    path = ensure_testing_attestation_file()
    monkeypatch.setenv("ORCH_SCRIPT_RUNNER_ATTESTATION_FILE", str(path))
    yield
    set_testing_hooks(None)


def test_testing_attestation_is_not_self_referential_pin_manifest() -> None:
    doc = load_verified_attestation()
    assert doc["source"] == SOURCE_TESTING_FIXTURE
    assert doc["image_digest"] == TESTING_IMAGE_DIGEST
    assert doc["image_digest"] == SCRIPT_RUNNER_IMAGE_DIGEST
    pins = Path(__file__).resolve().parents[2] / "src/flow_engine/script_sandbox/pins.py"
    assert "pinned_script_worker_image_digest" not in pins.read_text(encoding="utf-8")


def test_missing_attestation_fails_closed_outside_testing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv(
        "ORCH_SCRIPT_RUNNER_ATTESTATION_FILE", str(tmp_path / "missing.json")
    )
    monkeypatch.delenv("ORCH_SCRIPT_IMAGE_DIGEST", raising=False)
    with pytest.raises(AuthzDeniedError):
        require_authorized_image_digest(TESTING_IMAGE_DIGEST)
    monkeypatch.setenv("ORCH_TESTING", "1")


def test_wrong_actual_image_attestation_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv("ORCH_ATTESTATION_HMAC_KEY", "prod-attestation-key")
    wrong = build_attestation_document(
        image_digest="sha256:" + ("11" * 32),
        image_id="sha256:" + ("22" * 32),
        executable_digest=ORCH_SCRIPT_EXECUTABLE_DIGEST,
        built_at="2026-07-26T00:00:00Z",
        source=SOURCE_DOCKER_INSPECT,
    )
    path = tmp_path / "att.json"
    write_attestation(path, wrong)
    monkeypatch.setenv("ORCH_SCRIPT_RUNNER_ATTESTATION_FILE", str(path))
    with pytest.raises(AuthzDeniedError):
        require_authorized_image_digest(TESTING_IMAGE_DIGEST)
    # Matching the attestation succeeds.
    assert require_authorized_image_digest(wrong["image_digest"]) == wrong["image_digest"]
    monkeypatch.setenv("ORCH_TESTING", "1")


def test_testing_attestation_rejected_outside_testing(tmp_path, monkeypatch) -> None:
    path = ensure_testing_attestation_file()
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv("ORCH_SCRIPT_RUNNER_ATTESTATION_FILE", str(path))
    with pytest.raises(AuthzDeniedError):
        verify_attestation(json.loads(path.read_text(encoding="utf-8")))
    monkeypatch.setenv("ORCH_TESTING", "1")


def test_env_digest_alone_insufficient_outside_testing(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv("ORCH_SCRIPT_IMAGE_DIGEST", TESTING_IMAGE_DIGEST)
    monkeypatch.setenv(
        "ORCH_SCRIPT_RUNNER_ATTESTATION_FILE", "/no/such/attestation.json"
    )
    with pytest.raises(AuthzDeniedError, match="attestation"):
        require_authorized_image_digest(TESTING_IMAGE_DIGEST)
    monkeypatch.setenv("ORCH_TESTING", "1")


def test_forged_spool_job_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None
    env = build_job_envelope(
        job_id="job-1",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={"dry_run": True},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    forged = dict(env)
    forged["script_id"] = "script.generic.secret_pattern_scan"
    # mac not recomputed
    with pytest.raises(AuthzDeniedError, match="mac"):
        validate_job_envelope(forged)


def test_forged_spool_result_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None
    good = build_result_envelope(
        job_id="job-2",
        job_nonce="abc123",
        result={
            "script_id": entry.script_id,
            "status": "complete",
            "argv": list(entry.argv),
            "executable_digest": entry.executable_digest,
            "image_digest": entry.image_digest,
            "output": {},
            "redacted_output": "",
            "error_code": None,
            "error": None,
            "bounded": True,
            "network_attempted": False,
            "hardening": {},
            "pgid": None,
        },
    )
    forged = dict(good)
    forged["result"] = {**forged["result"], "status": "failed"}
    path = write_result(forged)
    with pytest.raises(AuthzDeniedError):
        read_result("job-2", expected_nonce="abc123", wait_timeout_sec=0.2)
    path.unlink(missing_ok=True)


def test_spool_traversal_and_symlink_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    with pytest.raises(AuthzDeniedError):
        confine_spool_path(root, "jobs", "../etc/passwd")
    with pytest.raises(AuthzDeniedError):
        confine_spool_path(root, "..", "escape")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret"
    target.write_text("nope", encoding="utf-8")
    link = root / "jobs" / "evil.link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink not permitted in this environment")
    with pytest.raises(AuthzDeniedError):
        confine_spool_path(root, "jobs", "evil.link")


def test_stale_and_replay_spool_job_denied(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.git_diff_summary")
    assert entry is not None
    env = build_job_envelope(
        job_id="job-stale",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
        ttl_sec=1,
    )
    env["expires_at"] = int(env["issued_at"]) - 10
    env = sign_envelope(env)
    with pytest.raises(AuthzDeniedError, match="stale"):
        validate_job_envelope(env)

    fresh = build_job_envelope(
        job_id="job-replay",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(fresh)
    claimed = claim_job(path)
    replay = sign_envelope({**claimed, "job_id": "job-replay-again"})
    replay_path = write_job(replay)
    with pytest.raises(AuthzDeniedError, match="replay"):
        claim_job(replay_path)


def test_spool_roundtrip_worker_to_runner(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.catalog_integrity_sweep")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-roundtrip",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={"scope": "agentic", "dry_run": True},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(job)
    set_testing_hooks(_InternalTestHooks(workspace_root=str(tmp_path / "ws")))
    claimed = claim_job(path)
    process_runner_job(claimed)
    result_env = read_result(
        "job-roundtrip", expected_nonce=str(job["nonce"]), wait_timeout_sec=1
    )
    assert result_env["result"]["status"] == "complete"
    set_testing_hooks(None)


def test_script_runner_rejects_backend_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ORCH_TESTING", "0")
    monkeypatch.setenv("ORCH_SCRIPT_ROLE", "script-runner")
    monkeypatch.setenv("COORDINATOR_URL", "http://coordinator:9001")
    with pytest.raises(AuthzDeniedError, match="backend credentials"):
        assert_runner_role()
    monkeypatch.delenv("COORDINATOR_URL")
    assert_runner_role()
    monkeypatch.setenv("ORCH_TESTING", "1")


def test_compose_networkless_runner_markers() -> None:
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "script-runner:" in compose
    assert "network_mode:" in compose
    assert "none" in compose
    # Runner service block must not list CELERY_BROKER_URL / COORDINATOR_URL.
    runner_idx = compose.index("script-runner:")
    next_svc = compose.find("\n  scheduler:", runner_idx)
    block = compose[runner_idx:next_svc]
    assert "COORDINATOR_URL" not in block
    assert "CELERY_BROKER_URL" not in block
    assert "REDIS_PASSWORD" not in block
    assert "FLOW_DB_PATH" not in block
    assert "ORCH_WORKER_SERVICE_TOKEN" not in block
    assert "network_mode" in block


def test_nested_secret_and_unbounded_result_bodies_rejected() -> None:
    with pytest.raises(ValidationFailedError):
        validate_and_redact_script_results(
            [
                {
                    "script_id": "script.generic.repository_health",
                    "status": "complete",
                    "summary": "x" * 10000,
                }
            ]
        )
    with pytest.raises(UnsupportedSurfaceError):
        validate_and_redact_script_results(
            [
                {
                    "script_id": "script.generic.repository_health",
                    "status": "complete",
                    "findings": [
                        {
                            "summary": "ok",
                            "severity": "low",
                            "remediation": True,
                        }
                    ],
                }
            ]
        )
    cleaned = validate_and_redact_script_results(
        [
            {
                "script_id": "script.generic.repository_health",
                "status": "complete",
                "evidence": [
                    {
                        "summary": "api_key=supersecret nested",
                        "uri": "orch://e/1",
                    }
                ],
                "findings": [
                    {
                        "summary": "token=abcd",
                        "severity": "low",
                    }
                ],
                "anomalies": [{"summary": "transient", "class": "A5"}],
                "follow_ups": [{"summary": "review drift"}],
            }
        ]
    )
    assert "[REDACTED]" in cleaned[0]["evidence"][0]["summary"]
    assert "[REDACTED]" in cleaned[0]["findings"][0]["summary"]


def test_forbidden_effect_vocabulary_rejected() -> None:
    with pytest.raises(UnsupportedSurfaceError):
        validate_and_redact_script_results(
            [
                {
                    "script_id": "script.generic.repository_health",
                    "status": "complete",
                    "summary": "please apply remediation now",
                }
            ]
        )
    with pytest.raises(UnsupportedSurfaceError):
        validate_and_redact_script_results(
            [
                {
                    "script_id": "script.generic.repository_health",
                    "status": "complete",
                    "effects": [{"type": "provider_call", "summary": "nope"}],
                }
            ]
        )


def test_failure_output_redacted_before_use() -> None:
    out = redact_failure_output(
        "stdout token=supersecret",
        "stderr password=hunter2",
    )
    assert "supersecret" not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_execute_script_job_local_testing_path(tmp_path) -> None:
    set_testing_hooks(_InternalTestHooks(workspace_root=str(tmp_path / "ws")))
    result = execute_script_job(
        ScriptRunRequest(script_id="script.generic.repository_health")
    )
    assert result.status == "complete"
    set_testing_hooks(None)


def test_digest_scratch_artifacts_removed() -> None:
    root = Path(__file__).resolve().parents[2]
    scratch = [
        root / "deploy/pins/_digests.ipynb",
        root / "deploy/pins/_run_digests.py",
        root / "deploy/pins/_trigger_pins",
        root / "deploy/pins/_orch_script_cli_copy.py",
        root / "deploy/pins/_compute_attestation_out.py",
        root / ".cursor_run_checks.sh",
        root / ".verify_r4a.sh",
        root / "bash_env.sh",
    ]
    for path in scratch:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "REMOVED" in text
        assert "pinned_script_worker_image_digest" not in text


class _NoDuplicateKeyLoader(yaml.SafeLoader):
    pass


def _no_duplicates(loader, node, deep=False):
    explicit_keys = set()
    for key_node, _value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in explicit_keys:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key: {key!r}",
                key_node.start_mark,
            )
        explicit_keys.add(key)
    loader.flatten_mapping(node)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_NoDuplicateKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates
)


def test_compose_still_no_duplicate_keys() -> None:
    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    yaml.load(compose_path.read_text(encoding="utf-8"), Loader=_NoDuplicateKeyLoader)


def test_spool_claim_rejects_symlink_and_filename_mismatch(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-real",
        execution_id="exec-1",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    real_path = write_job(job)

    # Symlink alias must not be discoverable or claimable.
    link = root / "jobs" / "job-alias.job.json"
    try:
        link.symlink_to(real_path)
    except OSError:
        pytest.skip("symlink not permitted in this environment")
    pending = list_pending_jobs()
    assert real_path in pending
    assert all(p.name != "job-alias.job.json" for p in pending)
    with pytest.raises(AuthzDeniedError, match="symlink|regular file|canonical|pending"):
        claim_job(link)

    # Filename job_id must equal signed envelope job_id.
    other = build_job_envelope(
        job_id="job-other",
        execution_id="exec-1",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    wrong_name = root / "jobs" / "job-wrongname.job.json"
    wrong_name.write_text(json.dumps(other), encoding="utf-8")
    with pytest.raises(AuthzDeniedError, match="filename job_id"):
        claim_job(wrong_name)
    # Nonce must remain unconsumed after rejected mismatch claim.
    assert not (root / "seen").joinpath(
        f"job-nonce.{other['nonce']}.seen"
    ).exists()
    # Invalid claimed file must be quarantined with recoverable audit state.
    assert not wrong_name.exists()
    quarantine = root / QUARANTINE_DIR
    bad = list(quarantine.glob("job-wrongname.job.json.*.bad"))
    audits = list(quarantine.glob("job-wrongname.job.json.*.audit.json"))
    assert len(bad) == 1
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["recovered"] is False
    assert "filename job_id" in audit["reason"]
    assert audit["quarantine_path"] == str(bad[0])


def test_spool_nonce_not_poisoned_by_alias(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.git_diff_summary")
    assert entry is not None
    victim = build_job_envelope(
        job_id="job-victim",
        execution_id="exec-v",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    victim_path = write_job(victim)

    # Symlink alias must not consume the victim nonce.
    link = root / "jobs" / "job-alias-dos.job.json"
    try:
        link.symlink_to(victim_path)
    except OSError:
        pytest.skip("symlink not permitted in this environment")
    with pytest.raises(AuthzDeniedError):
        claim_job(link)
    assert not (root / "seen").joinpath(
        f"job-nonce.{victim['nonce']}.seen"
    ).exists()

    # Filename mismatch (copy of envelope under wrong name) must not consume nonce.
    wrong = root / "jobs" / "job-wrong-dos.job.json"
    wrong.write_text(json.dumps(victim), encoding="utf-8")
    with pytest.raises(AuthzDeniedError, match="filename job_id"):
        claim_job(wrong)
    assert not (root / "seen").joinpath(
        f"job-nonce.{victim['nonce']}.seen"
    ).exists()

    # Canonical victim remains claimable after failed alias/DoS attempts.
    claimed = claim_job(victim_path)
    assert claimed["nonce"] == victim["nonce"]
    assert claimed["job_id"] == "job-victim"


def test_durable_cancel_across_spool_terminates_process(tmp_path, monkeypatch) -> None:
    """Long-running fixture via spool: durable cancel → pg kill → cancelled result."""
    import signal
    import subprocess
    import threading
    import time

    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_WAIT_SEC", "15")
    ensure_spool_layout()

    proc_box: dict[str, subprocess.Popen[Any]] = {}
    started = threading.Event()

    def long_running_fixture(request, entry, env, cwd):
        proc = subprocess.Popen(  # noqa: S603
            ["sleep", "60"],
            start_new_session=True,
        )
        proc_box["proc"] = proc
        started.set()
        while True:
            if request.cancel_check is not None and request.cancel_check():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass
                return ScriptRunResult(
                    script_id=entry.script_id,
                    status="cancelled",
                    argv=entry.argv,
                    executable_digest=entry.executable_digest,
                    image_digest=entry.image_digest,
                    error_code="VALIDATION_FAILED",
                    error="cancelled",
                    hardening=dict(entry.hardening),
                    pgid=proc.pid,
                )
            if proc.poll() is not None:
                return ScriptRunResult(
                    script_id=entry.script_id,
                    status="failed",
                    argv=entry.argv,
                    executable_digest=entry.executable_digest,
                    image_digest=entry.image_digest,
                    error_code="VALIDATION_FAILED",
                    error=f"exit {proc.returncode}",
                    hardening=dict(entry.hardening),
                    pgid=proc.pid,
                )
            time.sleep(0.05)

    set_testing_hooks(
        _InternalTestHooks(
            workspace_root=str(tmp_path / "ws"),
            stub_executor=long_running_fixture,
        )
    )

    stop = threading.Event()

    def runner_loop() -> None:
        from flow_engine.script_sandbox.runner_service import run_once

        while not stop.is_set():
            try:
                run_once()
            except Exception:
                pass
            time.sleep(0.05)

    runner = threading.Thread(target=runner_loop, daemon=True)
    runner.start()

    cancel_flag = {"v": False}

    def cancel_check() -> bool:
        return bool(cancel_flag["v"])

    result_box: dict[str, Any] = {}
    error_box: dict[str, BaseException] = {}

    def controller_side() -> None:
        try:
            result_box["result"] = execute_script_job(
                ScriptRunRequest(
                    script_id="script.generic.repository_health",
                    execution_id="exec-cancel-1",
                    cancel_check=cancel_check,
                )
            )
        except BaseException as exc:  # noqa: BLE001 — surface to main thread
            error_box["err"] = exc

    ctrl = threading.Thread(target=controller_side, daemon=True)
    ctrl.start()

    assert started.wait(timeout=10), "long-running fixture did not start"
    proc = proc_box["proc"]
    assert proc.poll() is None, "process should still be running before cancel"
    cancel_flag["v"] = True
    ctrl.join(timeout=15)
    stop.set()
    runner.join(timeout=2)

    if "err" in error_box:
        raise error_box["err"]
    result = result_box["result"]
    assert result.status == "cancelled"
    assert result.pgid == proc.pid
    deadline = time.time() + 5
    while proc.poll() is None and time.time() < deadline:
        time.sleep(0.05)
    assert proc.poll() is not None, "process was not terminated after durable cancel"
    cancels = list((tmp_path / "spool" / "cancels").glob("*.cancel.json"))
    assert cancels, "authenticated cancel envelope must be published on spool"
    set_testing_hooks(None)


def test_publish_cancel_bound_to_job_execution_nonce(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.catalog_integrity_sweep")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-c1",
        execution_id="exec-c1",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    publish_cancel_for_job(
        job_id="job-c1",
        execution_id="exec-c1",
        job_nonce=str(job["nonce"]),
    )
    assert is_cancel_published(
        job_id="job-c1",
        job_nonce=str(job["nonce"]),
        execution_id="exec-c1",
    )
    assert not is_cancel_published(
        job_id="job-c1",
        job_nonce="wrong-nonce",
        execution_id="exec-c1",
    )
    assert not is_cancel_published(
        job_id="job-c1",
        job_nonce=str(job["nonce"]),
        execution_id="exec-other",
    )


def test_claim_rename_failure_preserves_pending_and_nonce(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-rename-fail",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(job)

    def boom(src, dst):
        raise OSError(errno.EIO, "simulated rename failure")

    monkeypatch.setattr(
        "flow_engine.script_sandbox.spool._atomic_move_noreplace", boom
    )
    with pytest.raises(AuthzDeniedError, match="atomic claim failed"):
        claim_job(path)
    assert path.exists()
    assert path.read_text(encoding="utf-8")
    assert not (root / "seen").joinpath(f"job-nonce.{job['nonce']}.seen").exists()
    assert list((root / QUARANTINE_DIR).glob("*")) == []


def test_concurrent_claims_only_one_wins(tmp_path, monkeypatch) -> None:
    import threading

    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.git_diff_summary")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-concurrent",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(job)
    results: list[object] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait(timeout=5)
        try:
            results.append(claim_job(path))
        except Exception as exc:  # noqa: BLE001 — collect winner/loser outcomes
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    wins = [r for r in results if isinstance(r, dict)]
    losses = [r for r in results if isinstance(r, AuthzDeniedError)]
    assert len(results) == 2
    assert len(wins) == 1
    assert len(losses) == 1
    assert wins[0]["nonce"] == job["nonce"]
    assert not path.exists()


def test_claim_inode_symlink_swap_rejected_without_nonce_consume(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.catalog_integrity_sweep")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-swap",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(job)
    real_move = _atomic_move_noreplace
    swapped = {"done": False}

    def swap_to_symlink_then_move(src, dst):
        if not swapped["done"]:
            swapped["done"] = True
            os.unlink(src)
            try:
                os.symlink("/tmp/spool-swap-target", src)
            except OSError:
                pytest.skip("symlink not permitted in this environment")
        return real_move(src, dst)

    monkeypatch.setattr(
        "flow_engine.script_sandbox.spool._atomic_move_noreplace",
        swap_to_symlink_then_move,
    )
    with pytest.raises(AuthzDeniedError, match="symlink|regular file|nofollow|claimed"):
        claim_job(path)
    assert not (root / "seen").joinpath(f"job-nonce.{job['nonce']}.seen").exists()
    # Symlink leaf was claimed then quarantined (pending gone).
    assert not path.exists()
    assert list((root / QUARANTINE_DIR).glob("job-swap.job.json.*.bad"))
    assert list((root / QUARANTINE_DIR).glob("job-swap.job.json.*.audit.json"))


def test_claim_inode_content_swap_validates_moved_inode(
    tmp_path, monkeypatch
) -> None:
    """Swap pending content before move: claim binds whatever inode was moved."""
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None
    original = build_job_envelope(
        job_id="job-content-swap",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={"mark": "original"},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(original)
    replacement = build_job_envelope(
        job_id="job-content-swap",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={"mark": "replacement"},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    real_move = _atomic_move_noreplace
    swapped = {"done": False}

    def swap_content_then_move(src, dst):
        if not swapped["done"]:
            swapped["done"] = True
            os.unlink(src)
            Path(src).write_text(json.dumps(replacement), encoding="utf-8")
        return real_move(src, dst)

    monkeypatch.setattr(
        "flow_engine.script_sandbox.spool._atomic_move_noreplace",
        swap_content_then_move,
    )
    claimed = claim_job(path)
    assert claimed["nonce"] == replacement["nonce"]
    assert claimed["input_json"]["mark"] == "replacement"
    # Original nonce was never observed post-move; must remain unconsumed.
    assert not (root / "seen").joinpath(
        f"job-nonce.{original['nonce']}.seen"
    ).exists()
    assert (root / "seen").joinpath(
        f"job-nonce.{replacement['nonce']}.seen"
    ).exists()


def test_invalid_claim_quarantined_with_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.secret_pattern_scan")
    assert entry is not None
    job = build_job_envelope(
        job_id="job-invalid",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(job)
    forged = dict(job)
    forged["script_id"] = "script.generic.repository_health"
    path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(AuthzDeniedError, match="mac|forged|tampered"):
        claim_job(path)
    assert not path.exists()
    assert not (root / "seen").joinpath(f"job-nonce.{job['nonce']}.seen").exists()
    quarantine = root / QUARANTINE_DIR
    bad = list(quarantine.glob("job-invalid.job.json.*.bad"))
    audits = list(quarantine.glob("job-invalid.job.json.*.audit.json"))
    assert len(bad) == 1
    assert len(audits) == 1
    audit = json.loads(audits[0].read_text(encoding="utf-8"))
    assert audit["kind"] == "spool_claim_quarantine"
    assert audit["recovered"] is False
    assert audit["filename_job_id"] == "job-invalid"
    assert audit["inode"] is not None
    assert Path(audit["quarantine_path"]).exists()


def test_valid_claim_recovers_after_rename_failure_and_quarantine(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("ORCH_SCRIPT_SPOOL_DIR", str(tmp_path / "spool"))
    root = ensure_spool_layout()
    entry = get_allowlist_entry("script.generic.repository_health")
    assert entry is not None

    # 1) Rename failure leaves pending claimable.
    first = build_job_envelope(
        job_id="job-recover",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    path = write_job(first)

    def boom(src, dst):
        raise OSError(errno.EIO, "simulated rename failure")

    monkeypatch.setattr(
        "flow_engine.script_sandbox.spool._atomic_move_noreplace", boom
    )
    with pytest.raises(AuthzDeniedError, match="atomic claim failed"):
        claim_job(path)
    monkeypatch.setattr(
        "flow_engine.script_sandbox.spool._atomic_move_noreplace",
        _atomic_move_noreplace,
    )

    recovered = claim_job(path)
    assert recovered["nonce"] == first["nonce"]
    assert recovered["job_id"] == "job-recover"
    assert (root / "seen").joinpath(f"job-nonce.{first['nonce']}.seen").exists()

    # 2) After an invalid quarantine, a fresh valid job remains claimable.
    poisoned = build_job_envelope(
        job_id="job-after-quarantine",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    poison_path = write_job(poisoned)
    forged = dict(poisoned)
    forged["timeout_sec"] = 999
    poison_path.write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(AuthzDeniedError):
        claim_job(poison_path)
    assert list((root / QUARANTINE_DIR).glob("job-after-quarantine.job.json.*.bad"))

    healthy = build_job_envelope(
        job_id="job-healthy-after",
        script_id=entry.script_id,
        argv=entry.argv,
        input_json={"ok": True},
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        timeout_sec=30,
    )
    healthy_path = write_job(healthy)
    claimed_healthy = claim_job(healthy_path)
    assert claimed_healthy["nonce"] == healthy["nonce"]
    assert claimed_healthy["input_json"]["ok"] is True
