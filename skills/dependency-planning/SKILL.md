---
name: dependency-planning
description: "Order work items, gates, and dependencies so completion stays blocked until prerequisites are satisfied. Use when sequencing delegated work or validating parent/child closure order."
---

# Dependency Planning

## Procedure

1. Inventory work IDs, gate IDs, and explicit depends-on edges.
2. Detect cycles; fail closed if found.
3. Order execution so blockers resolve before dependents.
4. Keep parent closure blocked until child evidence/review is accepted.
5. Record the plan in the task packet; do not silently waive incomplete deps.
