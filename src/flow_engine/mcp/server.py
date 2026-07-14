"""Stdio MCP server for read-only project capabilities."""

from __future__ import annotations

import os
import sys
from typing import Any

from flow_engine.capabilities.service import CapabilityService
from flow_engine.capabilities.transport import (
    DEFAULT_CAPABILITY_TIMEOUT_SEC,
    MCP_TOOL_TO_CAPABILITY,
    TOOL_DESCRIPTIONS,
    TOOL_INPUT_SCHEMAS,
    build_request_from_tool,
    dispatch_with_timeout,
    serialize_result,
)
from flow_engine.cli.context import resolve_db_path

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

SERVER_NAME = "orchestrator"
SERVER_VERSION = "0.1.0"


def _resolve_timeout() -> float:
    raw = os.environ.get("FLOW_CAPABILITY_TIMEOUT_SEC", "")
    if not raw.strip():
        return DEFAULT_CAPABILITY_TIMEOUT_SEC
    try:
        return max(0.1, float(raw))
    except ValueError:
        return DEFAULT_CAPABILITY_TIMEOUT_SEC


def create_service() -> CapabilityService:
    db_path = resolve_db_path(os.environ.get("FLOW_DB_PATH"))
    projects_config = os.environ.get("FLOW_PROJECTS_CONFIG")
    return CapabilityService(
        db_path=db_path,
        projects_config=projects_config,
        provider_timeout_sec=min(_resolve_timeout(), DEFAULT_CAPABILITY_TIMEOUT_SEC),
    )


def handle_tool_call(
    service: CapabilityService,
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    timeout_sec: float = DEFAULT_CAPABILITY_TIMEOUT_SEC,
) -> dict[str, Any]:
    try:
        request = build_request_from_tool(tool_name, arguments)
    except ValueError as exc:
        from flow_engine.capabilities.envelope import (
            CapabilityError,
            CapabilityRequest,
            CapabilityResult,
            ResultCode,
        )

        fallback = CapabilityRequest(
            capability=MCP_TOOL_TO_CAPABILITY.get(tool_name, "unknown"),
            request_id="invalid",
            actor=str((arguments or {}).get("actor", "mcp:client")),
            project_id=str((arguments or {}).get("project_id", "")),
        )
        return serialize_result(
            CapabilityResult.failure(
                fallback,
                CapabilityError(ResultCode.INVALID_INPUT, str(exc)),
            )
        )
    result = dispatch_with_timeout(service, request, timeout_sec=timeout_sec)
    return serialize_result(result)


def build_server(service: CapabilityService, *, timeout_sec: float = DEFAULT_CAPABILITY_TIMEOUT_SEC) -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=tool_name,
                description=TOOL_DESCRIPTIONS[tool_name],
                inputSchema=schema,
            )
            for tool_name, schema in TOOL_INPUT_SCHEMAS.items()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        payload = handle_tool_call(service, name, arguments, timeout_sec=timeout_sec)
        return [types.TextContent(type="text", text=_json_dumps(payload))]

    return server


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True)


async def _run_stdio_server() -> None:
    service = create_service()
    server = build_server(service, timeout_sec=_resolve_timeout())
    init_options = InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_options)


def main() -> int:
    if MCP_IMPORT_ERROR is not None:
        print(
            "flowctl-mcp requires the optional mcp dependency; install with: pip install 'flow-engine[mcp]'",
            file=sys.stderr,
        )
        return 2
    anyio.run(_run_stdio_server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
