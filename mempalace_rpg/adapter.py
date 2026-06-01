"""Episode-store adapters for RPG memory.

The RPG kernel is intentionally an outer layer.  It owns ACL, stateful facts,
and narrative projections, while adapters write verbatim scene text to whatever
raw-memory backend a host wants.  The default adapter is in-memory/no-op so the
kernel can be embedded without forcing ChromaDB startup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


@dataclass(frozen=True)
class DrawerWrite:
    text: str
    wing: str
    room: str
    drawer_id: str
    metadata: dict = field(default_factory=dict)


class EpisodeAdapter(Protocol):
    """Write verbatim episode drawers to an external memory backend."""

    def add_scene_drawer(
        self,
        *,
        text: str,
        wing: str,
        room: str,
        drawer_id: str,
        metadata: dict,
    ) -> str:
        """Persist a scene drawer and return its backend vector/drawer id."""


class NullEpisodeAdapter:
    """No-op adapter used when callers only need SQLite state/ACL behavior."""

    def add_scene_drawer(
        self,
        *,
        text: str,
        wing: str,
        room: str,
        drawer_id: str,
        metadata: dict,
    ) -> str:
        return drawer_id


class RecordingEpisodeAdapter:
    """Test/debug adapter that records drawer writes in memory."""

    def __init__(self) -> None:
        self.drawers: list[DrawerWrite] = []

    def add_scene_drawer(
        self,
        *,
        text: str,
        wing: str,
        room: str,
        drawer_id: str,
        metadata: dict,
    ) -> str:
        self.drawers.append(
            DrawerWrite(
                text=text,
                wing=wing,
                room=room,
                drawer_id=drawer_id,
                metadata=dict(metadata),
            )
        )
        return drawer_id


class MempalaceEpisodeAdapter:
    """Adapter that writes RPG scene transcripts to MemPalace drawers.

    This adapter is optional and imported lazily so projects can use the SQLite
    kernel without opening a ChromaDB collection.  It uses MemPalace's standard
    collection API and stores RPG-specific fields as drawer metadata, allowing
    later search to remain wing/room scoped.
    """

    def __init__(self, palace_path: str, collection_name: str | None = None) -> None:
        self.palace_path = palace_path
        self.collection_name = collection_name

    def add_scene_drawer(
        self,
        *,
        text: str,
        wing: str,
        room: str,
        drawer_id: str,
        metadata: dict,
    ) -> str:
        from mempalace.palace import get_collection

        col = get_collection(
            self.palace_path,
            collection_name=self.collection_name,
            create=True,
        )
        filed_at = metadata.get("filed_at") or datetime.now(timezone.utc).isoformat()
        drawer_meta = {
            **metadata,
            "wing": wing,
            "room": room,
            "source_file": metadata.get("source_file") or f"rpg_scene:{metadata.get('scene_id', drawer_id)}",
            "chunk_index": int(metadata.get("chunk_index") or 0),
            "added_by": "rpg_memory_kernel",
            "filed_at": filed_at,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
        col.upsert(documents=[text], ids=[drawer_id], metadatas=[drawer_meta])
        return drawer_id
