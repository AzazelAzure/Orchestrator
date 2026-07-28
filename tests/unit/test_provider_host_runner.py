from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from flow_engine.persistence.migrations import apply_migrations, list_tables
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
)


def _fake_cli(tmp_path: Path, provider: str) -> Path:
    versions = {
        "codex": "0.144.6",
        "cursor": "2026.07.23",
        "claude": "2.1.212",
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


def _binding(tmp_path: Path, provider: str = "codex") -> ProviderBinding:
    return ProviderBinding(
        provider=provider,
        executable=_fake_cli(tmp_path, provider),
        model=f"{provider}-test-model",
        workspace_root=tmp_path,
        socket_path=tmp_path / "sockets" / f"{provider}.sock",
        auth_token="test-only-host-token",
        allowed_models=(f"{provider}-test-model",),
        expected_peer_uid=os.getuid(),
    )


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
        "if '--version' in sys.argv: print('codex 0.144.6'); raise SystemExit(0)\n"
        "print('{\"type\":\"result\",\"result\":\"token=leaked-value\"}')\n",
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    snapshot = handshake["snapshot_digest"]
    task_packet = {"objective": "task"}
    packet_digest = digest_json(task_packet)
    packet = {
        "invocation_id": "inv-1",
        "attempt_id": "att-1",
        "provider": "codex",
        "credit_reservation_id": "credit-1",
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot,
        "task_packet": task_packet,
        "cwd": ".",
    }
    packet["binding_digest"] = digest_json({
        "provider": "codex",
        "attempt_id": "att-1",
        "invocation_id": "inv-1",
        "credit_reservation_id": "credit-1",
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot,
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    })
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
    runner = HostRunner(replace(_binding(tmp_path), acceptance_mode=False))
    with pytest.raises(ValueError, match="escapes"):
        handshake = runner.handshake()
        task_packet = {"objective": "task"}
        packet_digest = digest_json(task_packet)
        packet = {
                "invocation_id": "inv-escape",
                "attempt_id": "att-escape",
                "provider": "codex",
                "credit_reservation_id": "credit-escape",
                "packet_digest": packet_digest,
                "snapshot_digest": handshake["snapshot_digest"],
                "task_packet": task_packet,
                "cwd": "..",
            }
        packet["binding_digest"] = digest_json({
            "provider": "codex", "attempt_id": "att-escape",
            "invocation_id": "inv-escape",
            "credit_reservation_id": "credit-escape",
            "packet_digest": packet_digest,
            "snapshot_digest": handshake["snapshot_digest"],
            "resolved_model": handshake["snapshot"]["resolved_model"],
            "adapter_version": handshake["snapshot"]["adapter_version"],
        })
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
    snapshot = handshake["snapshot_digest"]
    task_packet = {"objective": "task"}
    packet_digest = digest_json(task_packet)
    packet = {
        "invocation_id": "inv-durable",
        "attempt_id": "att-durable",
        "provider": "codex",
        "credit_reservation_id": "credit-durable",
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot,
        "task_packet": task_packet,
        "cwd": ".",
    }
    packet["binding_digest"] = digest_json({
        "provider": "codex", "attempt_id": "att-durable",
        "invocation_id": "inv-durable",
        "credit_reservation_id": "credit-durable",
        "packet_digest": packet_digest,
        "snapshot_digest": snapshot,
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    })
    result = first_runner.invoke(packet)
    restarted = HostRunner(binding)
    assert restarted.reconcile("inv-durable") == result


def test_prompt_substitution_rejected_before_cli_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    original = {"objective": "approved"}
    digest = digest_json(original)
    binding = {
        "provider": "codex",
        "attempt_id": "att-sub",
        "invocation_id": "inv-sub",
        "credit_reservation_id": "credit-sub",
        "packet_digest": digest,
        "snapshot_digest": handshake["snapshot_digest"],
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    }
    packet = {
        **binding,
        "binding_digest": digest_json(binding),
        "task_packet": {"objective": "substituted"},
    }
    monkeypatch.setattr(
        "flow_engine.providers.host_runner.subprocess.Popen",
        lambda *args, **kwargs: pytest.fail("provider CLI process launched"),
    )
    with pytest.raises(PermissionError, match="packet digest mismatch"):
        runner.invoke(packet)


def test_acceptance_workspace_is_isolated_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _binding(tmp_path)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    task_packet = {"objective": "read-only acceptance"}
    packet_digest = digest_json(task_packet)
    binding_fields = {
        "provider": "codex",
        "attempt_id": "att-isolated",
        "invocation_id": "inv-isolated",
        "credit_reservation_id": "credit-isolated",
        "packet_digest": packet_digest,
        "snapshot_digest": handshake["snapshot_digest"],
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    }
    isolated = tmp_path / "dedicated-acceptance"

    def make_workspace(*args, **kwargs):
        _ = args, kwargs
        isolated.mkdir(mode=0o700)
        return str(isolated)

    monkeypatch.setattr(
        "flow_engine.providers.host_runner.tempfile.mkdtemp", make_workspace
    )
    runner.invoke({
        **binding_fields,
        "binding_digest": digest_json(binding_fields),
        "task_packet": task_packet,
    })
    assert not isolated.exists()


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied")])
def test_acceptance_workspace_removed_when_popen_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError
) -> None:
    runner = HostRunner(_binding(tmp_path))
    handshake = runner.handshake()
    task_packet = {"objective": "launch failure"}
    packet_digest = digest_json(task_packet)
    binding_fields = {
        "provider": "codex", "attempt_id": "att-launch",
        "invocation_id": f"inv-{type(error).__name__}",
        "credit_reservation_id": "credit-launch",
        "packet_digest": packet_digest,
        "snapshot_digest": handshake["snapshot_digest"],
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    }
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
        runner.invoke({
            **binding_fields,
            "binding_digest": digest_json(binding_fields),
            "task_packet": task_packet,
        })
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
    task_packet = {"objective": "symlink rejection"}
    packet_digest = digest_json(task_packet)
    binding_fields = {
        "provider": "codex", "attempt_id": "att-link",
        "invocation_id": "inv-link", "credit_reservation_id": "credit-link",
        "packet_digest": packet_digest,
        "snapshot_digest": handshake["snapshot_digest"],
        "resolved_model": handshake["snapshot"]["resolved_model"],
        "adapter_version": handshake["snapshot"]["adapter_version"],
    }
    with pytest.raises(PermissionError, match="symlink"):
        runner.invoke({
            **binding_fields,
            "binding_digest": digest_json(binding_fields),
            "task_packet": task_packet,
        })
    assert not link.exists()
    assert target.exists()
