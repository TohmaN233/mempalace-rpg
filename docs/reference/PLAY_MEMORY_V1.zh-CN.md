# play-memory v1 协议参考

[English](PLAY_MEMORY_V1.md)

Host 按 `play-memory-v1` schema 提交数据。

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

下一批的 `predecessorBatchId` 指向当前语义记忆批次。Host 的机械编辑可以推进 World 版本而不写记忆。已有分支允许 `previousWorldRevision` 位于语义记忆头与新 `worldRevision` 之间，后者必须严格更大。这些间隔无需填充空批次，错误前驱或倒序版本仍会报错。

### Qwen profile 参数

服务默认 `--profile mechanical-bm25-v1`。Qwen profile 使用：

- `--profile qwen-summary768-lexical-weave-v1`
- 必需的 `--model-dir`、`--model-tree-sha256`、`--model-revision`
- 可选的 `--device`、`--dtype`、`--cuda-cache-policy`、`--vector-cache-path`

Qwen 运行依赖操作方预装 `torch` 和 `transformers`，并在模型目录提供通过哈希验证的模型文件。载入为本地模式，无下载、模型替代、设备回退或 lexical-only recovery。`scene-summaries-v1` 的候选和向量输入边界见架构页。

角色资料还返回按本人实际知情建立的 `memoryHierarchy`，其中含事件父子引用、概括、心级、锁定、折叠与来源范围。`actor_dossiers` 可传 `includeSourceExcerpts=false` 省略原文摘录。归档年龄不再导致删除，旧 `expiresAfterFloor` 为兼容字段。
