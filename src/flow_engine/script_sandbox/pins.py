"""Content-addressed executable pins and execution-role gates (fail closed)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from flow_engine.domain.errors import AuthzDeniedError

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

ORCH_SCRIPT_CLI_PATH = Path(__file__).resolve().parent / "orch_script_cli.py"


def assert_valid_sha256_digest(value: str, *, what: str = "digest") -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise AuthzDeniedError(
            f"{what} must be sha256:<64 lowercase hex>; refused placeholder/invalid"
        )
    return value


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                break
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def orch_script_source_digest() -> str:
    if not ORCH_SCRIPT_CLI_PATH.is_file():
        raise AuthzDeniedError("orch-script source pin missing")
    return assert_valid_sha256_digest(
        sha256_file(ORCH_SCRIPT_CLI_PATH), what="executable digest"
    )


def resolve_runtime_executable_path() -> Path:
    override = os.environ.get("ORCH_SCRIPT_EXECUTABLE")
    if override:
        return Path(override)
    return Path("/usr/local/bin/orch-script")


def verify_executable_bytes(*, expected_digest: str) -> str:
    expected = assert_valid_sha256_digest(expected_digest, what="executable digest")
    path = resolve_runtime_executable_path()
    if not path.is_file():
        raise AuthzDeniedError(f"pinned executable missing: {path}")
    actual = sha256_file(path)
    if actual != expected:
        raise AuthzDeniedError(
            f"executable digest mismatch: expected {expected}, got {actual}"
        )
    return actual


def testing_fixtures_enabled() -> bool:
    return os.environ.get("ORCH_TESTING", "0") == "1"


def assert_script_runner_execution_authority() -> None:
    """Subprocess execution is script-runner only (or ORCH_TESTING fixtures)."""
    role = os.environ.get("ORCH_SCRIPT_ROLE", "").strip()
    if testing_fixtures_enabled():
        return
    if role != "script-runner":
        raise AuthzDeniedError(
            "script subprocess execution denied outside script-runner role"
        )


def assert_script_worker_controller_authority() -> None:
    """Spool dispatch / coordinator transport is script-worker only (or testing)."""
    role = os.environ.get("ORCH_SCRIPT_ROLE", "").strip()
    if testing_fixtures_enabled():
        return
    if role != "script-worker":
        raise AuthzDeniedError(
            "script-worker controller authority required for spool dispatch"
        )


def assert_script_worker_execution_authority() -> None:
    """Back-compat alias: subprocess path now requires script-runner."""
    assert_script_runner_execution_authority()


def verify_image_digest(*, expected_digest: str) -> str:
    """Verify expected digest against authorized attestation — never calls Docker."""
    from flow_engine.script_sandbox.attestation import require_authorized_image_digest

    return require_authorized_image_digest(expected_digest)
