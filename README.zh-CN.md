# 面向 RPG 的可追溯长期记忆服务

[English README](README.md)

RP Host 当前使用 **play-memory v1** stdio JSON-RPC 接入 MemPalace-RPG。

- **事件记忆**按延续关系和子事件组织，可将较早的细节折叠到摘要下，保留已采纳原文的回读引用。
- **角色记忆**随角色实际获知的事件组织。角色档案可展开个人记忆、重要度与获准的原文摘录。个人重要度为一至三心。
- **Host**负责语义摘要、模型调用、游戏状态和工作流。MemPalace-RPG 按存档、世界线和角色权限保存、校验并检索已采纳记录。

## 环境要求

- Python 3.9 或更高版本。
- 选择 Qwen 本地向量 profile 时，还需自行准备匹配配置的本地模型，以及已安装的 `torch` 和 `transformers`；它们不属于基础依赖。详见[检索架构](docs/architecture/PLAY_MEMORY_MODEL.zh-CN.md)。

## 安装

在仓库根目录安装：

```bash
python -m pip install -e .
```

## 配置

play-memory v1 通过 `--db` 指定 SQLite 文件。默认检索 profile 是 `mechanical-bm25-v1`，不加载生成式模型。

## 启动当前服务

启动当前 play-memory v1 服务（默认机械检索）：

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

它通过 stdin/stdout 接收 JSON-RPC 请求，接入示例见[RP Host 指南](docs/guides/RP_INTEGRATION.zh-CN.md)。

## 文档

- [文档导航与旧内容去向](docs/README.zh-CN.md)
- [安装与 stdio 服务](docs/guides/INSTALLATION_AND_CLI.zh-CN.md)
- [RP Host 接入指南](docs/guides/RP_INTEGRATION.zh-CN.md)
- [play-memory v1 架构](docs/architecture/PLAY_MEMORY_MODEL.zh-CN.md)
- [play-memory v1 协议参考](docs/reference/PLAY_MEMORY_V1.zh-CN.md)
- [兼容工具与研究索引](docs/README.zh-CN.md#兼容与研究)

## 许可与归属

`pyproject.toml` 将本项目声明为 MIT 许可。仓库保留对 MemPalace v3.8.0 的归属信息于 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)，其上游许可文本副本位于 [`docs/legal/MemPalace-LICENSE.txt`](docs/legal/MemPalace-LICENSE.txt)。
