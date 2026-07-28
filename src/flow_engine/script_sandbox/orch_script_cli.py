#!/usr/bin/env python3
"""Pinned argv-only orch-script CLI (generic allowlist entrypoint).

Installed only into the script-worker image as /usr/local/bin/orch-script.
Never projects secrets; emits bounded JSON on stdout.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load_input(argv: list[str]) -> dict[str, Any]:
    if "--json-in" in argv:
        idx = argv.index("--json-in")
        if idx + 1 >= len(argv):
            raise SystemExit("missing --json-in path")
        path = Path(argv[idx + 1])
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(json.dumps({"error": "script short-name required"}), file=sys.stderr)
        return 2
    short = args[0]
    script_id = f"script.generic.{short}"
    payload = _load_input(args)
    dry_run = bool(payload.get("dry_run", True))
    effects: list[dict[str, Any]] = [
        {
            "type": "evidence",
            "summary": f"{script_id} completed",
            "uri": f"orch://script/{script_id}/evidence",
        }
    ]
    output = {
        "script_id": script_id,
        "status": "complete",
        "summary": f"{short} ok" + (" (dry_run)" if dry_run else ""),
        "effects": effects,
        "redacted_output": f"ok:{short}",
    }
    sys.stdout.write(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
