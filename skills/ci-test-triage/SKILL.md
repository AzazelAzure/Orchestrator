---
name: ci-test-triage
description: "Triage test and continuous-integration failures by classifying failure type, narrowing blast radius, and proposing deterministic fixes. Use when checks fail locally or in CI. May register findings for infrastructure flakes that need escalation."
---

# CI and Test Triage

## Procedure

1. Capture failing command and exact error.
2. Reproduce in the narrowest scope.
3. Classify: environment/setup | test regression | flake | fixture drift | lint/type.
4. Distinguish flake from deterministic failure before code edits.
5. Apply minimal fix; rerun targeted checks before the full suite.
6. Escalate infrastructure/auth flakes with evidence rather than masking; use `finding.create` when durable escalation is required.
7. Return via handoff-contract noting this procedure was used.

## Guidance

- Avoid full-suite reruns until targeted failures pass.
