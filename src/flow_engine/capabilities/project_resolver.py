"""Logical project resolution via machine-local configuration ."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


class ProjectResolverError(Exception):
    """Project resolution failure."""


@dataclass(frozen=True)
class ProjectBinding:
    logical_id: str
    checkout_path: Path
    engine_project_name: str | None = None


@dataclass(frozen=True)
class ProjectResolution:
    binding: ProjectBinding
    source: str


class ProjectResolver:
    """Resolve logical project IDs from explicit config and working context."""

    def __init__(
        self,
        config_path: Path | str | None = None,
        *,
        active_project_id: str | None = None,
    ) -> None:
        self._config_path = self._resolve_config_path(config_path)
        self._active_project_id = active_project_id or os.environ.get("FLOW_ACTIVE_PROJECT_ID")
        self._bindings = self._load_bindings(self._config_path)

    @staticmethod
    def _resolve_config_path(explicit: Path | str | None) -> Path:
        if explicit is not None:
            return Path(explicit).expanduser()
        env_path = os.environ.get("FLOW_PROJECTS_CONFIG")
        if env_path:
            return Path(env_path).expanduser()
        return Path.home() / ".config" / "orchestrator" / "projects.json"

    @staticmethod
    def _load_bindings(config_path: Path) -> dict[str, ProjectBinding]:
        if not config_path.is_file():
            return {}
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectResolverError(f"invalid projects config: {config_path}") from exc
        projects = raw.get("projects")
        if not isinstance(projects, dict):
            raise ProjectResolverError("projects config must contain a 'projects' object")

        bindings: dict[str, ProjectBinding] = {}
        for logical_id, entry in projects.items():
            if not isinstance(entry, dict):
                raise ProjectResolverError(f"project entry must be an object: {logical_id}")
            checkout = entry.get("checkout_path")
            if not isinstance(checkout, str) or not checkout.strip():
                raise ProjectResolverError(
                    f"project {logical_id} requires non-empty checkout_path"
                )
            engine_name = entry.get("engine_project_name")
            bindings[str(logical_id)] = ProjectBinding(
                logical_id=str(logical_id),
                checkout_path=Path(checkout).expanduser(),
                engine_project_name=str(engine_name) if engine_name else None,
            )
        return bindings

    def list_logical_ids(self) -> list[str]:
        return sorted(self._bindings)

    def resolve(
        self,
        project_id: str | None = None,
        *,
        require_checkout: bool = True,
    ) -> ProjectResolution:
        logical_id = (project_id or self._active_project_id or "").strip()
        if not logical_id:
            if len(self._bindings) == 1:
                only_id = next(iter(self._bindings))
                return self._resolve_binding(only_id, require_checkout=require_checkout)
            raise ProjectResolverError(
                "ambiguous project context: specify project_id or FLOW_ACTIVE_PROJECT_ID"
            )
        if logical_id not in self._bindings:
            raise ProjectResolverError(f"unknown logical project_id: {logical_id}")
        return self._resolve_binding(logical_id, require_checkout=require_checkout)

    def _resolve_binding(
        self,
        logical_id: str,
        *,
        require_checkout: bool,
    ) -> ProjectResolution:
        binding = self._bindings[logical_id]
        if require_checkout and not binding.checkout_path.is_dir():
            raise ProjectResolverError(
                f"checkout unavailable for project {logical_id}: {binding.checkout_path}"
            )
        return ProjectResolution(binding=binding, source=str(self._config_path))
