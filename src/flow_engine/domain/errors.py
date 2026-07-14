"""Domain-level errors for the workflow engine."""


class FlowError(Exception):
    """Base error for engine operations."""


class NotFoundError(FlowError):
    """Requested entity does not exist."""


class ConflictError(FlowError):
    """Compare-and-set or uniqueness conflict."""


class InvalidTransitionError(FlowError):
    """State transition is not permitted."""


class AdvisoryConflictError(FlowError):
    """Advisory resource is held; claim rejected without force."""


class PrerequisiteError(FlowError):
    """Completion or transition blocked by unresolved prerequisites."""
