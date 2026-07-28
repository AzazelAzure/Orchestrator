"""Typed command vocabulary for the sole-writer state coordinator."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from flow_engine.domain.states import PrincipalRole, Surface


def stable_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StepUpEvidence:
    """Founder step-up proof for waiver / retry-after-unknown."""

    reauthenticated_at: str
    reason: str
    evidence: str
    duplicate_cost_warning_ack: bool
    policy_revision: str
    new_idempotency_identity: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SystemTestGrant:
    """Explicit R2 compatibility grant. Loadout/org resolution is refused.

    Marked compatibility path for tests and R2-only flows. Consequential R3
    dispatch must use ResolvedTaskGrant instead.
    """

    grant_id: str
    principal_id: str
    role: PrincipalRole
    surfaces: tuple[Surface, ...]
    providers: tuple[str, ...]
    budget_scope_id: str
    capabilities: tuple[str, ...] = ()
    policy_revision: str = "system-test"
    compatibility_mode: str = "r2_system_test"

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "principal_id": self.principal_id,
            "role": str(self.role),
            "surfaces": [str(s) for s in self.surfaces],
            "providers": list(self.providers),
            "budget_scope_id": self.budget_scope_id,
            "capabilities": list(self.capabilities),
            "policy_revision": self.policy_revision,
            "compatibility_mode": self.compatibility_mode,
            "loadout_id": None,
            "organization_profile_id": None,
        }


@dataclass(frozen=True)
class ResolvedTaskGrant:
    """R3 grant backed by a pinned resolved-loadout snapshot."""

    grant_id: str
    principal_id: str
    role: PrincipalRole
    surfaces: tuple[Surface, ...]
    providers: tuple[str, ...]
    budget_scope_id: str
    organization_id: str
    organization_profile_hash: str
    loadout_id: str
    snapshot_id: str
    assignment_id: str
    capabilities: tuple[str, ...] = ()
    policy_revision: str = "r3-default"
    effect_ceiling: str = ""
    compatibility_mode: str = "r3_resolved"

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "principal_id": self.principal_id,
            "role": str(self.role),
            "surfaces": [str(s) for s in self.surfaces],
            "providers": list(self.providers),
            "budget_scope_id": self.budget_scope_id,
            "organization_id": self.organization_id,
            "organization_profile_hash": self.organization_profile_hash,
            "loadout_id": self.loadout_id,
            "snapshot_id": self.snapshot_id,
            "assignment_id": self.assignment_id,
            "capabilities": list(self.capabilities),
            "policy_revision": self.policy_revision,
            "effect_ceiling": self.effect_ceiling,
            "compatibility_mode": self.compatibility_mode,
        }


Grant = SystemTestGrant | ResolvedTaskGrant


@dataclass(frozen=True)
class CommandContext:
    principal_id: str
    role: PrincipalRole
    surface: Surface
    grant: Grant | None = None
    step_up: StepUpEvidence | None = None
    attempt_id: str | None = None
    provider_invocation_id: str | None = None
    expected_revision: int | None = None
    # R4B: preserve MCP service identity alongside initiating principal.
    # Immutable tool-snapshot identity travels on context — never trust payload.
    mcp_service_principal_id: str | None = None
    mcp_lane_id: str | None = None
    mcp_tool_snapshot_digest: str | None = None
    mcp_tool_name: str | None = None


FOUNDER_ONLY_COMMANDS = frozenset(
    {
        "runtime.new_attempt_after_unknown",
        "runtime.waive_gate",
        "runtime.hitm_exception",
    }
)

MCP_FORBIDDEN_COMMANDS = frozenset(
    {
        "runtime.new_attempt_after_unknown",
        "runtime.waive_gate",
        "runtime.hitm_exception",
    }
)


@dataclass(frozen=True)
class RuntimeCommand:
    command_type: str
    target_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None
    context: CommandContext = field(
        default_factory=lambda: CommandContext(
            principal_id="anonymous",
            role=PrincipalRole.WORKER,
            surface=Surface.CLI,
        )
    )

    @property
    def request_digest(self) -> str:
        return stable_digest(
            {
                "command_type": self.command_type,
                "target_id": self.target_id,
                "payload": self.payload,
            }
        )

    @property
    def idempotency_scope(self) -> str:
        ctx = self.context
        if self.idempotency_key is not None:
            return (
                f"{ctx.principal_id}|{self.command_type}|{self.target_id or ''}|"
                f"{self.idempotency_key}"
            )
        identity = (
            ctx.step_up.new_idempotency_identity
            if ctx.step_up is not None
            else f"{ctx.principal_id}|{self.command_type}|{self.target_id or ''}|"
            f"{self.request_digest}|{ctx.attempt_id or ''}|"
            f"{ctx.provider_invocation_id or ''}"
        )
        if ctx.step_up is not None:
            return (
                f"{ctx.principal_id}|{self.command_type}|{self.target_id or ''}|"
                f"{self.request_digest}|{ctx.attempt_id or ''}|"
                f"{ctx.provider_invocation_id or ''}|{identity}"
            )
        return identity
