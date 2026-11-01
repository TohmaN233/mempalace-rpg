"""Branch-scoped projection, archive access, and derived-cache expiry tests."""
import copy

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from mempalace_rpg.play_memory import PlayMemoryStore
from mempalace_rpg.play_memory_lifecycle import archive_specs
from mempalace_rpg.play_memory_protocol import SCHEMA, sealed_batch, validate
from mempalace_rpg.retrieval import RankingResult
from test_play_memory import TOKEN, TEXT, batch, binding, call, sha, span


def _metadata(phase="active", **values):
    return {"memoryLifecycle": {"schema": "episodic-memory-v1", "phase": phase, **values}}


def _seed(store):
    call(store, "apply_batch", batch=batch())
    additions = [
        {"kind": "scene", "id": "floor:2", "order": 3, "text": "second floor",
         "sourceSpans": [span()]},
        {"kind": "event", "id": "draft:investigation", "order": 3,
         "sceneId": "floor:2", "text": "draft-only investigation detail",
         "sourceSpans": [span()], "metadata": _metadata("draft")},
        {"kind": "event", "id": "defense:v1", "order": 3, "sceneId": "scene:1",
         "text": "A held the east gate.", "sourceSpans": [span()],
         "metadata": _metadata()},
        {"kind": "event", "id": "public:defense:v1", "order": 3,
         "sceneId": "scene:1", "text": "Public account: A defended the east gate.",
         "sourceSpans": [span()], "metadata": _metadata()},
        {"kind": "event", "id": "committee-secret:1", "order": 4, "sceneId": "scene:1",
         "text": "committee only: the gate was sabotaged", "sourceSpans": [span()],
         "metadata": _metadata()},
        {"kind": "event", "id": "committee-secret:2", "order": 4, "sceneId": "floor:2",
         "text": "committee only: the second signal was false", "sourceSpans": [span()],
         "metadata": _metadata()},
        {"kind": "acquisition", "id": "c:defense", "order": 4, "actorId": "C",
         "text": "B声称钥匙可能在北边", "eventIds": ["defense:v1"],
         "beliefKind": "hearsay", "method": "retelling", "basisIds": ["retelling"],
         "sourceSpans": [span(part="B向C说：钥匙可能在北边。")]},
        {"kind": "acquisition", "id": "c:draft", "order": 4, "actorId": "C",
         "text": "draft-only acquisition detail", "eventIds": ["draft:investigation"],
         "beliefKind": "inference", "method": "inference", "basisIds": ["draft:investigation"],
         "sourceSpans": [span()]},
        {"kind": "acquisition", "id": "d:secret1", "order": 4, "actorId": "D",
         "text": "D knows the sabotage detail", "eventIds": ["committee-secret:1"],
         "beliefKind": "fact", "method": "witness", "basisIds": ["secret"],
         "sourceSpans": [span()]},
        {"kind": "acquisition", "id": "d:committee-facet", "order": 4,
         "actorId": "D", "text": "Committee-only route detail remains separate.",
         "eventIds": ["public:defense:v1"], "beliefKind": "fact", "method": "witness",
         "basisIds": ["public:defense:v1"], "sourceSpans": [span()]},
        {"kind": "acquisition", "id": "d:secret2", "order": 4, "actorId": "D",
         "text": "D knows the false signal detail", "eventIds": ["committee-secret:2"],
         "beliefKind": "fact", "method": "witness", "basisIds": ["secret"],
         "sourceSpans": [span()]},
    ]
    call(store, "apply_batch", batch=batch("batch:2", 2, additions, "batch:1"))
    continuation = [
        {"kind": "event", "id": "public:defense:v2", "order": 5,
         "sceneId": "floor:2", "text": "Public account: A held the gate and the raiders retreated.",
         "sourceSpans": [span()],
         "metadata": {"continuationOf": "public:defense:v1", **_metadata()}},
        {"kind": "event", "id": "defense:v2", "order": 5, "sceneId": "floor:2",
         "text": "A held the east gate; the public outcome was a retreat.",
         "sourceSpans": [span()],
         "metadata": {"continuationOf": "defense:v1",
                      **_metadata(parentEventId="defense:v1",
                                  archiveEventIds=["committee-secret:1", "committee-secret:2"])}},
        {"kind": "acquisition", "id": "c:defense:v2", "order": 5, "actorId": "C",
         "text": "B repeated the north-side claim", "eventIds": ["defense:v2"],
         "beliefKind": "hearsay", "method": "retelling", "basisIds": ["retelling"],
         "sourceSpans": [span(part="B向C说：钥匙可能在北边。")],
         "metadata": {"memorySupersedes": ["c:defense"]}},
    ]
    call(store, "apply_batch", batch=batch("batch:3", 3, continuation, "batch:2"))


def _scope(store, revision=3, batch_id=None, *, branch="main", epoch=1, story_order=1000,
           grants=(), actors=("A", "C", "D"), purpose="director-plot", lineage=None,
           navigation_summaries=(), max_read_bytes=20000, max_read_calls=100):
    batch_id = batch_id or f"batch:{revision}"
    lineage = lineage or [{"branchId": branch, "throughRevision": revision}]
    params = {"hostToken": TOKEN, "binding": binding(
        branchId=branch, epoch=epoch, worldRevision=revision, storyOrder=story_order,
        checkpointId=f"world:{branch}:{revision}", materialRevision=f"author:{branch}:{revision}",
        purpose=purpose, actorIds=list(actors), grants=list(grants),
        lineage=lineage, requiredBatchId=batch_id, maxReadBytes=max_read_bytes,
        maxReadCalls=max_read_calls),
        "navigationSummaries": list(navigation_summaries)}
    return store.dispatch("open_scope", params)["scopeHandle"]


def _host_call(store, method, **params):
    return store.dispatch(method, {"hostToken": TOKEN, **params})


def _graph(store, handle):
    result = _host_call(store, "memory_graph", scopeHandle=handle)
    validate(result, SCHEMA["$defs"]["memoryGraphResult"])
    return result


def _archive(store, handle, event_ids, operation="read"):
    result = _host_call(store, "memory_archive", scopeHandle=handle,
                        eventIds=event_ids, operation=operation)
    validate(result, SCHEMA["$defs"]["memoryArchiveResult"])
    return result


def _add_scenes(store, revision, predecessor, first_number, count):
    changes = [
        {"kind": "scene", "id": f"floor:{number}", "order": number + 2,
         "text": f"floor summary {number}", "sourceSpans": [span()]}
        for number in range(first_number, first_number + count)
    ]
    call(store, "apply_batch", batch=batch(f"batch:{revision}", revision, changes, predecessor))


@pytest.fixture
def store(tmp_path):
    value = PlayMemoryStore(tmp_path / "lifecycle.sqlite", host_token=TOKEN)
    _seed(value)
    yield value
    value.close()


def test_graph_merges_cross_floor_event_and_archive_read_respects_actor_facets(store):
    handle = _scope(store, grants=["defense:v1", "defense:v2", "public:defense:v1",
        "public:defense:v2", "committee-secret:1",
        "committee-secret:2", "draft:investigation", "c:draft", "c:defense",
        "c:defense:v2", "d:secret1", "d:secret2", "d:committee-facet"])
    graph = _graph(store, handle)

    assert graph["aliases"] == {"defense:v1": "defense:v2",
                                 "public:defense:v1": "public:defense:v2"}
    defense = next(row for row in graph["nodes"] if row["eventId"] == "defense:v2")
    assert defense["rootId"] == "defense:v1"
    assert defense["parentEventId"] == "defense:v1"
    assert defense["phase"] == "active"
    assert defense["sourceSceneIds"] == ["floor:2", "scene:1"]
    assert {"defense:v2", "c:defense:v2"} <= set(graph["activeRecordIds"])
    assert {"public:defense:v2", "d:committee-facet"} <= set(graph["activeRecordIds"])
    assert {"scene:1", "floor:2"} <= set(graph["activeRecordIds"])
    draft = next(row for row in graph["nodes"] if row["eventId"] == "draft:investigation")
    assert draft["phase"] == "draft"
    assert not ({"draft:investigation", "c:draft"} & set(graph["activeRecordIds"]))
    assert not ({"defense:v1", "c:defense", "committee-secret:1", "committee-secret:2",
                 "d:secret1", "d:secret2"} & set(graph["activeRecordIds"]))
    assert {row["eventId"] for row in graph["archiveMarkers"]} >= {
        "committee-secret:1", "committee-secret:2", "defense:v2"}

    class AllRanker:
        identity = "lifecycle-all-candidates"

        def rank(self, *, query, candidates):
            ids = [row.source_event_id for row in candidates]
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {"profile": self.identity})

    store.ranker = AllRanker()
    store.ranker_identity = store.ranker.identity
    hits = store.dispatch("search", {"scopeHandle": handle, "query": "committee defense", "limit": 100})
    hit_ids = {row["id"] for row in hits["candidates"]}
    assert "defense:v2" in hit_ids and "c:defense:v2" in hit_ids
    assert not ({"defense:v1", "c:defense", "committee-secret:1", "committee-secret:2",
                 "d:secret1", "d:secret2", "draft:investigation", "c:draft",
                 "public:defense:v1"} & hit_ids)
    navigated = store.dispatch("navigate", {"scopeHandle": handle, "seed": "defense:v2",
        "relation": "event", "page": 0, "limit": 100})
    assert {row["id"] for row in navigated["entries"]} == {"c:defense:v2"}
    public_navigated = store.dispatch("navigate", {"scopeHandle": handle,
        "seed": "public:defense:v2", "relation": "event", "page": 0, "limit": 100})
    assert {row["id"] for row in public_navigated["entries"]} == {"d:committee-facet"}

    director_read = _archive(store, handle, ["committee-secret:1"])
    rows = director_read["events"][0]["records"]
    assert {row["id"] for row in rows} == {"committee-secret:1", "d:secret1"}
    assert any("sabotaged" in row.get("text", "") for row in rows)
    superseded = _archive(store, handle, ["defense:v1"])
    assert [row["id"] for row in superseded["events"][0]["records"]] == ["c:defense"]

    actor_c = _scope(store, grants=[], actors=("C",), purpose="actor-knowledge")
    c_dossier = _host_call(store, "actor_dossiers", scopeHandle=actor_c,
                           actors=["C"])["actors"][0]
    assert [row["record"]["id"] for row in c_dossier["acquisitions"]] == ["c:1", "c:defense:v2"]
    assert "committee only" not in str(c_dossier) and "draft-only" not in str(c_dossier)
    c_pack = _host_call(store, "actor_packs", scopeHandle=actor_c, actors=["C"],
                        references=["c:defense:v2"], budgetBytes=20000)
    assert "committee only" not in str(c_pack)
    c_public = _host_call(store, "actor_packs", scopeHandle=actor_c, actors=["C"],
        references=["public:defense:v2"], budgetBytes=20000)
    assert c_public["actors"][0]["knowledge"][0]["knowledgeStatus"] == "unknown"
    c_public_detail = _host_call(store, "actor_packs", scopeHandle=actor_c, actors=["C"],
        references=["public:defense:v2"], budgetBytes=20000,
        detailPolicy="character-importance-v1")
    assert c_public_detail["actors"][0]["knowledge"][0]["knowledgeStatus"] == "unknown"
    c_superseded = _archive(store, actor_c, ["defense:v1"])
    assert [row["id"] for row in c_superseded["events"][0]["records"]] == ["c:defense"]
    assert "A held the east gate" not in str(c_superseded)
    c_read = _archive(store, actor_c, ["committee-secret:1"])
    assert c_read["events"] == []
    assert c_read["missingEventIds"] == ["committee-secret:1"]
    actor_d = _scope(store, grants=[], actors=("D",), purpose="actor-knowledge")
    d_public = _host_call(store, "actor_packs", scopeHandle=actor_d, actors=["D"],
        references=["public:defense:v2"], budgetBytes=20000)
    assert d_public["actors"][0]["knowledge"][0]["record"]["id"] == "d:committee-facet"
    assert d_public["actors"][0]["knowledge"][0]["record"]["eventIds"] == ["public:defense:v1"]
    d_public_detail = _host_call(store, "actor_packs", scopeHandle=actor_d, actors=["D"],
        references=["public:defense:v2"], budgetBytes=20000,
        detailPolicy="character-importance-v1")
    assert d_public_detail["actors"][0]["knowledge"][0]["record"]["id"] == "d:committee-facet"
    d_dossier = _host_call(store, "actor_dossiers", scopeHandle=actor_d, actors=["D"])["actors"][0]
    assert "public:defense:v2" in d_dossier["eventReferences"]
    assert "public:defense:v1" not in d_dossier["eventReferences"]
    # Only the navigational reference is current; the authorized source row is immutable.
    assert d_public_detail["actors"][0]["knowledge"][0]["record"]["eventIds"] == ["public:defense:v1"]
    restricted = _scope(store, grants=["c:defense:v2"])
    with pytest.raises(EvidenceError, match="outside authorized recall scope"):
        _host_call(store, "actor_packs", scopeHandle=restricted, actors=["D"],
            references=["public:defense:v2"], budgetBytes=20000)
    actor_d = _scope(store, grants=[], actors=("D",), purpose="actor-knowledge")
    d_read = _archive(store, actor_d, ["committee-secret:1"])
    assert [row["id"] for row in d_read["events"][0]["records"]] == ["d:secret1"]
    assert "committee only: the gate was sabotaged" not in str(d_read)


def test_same_batch_archive_uses_visible_continuation_family_and_hides_representative(tmp_path):
    store = PlayMemoryStore(tmp_path / "same-batch-archive.sqlite", host_token=TOKEN)
    try:
        first_records = [
            {"kind": "source", "id": "source:1", "order": 0, "sourceId": "src",
             "version": "1", "text": TEXT, "sha256": sha(TEXT)},
            {"kind": "scene", "id": "scene:1", "order": 1, "text": "floor one",
             "sourceSpans": [span()]},
            {"kind": "event", "id": "secret:legacy", "order": 1,
             "sceneId": "scene:1", "text": "legacy secret event detail",
             "sourceSpans": [span()]},
            {"kind": "acquisition", "id": "c:secret", "order": 1,
             "actorId": "C", "text": "C knows a committee-only detail",
             "eventIds": ["secret:legacy"], "beliefKind": "fact", "method": "witness",
             "basisIds": ["secret:legacy"], "sourceSpans": [span()]},
        ]
        call(store, "apply_batch", batch=batch("batch:1", 1, first_records))
        same_batch = [
            {"kind": "event", "id": "secret:resolved", "order": 2,
             "sceneId": "scene:1", "text": "resolved secret family summary",
             "sourceSpans": [span()],
             "metadata": {"continuationOf": "secret:legacy", **_metadata("resolved")}},
            {"kind": "event", "id": "archive:trigger", "order": 2,
             "sceneId": "scene:1", "text": "the committee closed its review",
             "sourceSpans": [span()],
             "metadata": _metadata(archiveEventIds=["secret:legacy"])},
        ]
        prebatch_plan = archive_specs(first_records, same_batch)
        secret_plan = next(spec for spec in prebatch_plan
                           if spec["rootId"] == "secret:legacy")
        assert secret_plan["relatedEventIds"] == ["secret:legacy", "secret:resolved"]
        call(store, "apply_batch", batch=batch("batch:2", 2, same_batch, "batch:1"))

        director = _scope(store, revision=2, batch_id="batch:2", story_order=10,
            grants=["secret:legacy", "secret:resolved", "archive:trigger", "c:secret"])
        graph = _graph(store, director)
        assert graph["aliases"] == {"secret:legacy": "secret:resolved"}
        assert {row["eventId"] for row in graph["nodes"]} == {"archive:trigger"}
        marker = graph["archiveMarkers"][0]
        assert marker["eventId"] == "secret:resolved"
        assert marker["relatedEventIds"] == ["secret:legacy", "secret:resolved"]
        assert not ({"secret:resolved", "c:secret"} & set(graph["activeRecordIds"]))

        class AllRanker:
            identity = "same-batch-archive-all-candidates"

            def rank(self, *, query, candidates):
                ids = [row.source_event_id for row in candidates]
                return RankingResult(ids, {identifier: 1.0 for identifier in ids},
                                     {"profile": self.identity})

        store.ranker = AllRanker()
        store.ranker_identity = store.ranker.identity
        hits = store.dispatch("search", {"scopeHandle": director, "query": "secret", "limit": 100})
        assert "secret:resolved" not in {row["id"] for row in hits["candidates"]}

        actor = _scope(store, revision=2, batch_id="batch:2", actors=("C",),
                       purpose="actor-knowledge")
        dossier = _host_call(store, "actor_dossiers", scopeHandle=actor,
                             actors=["C"])["actors"][0]
        assert dossier["acquisitions"] == []
        pack = _host_call(store, "actor_packs", scopeHandle=actor, actors=["C"],
                          references=["secret:resolved"], budgetBytes=20000)
        assert pack["actors"][0]["knowledge"][0]["knowledgeStatus"] == "unknown"

        archived = _archive(store, director, ["secret:resolved"])
        assert {row["id"] for row in archived["events"][0]["records"]} == {
            "secret:legacy", "secret:resolved", "c:secret"}
        assert "secret:resolved" not in set(_graph(store, director)["activeRecordIds"])
    finally:
        store.close()


def test_legacy_continuations_stay_append_only_until_a_policy_node_opts_in(tmp_path):
    store = PlayMemoryStore(tmp_path / "legacy-continuations.sqlite", host_token=TOKEN)
    try:
        first_records = [
            {"kind": "source", "id": "source:1", "order": 0, "sourceId": "src",
             "version": "1", "text": TEXT, "sha256": sha(TEXT)},
            {"kind": "scene", "id": "scene:1", "order": 1, "text": "floor one",
             "sourceSpans": [span()]},
            {"kind": "event", "id": "legacy:v1", "order": 1, "sceneId": "scene:1",
             "text": "legacy delta one", "sourceSpans": [span()]},
            {"kind": "event", "id": "legacy:v2", "order": 2, "sceneId": "scene:1",
             "text": "legacy delta two", "sourceSpans": [span()],
             "metadata": {"continuationOf": "legacy:v1"}},
            {"kind": "event", "id": "legacy:orphan", "order": 2,
             "text": "legacy opaque metadata remains accepted", "sourceSpans": [span()],
             "metadata": {"continuationOf": 42}},
            {"kind": "event", "id": "legacy:missing-target", "order": 2,
             "text": "legacy unresolved pointer remains accepted", "sourceSpans": [span()],
             "metadata": {"continuationOf": "missing:legacy"}},
            {"kind": "acquisition", "id": "c:legacy", "order": 2, "actorId": "C",
             "text": "C's facet refers to the first delta", "eventIds": ["legacy:v1"],
             "beliefKind": "fact", "method": "witness", "basisIds": ["legacy:v1"],
             "sourceSpans": [span()]},
        ]
        call(store, "apply_batch", batch=batch("batch:1", 1, first_records))

        legacy_scope = _scope(store, revision=1, batch_id="batch:1", actors=("C",),
                              purpose="actor-knowledge")
        before = _graph(store, legacy_scope)
        assert before["aliases"] == {}
        assert {"legacy:v1", "legacy:v2", "legacy:orphan", "legacy:missing-target",
                "c:legacy"} <= set(before["activeRecordIds"])
        assert {row["eventId"] for row in before["nodes"]} == {
            "legacy:v1", "legacy:v2", "legacy:orphan", "legacy:missing-target"}
        old_pack = _host_call(store, "actor_packs", scopeHandle=legacy_scope, actors=["C"],
            references=["legacy:v2"], budgetBytes=20000)
        assert old_pack["actors"][0]["knowledge"][0]["knowledgeStatus"] == "unknown"

        opt_in = [{"kind": "event", "id": "legacy:v3", "order": 3,
            "sceneId": "scene:1", "text": "cumulative migrated event",
            "sourceSpans": [span()],
            "metadata": {"continuationOf": "legacy:v2", **_metadata("active")}}]
        call(store, "apply_batch", batch=batch("batch:2", 2, opt_in, "batch:1"))
        migrated = _scope(store, revision=2, batch_id="batch:2", actors=("C",),
                          purpose="actor-knowledge")
        after = _graph(store, migrated)
        assert after["aliases"] == {"legacy:v1": "legacy:v3", "legacy:v2": "legacy:v3"}
        assert {"legacy:v3", "legacy:orphan", "legacy:missing-target",
                "c:legacy"} <= set(after["activeRecordIds"])
        assert not ({"legacy:v1", "legacy:v2"} & set(after["activeRecordIds"]))
        new_pack = _host_call(store, "actor_packs", scopeHandle=migrated, actors=["C"],
            references=["legacy:v3"], budgetBytes=20000)
        knowledge = new_pack["actors"][0]["knowledge"][0]
        assert knowledge["knowledgeStatus"] == "acquired"
        assert knowledge["record"]["id"] == "c:legacy"
        assert knowledge["record"]["eventIds"] == ["legacy:v1"]
    finally:
        store.close()


def test_legacy_continuation_forks_migrate_to_latest_v1_with_all_sources_and_actor_acl(tmp_path):
    store = PlayMemoryStore(tmp_path / "legacy-fork-migration.sqlite", host_token=TOKEN)
    try:
        first_records = [
            {"kind": "source", "id": "source:1", "order": 0, "sourceId": "src",
             "version": "1", "text": TEXT, "sha256": sha(TEXT)},
        ]
        for index, scene_id in enumerate(("F22", "F23", "F25", "F26", "F28"), start=1):
            first_records.append({"kind": "scene", "id": f"scene:{scene_id}",
                "order": index * 2 - 1, "text": f"floor {scene_id}",
                "sourceSpans": [span()]})
        legacy_events = [
            ("F22", "F22", 2, None),
            ("F23", "F23", 4, "F22"),
            ("F25", "F25", 6, "F22"),
            ("F26", "F26", 8, "F25"),
            ("F28", "F28", 10, "F25"),
        ]
        for event_id, scene_id, order, parent in legacy_events:
            metadata = {"continuationOf": parent} if parent else {}
            first_records.append({"kind": "event", "id": event_id, "order": order,
                "sceneId": f"scene:{scene_id}", "text": f"legacy delta {event_id}",
                "sourceSpans": [span()], "metadata": metadata})
        first_records.extend([
            {"kind": "acquisition", "id": "C:facet", "order": 4, "actorId": "C",
             "text": "C knows the public account", "eventIds": ["F23"],
             "beliefKind": "fact", "method": "witness", "basisIds": ["F23"],
             "sourceSpans": [span(part="B向C说：钥匙可能在北边。")]},
            {"kind": "acquisition", "id": "D:secret", "order": 8, "actorId": "D",
             "text": "D alone knows the committee detail", "eventIds": ["F26"],
             "beliefKind": "fact", "method": "witness", "basisIds": ["F26"],
             "sourceSpans": [span(part="密谈：钥匙在南井。")]},
        ])
        call(store, "apply_batch", batch=batch("batch:1", 1, first_records))

        migrated = [{"kind": "event", "id": "episodic:migration", "order": 12,
            "sceneId": "scene:F28", "text": "cumulative migrated account",
            "sourceSpans": [span()],
            "metadata": {"continuationOf": "F28", **_metadata("active")}}]
        call(store, "apply_batch", batch=batch("batch:2", 2, migrated, "batch:1"))

        director = _scope(store, revision=2, batch_id="batch:2", story_order=1000,
            grants=["F22", "F23", "F25", "F26", "F28", "episodic:migration"],
            actors=("C", "D"))
        graph = _graph(store, director)
        assert graph["aliases"] == {
            "F22": "episodic:migration",
            "F23": "episodic:migration",
            "F25": "episodic:migration",
            "F26": "episodic:migration",
            "F28": "episodic:migration",
        }
        assert graph["nodes"] == [{
            "eventId": "episodic:migration", "rootId": "F22", "phase": "active",
            "sourceSceneIds": ["scene:F22", "scene:F23", "scene:F25", "scene:F26", "scene:F28"],
        }]
        assert {"episodic:migration", "C:facet", "D:secret"} <= set(graph["activeRecordIds"])

        c_scope = _scope(store, revision=2, batch_id="batch:2", story_order=1000,
            actors=("C",), purpose="actor-knowledge")
        c_pack = _host_call(store, "actor_packs", scopeHandle=c_scope, actors=["C"],
            references=["episodic:migration"], budgetBytes=20000)
        c_knowledge = c_pack["actors"][0]["knowledge"]
        assert [row["record"]["id"] for row in c_knowledge if row.get("record")] == ["C:facet"]
        assert "committee detail" not in str(c_pack)

        d_scope = _scope(store, revision=2, batch_id="batch:2", story_order=1000,
            actors=("D",), purpose="actor-knowledge")
        d_pack = _host_call(store, "actor_packs", scopeHandle=d_scope, actors=["D"],
            references=["episodic:migration"], budgetBytes=20000)
        d_knowledge = d_pack["actors"][0]["knowledge"]
        assert [row["record"]["id"] for row in d_knowledge if row.get("record")] == ["D:secret"]
        assert "public account" not in str(d_pack)
    finally:
        store.close()


def test_multiple_same_order_v1_terminal_updates_remain_ambiguous():
    from mempalace_rpg.play_memory_lifecycle import event_families

    records = [
        {"kind": "event", "id": "root", "order": 1, "text": "root", "metadata": {}},
        {"kind": "event", "id": "v1:left", "order": 2, "text": "left",
         "metadata": {"continuationOf": "root", **_metadata("active")}},
        {"kind": "event", "id": "v1:right", "order": 2, "text": "right",
         "metadata": {"continuationOf": "root", **_metadata("active")}},
    ]
    with pytest.raises(EvidenceError, match="ambiguous current version"):
        event_families(records, managed_event_ids={"v1:left"})


def test_archive_cache_expires_at_100_floors_and_explicit_read_renews_only_that_family(store):
    grants = ["committee-secret:1", "committee-secret:2", "d:secret1", "d:secret2"]
    at_archive = _scope(store, grants=grants)
    before = _graph(store, at_archive)
    by_root = {row["rootId"]: row for row in before["archiveMarkers"]}
    assert before["acceptedFloor"] == 1
    assert by_root["committee-secret:1"]["expiresAfterFloor"] == 101
    assert by_root["committee-secret:2"]["expiresAfterFloor"] == 101

    _add_scenes(store, 4, "batch:3", 3, 99)
    at_floor_100 = _scope(store, revision=4, batch_id="batch:4", grants=grants)
    assert _graph(store, at_floor_100)["acceptedFloor"] == 100
    renewed = _archive(store, at_floor_100, ["committee-secret:1"])
    assert {row["id"] for row in renewed["events"][0]["records"]} == {
        "committee-secret:1", "d:secret1"}
    renewed_marker = next(row for row in _graph(store, at_floor_100)["archiveMarkers"]
                          if row["rootId"] == "committee-secret:1")
    assert renewed_marker["expiresAfterFloor"] == 200

    _add_scenes(store, 5, "batch:4", 102, 1)
    at_floor_101 = _scope(store, revision=5, batch_id="batch:5", grants=grants)
    current = {row["rootId"]: row for row in _graph(store, at_floor_101)["archiveMarkers"]}
    assert current["committee-secret:1"]["available"] is True
    assert current["committee-secret:2"]["available"] is False
    expired = _archive(store, at_floor_101, ["committee-secret:2"])
    assert expired["expiredEventIds"] == ["committee-secret:2"]
    assert expired["events"] == []
    assert store.db.execute("SELECT COUNT(*) FROM play_lifecycle_archive_cache "
                            "WHERE event_id='committee-secret:2'").fetchone()[0] == 0
    original_body = store.db.execute("SELECT body,body_hash FROM play_records "
                                     "WHERE campaign='camp' AND id='committee-secret:2'").fetchone()
    assert original_body is not None
    assert "the second signal was false" in original_body["body"]

    _add_scenes(store, 6, "batch:5", 103, 98)
    at_floor_199 = _scope(store, revision=6, batch_id="batch:6", grants=grants)
    assert _graph(store, at_floor_199)["acceptedFloor"] == 199
    marker_199 = next(row for row in _graph(store, at_floor_199)["archiveMarkers"]
                      if row["rootId"] == "committee-secret:1")
    assert marker_199["available"] is True
    _add_scenes(store, 7, "batch:6", 201, 1)
    at_floor_200 = _scope(store, revision=7, batch_id="batch:7", grants=grants)
    marker_200 = next(row for row in _graph(store, at_floor_200)["archiveMarkers"]
                      if row["rootId"] == "committee-secret:1")
    assert marker_200["available"] is False
    assert _archive(store, at_floor_200, ["committee-secret:1"])["expiredEventIds"] == ["committee-secret:1"]
    after_body = store.db.execute("SELECT body,body_hash FROM play_records "
                                  "WHERE campaign='camp' AND id='committee-secret:2'").fetchone()
    assert tuple(after_body) == tuple(original_body)
    assert store.db.execute("SELECT COUNT(*) FROM play_records WHERE campaign='camp' AND kind='scene'").fetchone()[0] == 201
    source_row = store.db.execute("SELECT body FROM play_records WHERE campaign='camp' AND id='source:1'").fetchone()
    scene_row = store.db.execute("SELECT body FROM play_records WHERE campaign='camp' AND id='scene:1'").fetchone()
    assert TEXT in source_row["body"] and "well scene" in scene_row["body"]

    summary = "The original floor summary remains available."
    scene_scope = _scope(store, revision=7, batch_id="batch:7", grants=["scene:1"],
        navigation_summaries=[{"id": "scene:1", "sourceHash": sha("well scene"),
            "summary": summary, "summaryHash": sha(summary)}])
    card = _host_call(store, "navigation_cards", scopeHandle=scene_scope,
                      references=["scene:1"], budgetBytes=4096)["cards"][0]
    assert card["synopsis"] == summary
    nav = store.dispatch("navigate", {"scopeHandle": scene_scope, "seed": "scene:1",
        "relation": "source", "page": 0, "limit": 10})
    original = store.dispatch("read", {"scopeHandle": scene_scope,
        "handle": nav["seed"]["handle"], "representation": "original"})
    assert original["text"] == TEXT


def test_authored_source_maintenance_retires_only_target_family_without_adding_a_floor(store):
    before_graph_scope = _scope(store, actors=("C", "D"))
    before_graph = _graph(store, before_graph_scope)
    original_rows = {
        identifier: tuple(store.db.execute(
            "SELECT body,body_hash FROM play_records WHERE campaign='camp' AND id=?", (identifier,)
        ).fetchone())
        for identifier in ("source:1", "scene:1", "floor:2")
    }
    additions = [
        {"kind": "event", "id": "gesture:detail", "order": 6, "sceneId": "floor:2",
         "text": "D briefly learned the private hand signal", "sourceSpans": [span()],
         "metadata": {}},
        {"kind": "acquisition", "id": "D:gesture", "order": 6, "actorId": "D",
         "text": "D remembers the private hand signal", "eventIds": ["gesture:detail"],
         "beliefKind": "fact", "method": "witness", "basisIds": ["gesture:detail"],
         "sourceSpans": [span(part="密谈：钥匙在南井。")]},
        {"kind": "acquisition", "id": "C:coarse", "order": 6, "actorId": "C",
         "text": "C knows the public defense outcome", "eventIds": ["defense:v2"],
         "beliefKind": "fact", "method": "witness", "basisIds": ["defense:v2"],
         "sourceSpans": [span(part="B向C说：钥匙可能在北边。")]},
    ]
    call(store, "apply_batch", batch=batch("batch:4", 4, additions, "batch:3"))
    control = {
        "schema": "episodic-maintenance-v1",
        "archives": [{"eventId": "gesture:detail", "relatedEventIds": ["defense:v2"]}],
    }
    operation_source = {"kind": "source", "id": "maintenance:gesture-control", "order": 7,
        "sourceId": "maintenance:empty", "version": "1", "text": "", "sha256": sha(""),
        "metadata": {"authored": True, "memoryMaintenance": control}}
    call(store, "apply_batch", batch=batch("batch:5", 5, [operation_source], "batch:4"))

    director = _scope(store, revision=5, batch_id="batch:5", story_order=1000,
        grants=["gesture:detail", "D:gesture", "C:coarse", "defense:v2",
                "maintenance:gesture-control"], actors=("C", "D"))
    graph = _graph(store, director)
    assert graph["acceptedFloor"] == before_graph["acceptedFloor"] == 1
    marker = next(row for row in graph["archiveMarkers"] if row["eventId"] == "gesture:detail")
    assert marker["expiresAfterFloor"] == graph["acceptedFloor"] + 100
    assert marker["relatedEventIds"] == ["defense:v2", "gesture:detail"]
    assert "maintenance:gesture-control" not in marker["relatedEventIds"]
    assert {"defense:v2", "C:coarse"} <= set(graph["activeRecordIds"])
    assert not ({"gesture:detail", "D:gesture"} & set(graph["activeRecordIds"]))
    assert "defense:v2" in {row["eventId"] for row in graph["nodes"]}

    class AllRanker:
        identity = "maintenance-control-all-candidates"

        def rank(self, *, query, candidates):
            ids = [row.source_event_id for row in candidates]
            return RankingResult(ids, {identifier: 1.0 for identifier in ids}, {"profile": self.identity})

    store.ranker = AllRanker()
    store.ranker_identity = store.ranker.identity
    hits = store.dispatch("search", {"scopeHandle": director, "query": "gesture", "limit": 100})
    hit_ids = {row["id"] for row in hits["candidates"]}
    assert "maintenance:gesture-control" not in hit_ids
    assert not ({"gesture:detail", "D:gesture"} & hit_ids)
    assert {"defense:v2", "C:coarse"} <= hit_ids

    archived = _archive(store, director, ["gesture:detail"])
    archived_ids = {row["id"] for row in archived["events"][0]["records"]}
    assert archived_ids == {"gesture:detail", "D:gesture"}
    assert not ({"defense:v2", "C:coarse", "maintenance:gesture-control"} & archived_ids)
    related_only = _archive(store, director, ["defense:v2"])
    assert related_only["events"] == []
    assert related_only["missingEventIds"] == ["defense:v2"]

    c_scope = _scope(store, revision=5, batch_id="batch:5", actors=("C",), purpose="actor-knowledge")
    c_pack = _host_call(store, "actor_packs", scopeHandle=c_scope, actors=["C"],
        references=["defense:v2"], budgetBytes=20000)
    c_record_ids = {row["record"]["id"] for row in c_pack["actors"][0]["knowledge"] if row.get("record")}
    assert {"C:coarse", "c:defense:v2"} <= c_record_ids
    assert "D:gesture" not in c_record_ids
    d_scope = _scope(store, revision=5, batch_id="batch:5", actors=("D",), purpose="actor-knowledge")
    d_pack = _host_call(store, "actor_packs", scopeHandle=d_scope, actors=["D"],
        references=["gesture:detail"], budgetBytes=20000)
    assert all(row.get("record", {}).get("id") != "D:gesture" for row in d_pack["actors"][0]["knowledge"])

    after_rows = {
        identifier: tuple(store.db.execute(
            "SELECT body,body_hash FROM play_records WHERE campaign='camp' AND id=?", (identifier,)
        ).fetchone())
        for identifier in original_rows
    }
    assert after_rows == original_rows


@pytest.mark.parametrize("damage", ["unauthored-source", "missing-target", "future-target"])
def test_memory_maintenance_control_rejects_unowned_or_nonvisible_targets(store, damage):
    control = {"schema": "episodic-maintenance-v1", "archives": [{"eventId": "defense:v2"}]}
    source = {"kind": "source", "id": "maintenance:invalid", "order": 6,
        "sourceId": "maintenance-invalid", "version": "1", "text": "", "sha256": sha("")}
    if damage == "unauthored-source":
        source["metadata"] = {"authored": False, "memoryMaintenance": control}
        changes = [source]
    elif damage == "missing-target":
        source["metadata"] = {"authored": True,
            "memoryMaintenance": {"schema": "episodic-maintenance-v1",
                "archives": [{"eventId": "not-an-event"}]}}
        changes = [source]
    else:
        source["metadata"] = {"authored": True,
            "memoryMaintenance": {"schema": "episodic-maintenance-v1",
                "archives": [{"eventId": "future:event"}]}}
        changes = [source, {"kind": "event", "id": "future:event", "order": 7,
            "sceneId": "floor:2", "text": "later event", "sourceSpans": [span()], "metadata": {}}]
    candidate = batch("maintenance-invalid", 4, changes, "batch:3")
    with pytest.raises(EvidenceError):
        call(store, "validate_batch", batch=candidate)


def test_future_parent_read_does_not_cross_fork_cutoff_or_rewrite_open_scope(store):
    _add_scenes(store, 4, "batch:3", 3, 9)
    grants = ["committee-secret:1", "d:secret1"]
    old_parent = _scope(store, revision=4, batch_id="batch:4", grants=grants)
    assert _graph(store, old_parent)["acceptedFloor"] == 10
    child = sealed_batch(campaignId="camp", batchId="child:0", branchId="child", epoch=2,
        worldRevision=0, predecessorBatchId="batch:4",
        parent={"branchId": "main", "batchId": "batch:4", "throughRevision": 4}, records=[])
    call(store, "apply_batch", batch=child)

    parent_read = _archive(store, old_parent, ["committee-secret:1"])
    assert parent_read["events"]
    assert next(row for row in _graph(store, old_parent)["archiveMarkers"]
                if row["rootId"] == "committee-secret:1")["expiresAfterFloor"] == 110

    child_scope = _scope(store, revision=0, batch_id="child:0", branch="child", epoch=2,
        grants=grants,
        lineage=[{"branchId": "child", "throughRevision": 0},
                 {"branchId": "main", "throughRevision": 4}])
    child_marker = next(row for row in _graph(store, child_scope)["archiveMarkers"]
                        if row["rootId"] == "committee-secret:1")
    assert child_marker["expiresAfterFloor"] == 101

    # A new parent checkpoint at the same accepted floor sees its own access row;
    # the already-open older checkpoint keeps its original projection.
    empty = batch("batch:5", 5, [], "batch:4")
    call(store, "apply_batch", batch=empty)
    new_parent = _scope(store, revision=5, batch_id="batch:5", grants=grants)
    fresh = next(row for row in _graph(store, new_parent)["archiveMarkers"]
                 if row["rootId"] == "committee-secret:1")
    assert fresh["expiresAfterFloor"] == 110
    assert next(row for row in _graph(store, old_parent)["archiveMarkers"]
                if row["rootId"] == "committee-secret:1")["expiresAfterFloor"] == 110


def test_failed_archive_read_budget_does_not_persist_access_or_renewal(store):
    handle = _scope(store, grants=["committee-secret:1", "d:secret1"], max_read_bytes=1)
    with pytest.raises(EvidenceError, match="shared scope read budget exceeded"):
        _archive(store, handle, ["committee-secret:1"])
    assert store.scopes[handle]["readBytes"] == 0
    assert store.scopes[handle]["readCalls"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM play_lifecycle_access ").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["continuation-cycle", "cross-actor-supersedes", "missing-archive-target"])
def test_lifecycle_references_fail_atomically(store, damage):
    changes = []
    if damage == "continuation-cycle":
        changes = [
            {"kind": "event", "id": "loop:a", "order": 6, "text": "A", "sourceSpans": [span()],
             "metadata": {"continuationOf": "loop:b", **_metadata()}},
            {"kind": "event", "id": "loop:b", "order": 6, "text": "B", "sourceSpans": [span()],
             "metadata": {"continuationOf": "loop:a", **_metadata()}},
        ]
    elif damage == "cross-actor-supersedes":
        changes = [{"kind": "acquisition", "id": "bad:supersedes", "order": 6,
            "actorId": "A", "text": "cannot replace C's facet", "eventIds": ["defense:v2"],
            "beliefKind": "fact", "method": "witness", "basisIds": ["defense:v2"],
            "sourceSpans": [span()], "metadata": {"memorySupersedes": ["c:defense:v2"]}}]
    else:
        changes = [{"kind": "event", "id": "bad:archive", "order": 6, "text": "bad archive",
            "sourceSpans": [span()], "metadata": _metadata(archiveEventIds=["no-such-event"])}]
    candidate = batch("candidate", 4, changes, "batch:3")
    baseline = list(store.db.iterdump())
    with pytest.raises(EvidenceError):
        call(store, "validate_batch", batch=candidate)
    with pytest.raises(EvidenceError):
        call(store, "apply_batch", batch=candidate)
    assert list(store.db.iterdump()) == baseline
    assert call(store, "receipt", campaignId="camp", batchId="candidate")["status"] == "not-applied"


def test_old_cutoff_cannot_use_future_archive_restore_access(store):
    # Both checkpoints have the same accepted floor. The later World revision's
    # restore must not affect a previously-open or reopened earlier cutoff.
    grants = ["committee-secret:1", "d:secret1"]
    old = _scope(store, grants=grants)
    old_graph = _graph(store, old)
    assert "committee-secret:1" not in set(old_graph["activeRecordIds"])

    call(store, "apply_batch", batch=batch("batch:4", 4, [], "batch:3"))
    later = _scope(store, revision=4, batch_id="batch:4", grants=grants)
    assert _graph(store, later)["acceptedFloor"] == old_graph["acceptedFloor"]
    restored = _archive(store, later, ["committee-secret:1"], operation="restore")
    assert restored["events"]
    assert "committee-secret:1" in set(_graph(store, later)["activeRecordIds"])
    assert "committee-secret:1" not in set(_graph(store, old)["activeRecordIds"])

    reopened_old = _scope(store, grants=grants)
    assert "committee-secret:1" not in set(_graph(store, reopened_old)["activeRecordIds"])
    assert old_graph["acceptedFloor"] == _graph(store, old)["acceptedFloor"]
