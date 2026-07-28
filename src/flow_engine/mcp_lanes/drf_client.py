"""HTTP client used by MCP lane containers to call DRF (never SQLite)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin


class McpLaneDrfClient:
    """Thin DRF adapter for a single MCP lane service identity."""

    def __init__(
        self,
        *,
        base_url: str,
        lane_id: str,
        service_token: str,
        timeout_sec: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.lane_id = lane_id
        self.service_token = service_token
        self.timeout_sec = timeout_sec

    @classmethod
    def from_env(cls) -> McpLaneDrfClient:
        base_url = os.environ.get("ORCH_API_BASE_URL", "").strip()
        lane_id = os.environ.get("ORCH_MCP_LANE_ID", "").strip()
        service_token = os.environ.get("ORCH_MCP_LANE_TOKEN", "").strip()
        if not base_url or not lane_id or not service_token:
            raise RuntimeError(
                "MCP lane client requires ORCH_API_BASE_URL, ORCH_MCP_LANE_ID, "
                "and ORCH_MCP_LANE_TOKEN"
            )
        # Fail closed: lane containers must not be configured for SQLite/providers.
        if os.environ.get("FLOW_DB_PATH", "").strip():
            raise RuntimeError("MCP lane must not set FLOW_DB_PATH")
        if os.environ.get("COORDINATOR_URL", "").strip():
            raise RuntimeError("MCP lane must not set COORDINATOR_URL (call DRF only)")
        if os.environ.get("ORCH_WORKER_SERVICE_TOKEN", "").strip():
            raise RuntimeError("MCP lane must not receive worker service credentials")
        return cls(base_url=base_url, lane_id=lane_id, service_token=service_token)

    def _request(
        self,
        method: str,
        path: str,
        *,
        initiating_token: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = urljoin(self.base_url, path.lstrip("/"))
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {initiating_token}",
            "X-Orchestrator-MCP-Service-Token": self.service_token,
            "X-Orchestrator-MCP-Lane-Id": self.lane_id,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8")
            try:
                parsed = json.loads(err_body) if err_body else {}
            except json.JSONDecodeError:
                parsed = {"error": err_body, "status_code": exc.code}
            if isinstance(parsed, dict):
                parsed.setdefault("status_code", exc.code)
                return parsed
            return {"error": err_body, "status_code": exc.code}

    def get_snapshot(self, *, initiating_token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/mcp/lanes/{self.lane_id}/snapshot",
            initiating_token=initiating_token,
        )

    def list_tools(self, *, initiating_token: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/mcp/lanes/{self.lane_id}/tools",
            initiating_token=initiating_token,
        )

    def invoke(
        self,
        *,
        initiating_token: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        expected_snapshot_digest: str | None = None,
        department: str | None = None,
        loadout_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "tool": tool_name,
            "arguments": arguments or {},
        }
        if expected_snapshot_digest:
            body["expected_snapshot_digest"] = expected_snapshot_digest
        if department:
            body["department"] = department
        if loadout_id:
            body["loadout_id"] = loadout_id
        return self._request(
            "POST",
            f"/api/v1/mcp/lanes/{self.lane_id}/tools/invoke",
            initiating_token=initiating_token,
            body=body,
        )
