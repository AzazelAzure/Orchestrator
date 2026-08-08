from __future__ import annotations

import pytest

from flow_engine.providers.cli_registry import (
    CLAUDE_RESULT_SUBTYPE_SUCCESS,
    CLAUDE_RESULT_SUBTYPES_ERROR,
    CLAUDE_REVIEW_MERGE_PERMISSION_MODE,
    EXECUTION_PROFILE_ACCEPTANCE,
    EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE,
    EXECUTION_PROFILE_CURSOR_IMPLEMENTATION,
    claude_result_subtype_is_error,
    claude_result_subtype_is_success,
    claude_result_subtype_is_terminal,
    probe_matches_pinned_version,
    validate_cli_version_pin,
    validate_execution_profile,
)


def test_validate_execution_profile_accepts_provider_compatible() -> None:
    validate_execution_profile("cursor", EXECUTION_PROFILE_ACCEPTANCE)
    validate_execution_profile("cursor", EXECUTION_PROFILE_CURSOR_IMPLEMENTATION)
    validate_execution_profile("claude", EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE)


def test_claude_review_merge_permission_mode_is_noninteractive_bypass() -> None:
    assert CLAUDE_REVIEW_MERGE_PERMISSION_MODE == "bypassPermissions"
    spec = validate_execution_profile("claude", EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE)
    assert spec["acceptance_policy"] == (
        "trusted-authorized-review-merge-bash-for-test-and-gh-not-sandbox-containment"
    )


def test_validate_execution_profile_denies_unknown() -> None:
    with pytest.raises(ValueError, match="unknown execution profile"):
        validate_execution_profile("cursor", "cursor-rogue-profile")


def test_validate_execution_profile_denies_provider_mismatch() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        validate_execution_profile("cursor", EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE)


def test_validate_cli_version_pin_requires_registered_exact_version() -> None:
    assert validate_cli_version_pin("cursor", "2026.08.04-aaa8809") == "cursor-events-v1"
    with pytest.raises(ValueError, match="not registered"):
        validate_cli_version_pin("cursor", "2099.01.01")


def test_probe_matches_pinned_version_exact_token() -> None:
    assert probe_matches_pinned_version("cursor-agent 2026.08.04-aaa8809", "2026.08.04-aaa8809")
    assert not probe_matches_pinned_version("cursor-agent 2026.07.23", "2026.08.04-aaa8809")


def test_claude_result_subtype_helpers_classify_terminal_family() -> None:
    assert claude_result_subtype_is_terminal(CLAUDE_RESULT_SUBTYPE_SUCCESS)
    for subtype in CLAUDE_RESULT_SUBTYPES_ERROR:
        assert claude_result_subtype_is_terminal(subtype)
        assert claude_result_subtype_is_error(subtype)
        assert not claude_result_subtype_is_success(subtype)
    assert not claude_result_subtype_is_terminal("error")
    assert not claude_result_subtype_is_error(CLAUDE_RESULT_SUBTYPE_SUCCESS)
    assert claude_result_subtype_is_success(CLAUDE_RESULT_SUBTYPE_SUCCESS)
