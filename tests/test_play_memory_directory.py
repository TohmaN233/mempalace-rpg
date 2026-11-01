import hashlib

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from mempalace_rpg.play_memory import PlayMemoryStore
from mempalace_rpg.play_memory_protocol import SCHEMA, validate
from mempalace_rpg.play_memory_directory import actor_directory, resolve_actors
from mempalace_rpg.play_memory_characters import CharacterImportancePolicy
from test_play_memory import TOKEN, batch, binding, call, scope


TEXT = "梅林在托马脑中说出秘密。车夫在旁整理面粉。"


def test_accepted_identity_can_participate_in_second_person_prose_without_a_new_name_grant():
    profile = {"kind": "profile", "id": "player:name", "order": 1, "actorId": "player",
               "field": "name", "text": "托马", "knownBy": ["player"]}
    scoped = {"effective": [profile], "binding": {"actorIds": ["player"], "purpose": "director"}}
    result = resolve_actors(scoped, {"originId": "floor2", "source": {
        "sourceId": "floor2:prose", "text": "你走到车尾，伸手稳住横木。", "order": 2},
        "proposals": [{"localRef": "toma", "actorId": "player", "displayName": "托马", "aliases": [],
                       "present": True, "channel": "physical"}]}, CharacterImportancePolicy())
    assert result["actors"][0]["actorId"] == "player"
    assert result["actors"][0]["isNew"] is False
    assert result["records"] == []


def test_host_role_and_binding_survive_identity_alias_changes_without_expanding_source_access():
    binding_identity = {"kind": "fated-selected-core-v1", "ownerActorId": "player", "coreId": "reviewed-core",
                        "definitionHash": "definition", "selectionHash": "selection", "source": {"uid": 415}}
    profile = {"kind": "profile", "id": "core:name", "order": 1, "actorId": "core",
               "field": "name", "text": "梅林", "knownBy": ["core"], "metadata": {"actorDirectory": {
                   "channel": "mental", "aliases": [], "role": "critical", "coreBinding": binding_identity}}}
    scoped = {"effective": [profile], "binding": {"actorIds": ["core"], "purpose": "director"}}
    assert actor_directory(scoped, CharacterImportancePolicy())[0]["importance"]["tier"] == "major"
    result = resolve_actors(scoped, {"originId": "floor2", "source": {"sourceId": "floor2", "text": TEXT, "order": 2},
        "proposals": [{"localRef": "core", "actorId": "core", "displayName": "梅林", "aliases": ["花之魔术师"],
                       "present": True, "channel": "mental", "quote": "梅林"}]}, CharacterImportancePolicy())
    updated = result["records"][0]
    assert updated["metadata"]["actorDirectory"]["role"] == "critical"
    assert updated["metadata"]["actorDirectory"]["coreBinding"] == binding_identity
    assert updated["knownBy"] == ["core"]
    position = updated["sourceSpans"][0]
    assert TEXT.encode()[position["startByte"]:position["endByte"]].decode() == "梅林"


def test_latest_complete_roster_clears_unmentioned_old_presence_without_knowledge_records():
    records = [
        {"kind": "profile", "id": "carter:name", "order": 1, "actorId": "carter",
         "field": "name", "text": "车夫", "knownBy": ["carter"]},
        {"kind": "state", "id": "carter:present", "order": 1, "actorId": "carter",
         "field": "memory.presence", "text": '{"present":true,"channel":"physical"}', "knownBy": ["carter"]},
        {"kind": "scene", "id": "scene:2", "order": 2, "metadata": {"actorRoster": [
            {"actorId": "player", "present": True, "channel": "physical"},
        ]}},
    ]
    scoped = {"effective": records, "binding": {"actorIds": ["player", "carter"], "purpose": "director"}}
    directory = actor_directory(scoped, CharacterImportancePolicy())
    assert next(a for a in directory if a["actorId"] == "carter")["present"] is False
    assert next(a for a in directory if a["actorId"] == "player")["present"] is True
    assert len(records) == 3  # No fake absence memory/source access is introduced.
    own = {**scoped, "binding": {"actorIds": ["carter"], "purpose": "actor-knowledge"}}
    assert [a["actorId"] for a in actor_directory(own, CharacterImportancePolicy())] == ["carter"]


def resolve(store, handle, *, proposal=None, origin="floor2"):
    return store.dispatch("resolve_actors", {"hostToken": TOKEN, "scopeHandle": handle,
        "originId": origin, "source": {"sourceId": "floor2:prose", "text": TEXT, "order": 3},
        "proposals": [proposal or {"localRef": "carter", "displayName": "车夫", "aliases": [],
                                  "present": True, "channel": "physical", "quote": TEXT}]})


@pytest.fixture
def store(tmp_path):
    result = PlayMemoryStore(tmp_path / "directory.sqlite", host_token=TOKEN)
    call(result, "apply_batch", batch=batch())
    yield result
    result.close()


def test_automatic_identity_preparation_is_replayable_without_writing_or_name_merging(store):
    handle = scope(store)
    before = store.db.total_changes
    prepared = resolve(store, handle)
    validate(prepared, SCHEMA["$defs"]["resolveActorsResult"])
    assert store.db.total_changes == before
    assert resolve(store, handle) == prepared
    assert resolve(store, handle, origin="different-scene")["actors"][0]["actorId"] != prepared["actors"][0]["actorId"]
    assert prepared["actors"][0]["isNew"] is True
    assert {r["kind"] for r in prepared["records"]} == {"profile"}
    assert not any(r.get("field") == "memory.presence" for r in prepared["records"])
    assert all(r["knownBy"] == [prepared["actors"][0]["actorId"]] for r in prepared["records"])
    # Even an excessively broad model quote cannot grant the private paragraph.
    for row in prepared["records"]:
        span = row["sourceSpans"][0]
        assert TEXT.encode()[span["startByte"]:span["endByte"]].decode() == "车夫"


def test_committed_directory_is_ambient_and_existing_id_survives_alias_update(store):
    prepared = resolve(store, scope(store))
    source = {"kind": "source", "id": "floor2:prose", "sourceId": "floor2:prose", "order": 3,
              "text": TEXT, "version": hashlib.sha256(TEXT.encode()).hexdigest(),
              "sha256": hashlib.sha256(TEXT.encode()).hexdigest()}
    call(store, "apply_batch", batch=batch("batch:2", 2, [source, *prepared["records"]], "batch:1"))
    handle = scope(store, worldRevision=2, requiredBatchId="batch:2",
                   lineage=[{"branchId": "main", "throughRevision": 2}])
    directory_result = store.dispatch("actor_directory", {"hostToken": TOKEN, "scopeHandle": handle})
    validate(directory_result, SCHEMA["$defs"]["actorDirectoryResult"])
    directory = directory_result["actors"]
    actor = next(row for row in directory if row["actorId"] == prepared["actors"][0]["actorId"])
    assert actor["displayName"] == "车夫"
    assert actor["importance"]["tier"] == "ambient"
    past = store.dispatch("actor_directory", {"hostToken": TOKEN, "scopeHandle": scope(store)})["actors"]
    assert all(row["actorId"] != actor["actorId"] for row in past)
    actor_scope = scope(store, worldRevision=2, requiredBatchId="batch:2",
                        lineage=[{"branchId": "main", "throughRevision": 2}],
                        purpose="actor-knowledge", actorIds=[actor["actorId"]], grants=[])
    own_records = store._actor_records(store._scope(actor_scope), actor["actorId"])
    for row in own_records:
        assert "秘密" not in store._original(store._scope(actor_scope), row)
    proposal = {"localRef": "returning-carter", "actorId": actor["actorId"], "displayName": "车夫",
                "aliases": ["运面粉的车夫"], "present": True, "channel": "physical"}
    assert resolve(store, handle, proposal=proposal)["actors"][0]["actorId"] == actor["actorId"]
    assert resolve(store, handle, proposal=proposal)["actors"][0]["isNew"] is False


def test_actor_reader_cannot_introduce_id_and_unanchored_absent_label_is_deferred(store, caplog):
    handle = scope(store, purpose="actor-knowledge", actorIds=["C"])
    with pytest.raises(EvidenceError, match="cannot introduce"):
        resolve(store, handle)
    handle = scope(store)
    invalid = {"localRef": "missing", "displayName": "未出场的人", "aliases": [],
               "present": False, "channel": "mental"}
    with caplog.at_level("WARNING", logger="mempalace_rpg.play_memory_directory"):
        result = resolve(store, handle, proposal=invalid)
    assert result == {"actors": [], "records": []}
    assert "unanchored-absent-label" in caplog.text


def test_descriptive_identity_label_is_not_a_name_or_private_source_grant(store, caplog):
    proposal = {"localRef": "receiver", "displayName": "收货负责人", "aliases": [],
                "present": True, "channel": "physical", "quote": "收货负责人走到门口"}
    with caplog.at_level("WARNING", logger="mempalace_rpg.play_memory_directory"):
        prepared = resolve(store, scope(store), proposal=proposal)
    identity = prepared["records"][0]
    assert identity["text"] == "收货负责人"
    assert identity["knownBy"] == []
    assert identity["metadata"]["actorDirectory"]["sourceContextOnly"] is True
    assert "no-knowledge-grant" in caplog.text
    source = {"kind": "source", "id": "floor2:prose", "sourceId": "floor2:prose", "order": 3,
              "text": TEXT, "version": hashlib.sha256(TEXT.encode()).hexdigest(),
              "sha256": hashlib.sha256(TEXT.encode()).hexdigest()}
    call(store, "apply_batch", batch=batch("batch:2", 2, [source, identity], "batch:1"))
    actor_id = prepared["actors"][0]["actorId"]
    own = store._scope(scope(store, purpose="actor-knowledge", actorIds=[actor_id], worldRevision=2,
                            requiredBatchId="batch:2", lineage=[{"branchId": "main", "throughRevision": 2}], grants=[]))
    assert store._actor_records(own, actor_id) == []


def test_identity_quote_typo_rebinds_only_exact_name_bytes_and_is_observable(store, caplog):
    proposal = {"localRef": "carter", "displayName": "车夫", "aliases": [],
                "present": True, "channel": "physical", "quote": "车夫整理着面粉。"}
    with caplog.at_level("INFO", logger="mempalace_rpg.play_memory_directory"):
        result = resolve(store, scope(store), proposal=proposal)
    assert "exact-literal-name" in caplog.text
    assert result["actors"][0]["displayName"] == "车夫"
    for row in result["records"]:
        span = row["sourceSpans"][0]
        assert TEXT.encode()[span["startByte"]:span["endByte"]].decode() == "车夫"


def test_departure_roster_roundtrip_does_not_authorize_private_next_scene(store):
    prepared = resolve(store, scope(store))
    actor_id = prepared["actors"][0]["actorId"]
    source = {"kind": "source", "id": "floor2:prose", "sourceId": "floor2:prose", "order": 3,
              "text": TEXT, "version": hashlib.sha256(TEXT.encode()).hexdigest(),
              "sha256": hashlib.sha256(TEXT.encode()).hexdigest()}
    source_span = {"sourceId": "floor2:prose", "version": source["version"],
                   "startByte": 0, "endByte": len(TEXT.encode()), "sha256": source["sha256"]}
    initial_scene = {"kind": "scene", "id": "floor2:scene", "order": 3, "text": TEXT,
                     "sourceSpans": [source_span], "metadata": {"actorRoster": [
                         {"actorId": actor_id, "present": True, "channel": "physical"}]}}
    call(store, "apply_batch", batch=batch("batch:2", 2, [source, initial_scene, *prepared["records"]], "batch:1"))
    initial_scope = scope(store, worldRevision=2, requiredBatchId="batch:2",
                          lineage=[{"branchId": "main", "throughRevision": 2}])
    initial_directory = store.dispatch("actor_directory", {"hostToken": TOKEN, "scopeHandle": initial_scope})["actors"]
    assert next(a for a in initial_directory if a["actorId"] == actor_id)["present"] is True
    private = "A独自进入房间，在心中想起只有自己知道的秘密。"
    digest = hashlib.sha256(private.encode()).hexdigest()
    private_source = {"kind": "source", "id": "floor3:prose", "sourceId": "floor3:prose",
                      "order": 4, "text": private, "version": digest, "sha256": digest}
    scene = {"kind": "scene", "id": "floor3:scene", "order": 4, "text": private,
             "sourceSpans": [{"sourceId": "floor3:prose", "version": digest,
                              "startByte": 0, "endByte": len(private.encode()), "sha256": digest}],
             "metadata": {"actorRoster": [{"actorId": "A", "present": True, "channel": "physical"}]}}
    call(store, "apply_batch", batch=batch("batch:3", 3, [private_source, scene], "batch:2"))
    settings = {"worldRevision": 3, "requiredBatchId": "batch:3",
                "lineage": [{"branchId": "main", "throughRevision": 3}]}
    handle = scope(store, **settings)
    actors = store.dispatch("actor_directory", {"hostToken": TOKEN, "scopeHandle": handle})["actors"]
    assert next(a for a in actors if a["actorId"] == actor_id)["present"] is False
    own = store._scope(scope(store, **settings, purpose="actor-knowledge", actorIds=[actor_id], grants=[]))
    for record in store._actor_records(own, actor_id):
        assert "只有自己知道的秘密" not in store._original(own, record)
