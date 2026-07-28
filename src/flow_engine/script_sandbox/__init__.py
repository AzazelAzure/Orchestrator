"""R4C generic registered-script allowlist and hardened runner boundary.

The public facade is deliberately lazy.  Attestation creation imports the
``attestation`` submodule before an attestation exists; eagerly importing the
allowlist here would require that not-yet-created document and make secure
bootstrap impossible.  Resolving runtime exports still imports their owning
modules and therefore preserves fail-closed attestation enforcement.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "ALLOWED_SCRIPT_EFFECTS": "effects",
    "FORBIDDEN_SCRIPT_EFFECTS": "effects",
    "GENERIC_SCRIPT_IDS": "allowlist",
    "ORCH_SCRIPT_EXECUTABLE_DIGEST": "allowlist",
    "SCRIPT_RUNNER_IMAGE_DIGEST": "allowlist",
    "SCRIPT_WORKER_IMAGE_DIGEST": "allowlist",
    "ScriptClass": "classify",
    "ScriptRunRequest": "runner",
    "ScriptRunResult": "runner",
    "_InternalTestHooks": "runner",
    "assert_allowed_effects": "effects",
    "classify_script": "classify",
    "get_allowlist_entry": "allowlist",
    "list_allowlist": "allowlist",
    "reject_repository_script": "classify",
    "run_allowlisted_script": "runner",
    "set_testing_hooks": "runner",
}

__all__ = [
    "ALLOWED_SCRIPT_EFFECTS",
    "FORBIDDEN_SCRIPT_EFFECTS",
    "GENERIC_SCRIPT_IDS",
    "ORCH_SCRIPT_EXECUTABLE_DIGEST",
    "SCRIPT_RUNNER_IMAGE_DIGEST",
    "SCRIPT_WORKER_IMAGE_DIGEST",
    "ScriptClass",
    "ScriptRunRequest",
    "ScriptRunResult",
    "_InternalTestHooks",
    "assert_allowed_effects",
    "classify_script",
    "get_allowlist_entry",
    "list_allowlist",
    "reject_repository_script",
    "run_allowlisted_script",
    "set_testing_hooks",
]


def __getattr__(name: str) -> Any:
    """Load public runtime symbols only when a caller actually requests them."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value
