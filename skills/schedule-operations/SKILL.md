---
name: schedule-operations
description: "Operate within grant-only schedule status/run guidance. Use when checking maintenance schedule state. Never activate schedules, waive gates, or invoke founder ops via MCP."
---

# Schedule Operations

## Procedure

1. Confirm the grant explicitly permits schedule status/run for this scope.
2. Read status or request on-demand run only within that grant.
3. Refuse schedule activation, waiver, HitM exception, and paid retry-after-unknown.
4. Treat alerts as dashboard + findings only.
5. Do not remediate repositories or mutate policies from schedule context.
