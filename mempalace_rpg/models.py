"""Public data models for the RPG memory kernel."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SceneEventInput:
    """Structured event extracted from a scene transcript.

    The kernel treats scene text as canonical evidence and this event as an
    indexable projection.  Visibility and witnesses are used before recall, not
    left for the model to police after retrieval.
    """

    event_type: str
    summary: str
    actor_id: str | None = None
    target_id: str | None = None
    truth_status: str = "canonical"
    visibility: str = "public_world"
    witness_set: list[str] = field(default_factory=list)
    related_entities: list[str] = field(default_factory=list)
    related_quests: list[str] = field(default_factory=list)
    related_locations: list[str] = field(default_factory=list)
    source_span: str | None = None
    emotional_weight: float = 0.0
    importance: float = 0.0
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryPack:
    """Runtime context bundle for one actor."""

    actor_id: str
    actor_type: str
    sections: list[tuple[str, str]]
    evidence: list[dict[str, Any]]
    forbidden_guard: str

    def render(self) -> str:
        lines: list[str] = []
        for title, body in self.sections:
            if not body.strip():
                continue
            lines.append(f"## {title}")
            lines.append(body.strip())
        if self.evidence:
            lines.append("## Retrieved Evidence Snippets")
            for item in self.evidence:
                marker = item.get("memory_id", "memory")
                domain = item.get("domain", "?")
                scene = item.get("scene") if isinstance(item.get("scene"), dict) else {}
                in_world_time = item.get("in_world_time") or scene.get("in_world_time") or "剧情时间未标注"
                location = item.get("location_id") or scene.get("location_id") or "地点未标注"
                created_at = item.get("created_at") or scene.get("created_at")
                source_scene_id = item.get("source_scene_id") or scene.get("scene_id")
                time_bits = [str(in_world_time), str(location)]
                if source_scene_id:
                    time_bits.append(f"scene:{source_scene_id}")
                if created_at:
                    time_bits.append(f"stored:{created_at}")
                text = str(item.get("text", "")).strip()
                lines.append(f"- [{domain}:{marker} | {' | '.join(time_bits)}] {text}")
        if self.forbidden_guard:
            lines.append("## Forbidden Knowledge Guard")
            lines.append(self.forbidden_guard.strip())
        return "\n\n".join(lines)
