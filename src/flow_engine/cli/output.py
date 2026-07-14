"""CLI output formatting — stdout is result-only; errors go to stderr."""

from __future__ import annotations

import json
import sys
from typing import Any


def emit_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def emit_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def fmt_row(values: list[str]) -> str:
        return "  ".join(value.ljust(widths[index]) for index, value in enumerate(values))

    sys.stdout.write(fmt_row(headers) + "\n")
    sys.stdout.write(fmt_row(["-" * width for width in widths]) + "\n")
    for row in rows:
        sys.stdout.write(fmt_row(row) + "\n")


def emit_result(data: Any, *, as_json: bool) -> None:
    if as_json:
        emit_json(data)
        return

    if isinstance(data, list):
        if not data:
            return
        if isinstance(data[0], dict):
            headers = list(data[0].keys())
            rows = [[_stringify(item.get(header, "")) for header in headers] for item in data]
            emit_table(headers, rows)
            return

    if isinstance(data, dict):
        emit_table(["key", "value"], [[key, _stringify(value)] for key, value in data.items()])
        return

    sys.stdout.write(f"{data}\n")


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    if value is None:
        return ""
    return str(value)
