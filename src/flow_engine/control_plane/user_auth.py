"""Human user accounts, opaque credentials, and durable auth throttle."""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from flow_engine.application.clock import is_expired, utc_after_seconds, utc_now_iso
from flow_engine.control_plane.password import hash_password, verify_password
from flow_engine.control_plane.principal_registry import (
    ResolvedPrincipal,
    register_principal,
    resolve_legacy_principal_token,
    token_digest,
)
from flow_engine.domain.errors import (
    AuthRequiredError,
    AuthzDeniedError,
    ConflictError,
    NotFoundError,
    ValidationFailedError,
)
from flow_engine.domain.models import new_id
from flow_engine.domain.states import PrincipalRole, Surface

DEFAULT_ACCESS_TTL_SEC = 1800  # 30 minutes
DEFAULT_REFRESH_TTL_SEC = 1_209_600  # 14 days
DEFAULT_PAT_TTL_SEC = 31_536_000  # 365 days
DEFAULT_THROTTLE_WINDOW_SEC = 900  # 15 minutes
DEFAULT_THROTTLE_MAX_HITS = 10


def _access_ttl() -> int:
    return int(os.environ.get("ORCH_ACCESS_TOKEN_TTL_SEC", str(DEFAULT_ACCESS_TTL_SEC)))


def _refresh_ttl() -> int:
    return int(os.environ.get("ORCH_REFRESH_TOKEN_TTL_SEC", str(DEFAULT_REFRESH_TTL_SEC)))


def _pat_ttl() -> int:
    return int(os.environ.get("ORCH_PAT_TTL_SEC", str(DEFAULT_PAT_TTL_SEC)))


def _throttle_window() -> int:
    return int(os.environ.get("ORCH_AUTH_THROTTLE_WINDOW_SEC", str(DEFAULT_THROTTLE_WINDOW_SEC)))


def _throttle_max() -> int:
    return int(os.environ.get("ORCH_AUTH_THROTTLE_MAX", str(DEFAULT_THROTTLE_MAX_HITS)))


def registration_allowed() -> bool:
    return os.environ.get("ORCH_ALLOW_USER_REGISTRATION", "0") == "1"


def _mint_raw_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class IssuedCredential:
    credential_id: str
    credential_kind: str
    raw_token: str
    expires_at: str | None
    family_id: str
    label: str | None = None
    parent_id: str | None = None
    scopes: tuple[str, ...] = ()

    def public_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "credential_kind": self.credential_kind,
            "token": self.raw_token,
            "expires_at": self.expires_at,
            "family_id": self.family_id,
            "label": self.label,
            "parent_id": self.parent_id,
            "scopes": list(self.scopes),
        }


@dataclass(frozen=True)
class UserAccount:
    account_id: str
    principal_id: str
    username: str
    status: str
    actor_id: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "principal_id": self.principal_id,
            "username": self.username,
            "status": self.status,
            "actor_id": self.actor_id,
        }


def _account_from_row(row: sqlite3.Row) -> UserAccount:
    return UserAccount(
        account_id=row["id"],
        principal_id=row["principal_id"],
        username=row["username"],
        status=row["status"],
        actor_id=row["actor_id"],
    )


def throttle_check_and_bump(
    conn: sqlite3.Connection,
    *,
    action: str,
    subject_key: str,
    max_hits: int | None = None,
    window_sec: int | None = None,
) -> dict[str, Any]:
    """Fixed-window counter shared across gunicorn workers via coordinator SQLite."""
    limit = max_hits if max_hits is not None else _throttle_max()
    window = window_sec if window_sec is not None else _throttle_window()
    now = utc_now_iso()
    row = conn.execute(
        """
        SELECT * FROM control_plane_auth_throttle
        WHERE action = ? AND subject_key = ?
        """,
        (action, subject_key),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO control_plane_auth_throttle (
                id, action, subject_key, window_started_at, hit_count
            ) VALUES (?, ?, ?, ?, 1)
            """,
            (new_id(), action, subject_key, now),
        )
        return {"allowed": True, "hit_count": 1, "limit": limit, "window_started_at": now}

    started = row["window_started_at"]
    if is_expired(utc_after_seconds(window, from_iso=started), now_iso=now):
        conn.execute(
            """
            UPDATE control_plane_auth_throttle
            SET window_started_at = ?, hit_count = 1
            WHERE id = ?
            """,
            (now, row["id"]),
        )
        return {"allowed": True, "hit_count": 1, "limit": limit, "window_started_at": now}

    hits = int(row["hit_count"]) + 1
    if hits > limit:
        conn.execute(
            """
            UPDATE control_plane_auth_throttle SET hit_count = ? WHERE id = ?
            """,
            (hits, row["id"]),
        )
        return {
            "allowed": False,
            "hit_count": hits,
            "limit": limit,
            "window_started_at": started,
        }
    conn.execute(
        """
        UPDATE control_plane_auth_throttle SET hit_count = ? WHERE id = ?
        """,
        (hits, row["id"]),
    )
    return {
        "allowed": True,
        "hit_count": hits,
        "limit": limit,
        "window_started_at": started,
    }


def register_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    display_name: str | None = None,
    actor_id: str | None = None,
    allow_registration: bool | None = None,
    founder_authorized: bool = False,
    capabilities: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Create human principal + account. Least-privilege capabilities by default."""
    uname = (username or "").strip()
    if not uname:
        raise ValidationFailedError("username is required")
    if not password or len(password) < 8:
        raise ValidationFailedError("password must be at least 8 characters")
    allowed = registration_allowed() if allow_registration is None else allow_registration
    if not allowed and not founder_authorized:
        raise AuthzDeniedError("user registration is disabled")

    existing = conn.execute(
        "SELECT 1 FROM control_plane_user_accounts WHERE username = ?",
        (uname,),
    ).fetchone()
    if existing is not None:
        raise ConflictError(f"username already registered: {uname}")

    # Humans never authenticate via principal-row token_digest; store unusable digest.
    unused_raw = _mint_raw_token()
    principal = register_principal(
        conn,
        principal_key=f"human.{uname}",
        kind="human",
        role=PrincipalRole.MANAGER,
        raw_token=unused_raw,
        display_name=(display_name or uname).strip() or uname,
        actor_id=actor_id,
        capabilities=capabilities,
        surfaces=(Surface.REST, Surface.CLI),
    )
    now = utc_now_iso()
    account_id = new_id()
    conn.execute(
        """
        INSERT INTO control_plane_user_accounts (
            id, principal_id, username, password_hash, status, actor_id,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        (
            account_id,
            principal.principal_id,
            uname,
            hash_password(password),
            actor_id,
            now,
            now,
        ),
    )
    account = _account_from_row(
        conn.execute(
            "SELECT * FROM control_plane_user_accounts WHERE id = ?", (account_id,)
        ).fetchone()
    )
    return {"account": account.to_dict(), "principal": principal.to_dict()}


def _load_active_account(conn: sqlite3.Connection, username: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT a.*, p.status AS principal_status, p.principal_key, p.kind, p.role,
               p.display_name, p.capabilities_json, p.surfaces_json, p.organization_id,
               p.provider_seat_id, p.grant_id, p.created_at AS principal_created_at,
               p.revoked_at AS principal_revoked_at, p.id AS pid
        FROM control_plane_user_accounts a
        JOIN control_plane_principals p ON p.id = a.principal_id
        WHERE a.username = ?
        """,
        (username,),
    ).fetchone()
    if row is None:
        raise AuthRequiredError("invalid username or password")
    if row["status"] != "active" or row["principal_status"] != "active":
        raise AuthRequiredError("account disabled or principal revoked")
    return row


def _insert_credential(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    credential_kind: str,
    raw_token: str,
    expires_at: str | None,
    family_id: str,
    label: str | None = None,
    parent_id: str | None = None,
    scopes: tuple[str, ...] = (),
) -> IssuedCredential:
    cid = new_id()
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO control_plane_credentials (
            id, principal_id, credential_kind, token_digest, expires_at, revoked_at,
            created_at, label, parent_id, family_id, scopes_json
        ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        (
            cid,
            principal_id,
            credential_kind,
            token_digest(raw_token),
            expires_at,
            now,
            label,
            parent_id,
            family_id,
            json.dumps(list(scopes)),
        ),
    )
    return IssuedCredential(
        credential_id=cid,
        credential_kind=credential_kind,
        raw_token=raw_token,
        expires_at=expires_at,
        family_id=family_id,
        label=label,
        parent_id=parent_id,
        scopes=scopes,
    )


def issue_session_pair(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
) -> dict[str, Any]:
    family_id = new_id()
    now = utc_now_iso()
    refresh = _insert_credential(
        conn,
        principal_id=principal_id,
        credential_kind="refresh",
        raw_token=_mint_raw_token(),
        expires_at=utc_after_seconds(_refresh_ttl(), from_iso=now),
        family_id=family_id,
    )
    access = _insert_credential(
        conn,
        principal_id=principal_id,
        credential_kind="access",
        raw_token=_mint_raw_token(),
        expires_at=utc_after_seconds(_access_ttl(), from_iso=now),
        family_id=family_id,
        parent_id=refresh.credential_id,
    )
    return {
        "access": access.public_dict(),
        "refresh": refresh.public_dict(),
        "family_id": family_id,
    }


def login_user(
    conn: sqlite3.Connection,
    *,
    username: str,
    password: str,
    client_ip: str | None = None,
) -> dict[str, Any]:
    subjects = [f"user:{(username or '').strip().lower()}"]
    if client_ip:
        subjects.append(f"ip:{client_ip}")
    for subject in subjects:
        result = throttle_check_and_bump(conn, action="auth.login", subject_key=subject)
        if not result["allowed"]:
            raise AuthzDeniedError("login rate limit exceeded")

    uname = (username or "").strip()
    row = _load_active_account(conn, uname)
    if not verify_password(password, row["password_hash"]):
        raise AuthRequiredError("invalid username or password")

    session = issue_session_pair(conn, principal_id=row["principal_id"])
    return {
        "account": {
            "account_id": row["id"],
            "principal_id": row["principal_id"],
            "username": row["username"],
            "status": row["status"],
            "actor_id": row["actor_id"],
        },
        **session,
    }


def _load_credential_by_raw(conn: sqlite3.Connection, raw_token: str) -> sqlite3.Row | None:
    digest = token_digest(raw_token)
    return conn.execute(
        """
        SELECT * FROM control_plane_credentials
        WHERE token_digest = ? AND revoked_at IS NULL
        """,
        (digest,),
    ).fetchone()


def revoke_credential_family(
    conn: sqlite3.Connection,
    *,
    family_id: str,
) -> int:
    now = utc_now_iso()
    cur = conn.execute(
        """
        UPDATE control_plane_credentials
        SET revoked_at = ?
        WHERE family_id = ? AND revoked_at IS NULL
        """,
        (now, family_id),
    )
    return int(cur.rowcount)


def revoke_credential(
    conn: sqlite3.Connection,
    *,
    credential_id: str,
    actor_principal_id: str,
    founder: bool = False,
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM control_plane_credentials WHERE id = ?",
        (credential_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"unknown credential: {credential_id}")
    if not founder and row["principal_id"] != actor_principal_id:
        raise AuthzDeniedError("cannot revoke another principal's credential")
    now = utc_now_iso()
    if row["credential_kind"] == "refresh":
        revoked = revoke_credential_family(conn, family_id=row["family_id"])
    else:
        conn.execute(
            """
            UPDATE control_plane_credentials
            SET revoked_at = ?
            WHERE id = ? AND revoked_at IS NULL
            """,
            (now, credential_id),
        )
        revoked = 1
    return {"credential_id": credential_id, "revoked_count": revoked}


def logout_with_token(
    conn: sqlite3.Connection,
    *,
    raw_token: str,
) -> dict[str, Any]:
    row = _load_credential_by_raw(conn, raw_token)
    if row is None:
        return {"revoked_count": 0, "family_id": None}
    count = revoke_credential_family(conn, family_id=row["family_id"])
    return {"revoked_count": count, "family_id": row["family_id"]}


def refresh_session(
    conn: sqlite3.Connection,
    *,
    refresh_token: str,
) -> dict[str, Any]:
    row = _load_credential_by_raw(conn, refresh_token)
    if row is None:
        digest = token_digest(refresh_token)
        prior = conn.execute(
            """
            SELECT * FROM control_plane_credentials
            WHERE token_digest = ? AND credential_kind = 'refresh'
            """,
            (digest,),
        ).fetchone()
        if prior is not None:
            revoke_credential_family(conn, family_id=prior["family_id"])
            raise AuthRequiredError("refresh token reuse detected; session revoked")
        raise AuthRequiredError("invalid or expired refresh token")

    if row["credential_kind"] != "refresh":
        raise AuthRequiredError("refresh token required")
    if row["expires_at"] and is_expired(row["expires_at"]):
        revoke_credential_family(conn, family_id=row["family_id"])
        raise AuthRequiredError("refresh token expired")

    family_id = row["family_id"]
    principal_id = row["principal_id"]
    revoke_credential_family(conn, family_id=family_id)
    return issue_session_pair(conn, principal_id=principal_id)


def issue_pat(
    conn: sqlite3.Connection,
    *,
    principal_id: str,
    label: str,
    scopes: tuple[str, ...] = (),
) -> dict[str, Any]:
    label_clean = (label or "").strip()
    if not label_clean:
        raise ValidationFailedError("PAT label is required")
    now = utc_now_iso()
    family_id = new_id()
    issued = _insert_credential(
        conn,
        principal_id=principal_id,
        credential_kind="pat",
        raw_token=_mint_raw_token(),
        expires_at=utc_after_seconds(_pat_ttl(), from_iso=now),
        family_id=family_id,
        label=label_clean,
        scopes=scopes,
    )
    return {"pat": issued.public_dict()}


def resolve_by_token(conn: sqlite3.Connection, raw_token: str) -> ResolvedPrincipal:
    """Resolve opaque user credential digests, then legacy principal-row tokens."""
    if not raw_token.strip():
        raise AuthRequiredError("authentication token required")

    cred = _load_credential_by_raw(conn, raw_token)
    if cred is not None:
        if cred["expires_at"] and is_expired(cred["expires_at"]):
            raise AuthRequiredError("invalid or expired credential")
        if cred["credential_kind"] == "refresh":
            raise AuthRequiredError("refresh token cannot be used as bearer")
        row = conn.execute(
            """
            SELECT * FROM control_plane_principals
            WHERE id = ? AND status = 'active'
            """,
            (cred["principal_id"],),
        ).fetchone()
        if row is None:
            raise AuthRequiredError("invalid or expired principal token")
        from flow_engine.control_plane.principal_registry import _row_to_principal

        return _row_to_principal(row)

    return resolve_legacy_principal_token(conn, raw_token)
