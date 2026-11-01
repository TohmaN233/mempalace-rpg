# Traceable long-term memory for RPGs

[简体中文 README](README.zh-CN.md)

`mempalace-rpg` gives game developers a standalone long-term memory service: it stores accepted sources, events, character knowledge, and branch scope, then mechanically retrieves and returns traceable evidence within the authorized scope. Adapted from [MemPalace](https://github.com/MemPalace/mempalace), it provides the legacy RPG kernel CLI/MCP and a separate play-memory v1 stdio JSON-RPC contract. The service handles recall, record validation, and actor packs; the host handles game rules, workflows, model calls, director decisions, and final state updates.

## Requirements

- Python 3.9 or later.
- To select the local Qwen vector profile, prepare a matching local model and install `torch` and `transformers` yourself; they are not base dependencies. See the [retrieval architecture](docs/architecture/PLAY_MEMORY_MODEL.md).

## Installation

Install from the repository root:

```bash
python -m pip install -e .
```

## Configuration

play-memory v1 uses `--db` to specify the SQLite file. Its default retrieval profile is `mechanical-bm25-v1` and does not load a generative model. On the legacy kernel, `--memo-setting` configures which game domains the old API writes or recalls. Start with [examples/templates/memo_setting.template.json](examples/templates/memo_setting.template.json), and first identify the authoritative state source already maintained by the host.

## Quick start

Start the current play-memory v1 service with the default mechanical retrieval profile:

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

It accepts JSON-RPC requests over stdin/stdout; see the [RP Host guide](docs/guides/RP_INTEGRATION.md) for integration.

The shortest CLI check for the legacy RPG kernel is:

```bash
mempalace-rpg --db state/rpg-memory.sqlite3 init
mempalace-rpg --db state/rpg-memory.sqlite3 status
```

See [installation and service entry points](docs/guides/INSTALLATION_AND_CLI.md) for server commands and the differences between the two interfaces.

## Documentation

- [Documentation map and where older material lives](docs/README.md)
- [Installation, CLI, MCP, and stdio service](docs/guides/INSTALLATION_AND_CLI.md)
- [RP Host integration guide](docs/guides/RP_INTEGRATION.md)
- [play-memory v1 architecture](docs/architecture/PLAY_MEMORY_MODEL.md)
- [play-memory v1 and legacy kernel MCP reference](docs/reference/PLAY_MEMORY_V1.md)
- [RP integration case study](docs/experiments/RP_INTEGRATION_GAP11.md)

## License and attribution

`pyproject.toml` declares this project under the MIT license. Attribution to MemPalace v3.8.0 is in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md); a copy of its upstream license is at [docs/legal/MemPalace-LICENSE.txt](docs/legal/MemPalace-LICENSE.txt).
