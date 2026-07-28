"""Lane-scoped MCP stdio server — DRF only; no SQLite / provider access."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from flow_engine.mcp_lanes.catalog import LANE_IDS, load_lane_by_id
from flow_engine.mcp_lanes.drf_client import McpLaneDrfClient
from flow_engine.mcp_lanes.forbidden import FORBIDDEN_TOOL_NAMES
from flow_engine.mcp_lanes.snapshots import lane_tool_snapshot

try:
    import anyio
    import mcp.types as types
    from mcp.server import Server
    from mcp.server.lowlevel.server import NotificationOptions
    from mcp.server.models import InitializationOptions
    from mcp.server.stdio import stdio_server
except ImportError as _MCP_IMPORT_ERROR:
    anyio = None  # type: ignore[assignment]
    types = None  # type: ignore[assignment]
    Server = None  # type: ignore[assignment,misc]
    NotificationOptions = None  # type: ignore[assignment,misc]
    InitializationOptions = None  # type: ignore[assignment,misc]
    stdio_server = None  # type: ignore[assignment]
    MCP_IMPORT_ERROR: Exception | None = _MCP_IMPORT_ERROR
else:
    MCP_IMPORT_ERROR = None

SERVER_VERSION = "0.1.0"

# Model-visible tool schemas must never advertise or accept these keys.
SECRET_TOOL_ARG_KEYS = frozenset(
    {
        "initiating_token",
        "authorization",
        "bearer_token",
        "access_token",
        "api_key",
        "service_token",
        "mcp_service_token",
    }
)


def assert_lane_runtime_safe() -> None:
    """Refuse to start if SQLite/coordinator/broker credentials are projected."""
    banned = (
        "FLOW_DB_PATH",
        "COORDINATOR_URL",
        "ORCH_WORKER_SERVICE_TOKEN",
        "ORCH_API_SERVICE_TOKEN",
        "REDIS_PASSWORD",
        "CELERY_BROKER_URL",
    )
    present = [key for key in banned if os.environ.get(key, "").strip()]
    if present:
        raise RuntimeError(
            "MCP lane container must not receive SQLite/coordinator/provider/"
            f"broker credentials; unset: {', '.join(present)}"
        )


def resolve_session_initiating_token(
    *,
    initiating_token: str | None = None,
) -> str:
    """Trusted session/transport config only — never from tool-call payloads."""
    token = (initiating_token or os.environ.get("ORCH_MCP_INITIATING_TOKEN", "")).strip()
    return token


def redact_secret_tool_args(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with secret argument keys redacted for traces/logs."""
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key in SECRET_TOOL_ARG_KEYS or "token" in key.lower() or "secret" in key.lower():
            redacted[key] = "[REDACTED]"
        elif isinstance(value, dict):
            redacted[key] = redact_secret_tool_args(value)
        else:
            redacted[key] = value
    return redacted


def _tool_schema(_tool_name: str) -> dict[str, Any]:
    """Model-visible input schema — initiating auth is never an argument."""
    return {
        "type": "object",
        "properties": {
            "expected_snapshot_digest": {"type": "string"},
            "department": {
                "type": "string",
                "enum": ["admin-ops", "qa", "tech"],
            },
            "loadout_id": {"type": "string"},
            "arguments": {"type": "object"},
        },
        "additionalProperties": True,
    }


def build_lane_server(
    *,
    lane_id: str,
    client: McpLaneDrfClient,
    initiating_token: str | None = None,
) -> Server:
    if lane_id not in LANE_IDS:
        raise RuntimeError(f"invalid lane_id: {lane_id}")
    lane = load_lane_by_id(lane_id)
    tools = [t for t in lane["tools"] if t not in FORBIDDEN_TOOL_NAMES]
    # Deduplicate while preserving order (catalog must already be unique).
    seen: set[str] = set()
    unique_tools: list[str] = []
    for tool_name in tools:
        if tool_name not in seen:
            seen.add(tool_name)
            unique_tools.append(tool_name)
    tools = unique_tools
    snapshot = lane_tool_snapshot(lane_id)
    session_token = resolve_session_initiating_token(initiating_token=initiating_token)
    server = Server(f"orchestrator-mcp-{lane_id}")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool_name,
                description=(
                    f"Lane {lane_id} tool `{tool_name}` via DRF "
                    f"(snapshot {snapshot['snapshot_digest'][:12]}…)"
                ),
                inputSchema=_tool_schema(tool_name),
            )
            for tool_name in tools
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        args = dict(arguments or {})
        leaked = sorted(SECRET_TOOL_ARG_KEYS.intersection(args))
        if leaked:
            # Do not echo secret values into the tool result / traces.
            payload = {
                "status": "rejected",
                "error_code": "VALIDATION_FAILED",
                "error": (
                    "secret arguments must not appear in tool-call payloads; "
                    "initiating authentication is session-configured"
                ),
                "rejected_keys": leaked,
                "arguments_trace": redact_secret_tool_args(args),
            }
        elif not session_token:
            payload = {
                "status": "rejected",
                "error_code": "AUTH_REQUIRED",
                "error": (
                    "initiating authentication not configured for session "
                    "(set ORCH_MCP_INITIATING_TOKEN or pass trusted session token)"
                ),
            }
        elif name in FORBIDDEN_TOOL_NAMES:
            payload = {
                "status": "rejected",
                "error_code": "UNSUPPORTED_SURFACE",
                "error": f"tool {name} forbidden on MCP",
            }
        else:
            expected = args.pop("expected_snapshot_digest", None) or snapshot["snapshot_digest"]
            department = args.pop("department", None)
            loadout_id = args.pop("loadout_id", None)
            tool_args = args.pop("arguments", None)
            if tool_args is None:
                tool_args = args
            payload = client.invoke(
                initiating_token=session_token,
                tool_name=name,
                arguments=tool_args if isinstance(tool_args, dict) else {},
                expected_snapshot_digest=str(expected) if expected else None,
                department=str(department) if department else None,
                loadout_id=str(loadout_id) if loadout_id else None,
            )
        return [
            types.TextContent(
                type="text",
                text=json.dumps(payload, indent=2, sort_keys=True),
            )
        ]

    return server


async def _run_stdio(*, lane_id: str) -> None:
    assert_lane_runtime_safe()
    client = McpLaneDrfClient.from_env()
    if client.lane_id != lane_id:
        raise RuntimeError("ORCH_MCP_LANE_ID mismatch")
    server = build_lane_server(lane_id=lane_id, client=client)
    init_options = InitializationOptions(
        server_name=f"orchestrator-mcp-{lane_id}",
        server_version=SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def run_stdio(lane_id: str | None = None) -> int:
    if MCP_IMPORT_ERROR is not None:
        print(
            "lane MCP stdio requires optional mcp dependency; "
            "install with: pip install 'orchestrator[mcp]'",
            file=sys.stderr,
        )
        return 2
    resolved = (lane_id or os.environ.get("ORCH_MCP_LANE_ID", "")).strip()
    if resolved not in LANE_IDS:
        print(f"ORCH_MCP_LANE_ID must be one of {LANE_IDS}", file=sys.stderr)
        return 2
    assert_lane_runtime_safe()
    anyio.run(_run_stdio, resolved)
    return 0
