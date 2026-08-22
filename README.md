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

1. Require a `campaign_id`, then apply the AERP-1 authorization decision to source events.
2. Rank allowed memories by relevance, recency, importance, actor tier, and query.
3. Render sections: profile, world truth, actor beliefs, evidence, and guardrails.

Evidence lines include time anchors from their source scene:

```text
- [character:mem_xxx | 488-01-01 07:12 | loc_ash_bridge | scene:scene_time_anchor | stored:2026-05-31T13:20:00+00:00] An old promise was made.
```

The in-world time and location help the GM/NPC avoid treating every recalled event as recent. `stored:` is system write time for debugging and rollback, not story time.

This means an NPC does not become omniscient just because the database contains GM-only or other-character memories.

### Authorized evidence boundary (AERP-1)

`RpgMemoryKernel.authorized_evidence(...)` is the single policy boundary used by
ordinary recall, deep recall, and scene transcript access. It constrains candidates
to the requested campaign before ranking, deduplicates by `source_event_id`, and
returns a complete policy trace. Retconned/abandoned material is never returned;
beliefs are visible only to their explicit `belief_owner_id`; and private faction,
quest, or party evidence needs an explicit campaign-scoped membership.

Scene access returns only authorized `source_span` evidence, never a full transcript
merely because an actor participated in a mixed-visibility scene. Callers must pass
`--campaign-id` to `recall`, `deep-recall`, and `get-scene`.

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
  --campaign-id campaign_current \
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

### AERP-1 offline authorization audit

Every scene event must name a non-empty `branch_id` and a `branch_status` of
`active`, `retconned`, or `abandoned`. Canonical, observed, reported, rumor,
uncertain, and belief events use `active`; retired truth uses its matching
retired branch status. Legacy rows with NULL branch data deliberately fail
closed. TavernDB imports use the deterministic `legacy:<campaign_id>` branch.
Before any entity creation, drawer call, or SQLite write, `campaign_id`, event
type, summary, branch ID, and exact unique `source_span` must be non-empty.
ACL metadata must have the one shape allowed by visibility; beliefs require the
same non-empty actor and belief owner.

Run the frozen 24-case / 48-call audit without any network or model calls:

```bash
python tests/run_aerp1_audit.py --output /outside/repo/aerp1-audit.json
```

The JSON includes a manifest SHA-256, full product policy telemetry, selected
event IDs, and delivered authorized spans. Policy traces are trusted audit
telemetry: denied candidate IDs may appear there, but never in selected evidence,
rendered product text, or returned spans. Keep generated reports outside the
repository.

### AERP-2 historical six-view replay and ownership ablation

This is a byte-pinned historical LoCoMo retrieval replay, not an RPG kernel
benchmark or a product-ranking claim. It freezes the historical dataset, model,
selection decision, scorer, source blobs, and ranking streams; raw BM25 and the
six-view stream must reproduce rank-for-rank before the two raw-dense arms are
reported.

```powershell
python -m benchmarks.aerp2_historical_export `
  --artifact E:\MemPalaceWorkspace\artifacts\benchmark-runs\locomo_story_dense_v2_full_run1.json `
  --dataset E:\MemPalaceWorkspace\data\benchmark-data\locomo\main\locomo10.json `
  --model-dir E:\MemPalaceWorkspace\artifacts\benchmark-runs\models\bge-small-en-v1.5\onnx `
  --source-repo E:\MemPalaceWorkspace\repos\mempalace `
  --selection-freeze E:\MemPalaceWorkspace\artifacts\benchmark-runs\locomo_story_dense_v2_selection_freeze.json `
  --manifest tests\fixtures\aerp2_six_view_replay_manifest.json `
  --output C:\outside-repo\aerp2-six-view-export.json
```

The completed 1,986-question / 603-hard-question, official-exact Recall@10
result is: raw BM25 0.563496 overall / 0.449526 hard; raw dense 0.510819 /
0.468811; raw BM25 + raw dense RRF 0.624343 / 0.525106; full six-view
0.722748 / 0.680230. The full six-view method uses historical observation and
session-summary annotations. Therefore this is annotation-assisted retrieval
evidence only: it does not establish annotation-free productization, graph or
clustering value, kernel ranking quality, or an improvement claim for the RPG
product.

### AERP-2 annotation-free product ranker

The product implementation keeps RPG/AERP responsible for authorization,
isolation, provenance, transactionality, and budgets, then applies retrieval
only inside the authorized event universe. `RpgMemoryKernel` accepts an optional
`AuthorizedEventRanker`; omitting it preserves the AERP-1 behavior. The bundled
`SixViewRanker` uses six label-free views with the historical frozen weights:

| View | Weight |
|---|---:|
| Raw event BM25 | 2.0 |
| Structured event observation BM25 | 0.5 |
| Raw event dense | 1.0 |
| Structured event observation dense | 2.0 |
| Policy-homogeneous checkpoint roll-up dense | 2.0 |
| Raw + observation dense | 1.0 |

The views are fused with weighted RRF at `k=60`. A checkpoint is
`payload.retrieval_checkpoint_id` when supplied, otherwise the source scene.
Roll-ups are chronological and never cross ACL-policy boundaries. The dense
encoder is injected and must expose distinct query and passage methods plus a
stable identity; importing the retrieval module does not start ONNX or Chroma.
The ranking trace contains hashes, ranks, finite scores/contributions, and only
packed evidence IDs, never transcript text.

For rebuild-stable rank ties and ranking digests, set a unique, non-empty
`payload.retrieval_ranking_key` on each source event (for example, LoCoMo's
opaque dialog ID). If omitted, the kernel falls back to `source_event_id` for
ordinary compatibility; generated UUIDs therefore cannot provide stable
cross-rebuild ordering.

The release claim is intentionally gated by a frozen annotation-free LoCoMo
run. Product Six-View must have zero unauthorized output, complete trace
coverage, beat the strongest raw-only control by at least 5 percentage points
on both overall and hard-category Recall@10, and have a paired
conversation-bootstrap confidence-interval lower bound above zero. Historical
annotation-assisted parity is a separate P2 target: no worse than 1 percentage
point below the frozen historical Six-View result. Failure of either quality
gate is reported as a measured gap; it does not authorize graph or clustering
changes by itself.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check .
```

## License and attribution

MIT, following upstream MemPalace licensing. This repository is adapted from MemPalace.
