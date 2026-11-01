# RP Host integration guide

[简体中文](RP_INTEGRATION.zh-CN.md)

Integrate the memory service as a mechanical component managed by the Host. The Host owns current game facts, sources, save-to-campaign/worldline mapping, branch lineage, and model workflows. The memory core validates batches, maintains traceable records, retrieves by scope, and returns material the caller is allowed to read. The core does not call a generative model to write plot, make semantic judgments, act as a director, or update game state.

## First decide who owns each fact

List the authoritative system for each domain, such as time and maps, combat and HP, inventory, quest stages, NPC profiles, and past events. Do not treat old memories as authoritative for current values already managed by Host state/tools. You can still store their sources, witnesses, historical changes, promises, or what a character once believed. The legacy kernel's `memo_setting.json` controls domain policy for the old API; it does not replace v1 Host authorization or scope binding.

## Write accepted data to the contract

1. The Host maps player input, visible dialogue, author material, and accepted world records to contract records such as `source`, `scene`, `event`, `acquisition`, `profile`, `state`, and `relationship`, while retaining source spans.
2. For each accepted batch, the Host sets `campaignId`, `branchId`, `epoch`, `worldRevision`, and the preceding batch identity. Call `validate_batch`, then submit `apply_batch` through the game's transaction flow and save the receipt.
3. Before each read, the Host creates an `open_scope` binding from the accepted game state for the current turn, including lineage, story order, actor scope, purpose, and exact grants. Records outside the scope do not become authorized merely because a relationship edge connects them.
4. Use the actor directory, dossiers, `search`, `navigate`, and `read` to retrieve the material available in this turn. The Host checks the sources and compiles the final context.
5. Semantic agents in the Host/workflow draft follow-up questions, interpret material, choose director actions, and propose memory changes. The Host reviews and accepts changes, then submits them as a new contract batch. The memory core does not make these decisions for the agents.

See the [play-memory-v1 stdio JSON-RPC reference](../reference/PLAY_MEMORY_V1.md#play-memory-v1-stdio-json-rpc) for the interface, methods, and parameters. RP Foundation integration, LLM-Wiki semantic agents, and game-specific directors belong to the Host/semantic layer; they are not built-in clients or model calls in this repository. See the [integration boundary](../RP_FOUNDATION_INTEGRATION_BOUNDARY.md).

## Save isolation and rerolls

Save slots and story/worldlines are game concepts. The core does not infer them from filenames or save paths. The Host must decide which saves map to the same `campaignId`, how `branchId`, `epoch`, `worldRevision`, and lineage describe a fork, rollback, or restart, and submit the correct values in each scope binding. After a reroll onto another line, the Host selects the corresponding branch and ancestor revision cutoff; it must not use one line's current scope as the default permission for another. Bindings and accepted records remain stored according to the schema; the service does not submit or replay game world transactions for the host.

The legacy kernel's `sync-branch` only uses a caller-provided pi scene ledger to clean up old scenes marked as auto-committed. It is not a save-slot, worldline, or reroll manager for play-memory v1. See the [architecture page](../architecture/PLAY_MEMORY_MODEL.md#scope-save-isolation-and-rerolls) for more on the two paths.

## Character knowledge and shared background

Character profile, state, and relationship records can carry `knownBy`. A `relationship` is a directed relation from `fromActorId` to `toActorId`. Character beliefs use `acquisition` records with evidence and a method of learning; they can mark a fact, hearsay, inference, or mistaken belief. `attitude` can separately record a stance toward a claim. The Host selects the current actor identity and visible scope; the actor directory and dossiers project only records valid within that scope.

`background.appliesTo` accepts explicit actor IDs. The literal `*` means only that Host-accepted shared knowledge or public messages are available by default to characters who appear later. An actor's `exception` can override that background knowledge. This is not a wildcard for `binding.grants` and does not grant permission to read other records.

## Dual-path retrieval and summary inputs

The Qwen profile's `scene-summaries-v1` candidate set includes only scenes within the current authorized scope. The Host provides a navigation summary for each candidate scene. Qwen vectors encode only those summaries and the query. Raw sources, player input, and scene prose remain available to lexical BM25 and exact span reads; they are not placed in this vector candidate store. The Host or its semantic workflow supplies summaries; the core does not generate them. The default mechanical profile uses BM25 and does not silently switch to Qwen.

The local Qwen encoder requires an operator-prepared model directory, exact tree hash and revision, and preinstalled `torch` and `transformers`. The service loads locally from that directory; it does not download or replace the model or automatically switch devices. The vector cache can persist passage vectors, but it does not build an index asynchronously. See [retrieval path and data boundaries](../architecture/PLAY_MEMORY_MODEL.md#retrieval-path-and-data-boundaries) for configuration details.
