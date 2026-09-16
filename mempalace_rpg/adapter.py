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
    """Write and compensate verbatim episode drawers.

    ``drawer_id`` is supplied by the kernel and is deterministic for a scene.
    Implementations must therefore treat ``add_scene_drawer`` as an upsert and
    ``delete_scene_drawer`` as an idempotent delete.
    """

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

    def delete_scene_drawer(self, *, drawer_id: str) -> None:
        """Delete a scene drawer; deleting an absent drawer must be harmless."""


class DrawerCompensationError(RuntimeError):
    """A scene write failed and its compensating drawer delete also failed."""

    def __init__(
        self,
        *,
        drawer_id: str,
        original_error: Exception,
        cleanup_error: Exception,
    ) -> None:
        self.drawer_id = drawer_id
        self.original_error = original_error
        self.cleanup_error = cleanup_error
        super().__init__(
            f"scene drawer compensation failed for {drawer_id!r}; "
            f"original={type(original_error).__name__}: {original_error}; "
            f"cleanup={type(cleanup_error).__name__}: {cleanup_error}"
        )


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

    def delete_scene_drawer(self, *, drawer_id: str) -> None:
        return None


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
        write = DrawerWrite(
            text=text,
            wing=wing,
            room=room,
            drawer_id=drawer_id,
            metadata=dict(metadata),
        )
        for index, existing in enumerate(self.drawers):
            if existing.drawer_id == drawer_id:
                self.drawers[index] = write
                break
        else:
            self.drawers.append(write)
        return drawer_id

    def delete_scene_drawer(self, *, drawer_id: str) -> None:
        self.drawers[:] = [drawer for drawer in self.drawers if drawer.drawer_id != drawer_id]


class MempalaceEpisodeAdapter:
    """Adapter that writes RPG scene transcripts to MemPalace drawers.

    The bundled MemPalace implementation is imported lazily so projects can use
    the SQLite kernel without opening a ChromaDB collection.  The adapter uses
    MemPalace's standard collection API and stores RPG-specific fields as drawer
    metadata, allowing later search to remain wing/room scoped.
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

    def delete_scene_drawer(self, *, drawer_id: str) -> None:
        from mempalace.palace import get_collection

        col = get_collection(
            self.palace_path,
            collection_name=self.collection_name,
            create=True,
        )
        col.delete(ids=[drawer_id])
