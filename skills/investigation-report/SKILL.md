---
name: investigation-report
description: "Produce scoped factual investigation reports answering concrete codebase questions for design gates without proposing product design decisions. Use for field existence, current behavior, and call-path evidence. May frame out-of-scope risks as finding candidates."
---

# Investigation Report

Produce facts for design gates, not design decisions.

## Procedure

1. Receive a scoped question set.
2. Trace with focused search and reads; report facts with file-and-line evidence.
3. Separate verified facts, inferences, and unresolved questions.
4. Stop at facts unless explicitly asked for labeled options.
5. Record out-of-scope risks as finding candidates when a configured interface and write authority exist; otherwise keep them in the report.

## Output

Use `Investigation scope`, `Findings`, `Open questions`, and `Evidence commands` sections.

## Degraded mode

If an authoritative source is unavailable, identify the missing source and do not substitute cached narrative as current evidence.
