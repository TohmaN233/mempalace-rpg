import copy
import hashlib
import json

import pytest

from benchmarks import aerp7_convomem_formal as formal
from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_scoring as score
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256


def h(value): return hashlib.sha256(value.encode()).hexdigest()


_MODEL_FILES = [{"path_role": "weights", "relative_path": "weights.onnx", "sha256": h("weights"), "bytes": 2}]
_MODEL_TREE_SHA = rank._digest([{key: row[key] for key in ("relative_path", "sha256", "bytes")} for row in _MODEL_FILES])


class Encoder:
    identity = "chromadb-native-minilm:" + _MODEL_TREE_SHA
    def encode_passages(self, texts): return [[float(len(text) + index + 1), 1.0] for index, text in enumerate(texts)]
    def encode_query(self, text): return [float(len(text) + 1), 1.0]


def receipts():
    return ({"encoder_identity": Encoder.identity, "encoder_semantics": "deterministic", "files": copy.deepcopy(_MODEL_FILES)}, {"head": h("head"), "tree": h("tree"), "diff_digest": h("diff"), "dirty_policy": "clean_required"})


def projection():
    selection = {"algorithm": "hmac-sha256-revision-bound-persona-group-tier-context-v1", "seed": 1, "persona_quota": 1, "per_persona_group_quota": 1, "context_rank_indices": [0], "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": h("selected"), "holdout_persona_set_sha256": h("holdout"), "group_values_sha256": h("groups"), "tier_values_sha256": h("tiers"), "context_values_sha256": h("contexts"), "desired_context_values_sha256": h("desired"), "variant_selection_sha256": h("variants"), "selected_item_context_count": 6, "item_supplement_count": 0, "exclusion_counts": {"multi_persona_cases": 0, "missing_crosswalk": 0, "ambiguous_canonical_keys": 0, "unmatched_premix_keys": 0, "multiple_logical_matches_or_variants": 0, "missing_requested_context_sizes": 0}, "quarantine_reason_digests": {name: h(name) for name in ("multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes")}, "quarantine_ledger_sha256": h("ledger")}
    corpus, conversation = h("corpus"), h("conversation")
    candidates = [{"message_id": h("m" + str(index)), "opaque_conversation_id": conversation, "conversation_order": 0, "message_order": index, "corpus_order": index, "speaker": "user" if index % 2 == 0 else "assistant", "text": "message " + str(index)} for index in range(11)]
    groups = ("user_evidence", "assistant_facts_evidence", "changing_evidence", "preference_evidence", "implicit_connection_evidence", "abstention_evidence")
    return {"schema": "aerp7-convomem-candidate-projection-v3", "dataset": {key: h(key) for key in ("canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256")}, "selection_receipt": selection, "corpora": [{"corpus_id": corpus, "declared_context_size": 2, "actual_conversation_count": 1, "actual_message_count": len(candidates), "candidates": candidates}], "items": [{"item_id": h("item" + group), "persona_id": h("persona"), "query_text": "question " + group, "corpus_id": corpus} for group in groups]}


def protocol(p, candidate=None):
    model, code = receipts()
    candidate = candidate or {"generation_id": h("generation"), "ready_sha256": h("ready"), "projection_raw_sha256": h("raw"), "projection_canonical_sha256": canonical_sha256(p)}
    row = {"schema": formal.FORMAL_PROTOCOL_SCHEMA, "synthetic_test_mode": False, "candidate": candidate, "current_code_receipt": code, "original_code_receipt": code, "source_receipt": rank.PROTOCOL_SOURCE, "model_receipt": model, "arms": list(rank_score_arms()), "serializer_contract": {"current": rank.CURRENT_SERIALIZER, "original_public_product": rank.ORIGINAL_MEMPALACE_SERIALIZER}, "top_k": 10, "tie_break": "stable_ranking_key_ascending", "original_build_count": 5, "p5_repeat_required": True, "bootstrap": {"seed": 20260822, "resamples": 5000, "percentile_lower": .025, "percentile_upper": .975, "percentile_rule": "linear", "original_replicate_rule": "per_query_arithmetic_mean"}, "gates": {"overall_delta_min": .01, "overall_ci_lower_gt_zero": 0.0, "hard_delta_min": 0.0, "hard_ci_lower_min": -.01, "abstention_ci_lower_min": -.01, "guardrails_required": True}, "resource_thresholds": {"peak_rss_bytes_max": 2_000_000_000, "storage_bytes_max": 2_000_000_000, "ingest_seconds_max": 100.0, "index_seconds_max": 100.0, "query_p95_ns_max": 1_000_000_000}}
    row["protocol_sha256"] = formal.protocol_digest(row)
    return row


def rank_score_arms():
    return ("original_public_product", "strong_raw", "static_p5", "six_view_secondary")


def worker_config(p, protocol_row):
    return {"role": "candidate_ranker", "projection_sha256": canonical_sha256(p), "projection_raw_sha256": protocol_row["candidate"]["projection_raw_sha256"], "projection_path": "projection.json", "model_receipt": protocol_row["model_receipt"], "code_receipt": protocol_row["current_code_receipt"], "staging_root": "staging", "arms": ["strong_raw", "static_p5", "six_view_secondary"], "top_k": 10, "tie_break": "stable_ranking_key_ascending", "serializer_contract": protocol_row["serializer_contract"]}


def bundle(tmp_path, p):
    root = tmp_path / "candidate"; root.mkdir(); staging = tmp_path / "staging"; staging.mkdir()
    raw = json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(); (root / "projection.json").write_bytes(raw)
    generation = h("generation")
    ready = {"schema": "aerp7-convomem-candidate-ready-v3", "generation_id": generation, "projection": {"raw_sha256": hashlib.sha256(raw).hexdigest(), "canonical_sha256": canonical_sha256(p)}, "durability": {"platform": "synthetic", "directory_fsync_guaranteed": False, "steps": []}}
    ready_raw = json.dumps(ready, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(); (root / "READY.json").write_bytes(ready_raw)
    return root, staging, {"generation_id": generation, "ready_sha256": hashlib.sha256(ready_raw).hexdigest(), "projection_raw_sha256": hashlib.sha256(raw).hexdigest(), "projection_canonical_sha256": canonical_sha256(p), "query_count": len(p["items"]), "candidate_text_count": sum(len(corpus["candidates"]) for corpus in p["corpora"])}


def original_replicates(p):
    model, code = receipts(); current = rank.rank_projection(projection=p, encoder=Encoder(), arm_id="strong_raw", model_receipt=model, code_receipt=code)
    corpora = {corpus["corpus_id"]: corpus for corpus in p["corpora"]}; items = {item["item_id"]: item for item in p["items"]}
    result = []
    for number in range(5):
        rows = [{**{key: value for key, value in row.items() if key not in {"confidence", "confidence_receipt"}}, "candidate_input_sha256": rank._candidate_input(corpora[items[row["item_id"]]["corpus_id"]], rank.ORIGINAL_MEMPALACE_SERIALIZER), "confidence": None, "confidence_receipt": None} for row in current["rankings"]]
        traces = [{key: value for key, value in row.items() if key in {"item_id", "query_sha256", "ranked_count", "ranking_sha256"}} for row in current["trace_receipt"]]
        for item in traces: item["candidate_input_sha256"] = rank._candidate_input(corpora[items[item["item_id"]]["corpus_id"]], rank.ORIGINAL_MEMPALACE_SERIALIZER)
        inputs = rank._input_receipt(p, rank.ORIGINAL_MEMPALACE_SERIALIZER); physical_ids = [f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in p["corpora"] for candidate in corpus["candidates"]]
        physical = {"physical_count": len(physical_ids), "physical_ids_sha256": rank._digest(sorted(physical_ids)), "embedding": {"count": len(physical_ids), "dimension": 384, "dtype": "float32", "float32_sha256": h("embedding" + str(number))}, "hnsw_config": rank.ORIGINAL_HNSW_CONFIG, "graph_files": [{"name": name, "bytes": 1, "sha256": h(f"graph-{number}-{name}")} for name in rank.ORIGINAL_GRAPH_NAMES], "immutable_backend_sha256": h("backend" + str(number)), "sqlite_semantic_sha256": h("sqlite" + str(number)), "operational_delta": rank.ORIGINAL_OPERATIONAL_DELTA}
        index = {"build_id": "build-" + str(number), "fresh_build": True, "collection_identity": "collection-" + str(number), "index_identity_sha256": "", "cold_reopen": True, "call_contract": rank.ORIGINAL_CALL_CONTRACT, "input_coverage_sha256": rank._digest(inputs["item_corpora"]), "query_coverage_sha256": rank._digest([{"item_id": item["item_id"], "query_sha256": rank._query_digest(item["query_text"])} for item in sorted(p["items"], key=lambda item: item["item_id"])]), "output_coverage_sha256": rank._digest([{"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in sorted(traces, key=lambda trace: trace["item_id"])]), "worker_physical_receipt": physical, "coordinator_physical_receipt": copy.deepcopy(physical)}
        index["index_identity_sha256"] = rank._digest({"collection_identity": index["collection_identity"], "physical": physical})
        result.append({"build_id": "build-" + str(number), "input_receipt": inputs, "input_sha256": rank._digest(inputs), "index_receipt": index, "index_sha256": rank._digest(index), "trace_receipt": traces, "trace_sha256": rank._digest(traces), "rankings": rows})
    return result


def resource(arm, artifact_sha, build_id=None, index_sha=None, p=None, execution_role=None):
    q, c = (len(p["items"]), sum(len(corpus["candidates"]) for corpus in p["corpora"])) if p is not None else (6, 11)
    execution_role = execution_role or ("fresh_build" if arm == "original_public_product" else "primary")
    accounting = {"primary": "primary_excludes_repeat", "repeat": "repeat_measured_separately"}[execution_role] if arm == "static_p5" else "not_applicable"
    query_rows = sorted(p["items"], key=lambda row: row["item_id"]) if p is not None else [{"item_id": h("resource-item-" + str(number)), "query_text": "resource-query-" + str(number)} for number in range(q)]
    measurements = [{"item_id": item["item_id"], "query_sha256": rank._query_digest(item["query_text"]), "wall_ns": number + 1, "cpu_ns": number + 2} for number, item in enumerate(query_rows)]
    original = arm == "original_public_product"
    passage = {"calls": 1, "texts": c, "measurement_kind": "public_upsert_request_proxy" if original else "encoder_adapter_api_calls", "native_embedding_observable": not original, "limitation": "synthetic public proxy" if original else None}
    query = {"calls": q, "texts": q, "measurement_kind": "public_search_request_proxy" if original else "encoder_adapter_api_calls", "native_embedding_observable": not original, "limitation": "synthetic public proxy" if original else None}
    row = {"schema": formal.RESOURCE_SCHEMA, "arm_id": arm, "execution_role": execution_role, "resource_semantics": "all_six_views_computed_then_raw_fusion_weights" if arm == "strong_raw" else "native_public_product" if original else "all_six_views_computed_then_fixed_fusion", "measurement_scope": "rank_only_excludes_trace_and_receipt_serialization", "measurement_mode": "live_original_public_product" if original else "live_native_adapter", "p5_repeat_accounting": accounting, "ingest_seconds": 1.0, "index_seconds": 1.0, "query_measurements": measurements, "query_latency_ns": {"wall": formal._latency_percentiles([row["wall_ns"] for row in measurements]), "cpu": formal._latency_percentiles([row["cpu_ns"] for row in measurements])}, "passage_embedding": passage, "query_embedding": query, "storage_scope": "palace_directory_after_cold_reopen" if original else "no_persistent_index", "storage_bytes": 100 if original else 0, "peak_rss_bytes": 100, "artifact_sha256": artifact_sha, "build_id": build_id, "index_sha256": index_sha, "input_denominators": {"query_count": q, "candidate_text_count": c}, "hardware_runtime": {"python": "synthetic-python", "platform": "synthetic-platform", "processor": "synthetic-cpu"}}
    row["resource_sha256"] = formal.resource_digest(row)
    return row


def resource_map(resources):
    by_arm = {}
    for item in resources:
        by_arm.setdefault(item["arm_id"], []).append(item)
    return {
        "original_public_product": [item["resource_sha256"] for item in sorted(by_arm["original_public_product"], key=lambda item: item["build_id"])],
        "strong_raw": by_arm["strong_raw"][0]["resource_sha256"],
        "static_p5": {item["execution_role"]: item["resource_sha256"] for item in by_arm["static_p5"]},
        "six_view_secondary": by_arm["six_view_secondary"][0]["resource_sha256"],
    }


def formal_resources(original_artifact, original, current, p=None):
    artifacts = {item["arm_id"]: item for item in current}
    return [resource("original_public_product", original_artifact["artifact_sha256"], row["build_id"], row["index_sha256"], p) for row in original] + [
        resource("strong_raw", artifacts["strong_raw"]["artifact_sha256"], p=p),
        resource("static_p5", artifacts["static_p5"]["artifact_sha256"], p=p, execution_role="primary"),
        resource("static_p5", artifacts["static_p5"]["artifact_sha256"], p=p, execution_role="repeat"),
        resource("six_view_secondary", artifacts["six_view_secondary"]["artifact_sha256"], p=p),
    ]


def bind_live_execution_receipts(current_receipt, proto, p, current, resources):
    """Construct a deliberately complete live-shaped receipt for validator tests."""
    artifact_by_arm = {row["arm_id"]: row for row in current}
    resource_by_role = {(row["arm_id"], row["execution_role"]): row for row in resources if row["arm_id"] != "original_public_product"}
    observed_model = {"model_file_tree_sha256": _MODEL_TREE_SHA, "model_file_tree_bytes": 2, "encoder_identity": proto["model_receipt"]["encoder_identity"], "runtime_identity": {"synthetic_test": "validator-shape-only"}}
    execution = []
    for number, (role, arm, resource_role) in enumerate((("raw", "strong_raw", "primary"), ("p5_primary", "static_p5", "primary"), ("p5_repeat", "static_p5", "repeat"), ("six", "six_view_secondary", "primary")), start=1):
        artifact, resource = artifact_by_arm[arm], resource_by_role[(arm, resource_role)]
        row = {"schema": formal.CURRENT_EXECUTION_RECEIPT_SCHEMA, "execution_mode": "live_native_adapter", "execution_role": role, "arm_id": arm, "protocol_sha256": proto["protocol_sha256"], "projection_sha256": canonical_sha256(p), "worker_config_sha256": formal._digest(formal.canonical_candidate_worker_config(proto)), "method_input_sha256": formal._digest({"arm_id": arm, "method_receipt": artifact["method_receipt"], "serializer_receipt": artifact["serializer_receipt"]}), "observed_code_before": proto["current_code_receipt"], "observed_code_after": proto["current_code_receipt"], "observed_model_before": observed_model, "observed_model_after": observed_model, "provider": {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"], "model_file_tree_sha256": _MODEL_TREE_SHA}, "encoder_identity": proto["model_receipt"]["encoder_identity"], "artifact_file_sha256": hashlib.sha256(formal._bytes(artifact)).hexdigest(), "artifact_sha256": artifact["artifact_sha256"], "resource_sha256": resource["resource_sha256"], "process_id": number, "supervisor_sha256": h("supervisor-" + role), "execution_sha256": ""}
        row["execution_sha256"] = formal._digest({key: value for key, value in row.items() if key != "execution_sha256"})
        execution.append(row)
    receipt = copy.deepcopy(current_receipt); receipt["execution_receipts"] = execution; receipt["worker_sha256"] = formal._digest({key: value for key, value in receipt.items() if key != "worker_sha256"})
    return receipt


def test_resource_v2_retains_raw_measurements_and_recomputes_both_percentiles():
    p = projection(); proto = protocol(p)
    good = resource("strong_raw", h("artifact"), p=p)
    expected_queries = formal.projection_query_keys(p)
    assert formal.validate_resource_receipt(good, arm_id="strong_raw", thresholds=proto["resource_thresholds"], expected_denominators=formal.projection_denominators(p), expected_query_keys=expected_queries)["storage_bytes"] == 0
    forged = copy.deepcopy(good); forged["query_latency_ns"]["wall"]["p95"] += 1; forged["resource_sha256"] = formal.resource_digest(forged)
    with pytest.raises(CustodyError, match="percentile_recompute_invalid"):
        formal.validate_resource_receipt(forged, arm_id="strong_raw", thresholds=proto["resource_thresholds"], expected_denominators=formal.projection_denominators(p), expected_query_keys=expected_queries)
    forged = copy.deepcopy(good); forged["passage_embedding"]["measurement_kind"] = "estimate"; forged["resource_sha256"] = formal.resource_digest(forged)
    with pytest.raises(CustodyError, match="current_embedding_semantics_invalid"):
        formal.validate_resource_receipt(forged, arm_id="strong_raw", thresholds=proto["resource_thresholds"], expected_denominators=formal.projection_denominators(p), expected_query_keys=expected_queries)
    for mutate in (
        lambda rows: rows[0].__setitem__("item_id", h("forged-item")),
        lambda rows: rows[0].__setitem__("query_sha256", h("forged-query")),
        lambda rows: rows.reverse(),
    ):
        forged = copy.deepcopy(good); mutate(forged["query_measurements"]); forged["resource_sha256"] = formal.resource_digest(forged)
        with pytest.raises(CustodyError, match="query_binding_invalid"):
            formal.validate_resource_receipt(forged, arm_id="strong_raw", thresholds=proto["resource_thresholds"], expected_denominators=formal.projection_denominators(p), expected_query_keys=expected_queries)


def scoring_custody(p):
    corpora = {corpus["corpus_id"]: corpus for corpus in p["corpora"]}
    items = []
    for item in p["items"]:
        group = item["query_text"].split()[-1]
        corpus = corpora[item["corpus_id"]]
        endpoint = score.UPSTREAM_GROUPS[group]
        items.append({"item_id": item["item_id"], "directory_group": group, "evidence_conversation_ids": [] if endpoint == "abstention" else [corpus["candidates"][0]["opaque_conversation_id"]], "evidence_spans": [] if endpoint == "abstention" else [{"speaker": "user", "text": corpus["candidates"][0]["text"]}]})
    return {"schema": score.CUSTODY_SCHEMA, "projection_sha256": canonical_sha256(p), "items": items}


def test_formal_protocol_candidate_boundary_and_current_repeat_are_fail_closed(tmp_path):
    p = projection(); root, staging, candidate = bundle(tmp_path, p); proto = protocol(p, candidate); config = worker_config(p, proto)
    assert formal.validate_formal_protocol(proto)["synthetic_test_mode"] is False
    artifacts, receipt = formal.freeze_current_worker(encoder=Encoder(), protocol=proto, worker_config=config, candidate_bundle_root=root, staging_parent=tmp_path)
    assert receipt["static_p5_byte_identical"] is True and receipt["static_p5_execution_count"] == 2 and {row["arm_id"] for row in artifacts} == {"strong_raw", "static_p5", "six_view_secondary"}
    for mutate in (
        lambda value: value.__setitem__("custody_path", "no"),
        lambda value: value.__setitem__("projection_path", "custody/READY.json"),
        lambda value: value.__setitem__("arms", ["static_p5"]),
    ):
        bad = copy.deepcopy(config); mutate(bad)
        with pytest.raises(CustodyError): formal.validate_candidate_worker_config(bad, protocol=proto)
    forged_candidate = {**candidate, "query_count": 1, "candidate_text_count": 1}; forged_protocol = protocol(p, forged_candidate)
    with pytest.raises(CustodyError, match="candidate_projection_denominator_binding_invalid"):
        formal.load_candidate_worker_projection(worker_config=worker_config(p, forged_protocol), protocol=forged_protocol, candidate_bundle_root=root, staging_parent=tmp_path)


def test_current_worker_executes_p5_exactly_twice_without_hidden_helper_repeat(tmp_path, monkeypatch):
    p = projection(); root, staging, candidate = bundle(tmp_path, p); proto = protocol(p, candidate); config = worker_config(p, proto)
    original = rank.rank_projection; calls = []
    def counted(*args, **kwargs):
        calls.append(kwargs["arm_id"])
        return original(*args, **kwargs)
    monkeypatch.setattr(rank, "rank_projection", counted)
    formal.freeze_current_worker(encoder=Encoder(), protocol=proto, worker_config=config, candidate_bundle_root=root, staging_parent=tmp_path)
    assert calls.count("strong_raw") == 1
    assert calls.count("static_p5") == 2
    assert calls.count("six_view_secondary") == 1
    assert calls == ["strong_raw", "static_p5", "static_p5", "six_view_secondary"]


def test_original_lifecycle_endpoint_and_release_cross_bind_everything(tmp_path, monkeypatch):
    p = projection(); root, staging, candidate = bundle(tmp_path, p); proto = protocol(p, candidate); current, current_receipt = formal.freeze_current_worker(encoder=Encoder(), protocol=proto, worker_config=worker_config(p, proto), candidate_bundle_root=root, staging_parent=tmp_path)
    original = original_replicates(p); sealed = {"replicates": original, "lifecycle": list(formal.ORIGINAL_LIFECYCLE), "original_code_before": proto["original_code_receipt"], "original_code_after": proto["original_code_receipt"]}; sealed["worker_sha256"] = formal._digest({key: sealed[key] for key in ("replicates", "lifecycle", "original_code_before", "original_code_after")})
    checked = formal.validate_original_worker_receipt(sealed, projection=p, protocol=proto)
    endpoint = formal.freeze_endpoint_manifest(projection=p, protocol=proto, ranking_artifacts=[checked["artifact"], *current])
    resources = formal_resources(checked["artifact"], original, current, p)
    current_receipt = bind_live_execution_receipts(current_receipt, proto, p, current, resources)
    resource_digests = resource_map(resources)
    original_map = {row["build_id"]: row["index_sha256"] for row in original}
    secret, custody_ready, custody_bundle = b"c" * 32, h("custody-ready"), h("custody-bundle")
    release = {"schema": formal.RELEASE_SCHEMA, "protocol_sha256": proto["protocol_sha256"], "endpoint_manifest_sha256": endpoint["manifest_sha256"], "candidate_ready_sha256": proto["candidate"]["ready_sha256"], "projection_raw_sha256": proto["candidate"]["projection_raw_sha256"], "projection_canonical_sha256": proto["candidate"]["projection_canonical_sha256"], "custody_ready_sha256": custody_ready, "custody_bundle_sha256": custody_bundle, "ranking_artifact_sha256": {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in endpoint["arms"]}, "resource_sha256": resource_digests, "original_build_index_sha256": original_map, "current_worker_sha256": current_receipt["worker_sha256"]}
    release = formal.sign_release_authorization(release, custody_capability_secret=secret)
    artifacts = [checked["artifact"], *current]
    assert formal.validate_release_authorization(release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)["release_sha256"] == release["release_sha256"]
    def reseal_worker(receipt):
        receipt = copy.deepcopy(receipt)
        for item in receipt.get("execution_receipts", []):
            item["execution_sha256"] = formal._digest({key: value for key, value in item.items() if key != "execution_sha256"})
        receipt["worker_sha256"] = formal._digest({key: value for key, value in receipt.items() if key != "worker_sha256"})
        return receipt
    def reseal_release(receipt):
        candidate = copy.deepcopy(release); candidate["current_worker_sha256"] = receipt["worker_sha256"]
        return formal.sign_release_authorization(candidate, custody_capability_secret=secret)
    deleted = copy.deepcopy(current_receipt); del deleted["execution_receipts"]; deleted = reseal_worker(deleted)
    with pytest.raises(CustodyError, match="current_worker_receipt_invalid"):
        formal.validate_release_authorization(reseal_release(deleted), projection=p, ranking_artifacts=artifacts, current_worker_receipt=deleted, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    for mutate, code in (
        (lambda row: row.__setitem__("resource_sha256", h("forged-resource")), "resource_binding_invalid"),
        (lambda row: row.__setitem__("artifact_file_sha256", h("forged-artifact-file")), "receipt_digest_invalid"),
        (lambda row: row.__setitem__("execution_mode", "synthetic_rehearsal"), "synthetic_not_formal"),
        (lambda row: row.__setitem__("observed_code_before", {"forged": True}), "live_code_binding_invalid"),
        (lambda row: row.__setitem__("observed_model_before", {"forged": True}), "live_model_binding_invalid"),
        (lambda row: row.__setitem__("provider", {"forged": True}), "live_provider_invalid"),
        (lambda row: row.__setitem__("worker_config_sha256", h("forged-worker-config")), "replay_binding_invalid"),
        (lambda row: row.__setitem__("method_input_sha256", h("forged-method-input")), "replay_binding_invalid"),
    ):
        forged = reseal_worker(current_receipt); mutate(forged["execution_receipts"][0]); forged = reseal_worker(forged)
        with pytest.raises(CustodyError, match=code):
            formal.validate_release_authorization(reseal_release(forged), projection=p, ranking_artifacts=artifacts, current_worker_receipt=forged, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    wrong_mode_resources = copy.deepcopy(resources); changed = next(item for item in wrong_mode_resources if item["arm_id"] == "strong_raw")
    changed["measurement_mode"] = "synthetic_rehearsal"; changed["resource_sha256"] = formal.resource_digest(changed)
    wrong_mode_worker = reseal_worker(current_receipt); next(item for item in wrong_mode_worker["execution_receipts"] if item["execution_role"] == "raw")["resource_sha256"] = changed["resource_sha256"]; wrong_mode_worker = reseal_worker(wrong_mode_worker)
    with pytest.raises(CustodyError, match="resource_mode_invalid"):
        formal.validate_release_authorization(reseal_release(wrong_mode_worker), projection=p, ranking_artifacts=artifacts, current_worker_receipt=wrong_mode_worker, protocol=proto, endpoint_manifest=endpoint, resource_receipts=wrong_mode_resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    wrong_original_resources = copy.deepcopy(resources); changed = wrong_original_resources[0]; changed["measurement_mode"] = "synthetic_rehearsal"; changed["resource_sha256"] = formal.resource_digest(changed)
    with pytest.raises(CustodyError, match="original_resource_mode_invalid"):
        formal.validate_release_authorization(release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=wrong_original_resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    with pytest.raises(CustodyError, match="release_resource_replicate_coverage_invalid"):
        formal.validate_release_authorization(release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=[item for item in resources if not (item["arm_id"] == "static_p5" and item["execution_role"] == "repeat")], custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    forged_worker = copy.deepcopy(current_receipt); forged_worker["static_p5_repeat_sha256"] = h("forged-repeat"); forged_worker["worker_sha256"] = formal._digest({key: item for key, item in forged_worker.items() if key != "worker_sha256"})
    forged_worker_release = copy.deepcopy(release); forged_worker_release["current_worker_sha256"] = forged_worker["worker_sha256"]; forged_worker_release = formal.sign_release_authorization(forged_worker_release, custody_capability_secret=secret)
    with pytest.raises(CustodyError, match="current_worker_repeat_invalid"):
        formal.validate_release_authorization(forged_worker_release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=forged_worker, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    tampered_repeat_resources = copy.deepcopy(resources)
    repeat_resource = next(item for item in tampered_repeat_resources if item["arm_id"] == "static_p5" and item["execution_role"] == "repeat")
    repeat_resource["artifact_sha256"] = h("forged-repeat-resource"); repeat_resource["resource_sha256"] = formal.resource_digest(repeat_resource)
    tampered_repeat_release = copy.deepcopy(release); tampered_repeat_release["resource_sha256"] = resource_map(tampered_repeat_resources); tampered_repeat_release = formal.sign_release_authorization(tampered_repeat_release, custody_capability_secret=secret)
    with pytest.raises(CustodyError, match="release_static_p5_repeat_resource_binding_invalid"):
        formal.validate_release_authorization(tampered_repeat_release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=tampered_repeat_resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    with pytest.raises(CustodyError, match="release_capability_invalid"):
        formal.validate_release_authorization(release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=b"d" * 32)
    monkeypatch.setattr(formal.confirmation, "load_custody_for_scoring", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not open custody")))
    with pytest.raises(CustodyError, match="release_capability_invalid"):
        formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=b"d" * 32, candidate_bundle_root=tmp_path / "not-opened-candidate", custody_bundle_root=tmp_path / "not-opened-custody", binding_secret=b"x" * 32)
    mismatched_resource = copy.deepcopy(resources); mismatched_resource[0]["artifact_sha256"] = h("wrong-artifact"); mismatched_resource[0]["resource_sha256"] = formal.resource_digest(mismatched_resource[0])
    bad_release = copy.deepcopy(release); bad_release["resource_sha256"] = resource_map(mismatched_resource); bad_release = formal.sign_release_authorization(bad_release, custody_capability_secret=secret)
    with pytest.raises(CustodyError): formal.validate_release_authorization(bad_release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=mismatched_resource, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    forged_resources = copy.deepcopy(resources); forged_resources[0]["build_id"] = "forged-build"; forged_resources[0]["index_sha256"] = h("forged-index"); forged_resources[0]["resource_sha256"] = formal.resource_digest(forged_resources[0])
    forged = copy.deepcopy(release); forged["resource_sha256"] = resource_map(forged_resources); forged["original_build_index_sha256"] = {item["build_id"]: item["index_sha256"] for item in forged_resources[:5]}; forged = formal.sign_release_authorization(forged, custody_capability_secret=secret)
    with pytest.raises(CustodyError, match="release_resource_original_artifact_binding_invalid"):
        formal.validate_release_authorization(forged, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=forged_resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=secret)
    bad_original = copy.deepcopy(sealed); bad_original["replicates"][1]["index_receipt"]["collection_identity"] = bad_original["replicates"][0]["index_receipt"]["collection_identity"]
    bad_original["replicates"][1]["index_receipt"]["index_identity_sha256"] = rank._digest({"collection_identity": bad_original["replicates"][1]["index_receipt"]["collection_identity"], "physical": bad_original["replicates"][1]["index_receipt"]["worker_physical_receipt"]})
    bad_original["replicates"][1]["index_sha256"] = rank._digest(bad_original["replicates"][1]["index_receipt"]); bad_original["worker_sha256"] = formal._digest({key: bad_original[key] for key in ("replicates", "lifecycle", "original_code_before", "original_code_after")})
    with pytest.raises(CustodyError): formal.validate_original_worker_receipt(bad_original, projection=p, protocol=proto)


def test_nonreplace_publish_cleans_failure_and_accepts_only_exact_retry(tmp_path):
    output = tmp_path / "report.json"; payload = b'{"ok":true}'
    calls = []
    def sync(parent): calls.append(parent)
    assert formal.publish_nonreplace(output, payload, fsync_parent=sync)["published"] is True
    assert formal.publish_nonreplace(output, payload, fsync_parent=sync)["retry_idempotent"] is True
    assert calls == [tmp_path, tmp_path]
    with pytest.raises(CustodyError): formal.publish_nonreplace(output, b'{"ok":false}', fsync_parent=sync)
    assert not list(tmp_path.glob(".report.json.tmp-*"))
    failed = tmp_path / "failed.json"
    with pytest.raises(RuntimeError): formal.publish_nonreplace(failed, payload, fsync_parent=lambda _parent: (_ for _ in ()).throw(RuntimeError("fsync")))
    assert not failed.exists() and not list(tmp_path.glob(".failed.json.tmp-*"))
    target = tmp_path / "target.json"; target.write_bytes(payload); linked = tmp_path / "linked.json"
    try:
        linked.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation unavailable in this Windows test environment")
    with pytest.raises(CustodyError): formal.publish_nonreplace(linked, payload, fsync_parent=sync)
    with pytest.raises(SystemExit): formal.main([])


def test_audit_envelope_requires_fresh_post_score_attestation_for_a_valid_scored_report(tmp_path):
    p = projection(); root, _staging, candidate = bundle(tmp_path, p); proto = protocol(p, candidate)
    current, current_receipt = formal.freeze_current_worker(encoder=Encoder(), protocol=proto, worker_config=worker_config(p, proto), candidate_bundle_root=root, staging_parent=tmp_path)
    original = original_replicates(p)
    sealed = {"replicates": original, "lifecycle": list(formal.ORIGINAL_LIFECYCLE), "original_code_before": proto["original_code_receipt"], "original_code_after": proto["original_code_receipt"]}
    sealed["worker_sha256"] = formal._digest({key: sealed[key] for key in ("replicates", "lifecycle", "original_code_before", "original_code_after")})
    original_artifact = formal.validate_original_worker_receipt(sealed, projection=p, protocol=proto)["artifact"]
    artifacts = [original_artifact, *current]; endpoint = formal.freeze_endpoint_manifest(projection=p, protocol=proto, ranking_artifacts=artifacts)
    resources = formal_resources(original_artifact, original, current, p)
    current_receipt = bind_live_execution_receipts(current_receipt, proto, p, current, resources)
    custody_ready, custody_bundle, release_secret, scorer_secret = h("custody-ready"), h("custody-bundle"), b"r" * 32, b"s" * 32
    release = {"schema": formal.RELEASE_SCHEMA, "protocol_sha256": proto["protocol_sha256"], "endpoint_manifest_sha256": endpoint["manifest_sha256"], "candidate_ready_sha256": candidate["ready_sha256"], "projection_raw_sha256": candidate["projection_raw_sha256"], "projection_canonical_sha256": candidate["projection_canonical_sha256"], "custody_ready_sha256": custody_ready, "custody_bundle_sha256": custody_bundle, "ranking_artifact_sha256": {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in endpoint["arms"]}, "resource_sha256": resource_map(resources), "original_build_index_sha256": {row["build_id"]: row["index_sha256"] for row in original}, "current_worker_sha256": current_receipt["worker_sha256"]}
    release = formal.sign_release_authorization(release, custody_capability_secret=release_secret)
    report = score.score_frozen(projection=p, endpoint_manifest=endpoint, ranking_artifacts=artifacts, custody_loader=lambda: scoring_custody(p), evidence_token_secret=b"e" * 32)
    unsigned = {"schema": formal.POST_SCORE_ATTESTATION_SCHEMA, "release_sha256": release["release_sha256"], "report_sha256": report["report_sha256"], "protocol_sha256": proto["protocol_sha256"], "endpoint_manifest_sha256": endpoint["manifest_sha256"], "ranking_artifact_sha256": report["ranking_artifact_sha256"], "resource_sha256": release["resource_sha256"]}
    attestation = formal.sign_post_score_attestation(unsigned, scorer_attestation_secret=scorer_secret)
    envelope = formal.audit_envelope(report=report, post_score_attestation=attestation, scorer_attestation_secret=scorer_secret, release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=release_secret)
    assert envelope["current_worker_receipt"]["worker_sha256"] == current_receipt["worker_sha256"]
    changed = copy.deepcopy(report)
    comparison = changed["paired_bootstrap"]["overall_positive"]["paired_deltas"]["static_p5"]["vs_original_public_product"]
    comparison["ci_lower"] = float(comparison["ci_lower"]) - .001
    changed["report_sha256"] = score.report_digest(changed)
    assert score.validate_report(changed)["report_sha256"] == changed["report_sha256"]
    with pytest.raises(CustodyError, match="post_score_attestation_binding_invalid"):
        formal.audit_envelope(report=changed, post_score_attestation=attestation, scorer_attestation_secret=scorer_secret, release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle, custody_capability_secret=release_secret)


def test_candidate_loader_reuses_ready_and_rejects_staging_overlap_or_raw_drift(tmp_path):
    p = projection(); root, _staging, candidate = bundle(tmp_path, p); proto = protocol(p, candidate); config = worker_config(p, proto)
    overlap = copy.deepcopy(config); overlap["staging_root"] = "candidate"
    with pytest.raises(CustodyError, match="candidate_staging_custody_overlap"):
        formal.load_candidate_worker_projection(worker_config=overlap, protocol=proto, candidate_bundle_root=root, staging_parent=tmp_path)
    (root / "projection.json").write_bytes(b"{}")
    with pytest.raises(CustodyError):
        formal.load_candidate_worker_projection(worker_config=config, protocol=proto, candidate_bundle_root=root, staging_parent=tmp_path)


def test_post_score_attestation_is_independent_of_release_and_rejects_resealed_report_change():
    secret = b"a" * 32; release = {"release_sha256": h("release"), "resource_sha256": {"strong_raw": h("resource")}}
    protocol_row = {"protocol_sha256": h("protocol")}; endpoint = {"manifest_sha256": h("endpoint")}
    report = {"report_sha256": h("report-v1"), "ranking_artifact_sha256": {"strong_raw": h("artifact")}, "metric": 0.5}
    unsigned = {"schema": formal.POST_SCORE_ATTESTATION_SCHEMA, "release_sha256": release["release_sha256"], "report_sha256": report["report_sha256"], "protocol_sha256": protocol_row["protocol_sha256"], "endpoint_manifest_sha256": endpoint["manifest_sha256"], "ranking_artifact_sha256": report["ranking_artifact_sha256"], "resource_sha256": release["resource_sha256"]}
    attestation = formal.sign_post_score_attestation(unsigned, scorer_attestation_secret=secret)
    assert formal.validate_post_score_attestation(attestation, release=release, report=report, protocol=protocol_row, endpoint_manifest=endpoint, scorer_attestation_secret=secret)["report_sha256"] == report["report_sha256"]
    report = {**report, "metric": .9, "report_sha256": h("report-v2")}
    with pytest.raises(CustodyError, match="post_score_attestation_binding_invalid"):
        formal.validate_post_score_attestation(attestation, release=release, report=report, protocol=protocol_row, endpoint_manifest=endpoint, scorer_attestation_secret=secret)


def _published_confirmation_bundle(tmp_path, monkeypatch, name):
    canonical, premix, candidate, custody = tmp_path / (name + "-labels"), tmp_path / (name + "-premix"), tmp_path / name, tmp_path / (name + "-custody")
    cases = []
    for persona in ("p-a", "p-b"):
        for group in ("group-1", "group-2"):
            conversation = f"{persona}-{group}"; suffix = group[-1]
            evidence = {"personId": persona, "question": f"q-{persona}-{suffix}", "answer": f"SECRET-{persona}-{group}", "category": group, "conversations": [{"id": conversation}], "message_evidences": [{"speaker": "secret-speaker", "text": "secret-evidence"}]}
            path = canonical / "core_benchmark" / "evidence_questions" / group / "tier-1" / f"{persona}.json"; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps({"evidence_items": [evidence]}))
            for size in (1, 8, 13): cases.append({"contextSize": size, "evidenceItems": [{key: evidence[key] for key in ("personId", "question", "answer", "category", "conversations")}], "conversations": [{"id": conversation, "messages": [{"speaker": "speaker", "text": "candidate-a"}, {"speaker": "speaker", "text": "candidate-b"}]}]})
    path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(cases))
    monkeypatch.setattr(confirmation, "_verify_sqlite_temp_environment", lambda staging: {"os_rule": "synthetic", "resolved_temp_path": str(staging)})
    staging = tmp_path / (name + "-staging"); staging.mkdir()
    secret = b"aerp7-synthetic-secret-key-must-be-long"
    confirmation.build_prelabel_bundle(canonical_root=canonical, premix_root=premix, candidate_output_dir=candidate, custody_output_dir=custody, staging_root=staging, secret=secret, selection=confirmation.SelectionConfig(seed=7, persona_quota=1, per_persona_group_quota=1, context_rank_indices=(0, 2)))
    return candidate, custody, secret


def test_open_custody_after_release_uses_actual_confirmation_bundles(tmp_path, monkeypatch):
    candidate, custody, binding_secret = _published_confirmation_bundle(tmp_path, monkeypatch, "A"); p = confirmation.load_candidate_projection(candidate)
    ready_raw = (candidate / "READY.json").read_bytes(); projection_raw = (candidate / "projection.json").read_bytes(); ready = json.loads(ready_raw)
    candidate_receipt = {"generation_id": ready["generation_id"], "ready_sha256": hashlib.sha256(ready_raw).hexdigest(), "projection_raw_sha256": hashlib.sha256(projection_raw).hexdigest(), "projection_canonical_sha256": canonical_sha256(p), "query_count": len(p["items"]), "candidate_text_count": sum(len(corpus["candidates"]) for corpus in p["corpora"])}
    (tmp_path / "staging").mkdir(); proto = protocol(p, candidate_receipt); config = worker_config(p, proto); current, current_receipt = formal.freeze_current_worker(encoder=Encoder(), protocol=proto, worker_config=config, candidate_bundle_root=candidate, staging_parent=tmp_path)
    original = original_replicates(p); sealed = {"replicates": original, "lifecycle": list(formal.ORIGINAL_LIFECYCLE), "original_code_before": proto["original_code_receipt"], "original_code_after": proto["original_code_receipt"]}; sealed["worker_sha256"] = formal._digest({key: sealed[key] for key in ("replicates", "lifecycle", "original_code_before", "original_code_after")}); original_artifact = formal.validate_original_worker_receipt(sealed, projection=p, protocol=proto)["artifact"]
    artifacts = [original_artifact, *current]; endpoint = formal.freeze_endpoint_manifest(projection=p, protocol=proto, ranking_artifacts=artifacts)
    resources = formal_resources(original_artifact, original, current, p)
    current_receipt = bind_live_execution_receipts(current_receipt, proto, p, current, resources)
    custody_ready = hashlib.sha256((custody / "READY.json").read_bytes()).hexdigest(); custody_raw = hashlib.sha256((custody / "sealed-custody.json").read_bytes()).hexdigest(); resource_digests = resource_map(resources)
    release = {"schema": formal.RELEASE_SCHEMA, "protocol_sha256": proto["protocol_sha256"], "endpoint_manifest_sha256": endpoint["manifest_sha256"], "candidate_ready_sha256": candidate_receipt["ready_sha256"], "projection_raw_sha256": candidate_receipt["projection_raw_sha256"], "projection_canonical_sha256": candidate_receipt["projection_canonical_sha256"], "custody_ready_sha256": custody_ready, "custody_bundle_sha256": custody_raw, "ranking_artifact_sha256": {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in endpoint["arms"]}, "resource_sha256": resource_digests, "original_build_index_sha256": {row["build_id"]: row["index_sha256"] for row in original}, "current_worker_sha256": current_receipt["worker_sha256"]}
    release = formal.sign_release_authorization(release, custody_capability_secret=b"c" * 32)
    assert formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_raw, custody_capability_secret=b"c" * 32, candidate_bundle_root=candidate, custody_bundle_root=custody, binding_secret=binding_secret)["projection_sha256"] == canonical_sha256(p)
    original_loader = confirmation.load_custody_for_scoring
    changed_ready = json.loads(ready_raw); changed_ready["durability"]["steps"] = ["synthetic-durability-change"]
    (candidate / "READY.json").write_bytes(json.dumps(changed_ready, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
    monkeypatch.setattr(confirmation, "load_custody_for_scoring", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("candidate READY mismatch must precede custody open")))
    with pytest.raises(CustodyError, match="candidate_release_ready_binding_invalid"):
        formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_raw, custody_capability_secret=b"c" * 32, candidate_bundle_root=candidate, custody_bundle_root=custody, binding_secret=binding_secret)
    (candidate / "READY.json").write_bytes(ready_raw)
    def candidate_drift(*args, **kwargs):
        value = original_loader(*args, **kwargs)
        drifted = json.loads(ready_raw); drifted["durability"]["steps"] = ["synthetic-loader-drift"]
        (candidate / "READY.json").write_bytes(json.dumps(drifted, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())
        return value
    monkeypatch.setattr(confirmation, "load_custody_for_scoring", candidate_drift)
    with pytest.raises(CustodyError, match="custody_release_bundle_drift"):
        formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_raw, custody_capability_secret=b"c" * 32, candidate_bundle_root=candidate, custody_bundle_root=custody, binding_secret=binding_secret)
    (candidate / "READY.json").write_bytes(ready_raw)
    def drift(*args, **kwargs):
        value = original_loader(*args, **kwargs); (custody / "READY.json").write_bytes(b"{}"); return value
    monkeypatch.setattr(confirmation, "load_custody_for_scoring", drift)
    with pytest.raises(CustodyError, match="custody_release_bundle_drift"):
        formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_raw, custody_capability_secret=b"c" * 32, candidate_bundle_root=candidate, custody_bundle_root=custody, binding_secret=binding_secret)
    monkeypatch.setattr(confirmation, "load_custody_for_scoring", original_loader)
    # Keep the original candidate (whose READY was restored byte-for-byte) so
    # the final custody tamper reaches the custody binding check.
    candidate_b, custody_b, _ = _published_confirmation_bundle(tmp_path, monkeypatch, "B")
    with pytest.raises(CustodyError, match="candidate_release_ready_binding_invalid"):
        formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_raw, custody_capability_secret=b"c" * 32, candidate_bundle_root=candidate_b, custody_bundle_root=custody_b, binding_secret=binding_secret)
    (custody / "sealed-custody.json").write_bytes(b"{}")
    with pytest.raises(CustodyError, match="custody_release_bundle_binding_invalid"):
        formal.open_custody_after_release(release_authorization=release, projection=p, ranking_artifacts=artifacts, current_worker_receipt=current_receipt, protocol=proto, endpoint_manifest=endpoint, resource_receipts=resources, custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_raw, custody_capability_secret=b"c" * 32, candidate_bundle_root=candidate, custody_bundle_root=custody, binding_secret=binding_secret)
