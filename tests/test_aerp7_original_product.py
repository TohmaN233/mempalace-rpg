import copy
import hashlib
import json
from pathlib import Path

import pytest

from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_executor as executor
from benchmarks import aerp7_original_product as original


def h(value):
    return hashlib.sha256(value.encode()).hexdigest()


def projection():
    selection = {
        "algorithm": "hmac-sha256-revision-bound-persona-group-tier-context-v1", "seed": 1,
        "persona_quota": 1, "per_persona_group_quota": 1, "context_rank_indices": [0],
        "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": h("selected"),
        "holdout_persona_set_sha256": h("holdout"), "group_values_sha256": h("groups"),
        "tier_values_sha256": h("tiers"), "context_values_sha256": h("contexts"),
        "desired_context_values_sha256": h("desired"), "variant_selection_sha256": h("variants"),
        "selected_item_context_count": 2, "item_supplement_count": 0,
        "exclusion_counts": {name: 0 for name in ("multi_persona_cases", "missing_crosswalk", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes")},
        "quarantine_reason_digests": {name: h(name) for name in ("multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes")},
        "quarantine_ledger_sha256": h("ledger"),
    }
    corpora = []
    for corpus_number in range(2):
        corpus_id, conversation = h(f"corpus-{corpus_number}"), h(f"conversation-{corpus_number}")
        candidates = [{"message_id": h(f"{corpus_number}-m-{index}"), "opaque_conversation_id": conversation, "conversation_order": 0, "message_order": index, "corpus_order": index, "speaker": "user" if index % 2 else "assistant", "text": f"corpus {corpus_number} text {index}"} for index in range(11)]
        corpora.append({"corpus_id": corpus_id, "declared_context_size": 2, "actual_conversation_count": 1, "actual_message_count": len(candidates), "candidates": candidates})
    return {"schema": "aerp7-convomem-candidate-projection-v3", "dataset": {name: h(name) for name in ("canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256")}, "selection_receipt": selection, "corpora": corpora, "items": [{"item_id": h(f"item-{number}"), "persona_id": h("persona"), "query_text": f"query {number}", "corpus_id": corpus["corpus_id"]} for number, corpus in enumerate(corpora)]}


class Collection:
    def __init__(self, owner): self.owner = owner

    def upsert(self, *, ids, documents, metadatas):
        self.owner.calls.append(("upsert", list(ids), list(documents), list(metadatas)))
        for identifier, document, metadata in zip(ids, documents, metadatas):
            self.owner.records[identifier] = {"id": identifier, "document": document, "metadata": metadata}


class Palace:
    def __init__(self): self.calls, self.records = [], {}

    def get_collection(self, palace_path, collection_name=None, create=None, backend=None):
        self.calls.append(("get_collection", palace_path, collection_name, create, backend))
        assert collection_name == "mempalace_drawers" and create is True and backend == "chroma"
        return Collection(self)


class Searcher:
    def __init__(self, palace, state): self.palace, self.state, self.calls = palace, state, []

    def search_memories(self, query, palace_path, *, room, n_results, max_distance, candidate_strategy, collection_name):
        assert self.state["reset"], "queries must occur only after cold reopen barrier"
        assert max_distance == 0.0 and candidate_strategy == "vector" and collection_name == "mempalace_drawers"
        self.calls.append((palace_path, query, room, n_results))
        return {"results": [{"source_path": record["id"]} for record in self.palace.records.values() if record["metadata"]["room"] == room][:n_results]}


class Observer:
    def __init__(self): self.phases = []

    def checkpoint(self, phase): self.phases.append(phase)

    def receipt(self):
        return {"peak_rss_bytes": 123, "storage_bytes": 456,
                "passage_embedding": {"calls": 1, "texts": 22, "measurement": "synthetic-instrumented"},
                "query_embedding": {"calls": 2, "texts": 2, "measurement": "synthetic-instrumented"},
                "provider": {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"]}}


class IncompleteObserver(Observer):
    def receipt(self): return {"peak_rss_bytes": 123}


def fake_auditor(*, palace_path, expected_namespace):
    ids = sorted(row["physical_id"] for row in expected_namespace["rows"])
    return {
        "physical_count": len(ids), "physical_ids_sha256": rank._digest(ids),
        "embedding": {"count": len(ids), "dimension": 384, "dtype": "float32", "float32_sha256": h("vectors")},
        "hnsw_config": rank.ORIGINAL_HNSW_CONFIG,
        "graph_files": [{"name": name, "bytes": 1, "sha256": h(name)} for name in rank.ORIGINAL_GRAPH_NAMES],
        "immutable_backend_sha256": h("immutable"), "sqlite_semantic_sha256": h("sqlite"),
        "operational_delta": rank.ORIGINAL_OPERATIONAL_DELTA,
    }


def seams():
    palace, state = Palace(), {"reset": False}

    def reset(_path):
        state["reset"] = True
        return {"verified_system_released": True, "closed_backend_client_count": 1}

    return original.OriginalProductSeams(palace=palace, searcher=Searcher(palace, state), reset_backends=reset, auditor=fake_auditor), palace, state


class InternalTypeErrorPalace(Palace):
    def get_collection(self, *args, **kwargs):
        self.calls.append(("get_collection", args, kwargs))
        raise TypeError("inside original get_collection")


def test_namespace_is_dynamic_global_and_uses_text_only_documents(tmp_path):
    p, observer = projection(), Observer()
    namespace = original.original_identity_namespace(p)
    assert namespace["expected_unique_count"] == 22
    assert all("::aerp7::" in row["physical_id"] for row in namespace["rows"])
    injected, palace, _state = seams()
    telemetry = []
    draft = original.run_original_public_replicate(projection=p, build_id="build-A", collection_identity="collection-A", palace_path=tmp_path / "palace", observer=observer, seams=injected, resource_sink=telemetry.append)
    upserts = [row for row in palace.calls if row[0] == "upsert"]
    assert len(upserts) == 2
    assert all(metadata["wing"] == "aerp7-convomem" and metadata["source_file"] == identifier for identifier, metadata in zip(upserts[0][1], upserts[0][3]))
    assert all("speaker" not in text for text in upserts[0][2])
    assert draft.replicate_without_coordinator_audit["index_receipt"]["cold_reopen"] is True
    assert telemetry[0]["formal_eligible"] is False and telemetry[0]["resources"]["peak_rss_bytes"] == 123
    assert observer.phases == ["before_ingest", "after_ingest", "after_cold_close", "after_queries"]


def test_query_is_scoped_to_its_corpus_and_receipt_rejects_tamper(tmp_path):
    p, observer = projection(), Observer()
    injected, _palace, _state = seams()
    draft = original.run_original_public_replicate(projection=p, build_id="build-B", collection_identity="collection-B", palace_path=tmp_path / "palace", observer=observer, seams=injected)
    assert {call[2] for call in injected.searcher.calls} == {item["corpus_id"] for item in p["items"]}
    corpora = {row["corpus_id"]: row for row in p["corpora"]}
    for row in draft.replicate_without_coordinator_audit["rankings"]:
        allowed = {candidate["message_id"] for candidate in corpora[next(item["corpus_id"] for item in p["items"] if item["item_id"] == row["item_id"])]["candidates"]}
        assert set(row["ranked_message_ids"]) <= allowed
    changed = dict(draft.replicate_without_coordinator_audit["index_receipt"]["worker_physical_receipt"])
    changed["physical_count"] -= 1
    with pytest.raises(original.OriginalProductError):
        original.dynamic_original_index_build_receipt(palace_path=tmp_path / "palace", expected_namespace=original.original_identity_namespace(p), auditor=lambda **_kwargs: changed)


def test_coordinator_audit_must_equal_worker_and_observer_is_required(tmp_path):
    p, observer = projection(), Observer()
    injected, _palace, _state = seams()
    with pytest.raises(original.OriginalProductError, match="resource observer"):
        original.run_original_public_replicate(projection=p, build_id="build-C", collection_identity="collection-C", palace_path=tmp_path / "palace", observer=None, seams=injected)
    with pytest.raises(original.OriginalProductError, match="native-embedding/provider"):
        original.run_original_public_replicate(projection=p, build_id="build-D", collection_identity="collection-D", palace_path=tmp_path / "palace-incomplete", observer=IncompleteObserver(), seams=seams()[0])
    draft = original.run_original_public_replicate(projection=p, build_id="build-C", collection_identity="collection-C", palace_path=tmp_path / "palace", observer=observer, seams=injected)
    completed = original.coordinator_reaudit_replicate(draft=draft, palace_path=tmp_path / "palace", projection=p, auditor=fake_auditor)
    assert completed["index_receipt"]["coordinator_physical_receipt"]["physical_count"] == 22
    changed = {**draft.worker_physical_receipt, "sqlite_semantic_sha256": h("forged")}
    with pytest.raises(original.OriginalProductError, match="worker/coordinator"):
        original.coordinator_reaudit_replicate(draft=draft, palace_path=tmp_path / "palace", projection=p, auditor=lambda **_kwargs: changed)


def test_five_dynamic_replicates_wrap_into_the_strict_frozen_original_artifact(tmp_path):
    p = projection()
    model = {"encoder_identity": "synthetic", "encoder_semantics": "deterministic", "files": [{"path_role": "weights", "sha256": h("weights"), "bytes": 1}]}
    code = {"head": h("head"), "tree": h("tree"), "diff_digest": h("diff"), "dirty_policy": "clean_required"}
    replicates = []
    for number in range(5):
        injected, _palace, _state = seams()
        draft = original.run_original_public_replicate(projection=p, build_id=f"build-{number}", collection_identity=f"collection-{number}", palace_path=tmp_path / f"palace-{number}", observer=Observer(), seams=injected)
        replicates.append(original.coordinator_reaudit_replicate(draft=draft, palace_path=tmp_path / f"palace-{number}", projection=p, auditor=fake_auditor))
    artifact = rank.wrap_original_public_rankings(projection=p, replicates=replicates, model_receipt=model, code_receipt=code)
    assert rank.validate_frozen_ranking(artifact, projection=p)["arm_id"] == "original_public_product"


def test_formal_rejects_synthetic_provenance_and_internal_api_type_error_is_not_retried(tmp_path):
    p = projection()
    injected, _palace, _state = seams()
    with pytest.raises(original.OriginalProductError, match="formal query clocks"):
        original.run_original_public_replicate(
            projection=p,
            build_id="formal-clock-build",
            collection_identity="formal-clock-collection",
            palace_path=tmp_path / "formal-clock",
            observer=Observer(),
            seams=injected,
            formal=True,
            wall_clock_ns=lambda: 1,
        )
    with pytest.raises(original.OriginalProductError, match="live-pinned"):
        original.run_original_public_replicate(projection=p, build_id="formal-build", collection_identity="formal-collection", palace_path=tmp_path / "formal", observer=Observer(), seams=injected, formal=True, live_receipt={"forged": True}, resource_sink=lambda _row: None)
    palace, state = InternalTypeErrorPalace(), {"reset": False}
    bad = original.OriginalProductSeams(palace=palace, searcher=Searcher(palace, state), reset_backends=lambda _path: {"verified_system_released": True}, auditor=fake_auditor)
    with pytest.raises(TypeError, match="inside original"):
        original.run_original_public_replicate(projection=p, build_id="type-build", collection_identity="type-collection", palace_path=tmp_path / "type", observer=Observer(), seams=bad)
    assert len(palace.calls) == 1


def test_formal_embedding_telemetry_uses_public_request_ledger_not_adapter_counter():
    class Counter:
        runtime_identity = {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"]}

        def receipt(self):
            # The exact original backend calls _function directly, so these
            # adapter counters can honestly remain zero.
            return {"passage_call_count": 0, "passage_text_count": 0, "query_call_count": 0, "query_text_count": 0}

    seam = original.OriginalProductSeams(palace=None, searcher=None, reset_backends=lambda _path: {}, auditor=fake_auditor, provenance=original.LIVE_PINNED, encoder=Counter())
    telemetry = Observer().receipt()
    telemetry["passage_embedding"] = {"calls": 2, "texts": 21, "measurement": original.PUBLIC_UPSERT_MEASUREMENT}
    telemetry["query_embedding"] = {"calls": 2, "texts": 2, "measurement": original.PUBLIC_SEARCH_MEASUREMENT}
    counts = {"passage_calls": 2, "passage_texts": 22, "query_calls": 2, "query_texts": 2}
    with pytest.raises(original.OriginalProductError, match="public request ledger"):
        original._validate_resource_telemetry(value=telemetry, seams=seam, formal=True, public_request_counts=counts, expected_query_count=2, expected_corpus_count=2, expected_candidate_count=22)
    telemetry["passage_embedding"]["texts"] = 22
    assert original._validate_resource_telemetry(value=telemetry, seams=seam, formal=True, public_request_counts=counts, expected_query_count=2, expected_corpus_count=2, expected_candidate_count=22)["query_embedding"]["texts"] == 2
    telemetry["query_embedding"]["texts"] = 1
    with pytest.raises(original.OriginalProductError, match="public request ledger"):
        original._validate_resource_telemetry(value=telemetry, seams=seam, formal=True, public_request_counts=counts, expected_query_count=2, expected_corpus_count=2, expected_candidate_count=22)


def test_worker_draft_canonical_wire_packet_reloads_for_independent_coordinator_audit(tmp_path, monkeypatch):
    p = projection(); injected, _palace, _state = seams()
    draft = original.run_original_public_replicate(projection=p, build_id="wire-build", collection_identity="wire-collection", palace_path=tmp_path / "palace", observer=Observer(), seams=injected)
    payload = original.serialize_worker_draft(draft)
    loaded = original.load_worker_draft(payload)
    assert loaded is not draft and loaded.worker_physical_receipt == draft.worker_physical_receipt
    completed = original.coordinator_reaudit_replicate(draft=loaded, palace_path=tmp_path / "palace", projection=p, auditor=fake_auditor)
    assert completed["index_receipt"]["coordinator_physical_receipt"] == draft.worker_physical_receipt
    draft_path = tmp_path / "wire-draft.json"; draft_path.write_bytes(payload)
    worker_index_sha256 = draft.replicate_without_coordinator_audit["index_sha256"]
    resource = {"build_id": "wire-build", "index_sha256": worker_index_sha256, "resource_sha256": "worker-only"}
    packet = {
        "schema": executor.FORMAL_ORIGINAL_PACKET_SCHEMA, "execution_mode": "exact_public_product_worker_draft",
        "draft_file_sha256": hashlib.sha256(payload).hexdigest(), "palace_path": str((tmp_path / "palace").resolve()),
        "resource_receipt": resource,
        "worker_execution_identity": {
            "original_python": str((tmp_path / "original-python.exe").resolve()),
            "original_execution_policy_sha256": "c" * 64,
        },
        "process_id": 123, "packet_sha256": "",
    }
    packet["packet_sha256"] = executor._digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    real_reaudit = original.coordinator_reaudit_replicate
    monkeypatch.setattr(
        executor.original_product, "coordinator_reaudit_replicate",
        lambda **kwargs: real_reaudit(**{key: value for key, value in kwargs.items() if key != "auditor"}, auditor=fake_auditor),
    )
    executor_completed, rebound_resource = executor.coordinator_reaudit_original_worker_packet(
        packet=packet, draft_path=draft_path, palace_path=tmp_path / "palace", projection=p,
    )
    assert executor_completed == completed
    assert rebound_resource["index_sha256"] == completed["index_sha256"] != worker_index_sha256
    assert rebound_resource["resource_sha256"] == executor.formal.resource_digest(rebound_resource)
    with pytest.raises(original.OriginalProductError, match="original-product worker draft"):
        original.coordinator_reaudit_replicate(draft=loaded.replicate_without_coordinator_audit, palace_path=tmp_path / "palace", projection=p, auditor=fake_auditor)


def _repack(packet):
    packet["draft_sha256"] = original._digest({key: value for key, value in packet.items() if key != "draft_sha256"})
    return original._canonical_bytes(packet)


def test_worker_draft_packet_rejects_schema_digest_and_cross_binding_tampering(tmp_path):
    p = projection(); injected, _palace, _state = seams()
    draft = original.run_original_public_replicate(projection=p, build_id="tamper-build", collection_identity="tamper-collection", palace_path=tmp_path / "palace", observer=Observer(), seams=injected)
    packet = json.loads(original.serialize_worker_draft(draft).decode("utf-8"))
    unknown = copy.deepcopy(packet); unknown["unknown"] = True
    with pytest.raises(original.OriginalProductError, match="packet schema"):
        original.load_worker_draft(_repack(unknown))
    missing = copy.deepcopy(packet); del missing["telemetry"]
    with pytest.raises(original.OriginalProductError, match="packet schema"):
        original.load_worker_draft(_repack(missing))
    telemetry_unknown = copy.deepcopy(packet); telemetry_unknown["telemetry"]["unknown"] = True; telemetry_unknown["telemetry_sha256"] = original._digest(telemetry_unknown["telemetry"])
    with pytest.raises(original.OriginalProductError, match="telemetry schema"):
        original.load_worker_draft(_repack(telemetry_unknown))
    digest_tamper = copy.deepcopy(packet); digest_tamper["projection_sha256"] = h("forged")
    with pytest.raises(original.OriginalProductError, match="draft digest"):
        original.load_worker_draft(original._canonical_bytes(digest_tamper))
    projection_tamper = copy.deepcopy(packet); projection_tamper["projection"]["corpora"][0]["candidates"][0]["text"] = "changed"; projection_tamper["projection_sha256"] = original._digest(projection_tamper["projection"])
    with pytest.raises(original.OriginalProductError, match="input/trace"):
        original.load_worker_draft(_repack(projection_tamper))
    namespace_tamper = copy.deepcopy(packet); namespace_tamper["namespace"]["rows"][0]["physical_id"] = "forged"; namespace_tamper["namespace_sha256"] = original._digest(namespace_tamper["namespace"])
    with pytest.raises(original.OriginalProductError, match="namespace"):
        original.load_worker_draft(_repack(namespace_tamper))
    worker_tamper = copy.deepcopy(packet); worker_tamper["worker_physical_receipt"]["sqlite_semantic_sha256"] = h("forged"); worker_tamper["worker_physical_receipt_sha256"] = original._digest(worker_tamper["worker_physical_receipt"])
    with pytest.raises(original.OriginalProductError, match="physical receipt mismatch"):
        original.load_worker_draft(_repack(worker_tamper))


def test_query_sidecar_uses_injected_process_clocks_and_does_not_change_rankings(tmp_path):
    p = projection()
    wall = iter([100, 250, 400, 700])
    cpu = iter([1_000, 1_100, 2_000, 2_200])
    clock_calls = []

    def wall_clock():
        clock_calls.append("wall")
        return next(wall)

    def cpu_clock():
        clock_calls.append("cpu")
        return next(cpu)

    injected, _palace, _state = seams()
    timed = original.run_original_public_replicate(
        projection=p,
        build_id="clock-build",
        collection_identity="clock-collection",
        palace_path=tmp_path / "clock-palace",
        observer=Observer(),
        seams=injected,
        wall_clock_ns=wall_clock,
        cpu_clock_ns=cpu_clock,
    )
    injected_again, _palace_again, _state_again = seams()
    untimed = original.run_original_public_replicate(
        projection=p,
        build_id="clock-build-2",
        collection_identity="clock-collection-2",
        palace_path=tmp_path / "clock-palace-2",
        observer=Observer(),
        seams=injected_again,
    )
    telemetry = timed.telemetry["resources"]
    ordered_items = sorted(p["items"], key=lambda row: row["item_id"])
    assert telemetry["query_measurements"] == [
        {"item_id": ordered_items[0]["item_id"], "query_sha256": rank._query_digest(ordered_items[0]["query_text"]), "wall_ns": 150, "cpu_ns": 100},
        {"item_id": ordered_items[1]["item_id"], "query_sha256": rank._query_digest(ordered_items[1]["query_text"]), "wall_ns": 300, "cpu_ns": 200},
    ]
    assert telemetry["clock_receipt"]["schema"] == original.CLOCK_RECEIPT_SCHEMA
    assert telemetry["clock_receipt"]["timing_source"] == "injected_test_clock"
    assert telemetry["process_cpu_scope"] == "worker_process_only_excludes_descendants"
    assert telemetry["descendant_observation"] == "external_supervisor_zero_required"
    assert clock_calls == ["wall", "cpu", "wall", "cpu"] * len(ordered_items)
    search_ledger = [row for row in timed.telemetry["ledger"] if row["event"] == "search"]
    assert [row["latency_seconds"] for row in search_ledger] == [
        measurement["wall_ns"] / 1_000_000_000
        for measurement in telemetry["query_measurements"]
    ]
    assert timed.replicate_without_coordinator_audit["rankings"] == untimed.replicate_without_coordinator_audit["rankings"]
    assert original._canonical_bytes(timed.replicate_without_coordinator_audit["rankings"]) == original._canonical_bytes(untimed.replicate_without_coordinator_audit["rankings"])
    assert original._canonical_bytes(timed.replicate_without_coordinator_audit["trace_receipt"]) == original._canonical_bytes(untimed.replicate_without_coordinator_audit["trace_receipt"])
    assert timed.replicate_without_coordinator_audit["trace_sha256"] == untimed.replicate_without_coordinator_audit["trace_sha256"]


def test_query_sidecar_rejects_nonpositive_timing_and_binding_tampering():
    p = projection()
    injected, _palace, _state = seams()
    draft = original.run_original_public_replicate(
        projection=p,
        build_id="sidecar-build",
        collection_identity="sidecar-collection",
        palace_path=Path("sidecar-palace"),
        observer=Observer(),
        seams=injected,
    )
    rows = draft.telemetry["resources"]["query_measurements"]
    with pytest.raises(original.OriginalProductError, match="query timing"):
        original.validate_query_measurements(
            measurements=[{**rows[0], "wall_ns": 0}, rows[1]],
            replicate=draft.replicate_without_coordinator_audit,
            expected_count=2,
        )
    with pytest.raises(original.OriginalProductError, match="query binding"):
        original.validate_query_measurements(
            measurements=[{**rows[0], "query_sha256": h("forged")}, rows[1]],
            replicate=draft.replicate_without_coordinator_audit,
            expected_count=2,
        )
