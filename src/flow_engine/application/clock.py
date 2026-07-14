"""Injectable UTC clock for lease expiry evaluation in tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

_override: str | None = None


def set_clock(iso_timestamp: str) -> None:
    global _override
    _override = iso_timestamp


def clear_clock() -> None:
    global _override
    _override = None


def utc_now_iso() -> str:
    if _override is not None:
        return _override
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def utc_after_seconds(seconds: int, *, from_iso: str | None = None) -> str:
    base = datetime.fromisoformat(from_iso or utc_now_iso())
    if base.tzinfo is None:
        base = base.replace(tzinfo=UTC)
    return (base + timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def is_expired(expires_at: str, *, now_iso: str | None = None) -> bool:
    now = datetime.fromisoformat(now_iso or utc_now_iso())
    expiry = datetime.fromisoformat(expires_at)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=UTC)
    return now >= expiry
