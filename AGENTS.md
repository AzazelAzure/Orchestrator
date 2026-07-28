# AGENTS.md

Repository-specific operating rules for human and agent contributors working in
**Orchestrator**.

This file is local to this repository. It does not import private installation
paths, product adapters, or unpublished governance trees.

## Purpose

Keep agent work aligned with a generic, auditable workflow core: gates before
completion, trust-but-verify on consequential claims, explicit findings, no
secrets in history, tests before publication claims, and responsible change
publication.

## Reading order

1. This file
2. `docs/read-the-docs.md`
3. `README.md`
4. `docs/architecture.md`
5. `docs/skills.md`
6. Relevant `skills/*/SKILL.md` for the active task

## Non-negotiables

1. **No secrets.** Never commit credentials, tokens, private keys, `.env` values, or customer data. Prefer redacted evidence.
2. **No private absolute paths** in tracked files (user home directories, private machines, unpublished product trees).
3. **No product adapter creep.** Do not add private-product branding, linkers, or product-specific default config paths to the core.
4. **Generic gates.** Required gates and incomplete dependencies block work completion in the engine; do not bypass with silent force except through an auditable waiver path the CLI/engine already supports.
5. **Trust-but-verify.** Before a “clean / ready / 0 findings” claim gates merge or release readiness, re-check the authoritative source (`skills/trust-but-verify`).
6. **Findings.** Material defects/escalations should be recorded via the configured finding interface when available; otherwise note them explicitly in the handoff (`skills/handoff-contract`, `skills/ci-test-triage`).
7. **Tests.** Run the relevant suite (`pytest`) before claiming verification. Partial runs must be labeled as partial.
8. **Skills.** Prefer repo-local packages under `skills/`. Skill-gap work is on-demand only with local candidate/evidence output — no scheduler, no external writes (`skills/skill-gap-detection`).
9. **CPPRD (publication).** When publishing material changes: commit + push (when a remote exists) + open PR when review is required + update documentation in the same pass. Changelog stubs must not be left empty. Do not invent production readiness.

## Change discipline

- Prefer smallest coherent diffs.
- Keep MCP default read-only.
- Do not claim production readiness from this candidate alone.

## Anomaly / finding logging

When something unexpected happens that may recur (infra flake, contract break,
security suspicion):

1. Capture evidence without secrets
2. Classify severity
3. Record a finding or handoff note with pointers
4. Do not bury the issue in chat-only memory

## Out of scope for agents without explicit human authorization

- Creating public forge remotes or force-pushing
- Adding deploy containers, Django apps, or paid SaaS integrations
  (**exception:** R4A–R4D local control-plane Django/DRF + Compose active-test
  harness is authorized by the Headquarters R4 implementation packet; do not
  expand to hosting, real providers, or publication without a new authorization)
- Enabling scheduled skill-gap or any automation schedule
- Broad filesystem scans outside this repository
