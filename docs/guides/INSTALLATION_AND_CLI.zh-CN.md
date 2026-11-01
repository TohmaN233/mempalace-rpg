# 安装与服务入口

[English](INSTALLATION_AND_CLI.md)

`mempalace-rpg` 当前包含两套用途和协议不同的 RPG 记忆服务。`mempalace-rpg` CLI 与 `mempalace-rpg-mcp` MCP 服务访问旧 `RpgMemoryKernel`；`play_memory_server` 是 play-memory v1 的 Host/模型作用域 JSON-RPC 服务。它们使用不同的数据合同和工具名，接入时应按宿主当前实现选择。

## Requirements

- Python 3.9 或更高版本。
- Qwen profile 需要预先安装的 `torch`、`transformers` 与符合模型树哈希、revision、设备和 dtype 配置的本地模型。基础依赖不包含这两个可选库；启动时只从指定目录本地加载，不下载模型，也不切换设备或检索算法。

## Installation

从仓库根目录执行：

```bash
python -m pip install -e .
```

`pyproject.toml` 声明包安装所需依赖及四个命令入口。当前仓库没有单独发布的 PyPI 安装证明。

## Configuration

play-memory v1 的数据库路径由必需的 `--db` 参数指定；默认 profile 是 `mechanical-bm25-v1`，无需模型参数。要启用 `qwen-summary768-lexical-weave-v1`，还需提供 `--model-dir`、`--model-tree-sha256`、`--model-revision`；可选 `--device`、`--dtype`、`--cuda-cache-policy` 和 `--vector-cache-path`。模型须已经存在于本地目录，vector cache 是本地 passage 向量缓存，不是异步索引服务。

旧 kernel 的 `mempalace-rpg` CLI 默认使用用户目录下的 RPG SQLite 文件，也可通过 `--db` 指定；`mempalace-rpg-mcp` 接受 `--db`、`--memo-setting` 和可选 `--palace`。旧配置模板见 [`examples/templates/memo_setting.template.json`](../../examples/templates/memo_setting.template.json)。

## Quick start

安装后可用旧 CLI 创建并检查其数据库：

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 init
mempalace-rpg --db state/rpg-memory.sqlite3 status
```

## 服务入口

### 旧 RPG kernel MCP

由 MCP 客户端以 stdio 传输启动：

```bash
mempalace-rpg-mcp --db state/rpg-memory.sqlite3
```

`--memo-setting` 与 `--palace` 可省略。此 MCP 服务暴露旧 kernel 的 `mempalace_rpg_recall`、`mempalace_rpg_commit_scene`、`mempalace_rpg_get_scene`、`mempalace_rpg_deep_recall` 等工具；完整列表见[合同参考](../reference/PLAY_MEMORY_V1.zh-CN.md#旧版-rpg-kernel-mcp)。

### play-memory v1 stdio JSON-RPC

由 Host 创建子进程并通过 stdin/stdout 逐行交换 JSON-RPC 2.0 请求和响应：

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

Host 启动时通过 `PLAY_MEMORY_HOST_TOKEN`（或 `--host-token`）传入非空的临时共享令牌，并在需要 Host 身份的请求中使用它；这是服务进程凭据，不是模型 API key。PowerShell 可用：

```powershell
$env:PLAY_MEMORY_HOST_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

stdout 保留给协议帧；诊断输出使用 stderr。该服务不是旧 MCP 工具接口，Host 需实现 `play-memory-v1` 合同、批次提交和 scope 绑定。方法名与参数见[参考页](../reference/PLAY_MEMORY_V1.zh-CN.md#play-memory-v1-stdio-json-rpc)。

### 旧 kernel CLI

可用 `mempalace-rpg --help` 查看 CLI 命令。写入结构化场景、查询角色记忆、深度检索、备份、恢复和按时间回滚由旧 CLI 提供；`sync-branch` 专用于依据调用方提供的 pi 分支账本清理旧 kernel 自动提交场景，不负责生成或映射通用游戏存档 worldline。
