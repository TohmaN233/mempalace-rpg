# GM Memory Rules Template

Add this to your GM/system rules after you have filled `memo_setting.json`.

## External RPG Memory Contract

External RPG memory is a long-term memory layer. It does not automatically replace this game's state engine. For every domain, follow `memo_setting.json`:

- If a domain is host-owned, current values and mechanical outcomes must come from the host state/tools.
- If a domain is enabled in RPG memory, the GM/engine may commit and recall it through `mcp_rpg_*` tools.
- If a domain is `narrative_only`, store and recall evidence, witnesses, promises, origins, rumors, and subjective beliefs, but do not treat memory as the current numeric/state source.
- If memory and host state conflict, the host-owned truth source wins for current values. Memory remains evidence about what was seen, said, believed, or recorded.

## Recall Discipline

When long-term NPCs, locations, factions, old campaign history, promises, secrets, rumors, or past consequences may affect the current scene, call:

```text
mcp_rpg_recall({ actor_id, actor_type, query, location_id?, active_quest_ids? })
```

When portraying an NPC or faction, `actor_id` must be that actor's stable entity id. The actor may only use information present in the returned `MemoryPack`. Do not give NPCs knowledge just because the GM or player knows it.

If the player asks for exact old wording, specific witnesses, or whether a promise/secret really happened, use deep recall if available:

```text
mcp_rpg_deep_recall(...)
mcp_rpg_get_scene(...)
```

## Commit Discipline

Major durable changes should be committed with structured visibility and witnesses:

```text
mcp_rpg_commit_scene({
  scene_id?, campaign_id, in_world_time, location_id?, participants, witnesses,
  transcript,
  events: [{ event_type, summary, truth_status, visibility, witness_set, related_entities, ... }]
})
```

Use explicit visibility:

- `public_world`: broadly known world fact.
- `party_only`: known to the current party.
- `witnessed_only`: known to witnesses/participants.
- `character_private`: private to a character.
- `faction_private`: private to a faction.
- `gm_only`: GM planning/hidden truth only.
- `rumor_public`: public rumor, not necessarily true.

## Host-Specific Section

Replace this paragraph for your game:

```text
In <GAME NAME>, the following domains are host-owned and must not be overwritten by RPG memory: <list domains and tools>. RPG memory is enabled for: <list enabled domains>. Narrative-only domains: <list narrative_only domains>.
```
