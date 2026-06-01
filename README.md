# MemPalace RPG

[中文 README](README.zh-CN.md)

RPG-focused long-term memory kernel and MCP server adapted from [MemPalace](https://github.com/MemPalace/mempalace).

This project is a focused extraction/fork of the RPG memory work built on top of MemPalace. Thanks to the upstream MemPalace author and contributors for the palace/drawer storage idea, Chroma backend integration, and the broader project-memory foundation.

## What this is

`mempalace-rpg` gives an AI game master a reusable RPG memory service:

- scene-rooted evidence storage
- ACL-first actor recall
- NPC subjective beliefs separate from world truth
- character profiles and recall budgets
- optional MemPalace/Chroma transcript archive (`--palace`)
- MCP tools for pi or other agent hosts
- SillyTavern/TavernDB legacy campaign import
- conflict controls so host games decide which domains memory may own

It is not tied to one game package. A game can use it as an external service through MCP, CLI, or Python.

## What changed from upstream MemPalace

Upstream MemPalace is a general searchable memory palace. This fork adds RPG semantics:

1. **RPG schema**: `scene_record`, `scene_event`, `memory_item`, `world_fact`, `actor_belief`, `character_profile`, `entity_registry`, and relationship/importance support.
2. **ACL-first recall**: an actor can only receive memories allowed by visibility and witness rules before ranking happens.
3. **WorldTruth vs ActorBelief**: rumors and subjective beliefs do not automatically become canonical facts.
4. **Host conflict policy**: `memo_setting.json` lets a game enable, disable, or mark domains as `narrative_only`.
5. **MCP server**: `mempalace-rpg-mcp` exposes RPG tools: recall, commit scene, deep recall, get scene, status, profile upsert, TavernDB import.
6. **Legacy migration**: TavernDB ChatSheets can be imported as old campaign history. Former protagonists become legacy characters, not the new player.
7. **Optional palace archive**: SQLite remains the authoritative RPG index; MemPalace/Chroma can additionally store raw transcripts for semantic/exact-source retrieval.

## Important design principle: memory may store mechanics, but must not fight the host

`mempalace-rpg` can store many domains, including mechanical events or snapshots, if the host enables them. It is not limited to narrative-only memory.

The rule is:

> First decide which game system owns each truth source. Then configure RPG memory so it only owns or recalls the domains that will not conflict.

Examples:

- A lightweight game without an engine may enable mechanical events in RPG memory.
- A full RPG package with HP, inventory, combat, quests, and economy tools should keep those current values host-owned and set memory to `false` or `narrative_only` for those domains.
- Even when a domain is host-owned, memory can still store narrative evidence about it: who saw a battle, why an item matters, what promise was made, who believes a rumor.

See `examples/templates/integration-checklist.md` and `examples/templates/memo_setting.template.json`.

## Memory architecture

Every durable memory starts from a scene.

```text
scene_record            full scene transcript, time, place, participants, witnesses
  └─ scene_event        typed facts/events with truth_status + visibility
       ├─ memory_item   recallable evidence snippets
       ├─ world_fact    canonical/observed world truth
       └─ actor_belief  subjective actor knowledge, rumors, mistakes
```

Actors retrieve a `MemoryPack`:

1. Apply ACL/visibility/witness filtering.
2. Rank allowed memories by relevance, recency, importance, actor tier, and query.
3. Render sections: profile, world truth, actor beliefs, evidence, and guardrails.

Evidence lines include time anchors from their source scene:

```text
- [character:mem_xxx | 488-01-01 07:12 | loc_ash_bridge | scene:scene_time_anchor | stored:2026-05-31T13:20:00+00:00] An old promise was made.
```

The in-world time and location help the GM/NPC avoid treating every recalled event as recent. `stored:` is system write time for debugging and rollback, not story time.

This means an NPC does not become omniscient just because the database contains GM-only or other-character memories.

## Install

```bash
pip install -e /path/to/mempalace-rpg
```

For pi integration:

```bash
pi install npm:pi-mcp-extension
```

## Storage locations

SQLite is the authoritative RPG index. It stores scenes, events, recall items, facts, beliefs, profiles, ACL metadata, and settings-derived behavior.

Default DB path:

```text
~/.mempalace/rpg_memory.sqlite3
```

Typical game package paths:

```text
state/rpg-memory.sqlite3   # structured RPG memory + ACL
state/rpg-palace/          # optional MemPalace/Chroma raw transcript archive
memo_setting.json          # domain conflict policy
```

If `--palace` is omitted, only SQLite is written. If `--palace` is set, full scene transcripts are also written as MemPalace drawers.

## CLI quick start

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 init
mempalace-rpg --db state/rpg-memory.sqlite3 status
```

Commit a scene:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 --memo-setting memo_setting.json commit-scene scene.json
```

Recall for an actor:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 --memo-setting memo_setting.json recall \
  --actor-id char_liora \
  --actor-type npc \
  --query "What promises does Liora know about?"
```

Enable raw transcript archive:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 --palace state/rpg-palace commit-scene scene.json
```

## Backup, restore, and time-based rollback

Simple maintenance commands are included:

```bash
# Backup SQLite and optional Palace directory
mempalace-rpg --db state/rpg-memory.sqlite3 --palace state/rpg-palace backup

# Restore from backup; creates a safety backup first by default
mempalace-rpg --db state/rpg-memory.sqlite3 --palace state/rpg-palace restore \
  --db-backup state/backups/rpg-memory.sqlite3.bak-20260530T203000Z \
  --palace-backup state/backups/rpg-palace.bak-20260530T203000Z

# Preview rollback to a system timestamp
mempalace-rpg --db state/rpg-memory.sqlite3 --palace state/rpg-palace \
  delete-after "2026-05-30T20:29:55+00:00" --dry-run

# Delete everything created after that timestamp
mempalace-rpg --db state/rpg-memory.sqlite3 --palace state/rpg-palace \
  delete-after "2026-05-30T20:29:55+00:00"
```

`delete-after` uses system write time (`created_at`), not in-world time. It deletes matching scenes, events, memory items, facts, beliefs, relationship rows evidenced by those events, and corresponding Palace drawers. It automatically creates a backup first unless `--no-backup` is set.

## pi tree reroll support

pi sessions are trees. A player can use `/tree` to jump back and reroll a branch. Since RPG memory is an external side-effect store, host extensions should record a branch ledger whenever they auto-commit a scene.

Recommended flow:

1. Auto-commit a visible in-world turn.
2. Append a pi custom ledger entry with `scene_id`, `campaign_id`, and `branch_scope_id`.
3. On `session_tree`, read ledger entries on the current active branch.
4. Call:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 --palace state/rpg-palace \
  sync-branch keep-scenes.json \
  --campaign-id fated-poem-dusk-song \
  --branch-scope-id <pi-session-id>
```

`sync-branch` deletes auto-commit scenes in that campaign/scope that are not present in the current branch ledger, from both SQLite and Palace. Legacy imports and manual worldbuilding entries are not removed unless the host explicitly marks and ledgers them.

## MCP tools

Run:

```bash
mempalace-rpg-mcp --db state/rpg-memory.sqlite3 --memo-setting memo_setting.json --palace state/rpg-palace
```

Exposed tools include:

| Tool | Purpose |
|---|---|
| `mempalace_rpg_status` / `mcp_rpg_status` | DB counts and config |
| `mempalace_rpg_recall` / `mcp_rpg_recall` | ACL-filtered actor memory pack |
| `mempalace_rpg_commit_scene` / `mcp_rpg_commit_scene` | Write scene + structured events |
| `mempalace_rpg_deep_recall` / `mcp_rpg_deep_recall` | Retrieve ACL-filtered scene snippets |
| `mempalace_rpg_get_scene` / `mcp_rpg_get_scene` | Inspect one scene with ACL checks |
| `mempalace_rpg_upsert_profile` / `mcp_rpg_upsert_profile` | Create/update actor profile |
| `mempalace_rpg_import_taverndb` / `mcp_rpg_import_taverndb` | One-time legacy TavernDB import |

## Integration workflow for a game

1. Inventory your game systems with `examples/templates/integration-checklist.md`.
2. Fill `memo_setting.json` from `examples/templates/memo_setting.template.json`.
3. Add MCP config and a launcher script.
4. Add the GM memory rules from `examples/templates/gm-memory-rules.template.md`.
5. Test `mcp_rpg_status`, `mcp_rpg_recall`, and one `mcp_rpg_commit_scene`.
6. Optionally add host-side automatic transcript commits.

## Case study: 命定之诗与黄昏之歌

The `examples/dest-poet/` directory shows the concrete setup used for 命定之诗.

That game already owns many mechanical systems:

- level, XP, attributes
- HP/MP/SP and status effects
- inventory, equipment, skills
- money and fate points
- combat runtime
- quest lifecycle and rewards
- affection/contracts
- DLC, fate core, news columns

Therefore its `memo_setting.json` disables mechanical event types and sets some domains to `narrative_only`. RPG memory remains enabled for:

- past campaign canon
- NPC profiles and subjective memories
- locations and factions
- secrets, rumors, witnesses, commitments
- old TavernDB history
- raw scene transcript archive through Palace

In other words, 命定之诗 uses RPG memory as long-term evidence and actor cognition, while its engine remains authoritative for current mechanics.

### pi config

`examples/dest-poet/mcp.json`:

```json
{
  "settings": { "toolPrefix": "mcp" },
  "mcpServers": {
    "rpg": {
      "transport": "stdio",
      "command": "bash",
      "args": ["scripts/run-rpg-mcp.sh"],
      "lifecycle": "eager"
    }
  }
}
```

`examples/dest-poet/run-rpg-mcp.sh` starts:

```bash
mempalace-rpg-mcp \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace
```

### GM rule snippet for 命定之诗

See `examples/dest-poet/gm-memory-rules.md`. The essential paragraph is:

```text
命定之诗已经拥有自己的 state/tools，以下当前值与机械结果永远以本包为准：等级、经验、属性点、HP/MP/SP、状态效果、死亡状态、金钱、命运点数、背包、装备、技能、战斗回合、战斗 HP、任务生命周期、好感度数值、契约状态、DLC、命定核心、新闻栏目。

外部 RPG 记忆在本包中启用为长期叙事记忆层：保存旧战役历史、NPC 档案、NPC 主观记忆、世界事实证据、地点/阵营叙事变化、秘密可见性、见证者、承诺、传闻、信物来历。quest 与 item 只作为 narrative_only 使用。
```

### TavernDB migration

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace \
  import-taverndb "TavernDB_data_命定之诗与黄昏之歌v4.2 ... imported.json"
```

The old protagonist is imported as a legacy character. The new current player does not automatically know private old history.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check .
```

## License and attribution

MIT, following upstream MemPalace licensing. This repository is adapted from MemPalace.
