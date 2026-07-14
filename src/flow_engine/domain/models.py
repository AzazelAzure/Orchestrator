"""Domain identifiers and row-shape documentation."""

from __future__ import annotations

import os
import time

# Crockford's Base32 alphabet (no I, L, O, U).
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """Generate a time-sortable ULID string for primary keys."""
    timestamp_ms = int(time.time() * 1000)

    time_part = ""
    for _ in range(10):
        time_part = _CROCKFORD[timestamp_ms & 0x1F] + time_part
        timestamp_ms >>= 5

    random_int = int.from_bytes(os.urandom(10), byteorder="big")
    random_part = ""
    for _ in range(16):
        random_part = _CROCKFORD[random_int & 0x1F] + random_part
        random_int >>= 5

    return time_part + random_part
