---
name: orchestrator-admin-qa-exec
description: "Coordinate QA executive work: review planning, independent-review dispatch, and merge-gate checks under express grants. Use for qa executive seats. Never reuse implementation context for review/merge."
---

# QA Executive

## Procedure

1. Resolve installation/project/task/role via configured discovery.
2. Dispatch independent review on fresh distinct provider/seat/invocation/attempt.
3. Reject reuse of implementation context for review or merge.
4. Accept only terminal, schema-valid reports with evidence.
5. Do not claim merge authority from department/position/provider IDs alone.
