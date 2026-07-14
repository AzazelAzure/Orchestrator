"""Read-only repository health capability."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from flow_engine.capabilities.envelope import (
    CapabilityError,
    CapabilityRequest,
    CapabilityResult,
    ResultCode,
)


def _run_git(repo_path: Path, *args: str, timeout_sec: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


def read_repo_health(
    request: CapabilityRequest,
    *,
    checkout_path: Path,
    timeout_sec: float = 5.0,
) -> CapabilityResult:
    validation = request.validate()
    if validation is not None:
        return CapabilityResult.failure(request, validation)

    if not checkout_path.is_dir():
        return CapabilityResult.failure(
            request,
            CapabilityError(
                ResultCode.UNAVAILABLE,
                "repository checkout is not available",
                {"logical_project_id": request.project_id},
            ),
        )

    git_dir = checkout_path / ".git"
    if not git_dir.exists():
        return CapabilityResult.failure(
            request,
            CapabilityError(ResultCode.NOT_FOUND, "path is not a git repository"),
        )

    try:
        branch_proc = _run_git(checkout_path, "branch", "--show-current", timeout_sec=timeout_sec)
        if branch_proc.returncode != 0:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.UNAVAILABLE,
                    "unable to read repository branch",
                    {"stderr": branch_proc.stderr.strip()},
                ),
            )

        head_proc = _run_git(checkout_path, "rev-parse", "HEAD", timeout_sec=timeout_sec)
        if head_proc.returncode != 0:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.NOT_FOUND,
                    "repository has no commits",
                ),
            )

        status_proc = _run_git(checkout_path, "status", "--porcelain", timeout_sec=timeout_sec)
        if status_proc.returncode != 0:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.UNAVAILABLE,
                    "unable to read repository status",
                    {"stderr": status_proc.stderr.strip()},
                ),
            )

        remote_proc = _run_git(checkout_path, "remote", "get-url", "origin", timeout_sec=timeout_sec)
        remote_url = remote_proc.stdout.strip() if remote_proc.returncode == 0 else None

        dirty = bool(status_proc.stdout.strip())
        data: dict[str, Any] = {
            "logical_project_id": request.project_id,
            "repository_identity": remote_url or checkout_path.name,
            "branch": branch_proc.stdout.strip() or "HEAD",
            "head": head_proc.stdout.strip(),
            "dirty": dirty,
            "dirty_file_count": len(
                [line for line in status_proc.stdout.splitlines() if line.strip()]
            ),
        }
        return CapabilityResult.success(request, data=data)
    except (subprocess.TimeoutExpired, OSError) as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            return CapabilityResult.failure(
                request,
                CapabilityError(ResultCode.TIMEOUT, "git command timed out"),
            )
        return CapabilityResult.failure(
            request,
            CapabilityError(
                ResultCode.UNAVAILABLE,
                "git is unavailable",
                {"error": str(exc)},
            ),
        )
