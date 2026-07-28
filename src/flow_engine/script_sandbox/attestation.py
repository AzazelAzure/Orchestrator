"""Deployment/build attestation for the networkless script-runner image.

Runtime never calls Docker/Podman. Authorized digests come from a signed/hashed
local attestation produced at build/deploy time via container image inspect
(Id / RepoDigest) using podman or docker CLI. Outside ORCH_TESTING, missing or
testing-only attestation fails closed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any

from flow_engine.domain.errors import AuthzDeniedError
from flow_engine.script_sandbox.pins import assert_valid_sha256_digest, sha256_bytes

SCHEMA_VERSION = 1
ATTESTATION_TARGET = "script-runner"
SOURCE_DOCKER_INSPECT = "docker_inspect"
SOURCE_CONTAINER_INSPECT = "container_inspect"
SOURCE_TESTING_FIXTURE = "orch_testing_fixture"
PRODUCTION_ATTESTATION_SOURCES = frozenset(
    {SOURCE_DOCKER_INSPECT, SOURCE_CONTAINER_INSPECT}
)

# Deterministic testing-only image digest (not claimed as a Docker RepoDigest).
TESTING_IMAGE_DIGEST = sha256_bytes(b"orch-testing-script-runner-image-fixture-v1")
TESTING_IMAGE_ID = sha256_bytes(b"orch-testing-script-runner-image-id-v1")
_TESTING_HMAC_KEY = b"orch-testing-attestation-hmac-key-v1"

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ATTESTATION_PATH = Path(
    "/etc/orch/script-runner.attestation.json"
)
DEFAULT_TESTING_ATTESTATION_PATH = (
    _REPO_ROOT / "deploy" / "attestations" / "script-runner.testing.attestation.json"
)


def testing_fixtures_enabled() -> bool:
    return os.environ.get("ORCH_TESTING", "0") == "1"


def _canonical_payload(doc: dict[str, Any]) -> bytes:
    body = {
        "schema_version": int(doc["schema_version"]),
        "target": str(doc["target"]),
        "image_digest": str(doc["image_digest"]),
        "image_id": str(doc["image_id"]),
        "executable_digest": str(doc["executable_digest"]),
        "built_at": str(doc["built_at"]),
        "source": str(doc["source"]),
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hmac_key(*, source: str) -> bytes:
    if source == SOURCE_TESTING_FIXTURE:
        return _TESTING_HMAC_KEY
    key = os.environ.get("ORCH_ATTESTATION_HMAC_KEY", "").strip()
    if not key:
        raise AuthzDeniedError(
            "ORCH_ATTESTATION_HMAC_KEY required to verify deployment attestation"
        )
    return key.encode("utf-8")


def compute_attestation_mac(doc: dict[str, Any]) -> str:
    payload = _canonical_payload(doc)
    digest = hmac.new(
        _hmac_key(source=str(doc["source"])), payload, hashlib.sha256
    ).hexdigest()
    return "sha256:" + digest


def compute_payload_hash(doc: dict[str, Any]) -> str:
    return sha256_bytes(_canonical_payload(doc))


def build_attestation_document(
    *,
    image_digest: str,
    image_id: str,
    executable_digest: str,
    built_at: str,
    source: str,
) -> dict[str, Any]:
    image_digest = assert_valid_sha256_digest(image_digest, what="image_digest")
    image_id = assert_valid_sha256_digest(image_id, what="image_id")
    executable_digest = assert_valid_sha256_digest(
        executable_digest, what="executable_digest"
    )
    if source not in PRODUCTION_ATTESTATION_SOURCES | {SOURCE_TESTING_FIXTURE}:
        raise AuthzDeniedError(f"unsupported attestation source: {source}")
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "target": ATTESTATION_TARGET,
        "image_digest": image_digest,
        "image_id": image_id,
        "executable_digest": executable_digest,
        "built_at": built_at,
        "source": source,
    }
    doc["payload_hash"] = compute_payload_hash(doc)
    doc["mac"] = compute_attestation_mac(doc)
    return doc


def write_attestation(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _resolve_attestation_path() -> Path:
    override = os.environ.get("ORCH_SCRIPT_RUNNER_ATTESTATION_FILE", "").strip()
    if override:
        return Path(override)
    if testing_fixtures_enabled():
        return DEFAULT_TESTING_ATTESTATION_PATH
    return DEFAULT_ATTESTATION_PATH


def load_raw_attestation(path: Path | None = None) -> dict[str, Any]:
    target = path or _resolve_attestation_path()
    if not target.is_file():
        raise AuthzDeniedError(
            f"script-runner attestation missing: {target} "
            "(build/deploy attestation required; runtime does not invoke Docker)"
        )
    try:
        doc = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthzDeniedError(f"script-runner attestation unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise AuthzDeniedError("script-runner attestation must be an object")
    return doc


def verify_attestation(doc: dict[str, Any]) -> dict[str, Any]:
    required = (
        "schema_version",
        "target",
        "image_digest",
        "image_id",
        "executable_digest",
        "built_at",
        "source",
        "payload_hash",
        "mac",
    )
    for key in required:
        if key not in doc:
            raise AuthzDeniedError(f"attestation missing field {key}")
    if int(doc["schema_version"]) != SCHEMA_VERSION:
        raise AuthzDeniedError("attestation schema_version mismatch")
    if str(doc["target"]) != ATTESTATION_TARGET:
        raise AuthzDeniedError("attestation target must be script-runner")
    source = str(doc["source"])
    if source == SOURCE_TESTING_FIXTURE and not testing_fixtures_enabled():
        raise AuthzDeniedError(
            "testing attestation rejected outside ORCH_TESTING (fail closed)"
        )
    if not testing_fixtures_enabled() and source not in PRODUCTION_ATTESTATION_SOURCES:
        raise AuthzDeniedError(
            "production attestation source must be container_inspect or "
            "docker_inspect (no runtime container verification claimed)"
        )
    assert_valid_sha256_digest(str(doc["image_digest"]), what="attestation image_digest")
    assert_valid_sha256_digest(str(doc["image_id"]), what="attestation image_id")
    assert_valid_sha256_digest(
        str(doc["executable_digest"]), what="attestation executable_digest"
    )
    expected_hash = compute_payload_hash(doc)
    if str(doc["payload_hash"]) != expected_hash:
        raise AuthzDeniedError("attestation payload_hash mismatch")
    expected_mac = compute_attestation_mac(doc)
    if not hmac.compare_digest(str(doc["mac"]), expected_mac):
        raise AuthzDeniedError("attestation mac mismatch")
    return doc


def load_verified_attestation(path: Path | None = None) -> dict[str, Any]:
    return verify_attestation(load_raw_attestation(path))


def authorized_script_runner_image_digest() -> str:
    """Resolve the authorized immutable image digest for dispatch/worker.

    Order:
    1. Verified local attestation file (container_inspect/docker_inspect in prod;
       testing fixture under ORCH_TESTING)
    2. ORCH_SCRIPT_IMAGE_DIGEST only when it matches attestation (prod) or under testing fallback
    Never invents a digest from a self-referential pin manifest.
    Never calls Docker/Podman at runtime.
    """
    if testing_fixtures_enabled():
        path = _resolve_attestation_path()
        if not path.is_file():
            ensure_testing_attestation_file()

    env_digest = os.environ.get("ORCH_SCRIPT_IMAGE_DIGEST", "").strip()
    attestation: dict[str, Any] | None = None
    try:
        attestation = load_verified_attestation()
    except AuthzDeniedError:
        if not testing_fixtures_enabled() and not env_digest:
            raise
        if not testing_fixtures_enabled() and env_digest:
            # Env alone is insufficient outside testing without attestation input.
            raise AuthzDeniedError(
                "fail closed: authorized image digest requires verified "
                "deployment attestation outside ORCH_TESTING"
            ) from None
        attestation = None

    if attestation is not None:
        authorized = assert_valid_sha256_digest(
            str(attestation["image_digest"]), what="authorized image digest"
        )
        if env_digest:
            env_digest = assert_valid_sha256_digest(env_digest, what="ORCH_SCRIPT_IMAGE_DIGEST")
            if env_digest != authorized:
                raise AuthzDeniedError(
                    "ORCH_SCRIPT_IMAGE_DIGEST does not match attestation image_digest"
                )
        return authorized

    # ORCH_TESTING only path without attestation file: use fixture digest.
    if env_digest:
        return assert_valid_sha256_digest(env_digest, what="ORCH_SCRIPT_IMAGE_DIGEST")
    return assert_valid_sha256_digest(TESTING_IMAGE_DIGEST, what="testing image digest")


def require_authorized_image_digest(expected: str) -> str:
    authorized = authorized_script_runner_image_digest()
    expected = assert_valid_sha256_digest(expected, what="expected image digest")
    if expected != authorized:
        raise AuthzDeniedError(
            f"image digest not authorized by attestation: expected {authorized}, got {expected}"
        )
    return authorized


def ensure_testing_attestation_file() -> Path:
    """Write/refresh the committed testing attestation (ORCH_TESTING only)."""
    from flow_engine.script_sandbox.pins import orch_script_source_digest

    if not testing_fixtures_enabled():
        raise AuthzDeniedError("testing attestation write requires ORCH_TESTING=1")
    doc = build_attestation_document(
        image_digest=TESTING_IMAGE_DIGEST,
        image_id=TESTING_IMAGE_ID,
        executable_digest=orch_script_source_digest(),
        built_at="1970-01-01T00:00:00Z",
        source=SOURCE_TESTING_FIXTURE,
    )
    write_attestation(DEFAULT_TESTING_ATTESTATION_PATH, doc)
    return DEFAULT_TESTING_ATTESTATION_PATH
