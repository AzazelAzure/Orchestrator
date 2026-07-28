-- R4 control-plane principals, token registry, and async delivery jobs (additive).
-- Preserves legacy queue rows and all R2/R3 tables.

CREATE TABLE IF NOT EXISTS control_plane_principals (
    id                  TEXT PRIMARY KEY,
    principal_key       TEXT    NOT NULL UNIQUE,
    kind                TEXT    NOT NULL
        CHECK (kind IN (
            'founder', 'scheduler', 'mcp_service', 'worker', 'provider_invocation'
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_control_plane_principals_token_digest
ON control_plane_principals (token_digest)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS control_plane_delivery_jobs (
    id                  TEXT PRIMARY KEY,
    idempotency_key     TEXT    NOT NULL UNIQUE,
    invocation_id       TEXT    NOT NULL REFERENCES provider_invocations (id),
    attempt_id          TEXT    NOT NULL REFERENCES runtime_attempts (id),
    run_id              TEXT    NOT NULL REFERENCES runtime_runs (id),
    provider            TEXT    NOT NULL,
    celery_task_id      TEXT,
    status              TEXT    NOT NULL
        CHECK (status IN (
            'registered', 'delivering', 'delivered', 'completed', 'failed', 'stale'
        )),
    registered_at       TEXT    NOT NULL,
    delivered_at        TEXT,
    completed_at        TEXT,
    redelivery_count    INTEGER NOT NULL DEFAULT 0,
    heartbeat_at        TEXT,
    result_json         TEXT,
    worker_principal_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_control_plane_delivery_jobs_status
ON control_plane_delivery_jobs (status, registered_at);

CREATE INDEX IF NOT EXISTS idx_control_plane_delivery_jobs_invocation
ON control_plane_delivery_jobs (invocation_id);
