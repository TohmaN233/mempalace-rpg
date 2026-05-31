# MemPalace RPG

基于 [MemPalace](https://github.com/MemPalace/mempalace) 改造/抽取的 RPG 专用长期记忆内核与 MCP 服务。

本项目不是完整 MemPalace 的搬运，而是把 RPG 运行时需要的记忆层单独整理出来：场景证据、NPC 主观认知、世界事实、ACL 权限召回、旧酒馆/TavernDB 迁移、以及给 pi 等 Agent 宿主使用的 MCP 工具。

感谢上游 MemPalace 作者与贡献者提供的 Memory Palace / drawer 存储思想、Chroma 后端集成和通用记忆检索基础。

## 这是什么

`mempalace-rpg` 是给 AI 跑团/文字 RPG/交互小说使用的外部长期记忆服务。它可以作为独立工具被任何游戏包调用，而不是绑死在某一个游戏项目里。

它提供：

- 以场景为根的证据存储
- ACL-first 的角色召回
- NPC 主观记忆与世界真相分离
- 角色档案与 tier 召回预算
- 可选 MemPalace/Chroma 原文归档：`--palace`
- pi / MCP 工具接口
- SillyTavern / TavernDB 旧战役导入
- `memo_setting.json` 冲突控制，让宿主游戏决定哪些领域由记忆系统接管

## 与原 MemPalace 相比改了什么

原 MemPalace 是通用可搜索记忆宫殿。本项目在其基础上加入 RPG 语义层：

1. **RPG schema**：新增 `scene_record`、`scene_event`、`memory_item`、`world_fact`、`actor_belief`、`character_profile`、`entity_registry` 等表。
2. **ACL-first 召回**：先按可见性/见证者过滤，再排序。NPC 不会因为数据库里有 GM-only 信息而自动全知。
3. **世界真相 vs 角色信念**：传闻、误解、私人判断进入 `actor_belief`，不会自动变成世界事实。
4. **宿主冲突策略**：通过 `memo_setting.json` 把每个领域设为 `true` / `narrative_only` / `false`。
5. **MCP 服务**：`mempalace-rpg-mcp` 暴露召回、写入场景、深度召回、读取场景、角色档案、TavernDB 导入等工具。
6. **旧酒馆迁移**：可导入 TavernDB ChatSheets。旧主角会变成 legacy character，不会覆盖当前新主角。
7. **可选 Palace 原文库**：SQLite 负责 RPG 索引与 ACL；Palace/Chroma 可额外保存原文 transcript，用于未来语义搜索和无损查证。

## 重要原则：可以存机械，但不能和宿主抢真相源

`mempalace-rpg` 不是“只能存叙事、不能存数值”。它可以存很多领域，包括机械状态快照、骰子结果、战斗结局、资源变化、物品变化等。

真正的原则是：

> 先判断宿主游戏哪些系统已经拥有真相源，再决定 RPG memory 启用哪些域。

例如：

- 轻量文字游戏没有独立 engine，可以让 RPG memory 记录机械事件或状态快照。
- 完整 RPG 包已经有 HP、背包、战斗、任务、经济工具，就应该把这些当前值保持为宿主 state/tools 权威，记忆系统对应域设为 `false` 或 `narrative_only`。
- 即使某领域由宿主拥有，RPG memory 仍可存叙事证据：谁见证过某场战斗、某道具为什么重要、谁承诺过什么、谁相信某个传闻。

参考：

```text
examples/templates/integration-checklist.md
examples/templates/memo_setting.template.json
```

## 记忆系统具体保存什么

所有长期记忆都从 scene 开始。scene 是证据根，event 是结构化解释，memory/fact/belief 是召回投影。

```text
scene_record            场景原始证据
  保存：campaign_id、世界内时间、地点、参与者、见证者、完整 transcript、hash、排序
  用途：追溯“当时到底发生了什么”；启用 --palace 时也写入 Chroma 原文库

scene_event             场景中的结构化事件
  保存：事件类型、行为者、目标、摘要、真伪状态、可见性、见证者、关联实体/任务/地点、重要度、情绪权重、payload
  用途：把长文本变成可检索的 RPG 事实，例如承诺、秘密、背叛、传闻、战斗结果、状态变化

memory_item             可召回证据片段
  保存：从 event 投影出的短记忆、归属域、可见性、known_by、关联对象、重要度、vector_id
  用途：普通 recall 主要检索它，提供给 GM/NPC 的 MemoryPack

world_fact              世界事实
  保存：subject、predicate、object、confidence、有效期、来源 event
  用途：GM 或有权限角色可用的客观/观察事实

actor_belief            角色主观认知
  保存：actor、subject、predicate、object、belief_status、confidence、来源 event
  用途：NPC 的记忆、误解、传闻、私人判断；不会自动升级成世界真相

character_profile       角色档案
  保存：角色名、tier、公开/私有身份、短人设、语言风格、目标、恐惧、阵营、home_location、memory_wing
  用途：长期 NPC 召回前先加载基础人设，并用 tier 控制召回预算

entity_registry         稳定实体注册表
  保存：entity_id、类型、显示名、active、创建/更新时间
  用途：保证 NPC、地点、阵营、任务、物品等都有稳定 ID

entity_importance       实体重要度
  保存：tier、剧情权重、玩家交互次数、任务关联、情绪事件、秘密关联、近期提及、importance_score
  用途：后续可做 NPC/地点重要度提升、召回预算调整

relationship_state      关系状态，可选
  保存：trust、affection、fear、hostility、debt、公开标签、私有备注、证据 event
  用途：轻量游戏可用；若宿主已有好感度/关系工具，应关闭或不作为真相源
```

角色召回会生成 `MemoryPack`：

1. **先过滤权限**：按 `visibility`、`witness_set`、`known_by`、actor 身份判断能不能看。
2. **再排序**：只在允许看到的记忆里按 query 相关度、近因、重要度、角色 tier 排序。
3. **再渲染**：输出 profile、world truth、actor belief、evidence、guardrails 等 section。

因此，长期 NPC 只能使用 MemoryPack 里返回的信息，而不是全数据库信息。

## 可调用函数 / 工具一览

### Python API

| 函数/类 | 功能 |
|---|---|
| `RpgMemoryKernel(db_path, episode_adapter?, memo_settings_path?)` | 打开 RPG 记忆内核；SQLite 是主索引，adapter 可接 Palace 原文库 |
| `kernel.init_schema()` | 初始化数据库 schema |
| `kernel.status()` | 返回 DB 路径、memo_setting、各表计数 |
| `kernel.upsert_entity(entity_id, entity_type, display_name?)` | 注册或更新稳定实体 |
| `kernel.upsert_character_profile(...)` | 新建/更新长期 NPC/actor 档案 |
| `kernel.commit_scene(...)` | 写入 scene_record，并按 events 投影 memory_item/world_fact/actor_belief |
| `kernel.build_memory_pack(actor_id, actor_type, query, ...)` | ACL 过滤后为 GM/NPC/player 构建 MemoryPack |
| `kernel.get_scene(scene_id, actor_id, actor_type, mode, ...)` | 按权限读取单个 scene 的摘要/片段/全文 |
| `kernel.deep_recall(actor_id, actor_type, query, ...)` | 深度召回相关 scene 片段，用于查原话/见证者/细节 |
| `kernel.list_memory_items(...)` | 调试：列出 memory_item |
| `kernel.list_world_facts(...)` | 调试：列出 world_fact |
| `kernel.list_actor_beliefs(...)` | 调试：列出 actor_belief |
| `import_taverndb(kernel, path, ...)` | 导入特定格式的酒馆数据库 JSON，见下文限制 |

### CLI 命令

| 命令 | 功能 |
|---|---|
| `mempalace-rpg init` | 初始化 DB |
| `mempalace-rpg status` | 查看状态和表计数 |
| `mempalace-rpg upsert-profile <json>` | 从 JSON 写入角色档案 |
| `mempalace-rpg commit-scene <json>` | 从 JSON 写入场景与事件 |
| `mempalace-rpg recall --actor-id ... --query ...` | 输出 ACL 过滤后的 MemoryPack |
| `mempalace-rpg list memories/facts/beliefs` | 调试列出记忆/事实/信念 |
| `mempalace-rpg import-taverndb <json>` | 导入当前支持格式的酒馆数据库 JSON |

所有 CLI 都可带：

```bash
--db state/rpg-memory.sqlite3
--memo-setting memo_setting.json
--palace state/rpg-palace
```

### MCP 工具

| MCP 工具 | pi 短别名 | 功能 |
|---|---|---|
| `mempalace_rpg_status` | `mcp_rpg_status` | 查看服务状态、DB、表计数 |
| `mempalace_rpg_recall` | `mcp_rpg_recall` | 普通召回，返回 ACL 过滤后的 MemoryPack |
| `mempalace_rpg_commit_scene` | `mcp_rpg_commit_scene` | 写入一幕场景和结构化事件 |
| `mempalace_rpg_deep_recall` | `mcp_rpg_deep_recall` | 深挖相关场景片段，用于查旧细节 |
| `mempalace_rpg_get_scene` | `mcp_rpg_get_scene` | 按 scene_id 读取可见片段或全文 |
| `mempalace_rpg_upsert_profile` | `mcp_rpg_upsert_profile` | 新建/更新角色档案 |
| `mempalace_rpg_import_taverndb` | `mcp_rpg_import_taverndb` | 导入当前支持格式的酒馆数据库；大文件更推荐 CLI |
| `mempalace_rpg_list_memories` | `mcp_rpg_list_memories` | 调试列出 memory_item |
| `mempalace_rpg_list_world_facts` | `mcp_rpg_list_world_facts` | 调试列出 world_fact |
| `mempalace_rpg_list_actor_beliefs` | `mcp_rpg_list_actor_beliefs` | 调试列出 actor_belief |

## 写入一幕场景时发生什么

`commit_scene` 的逻辑：

1. 写入 `scene_record`：保存完整 transcript、参与者、见证者、地点、时间。
2. 如果启用 `--palace`，把 transcript 写入 MemPalace/Chroma drawer，返回 `vector_id`。
3. 逐个处理 `events`：检查 `memo_setting.json` 是否允许该事件类型。
4. 写入 `scene_event`。
5. 从 event 投影：
   - `memory_item`：用于普通召回的证据片段。
   - `world_fact`：canonical/observed/reported 等事实。
   - `actor_belief`：见证者、行为者、目标角色的主观认知。
6. 更新相关实体重要度。

`truth_status` 决定“这是真的、观察到的、传闻、误解还是已废弃”；`visibility` 决定谁可以看到。

## 安装

```bash
pip install -e /path/to/mempalace-rpg
```

如果接入 pi：

```bash
pi install npm:pi-mcp-extension
```

## CLI 快速开始

初始化/查看状态：

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 init
mempalace-rpg --db state/rpg-memory.sqlite3 status
```

写入场景：

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  commit-scene scene.json
```

为某个 actor 召回：

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  recall \
  --actor-id char_liora \
  --actor-type npc \
  --query "Liora 知道玩家承诺过什么？"
```

启用 Palace 原文归档：

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace \
  commit-scene scene.json
```

## MCP 工具

启动：

```bash
mempalace-rpg-mcp \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace
```

常用工具：

| 工具 | 用途 |
|---|---|
| `mempalace_rpg_status` / `mcp_rpg_status` | 查看 DB 计数与配置 |
| `mempalace_rpg_recall` / `mcp_rpg_recall` | ACL 过滤后的角色记忆包 |
| `mempalace_rpg_commit_scene` / `mcp_rpg_commit_scene` | 写入场景与结构化事件 |
| `mempalace_rpg_deep_recall` / `mcp_rpg_deep_recall` | 深挖可见场景片段 |
| `mempalace_rpg_get_scene` / `mcp_rpg_get_scene` | 按 ACL 读取单个场景 |
| `mempalace_rpg_upsert_profile` / `mcp_rpg_upsert_profile` | 新建/更新角色档案 |
| `mempalace_rpg_import_taverndb` / `mcp_rpg_import_taverndb` | 导入 TavernDB 旧战役 |

## 游戏接入流程

1. 用 `examples/templates/integration-checklist.md` 盘点宿主游戏已有系统。
2. 从 `examples/templates/memo_setting.template.json` 填写自己的 `memo_setting.json`。
3. 添加 MCP 配置与启动脚本。
4. 把 `examples/templates/gm-memory-rules.template.md` 加入 GM 规则。
5. 测试 `mcp_rpg_status`、`mcp_rpg_recall`、`mcp_rpg_commit_scene`。
6. 按需添加宿主侧自动 transcript 归档。

## 案例：命定之诗与黄昏之歌

`examples/dest-poet/` 是命定之诗的实际接入示例。

命定之诗已经拥有以下机械系统：

- 等级、经验、属性点
- HP/MP/SP、状态效果
- 背包、装备、技能
- 金钱、命运点数
- 战斗运行时
- 任务生命周期与奖励
- 好感度/契约
- DLC、命定核心、新闻栏目

因此它的 `memo_setting.json` 禁用了机械事件类型，并将部分领域设为 `narrative_only`。RPG memory 主要用于：

- 旧战役历史
- NPC 档案与主观记忆
- 地点与阵营长期变化
- 秘密、传闻、见证者、承诺
- TavernDB 旧酒馆导入
- Palace 原文归档

也就是说，命定之诗用本项目管理“长期叙事证据与角色认知”，但当前机械状态仍由命定之诗自己的 engine/state/tools 管理。

### pi 配置

`examples/dest-poet/mcp.json`：

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

启动脚本核心等价于：

```bash
mempalace-rpg-mcp \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace
```

### 给 GM 加的规则段落

见：

```text
examples/dest-poet/gm-memory-rules.md
```

核心意思：

```text
命定之诗已经拥有自己的 state/tools，等级、经验、HP/MP/SP、金钱、背包、战斗、任务状态、好感度、DLC、命定核心等当前值与机械结果永远以本包为准。

外部 RPG 记忆只作为长期叙事记忆层：旧战役历史、NPC 主观记忆、世界事实证据、地点/阵营变化、秘密可见性、见证者、承诺、传闻、信物来历。quest 与 item 在本包中只作为 narrative_only 使用。
```

## 酒馆数据库导入

当前导入器命令名仍叫 `import-taverndb`，但更准确地说，它支持的是**我这次使用的 SillyTavern ChatSheets 风格酒馆数据库 JSON**，不是所有酒馆数据库格式的通用导入器。

```bash
mempalace-rpg \
  --db state/rpg-memory.sqlite3 \
  --memo-setting memo_setting.json \
  --palace state/rpg-palace \
  import-taverndb "TavernDB_data_命定之诗与黄昏之歌v4.2 ... imported.json"
```

当前已适配的表名/列名：

| 当前支持的表 | 期望内容 | 导入结果 |
|---|---|---|
| `主角信息表` | 人物名称、性别/年龄、外貌、职业/身份、过往经历、性格 | 旧主角变 legacy character，不是当前 `player` |
| `重要角色表` | 姓名、性别/年龄/等级、重要日期、关系、关键记忆、目前经历 | 角色档案与角色私有/见证记忆 |
| `角色扮演指南` | 角色姓名、语言特征、动态对话示例、互动态度字典 | 语言风格/人设提示 |
| `纪要表` | 编码索引、时间跨度、地点、纪要、概览、参与人员 | 过去剧情 scene |
| `约定表` | 档案ID、约定主题、约定双方、详细内容、状态 | 旧约定；可按需要保留或清理 |
| `伏笔表` | 档案ID、伏笔主题、详细内容、状态 | GM-only 伏笔；可按需要保留或清理 |

限制：

- 不保证兼容所有 SillyTavern、世界书、聊天记录、角色卡或其他扩展数据库格式。
- 如果你的表名、列名、结构不同，需要写一个新的 importer 或先把数据转换成上述表结构。
- 大文件迁移建议用 CLI，不建议通过 MCP 让模型传整份 JSON。

适合同一世界换主角：当前新主角默认不知道旧主角私密经历，除非剧情中通过可见来源得知。

## 开发

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
pytest -q
ruff check .
```

## 许可与致谢

MIT，沿用上游 MemPalace 许可精神。本项目改造自 MemPalace，并在 README 中保留上游致谢。发布前请替换 `pyproject.toml` 中的占位仓库地址，并保留对 upstream MemPalace 的 credit。
