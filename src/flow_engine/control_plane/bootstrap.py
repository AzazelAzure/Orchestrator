"""Bootstrap control-plane principals from injected secrets (fail closed).

Fixed source-controlled founder/service tokens are NOT used at runtime.
Deterministic fixtures exist only for tests via bootstrap_test_principals().

R4B registers one generic mcp-service principal (R4A compat) plus five
lane-scoped mcp_service principals with distinct tokens.
"""

from __future__ import annotations

import os
import secrets
import sqlite3

from flow_engine.control_plane.principal_registry import register_principal, token_digest
from flow_engine.domain.states import PrincipalRole, Surface
from flow_engine.mcp_lanes.catalog import LANE_IDS, principal_key_for_lane

# Deterministic test-only fixtures — never used by runtime bootstrap.
TEST_FIXTURE_TOKENS = {
    "founder": "test-fixture-founder",
    "scheduler": "test-fixture-scheduler",
    "mcp-service": "test-fixture-mcp",
    "worker": "test-fixture-worker",
    "worker.provider.codex": "test-fixture-worker-codex",
    "worker.provider.cursor": "test-fixture-worker-cursor",
    "worker.provider.claude": "test-fixture-worker-claude",
    "provider-invocation": "test-fixture-provider-invocation",
    **{principal_key_for_lane(lane_id): f"test-fixture-mcp-{lane_id}" for lane_id in LANE_IDS},
}

_BASE_PRINCIPAL_SPECS = (
    ("founder", "founder", PrincipalRole.FOUNDER, "Founder"),
    ("scheduler", "scheduler", PrincipalRole.SYSTEM, "Scheduler service"),
    ("mcp-service", "mcp_service", PrincipalRole.WORKER, "MCP service principal"),
    ("worker", "worker", PrincipalRole.WORKER, "Celery worker"),
    ("worker.provider.codex", "worker", PrincipalRole.WORKER, "Codex provider worker"),
    ("worker.provider.cursor", "worker", PrincipalRole.WORKER, "Cursor provider worker"),
    ("worker.provider.claude", "worker", PrincipalRole.WORKER, "Claude provider worker"),
    (
        "provider-invocation",
        "provider_invocation",
        PrincipalRole.WORKER,
        "Provider invocation principal",
    ),
)

_ENV_TOKEN_KEYS = {
    "founder": "ORCH_TOKEN_FOUNDER",
    "scheduler": "ORCH_TOKEN_SCHEDULER",
    "mcp-service": "ORCH_TOKEN_MCP",
    "worker": "ORCH_TOKEN_WORKER",
    "worker.provider.codex": "ORCH_TOKEN_WORKER_CODEX",
    "worker.provider.cursor": "ORCH_TOKEN_WORKER_CURSOR",
    "worker.provider.claude": "ORCH_TOKEN_WORKER_CLAUDE",
    "provider-invocation": "ORCH_TOKEN_PROVIDER_INVOCATION",
}

_LANE_ENV_TOKEN_KEYS = {
    principal_key_for_lane(lane_id): {
        "context-assets": "ORCH_TOKEN_MCP_CONTEXT_ASSETS",
        "workflow-control": "ORCH_TOKEN_MCP_WORKFLOW_CONTROL",
        "delegation-coordination": "ORCH_TOKEN_MCP_DELEGATION_COORDINATION",
        "evidence-governance": "ORCH_TOKEN_MCP_EVIDENCE_GOVERNANCE",
        "maintenance": "ORCH_TOKEN_MCP_MAINTENANCE",
        "skills-scripts": "ORCH_TOKEN_MCP_SKILLS_SCRIPTS",
    }[lane_id]
    for lane_id in LANE_IDS
}


def _surfaces_for(kind: str) -> tuple[Surface, ...]:
    if kind == "founder":
        # REST/CLI for step-up ops; MCP allowed as initiating surface via lane tools.
        return (Surface.REST, Surface.CLI, Surface.TEST, Surface.MCP)
    if kind in {"worker", "provider_invocation"}:
        return (Surface.WORKER, Surface.REST)
    if kind == "scheduler":
        return (Surface.SCHEDULE, Surface.REST)
    return (Surface.MCP, Surface.REST)


def _principal_specs() -> tuple[tuple[str, str, PrincipalRole, str], ...]:
    lane_specs = tuple(
        (
            principal_key_for_lane(lane_id),
            "mcp_service",
            PrincipalRole.WORKER,
            f"MCP lane {lane_id}",
        )
        for lane_id in LANE_IDS
    )
    return _BASE_PRINCIPAL_SPECS + lane_specs


def bootstrap_test_principals(
    conn: sqlite3.Connection,
    *,
    tokens: dict[str, str] | None = None,
) -> list[str]:
    """Register deterministic principals for tests only."""
    token_map = tokens or TEST_FIXTURE_TOKENS
    return _register_from_tokens(conn, token_map)


def bootstrap_default_principals(conn: sqlite3.Connection) -> list[str]:
    """Backward-compatible test alias → deterministic fixtures only."""
    return bootstrap_test_principals(conn)


def bootstrap_principals_from_env(conn: sqlite3.Connection) -> list[str]:
    """Register principals from injected env tokens. Fail closed if any missing."""
    tokens: dict[str, str] = {}
    missing: list[str] = []
    for key, env_name in {**_ENV_TOKEN_KEYS, **_LANE_ENV_TOKEN_KEYS}.items():
        value = os.environ.get(env_name, "").strip()
        if not value:
            missing.append(env_name)
        else:
            tokens[key] = value
    if missing:
        raise RuntimeError(
            "principal bootstrap failed closed; missing env tokens: "
            + ", ".join(missing)
        )
    return _register_from_tokens(conn, tokens)


def generate_principal_token() -> str:
    """Generate an injectable principal token (not source-controlled)."""
    return secrets.token_urlsafe(32)


def bootstrap_test_token_for(key: str) -> str:
    """Test-only token lookup."""
    return TEST_FIXTURE_TOKENS[key]


def lane_env_token_key(lane_id: str) -> str:
    return _LANE_ENV_TOKEN_KEYS[principal_key_for_lane(lane_id)]


# Deprecated alias kept for in-flight test imports during remediation.
def dev_token_for(key: str) -> str:
    return bootstrap_test_token_for(key)


def _register_from_tokens(conn: sqlite3.Connection, tokens: dict[str, str]) -> list[str]:
    created: list[str] = []
    for key, kind, role, display in _principal_specs():
        existing = conn.execute(
            "SELECT 1 FROM control_plane_principals WHERE principal_key = ?",
            (key,),
        ).fetchone()
        token = tokens.get(key)
        if not token:
            raise RuntimeError(f"missing token for principal key {key}")
        if existing:
            # Local stacks rotate ephemeral env tokens across restarts while
            # retaining coordinator state; refresh digests on bootstrap.
            conn.execute(
                """
                UPDATE control_plane_principals
                SET token_digest = ?
                WHERE principal_key = ? AND status = 'active'
                """,
                (token_digest(token), key),
            )
            continue
        register_principal(
            conn,
            principal_key=key,
            kind=kind,
            role=role,
            raw_token=token,
            display_name=display,
            surfaces=_surfaces_for(kind),
            capabilities=(f"mcp.lane:{key}",) if kind == "mcp_service" and key.startswith("mcp.lane.") else (),
        )
        created.append(key)
    return created
