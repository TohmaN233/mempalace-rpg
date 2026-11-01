"""Offline behavior/negative/recovery evidence for the additive gameplay profile."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError, canonical_hash
from mempalace_rpg.play_memory import PlayMemoryStore
from mempalace_rpg.play_memory_protocol import (MAX_NAVIGATION_SUMMARIES,
                                                MAX_NAVIGATION_SUMMARY_BYTES,
                                                SCHEMA, batch_hash, sealed_batch,
                                                validate)
from mempalace_rpg.retrieval import RankingResult

TOKEN = "test-host-capability"
TEXT = "密谈：钥匙在南井。B向C说：钥匙可能在北边。"


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def span(text=TEXT, part=None):
    part = text if part is None else part
    start = text.encode("utf-8").index(part.encode("utf-8"))
    return {"sourceId": "src", "version": "1", "startByte": start,
            "endByte": start + len(part.encode("utf-8")), "sha256": sha(part)}


def records():
    full, retell = [span()], [span(part="B向C说：钥匙可能在北边。")]
    return [
        {"kind": "source", "id": "source:1", "order": 0, "sourceId": "src", "version": "1", "text": TEXT, "sha256": sha(TEXT)},
        {"kind": "scene", "id": "scene:1", "order": 1, "text": "well scene", "sourceSpans": full},
        {"kind": "event", "id": "secret", "order": 1, "sceneId": "scene:1", "text": "钥匙在南井", "sourceSpans": full},
        {"kind": "event", "id": "retelling", "order": 2, "sceneId": "scene:1", "text": "B claims north", "sourceSpans": retell},
        {"kind": "link", "id": "presence", "order": 1, "actorId": "D", "eventIds": ["secret"], "relation": "present", "sourceSpans": full},
        {"kind": "acquisition", "id": "a:1", "order": 1, "actorId": "A", "text": "钥匙在南井", "eventIds": ["secret"], "claimId": "key-position", "beliefKind": "fact", "method": "witness", "basisIds": ["secret"], "sourceSpans": full},
        {"kind": "acquisition", "id": "c:1", "order": 2, "actorId": "C", "text": "B声称钥匙可能在北边", "eventIds": ["secret", "retelling"], "claimId": "b-north-claim", "beliefKind": "hearsay", "method": "retelling", "basisIds": ["retelling"], "sourceSpans": retell},
        {"kind": "profile", "id": "c:profile", "order": 0, "actorId": "C", "field": "values", "text": "cautious", "knownBy": ["C"], "sourceSpans": full},
        {"kind": "state", "id": "c:state", "order": 1, "actorId": "C", "field": "goal", "text": "find key", "knownBy": ["C"], "sourceSpans": full},
        {"kind": "relationship", "id": "a-to-c", "order": 1, "fromActorId": "A", "toActorId": "C", "text": "A distrusts C", "knownBy": ["A"], "sourceSpans": full},
        {"kind": "background", "id": "public-rule", "order": 0, "text": "festival custom", "appliesTo": ["A", "C", "D"], "sourceSpans": full},
        {"kind": "date", "id": "anniversary", "order": 1, "text": "C remembers anniversary", "calendarId": "host-calendar", "dateRule": {"month": 2, "day": None}, "knownBy": ["C"], "sourceSpans": full},
    ]


def batch(identifier="batch:1", revision=1, changes=None, predecessor=None, **kwargs):
    return sealed_batch(campaignId="camp", batchId=identifier, branchId="main", epoch=1,
                        worldRevision=revision, predecessorBatchId=predecessor,
                        records=records() if changes is None else changes, **kwargs)


def binding(**kwargs):
    return {"campaignId": "camp", "branchId": "main", "epoch": 1, "worldRevision": 1,
            "storyOrder": 10, "checkpointId": "world:1", "materialRevision": "author:1",
            "configHash": "a" * 64, "purpose": "director-plot", "actorIds": ["A", "C", "D"],
            "grants": ["secret", "retelling", "public-rule", "anniversary"],
            "lineage": [{"branchId": "main", "throughRevision": 1}],
            "maxReadBytes": 20000, "maxReadCalls": 100, "requiredBatchId": "batch:1", **kwargs}


def call(store, method, **params):
    if method in ("apply_batch", "validate_batch", "receipt", "open_scope", "warm_scope", "actor_cues", "actor_packs", "navigation_cards", "close_scope"):
        params["hostToken"] = TOKEN
    return store.dispatch(method, params)


@pytest.fixture
def store(tmp_path):
    result = PlayMemoryStore(tmp_path / "memory.sqlite", host_token=TOKEN)
    call(result, "apply_batch", batch=batch())
    yield result
    result.close()


def scope(store, **kwargs):
    return call(store, "open_scope", binding=binding(**kwargs))["scopeHandle"]


def packs(store, handle, actors=None, references=None, budget=20000):
    return call(store, "actor_packs", scopeHandle=handle, actors=actors or ["A", "C", "D"],
                references=["secret"] if references is None else references, budgetBytes=budget)


def test_shared_schema_is_exact_and_fixture_valid():
    path = Path(__file__).resolve().parents[1] / "schemas" / "play-memory-v1.schema.json"
    assert json.loads(path.read_text(encoding="utf-8")) == SCHEMA
    from mempalace_rpg.play_memory_protocol import validate, BATCH, BINDING
    validate(batch(), BATCH)
    validate(binding(), BINDING)
    example = json.loads(path.with_name("play-memory-v1.examples.json").read_text(encoding="utf-8"))
    validate(example["batch"], BATCH)
    validate(example["binding"], BINDING)
    validate(example["receipt"], SCHEMA["$defs"]["receipt"])
    assert batch_hash(example["batch"]) == example["batch"]["payloadHash"]


def test_portable_results_match_canonical_wire_definitions(store):
    from mempalace_rpg.play_memory_protocol import validate
    opened = call(store, "open_scope", binding=binding())
    validate(opened, SCHEMA["$defs"]["openScopeResult"])
    handle = opened["scopeHandle"]
    search = call(store, "search", scopeHandle=handle, query="钥匙", limit=10)
    validate(search, SCHEMA["$defs"]["searchResult"])
    navigated = call(store, "navigate", scopeHandle=handle, seed="secret", relation="source", page=0, limit=10)
    validate(navigated, SCHEMA["$defs"]["navigateResult"])
    read = call(store, "read", scopeHandle=handle, handle=navigated["seed"]["handle"], representation="original")
    validate(read, SCHEMA["$defs"]["readResult"])
    validate(packs(store, handle), SCHEMA["$defs"]["actorPacksResult"])
    validate(call(store, "actor_cues", scopeHandle=handle, actors=["C"]), SCHEMA["$defs"]["actorCuesResult"])
    validate(call(store, "navigation_cards", scopeHandle=handle, references=["secret"], budgetBytes=4096),
             SCHEMA["$defs"]["navigationCardsResult"])


def test_navigation_cards_only_use_scope_authority_and_verified_scene_summaries(store):
    scene = next(record for record in records() if record["id"] == "scene:1")
    summary = "The scene's authorized navigation note."
    item = {"id": "scene:1", "sourceHash": sha(scene["text"]), "summary": summary,
            "summaryHash": sha(summary)}

    with pytest.raises(EvidenceError, match="authorized scope"):
        call(store, "open_scope", binding=binding(grants=["a:1"]), navigationSummaries=[item])

    acquisition_only = call(store, "open_scope", binding=binding(grants=["a:1"]))["scopeHandle"]
    own_card = call(store, "navigation_cards", scopeHandle=acquisition_only,
                    references=["a:1"], budgetBytes=4096)["cards"][0]
    assert own_card["navigationStatus"] == "available"
    assert own_card["synopsis"] == "钥匙在南井"
    assert summary not in own_card["synopsis"]

    authorized = call(store, "open_scope", binding=binding(grants=["a:1", "scene:1"]),
                      navigationSummaries=[item])["scopeHandle"]
    result = call(store, "navigation_cards", scopeHandle=authorized,
                  references=["scene:1", "a:1"], budgetBytes=4096)
    assert result["cards"][0]["synopsis"] == summary
    assert result["cards"][1]["synopsis"] == "钥匙在南井"
    assert result["cards"][1]["synopsis"] != summary

    no_summary = call(store, "open_scope", binding=binding(grants=["scene:1"]))["scopeHandle"]
    unavailable = call(store, "navigation_cards", scopeHandle=no_summary,
                        references=["scene:1"], budgetBytes=4096)["cards"][0]
    assert unavailable["navigationStatus"] == "unavailable"
    assert "synopsis" not in unavailable and "synopsisHash" not in unavailable
    assert scene["text"] not in json.dumps(unavailable, ensure_ascii=False)

    ungranted = scope(store)
    with pytest.raises(EvidenceError, match="outside authorized scope"):
        call(store, "navigation_cards", scopeHandle=ungranted,
             references=["scene:1"], budgetBytes=4096)


def test_navigation_cards_omit_whole_cards_and_fail_when_minimum_markers_do_not_fit(tmp_path):
    result = PlayMemoryStore(tmp_path / "navigation.sqlite", host_token=TOKEN)
    try:
        call(result, "apply_batch", batch=batch())
        long_text = "evidence must be read in full: " + "x" * 10000
        long_hash = sha(long_text)
        long_source_id = "long-source-content"
        long_source = {"kind": "source", "id": "long-source-record", "order": 1,
                       "sourceId": long_source_id, "version": long_hash,
                       "text": long_text, "sha256": long_hash}
        identifiers = [f"long-event-{index}" for index in range(24)]
        extra = [
            {"kind": "event", "id": identifier, "order": index + 2, "sceneId": "scene:1",
             "text": long_text,
             "sourceSpans": [{"sourceId": long_source_id, "version": long_hash,
                              "startByte": 0, "endByte": len(long_text.encode("utf-8")),
                              "sha256": long_hash}]}
            for index, identifier in enumerate(identifiers)
        ]
        call(result, "apply_batch", batch=batch("batch:2", revision=2, changes=[long_source, *extra],
                                                predecessor="batch:1"))
        handle = call(result, "open_scope", binding=binding(
            storyOrder=100,
            worldRevision=2,
            lineage=[{"branchId": "main", "throughRevision": 2}],
            requiredBatchId="batch:2",
            maxReadBytes=100000,
            grants=identifiers,
        ))["scopeHandle"]
        one = call(result, "navigation_cards", scopeHandle=handle,
                   references=[identifiers[0]], budgetBytes=1024)
        card = one["cards"][0]
        assert card["navigationStatus"] == "omitted-budget"
        assert "synopsis" not in card and "synopsisHash" not in card
        assert one["omittedCount"] == 1
        assert one["contentBytes"] <= 1024
        read_handle = call(result, "navigate", scopeHandle=handle, seed=identifiers[0],
                           relation="source", page=0, limit=1)["seed"]["handle"]
        assert call(result, "read", scopeHandle=handle, handle=read_handle,
                    representation="accepted")["text"] == long_text
        assert call(result, "read", scopeHandle=handle, handle=read_handle,
                    representation="original")["text"] == long_text

        with pytest.raises(EvidenceError, match="minimum navigation card markers exceed"):
            call(result, "navigation_cards", scopeHandle=handle,
                 references=identifiers, budgetBytes=512)
        with pytest.raises(EvidenceError, match="duplicate items"):
            call(result, "navigation_cards", scopeHandle=handle,
                 references=[identifiers[0], identifiers[0]], budgetBytes=1024)
    finally:
        result.close()


def test_partial_retelling_and_presence_never_grant_truth(store):
    result = packs(store, scope(store))
    a, c, d = result["actors"]
    assert a["knowledge"][0]["record"]["beliefKind"] == "fact"
    assert c["knowledge"][0]["record"]["text"] == "B声称钥匙可能在北边"
    assert c["knowledge"][0]["attitude"] == "unstated"
    assert c["knowledge"][0]["record"]["sourceSpans"] == [span(part="B向C说：钥匙可能在北边。")]
    assert d["knowledge"][0]["knowledgeStatus"] == "unknown"
    assert all(r["id"] != "a-to-c" for r in c["basics"])
    handle = scope(store, purpose="actor-knowledge", actorIds=["C"], grants=[])
    hits = call(store, "search", scopeHandle=handle, query="钥匙", limit=100)
    assert "secret" not in {r["id"] for r in hits["candidates"]}
    claim = next(r for r in hits["candidates"] if r["id"] == "c:1")
    read = call(store, "read", scopeHandle=handle, handle=claim["handle"], representation="original")
    assert read["text"] == "B向C说：钥匙可能在北边。"
    assert "南井" not in read["text"]


def test_acl_is_applied_before_injected_ranker_and_validates_output(tmp_path):
    class Ranker:
        identity = "deterministic-test-ranker"
        seen = []
        bad = False

        def rank(self, *, query, candidates):
            self.seen = [c.source_event_id for c in candidates]
            self.candidates = list(candidates)
            ids = ["secret"] if self.bad else list(self.seen)
            return RankingResult(ids, {i: 1.0 for i in ids}, {"identity": self.identity})
    ranker = Ranker()
    service = PlayMemoryStore(tmp_path / "m.sqlite", host_token=TOKEN, ranker=ranker)
    try:
        call(service, "apply_batch", batch=batch())
        handle = scope(service, purpose="actor-knowledge", actorIds=["C"], grants=[])
        call(service, "search", scopeHandle=handle, query="钥匙", limit=20)
        assert "c:1" in ranker.seen and "secret" not in ranker.seen and "a:1" not in ranker.seen
        claim = next(c for c in ranker.candidates if c.source_event_id == "c:1")
        assert claim.raw_text == "B向C说：钥匙可能在北边。"
        assert claim.observation == "B声称钥匙可能在北边"
        assert "南井" not in claim.raw_text
        assert service.scopes[handle]["readBytes"] == 0
        ranker.bad = True
        with pytest.raises(EvidenceError, match="unauthorized"):
            call(service, "search", scopeHandle=handle, query="钥匙", limit=20)
    finally:
        service.close()


def test_search_excludes_visible_records_before_ranking_and_limit_without_changing_actor_knowledge(store):
    class Ranker:
        identity = "visible-history-exclusion-test"
        seen = []
        reintroduce_excluded = False

        def rank(self, *, query, candidates):
            self.seen = [candidate.source_event_id for candidate in candidates]
            ids = ["secret"] if self.reintroduce_excluded else list(self.seen)
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {})

    ranker = Ranker()
    store.ranker = ranker
    handle = scope(store)
    result = call(store, "search", scopeHandle=handle, query="key", limit=1, excludeIds=["secret"])
    assert "secret" not in ranker.seen
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["id"] == ranker.seen[0]
    assert result["trace"]["beforeExclusionCount"] == len(ranker.seen) + 1
    assert result["trace"]["excludedCount"] == 1
    assert result["trace"]["excludedDigest"] == canonical_hash(["secret"])
    assert packs(store, handle, ["A", "C"], ["secret"])["actors"][0]["knowledge"][0]["knowledgeStatus"] == "acquired"
    ranker.reintroduce_excluded = True
    with pytest.raises(EvidenceError, match="unauthorized"):
        call(store, "search", scopeHandle=handle, query="key", limit=1, excludeIds=["secret"])
    actor_handle = scope(store, purpose="actor-knowledge", actorIds=["C"], grants=[])
    with pytest.raises(EvidenceError, match="exclusion outside authorized"):
        call(store, "search", scopeHandle=actor_handle, query="key", limit=1, excludeIds=["secret"])
    with pytest.raises(EvidenceError):
        call(store, "search", scopeHandle=handle, query="key", limit=1, excludeIds=["secret", "secret"])


def test_scope_navigation_summaries_only_replace_ranking_observations(store):
    class Ranker:
        identity = "test-vector-ranker"
        encoder = object()

        def __init__(self):
            self.prepared = None
            self.ranked = None

        def prepare(self, *, candidates):
            self.prepared = tuple(candidates)
            unique = len({candidate.observation for candidate in candidates})
            return {"profile": self.identity, "candidateCount": len(candidates),
                    "cacheHits": len(candidates) - unique, "cacheMisses": unique,
                    "cacheSize": unique, "cacheCapacity": 100}

        def rank(self, *, query, candidates):
            self.ranked = tuple(candidates)
            ids = [candidate.source_event_id for candidate in candidates]
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {"profile": self.identity})

    ranker = Ranker()
    store.ranker = ranker
    store.ranker_identity = ranker.identity
    store.vector_mode = "live-authorized-ranking"
    scene = next(record for record in records() if record["id"] == "scene:1")
    summary = "Only the Host-provided navigation observation."
    supplied = {"id": "scene:1", "sourceHash": sha(scene["text"]), "summary": summary,
                "summaryHash": sha(summary)}
    scope_binding = binding(grants=[*binding()["grants"], "scene:1"])
    opened = call(store, "open_scope", binding=scope_binding, navigationSummaries=[supplied])
    handle = opened["scopeHandle"]

    warm = call(store, "warm_scope", scopeHandle=handle)
    validate(warm, SCHEMA["$defs"]["warmScopeResult"])
    assert warm["scopeBindingHash"] == opened["bindingHash"]
    assert warm["navigationSummariesHash"] == canonical_hash(store.scopes[handle]["navigationSummaries"])
    assert warm["candidateCorpusHash"] == canonical_hash([
        {"candidateId": candidate.source_event_id,
         "observationSha256": sha(candidate.observation)}
        for candidate in ranker.prepared])
    assert warm["candidateCount"] == len(ranker.prepared)
    assert warm["profile"] == ranker.identity
    assert warm["cacheState"]["kind"] == "process-local-exact-text-vector-cache"
    prepared = {candidate.source_event_id: candidate for candidate in ranker.prepared}
    assert prepared["scene:1"].observation == summary
    assert prepared["secret"].observation == "钥匙在南井"
    assert prepared["retelling"].observation == "B claims north"
    assert prepared["secret"].raw_text == TEXT
    assert store.scopes[handle]["records"] == store._records("camp", {"main": 1})
    assert all(summary not in row[0] for row in store.db.execute("SELECT body FROM play_records"))

    searched = call(store, "search", scopeHandle=handle, query="north", limit=100)
    ranked = {candidate.source_event_id: candidate for candidate in ranker.ranked}
    assert ranked["secret"].observation == "钥匙在南井"
    scene_hit = next(row for row in searched["candidates"] if row["id"] == "scene:1")
    accepted = call(store, "read", scopeHandle=handle, handle=scene_hit["handle"], representation="accepted")
    original = call(store, "read", scopeHandle=handle, handle=scene_hit["handle"], representation="original")
    assert accepted["text"] == scene["text"]
    assert original["text"] == TEXT
    actor_pack = call(store, "actor_packs", scopeHandle=handle, actors=["C"],
                      references=["public-rule"], budgetBytes=20000)
    assert summary not in json.dumps(actor_pack, ensure_ascii=False)


def test_scene_summaries_policy_limits_search_and_warm_candidates_only(store):
    class Ranker:
        identity = "scene-summary-policy-test-ranker"
        encoder = object()

        def __init__(self):
            self.prepared = None
            self.ranked = None

        def prepare(self, *, candidates):
            self.prepared = tuple(candidates)
            count = len(self.prepared)
            return {"profile": self.identity, "candidateCount": count, "cacheHits": 0,
                    "cacheMisses": count, "cacheSize": count, "cacheCapacity": 100}

        def rank(self, *, query, candidates):
            self.ranked = tuple(candidates)
            ids = [candidate.source_event_id for candidate in self.ranked]
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {"profile": self.identity})

    ranker = Ranker()
    store.ranker = ranker
    store.ranker_identity = ranker.identity
    store.vector_mode = "live-authorized-ranking"
    scene = next(record for record in records() if record["id"] == "scene:1")
    summary = "Only this scene summary participates in retrieval."
    summary_item = {"id": "scene:1", "sourceHash": sha(scene["text"]), "summary": summary,
                    "summaryHash": sha(summary)}
    grants = ["secret", "retelling", "public-rule", "anniversary", "c:1", "scene:1"]
    scope_binding = binding(grants=grants)

    legacy_opened = call(store, "open_scope", binding=scope_binding)
    assert legacy_opened["bindingHash"] == canonical_hash(scope_binding)

    opened = call(store, "open_scope", binding=scope_binding,
                  retrievalPolicy="scene-summaries-v1", navigationSummaries=[summary_item])
    handle = opened["scopeHandle"]
    normalized_binding = {**scope_binding, "retrievalPolicy": "scene-summaries-v1"}
    assert opened["bindingHash"] == canonical_hash(normalized_binding)
    assert store.scopes[handle]["binding"] == normalized_binding
    assert opened["bindingHash"] != legacy_opened["bindingHash"]

    warmed = call(store, "warm_scope", scopeHandle=handle)
    assert warmed["candidateCount"] == 1
    assert [candidate.source_event_id for candidate in ranker.prepared] == ["scene:1"]
    assert ranker.prepared[0].observation == summary

    searched = call(store, "search", scopeHandle=handle, query="north", limit=100)
    assert [item["id"] for item in searched["candidates"]] == ["scene:1"]
    assert [candidate.source_event_id for candidate in ranker.ranked] == ["scene:1"]

    # Search filtering does not remove graph records from the bound scope or
    # change the existing explicit-read and actor-pack authorization paths.
    navigated = call(store, "navigate", scopeHandle=handle, seed="retelling",
                     relation="event", page=0, limit=100)
    assert {item["id"] for item in navigated["entries"]} == {"c:1"}
    event_read = call(store, "read", scopeHandle=handle, handle=navigated["seed"]["handle"],
                      representation="accepted")
    claim_read = call(store, "read", scopeHandle=handle,
                      handle=next(item["handle"] for item in navigated["entries"] if item["id"] == "c:1"),
                      representation="original")
    assert event_read["text"] == "B claims north"
    assert claim_read["text"] == "B向C说：钥匙可能在北边。"
    pack = call(store, "actor_packs", scopeHandle=handle, actors=["C"],
                references=["retelling"], budgetBytes=20000)
    assert pack["actors"][0]["knowledge"][0]["record"]["id"] == "c:1"


@pytest.mark.parametrize("method", ["search", "warm_scope"])
def test_scene_summaries_policy_rejects_missing_candidate_summary_before_ranker(store, method):
    class Ranker:
        identity = "missing-summary-policy-test-ranker"
        encoder = object()

        def __init__(self):
            self.calls = 0

        def prepare(self, *, candidates):
            self.calls += 1
            raise AssertionError("missing summary must fail before prewarm")

        def rank(self, *, query, candidates):
            self.calls += 1
            raise AssertionError("missing summary must fail before ranking")

    ranker = Ranker()
    store.ranker = ranker
    store.ranker_identity = ranker.identity
    store.vector_mode = "live-authorized-ranking"
    handle = call(store, "open_scope", binding=binding(grants=["scene:1"]),
                  retrievalPolicy="scene-summaries-v1")["scopeHandle"]

    with pytest.raises(EvidenceError, match="requires a navigation summary"):
        if method == "warm_scope":
            call(store, method, scopeHandle=handle)
        else:
            call(store, method, scopeHandle=handle, query="north", limit=10)
    assert ranker.calls == 0


def test_scene_summaries_policy_rejects_unknown_and_actor_knowledge_requests(store):
    with pytest.raises(EvidenceError, match=r"retrievalPolicy has invalid enum"):
        call(store, "open_scope", binding=binding(grants=["scene:1"]),
             retrievalPolicy="unknown-policy")

    actor_binding = binding(purpose="actor-knowledge", actorIds=["C"], grants=[])
    with pytest.raises(EvidenceError, match="cannot be used with actor-knowledge"):
        call(store, "open_scope", binding=actor_binding,
             retrievalPolicy="scene-summaries-v1")


def test_stdio_server_forwards_scene_summaries_retrieval_policy(tmp_path):
    from mempalace_rpg.play_memory_server import serve

    class Ranker:
        identity = "stdio-scene-summary-policy-ranker"

        def rank(self, *, query, candidates):
            ids = [candidate.source_event_id for candidate in candidates]
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {"profile": self.identity})

    store = PlayMemoryStore(tmp_path / "stdio-policy.sqlite", host_token=TOKEN, ranker=Ranker())
    call(store, "apply_batch", batch=batch())
    scene = next(record for record in records() if record["id"] == "scene:1")
    summary = "Scene-only server retrieval observation."
    scope_binding = binding(grants=["scene:1", "secret", "retelling"])
    params = {"hostToken": TOKEN, "binding": scope_binding, "retrievalPolicy": "scene-summaries-v1",
              "navigationSummaries": [{"id": "scene:1", "sourceHash": sha(scene["text"]),
                                       "summary": summary, "summaryHash": sha(summary)}]}
    request = {"jsonrpc": "2.0", "id": 7, "method": "open_scope", "params": params}
    output = io.StringIO()
    serve(store, input_stream=io.StringIO(json.dumps(request, ensure_ascii=False) + "\n"),
          output_stream=output, diagnostic_stream=io.StringIO())
    response = json.loads(output.getvalue())
    assert response["id"] == 7 and "error" not in response
    assert response["result"]["bindingHash"] == canonical_hash({
        **scope_binding, "retrievalPolicy": "scene-summaries-v1"})


def test_navigation_summaries_reject_bad_authority_hash_kind_and_bounds(store):
    scene = next(record for record in records() if record["id"] == "scene:1")
    summary = "A valid navigation note."

    def item(identifier="scene:1", *, source_text=None, text=summary):
        source_text = scene["text"] if source_text is None else source_text
        return {"id": identifier, "sourceHash": sha(source_text), "summary": text,
                "summaryHash": sha(text)}

    with pytest.raises(EvidenceError, match="authorized scope"):
        call(store, "open_scope", binding=binding(), navigationSummaries=[item()])
    with pytest.raises(EvidenceError, match="only describe authorized scenes"):
        call(store, "open_scope", binding=binding(), navigationSummaries=[item("secret")])
    with pytest.raises(EvidenceError, match="source hash mismatch"):
        call(store, "open_scope", binding=binding(grants=[*binding()["grants"], "scene:1"]),
             navigationSummaries=[{**item(), "sourceHash": "0" * 64}])
    with pytest.raises(EvidenceError, match="summary hash mismatch"):
        call(store, "open_scope", binding=binding(grants=[*binding()["grants"], "scene:1"]),
             navigationSummaries=[{**item(), "summaryHash": "0" * 64}])
    with pytest.raises(EvidenceError, match="nonblank"):
        call(store, "open_scope", binding=binding(grants=[*binding()["grants"], "scene:1"]),
             navigationSummaries=[item(text="   ")])
    with pytest.raises(EvidenceError, match="unique"):
        call(store, "open_scope", binding=binding(grants=[*binding()["grants"], "scene:1"]),
             navigationSummaries=[item(), item()])
    with pytest.raises(EvidenceError, match=r"params\.navigationSummaries must be an array of required size") as exc:
        call(store, "open_scope", binding=binding(grants=[*binding()["grants"], "scene:1"]),
             navigationSummaries=[item()] * (MAX_NAVIGATION_SUMMARIES + 1))
    assert exc.value.kind == "invalid-params"
    oversized = "x" * (MAX_NAVIGATION_SUMMARY_BYTES + 1)
    with pytest.raises(EvidenceError, match="byte limit"):
        call(store, "open_scope", binding=binding(grants=[*binding()["grants"], "scene:1"]),
             navigationSummaries=[item(text=oversized)])


@pytest.mark.parametrize("method", ["warm_scope", "search"])
def test_long_rank_or_warm_rejects_scope_invalidated_during_inference(store, method):
    class InvalidatingRanker:
        identity = "test-invalidation-ranker"
        encoder = object()

        def prepare(self, *, candidates):
            scope(store, epoch=2)
            return {"profile": self.identity, "candidateCount": len(candidates), "cacheHits": 0,
                    "cacheMisses": len(candidates), "cacheSize": len(candidates), "cacheCapacity": 100}

        def rank(self, *, query, candidates):
            scope(store, epoch=2)
            ids = [candidate.source_event_id for candidate in candidates]
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {"profile": self.identity})

    store.ranker = InvalidatingRanker()
    store.ranker_identity = store.ranker.identity
    store.vector_mode = "live-authorized-ranking"
    handle = scope(store)
    with pytest.raises(EvidenceError, match="epoch"):
        if method == "warm_scope":
            call(store, method, scopeHandle=handle)
        else:
            call(store, method, scopeHandle=handle, query="north", limit=10)


def test_navigation_summary_cannot_cross_fork_cutoff(store):
    future_text = "A future scene beyond the retained fork cutoff."
    call(store, "apply_batch", batch=batch("batch:2", 2, [
        {"kind": "scene", "id": "future-scene", "order": 3, "text": future_text,
         "sourceSpans": [span()]}
    ], "batch:1"))
    child = sealed_batch(campaignId="camp", batchId="child:0", branchId="child", epoch=2, worldRevision=0,
                         predecessorBatchId="batch:1", parent={"branchId": "main", "batchId": "batch:1", "throughRevision": 1}, records=[])
    call(store, "apply_batch", batch=child)
    child_binding = binding(branchId="child", epoch=2, worldRevision=0, requiredBatchId="child:0",
                            lineage=[{"branchId": "child", "throughRevision": 0}, {"branchId": "main", "throughRevision": 1}])
    summary = "Must never be included by the retained parent cutoff."
    with pytest.raises(EvidenceError, match="outside the authorized scope"):
        call(store, "open_scope", binding=child_binding, navigationSummaries=[
            {"id": "future-scene", "sourceHash": sha(future_text), "summary": summary,
             "summaryHash": sha(summary)}])


def test_navigation_does_not_expand_actor_authority(store):
    handle = scope(store, purpose="actor-knowledge", actorIds=["C"], grants=[])
    hit = next(r for r in call(store, "search", scopeHandle=handle, query="key", limit=100)["candidates"] if r["id"] == "c:1")
    result = call(store, "navigate", scopeHandle=handle, seed=hit["handle"], relation="event", page=0, limit=100)
    assert "secret" not in {r["id"] for r in result["entries"]}
    with pytest.raises(EvidenceError):
        call(store, "navigate", scopeHandle=handle, seed="secret", relation="event", page=0, limit=100)
    with pytest.raises(EvidenceError):
        call(store, "read", scopeHandle=handle, handle="secret", representation="original")


def test_batch_replay_conflict_and_reopen_receipt(tmp_path):
    path = tmp_path / "m.sqlite"
    first = PlayMemoryStore(path, host_token=TOKEN)
    accepted = batch()
    receipt = call(first, "apply_batch", batch=accepted)
    assert call(first, "apply_batch", batch=accepted) == receipt
    changed = copy.deepcopy(accepted)
    changed["records"][2]["text"] = "changed"
    changed["payloadHash"] = batch_hash(changed)
    with pytest.raises(EvidenceError, match="identity reused"):
        call(first, "apply_batch", batch=changed)
    first.close()
    second = PlayMemoryStore(path, host_token=TOKEN)
    try:
        assert call(second, "receipt", campaignId="camp", batchId="batch:1")["receipt"] == receipt
        assert call(second, "receipt", campaignId="camp", batchId="absent") == {"status": "not-applied"}
        assert call(second, "apply_batch", batch=accepted) == receipt
    finally:
        second.close()


@pytest.mark.parametrize("damage", ["hash", "split", "overrun", "missing", "future", "attitude-owner"])
def test_invalid_source_or_references_abort_entire_batch(tmp_path, damage):
    service = PlayMemoryStore(tmp_path / "m.sqlite", host_token=TOKEN)
    changes = records()
    if damage == "hash":
        changes[0]["sha256"] = "f" * 64
    elif damage == "split":
        changes[2]["sourceSpans"][0]["startByte"] = 1
    elif damage == "overrun":
        changes[2]["sourceSpans"][0]["endByte"] = 99999
    elif damage == "missing":
        changes[6]["eventIds"] = ["absent"]
    elif damage == "future":
        changes[0]["order"] = 99
    else:
        changes.append({"kind": "attitude", "id": "bad-owner", "order": 3, "actorId": "D", "acquisitionId": "c:1", "attitude": "believe", "basisIds": ["retelling"], "sourceSpans": [span()]})
    try:
        with pytest.raises(EvidenceError):
            call(service, "apply_batch", batch=batch(changes=changes))
        assert service.db.execute("SELECT COUNT(*) FROM play_records").fetchone()[0] == 0
        assert call(service, "receipt", campaignId="camp", batchId="batch:1")["status"] == "not-applied"
    finally:
        service.close()


def test_same_effective_order_conflict_is_atomic(store):
    changes = [{"kind": "state", "id": "c:state:2", "order": 1, "actorId": "C", "field": "goal", "text": "conflicting", "knownBy": ["C"], "sourceSpans": [span()]}]
    with pytest.raises(EvidenceError, match="ambiguous"):
        call(store, "apply_batch", batch=batch("batch:2", 2, changes, "batch:1"))


def test_history_stance_background_exception_and_directed_relation(store):
    old = scope(store)
    changes = [
        {"kind": "attitude", "id": "c:stance", "order": 3, "actorId": "C", "acquisitionId": "c:1", "attitude": "disbelieve", "basisIds": ["retelling"], "sourceSpans": [span()]},
        {"kind": "state", "id": "c:state:2", "order": 3, "actorId": "C", "field": "goal", "text": "question B", "knownBy": ["C"], "sourceSpans": [span()]},
        {"kind": "exception", "id": "c:exception", "order": 3, "actorId": "C", "backgroundId": "public-rule", "status": "unknown", "basisIds": ["retelling"], "sourceSpans": [span()]},
        {"kind": "relationship", "id": "c-to-a", "order": 3, "fromActorId": "C", "toActorId": "A", "text": "C respects A", "knownBy": ["C"], "sourceSpans": [span()]},
    ]
    call(store, "apply_batch", batch=batch("batch:2", 2, changes, "batch:1"))
    current = scope(store, worldRevision=2, lineage=[{"branchId": "main", "throughRevision": 2}], requiredBatchId="batch:2")
    result = packs(store, current, references=["secret", "public-rule"])["actors"][1]
    assert result["knowledge"][0]["attitude"] == "disbelieve"
    assert result["knowledge"][1]["knowledgeStatus"] == "unknown"
    assert {r["text"] for r in result["basics"]} == {"cautious", "question B", "C respects A"}
    assert packs(store, old)["actors"][1]["knowledge"][0]["attitude"] == "unstated"
    call(store, "apply_batch", batch=batch("batch:3", 3, [], "batch:2"))
    later = scope(store, worldRevision=3, lineage=[{"branchId": "main", "throughRevision": 3}], requiredBatchId="batch:3")
    assert packs(store, later)["actors"][1]["knowledge"][0]["attitude"] == "disbelieve"


def test_hearing_same_explicit_claim_again_does_not_reset_attitude(store):
    changes = [
        {"kind": "attitude", "id": "c:stance", "order": 3, "actorId": "C", "acquisitionId": "c:1", "attitude": "disbelieve", "basisIds": ["retelling"], "sourceSpans": [span()]},
        {"kind": "acquisition", "id": "c:2", "order": 4, "actorId": "C", "text": "B repeats north", "eventIds": ["secret"], "claimId": "b-north-claim", "beliefKind": "hearsay", "method": "retelling", "basisIds": ["retelling"], "sourceSpans": [span(part="B向C说：钥匙可能在北边。")]}]
    call(store, "apply_batch", batch=batch("batch:2", 2, changes, "batch:1"))
    handle = scope(store, worldRevision=2, lineage=[{"branchId": "main", "throughRevision": 2}], requiredBatchId="batch:2")
    knowledge = packs(store, handle, actors=["C"])["actors"][0]["knowledge"]
    assert {row["record"]["id"] for row in knowledge} == {"c:1", "c:2"}
    assert {row["attitude"] for row in knowledge} == {"disbelieve"}


def test_shared_background_default_applies_to_later_actors_with_persistent_exception(store):
    changes = [
        {"kind": "background", "id": "common-news", "order": 3, "text": "The bridge is closed", "appliesTo": ["*"], "sourceSpans": [span()]},
        {"kind": "exception", "id": "later-actor-exception", "order": 3, "actorId": "later-actor", "backgroundId": "common-news", "status": "unknown", "basisIds": ["retelling"], "sourceSpans": [span()]}]
    call(store, "apply_batch", batch=batch("batch:2", 2, changes, "batch:1"))
    handle = scope(store, worldRevision=2, lineage=[{"branchId": "main", "throughRevision": 2}], requiredBatchId="batch:2",
                   actorIds=["newly-registered", "later-actor"], grants=["common-news"])
    result = packs(store, handle, actors=["newly-registered", "later-actor"], references=["common-news"])["actors"]
    assert result[0]["knowledge"][0]["knowledgeStatus"] == "acquired"
    assert result[1]["knowledge"][0]["knowledgeStatus"] == "unknown"
    with pytest.raises(EvidenceError, match="unavailable record"):
        scope(store, grants=["*"])


def test_unknown_unqueried_and_budget_omission_are_distinct(store):
    handle = scope(store)
    assert packs(store, handle, references=[])["actors"][0]["status"] == "unqueried"
    baseline = packs(store, handle, actors=["A", "D"])["actors"][0]["basics"]
    minimum = sum(len(json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) for r in baseline)
    result = packs(store, handle, actors=["A", "D"], budget=minimum)["actors"]
    assert result[0]["knowledge"][0]["knowledgeStatus"] == "acquired"
    assert result[0]["knowledge"][0]["deliveryStatus"] == "omitted-budget"
    assert result[1]["knowledge"][0]["knowledgeStatus"] == "unknown"
    with pytest.raises(EvidenceError, match="minimum actor"):
        packs(store, handle, actors=["C"], budget=0)
    with pytest.raises(EvidenceError, match="actors outside"):
        packs(store, handle, actors=["private-other"])


def test_known_basic_reference_is_not_mislabeled_as_unknown(store):
    handle = scope(store, grants=["c:profile", "c:state", "a-to-c"])
    result = packs(store, handle, actors=["C"], references=["c:profile", "c:state", "a-to-c"])["actors"][0]
    assert [row["knowledgeStatus"] for row in result["knowledge"]] == ["acquired", "acquired", "unknown"]


def test_branch_fork_uses_retained_parent_cutoff_not_current_parent_head(store):
    call(store, "apply_batch", batch=batch("batch:2", 2, [{"kind": "event", "id": "future-secret", "order": 3, "text": "future", "sourceSpans": [span()]}], "batch:1"))
    child = sealed_batch(campaignId="camp", batchId="child:0", branchId="child", epoch=2, worldRevision=0,
                         predecessorBatchId="batch:1", parent={"branchId": "main", "batchId": "batch:1", "throughRevision": 1}, records=[])
    receipt = call(store, "apply_batch", batch=child)
    assert receipt["readableWatermark"] == {"child": 0, "main": 1}
    child_binding = dict(branchId="child", epoch=2, worldRevision=0, requiredBatchId="child:0",
                         lineage=[{"branchId": "child", "throughRevision": 0}, {"branchId": "main", "throughRevision": 1}])
    handle = scope(store, **child_binding)
    ids = {r["id"] for r in call(store, "search", scopeHandle=handle, query="future", limit=100)["candidates"]}
    assert "future-secret" not in ids
    with pytest.raises(EvidenceError, match="lineage"):
        scope(store, **{**child_binding, "lineage": [{"branchId": "child", "throughRevision": 0}, {"branchId": "main", "throughRevision": 2}]})
    with pytest.raises(EvidenceError, match="unavailable"):
        scope(store, **{**child_binding, "grants": ["future-secret"]})


def test_scope_epoch_cancel_handle_binding_and_budgets(store):
    handle = scope(store, maxReadBytes=1)
    hit = call(store, "search", scopeHandle=handle, query="key", limit=10)["candidates"][0]
    with pytest.raises(EvidenceError, match="budget"):
        call(store, "read", scopeHandle=handle, handle=hit["handle"], representation="original")
    assert store.scopes[handle]["readBytes"] == 0
    other = scope(store)
    with pytest.raises(EvidenceError, match="handle"):
        call(store, "read", scopeHandle=other, handle=hit["handle"], representation="accepted")
    call(store, "close_scope", scopeHandle=handle)
    with pytest.raises(EvidenceError, match="scope"):
        call(store, "search", scopeHandle=handle, query="x", limit=1)
    scope(store, epoch=2)
    with pytest.raises(EvidenceError, match="epoch"):
        call(store, "search", scopeHandle=other, query="x", limit=1)


def test_sibling_branch_epochs_are_independent(store):
    main = scope(store)
    scope(store, epoch=5)
    child = sealed_batch(campaignId="camp", batchId="child:0", branchId="child", epoch=0, worldRevision=0,
                         predecessorBatchId="batch:1", parent={"branchId": "main", "batchId": "batch:1", "throughRevision": 1}, records=[])
    call(store, "apply_batch", batch=child)
    child_handle = scope(store, branchId="child", epoch=0, worldRevision=0, requiredBatchId="child:0",
                         lineage=[{"branchId": "child", "throughRevision": 0}, {"branchId": "main", "throughRevision": 1}])
    assert call(store, "search", scopeHandle=child_handle, query="钥匙", limit=10)["candidates"]
    with pytest.raises(EvidenceError, match="epoch"):
        call(store, "search", scopeHandle=main, query="钥匙", limit=10)


def test_readable_records_are_always_searched_without_fabricated_index_readiness(store):
    handle = scope(store)
    before = call(store, "search", scopeHandle=handle, query="钥匙", limit=100)
    assert before["coverage"]["status"] == "complete"
    assert before["coverage"]["indexedWatermark"] == {"main": -1}
    assert {r["origin"] for r in before["candidates"]} == {"readable"}
    assert before["coverage"]["vectorMode"] == "none"
    assert before["coverage"]["indexedAvailability"] == "not-applicable-live-ranking"
    assert before["coverage"]["readableRanges"] == [{"branchId": "main", "throughRevision": 1}]
    with pytest.raises(EvidenceError, match="persistent vector watermark unavailable"):
        store.acknowledge_indexed(campaign_id="camp", branch_id="main", batch_id="batch:1", profile_identity=store.ranker_identity)
    after = call(store, "search", scopeHandle=handle, query="钥匙", limit=100)
    assert [r["id"] for r in before["candidates"]] == [r["id"] for r in after["candidates"]]
    assert {r["origin"] for r in after["candidates"]} == {"readable"}
    assert after["coverage"]["recentDelta"] == []


def test_predecessor_gaps_stale_and_missing_required_watermark_are_errors(store):
    with pytest.raises(EvidenceError, match="contiguous"):
        call(store, "apply_batch", batch=batch("batch:3", 3, [], "batch:1"))
    with pytest.raises(EvidenceError, match="required batch"):
        scope(store, requiredBatchId="absent")
    with pytest.raises(EvidenceError, match="revision unavailable"):
        scope(store, worldRevision=2, lineage=[{"branchId": "main", "throughRevision": 2}])
    with pytest.raises(EvidenceError, match="Host"):
        store.dispatch("apply_batch", {"hostToken": "model", "batch": batch("batch:2", 2, [], "batch:1")})


def test_world_transaction_revision_interval_has_no_holes_or_intermediate_scopes(store):
    accepted = batch("multi-event", 3, [], "batch:1", previousWorldRevision=1)
    assert call(store, "apply_batch", batch=accepted)["worldRevision"] == 3
    with pytest.raises(EvidenceError, match="transaction boundary"):
        scope(store, worldRevision=2, lineage=[{"branchId": "main", "throughRevision": 2}], requiredBatchId="batch:1")
    with pytest.raises(EvidenceError, match="contiguous"):
        call(store, "apply_batch", batch=batch("hole", 6, [], "multi-event", previousWorldRevision=4))
    assert call(store, "apply_batch", batch=batch("next", 6, [], "multi-event", previousWorldRevision=3))["worldRevision"] == 6


def test_child_initial_marker_can_advance_to_global_world_sequence_without_fake_holes(store):
    child = sealed_batch(campaignId="camp", batchId="child:0", branchId="child", epoch=0, worldRevision=0,
                         predecessorBatchId="batch:1", parent={"branchId": "main", "batchId": "batch:1", "throughRevision": 1}, records=[])
    call(store, "apply_batch", batch=child)
    successor = sealed_batch(campaignId="camp", batchId="child:7", branchId="child", epoch=0, worldRevision=7,
                             previousWorldRevision=0, predecessorBatchId="child:0", records=[])
    result = call(store, "apply_batch", batch=successor)
    assert result["readableWatermark"] == {"child": 7, "main": 1}
    with pytest.raises(EvidenceError, match="transaction boundary"):
        scope(store, branchId="child", epoch=0, worldRevision=6, requiredBatchId="child:0",
              lineage=[{"branchId": "child", "throughRevision": 6}, {"branchId": "main", "throughRevision": 1}])


def test_precommit_failure_rolls_back_and_postcommit_failure_recovers(tmp_path):
    path = tmp_path / "m.sqlite"
    def fault(stage, accepted):
        raise RuntimeError("injected loss before commit")
    service = PlayMemoryStore(path, host_token=TOKEN, fault_hook=fault)
    with pytest.raises(RuntimeError):
        call(service, "apply_batch", batch=batch())
    assert service.db.execute("SELECT COUNT(*) FROM play_records").fetchone()[0] == 0
    service.fault_hook = lambda stage, accepted: (_ for _ in ()).throw(RuntimeError("lost response")) if stage == "after-commit" else None
    with pytest.raises(RuntimeError, match="lost response"):
        call(service, "apply_batch", batch=batch())
    service.close()
    recovered = PlayMemoryStore(path, host_token=TOKEN)
    try:
        assert call(recovered, "receipt", campaignId="camp", batchId="batch:1")["status"] == "applied"
        assert call(recovered, "apply_batch", batch=batch())["readableWatermark"] == {"main": 1}
    finally:
        recovered.close()


def test_process_death_after_durable_commit_recovers_same_receipt(tmp_path):
    path, packet = tmp_path / "crash.sqlite", tmp_path / "batch.json"
    packet.write_text(json.dumps(batch(), ensure_ascii=False), encoding="utf-8")
    program = """import json,os,sys
from mempalace_rpg.play_memory import PlayMemoryStore
def fault(stage,batch):
 if stage=='after-commit': os._exit(73)
s=PlayMemoryStore(sys.argv[1],host_token='test-host-capability',fault_hook=fault)
s.dispatch('apply_batch',{'hostToken':'test-host-capability','batch':json.load(open(sys.argv[2],encoding='utf-8'))})
"""
    process = subprocess.run([sys.executable, "-c", program, str(path), str(packet)], capture_output=True, timeout=20)
    assert process.returncode == 73, process.stderr
    recovered = PlayMemoryStore(path, host_token=TOKEN)
    try:
        assert call(recovered, "apply_batch", batch=batch()) == call(recovered, "receipt", campaignId="camp", batchId="batch:1")["receipt"]
    finally:
        recovered.close()


def test_validate_batch_is_readonly_without_hooks_ranking_epoch_or_scope_side_effects(store):
    import sqlite3
    handle = scope(store)
    accepted = batch("batch:2", 2, [], "batch:1")
    accepted["epoch"] = 9
    accepted["payloadHash"] = batch_hash(accepted)
    before = store.db.total_changes
    store.fault_hook = lambda *args: pytest.fail("validation invoked apply fault hook")
    store.ranker.rank = lambda **kwargs: pytest.fail("validation invoked a ranker")
    statements = []
    def read_only(action, first, second, database, trigger):
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            statements.append((action, first))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    store.db.set_authorizer(read_only)
    try:
        result = call(store, "validate_batch", batch=accepted)
    finally:
        store.db.set_authorizer(None)
    assert result == {"valid": True, "batchId": accepted["batchId"], "payloadHash": accepted["payloadHash"]}
    from mempalace_rpg.play_memory_protocol import validate
    validate(result, SCHEMA["$defs"]["validateBatchResult"])
    assert statements == []
    assert store.db.total_changes == before
    assert call(store, "receipt", campaignId="camp", batchId="batch:2")["status"] == "not-applied"
    assert store._scope(handle)["binding"]["epoch"] == 1
    assert not store.db.in_transaction


@pytest.mark.parametrize("damage", ["empty-exception-basis", "conflicting-state", "wrong-attitude-owner", "bad-payload-hash", "future-source", "revision-hole", "stale-epoch", "bad-span"])
def test_validate_and_apply_share_every_acceptance_rule_without_poisoning_state(store, damage):
    changes = []
    if damage == "empty-exception-basis":
        changes = [{"kind": "exception", "id": "bad", "order": 3, "actorId": "C", "backgroundId": "public-rule", "status": "unknown", "basisIds": [], "sourceSpans": [span()]}]
    elif damage == "conflicting-state":
        changes = [{"kind": "state", "id": "bad", "order": 1, "actorId": "C", "field": "goal", "text": "conflict", "knownBy": ["C"], "sourceSpans": [span()]}]
    elif damage == "wrong-attitude-owner":
        changes = [{"kind": "attitude", "id": "bad", "order": 3, "actorId": "D", "acquisitionId": "c:1", "attitude": "believe", "basisIds": ["retelling"], "sourceSpans": [span()]}]
    elif damage == "future-source":
        changes = [{"kind": "source", "id": "future", "order": 99, "sourceId": "future", "version": "1", "text": TEXT, "sha256": sha(TEXT)}, {"kind": "event", "id": "bad", "order": 3, "text": "future source", "sourceSpans": [{**span(), "sourceId": "future"}]}]
    elif damage == "bad-span":
        changes = [{"kind": "event", "id": "bad", "order": 3, "text": "bad span", "sourceSpans": [{**span(), "startByte": 1}]}]
    accepted = batch("candidate", 3 if damage == "revision-hole" else 2, changes, "batch:1")
    if damage == "stale-epoch":
        accepted["epoch"] = 0
        accepted["payloadHash"] = batch_hash(accepted)
    if damage == "bad-payload-hash":
        accepted["payloadHash"] = "0" * 64
    baseline = list(store.db.iterdump())
    with pytest.raises(EvidenceError) as preflight:
        call(store, "validate_batch", batch=accepted)
    assert list(store.db.iterdump()) == baseline
    with pytest.raises(EvidenceError) as applied:
        call(store, "apply_batch", batch=accepted)
    assert (preflight.value.code, preflight.value.kind, str(preflight.value)) == (applied.value.code, applied.value.kind, str(applied.value))
    assert list(store.db.iterdump()) == baseline
    assert not store.db.in_transaction


def test_apply_revalidates_preflight_when_another_batch_advances_the_head(store):
    accepted = batch("candidate", 2, [], "batch:1")
    assert call(store, "validate_batch", batch=accepted)["valid"] is True
    call(store, "apply_batch", batch=batch("other", 2, [], "batch:1"))
    with pytest.raises(EvidenceError, match="predecessor"):
        call(store, "apply_batch", batch=accepted)
    assert call(store, "receipt", campaignId="camp", batchId="candidate")["status"] == "not-applied"
    replay = batch("other", 2, [], "batch:1")
    assert call(store, "validate_batch", batch=replay)["valid"] is True


def test_corrupted_persistent_body_fails_on_reopen_scope(store):
    store.db.execute("UPDATE play_records SET body=replace(body,'钥匙','伪造') WHERE id='secret'")
    with pytest.raises(EvidenceError, match="persisted record hash"):
        scope(store)


def test_stdio_real_process_apply_scope_read_and_negative_contract(tmp_path):
    env = {**os.environ, "PLAY_MEMORY_HOST_TOKEN": TOKEN, "PYTHONIOENCODING": "utf-8"}
    process = subprocess.Popen([sys.executable, "-m", "mempalace_rpg.play_memory_server", "--db", str(tmp_path / "rpc.sqlite")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", env=env)
    def rpc(method, params):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, ensure_ascii=False) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())
    try:
        assert rpc("capabilities", {})["result"]["protocol"] == "play-memory-v1"
        assert "error" in rpc("validate_batch", {"hostToken": "model", "batch": batch()})
        assert rpc("validate_batch", {"hostToken": TOKEN, "batch": batch()})["result"] == {"valid": True, "batchId": "batch:1", "payloadHash": batch()["payloadHash"]}
        assert rpc("receipt", {"hostToken": TOKEN, "campaignId": "camp", "batchId": "batch:1"})["result"]["status"] == "not-applied"
        assert "error" in rpc("apply_batch", {"hostToken": "model", "batch": batch()})
        assert rpc("apply_batch", {"hostToken": TOKEN, "batch": batch()})["result"]["worldRevision"] == 1
        opened = rpc("open_scope", {"hostToken": TOKEN, "binding": binding()})["result"]
        handle = opened["scopeHandle"]
        hit = rpc("search", {"scopeHandle": handle, "query": "钥匙", "limit": 3})["result"]["candidates"][0]
        read = rpc("read", {"scopeHandle": handle, "handle": hit["handle"], "representation": "accepted"})["result"]
        assert read["text"] and read["deliveryStatus"] == "not-delivered"
        assert "error" in rpc("search", {"scopeHandle": handle, "query": "x", "limit": 1, "actorId": "other"})
        assert rpc("close_scope", {"hostToken": TOKEN, "scopeHandle": handle})["result"]["closed"]
        assert "error" in rpc("read", {"scopeHandle": handle, "handle": hit["handle"], "representation": "accepted"})
    finally:
        process.stdin.close()
        process.wait(timeout=10)
        stderr = process.stderr.read()
        assert process.returncode == 0, stderr
        assert TOKEN not in stderr


def test_stdio_server_startup_timing_stays_on_stderr_without_request_content(tmp_path):
    explicit_token = "explicit-diagnostic-test-token"
    environment_sentinel = "environment-token-must-not-be-used-or-logged"
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "receipt",
        "params": {
            "hostToken": explicit_token,
            "campaignId": "PRIVATE-REQUEST-CONTENT",
            "batchId": "missing-batch",
        },
    }
    env = {**os.environ, "PLAY_MEMORY_HOST_TOKEN": environment_sentinel, "PYTHONUTF8": "1"}
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "mempalace_rpg.play_memory_server",
            "--db",
            str(tmp_path / "timing-rpc.sqlite"),
            "--host-token",
            explicit_token,
            "--profile",
            "mechanical-bm25-v1",
        ],
        input=json.dumps(request, ensure_ascii=False) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    output_lines = completed.stdout.splitlines()
    assert len(output_lines) == 1
    response = json.loads(output_lines[0])
    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"status": "not-applied"}}

    prefix = "play-memory-server-timing "
    timing_records = [json.loads(line[len(prefix):]) for line in completed.stderr.splitlines() if line.startswith(prefix)]
    events = [record["event"] for record in timing_records]
    assert events == [
        "core-import-start",
        "core-import-complete",
        "store-init-start",
        "store-init-complete",
        "first-handler-enter",
        "first-handler-complete",
    ]
    assert all(type(record["pid"]) is int for record in timing_records)
    assert all(type(record["elapsedMs"]) in (int, float) and record["elapsedMs"] >= 0 for record in timing_records)
    allowed_fields = {"event", "pid", "elapsedMs", "durationMs", "method", "requestBytes", "responseBytes", "outcome"}
    assert all(set(record) <= allowed_fields for record in timing_records)

    enter, complete = timing_records[-2:]
    assert enter["method"] == complete["method"] == "receipt"
    assert type(enter["requestBytes"]) is int and enter["requestBytes"] > 0
    assert complete["requestBytes"] == enter["requestBytes"]
    assert type(complete["responseBytes"]) is int and complete["responseBytes"] > 0
    assert type(complete["durationMs"]) in (int, float) and complete["durationMs"] >= 0
    assert complete["outcome"] == "success"
    assert explicit_token not in completed.stdout + completed.stderr
    assert environment_sentinel not in completed.stdout + completed.stderr
    assert "PRIVATE-REQUEST-CONTENT" not in completed.stdout + completed.stderr
