# play-memory v1 架构

[English](PLAY_MEMORY_MODEL.md)

此页描述当前 `play-memory-v1` schema 与 `PlayMemoryStore`。它和旧 `RpgMemoryKernel` 的表结构、CLI 与 MCP 是两种独立合同；旧入口见[合同参考](../reference/PLAY_MEMORY_V1.zh-CN.md#旧版-rpg-kernel-mcp)。

## 已接受来源、记录与事件图

Host 以不可变身份提交 `play-memory-v1` 批次。批次带有 `campaignId`、`branchId`、`epoch`、`worldRevision`、前序批次和 `records`；每条记录保留自己的 `id`、`order`、有效期和来源 span。服务接受并索引 Host 提供的内容，不判断玩家输入或导演文本的语义真假。

常用记录包括：

| 类型 | 作用 |
| --- | --- |
| `source`、`scene` | 保存来源文本、场景内容与精确来源 span |
| `event`、`link` | 保存事件及角色到事件的显式关联 |
| `acquisition`、`attitude` | 记录角色如何获知主张、其依据和对该主张的立场 |
| `profile`、`state` | 保存角色资料或状态字段，可用 `knownBy` 指定哪些角色知道 |
| `relationship` | 保存有方向的 `fromActorId` → `toActorId` 关系，并可用 `knownBy` 限制知情角色 |
| `background`、`exception` | 表示共享背景知识及单个角色的例外 |
| `date` | 保存由 Host 提供的日期和日历规则 |

`sourceSpans` 以来源 ID、版本、UTF-8 字节范围和哈希定位原文。`acquisition` 将主张和证据引用连接起来；`memory_graph` 可用于沿已存的 scene、event、actor 和 source 关系导航。图连接本身不会扩大 scope 的 grants。

## 角色目录、dossier 与认知

当前角色库接受 `core`、`major`、`recurring`、`ambient` 四种 tier。Host 根据自己的角色状态提交名单和 override；目录与 `actor_dossiers` 从当前 scope 中可见的记录构造投影。tier 与重要度策略影响角色细节选择和预算，不会调用模型生成新的人设或推断角色已知什么。

角色认知通过来源可追溯记录表达：

- `profile`、`state` 和 `relationship` 可以使用 `knownBy` 明确哪些 actor 知道该内容。
- `acquisition.beliefKind` 区分 `fact`、`hearsay`、`inference` 和 `mistaken-belief`；`method`、`basisIds`、`sourceSpans` 保留获知方式与证据。
- `attitude` 对某个 acquisition 记录 `believe`、`doubtful` 或 `disbelieve`，不会把态度转成世界事实。
- `link` 连接 actor 与 event；关系边是有向的，不会隐式变成反向关系。

这些记录使 Host 能将“发生了什么”“某人知道什么”“某人相信什么”分开提供；最终说话者仍由 Host 选择，并须使用正确的 actor ID 和 scope。

## scope、保存隔离与 reroll

Host 通过 `open_scope` 提交读取 binding。binding 包含 `campaignId`、`branchId`、`epoch`、`worldRevision`、`storyOrder`、`checkpointId`、`materialRevision`、`configHash`、purpose、actorIds、精确 grants 和 lineage。服务用 lineage 中各 ancestor 的 revision 截止点构造当前可见记录；分支身份属于绑定的一部分。

存档槽和 worldline 并非数据库自动识别的实体。Host 必须决定 save 与 campaign/branch/epoch 如何对应，何时开启新线，reroll 时应读取哪个 ancestor 及其截止 revision，并在下一次提交和 scope 中保持一致。scope grants 只授权精确记录身份；actor 邻居、图边或通配背景不会带来额外记录权限。

旧 kernel 的 `sync-branch` 使用 Host 或 pi 扩展提供的 scene ledger 清理旧自动提交场景。它不负责 play-memory v1 的 save mapping、epoch 生成或 worldline reroll。

## 背景知识与例外

`background.appliesTo` 可列出显式 actor ID，也可使用字面量 `*` 表示 Host 接受的共享常识或公共消息默认适用于后来出现的角色。某 actor 可通过指向该 background 的 `exception` 表明未知或已获知。这个规则只影响背景记录投影，不是 scope grants 的通配授权。

## 检索路径与数据边界

默认 `mechanical-bm25-v1` 使用机械检索，不加载 Qwen。选用 `qwen-summary768-lexical-weave-v1` 时，Host 可以用 `scene-summaries-v1` 打开 scope：候选必须是授权 scene，并且每个候选都需要 Host 提供导航摘要。Qwen profile 把摘要作为向量输入，同时对原始 scene/source 文本执行 BM25 lexical 检索，再按其 profile 交织结果。

因此，player input、source 原文和 scene prose 可用于词法召回或按 span 回读，但不会成为这个 Qwen profile 的 passage embedding。向量检索的是 Host 输入的 per-scene/per-floor navigation summary 与当前 query。core 不调用生成式模型产出摘要，不接受无摘要候选后偷偷回退。

本地 Qwen encoder 要求操作方预先安装 `torch`、`transformers`，并提供本地模型目录、精确树哈希与 revision；加载通过本地文件完成。可选 SQLite vector cache 持久化 passage vectors，不构成异步索引、候选发现或授权机制。

## 事件归档与 100-floor 到期

生命周期只是对显式纳入生命周期控制的事件派生 archive 投影。被接受的 source/event 记录继续作为 append-only history；归档载荷可按 `memory_archive` 读取或恢复，且在过期后可能被清除，不能据 archive payload 过期推断来源记录已经删除。

这里的 floor 是当前 lineage 中可见不同 scene 的零起始序号，不是游戏地图楼层。初始归档有效期为归档 floor 之后 100 个 floor；读取记录可能延长可读期限。具体 archive/restore、有效事件集合和到期边界由 [`play_memory_lifecycle.py`](../../mempalace_rpg/play_memory_lifecycle.py) 与 [`play-memory-v1.schema.json`](../../schemas/play-memory-v1.schema.json) 定义。

## 支持范围

| 范围 | 当前含义 |
| --- | --- |
| play-memory v1 | `schemas/play-memory-v1.schema.json`、Python 协议和 stdio 服务定义的 Host 作用域合同 |
| 旧 RPG kernel | `RpgMemoryKernel`、旧 CLI 与 `mempalace-rpg-mcp` 的兼容接口；不与 v1 schema 合并 |
| Qwen profile | 可配置的本地摘要向量 + 原文 lexical 导航路径；不代表完整 Qwen17 或一般语义召回效果 |
| RP Foundation / LLM-Wiki | 接入侧工作流、语义代理、导演和 Host 更新流程；不在此 Python core 执行 |
| 研究记录 | 单独保留的实验协议与历史研究，不自动成为当前产品契约或效果保证 |
