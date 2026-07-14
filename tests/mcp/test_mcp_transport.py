"""MCP transport tests requiring the optional mcp dependency."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from flow_engine.capabilities.service import CapabilityService
from flow_engine.capabilities.transport import (
    APPROVED_MCP_TOOL_NAMES,
    MCP_TOOL_OPEN_PRS,
    MCP_TOOL_REPO_HEALTH,
    MCP_TOOL_TO_CAPABILITY,
    TOOL_INPUT_SCHEMAS,
)
from flow_engine.mcp.server import MCP_IMPORT_ERROR, handle_tool_call, main


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


def test_mcp_dependency_available() -> None:
    assert MCP_IMPORT_ERROR is None


def test_tool_schemas_are_machine_readable() -> None:
    assert set(TOOL_INPUT_SCHEMAS) == set(MCP_TOOL_TO_CAPABILITY) == set(APPROVED_MCP_TOOL_NAMES)
    assert len(APPROVED_MCP_TOOL_NAMES) == 5
    for schema in TOOL_INPUT_SCHEMAS.values():
        assert schema["type"] == "object"
        assert schema.get("additionalProperties") is False
        for prop_schema in schema.get("properties", {}).values():
            if prop_schema.get("type") == "string":
                assert "maxLength" in prop_schema


def test_approved_tools_exclude_mutations_and_paths() -> None:
    forbidden = {
        "execute",
        "deploy",
        "dispatch",
        "review",
        "submodule",
        "mutate",
        "path",
        "hfm",
        "finance",
    }
    for tool_name in TOOL_INPUT_SCHEMAS:
        lowered = tool_name.lower()
        for token in forbidden:
            assert token not in lowered
        for prop in TOOL_INPUT_SCHEMAS[tool_name].get("properties", {}):
            assert "path" not in prop.lower()


def test_handle_tool_call_invalid_input_is_typed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(db_path=tmp_path / "state.db", projects_config=config)
    payload = handle_tool_call(service, MCP_TOOL_REPO_HEALTH, {"project_id": "", "actor": "mcp:test"})
    assert payload["code"] == "invalid_input"


def test_handle_tool_call_rejects_unknown_tool(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_projects_config(config, repo)
    service = CapabilityService(db_path=tmp_path / "state.db", projects_config=config)
    payload = handle_tool_call(
        service,
        "execute_command",
        {"project_id": "demo_project", "actor": "mcp:test"},
    )
    assert payload["code"] == "invalid_input"


def test_open_prs_schema_requires_github_coordinates() -> None:
    schema = TOOL_INPUT_SCHEMAS[MCP_TOOL_OPEN_PRS]
    assert "github_owner" in schema["required"]
    assert "github_repo" in schema["required"]


def test_flowctl_mcp_entrypoint_imports() -> None:
    from flow_engine.mcp.server import main as mcp_main

    assert callable(mcp_main)


def test_main_without_mcp_reports_install_hint(monkeypatch) -> None:
    monkeypatch.setattr("flow_engine.mcp.server.MCP_IMPORT_ERROR", ImportError("missing"))
    assert main() == 2
