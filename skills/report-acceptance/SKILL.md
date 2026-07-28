---
name: report-acceptance
description: "Accept or reject terminal reports against packet acceptance criteria. Use when a child handoff claims completion. Parent closure stays blocked until acceptance."
---

# Report Acceptance

## Procedure

1. Load the pinned packet and acceptance criteria.
2. Verify terminal status, evidence refs, anomalies/findings list, and gaps.
3. Reject non-terminal, schema-invalid, or context-colliding reports.
4. On accept, record handoff acceptance so parent closure may proceed.
5. On reject, require corrected evidence; do not silently waive.
