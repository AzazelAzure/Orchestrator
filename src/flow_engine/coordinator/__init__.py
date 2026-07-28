"""Sole-writer state coordinator package."""

from typing import TYPE_CHECKING, Any

from flow_engine.coordinator.audit import append_audit_event, list_audit_events
from flow_engine.coordinator.authz import authorize_command, validate_step_up
from flow_engine.coordinator.commands import (
    FOUNDER_ONLY_COMMANDS,
    MCP_FORBIDDEN_COMMANDS,
    CommandContext,
    ResolvedTaskGrant,
    RuntimeCommand,
    StepUpEvidence,
    SystemTestGrant,
    stable_digest,
)

if TYPE_CHECKING:
    from flow_engine.coordinator.coordinator import StateCoordinator


def __getattr__(name: str) -> Any:
    if name == "StateCoordinator":
        from flow_engine.coordinator.coordinator import StateCoordinator

        return StateCoordinator
    raise AttributeError(name)

__all__ = [
    "FOUNDER_ONLY_COMMANDS",
    "MCP_FORBIDDEN_COMMANDS",
    "CommandContext",
    "ResolvedTaskGrant",
    "RuntimeCommand",
    "StateCoordinator",
    "StepUpEvidence",
    "SystemTestGrant",
    "append_audit_event",
    "authorize_command",
    "list_audit_events",
    "stable_digest",
    "validate_step_up",
]
