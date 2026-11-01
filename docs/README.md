# Documentation map

[简体中文](README.zh-CN.md)

This directory separates the current play-memory v1 contract, the legacy RPG kernel kept for compatibility, and research and integration records. Start with [installation and service entry points](guides/INSTALLATION_AND_CLI.md), then read the [integration guide](guides/RP_INTEGRATION.md), [architecture](architecture/PLAY_MEMORY_MODEL.md), and [contract reference](reference/PLAY_MEMORY_V1.md) as needed.

## Current documentation

| Category | Document | Purpose |
| --- | --- | --- |
| Guide | [Installation and service entry points](guides/INSTALLATION_AND_CLI.md) | Python installation, legacy kernel CLI/MCP, and play-memory v1 stdio startup |
| Guide | [RP Host integration](guides/RP_INTEGRATION.md) | Host data mapping, scope, semantic agents, and branch responsibilities |
| Architecture | [play-memory v1 model](architecture/PLAY_MEMORY_MODEL.md) | Sources, actor cognition, graph navigation, lifecycle, isolation, and dual-path Qwen retrieval |
| Reference | [play-memory v1 and legacy kernel MCP](reference/PLAY_MEMORY_V1.md) | Methods, parameters, and boundaries of both protocols |
| Experiment | [RP integration: the 11-question gap case](experiments/RP_INTEGRATION_GAP11.md) | Material-recall and final-answer results from one frozen real-context case |

## Existing material

- [RPG Memory Design](memory-design.md) remains a conceptual guide to the legacy `RpgMemoryKernel` compatibility layer; it does not define play-memory v1.
- [pi Integration](pi-integration.md) remains the integration guide for the legacy MCP.
- [TavernDB import](taverndb-import.md) remains a migration guide for the legacy kernel.
- [RP Foundation integration boundary](RP_FOUNDATION_INTEGRATION_BOUNDARY.md) summarizes the current Host boundary and links to the integration guide.
- The [Phase 1 retrieval evidence report (Chinese)](phase-1-retrieval-evidence-report.zh-CN.md) and [paper research program](paper-research-program.md) remain in their original research languages and scopes. They are historical research materials, not current product quality claims.
- [Maintenance history summary](archive/MAINTENANCE_HISTORY.md) records themes from the old `AGENTS.md`; machine-specific paths, private story text, old run budgets, and worktree status have been removed from the public archive.

## Where older topics live

| Older topic | Current location | Treatment |
| --- | --- | --- |
| Installation and CLI commands scattered through the README | [Installation and service entry points](guides/INSTALLATION_AND_CLI.md) | Organized into three separate entry points: legacy kernel CLI, legacy MCP, and v1 stdio |
| Scene, event, belief, permission, and relationship concepts | [play-memory v1 architecture](architecture/PLAY_MEMORY_MODEL.md) | Uses the current `play-memory-v1` schema as its source of truth; legacy kernel table names are labeled separately |
| Saving branches, rerolls, and host responsibilities | [RP Host integration](guides/RP_INTEGRATION.md) and [integration boundary](RP_FOUNDATION_INTEGRATION_BOUNDARY.md) | Clarifies that the Host maps saves and worldlines and submits the correct branch, epoch, and lineage |
| AERP retrieval research details in the README | [Phase 1 evidence report (Chinese)](phase-1-retrieval-evidence-report.zh-CN.md) and [research program](paper-research-program.md) | Kept as historical research, not as current play-memory results |
| Character directories, dossiers, background knowledge, and lifecycle notes from the old AGENTS | [play-memory v1 architecture](architecture/PLAY_MEMORY_MODEL.md) | Restated against the current contract; the archive does not carry forward old plan status |
| The accepted F150/r433 gap test | [RP integration case study](experiments/RP_INTEGRATION_GAP11.md) and its [aggregated data](experiments/fated-poem-gap11-20261031.json) | Preserves the distinction between material and final-answer scores and notes the single-case limit |
| Implementation roadmap and research plans from the old AGENTS | [Maintenance history summary](archive/MAINTENANCE_HISTORY.md) | Retained as a summary, with stale local paths, private text, and expired budget/worktree requirements removed |
