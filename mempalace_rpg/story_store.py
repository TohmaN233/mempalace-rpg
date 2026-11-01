"""Additive story-recall-v1 service over immutable, host-filtered recipient views.

This contract deliberately does not mint legacy approved acquisition grants.
The host owns material authorization; a bound view cannot change after opening.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import logging
import math
import secrets

from .evidence_protocol import EvidenceError, fields, integer, sha256, string, string_list, unicode_text
from .retrieval import AuthorizedRetrievalCandidate, FixedP5Policy, SixViewRanker, structured_observation

logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


class StoryStore:
    def __init__(self, encoder):
        self.ranker = SixViewRanker(encoder, diagnostic_ledger=False, routing_policy=FixedP5Policy())
        self.views = {}

    def capabilities(self, params):
        fields(params, set())
        return {"protocol": "story-recall-v1", "profile": "static-p5", "encoderIdentity": self.ranker.encoder.identity}

    def open_view(self, params):
        fields(params, {"binding", "entries", "corpusHash", "maxReadCharacters"})
        fields(params["binding"], {"materialRevisionId", "materialHash", "channel", "point", "reviewPolicy"})
        for key in ("materialRevisionId", "materialHash", "channel", "reviewPolicy"):
            string(params["binding"][key], key)
        sha256(params["binding"]["materialHash"], "materialHash")
        fields(params["binding"]["point"], {"pathId", "worldlineId", "order"})
        string(params["binding"]["point"]["pathId"], "pathId")
        string(params["binding"]["point"]["worldlineId"], "worldlineId")
        integer(params["binding"]["point"]["order"], "point order")
        channel = params["binding"]["channel"]
        if channel != "plot" and not channel.startswith("actor:"):
            raise ValueError("invalid story recipient")
        if len(self.views) >= 128:
            raise EvidenceError(-32002, "story view capacity exceeded", kind="capacity-exceeded")
        if params["binding"]["reviewPolicy"] not in ("human-reviewed-only", "allow-unreviewed-experimental"):
            raise ValueError("invalid story review policy")
        if not isinstance(params["entries"], list):
            raise ValueError("entries must be an array")
        if digest({"binding": params["binding"], "entries": params["entries"]}) != params["corpusHash"]:
            raise ValueError("story authorized corpus hash mismatch")
        entries = {}
        for entry in params["entries"]:
            fields(entry, {"id", "sceneId", "order", "original", "summaries"})
            string(entry["id"], "id")
            string(entry["sceneId"], "sceneId")
            integer(entry["order"], "order")
            if entry["id"] in entries:
                raise ValueError("duplicate story entry")
            packets = ([entry["original"]] if entry["original"] is not None else []) + entry["summaries"]
            if not packets:
                raise ValueError("empty story entry")
            representations = set()
            for packet in packets:
                fields(packet, {"id", "purpose", "actorId", "representation", "text", "textSha256", "sources",
                                "materialRevisionId", "materialHash", "analysisId", "analysisVersion"})
                if packet["purpose"] != ("plot" if channel == "plot" else "actor") or packet["actorId"] != (None if channel == "plot" else channel[6:]):
                    raise ValueError("story packet recipient mismatch")
                unicode_text(packet["text"], "packet text", nonempty=True)
                if hashlib.sha256(packet["text"].encode("utf-8")).hexdigest() != packet["textSha256"]:
                    raise ValueError("story representation UTF-8 hash mismatch")
                if packet["materialRevisionId"] != params["binding"]["materialRevisionId"] or packet["materialHash"] != params["binding"]["materialHash"]:
                    raise ValueError("story packet material mismatch")
                if packet["representation"] in representations or packet["representation"] not in ("original", "event-summary", "scene-summary"):
                    raise ValueError("invalid or duplicate representation")
                if packet["representation"] == "original":
                    if packet["analysisId"] is not None or packet["analysisVersion"] is not None:
                        raise ValueError("original packet must not claim summary analysis identity")
                else:
                    string(packet["analysisId"], "summary analysisId")
                    integer(packet["analysisVersion"], "summary analysisVersion", minimum=1)
                representations.add(packet["representation"])
                if not isinstance(packet["sources"], list) or not packet["sources"]:
                    raise ValueError("packet source references must be an array")
                for ref in packet["sources"]:
                    fields(ref, {"sourceId", "sourceVersion", "startByte", "endByte", "sha256"})
                    string(ref["sourceId"], "sourceId")
                    string(ref["sourceVersion"], "sourceVersion")
                    integer(ref["startByte"], "startByte")
                    integer(ref["endByte"], "endByte", minimum=ref["startByte"] + 1)
                    sha256(ref["sha256"], "source sha256")
            entries[entry["id"]] = deepcopy(entry)
        handle = secrets.token_urlsafe(32)
        self.views[handle] = {"binding": deepcopy(params["binding"]), "entries": entries, "hash": params["corpusHash"],
                              "handles": {}, "read": 0, "maxRead": integer(params["maxReadCharacters"], "maxReadCharacters")}
        logger.info("story view opened channel=%s entries=%d corpus=%s", params["binding"]["channel"], len(entries), params["corpusHash"])
        return {"viewHandle": handle, "corpusHash": params["corpusHash"]}

    def _view(self, params):
        view = self.views.get(params["viewHandle"])
        if view is None:
            raise EvidenceError(-32003, "unknown story view", kind="invalid-view")
        return view

    def search(self, params):
        fields(params, {"viewHandle", "query", "limit"}, {"excludedIds"})
        view = self._view(params)
        query = unicode_text(params["query"], "query", nonempty=True)
        limit = integer(params["limit"], "limit", minimum=1, maximum=20)
        excluded_ids = set()
        if "excludedIds" in params:
            requested_exclusions = string_list(params["excludedIds"], "excludedIds")
            if not set(requested_exclusions).issubset(view["entries"]):
                raise EvidenceError(-32602, "excludedIds contains an unauthorized story entry", kind="invalid-params")
            excluded_ids = set(requested_exclusions)
        candidates = []
        for entry in view["entries"].values():
            # U is the complete authorized acquired original corpus. Scene-only
            # overview entries provide additional graph navigation, not fake events.
            if entry["original"] is None:
                continue
            raw = entry["original"]["text"]
            observation = structured_observation(summary=raw, event_type="scene_event", actor_id=None, target_id=None,
                related_entities=[], related_quests=[], related_locations=[], in_world_time=None, location_id=None)
            candidates.append(AuthorizedRetrievalCandidate(entry["id"], entry["sceneId"], raw, observation,
                entry["sceneId"], (view["binding"]["materialHash"], view["binding"]["channel"], None, None, None, None, None, None),
                (entry["order"], entry["id"]), entry["id"]))
        result = self.ranker.rank(query=query, candidates=candidates) if candidates else None
        ranked = result.ranked_event_ids if result else []
        if set(ranked) != {candidate.source_event_id for candidate in candidates} or len(ranked) != len(set(ranked)):
            raise RuntimeError("P5 returned incomplete or unauthorized order")
        if result and (set(result.scores) != set(ranked) or any(type(score) not in (int, float) or not math.isfinite(score) for score in result.scores.values())):
            raise RuntimeError("P5 returned non-finite scores")
        eligible = [identifier for identifier in ranked if identifier not in excluded_ids]
        top = eligible[:limit]
        rows = []
        for rank, identifier in enumerate(top, 1):
            handle = secrets.token_urlsafe(32)
            view["handles"][handle] = identifier
            entry = view["entries"][identifier]
            rows.append({"handle": handle, "id": identifier, "rank": rank})
        logger.info("story P5 search corpus=%s candidates=%d excluded=%d returned=%d", view["hash"], len(candidates),
                    len(excluded_ids), len(rows))
        trace = {"profile": "static-p5", "corpusHash": view["hash"],
                "encoderIdentity": self.ranker.encoder.identity, "rankedIds": top,
                "inputHash": result.trace["input_sha256"] if result else None}
        if "excludedIds" in params:
            trace["selectionPolicy"] = "host-source-nonoverlap-v1"
            trace["excludedIds"] = requested_exclusions
        return {"candidates": rows, "trace": trace}

    def read(self, params):
        fields(params, {"viewHandle", "handle", "representation"})
        view = self._view(params)
        identifier = view["handles"].get(params["handle"])
        if identifier is None:
            raise EvidenceError(-32004, "unexposed story handle", kind="invalid-handle")
        entry = view["entries"][identifier]
        packets = ([entry["original"]] if entry["original"] else []) + entry["summaries"]
        packet = next((item for item in packets if item["representation"] == params["representation"]), None)
        if packet is None:
            raise EvidenceError(-32004, "unavailable representation", kind="invalid-representation")
        count = len(packet["text"])
        if view["read"] + count > view["maxRead"]:
            raise EvidenceError(-32010, "story read budget exceeded", kind="read-budget-exceeded")
        view["read"] += count
        return {"packet": deepcopy(packet)}

    def expose(self, params):
        """Host navigation may expose only IDs already in this immutable recipient view."""
        fields(params, {"viewHandle", "ids"})
        view = self._view(params)
        identifiers = string_list(params["ids"], "ids")
        if not 1 <= len(identifiers) <= 20:
            raise EvidenceError(-32602, "navigation ids count must be 1 to 20", kind="invalid-params")
        if not set(identifiers).issubset(view["entries"]):
            raise EvidenceError(-32004, "navigation contains an unauthorized entry", kind="invalid-handle")
        rows = []
        for rank, identifier in enumerate(identifiers, 1):
            handle = secrets.token_urlsafe(32)
            view["handles"][handle] = identifier
            rows.append({"handle": handle, "id": identifier, "rank": rank})
        logger.info("story navigation expose corpus=%s returned=%d", view["hash"], len(rows))
        return {"candidates": rows, "trace": {"policy": "host-bound-expose-v1", "corpusHash": view["hash"],
                                              "ids": identifiers}}

    def close_view(self, params):
        fields(params, {"viewHandle"})
        self._view(params)
        del self.views[params["viewHandle"]]
        return {"closed": True}

    def dispatch(self, method, params):
        if method not in ("capabilities", "open_view", "search", "expose", "read", "close_view"):
            raise EvidenceError(-32601, "unknown story method", kind="method-not-found")
        return getattr(self, method)(params)

    def close(self):
        logger.info("story store closed leases=%d", len(self.views))
        self.views.clear()
