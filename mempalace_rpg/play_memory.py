"""Portable accepted-memory store. World authority and semantic decisions stay in Host.

This additive profile never mutates the historical kernel or frozen rankers.
Scopes are immutable process-local capabilities over persisted, append-only data.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import math
import secrets
import sqlite3
import time

from .evidence_protocol import canonical_hash, strict_json_loads
from .play_memory_protocol import (BATCH, BINDING, MAX_NAVIGATION_SUMMARIES,
                                   MAX_NAVIGATION_SUMMARIES_BYTES,
                                   MAX_NAVIGATION_SUMMARY_BYTES, MAX_NAVIGATION_CARDS,
                                   MIN_NAVIGATION_CARD_BUDGET_BYTES,
                                   MAX_NAVIGATION_CARD_BUDGET_BYTES, RPC_PARAMS,
                                   VERSION, batch_hash, fail, validate)
from .retrieval import AuthorizedRetrievalCandidate, RankingResult, _bm25
from .play_memory_characters import CharacterImportancePolicy, DETAIL_POLICY, detail_records
from .play_memory_directory import actor_directory, resolve_actors
from .play_memory_library import (LIBRARY_POLICY, blocked_memory_actors,
                                  character_library_projection, seen_identities,
                                  validate_library_records)
from . import play_memory_lifecycle as episodic_lifecycle

logger = logging.getLogger(__name__)
SCENE_SUMMARIES_RETRIEVAL_POLICY = "scene-summaries-v1"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _hash_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _key(record):
    kind = record["kind"]
    if kind in ("profile", "state"):
        return kind, record["actorId"], record["field"]
    if kind == "relationship":
        return kind, record["fromActorId"], record["toActorId"]
    if kind == "attitude":
        return kind, record["actorId"], record["acquisitionId"]
    if kind == "exception":
        return kind, record["actorId"], record["backgroundId"]
    if kind == "character-library":
        return kind, record["actorId"]
    return kind, record["id"]


def effective_records(records, story_order):
    """Reusable point projection for DTO adapters; expiry never resurrects old state."""
    latest = {}
    for record in records:
        if record["order"] > story_order:
            continue
        key = _key(record)
        old = latest.get(key)
        if old is not None and old["order"] == record["order"] and old["id"] != record["id"]:
            fail("ambiguous effective record order", "record-conflict", -32009)
        if old is None or old["order"] < record["order"]:
            latest[key] = record
    return [r for r in latest.values() if r.get("validUntil", story_order + 1) > story_order]


def project_attitudes(records, actor):
    """Explicit claim identity carries stance across hearing the same claim again.

    A missing claimId deliberately keeps acquisition identities separate; text
    similarity never merges propositions or invents the belief in their negation.
    """
    acquisitions = {r["id"]: r for r in records if r["kind"] == "acquisition" and r["actorId"] == actor}
    latest = {}
    for record in records:
        if record["kind"] != "attitude" or record["actorId"] != actor:
            continue
        target = acquisitions.get(record["acquisitionId"])
        if target is None:
            continue
        key = ("claim", target["claimId"]) if "claimId" in target else ("acquisition", target["id"])
        old = latest.get(key)
        if old is not None and old["order"] == record["order"] and old["id"] != record["id"]:
            fail("ambiguous claim attitude order", "record-conflict", -32009)
        if old is None or old["order"] < record["order"]:
            latest[key] = record
    result = {}
    for identifier, acquisition in acquisitions.items():
        key = ("claim", acquisition["claimId"]) if "claimId" in acquisition else ("acquisition", identifier)
        result[identifier] = latest[key]["attitude"] if key in latest else "unstated"
    return result


class MechanicalRanker:
    """Explicit lexical profile reusing the existing production BM25 operation."""
    identity = "play-memory-mechanical-bm25-v1"

    def rank(self, *, query, candidates):
        scores = _bm25(query, [c.observation for c in candidates], [c.source_event_id for c in candidates])
        ids = sorted(scores, key=lambda identifier: (-scores[identifier], identifier))
        return RankingResult(ids, scores, {"profile": self.identity})


class PlayMemoryStore:
    def __init__(self, path, *, host_token, ranker=None, ranker_identity=None,
                 max_scopes=128, max_records=100000, fault_hook=None, importance_policy=None):
        if not isinstance(host_token, str) or not host_token:
            raise ValueError("nonempty Host token required")
        self.host_token = host_token
        self.importance_policy = importance_policy if importance_policy is not None else CharacterImportancePolicy()
        if not isinstance(self.importance_policy, CharacterImportancePolicy):
            raise ValueError("importance_policy must be CharacterImportancePolicy")
        self.ranker = ranker if ranker is not None else MechanicalRanker()
        self.ranker_identity = ranker_identity or getattr(self.ranker, "identity", None)
        if not self.ranker_identity:
            raise ValueError("injected ranker requires explicit stable identity")
        self.vector_mode = "live-authorized-ranking" if getattr(self.ranker, "encoder", None) is not None else "none"
        self.max_scopes, self.max_records = max_scopes, max_records
        self.fault_hook = fault_hook
        self.scopes = {}
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS play_meta(version TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS play_epochs(campaign TEXT NOT NULL, branch TEXT NOT NULL,
                epoch INTEGER NOT NULL, PRIMARY KEY(campaign,branch));
            CREATE TABLE IF NOT EXISTS play_branches(
                campaign TEXT NOT NULL, branch TEXT NOT NULL, parent TEXT,
                parent_revision INTEGER, parent_batch TEXT,
                head_batch TEXT NOT NULL, head_revision INTEGER NOT NULL,
                indexed_revision INTEGER NOT NULL DEFAULT -1, index_identity TEXT,
                PRIMARY KEY(campaign,branch));
            CREATE TABLE IF NOT EXISTS play_batches(
                campaign TEXT NOT NULL, batch TEXT NOT NULL, branch TEXT NOT NULL,
                revision INTEGER NOT NULL, epoch INTEGER NOT NULL, payload_hash TEXT NOT NULL,
                receipt TEXT NOT NULL, PRIMARY KEY(campaign,batch), UNIQUE(campaign,branch,revision));
            CREATE TABLE IF NOT EXISTS play_records(
                campaign TEXT NOT NULL, id TEXT NOT NULL, branch TEXT NOT NULL,
                revision INTEGER NOT NULL, kind TEXT NOT NULL, story_order INTEGER NOT NULL,
                actor TEXT, scene TEXT, body TEXT NOT NULL, body_hash TEXT NOT NULL,
                PRIMARY KEY(campaign,id));
            CREATE INDEX IF NOT EXISTS play_record_origin ON play_records(campaign,branch,revision,story_order);
            CREATE INDEX IF NOT EXISTS play_record_actor ON play_records(campaign,actor,kind,story_order);
            CREATE INDEX IF NOT EXISTS play_record_scene ON play_records(campaign,scene);
            CREATE TABLE IF NOT EXISTS play_edges(
                campaign TEXT NOT NULL, record_id TEXT NOT NULL, relation TEXT NOT NULL,
                target TEXT NOT NULL, PRIMARY KEY(campaign,record_id,relation,target));
            CREATE INDEX IF NOT EXISTS play_edge_target ON play_edges(campaign,relation,target);
            CREATE TABLE IF NOT EXISTS play_lifecycle_clock(id INTEGER PRIMARY KEY AUTOINCREMENT);
            CREATE TABLE IF NOT EXISTS play_lifecycle_archive_markers(
                campaign TEXT NOT NULL, branch TEXT NOT NULL, revision INTEGER NOT NULL,
                story_order INTEGER NOT NULL, event_id TEXT NOT NULL, root_id TEXT NOT NULL,
                archived_floor INTEGER NOT NULL, expires_after_floor INTEGER NOT NULL,
                related_event_ids TEXT NOT NULL, related_record_ids TEXT NOT NULL,
                archive_kind TEXT NOT NULL, sequence INTEGER NOT NULL,
                PRIMARY KEY(campaign,branch,revision,event_id,root_id));
            CREATE INDEX IF NOT EXISTS play_lifecycle_marker_scope
                ON play_lifecycle_archive_markers(campaign,branch,revision,story_order);
            CREATE TABLE IF NOT EXISTS play_lifecycle_archive_cache(
                campaign TEXT NOT NULL, branch TEXT NOT NULL, revision INTEGER NOT NULL,
                marker_branch TEXT NOT NULL, marker_revision INTEGER NOT NULL,
                event_id TEXT NOT NULL, root_id TEXT NOT NULL,
                payload BLOB NOT NULL, payload_hash TEXT NOT NULL,
                PRIMARY KEY(campaign,branch,revision,marker_branch,marker_revision,event_id,root_id));
            CREATE INDEX IF NOT EXISTS play_lifecycle_cache_marker
                ON play_lifecycle_archive_cache(campaign,marker_branch,marker_revision,event_id,root_id);
            CREATE TABLE IF NOT EXISTS play_lifecycle_access(
                sequence INTEGER PRIMARY KEY, campaign TEXT NOT NULL, branch TEXT NOT NULL,
                revision INTEGER NOT NULL, epoch INTEGER NOT NULL, story_order INTEGER NOT NULL,
                accepted_floor INTEGER NOT NULL, marker_branch TEXT NOT NULL,
                marker_revision INTEGER NOT NULL, event_id TEXT NOT NULL, root_id TEXT NOT NULL,
                operation TEXT NOT NULL, expires_after_floor INTEGER NOT NULL,
                restored_record_ids TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS play_lifecycle_access_scope
                ON play_lifecycle_access(campaign,branch,revision,story_order,accepted_floor);
            CREATE TABLE IF NOT EXISTS play_lifecycle_fork_access_cutoffs(
                campaign TEXT NOT NULL, child_branch TEXT NOT NULL, ancestor_branch TEXT NOT NULL,
                through_revision INTEGER NOT NULL, sequence_cutoff INTEGER NOT NULL,
                PRIMARY KEY(campaign,child_branch,ancestor_branch));
        """)
        rows = self.db.execute("SELECT version FROM play_meta").fetchall()
        if not rows:
            self.db.execute("INSERT INTO play_meta VALUES (?)", (VERSION,))
        elif len(rows) != 1 or rows[0][0] != VERSION:
            self.db.close()
            raise ValueError("incompatible portable memory database schema")

    def close(self):
        self.scopes.clear()
        self.db.close()

    def _host(self, params):
        if not hmac.compare_digest(params.get("hostToken", ""), self.host_token):
            fail("trusted Host capability required", "forbidden", -32001)

    def capabilities(self, params):
        return {"protocol": VERSION, "schemaVersion": VERSION, "profile": self.ranker_identity,
                "encoderIdentity": getattr(getattr(self.ranker, "encoder", None), "identity", None),
                "methods": list(RPC_PARAMS), "authority": "host-accepted-only",
                "representations": ["accepted", "original"], "deltaPolicy": "none-live-authorized-scan",
                "queryCoveragePolicy": "complete-authorized-live-ranking-v1" if self.vector_mode == "live-authorized-ranking" else "complete-authorized-lexical-v1",
                "indexPolicy": "no-persistent-vector-index", "scopeLifetime": "process-and-epoch",
                "vectorMode": self.vector_mode, "indexedAvailability": "not-applicable-live-ranking"}

    def receipt(self, params):
        self._host(params)
        row = self.db.execute("SELECT receipt FROM play_batches WHERE campaign=? AND batch=?",
                              (params["campaignId"], params["batchId"])).fetchone()
        return {"status": "applied", "receipt": strict_json_loads(row[0])} if row else {"status": "not-applied"}

    def _branch(self, campaign, branch):
        return self.db.execute("SELECT * FROM play_branches WHERE campaign=? AND branch=?", (campaign, branch)).fetchone()

    def _lineage(self, campaign, branch, through_revision):
        result, visited = {}, set()
        while True:
            if branch in visited:
                fail("cyclic branch lineage", "integrity-error", -32009)
            visited.add(branch)
            row = self._branch(campaign, branch)
            if row is None or through_revision > row["head_revision"]:
                fail("required readable revision unavailable", "not-ready", -32011)
            if through_revision != row["head_revision"] and not self.db.execute("SELECT 1 FROM play_batches WHERE campaign=? AND branch=? AND revision=?", (campaign, branch, through_revision)).fetchone():
                fail("scope revision is not a completed World transaction boundary", "not-ready", -32011)
            result[branch] = through_revision
            if row["parent"] is None:
                return result
            branch, through_revision = row["parent"], row["parent_revision"]

    def _lifecycle_sequence(self):
        cursor = self.db.execute("INSERT INTO play_lifecycle_clock DEFAULT VALUES")
        return cursor.lastrowid

    def _lifecycle_cache_row(self, campaign, lineage, marker):
        for storage_branch, cutoff in lineage.items():
            row = self.db.execute(
                "SELECT payload,payload_hash FROM play_lifecycle_archive_cache "
                "WHERE campaign=? AND branch=? AND revision<=? AND marker_branch=? "
                "AND marker_revision=? AND event_id=? AND root_id=? ORDER BY revision DESC LIMIT 1",
                (campaign, storage_branch, cutoff, marker["branchId"], marker["revision"],
                 marker["eventId"], marker["rootId"])).fetchone()
            if row is not None:
                return row
        return None

    def _lifecycle_markers(self, campaign, lineage, story_order):
        result = []
        for branch_rank, (branch, cutoff) in enumerate(lineage.items()):
            rows = self.db.execute(
                "SELECT * FROM play_lifecycle_archive_markers WHERE campaign=? AND branch=? "
                "AND revision<=? AND story_order<=? ORDER BY revision,sequence",
                (campaign, branch, cutoff, story_order)).fetchall()
            for row in rows:
                marker = {"eventId": row["event_id"], "rootId": row["root_id"],
                    "archivedFloor": row["archived_floor"],
                    "expiresAfterFloor": row["expires_after_floor"],
                    "relatedEventIds": strict_json_loads(row["related_event_ids"]),
                    "relatedRecordIds": strict_json_loads(row["related_record_ids"]),
                    "kind": row["archive_kind"], "branchId": branch,
                    "revision": row["revision"], "storyOrder": row["story_order"],
                    "sequence": row["sequence"], "_branchRank": branch_rank}
                marker["payloadAvailable"] = self._lifecycle_cache_row(campaign, lineage, marker) is not None
                result.append(marker)
        return result

    def _lifecycle_accesses(self, campaign, lineage, story_order, floor, *,
                            current_branch=None, epoch=None):
        result = []
        current_branch = current_branch or next(iter(lineage), None)
        for branch_rank, (branch, cutoff) in enumerate(lineage.items()):
            sequence_cutoff = None
            if branch != current_branch:
                cutoff_row = self.db.execute(
                    "SELECT sequence_cutoff FROM play_lifecycle_fork_access_cutoffs "
                    "WHERE campaign=? AND child_branch=? AND ancestor_branch=?",
                    (campaign, current_branch, branch)).fetchone()
                sequence_cutoff = cutoff_row[0] if cutoff_row is not None else 0
            rows = self.db.execute(
                "SELECT * FROM play_lifecycle_access WHERE campaign=? AND branch=? "
                "AND revision<=? AND story_order<=? AND accepted_floor<=? "
                "AND (? IS NULL OR sequence<=?) AND (? IS NULL OR epoch<=?) ORDER BY sequence",
                (campaign, branch, cutoff, story_order, floor, sequence_cutoff, sequence_cutoff,
                 epoch if branch == current_branch else None,
                 epoch if branch == current_branch else None)).fetchall()
            for row in rows:
                result.append({"operation": row["operation"], "rootId": row["root_id"],
                    "eventId": row["event_id"], "markerBranch": row["marker_branch"],
                    "markerRevision": row["marker_revision"],
                    "expiresAfterFloor": row["expires_after_floor"],
                    "restoredRecordIds": strict_json_loads(row["restored_record_ids"]),
                    "branchId": branch, "revision": row["revision"], "epoch": row["epoch"],
                    "storyOrder": row["story_order"], "acceptedFloor": row["accepted_floor"],
                    "sequence": row["sequence"], "_branchRank": branch_rank})
        return result

    def _record_lifecycle_access(self, *, campaign, branch, revision, epoch, story_order, floor,
                                 marker, operation, expires_after_floor, restored_record_ids=()):
        sequence = self._lifecycle_sequence()
        self.db.execute("INSERT INTO play_lifecycle_access VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sequence, campaign, branch, revision, epoch, story_order, floor, marker["branchId"],
             marker["revision"], marker["eventId"], marker["rootId"], operation,
             expires_after_floor, _json(sorted(set(restored_record_ids)))))
        return {"operation": operation, "rootId": marker["rootId"], "eventId": marker["eventId"],
            "markerBranch": marker["branchId"], "markerRevision": marker["revision"],
            "expiresAfterFloor": expires_after_floor,
            "restoredRecordIds": sorted(set(restored_record_ids)), "branchId": branch,
            "revision": revision, "epoch": epoch, "storyOrder": story_order, "acceptedFloor": floor,
            "sequence": sequence, "_branchRank": 0}

    def _capture_lifecycle_fork_cutoffs(self, campaign, child_branch, parent_branch, parent_revision):
        lineage = self._lineage(campaign, parent_branch, parent_revision)
        visible = self._lifecycle_accesses(campaign, lineage, 9007199254740991,
            9007199254740991, current_branch=parent_branch)
        maxima = {branch: 0 for branch in lineage}
        for access in visible:
            maxima[access["branchId"]] = max(maxima[access["branchId"]], access["sequence"])
        for ancestor, cutoff in lineage.items():
            self.db.execute("INSERT INTO play_lifecycle_fork_access_cutoffs VALUES (?,?,?,?,?)",
                (campaign, child_branch, ancestor, cutoff, maxima[ancestor]))

    def _refresh_lifecycle_scope(self, scope, *, refresh_cache=False, purge=False):
        if refresh_cache:
            for marker in scope["lifecycleMarkers"]:
                marker["payloadAvailable"] = self._lifecycle_cache_row(
                    scope["binding"]["campaignId"], scope["lineage"], marker) is not None
        projected = episodic_lifecycle.project(scope["records"], scope["baseEffective"],
            scope["lifecycleMarkers"], scope["lifecycleAccesses"], scope["acceptedFloor"])
        if purge:
            expired = [marker for marker in projected["archiveMarkers"]
                       if scope["acceptedFloor"] >= marker["expiresAfterFloor"]]
            for public_marker in expired:
                internal = projected["latestMarkers"][public_marker["rootId"]]
                deleted = self.db.execute(
                    "DELETE FROM play_lifecycle_archive_cache WHERE campaign=? AND branch=? "
                    "AND marker_branch=? AND marker_revision=? AND event_id=? AND root_id=?",
                    (scope["binding"]["campaignId"], scope["binding"]["branchId"],
                     internal["branchId"], internal["revision"], internal["eventId"],
                     internal["rootId"])).rowcount
                if deleted:
                    logger.info("play archive cache purged campaign=%s branch=%s event=%s floor=%s",
                        scope["binding"]["campaignId"], scope["binding"]["branchId"],
                        internal["eventId"], scope["acceptedFloor"])
            if expired:
                for marker in scope["lifecycleMarkers"]:
                    marker["payloadAvailable"] = self._lifecycle_cache_row(
                        scope["binding"]["campaignId"], scope["lineage"], marker) is not None
                projected = episodic_lifecycle.project(scope["records"], scope["baseEffective"],
                    scope["lifecycleMarkers"], scope["lifecycleAccesses"], scope["acceptedFloor"])
        active_ids = set(projected["activeRecordIds"])
        scope["effective"] = [record for record in scope["baseEffective"] if record["id"] in active_ids]
        scope["activeRecordIds"] = active_ids
        scope["lifecycleProjection"] = projected
        scope["lifecycleGraph"] = {key: copy.deepcopy(projected[key]) for key in (
            "acceptedFloor", "activeRecordIds", "aliases", "nodes", "archiveMarkers")}
        scope["lifecycleGraph"]["activeRecordIds"] = sorted(scope["lifecycleGraph"]["activeRecordIds"])
        return scope["lifecycleGraph"]

    def _insert_archive_specs(self, campaign, branch, revision, specs):
        lineage = self._lineage(campaign, branch, revision)
        for spec in specs:
            sequence = self._lifecycle_sequence()
            marker = {"eventId": spec["eventId"], "rootId": spec["rootId"],
                "branchId": branch, "revision": revision}
            combined_records = {row["id"]: row for row in spec["records"]}
            previous = self._latest_lifecycle_markers(campaign, lineage, spec["storyOrder"]).get(spec["rootId"])
            floor = spec["archivedFloor"]
            if previous is not None and previous["payloadAvailable"]:
                accesses = self._lifecycle_accesses(campaign, lineage, spec["storyOrder"], floor)
                renewals = [row["expiresAfterFloor"] for row in accesses
                    if row["operation"] == "read" and row["rootId"] == spec["rootId"]
                    and row["markerBranch"] == previous["branchId"]
                    and row["markerRevision"] == previous["revision"]
                    and row["eventId"] == previous["eventId"]]
                if floor < max([previous["expiresAfterFloor"], *renewals]):
                    cached = self._lifecycle_cache_row(campaign, lineage, previous)
                    if cached is not None:
                        for row in episodic_lifecycle.decode_payload(cached[0], cached[1]):
                            combined_records.setdefault(row["id"], row)
                        spec["relatedRecordIds"] = sorted(set(spec["relatedRecordIds"])
                                                           | set(previous["relatedRecordIds"]))
                        spec["relatedEventIds"] = sorted(set(spec["relatedEventIds"])
                                                          | set(previous["relatedEventIds"]))
            payload, payload_hash = episodic_lifecycle.encode_payload(
                [combined_records[key] for key in sorted(combined_records)])
            self.db.execute("INSERT INTO play_lifecycle_archive_markers VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (campaign, branch, revision, spec["storyOrder"], spec["eventId"], spec["rootId"],
                 spec["archivedFloor"], spec["expiresAfterFloor"], _json(spec["relatedEventIds"]),
                 _json(spec["relatedRecordIds"]), spec["kind"], sequence))
            self.db.execute("INSERT INTO play_lifecycle_archive_cache VALUES (?,?,?,?,?,?,?,?,?)",
                (campaign, branch, revision, branch, revision, spec["eventId"], spec["rootId"],
                 payload, payload_hash))

    def _latest_lifecycle_markers(self, campaign, lineage, story_order):
        markers = self._lifecycle_markers(campaign, lineage, story_order)
        latest = {}
        for marker in markers:
            old = latest.get(marker["rootId"])
            rank = (-marker["_branchRank"], marker["revision"], marker["sequence"])
            old_rank = (-old["_branchRank"], old["revision"], old["sequence"]) if old else None
            if old is None or old_rank < rank:
                latest[marker["rootId"]] = marker
        return latest

    def _validate_lifecycle_restores(self, campaign, lineage, records, new_records):
        specs = episodic_lifecycle.archive_specs(records, new_records)
        new_roots = {spec["rootId"]: spec for spec in specs}
        for row in new_records:
            if row["kind"] != "event":
                continue
            restore_ids = row.get("metadata", {}).get("memoryLifecycle", {}).get("restoreEventIds", [])
            graph = episodic_lifecycle.event_families(records,
                managed_event_ids=episodic_lifecycle.policy_event_ids(records) | set(restore_ids))
            for event_id in restore_ids:
                root = graph["roots"][event_id]
                floor = episodic_lifecycle.accepted_floor(records, row["order"])
                spec = new_roots.get(root)
                if spec is not None:
                    if floor >= spec["expiresAfterFloor"]:
                        fail("archive restoration is past its accepted-floor expiry",
                             "archive-expired", -32009)
                    continue
                marker = self._latest_lifecycle_markers(campaign, lineage, row["order"]).get(root)
                if marker is None or not marker["payloadAvailable"]:
                    fail("archive restoration has no retained derived payload",
                         "archive-expired", -32009)
                expiry = marker["expiresAfterFloor"]
                accesses = self._lifecycle_accesses(campaign, lineage, row["order"], floor)
                reads = [access["expiresAfterFloor"] for access in accesses
                    if access["operation"] == "read" and access["rootId"] == root
                    and access["markerBranch"] == marker["branchId"]
                    and access["markerRevision"] == marker["revision"]
                    and access["eventId"] == marker["eventId"]]
                if floor >= max([expiry, *reads]):
                    fail("archive restoration is past its accepted-floor expiry",
                         "archive-expired", -32009)

    def _apply_lifecycle_metadata_restores(self, campaign, branch, revision, epoch, lineage,
                                           records, new_records):
        for row in new_records:
            if row["kind"] != "event":
                continue
            restore_ids = row.get("metadata", {}).get("memoryLifecycle", {}).get("restoreEventIds", [])
            graph = episodic_lifecycle.event_families(records,
                managed_event_ids=episodic_lifecycle.policy_event_ids(records) | set(restore_ids))
            floor = episodic_lifecycle.accepted_floor(records, row["order"])
            for event_id in restore_ids:
                root = graph["roots"][event_id]
                latest = self._latest_lifecycle_markers(campaign, lineage, row["order"])
                marker = latest.get(root)
                if marker is None or not marker["payloadAvailable"]:
                    fail("archive restoration has no retained derived payload",
                         "archive-expired", -32009)
                accesses = self._lifecycle_accesses(campaign, lineage, row["order"], floor,
                    current_branch=branch, epoch=epoch)
                reads = [access["expiresAfterFloor"] for access in accesses
                    if access["operation"] == "read" and access["rootId"] == root
                    and access["markerBranch"] == marker["branchId"]
                    and access["markerRevision"] == marker["revision"]
                    and access["eventId"] == marker["eventId"]]
                if floor >= max([marker["expiresAfterFloor"], *reads]):
                    fail("archive restoration is past its accepted-floor expiry",
                         "archive-expired", -32009)
                cache = self._lifecycle_cache_row(campaign, lineage, marker)
                payload = episodic_lifecycle.decode_payload(cache[0], cache[1])
                restored_ids = [record["id"] for record in payload]
                access = self._record_lifecycle_access(campaign=campaign, branch=branch,
                    revision=revision, epoch=epoch, story_order=row["order"], floor=floor, marker=marker,
                    operation="restore", expires_after_floor=marker["expiresAfterFloor"],
                    restored_record_ids=restored_ids)
                access["_branchRank"] = 0

    def _purge_current_branch_expired_archives(self, campaign, branch, lineage, records):
        # An accepted batch has no scope story-order cutoff. Consider every
        # committed access row visible at this branch/revision when deciding
        # whether physical cache deletion is safe; each later scope still
        # applies its own story-order cutoff before it can use that access.
        story_order = 9007199254740991
        floor = episodic_lifecycle.accepted_floor(records)
        markers = self._lifecycle_markers(campaign, lineage, story_order)
        accesses = self._lifecycle_accesses(campaign, lineage, story_order, floor)
        projected = episodic_lifecycle.project(records, effective_records(records, story_order),
                                               markers, accesses, floor)
        for public_marker in projected["archiveMarkers"]:
            if floor < public_marker["expiresAfterFloor"]:
                continue
            marker = projected["latestMarkers"][public_marker["rootId"]]
            deleted = self.db.execute(
                "DELETE FROM play_lifecycle_archive_cache WHERE campaign=? AND branch=? "
                "AND marker_branch=? AND marker_revision=? AND event_id=? AND root_id=?",
                (campaign, branch, marker["branchId"], marker["revision"],
                 marker["eventId"], marker["rootId"])).rowcount
            if deleted:
                logger.info("play archive cache purged campaign=%s branch=%s event=%s floor=%s",
                    campaign, branch, marker["eventId"], floor)

    def _records(self, campaign, lineage):
        records = []
        for branch, cutoff in lineage.items():
            rows = self.db.execute("SELECT * FROM play_records WHERE campaign=? AND branch=? AND revision<=? ORDER BY story_order,id", (campaign, branch, cutoff)).fetchall()
            for row in rows:
                if _hash_bytes(row["body"].encode("utf-8")) != row["body_hash"]:
                    fail("persisted record hash differs", "record-integrity", -32009)
                record = strict_json_loads(row["body"])
                record["_branch"], record["_revision"] = branch, row["revision"]
                records.append(record)
        return records

    def _validate_records(self, campaign, records, existing, lifecycle_policy=None):
        combined = {r["id"]: r for r in existing}
        for record in records:
            if record["id"] in combined or self.db.execute("SELECT 1 FROM play_records WHERE campaign=? AND id=?", (campaign, record["id"])).fetchone():
                fail("record identity already exists", "record-conflict", -32009)
            combined[record["id"]] = record
            if record.get("validUntil", record["order"] + 1) <= record["order"]:
                fail("validUntil must follow effective order")
            if (record["kind"] in ("profile", "state") and record["field"] == "memory.importance"
                    and record["text"] not in ("core", "major", "recurring", "ambient", "auto")):
                fail("memory.importance must be core, major, recurring, ambient or auto")
        sources = {}
        # Source identity remains immutable even across sibling branches. Being
        # physically present here still does not authorize using a sibling source.
        global_sources = {}
        for row in self.db.execute("SELECT body FROM play_records WHERE campaign=? AND kind='source'", (campaign,)):
            source = strict_json_loads(row[0])
            global_sources[(source["sourceId"], source["version"])] = source["sha256"]
        for record in combined.values():
            if record["kind"] == "source":
                raw = record["text"].encode("utf-8")
                if _hash_bytes(raw) != record["sha256"]:
                    fail("source content hash mismatch", "source-integrity", -32009)
                source_key = record["sourceId"], record["version"]
                if source_key in global_sources and global_sources[source_key] != record["sha256"]:
                    fail("source version differs across branch", "source-integrity", -32009)
                if source_key in sources and sources[source_key]["sha256"] != record["sha256"]:
                    fail("source version collision", "source-integrity", -32009)
                sources[source_key] = record
        effective_records(list(combined.values()), max((r["order"] for r in combined.values()), default=0))
        importance_orders = set()
        for record in combined.values():
            if (record["kind"] in ("profile", "state") and record["field"] == "memory.importance"
                    and record["actorId"] in record["knownBy"]):
                key = record["actorId"], record["order"]
                if key in importance_orders:
                    fail("ambiguous importance override order", "record-conflict", -32009)
                importance_orders.add(key)
        for record in records:
            for span in record.get("sourceSpans", []):
                source = sources.get((span["sourceId"], span["version"]))
                if source is None or source["order"] > record["order"]:
                    fail("missing or future source", "source-integrity", -32009)
                raw = source["text"].encode("utf-8")
                start, end = span["startByte"], span["endByte"]
                if start >= end or end > len(raw):
                    fail("source span outside strict half-open byte range", "source-integrity", -32009)
                try:
                    raw[:start].decode("utf-8", errors="strict")
                    raw[start:end].decode("utf-8", errors="strict")
                except UnicodeError:
                    fail("source span splits UTF-8 character", "source-integrity", -32009)
                if _hash_bytes(raw[start:end]) != span["sha256"]:
                    fail("source span hash mismatch", "source-integrity", -32009)
            refs = [(identifier, "event") for identifier in record.get("eventIds", [])]
            refs += [(identifier, None) for identifier in record.get("basisIds", [])]
            for field, expected in (("sceneId", "scene"), ("acquisitionId", "acquisition"), ("backgroundId", "background")):
                if field in record:
                    refs.append((record[field], expected))
            for identifier, expected in refs:
                target = combined.get(identifier)
                if target is None or target["order"] > record["order"] or (expected and target["kind"] != expected):
                    fail("missing, future or wrongly typed record reference", "reference-integrity", -32009)
            if record["kind"] == "attitude" and combined[record["acquisitionId"]]["actorId"] != record["actorId"]:
                fail("attitude owner differs from acquisition", "reference-integrity", -32009)
            if record["kind"] in ("attitude", "exception") and not record["basisIds"]:
                fail("cognitive change requires explicit basis")
        for actor in {r["actorId"] for r in combined.values() if r["kind"] == "attitude"}:
            project_attitudes(list(combined.values()), actor)
        episodic_lifecycle.validate_lifecycle(list(combined.values()))
        validate_library_records(records, existing, lifecycle_policy)

    def _plan_batch(self, value):
        """Pure acceptance planning over the caller's SQLite snapshot.

        It must never mutate state, run a ranker, invoke hooks or claim a write.
        Apply reruns it under its own write lock, closing the preflight race.
        """
        batch = copy.deepcopy(value)
        validate(batch, BATCH, "batch")
        if batch_hash(batch) != batch["payloadHash"]:
            fail("accepted batch payload hash mismatch", "batch-conflict", -32009)
        campaign, identifier, branch = batch["campaignId"], batch["batchId"], batch["branchId"]
        old = self.db.execute("SELECT payload_hash,receipt FROM play_batches WHERE campaign=? AND batch=?", (campaign, identifier)).fetchone()
        if old:
            if old[0] != batch["payloadHash"]:
                fail("batch identity reused with changed payload", "batch-conflict", -32009)
            return batch, strict_json_loads(old[1]), None, batch.get("parent")
        epoch_row = self.db.execute("SELECT epoch FROM play_epochs WHERE campaign=? AND branch=?", (campaign, branch)).fetchone()
        if epoch_row and batch["epoch"] < epoch_row[0]:
            fail("accepted batch epoch is stale", "stale-epoch", -32003)
        current = self._branch(campaign, branch)
        revision = batch["worldRevision"]
        parent = batch.get("parent")
        if current:
            previous_revision = batch.get("previousWorldRevision", revision - 1)
            if parent is not None or batch["predecessorBatchId"] != current["head_batch"] or previous_revision != current["head_revision"] or revision <= current["head_revision"]:
                fail("batch predecessor or contiguous revision mismatch", "predecessor-conflict", -32009)
            lineage = self._lineage(campaign, branch, current["head_revision"])
        elif parent:
            parent_receipt = self.db.execute("SELECT branch,revision FROM play_batches WHERE campaign=? AND batch=?", (campaign, parent["batchId"])).fetchone()
            valid_initial = revision in (0, 1) or (batch.get("previousWorldRevision") == 0 and revision > 0)
            if parent["branchId"] == branch or not parent_receipt or tuple(parent_receipt) != (parent["branchId"], parent["throughRevision"]) or batch["predecessorBatchId"] != parent["batchId"] or not valid_initial or batch.get("previousWorldRevision", 0) != 0:
                fail("first child batch has invalid fork predecessor", "predecessor-conflict", -32009)
            lineage = self._lineage(campaign, parent["branchId"], parent["throughRevision"])
        else:
            valid_initial = revision in (0, 1) or (batch.get("previousWorldRevision") == 0 and revision > 0)
            if batch["predecessorBatchId"] is not None or not valid_initial or batch.get("previousWorldRevision", 0) != 0:
                fail("first root batch requires initial revision and null predecessor", "predecessor-conflict", -32009)
            lineage = {}
        count = self.db.execute("SELECT COUNT(*) FROM play_records WHERE campaign=?", (campaign,)).fetchone()[0]
        if count + len(batch["records"]) > self.max_records:
            fail("persistent record capacity exceeded", "capacity-exceeded", -32002)
        existing_records = self._records(campaign, lineage)
        self._validate_records(campaign, batch["records"], existing_records,
                               batch.get("characterLibraryPolicy"))
        planned_records = [*existing_records, *batch["records"]]
        self._validate_lifecycle_restores(campaign, lineage, planned_records, batch["records"])
        return batch, None, current, parent

    def validate_batch(self, params):
        self._host(params)
        self.db.execute("BEGIN")
        try:
            batch, _, _, _ = self._plan_batch(params["batch"])
            self.db.execute("COMMIT")
            return {"valid": True, "batchId": batch["batchId"], "payloadHash": batch["payloadHash"]}
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def apply_batch(self, params):
        self._host(params)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            batch, replay_receipt, current, parent = self._plan_batch(params["batch"])
            if replay_receipt is not None:
                self.db.execute("COMMIT")
                return replay_receipt
            campaign, identifier, branch = batch["campaignId"], batch["batchId"], batch["branchId"]
            revision = batch["worldRevision"]
            for record in batch["records"]:
                body = _json(record)
                self.db.execute("INSERT INTO play_records VALUES (?,?,?,?,?,?,?,?,?,?)", (campaign, record["id"], branch, revision, record["kind"], record["order"], record.get("actorId"), record.get("sceneId"), body, _hash_bytes(body.encode("utf-8"))))
                edges = [("event", value) for value in record.get("eventIds", [])]
                edges += [("source", span["sourceId"] + "@" + span["version"]) for span in record.get("sourceSpans", [])]
                edges += [("actor", record[key]) for key in ("actorId", "fromActorId", "toActorId") if key in record]
                edges += [("scene", record["sceneId"])] if "sceneId" in record else []
                self.db.executemany("INSERT OR IGNORE INTO play_edges VALUES (?,?,?,?)", [(campaign, record["id"], relation, target) for relation, target in edges])
            if current:
                self.db.execute("UPDATE play_branches SET head_batch=?,head_revision=? WHERE campaign=? AND branch=?", (identifier, revision, campaign, branch))
            else:
                self.db.execute("INSERT INTO play_branches(campaign,branch,parent,parent_revision,parent_batch,head_batch,head_revision) VALUES (?,?,?,?,?,?,?)", (campaign, branch, parent["branchId"] if parent else None, parent["throughRevision"] if parent else None, parent["batchId"] if parent else None, identifier, revision))
                if parent is not None:
                    self._capture_lifecycle_fork_cutoffs(
                        campaign, branch, parent["branchId"], parent["throughRevision"])
            self.db.execute("INSERT INTO play_epochs VALUES (?,?,?) ON CONFLICT(campaign,branch) DO UPDATE SET epoch=excluded.epoch", (campaign, branch, batch["epoch"]))
            readable = self._lineage(campaign, branch, revision)
            indexed = self._indexed(campaign, readable)
            accepted_records = self._records(campaign, readable)
            self._insert_archive_specs(campaign, branch, revision,
                episodic_lifecycle.archive_specs(accepted_records, batch["records"]))
            self._apply_lifecycle_metadata_restores(campaign, branch, revision, batch["epoch"],
                readable, accepted_records, batch["records"])
            self._purge_current_branch_expired_archives(campaign, branch, readable, accepted_records)
            receipt = {"schemaVersion": VERSION, "campaignId": campaign, "batchId": identifier,
                       "payloadHash": batch["payloadHash"], "branchId": branch, "epoch": batch["epoch"],
                       "worldRevision": revision, "predecessorBatchId": batch["predecessorBatchId"],
                       "readableWatermark": readable, "indexedWatermark": indexed,
                       "vectorMode": self.vector_mode, "indexedAvailability": "not-applicable-live-ranking",
                       "receiptHash": ""}
            projection_records = effective_records(
                accepted_records,
                max((record["order"] for record in accepted_records), default=0),
            )
            receipt["characterLibrary"] = character_library_projection(
                projection_records, self.importance_policy, identity_records=accepted_records)
            receipt["receiptHash"] = canonical_hash({k: v for k, v in receipt.items() if k != "receiptHash"})
            self.db.execute("INSERT INTO play_batches VALUES (?,?,?,?,?,?,?)", (campaign, identifier, branch, revision, batch["epoch"], batch["payloadHash"], _json(receipt)))
            if self.fault_hook:
                self.fault_hook("before-commit", batch)
            self.db.execute("COMMIT")
            logger.info("play memory applied campaign=%s branch=%s batch=%s revision=%s payload=%s", campaign, branch, identifier, revision, batch["payloadHash"])
            if self.fault_hook:
                self.fault_hook("after-commit", batch)
            return receipt
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _indexed(self, campaign, lineage):
        # Compatibility only: this profile has no persisted vector index and
        # therefore no asynchronously lagging vector readiness to acknowledge.
        return {branch: -1 for branch in lineage}

    def acknowledge_indexed(self, *, campaign_id, branch_id, batch_id, profile_identity):
        """Disabled: bounded query caches cannot attest a durable full index."""
        fail("persistent vector watermark unavailable in live-authorized-ranking profile", "index-mode-conflict", -32009)

    def _validate_navigation_summaries(self, binding, effective, summaries):
        if len(summaries) > MAX_NAVIGATION_SUMMARIES:
            fail("navigation summary count exceeds the bounded scope limit", "capacity-exceeded", -32002)
        authorized = {record["id"]: record for record in self._authorized({"binding": binding, "effective": effective})}
        result, total_bytes = {}, 0
        for item in summaries:
            identifier = item["id"]
            if not identifier.strip():
                fail("navigation summary id must be nonblank")
            if identifier in result:
                fail("navigation summary ids must be unique")
            record = authorized.get(identifier)
            if record is None:
                fail("navigation summary is outside the authorized scope", "forbidden", -32001)
            if record["kind"] != "scene":
                fail("navigation summaries may only describe authorized scenes")
            source_bytes = record["text"].encode("utf-8", errors="strict")
            if item["sourceHash"] != _hash_bytes(source_bytes):
                fail("navigation summary source hash mismatch", "summary-integrity", -32009)
            summary = item["summary"]
            if not summary.strip():
                fail("navigation summary must be nonblank")
            summary_bytes = summary.encode("utf-8", errors="strict")
            if len(summary_bytes) > MAX_NAVIGATION_SUMMARY_BYTES:
                fail("navigation summary exceeds the bounded byte limit", "capacity-exceeded", -32002)
            total_bytes += len(summary_bytes)
            if total_bytes > MAX_NAVIGATION_SUMMARIES_BYTES:
                fail("navigation summaries exceed the bounded scope byte limit", "capacity-exceeded", -32002)
            if item["summaryHash"] != _hash_bytes(summary_bytes):
                fail("navigation summary hash mismatch", "summary-integrity", -32009)
            result[identifier] = {"sourceHash": item["sourceHash"], "summary": summary,
                                  "summaryHash": item["summaryHash"]}
        return result

    def open_scope(self, params):
        self._host(params)
        binding = copy.deepcopy(params["binding"])
        validate(binding, BINDING, "binding")
        has_retrieval_policy = "retrievalPolicy" in params
        retrieval_policy = params.get("retrievalPolicy")
        if has_retrieval_policy and (not isinstance(retrieval_policy, str)
                                     or retrieval_policy != SCENE_SUMMARIES_RETRIEVAL_POLICY):
            fail("retrievalPolicy must be scene-summaries-v1")
        if has_retrieval_policy and binding["purpose"] == "actor-knowledge":
            fail("scene-summaries-v1 retrievalPolicy cannot be used with actor-knowledge scopes",
                 "invalid-scope", -32003)
        if len(self.scopes) >= self.max_scopes:
            fail("scope capacity exceeded", "capacity-exceeded", -32002)
        campaign, branch = binding["campaignId"], binding["branchId"]
        active = self.db.execute("SELECT epoch FROM play_epochs WHERE campaign=? AND branch=?", (campaign, branch)).fetchone()
        if active is None or binding["epoch"] < active[0]:
            fail("scope epoch is stale or campaign unavailable", "stale-epoch", -32003)
        lineage = self._lineage(campaign, branch, binding["worldRevision"])
        supplied = {entry["branchId"]: entry["throughRevision"] for entry in binding["lineage"]}
        if len(supplied) != len(binding["lineage"]) or supplied != lineage:
            fail("scope lineage differs from accepted fork boundaries", "invalid-scope", -32003)
        if binding["purpose"] == "actor-knowledge" and len(binding["actorIds"]) != 1:
            fail("actor knowledge scope requires exactly one actor")
        if has_retrieval_policy:
            # This normalized policy is part of the immutable scope identity.
            # Leaving it absent preserves every existing binding hash.
            binding["retrievalPolicy"] = retrieval_policy
        if binding["requiredBatchId"] is not None:
            row = self.db.execute("SELECT branch,revision FROM play_batches WHERE campaign=? AND batch=?", (campaign, binding["requiredBatchId"])).fetchone()
            if row is None or row["branch"] not in lineage or row["revision"] > lineage[row["branch"]]:
                fail("required batch is not readable in scope", "not-ready", -32011)
        records = self._records(campaign, lineage)
        records = [r for r in records if r["order"] <= binding["storyOrder"]]
        record_ids = {r["id"] for r in records}
        if set(binding["grants"]) - record_ids:
            fail("scope grant references unavailable record", "invalid-scope", -32003)
        effective = effective_records(records, binding["storyOrder"])
        blocked = blocked_memory_actors(effective)
        if binding["purpose"] == "actor-knowledge" and set(binding["actorIds"]) & blocked:
            fail("actor-knowledge scope includes a deleted or excluded identity",
                 "character-memory-ineligible", -32009)
        if (binding.get("characterLibraryPolicy") == LIBRARY_POLICY
                and binding["purpose"] == "actor-knowledge"):
            library_ids = {row["actorId"] for row in character_library_projection(
                effective, self.importance_policy, identity_records=records)["actors"]}
            if set(binding["actorIds"]) - library_ids:
                fail("actor-knowledge scope includes an unsaved or unclassified identity",
                     "character-memory-ineligible", -32009)
        navigation_summaries = self._validate_navigation_summaries(
            binding, effective, params.get("navigationSummaries", []))
        actor_first_appearance_ids = None
        if binding.get("characterLibraryPolicy") == LIBRARY_POLICY:
            seen = seen_identities(effective)
            actor_first_appearance_ids = [
                actor_id for actor_id in binding["actorIds"] if actor_id not in seen
            ]
        binding_hash = canonical_hash(binding)
        token = secrets.token_urlsafe(32)
        self.db.execute("UPDATE play_epochs SET epoch=? WHERE campaign=? AND branch=?", (binding["epoch"], campaign, branch))
        floor = episodic_lifecycle.accepted_floor(records)
        scope = {"binding": binding, "bindingHash": binding_hash, "lineage": lineage,
                 "records": records, "baseEffective": effective, "effective": effective,
                 "acceptedFloor": floor,
                 "lifecycleMarkers": self._lifecycle_markers(campaign, lineage, binding["storyOrder"]),
                 "lifecycleAccesses": self._lifecycle_accesses(campaign, lineage,
                     binding["storyOrder"], floor, current_branch=branch, epoch=binding["epoch"]),
                 "navigationSummaries": navigation_summaries,
                 "handles": {}, "readBytes": 0, "readCalls": 0, "readIds": set()}
        self._refresh_lifecycle_scope(scope, refresh_cache=True, purge=True)
        self.scopes[token] = scope
        logger.info("play scope opened campaign=%s branch=%s epoch=%s binding=%s navigation_summaries=%d floor=%d active_records=%d archives=%d",
                    campaign, branch, binding["epoch"], binding_hash, len(navigation_summaries),
                    floor, len(scope["activeRecordIds"]), len(scope["lifecycleGraph"]["archiveMarkers"]))
        result = {"scopeHandle": token, "bindingHash": binding_hash, "readableWatermark": lineage,
                  "indexedWatermark": self._indexed(campaign, lineage), "profile": self.ranker_identity,
                  "vectorMode": self.vector_mode, "indexedAvailability": "not-applicable-live-ranking"}
        if actor_first_appearance_ids is not None:
            result["actorFirstAppearanceIds"] = actor_first_appearance_ids
        return result

    def _scope(self, handle):
        scope = self.scopes.get(handle)
        if scope is None:
            fail("unknown or closed scope capability", "invalid-scope", -32003)
        binding = scope["binding"]
        epoch = self.db.execute("SELECT epoch FROM play_epochs WHERE campaign=? AND branch=?", (binding["campaignId"], binding["branchId"])).fetchone()
        if epoch is None or epoch[0] != binding["epoch"]:
            fail("scope epoch was invalidated", "stale-epoch", -32003)
        return scope

    def close_scope(self, params):
        self._host(params)
        # Trusted lifecycle cleanup must also release cancelled/stale epochs.
        # This never grants another read and is safely idempotent after a retry.
        self.scopes.pop(params["scopeHandle"], None)
        return {"closed": True}

    def _revalidate_scope(self, handle, expected_binding_hash):
        scope = self._scope(handle)
        if (scope.get("bindingHash") != expected_binding_hash
                or canonical_hash(scope["binding"]) != expected_binding_hash):
            fail("scope binding changed while ranking or warming", "stale-epoch", -32003)
        return scope

    def _actor_records(self, scope, actor):
        effective = scope["effective"]
        exceptions = {r["backgroundId"]: r for r in effective if r["kind"] == "exception" and r["actorId"] == actor}
        result = []
        for record in effective:
            kind = record["kind"]
            allowed = ((kind in ("acquisition", "attitude") and record["actorId"] == actor)
                       or (kind in ("profile", "state", "relationship", "date") and actor in record["knownBy"]))
            if kind == "background" and (actor in record["appliesTo"] or "*" in record["appliesTo"]):
                exception = exceptions.get(record["id"])
                allowed = exception is None or exception["status"] == "acquired"
            if allowed:
                result.append(record)
        return result

    @staticmethod
    def _canonical_event_reference(scope, event_id):
        return scope["lifecycleProjection"]["aliases"].get(event_id, event_id)

    def _actor_pack_reference_allowed(self, scope, references):
        authorized = self._authorized(scope)
        allowed = {row["id"] for row in authorized}
        aliases = scope["lifecycleProjection"]["aliases"]
        if scope["binding"]["purpose"] == "actor-knowledge":
            # Actor scopes may ask about any visible event identity and receive
            # an unknown result. Matches below still use only their own legal
            # records, so this grants neither another actor's facet nor event text.
            allowed.update(row["id"] for row in scope["records"] if row["kind"] == "event")
            allowed.update(aliases)
        else:
            # In director/review scopes an event reference remains bounded by
            # exact authorized event/acquisition references before family aliasing.
            authorized_families = {self._canonical_event_reference(scope, row["id"])
                                   for row in authorized if row["kind"] == "event"}
            authorized_families.update(self._canonical_event_reference(scope, event_id)
                for row in authorized if row["kind"] == "acquisition"
                for event_id in row.get("eventIds", []))
            allowed.update(row["id"] for row in scope["records"] if row["kind"] == "event"
                           and self._canonical_event_reference(scope, row["id"])
                           in authorized_families)
            allowed.update(alias for alias, representative in aliases.items()
                           if representative in authorized_families)
        outside = set(references) - allowed
        if outside:
            fail("actor pack references outside authorized recall scope", "forbidden", -32001)

    def _record_matches_actor_pack_reference(self, scope, record, reference):
        if record["id"] == reference:
            return True
        if record["kind"] != "acquisition":
            return False
        canonical = self._canonical_event_reference(scope, reference)
        return any(self._canonical_event_reference(scope, event_id) == canonical
                   for event_id in record.get("eventIds", []))

    @staticmethod
    def _attitudes(scope, actor):
        stance_records = [r for r in scope["effective"] if r["kind"] == "attitude"]
        stance_records += [r for r in scope["records"] if r["kind"] == "acquisition"]
        return project_attitudes(stance_records, actor)

    def _authorized(self, scope):
        binding = scope["binding"]
        if binding["purpose"] == "actor-knowledge":
            return self._actor_records(scope, binding["actorIds"][0])
        grants = set(binding["grants"])
        return [r for r in scope["effective"] if r["id"] in grants
                and r["kind"] not in ("source", "attitude", "exception", "character-library")]

    @staticmethod
    def _public(record):
        return {key: copy.deepcopy(value) for key, value in record.items() if not key.startswith("_")}

    def _handle(self, scope, record):
        for handle, identifier in scope["handles"].items():
            if identifier == record["id"]:
                return handle
        handle = secrets.token_urlsafe(24)
        scope["handles"][handle] = record["id"]
        return handle

    def _coverage(self, scope):
        indexed = self._indexed(scope["binding"]["campaignId"], scope["lineage"])
        return {"status": "complete", "readableWatermark": scope["lineage"], "indexedWatermark": indexed,
                "recentDelta": [], "readableRanges": [{"branchId": branch, "throughRevision": cutoff} for branch, cutoff in scope["lineage"].items()],
                "vectorMode": self.vector_mode, "indexedAvailability": "not-applicable-live-ranking",
                "policy": "complete-authorized-live-ranking-v1" if self.vector_mode == "live-authorized-ranking" else "complete-authorized-lexical-v1"}

    def _search_candidates(self, scope):
        records = [r for r in self._authorized(scope) if "text" in r]
        retrieval_policy = scope["binding"].get("retrievalPolicy")
        if retrieval_policy == SCENE_SUMMARIES_RETRIEVAL_POLICY:
            records = [record for record in records if record["kind"] == "scene"]
        summaries = scope["navigationSummaries"]
        candidates = []
        for record in records:
            scene_id = record.get("sceneId", record["id"])
            summary = summaries.get(scene_id)
            if retrieval_policy == SCENE_SUMMARIES_RETRIEVAL_POLICY and summary is None:
                fail("scene-summaries-v1 requires a navigation summary for every scene candidate",
                     "summary-integrity", -32009)
            observation = (summary["summary"] if record["kind"] == "scene" and summary is not None
                           else record["text"])
            candidates.append(AuthorizedRetrievalCandidate(
                record["id"], scene_id, self._original(scope, record),
                observation,
                scene_id, (scope["binding"]["campaignId"], scope["binding"]["purpose"],
                           *scope["binding"]["actorIds"]),
                (record["order"], record["id"]), record["id"]))
        return records, candidates

    def warm_scope(self, params):
        self._host(params)
        started = time.perf_counter()
        scope = self._scope(params["scopeHandle"])
        binding_hash = scope["bindingHash"]
        records, candidates = self._search_candidates(scope)
        prepare = getattr(self.ranker, "prepare", None)
        if not callable(prepare):
            fail("selected ranker does not support passage-vector prewarm", "unsupported-operation", -32009)
        prepared = prepare(candidates=candidates)
        self._revalidate_scope(params["scopeHandle"], binding_hash)
        if not isinstance(prepared, dict):
            fail("ranker returned invalid prewarm statistics", "ranker-integrity", -32009)
        expected = {"candidateCount": len(records), "profile": self.ranker_identity}
        if any(prepared.get(key) != value for key, value in expected.items()):
            fail("ranker prewarm statistics differ from the authorized scope", "ranker-integrity", -32009)
        for key in ("cacheHits", "cacheMisses", "cacheSize", "cacheCapacity"):
            if type(prepared.get(key)) is not int or prepared[key] < 0:
                fail("ranker returned invalid prewarm cache statistics", "ranker-integrity", -32009)
        if prepared["cacheCapacity"] < 1 or prepared["cacheSize"] > prepared["cacheCapacity"]:
            fail("ranker returned inconsistent prewarm cache state", "ranker-integrity", -32009)
        return {"scopeBindingHash": binding_hash, "candidateCount": len(records),
                "navigationSummariesHash": canonical_hash(scope["navigationSummaries"]),
                "candidateCorpusHash": canonical_hash([
                    {"candidateId": candidate.source_event_id,
                     "observationSha256": _hash_bytes(candidate.observation.encode("utf-8"))}
                    for candidate in candidates]),
                "cacheHits": prepared["cacheHits"], "cacheMisses": prepared["cacheMisses"],
                "profile": self.ranker_identity,
                "elapsedMs": max(0.0, (time.perf_counter() - started) * 1000.0),
                "cacheState": {"kind": "process-local-exact-text-vector-cache",
                               "size": prepared["cacheSize"], "capacity": prepared["cacheCapacity"]}}

    def search(self, params):
        scope = self._scope(params["scopeHandle"])
        binding_hash = scope["bindingHash"]
        records, candidates = self._search_candidates(scope)
        authorized_count = len(records)
        excluded = set(params.get("excludeIds", []))
        if excluded - {record["id"] for record in self._authorized(scope)}:
            fail("search exclusion outside authorized universe", "forbidden", -32001)
        records = [record for record in records if record["id"] not in excluded]
        candidates = [candidate for candidate in candidates if candidate.source_event_id not in excluded]
        result = self.ranker.rank(query=params["query"], candidates=candidates)
        self._revalidate_scope(params["scopeHandle"], binding_hash)
        by_id = {r["id"]: r for r in records}
        ids = result.ranked_event_ids
        if not isinstance(ids, list) or len(ids) != len(set(ids)) or set(ids) - set(by_id):
            fail("ranker returned unauthorized or duplicate identities", "ranker-integrity", -32009)
        for identifier in ids:
            score = result.scores.get(identifier)
            if type(score) not in (int, float) or not math.isfinite(score):
                fail("ranker returned missing or nonfinite score", "ranker-integrity", -32009)
        canonical_hash(result.trace)
        coverage = self._coverage(scope)
        rows = []
        for identifier in ids[:params["limit"]]:
            record = by_id[identifier]
            rows.append({"id": identifier, "handle": self._handle(scope, record), "kind": record["kind"],
                         "representation": "accepted", "representationHash": _hash_bytes(record["text"].encode("utf-8")),
                         "score": result.scores[identifier], "candidateStatus": "candidate", "readStatus": "unread",
                         "origin": "live" if self.vector_mode == "live-authorized-ranking" else "readable"})
        return {"candidates": rows, "coverage": coverage, "hasMore": len(ids) > params["limit"],
                "trace": {"profile": self.ranker_identity, "authorizedCount": len(records),
                          "authorizedDigest": canonical_hash(sorted(by_id)), "ranker": result.trace,
                          **({"beforeExclusionCount": authorized_count,
                              "excludedCount": authorized_count - len(records),
                              "excludedDigest": canonical_hash(sorted(excluded))} if "excludeIds" in params else {})}}

    def navigate(self, params):
        scope = self._scope(params["scopeHandle"])
        records = self._authorized(scope)
        by_id = {r["id"]: r for r in records}
        identifier = scope["handles"].get(params["seed"], params["seed"])
        seed = by_id.get(identifier)
        if seed is None:
            fail("navigation seed outside authorized universe", "forbidden", -32001)
        relation = params["relation"]
        def targets(record):
            if relation == "event":
                event_ids = {self._canonical_event_reference(scope, event_id)
                             for event_id in record.get("eventIds", [])}
                if record["kind"] == "event":
                    event_ids.add(self._canonical_event_reference(scope, record["id"]))
                return event_ids
            if relation == "scene":
                return {record.get("sceneId", record["id"] if record["kind"] == "scene" else "")} - {""}
            if relation == "actor":
                actors = {record[k] for k in ("actorId", "fromActorId", "toActorId") if k in record}
                for link in records:
                    if link["kind"] == "link" and record["id"] in link["eventIds"]:
                        actors.add(link["actorId"])
                return actors
            return {(span["sourceId"], span["version"]) for span in record.get("sourceSpans", [])}
        seed_targets = targets(seed)
        neighbors = sorted([r for r in records if r["id"] != identifier and seed_targets & targets(r)], key=lambda r: (r["order"], r["id"]))
        start = params["page"] * params["limit"]
        return {"seed": {"id": seed["id"], "kind": seed["kind"], "handle": self._handle(scope, seed)},
                "entries": [{"id": r["id"], "kind": r["kind"], "handle": self._handle(scope, r)} for r in neighbors[start:start + params["limit"]]],
                "nextPage": params["page"] + 1 if len(neighbors) > start + params["limit"] else None,
                "coverage": self._coverage(scope)}

    def navigation_cards(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        references = params["references"]
        if len(references) > MAX_NAVIGATION_CARDS:
            fail("navigation card reference limit exceeded", "capacity-exceeded", -32002)
        budget = params["budgetBytes"]
        if (type(budget) is not int or budget < MIN_NAVIGATION_CARD_BUDGET_BYTES
                or budget > MAX_NAVIGATION_CARD_BUDGET_BYTES):
            fail("navigation card byte budget is outside its bounded range", "invalid-params", -32602)
        authorized = {record["id"]: record for record in self._authorized(scope)}
        records = []
        for identifier in references:
            record = authorized.get(identifier)
            if record is None:
                fail("navigation card reference outside authorized scope", "forbidden", -32001)
            text = record.get("text")
            if not isinstance(text, str):
                fail("navigation card reference has no accepted text representation",
                     "invalid-navigation-reference", -32004)
            raw = text.encode("utf-8", errors="strict")
            metadata = record.get("metadata", {})
            if not isinstance(metadata, dict):
                fail("navigation card metadata is not an object", "record-integrity", -32009)
            safe_metadata = {}
            for key in ("tag", "storyTime"):
                value = metadata.get(key)
                if value is not None:
                    if not isinstance(value, str) or not value:
                        fail(f"navigation metadata {key} is not nonempty text", "record-integrity", -32009)
                    safe_metadata[key] = value
            source_hash = _hash_bytes(raw)
            if record["kind"] == "scene":
                summary = scope["navigationSummaries"].get(identifier)
                if summary is None:
                    full_card = {"id": identifier, "kind": record["kind"],
                                 "navigationStatus": "unavailable", "sourceHash": source_hash,
                                 **safe_metadata}
                else:
                    synopsis = summary["summary"]
                    full_card = {"id": identifier, "kind": record["kind"],
                                 "navigationStatus": "available", "sourceHash": source_hash,
                                 "synopsis": synopsis,
                                 "synopsisHash": _hash_bytes(synopsis.encode("utf-8", errors="strict")),
                                 **safe_metadata}
            elif text:
                full_card = {"id": identifier, "kind": record["kind"],
                             "navigationStatus": "available", "sourceHash": source_hash,
                             "synopsis": text, "synopsisHash": source_hash, **safe_metadata}
            else:
                full_card = {"id": identifier, "kind": record["kind"],
                             "navigationStatus": "unavailable", "sourceHash": source_hash,
                             **safe_metadata}
            records.append((identifier, record["kind"], source_hash, full_card))

        coverage = self._coverage(scope)

        def response_for(cards):
            response = {"cards": cards, "contentBytes": 0,
                        "omittedCount": sum(card["navigationStatus"] == "omitted-budget" for card in cards),
                        "coverage": coverage}
            for _ in range(4):
                actual = len(_json(response).encode("utf-8", errors="strict"))
                if response["contentBytes"] == actual:
                    break
                response["contentBytes"] = actual
            actual = len(_json(response).encode("utf-8", errors="strict"))
            if response["contentBytes"] != actual:
                fail("navigation card byte accounting did not stabilize", "integrity-error", -32009)
            return response

        cards = [{"id": identifier, "kind": kind, "navigationStatus": "omitted-budget",
                  "sourceHash": source_hash}
                 for identifier, kind, source_hash, _ in records]
        response = response_for(cards)
        if response["contentBytes"] > budget:
            fail("minimum navigation card markers exceed the requested byte budget",
                 "capacity-exceeded", -32002)
        for index, (_, _, _, full_card) in enumerate(records):
            candidate = list(cards)
            candidate[index] = full_card
            trial = response_for(candidate)
            if trial["contentBytes"] <= budget:
                cards = candidate
                response = trial
        self._charge(scope, response["contentBytes"])
        return response

    def _original(self, scope, record):
        sources = {(r["sourceId"], r["version"]): r for r in scope["records"] if r["kind"] == "source"}
        chunks = []
        for span in record.get("sourceSpans", []):
            source = sources.get((span["sourceId"], span["version"]))
            if source is None:
                fail("scope source is unavailable", "source-integrity", -32009)
            raw = source["text"].encode("utf-8")
            chunk = raw[span["startByte"]:span["endByte"]]
            if _hash_bytes(raw) != source["sha256"] or _hash_bytes(chunk) != span["sha256"]:
                fail("persisted source bytes changed", "source-integrity", -32009)
            chunks.append(chunk.decode("utf-8", errors="strict"))
        return "\n".join(chunks)

    def read(self, params):
        scope = self._scope(params["scopeHandle"])
        identifier = scope["handles"].get(params["handle"])
        record = next((r for r in self._authorized(scope) if r["id"] == identifier), None)
        if record is None:
            fail("unexposed or unauthorized read handle", "invalid-handle", -32004)
        text = record.get("text") if params["representation"] == "accepted" else self._original(scope, record)
        if text is None:
            fail("representation unavailable", "invalid-representation", -32004)
        count = len(text.encode("utf-8"))
        self._charge(scope, count)
        scope["readIds"].add(identifier)
        response = {"id": identifier, "kind": record["kind"], "text": text, "order": record["order"],
                "representation": params["representation"], "representationHash": _hash_bytes(text.encode("utf-8")),
                "sourceSpans": copy.deepcopy(record.get("sourceSpans", [])),
                "readStatus": "read", "deliveryStatus": "not-delivered",
                "readBytes": scope["readBytes"], "readCalls": scope["readCalls"]}
        if record["kind"] == "acquisition":
            response["cognition"] = {key: copy.deepcopy(record[key]) for key in ("actorId", "beliefKind", "method", "claimId", "eventIds") if key in record}
            response["cognition"]["attitude"] = self._attitudes(scope, record["actorId"])[record["id"]]
        return response

    @staticmethod
    def _charge(scope, count):
        binding = scope["binding"]
        if scope["readBytes"] + count > binding["maxReadBytes"] or scope["readCalls"] + 1 > binding["maxReadCalls"]:
            fail("shared scope read budget exceeded", "read-budget-exceeded", -32010)
        scope["readBytes"] += count
        scope["readCalls"] += 1

    def _actors(self, scope, actors):
        if set(actors) - set(scope["binding"]["actorIds"]):
            fail("actors outside immutable Host scope", "forbidden", -32001)
        if set(actors) & blocked_memory_actors(scope["effective"]):
            fail("actor pack request includes a deleted or excluded identity",
                 "character-memory-ineligible", -32009)
        if scope["binding"].get("characterLibraryPolicy") == LIBRARY_POLICY:
            library_ids = {row["actorId"] for row in character_library_projection(
                scope["effective"], self.importance_policy, identity_records=scope["records"])["actors"]}
            if set(actors) - library_ids:
                fail("actor pack request includes an unsaved or unclassified identity",
                     "character-memory-ineligible", -32009)

    def actor_cues(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        self._actors(scope, params["actors"])
        rows = []
        for actor in params["actors"]:
            cues = [self._public(r) for r in self._actor_records(scope, actor)
                    if (r["kind"] == "state" and r["actorId"] == actor) or r["kind"] == "date"]
            rows.append({"actorId": actor, "cues": cues[:32], "omittedCount": max(0, len(cues) - 32),
                         "status": "partial" if len(cues) > 32 else "complete"})
        response = {"actors": rows, "coverage": self._coverage(scope)}
        self._charge(scope, len(_json(response).encode("utf-8")))
        return response

    def _character_dossier(self, scope, actor):
        legal = self._actor_records(scope, actor)
        attitudes = self._attitudes(scope, actor)
        groups = {"profile": [], "state": [], "relationships": [], "acquisitions": [],
                  "attitudes": [], "dates": [], "backgrounds": [], "exceptions": []}
        names = {"relationship": "relationships", "date": "dates", "background": "backgrounds",
                 "attitude": "attitudes", "exception": "exceptions"}
        selected = []
        for record in legal:
            kind = record["kind"]
            if kind in ("profile", "state") and record["actorId"] != actor:
                continue
            if kind == "acquisition":
                groups["acquisitions"].append({"record": self._public(record),
                                                "attitude": attitudes.get(record["id"], "unstated")})
            else:
                groups[names.get(kind, kind)].append(self._public(record))
            selected.append(record)
        # An unknown exception is a cognition marker; it never delivers the
        # denied background's text or changes the actor's knowledge grant.
        for record in scope["effective"]:
            if record["kind"] == "exception" and record["actorId"] == actor:
                groups["exceptions"].append(self._public(record))
                selected.append(record)
        importance = self.importance_policy.classify(scope["effective"], actor, identity_records=scope["records"])
        active_record_ids = scope["activeRecordIds"]
        history = [r for r in scope["records"] if (
                   r.get("actorId") == actor and r["kind"] in ("profile", "state"))
                   or (r.get("actorId") == actor and r["kind"] == "acquisition"
                       and r["id"] in active_record_ids)
                   or (r["kind"] == "event" and r["id"] in active_record_ids)
                   or (r["kind"] in ("profile", "state") and r["field"] == "memory.role"
                       and r["text"] == "player")
                   or (r["kind"] in ("source", "scene")
                       and "characterRegistry" in r.get("metadata", {}))]
        orders = {r["order"] for r in history}
        orders.update(r["validUntil"] for r in history if "validUntil" in r
                      and r["validUntil"] <= scope["binding"]["storyOrder"])
        transitions = []
        previous = None
        for order in sorted(orders):
            projected = effective_records(history, order)
            point = self.importance_policy.classify(projected, actor,
                identity_records=[row for row in history if row["order"] <= order])
            if point["tier"] != previous:
                transitions.append({"order": order, "tier": point["tier"], "reasons": point["reasons"],
                                    "evidenceIds": point["evidenceIds"]})
                previous = point["tier"]
        importance["transitions"] = transitions
        identifiers = sorted(r["id"] for r in selected)
        return {"actorId": actor, **groups, "importance": importance,
                "eventReferences": sorted({scope["lifecycleProjection"]["aliases"].get(event, event)
                                           for row in groups["acquisitions"]
                                           for event in row["record"].get("eventIds", [])}),
                "provenance": [{"id": r["id"], "branchId": r["_branch"], "worldRevision": r["_revision"]}
                               for r in selected],
                "coverage": {"available": identifiers, "delivered": identifiers, "deferred": []}}

    def actor_dossiers(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        self._actors(scope, params["actors"])
        response = {"actors": [self._character_dossier(scope, actor) for actor in params["actors"]],
                    "coverage": self._coverage(scope)}
        self._charge(scope, len(_json(response).encode("utf-8")))
        logger.info("play actor dossiers binding=%s actors=%d records=%d policy=%s",
                    scope["bindingHash"], len(response["actors"]),
                    sum(len(row["coverage"]["available"]) for row in response["actors"]),
                    self.importance_policy.policy_id)
        return response

    def actor_packs(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        self._actors(scope, params["actors"])
        references = params["references"]
        self._actor_pack_reference_allowed(scope, references)
        if params.get("detailPolicy") == DETAIL_POLICY:
            return self._character_packs(scope, params)
        used, packs = 0, []
        actor_legal = {actor: self._actor_records(scope, actor) for actor in params["actors"]}
        actor_basics = {actor: [self._public(r) for r in legal
                               if ((r["kind"] in ("profile", "state") and r["actorId"] == actor)
                                   or r["kind"] == "relationship")]
                        for actor, legal in actor_legal.items()}
        # Reserve every actor's minimum package before consuming any shared
        # knowledge allowance; actor ordering cannot starve later identities.
        used = sum(len(_json(r).encode("utf-8")) for basics in actor_basics.values() for r in basics)
        if used > params["budgetBytes"]:
            fail("minimum actor identity/state/relationship package exceeds shared budget", "pack-budget-exceeded", -32010)
        for actor in params["actors"]:
            legal = actor_legal[actor]
            attitudes = self._attitudes(scope, actor)
            pack = {"actorId": actor, "basics": actor_basics[actor], "knowledge": [], "status": "complete"}
            for reference in references:
                matches = [r for r in legal
                           if self._record_matches_actor_pack_reference(scope, r, reference)]
                matches = [r for r in matches if r["kind"] in ("acquisition", "background", "date", "profile", "state", "relationship")]
                if not matches:
                    pack["knowledge"].append({"reference": reference, "knowledgeStatus": "unknown", "deliveryStatus": "not-applicable", "coverage": "complete"})
                for record in matches:
                    item = {"reference": reference, "knowledgeStatus": "acquired", "deliveryStatus": "included",
                            "record": self._public(record), "attitude": attitudes.get(record["id"], "unstated")}
                    size = len(_json(item).encode("utf-8"))
                    if used + size > params["budgetBytes"]:
                        item = {"reference": reference, "id": record["id"], "knowledgeStatus": "acquired",
                                "deliveryStatus": "omitted-budget", "attitude": attitudes.get(record["id"], "unstated")}
                        pack["status"] = "partial"
                    else:
                        used += size
                    pack["knowledge"].append(item)
            if not references:
                pack["status"] = "unqueried"
            packs.append(pack)
        response = {"actors": packs, "contentBytes": used, "coverage": self._coverage(scope),
                    "knowledgeCoverage": "references-only" if references else "unqueried"}
        self._charge(scope, len(_json(response).encode("utf-8")))
        return response

    def _character_packs(self, scope, params):
        packs = []
        for actor in params["actors"]:
            dossier = self._character_dossier(scope, actor)
            basics = detail_records(dossier, params["references"])
            legal = self._actor_records(scope, actor)
            attitudes = self._attitudes(scope, actor)
            knowledge = []
            for reference in params["references"]:
                matches = [r for r in legal
                           if self._record_matches_actor_pack_reference(scope, r, reference)]
                if not matches:
                    knowledge.append({"reference": reference, "knowledgeStatus": "unknown",
                                      "deliveryStatus": "not-applicable", "coverage": "complete"})
                for record in matches:
                    knowledge.append({"reference": reference, "knowledgeStatus": "acquired",
                                      "deliveryStatus": "included", "record": self._public(record),
                                      "attitude": attitudes.get(record["id"], "unstated")})
            delivered = {r["id"] for r in basics}
            available = {r["id"] for r in dossier["profile"] + dossier["state"] + dossier["relationships"]}
            packs.append({"actorId": actor, "basics": basics, "knowledge": knowledge,
                          "status": "complete" if params["references"] else "unqueried",
                          "importance": dossier["importance"], "eventReferences": dossier["eventReferences"],
                          "detailCoverage": {"available": sorted(available), "delivered": sorted(delivered),
                                             "deferred": sorted(available - delivered)}})
        used = sum(len(_json(record).encode("utf-8")) for pack in packs for record in pack["basics"])
        used += sum(len(_json(row).encode("utf-8")) for pack in packs for row in pack["knowledge"])
        if used > params["budgetBytes"]:
            fail("importance detail and relevant knowledge exceed shared budget; deepen or increase budget",
                 "pack-budget-exceeded", -32010)
        response = {"actors": packs, "contentBytes": used, "coverage": self._coverage(scope),
                    "knowledgeCoverage": "references-only" if params["references"] else "unqueried"}
        self._charge(scope, len(_json(response).encode("utf-8")))
        logger.info("play actor importance packs binding=%s actors=%d content_bytes=%d policy=%s",
                    scope["bindingHash"], len(packs), used, self.importance_policy.policy_id)
        return response

    def actor_directory(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        return {"actors": actor_directory(scope, self.importance_policy),
                "coverage": self._coverage(scope)}

    def resolve_actors(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        return resolve_actors(scope, params, self.importance_policy)

    def memory_graph(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        graph = self._refresh_lifecycle_scope(scope, refresh_cache=True, purge=True)
        logger.info("play memory graph binding=%s floor=%d active_records=%d archives=%d",
            scope["bindingHash"], graph["acceptedFloor"], len(graph["activeRecordIds"]),
            len(graph["archiveMarkers"]))
        return copy.deepcopy(graph)

    def _archive_marker_for_request(self, scope, event_id):
        projection = scope["lifecycleProjection"]
        markers = projection["latestMarkers"]
        visible_events = {row["id"] for row in scope["records"] if row["kind"] == "event"}
        for candidate in markers.values():
            if event_id == candidate["eventId"]:
                return candidate
            if candidate.get("kind") == "event-family":
                archived_events = set(candidate["relatedRecordIds"]) & visible_events
            else:
                # Acquisition-facet markers retain the event family they
                # qualify; maintenance relatedEventIds are annotations only.
                archived_events = set(candidate["relatedEventIds"])
            if event_id in archived_events:
                return candidate
        return None

    def _archive_expiry(self, scope, marker):
        return next((row["expiresAfterFloor"] for row in scope["lifecycleGraph"]["archiveMarkers"]
                     if row["rootId"] == marker["rootId"]), marker["expiresAfterFloor"])

    def _archive_authorized_records(self, scope, records):
        binding = scope["binding"]
        if binding["purpose"] == "actor-knowledge":
            actor = binding["actorIds"][0]
            acquisitions = [row for row in records
                            if row["kind"] == "acquisition" and row.get("actorId") == actor]
            acquisition_ids = {row["id"] for row in acquisitions}
            attitudes = [row for row in records if row["kind"] == "attitude"
                         and row.get("actorId") == actor
                         and row.get("acquisitionId") in acquisition_ids]
            return acquisitions + attitudes
        if binding["purpose"] in ("director-plot", "review"):
            grants = set(binding["grants"])
            return [row for row in records if row["id"] in grants]
        return []

    def memory_archive(self, params):
        self._host(params)
        scope = self._scope(params["scopeHandle"])
        graph = self._refresh_lifecycle_scope(scope, refresh_cache=True, purge=True)
        requested, seen = params["eventIds"], set()
        events, expired, missing = [], [], []
        renewed = 0
        pending_accesses = []
        campaign, branch = scope["binding"]["campaignId"], scope["binding"]["branchId"]
        revision, story_order = scope["binding"]["worldRevision"], scope["binding"]["storyOrder"]
        for requested_id in requested:
            marker = self._archive_marker_for_request(scope, requested_id)
            if marker is None:
                missing.append(requested_id)
                continue
            root = marker["rootId"]
            if root in seen:
                continue
            seen.add(root)
            expiry = self._archive_expiry(scope, marker)
            cache = self._lifecycle_cache_row(campaign, scope["lineage"], marker)
            if scope["acceptedFloor"] >= expiry or cache is None:
                expired.append(requested_id)
                continue
            records = episodic_lifecycle.decode_payload(cache[0], cache[1])
            authorized = self._archive_authorized_records(scope, records)
            if not authorized:
                missing.append(requested_id)
                continue
            authorized_ids = sorted({row["id"] for row in authorized})
            events.append({"eventId": marker["eventId"], "rootId": root,
                           "records": [self._public(row) for row in authorized]})
            if params["operation"] == "read":
                pending_accesses.append((marker, cache, "read", authorized_ids,
                    scope["acceptedFloor"] + episodic_lifecycle.ARCHIVE_FLOORS))
                renewed += 1
            else:
                pending_accesses.append((marker, cache, "restore", authorized_ids, expiry))
        result = {"operation": params["operation"], "acceptedFloor": scope["acceptedFloor"],
                  "events": events, "expiredEventIds": expired}
        if missing:
            result["missingEventIds"] = missing
        previous_budget = scope["readBytes"], scope["readCalls"]
        accesses = []
        try:
            if pending_accesses:
                self.db.execute("BEGIN IMMEDIATE")
            for marker, cache, operation, authorized_ids, expires_after_floor in pending_accesses:
                # Copy an inherited payload into this branch before recording
                # access; expiry in this branch must not depend on reconstructing
                # from an ancestor after the fork cutoff.
                self.db.execute("INSERT OR IGNORE INTO play_lifecycle_archive_cache "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (campaign, branch, revision,
                    marker["branchId"], marker["revision"], marker["eventId"], marker["rootId"],
                    cache[0], cache[1]))
                accesses.append(self._record_lifecycle_access(campaign=campaign, branch=branch,
                    revision=revision, epoch=scope["binding"]["epoch"], story_order=story_order,
                    floor=scope["acceptedFloor"], marker=marker, operation=operation,
                    expires_after_floor=expires_after_floor,
                    restored_record_ids=authorized_ids if operation == "restore" else ()))
            self._charge(scope, len(_json(result).encode("utf-8", errors="strict")))
            if pending_accesses:
                self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            scope["readBytes"], scope["readCalls"] = previous_budget
            raise
        scope["lifecycleAccesses"].extend(accesses)
        self._refresh_lifecycle_scope(scope, refresh_cache=True)
        logger.info("play archive operation=%s campaign=%s branch=%s floor=%d requested=%d delivered=%d renewed=%d expired=%d missing=%d",
            params["operation"], campaign, branch, scope["acceptedFloor"], len(requested),
            sum(len(row["records"]) for row in events), renewed, len(expired), len(missing))
        return result

    def dispatch(self, method, params):
        schema = RPC_PARAMS.get(method)
        if schema is None:
            fail("method not found", "method-not-found", -32601)
        validate(params, schema, "params")
        return getattr(self, method)(params)
