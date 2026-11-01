"""Authoritative, dependency-free play-memory-v1 wire contract.

SCHEMA is exported verbatim to schemas/play-memory-v1.schema.json. It describes
wire shape; the store additionally checks references, bytes, time and lineage.
"""
from __future__ import annotations

import copy
import re
from .evidence_protocol import EvidenceError, canonical_hash

VERSION = "play-memory-v1"
MAX_NAVIGATION_SUMMARIES = 4096
MAX_NAVIGATION_SUMMARY_BYTES = 65536
MAX_NAVIGATION_SUMMARIES_BYTES = 4 * 1024 * 1024
MAX_NAVIGATION_CARDS = 100
MIN_NAVIGATION_CARD_BUDGET_BYTES = 512
MAX_NAVIGATION_CARD_BUDGET_BYTES = 65536


def fail(message, kind="invalid-params", code=-32602):
    raise EvidenceError(code, message, kind=kind)


def obj(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


STR = {"type": "string", "minLength": 1}
TEXT = {"type": "string"}
INT = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
STRINGS = {"type": "array", "items": STR, "uniqueItems": True}
NULL_STR = {"anyOf": [STR, {"type": "null"}]}
SPAN = obj({"sourceId": STR, "version": STR, "startByte": INT, "endByte": INT, "sha256": HASH})
SPANS = {"type": "array", "items": SPAN, "minItems": 1}
REGISTRY_INTRODUCTION = obj({
    "entityKind": {"enum": ["character", "monster"]},
    "hasProperName": {"type": "boolean"},
    "intelligent": {"type": "boolean"},
    "plotImportant": {"type": "boolean"},
})
REGISTRY_FIRST_APPEARANCE = obj({
    "requestId": STR,
    "panelHash": HASH,
    "basis": {"enum": ["panel", "legacy-name-profile", "static-profile"]},
}, ["requestId", "panelHash"])
REGISTRY_ENTRY = obj({
    "actorId": STR,
    "canonicalName": STR,
    "aliases": STRINGS,
    "introduction": REGISTRY_INTRODUCTION,
    "firstAppearance": REGISTRY_FIRST_APPEARANCE,
})
CHARACTER_REGISTRY = obj({
    "schemaVersion": {"const": "play-memory-character-registry-v1"},
    "entries": {"type": "array", "items": REGISTRY_ENTRY},
})
MEMORY_LIFECYCLE = obj({
    "schema": {"const": "episodic-memory-v1"},
    "phase": {"enum": ["draft", "active", "resolved"]},
    "parentEventId": STR,
    "archiveEventIds": STRINGS,
    "restoreEventIds": STRINGS,
}, ["schema", "phase"])
MEMORY_MAINTENANCE_ARCHIVE = obj({
    "eventId": STR,
    "relatedEventIds": STRINGS,
}, ["eventId"])
MEMORY_MAINTENANCE = obj({
    "schema": {"const": "episodic-maintenance-v1"},
    "archives": {"type": "array", "minItems": 1, "items": MEMORY_MAINTENANCE_ARCHIVE},
})
EVENT_METADATA = {"type": "object", "properties": {
    "memoryLifecycle": MEMORY_LIFECYCLE,
}, "additionalProperties": True}
ACQUISITION_METADATA = {"type": "object", "properties": {
    "memorySupersedes": STRINGS,
}, "additionalProperties": True}
SCENE_METADATA = {"type": "object", "properties": {
    "characterRegistry": CHARACTER_REGISTRY,
    "memoryMaintenance": MEMORY_MAINTENANCE,
},
                  "additionalProperties": True}
SOURCE_METADATA = {"type": "object", "properties": {
    "authored": {"type": "boolean"},
    "characterRegistryImport": {"type": "boolean"},
    "characterRegistry": CHARACTER_REGISTRY,
    "memoryMaintenance": MEMORY_MAINTENANCE,
}, "additionalProperties": True}
COMMON = {"id": STR, "order": INT, "validUntil": INT, "metadata": {"type": "object"}}


def record(kind, properties, required):
    props = {**COMMON, "kind": {"const": kind}, **properties}
    return obj(props, ["kind", "id", "order", *required])


RECORDS = {
    "source": record("source", {"sourceId": STR, "version": STR, "text": TEXT, "sha256": HASH,
                                  "metadata": SOURCE_METADATA}, ["sourceId", "version", "text", "sha256"]),
    "scene": record("scene", {"text": TEXT, "sourceSpans": SPANS, "metadata": SCENE_METADATA}, ["text", "sourceSpans"]),
    "character-library": record("character-library", {
        "actorId": STR,
        "status": {"enum": ["saved", "deleted"]},
        "sourceSpans": SPANS,
    }, ["actorId", "status", "sourceSpans"]),
    "event": record("event", {"text": TEXT, "sceneId": STR, "sourceSpans": SPANS,
                                "beliefKind": STR, "metadata": EVENT_METADATA}, ["text", "sourceSpans"]),
    "link": record("link", {"actorId": STR, "eventIds": STRINGS, "relation": STR, "sourceSpans": SPANS}, ["actorId", "eventIds", "relation", "sourceSpans"]),
    "acquisition": record("acquisition", {"actorId": STR, "text": TEXT, "eventIds": STRINGS,
        "claimId": STR, "beliefKind": {"enum": ["fact", "hearsay", "inference", "mistaken-belief"]},
        "method": STR, "basisIds": STRINGS, "sourceSpans": SPANS,
        "metadata": ACQUISITION_METADATA},
        ["actorId", "text", "beliefKind", "method", "basisIds", "sourceSpans"]),
    "attitude": record("attitude", {"actorId": STR, "acquisitionId": STR, "attitude": {"enum": ["disbelieve", "doubtful", "believe"]}, "basisIds": STRINGS, "sourceSpans": SPANS}, ["actorId", "acquisitionId", "attitude", "basisIds", "sourceSpans"]),
    "profile": record("profile", {"actorId": STR, "field": STR, "text": TEXT, "knownBy": STRINGS, "sourceSpans": SPANS}, ["actorId", "field", "text", "knownBy", "sourceSpans"]),
    "state": record("state", {"actorId": STR, "field": STR, "text": TEXT, "knownBy": STRINGS, "sourceSpans": SPANS}, ["actorId", "field", "text", "knownBy", "sourceSpans"]),
    "relationship": record("relationship", {"fromActorId": STR, "toActorId": STR, "text": TEXT, "knownBy": STRINGS, "sourceSpans": SPANS}, ["fromActorId", "toActorId", "text", "knownBy", "sourceSpans"]),
    "background": record("background", {"text": TEXT, "appliesTo": {**STRINGS, "description": "Explicit actor IDs or literal '*' for a Host-accepted shared common-knowledge/news default, including later actors. Persisted per-actor exceptions override it. This is not a scope.grants wildcard."}, "sourceSpans": SPANS}, ["text", "appliesTo", "sourceSpans"]),
    "exception": record("exception", {"actorId": STR, "backgroundId": STR, "status": {"enum": ["unknown", "acquired"]}, "basisIds": STRINGS, "sourceSpans": SPANS}, ["actorId", "backgroundId", "status", "basisIds", "sourceSpans"]),
    "date": record("date", {"text": TEXT, "calendarId": STR, "dateRule": {"type": "object"}, "knownBy": STRINGS, "sourceSpans": SPANS}, ["text", "calendarId", "dateRule", "knownBy", "sourceSpans"]),
}
PARENT = obj({"branchId": STR, "batchId": STR, "throughRevision": INT})
BATCH = obj({"schemaVersion": {"const": VERSION}, "campaignId": STR, "batchId": STR,
             "payloadHash": HASH, "predecessorBatchId": NULL_STR, "branchId": STR,
             "epoch": INT, "worldRevision": INT, "previousWorldRevision": INT, "records": {"type": "array", "items": {"oneOf": list(RECORDS.values())}},
             "parent": PARENT, "characterLibraryPolicy": {"const": "panel-named-intelligent-v1"}},
             ["schemaVersion", "campaignId", "batchId", "payloadHash", "predecessorBatchId", "branchId", "epoch", "worldRevision", "records"])
BINDING = obj({"campaignId": STR, "branchId": STR, "epoch": INT, "worldRevision": INT,
               "storyOrder": INT, "checkpointId": STR, "materialRevision": STR, "configHash": HASH,
               "purpose": {"enum": ["director-plot", "actor-knowledge", "review", "readonly"]},
               "actorIds": STRINGS, "grants": {**STRINGS, "description": "Exact existing record identities only. No wildcard or transitive/neighbor authority."},
               "lineage": {"type": "array", "minItems": 1, "items": obj({"branchId": STR, "throughRevision": INT})},
               "maxReadBytes": INT, "maxReadCalls": INT, "requiredBatchId": NULL_STR,
               "characterLibraryPolicy": {"const": "panel-named-intelligent-v1"}},
              ["campaignId", "branchId", "epoch", "worldRevision", "storyOrder", "checkpointId", "materialRevision", "configHash",
               "purpose", "actorIds", "grants", "lineage", "maxReadBytes", "maxReadCalls", "requiredBatchId"])
NAVIGATION_SUMMARY = obj({"id": STR, "sourceHash": HASH,
                          "summary": {"type": "string", "minLength": 1, "maxLength": MAX_NAVIGATION_SUMMARY_BYTES},
                          "summaryHash": HASH})
ACTOR_PROPOSAL = obj({"localRef": STR, "actorId": STR, "displayName": STR,
    "aliases": STRINGS, "present": {"type": "boolean"},
    "channel": {"enum": ["physical", "mental"]}, "quote": STR,
    "introduction": REGISTRY_INTRODUCTION},
    ["localRef", "displayName", "aliases", "present", "channel"])
FIRST_APPEARANCE = obj({"requestId": STR, "panelHash": HASH})
RPC_PARAMS = {
    "capabilities": obj({}),
    "apply_batch": obj({"hostToken": STR, "batch": BATCH}),
    "validate_batch": obj({"hostToken": STR, "batch": BATCH}),
    "receipt": obj({"hostToken": STR, "campaignId": STR, "batchId": STR}),
    "open_scope": obj({"hostToken": STR, "binding": BINDING,
                        "navigationSummaries": {"type": "array", "maxItems": MAX_NAVIGATION_SUMMARIES,
                                                "items": NAVIGATION_SUMMARY},
                        "retrievalPolicy": {"enum": ["scene-summaries-v1"]}},
                       ["hostToken", "binding"]),
    "actor_cues": obj({"hostToken": STR, "scopeHandle": STR, "actors": STRINGS}),
    "actor_dossiers": obj({"hostToken": STR, "scopeHandle": STR, "actors": STRINGS}),
    "actor_directory": obj({"hostToken": STR, "scopeHandle": STR}),
    "resolve_actors": obj({"hostToken": STR, "scopeHandle": STR, "originId": STR,
        "firstAppearance": FIRST_APPEARANCE,
        "preparedRegistry": {"type": "array", "items": REGISTRY_ENTRY},
        "source": obj({"sourceId": STR, "text": STR, "order": INT}),
        "proposals": {"type": "array", "items": ACTOR_PROPOSAL}},
        ["hostToken", "scopeHandle", "originId", "source", "proposals"]),
    "actor_packs": obj({"hostToken": STR, "scopeHandle": STR, "actors": STRINGS, "references": STRINGS, "budgetBytes": INT,
                        "detailPolicy": {"const": "character-importance-v1"}},
                       ["hostToken", "scopeHandle", "actors", "references", "budgetBytes"]),
    "memory_graph": obj({"hostToken": STR, "scopeHandle": STR}),
    "memory_archive": obj({"hostToken": STR, "scopeHandle": STR,
        "eventIds": {**STRINGS, "minItems": 1}, "operation": {"enum": ["read", "restore"]}}),
    "navigation_cards": obj({"hostToken": STR, "scopeHandle": STR,
                              "references": {**STRINGS, "maxItems": MAX_NAVIGATION_CARDS},
                              "budgetBytes": {**INT, "minimum": MIN_NAVIGATION_CARD_BUDGET_BYTES,
                                              "maximum": MAX_NAVIGATION_CARD_BUDGET_BYTES}}),
    "search": obj({"scopeHandle": STR, "query": STR, "limit": {**INT, "minimum": 1, "maximum": 100},
                   "excludeIds": {**STRINGS, "description": "Exact authorized records already delivered by the Host. Removed before ranking; grants no knowledge or read authority."}},
                  ["scopeHandle", "query", "limit"]),
    "warm_scope": obj({"hostToken": STR, "scopeHandle": STR}),
    "navigate": obj({"scopeHandle": STR, "seed": STR, "relation": {"enum": ["event", "scene", "actor", "source"]}, "page": INT, "limit": {**INT, "minimum": 1, "maximum": 100}}),
    "read": obj({"scopeHandle": STR, "handle": STR, "representation": {"enum": ["accepted", "original"]}}),
    "close_scope": obj({"hostToken": STR, "scopeHandle": STR}),
}
SCHEMA = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "urn:mempalace:play-memory-v1",
          "title": VERSION, "$defs": {"batch": BATCH, "binding": BINDING, "record": {"oneOf": list(RECORDS.values())}, "sourceSpan": SPAN, "rpcParams": {"type": "object", "properties": RPC_PARAMS}},
          "oneOf": [{"$ref": "#/$defs/batch"}, {"$ref": "#/$defs/binding"}]}
WATERMARK = {"type": "object", "additionalProperties": {**INT, "minimum": -1}}
CHARACTER_LIBRARY_PROJECTION = obj({
    "schemaVersion": {"const": "play-memory-character-library-v1"},
    "actors": {"type": "array", "items": obj({
        "actorId": STR, "displayName": STR, "aliases": STRINGS,
        "tier": {"enum": ["core", "major", "recurring", "ambient"]},
        "tierOverride": {"enum": ["core", "major", "recurring", "ambient", "auto"]},
    })},
})
RECEIPT = obj({"schemaVersion": {"const": VERSION}, "campaignId": STR, "batchId": STR,
               "payloadHash": HASH, "branchId": STR, "epoch": INT, "worldRevision": INT,
               "predecessorBatchId": NULL_STR, "readableWatermark": WATERMARK,
               "indexedWatermark": WATERMARK, "receiptHash": HASH,
               "vectorMode": {"enum": ["none", "live-authorized-ranking"]},
               "indexedAvailability": {"const": "not-applicable-live-ranking"},
               "characterLibrary": CHARACTER_LIBRARY_PROJECTION},
              ["schemaVersion", "campaignId", "batchId", "payloadHash", "branchId", "epoch", "worldRevision",
               "predecessorBatchId", "readableWatermark", "indexedWatermark", "receiptHash", "vectorMode",
               "indexedAvailability"])
COVERAGE = obj({"status": {"const": "complete"}, "readableWatermark": WATERMARK,
                "indexedWatermark": WATERMARK, "recentDelta": {"type": "array", "items": obj({
                    "branchId": STR, "afterRevision": {**INT, "minimum": -1}, "throughRevision": INT})},
                "readableRanges": {"type": "array", "items": obj({"branchId": STR, "throughRevision": INT})},
                "vectorMode": {"enum": ["none", "live-authorized-ranking"]},
                "indexedAvailability": {"const": "not-applicable-live-ranking"},
                "policy": {"enum": ["complete-authorized-lexical-v1", "complete-authorized-live-ranking-v1"]}})
OPEN_SCOPE_RESULT = obj({"scopeHandle": STR, "bindingHash": HASH, "readableWatermark": WATERMARK,
                         "indexedWatermark": WATERMARK, "profile": STR,
                         "vectorMode": {"enum": ["none", "live-authorized-ranking"]},
                         "indexedAvailability": {"const": "not-applicable-live-ranking"},
                         "actorFirstAppearanceIds": STRINGS},
                        ["scopeHandle", "bindingHash", "readableWatermark", "indexedWatermark", "profile",
                         "vectorMode", "indexedAvailability"])
READ_RESULT = obj({"id": STR, "kind": STR, "text": TEXT, "order": INT,
                   "representation": {"enum": ["accepted", "original"]}, "representationHash": HASH,
                   "sourceSpans": {"type": "array", "items": SPAN}, "readStatus": {"const": "read"},
                   "deliveryStatus": {"const": "not-delivered"}, "readBytes": INT, "readCalls": INT,
                   "cognition": obj({"actorId": STR, "beliefKind": STR, "method": STR, "claimId": STR,
                                      "eventIds": STRINGS, "attitude": {"enum": ["unstated", "disbelieve", "doubtful", "believe"]}},
                                     ["actorId", "beliefKind", "method", "attitude"])},
                  ["id", "kind", "text", "order", "representation", "representationHash", "sourceSpans", "readStatus", "deliveryStatus", "readBytes", "readCalls"])
SEARCH_RESULT = obj({"candidates": {"type": "array", "items": obj({
    "id": STR, "handle": STR, "kind": STR, "representation": {"const": "accepted"},
    "representationHash": HASH, "score": {"type": "number"}, "candidateStatus": {"const": "candidate"},
    "readStatus": {"const": "unread"}, "origin": {"enum": ["readable", "live"]}})},
    "coverage": COVERAGE, "hasMore": {"type": "boolean"}, "trace": {"type": "object"}})
SCHEMA["$defs"].update({"receipt": RECEIPT, "coverage": COVERAGE, "openScopeResult": OPEN_SCOPE_RESULT,
                        "readResult": READ_RESULT, "searchResult": SEARCH_RESULT})
SCHEMA["$defs"]["memoryGraphResult"] = obj({
    "acceptedFloor": INT,
    "activeRecordIds": STRINGS,
    "aliases": {"type": "object", "properties": {}, "additionalProperties": STR},
    "nodes": {"type": "array", "items": obj({
        "eventId": STR, "rootId": STR, "parentEventId": STR,
        "phase": {"enum": ["draft", "active", "resolved"]}, "sourceSceneIds": STRINGS,
    }, ["eventId", "rootId", "phase", "sourceSceneIds"])},
    "archiveMarkers": {"type": "array", "items": obj({
        "eventId": STR, "rootId": STR, "archivedFloor": INT, "expiresAfterFloor": INT,
        "relatedEventIds": STRINGS, "available": {"type": "boolean"},
    })},
}, ["acceptedFloor", "activeRecordIds", "aliases", "nodes", "archiveMarkers"])
SCHEMA["$defs"]["memoryArchiveResult"] = obj({
    "operation": {"enum": ["read", "restore"]},
    "acceptedFloor": INT,
    "events": {"type": "array", "items": obj({
        "eventId": STR, "rootId": STR, "records": {"type": "array", "items": {"$ref": "#/$defs/record"}},
    })},
    "expiredEventIds": STRINGS,
    "missingEventIds": STRINGS,
}, ["operation", "acceptedFloor", "events", "expiredEventIds"])
SCHEMA["$defs"]["warmScopeResult"] = obj({
    "scopeBindingHash": HASH, "navigationSummariesHash": HASH, "candidateCorpusHash": HASH,
    "candidateCount": INT, "cacheHits": INT, "cacheMisses": INT,
    "profile": STR, "elapsedMs": {"type": "number", "minimum": 0},
    "cacheState": obj({"kind": {"const": "process-local-exact-text-vector-cache"},
                        "size": INT, "capacity": INT})})
NAVIGATION_ENTRY = obj({"id": STR, "kind": STR, "handle": STR})
SCHEMA["$defs"]["navigateResult"] = obj({"seed": NAVIGATION_ENTRY,
    "entries": {"type": "array", "items": NAVIGATION_ENTRY},
    "nextPage": {"anyOf": [INT, {"type": "null"}]}, "coverage": COVERAGE})
NAVIGATION_CARD = {"oneOf": [
    obj({"id": STR, "kind": STR, "navigationStatus": {"const": "available"}, "sourceHash": HASH,
         "synopsis": STR, "synopsisHash": HASH, "tag": STR, "storyTime": STR},
        ["id", "kind", "navigationStatus", "sourceHash", "synopsis", "synopsisHash"]),
    obj({"id": STR, "kind": STR, "navigationStatus": {"const": "unavailable"}, "sourceHash": HASH,
         "tag": STR, "storyTime": STR}, ["id", "kind", "navigationStatus", "sourceHash"]),
    obj({"id": STR, "kind": STR, "navigationStatus": {"const": "omitted-budget"}, "sourceHash": HASH}),
]}
SCHEMA["$defs"]["navigationCardsResult"] = obj({
    "cards": {"type": "array", "maxItems": MAX_NAVIGATION_CARDS, "items": NAVIGATION_CARD},
    "contentBytes": INT, "omittedCount": INT, "coverage": COVERAGE})
WIRE_RECORD = {"$ref": "#/$defs/record"}
KNOWLEDGE_ROW = obj({"reference": STR, "id": STR,
    "knowledgeStatus": {"enum": ["acquired", "unknown"]},
    "deliveryStatus": {"enum": ["included", "omitted-budget", "not-applicable"]},
    "record": WIRE_RECORD, "attitude": {"enum": ["unstated", "disbelieve", "doubtful", "believe"]},
    "coverage": {"const": "complete"}}, ["reference", "knowledgeStatus", "deliveryStatus"])
IMPORTANCE_TIER = {"enum": ["core", "major", "recurring", "ambient"]}
IMPORTANCE = obj({"tier": IMPORTANCE_TIER, "policyId": STR,
    "override": {"enum": ["core", "major", "recurring", "ambient", "auto"]},
    "reasons": STRINGS, "evidenceIds": STRINGS, "interactionCount": INT,
    "transitions": {"type": "array", "items": obj({"order": INT, "tier": IMPORTANCE_TIER,
        "reasons": STRINGS, "evidenceIds": STRINGS})}})
DETAIL_COVERAGE = obj({"available": STRINGS, "delivered": STRINGS, "deferred": STRINGS})
RECORD_ARRAY = {"type": "array", "items": WIRE_RECORD}
SCHEMA["$defs"]["characterImportance"] = IMPORTANCE
SCHEMA["$defs"]["characterDetailCoverage"] = DETAIL_COVERAGE
DIRECTORY_ACTOR = {"actorId": STR, "displayName": STR, "aliases": STRINGS,
    "present": {"type": "boolean"}, "channel": {"enum": ["physical", "mental"]}}
SCHEMA["$defs"]["actorDirectoryResult"] = obj({
    "actors": {"type": "array", "items": obj({**DIRECTORY_ACTOR, "importance": {"type": "object"}})},
    "coverage": COVERAGE})
SCHEMA["$defs"]["resolveActorsResult"] = obj({
    "actors": {"type": "array", "items": obj({**DIRECTORY_ACTOR, "localRef": STR, "isNew": {"type": "boolean"},
        "memoryEligible": {"type": "boolean"},
        "memoryDisposition": {"enum": ["legacy-unclassified"]}},
        ["actorId", "displayName", "aliases", "present", "channel", "localRef", "isNew"])},
    "records": RECORD_ARRAY,
    "characterRegistry": {"type": "array", "items": REGISTRY_ENTRY}}, ["actors", "records"])
SCHEMA["$defs"]["actorDossiersResult"] = obj({"actors": {"type": "array", "items": obj({
    "actorId": STR, "profile": RECORD_ARRAY, "state": RECORD_ARRAY, "relationships": RECORD_ARRAY,
    "acquisitions": {"type": "array", "items": obj({"record": WIRE_RECORD,
        "attitude": {"enum": ["unstated", "disbelieve", "doubtful", "believe"]}})},
    "attitudes": RECORD_ARRAY, "dates": RECORD_ARRAY, "backgrounds": RECORD_ARRAY, "exceptions": RECORD_ARRAY,
    "eventReferences": STRINGS, "importance": IMPORTANCE,
    "provenance": {"type": "array", "items": obj({"id": STR, "branchId": STR, "worldRevision": INT})},
    "coverage": DETAIL_COVERAGE})}, "coverage": COVERAGE})
SCHEMA["$defs"]["actorPacksResult"] = obj({"actors": {"type": "array", "items": obj({
    "actorId": STR, "basics": {"type": "array", "items": WIRE_RECORD},
    "knowledge": {"type": "array", "items": KNOWLEDGE_ROW},
    "status": {"enum": ["complete", "partial", "unqueried"]},
    "importance": IMPORTANCE, "detailCoverage": DETAIL_COVERAGE, "eventReferences": STRINGS},
    ["actorId", "basics", "knowledge", "status"])},
    "contentBytes": INT, "coverage": COVERAGE, "knowledgeCoverage": {"enum": ["references-only", "unqueried"]}})
SCHEMA["$defs"]["actorCuesResult"] = obj({"actors": {"type": "array", "items": obj({
    "actorId": STR, "cues": {"type": "array", "items": WIRE_RECORD}, "omittedCount": INT,
    "status": {"enum": ["complete", "partial"]}})}, "coverage": COVERAGE})
SCHEMA["$defs"]["receiptResult"] = {"oneOf": [obj({"status": {"const": "applied"}, "receipt": RECEIPT}),
                                                        obj({"status": {"const": "not-applied"}})]}
SCHEMA["$defs"]["validateBatchResult"] = obj({"valid": {"const": True}, "batchId": STR, "payloadHash": HASH})
SCHEMA["$defs"]["rpcRequest"] = {"oneOf": [obj({"jsonrpc": {"const": "2.0"},
    "id": {"anyOf": [STR, INT]}, "method": {"const": method}, "params": params})
    for method, params in RPC_PARAMS.items()]}


def validate(value, schema, path="value"):
    """Validate precisely the JSON Schema subset emitted above, without imports."""
    if "$ref" in schema:
        return validate(value, SCHEMA["$defs"][schema["$ref"].split("/")[-1]], path)
    for combinator in ("oneOf", "anyOf"):
        if combinator in schema:
            count = 0
            for choice in schema[combinator]:
                try:
                    validate(value, choice, path)
                    count += 1
                except EvidenceError:
                    pass
            if count < 1 or (combinator == "oneOf" and count != 1):
                fail(f"{path} does not match {combinator}")
            return
    if "const" in schema and value != schema["const"]:
        fail(f"{path} has wrong constant")
    if "enum" in schema and value not in schema["enum"]:
        fail(f"{path} has invalid enum")
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            fail(f"{path} must be an object")
        props = schema.get("properties", {})
        if set(schema.get("required", [])) - set(value):
            fail(f"{path} missing required fields")
        if schema.get("additionalProperties") is False and set(value) - set(props):
            fail(f"{path} contains unknown fields")
        for key, item in value.items():
            if key in props:
                validate(item, props[key], f"{path}.{key}")
            elif isinstance(schema.get("additionalProperties"), dict):
                validate(item, schema["additionalProperties"], f"{path}.{key}")
    elif kind == "array":
        if (not isinstance(value, list) or len(value) < schema.get("minItems", 0)
                or len(value) > schema.get("maxItems", 9007199254740991)):
            fail(f"{path} must be an array of required size")
        if schema.get("uniqueItems") and len({canonical_hash(v) for v in value}) != len(value):
            fail(f"{path} contains duplicate items")
        for index, item in enumerate(value):
            validate(item, schema["items"], f"{path}[{index}]")
    elif kind == "string":
        if not isinstance(value, str) or len(value) < schema.get("minLength", 0):
            fail(f"{path} must be a string of required size")
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            fail(f"{path} is not strict UTF-8: {exc}")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            fail(f"{path} has invalid format")
    elif kind == "integer":
        if type(value) is not int or not schema.get("minimum", 0) <= value <= schema.get("maximum", 9007199254740991):
            fail(f"{path} must be a safe integer in range")
    elif kind == "number":
        import math
        if type(value) not in (int, float) or not math.isfinite(value):
            fail(f"{path} must be a finite number")
    elif kind == "boolean" and type(value) is not bool:
        fail(f"{path} must be boolean")
    elif kind == "null" and value is not None:
        fail(f"{path} must be null")


def batch_hash(batch):
    return canonical_hash({key: value for key, value in batch.items() if key != "payloadHash"})


def sealed_batch(**kwargs):
    result = copy.deepcopy({"schemaVersion": VERSION, **kwargs})
    result["payloadHash"] = batch_hash(result)
    validate(result, BATCH, "batch")
    return result
