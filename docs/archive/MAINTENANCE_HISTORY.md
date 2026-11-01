# Maintenance history summary (sanitized archive)

[简体中文](MAINTENANCE_HISTORY.zh-CN.md)

This page summarizes design themes in the old `AGENTS.md` from around 2026-10-31 to help maintainers find the background for changes. The original file contained machine-specific absolute paths, private story material, old run budgets, and temporary worktree status; none of that was copied into this public summary. This summary is not a current API, operating policy, or performance commitment.

## Topics and current references

- Legacy scene/event and actor-visibility design: the current interface is defined by the [play-memory v1 architecture](../architecture/PLAY_MEMORY_MODEL.md) and [schema](../../schemas/play-memory-v1.schema.json).
- Character directories, tiers, dossiers, acquisition, and shared background: organized around current v1 record and scope semantics in the [architecture page](../architecture/PLAY_MEMORY_MODEL.md#actor-directory-dossiers-and-cognition).
- Branches, epochs, scope, and rerolls: described in terms of Host-provided bindings and lineage in the [RP Host integration guide](../guides/RP_INTEGRATION.md#save-isolation-and-rerolls).
- Legacy `RpgMemoryKernel` / AERP-1, AERP-2, and AERP-3 research themes: see the [reference page](../reference/PLAY_MEMORY_V1.md#legacy-rpg-kernel-mcp) for the legacy kernel API and retrieval boundaries; historical evaluations and research protocols remain in the [Phase 1 evidence report (Chinese)](../phase-1-retrieval-evidence-report.zh-CN.md) and [paper research program](../paper-research-program.md).
- Historical Story/Plan2/RP integration proposals: current module responsibilities are in the [integration guide](../guides/RP_INTEGRATION.md) and [integration boundary](../RP_FOUNDATION_INTEGRATION_BOUNDARY.md); experimental results should be interpreted from their corresponding original research records.
- Design themes for local passage and Qwen summary-vector caches: current scope is described on the [architecture page](../architecture/PLAY_MEMORY_MODEL.md#retrieval-path-and-data-boundaries); cache performance does not establish semantic retrieval quality.
- The old validation checklist and model/data identity policies: those apply only to the frozen protocols cited by each historical study; they are not universal budget requirements for everyday documentation or game integrations.
