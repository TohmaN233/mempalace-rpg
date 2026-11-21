# Traceable long-term memory for RPGs

[简体中文 README](README.zh-CN.md)

MemPalace-RPG is a role-playing memory service adapted from [MemPalace](https://github.com/MemPalace/mempalace). RP Host integrations use **play-memory v1** over stdio JSON-RPC.

- **Event memory** groups continuations and subevents, folding older details below summaries while retaining accepted source references.
- **Character memory** follows each character's actual knowledge of an event. Dossiers can expand to personal memories, one-to-three-heart significance, and authorized source excerpts.
- **The Host** owns semantic summaries, model calls, game state, and workflow. MemPalace-RPG stores, validates, and retrieves accepted records within the requested save, worldline, and actor authority.

## Requirements

- Python 3.9 or later.
- To select the local Qwen vector profile, prepare a matching local model and install `torch` and `transformers` yourself; they are not base dependencies. See the [retrieval architecture](docs/architecture/PLAY_MEMORY_MODEL.md).

## Installation

Install from the repository root:

```bash
python -m pip install -e .
```

## Configuration

play-memory v1 uses `--db` to specify the SQLite file. Its default retrieval profile is `mechanical-bm25-v1` and does not load a generative model.

## Quick start

Start the current play-memory v1 service with the default mechanical retrieval profile:

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

It accepts JSON-RPC requests over stdin/stdout; see the [RP Host guide](docs/guides/RP_INTEGRATION.md) for integration.

## Documentation

- [Documentation map and where older material lives](docs/README.md)
- [Installation, CLI, MCP, and stdio service](docs/guides/INSTALLATION_AND_CLI.md)
- [RP Host integration guide](docs/guides/RP_INTEGRATION.md)
- [play-memory v1 architecture](docs/architecture/PLAY_MEMORY_MODEL.md)
- [play-memory v1 protocol reference](docs/reference/PLAY_MEMORY_V1.md)
- [RP integration evaluation](docs/experiments/RP_INTEGRATION_GAP11.md)

## License and attribution

This project is licensed under MIT. See the [third-party notice](THIRD_PARTY_NOTICES.md) for upstream attribution.
