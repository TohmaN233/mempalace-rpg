# RP Host 接入指南

[English](RP_INTEGRATION.md)

将记忆服务作为 Host 管理的机械组件接入。Host 持有当前游戏事实、来源、存档到 campaign/worldline 的映射、分支 lineage 与模型工作流；记忆 core 校验批次、维护可追溯记录、按 scope 检索并返回允许读取的资料。core 不调用生成式模型来写剧情、判断语义、担任导演或更新游戏状态。

## 先确定谁拥有事实

列出每个域的当前权威系统：例如时间与地图、战斗和 HP、物品库存、任务阶段、NPC 人设与过往事件。对已由 Host state/tool 管理的当前数值，不要再把旧记忆当成权威；可以保存其来源、见证者、历史变化、承诺或角色曾经相信的内容。旧 kernel 的 `memo_setting.json` 仅控制旧接口的域策略，不能替代 v1 Host 的授权与范围绑定。

## 将已接受数据写入合同

1. Host 把玩家输入、可见对话、作者资料和已接受的世界记录映射为 `source`、`scene`、`event`、`acquisition`、`profile`、`state`、`relationship` 等合同记录，并保留来源 span。
2. Host 为每个已接受批次确定 `campaignId`、`branchId`、`epoch`、`worldRevision` 与前序批次身份。调用 `validate_batch` 后，再按游戏事务流程提交 `apply_batch` 并保存回执。
3. 每次读取前，Host 依据本轮已接受的游戏状态建立 `open_scope` binding，提供 lineage、story order、actor 范围、用途及精确 grants。作用域之外的记录不因关系边相连而自动获权。
4. 通过角色目录、dossier、`search`、`navigate` 和 `read` 获取本轮可用材料；Host 校验来源并编译最终上下文。
5. Host/工作流中的语义代理负责拟定补充问题、解读材料、决定导演行动和提出记忆变化。Host 负责审核、接受并把变化作为新的合同批次提交；memory core 不替代理作出这些判断。

接口、方法和参数定义见 [`play-memory-v1`](../reference/PLAY_MEMORY_V1.zh-CN.md#play-memory-v1-stdio-json-rpc)。项目接入中的 RP Foundation、LLM-Wiki semantic agents 和具体游戏导演属于 Host/语义层职责，不是本仓库内置客户端或模型调用。接入边界记录见[职责约定](../RP_FOUNDATION_INTEGRATION_BOUNDARY.zh-CN.md)。

## 保存隔离与 reroll

存档槽和 story/worldline 是游戏概念，core 不会从文件名或存档路径自动推导它们。Host 必须决定哪些 save 映射到同一个 `campaignId`，如何用 `branchId`、`epoch`、`worldRevision` 和 lineage 描述分叉、回滚或重开，并在每个 scope binding 中提交正确值。重 roll 到另一条线后，要由 Host 选择相应分支及祖先 revision 截止点；不要把一条线的当前 scope 当成另一条线的默认权限。绑定记录和已接受记录仍按 schema 保存，服务不会替游戏主持提交或重演 world transaction。

旧 kernel 的 `sync-branch` 只根据调用方给的 pi scene ledger 清理标记为自动提交的旧场景；它不是 play-memory v1 的 save-slot、worldline 或 reroll 管理器。两套路径的更多区别见[架构页](../architecture/PLAY_MEMORY_MODEL.zh-CN.md#存档worldline-与-reroll)。

## 角色知识与公共背景

角色资料、状态和关系记录可带 `knownBy`；`relationship` 是 `fromActorId` 指向 `toActorId` 的有向关系。角色信念通过带依据和获知方式的 `acquisition` 表示，可标记事实、转述、推断或误信，并可用 `attitude` 单独记录对某项主张的立场。Host 选择当前角色身份和可见范围；角色目录与 dossier 只投影 scope 内有效的记录。

`background.appliesTo` 接受显式 actor ID；字面量 `*` 只表示 Host 已接纳的共享常识/公共消息默认可用于后来出现的角色。某个角色的 `exception` 可覆盖这条背景知识。它不等于 binding.grants 的授权通配符，不能授予读取其他记录的权力。

## 双路检索与摘要输入

Qwen profile 的 `scene-summaries-v1` 候选集合只包含当前授权范围内的 scene。Host 为每个候选 scene 提供导航摘要；Qwen 向量只编码这些摘要和查询，原始来源/玩家输入/场景 prose 仍用于 lexical BM25 与按 span 原文读取，不进入该向量候选库。摘要由 Host 或其语义流程提供，core 不替它生成摘要。默认机械 profile 使用 BM25，不会静默切换到 Qwen。

本地 Qwen encoder 需要 operator 已准备好的模型目录、精确树哈希与 revision，以及预先安装的 `torch` 和 `transformers`。服务在该目录本地载入，不下载、替换模型或自动改用其他设备；向量缓存可以持久化 passage 向量，但不负责异步建索引。详细配置见[架构页](../architecture/PLAY_MEMORY_MODEL.zh-CN.md#检索路径与数据边界)。
