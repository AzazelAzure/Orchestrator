"""R2 credit and concurrency envelopes."""

from __future__ import annotations

from dataclasses import dataclass

# Timing (seconds)
HEARTBEAT_INTERVAL_SEC = 60
INACTIVITY_THRESHOLD_SEC = 5 * 60
HARD_ATTEMPT_TIMEOUT_SEC = 30 * 60
STEP_UP_MAX_AGE_SEC = 5 * 60

# Concurrency envelopes
GLOBAL_PROVIDER_CONCURRENCY = 3
PER_PROVIDER_CONCURRENCY = 1
PER_PROJECT_CONCURRENCY = 3
PER_RUN_CONCURRENCY = 2
PER_ATTEMPT_PROVIDER_CALLS = 1

# Acceptance-run credit budget
ACCEPTANCE_CREDIT_TOTAL = 9
ACCEPTANCE_CREDIT_PER_PROVIDER = 3

ACTIVE_RUN_STATUSES = frozenset({"claimed", "reconciling"})
ACTIVE_ATTEMPT_STATUSES = frozenset({"claimed", "reconciling"})
ACTIVE_INVOCATION_STATUSES = frozenset({"reserved", "dispatched", "outcome_unknown"})


@dataclass(frozen=True)
class CreditEnvelope:
    total: int = ACCEPTANCE_CREDIT_TOTAL
    per_provider: int = ACCEPTANCE_CREDIT_PER_PROVIDER


DEFAULT_CREDIT_ENVELOPE = CreditEnvelope()
