# MemPalace-RPG and RP Host responsibility boundary

[简体中文](RP_FOUNDATION_INTEGRATION_BOUNDARY.zh-CN.md)

This repository provides RPG memory storage, validation, scope, and mechanical-retrieval contracts. Current play-memory v1 is implemented by [schemas/play-memory-v1.schema.json](../schemas/play-memory-v1.schema.json), [play_memory_server.py](../mempalace_rpg/play_memory_server.py), and [play_memory.py](../mempalace_rpg/play_memory.py). The legacy `RpgMemoryKernel` MCP is a separate compatibility interface.

## Responsibility table

| Work | MemPalace-RPG | RP Host / RP Foundation / LLM-Wiki semantic layer |
| --- | --- | --- |
| Memory contract and sources | Validates batches; stores records, source spans, source identities, and receipts | Maps game, author, or player material to the contract and decides which facts have been accepted |
| Recall and actor views | Filters by binding and lineage; performs mechanical retrieval, navigates explicit graph relations, builds directories/dossiers, and reads authorized representations | Selects the current actor, query, purpose, and game scope; checks results and compiles context for the model |
| Semantic judgment and directing | Does not call generative models, interpret semantics, decide plot, or choose NPC actions | Runs workflows and model calls; semantic agents ask questions, interpret evidence, plan director actions, and draft memory changes |
| Game truth and updates | Accepts Host-approved batches and returns receipts | RP Host manages rules, the world, saves, characters, and final game state; after review, it accepts updates proposed by the semantic layer |
| Branches and saves | Isolates reads using Host-provided campaign/branch/epoch/revision/lineage | Maintains save-to-campaign/worldline mapping and chooses correct bindings and submission order for forks, rerolls, and rollbacks |

During integration, the core can run a local embedding encoder for configured mechanical vector ranking. This does not mean it calls a director model, runs semantic agents, or owns RP Foundation's model workflow. LLM-Wiki and specific semantic agents are recorded as Host-side integration responsibilities, not runtime dependencies of this repository.

## Two service contracts

- **play-memory v1**: The Host submits accepted batches over stdio JSON-RPC, binds a read scope, and searches or reads authorized representations. Records and fields are defined by the v1 schema.
- **Legacy RPG kernel MCP**: Legacy `mempalace-rpg-*` CLI/MCP tools for `RpgMemoryKernel`, with their own scene, profile, ACL, and JSON fields.

The two interfaces do not share interchangeable tool names, data structures, or save management. See the [RP Host guide](guides/RP_INTEGRATION.md) for integration steps and the [protocol reference](reference/PLAY_MEMORY_V1.md) for the methods.

## Scope and final context

The Host decides which game snapshot a request represents and provides `campaignId`, `branchId`, `epoch`, `worldRevision`, purpose, actor IDs, exact grants, and ancestor lineage in the binding. The core returns valid records within those mechanical boundaries; graph adjacency, matching actor names, or shared background do not automatically grant permission to other records.

The Host/workflow selects the correct context and actor for the model, handles follow-up questions and directing, reviews changes, and produces the final output. A memory pack or read handle only describes the material the service returned under the binding. It does not replace Host validation or automatically become the plot the model should follow.
