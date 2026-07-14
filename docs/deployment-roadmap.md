# Deployment roadmap (planning only)

**Status:** planning document only.  
**Not production ready.**  
**No schedule authorization** — phases are sequenced gates, not a calendar.

This document describes a possible progression toward hosted use of Orchestrator.
It does **not** add Dockerfiles, Django dependencies, deployment configs, secrets,
domains, VPS paths, or automation workflows to this repository.

## Non-goals (current repository)

- No product adapters or linkers in core (including any Portfolio linker — **not enabled**)
- No Django / DRF packaging yet
- No container images, reverse-proxy configs, or forge deploy workflows
- No committed secrets, environment files, hostnames, or machine-absolute paths
- No claim that local test green equals production readiness

## Phased progression

### P0 — Current local core (where we are)

Ship / operate locally:

- SQLite-backed engine kernel and application services
- CLI (`flowctl`)
- Optional **read-only** generic stdio MCP (`flowctl-mcp`)

**Go:** documented install; `pytest` green on supported Python; MCP remains read-only by default.  
**No-go:** writable MCP by default; product coupling; undisclosed secrets in tree.

### P1 — Portfolio pilot via external adapter (future, gated)

Only after **Portfolio** is public/live as a separate project:

- Integrate through an **external** adapter/linker **repo or package**
- Keep Orchestrator core product-agnostic (no in-tree Portfolio linker)

**Go:** Portfolio public; adapter owned outside this core; contract tests on the adapter boundary; no core brand/default-path leakage.  
**No-go:** embedding Portfolio linker or private paths in this repository.

### P2 — Thin HTTP control plane (future, gated)

A thin **Django + Django REST Framework** control plane that:

- Calls existing application services (no reimplementation of engine logic in views)
- Provides authn/authz, idempotency keys, audit trails, schema/version negotiation
- Applies rate limiting and publishes OpenAPI
- Ships **read-only-first** (mutating APIs behind explicit later gates)

**Go:** authz model reviewed; idempotency + audit proven; OpenAPI matches behavior; read-only surface complete before writes.  
**No-go:** bypassing services; anonymous write; missing audit on mutating paths.

### P3 — Blue/green stateless API containers (future, gated)

Stateless API containers behind a reverse proxy on a VPS, with:

- Health and readiness probes
- Drain and rollback procedures
- **Explicit SQLite constraint:** one authoritative database and **single-writer** coordination
- Migrations, backups, restore, and integrity gates are **separate from container color**
- **Do not duplicate the database per color**

**Go:** single-writer proven under rollout; backup/restore rehearsal passed; rollback drill recorded; integrity gate green.  
**No-go:** one DB file per color; multi-writer SQLite; cutover without restore proof.

### P4 — Scale evaluation (future, gated)

Only after load, recovery, and security evidence from earlier phases:

- Evaluate PostgreSQL and/or distributed workers

**Go:** measured evidence that SQLite single-writer limits are binding; migration design + dual-run plan.  
**No-go:** premature DB split without load/recovery/security evidence.

## Cross-cutting concerns

| Area | Expectations before hosted cutover |
|------|-------------------------------------|
| Threat / operational | Threat notes for auth, injection, secret handling, backup theft, MCP misuse; operator runbooks for drain/rollback |
| Observability | Structured logs, request/correlation IDs, health/readiness semantics; no secrets in logs |
| Backup / restore | Scheduled backup ownership; restore rehearsal on a non-authoritative copy; integrity check before traffic |
| Gates / TBV | Consequential “ready” claims re-checked against live probes and restore evidence, not narrative alone |

## Explicit SQLite rule (P3+)

Blue and green (or any concurrent API generations) share **one** authoritative SQLite database with single-writer coordination. Container color is disposable. Database lifecycle (migrate / backup / restore / integrity) is owned outside color cutover.

## Summary

| Phase | Intent | In this repo now? |
|-------|--------|-------------------|
| P0 | Local engine + CLI + read-only MCP | Yes (candidate) |
| P1 | External Portfolio adapter after Portfolio is public | No |
| P2 | Thin Django/DRF control plane | No |
| P3 | Blue/green stateless API + shared SQLite | No |
| P4 | Evaluate Postgres / workers | No |

This roadmap authorizes **neither** implementation nor scheduling of P1–P4.
