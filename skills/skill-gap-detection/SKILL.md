---
name: skill-gap-detection
description: "Interpret a session evidence window for repeated manual friction patterns and propose skill-gap wishlist candidates only. Use on demand when reviewing recent tool or command traces for reusable procedure gaps. Does not create, install, activate, or schedule skill packages. Local candidate/evidence output only."
---

# Skill Gap Detection

On-demand interpretation procedure. This package teaches how to read friction
patterns and draft a candidate record. It does not own schedulers, durable
stores, or package mutation.

## Guardrails

- On-demand only. No scheduler. No external network writes.
- Local candidate/evidence output only (repo-local notes under an operator-chosen path such as `.local/skill-gap/`).
- Do not create, modify, install, activate, or schedule skill packages.
- Do not consume prior candidates or wishlist rows as friction input (no self-signal loop).
- Proposer, reviewer, and approver identities must be distinct; do not self-approve.
- Prefer derived pattern summaries over raw command/tool bodies on portable surfaces.

## Procedure

1. Obtain the current session evidence window (tool/command pattern summary).
2. Identify repeated manual sequences that a future procedure could absorb.
3. For each pattern, record description, recurrence signal, and logical capability needs.
4. Emit a local wishlist / candidate draft only.
5. Do not claim durable engine recording unless an operator-provided store is explicitly configured and used outside this package.

## Degraded mode

If no durable store is configured: keep the wishlist local and human-readable only. Do not escalate privileges or invent schedules.
