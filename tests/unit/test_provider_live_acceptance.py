from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.provider_live_acceptance import (
    ACCEPTANCE_TOKEN,
    DEFAULT_PROVIDERS,
    PROVIDER_EXECUTABLES,
    acceptance_checks,
    acceptance_success,
    acceptance_task_packet,
    build_binding,
    load_env_file,
    parse_args,
    redact_evidence,
    run_provider_acceptance,
)
from scripts.provider_runtime_acceptance import ACCEPTANCE_MATRIX


def test_load_env_file_parses_comments_and_quotes(tmp_path: Path) -> None:
    env_path = tmp_path / "pins.env"
    env_path.write_text(
        "# comment\n"
        "ORCH_PROVIDER_MODEL='composer-2.5'\n"
        "export ORCH_PROVIDER_ALLOWED_MODELS=\"composer-2.5\"\n",
        encoding="utf-8",
    )
    loaded = load_env_file(env_path)
    assert loaded["ORCH_PROVIDER_MODEL"] == "composer-2.5"
    assert loaded["ORCH_PROVIDER_ALLOWED_MODELS"] == "composer-2.5"


def test_acceptance_task_packet_is_canonical_and_bounded() -> None:
    packet = acceptance_task_packet("cursor")
    assert packet["acceptance_probe"] is True
    assert ACCEPTANCE_TOKEN in packet["instruction"]
    encoded = json.dumps(packet)
    assert len(encoded) < 4096


def test_acceptance_success_requires_all_checks() -> None:
    ok = {
        "outcome": "complete",
        "exit_code": 0,
        "reconciliation_required": False,
        "provider_call_id": "call-1",
        "redacted_output": f'{{"type":"result","result":"{ACCEPTANCE_TOKEN}"}}',
    }
    assert acceptance_success(ok) is True
    bad = dict(ok)
    bad["redacted_output"] = '{"type":"result","result":"nope"}'
    assert acceptance_success(bad) is False
    checks = acceptance_checks(bad)
    assert checks["acceptance_token_present"] is False


def test_redact_evidence_strips_sensitive_keys() -> None:
    payload = {
        "auth_token": "secret-value",
        "snapshot": {"cli_version": "1.0", "note": "api_key=abc"},
    }
    redacted = redact_evidence(payload)
    assert redacted["auth_token"] == "[REDACTED]"
    assert "secret-value" not in json.dumps(redacted)
    assert "[REDACTED]" in redacted["snapshot"]["note"]


def test_load_env_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_env_file(tmp_path / "missing.env")


def test_default_providers_include_codex() -> None:
    assert "codex" in DEFAULT_PROVIDERS
    assert PROVIDER_EXECUTABLES["codex"] == "codex"
    assert ACCEPTANCE_MATRIX["codex"] == "AM-04"


def test_parse_args_accepts_codex_provider() -> None:
    args = parse_args(["--provider", "codex"])
    assert args.provider == "codex"


def test_build_binding_codex_uses_pins_and_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    codex_bin.chmod(0o700)
    monkeypatch.setenv("ORCH_PROVIDER_EXECUTABLE_CODEX", str(codex_bin))
    pins = {
        "ORCH_PROVIDER_MODEL": "gpt-5.6-sol",
        "ORCH_PROVIDER_ALLOWED_MODELS": "gpt-5.6-sol",
    }
    run_dir = tmp_path / "run"
    binding = build_binding("codex", root=tmp_path, pins=pins, run_dir=run_dir)
    assert binding.provider == "codex"
    assert binding.model == "gpt-5.6-sol"
    assert binding.acceptance_mode is True
    assert binding.executable == codex_bin


def test_run_provider_acceptance_codex_mocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins_path = tmp_path / ".local" / "provider" / "codex.pins.env"
    pins_path.parent.mkdir(parents=True)
    pins_path.write_text(
        "ORCH_PROVIDER_MODEL=gpt-5.6-sol\n"
        "ORCH_PROVIDER_ALLOWED_MODELS=gpt-5.6-sol\n",
        encoding="utf-8",
    )
    codex_bin = tmp_path / "codex"
    codex_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    codex_bin.chmod(0o700)
    monkeypatch.setenv("ORCH_PROVIDER_EXECUTABLE_CODEX", str(codex_bin))

    mock_result = {
        "outcome": "complete",
        "exit_code": 0,
        "reconciliation_required": False,
        "provider_call_id": "call-codex-1",
        "redacted_output": (
            f'{{"type":"turn.completed","result":"{ACCEPTANCE_TOKEN}"}}'
        ),
    }

    class FakeRunner:
        def handshake(self) -> dict[str, object]:
            return {
                "snapshot": {
                    "resolved_model": "gpt-5.6-sol",
                    "adapter_version": "0.1",
                },
                "snapshot_digest": "digest-1",
            }

        def invoke(self, packet: dict[str, object]) -> dict[str, object]:
            assert packet["provider"] == "codex"
            return mock_result

    monkeypatch.setattr(
        "scripts.provider_live_acceptance.HostRunner",
        lambda binding: FakeRunner(),
    )

    outcome = run_provider_acceptance(
        "codex", root=tmp_path, run_dir=tmp_path / "evidence"
    )
    assert outcome.success is True
    assert outcome.provider == "codex"
    summary = json.loads(
        (outcome.evidence_dir / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["model"] == "gpt-5.6-sol"
    assert summary["acceptance_mode"] is True
