"""Domain state enumerations for the workflow kernel."""

from __future__ import annotations

from enum import StrEnum


class WorkItemStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETE = "complete"
    FAILED = "failed"


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
