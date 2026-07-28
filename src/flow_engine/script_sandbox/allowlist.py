"""Exact generic registered-script allowlist (HD-ACP-013 / loadout-catalog).

Catalog presence is not authority. Only entries here may execute. Repository
scripts and any other IDs are rejected at every surface.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from flow_engine.script_sandbox.attestation import authorized_script_runner_image_digest
from flow_engine.script_sandbox.pins import (
    assert_valid_sha256_digest,
    orch_script_source_digest,
)

# Authorized digests: executable bytes + deployment attestation image digest.
# Image digest is never a self-referential pin-manifest hash.
ORCH_SCRIPT_EXECUTABLE_DIGEST = orch_script_source_digest()
SCRIPT_RUNNER_IMAGE_DIGEST = authorized_script_runner_image_digest()
# Back-compat alias used by earlier R4C tests/docs.
SCRIPT_WORKER_IMAGE_DIGEST = SCRIPT_RUNNER_IMAGE_DIGEST

DEFAULT_ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TZ", "ORCH_SCRIPT_ID", "ORCH_WORKDIR")
SECRET_ENV_DENY_PREFIXES = (
    "ORCH_TOKEN_",
    "ORCH_API_SERVICE_",
    "ORCH_WORKER_SERVICE_",
    "REDIS_",
    "DJANGO_SECRET",
    "AWS_",
    "OPENAI_",
    "ANTHROPIC_",
    "GITHUB_TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
)

HARDENING = {
    "non_root": True,
    "read_only_root_fs": True,
    "tmpfs": True,
    "no_new_privileges": True,
    "seccomp": True,
    "cap_drop_all": True,
    "network": "none",
    "network_mode": "none",
    "spool_boundary": True,
}

# Server-resolved workspace (never caller-controlled).
SERVER_WORKSPACE_ROOT = "/tmp/orch/workspace"
SERVER_ALLOWED_PATH_PREFIXES = ("/tmp/orch", "/tmp/orch/workspace")


@dataclass(frozen=True)
class AllowlistEntry:
    script_id: str
    name: str
    mutation_class: str  # read_only | evidence_producing
    # Argv template: first element is executable path inside the sandbox image.
    argv: tuple[str, ...]
    executable_digest: str
    image_digest: str = SCRIPT_RUNNER_IMAGE_DIGEST
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    cwd_policy: str = "server-workspace-root"
    allowed_path_prefixes: tuple[str, ...] = SERVER_ALLOWED_PATH_PREFIXES
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    network_policy: str = "none"
    timeout_sec: int = 900  # 15 minutes default
    output_cap_bytes: int = 65536
    concurrency: int = 1
    idempotent: bool = True
    hardening: dict[str, Any] = field(default_factory=lambda: dict(HARDENING))
    argv_only: bool = True
    repository_script: bool = False
    executable: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["argv"] = list(self.argv)
        data["allowed_path_prefixes"] = list(self.allowed_path_prefixes)
        data["env_allowlist"] = list(self.env_allowlist)
        return data


def _io_schemas(script_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    input_schema = {
        "type": "object",
        "properties": {
            "scope": {"type": "string", "maxLength": 256},
            "dry_run": {"type": "boolean"},
        },
        "additionalProperties": False,
    }
    output_schema = {
        "type": "object",
        "required": ["script_id", "status", "effects"],
        "properties": {
            "script_id": {"type": "string", "enum": [script_id]},
            "status": {"type": "string", "enum": ["complete", "failed", "cancelled", "timeout"]},
            "summary": {"type": "string", "maxLength": 2048},
            "effects": {
                "type": "array",
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "required": ["type"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "evidence",
                                "finding",
                                "anomaly",
                                "follow_up_work_candidate",
                            ],
                        },
                        "summary": {"type": "string", "maxLength": 1024},
                        "severity": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                        },
                        "uri": {"type": "string", "maxLength": 512},
                    },
                    "additionalProperties": False,
                },
            },
            "redacted_output": {"type": "string", "maxLength": 8192},
        },
        "additionalProperties": False,
    }
    return input_schema, output_schema


def _entry(
    script_id: str,
    name: str,
    mutation_class: str,
    *,
    timeout_sec: int = 900,
) -> AllowlistEntry:
    short = script_id.rsplit(".", 1)[-1]
    argv = ("/usr/local/bin/orch-script", short, "--json-in", "/tmp/orch/in.json")
    executable_digest = assert_valid_sha256_digest(
        ORCH_SCRIPT_EXECUTABLE_DIGEST, what="executable digest"
    )
    image_digest = assert_valid_sha256_digest(
        SCRIPT_RUNNER_IMAGE_DIGEST, what="image digest"
    )
    input_schema, output_schema = _io_schemas(script_id)
    return AllowlistEntry(
        script_id=script_id,
        name=name,
        mutation_class=mutation_class,
        argv=argv,
        executable_digest=executable_digest,
        image_digest=image_digest,
        input_schema=input_schema,
        output_schema=output_schema,
        timeout_sec=timeout_sec,
    )


_ALLOWLIST: dict[str, AllowlistEntry] = {
    e.script_id: e
    for e in (
        _entry("script.generic.repository_health", "Repository health", "read_only"),
        _entry("script.generic.git_diff_summary", "Git diff summary", "read_only"),
        _entry("script.generic.repository_inventory", "Repository inventory", "read_only"),
        _entry(
            "script.generic.documentation_link_sweep",
            "Documentation link sweep",
            "evidence_producing",
        ),
        _entry(
            "script.generic.documentation_metadata_sweep",
            "Documentation metadata sweep",
            "evidence_producing",
        ),
        _entry(
            "script.generic.governance_integrity_sweep",
            "Governance integrity sweep",
            "evidence_producing",
        ),
        _entry(
            "script.generic.secret_pattern_scan",
            "Secret-pattern scan",
            "evidence_producing",
        ),
        _entry(
            "script.generic.dependency_manifest_inventory",
            "Dependency-manifest inventory",
            "evidence_producing",
        ),
        _entry(
            "script.generic.catalog_integrity_sweep",
            "Catalog integrity sweep",
            "evidence_producing",
        ),
        _entry(
            "script.generic.stale_work_sweep",
            "Stale work/gate/lease/attempt sweep",
            "evidence_producing",
        ),
        _entry(
            "script.generic.queue_worker_heartbeat_health",
            "Queue/worker/heartbeat health",
            "read_only",
        ),
        _entry(
            "script.generic.backup_restore_probe",
            "Backup/restore probe",
            "evidence_producing",
            timeout_sec=1800,
        ),
    )
}

GENERIC_SCRIPT_IDS = frozenset(_ALLOWLIST)


def get_allowlist_entry(script_id: str) -> AllowlistEntry | None:
    return _ALLOWLIST.get(script_id)


def list_allowlist() -> list[dict[str, Any]]:
    return [entry.to_dict() for entry in sorted(_ALLOWLIST.values(), key=lambda e: e.script_id)]


def require_allowlist_entry(script_id: str) -> AllowlistEntry:
    from flow_engine.script_sandbox.classify import reject_repository_script

    reject_repository_script(script_id)
    entry = get_allowlist_entry(script_id)
    if entry is None:
        from flow_engine.domain.errors import ValidationFailedError

        raise ValidationFailedError(f"script not on allowlist: {script_id}")
    return entry
