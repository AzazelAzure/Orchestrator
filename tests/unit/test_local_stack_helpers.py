"""Manifest isolation for scripts.local_stack_helpers.refresh_work_item.

ORCH-LI (LI-1): each concurrent caller must supply the same explicit path for
both the in-memory manifest it loaded and the path refresh_work_item persists
to, so two concurrent slices holding distinct manifests never collide on one
file. No fcntl / slice-identity heuristic is required (see conference close
record, ORCH-LI acceptance item 1).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.local_stack_helpers import refresh_work_item


def _seed_completed(work_item_id: str) -> MagicMock:
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = json.dumps({"work_item_id": work_item_id}) + "\n"
    completed.stderr = ""
    return completed


def _reload_completed() -> MagicMock:
    completed = MagicMock()
    completed.returncode = 0
    completed.stdout = ""
    completed.stderr = ""
    return completed


def _refresh_with_mocks(manifest: dict, manifest_path: Path, work_item_id: str) -> None:
    with (
        patch("scripts.local_stack_helpers.subprocess.run") as run_mock,
        patch("urllib.request.urlopen") as urlopen_mock,
    ):
        run_mock.side_effect = [_seed_completed(work_item_id), _reload_completed()]
        urlopen_mock.return_value.__enter__.return_value = MagicMock()
        refresh_work_item(manifest, manifest_path=manifest_path)


def test_refresh_work_item_persists_to_explicit_path_not_default_env(tmp_path: Path) -> None:
    """The explicit manifest_path argument is authoritative, not the ambient env var."""
    manifest_path = tmp_path / "manifest-explicit.json"
    env_file = tmp_path / "stack.env"
    env_file.write_text("ORCH_TOKEN_FOUNDER=token\n", encoding="utf-8")
    manifest = {
        "env_file": str(env_file),
        "work_item_id": "old-item",
        "budget_scope_id": "old-scope",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    # A stale/irrelevant default-looking env var must not redirect the write.
    with patch.dict(
        "os.environ", {"ORCH_LOCAL_STACK_MANIFEST": str(tmp_path / "should-not-be-used.json")}
    ):
        _refresh_with_mocks(manifest, manifest_path, "work-item-new")

    assert not (tmp_path / "should-not-be-used.json").exists()
    persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert persisted["work_item_id"] == "work-item-new"


def test_refresh_work_item_isolates_two_concurrent_manifests(tmp_path: Path) -> None:
    """Refreshing manifest A must not alter manifest B's work_item_id or budget_scope_id."""
    manifest_a_path = tmp_path / "manifest-a.json"
    manifest_b_path = tmp_path / "manifest-b.json"
    env_a = tmp_path / "a.env"
    env_b = tmp_path / "b.env"
    env_a.write_text("ORCH_TOKEN_FOUNDER=token-a\n", encoding="utf-8")
    env_b.write_text("ORCH_TOKEN_FOUNDER=token-b\n", encoding="utf-8")

    manifest_a = {"env_file": str(env_a), "work_item_id": "old-a", "budget_scope_id": "old-scope-a"}
    manifest_b = {"env_file": str(env_b), "work_item_id": "old-b", "budget_scope_id": "old-scope-b"}
    manifest_a_path.write_text(json.dumps(manifest_a), encoding="utf-8")
    manifest_b_path.write_text(json.dumps(manifest_b), encoding="utf-8")

    # Only slice A refreshes.
    _refresh_with_mocks(manifest_a, manifest_a_path, "work-item-a-new")

    # Manifest A reflects the refresh both on disk and in-memory.
    persisted_a = json.loads(manifest_a_path.read_text(encoding="utf-8"))
    assert persisted_a["work_item_id"] == "work-item-a-new"
    assert manifest_a["work_item_id"] == "work-item-a-new"
    assert persisted_a["budget_scope_id"] != "old-scope-a"

    # Manifest B is completely untouched, on disk and in the in-memory dict
    # that was never passed to refresh_work_item.
    persisted_b = json.loads(manifest_b_path.read_text(encoding="utf-8"))
    assert persisted_b["work_item_id"] == "old-b"
    assert persisted_b["budget_scope_id"] == "old-scope-b"
    assert manifest_b["work_item_id"] == "old-b"
    assert manifest_b["budget_scope_id"] == "old-scope-b"


def test_refresh_work_item_default_path_unchanged_when_manifest_path_omitted(tmp_path: Path) -> None:
    """Sequential single-slice callers that omit manifest_path keep prior behavior."""
    default_manifest_path = tmp_path / "manifest.json"
    env_file = tmp_path / "stack.env"
    env_file.write_text("ORCH_TOKEN_FOUNDER=token\n", encoding="utf-8")
    manifest = {"env_file": str(env_file), "work_item_id": "old", "budget_scope_id": "old-scope"}
    default_manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with (
        patch.dict("os.environ", {"ORCH_LOCAL_STACK_MANIFEST": str(default_manifest_path)}),
        patch("scripts.local_stack_helpers.subprocess.run") as run_mock,
        patch("urllib.request.urlopen") as urlopen_mock,
    ):
        run_mock.side_effect = [_seed_completed("work-item-default"), _reload_completed()]
        urlopen_mock.return_value.__enter__.return_value = MagicMock()
        refresh_work_item(manifest)  # manifest_path omitted, as existing callers do today

    persisted = json.loads(default_manifest_path.read_text(encoding="utf-8"))
    assert persisted["work_item_id"] == "work-item-default"
