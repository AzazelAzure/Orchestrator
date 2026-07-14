"""Application capability service for read-only configured logical projects."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from flow_engine.capabilities.envelope import (
    CapabilityError,
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    ResultCode,
)
from flow_engine.capabilities.project_resolver import ProjectResolver, ProjectResolverError
from flow_engine.capabilities.providers import (
    CIStatusProvider,
    OpenPRProvider,
    ProviderResponse,
    safe_get_ci_status,
    safe_list_open_prs,
)
from flow_engine.capabilities.repo_health import read_repo_health
from flow_engine.capabilities.session_brief import compose_session_brief
from flow_engine.capabilities.work_lookup import lookup_work
from flow_engine.persistence.connection import open_connection


class CapabilityService:
    """Shared read-only capability entry point for CLI and future MCP transport."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        projects_config: Path | str | None = None,
        open_pr_provider: OpenPRProvider | None = None,
        ci_provider: CIStatusProvider | None = None,
        provider_timeout_sec: float = 3.0,
    ) -> None:
        self._db_path = Path(db_path)
        self._resolver = ProjectResolver(projects_config)
        self._open_pr_provider = open_pr_provider
        self._ci_provider = ci_provider
        self._provider_timeout_sec = provider_timeout_sec

    def _connection(self) -> sqlite3.Connection:
        return open_connection(self._db_path)

    def _resolve_or_fail(self, request: CapabilityRequest) -> tuple[Any, CapabilityResult | None]:
        try:
            return self._resolver.resolve(request.project_id), None
        except ProjectResolverError as exc:
            return None, CapabilityResult.failure(
                request,
                CapabilityError(ResultCode.UNAVAILABLE, str(exc)),
                status=CapabilityStatus.UNAVAILABLE,
            )

    def repo_health(self, request: CapabilityRequest) -> CapabilityResult:
        resolution, failure = self._resolve_or_fail(request)
        if failure is not None:
            return failure
        return read_repo_health(
            request,
            checkout_path=resolution.binding.checkout_path,
        )

    def open_prs(self, request: CapabilityRequest) -> CapabilityResult:
        resolution, failure = self._resolve_or_fail(request)
        if failure is not None:
            return failure
        if self._open_pr_provider is None:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.UNAVAILABLE,
                    "open PR provider is not configured",
                ),
                status=CapabilityStatus.UNAVAILABLE,
            )

        owner = str(request.params.get("github_owner", "")).strip()
        repo = str(request.params.get("github_repo", "")).strip()
        if not owner or not repo:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.INVALID_INPUT,
                    "github_owner and github_repo are required",
                ),
            )

        response = safe_list_open_prs(
            self._open_pr_provider,
            owner=owner,
            repo=repo,
            timeout_sec=self._provider_timeout_sec,
        )
        if not response.available:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.UNAVAILABLE,
                    response.reason or "open PR provider unavailable",
                ),
                status=CapabilityStatus.DEGRADED if response.degraded else CapabilityStatus.UNAVAILABLE,
            )

        return CapabilityResult.success(
            request,
            data={
                "logical_project_id": request.project_id,
                "pull_requests": [item.__dict__ for item in (response.data or [])],
            },
            status=CapabilityStatus.DEGRADED if response.degraded else CapabilityStatus.READY,
        )

    def ci_status(self, request: CapabilityRequest) -> CapabilityResult:
        resolution, failure = self._resolve_or_fail(request)
        if failure is not None:
            return failure
        if self._ci_provider is None:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.UNAVAILABLE,
                    "CI status provider is not configured",
                ),
                status=CapabilityStatus.UNAVAILABLE,
            )

        owner = str(request.params.get("github_owner", "")).strip()
        repo = str(request.params.get("github_repo", "")).strip()
        ref = str(request.params.get("ref", "")).strip()
        if not owner or not repo or not ref:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.INVALID_INPUT,
                    "github_owner, github_repo, and ref are required",
                ),
            )

        response = safe_get_ci_status(
            self._ci_provider,
            owner=owner,
            repo=repo,
            ref=ref,
            timeout_sec=self._provider_timeout_sec,
        )
        if not response.available:
            return CapabilityResult.failure(
                request,
                CapabilityError(
                    ResultCode.UNAVAILABLE,
                    response.reason or "CI status provider unavailable",
                ),
                status=CapabilityStatus.DEGRADED if response.degraded else CapabilityStatus.UNAVAILABLE,
            )

        return CapabilityResult.success(
            request,
            data={
                "logical_project_id": request.project_id,
                "checks": [item.__dict__ for item in (response.data or [])],
            },
            status=CapabilityStatus.DEGRADED if response.degraded else CapabilityStatus.READY,
        )

    def work_lookup(self, request: CapabilityRequest) -> CapabilityResult:
        resolution, failure = self._resolve_or_fail(request)
        if failure is not None:
            return failure
        with closing(self._connection()) as conn:
            return lookup_work(
                conn,
                request,
                engine_project_name=resolution.binding.engine_project_name,
            )

    def session_brief(self, request: CapabilityRequest) -> CapabilityResult:
        resolution, failure = self._resolve_or_fail(request)
        if failure is not None:
            return failure
        with closing(self._connection()) as conn:
            return compose_session_brief(
                conn,
                request,
                checkout_path=resolution.binding.checkout_path,
                open_pr_provider=self._open_pr_provider,
                ci_provider=self._ci_provider,
                engine_project_name=resolution.binding.engine_project_name,
                github_owner=request.params.get("github_owner"),
                github_repo=request.params.get("github_repo"),
                provider_timeout_sec=self._provider_timeout_sec,
            )


class UnconfiguredProvider:
    """Explicit unavailable provider used in tests and degraded mode."""

    def __init__(self, reason: str = "credentials not configured") -> None:
        self._reason = reason

    def list_open_prs(self, *, owner: str, repo: str, timeout_sec: float) -> ProviderResponse:
        return ProviderResponse(available=False, reason=self._reason)

    def get_ci_status(
        self,
        *,
        owner: str,
        repo: str,
        ref: str,
        timeout_sec: float,
    ) -> ProviderResponse:
        return ProviderResponse(available=False, reason=self._reason)
