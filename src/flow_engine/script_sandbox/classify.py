"""Classify scripts: only the generic allowlist is executable."""

from __future__ import annotations

from enum import StrEnum

from flow_engine.domain.errors import UnsupportedSurfaceError, ValidationFailedError
from flow_engine.script_sandbox.allowlist import GENERIC_SCRIPT_IDS


class ScriptClass(StrEnum):
    GENERIC_ALLOWLISTED = "generic_allowlisted"
    REPOSITORY_CATALOG_ONLY = "repository_catalog_only"
    UNKNOWN = "unknown"


def classify_script(script_id: str) -> ScriptClass:
    if not script_id or not str(script_id).strip():
        return ScriptClass.UNKNOWN
    sid = str(script_id).strip()
    if sid in GENERIC_SCRIPT_IDS:
        return ScriptClass.GENERIC_ALLOWLISTED
    # Catalog presence / repository-extension IDs are never executable.
    if sid.startswith("script."):
        return ScriptClass.REPOSITORY_CATALOG_ONLY
    return ScriptClass.UNKNOWN


def reject_repository_script(script_id: str) -> None:
    """Fail closed for anything outside the generic allowlist."""
    kind = classify_script(script_id)
    if kind == ScriptClass.GENERIC_ALLOWLISTED:
        return
    if kind == ScriptClass.REPOSITORY_CATALOG_ONLY:
        raise UnsupportedSurfaceError(
            f"repository script {script_id} is catalog-only and non-executable"
        )
    raise ValidationFailedError(f"unknown or unregistered script_id: {script_id}")
