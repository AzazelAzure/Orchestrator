-- User accounts, opaque credentials, and durable auth throttle (R4 auth slice).
-- Widens control_plane_principals.kind CHECK to include 'human' via SQLite rebuild.
-- Forward-only: rollback is restore from pre-migration SQLite backup (no down path).

PRAGMA foreign_keys=OFF;

CREATE TABLE control_plane_principals__new (
    id                  TEXT PRIMARY KEY,
    principal_key       TEXT    NOT NULL UNIQUE,
    kind                TEXT    NOT NULL
        CHECK (kind IN (
            'founder', 'scheduler', 'mcp_service', 'worker',
            'provider_invocation', 'human'
        )),
    role                TEXT    NOT NULL,
    display_name        TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'revoked')),
    token_digest        TEXT    NOT NULL,
    organization_id     TEXT    REFERENCES organization_profiles (id),
    actor_id            TEXT    REFERENCES actors (id),
    provider_seat_id    TEXT    REFERENCES provider_seats (id),
    grant_id            TEXT,
    capabilities_json   TEXT    NOT NULL DEFAULT '[]',
    surfaces_json       TEXT    NOT NULL DEFAULT '[]',
    created_at          TEXT    NOT NULL,
    revoked_at          TEXT
);

INSERT INTO control_plane_principals__new (
    id, principal_key, kind, role, display_name, status, token_digest,
    organization_id, actor_id, provider_seat_id, grant_id,
    capabilities_json, surfaces_json, created_at, revoked_at
)
SELECT
    id, principal_key, kind, role, display_name, status, token_digest,
    organization_id, actor_id, provider_seat_id, grant_id,
    capabilities_json, surfaces_json, created_at, revoked_at
FROM control_plane_principals;

DROP TABLE control_plane_principals;
ALTER TABLE control_plane_principals__new RENAME TO control_plane_principals;

CREATE UNIQUE INDEX IF NOT EXISTS idx_control_plane_principals_token_digest
ON control_plane_principals (token_digest)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS control_plane_user_accounts (
    id                  TEXT PRIMARY KEY,
    principal_id        TEXT    NOT NULL UNIQUE
        REFERENCES control_plane_principals (id),
    username            TEXT    NOT NULL UNIQUE,
    password_hash       TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disabled')),
    actor_id            TEXT    REFERENCES actors (id),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS control_plane_credentials (
    id                  TEXT PRIMARY KEY,
    principal_id        TEXT    NOT NULL
        REFERENCES control_plane_principals (id),
    credential_kind     TEXT    NOT NULL
        CHECK (credential_kind IN ('access', 'refresh', 'pat')),
    token_digest        TEXT    NOT NULL,
    expires_at          TEXT,
    revoked_at          TEXT,
    created_at          TEXT    NOT NULL,
    label               TEXT,
    parent_id           TEXT    REFERENCES control_plane_credentials (id),
    family_id           TEXT    NOT NULL,
    scopes_json         TEXT    NOT NULL DEFAULT '[]'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_control_plane_credentials_active_digest
ON control_plane_credentials (token_digest)
WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_control_plane_credentials_principal
ON control_plane_credentials (principal_id, credential_kind);

CREATE INDEX IF NOT EXISTS idx_control_plane_credentials_family
ON control_plane_credentials (family_id);

CREATE TABLE IF NOT EXISTS control_plane_auth_throttle (
    id                  TEXT PRIMARY KEY,
    action              TEXT    NOT NULL,
    subject_key         TEXT    NOT NULL,
    window_started_at   TEXT    NOT NULL,
    hit_count           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (action, subject_key)
);

PRAGMA foreign_keys=ON;
