---
name: review-gate
description: "Perform independent review using a distinct provider principal, seat, invocation, and attempt from implementation. Use for QA review seats."
---

# Review Gate

## Procedure

1. Verify review identities differ from implementation on provider, seat, invocation, attempt.
2. Review packet-only evidence; reject ambient implementation context reuse.
3. Check acceptance criteria, tests, anomalies, and risk.
4. Accept or reject explicitly; never self-merge or waive via MCP.
5. Record findings when durable escalation is required.
