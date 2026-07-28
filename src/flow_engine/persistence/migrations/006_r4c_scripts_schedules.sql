-- R4C script executions and schedule runs (additive).
-- Preserves R1–R4B tables and legacy queue claim eligibility.

CREATE TABLE IF NOT EXISTS script_executions (
    id                      TEXT PRIMARY KEY,
    script_id               TEXT    NOT NULL,
    status                  TEXT    NOT NULL
        CHECK (status IN (
            'registered', 'running', 'complete', 'failed',
            'cancelled', 'timeout', 'rejected'
        )),
    actor                   TEXT    NOT NULL,
    idempotency_key         TEXT    NOT NULL UNIQUE,
    executable_digest       TEXT    NOT NULL,
    image_digest            TEXT    NOT NULL,
    input_json              TEXT    NOT NULL DEFAULT '{}',
    output_json             TEXT,
    schedule_run_id         TEXT,
    registered_at           TEXT    NOT NULL,
    started_at              TEXT,
    completed_at            TEXT,
    error_code              TEXT,
    cancel_requested        INTEGER NOT NULL DEFAULT 0
        CHECK (cancel_requested IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_script_executions_status
ON script_executions (status, registered_at);

CREATE INDEX IF NOT EXISTS idx_script_executions_script
ON script_executions (script_id, registered_at);

CREATE TABLE IF NOT EXISTS schedule_runs (
    id                      TEXT PRIMARY KEY,
    schedule_id             TEXT    NOT NULL,
    planned_time            TEXT    NOT NULL,
    status                  TEXT    NOT NULL
        CHECK (status IN (
            'claimed', 'running', 'complete', 'failed', 'cancelled'
        )),
    actor                   TEXT    NOT NULL,
    provider_call_budget    INTEGER NOT NULL DEFAULT 0
        CHECK (provider_call_budget = 0),
    script_ids_json         TEXT    NOT NULL DEFAULT '[]',
    result_json             TEXT,
    claimed_at              TEXT    NOT NULL,
    completed_at            TEXT,
    timezone                TEXT    NOT NULL DEFAULT 'Asia/Manila',
    UNIQUE (schedule_id, planned_time)
);

CREATE INDEX IF NOT EXISTS idx_schedule_runs_status
ON schedule_runs (status, claimed_at);

CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule
ON schedule_runs (schedule_id, planned_time);
