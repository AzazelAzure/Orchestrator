"""MCP stdio integration tests using the maintained Python MCP SDK client."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp.client.stdio import get_default_environment, stdio_client

from flow_engine.capabilities.transport import (
    APPROVED_MCP_TOOL_NAMES,
    MCP_TOOL_REPO_HEALTH,
)
from mcp import ClientSession, StdioServerParameters


def _write_projects_config(path: Path, checkout: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "projects": {
                    "demo_project": {
                        "checkout_path": str(checkout),
                        "engine_project_name": "demo_project",
                    }
                }
            }
        ),
        encoding="utf-8",
    )


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test User"], check=True)
    sample = path / "README.md"
    sample.write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True)


def _server_env(tmp_path: Path, config: Path) -> dict[str, str]:
    env = get_default_environment()
    env["FLOW_DB_PATH"] = str(tmp_path / "state.db")
    env["FLOW_PROJECTS_CONFIG"] = str(config)
    return env


@pytest.mark.anyio
async def test_stdio_client_lists_exact_five_tools_and_calls_repo_health(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "flow_engine.mcp.server"],
        env=_server_env(tmp_path, config),
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tool_names = sorted(tool.name for tool in tools_result.tools)
            assert tool_names == list(APPROVED_MCP_TOOL_NAMES)

            call_result = await session.call_tool(
                MCP_TOOL_REPO_HEALTH,
                {"project_id": "demo_project", "actor": "mcp:integration-test"},
            )
            assert call_result.isError is False
            assert call_result.content
            text_block = call_result.content[0]
            assert text_block.type == "text"
            payload = json.loads(text_block.text)
            assert payload["code"] == "ok"
            assert payload["capability"] == "repo_health"
            assert payload["project_id"] == "demo_project"
            assert payload["status"] == "ready"
            assert "captured_at" in payload
            assert isinstance(payload["data"]["dirty"], bool)
