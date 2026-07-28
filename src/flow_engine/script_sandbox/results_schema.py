"""Exact typed evidence/finding/anomaly/follow-up schemas with bounds and redaction."""

from __future__ import annotations

import json
import re
from typing import Any

from flow_engine.domain.errors import UnsupportedSurfaceError, ValidationFailedError
from flow_engine.script_sandbox.effects import (
    ALLOWED_SCRIPT_EFFECTS,
    FORBIDDEN_SCRIPT_EFFECTS,
    assert_allowed_effects,
)
from flow_engine.script_sandbox.schemas import validate_against_schema

# Aggregate and per-field bounds.
MAX_SCRIPT_RESULTS = 32
MAX_BUCKET_ITEMS = 32
MAX_STRING_FIELD = 2048
MAX_SUMMARY = 2048
MAX_URI = 512
MAX_REDACTED_OUTPUT = 8192
MAX_NESTED_DEPTH = 6
MAX_AGGREGATE_JSON_BYTES = 65536
MAX_FIELD_JSON_BYTES = 8192

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret|credential)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[a-z0-9._\-]+"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(sk|rk|pk)-[a-z0-9]{16,}"),
)

_CLAIM_DENY = frozenset(
    {
        "remediation",
        "provider",
        "provider_call",
        "provider_calls",
        "policy",
        "policy_change",
        "gate",
        "gate_change",
        "repo_mutation",
        "repository_mutation",
        "merge",
        "deploy",
        "publication",
        "waiver",
        "hitm_exception",
        "repair",
        "secret_projection",
        "credential_projection",
        "paid_retry_after_unknown",
        "schedule_mutation",
    }
)

EVIDENCE_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "uri": {"type": "string", "maxLength": MAX_URI},
        "kind": {"type": "string", "maxLength": 64},
    },
}

FINDING_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "severity"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "severity": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "code": {"type": "string", "maxLength": 128},
    },
}

ANOMALY_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary", "class"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "class": {
            "type": "string",
            "enum": ["A0", "A1", "A2", "A3", "A4", "A5"],
        },
    },
}

FOLLOW_UP_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["summary"],
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "candidate_type": {"type": "string", "maxLength": 128},
    },
}

EFFECT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["type"],
    "additionalProperties": False,
    "properties": {
        "type": {
            "type": "string",
            "enum": sorted(ALLOWED_SCRIPT_EFFECTS),
        },
        "summary": {"type": "string", "maxLength": 1024},
        "severity": {
            "type": "string",
            "enum": ["low", "medium", "high", "critical"],
        },
        "uri": {"type": "string", "maxLength": MAX_URI},
    },
}

SCRIPT_RESULT_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["script_id", "status"],
    "additionalProperties": False,
    "properties": {
        "script_id": {"type": "string", "maxLength": 256},
        "status": {
            "type": "string",
            "enum": ["complete", "failed", "cancelled", "timeout", "rejected"],
        },
        "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        "effects": {
            "type": "array",
            "maxItems": MAX_BUCKET_ITEMS,
            "items": EFFECT_ITEM_SCHEMA,
        },
        "evidence": {
            "type": "array",
            "maxItems": MAX_BUCKET_ITEMS,
            "items": EVIDENCE_ITEM_SCHEMA,
        },
        "findings": {
            "type": "array",
            "maxItems": MAX_BUCKET_ITEMS,
            "items": FINDING_ITEM_SCHEMA,
        },
        "anomalies": {
            "type": "array",
            "maxItems": MAX_BUCKET_ITEMS,
            "items": ANOMALY_ITEM_SCHEMA,
        },
        "follow_ups": {
            "type": "array",
            "maxItems": MAX_BUCKET_ITEMS,
            "items": FOLLOW_UP_ITEM_SCHEMA,
        },
        "redacted_output": {"type": "string", "maxLength": MAX_REDACTED_OUTPUT},
    },
}

_BUCKET_SCHEMAS = {
    "evidence": EVIDENCE_ITEM_SCHEMA,
    "findings": FINDING_ITEM_SCHEMA,
    "anomalies": ANOMALY_ITEM_SCHEMA,
    "follow_ups": FOLLOW_UP_ITEM_SCHEMA,
}


def redact_text(value: str, *, max_len: int = MAX_REDACTED_OUTPUT) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted[:max_len]


def redact_failure_output(stdout: str = "", stderr: str = "") -> str:
    """Redact and bound failure/cap stdout/stderr before persistence."""
    combined = ""
    if stdout:
        combined += stdout
    if stderr:
        if combined:
            combined += "\n"
        combined += stderr
    return redact_text(combined, max_len=MAX_REDACTED_OUTPUT)


def _reject_forbidden_vocabulary(obj: Any, *, where: str, depth: int = 0) -> None:
    if depth > MAX_NESTED_DEPTH:
        raise ValidationFailedError(f"{where}: exceeds nested depth bound")
    if isinstance(obj, dict):
        for key, value in obj.items():
            lowered = str(key).lower()
            if lowered in _CLAIM_DENY or lowered in FORBIDDEN_SCRIPT_EFFECTS:
                raise UnsupportedSurfaceError(f"{where}: forbidden claim key {key}")
            if any(part == lowered or part in lowered for part in _CLAIM_DENY):
                # Exact key fragments that encode forbidden effect vocabulary.
                for part in FORBIDDEN_SCRIPT_EFFECTS | _CLAIM_DENY:
                    if part == lowered:
                        raise UnsupportedSurfaceError(
                            f"{where}: forbidden-effect vocabulary key {key}"
                        )
            _reject_forbidden_vocabulary(value, where=f"{where}.{key}", depth=depth + 1)
    elif isinstance(obj, list):
        if len(obj) > MAX_BUCKET_ITEMS:
            raise ValidationFailedError(f"{where}: exceeds item count bound")
        for index, item in enumerate(obj):
            _reject_forbidden_vocabulary(
                item, where=f"{where}[{index}]", depth=depth + 1
            )
    elif isinstance(obj, str):
        lowered = obj.lower()
        for claim in (
            "remediation",
            "repository_mutation",
            "provider_call",
            "policy_change",
            "gate_change",
            "secret_projection",
        ):
            if claim in lowered:
                raise UnsupportedSurfaceError(
                    f"{where}: forbidden-effect vocabulary in text"
                )


def _redact_tree(obj: Any, *, depth: int = 0) -> Any:
    if depth > MAX_NESTED_DEPTH:
        return "[REDACTED_DEPTH]"
    if isinstance(obj, str):
        return redact_text(obj, max_len=MAX_STRING_FIELD)
    if isinstance(obj, list):
        return [_redact_tree(item, depth=depth + 1) for item in obj[:MAX_BUCKET_ITEMS]]
    if isinstance(obj, dict):
        return {
            str(key)[:128]: _redact_tree(value, depth=depth + 1)
            for key, value in list(obj.items())[:MAX_BUCKET_ITEMS]
        }
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return str(obj)[:MAX_STRING_FIELD]


def _bounded_json_bytes(obj: Any, *, where: str, limit: int) -> None:
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > limit:
        raise ValidationFailedError(f"{where}: exceeds aggregate byte bound ({limit})")


def validate_and_redact_script_results(
    script_results: list[dict[str, Any]] | None,
    *,
    max_items: int = MAX_SCRIPT_RESULTS,
) -> list[dict[str, Any]]:
    if script_results is None:
        return []
    if not isinstance(script_results, list):
        raise ValidationFailedError("script_results must be an array")
    if len(script_results) > max_items:
        raise ValidationFailedError("script_results exceeds bound")

    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(script_results):
        where = f"script_results[{index}]"
        if not isinstance(item, dict):
            raise ValidationFailedError(f"{where} must be an object")
        _reject_forbidden_vocabulary(item, where=where)
        _bounded_json_bytes(item, where=where, limit=MAX_FIELD_JSON_BYTES)
        validate_against_schema(item, SCRIPT_RESULT_ITEM_SCHEMA, where=where)

        effects = item.get("effects")
        if effects is not None:
            assert_allowed_effects(effects)
            for effect in effects:
                et = str(effect.get("type") or "")
                if et in FORBIDDEN_SCRIPT_EFFECTS:
                    raise UnsupportedSurfaceError(f"{where} forbidden effect {et}")

        out: dict[str, Any] = {
            "script_id": str(item["script_id"]),
            "status": str(item["status"]),
        }
        if "summary" in item:
            out["summary"] = redact_text(str(item["summary"]), max_len=MAX_SUMMARY)
        if "redacted_output" in item:
            out["redacted_output"] = redact_text(
                str(item["redacted_output"]), max_len=MAX_REDACTED_OUTPUT
            )
        if effects is not None:
            out["effects"] = []
            for effect in effects:
                cleaned_effect = {"type": effect["type"]}
                if "summary" in effect:
                    cleaned_effect["summary"] = redact_text(
                        str(effect["summary"]), max_len=1024
                    )
                if "severity" in effect:
                    cleaned_effect["severity"] = effect["severity"]
                if "uri" in effect:
                    cleaned_effect["uri"] = redact_text(
                        str(effect["uri"]), max_len=MAX_URI
                    )
                out["effects"].append(cleaned_effect)

        for bucket, schema in _BUCKET_SCHEMAS.items():
            if bucket not in item:
                continue
            values = item[bucket]
            if not isinstance(values, list):
                raise ValidationFailedError(f"{where}.{bucket} must be an array")
            if len(values) > MAX_BUCKET_ITEMS:
                raise ValidationFailedError(f"{where}.{bucket} exceeds count bound")
            cleaned_bucket: list[dict[str, Any]] = []
            for b_index, entry in enumerate(values):
                b_where = f"{where}.{bucket}[{b_index}]"
                validate_against_schema(entry, schema, where=b_where)
                _bounded_json_bytes(entry, where=b_where, limit=MAX_FIELD_JSON_BYTES)
                cleaned_bucket.append(_redact_tree(entry))
            out[bucket] = cleaned_bucket

        cleaned.append(out)

    _bounded_json_bytes(
        cleaned, where="script_results", limit=MAX_AGGREGATE_JSON_BYTES
    )
    return cleaned
