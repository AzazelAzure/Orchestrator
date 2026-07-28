"""Domain state enumerations for the workflow kernel."""

from __future__ import annotations

from enum import StrEnum


class WorkItemStatus(StrEnum):
    """Legacy queue statuses plus R2 governed lifecycle expansions."""

    PENDING = "pending"
    CLAIMED = "claimed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETE = "complete"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILING = "reconciling"


# Legacy four-state set retained for additive-migration compatibility checks.
LEGACY_WORK_ITEM_STATUSES = frozenset(
    {
        WorkItemStatus.PENDING,
        WorkItemStatus.CLAIMED,
        WorkItemStatus.COMPLETE,
        WorkItemStatus.FAILED,
    }
)


class RunStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETE = "complete"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILING = "reconciling"


class AttemptStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETE = "complete"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILING = "reconciling"


class InvocationStatus(StrEnum):
    RESERVED = "reserved"
    DISPATCHED = "dispatched"
    COMPLETE = "complete"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    RECONCILED = "reconciled"


class ProviderLimitState(StrEnum):
    OPEN = "open"
    HALTED = "halted"


class ClaimPolicy(StrEnum):
    ADVISORY = "advisory"
    STRICT = "strict"


class LeaseMode(StrEnum):
    EXCLUSIVE = "exclusive"


class GateStatus(StrEnum):
    OPEN = "open"
    PASSED = "passed"
    FAILED = "failed"
    WAIVED = "waived"


class GateRequirement(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"


class FindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class FindingStatus(StrEnum):
    OPEN = "open"
    TRIAGED = "triaged"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"
    REOPENED = "reopened"


class AnomalyCode(StrEnum):
    A0 = "A0"  # Integrity / security
    A1 = "A1"  # Uncertain side effect
    A2 = "A2"  # Authority / scope / gate
    A3 = "A3"  # Runtime / resource
    A4 = "A4"  # Evidence / report
    A5 = "A5"  # Quality / maintenance


class Surface(StrEnum):
    CLI = "cli"
    REST = "rest"
    MCP = "mcp"
    WORKER = "worker"
    SCHEDULE = "schedule"
    TEST = "test"


class PrincipalRole(StrEnum):
    FOUNDER = "founder"
    WORKER = "worker"
    EXECUTIVE = "executive"
    MANAGER = "manager"
    SYSTEM = "system"
