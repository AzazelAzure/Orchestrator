# R2 persistent governed runtime

R2 adds a sole-writer state coordinator, additive SQLite schema, governed run
lifecycle, credit/concurrency envelopes, mock provider runners, and CLI
controls. It does **not** activate organization/loadout semantics (R3), DRF,
Redis/Celery, MCP lane containers, scripts, schedules, Docker, or hosted
access (R4+).

## Sole writer

All R2 mutations enter through `flow_engine.coordinator.StateCoordinator.accept`
with a typed `RuntimeCommand`. CLI (`flowctl runtime …`) is a thin adapter.
Adapters must not open SQLite for authoritative writes outside the coordinator.

## Lifecycle

Runs/attempts use:

`pending → claimed → {complete, failed, paused, cancelled, outcome_unknown}`

with `paused → claimed|cancelled`, `outcome_unknown → reconciling`, and
`reconciling → complete|failed|cancelled`. Legacy queue work items retain
`pending|claimed|complete|failed` claim/complete/fail/retry behavior; R2 syncs
work status when a governed run transitions.

## Credits and timing

| Envelope | Value |
|---|---|
| Global / per-provider / per-project / per-run concurrency | 3 / 1 / 3 / 2 |
| Per-attempt provider calls | 1 |
| Acceptance credits | 9 total / 3 per provider |
| Heartbeat / inactivity / hard timeout | 60s / 5m / 30m |

Reserve before dispatch. Terminal or `outcome_unknown` settles (consumes) the
reservation. No automatic paid retry. The acceptance ceiling is shared across
every run carrying the same explicit `budget_scope_id`; it is not reset per
work item or runtime run.

## System test grants

R2 uses explicit `SystemTestGrant` objects. Each grant must carry a non-empty,
stable `budget_scope_id` for the complete acceptance campaign; the CLI requires
`--budget-scope-id` on every runtime operation. Minting a new scope per work
item would define a different campaign and must not be used to evade a budget.
Passing `loadout_id` or
`organization_profile_id` is denied. Concrete installation CLI paths and
credentials stay untracked; only mock provider runners ship in-tree.

## Founder step-up

`flowctl runtime new-attempt` requires founder role plus step-up evidence
(reauthentication ≤5 minutes, reason, evidence, duplicate-cost ack, policy
revision, new idempotency identity). MCP/schedule surfaces cannot issue this
command.

## Recovery

`flowctl runtime recover restart|worker-death|reconstruct|timeouts` reconstructs
eligible delivery from SQLite without duplicating paid calls. Timeout after
possible dispatch becomes `outcome_unknown`; reconcile original invocation
before any new paid attempt.

## CLI surface

```bash
flowctl runtime preview|create|run|step|claim|pause|resume|cancel|result|heartbeat
flowctl runtime reconcile|provider-limit|new-attempt|show|recover
```
