"""Valid state transitions for mutable domain entities."""

from __future__ import annotations

from flow_engine.domain.errors import InvalidTransitionError
from flow_engine.domain.states import (
    AttemptStatus,
    FindingStatus,
    GateStatus,
    RunStatus,
    WorkItemStatus,
)

FINDING_TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN: {FindingStatus.TRIAGED, FindingStatus.RESOLVED, FindingStatus.ACCEPTED},
    FindingStatus.TRIAGED: {FindingStatus.RESOLVED, FindingStatus.ACCEPTED, FindingStatus.REOPENED},
    FindingStatus.RESOLVED: {FindingStatus.REOPENED, FindingStatus.ACCEPTED},
    FindingStatus.ACCEPTED: {FindingStatus.REOPENED},
    FindingStatus.REOPENED: {FindingStatus.TRIAGED, FindingStatus.RESOLVED, FindingStatus.ACCEPTED},
}

# Legacy four-state edges remain; R2 expands claimed/paused/unknown paths.
WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, set[WorkItemStatus]] = {
    WorkItemStatus.PENDING: {WorkItemStatus.CLAIMED, WorkItemStatus.CANCELLED},
    WorkItemStatus.CLAIMED: {
        WorkItemStatus.COMPLETE,
        WorkItemStatus.FAILED,
        WorkItemStatus.PAUSED,
        WorkItemStatus.CANCELLED,
        WorkItemStatus.OUTCOME_UNKNOWN,
    },
    WorkItemStatus.PAUSED: {WorkItemStatus.CLAIMED, WorkItemStatus.CANCELLED},
    WorkItemStatus.FAILED: {WorkItemStatus.PENDING},
    WorkItemStatus.OUTCOME_UNKNOWN: {WorkItemStatus.RECONCILING},
    WorkItemStatus.RECONCILING: {
        WorkItemStatus.COMPLETE,
        WorkItemStatus.FAILED,
        WorkItemStatus.CANCELLED,
    },
    WorkItemStatus.COMPLETE: set(),
    WorkItemStatus.CANCELLED: set(),
}

RUN_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.PENDING: {RunStatus.CLAIMED, RunStatus.CANCELLED},
    RunStatus.CLAIMED: {
        RunStatus.COMPLETE,
        RunStatus.FAILED,
        RunStatus.PAUSED,
        RunStatus.CANCELLED,
        RunStatus.OUTCOME_UNKNOWN,
    },
    RunStatus.PAUSED: {RunStatus.CLAIMED, RunStatus.CANCELLED},
    RunStatus.FAILED: {RunStatus.PENDING},
    RunStatus.OUTCOME_UNKNOWN: {RunStatus.RECONCILING},
    RunStatus.RECONCILING: {
        RunStatus.COMPLETE,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.COMPLETE: set(),
    RunStatus.CANCELLED: set(),
}

ATTEMPT_TRANSITIONS: dict[AttemptStatus, set[AttemptStatus]] = {
    AttemptStatus.PENDING: {AttemptStatus.CLAIMED, AttemptStatus.CANCELLED},
    AttemptStatus.CLAIMED: {
        AttemptStatus.COMPLETE,
        AttemptStatus.FAILED,
        AttemptStatus.PAUSED,
        AttemptStatus.CANCELLED,
        AttemptStatus.OUTCOME_UNKNOWN,
    },
    AttemptStatus.PAUSED: {AttemptStatus.CLAIMED, AttemptStatus.CANCELLED},
    AttemptStatus.OUTCOME_UNKNOWN: {AttemptStatus.RECONCILING},
    AttemptStatus.RECONCILING: {
        AttemptStatus.COMPLETE,
        AttemptStatus.FAILED,
        AttemptStatus.CANCELLED,
    },
    AttemptStatus.COMPLETE: set(),
    AttemptStatus.FAILED: set(),
    AttemptStatus.CANCELLED: set(),
}

GATE_TRANSITIONS: dict[GateStatus, set[GateStatus]] = {
    GateStatus.OPEN: {GateStatus.PASSED, GateStatus.FAILED, GateStatus.WAIVED},
    GateStatus.PASSED: set(),
    GateStatus.FAILED: set(),
    GateStatus.WAIVED: set(),
}


def assert_work_transition(current: WorkItemStatus, target: WorkItemStatus) -> None:
    allowed = WORK_ITEM_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"cannot transition work item from {current} to {target}"
        )


def assert_run_transition(current: RunStatus, target: RunStatus) -> None:
    allowed = RUN_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(f"cannot transition run from {current} to {target}")


def assert_attempt_transition(current: AttemptStatus, target: AttemptStatus) -> None:
    allowed = ATTEMPT_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"cannot transition attempt from {current} to {target}"
        )


def assert_gate_transition(current: GateStatus, target: GateStatus) -> None:
    allowed = GATE_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"cannot transition gate from {current} to {target}"
        )


def assert_finding_transition(current: FindingStatus, target: FindingStatus) -> None:
    allowed = FINDING_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(
            f"cannot transition finding from {current} to {target}"
        )
