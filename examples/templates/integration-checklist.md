# RPG Memory Integration Checklist

Before enabling `mempalace-rpg`, decide which system owns each truth source. The memory kernel can store narrative evidence and mechanical state, but only one component should be authoritative for current values.

## 1. Inventory existing game systems

Fill this table for your host game:

| Domain | Host already owns it? | Host state path/tool | RPG memory setting |
|---|---|---|---|
| World time/location | `<yes/no>` | `<path/tool>` | `<true/narrative_only/false>` |
| Level/XP/attributes | `<yes/no>` | `<path/tool>` | `<true/false>` |
| HP/MP/SP/status effects | `<yes/no>` | `<path/tool>` | `<true/false>` |
| Inventory/equipment/skills | `<yes/no>` | `<path/tool>` | `<true/narrative_only/false>` |
| Money/economy | `<yes/no>` | `<path/tool>` | `<true/false>` |
| Combat runtime | `<yes/no>` | `<path/tool>` | `<true/false>` |
| Quest lifecycle | `<yes/no>` | `<path/tool>` | `<true/narrative_only/false>` |
| Relationship numbers | `<yes/no>` | `<path/tool>` | `<true/false>` |
| NPC profiles/beliefs | `<yes/no>` | `<path/tool>` | Usually `true` |
| Past plot/world facts | `<yes/no>` | `<path/tool>` | Usually `true` |

## 2. Choose mode per domain

- `true`: RPG memory may write and recall this domain.
- `narrative_only`: RPG memory may store evidence, witnesses, rumors, origin stories, and commitments, but not current numeric/state values.
- `false`: RPG memory ignores this domain.

## 3. Add `memo_setting.json`

Start from `examples/templates/memo_setting.template.json` and replace placeholders.

## 4. Wire MCP

Use `pi-mcp-extension` or another MCP client. For pi, add `.pi/mcp.json` and a launcher script like `examples/dest-poet/run-rpg-mcp.sh`.

## 5. Add GM rules

Copy `examples/templates/gm-memory-rules.template.md` into your GM prompt and fill the host-specific section.

## 6. Test

- `mcp_rpg_status` returns counts and paths.
- GM recall can see world/canon facts.
- An NPC actor can only see memories returned in its `MemoryPack`.
- The player cannot see private old-history facts unless a scene exposed them.
- Host-owned state is never overwritten by memory recall.
