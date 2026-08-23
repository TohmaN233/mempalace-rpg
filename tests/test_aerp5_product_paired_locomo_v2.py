from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from benchmarks import aerp5_product_paired_locomo_v2 as runner
from mempalace_rpg.retrieval import AuthorizedRetrievalCandidate


def _token(number: int) -> str:
    return f"{number:064x}"


def _expected_identity_namespace() -> dict:
    rows = [{"physical_id": f"conversation::aerp5::dialog_{index:06d}"} for index in range(5882)]
    return {"expected_unique_count": 5882, "mapping_sha256": "n" * 64, "rows": rows}


def _row(number: int) -> dict:
    return {"item_token": _token(number), "raw_top10": [f"r{number}-{i}" for i in range(10)], "p5_top10": [f"p{number}-{i}" for i in range(10)]}


def _compact_projection(tokens: tuple[str, ...]) -> dict:
    conversations = [
        {
            "conversation_id": f"conversation_{index:06d}",
            "conversation_token": _token(10_000 + index),
            "sessions": [{"opaque_session_id": f"s-{index}", "dialogs": []}],
        }
        for index in range(10)
    ]
    return {
        "schema": runner.PROJECTION_SCHEMA,
        "conversations": conversations,
        "items": [
            {
                "item_token": token,
                "item_id": f"item-{index}",
                "conversation_id": conversations[index % len(conversations)]["conversation_id"],
                "query": f"q-{index}",
            }
            for index, token in enumerate(tokens)
        ],
    }


def _valid_worker_freeze(arm: str) -> tuple[dict, tuple[str, ...]]:
    tokens = tuple(_token(index) for index in range(1982))
    items = {
        token: {
            "dialog_top10": [f"d{index}-{rank}" for rank in range(10)],
            "evidence_top10": runner._evidence_tokens([f"d{index}-{rank}" for rank in range(10)]),
        }
        for index, token in enumerate(tokens)
    }
    unsupported = "unsupported_through_original_public_interface"
    if arm == runner.ARM_ORIGINAL:
        traces = {}
        trace_receipt = {
            "supported": False,
            "complete_count": unsupported,
            "expected_count": unsupported,
            "trace_sha256": unsupported,
            "items": {},
        }
        policies = {}
        policy_sha = unsupported
        expected_namespace = _expected_identity_namespace()
        identity_namespace = {
            "schema": "aerp5-original-identity-namespace-v1",
            "scheme": runner.IDENTITY_NAMESPACE_SCHEME,
            "expected_unique_count": 5882,
            "actual_collection_count": 5882,
            "mapping_sha256": expected_namespace["mapping_sha256"],
        }
        index_receipt = {
            "schema": "aerp5-original-index-build-receipt-v1", "physical_id_count": 5882,
            "physical_id_sha256": runner.canonical_sha256(sorted(row["physical_id"] for row in expected_namespace["rows"])),
            "embedding_count": 5882, "embedding_dimension": 384, "embedding_float32_sha256": "e" * 64,
            "immutable_backend_sha256": "a" * 64, "sqlite_semantic_sha256": "b" * 64,
            "sqlite_operational_delta": {
                "schema": "aerp5-chroma-operational-delta-v1", "excluded_table": "acquire_write",
                "permitted_transition": "unchanged_or_append_next_integer_id_lock_status_1", "validation": "passed",
            },
            "hnsw_configuration": runner._sqlite_hnsw_configuration_expected(),
            "hnsw_graph_files": [{"name": name, "bytes": 1, "sha256": f"{index + 1:064x}"} for index, name in enumerate(("data_level0.bin", "header.bin", "length.bin", "link_lists.bin"))],
        }
        cold_reopen_cleanup = {"verified_system_released": True}
    else:
        traces = {}
        for index, token in enumerate(tokens):
            selected_keys = [
                hashlib.sha256(dialog.encode("utf-8")).hexdigest()
                for dialog in items[token]["dialog_top10"]
            ]
            stable_trace = {
                "schema": "aerp5-stable-product-trace-v1",
                "authorization_audit": {"complete": True},
                "authorized_dialog_universe_sha256": _token(index + 3),
                "selected_dialog_order_sha256": runner.canonical_sha256(items[token]["dialog_top10"]),
                "retrieval_ranking": {
                    "selected": [{"ranking_key_sha256": key} for key in selected_keys],
                },
            }
            traces[token] = {
                "trace_sha256": runner.canonical_sha256(stable_trace),
                "stable_trace": stable_trace,
                "selected_count": 10,
                "selected_evidence_top10": list(items[token]["evidence_top10"]),
                "selected_ranking_key_sha256": selected_keys,
                "selected_ranking_keys_sha256": runner.canonical_sha256(selected_keys),
                "complete": True,
            }
            if arm == runner.ARM_P5:
                traces[token]["policy_final_ranking_sha256"] = _token(index + 2)
        trace_receipt = {
            "supported": True,
            "complete_count": 1982,
            "expected_count": 1982,
            "trace_sha256": runner.canonical_sha256(traces),
            "items": traces,
        }
        expected_policy = runner._expected_policy_receipt(arm)
        policies = {}
        for index, token in enumerate(tokens):
            policies[token] = {
                **expected_policy,
                "selected_top10_ranking_sha256": traces[token]["selected_ranking_keys_sha256"],
            }
            if arm == runner.ARM_P5:
                policies[token]["final_ranking_sha256"] = traces[token]["policy_final_ranking_sha256"]
        policy_sha = runner.canonical_sha256(policies)
        identity_namespace = "not_applicable"
        cold_reopen_cleanup = "not_applicable"
        index_receipt = "not_applicable"
    value = {
        "schema": runner.SCHEMA + "-worker",
        "arm": arm,
        "items": items,
        "input_projection_sha256": "p" * 64,
        "input_projection_content_sha256": "c" * 64,
        "dialog_ranking_sha256": runner.canonical_sha256(
            {token: row["dialog_top10"] for token, row in items.items()}
        ),
        "evidence_ranking_sha256": runner.canonical_sha256(
            {token: row["evidence_top10"] for token, row in items.items()}
        ),
        "projection_sha256": runner.canonical_sha256(items),
        "latency": runner.latency_receipt(range(1, 1983)),
        "conversation_ingest": runner.distribution_receipt(range(1, 11), expected_count=10, label="conversation ingest"),
        "trace_receipt": trace_receipt,
        "policy_receipts": policies,
        "policy_receipts_sha256": policy_sha,
        "onnx_providers": ["CPUExecutionProvider"],
        "model_file_tree_sha256": "m" * 64,
        "original_product_configuration": {
            "backend": "chroma",
            "collection": runner.v1.ORIGINAL_COLLECTION,
            "embedding_model": "minilm",
            "embedding_device": "cpu",
            "providers": ["CPUExecutionProvider"],
            "model_file_tree_sha256": "m" * 64,
            "same_cached_embedding_object_for_both_arms": True,
        },
        "identity_namespace_receipt": identity_namespace,
        "original_index_build_receipt": index_receipt,
        "cold_reopen_cleanup": cold_reopen_cleanup,
        "lifecycle_receipt": {"all_ingest_before_query": True, "cold_reopen_before_query": True, "query_latency_boundary": "public_search_return_through_namespace_validation" if arm == runner.ARM_ORIGINAL else "current_product_rank_return_only"},
    }
    return value, tokens


def test_manifest_freezes_public_known_inputs_and_rejects_tunable_surface():
    manifest = runner.load_manifest()
    assert manifest["dataset"]["sha256"] == "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
    assert manifest["original"]["commit"] == runner.v1.ORIGINAL_PIN
    assert manifest["run"]["repeats_by_arm"] == runner.REPEATS_BY_ARM and manifest["run"]["top_k"] == 10
    with pytest.raises(ValueError, match="tau/router"):
        runner.main(["--tau", "0.1"])


def test_manifest_rejects_noncanonical_path_and_gate_bytes(tmp_path):
    manifest = runner.load_manifest()
    alternate = tmp_path / "manifest.json"
    alternate.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="canonical manifest bytes"):
        runner.load_manifest(alternate)


def test_custodian_rejects_self_consistent_noncanonical_scientific_gate():
    gates = {**runner.EXPECTED_SCIENTIFIC_GATES, "p5_vs_original_question_ci_lower_strictly_gt": -1.0}
    with pytest.raises(ValueError, match="canonical frozen scientific gates"):
        runner.custodian_score_run({
            "projection": "unused",
            "dataset": "unused",
            "freezes": {},
            "scientific_gates": gates,
            "expected_scientific_gates_sha256": runner.canonical_sha256(gates),
            "manifest_sha256": runner.canonical_sha256(runner.load_manifest()),
            "original_root": "unused",
        })


def test_fixed_p5_is_exported_from_retrieval_public_api():
    from mempalace_rpg import retrieval
    assert "FixedP5Policy" in retrieval.__all__


def test_actual_onnx_provider_uses_live_session_not_declared_config():
    session = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
    assert runner.actual_onnx_session_providers(SimpleNamespace(_session=session)) == ["CPUExecutionProvider"]
    with pytest.raises(RuntimeError, match="escaped"):
        runner.actual_onnx_session_providers(SimpleNamespace(_session=SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider"])))


@pytest.mark.parametrize(
    ("arm", "policy", "route"),
    [
        (runner.ARM_P5, runner.FixedP5Policy(), "p5"),
        (runner.ARM_SIX_VIEW, runner.FixedSixViewPolicy(), "six_view"),
    ],
)
def test_policy_receipt_extracts_from_real_ranker_trace_without_invented_top_level_fields(arm, policy, route):
    class Encoder:
        identity = "test"
        def encode_passages(self, texts): return [[float(index + 1), 1.0] for index, _ in enumerate(texts)]
        def encode_query(self, _query): return [1.0, 1.0]

    candidates = [
        AuthorizedRetrievalCandidate(
            source_event_id=f"event-{index}", source_scene_id="scene", raw_text=f"raw {index}",
            observation=f"observation {index}", checkpoint_key=f"checkpoint {index}",
            policy_tuple=("canonical", "public"), chronological_order_key=(index, f"event-{index}"),
            ranking_key=f"ranking-key-{index}",
        )
        for index in range(2)
    ]
    ranking = runner.SixViewRanker(Encoder(), routing_policy=policy).rank(query="query", candidates=candidates).trace
    assert "route" not in ranking and "effective_weights" not in ranking
    selected_keys = [entry["ranking_key_sha256"] for entry in ranking["selected"]]
    receipt, final_digest = runner.policy_receipt_from_ranking_trace(
        arm=arm, ranking=ranking, selected_ranking_keys=selected_keys
    )
    assert receipt["route"] == route
    assert receipt["selected_top10_ranking_sha256"] == runner.canonical_sha256(selected_keys)
    assert (final_digest is not None) is (arm == runner.ARM_P5)


def test_stable_product_trace_receipt_removes_random_backend_event_ids():
    def product_trace(prefix):
        authorized = [f"{prefix}-a", f"{prefix}-b"]
        selected = [authorized[1]]
        return {
            "policy": "AERP-1",
            "candidate_generation": {"candidate_count": 2},
            "candidates": [{"source_event_id": selected[0], "decision": "allow"}],
            "authorized_candidate_ids": authorized,
            "selected_evidence_ids": selected,
            "denied_partitions": [],
            "returned_spans": [],
            "retrieval_ranking": {
                "schema": "aerp2-product-six-view-v1", "encoder_identity": "same",
                "weights": dict(runner.SIX_VIEW_WEIGHTS), "rrf_k": 60,
                "query_sha256": "q", "input_sha256": "i", "view_digests": {},
                "selected": [{"source_event_id": selected[0], "ranking_key_sha256": _token(9), "final_rrf": 1.0, "component_ranks": {}, "contributions": {}}],
            },
            **{key: None for key in runner.aerp1.TRACE_REQUIRED - {"policy", "candidate_generation", "candidates", "authorized_candidate_ids", "selected_evidence_ids", "denied_partitions", "returned_spans", "retrieval_ranking"}},
        }, {authorized[0]: "dialog-a", authorized[1]: "dialog-b"}

    first_trace, first_map = product_trace("random-one")
    second_trace, second_map = product_trace("random-two")
    first = runner.stable_product_trace_receipt(first_trace, event_to_dialog=first_map)
    second = runner.stable_product_trace_receipt(second_trace, event_to_dialog=second_map)
    assert first == second
    assert "random-one" not in json.dumps(first) and "random-two" not in json.dumps(second)


def test_stable_trace_repeats_through_real_kernel_with_fresh_random_event_ids(tmp_path):
    class Encoder:
        identity = "deterministic-test"
        @staticmethod
        def _vector(text): return [float(len(text)), float(sum(map(ord, text)) % 997 + 1)]
        def encode_passages(self, texts): return [self._vector(text) for text in texts]
        def encode_query(self, query): return self._vector(query)

    conversation = {"sessions": [{
        "opaque_session_id": "session-1",
        "dialogs": [
            {"opaque_dialog_id": f"dialog-{index}", "speaker": "speaker", "date": f"day-{index}", "caption": "caption", "text": f"memory detail {index}"}
            for index in range(12)
        ],
    }]}

    def one_run(name):
        with runner.RpgMemoryKernel(
            db_path=str(tmp_path / f"{name}.sqlite3"),
            retrieval_ranker=runner.SixViewRanker(Encoder(), diagnostic_ledger=True, routing_policy=runner.FixedP5Policy()),
        ) as kernel:
            event_map, _ = runner.aerp2.seed_sanitized_conversation(kernel, conversation, conversation_id="conversation")
            ranking, trace = runner.v1.current_product_rank(
                kernel, conversation_id="conversation", query="memory detail", event_to_dialog=event_map, item_id="item"
            )
            return ranking, runner.stable_product_trace_receipt(trace, event_to_dialog=event_map), tuple(event_map)

    first_ranking, first_receipt, first_event_ids = one_run("first")
    second_ranking, second_receipt, second_event_ids = one_run("second")
    assert first_event_ids != second_event_ids
    assert first_ranking == second_ranking
    assert first_receipt == second_receipt


def test_aerp4_membership_requires_1982_allowed_and_four_exclusions(monkeypatch):
    manifest = runner.load_manifest()
    study, custody, train, dev = {}, {"label_custody": {"exclusion_receipt": {"excluded_item_tokens": manifest["aerp4"]["excluded_item_tokens"]}}}, {"partition": "train", "status": "complete", "items": [_row(i) for i in range(937)]}, {"partition": "dev", "status": "complete", "items": [_row(i) for i in range(937, 1982)]}
    study_digest = runner.canonical_sha256(study); custody["study_sha256"] = study_digest
    digests = [study_digest, runner.canonical_sha256(custody), runner.canonical_sha256(train), runner.canonical_sha256(dev)]
    monkeypatch.setitem(manifest["aerp4"], "study_sha256", digests[0]); monkeypatch.setitem(manifest["aerp4"], "custody_bundle_sha256", digests[1]); monkeypatch.setitem(manifest["aerp4"], "train_ranking_freeze_sha256", digests[2]); monkeypatch.setitem(manifest["aerp4"], "dev_ranking_freeze_sha256", digests[3])
    assert runner.validate_aerp4_membership(study=study, custody=custody, train_freeze=train, dev_freeze=dev, manifest=manifest)["count"] == 1982


def test_pinned_json_uses_file_bytes_digest_not_reserialized_json(tmp_path):
    source = tmp_path / "input.json"; source.write_text('{\n  "x": 1\n}\n', encoding="utf-8")
    pinned = runner.PinnedJson.pin(source, runner.sha256_file(source))
    value, digest = runner._pinned_value(pinned)
    assert value == {"x": 1} and digest == runner.sha256_file(source)


def test_projection_is_exact_and_label_free():
    allowed = [_token(i) for i in range(1982)]
    rows = [
        {
            "item_token": token,
            "item_id": f"i{index}",
            "conversation_id": f"c{index % 10}",
            "conversation_token": _token(5_000 + index % 10),
            "query": "q",
            "sessions": [{"opaque_session_id": f"s-{index % 10}", "dialogs": [{"id": "d", "text": "x" * 20_000}]}],
        }
        for index, token in enumerate(allowed)
    ]
    projection = runner.project_public_items(public_items=rows, allowed_item_tokens=allowed)
    items, conversations = runner._validate_public_projection(projection)
    assert [row["item_token"] for row in items] == allowed
    assert len(conversations) == 10
    assert all("sessions" not in row and set(row) == {"item_token", "item_id", "conversation_id", "query"} for row in items)
    # A corpus duplicated per item would be about 200MB here; the projection stores ten copies.
    assert len(runner._canonical(projection)) < len(runner._canonical(rows)) // 50
    with pytest.raises(ValueError, match="exactly"):
        runner.project_public_items(public_items=rows[:1], allowed_item_tokens=allowed)
    assert "scorer" not in inspect.signature(runner.worker_run).parameters


def test_projection_rejects_repeated_conversation_drift_and_forbidden_nested_fields():
    tokens = tuple(_token(index) for index in range(1982))
    projection = _compact_projection(tokens)
    first, second = projection["items"][:2]
    public_rows = []
    for item in projection["items"]:
        conversation = next(row for row in projection["conversations"] if row["conversation_id"] == item["conversation_id"])
        public_rows.append({**item, "conversation_token": conversation["conversation_token"], "sessions": copy.deepcopy(conversation["sessions"])})
    public_rows[1]["conversation_id"] = first["conversation_id"]
    public_rows[1]["conversation_token"] = next(row for row in projection["conversations"] if row["conversation_id"] == first["conversation_id"])["conversation_token"]
    public_rows[1]["sessions"] = [{"opaque_session_id": "drift", "dialogs": []}]
    with pytest.raises(ValueError, match="diverged"):
        runner.project_public_items(public_items=public_rows, allowed_item_tokens=tokens)
    projection["conversations"][0]["sessions"][0]["scorer_hint"] = "forbidden"
    with pytest.raises(ValueError, match="label/scorer"):
        runner._validate_public_projection(projection)


def test_projection_parser_rejects_item_corpus_duplication_or_membership_drift():
    tokens = tuple(_token(index) for index in range(1982))
    projection = _compact_projection(tokens)
    projection["items"][0]["sessions"] = []
    with pytest.raises(ValueError, match="only reference"):
        runner._validate_public_projection(projection)
    projection = _compact_projection(tokens)
    projection["items"][1]["item_token"] = projection["items"][0]["item_token"]
    with pytest.raises(ValueError, match="membership/query"):
        runner._validate_public_projection(projection)


def test_original_identity_namespace_prevents_local_dialog_id_overwrite_and_binds_5882_total():
    counts = (419, 369, 663, 629, 680, 675, 689, 681, 509, 568)
    conversations = {
        f"conversation-{index}": {"sessions": [{"opaque_session_id": str(index), "dialogs": [{"opaque_dialog_id": f"dialog_{dialog:06d}", "speaker": "s", "date": "d", "caption": "c", "text": "x"} for dialog in range(count)]}]}
        for index, count in enumerate(counts)
    }
    namespace = runner.original_identity_namespace(conversations)
    assert namespace["expected_unique_count"] == 5882
    assert len({row["physical_id"] for row in namespace["rows"]}) == 5882
    same_local = [row for row in namespace["rows"] if row["local_dialog_id"] == "dialog_000000"]
    assert len(same_local) == 10 and len({row["physical_id"] for row in same_local}) == 10


@pytest.mark.parametrize("returned", ["other::aerp5::dialog_000000", "conversation::aerp5::unknown"])
def test_original_public_query_rejects_wrong_or_unknown_physical_ids(monkeypatch, returned):
    conversation = "conversation"; local = [f"dialog_{index:06d}" for index in range(10)]
    physical = {identifier: conversation + runner.IDENTITY_NAMESPACE_SEPARATOR + identifier for identifier in local}
    monkeypatch.setattr(runner.v1, "original_product_query", lambda **_: [returned] * 10)
    with pytest.raises(RuntimeError, match="outside|unknown"):
        runner.original_product_query_namespaced(
            searcher=object(), palace_path=Path("unused"), conversation_id=conversation,
            local_corpus_ids=local, physical_by_local=physical,
            local_by_physical={value: key for key, value in physical.items()}, query="q", item_id="i",
        )


def test_original_index_receipt_fails_closed_on_id_embedding_or_config_drift_and_allows_graph_variation():
    value, _tokens = _valid_worker_freeze(runner.ARM_ORIGINAL)
    expected = _expected_identity_namespace()
    runner.validate_original_index_build_receipt(value["original_index_build_receipt"], expected_namespace=expected)
    for field, replacement in (("physical_id_sha256", "0" * 64), ("hnsw_configuration", {})):
        corrupted = copy.deepcopy(value["original_index_build_receipt"]); corrupted[field] = replacement
        with pytest.raises(ValueError, match="physical-ID|embedding|HNSW"):
            runner.validate_original_index_build_receipt(corrupted, expected_namespace=expected)
    repeats = [copy.deepcopy(value) for _ in range(runner.ORIGINAL_REPEATS)]
    repeats[1]["original_index_build_receipt"]["hnsw_graph_files"][0]["sha256"] = "f" * 64
    runner.validate_repeat_identity(runner.ARM_ORIGINAL, repeats)
    repeats[1]["original_index_build_receipt"]["embedding_float32_sha256"] = "f" * 64
    with pytest.raises(RuntimeError, match="index-build corpus/configuration"):
        runner.validate_repeat_identity(runner.ARM_ORIGINAL, repeats)


def test_original_identity_namespace_requires_exact_fixed_dialog_count():
    with pytest.raises(RuntimeError, match="exactly 5882"):
        runner.original_identity_namespace({"conversation": {"sessions": [{"dialogs": [{"opaque_dialog_id": "dialog_000000", "speaker": "s", "date": "d", "caption": "c", "text": "x"}]}]}})


def test_direct_index_audit_never_loads_product_and_rejects_persisted_byte_mutation(monkeypatch, tmp_path):
    expected = _expected_identity_namespace(); ids = [row["physical_id"] for row in expected["rows"]]
    closed = []
    schema = {"keys": {"#embedding": {"float_list": {"vector_index": {"config": {"space": "cosine", "hnsw": {"ef_construction": 100, "ef_search": 100, "max_neighbors": 16, "num_threads": 1, "batch_size": 2, "sync_threshold": 2, "resize_factor": 1.2}}}}}}}
    database = tmp_path / "chroma.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE collections (name TEXT, schema_str TEXT)")
        connection.execute("INSERT INTO collections VALUES (?, ?)", (runner.v1.ORIGINAL_COLLECTION, json.dumps(schema)))
        connection.execute("CREATE TABLE acquire_write (id INTEGER PRIMARY KEY, lock_status INTEGER NOT NULL)")
        connection.execute("INSERT INTO acquire_write VALUES (10, 1)")
    segment = tmp_path / "only-segment"; segment.mkdir()
    for name in ("data_level0.bin", "header.bin", "length.bin", "link_lists.bin"):
        (segment / name).write_bytes(name.encode("ascii"))
    append_lock = [True]; mutate_file = [False]
    class Collection:
        def get(self, *, include):
            assert include == ["embeddings"]
            if append_lock[0]:
                with sqlite3.connect(database) as connection:
                    connection.execute("INSERT INTO acquire_write (lock_status) VALUES (1)")
            if mutate_file[0]:
                with (segment / "header.bin").open("ab") as handle: handle.write(b"!")
            return {"ids": ids, "embeddings": [[0.0] * 384 for _ in ids]}
    class Client:
        def get_collection(self, name): assert name == runner.v1.ORIGINAL_COLLECTION; return Collection()
        def close(self): closed.append(True)
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=lambda **_: Client()))
    monkeypatch.setattr(runner.v1, "load_original_product", lambda *_: pytest.fail("direct audit must not load product"))
    receipt = runner.original_index_build_receipt(palace_path=tmp_path, expected_namespace=expected)
    assert closed == [True] and receipt["sqlite_operational_delta"]["validation"] == "passed"
    mutate_file[0] = True
    with pytest.raises(RuntimeError, match="mutated persisted"):
        runner.original_index_build_receipt(palace_path=tmp_path, expected_namespace=expected)


def test_coordinator_index_audit_does_not_call_original_loader(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.v1, "load_original_product", lambda *_: pytest.fail("coordinator audit must not load original product"))
    sentinel = {"receipt": "direct"}
    monkeypatch.setattr(runner, "original_index_build_receipt", lambda **_: sentinel)
    assert runner.coordinator_original_index_build_receipt(palace_path=tmp_path, expected_namespace=_expected_identity_namespace()) == sentinel


def test_original_worker_keeps_one_loader_and_cold_reopen_barrier_in_same_process():
    source = inspect.getsource(runner.worker_run)
    assert source.count("v1.load_original_product(original_root)") == 1
    assert source.index("cold_reopen_cleanup = v1.reset_original_product_backends(original_palace)") < source.index("original_product_query_namespaced(")


def test_p5_validation_fails_on_any_freeze_drift():
    row = _row(1)
    runner.validate_p5_against_aerp4({_token(1): row["p5_top10"]}, [row])
    with pytest.raises(RuntimeError, match="differs"):
        runner.validate_p5_against_aerp4({_token(1): row["raw_top10"]}, [row])


@pytest.mark.parametrize("arm", runner.ALL_ARMS)
def test_strict_worker_parser_binds_projection_trace_policy_and_product_config(arm):
    value, tokens = _valid_worker_freeze(arm)
    parsed = runner.parse_worker_freeze(
        value,
        arm=arm,
        projection_sha256="p" * 64,
        projection_content_sha256="c" * 64,
        item_tokens=tokens,
        model_sha256="m" * 64,
        identity_namespace=_expected_identity_namespace() if arm == runner.ARM_ORIGINAL else None,
    )
    assert parsed["arm"] == arm
    corrupted = copy.deepcopy(value)
    if arm == runner.ARM_ORIGINAL:
        corrupted["original_product_configuration"]["backend"] = "sqlite"
        match = "configuration"
    else:
        corrupted["policy_receipts"][tokens[0]]["route"] = "wrong"
        corrupted["policy_receipts_sha256"] = runner.canonical_sha256(
            corrupted["policy_receipts"]
        )
        match = "frozen configuration"
    with pytest.raises(ValueError, match=match):
        runner.parse_worker_freeze(
            corrupted,
            arm=arm,
            projection_sha256="p" * 64,
            projection_content_sha256="c" * 64,
            item_tokens=tokens,
            model_sha256="m" * 64,
            identity_namespace=_expected_identity_namespace() if arm == runner.ARM_ORIGINAL else None,
        )


def test_strict_worker_parser_recomputes_trace_and_projection_content_receipts():
    value, tokens = _valid_worker_freeze(runner.ARM_P5)
    value["trace_receipt"]["trace_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="trace receipt"):
        runner.parse_worker_freeze(
            value,
            arm=runner.ARM_P5,
            projection_sha256="p" * 64,
            projection_content_sha256="c" * 64,
            item_tokens=tokens,
            model_sha256="m" * 64,
            identity_namespace=None,
        )


@pytest.mark.parametrize("mutation", ["dialog_evidence", "trace_selection", "policy_final"])
def test_strict_worker_parser_binds_dialog_evidence_trace_and_p5_final_receipt(mutation):
    value, tokens = _valid_worker_freeze(runner.ARM_P5)
    token = tokens[0]
    if mutation == "dialog_evidence":
        value["items"][token]["evidence_top10"][0] = "f" * 64
        value["evidence_ranking_sha256"] = runner.canonical_sha256(
            {key: row["evidence_top10"] for key, row in value["items"].items()}
        )
        value["projection_sha256"] = runner.canonical_sha256(value["items"])
        match = "dialog/evidence"
    elif mutation == "trace_selection":
        value["trace_receipt"]["items"][token]["selected_evidence_top10"][0] = "f" * 64
        value["trace_receipt"]["trace_sha256"] = runner.canonical_sha256(value["trace_receipt"]["items"])
        match = "trace selection"
    else:
        value["policy_receipts"][token]["final_ranking_sha256"] = "f" * 64
        value["policy_receipts_sha256"] = runner.canonical_sha256(value["policy_receipts"])
        match = "final ranking"
    with pytest.raises(ValueError, match=match):
        runner.parse_worker_freeze(
            value,
            arm=runner.ARM_P5,
            projection_sha256="p" * 64,
            projection_content_sha256="c" * 64,
            item_tokens=tokens,
            model_sha256="m" * 64,
            identity_namespace=None,
        )
    value, tokens = _valid_worker_freeze(runner.ARM_P5)
    with pytest.raises(ValueError, match="identity/projection"):
        runner.parse_worker_freeze(
            value,
            arm=runner.ARM_P5,
            projection_sha256="p" * 64,
            projection_content_sha256="wrong",
            item_tokens=tokens,
            model_sha256="m" * 64,
            identity_namespace=None,
        )


def test_latency_requires_full_denominator_and_reports_distribution():
    values = list(range(1, 1983))
    receipt = runner.latency_receipt(values)
    assert receipt["count"] == 1982 and receipt["cold_first_query_included"] is True
    assert receipt["cold_first_ns"] == values[0] and receipt["samples_sha256"] == runner.canonical_sha256(values)
    with pytest.raises(ValueError, match="1,982"):
        runner.latency_receipt(values[:-1])


def test_strict_worker_parser_rejects_unreplayable_latency_receipt():
    value, tokens = _valid_worker_freeze(runner.ARM_P5)
    value["latency"]["cold_first_ns"] += 1
    with pytest.raises(ValueError, match="latency receipt"):
        runner.parse_worker_freeze(
            value,
            arm=runner.ARM_P5,
            projection_sha256="p" * 64,
            projection_content_sha256="c" * 64,
            item_tokens=tokens,
            model_sha256="m" * 64,
        )


def test_worker_config_is_the_only_subprocess_entry_and_carries_no_scorer(tmp_path):
    config = {"arm": runner.ARM_P5, "projection": str(tmp_path / "projection.json"), "output": str(tmp_path / "out.json"), "temporary_backend": str(tmp_path / "backend"), "original_root": "original", "model_dir": "model"}
    assert not any("label" in key or "scorer" in key for key in config)
    assert runner.END_TO_END_PRODUCT_WORKER_IMPLEMENTED is True


def test_score_is_impossible_before_all_repeats_and_is_cluster_paired():
    with pytest.raises(RuntimeError, match="all arms"):
        runner.score_after_all_freezes(scorer=SimpleNamespace(scorer_items={}), freezes={})
    rows = [{"conversation_id": "a", "recall": {"x": 1., "y": 0.}, "original_replicate_recall": [0.] * 5}]
    assert runner.paired_cluster_bootstrap(rows, current="x", original="y", estimand="conversation_macro")["point_estimate"] == 1


def test_score_reads_dialog_top10_and_keeps_unresolved_in_denominator():
    items = {}
    rankings = {arm: {} for arm in runner.ALL_ARMS}
    for index in range(1982):
        token = _token(index); dialogs = [f"d{index}-{rank}" for rank in range(10)]
        items[token] = SimpleNamespace(opaque_conversation_id="c", category=5, official_exact=SimpleNamespace(resolved_opaque_dialog_ids=(dialogs[0],), source_evidence_item_count=2, unresolved_evidence_item_count=1))
        for arm in runner.ALL_ARMS: rankings[arm][token] = {"dialog_top10": dialogs}
    freezes = {arm: [{"projection_sha256": "same", "items": rankings[arm]} for _ in range(runner.REPEATS_BY_ARM[arm])] for arm in runner.ALL_ARMS}
    varied_original = copy.deepcopy(rankings[runner.ARM_ORIGINAL])
    first_token = _token(0)
    varied_original[first_token]["dialog_top10"] = [f"different-{rank}" for rank in range(10)]
    freezes[runner.ARM_ORIGINAL][1] = {
        "projection_sha256": "different-original-ranking",
        "items": varied_original,
    }
    aggregate, rows = runner.score_after_all_freezes(scorer=SimpleNamespace(scorer_items=items), freezes=freezes)
    assert aggregate["question_macro"][runner.ARM_P5] == .5 and sum(row["unresolved"] for row in rows) == 1982
    assert rows[0]["original_replicate_recall"][0] == .5
    assert rows[0]["original_replicate_recall"][1] == 0.0
    summary = runner.summarize_scored_rows(rows)
    replicate_report = runner.original_replicate_reports(rows)
    assert replicate_report["replicate_count"] == runner.ORIGINAL_REPEATS
    assert len(replicate_report["replicates"]) == runner.ORIGINAL_REPEATS
    assert replicate_report["variability"]["overall"]["question_macro_recall_at_10"]["range"] > 0.0
    for arm in (runner.ARM_P5, runner.ARM_SIX_VIEW):
        metrics = summary["arms"][arm]
        assert metrics["question_macro_recall_at_10"] == .5
        assert metrics["conversation_macro_recall_at_10"] == .5
        assert metrics["evidence_micro_recall_at_10"] == .5
        assert metrics["hit_at_10"] == 1.0 and metrics["all_at_10"] == 0.0
        assert metrics["ndcg_at_10"] == rows[0]["official_metrics"][arm]["ndcg_at_10"]
        assert metrics["evidence_item_count"] == 3964
        assert metrics["resolved_evidence_item_count"] == 1982
        assert metrics["unresolved_evidence_item_count"] == 1982


def test_official_metrics_preserve_duplicate_evidence_item_multiplicity():
    metric = runner.aerp1.question_metrics(["dialog-a"], ("dialog-a", "dialog-a"), evidence_item_count=2, unresolved_evidence_item_count=0, top_k=10)
    assert metric["retrieved_evidence_count_at_10"] == 2 and metric["recall_at_10"] == 1.0


def test_atomic_publication_is_nonclobber_and_never_claims_confirmation(tmp_path):
    output = tmp_path / "report.json"; manifest = runner.load_manifest()
    runner.publish_report(output=output, manifest=manifest, report={"engineering_gates_passed": False})
    payload = json.loads(output.read_text())
    assert payload["confirmation_claim"] is False and payload["public_nonblind"] is True and payload["resource_gate_eligible"] is False
    with pytest.raises(FileExistsError): runner.publish_report(output=output, manifest=manifest, report={})


def test_nonreplace_publish_handles_racing_existing_target(tmp_path):
    target = tmp_path / "target.json"; target.write_bytes(b"winner")
    with pytest.raises(FileExistsError): runner._publish_nonreplace(target, b"loser")
    assert target.read_bytes() == b"winner"


def test_supervisor_sidecar_binds_command_and_completed_freeze(monkeypatch, tmp_path):
    freeze = tmp_path / "freeze.json"; freeze.write_bytes(b"freeze")
    class Proc:
        pid = os.getpid()
        def wait(self): return 0
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *_args, **_kwargs: Proc())
    receipt = runner.supervise_worker(["worker", "--x"], sidecar=tmp_path / "sidecar.json", freeze_path=freeze)
    assert receipt["freeze_sha256"] == runner.sha256_file(freeze) and receipt["command_sha256"] == runner.canonical_sha256(["worker", "--x"])


def test_coordinator_vertical_slice_launches_six_then_custodian_then_nonreplace_publish(monkeypatch, tmp_path):
    manifest = runner.load_manifest(); dataset = tmp_path / "dataset.json"; dataset.write_bytes(b"dataset")
    model = tmp_path / "model"; model.mkdir(); (model / "m").write_bytes(b"m")
    original = tmp_path / "original"; original.mkdir(); work = tmp_path / "work"; output = tmp_path / "final.json"
    tokens = tuple(_token(index) for index in range(1982)); timeline = []
    state = {"git_head": "h", "git_tree": "t", "git_dirty": False}
    monkeypatch.setattr(runner.v1, "require_external_output", lambda *_: None)
    monkeypatch.setattr(runner.v1, "git_state", lambda *_: dict(state))
    monkeypatch.setattr(runner.v1, "require_clean_pinned_original", lambda *_: dict(state))
    monkeypatch.setattr(runner, "validate_aerp4_membership", lambda **_: {"item_tokens": tokens})
    monkeypatch.setattr(runner, "environment_receipt", lambda **_: {"ok": True})
    monkeypatch.setattr(runner, "validate_environment_receipt", lambda *_: None)
    monkeypatch.setattr(runner, "scorer_implementation_receipt", lambda *_: {"scorer": "pinned"})
    monkeypatch.setattr(runner.v1, "file_tree_receipt", lambda *_: {"model": "same"})
    monkeypatch.setattr(runner, "build_public_projection", lambda **_: _compact_projection(tokens))
    monkeypatch.setattr(runner, "original_identity_namespace", lambda *_: {"expected_unique_count": 5882, "mapping_sha256": "n" * 64})
    monkeypatch.setattr(runner, "_sqlite_embedding_count", lambda *_: 5882)
    monkeypatch.setattr(runner, "coordinator_original_index_build_receipt", lambda **_: {"receipt": "coordinator"})
    monkeypatch.setattr(runner, "validate_p5_against_aerp4", lambda *_: None)
    def fake_parse(_value, *, arm, **_kwargs):
        return {"projection_sha256": "repeat", "onnx_providers": ["CPUExecutionProvider"], "model_file_tree_sha256": manifest["run"]["model"]["file_tree_sha256"], "dialog_ranking_sha256": "dialog", "evidence_ranking_sha256": "evidence", "policy_receipts_sha256": "policy" if arm != runner.ARM_ORIGINAL else "unsupported", "trace_receipt": {"trace_sha256": "trace" if arm != runner.ARM_ORIGINAL else "unsupported"}, "items": {token: {"evidence_top10": []} for token in tokens}}
    monkeypatch.setattr(runner, "parse_worker_freeze", fake_parse)
    def fake_supervise(_command, *, sidecar, freeze_path):
        timeline.append("worker")
        worker_config = json.loads(Path(_command[-1]).read_text(encoding="utf-8"))
        payload = {"original_index_build_receipt": {"receipt": "coordinator"}} if worker_config["arm"] == runner.ARM_ORIGINAL else {}
        freeze_path.write_text(json.dumps(payload), encoding="utf-8"); sidecar.write_text("{}", encoding="utf-8"); return {"ok": True}
    monkeypatch.setattr(runner, "supervise_worker", fake_supervise)
    def fake_run(command, **_kwargs):
        assert len(timeline) == 9; timeline.append("scorer")
        config = json.loads((work / "custodian-config.json").read_text())
        (work / "custodian-score.json").write_text(json.dumps({"scorer_implementation_receipt": config["scorer_implementation_receipt"], "input_receipts": {"dataset_sha256": config["expected_dataset_sha256"], "projection_sha256": config["expected_projection_sha256"], "projection_content_sha256": config["expected_projection_content_sha256"], "freeze_sha256": config["expected_freeze_sha256"], "original_index_receipts_sha256": config["expected_original_index_receipts_sha256"], "scientific_gates_sha256": config["expected_scientific_gates_sha256"], "manifest_sha256": config["manifest_sha256"]}}), encoding="utf-8")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    train_json = {"partition": "train", "status": "complete", "items": []}; dev_json = {"partition": "dev", "status": "complete", "items": []}
    pins = [SimpleNamespace(load=lambda: {}), SimpleNamespace(load=lambda: {}), SimpleNamespace(load=lambda: train_json), SimpleNamespace(load=lambda: dev_json)]
    result = runner.coordinator_run(dataset=dataset, original_root=original, model_dir=model, study=pins[0], custody=pins[1], train=pins[2], dev=pins[3], work=work, output=output, manifest=manifest)
    assert timeline == ["worker"] * 9 + ["scorer"] and output.is_file() and result["score"]["input_receipts"] == json.loads((work / "custodian-score.json").read_text())["input_receipts"]


def test_environment_gate_rejects_wrong_model_or_dirty_original():
    manifest = runner.load_manifest()
    receipt = {"dataset_sha256": manifest["dataset"]["sha256"], "model": {"sha256": manifest["run"]["model"]["file_tree_sha256"]}, "original": {"git_head": manifest["original"]["commit"], "git_tree": manifest["original"]["tree"], "git_dirty": False}, "latest": {"git_dirty": False}}
    runner.validate_environment_receipt(receipt, manifest)
    receipt["original"]["git_dirty"] = True
    with pytest.raises(ValueError, match="clean pinned"):
        runner.validate_environment_receipt(receipt, manifest)


def test_rss_monitor_samples_immediately_and_on_exit(monkeypatch):
    calls = []
    class Process:
        def __init__(self, _pid): pass
        def children(self, recursive): return []
        def is_running(self): return True
        def memory_info(self): calls.append("rss"); return SimpleNamespace(rss=123)
    monkeypatch.setitem(__import__("sys").modules, "psutil", SimpleNamespace(Process=Process))
    with runner.RssMonitor(os.getpid()) as monitor: pass
    assert monitor.peak_bytes == 123 and len(calls) >= 2


def test_main_worker_config_executes_real_worker_entrypoint(monkeypatch, tmp_path):
    config = tmp_path / "worker.json"; config.write_text("{}", encoding="utf-8")
    seen = []
    monkeypatch.setattr(runner, "worker_run", lambda value: seen.append(value) or {})
    assert runner.main(["--worker-config", str(config)]) == 0
    assert seen == [{}]
