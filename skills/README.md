# Orchestrator skills

Canonical portable skill packages for this repository live under `skills/`.

Each skill directory contains:

- `SKILL.md` — agent-facing procedure
- `agents/openai.yaml` — optional UI hints for agent surfaces that read it
- `manifest.json` — portable metadata (`on_demand` triggers; `scheduling_ref` always null for these seeds)

## Seed set

| Directory | Skill ID |
|-----------|----------|
| `session-orientation/` | `skill.session-orientation` |
| `repo-exploration-briefing/` | `skill.repo-exploration-briefing` |
| `trust-but-verify/` | `skill.trust-but-verify` |
| `design-first-gate/` | `skill.design-first-gate` |
| `handoff-contract/` | `skill.handoff-contract` |
| `ci-test-triage/` | `skill.ci-test-triage` |
| `code-review-risk-triage/` | `skill.code-review-risk-triage` |
| `security-audit-procedure/` | `skill.security-audit-procedure` |
| `skill-gap-detection/` | `skill.skill-gap-detection` |

`skill-gap-detection` is **on-demand only**: local candidate/evidence notes, no scheduler, no external writes.

See [docs/skills.md](../docs/skills.md) for agent discovery and install mappings (no user-home absolute paths).
