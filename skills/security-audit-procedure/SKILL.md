---
name: security-audit-procedure
description: "Audit code and configuration for security risks, prioritize exploitability, and propose concrete hardening fixes. Use for auth, secrets, input handling, dependency risk, or pre-release readiness. Re-verify tooling output before treating a clean result as trusted."
---

# Security Audit Procedure

## Procedure

1. Define audit scope and threat assumptions.
2. Run configured audit tooling; trust-but-verify the output (confirm checks executed).
3. Identify findings with severity and exploitability.
4. Propose concrete fixes and minimum verification for high/critical items.
5. Re-check high-severity findings after fixes.
6. Report residual risk. Register findings via the configured finding interface when available.

## Severity

- Critical — likely exploit, high impact; block release readiness.
- High — meaningful exposure; fix or explicit accept-with-risk.
- Medium — schedule near-term.
- Low — defense-in-depth.

## Guardrails

- Do not log secrets in findings.
- Unresolved critical findings block release readiness claims.
