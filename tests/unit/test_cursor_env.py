from __future__ import annotations

from pathlib import Path

import pytest

from flow_engine.providers.cursor_env import (
    CURSOR_API_KEY,
    UnsafeCursorEnvError,
    bootstrap_cursor_host_runner_env,
    cursor_env_path,
    load_cursor_api_key_from_env_file,
)
from flow_engine.providers.host_runner import (
    CURSOR_API_KEY_VAR,
    SAFE_ENV,
    HostRunner,
    ProviderBinding,
    provider_env_allowlist,
)


def _write_cursor_env(path: Path, value: str = "fake-cursor-key-for-tests") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{CURSOR_API_KEY}={value}\n", encoding="utf-8")
    path.chmod(0o600)


def test_provider_env_allowlist_cursor_includes_api_key() -> None:
    assert CURSOR_API_KEY_VAR in provider_env_allowlist("cursor")
    assert provider_env_allowlist("cursor")[: len(SAFE_ENV)] == SAFE_ENV


@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_provider_env_allowlist_non_cursor_unchanged(provider: str) -> None:
    assert provider_env_allowlist(provider) == SAFE_ENV
    assert CURSOR_API_KEY_VAR not in provider_env_allowlist(provider)


def test_cursor_binding_without_key_still_constructs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(CURSOR_API_KEY, raising=False)
    binding = ProviderBinding(
        provider="cursor",
        executable=tmp_path / "cursor-agent",
        model="composer-2.5",
        workspace_root=tmp_path,
        socket_path=tmp_path / "cursor.sock",
        auth_token="test-token",
        cli_version_pin="2026.08.04-aaa8809",
        allowed_models=("composer-2.5",),
    )
    binding.executable.write_text("#!/bin/sh\n", encoding="utf-8")
    binding.executable.chmod(0o700)
    assert CURSOR_API_KEY_VAR in binding.env_allowlist
    assert "HOME" in binding.env_allowlist
    child_env = HostRunner(binding)._environment()
    assert CURSOR_API_KEY not in child_env


def test_cursor_child_env_passes_api_key_when_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CURSOR_API_KEY, "fake-cursor-key-for-tests")
    monkeypatch.setenv("PATH", "/usr/bin")
    binding = ProviderBinding(
        provider="cursor",
        executable=tmp_path / "cursor-agent",
        model="composer-2.5",
        workspace_root=tmp_path,
        socket_path=tmp_path / "cursor.sock",
        auth_token="test-token",
        cli_version_pin="2026.08.04-aaa8809",
        allowed_models=("composer-2.5",),
    )
    binding.executable.write_text("#!/bin/sh\n", encoding="utf-8")
    binding.executable.chmod(0o700)
    child_env = HostRunner(binding)._environment()
    assert child_env[CURSOR_API_KEY] == "fake-cursor-key-for-tests"
    assert "PATH" in child_env
    assert "fake-cursor-key-for-tests" not in repr(binding)


def test_codex_child_env_does_not_pass_cursor_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CURSOR_API_KEY, "fake-cursor-key-for-tests")
    monkeypatch.setenv("PATH", "/usr/bin")
    binding = ProviderBinding(
        provider="codex",
        executable=tmp_path / "codex",
        model="codex-test-model",
        workspace_root=tmp_path,
        socket_path=tmp_path / "codex.sock",
        auth_token="test-token",
        cli_version_pin="0.146.0",
        allowed_models=("codex-test-model",),
    )
    binding.executable.write_text("#!/bin/sh\n", encoding="utf-8")
    binding.executable.chmod(0o700)
    child_env = HostRunner(binding)._environment()
    assert CURSOR_API_KEY not in child_env


def test_load_cursor_api_key_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".local" / "provider" / "cursor.env"
    _write_cursor_env(env_file, "only-cursor-key")
    assert load_cursor_api_key_from_env_file(tmp_path) == "only-cursor-key"
    assert load_cursor_api_key_from_env_file(tmp_path / "missing") is None


def test_load_cursor_api_key_ignores_other_variables(tmp_path: Path) -> None:
    env_file = tmp_path / ".local" / "provider" / "cursor.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        "SLACK_TOKEN=ignored\nexport CURSOR_API_KEY=scoped-key\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    assert load_cursor_api_key_from_env_file(tmp_path) == "scoped-key"


def test_bootstrap_cursor_host_runner_env_respects_existing_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".local" / "provider" / "cursor.env"
    _write_cursor_env(env_file, "from-file")
    target: dict[str, str] = {CURSOR_API_KEY: "already-set"}
    bootstrap_cursor_host_runner_env(tmp_path, environ=target)
    assert target[CURSOR_API_KEY] == "already-set"


def test_bootstrap_cursor_host_runner_env_loads_from_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".local" / "provider" / "cursor.env"
    _write_cursor_env(env_file, "from-file")
    target: dict[str, str] = {}
    bootstrap_cursor_host_runner_env(tmp_path, environ=target)
    assert target[CURSOR_API_KEY] == "from-file"


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("symlink", "symlink"),
        ("world_readable", "group/world-readable"),
    ],
)
def test_unsafe_cursor_env_file_is_rejected(
    tmp_path: Path, setup: str, match: str
) -> None:
    env_file = tmp_path / ".local" / "provider" / "cursor.env"
    target = tmp_path / "target"
    target.write_text(f"{CURSOR_API_KEY}=value\n", encoding="utf-8")
    target.chmod(0o600)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if setup == "symlink":
        env_file.symlink_to(target)
    else:
        env_file.write_text(f"{CURSOR_API_KEY}=value\n", encoding="utf-8")
        env_file.chmod(0o644)
    with pytest.raises(UnsafeCursorEnvError, match=match):
        load_cursor_api_key_from_env_file(tmp_path)


def test_cursor_env_path_resolves_under_root(tmp_path: Path) -> None:
    assert cursor_env_path(tmp_path).name == "cursor.env"
