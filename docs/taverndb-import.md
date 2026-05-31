# SillyTavern / TavernDB Import

`mempalace-rpg import-taverndb` imports ChatSheets-style TavernDB JSON exports as legacy campaign memory.

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace \
  import-taverndb "TavernDB_data_... imported.json"
```

Use `--dry-run` first to inspect counts.

## Mapping

| TavernDB sheet | RPG memory result |
|---|---|
| `主角信息表` | Former protagonist becomes a legacy character, not the current `player` |
| `重要角色表` | Character profiles and private/witnessed profile memories |
| `角色扮演指南` | Speech style/persona hints in character profiles |
| `纪要表` | Past-plot scenes (`legacy_past_plot`) with participants/witnesses |
| `约定表` | Legacy promises, if you choose to keep them |
| `伏笔表` | GM-only foreshadowing, if you choose to keep it |

## New protagonist safety

The importer is designed for “same world, new protagonist” campaigns:

- The old protagonist is stored as a legacy character.
- Timeline scenes are witnessed by their listed participants.
- The current `player` does not automatically know private old history.
- GM can recall canon history; NPCs can only recall what ACL allows.

## Cleaning unwanted imported rows

If you do not want certain sheets, import with a filtered source JSON or delete rows afterwards from both SQLite and Palace. For large migrations, prefer removing rows from the TavernDB export before import so the vector archive and SQLite stay aligned.
