# 维护历史摘要（脱敏归档）

[English](MAINTENANCE_HISTORY.md)

本页归纳 2026-10-31 前后旧 `AGENTS.md` 中的设计主题，供维护者查找变更背景。原文件包含本机绝对路径、私人剧情材料、旧运行预算和短期工作树状态；这些内容没有复制到此公开摘要。摘要不是当前 API、运行政策或性能承诺。

## 主题与当前入口

- 旧 scene/event 与角色可见性设计：当前接口以 [play-memory v1 架构](../architecture/PLAY_MEMORY_MODEL.zh-CN.md) 和 [schema](../../schemas/play-memory-v1.schema.json) 为准。
- 人物目录、tier、dossier、获知与共享 background：按当前 v1 record 和 scope 语义整理在[架构页](../architecture/PLAY_MEMORY_MODEL.zh-CN.md#角色目录dossier-与认知)。
- branch、epoch、scope 和 reroll：按 Host 传入的绑定与 lineage 表述，见 [RP Host 接入指南](../guides/RP_INTEGRATION.zh-CN.md#保存隔离与-reroll)。
- 旧 `RpgMemoryKernel` / AERP-1、AERP-2 与 AERP-3 研究主题：旧 kernel 的 API 与检索边界见[参考页](../reference/PLAY_MEMORY_V1.zh-CN.md#旧版-rpg-kernel-mcp)；历史评测和研究协议保留在[第一阶段证据报告](../phase-1-retrieval-evidence-report.zh-CN.md)与 [paper research program](../paper-research-program.md)。
- Story/Plan2/RP 接入的历史方案：现行模块责任以[接入指南](../guides/RP_INTEGRATION.zh-CN.md)和[职责边界](../RP_FOUNDATION_INTEGRATION_BOUNDARY.zh-CN.md)为准；实验成绩按对应原始研究记录判断。
- 本地 passage 与 Qwen 摘要向量缓存的设计主题：当前范围见[架构页](../architecture/PLAY_MEMORY_MODEL.zh-CN.md#检索路径与数据边界)，不得据缓存性能推导语义召回效果。
- 旧验证清单与模型/数据身份政策：仅适用于各历史研究所引用的冻结协议，不是所有日常文档或游玩接入的通用预算要求。
