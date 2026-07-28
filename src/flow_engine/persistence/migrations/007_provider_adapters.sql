-- Provider host-runner protocol and immutable dispatch snapshots (additive).
-- Credentials and installation-private paths are deliberately not persisted.
ALTER TABLE provider_invocations ADD COLUMN invocation_packet_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE provider_invocations ADD COLUMN adapter_snapshot_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE provider_invocations ADD COLUMN adapter_snapshot_digest TEXT;
ALTER TABLE provider_invocations ADD COLUMN binding_digest TEXT;
ALTER TABLE provider_invocations ADD COLUMN provider_call_id TEXT;
ALTER TABLE provider_invocations ADD COLUMN heartbeat_at TEXT;
ALTER TABLE provider_invocations ADD COLUMN reconciliation_required INTEGER NOT NULL DEFAULT 0 CHECK (reconciliation_required IN (0, 1));

CREATE TABLE IF NOT EXISTS provider_runner_events (
    id TEXT PRIMARY KEY,
    invocation_id TEXT NOT NULL REFERENCES provider_invocations (id),
    event_type TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    redacted_event_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (invocation_id, event_digest)
);
CREATE INDEX IF NOT EXISTS idx_provider_runner_events_invocation
ON provider_runner_events (invocation_id, created_at);
