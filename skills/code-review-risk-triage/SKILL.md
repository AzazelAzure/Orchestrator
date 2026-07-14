---
name: code-review-risk-triage
description: "Review a local or branch diff for correctness, regressions, security, and test gaps before change publication. Pre-publication self-review distinct from merge-gate procedures. May register findings when durable escalation is required."
---

# Code Review Risk Triage

Pre-publication risk review of a diff.

## Procedure

1. Identify changed files and runtime impact surface.
2. Review in severity order: correctness/regressions → security/data integrity → contracts → tests → maintainability.
3. List findings with concrete evidence (file:line).
4. Include explicit test coverage gaps.
5. Fix blockers or document accepted risks before publication readiness.
6. Optionally register durable findings via `finding.create` when escalation is required.
7. Return via handoff-contract.

## Output

Lead with findings, highest severity first. State "No critical issues found" when applicable. Actionable issues only.
