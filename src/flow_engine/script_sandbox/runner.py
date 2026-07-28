"""Hardened argv-array script runner (never shell strings).

Subprocess execution is for script-worker only. Public callers cannot supply
workspace_root / simulate_network / force_timeout / inject_env / override_argv/cwd.
Deterministic test doubles are internal and gated by ORCH_TESTING.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flow_engine.domain.errors import (
    AuthzDeniedError,
    UnsupportedSurfaceError,
    ValidationFailedError,
)
from flow_engine.script_sandbox.allowlist import (
    SECRET_ENV_DENY_PREFIXES,
    SERVER_WORKSPACE_ROOT,
    AllowlistEntry,
    require_allowlist_entry,
)
from flow_engine.script_sandbox.effects import assert_allowed_effects
from flow_engine.script_sandbox.pins import (
    assert_script_runner_execution_authority,
    testing_fixtures_enabled,
    verify_executable_bytes,
    verify_image_digest,
)
from flow_engine.script_sandbox.results_schema import redact_failure_output
from flow_engine.script_sandbox.schemas import validate_against_schema


@dataclass(frozen=True)
class ScriptRunRequest:
    script_id: str
    input_json: dict[str, Any] = field(default_factory=dict)
    expected_executable_digest: str | None = None
    expected_image_digest: str | None = None
    cancel_check: Callable[[], bool] | None = None
    execution_id: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class ScriptRunResult:
    script_id: str
    status: str  # complete | failed | cancelled | timeout | rejected
    argv: tuple[str, ...]
    executable_digest: str
    image_digest: str
    output: dict[str, Any] = field(default_factory=dict)
    redacted_output: str = ""
    error_code: str | None = None
    error: str | None = None
    bounded: bool = True
    network_attempted: bool = False
    hardening: dict[str, Any] = field(default_factory=dict)
    pgid: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "script_id": self.script_id,
            "status": self.status,
            "argv": list(self.argv),
            "executable_digest": self.executable_digest,
            "image_digest": self.image_digest,
            "output": self.output,
            "redacted_output": self.redacted_output,
            "error_code": self.error_code,
            "error": self.error,
            "bounded": self.bounded,
            "network_attempted": self.network_attempted,
            "hardening": dict(self.hardening),
            "pgid": self.pgid,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScriptRunResult:
        return cls(
            script_id=str(data["script_id"]),
            status=str(data["status"]),
            argv=tuple(data.get("argv") or ()),
            executable_digest=str(data["executable_digest"]),
            image_digest=str(data["image_digest"]),
            output=dict(data.get("output") or {}),
            redacted_output=str(data.get("redacted_output") or ""),
            error_code=data.get("error_code"),
            error=data.get("error"),
            bounded=bool(data.get("bounded", True)),
            network_attempted=bool(data.get("network_attempted", False)),
            hardening=dict(data.get("hardening") or {}),
            pgid=data.get("pgid"),
        )


@dataclass
class _InternalTestHooks:
    """ORCH_TESTING-only doubles. Never exposed via DRF/MCP/schedule schemas."""

    simulate_network: bool = False
    force_timeout: bool = False
    inject_env: dict[str, str] | None = None
    override_argv: tuple[str, ...] | list[str] | None = None
    override_cwd: str | None = None
    workspace_root: str | None = None
    stub_executor: Callable[
        [ScriptRunRequest, AllowlistEntry, dict[str, str], str], ScriptRunResult
    ] | None = None


# Module-private test hook slot (set only by unit tests under ORCH_TESTING).
_TEST_HOOKS: _InternalTestHooks | None = None


def _testing_hooks() -> _InternalTestHooks | None:
    if not testing_fixtures_enabled():
        return None
    return _TEST_HOOKS


def set_testing_hooks(hooks: _InternalTestHooks | None) -> None:
    """Install deterministic test doubles. Raises outside ORCH_TESTING."""
    global _TEST_HOOKS
    if hooks is not None and not testing_fixtures_enabled():
        raise AuthzDeniedError("test hooks require ORCH_TESTING=1")
    _TEST_HOOKS = hooks


def _assert_argv_safe(argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    if not argv:
        raise ValidationFailedError("argv must be a non-empty array")
    if isinstance(argv, str):
        raise ValidationFailedError("argv must be an array, never a shell string")
    out: list[str] = []
    for part in argv:
        if not isinstance(part, str):
            raise ValidationFailedError("argv elements must be strings")
        if any(ch in part for ch in ("\n", "\r", "\x00")):
            raise ValidationFailedError("argv token contains forbidden control characters")
        out.append(part)
    return tuple(out)


def resolve_server_workspace(entry: AllowlistEntry) -> str:
    """Server-resolved cwd/path policy — never caller-controlled."""
    hooks = _testing_hooks()
    if hooks and hooks.workspace_root:
        root = Path(hooks.workspace_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    root = Path(os.environ.get("ORCH_SCRIPT_WORKSPACE", SERVER_WORKSPACE_ROOT))
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    for prefix in entry.allowed_path_prefixes:
        pref = Path(prefix)
        try:
            resolved.relative_to(pref if pref.is_absolute() else Path("/") / pref)
            return str(resolved)
        except ValueError:
            continue
    # Allow exact server workspace when prefixes are absolute /tmp/orch*.
    if str(resolved).startswith("/tmp/orch"):
        return str(resolved)
    raise AuthzDeniedError(f"server workspace outside path policy: {resolved}")


def _assert_cwd_confined(cwd: str, entry: AllowlistEntry, workspace_root: str) -> str:
    root = Path(workspace_root).resolve()
    target = Path(cwd).resolve()
    if entry.cwd_policy not in {"server-workspace-root", "workspace-root"}:
        raise ValidationFailedError(f"unsupported cwd_policy: {entry.cwd_policy}")
    try:
        target.relative_to(root)
        return str(target)
    except ValueError:
        for prefix in entry.allowed_path_prefixes:
            pref = Path(prefix)
            if not pref.is_absolute():
                continue
            try:
                target.relative_to(pref.resolve() if pref.exists() else pref)
                return str(target)
            except ValueError:
                continue
        raise AuthzDeniedError(f"cwd escape denied: {cwd}") from None


def _build_env(entry: AllowlistEntry, inject: dict[str, str] | None) -> dict[str, str]:
    allowed = set(entry.env_allowlist)
    env: dict[str, str] = {}
    for key in allowed:
        if key == "PATH":
            # The pinned image installs Python and orch-script under
            # /usr/local/bin; keep the path fixed rather than host-derived.
            env[key] = "/usr/local/bin:/usr/bin:/bin"
        elif key == "LANG":
            env[key] = "C.UTF-8"
        elif key == "LC_ALL":
            env[key] = "C.UTF-8"
        elif key == "TZ":
            env[key] = "Asia/Manila"
        elif key == "ORCH_SCRIPT_ID":
            env[key] = entry.script_id
        elif key == "ORCH_WORKDIR":
            env[key] = SERVER_WORKSPACE_ROOT
    for key, value in (inject or {}).items():
        upper = key.upper()
        if any(upper.startswith(p) or p in upper for p in SECRET_ENV_DENY_PREFIXES):
            raise AuthzDeniedError(f"secret/env projection denied for {key}")
        if key not in allowed:
            raise AuthzDeniedError(f"environment key {key} not on allowlist")
        env[key] = value
    for key in list(os.environ):
        upper = key.upper()
        if any(upper.startswith(p) or p.rstrip("_") in upper for p in SECRET_ENV_DENY_PREFIXES):
            if key in env:
                raise AuthzDeniedError(f"refusing to project secret-bearing env {key}")
    return env


def _stream_bounded(pipe: Any, cap: int, sink: bytearray, exceeded: list[bool]) -> None:
    """Read pipe in chunks; stop accepting once cap is hit (not post-truncation)."""
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            remaining = cap - len(sink)
            if remaining <= 0:
                exceeded[0] = True
                # Drain without retaining.
                continue
            if len(chunk) > remaining:
                sink.extend(chunk[:remaining])
                exceeded[0] = True
            else:
                sink.extend(chunk)
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _kill_process_group(proc: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        try:
            proc.kill()
        except Exception:
            pass


def _default_testing_stub(
    request: ScriptRunRequest,
    entry: AllowlistEntry,
    env: dict[str, str],
    cwd: str,
    hooks: _InternalTestHooks,
) -> ScriptRunResult:
    if hooks.simulate_network:
        return ScriptRunResult(
            script_id=entry.script_id,
            status="rejected",
            argv=entry.argv,
            executable_digest=entry.executable_digest,
            image_digest=entry.image_digest,
            error_code="AUTHZ_DENIED",
            error="network denied by default",
            network_attempted=True,
            hardening=dict(entry.hardening),
        )
    if hooks.force_timeout:
        return ScriptRunResult(
            script_id=entry.script_id,
            status="timeout",
            argv=entry.argv,
            executable_digest=entry.executable_digest,
            image_digest=entry.image_digest,
            error_code="OUTCOME_UNKNOWN",
            error="script timeout",
            hardening=dict(entry.hardening),
        )
    if request.cancel_check is not None and request.cancel_check():
        return ScriptRunResult(
            script_id=entry.script_id,
            status="cancelled",
            argv=entry.argv,
            executable_digest=entry.executable_digest,
            image_digest=entry.image_digest,
            error_code="VALIDATION_FAILED",
            error="cancelled",
            hardening=dict(entry.hardening),
        )

    effects: list[dict[str, Any]] = [
        {
            "type": "evidence",
            "summary": f"{entry.name} completed (sandbox stub)",
            "uri": f"orch://script/{entry.script_id}/evidence",
        }
    ]
    if entry.mutation_class == "evidence_producing":
        effects.append(
            {
                "type": "finding",
                "summary": f"{entry.name} observation",
                "severity": "low",
            }
        )
    output = {
        "script_id": entry.script_id,
        "status": "complete",
        "summary": f"{entry.name} ok",
        "effects": effects,
        "redacted_output": f"stub:{entry.script_id}",
    }
    validate_against_schema(output, entry.output_schema, where="output")
    assert_allowed_effects(effects)
    return ScriptRunResult(
        script_id=entry.script_id,
        status="complete",
        argv=entry.argv,
        executable_digest=entry.executable_digest,
        image_digest=entry.image_digest,
        output=output,
        redacted_output=output["redacted_output"],
        bounded=True,
        hardening={
            **entry.hardening,
            "env_keys": sorted(env),
            "cwd": cwd,
            "uid_non_root": True,
            "network": entry.network_policy,
            "fixture": "ORCH_TESTING",
        },
    )


def run_allowlisted_script(request: ScriptRunRequest) -> ScriptRunResult:
    """Execute only an allowlisted generic script via argv arrays.

    Intended for the networkless script-runner (or ORCH_TESTING fixtures).
    Networked script-worker must use controller/spool dispatch instead.
    """
    assert_script_runner_execution_authority()
    entry = require_allowlist_entry(request.script_id)
    hooks = _testing_hooks()

    if hooks and hooks.override_argv is not None:
        raise AuthzDeniedError(
            "caller argv override is denied; allowlist argv is authoritative"
        )

    argv = _assert_argv_safe(entry.argv)
    if not entry.argv_only:
        raise ValidationFailedError("allowlist entry must be argv_only")

    if (
        request.expected_executable_digest
        and request.expected_executable_digest != entry.executable_digest
    ):
        raise AuthzDeniedError("executable digest mismatch")
    if request.expected_image_digest and request.expected_image_digest != entry.image_digest:
        raise AuthzDeniedError("image digest mismatch")

    validate_against_schema(request.input_json, entry.input_schema, where="input")

    workspace = resolve_server_workspace(entry)
    cwd = (hooks.override_cwd if hooks and hooks.override_cwd else workspace)
    if hooks and hooks.override_cwd:
        # Still enforce confinement — escape must fail.
        confined_cwd = _assert_cwd_confined(cwd, entry, workspace)
    else:
        confined_cwd = _assert_cwd_confined(workspace, entry, workspace)

    inject = hooks.inject_env if hooks else None
    env = _build_env(entry, inject)

    if hooks and hooks.stub_executor is not None:
        return hooks.stub_executor(request, entry, env, confined_cwd)

    # Outside ORCH_TESTING: pin+verify real bytes; never silent stub fallback.
    if not testing_fixtures_enabled():
        verify_image_digest(expected_digest=entry.image_digest)
        verify_executable_bytes(expected_digest=entry.executable_digest)
    elif hooks and (hooks.simulate_network or hooks.force_timeout):
        return _default_testing_stub(request, entry, env, confined_cwd, hooks)
    else:
        # ORCH_TESTING without explicit hooks: prefer real binary if present,
        # else deterministic fixture stub (explicit testing path only).
        exe = Path(argv[0])
        test_exe = Path(os.environ.get("ORCH_SCRIPT_EXECUTABLE", ""))
        if test_exe.is_file():
            os.environ.setdefault("ORCH_SCRIPT_EXECUTABLE", str(test_exe))
        if not exe.is_file() and not test_exe.is_file():
            return _default_testing_stub(
                request, entry, env, confined_cwd, hooks or _InternalTestHooks()
            )
        # When binary exists under testing, still verify digest if image env set.
        if os.environ.get("ORCH_SCRIPT_IMAGE_DIGEST"):
            verify_image_digest(expected_digest=entry.image_digest)
        try:
            verify_executable_bytes(expected_digest=entry.executable_digest)
        except AuthzDeniedError:
            # Point ORCH_SCRIPT_EXECUTABLE at source CLI for digest match in tests.
            os.environ["ORCH_SCRIPT_EXECUTABLE"] = str(
                Path(__file__).resolve().parent / "orch_script_cli.py"
            )
            verify_executable_bytes(expected_digest=entry.executable_digest)

    # Real subprocess path: argv array only, never shell=True.
    try:
        tmp_root = Path("/tmp/orch")
        try:
            tmp_root.mkdir(parents=True, exist_ok=True)
            tmp_dir_parent: str | None = str(tmp_root)
        except OSError:
            tmp_dir_parent = None
        with tempfile.TemporaryDirectory(
            prefix="orch-script-tmp-", dir=tmp_dir_parent
        ) as tmp:
            in_path = Path(tmp) / "in.json"
            in_path.write_text(json.dumps(request.input_json), encoding="utf-8")
            run_argv = list(argv)
            # Use source CLI under testing when /usr/local/bin/orch-script absent.
            exe_path = Path(os.environ.get("ORCH_SCRIPT_EXECUTABLE", run_argv[0]))
            if exe_path.is_file():
                run_argv[0] = (
                    str(exe_path)
                    if exe_path.name != "orch_script_cli.py"
                    else str(exe_path)
                )
                if exe_path.suffix == ".py":
                    run_argv = [os.environ.get("ORCH_PYTHON", "python3"), str(exe_path), *run_argv[1:]]
            for i, part in enumerate(run_argv):
                if part.endswith("in.json"):
                    run_argv[i] = str(in_path)
            proc = subprocess.Popen(  # noqa: S603 — intentional argv-only exec
                run_argv,
                cwd=confined_cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
            stdout_buf = bytearray()
            stderr_buf = bytearray()
            exceeded = [False]
            readers = [
                threading.Thread(
                    target=_stream_bounded,
                    args=(proc.stdout, entry.output_cap_bytes, stdout_buf, exceeded),
                    daemon=True,
                ),
                threading.Thread(
                    target=_stream_bounded,
                    args=(proc.stderr, entry.output_cap_bytes, stderr_buf, exceeded),
                    daemon=True,
                ),
            ]
            for thread in readers:
                thread.start()

            deadline = time.monotonic() + entry.timeout_sec
            while True:
                if request.cancel_check is not None and request.cancel_check():
                    _kill_process_group(proc)
                    for thread in readers:
                        thread.join(timeout=1)
                    return ScriptRunResult(
                        script_id=entry.script_id,
                        status="cancelled",
                        argv=tuple(run_argv),
                        executable_digest=entry.executable_digest,
                        image_digest=entry.image_digest,
                        error_code="VALIDATION_FAILED",
                        error="cancelled",
                        hardening=dict(entry.hardening),
                        pgid=proc.pid,
                    )
                if time.monotonic() > deadline:
                    _kill_process_group(proc)
                    for thread in readers:
                        thread.join(timeout=1)
                    return ScriptRunResult(
                        script_id=entry.script_id,
                        status="timeout",
                        argv=tuple(run_argv),
                        executable_digest=entry.executable_digest,
                        image_digest=entry.image_digest,
                        error_code="OUTCOME_UNKNOWN",
                        error="script timeout",
                        hardening=dict(entry.hardening),
                        pgid=proc.pid,
                    )
                if exceeded[0]:
                    _kill_process_group(proc)
                    for thread in readers:
                        thread.join(timeout=1)
                    combined = (bytes(stdout_buf) + bytes(stderr_buf)).decode(
                        "utf-8", errors="replace"
                    )
                    return ScriptRunResult(
                        script_id=entry.script_id,
                        status="failed",
                        argv=tuple(run_argv),
                        executable_digest=entry.executable_digest,
                        image_digest=entry.image_digest,
                        redacted_output=redact_failure_output(combined),
                        bounded=False,
                        error_code="VALIDATION_FAILED",
                        error="output cap exceeded during execution",
                        hardening=dict(entry.hardening),
                        pgid=proc.pid,
                    )
                ret = proc.poll()
                if ret is not None:
                    for thread in readers:
                        thread.join(timeout=5)
                    stdout_text = bytes(stdout_buf).decode("utf-8", errors="replace")
                    stderr_text = bytes(stderr_buf).decode("utf-8", errors="replace")
                    combined = redact_failure_output(stdout_text, stderr_text)
                    bounded = not exceeded[0]
                    if ret != 0:
                        return ScriptRunResult(
                            script_id=entry.script_id,
                            status="failed",
                            argv=tuple(run_argv),
                            executable_digest=entry.executable_digest,
                            image_digest=entry.image_digest,
                            redacted_output=combined,
                            bounded=bounded,
                            error_code="VALIDATION_FAILED",
                            error=f"exit {ret}",
                            hardening=dict(entry.hardening),
                            pgid=proc.pid,
                        )
                    try:
                        output = json.loads(stdout_text or "{}")
                    except json.JSONDecodeError as exc:
                        raise ValidationFailedError("script output is not JSON") from exc
                    validate_against_schema(output, entry.output_schema, where="output")
                    assert_allowed_effects(output.get("effects"))
                    return ScriptRunResult(
                        script_id=entry.script_id,
                        status=str(output.get("status") or "complete"),
                        argv=tuple(run_argv),
                        executable_digest=entry.executable_digest,
                        image_digest=entry.image_digest,
                        output=output,
                        redacted_output=redact_failure_output(
                            str(output.get("redacted_output") or combined)
                        ),
                        bounded=bounded,
                        hardening=dict(entry.hardening),
                        pgid=proc.pid,
                    )
                time.sleep(0.05)
    except AuthzDeniedError:
        raise
    except UnsupportedSurfaceError:
        raise
    except ValidationFailedError:
        raise
    except OSError as exc:
        return ScriptRunResult(
            script_id=entry.script_id,
            status="failed",
            argv=argv,
            executable_digest=entry.executable_digest,
            image_digest=entry.image_digest,
            error_code="VALIDATION_FAILED",
            error=str(exc),
            hardening=dict(entry.hardening),
        )
