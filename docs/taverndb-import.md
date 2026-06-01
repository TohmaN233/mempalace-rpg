# 酒馆数据库导入

`mempalace-rpg import-taverndb` 当前导入的是特定 ChatSheets 风格的酒馆数据库 JSON。命令名沿用 TavernDB，但它不是所有 SillyTavern/酒馆数据结构的通用导入器。

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace \
  import-taverndb "TavernDB_data_... imported.json"
```

Use `--dry-run` first to inspect counts.

## 当前支持的表结构

当前导入器是按“命定之诗与黄昏之歌 v4.2”的 ChatSheets 表设计的，支持以下表名/列名：

| 表 | 期望字段 | RPG memory 结果 |
|---|---|---|
| `主角信息表` | 人物名称、性别/年龄、外貌特征、职业/身份、过往经历、性格特点 | Former protagonist becomes a legacy character, not the current `player` |
| `重要角色表` | 姓名、性别/年龄/等级、重要日期、角色间关系、关键记忆、目前经历 | Character profiles and private/witnessed profile memories |
| `角色扮演指南` | 角色姓名、语言特征、动态对话示例、互动态度字典 | Speech style/persona hints in character profiles |
| `纪要表` | 编码索引、时间跨度、地点、纪要、概览、参与人员 | Past-plot scenes (`legacy_past_plot`) with participants/witnesses |
| `约定表` | 档案ID、关联编码、约定主题、约定双方、详细内容、重要度、状态、信息来源 | Legacy promises, if you choose to keep them |
| `伏笔表` | 档案ID、关联编码、伏笔主题、详细内容、重要度、状态、信息来源 | GM-only foreshadowing, if you choose to keep it |

If another Tavern/SillyTavern export uses different sheet names, columns, or nested structure, write a new importer or normalize the JSON first.

## New protagonist safety

The importer is designed for “same world, new protagonist” campaigns:

- The old protagonist is stored as a legacy character.
- Timeline scenes are witnessed by their listed participants.
- The current `player` does not automatically know private old history.
- GM can recall canon history; NPCs can only recall what ACL allows.

## Cleaning unwanted imported rows

If you do not want certain sheets, import with a filtered source JSON or delete rows afterwards from both SQLite and Palace. For large migrations, prefer removing rows from the TavernDB export before import so the vector archive and SQLite stay aligned.
