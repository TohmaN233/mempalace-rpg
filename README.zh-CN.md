# 面向 RPG 的可追溯长期记忆服务

[English README](README.md)

`mempalace-rpg` 为游戏开发者提供可独立接入的长期记忆服务：保存已接受的来源、事件、角色知识与分支范围，并按已授权范围机械检索和返回可追溯证据。它提供旧版 RPG kernel CLI/MCP 与独立的 play-memory v1 stdio JSON-RPC 合同；召回、资料校验和角色包由服务执行，游戏规则、工作流、模型调用、导演判断和最终状态更新由宿主负责。

## Requirements

- Python 3.9 或更高版本。
- 选择 Qwen 本地向量 profile 时，还需自行准备匹配配置的本地模型，以及已安装的 `torch` 和 `transformers`；它们不属于基础依赖。详见[检索架构](docs/architecture/PLAY_MEMORY_MODEL.zh-CN.md)。

## Installation

在仓库根目录安装：

```bash
python -m pip install -e .
```

## Configuration

play-memory v1 通过 `--db` 指定 SQLite 文件。默认检索 profile 是 `mechanical-bm25-v1`，不加载生成式模型。旧版 kernel 的 `--memo-setting` 可配置哪些游戏领域由旧 API 写入或召回；可从 [`examples/templates/memo_setting.template.json`](examples/templates/memo_setting.template.json) 开始，并先确定宿主已有的权威状态源。

## Quick start

启动当前 play-memory v1 服务（默认机械检索）：

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

它通过 stdin/stdout 接收 JSON-RPC 请求，接入示例见[RP Host 指南](docs/guides/RP_INTEGRATION.zh-CN.md)。

下面是旧版 RPG kernel 的最短 CLI 检查路径：

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 init
mempalace-rpg --db state/rpg-memory.sqlite3 status
```

服务启动命令和两套接口的区别见[安装与服务入口](docs/guides/INSTALLATION_AND_CLI.zh-CN.md)。

## Documentation

- [文档导航与旧内容去向](docs/README.zh-CN.md)
- [安装、CLI、MCP 与 stdio 服务](docs/guides/INSTALLATION_AND_CLI.zh-CN.md)
- [RP Host 接入指南](docs/guides/RP_INTEGRATION.zh-CN.md)
- [play-memory v1 架构](docs/architecture/PLAY_MEMORY_MODEL.zh-CN.md)
- [play-memory v1 与旧 kernel MCP 参考](docs/reference/PLAY_MEMORY_V1.zh-CN.md)
- [RP 接入实验记录](docs/experiments/RP_INTEGRATION_GAP11.zh-CN.md)

## License and attribution

`pyproject.toml` 将本项目声明为 MIT 许可。仓库保留对 MemPalace v3.8.0 的归属信息于 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)，其上游许可文本副本位于 [`docs/legal/MemPalace-LICENSE.txt`](docs/legal/MemPalace-LICENSE.txt)。
