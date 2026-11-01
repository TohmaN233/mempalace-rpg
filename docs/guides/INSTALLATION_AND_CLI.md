# Installation and service entry points

[简体中文](INSTALLATION_AND_CLI.zh-CN.md)

`mempalace-rpg` currently includes two RPG memory services with different purposes and protocols. The `mempalace-rpg` CLI and `mempalace-rpg-mcp` MCP service access the legacy `RpgMemoryKernel`; `play_memory_server` is the Host/model-scoped JSON-RPC service for play-memory v1. They use different data contracts and tool names, so choose the one that matches the host implementation.

## Requirements

- Python 3.9 or later.
- The Qwen profile requires preinstalled `torch` and `transformers`, plus a local model matching the configured model-tree hash, revision, device, and dtype. These optional libraries are not base dependencies. At startup the service loads only from the specified local directory; it does not download a model or switch devices or retrieval algorithms.

## Installation

Run from the repository root:

```bash
python -m pip install -e .
```

`pyproject.toml` declares the installation dependencies and four command entry points. The current repository has no separate PyPI installation evidence.

## Configuration

The required `--db` option specifies the play-memory v1 database path. The default profile is `mechanical-bm25-v1` and needs no model arguments. To enable `qwen-summary768-lexical-weave-v1`, also provide `--model-dir`, `--model-tree-sha256`, and `--model-revision`. Optional arguments are `--device`, `--dtype`, `--cuda-cache-policy`, and `--vector-cache-path`. The model must already exist in a local directory. The vector cache stores local passage vectors; it is not an asynchronous indexing service.

The legacy `mempalace-rpg` CLI uses an RPG SQLite file in the user's home directory by default, or a path supplied with `--db`. `mempalace-rpg-mcp` accepts `--db` and `--memo-setting`, and optionally `--palace`. See the legacy configuration template at [examples/templates/memo_setting.template.json](../../examples/templates/memo_setting.template.json).

## Quick start

After installation, use the legacy CLI to create and inspect its database:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 init
mempalace-rpg --db state/rpg-memory.sqlite3 status
```

## Service entry points

### Legacy RPG kernel MCP

An MCP client starts the service over stdio:

```bash
mempalace-rpg-mcp --db state/rpg-memory.sqlite3
```

`--memo-setting` and `--palace` may be omitted. This MCP service exposes legacy kernel tools such as `mempalace_rpg_recall`, `mempalace_rpg_commit_scene`, `mempalace_rpg_get_scene`, and `mempalace_rpg_deep_recall`. See the full list in the [contract reference](../reference/PLAY_MEMORY_V1.md#legacy-rpg-kernel-mcp).

### play-memory v1 stdio JSON-RPC

The Host creates a child process and exchanges JSON-RPC 2.0 requests and responses one line at a time over stdin/stdout:

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

The Host supplies a nonempty temporary shared token through `PLAY_MEMORY_HOST_TOKEN` (or `--host-token`) and uses it in requests requiring Host identity. This is a service-process credential, not a model API key. In PowerShell:

```powershell
$env:PLAY_MEMORY_HOST_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

stdout is reserved for protocol frames; diagnostics go to stderr. This is not the legacy MCP tool interface. The Host must implement the `play-memory-v1` contract, batch submission, and scope binding. See method names and parameters in the [reference](../reference/PLAY_MEMORY_V1.md#play-memory-v1-stdio-json-rpc).

### Legacy kernel CLI

Run `mempalace-rpg --help` to see CLI commands. The legacy CLI provides structured scene writes, character-memory queries, deep recall, backup, restore, and time-based rollback. `sync-branch` uses the caller-provided pi branch ledger to clean up legacy auto-committed scenes; it does not generate or map a general game-save worldline.
