"""Immutable JSON custody, temporal grants, and native P5 evidence leases.

This is a new host-published projection, not a compatibility facade over the
kernel's mutable SQLite schema. No kernel is instantiated and no file is written.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import logging
import math
from pathlib import Path
import secrets
from typing import Any

from .evidence_protocol import (
    EvidenceError, METHODS, PROJECTION_VERSION, PROTOCOL_VERSION, binding,
    canonical_hash, fields, integer, invalid, sha256, strict_json_loads, string,
    string_list, timestamp, unicode_text,
)
from .retrieval import AuthorizedRetrievalCandidate, FixedP5Policy, RankingResult, SixViewRanker, structured_observation

logger = logging.getLogger(__name__)
EVENT_KEYS = {"campaignId", "sourceId", "sourceVersion", "originBranch", "sourceCommit",
              "sceneId", "occurredAt", "text", "rawSha256", "grants"}
GRANT_KEYS = {"actorId", "acquiredAt", "revealedAt", "originBranch", "sourceCommit", "reviewStatus"}
RETRIEVAL_KEYS = {"observation", "checkpointKey", "policyTuple", "chronologicalOrderKey", "rankingKey"}
MAX_READ_BYTES = 24_000
MAX_DISTINCT_READ_EVENTS = 12


def validate_branches(rows: Any, selected: str) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list):
        raise invalid("branches must be a list")
    branches = {}
    for row in rows:
        fields(row, {"branchId", "parentId", "forkSourceCommit"}, label="branch")
        identifier = string(row["branchId"], "branchId")
        if identifier in branches:
            raise invalid("duplicate branchId")
        if row["parentId"] is None:
            if row["forkSourceCommit"] is not None:
                raise invalid("root forkSourceCommit must be null")
        else:
            string(row["parentId"], "parentId")
            integer(row["forkSourceCommit"], "forkSourceCommit")
        branches[identifier] = deepcopy(row)
    if selected not in branches:
        raise invalid("watermark branch is unknown")
    for identifier in branches:
        seen = set()
        current = identifier
        while current is not None:
            if current in seen or current not in branches:
                raise invalid("branches contain a cycle or missing parent")
            seen.add(current)
            current = branches[current]["parentId"]
    return branches


def ancestry_cutoffs(branches: dict[str, dict[str, Any]], selected: str) -> dict[str, int | float]:
    """Every crossed fork contributes a cutoff, including nested child forks."""
    result = {}
    current = selected
    cutoff = math.inf
    while current is not None:
        result[current] = cutoff
        row = branches[current]
        if row["parentId"] is not None:
            cutoff = min(cutoff, row["forkSourceCommit"])
        current = row["parentId"]
    return result


def validate_event(event: Any, branches: dict[str, dict[str, Any]]) -> dict[str, Any]:
    fields(event, EVENT_KEYS, {"retrieval"}, label="event")
    for key in EVENT_KEYS - {"sourceCommit", "occurredAt", "text", "rawSha256", "grants"}:
        string(event[key], key)
    integer(event["sourceCommit"], "sourceCommit")
    timestamp(event["occurredAt"], "occurredAt")
    unicode_text(event["text"], "text", nonempty=True)
    sha256(event["rawSha256"], "rawSha256")
    if hashlib.sha256(event["text"].encode("utf-8")).hexdigest() != event["rawSha256"]:
        raise invalid("event UTF-8 rawSha256 mismatch")
    if event["originBranch"] not in branches:
        raise invalid("event originBranch is unknown")
    if not isinstance(event["grants"], list):
        raise invalid("event grants must be a list")
    for grant in event["grants"]:
        fields(grant, GRANT_KEYS, label="grant")
        string(grant["actorId"], "grant actorId")
        string(grant["originBranch"], "grant originBranch")
        integer(grant["sourceCommit"], "grant sourceCommit")
        timestamp(grant["acquiredAt"], "acquiredAt")
        if grant["revealedAt"] is not None:
            timestamp(grant["revealedAt"], "revealedAt")
        if grant["reviewStatus"] not in ("approved", "needs-review", "rejected"):
            raise invalid("unknown grant reviewStatus")
        if grant["originBranch"] not in branches:
            raise invalid("grant originBranch is unknown")
    if "retrieval" in event:
        retrieval = fields(event["retrieval"], RETRIEVAL_KEYS, label="retrieval")
        unicode_text(retrieval["observation"], "observation", nonempty=True)
        string(retrieval["checkpointKey"], "checkpointKey")
        string(retrieval["rankingKey"], "rankingKey")
        policy = retrieval["policyTuple"]
        if not isinstance(policy, list) or len(policy) != 8:
            raise invalid("policyTuple must contain eight nullable strings")
        for item in policy:
            if item is not None:
                unicode_text(item, "policyTuple item")
        chronological = retrieval["chronologicalOrderKey"]
        if not isinstance(chronological, list) or len(chronological) != 2:
            raise invalid("chronologicalOrderKey must be [integer, rankingKey]")
        integer(chronological[0], "chronologicalOrderKey")
        if chronological[1] != retrieval["rankingKey"]:
            raise invalid("chronologicalOrderKey must bind rankingKey")
    return deepcopy(event)


@dataclass
class Lease:
    binding: dict[str, Any]
    events: dict[str, dict[str, Any]]
    universe_hash: str
    handles: dict[str, tuple[str, bool]] = field(default_factory=dict)
    event_handles: dict[str, str] = field(default_factory=dict)
    span_handles: dict[str, str] = field(default_factory=dict)
    cursors: dict[str, int] = field(default_factory=dict)
    read_bytes: int = 0
    read_events: set[str] = field(default_factory=set)


class EvidenceStore:
    def __init__(self, projection_path: str | Path, *, encoder) -> None:
        requested = Path(projection_path)
        if requested.is_symlink():
            raise ValueError("projection must not be a symbolic link")
        self._path = requested.resolve(strict=True)
        if not self._path.is_file():
            raise ValueError("projection must be an immutable regular JSON file")
        # One read freezes the complete generation in memory. Later publication
        # or path replacement cannot alter any open lease.
        raw = self._path.read_bytes()
        projection = strict_json_loads(raw)
        fields(projection, {"schema", "watermark", "branches", "events"}, label="projection")
        if projection["schema"] != PROJECTION_VERSION:
            raise invalid("unsupported evidence projection schema")
        self._watermark = binding(projection["watermark"])
        self._branches = validate_branches(projection["branches"], self._watermark["branchId"])
        if not isinstance(projection["events"], list):
            raise invalid("projection events must be a list")
        self._events = {}
        for value in projection["events"]:
            event = validate_event(value, self._branches)
            if event["campaignId"] != self._watermark["campaignId"]:
                raise invalid("projection mixes campaigns")
            if event["sourceId"] in self._events:
                raise invalid("projection has duplicate sourceId")
            self._events[event["sourceId"]] = event
        # Match the trusted host's JSON.stringify({branches, events}) byte
        # encoding: preserve original JSON property insertion order, emit UTF-8
        # scalars directly, use compact separators. The strict schema permits
        # only safe-integer numeric fields, so JS/Python number encoding agrees.
        # Hash the original objects, never the validator's reconstructed maps.
        source_index = hashlib.sha256(json.dumps(
            {"branches": projection["branches"], "events": projection["events"]},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")).hexdigest()
        if self._watermark["sourceIndexVersion"] != source_index:
            raise EvidenceError(-32007, "projection payload does not match sourceIndexVersion", kind="source-index-mismatch")
        self._ranker = SixViewRanker(encoder, diagnostic_ledger=False, routing_policy=FixedP5Policy())
        self._leases: dict[str, Lease] = {}
        logger.info("evidence projection loaded sha256=%s events=%d sourceIndexVersion=%s encoder=%s",
                    hashlib.sha256(raw).hexdigest(), len(self._events),
                    self._watermark["sourceIndexVersion"], self._ranker.encoder.identity)

    def capabilities(self, params):
        fields(params, set())
        return {"protocolVersion": PROTOCOL_VERSION, "capabilities": list(METHODS),
                "retrieval": {"profile": "static-p5", "rrfK": 60, "weights": {
                    "rawBm25": 2.0, "observationBm25": 0.5, "rawDense": 1.0,
                    "observationDense": 2.0, "checkpointDense": 0.0, "comboDense": 1.0}}}

    def _authorized(self, bound):
        cutoffs = ancestry_cutoffs(self._branches, bound["branchId"])
        as_of = min(timestamp(bound["asOf"], "asOf"), timestamp(bound["storyTime"], "storyTime"))

        def visible(origin, commit):
            return origin in cutoffs and commit <= cutoffs[origin]

        authorized = {}
        for identifier, event in self._events.items():
            if not visible(event["originBranch"], event["sourceCommit"]) or timestamp(event["occurredAt"], "occurredAt") > as_of:
                continue
            grants = [grant for grant in event["grants"] if (
                grant["actorId"] == bound["actorId"] and grant["reviewStatus"] == "approved"
                and timestamp(grant["acquiredAt"], "acquiredAt") <= as_of
                and (grant["revealedAt"] is None or timestamp(grant["revealedAt"], "revealedAt") <= as_of)
                and visible(grant["originBranch"], grant["sourceCommit"])
            )]
            if grants:
                output = deepcopy(event)
                output["grants"] = grants
                authorized[identifier] = output
        return authorized

    def open_snapshot(self, params):
        fields(params, {"binding"})
        bound = binding(params["binding"])
        if bound != self._watermark:
            raise EvidenceError(-32001, "snapshot binding does not match published watermark", kind="binding-mismatch")
        if len(self._leases) >= 128:
            raise EvidenceError(-32002, "snapshot lease capacity exceeded", kind="capacity-exceeded")
        events = self._authorized(bound)
        handle = secrets.token_urlsafe(32)
        universe_hash = canonical_hash({"binding": bound, "events": [
            {key: value for key, value in events[identifier].items() if key != "retrieval"}
            for identifier in sorted(events)
        ]})
        self._leases[handle] = Lease(bound, events, universe_hash)
        logger.info("snapshot opened handle=%s authorized=%d universe=%s", handle, len(events), universe_hash)
        return {"protocolVersion": PROTOCOL_VERSION, "snapshotHandle": handle,
                "binding": deepcopy(bound), "branches": deepcopy(list(self._branches.values()))}

    def _lease(self, params):
        handle = string(params["snapshotHandle"], "snapshotHandle")
        lease = self._leases.get(handle)
        if lease is None:
            raise EvidenceError(-32003, "unknown or closed snapshot", kind="invalid-snapshot")
        return lease

    def _handle(self, lease: Lease, identifier: str, *, seed: bool = False) -> str:
        existing = lease.event_handles.get(identifier)
        if existing is None:
            existing = secrets.token_urlsafe(32)
            lease.event_handles[identifier] = existing
            lease.handles[existing] = (identifier, seed)
        elif seed:
            lease.handles[existing] = (identifier, True)
        return existing

    def _event(self, lease: Lease, handle):
        string(handle, "eventHandle")
        result = lease.handles.get(handle)
        if result is None:
            raise EvidenceError(-32004, "event handle is not valid in this snapshot", kind="invalid-event")
        return result

    def _candidate(self, lease: Lease, identifier: str, native_rank: int):
        event = lease.events[identifier]
        return {"eventHandle": self._handle(lease, identifier, seed=True),
                **{key: event[key] for key in ("sourceId", "sourceVersion", "originBranch", "sourceCommit",
                                              "sceneId", "occurredAt", "rawSha256")},
                "preview": event["text"][:240], "nativeRank": native_rank}

    def _ranking_candidate(self, lease: Lease, event):
        retrieval = event.get("retrieval")
        if retrieval is None:
            ranking_key = event["sourceId"]
            observation = structured_observation(
                summary=event["text"], event_type="scene_event", actor_id=None, target_id=None,
                related_entities=[], related_quests=[], related_locations=[],
                in_world_time=event["occurredAt"], location_id=None,
            )
            chronological = (int(timestamp(event["occurredAt"], "occurredAt").timestamp() * 1000), ranking_key)
            policy = (event["campaignId"], lease.binding["actorId"], event["originBranch"],
                      "active", None, None, None, canonical_hash(event["grants"]))
            checkpoint = event["sceneId"]
        else:
            ranking_key = retrieval["rankingKey"]
            observation = retrieval["observation"]
            chronological = tuple(retrieval["chronologicalOrderKey"])
            policy = tuple(retrieval["policyTuple"])
            checkpoint = retrieval["checkpointKey"]
        return AuthorizedRetrievalCandidate(
            source_event_id=event["sourceId"], source_scene_id=event["sceneId"],
            raw_text=event["text"], observation=observation, checkpoint_key=checkpoint,
            policy_tuple=policy, chronological_order_key=chronological, ranking_key=ranking_key)

    def search(self, params):
        fields(params, {"snapshotHandle", "query"}, {"scopeSceneIds"})
        lease = self._lease(params)
        query = unicode_text(params["query"], "query", nonempty=True)
        if not query.strip():
            raise invalid("query must not be blank")
        scope = None if "scopeSceneIds" not in params else set(string_list(params["scopeSceneIds"], "scopeSceneIds"))
        trace = {"profile": "static-p5", "authorizedUniverseHash": lease.universe_hash,
                 "rankedTop10SourceIds": [], "encoderIdentity": self._ranker.encoder.identity, "inputHash": None}
        if not lease.events:
            return {"status": "no-history", "candidates": [], "trace": trace}
        result = self._ranker.rank(query=query, candidates=[self._ranking_candidate(lease, event) for event in lease.events.values()])
        if not isinstance(result, RankingResult) or len(result.ranked_event_ids) != len(set(result.ranked_event_ids)) or set(result.ranked_event_ids) != set(lease.events):
            raise RuntimeError("P5 returned an incomplete or unauthorized candidate order")
        if set(result.scores) != set(lease.events) or any(type(score) not in (float, int) or not math.isfinite(score) for score in result.scores.values()):
            raise RuntimeError("P5 returned invalid candidate scores")
        top = result.ranked_event_ids[:10]
        trace["rankedTop10SourceIds"] = top
        trace["inputHash"] = result.trace["input_sha256"]
        candidates = [self._candidate(lease, identifier, rank) for rank, identifier in enumerate(top, 1)
                      if scope is None or lease.events[identifier]["sceneId"] in scope]
        logger.info("search universe=%s ranked=%d scopedReturned=%d input=%s", lease.universe_hash, len(lease.events), len(candidates), trace["inputHash"])
        return {"status": "ok", "candidates": candidates, "trace": trace}

    @staticmethod
    def _scenes(lease):
        scenes = {}
        for event in lease.events.values():
            scenes.setdefault(event["sceneId"], []).append(event)
        return sorted(scenes.items(), key=lambda pair: (min(timestamp(event["occurredAt"], "occurredAt") for event in pair[1]), pair[0]))

    def catalogue(self, params):
        fields(params, {"snapshotHandle"}, {"cursor", "limit"})
        lease = self._lease(params)
        limit = integer(params.get("limit", 12), "limit", minimum=1, maximum=12)
        offset = 0
        if "cursor" in params:
            cursor = string(params["cursor"], "cursor")
            if cursor not in lease.cursors:
                raise EvidenceError(-32005, "catalogue cursor is not valid in this snapshot", kind="invalid-cursor")
            offset = lease.cursors[cursor]
        scenes = self._scenes(lease)
        nodes = []
        for scene_id, events in scenes[offset:offset + limit]:
            ordered = sorted(events, key=lambda event: timestamp(event["occurredAt"], "occurredAt"))
            nodes.append({"sceneId": scene_id, "eventCount": len(events),
                          "firstOccurredAt": ordered[0]["occurredAt"], "lastOccurredAt": ordered[-1]["occurredAt"]})
        next_cursor = None
        if offset + limit < len(scenes):
            next_cursor = secrets.token_urlsafe(32)
            lease.cursors[next_cursor] = offset + limit
        return {"nodes": nodes, "nextCursor": next_cursor}

    def _read(self, lease, identifier):
        event = lease.events[identifier]
        handle = self._handle(lease, identifier)
        projection = {"eventHandle": handle, **deepcopy({key: event[key] for key in EVENT_KEYS})}
        if identifier not in lease.span_handles:
            lease.span_handles[identifier] = secrets.token_urlsafe(32)
        span = {"eventHandle": handle, "spanHandle": lease.span_handles[identifier],
                **{key: event[key] for key in ("sourceId", "sourceVersion", "rawSha256", "text")},
                "startByte": 0, "endByteExclusive": len(event["text"].encode("utf-8"))}
        return {"span": span, "projection": projection}

    def _charge_read_group(self, lease: Lease, identifiers: list[str]) -> None:
        # Count each event's exact original text once per operation, even though
        # the host wire carries it in both span and projection. Repeated reads
        # charge the original bytes again; there is no pretend read cache.
        required_bytes = sum(len(lease.events[identifier]["text"].encode("utf-8")) for identifier in identifiers)
        next_events = lease.read_events | set(identifiers)
        if lease.read_bytes + required_bytes > MAX_READ_BYTES or len(next_events) > MAX_DISTINCT_READ_EVENTS:
            logger.warning("read group refused universe=%s consumedBytes=%d requestedBytes=%d distinctAfter=%d",
                           lease.universe_hash, lease.read_bytes, required_bytes, len(next_events))
            raise EvidenceError(-32010, "snapshot evidence read budget exceeded", kind="read-budget-exceeded")
        lease.read_bytes += required_bytes
        lease.read_events = next_events
        logger.info("read group charged universe=%s bytes=%d distinct=%d", lease.universe_hash,
                    lease.read_bytes, len(lease.read_events))

    def read_event(self, params):
        fields(params, {"snapshotHandle", "eventHandle"})
        lease = self._lease(params)
        identifier, _seed = self._event(lease, params["eventHandle"])
        # The whole event is returned so late negations/conditions cannot be
        # dropped by a preview or model-selected span boundary.
        self._charge_read_group(lease, [identifier])
        return self._read(lease, identifier)

    def read_scene_window(self, params):
        fields(params, {"snapshotHandle", "eventHandle"})
        lease = self._lease(params)
        identifier, seed = self._event(lease, params["eventHandle"])
        if not seed:
            raise EvidenceError(-32006, "scene-window expansion requires a search seed", kind="window-depth-exceeded")
        source_scene = lease.events[identifier]["sceneId"]
        scenes = self._scenes(lease)
        index = next(index for index, (scene_id, _events) in enumerate(scenes) if scene_id == source_scene)
        selected_scenes = scenes[max(0, index - 1):index + 2]
        # Reject the complete group before materializing any plaintext or
        # granting handles for neighbor events. No partial window escapes.
        self._charge_read_group(lease, [event["sourceId"] for _scene_id, events in selected_scenes for event in events])
        window = []
        for scene_id, events in selected_scenes:
            # Every event comes from the independently authorized U; knowing a
            # scene or witnessing one event grants no transcript-wide access.
            window.append({"sceneId": scene_id, "events": [self._read(lease, event["sourceId"])
                for event in sorted(events, key=lambda event: (timestamp(event["occurredAt"], "occurredAt"), event["sourceCommit"], event["sourceId"]))]})
        return {"scenes": window}

    def close_snapshot(self, params):
        fields(params, {"snapshotHandle"})
        self._lease(params)
        del self._leases[params["snapshotHandle"]]
        logger.info("snapshot closed handle=%s", params["snapshotHandle"])
        return {"closed": True}

    def close(self):
        count = len(self._leases)
        self._leases.clear()
        logger.info("evidence store closed leases=%d", count)

    def dispatch(self, method, params):
        if method not in METHODS:
            raise EvidenceError(-32601, "method not found", kind="method-not-found")
        return getattr(self, method)(params)
