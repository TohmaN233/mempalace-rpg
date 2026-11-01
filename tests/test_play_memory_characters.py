"""Offline lossless dossier / evidence-tier / scope-isolation acceptance."""
import copy

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from mempalace_rpg.play_memory import PlayMemoryStore
from mempalace_rpg.play_memory_characters import CharacterImportancePolicy, DETAIL_POLICY
from mempalace_rpg.play_memory_protocol import SCHEMA, sealed_batch, validate
from test_play_memory import TOKEN, batch, binding, call, records, scope, span


def dossier(store, handle, actor="C"):
    result = store.dispatch("actor_dossiers", {"hostToken": TOKEN, "scopeHandle": handle, "actors": [actor]})
    validate(result, SCHEMA["$defs"]["actorDossiersResult"])
    return result["actors"][0]


def pack(store, handle, actor="C", references=None, budget=100000, policy=True):
    params = {"hostToken": TOKEN, "scopeHandle": handle, "actors": [actor],
              "references": ["secret"] if references is None else references, "budgetBytes": budget}
    if policy:
        params["detailPolicy"] = DETAIL_POLICY
    result = store.dispatch("actor_packs", params)
    validate(result, SCHEMA["$defs"]["actorPacksResult"])
    return result["actors"][0]


def field(identifier, text, order=3, name="memory.importance", kind="state", **kwargs):
    return {"kind": kind, "id": identifier, "order": order, "actorId": "C", "field": name,
            "text": text, "knownBy": ["C"], "sourceSpans": [span()], **kwargs}


def apply(store, additions, revision=2, predecessor="batch:1"):
    return call(store, "apply_batch", batch=batch(f"batch:{revision}", revision, additions, predecessor))


@pytest.fixture
def store(tmp_path):
    result = PlayMemoryStore(tmp_path / "characters.sqlite", host_token=TOKEN)
    call(result, "apply_batch", batch=batch())
    yield result
    result.close()


def test_lossless_dossier_preserves_partial_belief_and_directed_private_relationships(store):
    handle = scope(store)
    c = dossier(store, handle)
    assert c["acquisitions"][0]["record"]["beliefKind"] == "hearsay"
    assert "南井" not in str(c)
    assert c["relationships"] == []
    a = dossier(store, handle, "A")
    assert [(r["fromActorId"], r["toActorId"]) for r in a["relationships"]] == [("A", "C")]
    assert c["coverage"]["available"] == c["coverage"]["delivered"]
    assert c["coverage"]["deferred"] == []
    assert all(r["branchId"] == "main" and r["worldRevision"] == 1 for r in c["provenance"])
    assert c["dates"][0]["dateRule"]["day"] is None


def test_ambient_relevant_secret_knowledge_is_always_delivered_and_detail_is_explicit(store):
    # D has only presence, which must not confer the secret acquisition.
    handle = scope(store)
    c = pack(store, handle)
    assert c["importance"]["tier"] == "ambient"  # interactions are telemetry, not semantic importance
    a = pack(store, handle, "A")
    assert a["importance"]["tier"] == "ambient"
    assert a["knowledge"][0]["record"]["text"] == "钥匙在南井"
    assert a["knowledge"][0]["deliveryStatus"] == "included"
    assert "a:1" not in a["detailCoverage"]["available"]  # basic cards only; knowledge has its own status
    assert "a-to-c" in a["detailCoverage"]["deferred"]
    d = pack(store, handle, "D")
    assert d["knowledge"][0]["knowledgeStatus"] == "unknown"
    assert "南井" not in str(d)
    with pytest.raises(EvidenceError, match="exceed shared budget"):
        pack(store, handle, "A", budget=1)


def test_opt_in_preserves_legacy_basics_and_omitted_budget_behavior(store):
    handle = scope(store)
    legacy = pack(store, handle, "A", policy=False)
    assert "importance" not in legacy and "detailCoverage" not in legacy
    assert legacy["basics"][0]["id"] == "a-to-c"
    minimal = pack(store, handle, "A", references=[], policy=False)
    assert minimal["status"] == "unqueried"


def test_override_persistence_auto_reset_expiry_and_field_supersession(store):
    apply(store, [field("core", "core"), field("new-goal", "protect friend", 3, "goal")])
    handle = scope(store, worldRevision=2, requiredBatchId="batch:2",
                   lineage=[{"branchId": "main", "throughRevision": 2}])
    core = dossier(store, handle)
    assert core["importance"]["tier"] == "core"
    assert [r["id"] for r in core["state"] if r["field"] == "goal"] == ["new-goal"]
    assert pack(store, handle)["detailCoverage"]["deferred"] == []
    path = store.path if hasattr(store, "path") else store.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = PlayMemoryStore(path, host_token=TOKEN)
    try:
        assert dossier(reopened, scope(reopened, worldRevision=2, requiredBatchId="batch:2",
            lineage=[{"branchId": "main", "throughRevision": 2}]))["importance"]["tier"] == "core"
    finally:
        reopened.close()
    apply(store, [field("reset", "auto", 4, validUntil=6)], 3, "batch:2")
    reset = dossier(store, scope(store, worldRevision=3, requiredBatchId="batch:3",
        lineage=[{"branchId": "main", "throughRevision": 3}]))
    assert reset["importance"]["tier"] == "ambient"
    assert reset["importance"]["override"] == "auto"  # expiry never resurrects core
    assert [t["tier"] for t in reset["importance"]["transitions"]] == ["ambient", "core", "ambient"]


def test_distinct_interactions_and_expiry_never_replace_semantic_importance_evidence(store):
    additions = []
    for number in range(3):
        event = {"kind": "event", "id": f"visit:{number}", "order": 3 + number,
                 "text": "visit", "sourceSpans": [span()]}
        acquisition = {"kind": "acquisition", "id": f"heard:{number}", "order": 3 + number,
                       "actorId": "C", "text": "a visit", "eventIds": [event["id"]],
                       "beliefKind": "fact", "method": "witness", "basisIds": [event["id"]],
                       "sourceSpans": [span()], "validUntil": 8}
        additions.extend([event, acquisition])
    apply(store, additions)
    settings = {"worldRevision": 2, "requiredBatchId": "batch:2",
                "lineage": [{"branchId": "main", "throughRevision": 2}]}
    major = dossier(store, scope(store, **{**settings, "storyOrder": 7}))
    assert major["importance"]["tier"] == "ambient"
    assert major["importance"]["interactionCount"] == 5
    assert "interaction-count-is-telemetry-only" in major["importance"]["reasons"]
    demoted = dossier(store, scope(store, **settings))
    assert demoted["importance"]["tier"] == "ambient"
    assert [row["tier"] for row in demoted["importance"]["transitions"]] == ["ambient"]


def test_host_grouped_clauses_count_as_one_interaction_but_legacy_events_stay_distinct():
    grouped = []
    for number in range(12):
        event_id = f"floor:1:event:{number}"
        grouped.extend([
            {"kind": "event", "id": event_id, "order": 1, "sceneId": "floor:1",
             "metadata": {"interactionGroup": "floor:1"}},
            {"kind": "acquisition", "id": f"heard:{number}", "order": 1,
             "actorId": "carter", "eventIds": [event_id]},
        ])
    policy = CharacterImportancePolicy()
    result = policy.classify(grouped, "carter")
    assert result["tier"] == "ambient"
    assert result["interactionCount"] == 1
    legacy = copy.deepcopy(grouped)
    for record in legacy:
        record.pop("metadata", None)
    assert policy.classify(legacy, "carter")["interactionCount"] == 12


def test_override_and_role_require_source_backing_valid_values_and_actor_visibility(store):
    with pytest.raises(EvidenceError, match="memory.importance"):
        apply(store, [field("invalid", "superhero")])
    with pytest.raises(EvidenceError, match="ambiguous importance"):
        apply(store, [field("profile-override", "major", kind="profile"), field("state-override", "ambient")])
    with pytest.raises(EvidenceError):
        invalid = field("unbacked", "major")
        invalid["sourceSpans"] = []
        apply(store, [invalid])
    hidden = field("hidden", "core")
    hidden["knownBy"] = ["A"]
    apply(store, [hidden, field("player", "player", 3, "memory.role", kind="profile", validUntil=5)])
    settings = {"worldRevision": 2, "requiredBatchId": "batch:2",
                "lineage": [{"branchId": "main", "throughRevision": 2}]}
    assert dossier(store, scope(store, **{**settings, "storyOrder": 4}))["importance"]["tier"] == "core"
    row = dossier(store, scope(store, **settings))
    assert row["importance"]["tier"] == "ambient"
    assert "hidden" not in row["importance"]["evidenceIds"]


def test_branch_cutoff_reroll_and_immutable_checkpoint_cannot_read_parent_future(store):
    old = scope(store)
    apply(store, [field("parent-future", "core")])
    child = sealed_batch(campaignId="camp", batchId="child:0", branchId="child", epoch=1,
        worldRevision=0, predecessorBatchId="batch:1",
        parent={"branchId": "main", "batchId": "batch:1", "throughRevision": 1}, records=[])
    call(store, "apply_batch", batch=child)
    child_handle = scope(store, branchId="child", worldRevision=0, requiredBatchId="child:0",
        lineage=[{"branchId": "child", "throughRevision": 0}, {"branchId": "main", "throughRevision": 1}])
    assert "parent-future" not in str(dossier(store, child_handle))
    assert dossier(store, old)["importance"]["tier"] == "ambient"
    scope(store, epoch=2, worldRevision=2, requiredBatchId="batch:2",
          lineage=[{"branchId": "main", "throughRevision": 2}])
    with pytest.raises(EvidenceError, match="invalidated"):
        dossier(store, old)
    assert dossier(store, child_handle)["importance"]["tier"] == "ambient"


def test_exceptions_attitude_and_host_scope_boundaries(store):
    apply(store, [{"kind": "exception", "id": "c-unknown", "order": 3, "actorId": "C",
                   "backgroundId": "public-rule", "status": "unknown", "basisIds": ["secret"],
                   "sourceSpans": [span()]},
                  {"kind": "attitude", "id": "c-doubt", "order": 3, "actorId": "C",
                   "acquisitionId": "c:1", "attitude": "doubtful", "basisIds": ["retelling"],
                   "sourceSpans": [span()]}])
    handle = scope(store, worldRevision=2, requiredBatchId="batch:2",
                   lineage=[{"branchId": "main", "throughRevision": 2}])
    c = dossier(store, handle)
    assert c["backgrounds"] == [] and c["exceptions"][0]["status"] == "unknown"
    assert c["acquisitions"][0]["attitude"] == "doubtful"
    assert c["attitudes"][0]["id"] == "c-doubt"
    with pytest.raises(EvidenceError, match="Host"):
        store.dispatch("actor_dossiers", {"hostToken": "wrong", "scopeHandle": handle, "actors": ["C"]})
    with pytest.raises(EvidenceError, match="outside"):
        dossier(store, handle, "unbound")


def test_policy_configuration_is_explicit_and_duplicate_hearing_is_not_an_interaction(store):
    original = next(r for r in records() if r["id"] == "a:1")
    repeated = {**copy.deepcopy(original), "id": "a:repeat", "order": 3}
    apply(store, [repeated])
    handle = scope(store, worldRevision=2, requiredBatchId="batch:2",
                   lineage=[{"branchId": "main", "throughRevision": 2}])
    assert dossier(store, handle, "A")["importance"]["interactionCount"] == 1
    store.importance_policy = CharacterImportancePolicy("test-policy-v2")
    importance = dossier(store, handle, "A")["importance"]
    assert importance["tier"] == "ambient" and importance["policyId"] == "test-policy-v2"
    with pytest.raises(ValueError):
        CharacterImportancePolicy("")
