---
name: trust-but-verify
description: "Re-check status or tool claims against the authoritative source before they gate dispatch, close-out, merge, or completion reporting. Use when a clean or ready claim is about to drive a consequential decision. Not a mandate to re-verify routine low-stakes output."
---

# Trust But Verify

Read-only verification discipline for consequential status claims.

## Scope

Apply when a claim (registry status, tool "clean"/"0 findings", exit summary)
is about to gate a real decision. Do not re-verify every incidental command.

## Procedure

1. Identify the claim and its underlying source (live check, raw log, authoritative record).
2. Re-run the check or read the raw source — not a cached narrative summary.
3. Confirm the tool actually executed (silent crash reporting success is a known failure mode).
4. If confirmed: proceed and record what was verified.
5. If contradicted: stop; route through the configured finding interface; do not proceed on the original claim.

## Clean-control note

When criteria are satisfied, "no findings" is valid. Do not invent issues to fill a template.
