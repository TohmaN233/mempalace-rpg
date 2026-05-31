# pi Integration

Install the package and `pi-mcp-extension`, then expose the MCP server from your game package.

```bash
pip install -e /path/to/mempalace-rpg
pi install npm:pi-mcp-extension
```

## `.pi/mcp.json`

```json
{
  "settings": {
    "toolPrefix": "mcp",
    "requestTimeoutMs": 30000,
    "maxRetries": 5
  },
  "mcpServers": {
    "rpg": {
      "transport": "stdio",
      "command": "bash",
      "args": ["scripts/run-rpg-mcp.sh"],
      "lifecycle": "eager"
    }
  }
}
```

With `toolPrefix: "mcp"` and server name `rpg`, short aliases become:

- `mcp_rpg_status`
- `mcp_rpg_recall`
- `mcp_rpg_commit_scene`
- `mcp_rpg_deep_recall`
- `mcp_rpg_get_scene`
- `mcp_rpg_upsert_profile`
- `mcp_rpg_import_taverndb`

## Launcher script

See `examples/dest-poet/run-rpg-mcp.sh`. Minimal form:

```bash
#!/usr/bin/env bash
set -euo pipefail
exec mempalace-rpg-mcp \
  --db "${MEMPALACE_RPG_DB:-state/rpg-memory.sqlite3}" \
  --memo-setting "${MEMPALACE_RPG_MEMO_SETTING:-memo_setting.json}" \
  --palace "${MEMPALACE_RPG_PALACE:-state/rpg-palace}"
```

Omit `--palace` or set `MEMPALACE_RPG_PALACE=off` if you only want SQLite.

## GM prompt rule

Use `examples/templates/gm-memory-rules.template.md`. The critical rule is: first decide which domains are host-owned, then tell the GM how to use memory without overriding them.

## Automatic transcript commit

A host extension can automatically commit visible in-world turns. `examples/dest-poet/auto-commit.ts` shows one deterministic approach:

1. Exclude engineering/meta/tool-debug conversations.
2. Build a transcript from the player and GM visible text.
3. Collect current participants and mentioned known entities.
4. Call `mempalace-rpg commit-scene` with a conservative `scene_transcript` event.
5. Let the GM additionally commit high-value structured events.

This pattern keeps raw history complete while preserving explicit structure for promises, secrets, faction shifts, and other durable facts.
