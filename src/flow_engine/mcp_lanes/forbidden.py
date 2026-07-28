"""Operations and tool names never exposed on any MCP lane."""

from __future__ import annotations

# Locked catalog forbidden_operations union + R4B explicit exclusions.
FORBIDDEN_OPERATIONS: frozenset[str] = frozenset(
    {
        "waiver",
        "hitm_exception",
        "paid_retry_after_unknown",
        "merge",
        "deploy",
        "publication",
        "schedule_activation",
        "arbitrary_script_execution",
        "policy_profile_activation",
        "credential_projection",
        "unrestricted_state_mutation",
        "direct_database_access",
        "provider_cli_invocation",
        "raw_shell",
        "raw_db",
        "raw_database",
        "secrets",
        "secret_projection",
        "repository_script",
        "repository_scripts",
        "exception",
        "hitm_exception_grant",
    }
)

FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset(FORBIDDEN_OPERATIONS) | frozenset(
    {
        "waive_gate",
        "new_attempt_after_unknown",
        "merge_pr",
        "deploy_release",
        "publish",
        "activate_schedule",
        "exec_shell",
        "execute_shell",
        "open_sqlite",
        "sql_exec",
        "project_secret",
        "run_repository_script",
    }
)
