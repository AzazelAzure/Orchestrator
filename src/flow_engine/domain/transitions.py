"""Valid state transitions for mutable domain entities."""

from __future__ import annotations

from flow_engine.domain.errors import InvalidTransitionError
from flow_engine.domain.states import FindingStatus, GateStatus, WorkItemStatus

FINDING_TRANSITIONS: dict[FindingStatus, set[FindingStatus]] = {
    FindingStatus.OPEN: {FindingStatus.TRIAGED, FindingStatus.RESOLVED, FindingStatus.ACCEPTED},
    FindingStatus.TRIAGED: {FindingStatus.RESOLVED, FindingStatus.ACCEPTED, FindingStatus.REOPENED},
    FindingStatus.RESOLVED: {FindingStatus.REOPENED, FindingStatus.ACCEPTED},
    FindingStatus.ACCEPTED: {FindingStatus.REOPENED},
    FindingStatus.REOPENED: {FindingStatus.TRIAGED, FindingStatus.RESOLVED, FindingStatus.ACCEPTED},
}

WORK_ITEM_TRANSITIONS: dict[WorkItemStatus, set[WorkItemStatus]] = {
    WorkItemStatus.PENDING: {WorkItemStatus.CLAIMED},
    WorkItemStatus.CLAIMED: {WorkItemStatus.COMPLETE, WorkItemStatus.FAILED},
    WorkItemStatus.FAILED: {WorkItemStatus.PENDING},
    WorkItemStatus.COMPLETE: set(),
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
