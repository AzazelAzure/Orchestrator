"""Shared helpers for persistent local-stack scripts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

_RESET_BUDGET_PY = """
import sqlite3
from flow_engine.domain.models import new_id
from flow_engine.application.clock import utc_now_iso

conn = sqlite3.connect("/data/state.db")
conn.row_factory = sqlite3.Row
conn.execute(
    "UPDATE provider_invocations SET status='failed' WHERE status='reserved'"
)
rows = conn.execute(
    '''
    SELECT e.run_id, e.provider, e.attempt_id, e.invocation_id, e.units
    FROM credit_entries e
    WHERE e.kind = 'reservation'
      AND NOT EXISTS (
        SELECT 1 FROM credit_entries x
        WHERE x.invocation_id = e.invocation_id
          AND x.kind IN ('settlement', 'release')
      )
    '''
).fetchall()
now = utc_now_iso()
for row in rows:
    conn.execute(
        '''
        INSERT INTO credit_entries (
            id, run_id, provider, kind, units, attempt_id, invocation_id, created_at
        ) VALUES (?, ?, ?, 'release', ?, ?, ?, ?)
        ''',
        (
            new_id(),
            row["run_id"],
            row["provider"],
            int(row["units"]),
            row["attempt_id"],
            row["invocation_id"],
            now,
        ),
    )
conn.commit()
print("budget reset ok", len(rows))
"""


def _compose_exec_python(manifest: dict[str, Any], code: str) -> None:
    proc = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/r4d_compose.sh"),
            "exec",
            "-T",
            "coordinator",
            "python",
            "-c",
            code,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ORCH_R4D_ENV_FILE": manifest["env_file"],
            "ORCH_COMPOSE_PROJECT": manifest.get("compose_project", "orch-local"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"coordinator exec failed: {(proc.stderr or proc.stdout)[:400]}")


def refresh_work_item(manifest: dict[str, Any]) -> str:
    """Seed a fresh work item and persist it to the stack manifest."""
    proc = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/r4d_compose.sh"),
            "exec",
            "-T",
            "coordinator",
            "python",
            "/app/scripts/r4d_seed_work.py",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ORCH_R4D_ENV_FILE": manifest["env_file"],
            "ORCH_COMPOSE_PROJECT": manifest.get("compose_project", "orch-local"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"refresh work item failed: {(proc.stderr or proc.stdout)[:800]}")
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    work_item_id = payload["work_item_id"]
    scope = f"local-stack-{work_item_id[-8:]}"
    manifest["work_item_id"] = work_item_id
    manifest["budget_scope_id"] = scope
    manifest_path = Path(
        os.environ.get("ORCH_LOCAL_STACK_MANIFEST", ROOT / ".tmp/local-stack/manifest.json")
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    env_path = Path(manifest["env_file"])
    lines = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("ORCH_LOCAL_BUDGET_SCOPE="):
            continue
        lines.append(line)
    lines.append(f"ORCH_LOCAL_BUDGET_SCOPE={scope}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    proc_reload = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/r4d_compose.sh"),
            "up",
            "-d",
            "api",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "ORCH_R4D_ENV_FILE": manifest["env_file"],
            "ORCH_COMPOSE_PROJECT": manifest.get("compose_project", "orch-local"),
            "ORCH_LOCAL_BUDGET_SCOPE": scope,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if proc_reload.returncode != 0:
        raise RuntimeError(f"reload api for budget scope failed: {(proc_reload.stderr or proc_reload.stdout)[:400]}")
    import urllib.request
    import time

    for _ in range(45):
        try:
            urllib.request.urlopen("http://127.0.0.1:8000/health/", timeout=2)
            break
        except Exception:
            time.sleep(2)
    else:
        raise RuntimeError("api did not become healthy after budget scope reload")
    return work_item_id


def reset_local_acceptance_budget(manifest: dict[str, Any]) -> None:
    """Release stuck codex reservations/credits so repeated local acceptance runs can proceed."""
    _compose_exec_python(manifest, _RESET_BUDGET_PY.strip())


def release_codex_concurrency(manifest: dict[str, Any]) -> None:
    """Backward-compatible alias for budget reset."""
    reset_local_acceptance_budget(manifest)
