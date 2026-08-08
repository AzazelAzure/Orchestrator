from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.unit.test_provider_host_runner import _fake_cli, _invoke_packet

from flow_engine.providers.cli_registry import EXECUTION_PROFILE_CURSOR_IMPLEMENTATION
from flow_engine.providers.git_workspace_evidence import (
    GitWorkspaceEvidenceError,
    _run_git,
    capture_git_baseline,
    diff_against_git_baseline,
    resolve_git_workspace,
    validate_paths_against_write_set,
)
from flow_engine.providers.host_runner import HostRunner, ProviderBinding, validate_write_set


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


def test_baseline_ls_files_nonzero_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[0] == "git" and "ls-files" in cmd:
            return subprocess.CompletedProcess(cmd, 128, "", "fatal: simulated ls-files")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(
        "flow_engine.providers.git_workspace_evidence.subprocess.run", fake_run
    )
    with pytest.raises(GitWorkspaceEvidenceError, match="ls-files"):
        capture_git_baseline(repo, repo)


def test_head_moved_diff_nonzero_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "tracked.py").write_text("a=1\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "seed")
    baseline = capture_git_baseline(repo, repo)
    (repo / "tracked.py").write_text("a=2\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "move-head")

    original = _run_git

    def flaky_run_git(cwd: Path, *args: str, check: bool = True) -> str:
        if args and args[0] == "diff" and len(args) >= 2:
            revs = [a for a in args if len(a) == 40 and all(c in "0123456789abcdef" for c in a)]
            if len(revs) == 2:
                raise GitWorkspaceEvidenceError("git diff failed: simulated head-range")
        return original(cwd, *args, check=check)

    monkeypatch.setattr(
        "flow_engine.providers.git_workspace_evidence._run_git", flaky_run_git
    )
    with pytest.raises(GitWorkspaceEvidenceError, match="diff"):
        diff_against_git_baseline(baseline, repo)


def test_invoke_blocked_when_post_diff_git_command_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    _init_repo(worktree)
    (worktree / "src").mkdir()
    (worktree / "src" / "allowed.py").write_text("x=1\n", encoding="utf-8")
    _git(worktree, "add", "src/allowed.py")
    _git(worktree, "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "init")

    binding = ProviderBinding(
        provider="cursor",
        executable=_fake_cli(tmp_path, "cursor"),
        model="cursor-test-model",
        workspace_root=worktree,
        socket_path=tmp_path / "cursor.sock",
        auth_token="test-only-host-token",
        cli_version_pin="2026.08.04-aaa8809",
        allowed_models=("cursor-test-model",),
        execution_profile=EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
    )
    runner = HostRunner(binding)
    handshake = runner.handshake()
    packet = _invoke_packet(
        runner,
        handshake,
        invocation_id="inv-git-fail",
        task_packet={"objective": "task", "write_set": ["src/"]},
    )

    original = _run_git
    ls_files_calls = 0

    def flaky_run_git(cwd: Path, *args: str, check: bool = True) -> str:
        nonlocal ls_files_calls
        if args and args[0] == "ls-files":
            ls_files_calls += 1
            if ls_files_calls > 1:
                raise GitWorkspaceEvidenceError("git ls-files failed: simulated post-invoke")
        return original(cwd, *args, check=check)

    monkeypatch.setattr(
        "flow_engine.providers.git_workspace_evidence._run_git", flaky_run_git
    )
    with pytest.raises(GitWorkspaceEvidenceError, match="ls-files"):
        runner.invoke(packet)
