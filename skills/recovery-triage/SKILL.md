---
name: recovery-triage
description: "Triage coordinator restart, broker loss, worker death, and timeout paths without duplicating paid provider calls. Use when recovery controls are invoked."
---

# Recovery Triage

## Procedure

1. Identify failure class: restart, broker loss, worker death, timeout, restore.
2. Reconstruct eligible delivery from durable SQLite state.
3. If dispatch was possible, treat timeout/kill as outcome_unknown and reconcile first.
4. Never auto-issue a new paid attempt; founder step-up required after unknown.
5. Record anomalies (A1/A3) and preserve attempt/invocation identities.
