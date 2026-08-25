import copy
import hashlib
import json
import os
import sys
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_confirmation as confirmation
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


def candidate_reference_bundle(tmp_path, *, query_count=None):
    bundle = tmp_path / "candidate"
    bundle.mkdir()
    value = projection()
    projection_raw = confirmation._bytes(value)
    projection_path = bundle / "projection.json"
    projection_path.write_bytes(projection_raw)
    ready = {
        "schema": confirmation.CANDIDATE_READY_SCHEMA,
        "generation_id": h("generation-1"),
        "projection": {
            "raw_sha256": hashlib.sha256(projection_raw).hexdigest(),
            "canonical_sha256": confirmation.canonical_sha256(value),
        },
        "durability": confirmation._durability_receipt(),
    }
    if query_count is not None:
        corpus_ids = [corpus["corpus_id"] for corpus in value["corpora"]]
        value["items"] = [{"item_id": h(f"item-{number}"), "persona_id": h("persona"), "query_text": f"query {number}", "corpus_id": corpus_ids[number % len(corpus_ids)]} for number in range(query_count)]
        projection_raw = confirmation._bytes(value)
        projection_path.write_bytes(projection_raw)
        ready["projection"]["raw_sha256"] = hashlib.sha256(projection_raw).hexdigest()
        ready["projection"]["canonical_sha256"] = confirmation.canonical_sha256(value)
    (bundle / "READY.json").write_bytes(confirmation._bytes(ready))
    return original.candidate_projection_reference(
        bundle_path=bundle,
        generation_id=h("generation-1"),
        projection_raw_sha256=ready["projection"]["raw_sha256"],
        projection_canonical_sha256=ready["projection"]["canonical_sha256"],
        dataset=value["dataset"],
        query_count=len(value["items"]),
        candidate_text_count=22,
    )


def test_candidate_reference_streams_one_corpus_and_item_without_projection_materialization(tmp_path, monkeypatch):
    reference = candidate_reference_bundle(tmp_path)
    projection_path = Path(reference["bundle_path"]) / reference["projection_path"]
    real_read_bytes = Path.read_bytes

    def no_projection_materialization(path):
        if path == projection_path:
            raise AssertionError("stream worker materialized projection bytes")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", no_projection_materialization)
    cursor = original.CandidateProjectionCursor(reference)
    corpora = list(cursor.corpora())
    assert [row["corpus_id"] for row in corpora] == sorted(row["corpus_id"] for row in corpora)
    items = list(cursor.items(corpus_ids={row["corpus_id"] for row in corpora}))
    assert [row["item_id"] for row in items] == sorted(row["item_id"] for row in items)
    assert sum(len(row["candidates"]) for row in corpora) == reference["candidate_text_count"]


def test_candidate_reference_fails_closed_on_raw_or_ready_drift(tmp_path):
    reference = candidate_reference_bundle(tmp_path)
    projection_path = Path(reference["bundle_path"]) / reference["projection_path"]
    projection_path.write_bytes(projection_path.read_bytes() + b" ")
    with pytest.raises(original.OriginalProductError, match="raw digest drift"):
        original.validate_candidate_projection_reference(reference)


def test_stream_worker_packet_contains_reference_but_never_projection(tmp_path):
    reference = candidate_reference_bundle(tmp_path)
    query_rows = [
        {"item_id": h(f"item-{number}"), "query_sha256": h(f"query-{number}"), "wall_ns": 1, "cpu_ns": 1}
        for number in range(2)
    ]
    telemetry = {
        "formal_eligible": False,
        "live_receipt": {},
        "ledger": [{"event": "upsert", "count": 22}, {"event": "search", "count": 1}],
        "resources": {
            "query_measurements": query_rows,
            "clock_receipt": original._clock_receipt(),
            "process_cpu_scope": "worker_process_only_excludes_descendants",
            "descendant_observation": "external_supervisor_zero_required",
        },
    }
    draft = original.OriginalProductWorkerDraft(
        projection=None,
        candidate_reference=reference,
        namespace={
            "schema": original.STREAM_NAMESPACE_SCHEMA,
            "candidate_reference": reference,
            "candidate_reference_sha256": original._digest(reference),
            "corpus_count": 2,
            "candidate_text_count": 22,
            "query_count": 2,
        },
        replicate_without_coordinator_audit={"stream": True},
        worker_physical_receipt={"physical": "receipt"},
        telemetry=telemetry,
    )
    packet = json.loads(original.serialize_worker_draft(draft))
    assert packet["schema"] == original.STREAM_DRAFT_SCHEMA
    assert "projection" not in packet
    loaded = original.load_worker_draft(original.serialize_worker_draft(draft))
    assert loaded.projection is None and loaded.candidate_reference == reference


def test_stream_worker_preserves_latest_original_product_order_and_ties(tmp_path):
    reference = candidate_reference_bundle(tmp_path)
    streamed_seams, streamed_palace, streamed_state = seams()
    streamed = original.run_original_public_replicate_streaming(
        candidate_reference=reference,
        build_id="stream-build",
        collection_identity="stream-collection",
        palace_path=tmp_path / "stream-palace",
        observer=Observer(),
        seams=streamed_seams,
        staging_parent=tmp_path,
    )
    legacy_seams, legacy_palace, legacy_state = seams()
    legacy = original.run_original_public_replicate(
        projection=projection(),
        build_id="legacy-build",
        collection_identity="legacy-collection",
        palace_path=tmp_path / "legacy-palace",
        observer=Observer(),
        seams=legacy_seams,
    )
    assert streamed.projection is None
    assert streamed.candidate_reference == reference
    assert streamed.replicate_without_coordinator_audit["rankings"] == legacy.replicate_without_coordinator_audit["rankings"]
    assert streamed.replicate_without_coordinator_audit["trace_receipt"] == legacy.replicate_without_coordinator_audit["trace_receipt"]
    assert streamed_state["reset"] and legacy_state["reset"]


def test_stream_coordinator_reaudits_reference_and_rejects_generation_drift(tmp_path):
    reference = candidate_reference_bundle(tmp_path)
    injected, _palace, _state = seams()
    draft = original.run_original_public_replicate_streaming(
        candidate_reference=reference,
        build_id="stream-coordinator-build",
        collection_identity="stream-coordinator-collection",
        palace_path=tmp_path / "stream-coordinator-palace",
        observer=Observer(),
        seams=injected,
        staging_parent=tmp_path,
    )
    completed = original.coordinator_reaudit_streaming_replicate(
        draft=draft,
        palace_path=tmp_path / "stream-coordinator-palace",
        auditor=fake_auditor,
    )
    assert completed["index_receipt"]["coordinator_physical_receipt"] == draft.worker_physical_receipt
    projection_path = Path(reference["bundle_path"]) / reference["projection_path"]
    projection_path.write_bytes(projection_path.read_bytes() + b" ")
    with pytest.raises(original.OriginalProductError, match="raw digest drift"):
        original.coordinator_reaudit_streaming_replicate(
            draft=draft,
            palace_path=tmp_path / "stream-coordinator-palace",
            auditor=lambda **_kwargs: pytest.fail("auditor ran after reference drift"),
        )


def test_stream_reference_and_five_build_artifact_never_embed_sequences(tmp_path):
    reference = candidate_reference_bundle(tmp_path)
    completed = []
    for number in range(5):
        injected, _palace, _state = seams()
        draft = original.run_original_public_replicate_streaming(
            candidate_reference=reference,
            build_id=f"stream-artifact-build-{number}",
            collection_identity=f"stream-artifact-collection-{number}",
            palace_path=tmp_path / f"stream-artifact-palace-{number}",
            observer=Observer(), seams=injected, staging_parent=tmp_path,
        )
        packet = json.loads(original.serialize_worker_draft(draft))
        replicate_ref = packet["replicate_without_coordinator_audit"]
        assert replicate_ref["schema"] == original.ORIGINAL_REPLICATE_REFERENCE_SCHEMA
        assert "rankings" not in replicate_ref and "trace_receipt" not in replicate_ref
        completed_reference = original.coordinator_reaudit_streaming_replicate(
            draft=draft, palace_path=tmp_path / f"stream-artifact-palace-{number}", auditor=fake_auditor,
        )
        assert original.coordinator_reaudit_streaming_replicate(
            draft=draft, palace_path=tmp_path / f"stream-artifact-palace-{number}", auditor=fake_auditor,
        ).as_reference() == completed_reference.as_reference()
        completed.append(completed_reference.as_reference())
    artifact = original.wrap_original_public_rankings_streaming(
        candidate_reference=reference, replicate_references=completed,
        model_receipt={"model": "test"}, code_receipt={"commit": h("code")},
        artifact_path=tmp_path / "original-artifact.json", ready_path=tmp_path / "original-artifact.READY.json",
    )
    assert artifact["replicate_count"] == 5
    assert "rankings" not in json.loads((tmp_path / "original-artifact.json").read_text())
    assert original.load_original_public_artifact_reference(tmp_path / "original-artifact.READY.json") == artifact


def test_stream_memory_does_not_grow_with_persisted_output_rows(tmp_path):
    reference = candidate_reference_bundle(tmp_path, query_count=250)
    injected, _palace, _state = seams()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    draft = original.run_original_public_replicate_streaming(
        candidate_reference=reference,
        build_id="stream-memory-build",
        collection_identity="stream-memory-collection",
        palace_path=tmp_path / "stream-memory-palace",
        observer=Observer(), seams=injected, staging_parent=tmp_path,
    )
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    # The seam has 250 queries; the assertion guards against retaining the
    # full logical arrays in the returned draft while still allowing normal
    # SQLite/JSON parser allocations.
    assert draft.replicate_without_coordinator_audit.as_reference()["schema"] == original.ORIGINAL_REPLICATE_REFERENCE_SCHEMA
    assert sum(stat.size_diff for stat in after.compare_to(before, "filename") if "aerp7_original_product.py" in str(stat.traceback)) < 2_000_000


def test_stream_reference_exposes_ready_bound_cursors_and_resource_summary(tmp_path):
    reference = candidate_reference_bundle(tmp_path)
    injected, _palace, _state = seams()
    draft = original.run_original_public_replicate_streaming(
        candidate_reference=reference,
        build_id="stream-cursor-build",
        collection_identity="stream-cursor-collection",
        palace_path=tmp_path / "stream-cursor-palace",
        observer=Observer(),
        seams=injected,
        staging_parent=tmp_path,
    )
    replicate = draft.replicate_without_coordinator_audit
    ranking_cursor = replicate.ranking_cursor()
    measurement_cursor = replicate.measurement_cursor()
    assert not isinstance(ranking_cursor, list)
    assert len(ranking_cursor) == reference["query_count"]
    assert ranking_cursor[0]["item_id"] == sorted(item["item_id"] for item in projection()["items"])[0]
    assert ranking_cursor.sha256() == original._digest_sequence(iter(ranking_cursor))
    assert len(measurement_cursor) == reference["query_count"]
    summary = replicate.sequence_summary()
    assert summary["rankings"]["count"] == reference["query_count"]
    assert summary["measurements"]["count"] == reference["query_count"]
    assert summary["rankings"]["sha256"] == replicate.ranking_cursor().sha256()
    resource_summary = replicate.resource_summary()
    assert resource_summary["resources"]["peak_rss_bytes"] == 123
    assert resource_summary["resources"]["query_embedding"]["calls"] == reference["query_count"]
    candidate_index = resource_summary["resources"]["candidate_index"]
    assert candidate_index["peak_bytes"] >= candidate_index["final_bytes"] > 0
    assert candidate_index["projection_bytes"] == (Path(reference["bundle_path"]) / reference["projection_path"]).stat().st_size
    assert candidate_index["peak_to_projection_ratio"] >= 0
    assert not list(tmp_path.rglob("candidate-index.sqlite*"))


@pytest.mark.skipif(os.environ.get("AERP7_RUN_LARGE_MEMORY_REGRESSION") != "1", reason="set AERP7_RUN_LARGE_MEMORY_REGRESSION=1 for 100k/200k seam run")
@pytest.mark.parametrize("query_count", (100_000, 200_000))
def test_stream_actual_public_seam_memory_scales_from_100k_to_200k(tmp_path, query_count):
    """The real public upsert/search seam must not retain full query output."""
    bundle_root = tmp_path / f"bundle-{query_count}"
    bundle_root.mkdir()
    reference = candidate_reference_bundle(bundle_root, query_count=query_count)
    palace, state = Palace(), {"reset": False}
    searcher = BoundedSearcher(palace, state)
    seams = original.OriginalProductSeams(
        palace=palace,
        searcher=searcher,
        reset_backends=lambda _path: state.update(reset=True) or {"verified_system_released": True, "closed_backend_client_count": 1},
        auditor=fake_auditor,
    )
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    draft = original.run_original_public_replicate_streaming(
        candidate_reference=reference,
        build_id=f"stream-large-{query_count}",
        collection_identity=f"stream-large-collection-{query_count}",
        palace_path=tmp_path / f"palace-{query_count}",
        observer=Observer(),
        seams=seams,
        staging_parent=tmp_path,
    )
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    module_growth = sum(stat.size_diff for stat in after.compare_to(before, "filename") if "aerp7_original_product.py" in str(stat.traceback))
    assert draft.replicate_without_coordinator_audit.as_reference()["schema"] == original.ORIGINAL_REPLICATE_REFERENCE_SCHEMA
    assert searcher.call_count == query_count
    assert module_growth < 8_000_000
    assert not list(tmp_path.rglob("candidate-index.sqlite*"))


def test_stream_coordinator_retry_repairs_ready_after_atomic_replace_failure(tmp_path, monkeypatch):
    reference = candidate_reference_bundle(tmp_path)
    injected, _palace, _state = seams()
    palace_path = tmp_path / "stream-retry-palace"
    draft = original.run_original_public_replicate_streaming(
        candidate_reference=reference,
        build_id="stream-retry-build",
        collection_identity="stream-retry-collection",
        palace_path=palace_path,
        observer=Observer(),
        seams=injected,
        staging_parent=tmp_path,
    )
    real_replace = original.os.replace
    injected_failure = False

    def fail_ready_replace(source, destination):
        nonlocal injected_failure
        if str(destination).endswith(".READY.json") and not injected_failure:
            injected_failure = True
            raise OSError("injected READY publication failure")
        return real_replace(source, destination)

    monkeypatch.setattr(original.os, "replace", fail_ready_replace)
    with pytest.raises(original.OriginalProductError, match="READY publication failed"):
        original.coordinator_reaudit_streaming_replicate(
            draft=draft, palace_path=palace_path, auditor=fake_auditor,
        )
    assert injected_failure is True
    monkeypatch.setattr(original.os, "replace", real_replace)
    repaired = original.coordinator_reaudit_streaming_replicate(
        draft=draft, palace_path=palace_path, auditor=fake_auditor,
    )
    assert repaired.as_reference()["state"] == "coordinator_complete"
    assert original.load_original_replicate_reference(repaired.as_reference()["ready_path"]) == repaired.as_reference()


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


def test_direct_dynamic_audit_immutable_drift_reports_canonical_before_after_state(
    monkeypatch, tmp_path
):
    before_immutable = {"header.bin": {"bytes": 10, "sha256": "a" * 64}}
    after_immutable = {"header.bin": {"bytes": 11, "sha256": "b" * 64}}
    before_config = {"batch_size": 100, "space": "cosine"}
    after_config = {"batch_size": 101, "space": "cosine"}
    storages = iter(
        [
            {"immutable_snapshot": before_immutable, "files": [], "immutable_sha256": "a" * 64},
            {"immutable_snapshot": after_immutable, "files": [], "immutable_sha256": "b" * 64},
        ]
    )
    configs = iter([before_config, after_config])
    monkeypatch.setattr(original.v2, "_audit_storage_digest", lambda _path: next(storages))
    monkeypatch.setattr(original.v2, "_sqlite_hnsw_configuration", lambda _path: next(configs))
    monkeypatch.setattr(
        original.v2,
        "_sqlite_semantic_snapshot",
        lambda _path: {"semantic_sha256": "c" * 64},
    )

    class Client:
        def get_collection(self, _name):
            return SimpleNamespace(get=lambda **_kwargs: {"ids": ["id"], "embeddings": [[0.0]]})

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=lambda **_kwargs: Client()))
    with pytest.raises(original.OriginalProductError) as excinfo:
        original._direct_dynamic_audit(palace_path=tmp_path, expected_ids=["id"])
    assert str(excinfo.value) == (
        "direct original index audit mutated persisted index: "
        f"immutable_before={original._canonical_bytes(before_immutable).decode('utf-8')}; "
        f"immutable_after={original._canonical_bytes(after_immutable).decode('utf-8')}; "
        f"config_before={original._canonical_bytes(before_config).decode('utf-8')}; "
        f"config_after={original._canonical_bytes(after_config).decode('utf-8')}"
    )


def test_direct_dynamic_audit_receipts_the_one_allowed_length_bin_normalization(
    monkeypatch, tmp_path
):
    def storage(*, length_sha256: str) -> dict:
        immutable = [
            {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "d" * 64},
            {"path": "segment/header.bin", "bytes": 10, "sha256": "h" * 64},
            {"path": "segment/length.bin", "bytes": 400, "sha256": length_sha256},
            {"path": "segment/link_lists.bin", "bytes": 10, "sha256": "l" * 64},
        ]
        return {
            "immutable_snapshot": immutable,
            "immutable_sha256": original._digest(immutable),
            "files": [
                {"name": row["path"].rsplit("/", 1)[1], "bytes": row["bytes"], "sha256": row["sha256"]}
                for row in immutable
            ],
        }

    before, after = storage(length_sha256="a" * 64), storage(length_sha256="b" * 64)
    storages = iter([before, after])
    config = {"batch_size": 100, "space": "cosine"}
    monkeypatch.setattr(original.v2, "_audit_storage_digest", lambda _path: next(storages))
    monkeypatch.setattr(original.v2, "_sqlite_hnsw_configuration", lambda _path: config)
    monkeypatch.setattr(
        original.v2,
        "_sqlite_semantic_snapshot",
        lambda _path: {"semantic_sha256": "s" * 64, "acquire_write_rows": []},
    )
    monkeypatch.setattr(
        original.v2,
        "_validated_acquire_write_delta",
        lambda _before, _after: original.rank.ORIGINAL_OPERATIONAL_DELTA,
    )

    class Client:
        def get_collection(self, _name):
            return SimpleNamespace(get=lambda **_kwargs: {"ids": ["id"], "embeddings": [[0.0]]})

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=lambda **_kwargs: Client()))
    receipt = original._direct_dynamic_audit(palace_path=tmp_path, expected_ids=["id"])
    assert receipt["graph_files"] == [
        {"name": row["path"].rsplit("/", 1)[1], **row}
        for row in after["immutable_snapshot"]
    ]
    assert receipt["immutable_backend_sha256"] == after["immutable_sha256"]
    assert receipt["immutable_non_length_backend_sha256"] == original.v2.canonical_sha256([
        row for row in after["immutable_snapshot"] if row["path"] != "segment/length.bin"
    ])
    assert receipt["direct_read_normalization_delta"] == {
        "schema": "aerp7-hnsw-direct-read-normalization-v1",
        "status": "length_bin_same_size_rewrite",
        "path": "segment/length.bin",
        "bytes": 400,
        "before_sha256": "a" * 64,
        "after_sha256": "b" * 64,
    }


def test_chroma_audit_requires_finite_pages_and_preserves_v38_embedding_digest(monkeypatch, tmp_path):
    monkeypatch.setattr(original, "ORIGINAL_CHROMA_AUDIT_BATCH_SIZE", 2)
    physical = ["physical-z", "physical-a", "physical-m"]
    vectors = [[0.0, 1.0], [2.0, 3.0], [4.0, 5.0]]

    class BoundedCollection:
        def __init__(self):
            self.calls = []

        def get(self, **kwargs):
            # This fake deliberately rejects the old unbounded call shape.
            assert kwargs["limit"] == 2
            assert kwargs["limit"] is not None
            assert kwargs["offset"] >= 0
            assert kwargs["include"] == ["embeddings"]
            self.calls.append(dict(kwargs))
            start = kwargs["offset"]
            stop = min(start + kwargs["limit"], len(physical))
            return {"ids": physical[start:stop], "embeddings": vectors[start:stop]}

    collection = BoundedCollection()
    actual = original._stream_chroma_embedding_receipt(
        collection=collection, expected_ids=sorted(physical), temporary_parent=tmp_path,
    )
    expected_vector_sha, expected_count, expected_dimension = original.v2._float32_embedding_digest(physical, vectors)
    assert actual == (
        expected_vector_sha,
        expected_count,
        expected_dimension,
        original._digest(sorted(physical)),
    )
    assert [call["offset"] for call in collection.calls] == [0, 2]
    assert all(call["limit"] == original.ORIGINAL_CHROMA_AUDIT_BATCH_SIZE for call in collection.calls)
    assert not list(tmp_path.glob(".aerp7-chroma-audit-*"))


@pytest.mark.skipif(os.environ.get("AERP7_RUN_LARGE_MEMORY_REGRESSION") != "1", reason="set AERP7_RUN_LARGE_MEMORY_REGRESSION=1 for 100k/200k direct Chroma seam run")
@pytest.mark.parametrize("physical_count", (100_000, 200_000))
def test_direct_dynamic_audit_actual_paginated_seam_memory_scales(monkeypatch, tmp_path, physical_count):
    """The direct audit must not retain all Chroma IDs or 384d vectors."""
    graph_rows = [
        {"path": f"segment/{name}", "bytes": number, "sha256": chr(97 + number) * 64}
        for number, name in enumerate(original.rank.ORIGINAL_GRAPH_NAMES)
    ]

    def storage(length_sha256):
        immutable = [
            graph_rows[0], graph_rows[1],
            {"path": graph_rows[2]["path"], "bytes": graph_rows[2]["bytes"], "sha256": length_sha256},
            graph_rows[3],
        ]
        return {
            "immutable_snapshot": immutable,
            "immutable_sha256": original._digest(immutable),
            "files": [
                {"name": row["path"].rsplit("/", 1)[1], "bytes": row["bytes"], "sha256": row["sha256"]}
                for row in immutable
            ],
        }

    monkeypatch.setattr(original.v2, "_audit_storage_digest", lambda _path: next(storage_receipts))
    monkeypatch.setattr(original.v2, "_sqlite_hnsw_configuration", lambda _path: original.rank.ORIGINAL_HNSW_CONFIG)
    monkeypatch.setattr(original.v2, "_sqlite_semantic_snapshot", lambda _path: {"semantic_sha256": "s" * 64})
    monkeypatch.setattr(original.v2, "_validated_acquire_write_delta", lambda _before, _after: original.rank.ORIGINAL_OPERATIONAL_DELTA)
    storage_receipts = iter([storage("a" * 64), storage("b" * 64)])

    class ExpectedIDs:
        def __len__(self):
            return physical_count

        def __iter__(self):
            for number in range(physical_count):
                yield f"physical-{number:08d}"

    class StreamingCollection:
        def __init__(self):
            self.calls = []
            self.zero = [0.0] * 384

        def get(self, **kwargs):
            assert kwargs["include"] == ["embeddings"]
            assert kwargs["limit"] == original.ORIGINAL_CHROMA_AUDIT_BATCH_SIZE
            assert isinstance(kwargs["limit"], int) and kwargs["limit"] > 0
            start = kwargs["offset"]
            self.calls.append((start, kwargs["limit"]))
            if start >= physical_count:
                return {"ids": [], "embeddings": []}
            stop = min(start + kwargs["limit"], physical_count)
            ids = [f"physical-{number:08d}" for number in range(start, stop)]
            return {"ids": ids, "embeddings": [self.zero] * len(ids)}

    collection = StreamingCollection()

    class Client:
        def get_collection(self, _name):
            return collection

        def close(self):
            return None

    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=lambda **_kwargs: Client()))
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    receipt = original._direct_dynamic_audit(
        palace_path=tmp_path / "palace", expected_ids=ExpectedIDs(),
    )
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    module_growth = sum(
        stat.size_diff
        for stat in after.compare_to(before, "filename")
        if "aerp7_original_product.py" in str(stat.traceback)
    )
    assert receipt["physical_count"] == physical_count
    assert receipt["embedding"]["count"] == physical_count
    assert receipt["embedding"]["dimension"] == 384
    assert collection.calls and all(limit == original.ORIGINAL_CHROMA_AUDIT_BATCH_SIZE for _offset, limit in collection.calls)
    assert module_growth < 16_000_000
    assert not list(tmp_path.glob(".aerp7-chroma-audit-*"))


@pytest.mark.parametrize(
    "after",
    [
        [
            {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "b" * 64},
            {"path": "segment/length.bin", "bytes": 400, "sha256": "a" * 64},
        ],
        [
            {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "a" * 64},
            {"path": "segment/length.bin", "bytes": 400, "sha256": "a" * 64},
            {"path": "unrelated.bin", "bytes": 1, "sha256": "b" * 64},
        ],
        [
            {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "a" * 64},
            {"path": "segment/length.bin", "bytes": 401, "sha256": "b" * 64},
        ],
    ],
)
def test_direct_read_normalization_rejects_other_graph_non_sqlite_or_size_drift(after):
    before = [
        {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "a" * 64},
        {"path": "segment/length.bin", "bytes": 400, "sha256": "a" * 64},
    ]
    assert original._hnsw_direct_read_normalization_delta(before, after) is None


def test_direct_read_normalization_accepts_only_the_canonical_paired_v380_rewrite():
    before = [
        {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "a" * 64},
        {"path": "segment/header.bin", "bytes": 10, "sha256": "h" * 64},
        {"path": "segment/length.bin", "bytes": 400, "sha256": "b" * 64},
        {"path": "segment/link_lists.bin", "bytes": 10, "sha256": "l" * 64},
    ]
    after = [
        {"path": "segment/data_level0.bin", "bytes": 100, "sha256": "c" * 64},
        *before[1:2],
        {"path": "segment/length.bin", "bytes": 400, "sha256": "d" * 64},
        *before[3:],
    ]
    assert original._hnsw_direct_read_normalization_delta(before, after) == {
        "schema": original.rank.PAIRED_DIRECT_READ_NORMALIZATION_SCHEMA,
        "status": "data_level0_and_length_same_size_rewrite",
        "transitions": [
            {"path": "segment/data_level0.bin", "bytes": 100, "before_sha256": "a" * 64, "after_sha256": "c" * 64},
            {"path": "segment/length.bin", "bytes": 400, "before_sha256": "b" * 64, "after_sha256": "d" * 64},
        ],
    }


def test_direct_read_normalization_rejects_length_bin_outside_the_canonical_hnsw_segment():
    before = [
        {"path": "canonical/data_level0.bin", "bytes": 100, "sha256": "a" * 64},
        {"path": "canonical/length.bin", "bytes": 400, "sha256": "a" * 64},
        {"path": "other/length.bin", "bytes": 400, "sha256": "a" * 64},
    ]
    after = [
        *before[:2],
        {"path": "other/length.bin", "bytes": 400, "sha256": "b" * 64},
    ]
    assert original._hnsw_direct_read_normalization_delta(before, after) is None


class Searcher:
    def __init__(self, palace, state): self.palace, self.state, self.calls = palace, state, []

    def search_memories(self, query, palace_path, *, room, n_results, max_distance, candidate_strategy, collection_name):
        assert self.state["reset"], "queries must occur only after cold reopen barrier"
        assert max_distance == 0.0 and candidate_strategy == "vector" and collection_name == "mempalace_drawers"
        self.calls.append((palace_path, query, room, n_results))
        return {"results": [{"source_path": record["id"]} for record in self.palace.records.values() if record["metadata"]["room"] == room][:n_results]}


class BoundedSearcher:
    """The same public search seam without retaining a full query log."""

    def __init__(self, palace, state): self.palace, self.state, self.call_count = palace, state, 0

    def search_memories(self, query, palace_path, *, room, n_results, max_distance, candidate_strategy, collection_name):
        assert self.state["reset"]
        assert max_distance == 0.0 and candidate_strategy == "vector" and collection_name == "mempalace_drawers"
        self.call_count += 1
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
        "graph_files": [{"name": name, "path": f"segment/{name}", "bytes": 1, "sha256": h(name)} for name in rank.ORIGINAL_GRAPH_NAMES],
        "immutable_backend_sha256": h("immutable"), "immutable_non_length_backend_sha256": h("non-length-immutable"), "immutable_residual_backend_sha256": h("residual-immutable"), "sqlite_semantic_sha256": h("sqlite"),
        "operational_delta": rank.ORIGINAL_OPERATIONAL_DELTA,
        "direct_read_normalization_delta": {"schema": rank.DIRECT_READ_NORMALIZATION_SCHEMA, "status": "none", "path": None, "bytes": None, "before_sha256": None, "after_sha256": None},
    }


def _graph(receipt, name):
    return next(row for row in receipt["graph_files"] if row["name"] == name)


def _paired_receipt(receipt, *, data_before, data_after, length_before, length_after, residual):
    value = copy.deepcopy(receipt)
    data, length = _graph(value, "data_level0.bin"), _graph(value, "length.bin")
    data["sha256"], length["sha256"] = data_after, length_after
    value["immutable_backend_sha256"] = h("raw-" + data_after + length_after)
    value["immutable_non_length_backend_sha256"] = h("non-length-" + data_after)
    value["immutable_residual_backend_sha256"] = residual
    value["direct_read_normalization_delta"] = {
        "schema": rank.PAIRED_DIRECT_READ_NORMALIZATION_SCHEMA,
        "status": "data_level0_and_length_same_size_rewrite",
        "transitions": [
            {"path": data["path"], "bytes": data["bytes"], "before_sha256": data_before, "after_sha256": data_after},
            {"path": length["path"], "bytes": length["bytes"], "before_sha256": length_before, "after_sha256": length_after},
        ],
    }
    return value


def _replace_worker_receipt(draft, worker):
    replicate = copy.deepcopy(draft.replicate_without_coordinator_audit)
    index = replicate["index_receipt"]
    index["worker_physical_receipt"] = worker
    index["index_identity_sha256"] = rank._digest({"collection_identity": index["collection_identity"], "physical": worker})
    replicate["index_sha256"] = rank._digest(index)
    return original.OriginalProductWorkerDraft(
        projection=draft.projection, namespace=draft.namespace,
        replicate_without_coordinator_audit=replicate,
        worker_physical_receipt=worker, telemetry=draft.telemetry,
    )


def _none_receipt(receipt, *, data_sha256, length_sha256, residual):
    value = copy.deepcopy(receipt)
    _graph(value, "data_level0.bin")["sha256"] = data_sha256
    _graph(value, "length.bin")["sha256"] = length_sha256
    value["immutable_backend_sha256"] = h("none-raw-" + data_sha256 + length_sha256)
    value["immutable_non_length_backend_sha256"] = h("none-non-length-" + data_sha256)
    value["immutable_residual_backend_sha256"] = residual
    value["direct_read_normalization_delta"] = {
        "schema": rank.DIRECT_READ_NORMALIZATION_SCHEMA, "status": "none",
        "path": None, "bytes": None, "before_sha256": None, "after_sha256": None,
    }
    return value


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
    with pytest.raises(original.OriginalProductError, match="worker/coordinator") as captured:
        original.coordinator_reaudit_replicate(draft=draft, palace_path=tmp_path / "palace", projection=p, auditor=lambda **_kwargs: changed)
    message = str(captured.value)
    assert "worker_scientific=" in message and "measured_scientific=" in message
    assert draft.worker_physical_receipt["sqlite_semantic_sha256"] in message
    assert changed["sqlite_semantic_sha256"] in message


def test_coordinator_compares_logical_index_state_not_the_observed_length_normalization(
    tmp_path,
):
    p, observer = projection(), Observer()
    injected, _palace, _state = seams()
    namespace = original.original_identity_namespace(p)
    base = fake_auditor(palace_path=tmp_path / "palace", expected_namespace=namespace)
    final_length = next(entry for entry in base["graph_files"] if entry["name"] == "length.bin")
    observed = iter(
        [
            {
                **base,
                "direct_read_normalization_delta": {
                    "schema": "aerp7-hnsw-direct-read-normalization-v1",
                    "status": "length_bin_same_size_rewrite",
                    "path": final_length["path"],
                    "bytes": final_length["bytes"],
                    "before_sha256": "a" * 64,
                    "after_sha256": final_length["sha256"],
                },
            },
            {
                **base,
                "direct_read_normalization_delta": {
                    "schema": "aerp7-hnsw-direct-read-normalization-v1",
                    "status": "none",
                    "path": None,
                    "bytes": None,
                    "before_sha256": None,
                    "after_sha256": None,
                },
            },
        ]
    )
    auditor = lambda **_kwargs: next(observed)
    custom = original.OriginalProductSeams(
        palace=injected.palace,
        searcher=injected.searcher,
        reset_backends=injected.reset_backends,
        auditor=auditor,
    )
    draft = original.run_original_public_replicate(
        projection=p,
        build_id="normalization-build",
        collection_identity="normalization-collection",
        palace_path=tmp_path / "palace",
        observer=observer,
        seams=custom,
    )
    completed = original.coordinator_reaudit_replicate(
        draft=draft,
        palace_path=tmp_path / "palace",
        projection=p,
        auditor=auditor,
    )
    index = completed["index_receipt"]
    assert index["worker_physical_receipt"]["direct_read_normalization_delta"]["status"] == "length_bin_same_size_rewrite"
    assert index["coordinator_physical_receipt"]["direct_read_normalization_delta"]["status"] == "none"


def test_coordinator_compares_non_length_hnsw_state_when_canonical_length_rewrites_between_processes(tmp_path):
    p, observer = projection(), Observer()
    injected, _palace, _state = seams()
    namespace = original.original_identity_namespace(p)
    base = fake_auditor(palace_path=tmp_path / "palace", expected_namespace=namespace)

    def receipt(*, length_sha256, backend_sha256):
        value = copy.deepcopy(base)
        value["immutable_backend_sha256"] = backend_sha256
        value["immutable_non_length_backend_sha256"] = h("same-non-length-backend")
        value["immutable_residual_backend_sha256"] = h("same-residual-backend")
        for graph in value["graph_files"]:
            if graph["name"] == "length.bin":
                graph["sha256"] = length_sha256
        return value

    observed = iter([
        receipt(length_sha256=h("worker-length"), backend_sha256=h("worker-raw-backend")),
        receipt(length_sha256=h("worker-length"), backend_sha256=h("coordinator-raw-backend")),
    ])
    auditor = lambda **_kwargs: next(observed)
    custom = original.OriginalProductSeams(
        palace=injected.palace,
        searcher=injected.searcher,
        reset_backends=injected.reset_backends,
        auditor=auditor,
    )
    draft = original.run_original_public_replicate(
        projection=p,
        build_id="cross-process-length-rewrite-build",
        collection_identity="cross-process-length-rewrite-collection",
        palace_path=tmp_path / "palace",
        observer=observer,
        seams=custom,
    )
    completed = original.coordinator_reaudit_replicate(
        draft=draft,
        palace_path=tmp_path / "palace",
        projection=p,
        auditor=auditor,
    )
    assert completed["index_receipt"]["worker_physical_receipt"]["immutable_backend_sha256"] == h("worker-raw-backend")
    assert completed["index_receipt"]["coordinator_physical_receipt"]["immutable_backend_sha256"] == h("coordinator-raw-backend")


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


@pytest.mark.parametrize("worker_mode,coordinator_mode", (("paired", "none"), ("none", "paired"), ("paired", "paired")))
def test_worker_draft_wire_publishes_all_paired_v2_handoff_combinations(tmp_path, worker_mode, coordinator_mode):
    p = projection()
    model = {"encoder_identity": "synthetic", "encoder_semantics": "deterministic", "files": [{"path_role": "weights", "sha256": h("weights"), "bytes": 1}]}
    code = {"head": h("head"), "tree": h("tree"), "diff_digest": h("diff"), "dirty_policy": "clean_required"}
    completed = []
    for number in range(5):
        injected, _palace, _state = seams()
        draft = original.run_original_public_replicate(projection=p, build_id=f"wire-paired-{worker_mode}-{coordinator_mode}-{number}", collection_identity=f"wire-collection-{number}", palace_path=tmp_path / f"worker-{number}", observer=Observer(), seams=injected)
        base = copy.deepcopy(draft.worker_physical_receipt)
        initial_data, initial_length = _graph(base, "data_level0.bin")["sha256"], _graph(base, "length.bin")["sha256"]
        residual = h(f"paired-residual-{number}")
        if worker_mode == "paired":
            worker_data, worker_length = h(f"worker-data-{number}"), h(f"worker-length-{number}")
            worker = _paired_receipt(base, data_before=initial_data, data_after=worker_data, length_before=initial_length, length_after=worker_length, residual=residual)
        else:
            worker_data, worker_length, worker = initial_data, initial_length, _none_receipt(base, data_sha256=initial_data, length_sha256=initial_length, residual=residual)
        loaded = original.load_worker_draft(original.serialize_worker_draft(_replace_worker_receipt(draft, worker)))
        coordinator = _none_receipt(base, data_sha256=worker_data, length_sha256=worker_length, residual=residual)
        if coordinator_mode == "paired":
            coordinator = _paired_receipt(coordinator, data_before=worker_data, data_after=h(f"coordinator-data-{number}"), length_before=worker_length, length_after=h(f"coordinator-length-{number}"), residual=residual)
        completed.append(original.coordinator_reaudit_replicate(draft=loaded, palace_path=tmp_path / f"coordinator-{number}", projection=p, auditor=lambda **_kwargs: coordinator))
    artifact = rank.wrap_original_public_rankings(projection=p, replicates=completed, model_receipt=model, code_receipt=code)
    assert rank.validate_frozen_ranking(artifact, projection=p)["artifact_sha256"] == artifact["artifact_sha256"]


def test_worker_draft_wire_publishes_v1_length_only_and_rejects_handoff_or_residual_drift(tmp_path):
    p = projection(); injected, _palace, _state = seams()
    draft = original.run_original_public_replicate(projection=p, build_id="wire-length", collection_identity="wire-length-collection", palace_path=tmp_path / "worker", observer=Observer(), seams=injected)
    base = copy.deepcopy(draft.worker_physical_receipt)
    data_sha256, length_sha256 = _graph(base, "data_level0.bin")["sha256"], _graph(base, "length.bin")["sha256"]
    worker = _none_receipt(base, data_sha256=data_sha256, length_sha256=h("worker-length"), residual=h("length-residual"))
    worker["direct_read_normalization_delta"] = {"schema": rank.DIRECT_READ_NORMALIZATION_SCHEMA, "status": "length_bin_same_size_rewrite", "path": _graph(worker, "length.bin")["path"], "bytes": _graph(worker, "length.bin")["bytes"], "before_sha256": length_sha256, "after_sha256": h("worker-length")}
    loaded = original.load_worker_draft(original.serialize_worker_draft(_replace_worker_receipt(draft, worker)))
    coordinator = _none_receipt(base, data_sha256=data_sha256, length_sha256=h("worker-length"), residual=h("length-residual"))
    completed = original.coordinator_reaudit_replicate(draft=loaded, palace_path=tmp_path / "coordinator", projection=p, auditor=lambda **_kwargs: coordinator)
    assert completed["index_receipt"]["worker_physical_receipt"]["direct_read_normalization_delta"]["status"] == "length_bin_same_size_rewrite"
    bad_handoff = _paired_receipt(coordinator, data_before=h("wrong-data"), data_after=h("coordinator-data"), length_before=h("worker-length"), length_after=h("coordinator-length"), residual=h("length-residual"))
    with pytest.raises(original.OriginalProductError, match="normalization receipt mismatch"):
        original.coordinator_reaudit_replicate(draft=loaded, palace_path=tmp_path / "bad-handoff", projection=p, auditor=lambda **_kwargs: bad_handoff)
    bad_residual = _none_receipt(base, data_sha256=data_sha256, length_sha256=h("worker-length"), residual=h("forged-residual"))
    with pytest.raises(original.OriginalProductError, match="normalization receipt mismatch"):
        original.coordinator_reaudit_replicate(draft=loaded, palace_path=tmp_path / "bad-residual", projection=p, auditor=lambda **_kwargs: bad_residual)


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


def test_capacity_chroma_sidecar_is_scalar_complete_and_tamper_detected(tmp_path):
    class Inner:
        def __init__(self): self.phases = []
        def checkpoint(self, phase): self.phases.append(phase)
        def receipt(self): return {"production": "unchanged"}

    palace = tmp_path / "palace"; palace.mkdir()
    inner = Inner()
    observer = original.CapacityPeakObserver(observer=inner, palace_path=palace, build_id="build", generation_id="generation", projection_sha256="p" * 64)
    observer.checkpoint("before_ingest")
    (palace / "index.bin").write_bytes(b"abc")
    observer.checkpoint("after_ingest")
    (palace / "index.bin").write_bytes(b"abcdef")
    observer.checkpoint("after_cold_close")
    observer.checkpoint("after_queries")
    assert observer.receipt() == {"production": "unchanged"}
    sidecar = tmp_path / "peak.json"
    assert observer.publish_sidecar(sidecar)["peak_bytes"] == 6
    assert original.load_capacity_chroma_sidecar(sidecar)["peak_bytes"] == 6
    broken = json.loads(sidecar.read_text(encoding="utf-8")); broken["peak_bytes"] = 7
    sidecar.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(original.OriginalProductError, match="sidecar invalid"):
        original.load_capacity_chroma_sidecar(sidecar)


def test_capacity_chroma_sidecar_fails_closed_on_missing_phase(tmp_path):
    class Inner:
        def checkpoint(self, phase): pass
        def receipt(self): return {}

    palace = tmp_path / "palace"; palace.mkdir(); (palace / "index.bin").write_bytes(b"x")
    observer = original.CapacityPeakObserver(observer=Inner(), palace_path=palace, build_id="build", generation_id="generation", projection_sha256="p" * 64)
    observer.checkpoint("before_ingest")
    with pytest.raises(original.OriginalProductError, match="observer incomplete"):
        observer.publish_sidecar(tmp_path / "peak.json")
