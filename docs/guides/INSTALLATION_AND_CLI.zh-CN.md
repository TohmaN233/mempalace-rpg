# 安装与服务入口

[English](INSTALLATION_AND_CLI.md)

使用 `python -m mempalace_rpg.play_memory_server` 启动当前 play-memory v1 服务，RP Host 通过 stdio JSON-RPC 接入。

## Requirements

- Python 3.9 或更高版本。
- Qwen profile 需要预先安装的 `torch`、`transformers` 与符合模型树哈希、revision、设备和 dtype 配置的本地模型。基础依赖不包含这两个可选库；启动时只从指定目录本地加载，不下载模型，也不切换设备或检索算法。

## Installation

从仓库根目录执行：

```bash
python -m pip install -e .
```

## Configuration

play-memory v1 的数据库路径由必需的 `--db` 参数指定；默认 profile 是 `mechanical-bm25-v1`，无需模型参数。要启用 `qwen-summary768-lexical-weave-v1`，还需提供 `--model-dir`、`--model-tree-sha256`、`--model-revision`；可选 `--device`、`--dtype`、`--cuda-cache-policy` 和 `--vector-cache-path`。模型须已经存在于本地目录，vector cache 是本地 passage 向量缓存，不是异步索引服务。

## play-memory v1 stdio JSON-RPC

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

stdout 保留给协议帧，诊断输出使用 stderr。Host 需实现 `play-memory-v1` 合同、批次提交和 scope 绑定。方法名与参数见[参考页](../reference/PLAY_MEMORY_V1.zh-CN.md#play-memory-v1-stdio-json-rpc)。
