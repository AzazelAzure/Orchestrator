"""Typed capability request/result/error envelope."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ResultCode(StrEnum):
    OK = "ok"
    INVALID_INPUT = "invalid_input"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    RESTRICTED = "restricted"
    INTERNAL_ERROR = "internal_error"


class CapabilityStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class EvidenceRef:
    """Sensitivity-safe evidence pointer."""

    ref_id: str
    kind: str
    uri: str | None = None
    sensitivity: str = "internal"
    redacted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_id": self.ref_id,
            "kind": self.kind,
            "uri": self.uri,
            "sensitivity": self.sensitivity,
            "redacted": self.redacted,
        }


@dataclass(frozen=True)
class CapabilityRequest:
    """Cross-cutting capability request fields."""

    capability: str
    request_id: str
    actor: str
    project_id: str
    role: str | None = None
    idempotency_key: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> CapabilityError | None:
        if not self.request_id.strip():
            return CapabilityError(ResultCode.INVALID_INPUT, "request_id is required")
        if not self.actor.strip():
            return CapabilityError(ResultCode.INVALID_INPUT, "actor is required")
        if not self.project_id.strip():
            return CapabilityError(ResultCode.INVALID_INPUT, "project_id is required")
        if not self.capability.strip():
            return CapabilityError(ResultCode.INVALID_INPUT, "capability is required")
        return None


@dataclass(frozen=True)
class CapabilityError:
    code: ResultCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code.value, "message": self.message, "details": self.details}


@dataclass(frozen=True)
class CapabilityResult:
    """Cross-cutting capability result fields."""

    request_id: str
    code: ResultCode
    capability: str
    project_id: str
    captured_at: str
    status: CapabilityStatus = CapabilityStatus.READY
    data: dict[str, Any] = field(default_factory=dict)
    error: CapabilityError | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()
    degraded_components: tuple[str, ...] = ()

    @staticmethod
    def capture_time() -> str:
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    @classmethod
    def success(
        cls,
        request: CapabilityRequest,
        *,
        data: dict[str, Any],
        status: CapabilityStatus = CapabilityStatus.READY,
        evidence_refs: tuple[EvidenceRef, ...] = (),
        degraded_components: tuple[str, ...] = (),
    ) -> CapabilityResult:
        return cls(
            request_id=request.request_id,
            code=ResultCode.OK,
            capability=request.capability,
            project_id=request.project_id,
            captured_at=cls.capture_time(),
            status=status,
            data=data,
            evidence_refs=evidence_refs,
            degraded_components=degraded_components,
        )

    @classmethod
    def failure(
        cls,
        request: CapabilityRequest,
        error: CapabilityError,
        *,
        status: CapabilityStatus = CapabilityStatus.UNAVAILABLE,
        data: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        return cls(
            request_id=request.request_id,
            code=error.code,
            capability=request.capability,
            project_id=request.project_id,
            captured_at=cls.capture_time(),
            status=status,
            data=data or {},
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "code": self.code.value,
            "capability": self.capability,
            "project_id": self.project_id,
            "captured_at": self.captured_at,
            "status": self.status.value,
            "data": self.data,
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "degraded_components": list(self.degraded_components),
        }
        if self.error is not None:
            payload["error"] = self.error.to_dict()
        return payload


def redact_evidence_refs(
    refs: list[dict[str, Any]],
    *,
    allowed_sensitivities: frozenset[str] | None = None,
) -> list[EvidenceRef]:
    """Convert raw evidence dicts to sensitivity-safe EvidenceRef objects."""
    allowed = allowed_sensitivities or frozenset({"public", "internal"})
    result: list[EvidenceRef] = []
    for index, raw in enumerate(refs):
        if not isinstance(raw, dict):
            continue
        sensitivity = str(raw.get("sensitivity", "internal"))
        redacted = sensitivity not in allowed
        result.append(
            EvidenceRef(
                ref_id=str(raw.get("ref_id", f"evidence-{index}")),
                kind=str(raw.get("kind", "artifact")),
                uri=None if redacted else raw.get("uri"),
                sensitivity=sensitivity,
                redacted=redacted,
            )
        )
    return result
