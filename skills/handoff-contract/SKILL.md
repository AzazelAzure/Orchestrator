---
name: handoff-contract
description: "Standardize delegated work outputs into a transferable handoff contract a receiver can act on without the original conversation. Use when completing a slice, synthesizing a large diff for closeout, or returning results for merge or review."
---

# Handoff Contract

Durable coordination surface for completing delegated work.

## Required section order

```markdown
## Objective
## Assumptions and Unknowns
## Evidence
## Files
## Risks
## Verification
## Finding disposition
## Branch/change publication status
## Skill(s) used
## Next Action
```

## Rules

- Keep each section compact and concrete.
- Finding disposition is required: `none found` or a list of finding refs filed this session.
- Skill(s) used lists every procedure actually loaded.
- Call out blockers immediately.
- If no files changed, state `Changed: none`.
- If verification is partial, state exactly what remains.
- For large diffs: note omitted chunks; do not claim completeness when chunks were skipped.
