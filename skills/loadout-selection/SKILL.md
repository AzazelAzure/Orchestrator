---
name: loadout-selection
description: "Select a department×position loadout from the twelve catalog seats and preview resolution pins. Use when assigning work or minting a resolved grant. Provider identity is never authority."
---

# Loadout Selection

## Procedure

1. Identify department and position required by the packet.
2. Map to catalog loadout ID `loadout.<dept>.<position>`.
3. Preview resolution: deny-wins, intersect capabilities, most-restrictive bounds.
4. Confirm member skill/lane/script hashes; fail closed on stale/missing hashes.
5. Never treat provider, actor-binding, or seat ID as merge/waiver/retry authority.
