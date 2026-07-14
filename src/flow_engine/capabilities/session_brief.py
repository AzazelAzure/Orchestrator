"""Read-only session brief projection composer."""

from __future__ import annotations

from typing import Any

from flow_engine.capabilities.envelope import (
    CapabilityRequest,
    CapabilityResult,
    CapabilityStatus,
    ResultCode,
)
from flow_engine.capabilities.providers import (
    CIStatusProvider,
    OpenPRProvider,
    safe_get_ci_status,
    safe_list_open_prs,
)
from flow_engine.capabilities.repo_health import read_repo_health
from flow_engine.capabilities.work_lookup import lookup_work


def _provider_section(
    name: str,
    response,
) -> dict[str, Any]:
    if response.available and response.data is not None:
        return {"status": "ready", "count": len(response.data)}
    if response.degraded:
        return {"status": "degraded", "reason": response.reason}
    return {"status": "unavailable", "reason": response.reason}


def compose_session_brief(
    conn,
    request: CapabilityRequest,
    *,
    checkout_path,
    open_pr_provider: OpenPRProvider | None = None,
    ci_provider: CIStatusProvider | None = None,
    engine_project_name: str | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
    provider_timeout_sec: float = 3.0,
) -> CapabilityResult:
    validation = request.validate()
    if validation is not None:
        return CapabilityResult.failure(request, validation)

    degraded: list[str] = []
    sections: dict[str, Any] = {}

    health_request = CapabilityRequest(
        capability="repo_health",
        request_id=request.request_id,
        actor=request.actor,
        project_id=request.project_id,
    )
    health = read_repo_health(health_request, checkout_path=checkout_path)
    sections["repository_health"] = health.to_dict()
    if health.code != ResultCode.OK:
        degraded.append("repository_health")

    work_id = request.params.get("work_id")
    logical_work_id = request.params.get("logical_work_id")
    if work_id or logical_work_id:
        work_request = CapabilityRequest(
            capability="work_lookup",
            request_id=request.request_id,
            actor=request.actor,
            project_id=request.project_id,
            params={
                **({"work_id": work_id} if work_id else {}),
                **({"logical_work_id": logical_work_id} if logical_work_id else {}),
            },
        )
        work = lookup_work(
            conn,
            work_request,
            engine_project_name=engine_project_name,
        )
        sections["work"] = work.to_dict()
        if work.code != ResultCode.OK:
            degraded.append("work_lookup")
    else:
        sections["work"] = {"status": "skipped", "reason": "no work selector provided"}

    provider_availability: dict[str, Any] = {}
    if open_pr_provider and github_owner and github_repo:
        pr_response = safe_list_open_prs(
            open_pr_provider,
            owner=github_owner,
            repo=github_repo,
            timeout_sec=provider_timeout_sec,
        )
        provider_availability["open_prs"] = _provider_section("open_prs", pr_response)
        sections["open_prs"] = {
            "status": provider_availability["open_prs"]["status"],
            "items": [item.__dict__ for item in (pr_response.data or [])],
        }
        if not pr_response.available:
            degraded.append("open_prs")
    else:
        provider_availability["open_prs"] = {
            "status": "unavailable",
            "reason": "provider not configured",
        }
        degraded.append("open_prs")

    if ci_provider and github_owner and github_repo and health.code == ResultCode.OK:
        ref = health.data.get("head", "HEAD")
        ci_response = safe_get_ci_status(
            ci_provider,
            owner=github_owner,
            repo=github_repo,
            ref=ref,
            timeout_sec=provider_timeout_sec,
        )
        provider_availability["ci_status"] = _provider_section("ci_status", ci_response)
        sections["ci_status"] = {
            "status": provider_availability["ci_status"]["status"],
            "checks": [item.__dict__ for item in (ci_response.data or [])],
        }
        if not ci_response.available:
            degraded.append("ci_status")
    else:
        provider_availability["ci_status"] = {
            "status": "unavailable",
            "reason": "provider not configured",
        }
        degraded.append("ci_status")

    text_lines = [
        f"Session brief for project {request.project_id}",
        f"Repository: {sections['repository_health'].get('data', {}).get('repository_identity', 'unknown')}",
        f"Branch: {sections['repository_health'].get('data', {}).get('branch', 'unknown')}",
        f"Dirty: {sections['repository_health'].get('data', {}).get('dirty', 'unknown')}",
    ]
    if sections["work"].get("code") == ResultCode.OK.value:
        work_data = sections["work"]["data"]
        text_lines.append(
            f"Work {work_data.get('work_id')}: {work_data.get('status')} (rev {work_data.get('revision')})"
        )
    text_lines.append(
        "Providers: "
        + ", ".join(f"{name}={info['status']}" for name, info in provider_availability.items())
    )

    status = CapabilityStatus.READY if not degraded else CapabilityStatus.DEGRADED
    structured = {
        "project_id": request.project_id,
        "sections": sections,
        "provider_availability": provider_availability,
        "text": "\n".join(text_lines),
    }
    return CapabilityResult.success(
        request,
        data=structured,
        status=status,
        degraded_components=tuple(degraded),
    )
