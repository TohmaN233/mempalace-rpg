"""RPG narrative memory kernel.

This module is deliberately project-agnostic.  It can be used by a tabletop
campaign, an interactive fiction package, or a game server without importing any
of those hosts.  MemPalace remains the raw-memory backend; this layer adds RPG
semantics: stable entities, scene-rooted events, world truth vs actor belief,
ACL-first recall, and tiered runtime budgets.
"""

from __future__ import annotations

import hashlib
import copy
import json
import math
import os
import re
import sqlite3
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .adapter import DrawerCompensationError, EpisodeAdapter, NullEpisodeAdapter
from .authorization import (
    AuthorizedEvidence,
    CANONICAL_TRUTH_STATUSES,
    EvidenceAuthorizer,
    VALID_BRANCH_STATUSES,
    VALID_TRUTH_STATUSES,
    VALID_VISIBILITIES,
)
from .budget import budget_for_tier
from .models import MemoryPack, SceneEventInput
from .retrieval import AuthorizedEventRanker, AuthorizedRetrievalCandidate, RankingResult, structured_observation
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
    request_fingerprint TEXT,
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
    access_owner_id TEXT,
    access_scope_id TEXT,
    belief_owner_id TEXT,
    branch_id TEXT,
    branch_status TEXT,
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

CREATE TABLE IF NOT EXISTS actor_membership (
    campaign_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    scope_kind TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (campaign_id, actor_id, scope_id, scope_kind)
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


def _strict_product_json(value: str | None, name: str, expected_type: type[dict[str, Any]] | type[list[Any]]) -> Any:
    """Decode product-ranker metadata without the legacy reader's fallback."""
    if not isinstance(value, str):
        raise ValueError(f"malformed product {name}")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed product {name}") from exc
    if not isinstance(decoded, expected_type):
        raise ValueError(f"malformed product {name}")
    if expected_type is list and not all(isinstance(item, str) for item in decoded):
        raise ValueError(f"malformed product {name}")
    return decoded


def _uniq(values: Iterable[str | None]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen.keys())


def _strict_string_list(value: Any, name: str, *, allow_none: bool = False) -> list[str]:
    """Accept only explicit list[str] provenance inputs; normalize/dedupe order."""
    if value is None and allow_none:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list of non-empty strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} must be a list of non-empty strings")
        text = item.strip()
        if text not in seen:
            seen.add(text)
            normalized.append(text)
    return normalized


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
        retrieval_ranker: AuthorizedEventRanker | None = None,
        memo_settings_path: str | None = None,
        memo_settings: dict[str, Any] | None = None,
    ) -> None:
        self.db_path = os.path.expanduser(db_path or DEFAULT_RPG_MEMORY_DB)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.episode_adapter = episode_adapter or NullEpisodeAdapter()
        self.retrieval_ranker = retrieval_ranker
        self.memo_settings, self.memo_settings_path = load_memo_settings(
            memo_settings_path,
            memo_settings,
        )
        self._connection: sqlite3.Connection | None = None
        self._read_cache_revision: tuple[int, int] | None = None
        self._authorization_cache: dict[tuple[str, str, str, str | None], dict[str, Any]] = {}
        self._projection_cache: dict[tuple[str, str], list[dict[str, Any]]] = {}
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

    def _refresh_read_caches(self) -> None:
        """Invalidate derived read state after local or external SQLite writes."""
        conn = self._conn()
        revision = (conn.total_changes, int(conn.execute("PRAGMA data_version").fetchone()[0]))
        if revision == self._read_cache_revision:
            return
        self._authorization_cache.clear()
        self._projection_cache.clear()
        self._read_cache_revision = revision

    def _init_db(self) -> None:
        conn = self._conn()
        conn.executescript(_SCHEMA)
        membership_columns = {row["name"] for row in conn.execute("PRAGMA table_info(actor_membership)")}
        if "scope_kind" not in membership_columns:
            conn.execute("DROP INDEX IF EXISTS idx_actor_membership_lookup")
            conn.execute("ALTER TABLE actor_membership RENAME TO actor_membership_legacy")
            conn.execute("CREATE TABLE actor_membership (campaign_id TEXT NOT NULL, actor_id TEXT NOT NULL, scope_id TEXT NOT NULL, scope_kind TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (campaign_id, actor_id, scope_id, scope_kind))")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_actor_membership_lookup ON actor_membership(campaign_id, actor_id, scope_id, scope_kind, active)")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(scene_event)")}
        for name in ("access_owner_id", "access_scope_id", "belief_owner_id", "branch_id", "branch_status"):
            if name not in columns:
                conn.execute(f"ALTER TABLE scene_event ADD COLUMN {name} TEXT")
        scene_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scene_record)")}
        if "request_fingerprint" not in scene_columns:
            conn.execute("ALTER TABLE scene_record ADD COLUMN request_fingerprint TEXT")
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
        now = _utcnow()
        self._conn().execute(
            """
            INSERT INTO entity_registry (entity_id, entity_type, display_name, active, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(entity_id) DO NOTHING
            """,
            (entity_id, _infer_entity_type(entity_id), display_name or entity_id, now, now),
        )

    def upsert_actor_membership(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        scope_id: str,
        scope_kind: str,
        active: bool = True,
    ) -> None:
        """Record campaign-scoped membership used by private evidence policy."""
        if not campaign_id or not actor_id or not scope_id or scope_kind not in {"faction", "quest", "party"}:
            raise ValueError("campaign_id, actor_id, scope_id, and valid scope_kind are required for membership")
        now = _utcnow()
        with self._conn():
            self._conn().execute(
                """
                INSERT INTO actor_membership (campaign_id, actor_id, scope_id, scope_kind, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(campaign_id, actor_id, scope_id, scope_kind) DO UPDATE SET
                    active=excluded.active, updated_at=excluded.updated_at
                """,
                (campaign_id, actor_id, scope_id, scope_kind, 1 if active else 0, now, now),
            )

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

    @staticmethod
    def _scene_request_fingerprint(
        *,
        campaign_id: str,
        in_world_time: str,
        transcript: str,
        location_id: str | None,
        active_quest_ids: list[str],
        participants: list[str],
        witnesses: list[str],
        events: list[SceneEventInput],
    ) -> str:
        canonical_request = {
            "version": 1,
            "campaign_id": campaign_id,
            "in_world_time": in_world_time,
            "transcript": transcript,
            "location_id": location_id,
            "active_quest_ids": active_quest_ids,
            "participants": participants,
            "witnesses": witnesses,
            "events": [asdict(event) for event in events],
        }
        return hashlib.sha256(_json(canonical_request).encode("utf-8")).hexdigest()

    @staticmethod
    def _check_scene_idempotency(
        conn: sqlite3.Connection,
        *,
        scene_id: str,
        request_fingerprint: str,
    ) -> bool:
        row = conn.execute(
            "SELECT request_fingerprint FROM scene_record WHERE scene_id=?",
            (scene_id,),
        ).fetchone()
        if row is None:
            return False
        stored_fingerprint = row["request_fingerprint"]
        if stored_fingerprint == request_fingerprint:
            return True
        raise ValueError(
            f"scene_id {scene_id!r} is already committed with a different request fingerprint; "
            f"stored={stored_fingerprint or '<missing>'}, received={request_fingerprint}"
        )

    def _scene_commit_checkpoint(self, stage: str) -> None:
        """Internal failure-injection seam; production commits leave it inert."""

    @staticmethod
    def _commit_scene_sqlite(conn: sqlite3.Connection) -> None:
        """Commit the scene transaction through an injectable boundary."""

        conn.commit()

    def _delete_failed_scene_drawer(self, *, drawer_id: str, original_error: Exception) -> None:
        try:
            self.episode_adapter.delete_scene_drawer(drawer_id=drawer_id)
        except Exception as cleanup_error:
            raise DrawerCompensationError(
                drawer_id=drawer_id,
                original_error=original_error,
                cleanup_error=cleanup_error,
            ) from original_error

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
        if not isinstance(campaign_id, str) or not campaign_id.strip():
            raise ValueError("campaign_id must be a non-empty string for scene evidence")
        campaign_id = campaign_id.strip()
        active_quest_ids = _strict_string_list(active_quest_ids, "active_quest_ids", allow_none=True)
        participants = _strict_string_list(participants, "participants", allow_none=True)
        witnesses = _strict_string_list(witnesses, "witnesses", allow_none=True)
        if events is not None and not isinstance(events, list):
            raise ValueError("events must be a list of SceneEventInput objects or dicts")
        event_inputs = [self._normalize_event_lists(self._coerce_event(raw)) for raw in (events or [])]
        located_spans: list[tuple[int, int, tuple[Any, ...]]] = []
        for event in event_inputs:
            self._validate_scene_event_security(event)
            if event.source_span:
                starts = [match.start() for match in re.finditer(re.escape(event.source_span), transcript)]
                if len(starts) != 1:
                    raise ValueError("source_span must occur exactly once in the supplied scene transcript")
                start, end = starts[0], starts[0] + len(event.source_span)
                policy = (event.truth_status, event.visibility, event.access_owner_id, event.access_scope_id, event.belief_owner_id, event.branch_id, event.branch_status)
                for prior_start, prior_end, prior_policy in located_spans:
                    if start < prior_end and prior_start < end and policy != prior_policy:
                        raise ValueError("overlapping source spans require identical security policy")
                located_spans.append((start, end, policy))
        scene_id = scene_id or _id("scene")
        if not isinstance(scene_id, str) or not scene_id.strip():
            raise ValueError("scene_id must be a non-empty string")
        scene_id = scene_id.strip()
        now = _utcnow()
        transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        request_fingerprint = self._scene_request_fingerprint(
            campaign_id=campaign_id,
            in_world_time=in_world_time,
            transcript=transcript,
            location_id=location_id,
            active_quest_ids=active_quest_ids,
            participants=participants,
            witnesses=witnesses,
            events=event_inputs,
        )
        conn = self._conn()
        if self._check_scene_idempotency(
            conn,
            scene_id=scene_id,
            request_fingerprint=request_fingerprint,
        ):
            return scene_id
        drawer_id = f"rpg_scene_{scene_id}"
        wing = "wing_campaign_canon"
        room = f"location_{location_id}" if location_id else "campaign"
        drawer_attempted = False

        try:
            conn.execute("BEGIN IMMEDIATE")
            if self._check_scene_idempotency(
                conn,
                scene_id=scene_id,
                request_fingerprint=request_fingerprint,
            ):
                conn.rollback()
                return scene_id

            current_sort = conn.execute(
                "SELECT COALESCE(MAX(scene_time_sort), 0) FROM scene_record WHERE campaign_id=?",
                (campaign_id,),
            ).fetchone()[0]
            scene_time_sort = int(current_sort) + 1

            drawer_attempted = True
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
                    "request_fingerprint": request_fingerprint,
                },
            )
            if vector_id != drawer_id:
                raise RuntimeError(
                    f"episode adapter returned non-deterministic drawer id {vector_id!r}; "
                    f"expected {drawer_id!r}"
                )

            for entity_id in [location_id, *active_quest_ids, *participants, *witnesses]:
                self._ensure_entity(entity_id)
            for event in event_inputs:
                for entity_id in [event.actor_id, event.target_id, *event.related_entities, event.access_owner_id, event.access_scope_id, event.belief_owner_id]:
                    self._ensure_entity(entity_id)
                for quest_id in event.related_quests:
                    self._ensure_entity(quest_id)
                for loc_id in event.related_locations:
                    self._ensure_entity(loc_id)

            conn.execute(
                """
                INSERT INTO scene_record (
                    scene_id, campaign_id, in_world_time, scene_time_sort, location_id,
                    active_quest_ids_json, participants_json, witnesses_json,
                    transcript, transcript_hash, request_fingerprint, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    request_fingerprint,
                    now,
                ),
            )
            self._scene_commit_checkpoint("scene_insert")
            if write_enabled(self.memo_settings):
                for event in event_inputs:
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
            self._commit_scene_sqlite(conn)
        except Exception as original_error:
            if conn.in_transaction:
                conn.rollback()
            committed = self._check_scene_idempotency(
                conn,
                scene_id=scene_id,
                request_fingerprint=request_fingerprint,
            )
            if not committed and drawer_attempted:
                self._delete_failed_scene_drawer(
                    drawer_id=drawer_id,
                    original_error=original_error,
                )
            raise

        self._scene_commit_checkpoint("after_sqlite_commit")
        return scene_id

    def _coerce_event(self, raw: SceneEventInput | dict[str, Any]) -> SceneEventInput:
        if isinstance(raw, SceneEventInput):
            return raw
        if not isinstance(raw, dict):
            raise ValueError("events must contain only SceneEventInput objects or dicts")
        return SceneEventInput(**raw)

    @staticmethod
    def _normalize_event_lists(event: SceneEventInput) -> SceneEventInput:
        return replace(
            event,
            witness_set=_strict_string_list(event.witness_set, "event.witness_set"),
            related_entities=_strict_string_list(event.related_entities, "event.related_entities"),
            related_quests=_strict_string_list(event.related_quests, "event.related_quests"),
            related_locations=_strict_string_list(event.related_locations, "event.related_locations"),
        )

    @staticmethod
    def _validate_scene_event_security(event: SceneEventInput) -> None:
        for name in ("event_type", "summary", "branch_id", "source_span"):
            value = getattr(event, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string for scene evidence")
        if event.truth_status not in VALID_TRUTH_STATUSES or event.visibility not in VALID_VISIBILITIES:
            raise ValueError("truth_status and visibility must be explicit and valid for scene evidence")
        if event.branch_status not in VALID_BRANCH_STATUSES:
            raise ValueError("branch_status must be explicit and valid for scene evidence")
        required_branch_status = "active" if event.truth_status in CANONICAL_TRUTH_STATUSES | {"belief"} else event.truth_status
        if event.branch_status != required_branch_status:
            raise ValueError("branch_status must match truth_status for scene evidence")
        owner, scope, belief_owner = event.access_owner_id, event.access_scope_id, event.belief_owner_id
        if event.visibility == "character_private":
            if not isinstance(owner, str) or not owner.strip() or scope is not None:
                raise ValueError("character_private requires access_owner_id and forbids access_scope_id")
        elif event.visibility in {"faction_private", "quest_participants", "party_only"}:
            if not isinstance(scope, str) or not scope.strip() or owner is not None:
                raise ValueError("scoped private visibility requires access_scope_id and forbids access_owner_id")
        elif owner is not None or scope is not None:
            raise ValueError("visibility forbids access_owner_id and access_scope_id")
        if event.truth_status == "belief":
            if not isinstance(belief_owner, str) or not belief_owner.strip() or event.actor_id != belief_owner:
                raise ValueError("belief requires belief_owner_id equal to actor_id")
        elif belief_owner is not None:
            raise ValueError("non-belief truth forbids belief_owner_id")

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
        self._validate_scene_event_security(event)
        truth_status = event.truth_status
        visibility = event.visibility
        if event.source_span is not None:
            transcript_row = self._conn().execute(
                "SELECT transcript FROM scene_record WHERE scene_id=?", (scene_id,)
            ).fetchone()
            if not transcript_row or event.source_span not in str(transcript_row["transcript"]):
                raise ValueError("source_span must be an exact substring of the source scene transcript")
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
        conn.execute(
                """
                INSERT INTO scene_event (
                    event_id, scene_id, event_type, actor_id, target_id, summary,
                    truth_status, visibility, witness_set_json, related_entities_json,
                    related_quests_json, related_locations_json, source_span,
                    access_owner_id, access_scope_id, belief_owner_id, branch_id, branch_status,
                    emotional_weight, importance, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    event.access_owner_id,
                    event.access_scope_id,
                    event.belief_owner_id,
                    event.branch_id,
                    event.branch_status,
                    float(event.emotional_weight),
                    float(event.importance),
                    _json(event.payload),
                    created_at,
                ),
        )
        self._scene_commit_checkpoint("event_insert")

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
        self._scene_commit_checkpoint("projection_insert")
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
        if truth_status in {"retconned", "abandoned"} or visibility == "retconned":
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
        if truth_status in {"retconned", "abandoned"}:
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

    def authorized_evidence(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        query: str,
        budget: int,
        active_quest_ids: list[str] | None = None,
        scene_id: str | None = None,
        _compact_product_trace: bool = False,
    ):
        """Return the single AERP-1 decision used by all evidence read paths."""
        authorizer = EvidenceAuthorizer(self._conn())
        if _compact_product_trace:
            self._refresh_read_caches()
            cache_key = (campaign_id, actor_id, actor_type, scene_id)
            cached_trace = self._authorization_cache.get(cache_key)
            if cached_trace is not None:
                trace = copy.deepcopy(cached_trace)
                trace["query"] = query
                trace["active_quest_ids"] = _uniq(active_quest_ids or [])
                return AuthorizedEvidence([], [], trace)
        decision = authorizer.authorize(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            budget=budget,
            active_quest_ids=_uniq(active_quest_ids or []),
            scene_id=scene_id,
            compact_product_trace=_compact_product_trace,
        )
        if _compact_product_trace:
            # Product code mutates selected IDs and detailed rows later, so the
            # cache owns an isolated base trace.
            self._authorization_cache[cache_key] = copy.deepcopy(decision.trace)
        return decision

    def build_memory_pack(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        query: str,
        scene_id: str | None = None,
        location_id: str | None = None,
        active_quest_ids: list[str] | None = None,
        in_world_time: str | None = None,
        max_chars: int | None = None,
        _authorization_decision: Any | None = None,
    ) -> MemoryPack:
        profile = self._profile(actor_id)
        tier = profile["tier"] if profile else ("core" if actor_type == "gm" else "recurring")
        budget = budget_for_tier(tier)
        max_chars = max_chars or budget.l2_chars

        # All evidence-derived projections share one decision.  Apart from
        # eliminating repeated 30k scans, this makes facts, beliefs, ranking and
        # the final trace observably descend from the same ACL boundary.
        decision = _authorization_decision or self.authorized_evidence(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            active_quest_ids=active_quest_ids or [],
            budget=1000,
            _compact_product_trace=True,
        )

        sections: list[tuple[str, str]] = []
        if profile and recall_section_enabled(self.memo_settings, "profile"):
            sections.append(("L0 ProfileCard", self._render_profile(profile, budget.l0_chars)))

        if recall_section_enabled(self.memo_settings, "current_state"):
            state_text = self._render_current_state(
                campaign_id=campaign_id, scene_id=scene_id, location_id=location_id
            )
            if state_text:
                sections.append(("Current State", state_text))

        if recall_section_enabled(self.memo_settings, "world_truth"):
            facts = self._allowed_world_facts(
                campaign_id=campaign_id, actor_id=actor_id, actor_type=actor_type, as_of=in_world_time,
                _authorization_decision=decision,
            )
            if facts:
                sections.append(("WorldTruth allowed to actor", self._render_fact_lines(facts, budget.l1_chars)))

        if recall_section_enabled(self.memo_settings, "actor_belief"):
            beliefs = self._allowed_actor_beliefs(
                campaign_id=campaign_id, actor_id=actor_id, actor_type=actor_type,
                _authorization_decision=decision,
            )
            if beliefs:
                sections.append(("ActorBelief", self._render_belief_lines(beliefs, budget.l1_chars)))

        evidence = []
        if recall_enabled(self.memo_settings) and recall_section_enabled(self.memo_settings, "evidence"):
            evidence = self._retrieve_memory_items(
                campaign_id=campaign_id, actor_id=actor_id, actor_type=actor_type, query=query,
                active_quest_ids=active_quest_ids or [], location_id=location_id,
                hit_limit=budget.hit_limit, max_chars=max_chars,
                authorized_event_ids=set(decision.trace["authorized_candidate_ids"]),
                ranking_trace=decision.trace,
            )
            decision.trace["selected_evidence_ids"] = [item["source_event_id"] for item in evidence]
        self._complete_product_trace(
            decision,
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        # Ordinary recall does not expose verbatim spans; its product trace must
        # describe that surface rather than the direct authorization interface.
        decision.trace["returned_spans"] = []
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
            policy_trace=decision.trace,
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

    def _render_current_state(
        self, *, campaign_id: str, scene_id: str | None, location_id: str | None
    ) -> str:
        if scene_id:
            row = self._conn().execute(
                "SELECT in_world_time, location_id FROM scene_record WHERE scene_id=? AND campaign_id=?",
                (scene_id, campaign_id),
            ).fetchone()
            if row:
                return f"时间：{row['in_world_time']}\n地点：{row['location_id'] or '未指定'}"
        if location_id:
            return f"地点：{location_id}"
        row = self._conn().execute(
            "SELECT in_world_time, location_id FROM scene_record WHERE campaign_id=? "
            "ORDER BY scene_time_sort DESC LIMIT 1",
            (campaign_id,),
        ).fetchone()
        if row:
            return f"时间：{row['in_world_time']}\n地点：{row['location_id'] or '未指定'}"
        return ""

    def _allowed_world_facts(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        as_of: str | None = None,
        _authorization_decision: Any | None = None,
    ) -> list[dict[str, Any]]:
        decision = _authorization_decision or self.authorized_evidence(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query="",
            budget=1000,
        )
        allowed_event_ids = set(decision.trace["authorized_candidate_ids"])
        rows = self._conn().execute("SELECT * FROM world_fact ORDER BY created_at DESC LIMIT 100").fetchall()
        facts: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            if as_of and not self._valid_as_of(data.get("valid_from"), data.get("valid_to"), as_of):
                continue
            source_event_id = data.get("source_event_id")
            if source_event_id not in allowed_event_ids:
                continue
            data["object"] = _loads(data.pop("object_json"), {})
            facts.append(data)
        return facts

    def _allowed_actor_beliefs(
        self, *, campaign_id: str, actor_id: str, actor_type: str, _authorization_decision: Any | None = None,
    ) -> list[dict[str, Any]]:
        decision = _authorization_decision or self.authorized_evidence(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query="",
            budget=1000,
        )
        allowed_event_ids = set(decision.trace["authorized_candidate_ids"])
        return [
            belief
            for belief in self.list_actor_beliefs(actor_id=actor_id)
            if belief.get("source_event_id") in allowed_event_ids
        ]

    def _render_fact_lines(self, facts: list[dict[str, Any]], max_chars: int) -> str:
        lines = [f"- {f['subject_id']} {f['predicate']}: {f['object'].get('summary', '')}" for f in facts]
        return _clamp("\n".join(lines), max_chars)

    def _render_belief_lines(self, beliefs: list[dict[str, Any]], max_chars: int) -> str:
        lines = [
            f"- {b['actor_id']} {b['belief_status']} {b['subject_id']} {b['predicate']}: {b['object'].get('summary', '')}"
            for b in beliefs
        ]
        return _clamp("\n".join(lines), max_chars)

    def _pack_authorized_events(
        self, events: list[dict[str, Any]], *, max_chars: int
    ) -> list[dict[str, Any]]:
        packed: list[dict[str, Any]] = []
        used = 0
        for event in events:
            text = str(event["summary"])
            if packed and used + len(text) > max_chars:
                break
            packed.append(
                {
                    "memory_id": event["source_event_id"],
                    "source_event_id": event["source_event_id"],
                    "source_scene_id": event["source_scene_id"],
                    "domain": "evidence",
                    "memory_type": "belief" if event["truth_status"] == "belief" else "summary",
                    "text": text,
                    "truth_status": event["truth_status"],
                    "visibility": event["visibility"],
                    "in_world_time": event["in_world_time"],
                    "location_id": event["location_id"],
                    "scene_time_sort": event["scene_time_sort"],
                    "created_at": event["created_at"],
                    "rank_score": event["rank_score"],
                }
            )
            used += len(text)
        return packed

    def get_scene_transcript(
        self,
        *,
        campaign_id: str,
        scene_id: str,
        actor_id: str,
        actor_type: str = "npc",
        query: str | None = None,
        mode: str = "snippets",
        max_chars: int | None = 4000,
    ) -> dict[str, Any]:
        """Return only verbatim spans descended from authorized event seeds."""
        if mode != "snippets":
            raise ValueError("only mode='snippets' is supported for authorized scene evidence")

        row = self._conn().execute(
            "SELECT * FROM scene_record WHERE scene_id=? AND campaign_id=?",
            (scene_id, campaign_id),
        ).fetchone()
        if not row:
            return {"success": False, "error": "scene_not_found", "scene_id": scene_id}

        scene = dict(row)
        active_quest_ids = _loads(scene["active_quest_ids_json"], [])
        decision = self.authorized_evidence(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query=query or "",
            budget=1000,
            active_quest_ids=active_quest_ids,
            scene_id=scene_id,
        )
        spans = [{"source_event_id": event["source_event_id"], "source_scene_id": event["source_scene_id"], "text": event["source_span"]} for event in decision.events if event.get("source_span")]
        response: dict[str, Any] = {
            "success": True,
            "scene_id": scene_id,
            "campaign_id": campaign_id,
            "in_world_time": scene["in_world_time"],
            "location_id": scene["location_id"],
            "active_quest_ids": active_quest_ids,
            "transcript_hash": scene["transcript_hash"],
            "mode": mode,
            "authorized_spans": spans,
            "policy_trace": decision.trace,
            "forbidden_guard": "Only spans descended from authorized event seeds are returned; scene participation alone grants no transcript access.",
        }
        if max_chars is not None:
            used = 0
            limited: list[dict[str, Any]] = []
            for span in spans:
                remaining = max_chars - used
                if remaining <= 0:
                    break
                raw = str(span["text"]); text = raw[:remaining]
                limited.append({**span, "text": text, "truncated": len(text) < len(raw)})
                used += len(text)
            response["authorized_spans"] = limited
        response["policy_trace"]["returned_spans"] = [{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "length": len(span["text"]), "truncated": bool(span.get("truncated"))} for span in response["authorized_spans"]]
        return response

    def deep_recall(
        self,
        *,
        campaign_id: str,
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

        profile = self._profile(actor_id)
        tier = profile["tier"] if profile else ("core" if actor_type == "gm" else "recurring")
        budget = budget_for_tier(tier)
        decision = self.authorized_evidence(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            active_quest_ids=active_quest_ids or [],
            budget=1000,
            _compact_product_trace=True,
        )
        pack = self.build_memory_pack(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            scene_id=scene_id,
            location_id=location_id,
            active_quest_ids=active_quest_ids or [],
            in_world_time=in_world_time,
            max_chars=max_chars,
            _authorization_decision=decision,
        )
        selected_ids = [str(item["source_event_id"]) for item in pack.evidence]
        events_by_id = self._selected_authorized_events(
            campaign_id=campaign_id,
            selected_event_ids=selected_ids,
            authorized_event_ids=set(decision.trace["authorized_candidate_ids"]),
        )
        spans_by_scene: dict[str, list[dict[str, Any]]] = {}
        # Pack evidence has B0 order. Preserve it through scene selection rather
        # than iterating authorization's recency order.
        for item in pack.evidence:
            event = events_by_id.get(str(item.get("source_event_id")))
            if not event or not event.get("source_span"):
                continue
            span = {"source_event_id": event["source_event_id"], "source_scene_id": event["source_scene_id"], "text": event["source_span"]}
            spans_by_scene.setdefault(str(span["source_scene_id"]), []).append(span)
        scene_evidence = [
            {
                "success": True,
                "scene_id": sid,
                "campaign_id": campaign_id,
                "authorized_spans": self._limit_spans(spans, per_scene_chars),
                "forbidden_guard": "Spans reuse the same authorized evidence decision as ordinary recall.",
            }
            for sid, spans in list(spans_by_scene.items())[: max(0, scene_limit)]
        ]
        delivered_spans = [span for scene in scene_evidence for span in scene["authorized_spans"]]
        pack.policy_trace["returned_spans"] = [
            {"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"],
             "length": len(span["text"]), "truncated": bool(span.get("truncated"))}
            for span in delivered_spans
        ]
        return {
            "success": True,
            "actor_id": pack.actor_id,
            "actor_type": pack.actor_type,
            "rendered": pack.render(),
            "sections": pack.sections,
            "evidence": pack.evidence,
            "scene_evidence": scene_evidence,
            "policy_trace": pack.policy_trace,
            "forbidden_guard": pack.forbidden_guard + " Verbatim scene snippets were also ACL-checked.",
        }

    def _limit_spans(self, spans: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
        used = 0
        limited: list[dict[str, Any]] = []
        for span in spans:
            remaining = max_chars - used
            if remaining <= 0:
                break
            raw = str(span["text"])
            text = raw[:remaining]
            limited.append({**span, "text": text, "truncated": len(text) < len(raw)})
            used += len(text)
        return limited

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
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        query: str,
        active_quest_ids: list[str],
        location_id: str | None,
        hit_limit: int,
        max_chars: int,
        authorized_event_ids: set[str],
        ranking_trace: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self.retrieval_ranker is not None:
            return self._retrieve_ranked_authorized_events(
                campaign_id=campaign_id, query=query, hit_limit=hit_limit,
                max_chars=max_chars, authorized_event_ids=authorized_event_ids,
                ranking_trace=ranking_trace,
            )
        rows = self._projection_candidates(campaign_id)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item in rows:
            source_event_id = str(item.get("source_event_id") or "")
            if not source_event_id or source_event_id not in authorized_event_ids:
                continue
            scored.append((self._rank_score(
                item,
                query=query,
                active_quest_ids=active_quest_ids,
                location_id=location_id,
            ), item))
        # Python's stable sort preserves the historical created-at/event order
        # for equal scores.
        scored.sort(key=lambda pair: pair[0], reverse=True)

        packed: list[dict[str, Any]] = []
        used = 0
        for rank_score, cached_item in scored:
            if len(packed) >= hit_limit:
                break
            text_len = len(str(cached_item.get("text", "")))
            if packed and used + text_len > max_chars:
                break
            used += text_len
            item = dict(cached_item)
            item["rank_score"] = rank_score
            self._attach_joined_scene_context(item)
            packed.append(self._memory_item_from_row(item))
        return packed

    def _retrieve_ranked_authorized_events(
        self,
        *,
        campaign_id: str,
        query: str,
        hit_limit: int,
        max_chars: int,
        authorized_event_ids: set[str],
        ranking_trace: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Pass only ACL-approved source rows to an injected product ranker."""
        rows_by_id: dict[str, dict[str, Any]] = {}
        selected_ids = sorted(authorized_event_ids)
        for start in range(0, len(selected_ids), 900):
            batch = selected_ids[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn().execute(
                "SELECT se.event_id, se.scene_id, se.event_type, se.actor_id, se.target_id, se.summary, se.source_span, se.payload_json, "
                "se.truth_status, se.visibility, se.branch_id, se.branch_status, "
                "se.access_owner_id, se.access_scope_id, se.belief_owner_id, se.witness_set_json, se.created_at, "
                "se.related_entities_json, se.related_quests_json, se.related_locations_json, "
                "sr.in_world_time, sr.location_id, sr.scene_time_sort FROM scene_event se JOIN scene_record sr ON sr.scene_id=se.scene_id "
                "WHERE sr.campaign_id=? AND se.event_id IN (" + placeholders + ")",
                [campaign_id, *batch],
            ).fetchall()
            rows_by_id.update({str(row["event_id"]): dict(row) for row in rows})
        if set(rows_by_id) != authorized_event_ids:
            raise PermissionError("authorized evidence disappeared before product ranking")
        candidates: list[AuthorizedRetrievalCandidate] = []
        for event_id in selected_ids:
            row = rows_by_id[event_id]
            payload = _strict_product_json(row["payload_json"], "payload_json", dict)
            policy_tuple = tuple(row.get(name) for name in (
                "truth_status", "visibility", "branch_id", "branch_status",
                "access_owner_id", "access_scope_id", "belief_owner_id", "witness_set_json",
            ))
            if "retrieval_checkpoint_id" not in payload:
                checkpoint = str(row["scene_id"])
            else:
                checkpoint = payload["retrieval_checkpoint_id"]
                if not isinstance(checkpoint, str) or not checkpoint.strip():
                    raise ValueError("malformed product retrieval_checkpoint_id")
            checkpoint = checkpoint.strip()
            if "retrieval_ranking_key" not in payload:
                ranking_key = event_id
            else:
                ranking_key = payload["retrieval_ranking_key"]
                if not isinstance(ranking_key, str) or not ranking_key.strip():
                    raise ValueError("malformed product retrieval_ranking_key")
            ranking_key = ranking_key.strip()
            summary = str(row["summary"] or "")
            raw = str(row["source_span"] or summary)
            candidates.append(AuthorizedRetrievalCandidate(
                source_event_id=event_id, source_scene_id=str(row["scene_id"]), raw_text=raw,
                observation=structured_observation(
                    summary=summary, event_type=row["event_type"], actor_id=row["actor_id"],
                    target_id=row["target_id"],
                    related_entities=_strict_product_json(row["related_entities_json"], "related_entities_json", list),
                    related_quests=_strict_product_json(row["related_quests_json"], "related_quests_json", list),
                    related_locations=_strict_product_json(row["related_locations_json"], "related_locations_json", list),
                    in_world_time=row["in_world_time"], location_id=row["location_id"],
                ),
                checkpoint_key=checkpoint, policy_tuple=policy_tuple,
                chronological_order_key=(int(row["scene_time_sort"]), ranking_key),
                ranking_key=ranking_key,
            ))
        result = self.retrieval_ranker.rank(query=query, candidates=candidates)
        if not isinstance(result, RankingResult):
            raise TypeError("retrieval ranker must return RankingResult")
        returned = result.ranked_event_ids
        if len(returned) != len(set(returned)):
            raise ValueError("retrieval ranker returned duplicate source_event_id")
        unknown = set(returned) - authorized_event_ids
        if unknown:
            raise PermissionError("retrieval ranker returned an unauthorized source_event_id")
        if not isinstance(result.scores, dict) or set(result.scores) != set(returned):
            raise ValueError("retrieval ranker must provide one score for every returned source_event_id")
        scores: dict[str, float] = {}
        for event_id in returned:
            value = result.scores[event_id]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("retrieval ranker score must be finite numeric")
            scores[event_id] = float(value)
        if not isinstance(result.trace, dict):
            raise TypeError("retrieval ranker trace must be a dictionary")
        selected_trace = result.trace.get("selected")
        if not isinstance(selected_trace, list):
            raise ValueError("retrieval ranker trace must contain selected entries")
        selected_by_id: dict[str, dict[str, Any]] = {}
        for entry in selected_trace:
            if not isinstance(entry, dict) or not isinstance(entry.get("source_event_id"), str):
                raise ValueError("retrieval ranker selected trace entry is malformed")
            event_id = entry["source_event_id"]
            if event_id in selected_by_id:
                raise ValueError("retrieval ranker selected trace has duplicate source_event_id")
            selected_by_id[event_id] = entry
        if set(selected_by_id) != set(returned):
            raise ValueError("retrieval ranker selected trace must match returned source_event_ids")
        packed: list[dict[str, Any]] = []
        used = 0
        for event_id in returned:
            if len(packed) >= hit_limit:
                break
            row = rows_by_id[event_id]
            text = str(row["summary"] or "")
            if packed and used + len(text) > max_chars:
                break
            used += len(text)
            packed.append({
                "memory_id": event_id, "source_event_id": event_id, "source_scene_id": row["scene_id"],
                "domain": "evidence", "memory_type": "belief" if row["truth_status"] == "belief" else "summary",
                "text": text, "truth_status": row["truth_status"], "visibility": row["visibility"],
                "in_world_time": row["in_world_time"], "location_id": row["location_id"],
                "created_at": row["created_at"], "rank_score": scores[event_id],
            })
        if ranking_trace is not None:
            trace = dict(result.trace)
            trace["selected"] = [selected_by_id[item["source_event_id"]] for item in packed]
            ranking_trace["retrieval_ranking"] = trace
        return packed

    def _projection_candidates(self, campaign_id: str) -> list[dict[str, Any]]:
        """Cache one rank-equivalent enabled projection per source event."""
        self._refresh_read_caches()
        settings_digest = hashlib.sha256(_json(self.memo_settings).encode("utf-8")).hexdigest()
        cache_key = (campaign_id, settings_digest)
        cached = self._projection_cache.get(cache_key)
        if cached is not None:
            return cached
        rows = self._conn().execute(
            "SELECT mi.*, sr.campaign_id AS scene_campaign_id, sr.in_world_time AS scene_in_world_time, "
            "sr.scene_time_sort AS scene_time_sort, sr.location_id AS scene_location_id, "
            "sr.participants_json AS scene_participants_json, sr.witnesses_json AS scene_witnesses_json, "
            "sr.created_at AS scene_created_at FROM memory_item mi JOIN scene_record sr ON sr.scene_id=mi.source_scene_id "
            "WHERE sr.campaign_id=? ORDER BY mi.created_at DESC", (campaign_id,)
        ).fetchall()
        best_by_event: dict[str, dict[str, Any]] = {}
        rank_fields_by_event: dict[str, tuple[Any, ...]] = {}
        for row in rows:
            item = dict(row)
            source_event_id = str(item.get("source_event_id") or "")
            if not source_event_id:
                continue
            if not domain_recall_enabled(self.memo_settings, str(item.get("domain"))):
                continue
            rank_fields = (
                item.get("text"), item.get("importance"), item.get("emotional_weight"),
                item.get("related_quests_json"), item.get("related_locations_json"),
                item.get("source_scene_id"), item.get("created_at"),
            )
            prior_fields = rank_fields_by_event.setdefault(source_event_id, rank_fields)
            if prior_fields != rank_fields:
                raise ValueError("memory projections for one source event diverged in rank fields")
            item["related_quests"] = _loads(item["related_quests_json"], [])
            item["related_locations"] = _loads(item["related_locations_json"], [])
            item.setdefault("in_world_time", item["scene_in_world_time"])
            item.setdefault("location_id", item["scene_location_id"])
            item.setdefault("scene_time_sort", item["scene_time_sort"])
            prior = best_by_event.get(source_event_id)
            item_projection_rank = (-0.05 if item.get("domain") == "canon" else 0.0, str(item.get("memory_id")))
            prior_projection_rank = (
                -0.05 if prior and prior.get("domain") == "canon" else 0.0,
                str(prior.get("memory_id")) if prior else "",
            )
            if prior is None or item_projection_rank > prior_projection_rank:
                best_by_event[source_event_id] = item
        candidates = list(best_by_event.values())
        self._projection_cache[cache_key] = candidates
        return candidates

    def _attach_joined_scene_context(self, item: dict[str, Any]) -> None:
        """Attach the scene already joined by retrieval; never N+1 lookup it."""
        scene_id = item.get("source_scene_id")
        if not scene_id:
            return
        item["scene"] = {
            "scene_id": scene_id,
            "campaign_id": item.pop("scene_campaign_id"),
            "in_world_time": item.pop("scene_in_world_time"),
            "scene_time_sort": item.pop("scene_time_sort"),
            "location_id": item.pop("scene_location_id"),
            "participants": _loads(item.pop("scene_participants_json"), []),
            "witnesses": _loads(item.pop("scene_witnesses_json"), []),
            "created_at": item.pop("scene_created_at"),
        }
        item.setdefault("in_world_time", item["scene"]["in_world_time"])
        item.setdefault("location_id", item["scene"]["location_id"])
        item.setdefault("scene_time_sort", item["scene"]["scene_time_sort"])

    def _complete_product_trace(
        self,
        decision: Any,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
    ) -> None:
        """Materialize product trace detail only after ranking has selected IDs."""
        EvidenceAuthorizer(self._conn()).add_selected_candidates(
            decision,
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            selected_event_ids=list(decision.trace.get("selected_evidence_ids", [])),
        )

    def _selected_authorized_events(
        self,
        *,
        campaign_id: str,
        selected_event_ids: list[str],
        authorized_event_ids: set[str],
    ) -> dict[str, dict[str, Any]]:
        """Load spans only for ranked evidence already approved by the decision."""
        selected = list(dict.fromkeys(selected_event_ids))
        unauthorized = [event_id for event_id in selected if event_id not in authorized_event_ids]
        if unauthorized:
            raise PermissionError("selected evidence is absent from the authorization decision")
        if not selected:
            return {}
        rows_by_id: dict[str, dict[str, Any]] = {}
        for start in range(0, len(selected), 900):
            batch = selected[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn().execute(
                "SELECT se.event_id, se.scene_id, se.source_span FROM scene_event se "
                "JOIN scene_record sr ON sr.scene_id=se.scene_id "
                "WHERE sr.campaign_id=? AND se.event_id IN (" + placeholders + ")",
                [campaign_id, *batch],
            ).fetchall()
            rows_by_id.update({str(row["event_id"]): {"source_event_id": row["event_id"], "source_scene_id": row["scene_id"], "source_span": row["source_span"]} for row in rows})
        missing = [event_id for event_id in selected if event_id not in rows_by_id]
        if missing:
            raise PermissionError("selected evidence disappeared before deep recall")
        return rows_by_id

    def _attach_scene_context(self, item: dict[str, Any]) -> None:
        scene_id = item.get("source_scene_id")
        if not scene_id:
            return
        row = self._conn().execute(
            """
            SELECT scene_id, campaign_id, in_world_time, scene_time_sort, location_id,
                   participants_json, witnesses_json, created_at
            FROM scene_record
            WHERE scene_id=?
            """,
            (scene_id,),
        ).fetchone()
        if not row:
            return
        item["scene"] = {
            "scene_id": row["scene_id"],
            "campaign_id": row["campaign_id"],
            "in_world_time": row["in_world_time"],
            "scene_time_sort": row["scene_time_sort"],
            "location_id": row["location_id"],
            "participants": _loads(row["participants_json"], []),
            "witnesses": _loads(row["witnesses_json"], []),
            "created_at": row["created_at"],
        }
        item.setdefault("in_world_time", row["in_world_time"])
        item.setdefault("location_id", row["location_id"])
        item.setdefault("scene_time_sort", row["scene_time_sort"])

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
