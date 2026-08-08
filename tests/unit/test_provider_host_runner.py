from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from flow_engine.persistence.migrations import apply_migrations, list_tables
from flow_engine.providers.cli_registry import (
    EXECUTION_PROFILE_ACCEPTANCE,
    EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE,
    EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
)
from flow_engine.providers.host_runner import (
    HostRunner,
    HostRunnerServer,
    ProviderBinding,
    UnixSocketClient,
    authorize_provider_packet,
    canonical_invocation_packet,
    canonical_json,
    digest_json,
    provider_argv,
    validate_write_set,
)


def _fake_cli(tmp_path: Path, provider: str, version: str | None = None) -> Path:
    versions = {
        "codex": version or "0.146.0",
        "cursor": version or "2026.08.04-aaa8809",
        "claude": version or "2.1.212",
    }
    path = tmp_path / provider
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--version' in sys.argv:\n"
        f" print('{provider} {versions[provider]}'); raise SystemExit(0)\n"
        "print(json.dumps({'type':'result','provider_call_id':'call-1','result':'ok'}))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _binding(
    tmp_path: Path,
    provider: str = "codex",
    *,
    execution_profile: str = EXECUTION_PROFILE_ACCEPTANCE,
    cli_version: str | None = None,
) -> ProviderBinding:
    version_pins = {
        "codex": cli_version or "0.146.0",
        "cursor": cli_version or "2026.08.04-aaa8809",
        "claude": cli_version or "2.1.212",
    }
    return ProviderBinding(
        provider=provider,
        executable=_fake_cli(tmp_path, provider, version_pins[provider]),
        model=f"{provider}-test-model",
        workspace_root=tmp_path,
        socket_path=tmp_path / "sockets" / f"{provider}.sock",
        auth_token="test-only-host-token",
        cli_version_pin=version_pins[provider],
        allowed_models=(f"{provider}-test-model",),
        expected_peer_uid=os.getuid(),
        execution_profile=execution_profile,
    )


def _invoke_packet(
    runner: HostRunner,
    handshake: dict[str, object],
    *,
    invocation_id: str = "inv-1",
    attempt_id: str = "att-1",
    task_packet: dict[str, object] | None = None,
    cwd: str = ".",
) -> dict[str, object]:
    snapshot = handshake["snapshot_digest"]
    task = task_packet or {"objective": "task"}
    packet_digest = digest_json(task)
    binding_fields = {
        "provider": runner.binding.provider,
        "attempt_id": attempt_id,
        "invocation_id": invocation_id,
        "credit_reservation_id": f"credit-{invocation_id}",
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot,
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
        "execution_profile": runner.binding.execution_profile,
    }
    return {
        **binding_fields,
        "binding_digest": digest_json(binding_fields),
        "task_packet": task,
        "cwd": cwd,
        "execution_profile": runner.binding.execution_profile,
    }


def test_migration_007_is_additive_and_persists_provider_fields(kernel_db) -> None:
    conn = kernel_db.connection
    apply_migrations(conn)
    assert "provider_runner_events" in list_tables(conn)
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(provider_invocations)").fetchall()
    }
    assert {
        "invocation_packet_json",
        "adapter_snapshot_json",
        "adapter_snapshot_digest",
        "provider_call_id",
        "heartbeat_at",
        "reconciliation_required",
    } <= columns


def test_canonical_packet_redacts_credentials_and_rejects_private_paths() -> None:
    packet = canonical_invocation_packet(
        {"objective": "review", "api_token": "super-secret", "nested": {"count": 1}}
    )
    assert packet["api_token"] == "[REDACTED]"
    assert "super-secret" not in json.dumps(packet)
    with pytest.raises(ValueError, match="absolute paths"):
        canonical_invocation_packet({"workspace": "/home/private/repo"})


def test_recursive_normalized_credential_redaction() -> None:
    packet = canonical_invocation_packet({
        "nested": {
            "Authorization": "Bearer abc",
            "apiKey": "one",
            "access-key": "two",
            "PRIVATE_KEY": "three",
            "x_api_key": "four",
            "list": [{"Cookie": "session=five"}],
            "note": "Authorization: Bearer six",
        }
    })
    encoded = json.dumps(packet)
    for secret in ("abc", "one", "two", "three", "four", "five", "six"):
        assert secret not in encoded


@pytest.mark.parametrize("provider", ["codex", "cursor", "claude"])
def test_cli_bindings_are_noninteractive_structured(provider: str, tmp_path: Path) -> None:
    binding = _binding(tmp_path, provider)
    via_stdin = provider == "claude"
    argv = provider_argv(binding, "bounded task", prompt_via_stdin=via_stdin)
    assert argv[0] == str(binding.executable)
    assert binding.model in argv
    if provider == "claude":
        assert "bounded task" not in argv
    else:
        assert "bounded task" in argv
    if provider == "codex":
        assert argv[1:3] == ("exec", "--json")
        assert ("--sandbox", "read-only") == (
            argv[argv.index("--sandbox")],
            argv[argv.index("--sandbox") + 1],
        )
    elif provider == "cursor":
        assert "--mode" in argv and argv[argv.index("--mode") + 1] == "ask"
        assert "--force" not in argv
        assert "--trust" in argv
    else:
        assert "--print" in argv
        assert "--verbose" in argv
        assert "stream-json" in argv
        assert "bounded task" not in argv  # claude prompt is supplied via stdin
        denied = argv[argv.index("--disallowedTools") + 1]
        assert all(tool in denied for tool in ("Read", "Edit", "Write", "Bash"))


def test_handshake_is_immutable_nonsecret_snapshot(tmp_path: Path) -> None:
    handshake = HostRunner(_binding(tmp_path)).handshake()
    snapshot = handshake["snapshot"]
    assert handshake["snapshot_digest"] == digest_json(snapshot)
    assert snapshot["auth_ready"] is True
    assert snapshot["provider"] == "codex"
    encoded = json.dumps(handshake)
    assert "test-only-host-token" not in encoded
    assert str(tmp_path) not in encoded


def test_invoke_redacts_and_replays_without_second_process(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    binding.executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv: print('codex 0.146.0'); raise SystemExit(0)\n"
        "print('{\"type\":\"result\",\"result\":\"token=leaked-value\"}')\n",
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    packet = _invoke_packet(runner, handshake)
    first = runner.invoke(packet)
    binding.executable.unlink()
    second = runner.invoke(packet)
    assert first == second
    assert "[REDACTED]" in first["redacted_output"]
    assert "leaked-value" not in json.dumps(first)


def test_socket_mode_peer_and_authenticated_handshake(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    server = HostRunnerServer(HostRunner(binding))
    thread = threading.Thread(target=server.serve_once)
    thread.start()
    for _ in range(100):
        if binding.socket_path.exists():
            break
        thread.join(0.01)
    response = UnixSocketClient(
        "codex", binding.socket_path, binding.auth_token
    ).request("handshake")
    thread.join(2)
    assert not thread.is_alive()
    assert response["snapshot"]["provider"] == "codex"
    assert stat.S_IMODE(binding.socket_path.parent.stat().st_mode) == 0o700
    assert not binding.socket_path.exists()


def test_cwd_escape_is_denied(tmp_path: Path) -> None:
    runner = HostRunner(
        _binding(
            tmp_path,
            "cursor",
            execution_profile=EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
        )
    )
    with pytest.raises(ValueError, match="escapes"):
        handshake = runner.handshake()
        packet = _invoke_packet(
            runner,
            handshake,
            invocation_id="inv-escape",
            attempt_id="att-escape",
            task_packet={"objective": "task", "write_set": ["src/"]},
            cwd="..",
        )
        runner.invoke(packet)


def test_signed_envelope_replay_and_expiry_are_denied(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    server = HostRunnerServer(HostRunner(binding))
    now = int(time.time())
    unsigned = {
        "payload": {"operation": "handshake"},
        "nonce": "n" * 32,
        "issued_at": now,
        "expires_at": now + 30,
    }
    envelope = {
        **unsigned,
        "signature": hmac.new(
            binding.auth_token.encode(),
            canonical_json(unsigned).encode(),
            hashlib.sha256,
        ).hexdigest(),
    }
    server._verify_envelope(envelope)
    with pytest.raises(PermissionError, match="replayed"):
        server._verify_envelope(envelope)
    expired_unsigned = {**unsigned, "nonce": "x" * 32, "expires_at": now - 1}
    expired = {
        **expired_unsigned,
        "signature": hmac.new(
            binding.auth_token.encode(),
            canonical_json(expired_unsigned).encode(),
            hashlib.sha256,
        ).hexdigest(),
    }
    with pytest.raises(PermissionError, match="expired"):
        server._verify_envelope(expired)


def test_provider_policy_requires_sensitivity_and_explicit_provider() -> None:
    authorize_provider_packet(
        {"sensitivity": "internal", "allowed_providers": ["codex"]}, "codex"
    )
    with pytest.raises(PermissionError):
        authorize_provider_packet({"allowed_providers": ["codex"]}, "codex")
    with pytest.raises(PermissionError):
        authorize_provider_packet(
            {"sensitivity": "internal", "allowed_providers": ["claude"]}, "codex"
        )


def test_reconcile_survives_runner_restart(tmp_path: Path) -> None:
    binding = _binding(tmp_path)
    first_runner = HostRunner(binding)
    handshake = first_runner.handshake()
    packet = _invoke_packet(
        first_runner, handshake, invocation_id="inv-durable", attempt_id="att-durable"
    )
    result = first_runner.invoke(packet)
    restarted = HostRunner(binding)
    assert restarted.reconcile("inv-durable") == result


def test_prompt_substitution_rejected_before_cli_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    packet = _invoke_packet(runner, handshake, attempt_id="att-sub", invocation_id="inv-sub")
    packet["task_packet"] = {"objective": "substituted"}
    monkeypatch.setattr(
        "flow_engine.providers.host_runner.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("provider CLI process launched"),
    )
    with pytest.raises(PermissionError, match="packet digest mismatch"):
        runner.invoke(packet)


def test_acceptance_workspace_is_isolated_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    isolated = tmp_path / "dedicated-acceptance"

    def make_workspace(*args, **kwargs):
        _ = args, kwargs
        isolated.mkdir(mode=0o700)
        return str(isolated)

    monkeypatch.setattr(
        "flow_engine.providers.host_runner.tempfile.mkdtemp", make_workspace
    )
    runner.invoke(
        _invoke_packet(
            runner,
            handshake,
            attempt_id="att-isolated",
            invocation_id="inv-isolated",
            task_packet={"objective": "read-only acceptance"},
        )
    )
    assert not isolated.exists()


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied")])
def test_acceptance_workspace_removed_when_popen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    isolated = tmp_path / f"accept-{type(error).__name__}"
    monkeypatch.setattr(
        "flow_engine.providers.host_runner.tempfile.mkdtemp",
        lambda **kwargs: (isolated.mkdir(mode=0o700) or str(isolated)),
    )
    monkeypatch.setattr(
        "flow_engine.providers.host_runner.subprocess.Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )
    with pytest.raises(type(error)):
        runner.invoke(
            _invoke_packet(
                runner,
                handshake,
                attempt_id="att-launch",
                invocation_id=f"inv-{type(error).__name__}",
                task_packet={"objective": "launch failure"},
            )
        )
    assert not isolated.exists()


def test_symlink_acceptance_workspace_is_unlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "accept-link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        "flow_engine.providers.host_runner.tempfile.mkdtemp", lambda **kwargs: str(link)
    )
    with pytest.raises(PermissionError, match="symlink"):
        runner.invoke(
            _invoke_packet(
                runner,
                handshake,
                attempt_id="att-link",
                invocation_id="inv-link",
                task_packet={"objective": "symlink rejection"},
            )
        )
    assert not link.exists()
    assert target.exists()


def test_handshake_rejects_unpinned_cli_version(tmp_path: Path) -> None:
    binding = _binding(tmp_path, cli_version="0.144.6")
    binding.executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv: print('codex 0.146.0'); raise SystemExit(0)\n",
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    with pytest.raises(RuntimeError, match="does not match"):
        HostRunner(binding).handshake()


def test_invoke_rejects_profile_upgrade(tmp_path: Path) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    packet = _invoke_packet(runner, handshake)
    packet["execution_profile"] = EXECUTION_PROFILE_CURSOR_IMPLEMENTATION
    with pytest.raises(PermissionError, match="execution profile"):
        runner.invoke(packet)


def test_cursor_implementation_argv_uses_agent_mode(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor", execution_profile=EXECUTION_PROFILE_CURSOR_IMPLEMENTATION)
    argv = provider_argv(binding, "implement slice")
    assert argv[argv.index("--mode") + 1] == "agent"
    assert "--force" in argv
    assert "--trust" not in argv


def test_claude_review_argv_disallows_edit_write_only(tmp_path: Path) -> None:
    binding = _binding(
        tmp_path, "claude", execution_profile=EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE
    )
    argv = provider_argv(binding, "review", prompt_via_stdin=True)
    denied = argv[argv.index("--disallowedTools") + 1]
    assert denied == "Edit,Write"
    assert "Read" not in denied


def test_write_set_validation_rejects_escape(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="relative"):
        validate_write_set(["/etc/passwd"], tmp_path)
    with pytest.raises(ValueError, match="escapes"):
        validate_write_set(["../outside"], tmp_path)


def test_write_set_violation_fails_without_deleting_evidence(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    src = worktree / "src"
    src.mkdir()
    (src / "allowed.py").write_text("x=1\n", encoding="utf-8")
    import subprocess as sp

    sp.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    sp.run(["git", "add", "src/allowed.py"], cwd=worktree, check=True)
    sp.run(
        ["git", "-c", "user.email=test@test", "-c", "user.name=test", "commit", "-m", "init"],
        cwd=worktree,
        check=True,
    )
    (worktree / "undeclared.py").write_text("y=2\n", encoding="utf-8")

    binding = ProviderBinding(
        provider="cursor",
        executable=_fake_cli(tmp_path, "cursor"),
        model="cursor-test-model",
        workspace_root=worktree,
        socket_path=tmp_path / "cursor.sock",
        auth_token="test-only-host-token",
        cli_version_pin="2026.08.04-aaa8809",
        allowed_models=("cursor-test-model",),
        execution_profile=EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
    )
    runner = HostRunner(binding)
    handshake = runner.handshake()
    packet = _invoke_packet(
        runner,
        handshake,
        invocation_id="inv-write-set",
        task_packet={"objective": "task", "write_set": ["src/"]},
    )
    result = runner.invoke(packet)
    assert result["write_set_validation"] == "fail"
    assert "undeclared.py" in result["undeclared_paths"]
    assert result["outcome"] == "failed"
    assert result["redacted_output"]
