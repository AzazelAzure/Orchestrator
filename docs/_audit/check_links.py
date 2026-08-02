#!/usr/bin/env python3
"""Check relative markdown links under docs/. Exit 1 on broken targets."""

from __future__ import annotations

import re
import sys
from pathlib import Path

DOCS_ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"\]\(([^)]+)\)")


def check_file(md_path: Path) -> list[str]:
    errors: list[str] = []
    text = md_path.read_text(encoding="utf-8")
    for match in LINK_RE.finditer(text):
        target = match.group(1).strip()
        if not target or target.startswith(("http://", "https://", "mailto:")):
            continue
        if target.startswith("#"):
            continue
        path_part = target.split("#", 1)[0]
        if not path_part:
            continue
        resolved = (md_path.parent / path_part).resolve()
        if not resolved.exists():
            errors.append(f"{md_path.relative_to(DOCS_ROOT)}: broken link -> {target}")
    return errors


def main() -> int:
    all_errors: list[str] = []
    for md in sorted(DOCS_ROOT.rglob("*.md")):
        all_errors.extend(check_file(md))
    if all_errors:
        for err in all_errors:
            print(err, file=sys.stderr)
        return 1
    print(f"OK: {len(list(DOCS_ROOT.rglob('*.md')))} markdown files, no broken relative links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
