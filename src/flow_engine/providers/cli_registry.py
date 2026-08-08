"""Registered provider CLI versions and governed execution profiles."""

from __future__ import annotations

from typing import Final

# Exact CLI version strings mapped to durable event-schema identifiers.
REGISTERED_CLI_VERSIONS: Final[dict[str, dict[str, str]]] = {
    "codex": {
        "0.144.6": "codex-events-v1",
        "0.146.0": "codex-events-v1",
    },
    "cursor": {
        "2026.07.23": "cursor-events-v1",
        "2026.08.04-aaa8809": "cursor-events-v1",
    },
    "claude": {
        "2.1.212": "claude-events-v1",
    },
}

EXECUTION_PROFILE_ACCEPTANCE: Final[str] = "acceptance"
EXECUTION_PROFILE_CURSOR_IMPLEMENTATION: Final[str] = "cursor-implementation"
EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE: Final[str] = "claude-independent-review-merge"
EXECUTION_PROFILE_CODEX_ADMIN: Final[str] = "codex-admin-reconciliation"

EXECUTION_PROFILES: Final[dict[str, dict[str, object]]] = {
    EXECUTION_PROFILE_ACCEPTANCE: {
        "providers": frozenset({"codex", "cursor", "claude"}),
        "requires_write_set": False,
        "requires_git_evidence": False,
        "acceptance_policy": "isolated-empty-read-only-no-tool",
    },
    EXECUTION_PROFILE_CURSOR_IMPLEMENTATION: {
        "providers": frozenset({"cursor"}),
        "requires_write_set": True,
        "requires_git_evidence": True,
        "acceptance_policy": "repository-confined-agent-write",
    },
    EXECUTION_PROFILE_CLAUDE_REVIEW_MERGE: {
        "providers": frozenset({"claude"}),
        "requires_write_set": False,
        "requires_git_evidence": True,
        "acceptance_policy": (
            "trusted-authorized-review-merge-bash-for-test-and-gh-not-sandbox-containment"
        ),
    },
    EXECUTION_PROFILE_CODEX_ADMIN: {
        "providers": frozenset({"codex"}),
        "requires_write_set": False,
        "requires_git_evidence": False,
        "acceptance_policy": "read-only-reconciliation",
    },
}

# Claude review profile: Bash is intentionally allowed for tests and gh merge/review.
# This is a trusted authorized role, not filesystem sandbox containment.
CLAUDE_ACCEPTANCE_DISALLOWED = (
    "Read,Grep,Glob,Edit,Write,Bash,WebFetch,WebSearch"
)
CLAUDE_REVIEW_DISALLOWED = "Edit,Write"

# Claude stream-json terminal result subtypes for claude-events-v1 (CLI 2.1.212).
CLAUDE_RESULT_SUBTYPE_SUCCESS: Final[str] = "success"
CLAUDE_RESULT_SUBTYPES_ERROR: Final[frozenset[str]] = frozenset({
    "error_during_execution",
    "error_max_turns",
    "error_max_budget_usd",
    "error_max_structured_output_retries",
})
CLAUDE_RESULT_SUBTYPES: Final[frozenset[str]] = frozenset(
    {CLAUDE_RESULT_SUBTYPE_SUCCESS, *CLAUDE_RESULT_SUBTYPES_ERROR}
)

# Bounded agentic turn caps (acceptance stays tight; review-merge needs PR headroom).
CLAUDE_ACCEPTANCE_MAX_TURNS: Final[str] = "8"
CLAUDE_REVIEW_MERGE_MAX_TURNS: Final[str] = "20"


def claude_result_subtype_is_terminal(subtype: object) -> bool:
    return isinstance(subtype, str) and subtype in CLAUDE_RESULT_SUBTYPES


def claude_result_subtype_is_success(subtype: object) -> bool:
    return subtype == CLAUDE_RESULT_SUBTYPE_SUCCESS


def claude_result_subtype_is_error(subtype: object) -> bool:
    return isinstance(subtype, str) and subtype in CLAUDE_RESULT_SUBTYPES_ERROR


def registered_cli_versions(provider: str) -> frozenset[str]:
    versions = REGISTERED_CLI_VERSIONS.get(provider)
    if versions is None:
        raise ValueError("unsupported provider")
    return frozenset(versions)


def event_schema_for_version(provider: str, cli_version: str) -> str:
    schema = REGISTERED_CLI_VERSIONS.get(provider, {}).get(cli_version)
    if schema is None:
        raise ValueError("cli version has no registered event schema")
    return schema


def validate_execution_profile(provider: str, profile: str) -> dict[str, object]:
    spec = EXECUTION_PROFILES.get(profile)
    if spec is None:
        raise ValueError("unknown execution profile")
    allowed = spec["providers"]
    if provider not in allowed:
        raise ValueError("execution profile incompatible with provider")
    return spec


def validate_cli_version_pin(provider: str, cli_version_pin: str) -> str:
    if not cli_version_pin or not isinstance(cli_version_pin, str):
        raise ValueError("cli version pin required")
    if cli_version_pin not in REGISTERED_CLI_VERSIONS.get(provider, {}):
        raise ValueError("cli version pin is not registered")
    return event_schema_for_version(provider, cli_version_pin)


def probe_matches_pinned_version(probed_output: str, pinned: str) -> bool:
    """Require the installation pin to appear as an exact token in --version output."""
    text = probed_output.strip()
    if not text:
        return False
    if text == pinned:
        return True
    return pinned in text.split()
