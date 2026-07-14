"""Shared CLI/MCP transport for read-only capabilities."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from flow_engine.capabilities.envelope import (
    CapabilityError,
    CapabilityRequest,
    CapabilityResult,
    ResultCode,
)
from flow_engine.capabilities.service import CapabilityService

CAPABILITY_REPO_HEALTH = "repo_health"
CAPABILITY_OPEN_PRS = "open_prs"
CAPABILITY_CI_STATUS = "ci_status"
CAPABILITY_WORK_LOOKUP = "work_lookup"
CAPABILITY_SESSION_BRIEF = "session_brief"

APPROVED_CAPABILITIES = frozenset(
    {
        CAPABILITY_REPO_HEALTH,
        CAPABILITY_OPEN_PRS,
        CAPABILITY_CI_STATUS,
        CAPABILITY_WORK_LOOKUP,
        CAPABILITY_SESSION_BRIEF,
    }
)

MCP_TOOL_REPO_HEALTH = "repo_health"
MCP_TOOL_OPEN_PRS = "open_prs"
MCP_TOOL_CI_STATUS = "ci_status"
MCP_TOOL_WORK_LOOKUP = "work_lookup"
MCP_TOOL_SESSION_BRIEF = "session_brief"

MCP_TOOL_TO_CAPABILITY = {
    MCP_TOOL_REPO_HEALTH: CAPABILITY_REPO_HEALTH,
    MCP_TOOL_OPEN_PRS: CAPABILITY_OPEN_PRS,
    MCP_TOOL_CI_STATUS: CAPABILITY_CI_STATUS,
    MCP_TOOL_WORK_LOOKUP: CAPABILITY_WORK_LOOKUP,
    MCP_TOOL_SESSION_BRIEF: CAPABILITY_SESSION_BRIEF,
}

MAX_TEXT_LENGTH = 16_000
MAX_LIST_ITEMS = 100
MAX_OBJECT_DEPTH = 8
MAX_INPUT_STRING_LENGTH = 256
DEFAULT_CAPABILITY_TIMEOUT_SEC = 10.0

FIELD_MAX_LENGTHS: dict[str, int] = {
    "project_id": 64,
    "actor": 128,
    "request_id": 64,
    "github_owner": 100,
    "github_repo": 100,
    "ref": 128,
    "work_id": 64,
    "logical_work_id": 128,
}

APPROVED_MCP_TOOL_NAMES = tuple(sorted(MCP_TOOL_TO_CAPABILITY))

_COMMON_PROPERTIES: dict[str, Any] = {
    "project_id": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["project_id"]},
    "actor": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["actor"]},
    "request_id": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["request_id"]},
}

TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    MCP_TOOL_REPO_HEALTH: {
        "type": "object",
        "properties": dict(_COMMON_PROPERTIES),
        "required": ["project_id", "actor"],
        "additionalProperties": False,
    },
    MCP_TOOL_OPEN_PRS: {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "github_owner": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["github_owner"]},
            "github_repo": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["github_repo"]},
        },
        "required": ["project_id", "actor", "github_owner", "github_repo"],
        "additionalProperties": False,
    },
    MCP_TOOL_CI_STATUS: {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "github_owner": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["github_owner"]},
            "github_repo": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["github_repo"]},
            "ref": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["ref"]},
        },
        "required": ["project_id", "actor", "github_owner", "github_repo", "ref"],
        "additionalProperties": False,
    },
    MCP_TOOL_WORK_LOOKUP: {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "work_id": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["work_id"]},
            "logical_work_id": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["logical_work_id"]},
        },
        "required": ["project_id", "actor"],
        "additionalProperties": False,
    },
    MCP_TOOL_SESSION_BRIEF: {
        "type": "object",
        "properties": {
            **_COMMON_PROPERTIES,
            "work_id": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["work_id"]},
            "logical_work_id": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["logical_work_id"]},
            "github_owner": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["github_owner"]},
            "github_repo": {"type": "string", "maxLength": FIELD_MAX_LENGTHS["github_repo"]},
        },
        "required": ["project_id", "actor"],
        "additionalProperties": False,
    },
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    MCP_TOOL_REPO_HEALTH: "Read-only repository health for a configured logical project.",
    MCP_TOOL_OPEN_PRS: "Read-only open pull request status via configured provider.",
    MCP_TOOL_CI_STATUS: "Read-only CI status for a repository ref via configured provider.",
    MCP_TOOL_WORK_LOOKUP: "Read-only engine work lookup by logical or engine work id.",
    MCP_TOOL_SESSION_BRIEF: "Read-only session brief projection for repository, work, and providers.",
}


def new_request_id(explicit: str | None = None) -> str:
    value = (explicit or "").strip()
    return value or f"req-{uuid.uuid4().hex[:12]}"


def build_request(
    capability: str,
    *,
    project_id: str,
    actor: str,
    request_id: str | None = None,
    params: dict[str, Any] | None = None,
) -> CapabilityRequest:
    capability_name = capability.strip()
    if capability_name not in APPROVED_CAPABILITIES:
        raise ValueError(f"unsupported capability: {capability_name}")
    request = CapabilityRequest(
        capability=capability_name,
        request_id=new_request_id(request_id),
        actor=actor.strip(),
        project_id=project_id.strip(),
        params=dict(params or {}),
    )
    return request


def build_request_from_tool(tool_name: str, arguments: dict[str, Any] | None) -> CapabilityRequest:
    if tool_name not in MCP_TOOL_TO_CAPABILITY:
        raise ValueError(f"unknown tool: {tool_name}")
    args = dict(arguments or {})
    schema = TOOL_INPUT_SCHEMAS[tool_name]
    _validate_against_schema(args, schema, tool_name)

    capability = MCP_TOOL_TO_CAPABILITY[tool_name]
    params = {key: value for key, value in args.items() if key not in {"project_id", "actor", "request_id"}}

    if capability == CAPABILITY_WORK_LOOKUP and not params.get("work_id") and not params.get("logical_work_id"):
        raise ValueError("work_id or logical_work_id is required")

    return build_request(
        capability,
        project_id=str(args["project_id"]),
        actor=str(args["actor"]),
        request_id=args.get("request_id"),
        params=params,
    )


def _validate_against_schema(args: dict[str, Any], schema: dict[str, Any], label: str) -> None:
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        allowed = set(properties)
        extra = set(args) - allowed
        if extra:
            raise ValueError(f"{label}: unsupported arguments: {', '.join(sorted(extra))}")

    for key in schema.get("required", []):
        value = args.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{label}: {key} is required")

    for key, value in args.items():
        prop_schema = properties.get(key)
        if prop_schema is None:
            continue
        _validate_property_value(label, key, value, prop_schema)


def _validate_property_value(label: str, key: str, value: Any, prop_schema: dict[str, Any]) -> None:
    expected_type = prop_schema.get("type")
    if expected_type == "string":
        if not isinstance(value, str):
            raise ValueError(f"{label}: {key} must be a string")
        if not value.strip() and key in {"project_id", "actor"}:
            raise ValueError(f"{label}: {key} is required")
        max_length = prop_schema.get("maxLength", FIELD_MAX_LENGTHS.get(key, MAX_INPUT_STRING_LENGTH))
        if len(value) > max_length:
            raise ValueError(f"{label}: {key} exceeds maximum length of {max_length}")
        return
    if expected_type == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"{label}: {key} must be an integer")
        return
    if expected_type == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(f"{label}: {key} must be a number")
        return
    if expected_type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{label}: {key} must be a boolean")
        return
    if expected_type == "array":
        if not isinstance(value, list):
            raise ValueError(f"{label}: {key} must be an array")
        return
    if expected_type == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{label}: {key} must be an object")


def dispatch_capability(service: CapabilityService, request: CapabilityRequest) -> CapabilityResult:
    validation = request.validate()
    if validation is not None:
        return CapabilityResult.failure(request, validation)

    handlers: dict[str, Callable[[CapabilityRequest], CapabilityResult]] = {
        CAPABILITY_REPO_HEALTH: service.repo_health,
        CAPABILITY_OPEN_PRS: service.open_prs,
        CAPABILITY_CI_STATUS: service.ci_status,
        CAPABILITY_WORK_LOOKUP: service.work_lookup,
        CAPABILITY_SESSION_BRIEF: service.session_brief,
    }
    handler = handlers[request.capability]
    try:
        return handler(request)
    except Exception as exc:
        return CapabilityResult.failure(
            request,
            CapabilityError(ResultCode.INTERNAL_ERROR, f"capability transport error: {exc}"),
        )


def dispatch_with_timeout(
    service: CapabilityService,
    request: CapabilityRequest,
    *,
    timeout_sec: float = DEFAULT_CAPABILITY_TIMEOUT_SEC,
) -> CapabilityResult:
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(dispatch_capability, service, request)
    try:
        return future.result(timeout=timeout_sec)
    except FuturesTimeoutError:
        return CapabilityResult.failure(
            request,
            CapabilityError(
                ResultCode.TIMEOUT,
                f"capability timed out after {timeout_sec}s",
            ),
        )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _bound_value(value: Any, *, depth: int = 0) -> Any:
    if depth >= MAX_OBJECT_DEPTH:
        return "<max-depth>"
    if isinstance(value, str):
        if len(value) <= MAX_TEXT_LENGTH:
            return value
        return value[:MAX_TEXT_LENGTH] + "…[truncated]"
    if isinstance(value, list):
        bounded = [_bound_value(item, depth=depth + 1) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            bounded.append({"_truncated_items": len(value) - MAX_LIST_ITEMS})
        return bounded
    if isinstance(value, dict):
        return {key: _bound_value(item, depth=depth + 1) for key, item in value.items()}
    return value


def bound_result(result: CapabilityResult) -> CapabilityResult:
    bounded_data = _bound_value(result.data)
    if not isinstance(bounded_data, dict):
        bounded_data = {"value": bounded_data}
    evidence_refs = tuple(
        type(ref)(
            ref_id=ref.ref_id,
            kind=ref.kind,
            uri=None if ref.redacted else ref.uri,
            sensitivity=ref.sensitivity,
            redacted=ref.redacted,
        )
        for ref in result.evidence_refs
    )
    return CapabilityResult(
        request_id=result.request_id,
        code=result.code,
        capability=result.capability,
        project_id=result.project_id,
        captured_at=result.captured_at,
        status=result.status,
        data=bounded_data,
        error=result.error,
        evidence_refs=evidence_refs,
        degraded_components=result.degraded_components,
    )


def serialize_result(result: CapabilityResult) -> dict[str, Any]:
    return bound_result(result).to_dict()


def capability_exit_code(result: CapabilityResult) -> int:
    return 0 if result.code == ResultCode.OK else 1
