# TavernDB import (legacy kernel)

The `import-taverndb` command imports the ChatSheets-style TavernDB JSON shapes listed below into the legacy `RpgMemoryKernel`. It is not a general importer for every SillyTavern or Tavern export. Start with a dry run:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 import-taverndb export.json --dry-run
```

The parser recognizes the following sheet names and columns:

| Sheet | Expected fields | Legacy memory result |
| --- | --- | --- |
| `主角信息表` | 人物名称、性别/年龄、外貌特征、职业/身份、过往经历、性格特点 | The former protagonist is imported as a legacy character, not as the current player. |
| `重要角色表` | 姓名、性别/年龄/等级、重要日期、角色间关系、关键记忆、目前经历 | Character profiles and sourced profile memories. |
| `角色扮演指南` | 角色姓名、语言特征、动态对话示例、互动态度字典 | Persona and speech-style hints for the legacy profile. |
| `纪要表` | 编码索引、时间跨度、地点、纪要、概览、参与人员 | Past-campaign scenes with listed participants and witnesses. |
| `约定表` | 档案 ID、关联编码、主题、双方、内容、重要度、状态、来源 | Legacy promises when retained by the import. |
| `伏笔表` | 档案 ID、关联编码、主题、内容、重要度、状态、来源 | GM-only foreshadowing when retained by the import. |

Exports with different sheet names, fields, or nested structure need a separate mapping or normalization step before import. `--dry-run` parses and counts without writing. Imported rows belong to the legacy kernel; the play-memory v1 batch contract is documented separately in the [protocol reference](reference/PLAY_MEMORY_V1.zh-CN.md).
