"""CLI integration tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "flow_engine.cli.app", "--db", str(db_path), "--json", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_init_status_and_workflow(tmp_path) -> None:
    db_path = tmp_path / "state.db"

    init = _run(db_path, "init", "--project", "demo", "--queue", "api")
    assert init.returncode == 0
    init_data = json.loads(init.stdout)
    assert init_data["project"]["name"] == "demo"

    status = _run(db_path, "status")
    assert status.returncode == 0
    status_data = json.loads(status.stdout)
    assert status_data["project"]["name"] == "demo"

    submit = _run(
        db_path,
        "work",
        "submit",
        "--queue",
        "api",
        "--payload",
        '{"task":"build"}',
        "--actor",
        "agent-1",
    )
    assert submit.returncode == 0
    work = json.loads(submit.stdout)
    work_id = work["id"]

    claim = _run(db_path, "work", "claim", work_id, "--actor", "agent-1")
    assert claim.returncode == 0
    assert json.loads(claim.stdout)["status"] == "claimed"

    complete = _run(db_path, "work", "complete", work_id, "--actor", "agent-1")
    assert complete.returncode == 0
    assert json.loads(complete.stdout)["status"] == "complete"

    export = _run(db_path, "export")
    assert export.returncode == 0
    snapshot = json.loads(export.stdout)
    assert len(snapshot["work_items"]) == 1


def test_cli_resource_advisory_exit_code(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    _run(db_path, "init")

    first = _run(
        db_path,
        "resource",
        "claim",
        "ws1",
        "--holder",
        "agent-1",
        "--policy",
        "advisory",
    )
    assert first.returncode == 0

    second = _run(
        db_path,
        "resource",
        "claim",
        "ws1",
        "--holder",
        "agent-2",
        "--policy",
        "advisory",
    )
    assert second.returncode == 1
    assert "force" in second.stderr.lower()

    forced = _run(
        db_path,
        "resource",
        "claim",
        "ws1",
        "--holder",
        "agent-2",
        "--policy",
        "advisory",
        "--force",
        "--reason",
        "override",
    )
    assert forced.returncode == 0
    assert json.loads(forced.stdout)["lease"]["holder"] == "agent-2"


def test_cli_idempotency(tmp_path) -> None:
    db_path = tmp_path / "state.db"
    _run(db_path, "init")

    key = "idem-123"
    first = _run(
        db_path,
        "work",
        "submit",
        "--queue",
        "default",
        "--payload",
        "{}",
        "--idempotency-key",
        key,
    )
    second = _run(
        db_path,
        "work",
        "submit",
        "--queue",
        "default",
        "--payload",
        '{"other":true}',
        "--idempotency-key",
        key,
    )
    assert first.returncode == 0
    assert second.returncode == 0
    assert json.loads(first.stdout)["id"] == json.loads(second.stdout)["id"]
    assert json.loads(second.stdout)["from_cache"] is True
