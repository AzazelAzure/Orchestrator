#!/usr/bin/env python3
"""Live acceptance for founder-authenticated registration over the API boundary."""

from __future__ import annotations

import json
import secrets
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from scripts.r4d_exercise import ApiClient, _redact
from scripts.verification_ladder import write_json


def _request(
    api_base: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    url = f"{api_base.rstrip('/')}{path}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            payload: Any = json.loads(raw) if raw else {}
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            payload = json.loads(raw) if raw else {"detail": str(exc)}
        except json.JSONDecodeError:
            payload = {"detail": raw or str(exc)}
        return exc.code, payload


def check_auth_founder_register(
    api: ApiClient,
    env: dict[str, str],
    *,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Run founder registration acceptance steps; optional per-step evidence JSON."""
    api_base = api.base
    founder_token = env["ORCH_TOKEN_FOUNDER"]
    worker_token = env.get("ORCH_TOKEN_WORKER", "")
    steps: list[dict[str, Any]] = []

    def record(name: str, payload: dict[str, Any]) -> None:
        steps.append({"step": name, **payload})
        if evidence_dir is not None:
            write_json(evidence_dir / f"auth_{name}.json", _redact(payload))

    status, body = _request(
        api_base,
        "POST",
        "/api/v1/auth/register",
        body={"username": "acceptance-anon", "password": "password123"},
    )
    anon_ok = status == 403 and body.get("error_code") == "AUTHZ_DENIED"
    record(
        "anon_register_denied",
        {"passed": anon_ok, "http_status": status, "error_code": body.get("error_code")},
    )

    status, body = _request(api_base, "GET", "/api/v1/auth/me", token=founder_token)
    founder_me_ok = status == 200 and body.get("kind") == "founder"
    record(
        "founder_me_ok",
        {"passed": founder_me_ok, "http_status": status, "kind": body.get("kind")},
    )

    fresh_username = f"live-{secrets.token_hex(4)}"
    status, body = _request(
        api_base,
        "POST",
        "/api/v1/auth/register",
        token=founder_token,
        body={"username": fresh_username, "password": "password123"},
    )
    founder_register_ok = status in {200, 202} and body.get("status") == "applied"
    record(
        "founder_register_applied",
        {
            "passed": founder_register_ok,
            "http_status": status,
            "status": body.get("status"),
            "username": fresh_username,
        },
    )

    non_founder_username = f"worker-{secrets.token_hex(4)}"
    status, body = _request(
        api_base,
        "POST",
        "/api/v1/auth/register",
        token=worker_token,
        body={"username": non_founder_username, "password": "password123"},
    )
    non_founder_ok = status == 403 and body.get("error_code") == "AUTHZ_DENIED"
    record(
        "non_founder_register_denied",
        {
            "passed": non_founder_ok,
            "http_status": status,
            "error_code": body.get("error_code"),
            "username": non_founder_username,
        },
    )

    anon_ops_status, _ = _request(api_base, "GET", "/ops/summary/")
    founder_ops_status, _ = _request(api_base, "GET", "/ops/summary/", token=founder_token)
    ops_ok = anon_ops_status in {401, 403} and founder_ops_status == 200
    record(
        "ops_summary_sanity",
        {
            "passed": ops_ok,
            "anon_http_status": anon_ops_status,
            "founder_http_status": founder_ops_status,
        },
    )

    passed = all(step.get("passed") for step in steps)
    return {"passed": passed, "steps": steps}
