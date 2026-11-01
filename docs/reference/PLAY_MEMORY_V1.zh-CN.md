# play-memory v1 与旧 kernel MCP 参考

[English](PLAY_MEMORY_V1.md)

`play-memory-v1` schema 是当前 Host 作用域服务的合同来源；旧 `mempalace-rpg-mcp` 仍是另一套基于 `RpgMemoryKernel` 的 MCP API。方法、工具和 record field 不可跨接口互换。

## play-memory v1 stdio JSON-RPC

服务入口：

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

Host 启动时通过 `PLAY_MEMORY_HOST_TOKEN`（或 `--host-token`）传入非空的临时共享令牌，并在需要 Host 身份的请求中使用它；这是服务进程凭据，不是模型 API key。PowerShell 可用：

```powershell
$env:PLAY_MEMORY_HOST_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

服务使用 JSON-RPC 2.0 JSON Lines，一行请求对应一行响应。`stdout` 仅用于协议帧，诊断写入 `stderr`。schema、record 定义和字段验证由 [`schemas/play-memory-v1.schema.json`](../../schemas/play-memory-v1.schema.json) 与 [`mempalace_rpg/play_memory_protocol.py`](../../mempalace_rpg/play_memory_protocol.py) 维护。

| 方法 | 用途 |
| --- | --- |
| `capabilities` | 查询协议、profile、表示、scope 和向量模式 |
| `validate_batch`、`apply_batch`、`receipt` | 校验、提交 Host 已接受的批次并检查提交回执 |
| `open_scope`、`close_scope` | 按 Host binding 创建并关闭进程内作用域 |
| `actor_directory`、`resolve_actors`、`actor_cues`、`actor_dossiers`、`actor_packs` | 列出、解析并投影作用域内角色资料 |
| `search`、`navigate`、`navigation_cards`、`read`、`warm_scope` | 作用域内检索、图导航、读取和预热 |
| `memory_graph`、`memory_archive` | 查看事件关系图，读取或恢复符合生命周期政策的归档记录 |

通用 `sourceSpan`、batch、binding 和每种 RPC 的精确字段约束位于 schema 的 `$defs`。binding 至少携带 `campaignId`、`branchId`、`epoch`、`worldRevision`、`storyOrder`、`checkpointId`、`materialRevision`、`configHash`、purpose、actorIds、grants 和 lineage。记录类型与 `knownBy`、`appliesTo`、`fromActorId`/`toActorId` 语义见[架构页](../architecture/PLAY_MEMORY_MODEL.zh-CN.md)。

### Qwen profile 参数

服务默认 `--profile mechanical-bm25-v1`。Qwen profile 使用：

- `--profile qwen-summary768-lexical-weave-v1`
- 必需的 `--model-dir`、`--model-tree-sha256`、`--model-revision`
- 可选的 `--device`、`--dtype`、`--cuda-cache-policy`、`--vector-cache-path`

Qwen 运行依赖操作方预装 `torch` 和 `transformers`，并在模型目录提供通过哈希验证的模型文件。载入为本地模式，无下载、模型替代、设备回退或 lexical-only recovery。`scene-summaries-v1` 的候选和向量输入边界见架构页。

## 旧版 RPG kernel MCP

旧服务入口：

```bash
mempalace-rpg-mcp --db state/rpg-memory.sqlite3 --memo-setting memo_setting.json --palace state/rpg-palace
```

MCP 协议通过 `initialize`、`tools/list`、`tools/call` 暴露以下主要工具名：

| 工具 | 用途 |
| --- | --- |
| `mempalace_rpg_init`、`mempalace_rpg_status` | 初始化和查看旧 kernel 状态 |
| `mempalace_rpg_upsert_profile` | 写入旧 `ProfileCard` |
| `mempalace_rpg_commit_scene` | 写入旧格式 scene transcript 与提取事件 |
| `mempalace_rpg_recall`、`mempalace_rpg_deep_recall` | 返回旧 `MemoryPack` 或按权限回读来源片段 |
| `mempalace_rpg_get_scene` | 按场景标识读取权限检查后的片段 |
| `mempalace_rpg_import_taverndb` | 导入旧 TavernDB/ChatSheets 数据 |

旧接口的 recall 需提供 `campaign_id`、`actor_id` 和 `query`；场景、角色与可见性字段采用其自己的 snake_case JSON contract。`mempalace_rpg_list_memories`、`mempalace_rpg_list_world_facts`、`mempalace_rpg_list_actor_beliefs` 虽在 server 中列出，但它们是禁用的本地管理员 surface，通过 MCP 会被拒绝。

旧 kernel CLI 还提供 `backup`、`restore`、`delete-after`、`delete-scenes` 与 `sync-branch` 等维护命令。这些能力不是 play-memory v1 stdio 的方法。旧 Pi 示例和导入资料见 [`pi-integration.md`](../pi-integration.md) 与 [`taverndb-import.md`](../taverndb-import.md)。
