"""Domain-level errors for the workflow engine."""

from __future__ import annotations


class FlowError(Exception):
    """Base error for engine operations."""

    code: str = "FLOW_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NotFoundError(FlowError):
    """Requested entity does not exist."""

    code = "NOT_FOUND"


class ConflictError(FlowError):
    """Compare-and-set or uniqueness conflict."""

    code = "CONFLICT_CAS"


class InvalidTransitionError(FlowError):
    """State transition is not permitted."""

    code = "VALIDATION_FAILED"


class AdvisoryConflictError(FlowError):
    """Advisory resource is held; claim rejected without force."""

    code = "CONFLICT_CAS"


class PrerequisiteError(FlowError):
    """Completion or transition blocked by unresolved prerequisites."""

    code = "GATE_OPEN"


class AuthRequiredError(FlowError):
    code = "AUTH_REQUIRED"


class AuthzDeniedError(FlowError):
    code = "AUTHZ_DENIED"


class IdempotencyReplayError(FlowError):
    """Same key with conflicting digest."""

    code = "IDEMPOTENCY_REPLAY"


class BudgetExhaustedError(FlowError):
    code = "BUDGET_EXHAUSTED"


class OutcomeUnknownError(FlowError):
    code = "OUTCOME_UNKNOWN"


class StaleAssetError(FlowError):
    code = "STALE_ASSET"


class UnsupportedSurfaceError(FlowError):
    code = "UNSUPPORTED_SURFACE"


class ValidationFailedError(FlowError):
    code = "VALIDATION_FAILED"


class PersistenceUnavailableError(FlowError):
    """Audit/anomaly persistence failed; mutations must stop."""

    code = "A0"
