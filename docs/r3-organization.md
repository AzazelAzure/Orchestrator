# R3 organization, delegation, and resolved loadouts

R3 adds hierarchy, scoped delegation, positional loadout resolution, and
immutable dispatch pins on top of the R2 governed runtime. It does **not**
activate Django/DRF, Redis/Celery, MCP lane containers, executable scripts,
schedules, real provider adapters, hosting, or publication (R4+).

## Concepts

| Concept | Role |
|--------|------|
| Organization profile | Installation-local org tree (departments, layers, positions) |
| Position | Department × hierarchy seat bound to a catalog loadout |
| Assignment | Actor + provider seat bound to a position for a work item |
| Delegation request | Scoped downward request with packet-only payload |
| Resolved-loadout snapshot | Immutable resolution of skills/lanes/scripts + authority merge |
| Task grant | R3 grant referencing a pinned snapshot |
| Dispatch pin | Immutable pin of policy/org/loadout/member/packet/budget/grant/attempt/invocation |

## Precedence

Resolve:

`engine safety floor → handbook → installation policy → product base → department → hierarchy layer → position → project/repo extension → task class → explicit task grant`

Rules: deny wins; mandatory controls union; capabilities/effects intersect;
numeric/path/network/secret bounds take the most restrictive value;
missing/stale/conflicting hashes fail closed.

## Chain of command

- No upward authority or silent authority broadening
- Provider identity never grants authority
- No self-review; independent review uses distinct provider, seat, invocation, attempt
- Packet-only handoffs (no ambient conversation context)
- Parent assignment closure blocked until child evidence/review is accepted
- HitM exceptions remain on the existing founder step-up path only

## R2 compatibility

`SystemTestGrant` remains the explicitly marked `compatibility_mode: r2_system_test`
path. It refuses organization/loadout payload fields and cannot authorize
`org.*` / `delegation.*` commands. Consequential R3 dispatch uses
`ResolvedTaskGrant` and fails closed without pins.

## CLI

```bash
flowctl org create-profile|show-profile|list-profiles|add-actor|add-seat|members
flowctl org find-position|loadout-preview|snapshot|show-snapshot|assign|complete-assignment
flowctl delegation request|accept|decline|reroute|dispatch|handoff|accept-handoff
flowctl delegation mint-grant|show-request|show-pin
```

Org/delegation CLI commands enter the sole-writer coordinator. Founder,
executive, manager, or system roles may bootstrap organization commands without
a resolved grant (profiles must exist before R3 grants can be minted).

## Catalogs

The twelve department×position loadouts and seventeen positional skills remain
discoverable via `agentic/catalogs/` and `skills/bundles/positional.json`.
Catalog JSON stays an inert design contract for schema/hash discovery; runtime
enforcement is the R3 services above. Scripts and MCP lane containers stay
non-executable / inactive.

## Deep documentation

- [Domain and lifecycle](guides/domain-and-lifecycle.md) — dispatch pins and gates
- [Auth and security](guides/auth-and-security.md) — delegation command matrix
- [Surface reference](reference/surfaces.md) — `flowctl org` / `delegation` commands
