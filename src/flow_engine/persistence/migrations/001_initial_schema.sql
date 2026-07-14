-- Initial kernel schema (Phase 0).
-- IDs are ULID strings; callers supply them at insert time.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS queues (
    id          TEXT PRIMARY KEY,
    project_id  TEXT    NOT NULL REFERENCES projects (id),
    name        TEXT    NOT NULL,
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS work_items (
    id            TEXT PRIMARY KEY,
    queue_id      TEXT    NOT NULL REFERENCES queues (id),
    status        TEXT    NOT NULL,
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    claimed_by    TEXT,
    revision      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS work_dependencies (
    work_item_id   TEXT NOT NULL REFERENCES work_items (id),
    depends_on_id  TEXT NOT NULL REFERENCES work_items (id),
    PRIMARY KEY (work_item_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS resources (
    id            TEXT PRIMARY KEY,
    kind          TEXT    NOT NULL,
    claim_policy  TEXT    NOT NULL CHECK (claim_policy IN ('advisory', 'strict')),
    revision      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS leases (
    id          TEXT PRIMARY KEY,
    resource_id TEXT    NOT NULL REFERENCES resources (id),
    holder      TEXT    NOT NULL,
    mode        TEXT    NOT NULL CHECK (mode = 'exclusive')
);

CREATE TABLE IF NOT EXISTS gates (
    id            TEXT PRIMARY KEY,
    work_item_id  TEXT    NOT NULL REFERENCES work_items (id),
    gate_type     TEXT    NOT NULL,
    status        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,
    event_type       TEXT    NOT NULL,
    actor            TEXT    NOT NULL,
    payload_json     TEXT    NOT NULL DEFAULT '{}',
    idempotency_key  TEXT,
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_results (
    key          TEXT PRIMARY KEY,
    result_json  TEXT NOT NULL
);

-- Append-only enforcement for the event ledger.
CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events table is append-only');
END;

-- Indexes for common access patterns.
CREATE INDEX IF NOT EXISTS idx_queues_project_id ON queues (project_id);
CREATE INDEX IF NOT EXISTS idx_work_items_queue_status ON work_items (queue_id, status);
CREATE INDEX IF NOT EXISTS idx_leases_resource_id ON leases (resource_id);
CREATE INDEX IF NOT EXISTS idx_gates_work_item_id ON gates (work_item_id);
CREATE INDEX IF NOT EXISTS idx_events_idempotency_key ON events (idempotency_key);
