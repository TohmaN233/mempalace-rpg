# pi integration: legacy RPG kernel MCP

This page covers the legacy `mempalace-rpg-mcp` server. It is a separate contract from the current play-memory v1 stdio JSON-RPC service; see the [current interface reference](reference/PLAY_MEMORY_V1.zh-CN.md) and [RP Host guide](guides/RP_INTEGRATION.zh-CN.md).

Install the existing pi MCP extension if it is not already available:

```bash
pi install npm:pi-mcp-extension
```

Configure the pi MCP client to launch the installed command `mempalace-rpg-mcp` over stdio. The server accepts `--db`, optional `--memo-setting`, and optional `--palace`; for example:

```bash
mempalace-rpg-mcp --db state/rpg-memory.sqlite3
```

The server's actual tools include `mempalace_rpg_status`, `mempalace_rpg_recall`, `mempalace_rpg_commit_scene`, `mempalace_rpg_deep_recall`, and `mempalace_rpg_get_scene`. Their argument names and behavior belong to the legacy kernel MCP schema. They should not be renamed or treated as aliases for play-memory v1 methods.

For roleplay integrations, decide which system owns each game domain before exposing tools. The generic domain-policy template is [`examples/templates/memo_setting.template.json`](../examples/templates/memo_setting.template.json); use it only with the legacy kernel. The Host remains responsible for model calls, semantic decisions, director behavior, and accepted world updates.
