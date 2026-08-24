"""Label-blind, independently auditable AERP-7 ranking freezes.

Artifacts bind a frozen top-10 to its exact candidate-side inputs. They do not
pretend that a digest can re-execute an encoder.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.aerp_mempalace_v380_runtime_contract import original_hnsw_configuration
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256, validate_candidate_projection
from mempalace_rpg.retrieval import (
    AuthorizedRetrievalCandidate, FixedP5Policy, FusionRoutingDecision,
    P5_EXPERT_WEIGHTS, RAW_EXPERT_WEIGHTS, SIX_VIEW_WEIGHTS, SixViewRanker,
    structured_observation,
)

RANKING_SCHEMA = "aerp7-convomem-frozen-ranking-v3"
CURRENT_ARMS = frozenset({"strong_raw", "static_p5", "six_view_secondary"})
CONFIDENCE_CONTRACT = "normalized_top_margin_v1"
PROTOCOL_SOURCE = {
    "repository": "SalesforceAIResearch/ConvoMem",
    "commit": "624f582ecf0d336ae1d4539d19186089800774b1",
    "tree": "1699a58948e7ac4e3263110a40d06bab457bcf8b",
    "files": {
        "LongContextMemoryAnswerer.scala": "857c90034aeaf167422c092515332293bc5548c9e90ecd03f57b7973ee9a0155",
        "MultithreadedEvaluator.scala": "e83194fb5eadf9c65c1a8d120445f6bee692fb1e45dd573113ecbeb90c039594",
        "AnsweringEvaluation.scala": "da951b2b96fd29ef87467c13b593764f9924f26a3bb49fbd97e0f830e1ed6b08",
        "BatchedTestCasesGenerator.scala": "43466f4c9d3d087e9ff8ef22f9f4c189468d6c31d4a1f6e819fde5233f23c6be",
    },
}
CURRENT_SERIALIZER = {
    "name": "aerp7-current-structured-observation-v1",
    "observation": "structured_observation(summary=text,event_type=conversation_message,actor_id=speaker,target_id=None,related_entities=None,related_quests=None,related_locations=None,in_world_time=None,location_id=None)",
}
ORIGINAL_MEMPALACE_SERIALIZER = {
    "name": "mempalace-public-product-text-only-v1",
    "document": "text",
    "metadata": "speaker metadata only; not encoder-visible",
}
ORIGINAL_METHOD = {
    "arm_id": "original_public_product", "runner": "mempalace_public_product",
    "candidate_strategy": "vector", "top_k": 10,
    "replicate_aggregation": "per_query_arithmetic_mean", "replicate_seed_rule": "manifest_fixed",
}
ORIGINAL_CALL_CONTRACT = {
    "candidate_strategy": "vector", "top_k": 10, "room_scope": "corpus_id",
    "collection_name": "mempalace_drawers", "cold_reopen": True,
}
ORIGINAL_HNSW_CONFIG = original_hnsw_configuration()
ORIGINAL_OPERATIONAL_DELTA = {"schema": "aerp5-chroma-operational-delta-v1", "excluded_table": "acquire_write", "permitted_transition": "unchanged_or_append_next_integer_id_lock_status_1", "validation": "passed"}
ORIGINAL_GRAPH_NAMES = ("data_level0.bin", "header.bin", "length.bin", "link_lists.bin")


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise CustodyError(code)
    return value


def _int(value: Any, code: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0):
        raise CustodyError(code)
    return value


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return dict(value)


def _finite(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CustodyError(code)
    return float(value)


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def _candidate_input(corpus: Mapping[str, Any], serializer: Mapping[str, Any]) -> str:
    return _digest({"serializer": serializer, "corpus_id": corpus["corpus_id"], "candidates": corpus["candidates"]})


def _input_receipt(projection: Mapping[str, Any], serializer: Mapping[str, Any]) -> dict[str, Any]:
    corpora = {row["corpus_id"]: row for row in projection["corpora"]}
    rows = [{"item_id": item["item_id"], "corpus_id": item["corpus_id"], "candidate_input_sha256": _candidate_input(corpora[item["corpus_id"]], serializer)} for item in sorted(projection["items"], key=lambda row: row["item_id"])]
    return {"projection_sha256": canonical_sha256(projection), "serializer_sha256": _digest(serializer), "item_corpora": rows, "item_corpus_set_sha256": _digest(rows)}


def _validate_model_receipt(value: Any) -> dict[str, Any]:
    row = _object(value, "model_receipt_invalid")
    if set(row) != {"encoder_identity", "encoder_semantics", "files"}:
        raise CustodyError("model_receipt_schema_invalid")
    if not isinstance(row["encoder_identity"], str) or not row["encoder_identity"].strip() or not isinstance(row["encoder_semantics"], str) or not row["encoder_semantics"].strip():
        raise CustodyError("model_receipt_identity_invalid")
    files = row["files"]
    if not isinstance(files, list) or not files:
        raise CustodyError("model_receipt_files_invalid")
    roles: set[str] = set()
    for item in files:
        item = _object(item, "model_receipt_file_invalid")
        # Historical synthetic receipts deliberately contain only a logical file
        # role.  A live executor must additionally require ``relative_path`` so
        # it can bind this receipt to a real model tree; accepting both here
        # keeps frozen synthetic fixtures readable without weakening live mode.
        if set(item) not in ({"path_role", "sha256", "bytes"}, {"path_role", "relative_path", "sha256", "bytes"}) or not isinstance(item["path_role"], str) or not item["path_role"].strip() or item["path_role"] in roles:
            raise CustodyError("model_receipt_file_invalid")
        if "relative_path" in item and (not isinstance(item["relative_path"], str) or not item["relative_path"].strip() or Path(item["relative_path"]).is_absolute() or ".." in Path(item["relative_path"]).parts):
            raise CustodyError("model_receipt_file_invalid")
        roles.add(item["path_role"]); _hex(item["sha256"], "model_receipt_file_invalid"); _int(item["bytes"], "model_receipt_file_invalid", positive=True)
    return row


def _validate_code_receipt(value: Any) -> dict[str, Any]:
    row = _object(value, "code_receipt_invalid")
    if set(row) != {"head", "tree", "diff_digest", "dirty_policy"} or row.get("dirty_policy") != "clean_required":
        raise CustodyError("code_receipt_formal_clean_required")
    for key in ("head", "tree", "diff_digest"):
        _hex(row.get(key), "code_receipt_invalid")
    return row


class FixedRawPolicy:
    def decide(self, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int) -> FusionRoutingDecision:
        if set(ranks) != set(SIX_VIEW_WEIGHTS) or set(ranking_keys_by_id) != set(next(iter(ranks.values()), {})):
            raise ValueError("raw ranking inputs are invalid")
        totals = {identifier: sum(weight / (rrf_k + ranks[name][identifier]) for name, weight in RAW_EXPERT_WEIGHTS.items()) for identifier in ranking_keys_by_id}
        return FusionRoutingDecision(route="raw", totals=tuple(totals.items()), effective_weights=tuple(RAW_EXPERT_WEIGHTS.items()))


def authorized_candidates(projection: Any) -> dict[str, list[AuthorizedRetrievalCandidate]]:
    frozen = validate_candidate_projection(projection); revision = frozen["dataset"]["revision_sha256"]
    result: dict[str, list[AuthorizedRetrievalCandidate]] = {}
    for corpus in frozen["corpora"]:
        result[corpus["corpus_id"]] = [AuthorizedRetrievalCandidate(
            source_event_id=row["message_id"], source_scene_id=corpus["corpus_id"], raw_text=row["text"],
            observation=structured_observation(summary=row["text"], event_type="conversation_message", actor_id=row["speaker"], target_id=None, related_entities=None, related_quests=None, related_locations=None, in_world_time=None, location_id=None),
            checkpoint_key=row["opaque_conversation_id"], policy_tuple=(), chronological_order_key=(row["corpus_order"], row["message_id"]),
            ranking_key=_digest({"revision": revision, "corpus_id": corpus["corpus_id"], "message_id": row["message_id"], "corpus_order": row["corpus_order"]}),
        ) for row in corpus["candidates"]]
    return result


def _ranker(encoder: Any, arm_id: str) -> SixViewRanker:
    if arm_id == "strong_raw": return SixViewRanker(encoder, diagnostic_ledger=True, routing_policy=FixedRawPolicy())
    if arm_id == "static_p5": return SixViewRanker(encoder, diagnostic_ledger=True, routing_policy=FixedP5Policy())
    if arm_id == "six_view_secondary": return SixViewRanker(encoder, diagnostic_ledger=True)
    raise CustodyError("ranking_arm_unknown")


def _arm_method(arm_id: str) -> dict[str, Any]:
    weights = {"strong_raw": RAW_EXPERT_WEIGHTS, "static_p5": P5_EXPERT_WEIGHTS, "six_view_secondary": SIX_VIEW_WEIGHTS}.get(arm_id)
    if weights is None: raise CustodyError("ranking_arm_unknown")
    return {"arm_id": arm_id, "ranker": "SixViewRanker", "rrf_k": SixViewRanker.rrf_k, "weights": weights, "confidence_contract": CONFIDENCE_CONTRACT}


def _margin(top_two_scores: Sequence[Any]) -> float:
    if not isinstance(top_two_scores, list) or len(top_two_scores) != 2:
        raise CustodyError("ranking_confidence_receipt_invalid")
    first, second = (_finite(value, "ranking_confidence_receipt_invalid") for value in top_two_scores)
    if first <= 0 or second < 0 or second > first:
        raise CustodyError("ranking_confidence_receipt_invalid")
    value = 1.0 - second / first
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise CustodyError("ranking_confidence_receipt_invalid")
    return value


def _current_row(item: Mapping[str, Any], corpus: Mapping[str, Any], result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    ranked = list(result.ranked_event_ids[:10]); allowed = {row["message_id"]: row for row in corpus["candidates"]}
    if len(ranked) != min(10, len(allowed)) or len(ranked) != len(set(ranked)) or set(ranked) - set(allowed):
        raise CustodyError("ranking_top10_invalid")
    scores = [float(result.scores[item_id]) for item_id in ranked[:2]]
    confidence = _margin(scores)
    row = {"item_id": item["item_id"], "query_sha256": _query_digest(item["query_text"]), "candidate_input_sha256": _candidate_input(corpus, CURRENT_SERIALIZER), "ranked_message_ids": ranked, "retrieved_conversation_ids": list(dict.fromkeys(allowed[item_id]["opaque_conversation_id"] for item_id in ranked)), "confidence": confidence, "confidence_receipt": {"contract": CONFIDENCE_CONTRACT, "top_two_scores": scores}}
    # ``result.trace`` and its FCD1 ledger are text-free (hashes, IDs, scores,
    # ranks and receipts only).  Store them verbatim so validation can replay
    # semantics rather than trusting a hash supplied by the ranker.
    trace = {"item_id": item["item_id"], "query_sha256": row["query_sha256"], "candidate_input_sha256": row["candidate_input_sha256"], "ranked_count": len(ranked), "ranking_sha256": _digest(ranked), "ranker_trace_sha256": _digest(result.trace), "ranking_trace": result.trace}
    return row, trace


def rank_projection(*, projection: Any, encoder: Any, arm_id: str, model_receipt: Mapping[str, Any] | None = None, code_receipt: Mapping[str, Any] | None = None, query_measurements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    frozen = validate_candidate_projection(projection)
    if arm_id not in CURRENT_ARMS or model_receipt is None or code_receipt is None: raise CustodyError("ranking_receipt_required")
    model = _validate_model_receipt(model_receipt); code = _validate_code_receipt(code_receipt)
    # A valid receipt is not an assertion about an arbitrary encoder instance.
    # The identity check is deliberately before ranker construction, so an
    # accidental fallback encoder cannot emit even a partial artifact.
    if getattr(encoder, "identity", None) != model["encoder_identity"]:
        raise CustodyError("ranking_encoder_identity_mismatch")
    if query_measurements is not None and query_measurements:
        raise CustodyError("ranking_query_measurement_prefilled")
    ranker = _ranker(encoder, arm_id)
    candidates = authorized_candidates(frozen); corpora = {row["corpus_id"]: row for row in frozen["corpora"]}; rows = []; trace = []
    for item in sorted(frozen["items"], key=lambda row: row["item_id"]):
        started_ns = time.perf_counter_ns(); cpu_started_ns = time.process_time_ns()
        result = ranker.rank(query=item["query_text"], candidates=candidates[item["corpus_id"]])
        elapsed_ns = max(1, time.perf_counter_ns() - started_ns)
        cpu_elapsed_ns = max(1, time.process_time_ns() - cpu_started_ns)
        if query_measurements is not None:
            query_measurements.append({
                "item_id": item["item_id"],
                "query_sha256": _query_digest(item["query_text"]),
                "wall_ns": elapsed_ns,
                "cpu_ns": cpu_elapsed_ns,
            })
        row, trace_row = _current_row(item, corpora[item["corpus_id"]], result)
        rows.append(row); trace.append(trace_row)
    value = {"schema": RANKING_SCHEMA, "arm_id": arm_id, "projection_sha256": canonical_sha256(frozen), "input_receipt": _input_receipt(frozen, CURRENT_SERIALIZER), "model_receipt": model, "method_receipt": _arm_method(arm_id), "source_receipt": PROTOCOL_SOURCE, "serializer_receipt": CURRENT_SERIALIZER, "code_receipt": code, "trace_receipt": trace, "rankings": rows}
    for field, receipt in (("input_sha256", "input_receipt"), ("model_sha256", "model_receipt"), ("method_sha256", "method_receipt"), ("source_commit_sha256", "source_receipt"), ("serializer_sha256", "serializer_receipt"), ("code_sha256", "code_receipt"), ("trace_sha256", "trace_receipt")):
        value[field] = _digest(value[receipt])
    value["artifact_sha256"] = _digest(value); return value


def freeze_current_rankings(*, projection: Any, encoder: Any, model_receipt: Mapping[str, Any] | None = None, code_receipt: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    raw = rank_projection(projection=projection, encoder=encoder, arm_id="strong_raw", model_receipt=model_receipt, code_receipt=code_receipt)
    first = rank_projection(projection=projection, encoder=encoder, arm_id="static_p5", model_receipt=model_receipt, code_receipt=code_receipt)
    second = rank_projection(projection=projection, encoder=encoder, arm_id="static_p5", model_receipt=model_receipt, code_receipt=code_receipt)
    if _bytes(first) != _bytes(second): raise CustodyError("static_p5_repeat_nondeterministic")
    return [raw, first, rank_projection(projection=projection, encoder=encoder, arm_id="six_view_secondary", model_receipt=model_receipt, code_receipt=code_receipt)]


def _original_replicate(projection: Mapping[str, Any], value: Any) -> dict[str, Any]:
    row = _object(value, "original_replicate_invalid")
    if set(row) != {"build_id", "input_receipt", "input_sha256", "index_receipt", "index_sha256", "trace_receipt", "trace_sha256", "rankings"} or not isinstance(row.get("build_id"), str) or not row["build_id"].strip():
        raise CustodyError("original_replicate_schema_invalid")
    expected_input = _input_receipt(projection, ORIGINAL_MEMPALACE_SERIALIZER)
    if row["input_receipt"] != expected_input: raise CustodyError("original_input_receipt_invalid")
    for digest_key, receipt_key in (("input_sha256", "input_receipt"), ("index_sha256", "index_receipt"), ("trace_sha256", "trace_receipt")):
        _hex(row.get(digest_key), "original_replicate_digest_invalid")
        if row[digest_key] != _digest(row[receipt_key]): raise CustodyError("original_replicate_digest_invalid")
    index = _object(row["index_receipt"], "original_index_receipt_invalid")
    expected_input = _input_receipt(projection, ORIGINAL_MEMPALACE_SERIALIZER)
    expected_queries = _digest([{"item_id": item["item_id"], "query_sha256": _query_digest(item["query_text"])} for item in sorted(projection["items"], key=lambda item: item["item_id"])])
    expected_outputs = _digest([{"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in sorted(row["trace_receipt"], key=lambda trace: trace["item_id"])])
    required_index = {"build_id", "fresh_build", "collection_identity", "index_identity_sha256", "cold_reopen", "call_contract", "input_coverage_sha256", "query_coverage_sha256", "output_coverage_sha256", "worker_physical_receipt", "coordinator_physical_receipt"}
    if set(index) != required_index or index["build_id"] != row["build_id"] or index["fresh_build"] is not True or index["cold_reopen"] is not True or index["call_contract"] != ORIGINAL_CALL_CONTRACT:
        raise CustodyError("original_index_receipt_invalid")
    if not isinstance(index["collection_identity"], str) or not index["collection_identity"].strip(): raise CustodyError("original_index_receipt_invalid")
    for key, expected in (("index_identity_sha256", None), ("input_coverage_sha256", _digest(expected_input["item_corpora"])), ("query_coverage_sha256", expected_queries), ("output_coverage_sha256", expected_outputs)):
        _hex(index[key], "original_index_receipt_invalid")
        if expected is not None and index[key] != expected: raise CustodyError("original_index_receipt_invalid")
    physical_ids = [f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in projection["corpora"] for candidate in corpus["candidates"]]
    expected_physical = {"physical_count": len(physical_ids), "physical_ids_sha256": _digest(sorted(physical_ids))}
    required_physical = {"physical_count", "physical_ids_sha256", "embedding", "hnsw_config", "graph_files", "immutable_backend_sha256", "sqlite_semantic_sha256", "operational_delta"}
    worker = _object(index["worker_physical_receipt"], "original_physical_receipt_invalid"); coordinator = _object(index["coordinator_physical_receipt"], "original_physical_receipt_invalid")
    if worker != coordinator or set(worker) != required_physical or any(worker[key] != expected for key, expected in expected_physical.items()) or worker.get("hnsw_config") != ORIGINAL_HNSW_CONFIG:
        raise CustodyError("original_physical_receipt_invalid")
    embedding = _object(worker["embedding"], "original_physical_receipt_invalid")
    if set(embedding) != {"count", "dimension", "dtype", "float32_sha256"} or embedding.get("count") != len(physical_ids) or embedding.get("dimension") != 384 or embedding.get("dtype") != "float32": raise CustodyError("original_physical_receipt_invalid")
    _hex(embedding.get("float32_sha256"), "original_physical_receipt_invalid"); _hex(worker.get("immutable_backend_sha256"), "original_physical_receipt_invalid"); _hex(worker.get("sqlite_semantic_sha256"), "original_physical_receipt_invalid")
    graphs = worker.get("graph_files")
    if not isinstance(graphs, list) or [entry.get("name") for entry in graphs if isinstance(entry, Mapping)] != list(ORIGINAL_GRAPH_NAMES): raise CustodyError("original_physical_receipt_invalid")
    for graph in graphs:
        graph = _object(graph, "original_physical_receipt_invalid")
        if set(graph) != {"name", "bytes", "sha256"} or not isinstance(graph["name"], str) or not graph["name"] or _int(graph["bytes"], "original_physical_receipt_invalid", positive=True) < 1: raise CustodyError("original_physical_receipt_invalid")
        _hex(graph["sha256"], "original_physical_receipt_invalid")
    if worker["operational_delta"] != ORIGINAL_OPERATIONAL_DELTA: raise CustodyError("original_physical_receipt_invalid")
    if index["index_identity_sha256"] != _digest({"collection_identity": index["collection_identity"], "physical": worker}): raise CustodyError("original_index_receipt_invalid")
    return row


def wrap_original_public_rankings(*, projection: Any, replicates: Sequence[Mapping[str, Any]], model_receipt: Mapping[str, Any], code_receipt: Mapping[str, Any]) -> dict[str, Any]:
    frozen = validate_candidate_projection(projection); model = _validate_model_receipt(model_receipt); code = _validate_code_receipt(code_receipt)
    if not isinstance(replicates, Sequence) or isinstance(replicates, (str, bytes)) or len(replicates) != 5: raise CustodyError("original_replicate_contract_invalid")
    records = [_original_replicate(frozen, row) for row in replicates]
    if len({row["build_id"] for row in records}) != 5: raise CustodyError("original_build_id_duplicate")
    value = {"schema": RANKING_SCHEMA, "arm_id": "original_public_product", "projection_sha256": canonical_sha256(frozen), "input_receipt": _input_receipt(frozen, ORIGINAL_MEMPALACE_SERIALIZER), "model_receipt": model, "method_receipt": ORIGINAL_METHOD, "source_receipt": PROTOCOL_SOURCE, "serializer_receipt": ORIGINAL_MEMPALACE_SERIALIZER, "code_receipt": code, "replicates": records}
    for field, receipt in (("input_sha256", "input_receipt"), ("model_sha256", "model_receipt"), ("method_sha256", "method_receipt"), ("source_commit_sha256", "source_receipt"), ("serializer_sha256", "serializer_receipt"), ("code_sha256", "code_receipt")):
        value[field] = _digest(value[receipt])
    value["artifact_sha256"] = _digest(value)
    # Reject an externally supplied five-run envelope at publication time too;
    # downstream scoring repeats this verification before custody is opened.
    return validate_frozen_ranking(value, projection=frozen)


def _validate_receipts(value: Mapping[str, Any], projection: Mapping[str, Any], *, arm_id: str) -> None:
    if value["projection_sha256"] != canonical_sha256(projection): raise CustodyError("ranking_projection_digest_invalid")
    for field, receipt in (("input_sha256", "input_receipt"), ("model_sha256", "model_receipt"), ("method_sha256", "method_receipt"), ("source_commit_sha256", "source_receipt"), ("serializer_sha256", "serializer_receipt"), ("code_sha256", "code_receipt")):
        _hex(value.get(field), "ranking_artifact_digest_invalid")
        if _digest(value[receipt]) != value[field]: raise CustodyError("ranking_receipt_digest_mismatch")
    _validate_model_receipt(value["model_receipt"]); _validate_code_receipt(value["code_receipt"])
    if value["source_receipt"] != PROTOCOL_SOURCE: raise CustodyError("ranking_source_receipt_invalid")
    serializer = ORIGINAL_MEMPALACE_SERIALIZER if arm_id == "original_public_product" else CURRENT_SERIALIZER
    method = ORIGINAL_METHOD if arm_id == "original_public_product" else _arm_method(arm_id)
    if value["serializer_receipt"] != serializer or value["method_receipt"] != method or value["input_receipt"] != _input_receipt(projection, serializer): raise CustodyError("ranking_contract_receipt_invalid")


def _validate_fcd1(ledger: Any, trace: Any, candidates: Sequence[AuthorizedRetrievalCandidate], weights: Mapping[str, float], ranked: Sequence[str]) -> None:
    """Replay the complete benchmark-only FCD1 receipt without plaintext.

    This is deliberately local rather than the LoCoMo helper: all three frozen
    arms have different valid weight vectors while the FCD1 structure is shared.
    """
    ledger = _object(ledger, "fcd1_ledger_invalid"); trace = _object(trace, "fcd1_trace_invalid")
    fields = {"schema", "input_sha256", "authorization_sha256", "view_top_50", "view_top_50_sha256", "view_full_order", "view_order_sha256", "fused_top_50", "checkpoint_tie_group_semantics", "checkpoint_tie_groups"}
    base_trace = {"schema", "encoder_identity", "weights", "rrf_k", "query_sha256", "input_sha256", "view_digests", "selected", "fcd1_diagnostic_ledger"}
    permitted = base_trace | ({"aerp5_fixed_p5"} if weights == P5_EXPERT_WEIGHTS else set())
    if set(trace) != permitted or trace.get("schema") != "aerp2-product-six-view-v1" or not isinstance(trace.get("encoder_identity"), str) or not trace["encoder_identity"].strip() or set(ledger) != fields or ledger.get("schema") != "aerp3-fcd1-replay-ledger-v1" or ledger.get("input_sha256") != trace.get("input_sha256") or trace.get("weights") != dict(weights) or trace.get("rrf_k") != SixViewRanker.rrf_k:
        raise CustodyError("fcd1_ledger_contract_invalid")
    candidates = sorted(candidates, key=lambda candidate: candidate.ranking_key)
    ids = [candidate.source_event_id for candidate in candidates]
    keys = {candidate.source_event_id: candidate.ranking_key for candidate in candidates}
    key_hash = {identifier: hashlib.sha256(key.encode("utf-8")).hexdigest() for identifier, key in keys.items()}
    key_order = {identifier: number for number, identifier in enumerate(ids, 1)}
    names = set(SIX_VIEW_WEIGHTS); top_count = min(50, len(ids))
    expected_input = _digest([{"ranking_key": candidate.ranking_key, "raw_sha256": hashlib.sha256(candidate.raw_text.encode("utf-8")).hexdigest(), "observation_sha256": hashlib.sha256(candidate.observation.encode("utf-8")).hexdigest(), "checkpoint": candidate.checkpoint_key.strip(), "policy": candidate.policy_tuple, "scene_time_sort": candidate.chronological_order_key[0]} for candidate in candidates])
    if trace["input_sha256"] != expected_input: raise CustodyError("fcd1_input_replay_invalid")
    views = ledger.get("view_top_50"); orders = ledger.get("view_full_order"); order_hashes = ledger.get("view_order_sha256"); top_hashes = ledger.get("view_top_50_sha256")
    if not all(isinstance(value, Mapping) and set(value) == names for value in (views, orders, order_hashes, top_hashes)):
        raise CustodyError("fcd1_view_schema_invalid")
    for name in names:
        full = orders[name]; top = views[name]
        if not isinstance(full, list) or len(full) != len(ids) or len(set(full)) != len(full) or set(full) != set(ids) or order_hashes[name] != _digest([key_hash[identifier] for identifier in full]):
            raise CustodyError("fcd1_view_order_invalid")
        if trace.get("view_digests", {}).get(name) != _digest([keys[identifier] for identifier in full]): raise CustodyError("fcd1_view_digest_invalid")
        if not isinstance(top, list) or len(top) != top_count or top_hashes[name] != _digest(top): raise CustodyError("fcd1_view_top_invalid")
        top_ids = []
        for number, record in enumerate(top, 1):
            record = _object(record, "fcd1_view_row_invalid")
            if set(record) != {"source_event_id", "ranking_key_sha256", "ranking_key_order", "rank", "score"} or record.get("source_event_id") not in keys or record.get("ranking_key_sha256") != key_hash[record["source_event_id"]] or record.get("ranking_key_order") != key_order[record["source_event_id"]] or record.get("rank") != number:
                raise CustodyError("fcd1_view_row_invalid")
            _finite(record.get("score"), "fcd1_view_score_invalid"); top_ids.append(record["source_event_id"])
        if top_ids != full[:top_count] or top_ids != [record["source_event_id"] for record in sorted(top, key=lambda record: (-float(record["score"]), int(record["ranking_key_order"])) )]: raise CustodyError("fcd1_view_semantics_invalid")
    groups = ledger.get("checkpoint_tie_groups")
    if ledger.get("checkpoint_tie_group_semantics") != "checkpoint_policy_rollup" or not isinstance(groups, list) or not groups: raise CustodyError("fcd1_checkpoint_schema_invalid")
    candidate_by_id = {candidate.source_event_id: candidate for candidate in candidates}; group_score = {}; authorization = []; group_members = []
    for group in groups:
        group = _object(group, "fcd1_checkpoint_group_invalid")
        if set(group) != {"group_id", "checkpoint_sha256", "policy_sha256", "checkpoint_score", "member_count", "chronological_members"}: raise CustodyError("fcd1_checkpoint_group_invalid")
        members = group.get("chronological_members")
        if not isinstance(members, list) or not members or group.get("member_count") != len(members) or group.get("policy_sha256") != _digest([]): raise CustodyError("fcd1_checkpoint_group_invalid")
        checkpoint = group.get("checkpoint_sha256"); policy = group.get("policy_sha256")
        _hex(checkpoint, "fcd1_checkpoint_group_invalid"); _hex(policy, "fcd1_checkpoint_group_invalid")
        if group.get("group_id") != "group:" + _digest([checkpoint, policy]): raise CustodyError("fcd1_checkpoint_group_invalid")
        score = _finite(group.get("checkpoint_score"), "fcd1_checkpoint_group_invalid")
        expected_members = []
        for member in members:
            member = _object(member, "fcd1_checkpoint_member_invalid"); identifier = member.get("source_event_id")
            if set(member) != {"source_event_id", "ranking_key_sha256"} or identifier not in candidate_by_id or member.get("ranking_key_sha256") != key_hash[identifier] or checkpoint != hashlib.sha256(candidate_by_id[identifier].checkpoint_key.encode("utf-8")).hexdigest(): raise CustodyError("fcd1_checkpoint_member_invalid")
            expected_members.append(identifier); group_members.append(identifier); group_score[identifier] = score; authorization.append({"ranking_key_sha256": key_hash[identifier], "policy_sha256": policy})
        ordered_members = [candidate.source_event_id for candidate in sorted((candidate_by_id[identifier] for identifier in expected_members), key=lambda candidate: candidate.chronological_order_key)]
        if expected_members != ordered_members: raise CustodyError("fcd1_checkpoint_chronology_invalid")
    if len(group_members) != len(set(group_members)) or set(group_members) != set(ids) or ledger.get("authorization_sha256") != _digest(sorted(authorization, key=lambda record: record["ranking_key_sha256"])): raise CustodyError("fcd1_authorization_invalid")
    checkpoint_rows = {record["source_event_id"]: record for record in views["checkpoint_dense"]}
    if any(not math.isclose(float(record["score"]), group_score[identifier], rel_tol=0.0, abs_tol=1e-15) for identifier, record in checkpoint_rows.items()): raise CustodyError("fcd1_checkpoint_score_invalid")
    fused = ledger.get("fused_top_50")
    if not isinstance(fused, list) or len(fused) != top_count: raise CustodyError("fcd1_fusion_schema_invalid")
    fused_ids = []
    for number, record in enumerate(fused, 1):
        record = _object(record, "fcd1_fusion_row_invalid"); identifier = record.get("source_event_id")
        required = {"source_event_id", "ranking_key_sha256", "ranking_key_order", "rank", "final_rrf", "component_ranks", "component_rank_receipts", "contributions"}
        if set(record) != required or identifier not in keys or record.get("ranking_key_sha256") != key_hash[identifier] or record.get("ranking_key_order") != key_order[identifier] or record.get("rank") != number: raise CustodyError("fcd1_fusion_row_invalid")
        component = _object(record.get("component_ranks"), "fcd1_fusion_row_invalid"); contribution = _object(record.get("contributions"), "fcd1_fusion_row_invalid")
        if set(component) != set(weights) or set(contribution) != set(weights): raise CustodyError("fcd1_fusion_weights_invalid")
        expected = {}
        for name, component_rank in component.items():
            _int(component_rank, "fcd1_component_rank_invalid", positive=True)
            if component_rank > len(ids) or orders[name][component_rank - 1] != identifier: raise CustodyError("fcd1_component_rank_invalid")
            expected[name] = float(weights[name]) / (SixViewRanker.rrf_k + component_rank)
            if not math.isclose(_finite(contribution[name], "fcd1_contribution_invalid"), expected[name], rel_tol=0.0, abs_tol=1e-15): raise CustodyError("fcd1_contribution_invalid")
        receipts = record.get("component_rank_receipts")
        if not isinstance(receipts, list) or len(receipts) != len(weights) or {entry.get("view") for entry in receipts if isinstance(entry, Mapping)} != set(weights): raise CustodyError("fcd1_component_receipt_invalid")
        for entry in receipts:
            entry = _object(entry, "fcd1_component_receipt_invalid")
            if set(entry) != {"view", "view_order_sha256", "ranking_key_sha256", "rank"} or entry["view_order_sha256"] != order_hashes[entry["view"]] or entry["ranking_key_sha256"] != key_hash[identifier] or entry["rank"] != component[entry["view"]]: raise CustodyError("fcd1_component_receipt_invalid")
        if not math.isclose(_finite(record.get("final_rrf"), "fcd1_fusion_score_invalid"), math.fsum(expected.values()), rel_tol=0.0, abs_tol=1e-15): raise CustodyError("fcd1_fusion_score_invalid")
        fused_ids.append(identifier)
    if fused_ids != [record["source_event_id"] for record in sorted(fused, key=lambda record: (-float(record["final_rrf"]), int(record["ranking_key_order"])))] or list(ranked) != fused_ids[:len(ranked)]: raise CustodyError("fcd1_fusion_order_invalid")
    selected = trace.get("selected")
    if not isinstance(selected, list) or len(selected) != len(ids): raise CustodyError("fcd1_selected_schema_invalid")
    for selected_row, fused_row in zip(selected[:top_count], fused):
        replay = dict(fused_row)
        for key in ("rank", "ranking_key_order", "component_rank_receipts"): replay.pop(key)
        if selected_row != replay: raise CustodyError("fcd1_selected_prefix_invalid")
    rank_by_view = {
        name: {identifier: rank for rank, identifier in enumerate(order, 1)}
        for name, order in orders.items()
    }
    full_selected = []
    for identifier in ids:
        ranks = {name: rank_by_view[name][identifier] for name in weights}
        contributions = {name: float(weights[name]) / (SixViewRanker.rrf_k + ranks[name]) for name in weights}
        full_selected.append({"source_event_id": identifier, "ranking_key_sha256": key_hash[identifier], "final_rrf": math.fsum(contributions.values()), "component_ranks": ranks, "contributions": contributions})
    full_selected.sort(key=lambda record: (-record["final_rrf"], key_order[record["source_event_id"]]))
    if selected != full_selected: raise CustodyError("fcd1_selected_full_replay_invalid")
    if weights == P5_EXPERT_WEIGHTS:
        p5 = _object(trace.get("aerp5_fixed_p5"), "fcd1_p5_receipt_invalid")
        expected_config = FixedP5Policy._config(SixViewRanker.rrf_k)
        if set(p5) != {"schema", "policy", "config", "config_sha256", "effective_weights", "final_ranking_sha256"} or p5.get("schema") != "aerp5-fixed-p5-v1" or p5.get("policy") != "fixed_p5" or p5.get("config") != expected_config or p5.get("effective_weights") != dict(weights) or p5.get("config_sha256") != _digest(expected_config) or p5.get("final_ranking_sha256") != _digest([key_hash[record["source_event_id"]] for record in selected]): raise CustodyError("fcd1_p5_receipt_invalid")


def _validate_rows(rows: Any, projection: Mapping[str, Any], serializer: Mapping[str, Any], *, confidence_required: bool, trace: Any, arm_id: str | None = None, encoder_identity: str | None = None) -> None:
    if not isinstance(rows, list) or not isinstance(trace, list): raise CustodyError("ranking_rows_invalid")
    corpora = {row["corpus_id"]: row for row in projection["corpora"]}; items = {row["item_id"]: row for row in projection["items"]}; seen = set(); trace_by_item = {}
    for entry in trace:
        entry = _object(entry, "ranking_trace_row_invalid")
        expected_trace = {"item_id", "query_sha256", "candidate_input_sha256", "ranked_count", "ranking_sha256", "ranker_trace_sha256", "ranking_trace"} if confidence_required else {"item_id", "query_sha256", "candidate_input_sha256", "ranked_count", "ranking_sha256"}
        if set(entry) != expected_trace: raise CustodyError("ranking_trace_schema_invalid")
        for key in expected_trace - {"ranked_count", "ranking_trace"}: _hex(entry.get(key), "ranking_trace_schema_invalid")
        _int(entry.get("ranked_count"), "ranking_trace_schema_invalid", positive=True)
        if entry["item_id"] in trace_by_item: raise CustodyError("ranking_trace_duplicate")
        trace_by_item[entry["item_id"]] = entry
    for row in rows:
        row = _object(row, "ranking_row_invalid")
        required = {"item_id", "query_sha256", "candidate_input_sha256", "ranked_message_ids", "retrieved_conversation_ids", "confidence", "confidence_receipt"}
        if set(row) != required or row.get("item_id") not in items or row["item_id"] in seen: raise CustodyError("ranking_row_schema_invalid")
        seen.add(row["item_id"]); item = items[row["item_id"]]; corpus = corpora[item["corpus_id"]]; allowed = {candidate["message_id"]: candidate for candidate in corpus["candidates"]}
        if row["query_sha256"] != _query_digest(item["query_text"]) or row["candidate_input_sha256"] != _candidate_input(corpus, serializer): raise CustodyError("ranking_row_input_mismatch")
        ids = row["ranked_message_ids"]
        if not isinstance(ids, list) or len(ids) != min(10, len(allowed)) or len(set(ids)) != len(ids) or set(ids) - set(allowed): raise CustodyError("ranking_top10_invalid")
        conversations = list(dict.fromkeys(allowed[item_id]["opaque_conversation_id"] for item_id in ids))
        if row["retrieved_conversation_ids"] != conversations: raise CustodyError("ranking_conversations_invalid")
        if confidence_required:
            receipt = _object(row["confidence_receipt"], "ranking_confidence_receipt_invalid")
            if set(receipt) != {"contract", "top_two_scores"} or receipt["contract"] != CONFIDENCE_CONTRACT or _margin(receipt["top_two_scores"]) != _finite(row["confidence"], "ranking_confidence_invalid"):
                raise CustodyError("ranking_confidence_invalid")
        elif row["confidence"] is not None or row["confidence_receipt"] is not None: raise CustodyError("ranking_confidence_contract_invalid")
        entry = trace_by_item.get(row["item_id"])
        if entry is None or entry["query_sha256"] != row["query_sha256"] or entry["candidate_input_sha256"] != row["candidate_input_sha256"] or entry["ranked_count"] != len(ids) or entry["ranking_sha256"] != _digest(ids): raise CustodyError("ranking_trace_binding_invalid")
        if confidence_required:
            trace_value = _object(entry["ranking_trace"], "ranking_trace_schema_invalid")
            if entry["ranker_trace_sha256"] != _digest(trace_value) or trace_value.get("query_sha256") != row["query_sha256"] or trace_value.get("encoder_identity") != encoder_identity: raise CustodyError("ranking_trace_binding_invalid")
            if arm_id not in CURRENT_ARMS: raise CustodyError("ranking_trace_arm_invalid")
            candidates = authorized_candidates(projection)[item["corpus_id"]]
            _validate_fcd1(trace_value.get("fcd1_diagnostic_ledger"), trace_value, candidates, _arm_method(arm_id)["weights"], ids)
    if seen != set(items) or set(trace_by_item) != set(items): raise CustodyError("ranking_item_coverage_invalid")


def validate_frozen_ranking(value: Any, *, projection: Any) -> dict[str, Any]:
    frozen = validate_candidate_projection(projection); row = _object(value, "ranking_artifact_invalid"); arm_id = row.get("arm_id")
    common = {"schema", "arm_id", "projection_sha256", "input_receipt", "input_sha256", "model_receipt", "model_sha256", "method_receipt", "method_sha256", "source_receipt", "source_commit_sha256", "serializer_receipt", "serializer_sha256", "code_receipt", "code_sha256", "artifact_sha256"}
    if arm_id in CURRENT_ARMS: required = common | {"trace_receipt", "trace_sha256", "rankings"}
    elif arm_id == "original_public_product": required = common | {"replicates"}
    else: raise CustodyError("ranking_arm_unknown")
    if set(row) != required or row.get("schema") != RANKING_SCHEMA: raise CustodyError("ranking_artifact_schema_invalid")
    _validate_receipts(row, frozen, arm_id=arm_id); _hex(row.get("artifact_sha256"), "ranking_artifact_digest_invalid")
    if _digest({key: item for key, item in row.items() if key != "artifact_sha256"}) != row["artifact_sha256"]: raise CustodyError("ranking_artifact_digest_mismatch")
    if arm_id in CURRENT_ARMS:
        _hex(row.get("trace_sha256"), "ranking_artifact_digest_invalid")
        if _digest(row["trace_receipt"]) != row["trace_sha256"]: raise CustodyError("ranking_receipt_digest_mismatch")
        _validate_rows(row["rankings"], frozen, CURRENT_SERIALIZER, confidence_required=True, trace=row["trace_receipt"], arm_id=arm_id, encoder_identity=row["model_receipt"]["encoder_identity"])
    else:
        if not isinstance(row["replicates"], list) or len(row["replicates"]) != 5: raise CustodyError("original_replicate_contract_invalid")
        builds = set(); collections = set(); indexes = set()
        for replica in row["replicates"]:
            replica = _original_replicate(frozen, replica)
            if replica["build_id"] in builds: raise CustodyError("original_build_id_duplicate")
            collection = replica["index_receipt"]["collection_identity"]; identity = replica["index_receipt"]["index_identity_sha256"]
            if collection in collections or identity in indexes: raise CustodyError("original_index_identity_duplicate")
            builds.add(replica["build_id"]); collections.add(collection); indexes.add(identity); _validate_rows(replica["rankings"], frozen, ORIGINAL_MEMPALACE_SERIALIZER, confidence_required=False, trace=replica["trace_receipt"])
    return row
