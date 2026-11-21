# Installation and service entry points

[简体中文](INSTALLATION_AND_CLI.zh-CN.md)

Start play-memory v1 with `python -m mempalace_rpg.play_memory_server`. RP Host communicates over stdio JSON-RPC.

## Requirements

- Python 3.9 or later.
- The Qwen profile requires preinstalled `torch` and `transformers`, plus a local model matching the configured model-tree hash, revision, device, and dtype. These optional libraries are not base dependencies. At startup the service loads only from the specified local directory; it does not download a model or switch devices or retrieval algorithms.

## Installation

Run from the repository root:

```bash
python -m pip install -e .
```

## Configuration

The required `--db` option specifies the play-memory v1 database path. The default profile is `mechanical-bm25-v1` and needs no model arguments. To enable `qwen-summary768-lexical-weave-v1`, also provide `--model-dir`, `--model-tree-sha256`, and `--model-revision`. Optional arguments are `--device`, `--dtype`, `--cuda-cache-policy`, and `--vector-cache-path`. The model must already exist in a local directory. The vector cache stores local passage vectors; it is not an asynchronous indexing service.

## play-memory v1 stdio JSON-RPC

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

The service writes protocol frames to stdout and diagnostics to stderr. The Host implements the `play-memory-v1` contract, batch submission, and scope binding. See method names and parameters in the [reference](../reference/PLAY_MEMORY_V1.md#play-memory-v1-stdio-json-rpc).
