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
    CLAUDE_ACCEPTANCE_MAX_BUDGET_USD,
    CLAUDE_ACCEPTANCE_MAX_TURNS,
    CLAUDE_RESULT_SUBTYPE_SUCCESS,
    CLAUDE_RESULT_SUBTYPES_ERROR,
    CLAUDE_REVIEW_MERGE_MAX_BUDGET_USD,
    CLAUDE_REVIEW_MERGE_MAX_TURNS,
    EXECUTION_PROFILE_ACCEPTANCE,
    EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE,
    EXECUTION_PROFILE_CODEX_ADMIN,
    EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
)
from flow_engine.providers.host_runner import (
    COORDINATOR_PROVIDER_RESULT_CAP_BYTES,
    CURSOR_EVENT_TYPES,
    DEFAULT_OUTPUT_CAP,
    MAX_FRAME_BYTES,
    MAX_LINE_BYTES,
    HostRunner,
    HostRunnerServer,
    ProviderBinding,
    UnixSocketClient,
    authorize_provider_packet,
    canonical_invocation_packet,
    canonical_json,
    digest_json,
    provider_argv,
    validate_provider_event,
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
        "print(json.dumps({'type':'result','subtype':'success','provider_call_id':'call-1','result':'ok'}))\n",
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
        assert "--skip-git-repo-check" in argv
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


def test_codex_acceptance_argv_includes_skip_git_repo_check(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "codex", execution_profile=EXECUTION_PROFILE_ACCEPTANCE)
    argv = provider_argv(binding, "acceptance probe")
    sandbox_idx = argv.index("--sandbox")
    assert argv[sandbox_idx - 1] == "--skip-git-repo-check"
    assert ("--sandbox", "read-only") == (argv[sandbox_idx], argv[sandbox_idx + 1])


def test_codex_admin_argv_omits_skip_git_repo_check(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "codex", execution_profile=EXECUTION_PROFILE_CODEX_ADMIN)
    argv = provider_argv(binding, "reconcile")
    assert "--skip-git-repo-check" not in argv
    assert ("--sandbox", "read-only") == (
        argv[argv.index("--sandbox")],
        argv[argv.index("--sandbox") + 1],
    )


def test_cursor_implementation_argv_uses_force_without_mode_flag(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor", execution_profile=EXECUTION_PROFILE_CURSOR_IMPLEMENTATION)
    argv = provider_argv(binding, "implement slice")
    assert "--force" in argv
    assert "--trust" not in argv
    assert "--mode" not in argv


def test_claude_review_argv_allows_bash(tmp_path: Path) -> None:
    binding = _binding(
        tmp_path, "claude", execution_profile=EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE
    )
    argv = provider_argv(binding, "review", prompt_via_stdin=True)
    denied = argv[argv.index("--disallowedTools") + 1]
    assert denied == "Edit,Write"
    assert "Bash" not in denied
    assert argv[argv.index("--max-turns") + 1] == CLAUDE_REVIEW_MERGE_MAX_TURNS
    assert argv[argv.index("--max-budget-usd") + 1] == CLAUDE_REVIEW_MERGE_MAX_BUDGET_USD


def test_claude_acceptance_argv_caps_max_turns_and_budget(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "claude", execution_profile=EXECUTION_PROFILE_ACCEPTANCE)
    argv = provider_argv(binding, "accept", prompt_via_stdin=True)
    assert argv[argv.index("--max-turns") + 1] == CLAUDE_ACCEPTANCE_MAX_TURNS
    assert argv[argv.index("--max-budget-usd") + 1] == CLAUDE_ACCEPTANCE_MAX_BUDGET_USD


def test_default_output_cap_within_coordinator_provider_result_cap() -> None:
    assert DEFAULT_OUTPUT_CAP <= COORDINATOR_PROVIDER_RESULT_CAP_BYTES
    assert COORDINATOR_PROVIDER_RESULT_CAP_BYTES == 524_288


@pytest.mark.parametrize(
    "subtype",
    [
        CLAUDE_RESULT_SUBTYPE_SUCCESS,
        *sorted(CLAUDE_RESULT_SUBTYPES_ERROR),
    ],
)
def test_claude_result_subtype_validation_accepts_registered(subtype: str) -> None:
    validate_provider_event(
        "claude",
        {
            "type": "result",
            "subtype": subtype,
            "provider_call_id": "call-registered",
        },
    )


@pytest.mark.parametrize("subtype", ["error", "success_with_extra", ""])
def test_claude_result_subtype_validation_rejects_unknown(subtype: str) -> None:
    with pytest.raises(ValueError, match="subtype invalid"):
        validate_provider_event(
            "claude",
            {"type": "result", "subtype": subtype, "provider_call_id": "call-bad"},
        )


def test_cursor_thinking_event_type_is_accepted() -> None:
    validate_provider_event(
        "cursor",
        {"type": "thinking", "session_id": "sess-thinking"},
    )


@pytest.mark.parametrize(
    "subtype",
    sorted(CLAUDE_RESULT_SUBTYPES_ERROR),
)
def test_claude_error_terminal_subtype_requires_reconciliation(
    tmp_path: Path, subtype: str
) -> None:
    binding = _binding(tmp_path, "claude")
    binding.executable.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv: print('claude 2.1.212'); raise SystemExit(0)\n"
        f"print('{{\"type\":\"result\",\"subtype\":\"{subtype}\",\"provider_call_id\":\"call-err\"}}')\n",
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    result = runner.invoke(
        _invoke_packet(
            runner,
            handshake,
            invocation_id=f"inv-{subtype}",
            attempt_id=f"att-{subtype}",
            task_packet={"objective": "terminal error"},
        )
    )
    assert result["reconciliation_required"] is True
    assert result["outcome"] != "complete"
    assert result["provider_call_id"] == "call-err"


def test_claude_success_terminal_subtype_can_complete(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "claude")
    runner = HostRunner(binding)
    handshake = runner.handshake()
    result = runner.invoke(
        _invoke_packet(
            runner,
            handshake,
            invocation_id="inv-success",
            attempt_id="att-success",
            task_packet={"objective": "terminal success"},
        )
    )
    assert result["reconciliation_required"] is False
    assert result["outcome"] == "complete"
    assert result["provider_call_id"] == "call-1"


def test_write_set_dot_allows_any_in_workspace_path(tmp_path: Path) -> None:
    assert validate_write_set(["."], tmp_path) == (".",)


def test_invoke_requires_git_evidence_when_profile_demands_it(tmp_path: Path) -> None:
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    binding = ProviderBinding(
        provider="cursor",
        executable=_fake_cli(tmp_path, "cursor"),
        model="cursor-test-model",
        workspace_root=nogit,
        socket_path=tmp_path / "sock",
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
        invocation_id="inv-nogit",
        task_packet={"objective": "task", "write_set": ["."]},
    )
    with pytest.raises(PermissionError, match="git workspace evidence"):
        runner.invoke(packet)


def test_claude_review_records_git_mutations_without_failing(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    import subprocess as sp

    sp.run(["git", "init"], cwd=worktree, capture_output=True, check=True)
    sp.run(
        ["git", "-c", "user.email=test@test", "-c", "user.name=test", "commit", "--allow-empty", "-m", "init"],
        cwd=worktree,
        check=True,
    )
    (worktree / "notes.txt").write_text("review\n", encoding="utf-8")

    binding = ProviderBinding(
        provider="claude",
        executable=_fake_cli(tmp_path, "claude"),
        model="claude-test-model",
        workspace_root=worktree,
        socket_path=tmp_path / "claude.sock",
        auth_token="test-only-host-token",
        cli_version_pin="2.1.212",
        allowed_models=("claude-test-model",),
        execution_profile=EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE,
    )
    runner = HostRunner(binding)
    handshake = runner.handshake()
    packet = _invoke_packet(
        runner,
        handshake,
        invocation_id="inv-review",
        task_packet={"objective": "gh pr review"},
    )
    result = runner.invoke(packet)
    assert result["workspace_mutations_detected"] is True
    assert "notes.txt" in result["changed_paths"]
    assert result.get("write_set_validation") is None
    assert result["outcome"] == "complete"


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


def _cursor_stream_script(body: str) -> str:
    return (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('cursor 2026.08.04-aaa8809')\n"
        "    raise SystemExit(0)\n"
        + body
    )


def test_cursor_event_contract_matches_observed_types() -> None:
    assert CURSOR_EVENT_TYPES == frozenset({
        "system", "user", "assistant", "tool_call", "result", "error", "thinking",
    })


def test_cursor_large_nonterminal_stream_completes(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor")
    binding.executable.write_text(
        _cursor_stream_script(
            "import json\n"
            "for i in range(300):\n"
            "    print(json.dumps({'type':'assistant','session_id':'s','text':'x'*900}))\n"
            "print(json.dumps({'type':'result','provider_call_id':'call-big'}))\n"
        ),
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    result = runner.invoke(
        _invoke_packet(
            runner,
            handshake,
            invocation_id="inv-big-stream",
            attempt_id="att-big-stream",
            task_packet={"objective": "large stream"},
        )
    )
    assert result["provider_call_id"] == "call-big"
    assert result["outcome"] == "complete"
    assert result["truncated"] is True
    assert len(result["redacted_output"].encode()) <= binding.output_cap


def test_invoke_rejects_oversized_single_event_line(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor")
    binding.executable.write_text(
        _cursor_stream_script(
            "print('{' + '\"type\":\"assistant\",\"session_id\":\"s\",\"text\":\"' + 'y'*70000 + '\"}')\n"
        ),
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    with pytest.raises(ValueError, match="exceeds cap"):
        runner.invoke(
            _invoke_packet(
                runner,
                handshake,
                invocation_id="inv-huge-line",
                attempt_id="att-huge-line",
                task_packet={"objective": "oversized line"},
            )
        )


def test_invoke_rejects_unknown_cursor_event_type(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor")
    binding.executable.write_text(
        _cursor_stream_script(
            "import json\n"
            "print(json.dumps({'type':'progress','session_id':'s'}))\n"
        ),
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    with pytest.raises(ValueError, match="unsupported provider event type"):
        runner.invoke(
            _invoke_packet(
                runner,
                handshake,
                invocation_id="inv-unknown",
                attempt_id="att-unknown",
                task_packet={"objective": "unknown event"},
            )
        )


def test_truncated_evidence_preserves_terminal_event(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor")
    binding.executable.write_text(
        _cursor_stream_script(
            "import json\n"
            "for i in range(400):\n"
            "    print(json.dumps({'type':'thinking','session_id':'s','text':'z'*800}))\n"
            "print(json.dumps({'type':'result','provider_call_id':'call-terminal'}))\n"
        ),
        encoding="utf-8",
    )
    binding.executable.chmod(0o700)
    runner = HostRunner(binding)
    handshake = runner.handshake()
    result = runner.invoke(
        _invoke_packet(
            runner,
            handshake,
            invocation_id="inv-terminal",
            attempt_id="att-terminal",
            task_packet={"objective": "terminal preservation"},
        )
    )
    assert result["truncated"] is True
    assert result["provider_call_id"] == "call-terminal"
    assert "call-terminal" in result["redacted_output"]
    assert len(result["redacted_output"].encode()) <= binding.output_cap


def test_host_runner_recv_rejects_oversized_socket_frame() -> None:
    from unittest.mock import MagicMock

    conn = MagicMock()
    conn.recv.return_value = b"x" * (MAX_FRAME_BYTES + 1)
    with pytest.raises(ValueError, match="frame exceeds cap"):
        HostRunnerServer._recv(conn)


def test_invoke_result_socket_response_stays_within_frame_cap(tmp_path: Path) -> None:
    binding = _binding(tmp_path, "cursor")
    runner = HostRunner(binding)
    handshake = runner.handshake()
    result = runner.invoke(
        _invoke_packet(
            runner,
            handshake,
            invocation_id="inv-frame",
            attempt_id="att-frame",
            task_packet={"objective": "frame bound"},
        )
    )
    response = canonical_json(result).encode() + b"\n"
    assert len(response) <= MAX_FRAME_BYTES
    assert len(result["redacted_output"].encode()) <= MAX_LINE_BYTES
