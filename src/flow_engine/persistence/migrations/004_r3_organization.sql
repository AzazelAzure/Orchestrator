-- R3 organization, delegation, and resolved loadout pins (additive).
-- Preserves legacy queue rows and R2 runtime tables.

CREATE TABLE IF NOT EXISTS organization_profiles (
    id              TEXT PRIMARY KEY,
    name            TEXT    NOT NULL UNIQUE,
    version         TEXT    NOT NULL DEFAULT '0.1.0',
    content_sha256  TEXT    NOT NULL,
    policy_revision TEXT    NOT NULL DEFAULT 'r3-default',
    profile_json    TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS departments (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    department_key  TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    authority_ceiling_json TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    UNIQUE (organization_id, department_key)
);

CREATE TABLE IF NOT EXISTS hierarchy_layers (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    layer_key       TEXT    NOT NULL,
    rank            INTEGER NOT NULL,
    authority_ceiling_json TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    UNIQUE (organization_id, layer_key),
    UNIQUE (organization_id, rank)
);

CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    department_id   TEXT    NOT NULL REFERENCES departments (id),
    hierarchy_layer_id TEXT NOT NULL REFERENCES hierarchy_layers (id),
    position_key    TEXT    NOT NULL,
    loadout_id      TEXT    NOT NULL,
    authority_ceiling_json TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    UNIQUE (organization_id, department_id, position_key)
);

CREATE TABLE IF NOT EXISTS actors (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    actor_key       TEXT    NOT NULL,
    display_name    TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    UNIQUE (organization_id, actor_key)
);

CREATE TABLE IF NOT EXISTS provider_seats (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    actor_id        TEXT    NOT NULL REFERENCES actors (id),
    provider        TEXT    NOT NULL,
    seat_key        TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    UNIQUE (organization_id, seat_key)
);

CREATE TABLE IF NOT EXISTS authority_ceilings (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    scope_kind      TEXT    NOT NULL
        CHECK (scope_kind IN (
            'engine_floor', 'handbook', 'installation_policy', 'product_base',
            'department', 'hierarchy_layer', 'position', 'project_repo_extension',
            'task_class', 'explicit_task_grant'
        )),
    scope_ref       TEXT,
    ceiling_json    TEXT    NOT NULL,
    content_sha256  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_authority_ceilings_org_scope
ON authority_ceilings (organization_id, scope_kind);

CREATE TABLE IF NOT EXISTS assignments (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    work_item_id    TEXT    NOT NULL REFERENCES work_items (id),
    position_id     TEXT    NOT NULL REFERENCES positions (id),
    actor_id        TEXT    NOT NULL REFERENCES actors (id),
    provider_seat_id TEXT   NOT NULL REFERENCES provider_seats (id),
    loadout_id      TEXT    NOT NULL,
    parent_assignment_id TEXT REFERENCES assignments (id),
    status          TEXT    NOT NULL
        CHECK (status IN ('active', 'completed', 'cancelled', 'superseded')),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assignments_work_item ON assignments (work_item_id);
CREATE INDEX IF NOT EXISTS idx_assignments_parent ON assignments (parent_assignment_id);

CREATE TABLE IF NOT EXISTS delegation_requests (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    parent_assignment_id TEXT NOT NULL REFERENCES assignments (id),
    from_position_id TEXT   NOT NULL REFERENCES positions (id),
    to_position_id  TEXT    NOT NULL REFERENCES positions (id),
    work_item_id    TEXT    NOT NULL REFERENCES work_items (id),
    packet_json     TEXT    NOT NULL,
    packet_sha256   TEXT    NOT NULL,
    status          TEXT    NOT NULL
        CHECK (status IN (
            'requested', 'accepted', 'declined', 'rerouted', 'dispatched', 'cancelled'
        )),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_delegation_requests_parent
ON delegation_requests (parent_assignment_id, status);

CREATE TABLE IF NOT EXISTS delegation_dispositions (
    id              TEXT PRIMARY KEY,
    request_id      TEXT    NOT NULL REFERENCES delegation_requests (id),
    disposition     TEXT    NOT NULL
        CHECK (disposition IN ('accepted', 'declined', 'rerouted')),
    actor_id        TEXT    NOT NULL REFERENCES actors (id),
    reason          TEXT    NOT NULL DEFAULT '',
    reroute_position_id TEXT REFERENCES positions (id),
    created_at      TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS delegation_dispositions_no_update
BEFORE UPDATE ON delegation_dispositions
BEGIN
    SELECT RAISE(ABORT, 'delegation_dispositions table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS delegation_dispositions_no_delete
BEFORE DELETE ON delegation_dispositions
BEGIN
    SELECT RAISE(ABORT, 'delegation_dispositions table is append-only');
END;

CREATE TABLE IF NOT EXISTS handoffs (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    from_assignment_id TEXT NOT NULL REFERENCES assignments (id),
    to_assignment_id TEXT   NOT NULL REFERENCES assignments (id),
    work_item_id    TEXT    NOT NULL REFERENCES work_items (id),
    packet_json     TEXT    NOT NULL,
    packet_sha256   TEXT    NOT NULL,
    review_required INTEGER NOT NULL DEFAULT 1,
    accepted_at     TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS handoffs_no_delete
BEFORE DELETE ON handoffs
BEGIN
    SELECT RAISE(ABORT, 'handoffs table is append-only for deletes');
END;

CREATE TABLE IF NOT EXISTS resolved_loadout_snapshots (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    loadout_id      TEXT    NOT NULL,
    organization_profile_hash TEXT NOT NULL,
    loadout_hash    TEXT    NOT NULL,
    policy_identity TEXT    NOT NULL,
    policy_hash     TEXT    NOT NULL,
    member_asset_hashes_json TEXT NOT NULL,
    resolution_json TEXT    NOT NULL,
    content_sha256  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS resolved_loadout_snapshots_no_update
BEFORE UPDATE ON resolved_loadout_snapshots
BEGIN
    SELECT RAISE(ABORT, 'resolved_loadout_snapshots table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS resolved_loadout_snapshots_no_delete
BEFORE DELETE ON resolved_loadout_snapshots
BEGIN
    SELECT RAISE(ABORT, 'resolved_loadout_snapshots table is append-only');
END;

CREATE TABLE IF NOT EXISTS task_grants (
    id              TEXT PRIMARY KEY,
    organization_id TEXT    NOT NULL REFERENCES organization_profiles (id),
    grant_id        TEXT    NOT NULL UNIQUE,
    principal_id    TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    surfaces_json   TEXT    NOT NULL,
    providers_json  TEXT    NOT NULL,
    budget_scope_id TEXT    NOT NULL,
    assignment_id   TEXT    REFERENCES assignments (id),
    loadout_id      TEXT    NOT NULL,
    snapshot_id     TEXT    NOT NULL REFERENCES resolved_loadout_snapshots (id),
    capabilities_json TEXT  NOT NULL DEFAULT '[]',
    effect_ceiling  TEXT    NOT NULL DEFAULT '',
    policy_revision TEXT    NOT NULL,
    compatibility_mode TEXT NOT NULL DEFAULT 'r3_resolved'
        CHECK (compatibility_mode IN ('r3_resolved', 'r2_system_test')),
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS immutable_dispatch_pins (
    id              TEXT PRIMARY KEY,
    run_id          TEXT    REFERENCES runtime_runs (id),
    attempt_id      TEXT,
    invocation_id   TEXT,
    grant_id        TEXT    NOT NULL,
    snapshot_id     TEXT    NOT NULL REFERENCES resolved_loadout_snapshots (id),
    policy_identity TEXT    NOT NULL,
    policy_hash     TEXT    NOT NULL,
    organization_profile_identity TEXT NOT NULL,
    organization_profile_hash TEXT NOT NULL,
    loadout_identity TEXT   NOT NULL,
    loadout_hash    TEXT    NOT NULL,
    member_asset_hashes_json TEXT NOT NULL,
    packet_hash     TEXT    NOT NULL,
    budget_identity TEXT    NOT NULL,
    grant_identity  TEXT    NOT NULL,
    pin_json        TEXT    NOT NULL,
    content_sha256  TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_immutable_dispatch_pins_run
ON immutable_dispatch_pins (run_id);

CREATE TRIGGER IF NOT EXISTS immutable_dispatch_pins_no_update
BEFORE UPDATE ON immutable_dispatch_pins
BEGIN
    SELECT RAISE(ABORT, 'immutable_dispatch_pins table is append-only');
END;

CREATE TRIGGER IF NOT EXISTS immutable_dispatch_pins_no_delete
BEFORE DELETE ON immutable_dispatch_pins
BEGIN
    SELECT RAISE(ABORT, 'immutable_dispatch_pins table is append-only');
END;

CREATE TABLE IF NOT EXISTS child_closure_evidence (
    id              TEXT PRIMARY KEY,
    parent_assignment_id TEXT NOT NULL REFERENCES assignments (id),
    child_assignment_id TEXT NOT NULL REFERENCES assignments (id),
    handoff_id      TEXT    REFERENCES handoffs (id),
    status          TEXT    NOT NULL
        CHECK (status IN ('pending', 'accepted', 'rejected')),
    evidence_json   TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE (parent_assignment_id, child_assignment_id)
);
