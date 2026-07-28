---
name: anomaly-handling
description: "Classify runtime anomalies using the A0–A5 taxonomy, persist findings/anomalies, and stop mutation when audit persistence is unavailable. Use when an unexpected integrity, authority, recovery, evidence, or quality issue must be recorded before continuing."
---

# Anomaly Handling

## Procedure

1. Capture redacted evidence (no secrets, no private absolute paths).
2. Classify using A0–A5:
   - A0 integrity/security → stop mutation; critical finding
   - A1 uncertain side effect → mark unknown; reconcile before retry
   - A2 authority/scope/gate → deny; high finding; revoke attempt grant
   - A3 runtime/resource → pause; preserve recovery state
   - A4 evidence/report → reject completion; require correction
   - A5 quality/maintenance → record and route; block only when gates require
3. Persist via the configured finding/anomaly interface; omission is invalid.
4. If audit/anomaly persistence is unavailable, stop further mutation.
5. Include anomalies/findings (empty list allowed), gaps, evidence, and terminal status in any handoff.
