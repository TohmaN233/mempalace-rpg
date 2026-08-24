"""Immutable-by-construction runtime expectations for formal MemPalace v3.8.0."""
from __future__ import annotations


def original_hnsw_configuration() -> dict[str, int | float | str]:
    """Return a fresh exact Chroma HNSW configuration for the pinned original arm."""
    return {
        "space": "cosine",
        "ef_construction": 100,
        "ef_search": 100,
        "max_neighbors": 16,
        "num_threads": 1,
        "batch_size": 100,
        "sync_threshold": 1000,
        "resize_factor": 1.2,
    }
