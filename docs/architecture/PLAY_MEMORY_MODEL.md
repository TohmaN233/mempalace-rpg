# play-memory v1 architecture

[简体中文](PLAY_MEMORY_MODEL.zh-CN.md)

This page describes the current `play-memory-v1` schema and `PlayMemoryStore`. These are separate contracts from the legacy `RpgMemoryKernel` tables, CLI, and MCP. See the [contract reference](../reference/PLAY_MEMORY_V1.md#legacy-rpg-kernel-mcp) for the legacy entry points.

## Accepted sources, records, and event graph

The Host submits `play-memory-v1` batches with immutable identities. A batch carries `campaignId`, `branchId`, `epoch`, `worldRevision`, the previous batch, and `records`. Each record retains its own `id`, `order`, validity period, and source span. The service accepts and indexes content provided by the Host; it does not judge the semantic truth of player input or director text.

Common record types include:

| Type | Purpose |
| --- | --- |
| `source`, `scene` | Store source text, scene content, and exact source spans |
| `event`, `link` | Store events and explicit actor-to-event links |
| `acquisition`, `attitude` | Record how an actor learned a claim, its evidence, and the actor's stance toward it |
| `profile`, `state` | Store a character profile or state fields, optionally using `knownBy` to identify who knows them |
| `relationship` | Store a directed `fromActorId` → `toActorId` relation, optionally using `knownBy` to restrict who knows it |
| `background`, `exception` | Represent shared background knowledge and an individual character's exception |
| `date` | Store dates and calendar rules supplied by the Host |

`sourceSpans` locates source text by source ID, version, UTF-8 byte range, and hash. `acquisition` connects claims to evidence references; `memory_graph` can navigate stored relationships among scenes, events, actors, and sources. Graph connections do not expand scope grants.

## Actor directory, dossiers, and cognition

The current actor catalog accepts four tiers: `core`, `major`, `recurring`, and `ambient`. The Host submits the roster and overrides from its own character state; the directory and `actor_dossiers` build projections from records visible in the current scope. Tier and importance policies affect character-detail selection and budgets; they do not call a model to generate a new profile or infer what an actor knows.

Actor cognition is represented through source-traceable records:

- `profile`, `state`, and `relationship` can use `knownBy` to specify which actors know the content.
- `acquisition.beliefKind` distinguishes `fact`, `hearsay`, `inference`, and `mistaken-belief`; `method`, `basisIds`, and `sourceSpans` retain how it was learned and its evidence.
- `attitude` records `believe`, `doubtful`, or `disbelieve` for an acquisition; it does not turn an attitude into a world fact.
- `link` connects an actor and an event. Relationship edges are directed and do not imply a reverse relation.

These records let the Host distinguish “what happened,” “what someone knows,” and “what someone believes.” The Host still selects the final speaker and must use the correct actor ID and scope.

## Scope, save isolation, and rerolls

The Host submits a read binding through `open_scope`. The binding includes `campaignId`, `branchId`, `epoch`, `worldRevision`, `storyOrder`, `checkpointId`, `materialRevision`, `configHash`, purpose, actorIds, exact grants, and lineage. The service constructs the visible records using each ancestor's revision cutoff in the lineage. Branch identity is part of the binding.

Save slots and worldlines are not entities the database identifies automatically. The Host must map saves to campaigns, branches, and epochs; decide when to open a new line; choose the ancestor and cutoff revision for a reroll; and keep the next submission and scope consistent. Scope grants authorize exact record identities only; actor neighbors, graph edges, or wildcard background do not confer access to additional records.

The legacy kernel's `sync-branch` uses a scene ledger provided by the Host or pi extension to clean up legacy auto-committed scenes. It does not handle play-memory v1 save mapping, epoch generation, or worldline rerolls.

## Background knowledge and exceptions

`background.appliesTo` can list explicit actor IDs, or use the literal `*` to mean that Host-accepted shared knowledge or public messages are available by default to characters who appear later. An actor can use an `exception` referring to that background to indicate that the knowledge is unknown or has been learned. This rule affects only the background-record projection; it is not wildcard authorization through scope grants.

## Retrieval path and data boundaries

The default `mechanical-bm25-v1` uses mechanical retrieval and does not load Qwen. With `qwen-summary768-lexical-weave-v1`, the Host can open a scope using `scene-summaries-v1`: candidates must be authorized scenes, and the Host must provide a navigation summary for each candidate. The Qwen profile uses the summaries as vector input, runs lexical BM25 over raw scene/source text, and interweaves the results according to its profile.

Player input, raw source text, and scene prose can therefore support lexical recall or exact span reads, but are not passage embeddings for this Qwen profile. Vector retrieval uses per-scene/per-floor navigation summaries supplied by the Host and the current query. The core does not call a generative model to create summaries or silently fall back when a candidate lacks one.

The operator must preinstall `torch` and `transformers` for the local Qwen encoder and provide a local model directory, exact tree hash, and revision; loading uses local files. An optional SQLite vector cache persists passage vectors but is not an asynchronous index, candidate-discovery mechanism, or authorization mechanism.

## Event archives and 100-floor expiry

Lifecycle behavior creates archive projections from events explicitly included in lifecycle control. Accepted source/event records remain append-only history. Archive payloads can be read or restored through `memory_archive` and may be removed after expiry; expiry of an archive payload does not imply that its source record was deleted.

A floor is a zero-based sequence number for distinct scenes visible in the current lineage, not a floor in a game map. An archive initially remains valid for 100 floors after its archive floor; reads may extend its readable period. The exact archive/restore behavior, eligible event set, and expiry boundaries are defined by [play_memory_lifecycle.py](../../mempalace_rpg/play_memory_lifecycle.py) and [play-memory-v1.schema.json](../../schemas/play-memory-v1.schema.json).

## Supported scope

| Area | Current meaning |
| --- | --- |
| play-memory v1 | Host-scoped contract defined by `schemas/play-memory-v1.schema.json`, the Python protocol, and the stdio service |
| Legacy RPG kernel | Compatibility interface of `RpgMemoryKernel`, the legacy CLI, and `mempalace-rpg-mcp`; separate from the v1 schema |
| Qwen profile | Configurable local summary-vector plus raw-text lexical navigation path; does not represent full Qwen17 or general semantic recall quality |
| RP Foundation / LLM-Wiki | Integration-side workflows, semantic agents, director, and Host update process; not executed in this Python core |
| Research records | Separately retained experimental protocols and historical research; not automatically a current product contract or performance guarantee |
