# 文档导航

[English](README.md)

本目录区分当前 play-memory v1 合同、兼容用的旧 RPG kernel，以及研究与接入记录。先看[安装与服务入口](guides/INSTALLATION_AND_CLI.zh-CN.md)，再按需要阅读[接入指南](guides/RP_INTEGRATION.zh-CN.md)、[架构](architecture/PLAY_MEMORY_MODEL.zh-CN.md)和[合同参考](reference/PLAY_MEMORY_V1.zh-CN.md)。

## 当前文档

| 类别 | 文档 | 用途 |
| --- | --- | --- |
| 指南 | [安装与服务入口](guides/INSTALLATION_AND_CLI.zh-CN.md) | Python 安装、旧 kernel CLI/MCP 与 play-memory v1 stdio 启动 |
| 指南 | [RP Host 接入](guides/RP_INTEGRATION.zh-CN.md) | Host 数据映射、scope、语义代理和分支职责 |
| 架构 | [play-memory v1 模型](architecture/PLAY_MEMORY_MODEL.zh-CN.md) | 来源、角色认知、图导航、生命周期、隔离和 Qwen 双路检索 |
| 参考 | [play-memory v1 与旧 kernel MCP](reference/PLAY_MEMORY_V1.zh-CN.md) | 两种协议的方法、参数和边界 |
| 实验 | [RP 接入缺口 11 题记录](experiments/RP_INTEGRATION_GAP11.zh-CN.md) | 一组冻结真实上下文案例的材料召回与最终回答结果 |

## 保留的已有资料

- [RPG Memory Design](memory-design.md) 保留为旧 `RpgMemoryKernel` 兼容层的概念说明；它不定义 play-memory v1。
- [pi Integration](pi-integration.md) 保留为旧 MCP 的 pi 接入说明。
- [TavernDB 导入](taverndb-import.md) 保留为旧 kernel 的迁移说明。
- [RP Foundation 职责边界](RP_FOUNDATION_INTEGRATION_BOUNDARY.zh-CN.md) 汇总当前 Host 边界，并链接到接入指南。
- [第一阶段检索方法证据报告](phase-1-retrieval-evidence-report.zh-CN.md) 与 [paper research program](paper-research-program.md) 保留原研究语言和范围，属于历史研究材料，不是当前产品质量承诺。
- [维护历史摘要](archive/MAINTENANCE_HISTORY.zh-CN.md) 收录旧 `AGENTS.md` 的设计主题；本机路径、私人剧情正文、旧运行预算与工作树状态已从公开归档中删除。

## 旧主题去向

| 旧主题 | 当前位置 | 处理方式 |
| --- | --- | --- |
| README 中散落的安装和 CLI 命令 | [安装与服务入口](guides/INSTALLATION_AND_CLI.zh-CN.md) | 整理为旧 kernel CLI、旧 MCP 和 v1 stdio 三条彼此独立的入口 |
| scene、event、belief、权限和关系概念 | [play-memory v1 架构](architecture/PLAY_MEMORY_MODEL.zh-CN.md) | 以当前 `play-memory-v1` schema 为准；旧 kernel 表名另行标注 |
| 保存分支、reroll 和宿主职责 | [RP Host 接入](guides/RP_INTEGRATION.zh-CN.md) 与 [职责边界](RP_FOUNDATION_INTEGRATION_BOUNDARY.zh-CN.md) | 明确由 Host 映射存档与 worldline，并提交正确的 branch、epoch 和 lineage |
| README 中的 AERP 检索研究细节 | [第一阶段证据报告](phase-1-retrieval-evidence-report.zh-CN.md) 与 [research program](paper-research-program.md) | 保留为历史研究；不作为当前 play-memory 成绩 |
| 旧 AGENTS 中的角色目录、dossier、后台知识和生命周期笔记 | [play-memory v1 架构](architecture/PLAY_MEMORY_MODEL.zh-CN.md) | 按当前合同重述；归档只保留历史主题，不沿用旧计划状态 |
| 已接受的 F150/r433 缺口测试 | [RP 接入实验记录](experiments/RP_INTEGRATION_GAP11.zh-CN.md) 及其[聚合数据](experiments/fated-poem-gap11-20261031.json) | 保留材料分数与最终回答分数的区别，并注明单案例边界 |
| 旧 AGENTS 的实现路线与研究计划 | [维护历史摘要](archive/MAINTENANCE_HISTORY.zh-CN.md) | 汇总保留，删除失效本地路径、私人正文和过期预算/工作树要求 |
