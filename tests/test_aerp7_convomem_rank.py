import copy
import hashlib

import pytest

from benchmarks import aerp7_convomem_rank as rank
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256


def h(value): return hashlib.sha256(value.encode()).hexdigest()


class Encoder:
    identity = "synthetic-encoder"
    def encode_passages(self, texts): return [[float(len(text) + index + 1), 1.0] for index, text in enumerate(texts)]
    def encode_query(self, text): return [float(len(text) + 1), 1.0]


def receipts():
    return ({"encoder_identity": "synthetic-encoder", "encoder_semantics": "deterministic synthetic dense encoder", "files": [{"path_role": "weights", "sha256": h("weights"), "bytes": 1}]}, {"head": h("head"), "tree": h("tree"), "diff_digest": h("diff"), "dirty_policy": "clean_required"})


def projection():
    receipt = {"algorithm": "hmac-sha256-revision-bound-persona-group-tier-context-v1", "seed": 1, "persona_quota": 1, "per_persona_group_quota": 1, "context_rank_indices": [0], "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": h("selected"), "holdout_persona_set_sha256": h("holdout"), "group_values_sha256": h("groups"), "tier_values_sha256": h("tiers"), "context_values_sha256": h("contexts"), "desired_context_values_sha256": h("desired"), "variant_selection_sha256": h("variants"), "selected_item_context_count": 6, "item_supplement_count": 0, "exclusion_counts": {"multi_persona_cases": 0, "missing_crosswalk": 0, "ambiguous_canonical_keys": 0, "unmatched_premix_keys": 0, "multiple_logical_matches_or_variants": 0, "missing_requested_context_sizes": 0}, "quarantine_reason_digests": {name: h(name) for name in ("multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes")}, "quarantine_ledger_sha256": h("ledger")}
    corpus = h("corpus"); conversation = h("conversation")
    candidates = [{"message_id": h("m" + str(index)), "opaque_conversation_id": conversation, "conversation_order": 0, "message_order": index, "corpus_order": index, "speaker": "user" if index % 2 == 0 else "assistant", "text": "message " + str(index)} for index in range(11)]
    groups = ("user_evidence", "assistant_facts_evidence", "changing_evidence", "preference_evidence", "implicit_connection_evidence", "abstention_evidence")
    return {"schema": "aerp7-convomem-candidate-projection-v3", "dataset": {key: h(key) for key in ("canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256")}, "selection_receipt": receipt, "corpora": [{"corpus_id": corpus, "declared_context_size": 2, "actual_conversation_count": 1, "actual_message_count": len(candidates), "candidates": candidates}], "items": [{"item_id": h("item" + group), "persona_id": h("persona"), "query_text": "question " + group, "corpus_id": corpus} for group in groups]}


def original_replicates(p):
    model, code = receipts(); current = rank.rank_projection(projection=p, encoder=Encoder(), arm_id="strong_raw", model_receipt=model, code_receipt=code)
    result = []
    for number in range(5):
        rows = [{**{key: value for key, value in row.items() if key not in {"confidence", "confidence_receipt"}}, "candidate_input_sha256": rank._candidate_input(p["corpora"][0], rank.ORIGINAL_MEMPALACE_SERIALIZER), "confidence": None, "confidence_receipt": None} for row in current["rankings"]]
        trace = [{key: value for key, value in row.items() if key in {"item_id", "query_sha256", "ranked_count", "ranking_sha256"}} for row in current["trace_receipt"]]
        for row in trace: row["candidate_input_sha256"] = rank._candidate_input(p["corpora"][0], rank.ORIGINAL_MEMPALACE_SERIALIZER)
        input_receipt = rank._input_receipt(p, rank.ORIGINAL_MEMPALACE_SERIALIZER); physical_ids=[f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in p["corpora"] for candidate in corpus["candidates"]]; physical={"physical_count":len(physical_ids),"physical_ids_sha256":rank._digest(sorted(physical_ids)),"embedding":{"count":len(physical_ids),"dimension":384,"dtype":"float32","float32_sha256":h("embedding"+str(number))},"hnsw_config":rank.ORIGINAL_HNSW_CONFIG,"graph_files":[{"name":name,"bytes":1,"sha256":h(f"graph-{number}-{name}")} for name in rank.ORIGINAL_GRAPH_NAMES],"immutable_backend_sha256":h("backend"+str(number)),"sqlite_semantic_sha256":h("sqlite"+str(number)),"operational_delta":rank.ORIGINAL_OPERATIONAL_DELTA}
        index_receipt = {"build_id": "build-" + str(number), "fresh_build": True, "collection_identity": "collection-" + str(number), "index_identity_sha256": "", "cold_reopen": True, "call_contract": rank.ORIGINAL_CALL_CONTRACT, "input_coverage_sha256": rank._digest(input_receipt["item_corpora"]), "query_coverage_sha256": rank._digest([{"item_id": item["item_id"], "query_sha256": rank._query_digest(item["query_text"])} for item in sorted(p["items"], key=lambda item: item["item_id"])]), "output_coverage_sha256": rank._digest([{"item_id": item["item_id"], "ranking_sha256": rank._digest(rows[[row["item_id"] for row in rows].index(item["item_id"])]["ranked_message_ids"])} for item in sorted(p["items"], key=lambda item: item["item_id"])]),"worker_physical_receipt":physical,"coordinator_physical_receipt":copy.deepcopy(physical)}; index_receipt["index_identity_sha256"]=rank._digest({"collection_identity":index_receipt["collection_identity"],"physical":physical})
        result.append({"build_id": "build-" + str(number), "input_receipt": input_receipt, "input_sha256": rank._digest(input_receipt), "index_receipt": index_receipt, "index_sha256": rank._digest(index_receipt), "trace_receipt": trace, "trace_sha256": rank._digest(trace), "rankings": rows})
    return result


def test_current_freeze_is_deterministic_and_public_validator_recomputes_receipts():
    p = projection(); model, code = receipts()
    first = rank.rank_projection(projection=p, encoder=Encoder(), arm_id="static_p5", model_receipt=model, code_receipt=code)
    second = rank.rank_projection(projection=p, encoder=Encoder(), arm_id="static_p5", model_receipt=model, code_receipt=code)
    assert rank._bytes(first) == rank._bytes(second)
    assert rank.validate_frozen_ranking(first, projection=p)["artifact_sha256"] == first["artifact_sha256"]
    for mutate in (
        lambda x: x["model_receipt"].__setitem__("encoder_identity", "wrong"),
        lambda x: x["code_receipt"].__setitem__("tree", h("wrong")),
        lambda x: x["source_receipt"].__setitem__("tree", h("wrong")),
        lambda x: x["serializer_receipt"].__setitem__("name", "wrong"),
        lambda x: x["method_receipt"].__setitem__("rrf_k", 1),
        lambda x: x["input_receipt"]["item_corpora"][0].__setitem__("corpus_id", h("wrong")),
        lambda x: x["rankings"][0].__setitem__("query_sha256", h("wrong")),
        lambda x: x["rankings"][0].__setitem__("candidate_input_sha256", h("wrong")),
        lambda x: x["trace_receipt"][0].__setitem__("ranking_sha256", h("wrong")),
        lambda x: x["rankings"][0].__setitem__("retrieved_conversation_ids", []),
    ):
        bad = copy.deepcopy(first); mutate(bad)
        with pytest.raises(CustodyError): rank.validate_frozen_ranking(bad, projection=p)


def test_query_latency_observer_records_each_rank_call_without_changing_artifact():
    p = projection(); model, code = receipts(); measurements = []
    observed = rank.rank_projection(
        projection=p, encoder=Encoder(), arm_id="strong_raw",
        model_receipt=model, code_receipt=code, query_measurements=measurements,
    )
    unobserved = rank.rank_projection(
        projection=p, encoder=Encoder(), arm_id="strong_raw",
        model_receipt=model, code_receipt=code,
    )
    assert rank._bytes(observed) == rank._bytes(unobserved)
    assert [row["item_id"] for row in measurements] == [row["item_id"] for row in sorted(p["items"], key=lambda row: row["item_id"])]
    assert all(set(row) == {"item_id", "query_sha256", "wall_ns", "cpu_ns"} and row["wall_ns"] > 0 and row["cpu_ns"] > 0 for row in measurements)
    with pytest.raises(CustodyError, match="prefilled"):
        rank.rank_projection(projection=p, encoder=Encoder(), arm_id="strong_raw", model_receipt=model, code_receipt=code, query_measurements=[{}])


def test_rank_projection_fails_closed_when_encoder_identity_disagrees_with_receipt():
    p = projection(); model, code = receipts(); encoder = Encoder(); encoder.identity = "wrong-encoder"
    with pytest.raises(CustodyError, match="identity_mismatch"):
        rank.rank_projection(projection=p, encoder=encoder, arm_id="strong_raw", model_receipt=model, code_receipt=code)


def test_original_artifact_contains_five_complete_fresh_outputs_and_rejects_single_replicate_tamper():
    p = projection(); model, code = receipts(); artifact = rank.wrap_original_public_rankings(projection=p, replicates=original_replicates(p), model_receipt=model, code_receipt=code)
    assert len(artifact["replicates"]) == 5
    assert rank.validate_frozen_ranking(artifact, projection=p)["arm_id"] == "original_public_product"
    for mutate in (
        lambda x: x["replicates"].pop(),
        lambda x: x["replicates"][1].__setitem__("build_id", x["replicates"][0]["build_id"]),
        lambda x: x["replicates"][2]["rankings"][0]["ranked_message_ids"].reverse(),
        lambda x: x["replicates"][3]["index_receipt"].__setitem__("fresh_build", False),
    ):
        bad = copy.deepcopy(artifact); mutate(bad)
        with pytest.raises(CustodyError): rank.validate_frozen_ranking(bad, projection=p)


def test_receipts_require_real_model_files_and_clean_formal_code_policy():
    p = projection(); model, code = receipts(); model["files"][0]["bytes"] = 0
    with pytest.raises(CustodyError): rank.rank_projection(projection=p, encoder=Encoder(), arm_id="strong_raw", model_receipt=model, code_receipt=code)


def test_fcd1_semantics_and_independent_original_index_identities_are_not_just_hashes():
    p=projection(); model, code=receipts(); artifact=rank.rank_projection(projection=p,encoder=Encoder(),arm_id="static_p5",model_receipt=model,code_receipt=code)
    def reseal_current(value):
        value["trace_sha256"]=rank._digest(value["trace_receipt"]); value["artifact_sha256"]=rank._digest({key:item for key,item in value.items() if key!="artifact_sha256"})
    for mutate in (
        lambda ledger: ledger["view_full_order"]["raw_bm25"].reverse(),
        lambda ledger: ledger["view_top_50"]["raw_bm25"][0].__setitem__("score", 999.0),
        lambda ledger: ledger["fused_top_50"][0].__setitem__("final_rrf", 0.0),
        lambda ledger: ledger["checkpoint_tie_groups"][0].__setitem__("checkpoint_score", 0.0),
    ):
        bad=copy.deepcopy(artifact); mutate(bad["trace_receipt"][0]["ranking_trace"]["fcd1_diagnostic_ledger"]); bad["trace_receipt"][0]["ranker_trace_sha256"]=rank._digest(bad["trace_receipt"][0]["ranking_trace"]); reseal_current(bad)
        with pytest.raises(CustodyError): rank.validate_frozen_ranking(bad,projection=p)
    original=rank.wrap_original_public_rankings(projection=p,replicates=original_replicates(p),model_receipt=model,code_receipt=code); original["replicates"][1]["index_receipt"]["index_identity_sha256"]=original["replicates"][0]["index_receipt"]["index_identity_sha256"]; original["replicates"][1]["index_sha256"]=rank._digest(original["replicates"][1]["index_receipt"]); original["artifact_sha256"]=rank._digest({key:item for key,item in original.items() if key!="artifact_sha256"})
    with pytest.raises(CustodyError): rank.validate_frozen_ranking(original,projection=p)
    model, code = receipts(); code["dirty_policy"] = "recorded_dirty"
    with pytest.raises(CustodyError): rank.rank_projection(projection=p, encoder=Encoder(), arm_id="strong_raw", model_receipt=model, code_receipt=code)
