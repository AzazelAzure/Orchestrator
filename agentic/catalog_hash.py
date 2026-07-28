"""Canonical content hashing for R1 inert catalogs."""

from __future__ import annotations

import hashlib
import json
from typing import Any

HASH_EXCLUDE_KEYS = frozenset({"content_sha256", "captured_at", "generated_at"})


def canonicalize(value: Any) -> Any:
    """Return a JSON-serializable structure with stable key order for hashing."""
    if isinstance(value, dict):
        return {
            str(key): canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if key not in HASH_EXCLUDE_KEYS
        }
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    if isinstance(value, tuple):
        return [canonicalize(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported catalog value type: {type(value)!r}")


def content_sha256(payload: Any) -> str:
    """SHA-256 hex digest of canonical JSON (hash/timestamp fields omitted)."""
    canonical = canonicalize(payload)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def attach_content_hash(record: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *record* with content_sha256 filled from payload."""
    out = dict(record)
    out.pop("content_sha256", None)
    digest = content_sha256(out)
    out["content_sha256"] = digest
    return out
