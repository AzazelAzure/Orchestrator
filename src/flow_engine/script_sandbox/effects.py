"""Allowed and forbidden effects from script/schedule outcomes."""

from __future__ import annotations

from typing import Any

from flow_engine.domain.errors import UnsupportedSurfaceError, ValidationFailedError

ALLOWED_SCRIPT_EFFECTS = frozenset(
    {
        "evidence",
        "finding",
        "anomaly",
        "follow_up_work_candidate",
    }
)

FORBIDDEN_SCRIPT_EFFECTS = frozenset(
    {
        "repair",
        "remediation",
        "repository_mutation",
        "repo_mutation",
        "merge",
        "deploy",
        "publication",
        "policy_change",
        "gate_change",
        "schedule_mutation",
        "provider_call",
        "secret_projection",
        "credential_projection",
        "waiver",
        "hitm_exception",
        "paid_retry_after_unknown",
    }
)


def assert_allowed_effects(effects: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> None:
    for item in effects or ():
        if not isinstance(item, dict):
            raise ValidationFailedError("effect entries must be objects")
        effect_type = str(item.get("type") or item.get("effect") or "").strip()
        if not effect_type:
            raise ValidationFailedError("effect type is required")
        if effect_type in FORBIDDEN_SCRIPT_EFFECTS:
            raise UnsupportedSurfaceError(
                f"script/schedule effect {effect_type} is forbidden"
            )
        if effect_type not in ALLOWED_SCRIPT_EFFECTS:
            raise UnsupportedSurfaceError(
                f"script/schedule effect {effect_type} is not in the allowlist"
            )
