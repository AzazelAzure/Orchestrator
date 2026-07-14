# Skills discovery and install

Canonical skill packages live in this repository under `skills/`.

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
pytest tests/unit/test_skill_packages.py
```
