"""Tests for typed capability envelope."""

from __future__ import annotations

from flow_engine.capabilities.envelope import (
    CapabilityError,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    ResultCode,
    redact_evidence_refs,
)


def _request(**overrides) -> CapabilityRequest:
    base = {
        "capability": "repo_health",
        "request_id": "req-1",
        "actor": "actor:reviewer",
        "project_id": "demo_project",
    }
    base.update(overrides)
    return CapabilityRequest(**base)


def test_success_envelope_fields() -> None:
    request = _request()
    result = CapabilityResult.success(request, data={"branch": "main"})
    payload = result.to_dict()
    assert result.code == ResultCode.OK
    assert payload["request_id"] == "req-1"
    assert payload["project_id"] == "demo_project"
    assert payload["status"] == CapabilityStatus.READY.value
    assert "captured_at" in payload


def test_invalid_input_validation() -> None:
    request = _request(project_id="")
    error = request.validate()
    assert error is not None
    assert error.code == ResultCode.INVALID_INPUT
    result = CapabilityResult.failure(request, error)
    assert result.code == ResultCode.INVALID_INPUT


def test_unavailable_adapter_failure() -> None:
    request = _request(capability="open_prs")
    error = CapabilityError(ResultCode.UNAVAILABLE, "provider not configured")
    result = CapabilityResult.failure(request, error)
    assert result.status == CapabilityStatus.UNAVAILABLE
    assert result.error is not None
    assert result.error.code == ResultCode.UNAVAILABLE


def test_timeout_failure() -> None:
    request = _request(capability="ci_status")
    error = CapabilityError(ResultCode.TIMEOUT, "provider timed out")
    result = CapabilityResult.failure(request, error)
    assert result.code == ResultCode.TIMEOUT


def test_redaction_of_restricted_evidence() -> None:
    refs = redact_evidence_refs(
        [
            {"ref_id": "a", "kind": "artifact", "uri": "file:///secret", "sensitivity": "restricted"},
            {"ref_id": "b", "kind": "artifact", "uri": "file:///ok", "sensitivity": "internal"},
        ]
    )
    assert refs[0].redacted is True
    assert refs[0].uri is None
    assert refs[1].redacted is False
    assert refs[1].uri == "file:///ok"
