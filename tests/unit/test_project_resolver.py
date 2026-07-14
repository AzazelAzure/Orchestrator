"""Tests for logical project resolver ."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flow_engine.capabilities.project_resolver import ProjectResolver, ProjectResolverError


def _write_config(path: Path, projects: dict) -> None:
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def test_resolve_demo_project(tmp_path: Path) -> None:
    repo = tmp_path / "demo-checkout"
    repo.mkdir()
    config = tmp_path / "projects.json"
    _write_config(
        config,
        {
            "demo_project": {
                "checkout_path": str(repo),
                "engine_project_name": "demo_project",
            }
        },
    )
    resolver = ProjectResolver(config)
    resolution = resolver.resolve("demo_project")
    assert resolution.binding.logical_id == "demo_project"
    assert resolution.binding.checkout_path == repo


def test_missing_configuration_fails(tmp_path: Path) -> None:
    resolver = ProjectResolver(tmp_path / "missing.json")
    with pytest.raises(ProjectResolverError, match="unknown logical project_id"):
        resolver.resolve("demo_project")


def test_ambiguous_context_when_multiple_projects_configured(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    _write_config(
        config,
        {
            "demo_project": {"checkout_path": str(tmp_path / "a")},
            "other": {"checkout_path": str(tmp_path / "b")},
        },
    )
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    resolver = ProjectResolver(config)
    with pytest.raises(ProjectResolverError, match="ambiguous project context"):
        resolver.resolve()


def test_unavailable_checkout_fails(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    _write_config(
        config,
        {"demo_project": {"checkout_path": str(tmp_path / "missing-checkout")}},
    )
    resolver = ProjectResolver(config)
    with pytest.raises(ProjectResolverError, match="checkout unavailable"):
        resolver.resolve("demo_project")


def test_unknown_project_never_falls_back_to_other_entries(tmp_path: Path) -> None:
    config = tmp_path / "projects.json"
    checkout = tmp_path / "demo-checkout"
    checkout.mkdir()
    _write_config(
        config,
        {"demo_project": {"checkout_path": str(checkout), "engine_project_name": "demo_project"}},
    )
    resolver = ProjectResolver(config)
    with pytest.raises(ProjectResolverError, match="unknown logical project_id: missing"):
        resolver.resolve("missing")

    config_text = config.read_text(encoding="utf-8")
    assert "hfm" not in config_text.lower()
    assert "finance" not in config_text.lower()
