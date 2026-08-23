import copy
import hashlib

import pytest

from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_scoring as score
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256


def h(value): return hashlib.sha256(value.encode()).hexdigest()


class Encoder:
    identity = "synthetic-encoder"
    def encode_passages(self, texts): return [[float(len(text) + index + 1), 1.0] for index, text in enumerate(texts)]
    def encode_query(self, text): return [float(len(text) + 1), 1.0]


def receipts(): return ({"encoder_identity": "synthetic-encoder", "encoder_semantics": "deterministic", "files": [{"path_role": "weights", "sha256": h("weights"), "bytes": 2}]}, {"head": h("head"), "tree": h("tree"), "diff_digest": h("diff"), "dirty_policy": "clean_required"})


def projection():
    selection = {"algorithm": "hmac-sha256-revision-bound-persona-group-tier-context-v1", "seed": 1, "persona_quota": 1, "per_persona_group_quota": 1, "context_rank_indices": [0], "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": h("selected"), "holdout_persona_set_sha256": h("holdout"), "group_values_sha256": h("groups"), "tier_values_sha256": h("tiers"), "context_values_sha256": h("contexts"), "desired_context_values_sha256": h("desired"), "variant_selection_sha256": h("variants"), "selected_item_context_count": 12, "item_supplement_count": 0, "exclusion_counts": {"multi_persona_cases": 0, "missing_crosswalk": 0, "ambiguous_canonical_keys": 0, "unmatched_premix_keys": 0, "multiple_logical_matches_or_variants": 0, "missing_requested_context_sizes": 0}, "quarantine_reason_digests": {name: h(name) for name in ("multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes")}, "quarantine_ledger_sha256": h("ledger")}
    corpora=[]; items=[]; groups=list(score.UPSTREAM_GROUPS)
    for person in range(2):
        corpus=h("corpus"+str(person)); conversation=h("conversation"+str(person)); candidates=[{"message_id":h(f"m{person}-{n}"),"opaque_conversation_id":conversation,"conversation_order":0,"message_order":n,"corpus_order":n,"speaker":"user" if n==0 else "assistant","text":"target" if n==0 else f"other {n}"} for n in range(11)]
        corpora.append({"corpus_id":corpus,"declared_context_size":2,"actual_conversation_count":1,"actual_message_count":len(candidates),"candidates":candidates})
        items.extend({"item_id":h(f"item-{person}-{group}"),"persona_id":h(f"persona-{person}"),"query_text":f"query {person} {group}","corpus_id":corpus} for group in groups)
    return {"schema":"aerp7-convomem-candidate-projection-v3","dataset":{key:h(key) for key in ("canonical_sha256","premix_sha256","revision_sha256","source_inventory_sha256")},"selection_receipt":selection,"corpora":corpora,"items":items}


def original_replicates(p):
    model, code=receipts(); source=rank.rank_projection(projection=p,encoder=Encoder(),arm_id="strong_raw",model_receipt=model,code_receipt=code); corpora={row["corpus_id"]:row for row in p["corpora"]}; items={row["item_id"]:row for row in p["items"]}; result=[]
    for number in range(5):
        rows=[]; traces=[]
        for row in source["rankings"]:
            corpus=corpora[items[row["item_id"]]["corpus_id"]]; candidate_digest=rank._candidate_input(corpus,rank.ORIGINAL_MEMPALACE_SERIALIZER)
            rows.append({**{key:value for key,value in row.items() if key not in {"confidence","confidence_receipt"}},"candidate_input_sha256":candidate_digest,"confidence":None,"confidence_receipt":None})
        for trace in source["trace_receipt"]:
            corpus=corpora[items[trace["item_id"]]["corpus_id"]]
            traces.append({"item_id":trace["item_id"],"query_sha256":trace["query_sha256"],"candidate_input_sha256":rank._candidate_input(corpus,rank.ORIGINAL_MEMPALACE_SERIALIZER),"ranked_count":trace["ranked_count"],"ranking_sha256":trace["ranking_sha256"]})
        input_receipt=rank._input_receipt(p,rank.ORIGINAL_MEMPALACE_SERIALIZER); physical_ids=[f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in p["corpora"] for candidate in corpus["candidates"]]; physical={"physical_count":len(physical_ids),"physical_ids_sha256":rank._digest(sorted(physical_ids)),"embedding":{"count":len(physical_ids),"dimension":384,"dtype":"float32","float32_sha256":h(f"embed-{number}")},"hnsw_config":rank.ORIGINAL_HNSW_CONFIG,"graph_files":[{"name":name,"bytes":1,"sha256":h(f"graph-{number}-{name}")} for name in rank.ORIGINAL_GRAPH_NAMES],"immutable_backend_sha256":h(f"backend-{number}"),"sqlite_semantic_sha256":h(f"sqlite-{number}"),"operational_delta":rank.ORIGINAL_OPERATIONAL_DELTA}
        index_receipt={"build_id":f"build-{number}","fresh_build":True,"collection_identity":f"collection-{number}","index_identity_sha256":"","cold_reopen":True,"call_contract":rank.ORIGINAL_CALL_CONTRACT,"input_coverage_sha256":rank._digest(input_receipt["item_corpora"]),"query_coverage_sha256":rank._digest([{"item_id":item["item_id"],"query_sha256":rank._query_digest(item["query_text"])} for item in sorted(p["items"],key=lambda item:item["item_id"])]),"output_coverage_sha256":rank._digest([{"item_id":trace["item_id"],"ranking_sha256":trace["ranking_sha256"]} for trace in sorted(traces,key=lambda trace:trace["item_id"])]),"worker_physical_receipt":physical,"coordinator_physical_receipt":copy.deepcopy(physical)}; index_receipt["index_identity_sha256"]=rank._digest({"collection_identity":index_receipt["collection_identity"],"physical":physical})
        result.append({"build_id":f"build-{number}","input_receipt":input_receipt,"input_sha256":rank._digest(input_receipt),"index_receipt":index_receipt,"index_sha256":rank._digest(index_receipt),"trace_receipt":traces,"trace_sha256":rank._digest(traces),"rankings":rows})
    return result


def artifacts(p):
    model, code=receipts(); current=rank.freeze_current_rankings(projection=p,encoder=Encoder(),model_receipt=model,code_receipt=code)
    return [rank.wrap_original_public_rankings(projection=p,replicates=original_replicates(p),model_receipt=model,code_receipt=code),*current]


def manifest(p, arts):
    row={"schema":score.MANIFEST_SCHEMA,"projection_sha256":canonical_sha256(p),"protocol_source":rank.PROTOCOL_SOURCE,"serializer_contract":{"current":rank.CURRENT_SERIALIZER,"original_public_product":rank.ORIGINAL_MEMPALACE_SERIALIZER},"arms":[{"arm_id":art["arm_id"],"ranking_artifact_sha256":art["artifact_sha256"],"confidence_contract":rank.CONFIDENCE_CONTRACT if art["arm_id"] in rank.CURRENT_ARMS else None} for art in arts],"directory_endpoints":[{"directory_group":group,"endpoint":endpoint} for group,endpoint in score.UPSTREAM_GROUPS.items()],"bootstrap":{"seed":17,"resamples":19,"percentile_lower":.025,"percentile_upper":.975,"percentile_rule":"linear","original_replicate_rule":"per_query_arithmetic_mean"},"synthetic_test_mode":True,"reference_arm":"strong_raw"}
    row["manifest_sha256"]=score.endpoint_manifest_digest(row); return row


def custody(p):
    corpora={row["corpus_id"]:row for row in p["corpora"]}; result=[]
    for item in p["items"]:
        group=item["query_text"].split()[-1]; corpus=corpora[item["corpus_id"]]; endpoint=score.UPSTREAM_GROUPS[group]
        result.append({"item_id":item["item_id"],"directory_group":group,"evidence_conversation_ids":[] if endpoint=="abstention" else [corpus["candidates"][0]["opaque_conversation_id"]],"evidence_spans":[] if endpoint=="abstention" else [{"speaker":"user","text":"target"}]})
    return {"schema":score.CUSTODY_SCHEMA,"projection_sha256":canonical_sha256(p),"items":result}


def run():
    p=projection(); arts=artifacts(p); return p, arts, manifest(p,arts), custody(p)


def test_full_synthetic_scoring_is_leak_free_and_bootstrap_is_deterministic():
    p, arts, m, c=run(); first=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32); second=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    assert first==second and first["paired_bootstrap"]["overall_positive"]["paired_deltas"]
    assert first["arms"]["original_public_product"]["confidence_separability"]["available"] is False
    assert first["arms"]["strong_raw"]["confidence_separability"]["by_declared_context"]["2"]["average_precision"] >= 0
    assert all("message_id" not in row for row in first["mapping_ledger"])
    assert score.validate_report(first)["report_sha256"]==first["report_sha256"]


def test_public_failures_precede_custody_and_mapping_is_exact_speaker_text_with_cardinality_gates():
    p, arts, m, c=run(); bad=copy.deepcopy(arts); bad[1]["trace_receipt"][0]["ranking_sha256"]=h("tamper")
    with pytest.raises(CustodyError): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=bad,custody_loader=lambda:(_ for _ in ()).throw(AssertionError("must not open custody")),evidence_token_secret=b"x"*32)
    ambiguous=copy.deepcopy(p); ambiguous["corpora"][0]["candidates"][1].update({"speaker":"user","text":"target"})
    # Projection is altered before all public receipts, which still must fail before custody.
    with pytest.raises(CustodyError): score.score_frozen(projection=ambiguous,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:(_ for _ in ()).throw(AssertionError()),evidence_token_secret=b"x"*32)
    c["items"][0]["evidence_spans"]=[]
    with pytest.raises(CustodyError,match="positive_evidence_span_missing"): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    c=custody(p); c["items"][5]["evidence_conversation_ids"]=[p["corpora"][0]["candidates"][0]["opaque_conversation_id"]]
    with pytest.raises(CustodyError,match="abstention_evidence_must_be_empty"): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    ambiguous=copy.deepcopy(p); candidate=copy.deepcopy(ambiguous["corpora"][0]["candidates"][0]); candidate["message_id"]=h("ambiguous-message"); candidate["message_order"]=11; candidate["corpus_order"]=11; ambiguous["corpora"][0]["candidates"].append(candidate); ambiguous["corpora"][0]["actual_message_count"]+=1
    ambiguous_arts=artifacts(ambiguous); ambiguous_report=score.score_frozen(projection=ambiguous,endpoint_manifest=manifest(ambiguous,ambiguous_arts),ranking_artifacts=ambiguous_arts,custody_loader=lambda:custody(ambiguous),evidence_token_secret=b"x"*32)
    assert "ambiguous" in {entry["status"] for entry in ambiguous_report["mapping_ledger"]}


def test_ap_ties_missing_stratum_secret_and_report_leak_are_fail_closed():
    assert score._auroc_ap([(0.5,1),(0.5,0)])==(0.5,0.5)
    with pytest.raises(CustodyError): score._auroc_ap([(0.5,1)])
    p,arts,m,c=run()
    with pytest.raises(CustodyError,match="scoring_secret_too_short"): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"short")
    report=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32); report["mapping_ledger"][0]["message_id"]=h("leak"); report["report_sha256"]=score.report_digest(report)
    with pytest.raises(CustodyError,match="scoring_report_leakage"): score.validate_report(report)


def test_formal_freeze_report_completeness_and_duplicate_span_ndcg_are_fail_closed():
    p,arts,m,c=run(); formal=copy.deepcopy(m); formal["synthetic_test_mode"]=False; formal["bootstrap"]={**score.FORMAL_BOOTSTRAP}; formal["reference_arm"]="strong_raw"; formal["manifest_sha256"]=score.endpoint_manifest_digest(formal)
    assert score.validate_endpoint_manifest(formal,projection_sha256=canonical_sha256(p))["reference_arm"]=="strong_raw"
    for mutate in (
        lambda value: value["arms"].pop(),
        lambda value: value["bootstrap"].__setitem__("seed",1),
        lambda value: value.__setitem__("reference_arm","static_p5"),
    ):
        bad=copy.deepcopy(formal); mutate(bad); bad["manifest_sha256"]=score.endpoint_manifest_digest(bad)
        with pytest.raises(CustodyError,match="formal_manifest_freeze_invalid"): score.validate_endpoint_manifest(bad,projection_sha256=canonical_sha256(p))
    report=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    for mutate in (
        lambda value: value["arms"]["strong_raw"]["positive"].pop("derived_hard_changing_and_implicit"),
        lambda value: value["paired_bootstrap"].pop("static_p5_vs_strong_raw_abstention_confidence"),
        lambda value: value["arms"]["original_public_product"].pop("original_replicates"),
    ):
        bad=copy.deepcopy(report); mutate(bad); bad["report_sha256"]=score.report_digest(bad)
        with pytest.raises(CustodyError): score.validate_report(bad)
    metrics=score.question_metrics(["gold"],[{"status":"mapped","message_id":"gold"},{"status":"mapped","message_id":"gold"}])
    assert metrics["recall_at_10"]==1.0 and metrics["ndcg_at_10"]==1.0
    bad=copy.deepcopy(report); bad["ranking_artifact_sha256"]["strong_raw"]=h("different-valid-digest"); bad["report_sha256"]=score.report_digest(bad)
    with pytest.raises(CustodyError,match="scoring_report_artifact_manifest_binding_invalid"): score.validate_report(bad)
