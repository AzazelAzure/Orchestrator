"""Generic provider-runner protocol and replay-safe mock adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class InvocationRequest:
    invocation_id: str
    attempt_id: str
    run_id: str
    provider: str
    payload: dict[str, Any]
    cwd_policy: str = "workspace-root"
    timeout_sec: int = 1800
    env_allowlist: tuple[str, ...] = ()


@dataclass(frozen=True)
class PreparedCall:
    invocation_id: str
    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    timeout_sec: int
    env_allowlist: tuple[str, ...] = ()
    heartbeat_interval_sec: int = 60


@dataclass(frozen=True)
class DeliveryHandle:
    invocation_id: str
    provider: str
    delivered: bool
    delivery_id: str

    @property
    def mock_token(self) -> str:
        """Compatibility alias; new code uses provider-neutral delivery_id."""
        return self.delivery_id


@dataclass(frozen=True)
class HeartbeatResult:
    invocation_id: str
    alive: bool
    detail: str = ""


@dataclass(frozen=True)
class ProviderResult:
    invocation_id: str
    outcome: str  # complete | failed | outcome_unknown
    evidence: dict[str, Any] = field(default_factory=dict)
    anomalies: list[dict[str, Any]] = field(default_factory=list)
    redacted_output: str = ""


@dataclass(frozen=True)
class ReconcileResult:
    invocation_id: str
    outcome: str
    evidence: dict[str, Any] = field(default_factory=dict)


class ProviderRunner(Protocol):
    name: str

    def prepare(self, request: InvocationRequest) -> PreparedCall: ...

    def deliver(self, prepared: PreparedCall) -> DeliveryHandle: ...

    def heartbeat(self, handle: DeliveryHandle) -> HeartbeatResult: ...

    def collect(self, handle: DeliveryHandle) -> ProviderResult: ...

    def reconcile(self, invocation_id: str) -> ReconcileResult: ...


class MockProviderRunner:
    """In-process mock adapter satisfying the runner protocol."""

    def __init__(
        self,
        name: str,
        *,
        default_outcome: str = "complete",
        fail_deliver: bool = False,
        unknown_on_timeout: bool = True,
    ) -> None:
        self.name = name
        self.default_outcome = default_outcome
        self.fail_deliver = fail_deliver
        self.unknown_on_timeout = unknown_on_timeout
        self._store: dict[str, dict[str, Any]] = {}
        self.delivery_count = 0

    def prepare(self, request: InvocationRequest) -> PreparedCall:
        return PreparedCall(
            invocation_id=request.invocation_id,
            argv=(f"mock-{self.name}", "--invocation", request.invocation_id),
            env={},
            cwd=request.cwd_policy,
            timeout_sec=request.timeout_sec,
            env_allowlist=request.env_allowlist,
        )

    def deliver(self, prepared: PreparedCall) -> DeliveryHandle:
        self.delivery_count += 1
        if self.fail_deliver:
            raise RuntimeError(f"mock deliver failed for {self.name}")
        token = f"mock:{self.name}:{prepared.invocation_id}"
        self._store[prepared.invocation_id] = {
            "outcome": self.default_outcome,
            "delivered": True,
            "token": token,
        }
        return DeliveryHandle(
            invocation_id=prepared.invocation_id,
            provider=self.name,
            delivered=True,
            delivery_id=token,
        )

    def heartbeat(self, handle: DeliveryHandle) -> HeartbeatResult:
        alive = handle.invocation_id in self._store
        return HeartbeatResult(invocation_id=handle.invocation_id, alive=alive)

    def collect(self, handle: DeliveryHandle) -> ProviderResult:
        record = self._store.get(handle.invocation_id, {})
        outcome = record.get("outcome", self.default_outcome)
        return ProviderResult(
            invocation_id=handle.invocation_id,
            outcome=outcome,
            evidence={"mock": True, "provider": self.name},
            anomalies=[],
            redacted_output=f"mock-{self.name}-ok",
        )

    def reconcile(self, invocation_id: str) -> ReconcileResult:
        record = self._store.get(invocation_id)
        if record is None:
            return ReconcileResult(
                invocation_id=invocation_id,
                outcome="failed",
                evidence={"mock": True, "found": False},
            )
        return ReconcileResult(
            invocation_id=invocation_id,
            outcome=record.get("outcome", "complete"),
            evidence={"mock": True, "found": True, "provider": self.name},
        )

    def force_outcome(self, invocation_id: str, outcome: str) -> None:
        if invocation_id in self._store:
            self._store[invocation_id]["outcome"] = outcome


def default_mock_registry() -> dict[str, MockProviderRunner]:
    return {
        "codex": MockProviderRunner("codex"),
        "cursor": MockProviderRunner("cursor"),
        "claude": MockProviderRunner("claude"),
    }
