-- Governance invariants: gates, leases, artifacts, policy, findings.
-- Forward migration from 001; preserves existing rows with backfills.

CREATE TABLE IF NOT EXISTS policy_versions (
    id            TEXT PRIMARY KEY,
    policy_id     TEXT    NOT NULL,
    version       TEXT    NOT NULL,
    content_hash  TEXT    NOT NULL,
    canonical_uri TEXT    NOT NULL,
    created_by    TEXT    NOT NULL,
    effective_at  TEXT    NOT NULL,
    UNIQUE (policy_id, version)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id              TEXT PRIMARY KEY,
    uri             TEXT    NOT NULL,
    artifact_type   TEXT    NOT NULL,
    content_hash    TEXT,
    sensitivity     TEXT    NOT NULL,
    retention_class TEXT    NOT NULL,
    created_by      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS policy_versions_no_update
BEFORE UPDATE ON policy_versions
BEGIN
    SELECT RAISE(ABORT, 'policy_versions table is immutable');
END;

CREATE TRIGGER IF NOT EXISTS policy_versions_no_delete
BEFORE DELETE ON policy_versions
BEGIN
    SELECT RAISE(ABORT, 'policy_versions table is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifacts_no_update
BEFORE UPDATE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts table is immutable');
END;

CREATE TRIGGER IF NOT EXISTS artifacts_no_delete
BEFORE DELETE ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifacts table is immutable');
END;

ALTER TABLE gates ADD COLUMN requirement TEXT NOT NULL DEFAULT 'required'
    CHECK (requirement IN ('required', 'optional'));
ALTER TABLE gates ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE gates ADD COLUMN created_at TEXT NOT NULL DEFAULT '';

UPDATE gates
SET created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE created_at = '';

CREATE TABLE IF NOT EXISTS gate_actions (
    id                   TEXT PRIMARY KEY,
    gate_id              TEXT NOT NULL REFERENCES gates (id),
    action_type          TEXT NOT NULL CHECK (action_type IN ('passed', 'failed', 'waived')),
    actor                TEXT NOT NULL,
    authority            TEXT,
    reason               TEXT,
    evidence_artifact_id TEXT REFERENCES artifacts (id),
    gate_revision        INTEGER NOT NULL,
    policy_version_id    TEXT REFERENCES policy_versions (id),
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_actions_gate_id ON gate_actions (gate_id);

CREATE TRIGGER IF NOT EXISTS gate_actions_no_update
BEFORE UPDATE ON gate_actions
BEGIN
    SELECT RAISE(ABORT, 'gate_actions table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS gate_actions_no_delete
BEFORE DELETE ON gate_actions
BEGIN
    SELECT RAISE(ABORT, 'gate_actions table is append-only');
END;

ALTER TABLE leases ADD COLUMN acquired_at TEXT;
ALTER TABLE leases ADD COLUMN expires_at TEXT;
ALTER TABLE leases ADD COLUMN released_at TEXT;
ALTER TABLE leases ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;

UPDATE leases
SET
    acquired_at = COALESCE(acquired_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    expires_at = COALESCE(expires_at, datetime(strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), '+1 year'))
WHERE released_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_one_active_per_resource
ON leases (resource_id)
WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS findings (
    id           TEXT PRIMARY KEY,
    project_id   TEXT REFERENCES projects (id),
    work_item_id TEXT REFERENCES work_items (id),
    severity     TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    status       TEXT NOT NULL CHECK (status IN ('open', 'triaged', 'resolved', 'accepted', 'reopened')),
    summary      TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS finding_actions (
    id                   TEXT PRIMARY KEY,
    finding_id           TEXT NOT NULL REFERENCES findings (id),
    action_type          TEXT NOT NULL,
    actor                TEXT NOT NULL,
    from_status          TEXT,
    to_status            TEXT NOT NULL,
    reason               TEXT,
    evidence_artifact_id TEXT REFERENCES artifacts (id),
    policy_version_id    TEXT REFERENCES policy_versions (id),
    finding_revision     INTEGER NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_finding_actions_finding_id ON finding_actions (finding_id);

CREATE TABLE IF NOT EXISTS finding_evidence (
    finding_id  TEXT NOT NULL REFERENCES findings (id),
    artifact_id TEXT NOT NULL REFERENCES artifacts (id),
    PRIMARY KEY (finding_id, artifact_id)
);

CREATE TRIGGER IF NOT EXISTS finding_actions_no_update
BEFORE UPDATE ON finding_actions
BEGIN
    SELECT RAISE(ABORT, 'finding_actions table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS finding_actions_no_delete
BEFORE DELETE ON finding_actions
BEGIN
    SELECT RAISE(ABORT, 'finding_actions table is append-only');
END;
