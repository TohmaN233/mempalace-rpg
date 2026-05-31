# RPG Memory Design

`mempalace-rpg` is a host-agnostic RPG memory layer. It can store narrative memory, subjective NPC knowledge, world facts, and—when a host chooses to enable it—mechanical state snapshots or history. The central rule is not “never store mechanics”; it is “do not compete with the host game's authoritative truth source.”

## Core model

### `scene_record`

A scene is the root of evidence. It stores campaign id, in-world time, location, participants, witnesses, and the full transcript. If `--palace` is enabled, the transcript is also archived as a MemPalace/Chroma drawer for future semantic search and exact-source inspection.

### `scene_event`

A scene can contain one or more typed events: promise, betrayal, secret reveal, faction change, item handoff, combat outcome, mechanical state snapshot, etc. Each event has:

- `truth_status`: canonical, observed, reported, rumor, belief, uncertain, retconned.
- `visibility`: public_world, party_only, witnessed_only, character_private, faction_private, quest_participants, gm_only, rumor_public.
- `witness_set`: who actually knows it.
- related entities, quests, and locations.

### `memory_item`

A projected recall unit derived from events. Recall ranks and filters memory items after ACL checks. This is what usually appears in a `MemoryPack` evidence section.

### `world_fact`

Canonical or observed world truth. GM recall can use it; actors only receive it when visibility/ACL allows.

### `actor_belief`

Subjective knowledge. Rumors, misunderstandings, and private conclusions live here rather than being promoted into world truth.

### `character_profile`

Stable NPC/actor profile: tier, persona, speech style, faction/home, goals, fears. Tier controls recall budget.

### `entity_registry`

Stable ids for players, NPCs, factions, locations, quests, items, events, and concepts.

## ACL-first recall

Recall starts by filtering what the actor is allowed to know. Ranking happens after filtering. This prevents the GM’s hidden truth or another NPC’s private belief from leaking into a character prompt.

Actors should only act on the returned `MemoryPack`. If the host gives an NPC omniscient context elsewhere, that host prompt can still break isolation; the GM rules must require `mcp_rpg_recall` for long-term NPCs.

## Domain conflict controls

`memo_setting.json` controls whether domains and event types are writable/recallable:

- `true`: enabled.
- `narrative_only`: can store evidence/history/origins/rumors, not current numeric state.
- `false`: disabled.

Typical examples:

| Domain | Enable when | Disable or narrative-only when |
|---|---|---|
| canon | You want persistent world history | Rarely disabled |
| character | Long-lived NPCs matter | One-shot NPCs only |
| location | Places evolve over time | Pure procedural scenes |
| faction | Faction knowledge/politics matter | No faction play |
| quest | No host quest engine, or narrative quest memory | Host owns quest lifecycle/rewards |
| item | Items have provenance or secrets | Host owns inventory counts/equipment |
| mechanics | Host has no state engine and memory should mirror mechanics | Host already has level/HP/combat/economy tools |

## Mechanical state is optional, not forbidden

RPG memory can record mechanical events if the host enables them: dice outcomes, battle outcomes, level changes, resource changes, inventory history, or state snapshots. This is useful for lightweight games without a separate engine.

For games with a real state engine, set those event types to `false` or use `narrative_only` so memory does not invent or override current values.
