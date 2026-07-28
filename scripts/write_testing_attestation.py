#!/usr/bin/env python3
"""Write ORCH_TESTING script-runner attestation fixture (no Docker)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["ORCH_TESTING"] = "1"

from flow_engine.script_sandbox.attestation import ensure_testing_attestation_file  # noqa: E402


def main() -> None:
    path = ensure_testing_attestation_file()
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
