"""Source-backed semantic importance, without name or interaction-count guesses."""
import json
from pathlib import Path

import pytest

from mempalace_rpg.play_memory_characters import CharacterImportancePolicy
from mempalace_rpg.play_memory import PlayMemoryStore, effective_records
from mempalace_rpg.play_memory_library import character_registry
from mempalace_rpg.play_memory_protocol import sealed_batch
from mempalace_rpg.evidence_protocol import EvidenceError
from test_play_memory import TOKEN, binding
from test_play_memory_library import host_call, registry_entry, introduction, source_record, profile_record


def profile(actor, field, text, **extra):
    return {"kind": "profile", "id": f"{actor}:{field}", "actorId": actor,
            "field": field, "text": text, "knownBy": [actor], "order": 1,
            "sourceSpans": [{"sourceId": "accepted-prose"}], **extra}


def qualification(kind, status="established", actor="friend", counterpart="player"):
    value = {"schemaVersion": "character-qualification-v1", "kind": kind,
             "status": status, "counterpartActorId": counterpart}
    return profile(actor, f"character.qualification:{kind}:{counterpart}", json.dumps(value), kind="state", knownBy=[])


def test_only_bound_selected_core_is_core_while_other_critical_remains_major():
    rows = [profile("merlin", "name", "Merlin", metadata={"actorDirectory": {
                "role": "critical", "coreBinding": {"kind": "fated-selected-core-v1",
                    "ownerActorId": "player", "coreId": "reviewed-choice",
                    "definitionHash": "definition", "selectionHash": "selection", "source": {"uid": 407}}}}),
            profile("official", "name", "Another critical actor", metadata={"actorDirectory": {"role": "critical"}}),
            profile("player", "memory.role", "player")]
    policy = CharacterImportancePolicy()
    assert policy.classify(rows, "merlin")["tier"] == "core"
    assert policy.classify(rows, "official")["tier"] == "major"


@pytest.mark.parametrize("kind", ["long-term-companion", "spouse"])
@pytest.mark.parametrize("status,expected", [("established", "core"), ("temporary", "ambient"),
    ("intended", "ambient"), ("reported", "ambient"), ("ended", "ambient")])
def test_only_established_player_relationship_enters_core(kind, status, expected):
    rows = [profile("player", "memory.role", "player"), qualification(kind, status)]
    assert CharacterImportancePolicy().classify(rows, "friend")["tier"] == expected


def test_external_spouse_or_unbacked_qualification_does_not_promote():
    rows = [profile("player", "memory.role", "player"), qualification("spouse", counterpart="another-npc")]
    assert CharacterImportancePolicy().classify(rows, "friend")["tier"] == "ambient"
    rows[-1] = qualification("spouse")
    rows[-1]["sourceSpans"] = []
    assert CharacterImportancePolicy().classify(rows, "friend")["tier"] == "ambient"


@pytest.mark.parametrize("kind,tier", [("plot-important", "major"), ("recurring", "recurring")])
def test_explicit_semantic_qualifications_and_manual_override(kind, tier):
    rows = [profile("player", "memory.role", "player"), qualification(kind)]
    result = CharacterImportancePolicy().classify(rows, "friend")
    assert result["tier"] == tier
    assert rows[-1]["id"] in result["evidenceIds"]
    rows.append(profile("friend", "memory.importance", "ambient"))
    assert CharacterImportancePolicy().classify(rows, "friend")["tier"] == "ambient"


def test_many_interactions_are_telemetry_and_never_tier_evidence():
    rows = []
    for index in range(20):
        rows.extend([{"kind": "event", "id": f"event:{index}"},
                     {"kind": "acquisition", "id": f"seen:{index}", "actorId": "carter",
                      "eventIds": [f"event:{index}"]}])
    result = CharacterImportancePolicy().classify(rows, "carter")
    assert result["interactionCount"] == 20
    assert result["tier"] == "ambient"
    assert result["evidenceIds"] == []


def test_existing_accepted_important_introduction_is_major_without_another_qualification(tmp_path):
    entry = registry_entry("friend", "Bela", intro=introduction(important=True), basis="static-profile")
    source = source_record("accepted-panel", "Bela", 1, metadata={"authored": True,
        "characterRegistryImport": True, "characterRegistry": {
            "schemaVersion": "play-memory-character-registry-v1", "entries": [entry]}})
    result = CharacterImportancePolicy().classify([source], "friend")
    assert result["tier"] == "major"
    assert result["evidenceIds"] == ["accepted-panel"]
    assert "accepted-plot-important-introduction" in result["reasons"]
    store = PlayMemoryStore(tmp_path / "intro-importance.sqlite", host_token=TOKEN)
    try:
        name = profile_record("friend", "Bela", 1, "accepted-panel", "Bela")
        host_call(store, "apply_batch", batch=sealed_batch(campaignId="camp", batchId="intro",
            branchId="main", epoch=1, worldRevision=1, predecessorBatchId=None,
            records=[source, name], characterLibraryPolicy="panel-named-intelligent-v1"))
        handle = host_call(store, "open_scope", binding=binding(actorIds=["friend"], grants=[],
            worldRevision=1, requiredBatchId="intro", lineage=[{"branchId": "main", "throughRevision": 1}]))["scopeHandle"]
        dossier = host_call(store, "actor_dossiers", scopeHandle=handle, actors=["friend"])["actors"][0]
        assert dossier["importance"]["tier"] == "major"
        assert [row["tier"] for row in dossier["importance"]["transitions"]] == ["major"]
        assert dossier["importance"]["transitions"][0]["evidenceIds"] == ["accepted-panel"]
    finally:
        store.close()
    # Importance never bypasses named/intelligent/character admission.
    entry["introduction"]["hasProperName"] = False
    assert CharacterImportancePolicy().classify([source], "friend")["tier"] == "ambient"


def test_real_merlin_identity_survives_mutable_name_updates_without_rebinding_their_sources():
    captured = json.loads(Path(__file__).with_name("fixtures").joinpath(
        "merlin-core-identity-20261031.json").read_text(encoding="utf-8"))
    history = [row["record"] for row in captured["rows"]]
    anchor = history[0]
    actor = anchor["actorId"]
    policy = CharacterImportancePolicy()
    before = json.loads(json.dumps(history))
    for order in (1, 18, 62, 200):
        bounded = [row for row in history if row["order"] <= order]
        current = effective_records(bounded, order)
        result = policy.classify(current, actor, identity_records=bounded)
        assert result["tier"] == "core"
        assert "source-bound-selected-core" in result["reasons"]
        assert anchor["id"] in result["evidenceIds"]
    latest = next(row for row in effective_records(history, 200) if row.get("actorId") == actor)
    assert "coreBinding" not in latest["metadata"]["actorDirectory"]
    assert latest["sourceSpans"] == history[-1]["sourceSpans"]
    assert history == before  # Never copy an old binding onto a new name/source.
    hidden_anchor = {**anchor, "knownBy": []}
    bounded = [hidden_anchor, *history[1:]]
    assert policy.classify(effective_records(bounded, 200), actor, identity_records=bounded)["tier"] == "core"
    # A sibling/future anchor is excluded by the caller's lineage/time cutoff.
    without_anchor = history[1:]
    assert policy.classify(effective_records(without_anchor, 200), actor, identity_records=without_anchor)["tier"] == "major"


def test_relationship_termination_expiry_and_parent_cutoff_never_resurrect_qualification():
    player = profile("player", "memory.role", "player")
    started = qualification("spouse")
    ended = {**qualification("spouse", "ended"), "id": "ended", "order": 4, "validUntil": 6}
    rows = [player, started, ended]
    policy = CharacterImportancePolicy()
    assert policy.classify(effective_records(rows, 3), "friend")["tier"] == "core"
    assert policy.classify(effective_records(rows, 4), "friend")["tier"] == "ambient"
    assert policy.classify(effective_records(rows, 7), "friend")["tier"] == "ambient"
    # A child cut off before the ending still retains its own established relation.
    assert policy.classify(effective_records(rows[:2], 7), "friend")["tier"] == "core"


def test_source_only_policy_refresh_preserves_old_receipt_identity_and_deleted_status(tmp_path):
    class HistoricalV1FixturePolicy(CharacterImportancePolicy):
        """The audited v1 critical-role result, only for constructing a historical receipt."""
        def classify(self, rows, actor, *, identity_records=None):
            result = super().classify(rows, actor, identity_records=identity_records)
            result["policyId"] = "character-importance-v1"
            if "source-bound-selected-core" in result["reasons"]:
                result["tier"] = "major"
            return result

    entries = [registry_entry("player", "托马", basis="static-profile"),
               registry_entry("merlin", "梅林", basis="static-profile")]
    entries += [registry_entry(f"npc:{index}", name, basis="static-profile") for index, name in
                enumerate(["艾薇", "莉薇娅", "伊莱", "罗温", "埃德里克"])]
    entries += [registry_entry("removed", "已移出的人", basis="static-profile"),
                registry_entry("unnamed", "车夫", basis="static-profile", intro=introduction(proper=False))]
    text = "托马、梅林、艾薇、莉薇娅、伊莱、罗温、埃德里克在此。"
    initial = [source_record("initial", text, 1, metadata={"authored": True,
        "characterRegistryImport": True, "characterRegistry": {
            "schemaVersion": "play-memory-character-registry-v1", "entries": entries}})]
    player = profile_record("player", "托马", 1, "initial", text)
    role = {**player, "id": "player-role", "field": "memory.role", "text": "player"}
    merlin = profile_record("merlin", "梅林", 1, "initial", text)
    merlin["metadata"] = {"actorDirectory": {"role": "critical", "coreBinding": {
        "kind": "fated-selected-core-v1", "ownerActorId": "player", "coreId": "reviewed",
        "definitionHash": "definition", "selectionHash": "selection", "source": {"uid": 407}}}}
    initial += [player, role, merlin, {"kind": "character-library", "id": "removed-status", "actorId": "removed",
        "order": 1, "status": "deleted", "sourceSpans": player["sourceSpans"]}]
    path = tmp_path / "policy-refresh.sqlite"
    store = PlayMemoryStore(path, host_token=TOKEN, importance_policy=HistoricalV1FixturePolicy())
    try:
        old = host_call(store, "apply_batch", batch=sealed_batch(campaignId="camp", batchId="before",
            branchId="main", epoch=1, worldRevision=1, predecessorBatchId=None, records=initial))
        old_receipt = old["receipt"] if "receipt" in old else old
        assert next(row for row in old_receipt["characterLibrary"]["actors"] if row["actorId"] == "merlin")["tier"] == "major"
    finally:
        store.close()

    store = PlayMemoryStore(path, host_token=TOKEN)
    try:
        # Repeat exact accepted registry entries through the existing authored import path.
        refresh = source_record("refresh", json.dumps(entries, ensure_ascii=False), 2, metadata={
            "authored": True, "reason": "derived importance policy refresh", "characterRegistryImport": True,
            "characterRegistry": {"schemaVersion": "play-memory-character-registry-v1", "entries": entries}})
        result = host_call(store, "apply_batch", batch=sealed_batch(campaignId="camp", batchId="after",
            branchId="main", epoch=1, worldRevision=2, previousWorldRevision=1,
            predecessorBatchId="before", records=[refresh], characterLibraryPolicy="panel-named-intelligent-v1"))
        receipt = result["receipt"] if "receipt" in result else result
        actors = receipt["characterLibrary"]["actors"]
        assert len(actors) == 7
        assert next(row for row in actors if row["actorId"] == "merlin")["tier"] == "core"
        assert {row["tierOverride"] for row in actors} == {"auto"}
        assert not {"removed", "unnamed"} & {row["actorId"] for row in actors}
        assert host_call(store, "receipt", campaignId="camp", batchId="before")["receipt"] == old_receipt
        assert character_registry([*initial, refresh]) == character_registry(initial)
        assert not any(row["kind"] in ("scene", "event", "acquisition") for row in [refresh])
        handle = host_call(store, "open_scope", binding=binding(actorIds=["merlin", "player"], grants=[],
            worldRevision=2, requiredBatchId="after", lineage=[{"branchId": "main", "throughRevision": 2}]))["scopeHandle"]
        dossier = host_call(store, "actor_dossiers", scopeHandle=handle, actors=["merlin"])["actors"][0]
        assert dossier["importance"]["policyId"] == "character-importance-v2"
        assert dossier["importance"]["transitions"][-1]["tier"] == "core"
    finally:
        store.close()

def test_accepted_relationship_state_consumes_exact_spans_and_branch_cutoff(tmp_path):
    text = "Ari and Bela establish a permanent shared household."
    entries = [registry_entry("player", "Ari", basis="static-profile"),
               registry_entry("friend", "Bela", basis="static-profile")]
    source = source_record("turn:1:prose", text, 1, metadata={"authored": True,
        "characterRegistryImport": True, "characterRegistry": {
            "schemaVersion": "play-memory-character-registry-v1", "entries": entries}})
    player = profile_record("player", "Ari", 1, source["id"], text)
    friend = profile_record("friend", "Bela", 1, source["id"], text)
    role = {**player, "id": "player-role", "field": "memory.role", "text": "player"}
    established = {**qualification("long-term-companion"), "kind": "state", "id": "relationship:start",
        "sourceSpans": [{"sourceId": source["id"], "version": source["version"], "startByte": 0,
                         "endByte": len(text.encode("utf-8")), "sha256": source["sha256"]}]}
    with_store = PlayMemoryStore(tmp_path / "qualification.sqlite", host_token=TOKEN)
    try:
        host_call(with_store, "apply_batch", batch=sealed_batch(campaignId="camp", batchId="start",
            branchId="main", epoch=1, worldRevision=1, predecessorBatchId=None,
            records=[source, player, friend, role, established], characterLibraryPolicy="panel-named-intelligent-v1"))
        # Host-only qualification can promote importance, never personal knowledge or raw-source access.
        handle = host_call(with_store, "open_scope", binding=binding(actorIds=["friend"], grants=[],
            purpose="actor-knowledge", worldRevision=1, requiredBatchId="start",
            lineage=[{"branchId": "main", "throughRevision": 1}]))["scopeHandle"]
        dossier = host_call(with_store, "actor_dossiers", scopeHandle=handle, actors=["friend"])["actors"][0]
        assert dossier["importance"]["tier"] == "core"
        assert dossier["state"] == [] and dossier["acquisitions"] == []
        packs = host_call(with_store, "actor_packs", scopeHandle=handle, actors=["friend"], references=[],
            budgetBytes=10000, detailPolicy="character-importance-v1")["actors"][0]
        assert all(row.get("field", "") != established["field"] for row in packs["basics"])
        with pytest.raises(EvidenceError, match="outside|unauthorized|authorized"):
            host_call(with_store, "navigate", scopeHandle=handle, seed=established["id"], relation="source", page=0, limit=10)
        navigated = host_call(with_store, "navigate", scopeHandle=handle, seed=friend["id"], relation="source", page=0, limit=10)
        original = host_call(with_store, "read", scopeHandle=handle, handle=navigated["seed"]["handle"], representation="original")
        assert "permanent shared household" not in str(original)
        ended = {**established, "id": "relationship:end", "order": 2,
                 "text": qualification("long-term-companion", "ended")["text"]}
        result = host_call(with_store, "apply_batch", batch=sealed_batch(campaignId="camp", batchId="end",
            branchId="main", epoch=1, worldRevision=2, previousWorldRevision=1, predecessorBatchId="start",
            records=[ended], characterLibraryPolicy="panel-named-intelligent-v1"))
        receipt = result["receipt"] if "receipt" in result else result
        assert next(row for row in receipt["characterLibrary"]["actors"] if row["actorId"] == "friend")["tier"] == "ambient"
        child = host_call(with_store, "apply_batch", batch=sealed_batch(campaignId="camp", batchId="child",
            branchId="child", epoch=1, worldRevision=0, predecessorBatchId="start", records=[],
            parent={"branchId": "main", "batchId": "start", "throughRevision": 1}))
        receipt = child["receipt"] if "receipt" in child else child
        assert next(row for row in receipt["characterLibrary"]["actors"] if row["actorId"] == "friend")["tier"] == "core"
    finally:
        with_store.close()
