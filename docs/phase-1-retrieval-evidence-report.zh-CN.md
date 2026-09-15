# 第一阶段检索方法证据报告：跨数据集工程验证与方法选择

**状态：阶段一完成；三组实验均构成本阶段正式决策证据，Static P5 已具备进入 RPG 记忆召回阶段的依据。**
**报告范围：** LoCoMo、ConvoMem 固定种子子集与 MemBench 同规模固定种子研究的终局证据。
**主张边界：** 本报告要回答的是新检索方法是否可行、是否足以冻结方案并进入 RPG 记忆召回研究。答案是肯定的。完整 ConvoMem census、官方 MemBench `data2test` 和 exact-unpatched baseline 属于另一种“官方 benchmark 完整复现”主张，不是本阶段的完成门槛。

**术语说明。** 产物中的 `formal_evidence_eligible=false` 是 AERP one-shot 官方复现协议的资格字段；它表示本次运行不能冒充该协议下的完整官方 benchmark release，不表示实验是随意的、seed 是事后挑选的，也不否定其作为本项目阶段一正式决策证据的效力。本文以下区分“阶段正式证据”和“官方完整复现资格”，不再用笼统的“非正式证据”混称二者。

## 摘要

本研究的第一阶段考察一个工程上直接的问题：在保持同一候选语料、同一查询与同一排名预算的前提下，固定的多视图检索融合是否能稳定优于原始兼容检索，并且这种收益能否跨越不同类型的长期记忆评测数据而保留。我们比较了 Original compatibility、Strong Raw、Static P5 与 Six-View 四种策略，并在 LoCoMo、ConvoMem 固定种子 observed-pair 子集及 MemBench 同规模固定种子研究上完成了可追溯的终局计算。

结果显示，Static P5 在三组研究的主要 Recall@10 指标上均优于 Original：LoCoMo 为 **+12.79 个百分点**，ConvoMem 为 **+6.86 个百分点**，MemBench 为 **+2.07 个百分点**。Six-View 也在三组 Recall@10 上为正（+12.24、+6.80、+0.93 个百分点），但在 MemBench 的排序质量上不如 Static P5；Strong Raw 则在 ConvoMem 和 MemBench 中未显示一致优势。LoCoMo 上 P5 相对 Original 的问题宏平均 Recall@10 提升经 5,000 次配对 bootstrap 验证，95% 置信区间完全高于零；ConvoMem 上 P5 与 Six-View 的 10,000 次配对 bootstrap 亦完全高于零。MemBench 中 P5 的两次独立 current 运行产物字节级一致。

因此，第一阶段——**提出并选择检索方法、完成跨数据集可行性验证、固定可复现执行与审计边界**——已经结项。ConvoMem 与 MemBench 都采用单一事前固定 seed，而不是扫描多个 seed 后选择最好结果：ConvoMem receipt 记录 `selection_seed=20260826`，MemBench receipt 记录 `seed=membench-same-scale-v1-20260902`。固定且非结果导向选择这一实验性质成立。本报告将方法收益、工程恢复与机制解释严格区分：观测到的是跨研究的一致增益与重复性；机制论述是受实现与切片结果约束的解释，不是单因素隔离后的因果证明。

## 1. 研究问题与阶段定义

### 1.1 研究问题

在有限的本地计算预算下，检索系统应采用何种固定融合策略，才能在不改变候选语料、查询集合和 `top-10` 排名预算的前提下，提高长期记忆证据的可检索性，并在不同数据集上保持可复现的收益？

### 1.2 阶段一的完成条件

本阶段的目标不是复刻每个 benchmark 的全部官方发布条件或宣称世界纪录，而是为下一阶段 RPG 记忆召回确定一个可行、稳定、可审计的检索方法。完成条件是：

1. 明确 Original 兼容基线和三种候选检索策略；
2. 在至少三个结构不同的研究集合上运行可审计比较；
3. 对主要收益给出配对不确定性或重复性证据；
4. 记录失败、恢复身份与证据资格，避免把工程恢复伪装为原始正式复现；
5. 形成下一阶段的明确方法选择与证据缺口。

按此定义，阶段一完成，且三组运行均是这一决策的正式输入。若未来另行主张“完整复现官方 benchmark”或对外比较 SOTA，则还需要相应的完整数据、未修改兼容路径和发布协议；那是额外的外部验证目标，不阻塞下一阶段 RPG 记忆召回研究。

### 1.3 面向非专业读者：这里的“长期记忆检索”是什么

长期记忆系统首先面对的并不是“生成一句好答案”，而是一个更基础的取证问题：面对一个新问题，能否从大量历史消息里把**足以支持回答的那几条原始证据**排到最前面。设查询为 $q$，历史消息集合为 $C$，评测者事先保存的正确证据集合为 $G(q)\subseteq C$。检索器只允许返回十条候选 $R_{10}(q)$；后续回答器、游戏叙事器或人工读者才可能使用其中的文本。

因此，本阶段衡量的是“证据是否被找回”，而不是语言模型是否写出看似合理的答案。这一区分很重要：若关键历史消息没有进入前十，后续模型即使能力很强也没有可靠事实可用；相反，即使证据已经找回，生成层仍可能误读、忽略或错误综合它。第一阶段只解决前者。

可以把这一过程想成在一座巨大档案馆里找十张最可能有用的卡片。系统通常使用两类线索：

- **词面检索（BM25）**：问题和历史消息共享越多有区分度的词，分数越高。它擅长人名、文件名、密码、数字等精确线索，但不天然理解同义表达。
- **稠密检索（dense retrieval）**：编码器把问题和消息各自变成一串数字，即“向量”；意思越接近，向量方向通常越接近。它能连接“情绪低落”和“感到沮丧”这类不同措辞，但可能错过精确实体或把语义相似、事实错误的消息排得很高。

本研究的核心不是发明新的语言模型，而是改变**同一批历史消息如何被表示、如何产生多个排名、以及这些排名如何合并**。最终输出仍是十个可回指到原文的候选 ID，而不是自由生成的答案。

### 1.4 指标如何阅读，以及为何使用它们

对每一个有正证据的查询，三个核心指标回答三个不同问题：

| 指标 | 直观定义 | 它防止遗漏的失败模式 |
| --- | --- | --- |
| Recall@10 | $|G(q)\cap R_{10}(q)|/|G(q)|$；正确证据有多少比例进入前十 | 关键记忆根本没被带到回答阶段 |
| MRR@10 | 正确证据首次出现名次的倒数（未命中为 0），再取平均 | 虽然命中，但第一个正确证据排得太靠后 |
| NDCG@10 | 按 $1/\log_2(1+r)$ 对第 $r$ 位的相关证据折损，并以理想排序归一化 | 多条正确证据的整体顺序不好，相关项被无关项挤到后面 |

例如，若一条唯一正确消息被排在第 1 位，Recall@10 和 MRR@10 都为 1；若被排在第 10 位，Recall@10 仍为 1，但 MRR@10 只有 0.1，NDCG@10 也显著下降。故 MemBench 同时报三项指标：它能区分“多找回了一点”与“真的把重要内容排到可用位置”。LoCoMo 和 ConvoMem 的主分析以各自协议定义的宏平均 Recall@10 为主；其含义是让每个问题或 persona 在平均中有相等影响，而不是让消息最多的会话主导结论。

## 2. 方法

### 2.1 原版 MemPalace 的完整概念模型：一条记忆如何被存入和找到

MemPalace 是一个本地优先、尽量保存原文的长期记忆系统。它用“宫殿”比喻组织信息，但这些名词对应真实的数据结构：

| 层级 | 对零基础读者的解释 | 原版中的职责 |
| --- | --- | --- |
| Palace | 一整座档案馆 | 一个本地记忆库及其索引、元数据和辅助结构 |
| Wing | 档案馆的大区 | 按人物、项目或大主题作粗粒度组织 |
| Room | 大区中的房间 | 按时间段、会话或子主题缩小检索范围 |
| Drawer | 房间里的抽屉/卡片 | 保存可回读的原始文本块；本实验的一条历史消息就是一张卡片 |
| AAAK | 很薄的目录卡 | 保存比原文短的指针/索引信息，供模型先定位可能相关的 drawer |
| Knowledge graph | 实体关系账本 | 记录“实体—关系—实体”及其时间有效性，支持结构化关系查询 |
| Closet | 额外的语义分区 | 可对特定类别的记忆提供额外候选或加权信号 |

原版的一般工作流可以拆成五步：

1. **写入原文。** 新信息以可追溯的文本块进入 drawer；Wing、Room、speaker、时间等作为元数据保存，而不是把原文只压缩成不可逆摘要。
2. **建立向量索引。** 默认 Chroma 后端用 `all-MiniLM-L6-v2` 把每段文本编码成 384 维向量，并用 HNSW 近邻索引保存。这里的向量不是人能直接阅读的摘要，而是供距离计算使用的数字表示。
3. **维护辅助入口。** 完整产品可以另外维护 AAAK 目录、知识图谱和 closet。它们解决的是“先缩小可能的位置”“沿实体关系找线索”或“对特殊记忆加权”等问题。
4. **搜索候选。** 用户提出问题后，系统可先按 Room 等元数据过滤，再用问题向量在 Chroma 中找语义接近的 drawer；随后可用词面分数重新排列一小批结果。
5. **回读证据。** 搜索返回的是带来源的原文命中，供后续模型或用户阅读；检索本身不等于最终回答生成。

### 2.2 完整产品、RPG 扩展与本报告实际测量的路径

本项目的 `RpgMemoryKernel` 是建立在上述记忆思想之上的 RPG 产品层：它维护场景、事件、记忆项、角色档案、世界事实与角色信念等 SQLite 状态，并由 `EvidenceAuthorizer` 按 campaign、actor、可见性、分支与事实状态先筛出角色有权知道的事件。可选的 `EpisodeAdapter` 再把场景转写到外部记忆后端。

但本报告的三项配对 benchmark **没有把完整 MemPalace 或完整 RPG 内核的所有能力混在一起比较**。Original 包装器只创建一个名为 `mempalace_drawers` 的 Chroma collection，把每条候选的原始文本写进去，并以 conversation/corpus 对应的 `room` 过滤后调用公开的 `search_memories`。它没有为这些基准数据填充 AAAK、知识图谱或 benchmark closet。因此这些模块既不参与本报告的 Original 分数，也不能被用来解释分数变化。

Current 路径比较的是另一个受控切面：先得到同一问题可访问的候选消息宇宙，再为候选构造多个文本表示并融合排名。LoCoMo 会通过真实授权接口把对话转成公开世界事件；ConvoMem 与 MemBench 则把冻结投影直接转换为 `AuthorizedRetrievalCandidate`。后两者的 actor、checkpoint 等字段是固定适配元数据，不代表完整 RPG 世界状态也参与了实验。

所以，本文能够归因的差异是：**Original 的 drawer 文本索引与产品搜索排序**，对比 **Strong Raw / Static P5 / Six-View 的全候选多表示排序**。本文不能把收益归因于未进入执行路径的 AAAK、知识图谱、closet 或生成模型。

### 2.3 精确的 paired-benchmark 数据流

每个项目遵循同一条可审计的最小数据流：

$$
\text{原始对话/轨迹}\;\rightarrow\;\text{label-free candidate projection}\;\rightarrow\;
\begin{cases}
\text{Original public-product worker}\\
\text{Current six-view ranker}
\end{cases}
\;\rightarrow\;\text{top-10 candidate IDs}\;\rightarrow\;\text{custody-held gold scoring}.
$$

candidate projection 可见的内容是查询、候选 ID、顺序、消息文本和必要的非标签元数据；正确答案、gold evidence ID、题型和 source locator 被放进 custody 工件，不被排名器读取。MemBench 的构建器还会拒绝候选投影中出现 `target_step_id`、`ground_truth`、`choices`、`question_type`、`scenario` 等标签字段。这个隔离的目的不是让数据“神秘化”，而是避免检索器或其日志从评测标签反向获益。

各 current arm 都在同一投影、同一查询、同一 MiniLM 编码器身份和同一六个基础排序上工作；只改变固定融合权重。Original 则从同一消息文本重建其公共产品所需的物理 ID 与 Chroma 索引，并通过公共搜索 API 查询。二者均不能读取 custody 的 gold；评分器在冻结 rankings 后才打开 custody。

### 2.4 Original compatibility：从消息到 Chroma 候选，再到 top-10 内 BM25 重排

这里描述的冻结原版身份是 MemPalace v3.8.0 基线 commit `87e6f38377b4bee0666374b05df6e14ffd154245`、tree `639b2a849816fd4853072920405822824464e9c6`。部分长跑恢复绑定了 commit `840063aa919d97afc481b9dc5e4a86f326575ef7`：它只让 Chroma `upsert` 按运行时批量上限安全分片，没有改变以下查询或评分逻辑。

Original arm 不以内部直接 Chroma 查询替代产品行为。包装器把每个候选消息按会话写入 `mempalace_drawers` collection，随后只调用公开接口：

```text
search_memories(query, palace_path,
  room=<conversation>, n_results=10, max_distance=0.0,
  candidate_strategy="vector", collection_name="mempalace_drawers")
```

运行时固定使用 Chroma native MiniLM、CPU provider 与 cosine HNSW。审核的 HNSW 配置是 `space=cosine`、`ef_construction=100`、`ef_search=100`、`max_neighbors=16`、`num_threads=1`、`batch_size=100`、`sync_threshold=1000`、`resize_factor=1.2`。`max_distance=0.0` 表示不作距离阈值过滤；`candidate_strategy="vector"` 使用向量索引的历史默认路径，而非 lexical union 扩展。

精确地说，`n_results=10` 时原产品会要求 HNSW 过取 $10\times3=30$ 条向量候选（如果该会话不足 30 条，则受实际候选数限制）。在本 benchmark 没有 closet 候选可合并的情况下，代码先按向量距离排序，并执行 `hits = scored[:n_results]`，也就是**先把集合截成向量 top-10**；之后 `_finalize_candidate_hits` 才在这十条内部计算 Okapi BM25。对这十条中的候选 $d$，MiniLM/Chroma 的 cosine distance $\delta(d)$ 转为 $s_v(d)=\max(0,1-\delta(d))$：

$$
\operatorname{BM25}(q,d)=\sum_{t\in q}
\log\!\left(\frac{N-df_t+0.5}{df_t+0.5}+1\right)
\frac{tf_{t,d}(1+1.5)}{tf_{t,d}+1.5(1-0.75+0.75|d|/\overline{|d|})}.
$$

BM25 原始分数除以这十条中的最大值，得到 $\widehat{b}(d)$；最终产品分数为

$$
S_{\mathrm{original}}(d)=0.6\,s_v(d)+0.4\,\widehat{b}(d).
$$

再按此分数重排这十条，同分时原则上偏向较新的 `authored_at`；但 benchmark 写入的元数据没有提供 `authored_at`/`filed_at`，所以这里没有可用的真实时间新旧信号。这解释了 Original 并非“纯向量排序”：BM25 可以改变 top-10 内的先后顺序，从而影响 MRR/NDCG；但它的 **Recall@10 成员集合已经由向量 top-10 冻结**，BM25 无法把向量第 11 名或更后的精确词面命中救进前十。

### 2.5 Current 策略的共同六视图框架

每个查询由六个候选排序视图产生列表：

| 视图 | 含义 |
| --- | --- |
| `raw_bm25` | 原始文本的 BM25 检索 |
| `observation_bm25` | observation 表示上的 BM25 检索 |
| `raw_dense` | 原始文本的稠密检索 |
| `observation_dense` | observation 表示上的稠密检索 |
| `checkpoint_dense` | checkpoint 表示上的稠密检索 |
| `combo_dense` | 组合表示上的稠密检索 |

融合采用加权 Reciprocal Rank Fusion（RRF），常数固定为 $k=60$：

$$
S(d\mid q)=\sum_{v \in V} \frac{w_v}{60+\operatorname{rank}_v(d)}.
$$

其中 $d$ 是候选证据，$q$ 是查询，$\operatorname{rank}_v(d)$ 为候选在视图 $v$ 中的名次。权重顺序固定为
`[raw_bm25, observation_bm25, raw_dense, observation_dense, checkpoint_dense, combo_dense]`。

| 策略 | 固定权重 | 作用 |
| --- | --- | --- |
| Original compatibility | 产品原有兼容检索路径 | 对照基线 |
| Strong Raw | `[2, 0, 1, 0, 0, 0]` | 以原始文本 lexical+dense 信号为主的强对照 |
| Static P5 | `[2, .5, 1, 2, 0, 1]` | 原始、observation 与组合表示的固定加权融合 |
| Six-View | `[2, .5, 1, 2, 2, 1]` | 在 P5 基础上加入 checkpoint dense 视图 |

所有比较将 `top-10` 作为统一排名预算。三个 current 策略共享候选投影、查询、编码器和六个基础排名视图，仅固定融合权重不同；Original compatibility 则保留原产品自身的 text-only serializer、索引构建和公开搜索调用。因而 current arms 的差异不是扩大候选数或改变评测问题；current 与 Original 比较的是两条端到端排名实现，而非只改一个权重的消融，也不等于比较完整 MemPalace 产品的全部模块。

#### 表示、视图与得分后果

对每条 `AuthorizedRetrievalCandidate`，`raw_text` 是原消息文本；`observation` 是稳定字段化字符串，包含 `summary`、`event_type`、`actor_id`、`target_id`、相关实体/任务/地点、世界时间和地点 ID；`combo` 是 `raw_text + "\\n" + observation`。`checkpoint_dense` 不是把 checkpoint 名字直接当文本，而是先按 `(checkpoint_key, policy_tuple)` 分组，将同组成员的 observation 按时间顺序拼接，做一次稠密编码，再把该组得分回填给组内成员。

其实际字段顺序固定为：

```text
summary=<对这条记忆的稳定文本表示>
event_type=<事件类型>
actor_id=<谁做了或说了这件事>
target_id=<动作指向谁>
related_entities=<相关实体>
related_quests=<相关任务>
related_locations=<相关地点>
in_world_time=<世界内时间>
location_id=<地点 ID>
```

这不是让生成模型临场“自由总结”。三组实际 benchmark adapter 都把原候选消息作为 `summary`，再附加固定字段标签和已有元数据；LoCoMo runner 明确不采用源文件中的 generated observation/session summary，ConvoMem 与 MemBench 也不会凭空生成实体、任务或地点知识，缺少的信息保持为空。因此 observation 是**原文的字段化重序列化**，不是语义摘要，不能把这层外壳误写成系统已经自动理解了人物、任务和地点。访问控制的 `policy_tuple` 也不会拼进可被编码器看到的文本，它只负责分组和授权边界，避免权限标签变成意外的相关性提示。

六个基础视图分别是 `raw_bm25`、`observation_bm25`、`raw_dense`、`observation_dense`、`checkpoint_dense` 与 `combo_dense`。BM25 使用去重 query token、Lucene-style 平滑 IDF、$k_1=1.5$、$b=0.75$；稠密视图使用查询向量与 passage 向量的点积。每个视图对**全部授权候选**形成从 1 到 $|C|$ 的完整名次，候选若基础分数相同则用稳定 `ranking_key` 打破平局。

加权 RRF 只合并名次，不混合六种原始分数：

$$
S_{\mathrm{RRF}}(d\mid q)=\sum_v\frac{w_v}{60+r_v(d)}.
$$

这消除了 BM25 与 dot-product 数值量纲不相容的问题。以第 1 名为例，权重 2 的视图贡献 $2/61\approx0.03279$，权重 1 的视图贡献 $1/61\approx0.01639$，权重 0.5 的视图贡献 $0.5/61\approx0.00820$；权重为 0 的视图不贡献分数。因此 P5 不是“平均六个模型”，而是有意让原始 lexical 与 observation dense 成为两条强主干，并让三个辅助视图只在其名次支持某候选时加分。

Original 与 Current 的决定性差别不只是公式长短，而是**谁有机会进入最终十条**：Original 在该路径中先由单一 dense 排名确定 top-10，BM25 只能调整这十条的顺序；Strong Raw 的 raw BM25 与 raw dense、以及 P5/Six 的各个启用视图，都分别对完整授权候选宇宙排名后才做 RRF。因此一个候选即使 dense 排名不在前十，只要在 raw BM25、observation dense 或 combo dense 中足够靠前，仍可能通过累计名次贡献进入最终 top-10。

P5 和 Six 的差别只有 checkpoint 视图的权重：P5 令其为 0，Six 令其为 2。checkpoint 先将同组 observation 按时间拼接并得到一个组级 dense 分数，再把同一分数回填给所有组员。这能让分散在同一事件段里的证据一起上升，也可能把组内无关邻居一并抬高；后者与 Six 在 MemBench 上 Recall 略增、MRR/NDCG 下降的结果一致。

### 2.6 从原版分数到 P5/Six：究竟改了哪几层

| 层面 | Original compatibility | Strong Raw / Static P5 / Six-View 的更改 | 对结果的直接含义 |
| --- | --- | --- | --- |
| 候选准入 | dense 先决定最终 top-10 成员 | 每个启用视图先给完整候选集排名，再融合 | 词面或另一表示可以把 dense top-10 外的候选带入结果 |
| 文本表示 | 只编码原始候选文本；speaker 只是 metadata | raw、字段化 observation、raw+observation，以及 Six 的 checkpoint 组文本 | 同一事实可以通过原话、角色/事件结构或上下文组被匹配 |
| 原始相关性 | cosine similarity 与 top-10 内 BM25 | raw/observation BM25，以及多个 dense dot-product 排名 | 不把所有异质量纲硬塞进一个线性分数 |
| 融合公式 | $0.6s_v+0.4\widehat b$ | $\sum_v w_v/(60+r_v)$ | 只依赖每个视图的相对名次，跨视图分数尺度不再需要校准 |
| P5 与 Six | 不适用 | Six 仅比 P5 多 `checkpoint_dense` 权重 2 | 可直接观察组级上下文究竟帮助召回还是污染前排顺序 |

这里的“基于原版更改”不是在 Original 的 $0.6/0.4$ 上再加几个小数项，而是替换了排名阶段：保留同一问题、候选原文和 top-10 预算，继续使用同一 MiniLM 身份作稠密表示，但把“单路 dense 截断后再重排”改成“多路完整排名后做固定 RRF”。Strong Raw 是最小的新框架对照；P5 再加入 observation 与 combo；Six 再打开 checkpoint。

一个纯示意例子可以帮助理解这个差别。假设正确消息在 raw dense 中排第 11，却因为包含准确文件名而在 raw BM25 中排第 1，并在 observation dense 中排第 3。Original 会在 BM25 运行前就把它排除；P5 则仍会给它 $2/61$ 的 raw-BM25 贡献、$1/71$ 的 raw-dense 贡献和 $2/63$ 的 observation-dense 贡献，再叠加其他启用视图。它不保证一定进入前十，但至少保留了被多种互补线索共同“投票”救回的可能。这里的名次只是解释公式的假设值，不是从实验结果挑出的个案。

### 2.7 机制假设与可证伪含义

Static P5 的设计假设是：同一记忆证据以原始字符串、带稳定字段标签/说话者信息的 observation 重序列化、以及两者拼接输入编码时，会产生互补但相关的排名；加权 RRF 可在任一视图排名不佳时利用其他视图提供冗余支持。P5 保留较强的 `raw_bm25` 和 `observation_dense` 权重，同时以较小权重加入 observation lexical 与 combo dense 信号。Six-View 进一步引入 checkpoint dense，测试更多上下文分组是否总是有益。这里没有“先由生成模型理解并摘要记忆”的步骤。

这是一个**机制解释**而非已隔离的因果结论。它受到三类可观测事实约束：P5 在三项研究主指标上均为正；ConvoMem 的 hard slice 具有更大的正增益；而 Six-View 在 MemBench 的 MRR/NDCG 上劣于 P5，说明额外视图可能引入排序噪声而非单调增益。要把该解释升级为因果证明，仍需要视图消融、预注册假设与新的独立盲测。

## 3. 三项研究的设计与证据等级

### 3.1 三个集合实际包含什么

三个数据集都把任务写成“从历史记录中找证据”，但历史记录的形态不同：LoCoMo 是两个人跨数月聊天，ConvoMem 是一个 persona 的大量多会话消息，MemBench 则用受控轨迹覆盖事实、更新、情绪和偏好。下面不只报告名称，而是说明系统究竟看见了什么、要找回什么。

#### 3.1.1 LoCoMo：跨许多次聊天找回人物生活史证据

本地官方源文件包含 **10 段长期人物对话、272 个 session、5,882 个 dialogue turns 和 1,986 个 QA**。一次完整对话可跨 19–32 个 session、包含 369–689 条消息。实验使用其中 **1,982 个有可用证据定义的问题**；源数据的 1,986 个 QA 按类别分为 multi-hop 282、temporal 321、open-domain 96、single-hop 841、adversarial 446。gold 共引用 2,815 个证据项，其中 2,806 个能解析到候选消息；423 个问题要求找回不止一条证据。

系统看到的是问题，以及该人物对话中按时间排列的历史消息。每条候选保留 speaker、session date、可选图片 caption、原始 text 和 opaque ID。系统不看到 gold ID 或答案；任务是返回十个历史消息 ID。独立评分器再检查官方标注的证据消息是否在这十条中。

具体问题包括：

| 类型 | 历史中实际出现的内容 | 查询要求找什么 | 为什么困难/重要 |
| --- | --- | --- | --- |
| Temporal | Caroline 说自己“昨天”去了 LGBTQ support group，同时 session 有日期 | 找回该发言并结合 session 日期，支持“她何时去过” | 只匹配事件词不够，还要把相对时间绑定到对话日期 |
| Open-domain | Caroline 谈到继续教育、心理健康与 counseling | 找到这些分散发言，支持她可能攻读 psychology/counseling certification | 问题措辞不是历史原句，需要从经历推向合理领域 |
| Single-hop | 一条消息说明 charity race 是为了提高 mental health awareness | 找回直接陈述该目的的消息 | 检验最基本的精确事实召回，复杂方法不能以牺牲简单题为代价 |
| Adversarial | 关于 charity race 后“self-care 很重要”的话实际由 Melanie 说出 | 面对带有错误人物归因的问题，仍应找回真实出处 | 防止系统只因关键词吻合，就把另一人物的经历错误归给 Caroline |

LoCoMo 因而模拟“认识一个人很久以后，对方问起自己曾说过的经历”。它重要在于：真实长期对话中的事实会被大量无关日常消息隔开，时间表达会变化，答案还可能依赖多条跨 session 证据。

#### 3.1.2 ConvoMem 固定种子子集：从近百万条 persona 消息中找事实、更新与缺失信息

完整 ConvoMem 源清单为 **2,067 个文件、26,894,940,265 字节**。由于完整 census 不适合个人笔记本，本研究没有事后挑最好结果，而是用事前固定的 `selection_seed=20260826` 和绑定源版本的确定性选择，生成 **360 个 observed item–context pair**。候选投影合计包含 **906,655 条消息文本**；每个评测项都有 `persona_id`、`corpus_id`、问题和该 corpus 内按顺序排列的 speaker/text 消息。

360 项平均分为六组，每组 60 项。下表给出实际内容而非仅给类别名：

| 分组 | 历史中实际出现的内容 | 查询要找回的证据/正确行为 |
| --- | --- | --- |
| `user_evidence` | 用户说明 Q4 campaign 预算 **$500k**、目标是 fintech startups、MQL 目标 **+20%** | 找回这些用户陈述，支持一份不遗漏数字和目标的 campaign 摘要 |
| `assistant_facts_evidence` | 助手曾为 leadership offsite 推荐 *The Path Between the Seas* | 当后来问“你推荐的书叫什么”时，找回**助手自己说过的话**，而不是只记用户事实 |
| `preference_evidence` | 用户说 Clay 会同步 contacts/LinkedIn，并帮助记住客户的 hobbies、family details | 当询问用什么 app 管理这些私人化客户信息时，找回该偏好并推荐 Clay |
| `implicit_connection_evidence` | 用户说 guest WiFi password 每月重置，但历史只含旧密码 | 当问“当前密码是什么”时，找回重置规则并推断旧密码已不可靠；应建议询问 IT，而非复述过期密码 |
| `changing_evidence` | 历史先后出现 `Globex_Onboarding_v1.pptx`、`Globex_Onboarding_Q3_UI.pptx`、最新的 `Globex_Custom_Snippets.pptx` | 找回版本变化链，并以最新文件名回答“当前培训演示文稿是什么” |
| `abstention_evidence` | 历史说 Michael 周五到 Boulder、要 hiking、逛 breweries、需要从 DIA 接机，却从未给 flight number；该 context 有 6,105 条消息 | 当问确切航班号时识别“无证据可答”，不能被大量相关旅行词诱导去编一个号码 |

其中 300 项有正证据，进入 persona-macro Recall@10；60 项 abstention 单独评估，不混入正证据 Recall 分母。sealed custody 另存答案、evidence conversation IDs、源定位和类别，排名器看不到它们。

这个数据集重要，因为现实中的个人记忆不只要回答“以前说过什么”，还要分清谁说的、哪个版本最新、偏好如何应用、旧信息是否失效，以及问题是否根本没有答案。近百万候选文本也检验方法在大量相似消息中的筛选能力。

#### 3.1.3 MemBench 同规模固定种子研究：受控覆盖事实、推理、情绪与偏好

MemBench 的一条源记录由 trajectory ID、`message_list` 和 QA 构成。官方术语把数据分为两种视角和两种认知层次：

| 角色 | 具体含义 | 本研究可评分项数 | 实际任务示例 |
| --- | --- | ---: | --- |
| `observation_factual` | 第三人称旁观一段事件，检索明确事实 | 2,300 | 历史说 CorpLaw Connect 持续 **one day**，问题询问活动时长；或旧信息被更正后，找回 ClimbFest 最新时间 **Oct 12 2024 at 2 PM** |
| `observation_reflective` | 第三人称叙事，需要从行为/措辞判断高层状态 | 400 | 从一段带时间地点的叙事及四条相关片段判断人物当时最可能是愤怒；或从读书经历判断偏好 Health & Fitness |
| `participation_factual` | 用户与助手直接参与多轮会话，检索双方说过的事实 | 1,600 | 回答“你给我推荐过哪些书/电影/菜”，需要找回助手跨 session 给出的多个推荐；或找回 EcoLaunch 更新后的 **six hundred people**，而非旧数字 |
| `participation_reflective` | 从第一人称对话判断用户状态或偏好 | 400 | 用户说在会议中总被忽视并感到 disheartened，问题要求识别其情绪；或从多次书籍讨论判断用户偏好 Law |

本研究用单一固定 seed `membench-same-scale-v1-20260902`，对每个 question-type × scenario 分层选取 SHA-256 顺序最前的 100 项，共 **47 个 strata、4,700 项、224,042 条候选文本**；其中 4,699 项有完整上游 gold，1 项明确记为 upstream-unresolved。它没有加入额外 noise message（`noise_length=0`），所以测的是同规模受控轨迹内的证据定位，而不是抗人工噪声上限。

题型不仅有 Single-hop，还包括 Multi-hop、Aggregative、Comparative、knowledge updating 和 post-processing。例如问题“持续两周的活动在什么时间举行”需要先从一条消息识别 **CulturaFest lasts two weeks**，再从另一条消息取回 **Saturday 9AM**；这就是检索多跳证据，而不是在单句中找答案。

候选投影只暴露 opaque `candidate_id`、消息顺序和原始文本；`target_step_id`、`ground_truth`、choices、`question_type`、scenario 与 `gold_candidate_ids` 全部留在 custody 中，并被构建器禁止写入投影。检索器仍只返回最多十个 candidate ID。

#### 3.1.4 三组实验共同在测什么

“candidate”不是答案摘要，也不是模型自由生成的记忆，而是一条可以回指原始历史的文本记录；“gold”是数据集作者或协议事先指定的支持证据 ID。一次实验的最小单位因此是：

```text
给定一个问题 + 一组排名器可见的历史消息
→ 输出最多 10 个历史消息 ID
→ 在排名冻结后，由独立 gold 检查正确 ID 是否出现、出现多早。
```

这种设计的重要性在于可诊断性。如果直接只评最终答案，错误可能来自“没找到证据”“找到了但生成模型忽略它”或“模型凭常识猜对了”，三者无法区分。先评证据 ID，可以确认本阶段的方法确实改善了检索，而不是让语言模型更会猜。

### 3.2 样本规模、比较单位与证据边界

| 研究 | 设计与规模 | 主要比较指标 | 证据等级与限制 |
| --- | --- | --- | --- |
| LoCoMo | 1,982 queries、10 conversations、2,815 evidence items；2,806 resolved | question-macro Recall@10 | 阶段一正式决策证据；已知/非盲全量工程研究，不单独承担外部盲测主张 |
| ConvoMem fixed-seed subset | `selection_seed=20260826`；360 total queries，300 positive scored；906,655 candidate texts | persona-macro Recall@10 | 阶段一正式决策证据；事前固定 seed 的 observed-pair 子集；`formal_evidence_eligible=false` 仅表示不是 AERP 完整 census release |
| MemBench same-scale | `seed=membench-same-scale-v1-20260902`；每个 qatype×scenario 100 条，47 strata；4,700 total/4,699 scorable；224,042 candidate texts；无噪声 | Recall@10、MRR@10、NDCG@10 | 阶段一正式决策证据；固定单一 seed 的同规模比较；资格字段不覆盖官方 paper `data2test` 或 exact-unpatched 声明 |

各研究均固定其自身的查询与候选语料，在同一研究内部进行成对比较；因此不应把绝对 Recall 数值跨数据集直接解释为难度排序。跨数据集可比较的是“相对于同一 Original 的方向和幅度”。

## 4. 结果

### 4.1 统一结论表

下表以每项研究预先采用的主要 Recall@10 聚合口径呈现。所有 delta 是相对于同一行 Original 的**百分点**变化，而非相对百分比。

| 研究（主要口径） | Original | Strong Raw | Static P5 | Six-View | P5 − Original | Six − Original |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| LoCoMo（question-macro） | 0.46624 | － | 0.59416 | 0.58868 | +12.79 pp | +12.24 pp |
| ConvoMem fixed-seed subset（persona-macro） | 0.68215 | 0.68006 | 0.75074 | 0.75015 | +6.86 pp | +6.80 pp |
| MemBench same-scale（overall） | 0.85820 | 0.84106 | 0.87890 | 0.86753 | +2.07 pp | +0.93 pp |

在三项研究中，Static P5 对 Original 的 Recall@10 方向均为正；Six-View 亦为正，但在 MemBench 的排序质量指标中没有维持 P5 的优势。Strong Raw 只在两项后续研究中纳入，且两次均低于 Original；它表明“加强原始文本检索”本身不足以解释 P5 的收益。

### 4.2 LoCoMo：整体收益与配对不确定性

| 策略 | question-macro Recall@10 | Δ vs Original | NDCG@10 |
| --- | ---: | ---: | ---: |
| Original | 0.4662386595 | — | 0.3541686215 |
| Static P5 | 0.5941615597 | +0.1279229002 | 0.4262464771 |
| Six-View | 0.5886832773 | +0.1224446177 | 0.3919401169 |

P5 相对 Original 的配对 bootstrap 结果为：问题宏平均 Recall@10 的点估计为 **+0.1279229002**，95% CI 为 **[0.1072268038, 0.1466064751]**；conversation-macro 的点估计为 **+0.1282723091**，95% CI 为 **[0.1077111156, 0.1465305407]**。两项区间均基于 5,000 次重采样且下界大于零。该结果是阶段一正式决策证据；“已知/非盲”只限制把它外推成全新盲测确认，不限制其用于判断方法可行性。

### 4.3 ConvoMem：固定种子子集与难例切片

| 策略 | persona-macro Recall@10 | Δ vs Original |
| --- | ---: | ---: |
| Original | 0.6821488057 | — |
| Strong Raw | 0.6800608670 | −0.0020879387 |
| Static P5 | 0.7507383869 | +0.0685895812 |
| Six-View | 0.7501473704 | +0.0679985647 |

以 10,000 次配对 bootstrap 计算，P5 的区间为 **[0.0362436677, 0.1050661200]**，Six-View 为 **[0.0372564695, 0.1039411833]**，均高于零。hard slice 中，P5 的 delta 为 **+0.1257936508**，Six-View 为 **+0.1292768959**。这些数值与“多表示有助于减少难例表示错配”的解释相符，并足以支持本阶段的可行性判断；其外推范围是固定种子 observed-pair 子集，而不是完整 ConvoMem census。

该 ConvoMem 协议预先指定的 primary current arm 是 Six-View；Six-View 的点提升超过 0.01，且区间下界高于零，因此它通过了这次**固定种子子集协议**的数值门槛。P5 在这里属于 secondary diagnostic。将 P5 选为下一阶段主方法，是综合三项研究之后的跨数据集方法选择，不应倒写成 ConvoMem 的预注册主比较。

### 4.4 MemBench：同规模工程比较与排序质量

| 策略 | Recall@10 | Δ vs Original | MRR@10 | NDCG@10 |
| --- | ---: | ---: | ---: | ---: |
| Original compatibility | 0.8581968085 | — | 0.7503716650 | 0.7377158899 |
| Strong Raw | 0.8410602837 | −0.0171365248 | 0.7538312226 | 0.7261214108 |
| Static P5 | 0.8788983452 | +0.0207015366 | 0.7894678318 | 0.7720903262 |
| Six-View | 0.8675313239 | +0.0093345154 | 0.7462455252 | 0.7339609937 |

P5 在此研究中同时提高 Recall@10、MRR@10 与 NDCG@10。Six-View 虽提高 Recall@10，却低于 Original 的 MRR@10 和 NDCG@10；这正是选择 P5 而非“视图越多越好”的实证理由。P5 primary 与 P5 repeat 的 artifact 为字节级一致，提供了固定输入下实现确定性的直接证据。

## 5. 重复性、失败与恢复透明度

阶段一不是无故障执行；其可信度依赖于失败被保留、根因被明确、恢复身份被记录，而非将恢复产物冒充最初的冻结执行。

1. **ConvoMem 独立 current 角色。** P5 primary 与 repeat 最终产物字节级一致，说明固定候选投影、固定种子与固定实现下的关键排序产物可重复。
2. **ConvoMem Original 的 Chroma 批量写入失败。** 首次 original worker 的单次 `upsert` 超过 Chroma 客户端公布的运行时批量上限。失败目录被封存；产品修复改为读取 live `get_max_batch_size()` 并通过 Chroma 的批处理工具分片，同时保持一次逻辑 upsert 的语义。恢复 runner 另以固定 5,000 条安全分片续跑，并显式绑定修复后的 clean commit/tree；恢复没有改变该实验作为阶段一决策证据的用途。
3. **ConvoMem 终局 crosswalk 恢复。** 第一版终局因 `all_unmatched_crosswalk_contract_mismatch` 被明确 supersede；当前 `final.json` 和 receipt 标记 `repair_version=crosswalk-v2` 与 `gate_outcome=NOT_FORMAL_RECOVERY`，而不是覆盖旧终局后伪装成原始一次通过。
4. **MemBench top-K 校验恢复。** 最终校验初始要求返回数必须等于 `min(K, candidate_count)`，将一个只有四个候选、且实际只有三个已知唯一结果的合法项目误判为失败。修复把等式要求改为上界约束，并加入目标测试；冻结 finalizer 的恢复 receipt 和新的 `final.json` 保留了这一次恢复身份。该变更修复的是验证器对“最多 K 个”的错误解释，而非为了提高任何方法的得分。
5. **协议资格不被恢复掩盖。** ConvoMem 与 MemBench 的 receipt 都原样保留 `formal_evidence_eligible=false`。该字段限制的是 AERP 官方完整复现声明：MemBench 的 Original 为 patched compatibility，且未使用官方 paper `data2test` 样本。它不否定固定设计、固定 seed、同输入成对比较对阶段一方法选择的证明力。

上述记录支持的是可审计工程恢复，而不是“从未发生失败”的叙述。未来正式研究应将 preflight、sidecar/receipt 完整性和真实依赖上限探测纳入冻结流程，以避免耗时计算在最终验证环节才暴露配置缺口。

## 6. 为什么 Static P5 能成功：证据与解释

### 6.1 直接可验证的事实

- P5 在 LoCoMo、ConvoMem 与 MemBench 的主 Recall@10 口径上均高于 Original。
- 在 LoCoMo，P5 的配对 bootstrap 两个主聚合口径的 95% CI 均严格高于零。
- 在 ConvoMem，P5 的 10,000 次 bootstrap 区间严格高于零，且 hard slice 的正增益更大。
- 在 MemBench，P5 在 Recall、MRR 和 NDCG 三项上同时优于 Original，并且 primary/repeat artifact 字节级一致。
- Strong Raw 在 ConvoMem 与 MemBench 的主要 Recall@10 口径上都未超过 Original；Six-View 在 MemBench 的 MRR/NDCG 未超过 P5，亦未超过 Original。

### 6.2 受证据约束的机制解释

这些事实与如下解释一致：单一路径先以 dense 截断 top-10，会丢失词面或字段化重序列化中排名很高的候选；P5 让 raw、observation 和 combo 的多路完整排名以固定权重共同决定 top-10，从而增加正确证据被互补线索救回的机会。由于 observation 的 `summary` 仍是原消息，现有实验**不能**把收益归因于生成式语义摘要；更窄的实现解释是全候选多路融合、字段标签/说话者信息带来的 token 序列变化，以及 lexical/dense 排名的互补。Strong Raw 的结果削弱了“只提高 raw 信号即可获得同等收益”这一更简单的解释；MemBench 上 Six-View 的排序退化则表明 checkpoint 视图并非无条件有益，其融合贡献可能使部分相关证据后移。P5 因而呈现为较小而平衡的固定集成，而不是最大化视图数的方案。

这不是对单个视图或权重的隔离因果估计。候选解释还包括数据集的证据分布、表示生成质量及 RRF 权重之间的交互。下一阶段需以系统消融和全新数据验证来区分这些解释。

## 7. 阶段判定

### 7.1 判定：阶段一完成

建议将 **Static P5** 冻结为下一阶段的主要检索策略；将 **Six-View** 保留为“更多视图”的对照/消融；将 **Strong Raw** 保留为强原始信号对照，而非默认方案。完成依据是：三项研究主 Recall 指标方向一致，LoCoMo 与 ConvoMem 已提供配对不确定性证据，MemBench 已提供多指标收益和字节级重复性证据，并且各恢复过程均保留了可审计身份与资格边界。

### 7.2 外部发表边界：不构成阶段阻塞

阶段一结果可以作为项目报告或论文中的正式实验结果，前提是准确写明抽样设计、seed、恢复身份和比较基线。它不能被改写成“完整复现官方 ConvoMem/MemBench benchmark”。如果未来需要后一种更窄、更强的外部声明，仍缺：

1. 完整 ConvoMem 正式盲测，或等价且计算规模可承受的未污染确认集；本机不应再尝试完整 ConvoMem census；
2. 官方 MemBench paper `data2test` 样本上的完整、冻结、兼容执行；
3. 明确预注册的主要终点、独立确认运行和完整消融；
4. 对 patched compatibility 与原始未改动 baseline 差异的正式复核。

## 8. 下一阶段：RPG 记忆召回

下一阶段应直接从“检索方法可行性”转向“RPG 场景中的记忆召回、权限隔离与叙事效果”：

1. 冻结 P5 权重、候选构造、top-10 预算、随机种子与 receipt 格式，避免进入 RPG 阶段后继续按结果调检索器；
2. 以 Original、Strong Raw 与 Six-View 作为固定对照，在 RPG 受控场景中测量授权记忆 Recall@10、错误角色/分支泄漏、冲突记忆与长时回调；
3. 将检索命中连接到最终回答或叙事行为，分别报告“证据被召回”和“模型正确使用证据”，避免把生成失败误判为检索失败；
4. 把依赖动态批量限制、sidecar 完整性、top-K 语义和可恢复断点写入 preflight，防止运行基础设施再次吞噬实验预算；
5. 完整 ConvoMem census 或官方 MemBench release 若未来需要，可作为独立的外部复现附加研究；它们不再阻塞 RPG 记忆召回阶段。

## 附录 A：终局证据定位

下列定位用于复核，不以路径本身替代 receipt、manifest 和 artifact 的内容验证。

| 研究 | 终局证据 |
| --- | --- |
| LoCoMo | `E:\\MemPalaceWorkspace\\repos\\benchmark-artifacts\\aerp5-v2-real-c7d737e-retry7-work\\custodian-score.json`（文件 SHA-256 `abb9eee3729709ebd244aa29ffc21ad8782f75aae679d31632f15fb2cd425c77`） |
| ConvoMem | `E:\\MemPalaceWorkspace\\experiments\\convomem\\seeded\\934d4b1\\run-20260827T022759Z-62f72939f187603a\\final.json`（文件 SHA-256 `0f65cba0342482a146296c3652aa960fc8aaf7a18f49278af2e2f40a2ee75247`；并由最终 score/receipt 约束） |
| MemBench | `E:\\MemPalaceWorkspace\\experiments\\membench\\same-scale\\20260902T235146Z-seed20260902\\execution\\finalization\\final.json`（文件 SHA-256 `84fd20f780dc416d67d41d5544368bbe02c79fb02ac92eea7990a75aa7e9e9fa`；语义 `final_sha256=6a564ee0be8f9a61b552b00f2657424a320e19857c1654cbb317e4aad32b9e48`） |

**可复现性声明。** 本报告仅概括已保存的终局工件与其 receipt/score 所限定的数值。任何后续再运行都应重新核对输入身份、代码提交、候选投影、artifact 哈希与 `formal_evidence_eligible`，而不能仅凭相同目录名称视为同一研究。

## 附录 B：实现复核入口

| 要核对的主张 | 实现/证据入口 |
| --- | --- |
| 冻结 Original 的过取、top-10 截断、BM25 与 0.6/0.4 融合 | 冻结 commit `87e6f38377b4bee0666374b05df6e14ffd154245` 的 `mempalace/searcher.py`，重点为 `_candidate_pool_size`、`search_memories`、`_finalize_candidate_hits`、`_hybrid_rank` |
| benchmark 如何只创建 drawer collection、写入原文并映射回消息 ID | `benchmarks/aerp7_original_product.py` 与 `benchmarks/aerp5_product_paired_locomo.py` |
| `AuthorizedRetrievalCandidate`、结构化 observation、六视图与加权 RRF | `mempalace_rpg/retrieval.py` 与 `benchmarks/aerp7_convomem_rank.py` |
| LoCoMo 类别、投影/custody 隔离与证据解析 | `benchmarks/locomo_story_protocol.py`、`benchmarks/aerp4_locomo_custody.py` 及官方 `locomo10.json` |
| ConvoMem 固定选择、源清单、serializer 和方法权重 | ConvoMem run root 的 `seeded-study-plan.json`、`protocol.json`、`custody/generation-v3.json` 与各 current worker receipt |
| MemBench 分层抽样、角色计数和最终分数 | MemBench run root 的 `prepared/sampling-receipt.json` 与 `execution/finalization/final.json` |
