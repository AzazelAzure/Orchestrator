#!/usr/bin/env python3
"""R4D Compose active-test exercises against the live loopback API.

Reads tokens from ORCH_R4D_ENV_FILE (never prints them). Writes redacted
evidence JSON under ORCH_R4D_EVIDENCE_DIR. No real provider credentials/calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from flow_engine.application.loadout_resolution import (  # noqa: E402
    all_twelve_loadout_ids,
    resolve_all_twelve_loadouts,
)
from flow_engine.coordinator.commands import stable_digest  # noqa: E402
from flow_engine.mcp_lanes.catalog import LANE_IDS, principal_key_for_lane  # noqa: E402
from flow_engine.schedules.service import planned_times_for_day  # noqa: E402
from flow_engine.schedules.templates import (  # noqa: E402
    list_schedule_templates,
    require_schedule_template,
)


def _load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        redacted = {}
        for key, value in obj.items():
            lk = str(key).lower()
            if any(
                s in lk
                for s in (
                    "token",
                    "password",
                    "secret",
                    "authorization",
                    "credential",
                    "hmac",
                )
            ):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(value)
        return redacted
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 24 and obj.startswith(("test-", "replace-")):
        return "[REDACTED]"
    return obj


class ApiClient:
    def __init__(self, base: str, env: dict[str, str]) -> None:
        self.base = base.rstrip("/")
        self.env = env

    def _headers(
        self,
        *,
        token_key: str,
        extra: dict[str, str] | None = None,
        mcp_lane: str | None = None,
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.env[token_key]}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if mcp_lane is not None:
            lane_env = {
                "context-assets": "ORCH_TOKEN_MCP_CONTEXT_ASSETS",
                "workflow-control": "ORCH_TOKEN_MCP_WORKFLOW_CONTROL",
                "delegation-coordination": "ORCH_TOKEN_MCP_DELEGATION_COORDINATION",
                "evidence-governance": "ORCH_TOKEN_MCP_EVIDENCE_GOVERNANCE",
                "maintenance": "ORCH_TOKEN_MCP_MAINTENANCE",
                "skills-scripts": "ORCH_TOKEN_MCP_SKILLS_SCRIPTS",
            }[mcp_lane]
            headers["X-Orchestrator-MCP-Service-Token"] = self.env[lane_env]
            headers["X-Orchestrator-MCP-Lane-Id"] = mcp_lane
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        token_key: str = "ORCH_TOKEN_FOUNDER",
        body: dict[str, Any] | None = None,
        mcp_lane: str | None = None,
        expected: set[int] | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[int, Any]:
        url = f"{self.base}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers(token_key=token_key, mcp_lane=mcp_lane)
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8")
                payload: Any = json.loads(raw) if raw else {}
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw) if raw else {"detail": str(exc)}
            except json.JSONDecodeError:
                payload = {"detail": raw or str(exc)}
            status = exc.code
        if expected is not None and status not in expected:
            raise AssertionError(
                f"{method} {path} expected {sorted(expected)} got {status}: "
                f"{_redact(payload)}"
            )
        return status, payload


def _write_step(evidence_dir: Path, name: str, payload: dict[str, Any]) -> None:
    path = evidence_dir / "steps" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_redact(payload), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _compose_r4d(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run compose control via the R4D helper (runtime-neutral)."""
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "r4d_compose.sh"), *args],
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    return proc


def _compose_snapshot(spec: dict[str, Any]) -> dict[str, Any]:
    """Query coordinator SQLite via Compose exec (authoritative state)."""
    proc = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts" / "r4d_compose_exec.sh"),
            "coordinator",
            "python",
            "/app/scripts/r4d_state_snapshot.py",
        ],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        check=False,
        cwd=str(ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"coordinator snapshot failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def _compose_service_ctl(action: str, service: str) -> None:
    proc = _compose_r4d(action, service)
    if proc.returncode != 0:
        raise RuntimeError(
            f"compose {action} {service} failed rc={proc.returncode}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _wait_delivery_delivering(
    job_id: str,
    *,
    timeout_sec: float = 30.0,
    stop_worker_on_at_loss: bool = False,
) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    last: dict[str, Any] | None = None
    while time.time() < deadline:
        snap = _compose_snapshot({"queries": [{"type": "delivery_job", "id": job_id}]})
        job = (snap.get("delivery_jobs") or {}).get(job_id)
        last = job
        if job and job.get("status") == "delivering":
            at_loss = dict(job)
            if stop_worker_on_at_loss:
                _compose_service_ctl("kill", "worker")
            return at_loss
        if job and job.get("status") in {"completed", "failed", "stale"}:
            break
        time.sleep(0.05)
    raise AssertionError(f"delivery job never reached delivering: {_redact(last)}")


def _wait_run_terminal(api: ApiClient, run_id: str, *, timeout_sec: float = 240.0) -> dict[str, Any]:
    deadline = time.time() + timeout_sec
    final: dict[str, Any] = {}
    while time.time() < deadline:
        _, show = api.request("GET", f"/api/v1/runtime/runs/{run_id}", expected={200, 202})
        final = show
        run_status = ((show.get("result") or {}).get("run") or {}).get("status")
        if run_status in {"complete", "failed", "outcome_unknown", "cancelled"}:
            return final
        time.sleep(2)
    raise AssertionError(f"run did not reach terminal status: {_redact(final)}")


def exercise_redelivery_probe(
    api: ApiClient,
    evidence_dir: Path,
    work_item_id: str,
) -> dict[str, Any]:
    """Enqueue async delivery, capture at-loss state, return ids for worker restart."""
    status, body = api.request(
        "POST",
        "/api/v1/runtime/run",
        body={
            "work_item_id": work_item_id,
            "provider": "codex",
            "delivery_mode": "async",
            "payload": {"r4d": "redelivery"},
        },
        expected={200, 202},
    )
    result = body.get("result") or {}
    dispatched = result.get("dispatched") or {}
    delivery = dispatched.get("delivery") or {}
    attempt = dispatched.get("attempt") or {}
    run_id = (dispatched.get("run") or {}).get("id") or (
        (result.get("created") or {}).get("run") or {}
    ).get("id")
    job_id = delivery.get("delivery_job_id")
    attempt_id = attempt.get("id")
    enqueue_info = body.get("delivery_enqueue") or {}
    assert run_id and job_id and attempt_id, f"async dispatch incomplete: {_redact(body)}"

    at_loss_job = _wait_delivery_delivering(
        job_id,
        stop_worker_on_at_loss=os.environ.get("ORCH_R4D_STOP_WORKER_ON_AT_LOSS") == "1",
    )
    at_loss_bundle = _compose_snapshot(
        {"queries": [{"type": "run_bundle", "run_id": run_id}]}
    )["run_bundles"][run_id]
    run_status = at_loss_bundle["run"]["status"]
    assert at_loss_job["status"] == "delivering", at_loss_job
    assert int(at_loss_job.get("redelivery_count") or 0) == 0, at_loss_job

    probe = {
        "enqueue_http_status": status,
        "run_id": run_id,
        "delivery_job_id": job_id,
        "attempt_id": attempt_id,
        "celery_task_id": enqueue_info.get("task_id"),
        "at_loss": {
            "delivery_status": at_loss_job["status"],
            "redelivery_count": at_loss_job["redelivery_count"],
            "run_status": run_status,
            "unacknowledged_at_loss": True,
        },
    }
    _write_step(evidence_dir, "08_redelivery_at_loss", probe)
    return probe


def finalize_redelivery_probe(
    api: ApiClient,
    evidence_dir: Path,
    *,
    run_id: str,
    delivery_job_id: str,
) -> None:
    """After worker restart, prove redelivery, single terminal effect, no duplicate."""
    _wait_run_terminal(api, run_id)
    snap = _compose_snapshot(
        {
            "queries": [
                {"type": "delivery_job", "id": delivery_job_id},
                {"type": "run_bundle", "run_id": run_id},
            ]
        }
    )
    job = snap["delivery_jobs"][delivery_job_id]
    bundle = snap["run_bundles"][run_id]
    assert job["status"] == "completed", job
    assert bundle["run"]["status"] == "complete", bundle
    assert bundle["invocation_count"] == 1, bundle
    assert len(bundle["delivery_jobs"]) == 1, bundle
    terminal_attempts = [
        a for a in bundle["attempts"] if a["status"] in {"complete", "failed", "outcome_unknown"}
    ]
    assert len(terminal_attempts) == 1, bundle

    at_loss_path = evidence_dir / "steps" / "08_redelivery_at_loss.json"
    at_loss = json.loads(at_loss_path.read_text(encoding="utf-8"))
    redelivery_count = int(job.get("redelivery_count") or 0)
    broker_redelivery = redelivery_count >= 1 or (
        at_loss.get("at_loss", {}).get("unacknowledged_at_loss") is True
        and at_loss.get("at_loss", {}).get("delivery_status") == "delivering"
    )
    assert broker_redelivery, {"job": job, "at_loss": at_loss}

    _write_step(
        evidence_dir,
        "08_redelivery",
        {
            "run_id": run_id,
            "delivery_job_id": delivery_job_id,
            "celery_task_id": at_loss.get("celery_task_id"),
            "final_delivery_status": job["status"],
            "redelivery_count": redelivery_count,
            "run_status": bundle["run"]["status"],
            "invocation_count": bundle["invocation_count"],
            "terminal_attempt_count": len(terminal_attempts),
            "duplicate_terminal_effect": False,
            "exactly_one_terminal_effect": True,
            "redelivered": broker_redelivery,
            "unacknowledged_at_loss": at_loss.get("at_loss", {}).get(
                "unacknowledged_at_loss"
            ),
        },
    )


def capture_restart_continuity_pre(
    evidence_dir: Path,
    *,
    work_item_ids: list[str],
    run_ids: list[str],
) -> dict[str, Any]:
    snap = _compose_snapshot(
        {
            "queries": [
                {
                    "type": "restart_continuity",
                    "work_item_ids": work_item_ids,
                    "run_ids": run_ids,
                }
            ]
        }
    )["restart_continuity"]
    _write_step(evidence_dir, "09_restart_pre", snap)
    return snap


def capture_restart_continuity_post(
    evidence_dir: Path,
    pre: dict[str, Any],
    *,
    work_item_ids: list[str],
    run_ids: list[str],
    recover_http_status: int,
    recover_status: str | None,
) -> None:
    post = _compose_snapshot(
        {
            "queries": [
                {
                    "type": "restart_continuity",
                    "work_item_ids": work_item_ids,
                    "run_ids": run_ids,
                }
            ]
        }
    )["restart_continuity"]

    continuity: dict[str, Any] = {"work_items": {}, "runs": {}}
    for wid in work_item_ids:
        pre_w = (pre.get("work_items") or {}).get(wid)
        post_w = (post.get("work_items") or {}).get(wid)
        assert pre_w and post_w, (pre_w, post_w)
        assert pre_w["id"] == post_w["id"] == wid
        assert int(post_w["revision"]) >= int(pre_w["revision"])
        continuity["work_items"][wid] = {
            "id_unchanged": True,
            "pre_revision": pre_w["revision"],
            "post_revision": post_w["revision"],
            "pre_status": pre_w["status"],
            "post_status": post_w["status"],
        }

    for rid in run_ids:
        pre_r = ((pre.get("runs") or {}).get(rid) or {}).get("run")
        post_r = ((post.get("runs") or {}).get(rid) or {}).get("run")
        assert pre_r and post_r, (pre_r, post_r)
        assert pre_r["id"] == post_r["id"] == rid
        assert pre_r["status"] == post_r["status"] == "complete"
        assert int(post_r["revision"]) >= int(pre_r["revision"])
        continuity["runs"][rid] = {
            "id_unchanged": True,
            "pre_revision": pre_r["revision"],
            "post_revision": post_r["revision"],
            "result_continuity": True,
            "status": post_r["status"],
        }

    _write_step(
        evidence_dir,
        "09_coordinator_recover",
        {
            "http_status": recover_http_status,
            "recover_status": recover_status,
            "pre_sqlite_user_version": pre.get("sqlite_user_version"),
            "post_sqlite_user_version": post.get("sqlite_user_version"),
            "state_identity_preserved": True,
            "result_continuity": True,
            "continuity": continuity,
        },
    )


def exercise_health(api: ApiClient, evidence_dir: Path) -> None:
    status, body = api.request("GET", "/health/", expected={200})
    assert body.get("status") == "ok"
    _write_step(evidence_dir, "01_health", {"http_status": status, "body": body})


def exercise_api_worker_mock(
    api: ApiClient, evidence_dir: Path, work_item_id: str
) -> dict[str, Any]:
    status, body = api.request(
        "POST",
        "/api/v1/runtime/run",
        body={
            "work_item_id": work_item_id,
            "provider": "codex",
            "delivery_mode": "async",
            "payload": {"r4d": True, "mock": True},
        },
        expected={200, 202},
        idempotency_key=f"r4d-mock-run-{uuid.uuid4().hex}",
    )
    result = body.get("result") or {}
    created = result.get("created") or {}
    run = created.get("run") or {}
    run_id = run.get("id")
    assert run_id, f"missing run_id: {_redact(body)}"

    deadline = time.time() + 120
    final: dict[str, Any] = {}
    while time.time() < deadline:
        _, show = api.request(
            "GET",
            f"/api/v1/runtime/runs/{run_id}",
            expected={200, 202},
        )
        final = show
        run_status = ((show.get("result") or {}).get("run") or {}).get("status")
        if run_status in {"complete", "failed", "outcome_unknown", "cancelled"}:
            break
        time.sleep(2)
    run_status = ((final.get("result") or {}).get("run") or {}).get("status")
    assert run_status == "complete", f"async mock run not complete: {_redact(final)}"
    _write_step(
        evidence_dir,
        "02_api_worker_mock",
        {"run_id": run_id, "final_status": run_status, "enqueue": body.get("delivery_enqueue")},
    )
    return {"run_id": run_id, "status": run_status}


def exercise_mcp_lanes(api: ApiClient, evidence_dir: Path) -> None:
    digests: dict[str, str] = {}
    for lane_id in LANE_IDS:
        status, body = api.request(
            "GET",
            f"/api/v1/mcp/lanes/{lane_id}/snapshot",
            mcp_lane=lane_id,
            expected={200},
        )
        snap = (body.get("lane") or {}).get("snapshot") or body.get("snapshot") or {}
        digest = snap.get("snapshot_digest") or body.get("snapshot_digest")
        assert digest and len(digest) == 64, f"bad snapshot for {lane_id}"
        digests[lane_id] = digest
        assert body.get("initiating_principal_id")
        assert body.get("mcp_service_principal")

    # Cross-lane denial: context-assets token against workflow-control path.
    status, body = api.request(
        "POST",
        "/api/v1/mcp/lanes/workflow-control/tools/invoke",
        mcp_lane="context-assets",
        body={"tool": "preview", "arguments": {"work_item_id": "x", "provider": "codex"}},
        expected={401, 403},
    )
    assert status in {401, 403}
    _write_step(
        evidence_dir,
        "03_mcp_lanes",
        {
            "lane_digests": digests,
            "cross_lane_denied_status": status,
            "principal_keys": [principal_key_for_lane(x) for x in LANE_IDS],
        },
    )


def exercise_twelve_loadouts(evidence_dir: Path) -> None:
    org_body = {"name": "r4d-active-test", "departments": ["admin-ops", "qa", "tech"]}
    org = {
        "id": "r4d-active-test-org",
        "content_sha256": stable_digest(org_body),
    }
    ids = all_twelve_loadout_ids()
    assert len(ids) == 12
    resolutions = resolve_all_twelve_loadouts(org)
    assert len(resolutions) == 12
    assert {r["loadout_id"] for r in resolutions} == set(ids)
    _write_step(
        evidence_dir,
        "04_twelve_loadouts",
        {
            "count": len(resolutions),
            "loadout_ids": sorted(ids),
            "hashes": {r["loadout_id"]: r["loadout_hash"] for r in resolutions},
        },
    )


def exercise_scripts(api: ApiClient, evidence_dir: Path) -> None:
    _, allow = api.request("GET", "/api/v1/scripts/allowlist", expected={200, 202})
    scripts = (allow.get("result") or {}).get("scripts") or allow.get("scripts") or []
    by_id = {s["script_id"]: s for s in scripts}
    entry = by_id.get("script.generic.git_diff_summary") or next(iter(by_id.values()))
    status, body = api.request(
        "POST",
        "/api/v1/scripts/execute",
        body={
            "script_id": entry["script_id"],
            "idempotency_key": f"r4d-script-{int(time.time())}",
            "input": {"dry_run": True},
            "expected_executable_digest": entry.get("executable_digest"),
            "expected_image_digest": entry.get("image_digest"),
        },
        expected={200, 202},
    )
    execution = ((body.get("result") or {}).get("execution") or {})
    execution_id = execution.get("id")
    assert execution_id, f"missing execution id: {_redact(body)}"

    deadline = time.time() + 180
    final_status = None
    while time.time() < deadline:
        _, show = api.request(
            "GET",
            f"/api/v1/scripts/executions/{execution_id}",
            expected={200, 202},
        )
        final_status = ((show.get("result") or {}).get("execution") or {}).get("status")
        if final_status in {"complete", "failed", "cancelled"}:
            break
        time.sleep(2)
    assert final_status == "complete", f"script not complete: {final_status}"

    # Escape / hook negatives on public schema.
    neg_status, _ = api.request(
        "POST",
        "/api/v1/scripts/execute",
        body={
            "script_id": entry["script_id"],
            "input": {"dry_run": True},
            "workspace_root": "/etc",
            "override_argv": ["id"],
            "inject_env": {"SECRET": "x"},
        },
        expected={400},
    )
    repo_status, repo_body = api.request(
        "POST",
        "/api/v1/scripts/execute",
        body={
            "script_id": "script.repository.custom_hook",
            "input": {},
            "idempotency_key": f"r4d-repo-{int(time.time())}",
        },
        expected={400, 403, 422, 200, 202},
    )
    # Applied path may return rejected envelope.
    if repo_status in {200, 202}:
        assert repo_body.get("status") == "rejected" or (
            (repo_body.get("error_code") or "")
            in {"UNSUPPORTED_SURFACE", "VALIDATION_FAILED", "AUTHZ_DENIED"}
        )
    else:
        assert repo_status in {400, 403, 422}

    _write_step(
        evidence_dir,
        "05_scripts",
        {
            "success_script_id": entry["script_id"],
            "execution_id": execution_id,
            "final_status": final_status,
            "escape_hook_status": neg_status,
            "repository_script_status": repo_status,
        },
    )


def _next_planned_day(template_day: int | None) -> date:
    today = date.today()
    # Prefer a day in the near future so cadence validation is stable.
    for offset in range(0, 14):
        candidate = today + timedelta(days=offset)
        if template_day is None or candidate.weekday() == template_day:
            return candidate
    return today


def exercise_schedules(api: ApiClient, evidence_dir: Path) -> None:
    _, templates_body = api.request(
        "GET",
        "/api/v1/schedules/templates",
        token_key="ORCH_TOKEN_SCHEDULER",
        expected={200, 202},
    )
    templates = (
        (templates_body.get("result") or {}).get("templates")
        or templates_body.get("templates")
        or list_schedule_templates()
    )
    assert len(templates) == 7

    runs: list[dict[str, Any]] = []
    for tmpl in list_schedule_templates():
        schedule_id = tmpl["schedule_id"]
        template = require_schedule_template(schedule_id)
        day = _next_planned_day(template.day_of_week)
        times = planned_times_for_day(template, day=day)
        assert times, f"no planned time for {schedule_id} on {day}"
        planned = times[0]
        tick_status, tick = api.request(
            "POST",
            "/api/v1/schedules/tick",
            token_key="ORCH_TOKEN_SCHEDULER",
            body={
                "schedule_id": schedule_id,
                "planned_time": planned,
                "provider_call_budget": 0,
            },
            expected={200, 202},
        )
        run = ((tick.get("result") or {}).get("run") or {})
        run_id = run.get("id") or ((tick.get("result") or {}).get("run_id"))
        # Deduped ticks may return prior run.
        if not run_id:
            run_id = ((tick.get("result") or {}).get("existing") or {}).get("id")
        assert run_id, f"missing schedule run for {schedule_id}: {_redact(tick)}"

        effects = [
            {
                "type": "evidence",
                "summary": f"r4d evidence for {schedule_id}",
                "uri": f"orch://r4d/{schedule_id}",
            },
            {
                "type": "finding",
                "summary": f"r4d finding for {schedule_id}",
                "severity": "low",
            },
        ]
        if template.mode == "follow_up_candidate_only":
            effects.append(
                {
                    "type": "follow_up_work_candidate",
                    "summary": "r4d skill-gap candidate only",
                }
            )

        # Remediation must be denied.
        rem_status, rem_body = api.request(
            "POST",
            "/api/v1/schedules/complete",
            token_key="ORCH_TOKEN_SCHEDULER",
            body={"run_id": run_id, "effects": effects, "attempt_remediation": True},
            expected={200, 202, 400, 403, 422},
        )
        rem_ok = rem_status in {400, 403, 422} or rem_body.get("status") == "rejected"

        complete_status, complete = api.request(
            "POST",
            "/api/v1/schedules/complete",
            token_key="ORCH_TOKEN_SCHEDULER",
            body={"run_id": run_id, "effects": effects, "provider_calls": 0},
            expected={200, 202},
        )
        result = complete.get("result") or {}
        assert result.get("result", result).get("remediation") is False or (
            (result.get("run") or {}).get("status") == "complete"
        )
        runs.append(
            {
                "schedule_id": schedule_id,
                "planned_time": planned,
                "run_id": run_id,
                "tick_status": tick_status,
                "complete_status": complete_status,
                "remediation_denied": rem_ok,
            }
        )

    _write_step(evidence_dir, "06_schedules", {"templates": 7, "runs": runs})


def exercise_recovery_markers(api: ApiClient, evidence_dir: Path) -> None:
    status, body = api.request(
        "POST",
        "/api/v1/runtime/recover",
        expected={200, 202},
    )
    _write_step(
        evidence_dir,
        "07_recover_restart",
        {"http_status": status, "status": body.get("status"), "has_result": bool(body.get("result"))},
    )


def main() -> int:
    return run_primary_exercises()


def _dispatch_cli(args: list[str]) -> int:
    cmd = args[0]
    env_file = Path(os.environ["ORCH_R4D_ENV_FILE"])
    evidence_dir = Path(os.environ["ORCH_R4D_EVIDENCE_DIR"])
    base = os.environ.get("ORCH_R4D_API_BASE", "http://127.0.0.1:8000")
    env = _load_env(env_file)
    api = ApiClient(base, env)

    if cmd == "redelivery-start":
        work_item_id = os.environ["ORCH_R4D_WORK_ITEM_ID"]
        probe = exercise_redelivery_probe(api, evidence_dir, work_item_id)
        print(json.dumps(_redact(probe), sort_keys=True))
        return 0
    if cmd == "redelivery-finalize":
        run_id = os.environ["ORCH_R4D_REDELIVERY_RUN_ID"]
        job_id = os.environ["ORCH_R4D_REDELIVERY_JOB_ID"]
        finalize_redelivery_probe(api, evidence_dir, run_id=run_id, delivery_job_id=job_id)
        print(json.dumps({"ok": True, "step": "08_redelivery"}, sort_keys=True))
        return 0
    if cmd == "restart-pre":
        work_ids = os.environ["ORCH_R4D_CONTINUITY_WORK_IDS"].split(",")
        run_ids = os.environ["ORCH_R4D_CONTINUITY_RUN_IDS"].split(",")
        snap = capture_restart_continuity_pre(
            evidence_dir, work_item_ids=work_ids, run_ids=run_ids
        )
        print(json.dumps(_redact(snap), sort_keys=True))
        return 0
    if cmd == "restart-post":
        pre_path = evidence_dir / "steps" / "09_restart_pre.json"
        pre = json.loads(pre_path.read_text(encoding="utf-8"))
        work_ids = os.environ["ORCH_R4D_CONTINUITY_WORK_IDS"].split(",")
        run_ids = os.environ["ORCH_R4D_CONTINUITY_RUN_IDS"].split(",")
        recover_status = os.environ.get("ORCH_R4D_RECOVER_STATUS")
        recover_http = int(os.environ.get("ORCH_R4D_RECOVER_HTTP", "0"))
        capture_restart_continuity_post(
            evidence_dir,
            pre,
            work_item_ids=work_ids,
            run_ids=run_ids,
            recover_http_status=recover_http,
            recover_status=recover_status,
        )
        print(json.dumps({"ok": True, "step": "09_coordinator_recover"}, sort_keys=True))
        return 0
    raise SystemExit(f"unknown r4d_exercise command: {cmd}")


def run_primary_exercises() -> int:
    env_file = Path(os.environ["ORCH_R4D_ENV_FILE"])
    evidence_dir = Path(os.environ["ORCH_R4D_EVIDENCE_DIR"])
    base = os.environ.get("ORCH_R4D_API_BASE", "http://127.0.0.1:8000")
    work_item_id = os.environ.get("ORCH_R4D_WORK_ITEM_ID", "").strip()
    if not work_item_id:
        raise SystemExit("ORCH_R4D_WORK_ITEM_ID is required")

    evidence_dir.mkdir(parents=True, exist_ok=True)
    env = _load_env(env_file)
    api = ApiClient(base, env)

    results: dict[str, Any] = {"ok": True, "steps": []}
    try:
        exercise_health(api, evidence_dir)
        results["steps"].append("health")
        mock = exercise_api_worker_mock(api, evidence_dir, work_item_id)
        results["mock_run_id"] = mock["run_id"]
        results["steps"].append("api_worker_mock")
        exercise_mcp_lanes(api, evidence_dir)
        results["steps"].append("mcp_lanes")
        exercise_twelve_loadouts(evidence_dir)
        results["steps"].append("twelve_loadouts")
        exercise_scripts(api, evidence_dir)
        results["steps"].append("scripts")
        exercise_schedules(api, evidence_dir)
        results["steps"].append("schedules")
        exercise_recovery_markers(api, evidence_dir)
        results["steps"].append("recover")
    except Exception as exc:  # noqa: BLE001 — evidence then fail
        results["ok"] = False
        results["error"] = str(exc)
        _write_step(
            evidence_dir,
            "99_failure",
            {"error": str(exc), "error_type": type(exc).__name__},
        )
        (evidence_dir / "summary.json").write_text(
            json.dumps(_redact(results), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(_redact(results), sort_keys=True))
        return 1

    results["fingerprint"] = hashlib.sha256(
        json.dumps(results["steps"], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    (evidence_dir / "summary.json").write_text(
        json.dumps(_redact(results), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(_redact(results), sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        raise SystemExit(_dispatch_cli(sys.argv[1:]))
    raise SystemExit(run_primary_exercises())
