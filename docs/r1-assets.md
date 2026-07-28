# R1 assets and contracts

R1 adds **inert**, versioned catalogs under `agentic/catalogs/`. These are
portable design contracts for assets, MCP lanes, loadouts, registered scripts,
and governance precedence. They are discoverable through
`agentic/build_manifest.py` but are **not** runtime activation.

## What is included

| Artifact | Path | Count / role |
|----------|------|----------------|
| Portable asset index | `agentic/catalogs/assets.json` | Existing + planned skill IDs |
| MCP lane profiles | `agentic/catalogs/mcp_lanes.json` | 5 lanes |
| Seat loadouts | `agentic/catalogs/loadouts.json` | 12 department×position |
| Registered scripts | `agentic/catalogs/scripts.json` | 12 generic scripts |
| Policy contract | `agentic/catalogs/policy.json` | Precedence, deny-wins, pins |
| JSON Schemas | `agentic/catalogs/schemas/` | Structural contracts |

## Explicit non-claims

- No Django, Celery, Redis, Docker, schedules, or provider invocation.
- No installation-policy activation and no executable Headquarters project contract.
- Repository scripts are catalog-only (`executable: false`).
- Native loadout enforcement remains an R3 concern (`G-ORCH-S4-RESUME`).
- Presence in the agentic manifest is discovery only, not authority.

## Governance encodings (inert)

The policy catalog records:

- resolution precedence (engine safety floor → … → explicit task grant);
- deny-wins and fail-closed hash rules;
- immutable dispatch-pin fields;
- mandatory anomaly reporting;
- independent-review separation (distinct provider principal, seat, invocation, attempt);
- retention of the five-tool read-only stdio MCP surface, also bound into
  `context-assets`.

## Regeneration and validation

```bash
python agentic/generate_catalogs.py
python agentic/validate_catalogs.py
python agentic/build_manifest.py
pytest tests/unit/test_r1_catalogs.py -q
```

Generation is deterministic: `content_sha256` covers canonical JSON with
volatile timestamp fields omitted from the hash input.
