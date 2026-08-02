# Skills discovery and install

Canonical skill packages live in this repository under `skills/`.

The default `core` bundle contains five workflow-floor skills. The `extended`
bundle ships with the repository but is opt-in and contains investigation,
exploration, security, publication documentation, CI triage, and code-review
risk triage. The `positional` bundle is opt-in and contains the seventeen R3
department/position skills referenced by the twelve loadouts. Bundle
declarations live under `skills/bundles/` and reference package IDs rather than
copying package bodies.

## Layout

```
skills/
  <skill-name>/
    SKILL.md
    agents/openai.yaml
    manifest.json
```

## Agent surface mappings (relative; no user-home absolutes)

Operators choose where an agent reads skills. Examples of relative mappings from
a checkout of this repository:

| Surface | Suggested mapping |
|---------|-------------------|
| Cursor | Point the agent skills root at `./skills` (or copy managed packages into the surface's skills root) |
| Claude | Copy or sync `./skills/<name>` into the Claude skills root used for this project |
| Codex | Copy or sync `./skills/<name>` into the Codex skills root used for this project |

Do not hard-code machine-specific absolute paths in tracked docs or configs.

## Seed constraints

- All seed skills are **on-demand**.
- `skill-gap-detection` writes only local candidate/evidence notes when used; it
  does not schedule jobs and does not perform external writes.
- Manifests keep `scheduling_ref: null` and `product_coupling: none`.

## Validation

```bash
pytest tests/unit/test_publication_candidate.py
```

## Deep documentation

- [Developer guide](guides/developer-guide.md) — extension points and test taxonomy
- [Surface reference](reference/surfaces.md) — MCP tools and R4 lane catalog
- [`read-the-docs.md`](read-the-docs.md) — agent routing cheat sheet
