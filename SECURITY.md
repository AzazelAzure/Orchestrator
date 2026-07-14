# Security Policy

## Supported versions

This repository is an early public-release candidate. Security fixes are accepted against the default branch when a public remote exists.

## Reporting a vulnerability

Please report suspected vulnerabilities privately to the repository maintainers
via the private contact channel configured on the hosting forge (for example a
security advisory form), not via public issues.

Include:

- affected component/version (commit SHA if known)
- reproduction steps
- impact assessment
- any known mitigations

Do not attach production secrets, customer data, or private credentials to a report.

## Scope notes

- The default MCP surface is read-only.
- Do not treat a green local test run as production readiness.
- Dependency alerts (Dependabot) are advisory until triage confirms exploitability.
