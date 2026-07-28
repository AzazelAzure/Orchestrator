#!/usr/bin/env python3
"""Seed project/queue/work inside the coordinator container (sole SQLite writer).

Intended invocation (Compose):
  orch_compose exec -T coordinator python /app/scripts/r4d_seed_work.py

Prints a single JSON object with work_item_id. No secrets.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Container image layout uses /app; local src layout uses repo/src.
for candidate in (Path("/app/src"), Path(__file__).resolve().parents[1] / "src"):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
        break

from flow_engine.application import ensure_queue, init_project, submit_work  # noqa: E402
from flow_engine.persistence import Kernel  # noqa: E402
from flow_engine.persistence.transactions import transaction  # noqa: E402


def main() -> int:
    db_path = Path(os.environ.get("FLOW_DB_PATH", "/data/state.db"))
    kernel = Kernel.init(db_path)
    try:
        with transaction(kernel.connection):
            existing = kernel.connection.execute(
                "SELECT id FROM projects ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if existing is None:
                init_project(kernel.connection, name="r4d-active-test", actor="r4d-seed")
            ensure_queue(kernel.connection, name="default")
            item = submit_work(
                kernel.connection,
                queue_name="default",
                payload={"purpose": "r4d-active-test", "provider": "mock"},
                actor="r4d-seed",
            )
        print(json.dumps({"status": "ok", "work_item_id": item["id"]}, sort_keys=True))
        return 0
    finally:
        kernel.close()


if __name__ == "__main__":
    raise SystemExit(main())
