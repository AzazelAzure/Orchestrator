"""External provider boundaries for read-only GitHub capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class ProviderPullRequest:
    number: int
    title: str
    url: str
    state: str
    author: str | None = None


@dataclass(frozen=True)
class ProviderCIStatus:
    context: str
    state: str
    target_url: str | None = None


@dataclass(frozen=True)
class ProviderResponse:
    available: bool
    degraded: bool = False
    reason: str | None = None
    data: list[Any] | None = None


@runtime_checkable
class OpenPRProvider(Protocol):
    def list_open_prs(self, *, owner: str, repo: str, timeout_sec: float) -> ProviderResponse:
        """Return open pull requests or an unavailable/degraded response."""


@runtime_checkable
class CIStatusProvider(Protocol):
    def get_ci_status(
        self,
        *,
        owner: str,
        repo: str,
        ref: str,
        timeout_sec: float,
    ) -> ProviderResponse:
        """Return CI check summaries or an unavailable/degraded response."""


def safe_list_open_prs(
    provider: OpenPRProvider,
    *,
    owner: str,
    repo: str,
    timeout_sec: float,
) -> ProviderResponse:
    try:
        return provider.list_open_prs(owner=owner, repo=repo, timeout_sec=timeout_sec)
    except Exception as exc:
        return ProviderResponse(
            available=False,
            degraded=True,
            reason=f"open PR provider error: {exc}",
        )


def safe_get_ci_status(
    provider: CIStatusProvider,
    *,
    owner: str,
    repo: str,
    ref: str,
    timeout_sec: float,
) -> ProviderResponse:
    try:
        return provider.get_ci_status(
            owner=owner,
            repo=repo,
            ref=ref,
            timeout_sec=timeout_sec,
        )
    except Exception as exc:
        return ProviderResponse(
            available=False,
            degraded=True,
            reason=f"CI status provider error: {exc}",
        )
