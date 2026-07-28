"""Server-side principal registry and resolution (R4)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from flow_engine.application.clock import utc_now_iso
from flow_engine.coordinator.commands import (
    Grant,
    ResolvedTaskGrant,
    SystemTestGrant,
)
from flow_engine.domain.errors import AuthRequiredError, AuthzDeniedError, NotFoundError
from flow_engine.domain.models import new_id
from flow_engine.domain.states import PrincipalRole, Surface


def token_digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedPrincipal:
    principal_id: str
    principal_key: str
    kind: str
    role: PrincipalRole
    display_name: str
    organization_id: str | None
    actor_id: str | None
    provider_seat_id: str | None
    grant_id: str | None
    capabilities: tuple[str, ...]
    surfaces: tuple[Surface, ...]
    status: str = "active"

    def to_dict(self) -> dict[str, Any]:
        return {
            "principal_id": self.principal_id,
            "principal_key": self.principal_key,
            "kind": self.kind,
            "role": str(self.role),
            "display_name": self.display_name,
            "organization_id": self.organization_id,
            "actor_id": self.actor_id,
            "provider_seat_id": self.provider_seat_id,
            "grant_id": self.grant_id,
            "capabilities": list(self.capabilities),
            "surfaces": [str(s) for s in self.surfaces],
            "status": self.status,
        }


DEFAULT_SURFACES: dict[str, tuple[Surface, ...]] = {
    "founder": (Surface.REST, Surface.CLI, Surface.TEST, Surface.MCP),
    "scheduler": (Surface.SCHEDULE, Surface.REST),
    "mcp_service": (Surface.MCP, Surface.REST),
    "worker": (Surface.WORKER, Surface.REST),
    "provider_invocation": (Surface.WORKER,),
}


def _row_to_principal(row: sqlite3.Row) -> ResolvedPrincipal:
    surfaces_raw = json.loads(row["surfaces_json"] or "[]")
    surfaces = tuple(Surface(s) for s in surfaces_raw) if surfaces_raw else DEFAULT_SURFACES.get(
        row["kind"], (Surface.REST,)
    )
    caps = tuple(json.loads(row["capabilities_json"] or "[]"))
    return ResolvedPrincipal(
        principal_id=row["id"],
        principal_key=row["principal_key"],
        kind=row["kind"],
        role=PrincipalRole(row["role"]),
        display_name=row["display_name"],
        organization_id=row["organization_id"],
        actor_id=row["actor_id"],
        provider_seat_id=row["provider_seat_id"],
        grant_id=row["grant_id"],
        capabilities=caps,
        surfaces=surfaces,
        status=row["status"],
    )


def register_principal(
    conn: sqlite3.Connection,
    *,
    principal_key: str,
    kind: str,
    role: PrincipalRole,
    raw_token: str,
    display_name: str,
    organization_id: str | None = None,
    actor_id: str | None = None,
    provider_seat_id: str | None = None,
    grant_id: str | None = None,
    capabilities: tuple[str, ...] = (),
    surfaces: tuple[Surface, ...] | None = None,
) -> ResolvedPrincipal:
    """Register a control-plane principal (coordinator-only write)."""
    pid = new_id()
    now = utc_now_iso()
    surf = surfaces or DEFAULT_SURFACES.get(kind, (Surface.REST,))
    conn.execute(
        """
        INSERT INTO control_plane_principals (
            id, principal_key, kind, role, display_name, status, token_digest,
            organization_id, actor_id, provider_seat_id, grant_id,
            capabilities_json, surfaces_json, created_at
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            pid,
            principal_key,
            kind,
            str(role),
            display_name,
            token_digest(raw_token),
            organization_id,
            actor_id,
            provider_seat_id,
            grant_id,
            json.dumps(list(capabilities)),
            json.dumps([str(s) for s in surf]),
            now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM control_plane_principals WHERE id = ?", (pid,)
    ).fetchone()
    assert row is not None
    return _row_to_principal(row)


def resolve_by_token(conn: sqlite3.Connection, raw_token: str) -> ResolvedPrincipal:
    if not raw_token.strip():
        raise AuthRequiredError("authentication token required")
    digest = token_digest(raw_token)
    row = conn.execute(
        """
        SELECT * FROM control_plane_principals
        WHERE token_digest = ? AND status = 'active'
        """,
        (digest,),
    ).fetchone()
    if row is None:
        raise AuthRequiredError("invalid or expired principal token")
    return _row_to_principal(row)


def resolve_by_key(conn: sqlite3.Connection, principal_key: str) -> ResolvedPrincipal:
    row = conn.execute(
        """
        SELECT * FROM control_plane_principals
        WHERE principal_key = ? AND status = 'active'
        """,
        (principal_key,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"unknown principal key: {principal_key}")
    return _row_to_principal(row)


def revoke_principal(
    conn: sqlite3.Connection,
    *,
    principal_key: str,
    actor: str,
) -> ResolvedPrincipal:
    row = conn.execute(
        "SELECT * FROM control_plane_principals WHERE principal_key = ?",
        (principal_key,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"unknown principal key: {principal_key}")
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE control_plane_principals
        SET status = 'revoked', revoked_at = ?
        WHERE principal_key = ?
        """,
        (now, principal_key),
    )
    updated = conn.execute(
        "SELECT * FROM control_plane_principals WHERE principal_key = ?",
        (principal_key,),
    ).fetchone()
    assert updated is not None
    return _row_to_principal(updated)


def load_grant_for_principal(conn: sqlite3.Connection, principal: ResolvedPrincipal) -> Grant | None:
    """Resolve server-side grant binding for a principal."""
    if principal.grant_id:
        row = conn.execute(
            "SELECT * FROM task_grants WHERE grant_id = ?",
            (principal.grant_id,),
        ).fetchone()
        if row is None:
            raise AuthzDeniedError("principal grant binding not found")
        if row["compatibility_mode"] == "r2_system_test":
            return SystemTestGrant(
                grant_id=row["grant_id"],
                principal_id=row["principal_id"],
                role=PrincipalRole(row["role"]),
                surfaces=tuple(Surface(s) for s in json.loads(row["surfaces_json"])),
                providers=tuple(json.loads(row["providers_json"])),
                budget_scope_id=row["budget_scope_id"],
                capabilities=tuple(json.loads(row["capabilities_json"] or "[]")),
                policy_revision=row["policy_revision"],
                compatibility_mode="r2_system_test",
            )
        return ResolvedTaskGrant(
            grant_id=row["grant_id"],
            principal_id=row["principal_id"],
            role=PrincipalRole(row["role"]),
            surfaces=tuple(Surface(s) for s in json.loads(row["surfaces_json"])),
            providers=tuple(json.loads(row["providers_json"])),
            budget_scope_id=row["budget_scope_id"],
            organization_id=row["organization_id"],
            organization_profile_hash="",
            loadout_id=row["loadout_id"],
            snapshot_id=row["snapshot_id"],
            assignment_id=row["assignment_id"] or "",
            capabilities=tuple(json.loads(row["capabilities_json"] or "[]")),
            policy_revision=row["policy_revision"],
            effect_ceiling=row["effect_ceiling"],
            compatibility_mode="r3_resolved",
        )

    # Founder/worker dev principals may use explicit system-test grant by key.
    if principal.kind == "founder":
        return SystemTestGrant(
            grant_id=f"founder-grant-{principal.principal_key}",
            principal_id=principal.principal_id,
            role=PrincipalRole.FOUNDER,
            surfaces=principal.surfaces,
            providers=("codex", "cursor", "claude"),
            budget_scope_id="acceptance-campaign-r4",
            capabilities=principal.capabilities,
            policy_revision="r4-local",
        )
    if principal.kind == "worker":
        return SystemTestGrant(
            grant_id=f"worker-grant-{principal.principal_key}",
            principal_id=principal.principal_id,
            role=PrincipalRole.WORKER,
            surfaces=principal.surfaces,
            providers=("codex", "cursor", "claude"),
            budget_scope_id="acceptance-campaign-r4",
            capabilities=principal.capabilities,
            policy_revision="r4-local",
        )
    if principal.kind == "scheduler":
        return None
    if principal.kind == "mcp_service":
        return None
    if principal.kind == "provider_invocation":
        return None
    return None


def assert_surface_allowed(principal: ResolvedPrincipal, surface: Surface) -> None:
    if surface not in principal.surfaces:
        raise AuthzDeniedError(f"surface {surface} not permitted for principal kind {principal.kind}")
