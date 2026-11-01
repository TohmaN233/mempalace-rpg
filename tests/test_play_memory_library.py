import copy
import hashlib
import json

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from mempalace_rpg.play_memory import PlayMemoryStore
from mempalace_rpg.play_memory_directory import actor_directory, resolve_actors
from mempalace_rpg.play_memory_library import character_registry
from mempalace_rpg.play_memory_protocol import SCHEMA, sealed_batch, validate
from test_play_memory import TOKEN, binding, call, records, scope, sha, span


POLICY = "panel-named-intelligent-v1"


def introduction(*, entity_kind="character", proper=True, intelligent=True, important=False):
    return {"entityKind": entity_kind, "hasProperName": proper, "intelligent": intelligent,
            "plotImportant": important}


def registry_entry(actor_id="M", name="梅林", aliases=None, *, intro=None, request="f1", panel_hash=None,
                   basis="panel"):
    panel_hash = panel_hash or sha("panel:" + request)
    return {"actorId": actor_id, "canonicalName": name, "aliases": aliases or [],
            "introduction": intro or introduction(),
            "firstAppearance": {"requestId": request, "panelHash": panel_hash, "basis": basis}}


def registry(records_, entries):
    scene = next(row for row in records_ if row["kind"] == "scene")
    scene.setdefault("metadata", {})["characterRegistry"] = {
        "schemaVersion": "play-memory-character-registry-v1", "entries": entries}


def batch_of(identifier, revision, rows, predecessor=None, *, policy=True, parent=None, previous=None):
    kwargs = {}
    if policy:
        kwargs["characterLibraryPolicy"] = POLICY
    if previous is not None:
        kwargs["previousWorldRevision"] = previous
    return sealed_batch(campaignId="camp", batchId=identifier, branchId="main" if parent is None else parent["child"],
                        epoch=1, worldRevision=revision, predecessorBatchId=predecessor,
                        records=rows, parent=parent and {key: parent[key] for key in
                        ("branchId", "batchId", "throughRevision")}, **kwargs)


def host_call(store, method, **params):
    if method in ("apply_batch", "validate_batch", "receipt", "open_scope", "actor_directory",
                  "actor_packs", "actor_dossiers", "resolve_actors"):
        params["hostToken"] = TOKEN
    return store.dispatch(method, params)


def open_library_scope(store, *, branch="main", revision=1, required="batch:1", lineage=None,
                       actor_ids=None):
    bind = binding(branchId=branch, worldRevision=revision, requiredBatchId=required,
                   lineage=lineage or [{"branchId": branch, "throughRevision": revision}],
                   actorIds=actor_ids or ["A", "C", "D", "M"])
    bind["characterLibraryPolicy"] = POLICY
    return host_call(store, "open_scope", binding=bind)["scopeHandle"]


def profile_record(actor_id, name, order, source_id, source_text):
    digest = sha(source_text)
    offset = source_text.encode("utf-8").find(name.encode("utf-8"))
    assert offset >= 0
    return {"kind": "profile", "id": f"{actor_id}:name:{order}", "order": order,
            "actorId": actor_id, "field": "name", "text": name, "knownBy": [actor_id],
            "sourceSpans": [{"sourceId": source_id, "version": digest, "startByte": offset,
                             "endByte": offset + len(name.encode("utf-8")), "sha256": sha(name)}]}


def source_record(source_id, text, order, *, metadata=None):
    digest = sha(text)
    return {"kind": "source", "id": source_id, "order": order, "sourceId": source_id,
            "version": digest, "text": text, "sha256": digest, **({"metadata": metadata} if metadata else {})}


@pytest.fixture
def store(tmp_path):
    instance = PlayMemoryStore(tmp_path / "library.sqlite", host_token=TOKEN)
    initial = records()
    registry(initial, [
        registry_entry(),
        registry_entry("G", "磨坊看门人", intro=introduction(entity_kind="character", proper=False)),
        registry_entry("X", "灰角兽", intro=introduction(entity_kind="monster", proper=True)),
        registry_entry("N", "宁", intro=introduction(intelligent=False)),
    ])
    host_call(instance, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:1", branchId="main", epoch=1,
        worldRevision=1, predecessorBatchId=None, records=initial))
    yield instance
    instance.close()


def test_receipt_projects_only_explicitly_eligible_identities_with_shared_importance_policy(store):
    receipt = host_call(store, "receipt", campaignId="camp", batchId="batch:1")["receipt"]
    validate(receipt, SCHEMA["$defs"]["receipt"])
    assert receipt["characterLibrary"] == {
        "schemaVersion": "play-memory-character-library-v1",
        "actors": [{"actorId": "M", "displayName": "梅林", "aliases": [],
                    "tier": "ambient", "tierOverride": "auto"}],
    }
    assert receipt["receiptHash"] == __import__("mempalace_rpg.evidence_protocol", fromlist=["canonical_hash"]).canonical_hash(
        {key: value for key, value in receipt.items() if key != "receiptHash"})


def test_opt_in_actor_directory_exposes_saved_library_not_legacy_or_excluded_identities(store):
    handle = open_library_scope(store)
    directory = host_call(store, "actor_directory", scopeHandle=handle)
    validate(directory, SCHEMA["$defs"]["actorDirectoryResult"])
    assert [row["actorId"] for row in directory["actors"]] == ["M"]
    assert directory["actors"][0]["displayName"] == "梅林"
    assert directory["actors"][0]["importance"]["tier"] == "ambient"


def test_open_scope_reports_only_host_bound_ids_without_any_seen_identity():
    initial = records()
    registry(initial, [
        registry_entry("M", "梅林"),
        registry_entry("S", "已保存同伴"),
        registry_entry("G", "磨坊看门人", intro=introduction(proper=False)),
        registry_entry("X", "灰角兽", intro=introduction(entity_kind="monster")),
        registry_entry("N", "宁", intro=introduction(intelligent=False)),
        registry_entry("outside", "范围外已知角色"),
    ])
    legacy_text = "旧人走进房间。"
    legacy_source = source_record("legacy:source", legacy_text, 1)
    initial.extend([legacy_source, profile_record("legacy", "旧人", 1, "legacy:source", legacy_text)])
    local = PlayMemoryStore(":memory:", host_token=TOKEN)
    try:
        host_call(local, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:1", branchId="main", epoch=1,
            worldRevision=1, predecessorBatchId=None, records=initial))

        deletion_text = "The owner removed Merlin from the saved library."
        deletion_source = source_record("delete:source", deletion_text, 2,
                                       metadata={"authored": True})
        deletion = {"kind": "character-library", "id": "delete:M", "order": 2,
                    "actorId": "M", "status": "deleted",
                    "sourceSpans": [{"sourceId": deletion_source["sourceId"],
                                     "version": deletion_source["version"],
                                     "startByte": 0,
                                     "endByte": len(deletion_text.encode("utf-8")),
                                     "sha256": deletion_source["sha256"]}],
                    "metadata": {"authored": True}}
        host_call(local, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:2", branchId="main", epoch=1,
            worldRevision=2, previousWorldRevision=1, predecessorBatchId="batch:1",
            records=[deletion_source, deletion]))

        actor_ids = ["M", "S", "G", "X", "N", "legacy", "world:new"]
        strict_binding = binding(
            worldRevision=2, requiredBatchId="batch:2", actorIds=actor_ids,
            lineage=[{"branchId": "main", "throughRevision": 2}],
            characterLibraryPolicy=POLICY)
        opened = host_call(local, "open_scope", binding=strict_binding)
        validate(opened, SCHEMA["$defs"]["openScopeResult"])
        assert opened["actorFirstAppearanceIds"] == ["world:new"]

        # A seen actor outside this exact scope is never included in the hint.
        assert "outside" not in opened["actorFirstAppearanceIds"]

        legacy_opened = host_call(local, "open_scope", binding={
            key: value for key, value in strict_binding.items()
            if key != "characterLibraryPolicy"
        })
        validate(legacy_opened, SCHEMA["$defs"]["openScopeResult"])
        assert "actorFirstAppearanceIds" not in legacy_opened
    finally:
        local.close()


def test_resolver_matches_only_primary_canonical_or_specific_alias_and_reports_ambiguity(store):
    initial = records()
    registry(initial, [
        registry_entry("M", "梅林", ["花之魔术师", "店主"]),
        registry_entry("Q", "奎因", ["店主"], request="f2"),
    ])
    local = PlayMemoryStore(":memory:", host_token=TOKEN)
    try:
        host_call(local, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:1", branchId="main", epoch=1,
            worldRevision=1, predecessorBatchId=None, records=initial))
        handle = open_library_scope(local)
        params = {"scopeHandle": handle, "originId": "f3", "firstAppearance": {
            "requestId": "f3", "panelHash": sha("panel:f3")},
            "source": {"sourceId": "f3:prose", "text": "花之魔术师走进房间。", "order": 3},
            "proposals": [{"localRef": "mage", "displayName": "花之魔术师", "aliases": ["店主"],
                           "present": True, "channel": "physical"}]}
        result = host_call(local, "resolve_actors", **params)
        assert result["actors"][0]["actorId"] == "M"
        assert result["actors"][0]["displayName"] == "梅林"
        assert result["actors"][0]["memoryEligible"] is True
        assert result["records"] == []
        params["proposals"][0]["displayName"] = "店主"
        with pytest.raises(EvidenceError, match="ambiguous"):
            host_call(local, "resolve_actors", **params)
    finally:
        local.close()


def test_lifecycle_title_only_identities_do_not_merge_or_block_proper_names():
    initial = records()
    registry(initial, [
        registry_entry("M", "洛恩", ["白狼", "店主"]),
        registry_entry("Q", "奎因", ["店主"], request="f2"),
        registry_entry("G", "店主", intro=introduction(proper=False), request="f3"),
        registry_entry("X", "店主", intro=introduction(proper=False), request="f4"),
    ])
    local = PlayMemoryStore(":memory:", host_token=TOKEN)
    try:
        host_call(local, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:1", branchId="main", epoch=1,
            worldRevision=1, predecessorBatchId=None, records=initial))
        lifecycle_scope = open_library_scope(
            local, actor_ids=["A", "C", "D", "M", "Q", "G", "X"])

        # An explicit current title-only classification cannot merge through a
        # generic spelling or alias shared by two excluded identities and a named actor.
        fresh = host_call(local, "resolve_actors", scopeHandle=lifecycle_scope, originId="title-only-new",
                          firstAppearance={"requestId": "title-only-new", "panelHash": sha("panel:title-only-new")},
                          source={"sourceId": "title-only:new", "text": "店主推门进来。", "order": 2},
                          proposals=[{"localRef": "shopkeeper", "displayName": "店主", "aliases": [],
                                      "present": True, "channel": "physical",
                                      "introduction": introduction(proper=False)}])
        fresh_actor = fresh["actors"][0]
        assert fresh_actor["actorId"] not in {"M", "Q", "G", "X"}
        assert fresh_actor["memoryEligible"] is False
        assert fresh["records"] == []
        assert len(fresh["characterRegistry"]) == 1
        assert fresh["characterRegistry"][0]["actorId"] == fresh_actor["actorId"]
        assert fresh["characterRegistry"][0]["introduction"]["hasProperName"] is False

        # The exact Host/accepted ID still resolves, despite the duplicate title.
        existing = host_call(local, "resolve_actors", scopeHandle=lifecycle_scope, originId="title-only-existing",
                             firstAppearance={"requestId": "title-only-existing", "panelHash": sha("panel:title-only-existing")},
                             source={"sourceId": "title-only:existing", "text": "店主把门带上。", "order": 2},
                             proposals=[{"localRef": "shopkeeper", "actorId": "G", "displayName": "店主",
                                         "aliases": [], "present": True, "channel": "physical",
                                         "introduction": introduction(proper=False)}])
        assert existing["actors"][0]["actorId"] == "G"
        assert existing["actors"][0]["memoryEligible"] is False
        assert existing["records"] == []

        # A real named alias still resolves, and ambiguity among proper-named
        # identities remains visible instead of picking one.
        named = host_call(local, "resolve_actors", scopeHandle=lifecycle_scope, originId="named-alias",
                          firstAppearance={"requestId": "named-alias", "panelHash": sha("panel:named-alias")},
                          source={"sourceId": "named:alias", "text": "白狼递来一张纸。", "order": 2},
                          proposals=[{"localRef": "wolf", "displayName": "白狼", "aliases": [],
                                      "present": True, "channel": "physical"}])
        assert named["actors"][0]["actorId"] == "M"
        assert named["actors"][0]["displayName"] == "洛恩"
        with pytest.raises(EvidenceError, match="ambiguous"):
            host_call(local, "resolve_actors", scopeHandle=lifecycle_scope, originId="named-ambiguous",
                      firstAppearance={"requestId": "named-ambiguous", "panelHash": sha("panel:named-ambiguous")},
                      source={"sourceId": "named:ambiguous", "text": "店主走进房间。", "order": 2},
                      proposals=[{"localRef": "named", "displayName": "店主", "aliases": [],
                                  "present": True, "channel": "physical"}])

        # The opt-in filter does not silently rewrite legacy resolution policy.
        legacy_scope = host_call(local, "open_scope", binding=binding(
            worldRevision=1, requiredBatchId="batch:1", actorIds=["A", "C", "D", "M", "Q", "G", "X"],
            lineage=[{"branchId": "main", "throughRevision": 1}]))["scopeHandle"]
        with pytest.raises(EvidenceError, match="ambiguous"):
            host_call(local, "resolve_actors", scopeHandle=legacy_scope, originId="legacy-ambiguous",
                      source={"sourceId": "legacy:ambiguous", "text": "店主走进房间。", "order": 2},
                      proposals=[{"localRef": "legacy", "displayName": "店主", "aliases": [],
                                  "present": True, "channel": "physical"}])
    finally:
        local.close()


def test_excluded_new_actor_is_current_only_and_never_gets_profile_records(store):
    handle = open_library_scope(store)
    before = store.db.total_changes
    result = host_call(store, "resolve_actors", scopeHandle=handle, originId="turn-monster",
                       firstAppearance={"requestId": "turn-monster", "panelHash": sha("panel:monster")},
                       source={"sourceId": "monster:prose", "text": "灰角兽甲冲出门。", "order": 2},
                       proposals=[{"localRef": "monster", "displayName": "灰角兽甲", "aliases": [],
                                   "present": True, "channel": "physical",
                                   "introduction": introduction(entity_kind="monster")}])
    assert result["actors"][0]["memoryEligible"] is False
    assert result["records"] == []
    assert result["characterRegistry"][0]["introduction"]["entityKind"] == "monster"
    assert store.db.total_changes == before


def test_new_host_bound_world_id_is_reused_only_with_first_panel_tags(store):
    handle = open_library_scope(store, actor_ids=["A", "C", "D", "M", "world:npc-17", "world:untagged"])
    result = host_call(store, "resolve_actors", scopeHandle=handle, originId="new-world-panel",
                       firstAppearance={"requestId": "new-world-panel", "panelHash": sha("panel:npc-17")},
                       source={"sourceId": "npc:prose", "text": "闻溪递来一张纸。", "order": 2},
                       proposals=[{"localRef": "npc", "actorId": "world:npc-17", "displayName": "闻溪",
                                   "aliases": [], "present": True, "channel": "physical",
                                   "introduction": introduction()}])
    assert result["actors"][0]["actorId"] == "world:npc-17"
    assert result["actors"][0]["memoryEligible"] is True
    assert result["characterRegistry"][0]["actorId"] == "world:npc-17"
    prose = "闻溪递来一张纸。"
    prose_source = source_record("npc:prose", prose, 2)
    scene = {"kind": "scene", "id": "npc:scene", "order": 2, "text": prose,
             "sourceSpans": [{"sourceId": prose_source["sourceId"], "version": prose_source["version"],
                              "startByte": 0, "endByte": len(prose.encode()),
                              "sha256": prose_source["sha256"]}],
             "metadata": {"characterRegistry": {"schemaVersion": "play-memory-character-registry-v1",
                                                   "entries": result["characterRegistry"]}}}
    accepted = host_call(store, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:2", branchId="main", epoch=1, worldRevision=2,
        predecessorBatchId="batch:1", previousWorldRevision=1,
        records=[prose_source, scene, *result["records"]], characterLibraryPolicy=POLICY))
    assert [actor["actorId"] for actor in accepted["characterLibrary"]["actors"]] == ["M", "world:npc-17"]
    accepted_scope = open_library_scope(store, revision=2, required="batch:2",
                                        actor_ids=["A", "C", "D", "M", "world:npc-17"])
    assert "world:npc-17" in {row["actorId"] for row in
                               host_call(store, "actor_directory", scopeHandle=accepted_scope)["actors"]}
    with pytest.raises(EvidenceError, match="not in this save's visible identity scope"):
        host_call(store, "resolve_actors", scopeHandle=handle, originId="foreign-id",
                  firstAppearance={"requestId": "foreign-id", "panelHash": sha("panel:foreign")},
                  source={"sourceId": "foreign:source", "text": "Foreign actor appears.", "order": 2},
                  proposals=[{"localRef": "foreign", "actorId": "world:foreign", "displayName": "Aster",
                              "aliases": [], "present": True, "channel": "physical",
                              "introduction": introduction()}])

    with pytest.raises(EvidenceError, match="requires first-panel introduction tags"):
        host_call(store, "resolve_actors", scopeHandle=handle, originId="missing-tags",
                  firstAppearance={"requestId": "missing-tags", "panelHash": sha("panel:no-tags")},
                  source={"sourceId": "no-tags:prose", "text": "新角色走进来。", "order": 2},
                  proposals=[{"localRef": "untagged", "actorId": "world:untagged", "displayName": "新角色",
                              "aliases": [], "present": True, "channel": "physical"}])


def test_correction_reuses_only_request_bound_pending_ids_and_prunes_removed_rows(store):
    actor_id, monster_id, unnamed_id = "world:npc-17", "world:monster-8", "world:unnamed-4"
    new_id = "world:new-corrected-actor"
    handle = open_library_scope(store, actor_ids=["A", "C", "D", "M", actor_id, monster_id, unnamed_id, new_id])
    receipt = {"requestId": "corrected-panel", "panelHash": sha("same frozen panel")}
    first = host_call(
        store, "resolve_actors", scopeHandle=handle, originId=receipt["requestId"],
        firstAppearance=receipt,
        source={"sourceId": "first:prose", "text": "奥斯卡，也有人叫他白狼。赤角兽追来，无名人退到门边。", "order": 2},
        proposals=[
            {"localRef": "npc", "actorId": actor_id, "displayName": "奥斯卡", "aliases": ["白狼"],
             "present": True, "channel": "physical", "introduction": introduction()},
            {"localRef": "monster", "actorId": monster_id, "displayName": "赤角兽", "aliases": [],
             "present": True, "channel": "physical", "introduction": introduction(entity_kind="monster")},
            {"localRef": "unnamed", "actorId": unnamed_id, "displayName": "无名人", "aliases": [],
             "present": True, "channel": "physical", "introduction": introduction(proper=False)},
        ])
    assert [row["actorId"] for row in first["characterRegistry"]] == [actor_id, monster_id, unnamed_id]
    assert [row["actorId"] for row in first["records"]] == [actor_id]
    assert first["actors"][1]["memoryEligible"] is False
    assert first["actors"][2]["memoryEligible"] is False

    corrected_receipt = {"requestId": receipt["requestId"], "panelHash": sha("corrected panel B")}
    corrected = host_call(
        store, "resolve_actors", scopeHandle=handle, originId=receipt["requestId"],
        firstAppearance=corrected_receipt, preparedRegistry=first["characterRegistry"],
        source={"sourceId": "corrected:prose", "text": "白狼收起纸条。新来的陶然点头。", "order": 2},
        # The cached Host introduction is deliberately omitted on retry. The
        # display name is a retained alias; profile text stays canonical.
        proposals=[{"localRef": "npc", "actorId": actor_id, "displayName": "白狼", "aliases": [],
                    "present": True, "channel": "physical"},
                   {"localRef": "new", "actorId": new_id, "displayName": "陶然", "aliases": [],
                    "present": True, "channel": "physical", "introduction": introduction()}])
    assert corrected["actors"][0]["actorId"] == actor_id
    assert corrected["actors"][0]["displayName"] == "奥斯卡"
    assert corrected["actors"][0]["memoryEligible"] is True
    assert [row["actorId"] for row in corrected["characterRegistry"]] == [actor_id, new_id]
    assert {row["firstAppearance"]["panelHash"] for row in corrected["characterRegistry"]} == {
        corrected_receipt["panelHash"]}
    assert len(corrected["records"]) == 2
    assert corrected["records"][0]["text"] == "奥斯卡"
    assert corrected["records"][0]["metadata"]["actorDirectory"]["aliases"] == ["白狼"]
    assert corrected["records"][0]["sourceSpans"][0]["sha256"] == sha("白狼")

    with pytest.raises(EvidenceError, match="not bound to this panel request"):
        host_call(store, "resolve_actors", scopeHandle=handle, originId="other-panel",
                  firstAppearance={"requestId": "other-panel", "panelHash": sha("different panel")},
                  preparedRegistry=first["characterRegistry"],
                  source={"sourceId": "other:prose", "text": "白狼再现。", "order": 2},
                  proposals=[{"localRef": "npc", "actorId": actor_id, "displayName": "白狼", "aliases": [],
                              "present": True, "channel": "physical"}])


def test_host_cached_pending_registry_can_resume_after_store_restart(tmp_path):
    path = tmp_path / "pending-restart.sqlite"
    actor_id = "world:pending-actor"
    receipt = {"requestId": "panel-restart", "panelHash": sha("frozen restart panel")}
    first_store = PlayMemoryStore(path, host_token=TOKEN)
    initial = records()
    registry(initial, [registry_entry()])
    host_call(first_store, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:1", branchId="main", epoch=1,
        worldRevision=1, predecessorBatchId=None, records=initial))
    handle = open_library_scope(first_store, actor_ids=["A", "C", "D", "M", actor_id])
    provisional = host_call(first_store, "resolve_actors", scopeHandle=handle,
        originId=receipt["requestId"], firstAppearance=receipt,
        source={"sourceId": "first:pending-prose", "text": "林岚转身，大家叫她白鸽。", "order": 2},
        proposals=[{"localRef": "actor", "actorId": actor_id, "displayName": "林岚", "aliases": ["白鸽"],
                    "present": True, "channel": "physical", "introduction": introduction()}])
    assert provisional["records"]
    assert actor_id not in {row["actorId"] for row in
                            host_call(first_store, "actor_directory", scopeHandle=handle)["actors"]}
    first_store.close()

    restarted = PlayMemoryStore(path, host_token=TOKEN)
    try:
        reopened = open_library_scope(restarted, actor_ids=["A", "C", "D", "M", actor_id])
        before = restarted.db.total_changes
        resumed = host_call(restarted, "resolve_actors", scopeHandle=reopened,
            originId=receipt["requestId"], firstAppearance=receipt,
            preparedRegistry=provisional["characterRegistry"],
            source={"sourceId": "retry:pending-prose", "text": "白鸽收下信封。", "order": 2},
            proposals=[{"localRef": "actor", "actorId": actor_id, "displayName": "白鸽", "aliases": [],
                        "present": True, "channel": "physical"}])
        assert restarted.db.total_changes == before
        assert resumed["actors"][0]["actorId"] == actor_id
        assert resumed["actors"][0]["displayName"] == "林岚"
        assert resumed["records"][0]["text"] == "林岚"
        assert resumed["records"][0]["knownBy"] == [actor_id]
        assert resumed["records"][0]["sourceSpans"][0]["sha256"] == sha("白鸽")
    finally:
        restarted.close()


def test_explicit_new_id_found_in_an_old_reference_still_requires_host_binding(store):
    text = "Unbound actor appears in a relationship target."
    source = source_record("unbound:source", text, 1)
    relationship = {"kind": "relationship", "id": "unbound:relationship", "order": 1,
                    "fromActorId": "A", "toActorId": "world:unbound", "text": "A remembers an unknown figure.",
                    "knownBy": ["A"], "sourceSpans": [{"sourceId": source["sourceId"],
                    "version": source["version"], "startByte": 0, "endByte": len(text.encode()),
                    "sha256": source["sha256"]}]}
    host_call(store, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:2", branchId="main", epoch=1, worldRevision=2,
        predecessorBatchId="batch:1", previousWorldRevision=1, records=[source, relationship]))
    handle = host_call(store, "open_scope", binding=binding(
        worldRevision=2, requiredBatchId="batch:2", lineage=[{"branchId": "main", "throughRevision": 2}],
        characterLibraryPolicy=POLICY)) ["scopeHandle"]
    with pytest.raises(EvidenceError, match="visible identity scope"):
        host_call(store, "resolve_actors", scopeHandle=handle, originId="unbound-panel",
                  firstAppearance={"requestId": "unbound-panel", "panelHash": sha("panel:unbound")},
                  source={"sourceId": "later:prose", "text": "New person walks in.", "order": 2},
                  proposals=[{"localRef": "person", "actorId": "world:unbound", "displayName": "New person",
                              "aliases": [], "present": True, "channel": "physical",
                              "introduction": introduction()}])


def test_delete_is_append_only_and_repeated_panel_cannot_restore_saved_status(store):
    entry = registry_entry()
    deletion_text = "User removed Merlin from the saved character library."
    deletion_source = source_record("delete:source", deletion_text, 2, metadata={"authored": True})
    delete_span = {"sourceId": deletion_source["sourceId"], "version": deletion_source["version"],
                   "startByte": 0, "endByte": len(deletion_text.encode()), "sha256": deletion_source["sha256"]}
    delete = {"kind": "character-library", "id": "delete:M", "order": 2, "actorId": "M",
              "status": "deleted", "sourceSpans": [delete_span], "metadata": {"authored": True}}
    host_call(store, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:2", branchId="main", epoch=1, worldRevision=2,
        predecessorBatchId="batch:1", previousWorldRevision=1, records=[deletion_source, delete],
        characterLibraryPolicy=POLICY))
    handle = open_library_scope(store, revision=2, required="batch:2")
    assert host_call(store, "actor_directory", scopeHandle=handle)["actors"] == []
    legacy_binding = binding(worldRevision=2, requiredBatchId="batch:2",
                             lineage=[{"branchId": "main", "throughRevision": 2}],
                             actorIds=["A", "C", "D", "M"], grants=["delete:M"])
    legacy_scope = host_call(store, "open_scope", binding=legacy_binding)["scopeHandle"]
    assert "M" not in {row["actorId"] for row in
                       host_call(store, "actor_directory", scopeHandle=legacy_scope)["actors"]}
    assert all(row["id"] != "delete:M" for row in
               host_call(store, "search", scopeHandle=legacy_scope, query="removed", limit=10)["candidates"])
    with pytest.raises(EvidenceError, match="deleted or excluded"):
        host_call(store, "actor_packs", scopeHandle=legacy_scope, actors=["M"], references=[], budgetBytes=4096)
    resolved = host_call(store, "resolve_actors", scopeHandle=handle, originId="later-panel",
                         firstAppearance={"requestId": "later-panel", "panelHash": sha("panel:later")},
                         source={"sourceId": "later:prose", "text": "梅林再次出现。", "order": 3},
                         proposals=[{"localRef": "mage", "actorId": "M", "displayName": "梅林", "aliases": [],
                                     "present": True, "channel": "physical", "introduction": entry["introduction"]}])
    assert resolved["actors"][0]["actorId"] == "M"
    assert resolved["actors"][0]["memoryEligible"] is False
    assert resolved["records"] == []
    # Duplicate accepted first appearance is idempotent with respect to deletion.
    repeated_scene_text = "A panel records Merlin's return."
    repeated_source = source_record("repeat:source", repeated_scene_text, 3)
    repeated_scene = {"kind": "scene", "id": "repeat:scene", "order": 3,
                      "text": repeated_scene_text,
                      "sourceSpans": [{"sourceId": repeated_source["sourceId"],
                                       "version": repeated_source["version"], "startByte": 0,
                                       "endByte": len(repeated_scene_text.encode()),
                                       "sha256": repeated_source["sha256"]}],
                      "metadata": {"characterRegistry": {"schemaVersion": "play-memory-character-registry-v1",
                                                           "entries": [entry]}}}
    repeated = [repeated_source, repeated_scene]
    host_call(store, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:3", branchId="main", epoch=1, worldRevision=3,
        predecessorBatchId="batch:2", previousWorldRevision=2, records=repeated,
        characterLibraryPolicy=POLICY))
    receipt = host_call(store, "receipt", campaignId="camp", batchId="batch:3")["receipt"]
    assert receipt["characterLibrary"]["actors"] == []


def test_deleted_and_excluded_alias_updates_remain_resolvable_without_personal_memory():
    local = PlayMemoryStore(":memory:", host_token=TOKEN)
    try:
        def apply_batch(batch_id, revision, rows, predecessor, previous=None, *, lifecycle=True):
            kwargs = {"characterLibraryPolicy": POLICY} if lifecycle else {}
            if previous is not None:
                kwargs["previousWorldRevision"] = previous
            return host_call(local, "apply_batch", batch=sealed_batch(
                campaignId="camp", batchId=batch_id, branchId="main", epoch=1,
                worldRevision=revision, predecessorBatchId=predecessor, records=rows, **kwargs))

        initial = records()
        eileen = registry_entry("M", "艾琳", request="f1")
        monster = registry_entry("X", "赤角兽", intro=introduction(entity_kind="monster"), request="f1")
        registry(initial, [eileen, monster])
        apply_batch("batch:1", 1, initial, predecessor=None, lifecycle=False)

        deletion_text = "用户从保存角色库删除艾琳。"
        deletion_source = source_record("delete:eileen:source", deletion_text, 2,
                                        metadata={"authored": True})
        deletion = {"kind": "character-library", "id": "delete:M", "order": 2, "actorId": "M",
                    "status": "deleted", "sourceSpans": [{"sourceId": deletion_source["sourceId"],
                    "version": deletion_source["version"], "startByte": 0,
                    "endByte": len(deletion_text.encode("utf-8")), "sha256": deletion_source["sha256"]}],
                    "metadata": {"authored": True}}
        apply_batch("batch:2", 2, [deletion_source, deletion], predecessor="batch:1", previous=1)
        handle = open_library_scope(local, revision=2, required="batch:2", actor_ids=["A", "C", "D", "M", "X"])
        assert host_call(local, "actor_directory", scopeHandle=handle)["actors"] == []

        alias_text = "小鸢把茶递给赤角兽甲。"
        alias_source = source_record("alias:update:source", alias_text, 3)
        current_receipt = {"requestId": "alias-update", "panelHash": sha("panel:alias-update")}
        updated = host_call(local, "resolve_actors", scopeHandle=handle, originId="alias-update",
                            firstAppearance=current_receipt,
                            source={"sourceId": alias_source["sourceId"], "text": alias_text, "order": 3},
                            proposals=[
                                {"localRef": "eileen", "actorId": "M", "displayName": "小鸢", "aliases": [],
                                 "present": True, "channel": "physical"},
                                {"localRef": "monster", "actorId": "X", "displayName": "赤角兽甲", "aliases": [],
                                 "present": True, "channel": "physical"},
                            ])
        assert {row["actorId"]: row["memoryEligible"] for row in updated["actors"]} == {"M": False, "X": False}
        assert updated["records"] == []
        assert {entry["actorId"] for entry in updated["characterRegistry"]} == {"M", "X"}
        entries = {entry["actorId"]: entry for entry in updated["characterRegistry"]}
        assert entries["M"]["canonicalName"] == "艾琳"
        assert entries["M"]["aliases"] == ["小鸢"]
        assert entries["M"]["firstAppearance"] == eileen["firstAppearance"]
        assert entries["X"]["canonicalName"] == "赤角兽"
        assert entries["X"]["aliases"] == ["赤角兽甲"]
        assert entries["X"]["firstAppearance"] == monster["firstAppearance"]

        scene = {"kind": "scene", "id": "alias:update:scene", "order": 3, "text": alias_text,
                 "sourceSpans": [{"sourceId": alias_source["sourceId"], "version": alias_source["version"],
                                  "startByte": 0, "endByte": len(alias_text.encode("utf-8")),
                                  "sha256": alias_source["sha256"]}],
                 "metadata": {"characterRegistry": {"schemaVersion": "play-memory-character-registry-v1",
                                                       "entries": updated["characterRegistry"]}}}
        accepted = apply_batch("batch:3", 3, [alias_source, scene], predecessor="batch:2", previous=2)
        assert accepted["characterLibrary"]["actors"] == []
        assert host_call(local, "actor_directory", scopeHandle=open_library_scope(
            local, revision=3, required="batch:3", actor_ids=["A", "C", "D", "M", "X"]))["actors"] == []

        followup_handle = open_library_scope(local, revision=3, required="batch:3",
                                             actor_ids=["A", "C", "D", "M", "X"])
        followup_text = "小鸢把信递给赤角兽甲。"
        followed = host_call(local, "resolve_actors", scopeHandle=followup_handle, originId="alias-followup",
                             firstAppearance={"requestId": "alias-followup",
                                              "panelHash": sha("panel:alias-followup")},
                             source={"sourceId": "alias:followup:source", "text": followup_text, "order": 4},
                             proposals=[
                                 {"localRef": "eileen", "displayName": "小鸢", "aliases": [],
                                  "present": True, "channel": "physical"},
                                 {"localRef": "monster", "displayName": "赤角兽甲", "aliases": [],
                                  "present": True, "channel": "physical"},
                             ])
        assert {row["localRef"]: row["actorId"] for row in followed["actors"]} == {"eileen": "M", "monster": "X"}
        assert all(row["memoryEligible"] is False for row in followed["actors"])
        assert followed["records"] == []
        assert followed["characterRegistry"] == []
    finally:
        local.close()


def test_deleted_owner_updates_are_rejected_but_saved_actor_facts_about_deleted_target_remain(store):
    text = "梅林已从保存列表移除；奎因仍记录他们的约定。"
    digest = sha(text)
    source = source_record("edit:source", text, 2)
    status = {"kind": "character-library", "id": "delete:M", "order": 2, "actorId": "M",
              "status": "deleted", "sourceSpans": [{"sourceId": source["sourceId"], "version": digest,
              "startByte": 0, "endByte": len(text.encode()), "sha256": digest}]}
    host_call(store, "apply_batch", batch=sealed_batch(
        campaignId="camp", batchId="batch:2", branchId="main", epoch=1, worldRevision=2,
        predecessorBatchId="batch:1", previousWorldRevision=1, records=[source, status],
        characterLibraryPolicy=POLICY))
    memory_source = source_record("later:source", "奎因记得梅林的约定。", 3)
    source_text = memory_source["text"]
    event = {"kind": "event", "id": "later:event", "order": 3, "sceneId": "scene:1",
             "text": "A promise remains recorded.", "sourceSpans": [{"sourceId": memory_source["sourceId"],
             "version": memory_source["version"], "startByte": 0, "endByte": len(source_text.encode()),
             "sha256": sha(source_text)}]}
    relationship = {"kind": "relationship", "id": "Q-to-M", "order": 3, "fromActorId": "Q",
                    "toActorId": "M", "text": "Quinn remembers the promise with Merlin.", "knownBy": ["Q"],
                    "sourceSpans": [{"sourceId": memory_source["sourceId"], "version": memory_source["version"],
                    "startByte": 0, "endByte": len(source_text.encode()), "sha256": sha(source_text)}]}
    # Q is intentionally a legacy, unclassified owner; omit the opt-in batch flag.
    saved_fact_batch = sealed_batch(campaignId="camp", batchId="batch:3", branchId="main", epoch=1,
                                   worldRevision=3, predecessorBatchId="batch:2", previousWorldRevision=2,
                                   records=[memory_source, event, relationship])
    host_call(store, "apply_batch", batch=saved_fact_batch)
    bad = profile_record("M", "梅林", 4, "bad:source", "梅林说话。")
    bad_source = source_record("bad:source", "梅林说话。", 4)
    with pytest.raises(EvidenceError, match="not allowed"):
        host_call(store, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:4", branchId="main", epoch=1, worldRevision=4,
            predecessorBatchId="batch:3", previousWorldRevision=3, records=[bad_source, bad]))


def test_legacy_unclassified_records_remain_legacy_without_opt_in_and_imports_are_host_authored_only(store):
    source_text = "店主收下账簿。"
    legacy_source = source_record("legacy:source", source_text, 2)
    legacy_name = profile_record("legacy", "店主", 2, "legacy:source", source_text)
    batch = sealed_batch(campaignId="camp", batchId="batch:2", branchId="main", epoch=1,
                         worldRevision=2, predecessorBatchId="batch:1", previousWorldRevision=1,
                         records=[legacy_source, legacy_name])
    host_call(store, "apply_batch", batch=batch)
    legacy_scope = host_call(store, "open_scope", binding=binding(
        worldRevision=2, requiredBatchId="batch:2", actorIds=["A", "C", "D", "legacy"],
        lineage=[{"branchId": "main", "throughRevision": 2}]))["scopeHandle"]
    assert any(row["actorId"] == "legacy" for row in host_call(store, "actor_directory", scopeHandle=legacy_scope)["actors"])
    strict_scope = open_library_scope(store, revision=2, required="batch:2", actor_ids=["A", "C", "D", "legacy"])
    before = store.db.total_changes
    repeated = host_call(store, "resolve_actors", scopeHandle=strict_scope, originId="legacy-panel",
                         firstAppearance={"requestId": "legacy-panel", "panelHash": sha("legacy panel")},
                         source={"sourceId": "legacy:later", "text": "店主再次出现。", "order": 3},
                         proposals=[{"localRef": "owner", "actorId": "legacy", "displayName": "店主",
                                     "aliases": [], "present": True, "channel": "physical",
                                     "introduction": introduction()}])
    assert store.db.total_changes == before
    assert repeated["actors"][0]["actorId"] == "legacy"
    assert repeated["actors"][0]["displayName"] == "店主"
    assert repeated["actors"][0]["memoryEligible"] is False
    assert repeated["actors"][0]["memoryDisposition"] == "legacy-unclassified"
    assert repeated["records"] == []
    assert repeated["characterRegistry"] == []
    # Source import is a backfill seam, but the source row must carry explicit Host authorship.
    import_entry = registry_entry("legacy", "店主", intro=introduction(), basis="legacy-name-profile")
    import_text = "Approved legacy identity classification."
    imported_source = source_record("import:source", import_text, 3,
                                    metadata={"authored": True, "characterRegistryImport": True,
                                              "characterRegistry": {"schemaVersion": "play-memory-character-registry-v1",
                                                                    "entries": [{**import_entry,
                                                                                 "firstAppearance": {
                                                                                     "requestId": "f1",
                                                                                     "panelHash": sha("original legacy profile panel"),
                                                                                     "basis": "legacy-name-profile"}}]}})
    assert imported_source["sha256"] != imported_source["metadata"]["characterRegistry"]["entries"][0]["firstAppearance"]["panelHash"]
    imported_batch = sealed_batch(campaignId="camp", batchId="batch:3", branchId="main", epoch=1,
                                  worldRevision=3, previousWorldRevision=2, predecessorBatchId="batch:2",
                                  records=[imported_source], characterLibraryPolicy=POLICY)
    validate(imported_batch, SCHEMA["$defs"]["batch"])
    imported_receipt = host_call(store, "apply_batch", batch=imported_batch)
    assert "legacy" in {row["actorId"] for row in imported_receipt["characterLibrary"]["actors"]}
    imported_scope = open_library_scope(store, revision=3, required="batch:3")
    assert {row["actorId"] for row in host_call(store, "actor_directory", scopeHandle=imported_scope)["actors"]} == {"M", "legacy"}
    invalid_hash_source = copy.deepcopy(imported_source)
    invalid_hash_source["id"] = "import:bad-hash"
    invalid_hash_source["sourceId"] = "import:bad-hash"
    invalid_hash_source["text"] = "Tampered authored identity classification."
    with pytest.raises(EvidenceError, match="source content hash mismatch"):
        host_call(store, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:4:bad-hash", branchId="main", epoch=1, worldRevision=4,
            previousWorldRevision=3, predecessorBatchId="batch:3", records=[invalid_hash_source],
            characterLibraryPolicy=POLICY))

    invalid_source = copy.deepcopy(imported_source)
    invalid_source["id"] = "import:bad-authorship"
    invalid_source["sourceId"] = "import:bad-authorship"
    invalid_source["metadata"].pop("authored")
    invalid_source["metadata"].pop("characterRegistryImport")
    with pytest.raises(EvidenceError, match="Host-authored"):
        host_call(store, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:4:bad-authorship", branchId="main", epoch=1, worldRevision=4,
            previousWorldRevision=3, predecessorBatchId="batch:3", records=[invalid_source],
            characterLibraryPolicy=POLICY))


@pytest.mark.parametrize("basis", ["legacy-name-profile", "static-profile"])
def test_scene_alias_upsert_preserves_known_non_panel_first_appearance(basis):
    original = registry_entry("M", "艾琳", request="import-first", basis=basis)
    source = source_record("import:registry", "Explicit Host identity import.", 1, metadata={
        "authored": True,
        "characterRegistryImport": True,
        "characterRegistry": {"schemaVersion": "play-memory-character-registry-v1", "entries": [original]},
    })
    update = {**original, "aliases": ["小鸢"]}
    scene = {"kind": "scene", "id": "alias:scene", "metadata": {
        "characterRegistry": {"schemaVersion": "play-memory-character-registry-v1", "entries": [update]},
    }}

    merged = character_registry([source, scene])
    assert merged["M"]["canonicalName"] == "艾琳"
    assert merged["M"]["aliases"] == ["小鸢"]
    assert merged["M"]["firstAppearance"] == original["firstAppearance"]

    with pytest.raises(EvidenceError, match="only for an accepted identity"):
        character_registry([scene])

    removed_alias = {**original, "aliases": []}
    removed_scene = {"kind": "scene", "id": "alias:remove", "metadata": {
        "characterRegistry": {"schemaVersion": "play-memory-character-registry-v1", "entries": [removed_alias]},
    }}
    with pytest.raises(EvidenceError, match="aliases may only be added"):
        character_registry([source, scene, removed_scene])

    changed_first = {**update, "firstAppearance": {**original["firstAppearance"], "requestId": "different"}}
    changed_scene = {"kind": "scene", "id": "alias:changed-first", "metadata": {
        "characterRegistry": {"schemaVersion": "play-memory-character-registry-v1", "entries": [changed_first]},
    }}
    with pytest.raises(EvidenceError, match="conflicting first-appearance"):
        character_registry([source, scene, changed_scene])


def test_fork_cutoff_and_restarted_store_keep_the_saved_projection_and_deletion_state(tmp_path):
    path = tmp_path / "lineage.sqlite"
    first = PlayMemoryStore(path, host_token=TOKEN)
    try:
        initial = records()
        registry(initial, [registry_entry()])
        receipt1 = host_call(first, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:1", branchId="main", epoch=1,
            worldRevision=1, predecessorBatchId=None, records=initial))
        child_batch = sealed_batch(campaignId="camp", batchId="child:1", branchId="child", epoch=1,
                                  worldRevision=1, previousWorldRevision=0, predecessorBatchId="batch:1",
                                  parent={"branchId": "main", "batchId": "batch:1", "throughRevision": 1},
                                  records=[], characterLibraryPolicy=POLICY)
        child_receipt = host_call(first, "apply_batch", batch=child_batch)
        text = "The owner removed Merlin from the saved library."
        source = source_record("delete:source", text, 2, metadata={"authored": True})
        status = {"kind": "character-library", "id": "delete:M", "order": 2, "actorId": "M",
                  "status": "deleted", "sourceSpans": [{"sourceId": source["sourceId"],
                  "version": source["version"], "startByte": 0, "endByte": len(text.encode()),
                  "sha256": source["sha256"]}]}
        receipt2 = host_call(first, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:2", branchId="main", epoch=1,
            worldRevision=2, previousWorldRevision=1, predecessorBatchId="batch:1",
            records=[source, status]))
        assert [row["actorId"] for row in receipt1["characterLibrary"]["actors"]] == ["M"]
        assert [row["actorId"] for row in child_receipt["characterLibrary"]["actors"]] == ["M"]
        assert receipt2["characterLibrary"]["actors"] == []
    finally:
        first.close()

    restarted = PlayMemoryStore(path, host_token=TOKEN)
    try:
        stored = host_call(restarted, "receipt", campaignId="camp", batchId="batch:2")["receipt"]
        assert stored["characterLibrary"]["actors"] == []
        assert host_call(restarted, "apply_batch", batch=sealed_batch(
            campaignId="camp", batchId="batch:2", branchId="main", epoch=1,
            worldRevision=2, previousWorldRevision=1, predecessorBatchId="batch:1",
            records=[source, status])) == stored
        child_binding = binding(branchId="child", worldRevision=1, requiredBatchId="child:1",
                                lineage=[{"branchId": "child", "throughRevision": 1},
                                         {"branchId": "main", "throughRevision": 1}],
                                characterLibraryPolicy=POLICY)
        child_scope = host_call(restarted, "open_scope", binding=child_binding)["scopeHandle"]
        assert [row["actorId"] for row in host_call(restarted, "actor_directory", scopeHandle=child_scope)["actors"]] == ["M"]
        deleted_binding = binding(worldRevision=2, requiredBatchId="batch:2",
                                  lineage=[{"branchId": "main", "throughRevision": 2}],
                                  characterLibraryPolicy=POLICY, actorIds=["A", "C", "D", "M"])
        deleted_scope = host_call(restarted, "open_scope", binding=deleted_binding)["scopeHandle"]
        assert host_call(restarted, "actor_directory", scopeHandle=deleted_scope)["actors"] == []
        with pytest.raises(EvidenceError, match="deleted or excluded"):
            host_call(restarted, "actor_packs", scopeHandle=deleted_scope, actors=["M"],
                      references=[], budgetBytes=4096)
    finally:
        restarted.close()
