# play-memory v1 and legacy kernel MCP reference

[简体中文](PLAY_MEMORY_V1.zh-CN.md)

The `play-memory-v1` schema is the contract source for the current Host-scoped service. The legacy `mempalace-rpg-mcp` is a separate MCP API based on `RpgMemoryKernel`. Methods, tools, and record fields are not interchangeable between the interfaces.

## play-memory v1 stdio JSON-RPC

Service entry point:

```bash
export PLAY_MEMORY_HOST_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

The Host supplies a nonempty temporary shared token through `PLAY_MEMORY_HOST_TOKEN` (or `--host-token`) and uses it in requests requiring Host identity. This is a service-process credential, not a model API key. In PowerShell:

```powershell
$env:PLAY_MEMORY_HOST_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
python -m mempalace_rpg.play_memory_server --db state/play-memory.sqlite3
```

The service uses JSON-RPC 2.0 JSON Lines, with one request line per response line. `stdout` is reserved for protocol frames; diagnostics go to `stderr`. The schema, record definitions, and field validation are maintained in [schemas/play-memory-v1.schema.json](../../schemas/play-memory-v1.schema.json) and [mempalace_rpg/play_memory_protocol.py](../../mempalace_rpg/play_memory_protocol.py).

| Method | Purpose |
| --- | --- |
| `capabilities` | Query protocol, profiles, representations, scope, and vector modes |
| `validate_batch`, `apply_batch`, `receipt` | Validate and submit Host-accepted batches, then inspect submission receipts |
| `open_scope`, `close_scope` | Create and close an in-process scope from a Host binding |
| `actor_directory`, `resolve_actors`, `actor_cues`, `actor_dossiers`, `actor_packs` | List, resolve, and project actor profiles within a scope |
| `search`, `navigate`, `navigation_cards`, `read`, `warm_scope` | Search, navigate the graph, read, and warm within a scope |
| `memory_graph`, `memory_archive` | Inspect the event relationship graph and read or restore records eligible under lifecycle policy |

The schema's `$defs` contains the exact field constraints for common `sourceSpan`, batch, binding, and each RPC. A binding includes at least `campaignId`, `branchId`, `epoch`, `worldRevision`, `storyOrder`, `checkpointId`, `materialRevision`, `configHash`, purpose, actorIds, grants, and lineage. See the [architecture page](../architecture/PLAY_MEMORY_MODEL.md) for record types and the meaning of `knownBy`, `appliesTo`, and `fromActorId`/`toActorId`.

The next batch names the current semantic head in `predecessorBatchId`. Mechanical Host edits can advance World without writing memory. For an existing branch, `previousWorldRevision` may lie between the semantic head revision and the new `worldRevision`, with the latter strictly greater. These gaps need no empty batches. A different predecessor or backwards revision remains invalid.

### Qwen profile parameters

The service defaults to `--profile mechanical-bm25-v1`. The Qwen profile uses:

- `--profile qwen-summary768-lexical-weave-v1`
- Required: `--model-dir`, `--model-tree-sha256`, `--model-revision`
- Optional: `--device`, `--dtype`, `--cuda-cache-policy`, `--vector-cache-path`

Qwen operation requires the operator to preinstall `torch` and `transformers` and provide hash-verified model files in the model directory. Loading is local-only; there is no download, model replacement, device fallback, or lexical-only recovery. See the architecture page for the candidate and vector-input boundaries of `scene-summaries-v1`.

## Legacy RPG kernel MCP

Legacy service entry point:

```bash
mempalace-rpg-mcp --db state/rpg-memory.sqlite3 --memo-setting memo_setting.json --palace state/rpg-palace
```

The MCP protocol exposes the following main tool names through `initialize`, `tools/list`, and `tools/call`:

| Tool | Purpose |
| --- | --- |
| `mempalace_rpg_init`, `mempalace_rpg_status` | Initialize and inspect legacy kernel status |
| `mempalace_rpg_upsert_profile` | Write a legacy `ProfileCard` |
| `mempalace_rpg_commit_scene` | Write a legacy-format scene transcript and extracted events |
| `mempalace_rpg_recall`, `mempalace_rpg_deep_recall` | Return a legacy `MemoryPack` or reread authorized source snippets |
| `mempalace_rpg_get_scene` | Read permission-checked snippets by scene identifier |
| `mempalace_rpg_import_taverndb` | Import legacy TavernDB/ChatSheets data |

Legacy recall requires `campaign_id`, `actor_id`, and `query`; scene, actor, and visibility fields use their own snake_case JSON contract. `mempalace_rpg_list_memories`, `mempalace_rpg_list_world_facts`, and `mempalace_rpg_list_actor_beliefs` are listed in the server but are disabled local-admin surfaces and are rejected over MCP.

The legacy kernel CLI also provides maintenance commands such as `backup`, `restore`, `delete-after`, `delete-scenes`, and `sync-branch`. These are not play-memory v1 stdio methods. See [pi-integration.md](../pi-integration.md) for the legacy Pi example and [taverndb-import.md](../taverndb-import.md) for import material.

Actor dossiers also return `memoryHierarchy` with actor-owned event edges, summaries, heart significance, locks, folding state and source ranges. `actor_dossiers` accepts `includeSourceExcerpts=false` to omit original bodies. Archive age no longer causes deletion. Legacy `expiresAfterFloor` remains a compatibility field.
