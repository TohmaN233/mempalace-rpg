"""RPG narrative memory kernel.

This module is deliberately project-agnostic.  It can be used by a tabletop
campaign, an interactive fiction package, or a game server without importing any
of those hosts.  MemPalace remains the raw-memory backend; this layer adds RPG
semantics: stable entities, scene-rooted events, world truth vs actor belief,
ACL-first recall, and tiered runtime budgets.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .adapter import EpisodeAdapter, NullEpisodeAdapter
from .budget import budget_for_tier
from .models import MemoryPack, SceneEventInput
from .settings import (
    belief_write_enabled,
    domain_recall_enabled,
    domain_write_enabled,
    event_type_write_enabled,
    fact_write_enabled,
    load_memo_settings,
    public_summary,
    recall_enabled,
    recall_section_enabled,
    write_enabled,
)

DEFAULT_RPG_MEMORY_DB = os.path.expanduser("~/.mempalace/rpg_memory.sqlite3")

VALID_VISIBILITIES = {
    "public_world",
    "party_only",
    "witnessed_only",
    "character_private",
    "faction_private",
    "quest_participants",
    "gm_only",
    "rumor_public",
    "retconned",
}
VALID_TRUTH_STATUSES = {
    "canonical",
    "observed",
    "reported",
    "rumor",
    "belief",
    "retconned",
    "uncertain",
}
VALID_ENTITY_TYPES = {
    "player",
    "character",
    "faction",
    "location",
    "quest",
    "item",
    "event",
    "concept",
}
VALID_TIERS = {"core", "major", "recurring", "ambient"}

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS entity_registry (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_profile (
    character_id TEXT PRIMARY KEY REFERENCES entity_registry(entity_id),
    tier TEXT NOT NULL,
    public_role TEXT,
    private_role TEXT,
    short_persona TEXT NOT NULL,
    speech_style TEXT,
    personality_tags_json TEXT NOT NULL,
    core_values_json TEXT NOT NULL,
    current_goal TEXT,
    core_fear TEXT,
    faction_id TEXT,
    home_location_id TEXT,
    memory_wing TEXT NOT NULL UNIQUE,
    promotable INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS entity_importance (
    entity_id TEXT PRIMARY KEY REFERENCES entity_registry(entity_id),
    current_tier TEXT NOT NULL,
    base_story_weight REAL NOT NULL DEFAULT 0.0,
    player_interaction_count INTEGER NOT NULL DEFAULT 0,
    quest_link_count INTEGER NOT NULL DEFAULT 0,
    emotional_event_count INTEGER NOT NULL DEFAULT 0,
    secret_link_count INTEGER NOT NULL DEFAULT 0,
    recent_mentions INTEGER NOT NULL DEFAULT 0,
    importance_score REAL NOT NULL DEFAULT 0.0,
    last_promoted_at TEXT,
    last_demoted_at TEXT
);

CREATE TABLE IF NOT EXISTS scene_record (
    scene_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    in_world_time TEXT NOT NULL,
    scene_time_sort INTEGER NOT NULL,
    location_id TEXT,
    active_quest_ids_json TEXT NOT NULL,
    participants_json TEXT NOT NULL,
    witnesses_json TEXT NOT NULL,
    transcript TEXT NOT NULL,
    transcript_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scene_event (
    event_id TEXT PRIMARY KEY,
    scene_id TEXT NOT NULL REFERENCES scene_record(scene_id),
    event_type TEXT NOT NULL,
    actor_id TEXT,
    target_id TEXT,
    summary TEXT NOT NULL,
    truth_status TEXT NOT NULL,
    visibility TEXT NOT NULL,
    witness_set_json TEXT NOT NULL,
    related_entities_json TEXT NOT NULL,
    related_quests_json TEXT NOT NULL,
    related_locations_json TEXT NOT NULL,
    source_span TEXT,
    emotional_weight REAL NOT NULL DEFAULT 0.0,
    importance REAL NOT NULL DEFAULT 0.0,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_item (
    memory_id TEXT PRIMARY KEY,
    owner_scope TEXT NOT NULL,
    domain TEXT NOT NULL,
    source_scene_id TEXT,
    source_event_id TEXT,
    memory_type TEXT NOT NULL,
    text TEXT NOT NULL,
    visibility TEXT NOT NULL,
    known_by_json TEXT NOT NULL,
    related_entities_json TEXT NOT NULL,
    related_quests_json TEXT NOT NULL,
    related_locations_json TEXT NOT NULL,
    importance REAL NOT NULL DEFAULT 0.0,
    emotional_weight REAL NOT NULL DEFAULT 0.0,
    valid_from TEXT,
    valid_to TEXT,
    vector_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_fact (
    fact_id TEXT PRIMARY KEY,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_json TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    valid_from TEXT,
    valid_to TEXT,
    source_event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actor_belief (
    belief_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object_json TEXT NOT NULL,
    belief_status TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    valid_from TEXT,
    valid_to TEXT,
    source_event_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship_state (
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    trust REAL NOT NULL DEFAULT 0.0,
    affection REAL NOT NULL DEFAULT 0.0,
    fear REAL NOT NULL DEFAULT 0.0,
    hostility REAL NOT NULL DEFAULT 0.0,
    debt REAL NOT NULL DEFAULT 0.0,
    public_label TEXT,
    private_note TEXT,
    evidence_event_id TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (subject_id, object_id)
);

CREATE INDEX IF NOT EXISTS idx_scene_record_campaign_sort
    ON scene_record(campaign_id, scene_time_sort);
CREATE INDEX IF NOT EXISTS idx_scene_event_scene ON scene_event(scene_id);
CREATE INDEX IF NOT EXISTS idx_memory_domain_owner ON memory_item(domain, owner_scope);
CREATE INDEX IF NOT EXISTS idx_memory_source_event ON memory_item(source_event_id);
CREATE INDEX IF NOT EXISTS idx_world_fact_subject ON world_fact(subject_id);
CREATE INDEX IF NOT EXISTS idx_actor_belief_actor_subject ON actor_belief(actor_id, subject_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _uniq(values: Iterable[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen.keys())


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _clamp(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _infer_entity_type(entity_id: str) -> str:
    if entity_id == "player" or entity_id.startswith("player_"):
        return "player"
    if entity_id.startswith(("char_", "npc_")):
        return "character"
    if entity_id.startswith("faction_"):
        return "faction"
    if entity_id.startswith(("loc_", "location_")):
        return "location"
    if entity_id.startswith("quest_"):
        return "quest"
    if entity_id.startswith("item_"):
        return "item"
    if entity_id.startswith("event_"):
        return "event"
    return "concept"


class RpgMemoryKernel:
    """Standalone RPG memory service backed by SQLite plus optional drawers."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        episode_adapter: EpisodeAdapter | None = None,
        memo_settings_path: str | None = None,
        memo_settings: dict[str, Any] | None = None,
    ) -> None:
        self.db_path = os.path.expanduser(db_path or DEFAULT_RPG_MEMORY_DB)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.episode_adapter = episode_adapter or NullEpisodeAdapter()
        self.memo_settings, self.memo_settings_path = load_memo_settings(
            memo_settings_path,
            memo_settings,
        )
        self._connection: sqlite3.Connection | None = None
        self._init_db()

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "RpgMemoryKernel":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _conn(self) -> sqlite3.Connection:
        if self._connection is None:
            self._connection = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys=ON")
        return self._connection

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    # ------------------------------------------------------------------
    # Registry / profile
    # ------------------------------------------------------------------

    def upsert_entity(
        self,
        entity_id: str,
        entity_type: str,
        display_name: str,
        *,
        active: bool = True,
    ) -> str:
        entity_type = entity_type if entity_type in VALID_ENTITY_TYPES else "concept"
        now = _utcnow()
        with self._conn():
            self._conn().execute(
                """
                INSERT INTO entity_registry (entity_id, entity_type, display_name, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    entity_type=excluded.entity_type,
                    display_name=excluded.display_name,
                    active=excluded.active,
                    updated_at=excluded.updated_at
                """,
                (entity_id, entity_type, display_name, 1 if active else 0, now, now),
            )
        return entity_id

    def _ensure_entity(self, entity_id: str | None, display_name: str | None = None) -> None:
        if not entity_id:
            return
        row = self._conn().execute(
            "SELECT entity_id FROM entity_registry WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row:
            return
        self.upsert_entity(entity_id, _infer_entity_type(entity_id), display_name or entity_id)

    def upsert_character_profile(
        self,
        *,
        character_id: str,
        display_name: str,
        tier: str,
        short_persona: str,
        memory_wing: str,
        public_role: str | None = None,
        private_role: str | None = None,
        speech_style: str | None = None,
        personality_tags: list[str] | None = None,
        core_values: list[str] | None = None,
        current_goal: str | None = None,
        core_fear: str | None = None,
        faction_id: str | None = None,
        home_location_id: str | None = None,
        promotable: bool = True,
    ) -> str:
        tier = tier if tier in VALID_TIERS else "recurring"
        self.upsert_entity(character_id, "character", display_name)
        now = _utcnow()
        with self._conn():
            self._conn().execute(
                """
                INSERT INTO character_profile (
                    character_id, tier, public_role, private_role, short_persona,
                    speech_style, personality_tags_json, core_values_json,
                    current_goal, core_fear, faction_id, home_location_id,
                    memory_wing, promotable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(character_id) DO UPDATE SET
                    tier=excluded.tier,
                    public_role=excluded.public_role,
                    private_role=excluded.private_role,
                    short_persona=excluded.short_persona,
                    speech_style=excluded.speech_style,
                    personality_tags_json=excluded.personality_tags_json,
                    core_values_json=excluded.core_values_json,
                    current_goal=excluded.current_goal,
                    core_fear=excluded.core_fear,
                    faction_id=excluded.faction_id,
                    home_location_id=excluded.home_location_id,
                    memory_wing=excluded.memory_wing,
                    promotable=excluded.promotable
                """,
                (
                    character_id,
                    tier,
                    public_role,
                    private_role,
                    short_persona,
                    speech_style,
                    _json(personality_tags or []),
                    _json(core_values or []),
                    current_goal,
                    core_fear,
                    faction_id,
                    home_location_id,
                    memory_wing,
                    1 if promotable else 0,
                ),
            )
            self._conn().execute(
                """
                INSERT INTO entity_importance (entity_id, current_tier)
                VALUES (?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET current_tier=excluded.current_tier
                """,
                (character_id, tier),
            )
            # updated_at belongs to entity_registry, not profile.
            self._conn().execute(
                "UPDATE entity_registry SET updated_at=? WHERE entity_id=?",
                (now, character_id),
            )
        return character_id

    def _profile(self, actor_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            """
            SELECT er.display_name, cp.*
            FROM character_profile cp
            JOIN entity_registry er ON er.entity_id = cp.character_id
            WHERE cp.character_id=?
            """,
            (actor_id,),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        data["personality_tags"] = _loads(data.pop("personality_tags_json"), [])
        data["core_values"] = _loads(data.pop("core_values_json"), [])
        return data

    # ------------------------------------------------------------------
    # Scene commit
    # ------------------------------------------------------------------

    def commit_scene(
        self,
        *,
        campaign_id: str,
        in_world_time: str,
        transcript: str,
        location_id: str | None = None,
        active_quest_ids: list[str] | None = None,
        participants: list[str] | None = None,
        witnesses: list[str] | None = None,
        events: list[SceneEventInput | dict[str, Any]] | None = None,
        scene_id: str | None = None,
    ) -> str:
        active_quest_ids = _uniq(active_quest_ids or [])
        participants = _uniq(participants or [])
        witnesses = _uniq(witnesses or [])
        scene_id = scene_id or _id("scene")
        now = _utcnow()
        transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()

        for entity_id in [location_id, *active_quest_ids, *participants, *witnesses]:
            self._ensure_entity(entity_id)

        conn = self._conn()
        current_sort = conn.execute(
            "SELECT COALESCE(MAX(scene_time_sort), 0) FROM scene_record WHERE campaign_id=?",
            (campaign_id,),
        ).fetchone()[0]
        scene_time_sort = int(current_sort) + 1

        with conn:
            conn.execute(
                """
                INSERT INTO scene_record (
                    scene_id, campaign_id, in_world_time, scene_time_sort, location_id,
                    active_quest_ids_json, participants_json, witnesses_json,
                    transcript, transcript_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scene_id,
                    campaign_id,
                    in_world_time,
                    scene_time_sort,
                    location_id,
                    _json(active_quest_ids),
                    _json(participants),
                    _json(witnesses),
                    transcript,
                    transcript_hash,
                    now,
                ),
            )

        drawer_id = f"rpg_scene_{scene_id}"
        wing = "wing_campaign_canon"
        room = f"location_{location_id}" if location_id else "campaign"
        vector_id = self.episode_adapter.add_scene_drawer(
            text=transcript,
            wing=wing,
            room=room,
            drawer_id=drawer_id,
            metadata={
                "scene_id": scene_id,
                "campaign_id": campaign_id,
                "location_id": location_id,
                "in_world_time": in_world_time,
                "scene_time_sort": scene_time_sort,
                "filed_at": now,
            },
        )

        if write_enabled(self.memo_settings):
            for raw_event in events or []:
                event = self._coerce_event(raw_event)
                self._commit_event(
                    scene_id=scene_id,
                    campaign_id=campaign_id,
                    location_id=location_id,
                    active_quest_ids=active_quest_ids,
                    participants=participants,
                    witnesses=witnesses,
                    event=event,
                    vector_id=vector_id,
                    created_at=now,
                )

        return scene_id

    def _coerce_event(self, raw: SceneEventInput | dict[str, Any]) -> SceneEventInput:
        if isinstance(raw, SceneEventInput):
            return raw
        return SceneEventInput(**raw)

    def _commit_event(
        self,
        *,
        scene_id: str,
        campaign_id: str,
        location_id: str | None,
        active_quest_ids: list[str],
        participants: list[str],
        witnesses: list[str],
        event: SceneEventInput,
        vector_id: str | None,
        created_at: str,
    ) -> str:
        if not event_type_write_enabled(self.memo_settings, event.event_type):
            return ""

        event_id = _id("event")
        truth_status = event.truth_status if event.truth_status in VALID_TRUTH_STATUSES else "uncertain"
        visibility = event.visibility if event.visibility in VALID_VISIBILITIES else "gm_only"
        related_entities = _uniq([event.actor_id, event.target_id, *event.related_entities])
        related_quests = _uniq([*active_quest_ids, *event.related_quests])
        related_locations = _uniq([location_id, *event.related_locations])
        witness_set = _uniq([*event.witness_set, *witnesses])
        known_by = _uniq([*witness_set, event.actor_id, event.target_id])

        for entity_id in related_entities:
            self._ensure_entity(entity_id)
        for quest_id in related_quests:
            self._ensure_entity(quest_id)
        for loc_id in related_locations:
            self._ensure_entity(loc_id)

        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO scene_event (
                    event_id, scene_id, event_type, actor_id, target_id, summary,
                    truth_status, visibility, witness_set_json, related_entities_json,
                    related_quests_json, related_locations_json, source_span,
                    emotional_weight, importance, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    scene_id,
                    event.event_type,
                    event.actor_id,
                    event.target_id,
                    event.summary,
                    truth_status,
                    visibility,
                    _json(witness_set),
                    _json(related_entities),
                    _json(related_quests),
                    _json(related_locations),
                    event.source_span,
                    float(event.emotional_weight),
                    float(event.importance),
                    _json(event.payload),
                    created_at,
                ),
            )

        self._project_memory_items(
            campaign_id=campaign_id,
            event_id=event_id,
            scene_id=scene_id,
            event=event,
            truth_status=truth_status,
            visibility=visibility,
            known_by=known_by,
            related_entities=related_entities,
            related_quests=related_quests,
            related_locations=related_locations,
            vector_id=vector_id,
            created_at=created_at,
        )
        self._project_facts_and_beliefs(
            event_id=event_id,
            event=event,
            truth_status=truth_status,
            known_by=known_by,
            related_entities=related_entities,
            created_at=created_at,
        )
        self._update_importance(
            related_entities=related_entities,
            related_quests=related_quests,
            emotional_weight=event.emotional_weight,
            importance=event.importance,
            visibility=visibility,
            created_at=created_at,
        )
        return event_id

    def _insert_memory_item(
        self,
        *,
        owner_scope: str,
        domain: str,
        source_scene_id: str,
        source_event_id: str,
        memory_type: str,
        text: str,
        visibility: str,
        known_by: list[str],
        related_entities: list[str],
        related_quests: list[str],
        related_locations: list[str],
        importance: float,
        emotional_weight: float,
        vector_id: str | None,
        created_at: str,
    ) -> str:
        memory_id = _id("mem")
        with self._conn():
            self._conn().execute(
                """
                INSERT INTO memory_item (
                    memory_id, owner_scope, domain, source_scene_id, source_event_id,
                    memory_type, text, visibility, known_by_json, related_entities_json,
                    related_quests_json, related_locations_json, importance,
                    emotional_weight, vector_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    owner_scope,
                    domain,
                    source_scene_id,
                    source_event_id,
                    memory_type,
                    text,
                    visibility,
                    _json(known_by),
                    _json(related_entities),
                    _json(related_quests),
                    _json(related_locations),
                    float(importance),
                    float(emotional_weight),
                    vector_id,
                    created_at,
                ),
            )
        return memory_id

    def _project_memory_items(
        self,
        *,
        campaign_id: str,
        event_id: str,
        scene_id: str,
        event: SceneEventInput,
        truth_status: str,
        visibility: str,
        known_by: list[str],
        related_entities: list[str],
        related_quests: list[str],
        related_locations: list[str],
        vector_id: str | None,
        created_at: str,
    ) -> None:
        if truth_status == "retconned" or visibility == "retconned":
            memory_type = "index_note"
        elif truth_status == "rumor":
            memory_type = "rumor"
        elif truth_status == "belief":
            memory_type = "belief"
        else:
            memory_type = "summary"

        projections: set[tuple[str, str]] = {("canon", campaign_id)}
        if "player" in related_entities or event.actor_id == "player" or event.target_id == "player":
            projections.add(("player", "player"))
        for quest_id in related_quests:
            projections.add(("quest", quest_id))
        for loc_id in related_locations:
            projections.add(("location", loc_id))
        for entity_id in related_entities:
            entity_type = self._entity_type(entity_id)
            if entity_type == "character":
                projections.add(("character", entity_id))
            elif entity_type == "faction":
                projections.add(("faction", entity_id))
            elif entity_type == "item":
                projections.add(("item", entity_id))

        for domain, owner_scope in sorted(projections):
            if not domain_write_enabled(self.memo_settings, domain):
                continue
            self._insert_memory_item(
                owner_scope=owner_scope,
                domain=domain,
                source_scene_id=scene_id,
                source_event_id=event_id,
                memory_type=memory_type,
                text=event.summary,
                visibility=visibility,
                known_by=known_by,
                related_entities=related_entities,
                related_quests=related_quests,
                related_locations=related_locations,
                importance=event.importance,
                emotional_weight=event.emotional_weight,
                vector_id=vector_id,
                created_at=created_at,
            )

    def _project_facts_and_beliefs(
        self,
        *,
        event_id: str,
        event: SceneEventInput,
        truth_status: str,
        known_by: list[str],
        related_entities: list[str],
        created_at: str,
    ) -> None:
        subject_id = event.target_id or (related_entities[0] if related_entities else event.actor_id)
        if not subject_id:
            return
        payload = {
            "summary": event.summary,
            "event_type": event.event_type,
            "actor_id": event.actor_id,
            "target_id": event.target_id,
            "truth_status": truth_status,
            "payload": event.payload,
        }
        conn = self._conn()
        if truth_status == "canonical" and fact_write_enabled(self.memo_settings):
            with conn:
                conn.execute(
                    """
                    INSERT INTO world_fact (
                        fact_id, subject_id, predicate, object_json, confidence,
                        source_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("fact"),
                        subject_id,
                        event.event_type,
                        _json(payload),
                        1.0,
                        event_id,
                        created_at,
                    ),
                )

        if not belief_write_enabled(self.memo_settings):
            return

        status, confidence = self._belief_status(truth_status)
        recipients = known_by if truth_status in {"canonical", "observed", "reported", "rumor", "belief", "uncertain"} else []
        for actor_id in recipients:
            if actor_id == subject_id and event.event_type in {"death", "injury"}:
                # A dead/unconscious target may not hold a fresh belief.  This is
                # intentionally conservative; external game logic can override.
                continue
            if actor_id.startswith("quest_") or actor_id.startswith("loc_") or actor_id.startswith("item_"):
                continue
            with conn:
                conn.execute(
                    """
                    INSERT INTO actor_belief (
                        belief_id, actor_id, subject_id, predicate, object_json,
                        belief_status, confidence, source_event_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        _id("belief"),
                        actor_id,
                        subject_id,
                        event.event_type,
                        _json(payload),
                        status,
                        confidence,
                        event_id,
                        created_at,
                    ),
                )

    def _belief_status(self, truth_status: str) -> tuple[str, float]:
        if truth_status == "rumor":
            return "rumored", 0.4
        if truth_status == "reported":
            return "suspected", 0.55
        if truth_status == "uncertain":
            return "doubted", 0.35
        if truth_status == "retconned":
            return "discredited", 0.1
        return "believed", 0.8 if truth_status == "observed" else 1.0

    def _update_importance(
        self,
        *,
        related_entities: list[str],
        related_quests: list[str],
        emotional_weight: float,
        importance: float,
        visibility: str,
        created_at: str,
    ) -> None:
        secret = visibility in {"gm_only", "character_private", "faction_private", "witnessed_only"}
        for entity_id in related_entities:
            self._ensure_entity(entity_id)
            tier = "recurring" if self._entity_type(entity_id) == "character" else "ambient"
            with self._conn():
                self._conn().execute(
                    """
                    INSERT INTO entity_importance (
                        entity_id, current_tier, quest_link_count, emotional_event_count,
                        secret_link_count, recent_mentions, importance_score
                    ) VALUES (?, ?, ?, ?, ?, 1, ?)
                    ON CONFLICT(entity_id) DO UPDATE SET
                        quest_link_count=quest_link_count + ?,
                        emotional_event_count=emotional_event_count + ?,
                        secret_link_count=secret_link_count + ?,
                        recent_mentions=recent_mentions + 1,
                        importance_score=importance_score + ?
                    """,
                    (
                        entity_id,
                        tier,
                        len(related_quests),
                        1 if emotional_weight >= 0.5 else 0,
                        1 if secret else 0,
                        float(importance) + float(emotional_weight),
                        len(related_quests),
                        1 if emotional_weight >= 0.5 else 0,
                        1 if secret else 0,
                        float(importance) + float(emotional_weight),
                    ),
                )

    # ------------------------------------------------------------------
    # Queries / pack building
    # ------------------------------------------------------------------

    def build_memory_pack(
        self,
        *,
        actor_id: str,
        actor_type: str,
        query: str,
        scene_id: str | None = None,
        location_id: str | None = None,
        active_quest_ids: list[str] | None = None,
        in_world_time: str | None = None,
        max_chars: int | None = None,
    ) -> MemoryPack:
        profile = self._profile(actor_id)
        tier = profile["tier"] if profile else ("core" if actor_type == "gm" else "recurring")
        budget = budget_for_tier(tier)
        max_chars = max_chars or budget.l2_chars

        sections: list[tuple[str, str]] = []
        if profile and recall_section_enabled(self.memo_settings, "profile"):
            sections.append(("L0 ProfileCard", self._render_profile(profile, budget.l0_chars)))

        if recall_section_enabled(self.memo_settings, "current_state"):
            state_text = self._render_current_state(scene_id=scene_id, location_id=location_id)
            if state_text:
                sections.append(("Current State", state_text))

        if recall_section_enabled(self.memo_settings, "world_truth"):
            facts = self._allowed_world_facts(actor_id=actor_id, actor_type=actor_type, as_of=in_world_time)
            if facts:
                sections.append(("WorldTruth allowed to actor", self._render_fact_lines(facts, budget.l1_chars)))

        if recall_section_enabled(self.memo_settings, "actor_belief"):
            beliefs = self.list_actor_beliefs(actor_id=actor_id)
            if beliefs:
                sections.append(("ActorBelief", self._render_belief_lines(beliefs, budget.l1_chars)))

        evidence = []
        if recall_enabled(self.memo_settings) and recall_section_enabled(self.memo_settings, "evidence"):
            evidence = self._retrieve_memory_items(
                actor_id=actor_id,
                actor_type=actor_type,
                query=query,
                active_quest_ids=active_quest_ids or [],
                location_id=location_id,
                hit_limit=budget.hit_limit,
                max_chars=max_chars,
            )
        guard = (
            "Recall was ACL-filtered before ranking. Do not reveal gm_only, "
            "private, unwitnessed, or retconned knowledge unless it appears above."
        )
        return MemoryPack(
            actor_id=actor_id,
            actor_type=actor_type,
            sections=sections,
            evidence=evidence,
            forbidden_guard=guard,
        )

    def _render_profile(self, profile: dict[str, Any], max_chars: int) -> str:
        parts = [f"{profile['display_name']}（{profile['character_id']}，{profile['tier']}）"]
        if profile.get("public_role"):
            parts.append(f"公开身份：{profile['public_role']}")
        if profile.get("short_persona"):
            parts.append(str(profile["short_persona"]))
        if profile.get("speech_style"):
            parts.append(f"说话风格：{profile['speech_style']}")
        if profile.get("current_goal"):
            parts.append(f"当前目标：{profile['current_goal']}")
        if profile.get("core_fear"):
            parts.append(f"核心恐惧：{profile['core_fear']}")
        return _clamp("\n".join(parts), max_chars)

    def _render_current_state(self, *, scene_id: str | None, location_id: str | None) -> str:
        if scene_id:
            row = self._conn().execute(
                "SELECT in_world_time, location_id FROM scene_record WHERE scene_id=?",
                (scene_id,),
            ).fetchone()
            if row:
                return f"时间：{row['in_world_time']}\n地点：{row['location_id'] or '未指定'}"
        if location_id:
            return f"地点：{location_id}"
        row = self._conn().execute(
            "SELECT in_world_time, location_id FROM scene_record ORDER BY scene_time_sort DESC LIMIT 1"
        ).fetchone()
        if row:
            return f"时间：{row['in_world_time']}\n地点：{row['location_id'] or '未指定'}"
        return ""

    def _allowed_world_facts(
        self,
        *,
        actor_id: str,
        actor_type: str,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM world_fact ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        facts: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            if as_of and not self._valid_as_of(data.get("valid_from"), data.get("valid_to"), as_of):
                continue
            source_event_id = data.get("source_event_id")
            if source_event_id and not self._event_allowed(source_event_id, actor_id, actor_type):
                continue
            data["object"] = _loads(data.pop("object_json"), {})
            facts.append(data)
        return facts

    def _render_fact_lines(self, facts: list[dict[str, Any]], max_chars: int) -> str:
        lines = [f"- {f['subject_id']} {f['predicate']}: {f['object'].get('summary', '')}" for f in facts]
        return _clamp("\n".join(lines), max_chars)

    def _render_belief_lines(self, beliefs: list[dict[str, Any]], max_chars: int) -> str:
        lines = [
            f"- {b['actor_id']} {b['belief_status']} {b['subject_id']} {b['predicate']}: {b['object'].get('summary', '')}"
            for b in beliefs
        ]
        return _clamp("\n".join(lines), max_chars)

    def get_scene_transcript(
        self,
        *,
        scene_id: str,
        actor_id: str,
        actor_type: str = "npc",
        query: str | None = None,
        mode: str = "snippets",
        max_chars: int | None = 4000,
    ) -> dict[str, Any]:
        """Return ACL-safe verbatim scene text or exact snippets.

        Full transcript access is intentionally stricter than event/memory
        access: non-GM actors may read verbatim scene text only if they were
        listed as scene participants or witnesses.  Actors who can see one
        projected event but were not present receive the allowed event summaries
        as evidence, not the whole transcript.
        """

        row = self._conn().execute(
            "SELECT * FROM scene_record WHERE scene_id=?",
            (scene_id,),
        ).fetchone()
        if not row:
            return {"success": False, "error": "scene_not_found", "scene_id": scene_id}

        scene = dict(row)
        participants = _loads(scene.pop("participants_json"), [])
        witnesses = _loads(scene.pop("witnesses_json"), [])
        active_quest_ids = _loads(scene.pop("active_quest_ids_json"), [])
        present = set(_uniq([*participants, *witnesses]))
        full_allowed = actor_type == "gm" or actor_id == "gm" or actor_id in present

        event_rows = self._conn().execute(
            "SELECT * FROM scene_event WHERE scene_id=? ORDER BY created_at, event_id",
            (scene_id,),
        ).fetchall()
        allowed_events: list[dict[str, Any]] = []
        for event_row in event_rows:
            event = dict(event_row)
            if not self._event_allowed(str(event["event_id"]), actor_id, actor_type):
                continue
            event["witness_set"] = _loads(event.pop("witness_set_json"), [])
            event["related_entities"] = _loads(event.pop("related_entities_json"), [])
            event["related_quests"] = _loads(event.pop("related_quests_json"), [])
            event["related_locations"] = _loads(event.pop("related_locations_json"), [])
            event["payload"] = _loads(event.pop("payload_json"), {})
            allowed_events.append(event)

        if not full_allowed and not allowed_events:
            return {
                "success": False,
                "error": "forbidden",
                "scene_id": scene_id,
                "forbidden_guard": "Actor is neither GM nor a scene participant/witness, and no scene event is ACL-visible.",
            }

        transcript = str(scene.get("transcript") or "")
        response: dict[str, Any] = {
            "success": True,
            "scene_id": scene_id,
            "campaign_id": scene.get("campaign_id"),
            "in_world_time": scene.get("in_world_time"),
            "location_id": scene.get("location_id"),
            "active_quest_ids": active_quest_ids,
            "participants": participants,
            "witnesses": witnesses,
            "transcript_hash": scene.get("transcript_hash"),
            "full_transcript_allowed": full_allowed,
            "allowed_events": allowed_events,
            "forbidden_guard": "Verbatim transcript access was ACL-checked before return; do not reveal unavailable text.",
        }

        if full_allowed:
            if mode == "full":
                response["transcript"] = _clamp(transcript, max_chars or len(transcript))
                response["truncated"] = bool(max_chars and len(transcript) > max_chars)
            else:
                snippets = self._scene_snippets(transcript=transcript, query=query or "", max_chars=max_chars or 4000)
                response["snippets"] = snippets
                response["transcript_excerpt"] = "\n[…]\n".join(snippet["text"] for snippet in snippets)
        return response

    def deep_recall(
        self,
        *,
        actor_id: str,
        actor_type: str,
        query: str,
        scene_id: str | None = None,
        location_id: str | None = None,
        active_quest_ids: list[str] | None = None,
        in_world_time: str | None = None,
        max_chars: int | None = None,
        per_scene_chars: int = 2000,
        scene_limit: int = 3,
    ) -> dict[str, Any]:
        """Build a normal MemoryPack, then fetch verbatim snippets for top evidence scenes."""

        pack = self.build_memory_pack(
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            scene_id=scene_id,
            location_id=location_id,
            active_quest_ids=active_quest_ids or [],
            in_world_time=in_world_time,
            max_chars=max_chars,
        )
        scene_ids = _uniq([str(item.get("source_scene_id") or "") for item in pack.evidence])[: max(0, scene_limit)]
        scene_evidence = [
            self.get_scene_transcript(
                scene_id=sid,
                actor_id=actor_id,
                actor_type=actor_type,
                query=query,
                mode="snippets",
                max_chars=per_scene_chars,
            )
            for sid in scene_ids
        ]
        return {
            "success": True,
            "actor_id": pack.actor_id,
            "actor_type": pack.actor_type,
            "rendered": pack.render(),
            "sections": pack.sections,
            "evidence": pack.evidence,
            "scene_evidence": scene_evidence,
            "forbidden_guard": pack.forbidden_guard + " Verbatim scene snippets were also ACL-checked.",
        }

    def _scene_snippets(self, *, transcript: str, query: str, max_chars: int) -> list[dict[str, Any]]:
        if not transcript:
            return []
        if not query.strip():
            text = _clamp(transcript, max_chars)
            return [{"start": 0, "end": len(text), "text": text}]

        paragraphs: list[tuple[int, int, str]] = []
        pos = 0
        for part in re.split(r"(\n\s*\n)", transcript):
            start = pos
            pos += len(part)
            if not part.strip() or re.fullmatch(r"\n\s*\n", part):
                continue
            paragraphs.append((start, pos, part.strip()))

        query_chars = set(query)
        scored: list[tuple[float, int, int, str]] = []
        for start, end, text in paragraphs:
            score = 0.1 * len(query_chars & set(text))
            for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", query):
                if token in text:
                    score += 5.0 + min(len(token), 20) * 0.1
            if score > 0:
                scored.append((score, start, end, text))
        if not scored:
            text = _clamp(transcript, max_chars)
            return [{"start": 0, "end": len(text), "text": text}]

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = sorted(scored[:6], key=lambda item: item[1])
        snippets: list[dict[str, Any]] = []
        used = 0
        for _score, start, end, text in selected:
            remaining = max_chars - used
            if remaining <= 0:
                break
            excerpt = _clamp(text, remaining)
            snippets.append({"start": start, "end": start + len(excerpt), "text": excerpt})
            used += len(excerpt)
        return snippets

    def _retrieve_memory_items(
        self,
        *,
        actor_id: str,
        actor_type: str,
        query: str,
        active_quest_ids: list[str],
        location_id: str | None,
        hit_limit: int,
        max_chars: int,
    ) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM memory_item ORDER BY created_at DESC LIMIT 1000"
        ).fetchall()
        allowed: list[dict[str, Any]] = []
        for row in rows:
            item = self._memory_item_from_row(row)
            if not domain_recall_enabled(self.memo_settings, str(item.get("domain"))):
                continue
            if not self._memory_allowed(item, actor_id, actor_type):
                continue
            item["rank_score"] = self._rank_score(
                item,
                query=query,
                active_quest_ids=active_quest_ids,
                location_id=location_id,
            )
            allowed.append(item)
        allowed.sort(key=lambda item: item["rank_score"], reverse=True)

        packed: list[dict[str, Any]] = []
        used = 0
        for item in allowed:
            if len(packed) >= hit_limit:
                break
            text_len = len(str(item.get("text", "")))
            if packed and used + text_len > max_chars:
                break
            used += text_len
            packed.append(item)
        return packed

    def _rank_score(
        self,
        item: dict[str, Any],
        *,
        query: str,
        active_quest_ids: list[str],
        location_id: str | None,
    ) -> float:
        text = str(item.get("text", ""))
        score = float(item.get("importance") or 0) * 3.0
        score += float(item.get("emotional_weight") or 0) * 2.0
        score += 0.1 * len(set(query) & set(text))
        item_quests = set(item.get("related_quests") or [])
        if item_quests & set(active_quest_ids):
            score += 1.5
        if location_id and location_id in set(item.get("related_locations") or []):
            score += 0.8
        if item.get("domain") == "canon":
            score -= 0.05
        return score

    def _memory_allowed(self, item: dict[str, Any], actor_id: str, actor_type: str) -> bool:
        return self._visibility_allowed(
            actor_id=actor_id,
            actor_type=actor_type,
            visibility=item.get("visibility"),
            known_by=item.get("known_by") or [],
            owner_scope=item.get("owner_scope"),
        )

    def _event_allowed(self, event_id: str, actor_id: str, actor_type: str) -> bool:
        row = self._conn().execute(
            "SELECT visibility, witness_set_json, actor_id, target_id FROM scene_event WHERE event_id=?",
            (event_id,),
        ).fetchone()
        if not row:
            return actor_type == "gm" or actor_id == "gm"
        known_by = _uniq([*_loads(row["witness_set_json"], []), row["actor_id"], row["target_id"]])
        return self._visibility_allowed(
            actor_id=actor_id,
            actor_type=actor_type,
            visibility=row["visibility"],
            known_by=known_by,
            owner_scope=row["target_id"],
        )

    def _visibility_allowed(
        self,
        *,
        actor_id: str,
        actor_type: str,
        visibility: str | None,
        known_by: list[str],
        owner_scope: str | None = None,
    ) -> bool:
        visibility = visibility or "gm_only"
        if actor_type == "gm" or actor_id == "gm":
            return True
        if visibility == "retconned":
            return False
        if visibility in {"public_world", "rumor_public"}:
            return True
        if visibility == "gm_only":
            return False
        if actor_id in known_by:
            return True
        if visibility == "party_only":
            return actor_id == "player" or actor_type in {"player", "companion"}
        if visibility == "quest_participants":
            return actor_id == "player" or actor_type in {"player", "companion"}
        if visibility == "character_private":
            return owner_scope == actor_id
        return False

    def _valid_as_of(self, valid_from: str | None, valid_to: str | None, as_of: str) -> bool:
        if valid_from and valid_from > as_of:
            return False
        if valid_to and valid_to < as_of:
            return False
        return True

    # ------------------------------------------------------------------
    # Listing helpers for tools/tests
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return compact storage counts for health checks and MCP status."""

        conn = self._conn()
        tables = [
            "entity_registry",
            "character_profile",
            "scene_record",
            "scene_event",
            "memory_item",
            "world_fact",
            "actor_belief",
            "relationship_state",
        ]
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        latest = conn.execute(
            "SELECT scene_id, campaign_id, in_world_time, location_id FROM scene_record ORDER BY scene_time_sort DESC LIMIT 1"
        ).fetchone()
        return {
            "success": True,
            "db": self.db_path,
            "counts": counts,
            "latest_scene": dict(latest) if latest else None,
            "memo_settings": public_summary(self.memo_settings, self.memo_settings_path),
        }

    def list_memory_items(
        self,
        *,
        domain: str | None = None,
        owner_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if domain:
            clauses.append("domain=?")
            params.append(domain)
        if owner_scope:
            clauses.append("owner_scope=?")
            params.append(owner_scope)
        sql = "SELECT * FROM memory_item"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, memory_id DESC"
        rows = self._conn().execute(sql, params).fetchall()
        return [self._memory_item_from_row(row) for row in rows]

    def list_world_facts(self, *, subject_id: str | None = None) -> list[dict[str, Any]]:
        if subject_id:
            rows = self._conn().execute(
                "SELECT * FROM world_fact WHERE subject_id=? ORDER BY created_at DESC",
                (subject_id,),
            ).fetchall()
        else:
            rows = self._conn().execute("SELECT * FROM world_fact ORDER BY created_at DESC").fetchall()
        facts: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["object"] = _loads(data.pop("object_json"), {})
            facts.append(data)
        return facts

    def list_actor_beliefs(
        self,
        *,
        actor_id: str | None = None,
        subject_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str] = []
        if actor_id:
            clauses.append("actor_id=?")
            params.append(actor_id)
        if subject_id:
            clauses.append("subject_id=?")
            params.append(subject_id)
        sql = "SELECT * FROM actor_belief"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, belief_id DESC"
        rows = self._conn().execute(sql, params).fetchall()
        beliefs: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["object"] = _loads(data.pop("object_json"), {})
            beliefs.append(data)
        return beliefs

    def _memory_item_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["known_by"] = _loads(data.pop("known_by_json"), [])
        data["related_entities"] = _loads(data.pop("related_entities_json"), [])
        data["related_quests"] = _loads(data.pop("related_quests_json"), [])
        data["related_locations"] = _loads(data.pop("related_locations_json"), [])
        return data

    def _entity_type(self, entity_id: str) -> str:
        row = self._conn().execute(
            "SELECT entity_type FROM entity_registry WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row:
            return str(row["entity_type"])
        return _infer_entity_type(entity_id)


__all__ = ["RpgMemoryKernel"]
