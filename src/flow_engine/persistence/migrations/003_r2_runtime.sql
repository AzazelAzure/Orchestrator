-- R2 persistent governed runtime (additive).
-- Preserves legacy queue/work/gate/lease rows and claim eligibility.

CREATE TABLE IF NOT EXISTS runtime_runs (
    id                    TEXT PRIMARY KEY,
    work_item_id          TEXT    NOT NULL REFERENCES work_items (id),
    project_id            TEXT    NOT NULL REFERENCES projects (id),
    budget_scope_id       TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    provider              TEXT,
    provider_limit_state  TEXT    NOT NULL DEFAULT 'open'
        CHECK (provider_limit_state IN ('open', 'halted')),
    revision              INTEGER NOT NULL DEFAULT 0,
    grant_json            TEXT    NOT NULL DEFAULT '{}',
    policy_snapshot_json  TEXT    NOT NULL DEFAULT '{}',
    gate_snapshot_json    TEXT    NOT NULL DEFAULT '{}',
    credit_budget_total   INTEGER NOT NULL DEFAULT 9,
    credit_budget_per_provider INTEGER NOT NULL DEFAULT 3,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runtime_runs_work_item_id ON runtime_runs (work_item_id);
CREATE INDEX IF NOT EXISTS idx_runtime_runs_project_status ON runtime_runs (project_id, status);
CREATE INDEX IF NOT EXISTS idx_runtime_runs_provider_status ON runtime_runs (provider, status);
CREATE INDEX IF NOT EXISTS idx_runtime_runs_budget_scope ON runtime_runs (budget_scope_id);

CREATE TABLE IF NOT EXISTS runtime_attempts (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT    NOT NULL REFERENCES runtime_runs (id),
    attempt_number     INTEGER NOT NULL,
    status             TEXT    NOT NULL,
    lease_holder       TEXT,
    lease_expires_at   TEXT,
    last_heartbeat_at  TEXT,
    hard_deadline_at   TEXT,
    inactivity_deadline_at TEXT,
    dispatched_at      TEXT,
    possible_side_effect INTEGER NOT NULL DEFAULT 0,
    revision           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    UNIQUE (run_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS idx_runtime_attempts_run_status ON runtime_attempts (run_id, status);

CREATE TABLE IF NOT EXISTS provider_invocations (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT    NOT NULL REFERENCES runtime_attempts (id),
    run_id          TEXT    NOT NULL REFERENCES runtime_runs (id),
    provider        TEXT    NOT NULL,
    status          TEXT    NOT NULL
        CHECK (status IN (
            'reserved', 'dispatched', 'complete', 'failed', 'outcome_unknown', 'reconciled'
        )),
    request_digest  TEXT    NOT NULL,
    result_json     TEXT,
    evidence_json   TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (attempt_id)
);

CREATE INDEX IF NOT EXISTS idx_provider_invocations_run_provider
ON provider_invocations (run_id, provider, status);

CREATE TABLE IF NOT EXISTS runtime_commands (
    id                       TEXT PRIMARY KEY,
    operation_id             TEXT    NOT NULL UNIQUE,
    command_type             TEXT    NOT NULL,
    principal_id             TEXT    NOT NULL,
    surface                  TEXT    NOT NULL,
    target_id                TEXT,
    request_digest           TEXT    NOT NULL,
    attempt_id               TEXT,
    provider_invocation_id   TEXT,
    idempotency_scope        TEXT    NOT NULL,
    status                   TEXT    NOT NULL
        CHECK (status IN ('accepted', 'applied', 'rejected')),
    result_json              TEXT,
    error_code               TEXT,
    created_at               TEXT    NOT NULL,
    applied_at               TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_runtime_commands_idempotency_scope
ON runtime_commands (idempotency_scope);

CREATE TABLE IF NOT EXISTS credit_entries (
    id              TEXT PRIMARY KEY,
    run_id          TEXT    NOT NULL REFERENCES runtime_runs (id),
    provider        TEXT    NOT NULL,
    kind            TEXT    NOT NULL
        CHECK (kind IN ('reservation', 'settlement', 'release')),
    units           INTEGER NOT NULL,
    attempt_id      TEXT,
    invocation_id   TEXT,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_credit_entries_run_provider ON credit_entries (run_id, provider);

CREATE TRIGGER IF NOT EXISTS credit_entries_no_update
BEFORE UPDATE ON credit_entries
BEGIN
    SELECT RAISE(ABORT, 'credit_entries table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS credit_entries_no_delete
BEFORE DELETE ON credit_entries
BEGIN
    SELECT RAISE(ABORT, 'credit_entries table is append-only');
END;

CREATE TABLE IF NOT EXISTS audit_events (
    id            TEXT PRIMARY KEY,
    event_type    TEXT    NOT NULL,
    actor         TEXT    NOT NULL,
    anomaly_code  TEXT,
    command_id    TEXT,
    payload_json  TEXT    NOT NULL DEFAULT '{}',
    created_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at ON audit_events (created_at);
CREATE INDEX IF NOT EXISTS idx_audit_events_anomaly_code ON audit_events (anomaly_code);

CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events table is append-only');
END;

CREATE TABLE IF NOT EXISTS reconciliation_evidence (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT    NOT NULL REFERENCES runtime_attempts (id),
    invocation_id   TEXT    NOT NULL REFERENCES provider_invocations (id),
    outcome         TEXT    NOT NULL,
    evidence_json   TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS reconciliation_evidence_no_update
BEFORE UPDATE ON reconciliation_evidence
BEGIN
    SELECT RAISE(ABORT, 'reconciliation_evidence table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS reconciliation_evidence_no_delete
BEFORE DELETE ON reconciliation_evidence
BEGIN
    SELECT RAISE(ABORT, 'reconciliation_evidence table is append-only');
END;
