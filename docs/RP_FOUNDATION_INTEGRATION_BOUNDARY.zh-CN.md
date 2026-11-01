# MemPalace-RPG 与 RP Host 的职责边界

[English](RP_FOUNDATION_INTEGRATION_BOUNDARY.md)

本仓库提供 RPG 记忆存储、校验、作用域和机械检索合同。当前 play-memory v1 由 [`schemas/play-memory-v1.schema.json`](../schemas/play-memory-v1.schema.json)、[`play_memory_server.py`](../mempalace_rpg/play_memory_server.py) 和 [`play_memory.py`](../mempalace_rpg/play_memory.py) 实现；旧 `RpgMemoryKernel` MCP 是另一套兼容接口。

## 职责表

| 工作 | MemPalace-RPG | RP Host / RP Foundation / LLM-Wiki 语义层 |
| --- | --- | --- |
| 记忆合同与来源 | 校验批次、保存记录、source spans、来源身份和回执 | 将游戏、作者或玩家材料映射到合同，决定哪些事实已接受 |
| 召回与角色视图 | 按 binding 和 lineage 过滤，机械检索、沿显式图关系导航、构造目录/dossier 并回读获准表示 | 选择正确的当前 actor、query、purpose 与游戏范围；检查结果并编译送入模型的上下文 |
| 语义判断和导演 | 不执行生成式模型调用，不解释语义，不决定剧情或 NPC 行动 | 执行工作流与模型调用；语义代理提出问题、解释证据、计划导演行为并草拟记忆变化 |
| 游戏真相与更新 | 接受 Host 已批准批次并返回回执 | RP Host 管理规则、World、存档、角色和最终游戏状态；审核后接受语义层提出的更新 |
| 分支和存档 | 用 Host 提供的 campaign/branch/epoch/revision/lineage 隔离读取 | 维护 save 到 campaign/worldline 的映射，决定分叉、reroll 和回滚的正确绑定与提交顺序 |

接入时，core 可以运行本地 embedding encoder 来完成配置的机械向量排序；这不等于它调用导演模型、执行语义代理或承担 RP Foundation 的模型工作流。LLM-Wiki 和具体语义代理作为宿主侧集成职责记录，不是此仓库的运行时依赖。

## 两套服务合同

- **play-memory v1**：Host 经 stdio JSON-RPC 提交已接受批次、绑定读取 scope、检索或读取获准表示。记录与字段由 v1 schema 定义。
- **旧 RPG kernel MCP**：面向 `RpgMemoryKernel` 的旧 `mempalace-rpg-*` CLI/MCP 工具，使用其自己的 scene、profile、ACL 和 JSON 字段。

两套接口不共享可互换的工具名、数据结构或存档管理。接入步骤见[RP Host 指南](guides/RP_INTEGRATION.zh-CN.md)，完整方法见[协议参考](reference/PLAY_MEMORY_V1.zh-CN.md)。

## 作用域与最终上下文

Host 决定一条请求对应的游戏快照，并在 binding 中提供 `campaignId`、`branchId`、`epoch`、`worldRevision`、用途、actor IDs、精确 grants 与 ancestor lineage。core 根据这些机械边界返回有效记录；图相邻关系、相同角色名或共享 background 不会自动授予其他记录权限。

Host/工作流负责为模型选择正确的上下文和 actor，执行补充提问、导演、审查及最终输出。记忆包或 read handle 只说明服务按绑定返回了哪些材料，不替代 Host 校验，也不自动变为模型应执行的最终剧情。
