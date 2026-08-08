from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from flow_engine.providers.git_workspace_evidence import (
    GitWorkspaceEvidenceError,
    capture_git_baseline,
    diff_against_git_baseline,
    resolve_git_workspace,
    validate_paths_against_write_set,
)
from flow_engine.providers.host_runner import validate_write_set


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    _git(path, "init")
    _git(path, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "--allow-empty", "-m", "init")


def test_write_set_dot_covers_whole_workspace(tmp_path: Path) -> None:
    normalized = validate_write_set(["."], tmp_path)
    assert normalized == (".",)
    result = validate_paths_against_write_set(
        ["src/foo.py", "README.md"],
        (".",),
        outside_workspace_paths=[],
    )
    assert result["write_set_validation"] == "pass"


def test_commit_after_baseline_detected_via_head_move(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "allowed.py").write_text("a=1\n", encoding="utf-8")
    _git(repo, "add", "src/allowed.py")
    _git(repo, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "seed")

    baseline = capture_git_baseline(repo, repo)
    (repo / "undeclared.py").write_text("b=2\n", encoding="utf-8")
    _git(repo, "add", "undeclared.py")
    _git(repo, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "bypass")

    diff = diff_against_git_baseline(baseline, repo)
    assert diff["head_moved"] is True
    assert "undeclared.py" in diff["changed_paths"]
    validation = validate_paths_against_write_set(
        diff["changed_paths"],
        ("src/",),
        outside_workspace_paths=diff["outside_workspace_paths"],
    )
    assert validation["write_set_validation"] == "fail"
    assert "undeclared.py" in validation["undeclared_paths"]


def test_cwd_subdirectory_paths_are_workspace_relative(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    pkg = repo / "pkg"
    pkg.mkdir(parents=True)
    _init_repo(repo)
    (pkg / "module.py").write_text("x=1\n", encoding="utf-8")
    _git(repo, "add", "pkg/module.py")
    _git(repo, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "seed")

    baseline = capture_git_baseline(pkg, repo)
    assert Path(baseline.cwd) == pkg.resolve()
    (pkg / "module.py").write_text("x=2\n", encoding="utf-8")

    diff = diff_against_git_baseline(baseline, pkg)
    assert "pkg/module.py" in diff["changed_paths"]


def test_linked_worktree_resolves_against_workspace_root(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _init_repo(main)
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", str(linked), "-b", "slice")

    toplevel, workspace = resolve_git_workspace(linked, linked)
    assert toplevel == linked.resolve()
    assert workspace == linked.resolve()
    baseline = capture_git_baseline(linked, linked)
    assert baseline.git_toplevel == linked.resolve().as_posix()


def test_missing_git_fails_closed(tmp_path: Path) -> None:
    bare = tmp_path / "nogit"
    bare.mkdir()
    with pytest.raises(GitWorkspaceEvidenceError):
        capture_git_baseline(bare, bare)
