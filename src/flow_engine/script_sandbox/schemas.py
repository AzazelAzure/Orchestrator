"""Minimal JSON Schema subset validation for script I/O (no external deps)."""

from __future__ import annotations

from typing import Any

from flow_engine.domain.errors import ValidationFailedError


def validate_against_schema(instance: Any, schema: dict[str, Any], *, where: str) -> None:
    """Validate a JSON-like value against a constrained subset of JSON Schema draft."""
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(instance, dict):
            raise ValidationFailedError(f"{where}: expected object")
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        additional = schema.get("additionalProperties", True)
        for key in required:
            if key not in instance:
                raise ValidationFailedError(f"{where}: missing required property {key}")
        for key, value in instance.items():
            if key in props:
                validate_against_schema(value, props[key], where=f"{where}.{key}")
            elif additional is False:
                raise ValidationFailedError(f"{where}: unexpected property {key}")
            elif isinstance(additional, dict):
                validate_against_schema(value, additional, where=f"{where}.{key}")
        return
    if expected_type == "array":
        if not isinstance(instance, list):
            raise ValidationFailedError(f"{where}: expected array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                validate_against_schema(item, item_schema, where=f"{where}[{index}]")
        max_items = schema.get("maxItems")
        if max_items is not None and len(instance) > int(max_items):
            raise ValidationFailedError(f"{where}: array exceeds maxItems")
        return
    if expected_type == "string":
        if not isinstance(instance, str):
            raise ValidationFailedError(f"{where}: expected string")
        enum = schema.get("enum")
        if enum is not None and instance not in enum:
            raise ValidationFailedError(f"{where}: value not in enum")
        max_len = schema.get("maxLength")
        if max_len is not None and len(instance) > int(max_len):
            raise ValidationFailedError(f"{where}: string exceeds maxLength")
        return
    if expected_type == "integer":
        if not isinstance(instance, int) or isinstance(instance, bool):
            raise ValidationFailedError(f"{where}: expected integer")
        return
    if expected_type == "number":
        if not isinstance(instance, (int, float)) or isinstance(instance, bool):
            raise ValidationFailedError(f"{where}: expected number")
        return
    if expected_type == "boolean":
        if not isinstance(instance, bool):
            raise ValidationFailedError(f"{where}: expected boolean")
        return
    if expected_type == "null":
        if instance is not None:
            raise ValidationFailedError(f"{where}: expected null")
        return
    # No type constraint: accept.
