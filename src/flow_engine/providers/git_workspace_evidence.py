"""Immutable git baseline capture for workspace-root-relative write_set enforcement."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class GitWorkspaceEvidenceError(RuntimeError):
    """Git evidence could not be established for a confined workspace."""


def _digest_json(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class GitWorkspaceBaseline:
    """Immutable pre-invocation git snapshot bound to workspace_root."""

    workspace_root: str
    git_toplevel: str
    cwd: str
    baseline_head: str
    baseline_untracked: tuple[str, ...]
    baseline_digest: str


def _run_git(cwd: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if check and completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise GitWorkspaceEvidenceError(f"git {args[0]} failed: {stderr[:256]}")
    return completed.stdout


def resolve_git_workspace(
    cwd: Path,
    workspace_root: Path,
) -> tuple[Path, Path]:
    """Resolve linked worktree top and verify workspace_root lies within it."""
    cwd = cwd.resolve(strict=True)
    workspace_root = workspace_root.resolve(strict=True)
    try:
        toplevel = Path(
            _run_git(cwd, "rev-parse", "--show-toplevel").strip()
        ).resolve(strict=True)
    except GitWorkspaceEvidenceError as exc:
        raise GitWorkspaceEvidenceError("git worktree top could not be resolved") from exc
    git_dir = _run_git(cwd, "rev-parse", "--git-dir").strip()
    if not git_dir:
        raise GitWorkspaceEvidenceError("git dir could not be resolved")
    if workspace_root != toplevel and toplevel not in workspace_root.parents:
        raise GitWorkspaceEvidenceError("workspace_root is outside git worktree")
    if cwd != toplevel and toplevel not in cwd.parents:
        raise GitWorkspaceEvidenceError("cwd escapes git worktree")
    return toplevel, workspace_root


def repo_path_to_workspace_relative(
    git_toplevel: Path,
    workspace_root: Path,
    repo_relative: str,
) -> str | None:
    candidate = (git_toplevel / repo_relative).resolve()
    try:
        return candidate.relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return None


def capture_git_baseline(cwd: Path, workspace_root: Path) -> GitWorkspaceBaseline:
    git_toplevel, workspace_root = resolve_git_workspace(cwd, workspace_root)
    head = _run_git(cwd, "rev-parse", "HEAD").strip()
    # baseline_untracked is bound into baseline_digest for fail-safe replay; pre-existing
    # untracked paths are recorded at capture and are not excluded from later diffs.
    untracked_raw = _run_git(cwd, "ls-files", "--others", "--exclude-standard")
    untracked: list[str] = []
    for line in untracked_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        rel = repo_path_to_workspace_relative(git_toplevel, workspace_root, line)
        if rel is not None:
            untracked.append(rel)
    payload = {
        "workspace_root": workspace_root.as_posix(),
        "git_toplevel": git_toplevel.as_posix(),
        "cwd": cwd.as_posix(),
        "baseline_head": head,
        "baseline_untracked": sorted(untracked),
    }
    return GitWorkspaceBaseline(
        workspace_root=workspace_root.as_posix(),
        git_toplevel=git_toplevel.as_posix(),
        cwd=cwd.as_posix(),
        baseline_head=head,
        baseline_untracked=tuple(sorted(untracked)),
        baseline_digest=_digest_json(payload),
    )


def _collect_repo_paths(cwd: Path, *git_args: str) -> list[str]:
    output = _run_git(cwd, *git_args)
    if not output.strip():
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def diff_against_git_baseline(
    baseline: GitWorkspaceBaseline,
    cwd: Path,
) -> dict[str, Any]:
    """Compare post-invocation state to immutable baseline; paths are workspace-relative."""
    git_toplevel = Path(baseline.git_toplevel)
    workspace_root = Path(baseline.workspace_root)
    cwd = cwd.resolve(strict=True)
    resolve_git_workspace(cwd, workspace_root)

    current_head = _run_git(cwd, "rev-parse", "HEAD").strip()
    repo_paths: set[str] = set()

    if current_head != baseline.baseline_head:
        repo_paths.update(
            _collect_repo_paths(
                cwd,
                "diff",
                "--name-only",
                "-M",
                "--diff-filter=ACDMRTUXB",
                baseline.baseline_head,
                current_head,
            )
        )

    repo_paths.update(
        _collect_repo_paths(
            cwd,
            "diff",
            "--name-only",
            "-M",
            "--diff-filter=ACDMRTUXB",
            baseline.baseline_head,
        )
    )
    repo_paths.update(
        _collect_repo_paths(
            cwd,
            "diff",
            "--cached",
            "--name-only",
            "-M",
            "--diff-filter=ACDMRTUXB",
            baseline.baseline_head,
        )
    )
    repo_paths.update(_collect_repo_paths(cwd, "ls-files", "--others", "--exclude-standard"))

    workspace_paths: set[str] = set()
    outside_workspace: list[str] = []
    for repo_path in repo_paths:
        rel = repo_path_to_workspace_relative(git_toplevel, workspace_root, repo_path)
        if rel is None:
            outside_workspace.append(repo_path)
        else:
            workspace_paths.add(rel)

    return {
        "git_baseline_digest": baseline.baseline_digest,
        "baseline_head": baseline.baseline_head,
        "post_head": current_head,
        "head_moved": current_head != baseline.baseline_head,
        "changed_paths": sorted(workspace_paths),
        "outside_workspace_paths": sorted(outside_workspace),
        "workspace_mutations_detected": bool(workspace_paths or outside_workspace),
    }


def validate_paths_against_write_set(
    changed_paths: list[str],
    write_set: tuple[str, ...],
    *,
    outside_workspace_paths: list[str],
) -> dict[str, Any]:
    if "." in write_set:
        undeclared = list(outside_workspace_paths)
    else:
        undeclared = [
            *outside_workspace_paths,
            *[
                path
                for path in changed_paths
                if not _path_covered_by_write_set(path, write_set)
            ],
        ]
    return {
        "write_set_validation": "fail" if undeclared else "pass",
        "undeclared_paths": sorted(set(undeclared)),
        "write_set": list(write_set),
    }


def _path_covered_by_write_set(rel_path: str, write_set: tuple[str, ...]) -> bool:
    if "." in write_set:
        return True
    normalized = Path(rel_path).as_posix()
    for entry in write_set:
        base = Path(entry).as_posix()
        if base == ".":
            return True
        if normalized == base:
            return True
        prefix = base.rstrip("/") + "/"
        if normalized.startswith(prefix):
            return True
    return False
