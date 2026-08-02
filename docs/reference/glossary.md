# Glossary

Terms used across Orchestrator documentation. Definitions trace to in-tree source
unless marked **external** (installation-local, not shipped in this repo).

---

## A

**Acceptance campaign** — A bounded credit budget shared by all runtime runs
using the same `--budget-scope-id`. Source: `cli/runtime_cmds.py`, `domain/credits.py`.

**Active-test harness** — R4D Compose script `scripts/r4d_active_test.sh`; produces
local evidence only, does not close external gates.

**Advisory claim** — Resource lease policy that warns on conflict instead of hard-failing
(`ClaimPolicy.ADVISORY`). Source: `domain/states.py`.

**Anomaly code** — Taxonomy A0–A5 for findings (`AnomalyCode`). Source: `domain/states.py`.

**Attempt** — R2 unit of provider execution under a run; statuses mirror run lifecycle
plus reconciliation paths. Source: `domain/states.py`.

---

## B

**Bootstrap principals** — Ephemeral Compose registration of service tokens when
`ORCH_BOOTSTRAP_PRINCIPALS=1` (default **0**). Source: `control_plane/bootstrap.py`.

**Budget scope** — Stable identifier naming a credit campaign; must not be minted
per work item to evade limits.

---

## C

**Capability** — Read-only project operation (`repo_health`, etc.). Source: `capabilities/transport.py`.

**CPPRD** — Change publication discipline: commit, push when applicable, PR when
required, changelog + docs in same pass. Source: `AGENTS.md`.

**Coordinator** — `StateCoordinator`; sole SQLite writer for authoritative mutations.
Source: `coordinator/coordinator.py`.

---

## D

**Delivery job** — Worker-claimable unit linking provider invocation to Celery execution.
Source: `application/worker_delivery.py`.

**Deny-by-default** — Unknown coordinator commands allow only `founder` principal kind.
Source: `authz_matrix.py`.

**Dispatch pin** — Immutable R3 hash binding loadout resolution at dispatch time.
Source: `application/delegation_service.py`, `docs/r3-organization.md`.

**Dual identity** — MCP lane invokes carry initiating principal bearer plus MCP service
principal. Source: `mcp_lanes/drf_client.py`.

---

## E

**Envelope** — JSON command result from `StateCoordinator.accept` with `status`,
`result`, or `error` fields.

**Evidence artifact** — Pointer required for gate waivers and founder step-up operations.

---

## F

**Fail-closed** — Missing auth, unknown commands, or invalid transitions reject rather
than degrade to anonymous or permissive defaults.

**Finding** — Generic defect record with amendment history. Source: `application/finding_service.py`.

**Founder step-up** — Elevated reauthentication path for `new-attempt` and similar ops.
CLI-only. Source: `cli/runtime_cmds.py`, `authz_matrix.py`.

**flowctl** — CLI entrypoint (`flow_engine.cli.app`).

**flowctl-mcp** — Stdio MCP server for read-only capabilities.

**flow_engine** — Python import package name (distribution: `orchestrator`).

---

## G

**Gate** — Completion control on a work item (`open|passed|failed|waived`). Required
gates block completion.

**Grant** — R3 resolved task grant or principal capability bundle attached to authz checks.

---

## H

**Host runner** — Unix-socket subprocess adapter for installed provider CLIs.
Source: `providers/host_runner.py`.

**Human principal** — `kind=human` account from migration 008 user auth tables.

---

## I

**Idempotency key** — Client-supplied key preventing duplicate mutation application.
Source: `application/idempotency.py`.

**Inert catalog (R1)** — Versioned JSON under `agentic/catalogs/`; discovery only,
not runtime activation.

**Invocation** — Provider call record (`reserved` → `dispatched` → terminal).
Source: `domain/states.py`.

---

## L

**Live acceptance** — Scripts such as `orchestrator_live_acceptance.py` against a
running Compose stack with authenticated ops checks.

**Loadout** — Resolved positional skill/script bundle for R3 dispatch. Source: `application/loadout_resolution.py`.

---

## M

**Manifest (local stack)** — `.tmp/local-stack/manifest.json` cache; not authoritative
over coordinator `work_item_id`.

**Mock provider** — In-process provider used when `ORCH_PROVIDER_MODE=mock`.

**MCP lane** — R4 container exposing catalog-scoped tools via DRF.

---

## O

**Ops summary** — `GET /ops/summary/` aggregated read model; requires founder or
`ops.read`. Source: `control_plane/api/ops_urls.py`.

**Outcome unknown** — State when provider result cannot be determined; requires
reconcile before new paid attempt.

---

## P

**PAT** — Personal access token; opaque secret with digest at rest. Source: `user_auth.py`.

**Principal** — Authenticated actor with `kind`, `role`, and optional capabilities.

**Principal bootstrap** — See **Bootstrap principals**.

**Provider mode** — `mock` vs host-runner live bindings (`ORCH_PROVIDER_MODE`).

---

## R

**R1–R4** — Layered architecture slices; see layer docs `docs/r1-assets.md` through
`docs/r4-control-plane.md`.

**Reconciliation** — Process to settle `outcome_unknown` invocations without duplicate spend.

**Recovery command** — Founder-only coordinator ops reconstructing delivery state.

**Revision** — Optimistic concurrency counter on work items and some gates.

---

## S

**Schedule surface** — Celery Beat identity; effects-only schedule completion, no
founder step-up.

**Sole writer** — Invariant that only coordinator performs authoritative SQLite writes.

**Step-up evidence** — Founder reauthentication metadata for `new-attempt`.

**Strict claim** — Resource lease that conflicts fail closed (`ClaimPolicy.STRICT`).

**Surface** — Channel enum (`cli`, `rest`, `mcp`, `worker`, `schedule`, `test`).

**System test grant** — R2 compatibility grant path denying org/loadout semantics.

---

## V

**Verification ladder** — `scripts/verification_ladder.py` L1–L4 structured checks.

**VPS overlay** — `deploy/` env examples; not claimed as production-landed product.

---

## W

**Waiver** — Auditable gate exception requiring authority, reason, and evidence artifact ID.

**Work item** — Claimable queue unit with dependencies, gates, and revision.

**WAL** — SQLite write-ahead logging mode for persistence.

---

## See also

- [Domain and lifecycle](../guides/domain-and-lifecycle.md)
- [Surface reference](surfaces.md)
