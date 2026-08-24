"""Fail-closed scorer for frozen, label-blind AERP-7 ranking artifacts."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import hmac
import math
import random
import unicodedata
from typing import Any, Callable, Mapping, Sequence

from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256, validate_candidate_projection
from benchmarks.aerp7_convomem_rank import (
    CONFIDENCE_CONTRACT, CURRENT_ARMS, CURRENT_SERIALIZER, ORIGINAL_MEMPALACE_SERIALIZER,
    PROTOCOL_SOURCE, RANKING_SCHEMA, validate_frozen_ranking,
)

MANIFEST_SCHEMA = "aerp7-convomem-endpoint-manifest-v3"
CUSTODY_SCHEMA = "aerp7-convomem-custody-for-scoring-v2"
SCHEMA = "aerp7-convomem-scoring-report-v3"
UPSTREAM_GROUPS = {
    "user_evidence": "positive", "assistant_facts_evidence": "positive",
    "changing_evidence": "positive", "preference_evidence": "positive",
    "implicit_connection_evidence": "positive", "abstention_evidence": "abstention",
}
_FORBIDDEN_REPORT_KEYS = frozenset({"text", "speaker", "answer", "source_locator", "query_text", "evidence_spans", "evidence_conversation_ids", "message_id", "ranked_message_ids", "messages"})
_METRIC_KEYS = ("recall_at_10", "hit_at_10", "all_at_10", "ndcg_at_10", "mrr_at_10")
FORMAL_ARMS = ("original_public_product", "strong_raw", "static_p5", "six_view_secondary")
FORMAL_BOOTSTRAP = {"resamples": 10000, "percentile_lower": 0.025, "percentile_upper": 0.975, "percentile_rule": "linear", "original_replicate_rule": "global_build_multiset_per_draw", "seed_derivation": "sha256(protocol_sha256|persona-bootstrap-v1)"}


def formal_bootstrap(protocol_sha256: str) -> dict[str, Any]:
    """Derive the one preregistered formal RNG stream from the sealed protocol."""
    _h(protocol_sha256, "formal_bootstrap_protocol_digest_invalid")
    seed = int.from_bytes(hashlib.sha256((protocol_sha256 + "|persona-bootstrap-v1").encode("utf-8")).digest()[:8], "big")
    return {"seed": seed, **FORMAL_BOOTSTRAP}


def normalize_v1(value: str) -> str:
    if not isinstance(value, str): raise CustodyError("normalization_input_invalid")
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _d(value: Any) -> str: return canonical_sha256(value)


def _o(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise CustodyError(code)
    return dict(value)


def _l(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list): raise CustodyError(code)
    return value


def _h(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value): raise CustodyError(code)
    return value


def _integer(value: Any, code: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0): raise CustodyError(code)
    return value


def _number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)): raise CustodyError(code)
    return float(value)


def artifact_digest(value: Mapping[str, Any]) -> str: return _d({key: item for key, item in value.items() if key != "artifact_sha256"})
def endpoint_manifest_digest(value: Mapping[str, Any]) -> str: return _d({key: item for key, item in value.items() if key != "manifest_sha256"})
def report_digest(value: Mapping[str, Any]) -> str: return _d({key: item for key, item in value.items() if key != "report_sha256"})


def validate_endpoint_manifest(value: Any, *, projection_sha256: str) -> dict[str, Any]:
    row = _o(value, "endpoint_manifest_invalid")
    keys = {"schema", "projection_sha256", "protocol_source", "serializer_contract", "arms", "directory_endpoints", "bootstrap", "synthetic_test_mode", "reference_arm", "manifest_sha256"}
    if set(row) != keys or row.get("schema") != MANIFEST_SCHEMA or _h(row.get("projection_sha256"), "endpoint_manifest_projection_digest_invalid") != projection_sha256:
        raise CustodyError("endpoint_manifest_schema_invalid")
    if row.get("protocol_source") != PROTOCOL_SOURCE or row.get("serializer_contract") != {"current": CURRENT_SERIALIZER, "original_public_product": ORIGINAL_MEMPALACE_SERIALIZER}:
        raise CustodyError("endpoint_manifest_contract_invalid")
    arms: dict[str, dict[str, Any]] = {}
    for arm in _l(row.get("arms"), "endpoint_manifest_arms_invalid"):
        arm = _o(arm, "endpoint_manifest_arm_invalid")
        if set(arm) != {"arm_id", "ranking_artifact_sha256", "confidence_contract"} or not isinstance(arm.get("arm_id"), str) or arm["arm_id"] in arms:
            raise CustodyError("endpoint_manifest_arm_invalid")
        _h(arm.get("ranking_artifact_sha256"), "endpoint_manifest_arm_invalid")
        expected = CONFIDENCE_CONTRACT if arm["arm_id"] in CURRENT_ARMS else None if arm["arm_id"] == "original_public_product" else "__invalid__"
        if arm.get("confidence_contract") != expected: raise CustodyError("endpoint_manifest_arm_contract_invalid")
        arms[arm["arm_id"]] = arm
    if row["synthetic_test_mode"] is False and tuple(arms) != FORMAL_ARMS:
        raise CustodyError("formal_manifest_freeze_invalid")
    if not arms or "original_public_product" not in arms or row.get("reference_arm") not in arms:
        raise CustodyError("endpoint_manifest_arm_registry_invalid")
    endpoints: dict[str, str] = {}
    for endpoint in _l(row.get("directory_endpoints"), "endpoint_manifest_endpoint_invalid"):
        endpoint = _o(endpoint, "endpoint_manifest_endpoint_invalid")
        if set(endpoint) != {"directory_group", "endpoint"} or endpoint["directory_group"] in endpoints or endpoint.get("endpoint") not in {"positive", "abstention"}:
            raise CustodyError("endpoint_manifest_endpoint_invalid")
        endpoints[endpoint["directory_group"]] = endpoint["endpoint"]
    if endpoints != UPSTREAM_GROUPS or not isinstance(row.get("synthetic_test_mode"), bool): raise CustodyError("endpoint_manifest_endpoint_invalid")
    bootstrap = _o(row.get("bootstrap"), "endpoint_manifest_bootstrap_invalid")
    if set(bootstrap) not in ({"seed", "resamples", "percentile_lower", "percentile_upper", "percentile_rule", "original_replicate_rule"}, {"seed", "resamples", "percentile_lower", "percentile_upper", "percentile_rule", "original_replicate_rule", "seed_derivation"}):
        raise CustodyError("endpoint_manifest_bootstrap_invalid")
    _integer(bootstrap.get("seed"), "endpoint_manifest_bootstrap_invalid"); _integer(bootstrap.get("resamples"), "endpoint_manifest_bootstrap_invalid", positive=True)
    low, high = _number(bootstrap.get("percentile_lower"), "endpoint_manifest_bootstrap_invalid"), _number(bootstrap.get("percentile_upper"), "endpoint_manifest_bootstrap_invalid")
    if bootstrap.get("percentile_rule") != "linear" or bootstrap.get("original_replicate_rule") not in {"per_query_arithmetic_mean", "global_build_multiset_per_draw"} or not 0 <= low < high <= 1:
        raise CustodyError("endpoint_manifest_bootstrap_invalid")
    if row["synthetic_test_mode"] is False:
        if tuple(arms) != FORMAL_ARMS or row["reference_arm"] != "six_view_secondary" or set(bootstrap) != {"seed", *FORMAL_BOOTSTRAP} or {key: bootstrap[key] for key in FORMAL_BOOTSTRAP} != FORMAL_BOOTSTRAP:
            raise CustodyError("formal_manifest_freeze_invalid")
    if row.get("manifest_sha256") != endpoint_manifest_digest(row): raise CustodyError("endpoint_manifest_digest_mismatch")
    return row


def validate_ranking_artifact(value: Any, *, projection: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    artifact = validate_frozen_ranking(value, projection=projection)
    arm = next((item for item in manifest["arms"] if item["arm_id"] == artifact["arm_id"]), None)
    if arm is None or arm["ranking_artifact_sha256"] != artifact["artifact_sha256"]: raise CustodyError("ranking_arm_manifest_binding_invalid")
    expected_confidence = CONFIDENCE_CONTRACT if artifact["arm_id"] in CURRENT_ARMS else None
    if arm["confidence_contract"] != expected_confidence: raise CustodyError("ranking_confidence_manifest_invalid")
    return artifact


def _map(projection: Mapping[str, Any], custody: Any, secret: bytes, *, formal_live: bool):
    if not isinstance(secret, bytes) or len(secret) < 32: raise CustodyError("scoring_secret_too_short")
    row = _o(custody, "scoring_custody_invalid")
    if set(row) != {"schema", "projection_sha256", "items"} or row.get("schema") != CUSTODY_SCHEMA or row.get("projection_sha256") != _d(projection):
        raise CustodyError("scoring_custody_schema_invalid")
    corpora = {item["corpus_id"]: item for item in projection["corpora"]}; items = {item["item_id"]: item for item in projection["items"]}
    mappings: dict[str, list[dict[str, Any]]] = {}; public_ledger: list[dict[str, Any]] = []; groups: dict[str, str] = {}; conversations: dict[str, list[str]] = {}; seen = set()
    for item in _l(row["items"], "scoring_custody_items_invalid"):
        item = _o(item, "scoring_custody_item_invalid")
        if set(item) != {"item_id", "directory_group", "evidence_conversation_ids", "evidence_spans"} or item.get("item_id") not in items or item["item_id"] in seen or item.get("directory_group") not in UPSTREAM_GROUPS:
            raise CustodyError("scoring_custody_item_invalid")
        seen.add(item["item_id"]); group = item["directory_group"]; endpoint = UPSTREAM_GROUPS[group]
        evidence_conversations = _l(item["evidence_conversation_ids"], "scoring_custody_conversations_invalid")
        corpus = corpora[items[item["item_id"]]["corpus_id"]]; allowed_conversations = {candidate["opaque_conversation_id"] for candidate in corpus["candidates"]}
        if len(evidence_conversations) != len(set(evidence_conversations)) or set(evidence_conversations) - allowed_conversations:
            raise CustodyError("scoring_custody_conversations_invalid")
        spans = _l(item["evidence_spans"], "scoring_custody_evidence_invalid")
        if endpoint == "positive" and not spans: raise CustodyError("positive_evidence_span_missing")
        if endpoint == "abstention" and (spans or evidence_conversations): raise CustodyError("abstention_evidence_must_be_empty")
        groups[item["item_id"]] = group; conversations[item["item_id"]] = list(evidence_conversations); resolved = []
        for ordinal, span in enumerate(spans):
            span = _o(span, "scoring_custody_evidence_invalid")
            if set(span) != {"speaker", "text"} or not isinstance(span["speaker"], str) or not isinstance(span["text"], str): raise CustodyError("scoring_custody_evidence_invalid")
            hits = [candidate["message_id"] for candidate in corpus["candidates"] if candidate["opaque_conversation_id"] in evidence_conversations and (normalize_v1(candidate["speaker"]), normalize_v1(candidate["text"])) == (normalize_v1(span["speaker"]), normalize_v1(span["text"]))]
            status = "mapped" if len(hits) == 1 else "unmatched" if not hits else "ambiguous"
            private = {"status": status}
            if status == "mapped": private["message_id"] = hits[0]
            resolved.append(private)
            # Never include mapped message id in a public report.  Token is domain
            # separated and only witnesses cardinality/status for this item.
            public_ledger.append({"item_id": item["item_id"], "evidence_token": hmac.new(secret, f"aerp7-public-ledger/v1/{item['item_id']}/{ordinal}".encode("utf-8"), hashlib.sha256).hexdigest(), "status": status})
        mappings[item["item_id"]] = resolved
    if seen != set(items): raise CustodyError("scoring_custody_item_coverage_invalid")
    if formal_live and any(entry["status"] != "mapped" for entry in public_ledger):
        # Exact-evidence Recall has no defensible denominator when a frozen
        # span maps to zero or multiple candidate messages.  Abort the formal
        # run; do not silently score unresolved labels as misses.  Synthetic
        # rehearsal retains the ledger as a diagnostic, not a formal claim.
        raise CustodyError("scoring_exact_evidence_mapping_incomplete")
    return mappings, public_ledger, groups, conversations


def question_metrics(ids: Sequence[str], spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    resolved = [span["message_id"] for span in spans if span["status"] == "mapped"]
    total = len(spans); retrieved = sum(message_id in ids[:10] for message_id in resolved); unique = set(resolved)
    if not total: raise CustodyError("positive_evidence_span_missing")
    first = next((rank for rank, message_id in enumerate(ids[:10], 1) if message_id in unique), None)
    # Recall uses AERP-1's evidence-span multiplicity denominator.  NDCG does
    # not: one retrieved document occupies one rank, so duplicate gold spans
    # are binary relevance at that rank and its ideal uses unique messages.
    dcg = sum(1 / math.log2(rank + 1) for rank, message_id in enumerate(ids[:10], 1) if message_id in unique)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(unique), 10) + 1))
    return {"evidence_item_count": total, "resolved_evidence_item_count": len(resolved), "unresolved_evidence_item_count": total - len(resolved), "retrieved_evidence_count_at_10": retrieved, "recall_at_10": retrieved / total, "hit_at_10": float(retrieved > 0), "all_at_10": float(retrieved == total), "ndcg_at_10": 0.0 if ideal == 0 else dcg / ideal, "mrr_at_10": 0.0 if first is None else 1.0 / first}


def _mean(values: Sequence[float]) -> float:
    if not values: raise CustodyError("metric_denominator_zero")
    return sum(values) / len(values)


def _metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows: raise CustodyError("metric_denominator_zero")
    metrics = [row["metrics"] for row in rows]
    result = {"item_count": len(rows), **{key: _mean([float(metric[key]) for metric in metrics]) for key in _METRIC_KEYS}}
    for key in ("evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count"):
        result[key] = sum(int(metric[key]) for metric in metrics)
    result["retrieved_evidence_count_at_10"] = sum(float(metric["retrieved_evidence_count_at_10"]) for metric in metrics)
    if result["evidence_item_count"] != result["resolved_evidence_item_count"] + result["unresolved_evidence_item_count"]: raise CustodyError("metric_denominator_identity_invalid")
    # Explicit secondary endpoint: unlike question-macro Recall@10, every
    # evidence span contributes one unit to this denominator.
    result["evidence_micro_recall_at_10"] = result["retrieved_evidence_count_at_10"] / result["evidence_item_count"]
    return result


def _persona_metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows: raise CustodyError("metric_denominator_zero")
    per_persona: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows: per_persona[row["persona_id"]].append(row["metrics"])
    return {"persona_count": len(per_persona), **{key: _mean([_mean([float(metric[key]) for metric in metrics]) for metrics in per_persona.values()]) for key in _METRIC_KEYS}}


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"question_macro": _metric_summary(rows), "persona_macro": _persona_metric_summary(rows)}


def _context_summary(context: int, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows: raise CustodyError("metric_denominator_zero")
    def distribution(name: str) -> dict[str, float | int]:
        values = [int(row[name]) for row in rows]
        return {"min": min(values), "max": max(values), "mean": _mean(values)}
    return {"declared_context_size": context, "actual_conversation_count": distribution("actual_conversation_count"), "actual_message_count": distribution("actual_message_count"), **_summary(rows)}


def _auroc_ap(pairs: Sequence[tuple[float, int]]) -> tuple[float, float]:
    if not pairs or {label for _, label in pairs} != {0, 1}: raise CustodyError("confidence_stratum_class_missing")
    positives = sum(label for _, label in pairs); negatives = len(pairs) - positives; wins = 0.0
    for positive, label in pairs:
        if label != 1: continue
        for negative, other_label in pairs:
            if other_label == 0: wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    ordered = sorted(pairs, key=lambda pair: -pair[0]); hit = 0; index = 0; ap = 0.0
    while index < len(ordered):
        end = index
        while end < len(ordered) and ordered[end][0] == ordered[index][0]: end += 1
        group_positive = sum(label for _, label in ordered[index:end]); hit += group_positive
        ap += (group_positive / positives) * (hit / end)
        index = end
    return wins / (positives * negatives), ap


def _confidence(rows: Sequence[Mapping[str, Any]], *, available: bool) -> dict[str, Any]:
    if not available: return {"available": False, "reason": "arm_has_no_frozen_comparable_confidence_contract", "by_declared_context": {}}
    by_context: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for row in rows:
        confidence = _number(row["confidence"], "ranking_confidence_invalid")
        if not 0 <= confidence <= 1: raise CustodyError("ranking_confidence_invalid")
        by_context[int(row["declared_context_size"])].append((confidence, 1 if row["endpoint"] == "positive" else 0))
    detail = {}
    for context, pairs in sorted(by_context.items()):
        auroc, ap = _auroc_ap(pairs); detail[str(context)] = {"item_count": len(pairs), "positive_count": sum(label for _, label in pairs), "negative_count": len(pairs) - sum(label for _, label in pairs), "auroc": auroc, "average_precision": ap}
    return {"available": True, "reason": None, "by_declared_context": detail}


def _percentile(values: Sequence[float], point: float) -> float:
    values = sorted(values); position = (len(values) - 1) * point; lower = int(math.floor(position)); upper = int(math.ceil(position))
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _persona_macro(rows: Sequence[Mapping[str, Any]], personas: Sequence[str]) -> float:
    values = []
    for persona in personas:
        subset = [float(row["metrics"]["recall_at_10"]) for row in rows if row["persona_id"] == persona and row["endpoint"] == "positive"]
        if not subset: raise CustodyError("bootstrap_persona_positive_missing")
        values.append(_mean(subset))
    return _mean(values)


def _bootstrap(arm_rows: Mapping[str, Sequence[Mapping[str, Any]]], manifest: Mapping[str, Any], *, subset: str) -> dict[str, Any]:
    positives = {persona for rows in arm_rows.values() for persona in [row["persona_id"] for row in rows if row["endpoint"] == "positive"]}
    if not positives: raise CustodyError("positive_endpoint_missing")
    personas = sorted(positives); reference = manifest["reference_arm"]; original = "original_public_product"; rng = random.Random(manifest["bootstrap"]["seed"])
    plans = []
    for _ in range(manifest["bootstrap"]["resamples"]):
        sample = [personas[rng.randrange(len(personas))] for _ in personas]
        # Hierarchical baseline: a clustered persona draw is shared across arms;
        # five original indexes are independently resampled with replacement.
        plans.append((sample, [rng.randrange(5) for _ in range(5)]))
    def original_macro(rows: Sequence[Mapping[str, Any]], sample: Sequence[str], replicate_draw: Sequence[int]) -> float:
        values=[]
        for persona in sample:
            values.append(_mean([_mean([float(row["replicate_metrics"][replica]["recall_at_10"]) for replica in replicate_draw]) for row in rows if row["persona_id"] == persona and row["endpoint"] == "positive"]))
        return _mean(values)
    output = {}
    for challenger in sorted(arm_rows):
        if challenger == original: continue
        comparison = {}
        for name, baseline in (("vs_original_public_product", original), ("vs_reference", reference)):
            if challenger == baseline: continue
            deltas = [(_persona_macro(arm_rows[challenger], sample) if challenger != original else original_macro(arm_rows[challenger], sample, replicas)) - (_persona_macro(arm_rows[baseline], sample) if baseline != original else original_macro(arm_rows[baseline], sample, replicas)) for sample, replicas in plans]
            estimate = _persona_macro(arm_rows[challenger], personas) - _persona_macro(arm_rows[baseline], personas)
            comparison[name] = {"estimate": estimate, "ci_lower": _percentile(deltas, manifest["bootstrap"]["percentile_lower"]), "ci_upper": _percentile(deltas, manifest["bootstrap"]["percentile_upper"]), "resample_count": len(deltas)}
        if comparison: output[challenger] = comparison
    plan_digest = _d([{"persona_clusters": sample, "original_replicate_indices": replicas} for sample, replicas in plans])
    return {"subset": subset, "metric": "positive_persona_macro_recall_at_10", "reference_arm": reference, "original_replicate_rule": manifest["bootstrap"]["original_replicate_rule"], "original_replicate_count": 5, "bootstrap_plan_sha256": plan_digest, "resamples": manifest["bootstrap"]["resamples"], "seed": manifest["bootstrap"]["seed"], "percentile_rule": "linear", "paired_deltas": output}


def _confidence_nonregression(arm_rows: Mapping[str, Sequence[Mapping[str, Any]]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    raw, p5 = arm_rows["strong_raw"], arm_rows["static_p5"]
    def strata(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int], tuple[float, float]]:
        grouped: dict[tuple[str, int], list[tuple[float, int]]] = defaultdict(list)
        for row in rows: grouped[(row["persona_id"], int(row["declared_context_size"]))].append((_number(row["confidence"], "ranking_confidence_invalid"), 1 if row["endpoint"] == "positive" else 0))
        return {key: _auroc_ap(pairs) for key, pairs in grouped.items()}
    raw_values, p5_values = strata(raw), strata(p5)
    if set(raw_values) != set(p5_values) or not raw_values: raise CustodyError("confidence_pairing_invalid")
    personas = sorted({persona for persona, _context in raw_values}); rng = random.Random(manifest["bootstrap"]["seed"])
    def macro(values: Mapping[tuple[str, int], tuple[float, float]], sampled: Sequence[str], index: int) -> float:
        return _mean([_mean([pair[index] for (persona, _), pair in values.items() if persona == sample]) for sample in sampled])
    result = {}
    for index, name in enumerate(("auroc", "average_precision")):
        draws = [[personas[rng.randrange(len(personas))] for _ in personas] for _ in range(manifest["bootstrap"]["resamples"])]
        deltas = [macro(p5_values, draw, index) - macro(raw_values, draw, index) for draw in draws]
        result[name] = {"pre_registered_scalar": name, "estimate": macro(p5_values, personas, index) - macro(raw_values, personas, index), "ci_lower": _percentile(deltas, manifest["bootstrap"]["percentile_lower"]), "ci_upper": _percentile(deltas, manifest["bootstrap"]["percentile_upper"]), "resample_count": len(deltas)}
    return {"comparison": "static_p5_vs_strong_raw", "unit": "paired_persona_by_declared_context", "metrics": result}


def _artifact_rows(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    if artifact["arm_id"] != "original_public_product": return [dict(row) for row in artifact["rankings"]]
    by_item: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for replicate in artifact["replicates"]:
        for row in replicate["rankings"]: by_item[row["item_id"]].append(row)
    rows = []
    for item_id, replicas in by_item.items():
        # The primary value is defined by the manifest's fixed per-query mean.
        rows.append({**dict(replicas[0]), "replicate_ranked_message_ids": [list(row["ranked_message_ids"]) for row in replicas], "replicate_retrieved_conversation_ids": [list(row["retrieved_conversation_ids"]) for row in replicas]})
    return rows


def score_frozen(*, projection: Any, endpoint_manifest: Any, ranking_artifacts: Sequence[Any], custody_loader: Callable[[], Any], evidence_token_secret: bytes, formal_live: bool | None = None) -> dict[str, Any]:
    projection = validate_candidate_projection(projection); manifest = validate_endpoint_manifest(endpoint_manifest, projection_sha256=_d(projection))
    # This complete public validation is intentionally before the first custody call.
    artifacts = [validate_ranking_artifact(artifact, projection=projection, manifest=manifest) for artifact in ranking_artifacts]
    if len(artifacts) != len(manifest["arms"]) or {artifact["arm_id"] for artifact in artifacts} != {arm["arm_id"] for arm in manifest["arms"]}: raise CustodyError("ranking_arm_set_invalid")
    # Execution mode is supplied by the custody boundary when available.  The
    # endpoint manifest is a public scientific artifact, not authorization to
    # reinterpret a rehearsal process as a live formal run.
    if formal_live is None:
        formal_live = manifest["synthetic_test_mode"] is False
    if not isinstance(formal_live, bool):
        raise CustodyError("scoring_execution_mode_invalid")
    mappings, ledger, groups, evidence_conversations = _map(
        projection, custody_loader(), evidence_token_secret,
        formal_live=formal_live,
    )
    corpora = {row["corpus_id"]: row for row in projection["corpora"]}; items = {row["item_id"]: row for row in projection["items"]}; arms = {}; rows_by_arm = {}
    for artifact in artifacts:
        arm_id = artifact["arm_id"]; ranked = {row["item_id"]: row for row in _artifact_rows(artifact)}; rows = []
        for item_id, item in items.items():
            source = ranked[item_id]; corpus = corpora[item["corpus_id"]]; group = groups[item_id]; endpoint = UPSTREAM_GROUPS[group]
            row = {"item_id": item_id, "persona_id": item["persona_id"], "declared_context_size": corpus["declared_context_size"], "actual_conversation_count": corpus["actual_conversation_count"], "actual_message_count": corpus["actual_message_count"], "directory_group": group, "endpoint": endpoint, "confidence": source["confidence"], "evidence_conversation_hit": bool(set(source["retrieved_conversation_ids"]) & set(evidence_conversations[item_id]))}
            if endpoint == "positive":
                if arm_id == "original_public_product":
                    replicas = source["replicate_ranked_message_ids"]; metric_rows = [question_metrics(ids, mappings[item_id]) for ids in replicas]
                    row["metrics"] = {key: _mean([float(metrics[key]) for metrics in metric_rows]) for key in _METRIC_KEYS}
                    for key in ("evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count"):
                        row["metrics"][key] = metric_rows[0][key]
                    row["metrics"]["retrieved_evidence_count_at_10"] = _mean([float(metrics["retrieved_evidence_count_at_10"]) for metrics in metric_rows])
                    row["replicate_metrics"] = metric_rows
                    row["replicate_evidence_conversation_hits"] = [bool(set(conversations) & set(evidence_conversations[item_id])) for conversations in source["replicate_retrieved_conversation_ids"]]
                else: row["metrics"] = question_metrics(source["ranked_message_ids"], mappings[item_id])
            rows.append(row)
        positives = [row for row in rows if row["endpoint"] == "positive"]
        if not positives: raise CustodyError("positive_endpoint_missing")
        exact = {group: _summary([row for row in positives if row["directory_group"] == group]) for group in UPSTREAM_GROUPS if UPSTREAM_GROUPS[group] == "positive"}
        contexts = {str(context): _context_summary(context, [row for row in positives if row["declared_context_size"] == context]) for context in sorted({row["declared_context_size"] for row in positives})}
        derived_rows = [row for row in positives if row["directory_group"] in {"changing_evidence", "implicit_connection_evidence"}]
        arm_contract = next(item["confidence_contract"] for item in manifest["arms"] if item["arm_id"] == arm_id)
        diagnostic_rows = positives
        diagnostic_value = {"not_official_primary": True, "positive_item_count": len(diagnostic_rows), "retrieved_relevant_conversation_count": sum(row["evidence_conversation_hit"] for row in diagnostic_rows), "total_relevant_conversation_item_count": len(diagnostic_rows), "recall": _mean([float(row["evidence_conversation_hit"]) for row in diagnostic_rows])}
        arm_result = {"positive": {"overall": _summary(positives), "by_exact_group": exact, "by_declared_context": contexts, "derived_hard_changing_and_implicit": {"derived": True, **_summary(derived_rows)}}, "confidence_separability": _confidence(rows, available=arm_contract == CONFIDENCE_CONTRACT), "official_style_evidence_conversation_diagnostic": diagnostic_value}
        if arm_id == "original_public_product":
            replicate_stats = []
            for number in range(5):
                replica_rows = [{**row, "metrics": row["replicate_metrics"][number], "evidence_conversation_hit": row["replicate_evidence_conversation_hits"][number]} for row in positives]
                replica_hard = [row for row in replica_rows if row["directory_group"] in {"changing_evidence", "implicit_connection_evidence"}]
                receipt = artifact["replicates"][number]
                replicate_stats.append({"replicate_index": number, "build_id": receipt["build_id"], "index_sha256": receipt["index_sha256"], "overall_positive": _summary(replica_rows), "by_exact_group": {group: _summary([row for row in replica_rows if row["directory_group"] == group]) for group in UPSTREAM_GROUPS if UPSTREAM_GROUPS[group] == "positive"}, "derived_hard_changing_and_implicit": _summary(replica_hard), "official_style_evidence_conversation_diagnostic": {"positive_item_count": len(replica_rows), "retrieved_relevant_conversation_count": sum(row["evidence_conversation_hit"] for row in replica_rows), "total_relevant_conversation_item_count": len(replica_rows), "recall": _mean([float(row["evidence_conversation_hit"]) for row in replica_rows])}})
            # The public aggregate is over all 5 frozen outputs, never replica 0.
            all_replica_rows = [{**row, "evidence_conversation_hit": row["replicate_evidence_conversation_hits"][number]} for number in range(5) for row in positives]
            arm_result["official_style_evidence_conversation_diagnostic"] = {"not_official_primary": True, "positive_item_count": len(all_replica_rows), "retrieved_relevant_conversation_count": sum(row["evidence_conversation_hit"] for row in all_replica_rows), "total_relevant_conversation_item_count": len(all_replica_rows), "recall": _mean([float(row["evidence_conversation_hit"]) for row in all_replica_rows])}
            arm_result["original_replicates"] = replicate_stats
        arms[arm_id] = arm_result
        rows_by_arm[arm_id] = rows
    hard_rows = {arm: [row for row in rows if row["endpoint"] == "positive" and row["directory_group"] in {"changing_evidence", "implicit_connection_evidence"}] for arm, rows in rows_by_arm.items()}
    report = {"schema": SCHEMA, "projection_sha256": _d(projection), "endpoint_manifest_sha256": manifest["manifest_sha256"], "endpoint_manifest": manifest, "protocol": {"synthetic_test_mode": manifest["synthetic_test_mode"], "arm_registry": [arm["arm_id"] for arm in manifest["arms"]], "reference_arm": manifest["reference_arm"], "bootstrap": manifest["bootstrap"]}, "ranking_artifact_sha256": {artifact["arm_id"]: artifact["artifact_sha256"] for artifact in artifacts}, "mapping_ledger": ledger, "arms": arms, "paired_bootstrap": {"overall_positive": _bootstrap(rows_by_arm, manifest, subset="overall_positive"), "derived_hard_changing_and_implicit": _bootstrap(hard_rows, manifest, subset="derived_hard_changing_and_implicit"), "static_p5_vs_strong_raw_abstention_confidence": _confidence_nonregression(rows_by_arm, manifest)}}
    report["report_sha256"] = report_digest(report); return validate_report(report)


def validate_report(value: Any) -> dict[str, Any]:
    row = _o(value, "scoring_report_invalid")
    keys = {"schema", "projection_sha256", "endpoint_manifest_sha256", "endpoint_manifest", "protocol", "ranking_artifact_sha256", "mapping_ledger", "arms", "paired_bootstrap", "report_sha256"}
    if set(row) != keys or row.get("schema") != SCHEMA: raise CustodyError("scoring_report_schema_invalid")
    for key in ("projection_sha256", "endpoint_manifest_sha256", "report_sha256"): _h(row.get(key), "scoring_report_digest_invalid")
    if row["report_sha256"] != report_digest(row): raise CustodyError("scoring_report_digest_mismatch")
    def scan(value: Any) -> None:
        if isinstance(value, Mapping):
            if _FORBIDDEN_REPORT_KEYS & set(value): raise CustodyError("scoring_report_leakage")
            for child in value.values(): scan(child)
        elif isinstance(value, list):
            for child in value: scan(child)
        elif isinstance(value, float) and not math.isfinite(value): raise CustodyError("scoring_report_nonfinite")
    scan(row)
    manifest = validate_endpoint_manifest(row["endpoint_manifest"], projection_sha256=row["projection_sha256"])
    if manifest["manifest_sha256"] != row["endpoint_manifest_sha256"]: raise CustodyError("scoring_report_protocol_invalid")
    manifest_artifacts = {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in manifest["arms"]}
    if row.get("ranking_artifact_sha256") != manifest_artifacts: raise CustodyError("scoring_report_artifact_manifest_binding_invalid")
    protocol = _o(row["protocol"], "scoring_report_protocol_invalid")
    if set(protocol) != {"synthetic_test_mode", "arm_registry", "reference_arm", "bootstrap"} or not isinstance(protocol["synthetic_test_mode"], bool) or not isinstance(protocol["arm_registry"], list) or len(protocol["arm_registry"]) != len(set(protocol["arm_registry"])) or protocol["reference_arm"] not in protocol["arm_registry"]:
        raise CustodyError("scoring_report_protocol_invalid")
    if protocol["synthetic_test_mode"] is False and (tuple(protocol["arm_registry"]) != FORMAL_ARMS or protocol["reference_arm"] != "six_view_secondary" or set(protocol["bootstrap"]) != {"seed", *FORMAL_BOOTSTRAP} or {key: protocol["bootstrap"][key] for key in FORMAL_BOOTSTRAP} != FORMAL_BOOTSTRAP): raise CustodyError("scoring_report_protocol_invalid")
    if protocol != {"synthetic_test_mode": manifest["synthetic_test_mode"], "arm_registry": [arm["arm_id"] for arm in manifest["arms"]], "reference_arm": manifest["reference_arm"], "bootstrap": manifest["bootstrap"]}: raise CustodyError("scoring_report_protocol_invalid")
    if not isinstance(row["ranking_artifact_sha256"], Mapping) or not row["ranking_artifact_sha256"] or not isinstance(row["arms"], Mapping) or set(row["ranking_artifact_sha256"]) != set(row["arms"]) or set(protocol["arm_registry"]) != set(row["arms"]): raise CustodyError("scoring_report_arm_schema_invalid")
    for digest in row["ranking_artifact_sha256"].values(): _h(digest, "scoring_report_digest_invalid")
    for entry in _l(row["mapping_ledger"], "scoring_report_ledger_invalid"):
        entry = _o(entry, "scoring_report_ledger_invalid")
        if set(entry) != {"item_id", "evidence_token", "status"} or entry["status"] not in {"mapped", "unmatched", "ambiguous"}: raise CustodyError("scoring_report_ledger_invalid")
        _h(entry["item_id"], "scoring_report_ledger_invalid"); _h(entry["evidence_token"], "scoring_report_ledger_invalid")
    if len({(entry["item_id"], entry["evidence_token"]) for entry in row["mapping_ledger"]}) != len(row["mapping_ledger"]): raise CustodyError("scoring_report_ledger_duplicate")
    metric_keys = {"item_count", "evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count", "retrieved_evidence_count_at_10", "evidence_micro_recall_at_10", *_METRIC_KEYS}
    def metric_summary(summary: Any) -> None:
        summary = _o(summary, "scoring_report_metric_schema_invalid")
        if set(summary) != metric_keys: raise CustodyError("scoring_report_metric_schema_invalid")
        for key in ("item_count", "evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count"):
            _integer(summary.get(key), "scoring_report_metric_schema_invalid")
            if summary[key] < 0: raise CustodyError("scoring_report_metric_schema_invalid")
        if _number(summary.get("retrieved_evidence_count_at_10"), "scoring_report_metric_schema_invalid") < 0:
            raise CustodyError("scoring_report_metric_schema_invalid")
        if summary["evidence_item_count"] != summary["resolved_evidence_item_count"] + summary["unresolved_evidence_item_count"] or summary["retrieved_evidence_count_at_10"] > summary["evidence_item_count"]:
            raise CustodyError("scoring_report_metric_denominator_invalid")
        for key in _METRIC_KEYS:
            number = _number(summary.get(key), "scoring_report_metric_schema_invalid")
            if not 0 <= number <= 1: raise CustodyError("scoring_report_metric_range_invalid")
        if summary["evidence_item_count"] <= 0 or not 0 <= _number(summary.get("evidence_micro_recall_at_10"), "scoring_report_metric_schema_invalid") <= 1:
            raise CustodyError("scoring_report_metric_range_invalid")
        if not math.isclose(float(summary["evidence_micro_recall_at_10"]), float(summary["retrieved_evidence_count_at_10"]) / int(summary["evidence_item_count"]), rel_tol=0.0, abs_tol=1e-12):
            raise CustodyError("scoring_report_metric_denominator_invalid")
    def persona_summary(summary: Any) -> None:
        summary = _o(summary, "scoring_report_metric_schema_invalid")
        if set(summary) != {"persona_count", *_METRIC_KEYS}: raise CustodyError("scoring_report_metric_schema_invalid")
        _integer(summary.get("persona_count"), "scoring_report_metric_schema_invalid", positive=True)
        for key in _METRIC_KEYS:
            if not 0 <= _number(summary.get(key), "scoring_report_metric_schema_invalid") <= 1: raise CustodyError("scoring_report_metric_range_invalid")
    def split_summary(summary: Any) -> None:
        summary = _o(summary, "scoring_report_metric_schema_invalid")
        if set(summary) != {"question_macro", "persona_macro"}: raise CustodyError("scoring_report_metric_schema_invalid")
        metric_summary(summary["question_macro"]); persona_summary(summary["persona_macro"])
    def diagnostic(value: Any, *, public: bool) -> None:
        value = _o(value, "scoring_report_diagnostic_schema_invalid")
        expected = {"positive_item_count", "retrieved_relevant_conversation_count", "total_relevant_conversation_item_count", "recall"} | ({"not_official_primary"} if public else set())
        if set(value) != expected or public and value.get("not_official_primary") is not True: raise CustodyError("scoring_report_diagnostic_schema_invalid")
        for key in ("positive_item_count", "retrieved_relevant_conversation_count", "total_relevant_conversation_item_count"): _integer(value.get(key), "scoring_report_diagnostic_schema_invalid")
        if value["positive_item_count"] != value["total_relevant_conversation_item_count"] or value["retrieved_relevant_conversation_count"] > value["total_relevant_conversation_item_count"] or not 0 <= _number(value.get("recall"), "scoring_report_diagnostic_schema_invalid") <= 1: raise CustodyError("scoring_report_diagnostic_schema_invalid")
    for arm_id, arm in row["arms"].items():
        if arm_id not in CURRENT_ARMS | {"original_public_product"}: raise CustodyError("scoring_report_arm_schema_invalid")
        arm = _o(arm, "scoring_report_arm_schema_invalid")
        expected_arm = {"positive", "confidence_separability", "official_style_evidence_conversation_diagnostic"} | ({"original_replicates"} if arm_id == "original_public_product" else set())
        if set(arm) != expected_arm: raise CustodyError("scoring_report_arm_schema_invalid")
        positive = _o(arm["positive"], "scoring_report_positive_schema_invalid")
        if set(positive) != {"overall", "by_exact_group", "by_declared_context", "derived_hard_changing_and_implicit"}: raise CustodyError("scoring_report_positive_schema_invalid")
        split_summary(positive["overall"])
        exact = _o(positive["by_exact_group"], "scoring_report_positive_schema_invalid")
        if set(exact) != {group for group, endpoint in UPSTREAM_GROUPS.items() if endpoint == "positive"}: raise CustodyError("scoring_report_positive_schema_invalid")
        for summary in exact.values(): split_summary(summary)
        contexts = _o(positive["by_declared_context"], "scoring_report_positive_schema_invalid")
        if not contexts or any(not isinstance(context, str) or not context.isdigit() or int(context) <= 0 for context in contexts): raise CustodyError("scoring_report_positive_schema_invalid")
        for context, summary in contexts.items():
            summary = _o(summary, "scoring_report_positive_schema_invalid")
            if set(summary) != {"declared_context_size", "actual_conversation_count", "actual_message_count", "question_macro", "persona_macro"} or summary["declared_context_size"] != int(context): raise CustodyError("scoring_report_positive_schema_invalid")
            for name in ("actual_conversation_count", "actual_message_count"):
                distribution = _o(summary[name], "scoring_report_positive_schema_invalid")
                if set(distribution) != {"min", "max", "mean"}: raise CustodyError("scoring_report_positive_schema_invalid")
                minimum, maximum, mean = _integer(distribution["min"], "scoring_report_positive_schema_invalid", positive=True), _integer(distribution["max"], "scoring_report_positive_schema_invalid", positive=True), _number(distribution["mean"], "scoring_report_positive_schema_invalid")
                if minimum > maximum or not minimum <= mean <= maximum: raise CustodyError("scoring_report_positive_schema_invalid")
            split_summary({"question_macro": summary["question_macro"], "persona_macro": summary["persona_macro"]})
        derived = _o(positive["derived_hard_changing_and_implicit"], "scoring_report_positive_schema_invalid")
        if set(derived) != {"derived", "question_macro", "persona_macro"} or derived.get("derived") is not True: raise CustodyError("scoring_report_positive_schema_invalid")
        split_summary({"question_macro": derived["question_macro"], "persona_macro": derived["persona_macro"]})
        confidence = _o(arm["confidence_separability"], "scoring_report_confidence_schema_invalid")
        if set(confidence) != {"available", "reason", "by_declared_context"} or not isinstance(confidence["available"], bool): raise CustodyError("scoring_report_confidence_schema_invalid")
        if confidence["available"] != (arm_id in CURRENT_ARMS) or (confidence["available"] and confidence["reason"] is not None) or (not confidence["available"] and confidence["reason"] != "arm_has_no_frozen_comparable_confidence_contract"):
            raise CustodyError("scoring_report_confidence_schema_invalid")
        conf_contexts = _o(confidence["by_declared_context"], "scoring_report_confidence_schema_invalid")
        if confidence["available"] and not conf_contexts or not confidence["available"] and conf_contexts: raise CustodyError("scoring_report_confidence_schema_invalid")
        for context, stats in conf_contexts.items():
            if not isinstance(context, str) or not context.isdigit() or int(context) <= 0: raise CustodyError("scoring_report_confidence_schema_invalid")
            stats = _o(stats, "scoring_report_confidence_schema_invalid")
            if set(stats) != {"item_count", "positive_count", "negative_count", "auroc", "average_precision"}: raise CustodyError("scoring_report_confidence_schema_invalid")
            for key in ("item_count", "positive_count", "negative_count"): _integer(stats.get(key), "scoring_report_confidence_schema_invalid", positive=True)
            if stats["item_count"] != stats["positive_count"] + stats["negative_count"]: raise CustodyError("scoring_report_confidence_schema_invalid")
            for key in ("auroc", "average_precision"):
                if not 0 <= _number(stats.get(key), "scoring_report_confidence_schema_invalid") <= 1: raise CustodyError("scoring_report_confidence_schema_invalid")
        diagnostic(arm["official_style_evidence_conversation_diagnostic"], public=True)
        if arm_id == "original_public_product":
            replicas = _l(arm["original_replicates"], "scoring_report_replicate_schema_invalid")
            if len(replicas) != 5: raise CustodyError("scoring_report_replicate_schema_invalid")
            replica_diagnostics = []
            for number, replica in enumerate(replicas):
                replica = _o(replica, "scoring_report_replicate_schema_invalid")
                if set(replica) != {"replicate_index", "build_id", "index_sha256", "overall_positive", "by_exact_group", "derived_hard_changing_and_implicit", "official_style_evidence_conversation_diagnostic"} or replica["replicate_index"] != number or not isinstance(replica["build_id"], str) or not replica["build_id"]: raise CustodyError("scoring_report_replicate_schema_invalid")
                _h(replica["index_sha256"], "scoring_report_replicate_schema_invalid")
                split_summary(replica["overall_positive"]); split_summary(replica["derived_hard_changing_and_implicit"]); diagnostic(replica["official_style_evidence_conversation_diagnostic"], public=False)
                replica_diagnostics.append(replica["official_style_evidence_conversation_diagnostic"])
                exact_replica = _o(replica["by_exact_group"], "scoring_report_replicate_schema_invalid")
                if set(exact_replica) != set(exact): raise CustodyError("scoring_report_replicate_schema_invalid")
                for summary in exact_replica.values(): split_summary(summary)
            aggregate = _o(arm["official_style_evidence_conversation_diagnostic"], "scoring_report_replicate_schema_invalid")
            expected_total = sum(item["total_relevant_conversation_item_count"] for item in replica_diagnostics); expected_hit = sum(item["retrieved_relevant_conversation_count"] for item in replica_diagnostics)
            if aggregate["total_relevant_conversation_item_count"] != expected_total or aggregate["retrieved_relevant_conversation_count"] != expected_hit or aggregate["positive_item_count"] != expected_total or not math.isclose(float(aggregate["recall"]), expected_hit / expected_total, rel_tol=0.0, abs_tol=1e-15): raise CustodyError("scoring_report_replicate_aggregate_invalid")
    bootstrap = _o(row["paired_bootstrap"], "scoring_report_bootstrap_schema_invalid")
    if set(bootstrap) != {"overall_positive", "derived_hard_changing_and_implicit", "static_p5_vs_strong_raw_abstention_confidence"}: raise CustodyError("scoring_report_bootstrap_schema_invalid")
    def retrieval_bootstrap(section: Any, expected_subset: str) -> None:
        section = _o(section, "scoring_report_bootstrap_schema_invalid")
        expected = {"subset", "metric", "reference_arm", "original_replicate_rule", "original_replicate_count", "bootstrap_plan_sha256", "resamples", "seed", "percentile_rule", "paired_deltas"}
        if set(section) != expected or section.get("subset") != expected_subset or section.get("metric") != "positive_persona_macro_recall_at_10" or section.get("reference_arm") not in row["arms"] or section.get("original_replicate_rule") not in {"per_query_arithmetic_mean", "global_build_multiset_per_draw"} or section.get("original_replicate_rule") != protocol["bootstrap"].get("original_replicate_rule") or section.get("original_replicate_count") != 5 or section.get("percentile_rule") != "linear": raise CustodyError("scoring_report_bootstrap_schema_invalid")
        _h(section.get("bootstrap_plan_sha256"), "scoring_report_bootstrap_schema_invalid")
        _integer(section.get("resamples"), "scoring_report_bootstrap_schema_invalid", positive=True); _integer(section.get("seed"), "scoring_report_bootstrap_schema_invalid")
        if section["resamples"] != protocol["bootstrap"].get("resamples") or section["seed"] != protocol["bootstrap"].get("seed"): raise CustodyError("scoring_report_bootstrap_schema_invalid")
        paired = _o(section["paired_deltas"], "scoring_report_bootstrap_schema_invalid")
        challengers = set(row["arms"]) - {"original_public_product"}
        if set(paired) != challengers: raise CustodyError("scoring_report_bootstrap_schema_invalid")
        for challenger, comparisons in paired.items():
            comparisons = _o(comparisons, "scoring_report_bootstrap_schema_invalid")
            expected_comparisons = {"vs_original_public_product"} | ({"vs_reference"} if challenger != section["reference_arm"] else set())
            if set(comparisons) != expected_comparisons: raise CustodyError("scoring_report_bootstrap_schema_invalid")
            for comparison in comparisons.values():
                comparison = _o(comparison, "scoring_report_bootstrap_schema_invalid")
                if set(comparison) != {"estimate", "ci_lower", "ci_upper", "resample_count"}: raise CustodyError("scoring_report_bootstrap_schema_invalid")
                _estimate, low, high = (_number(comparison[key], "scoring_report_bootstrap_schema_invalid") for key in ("estimate", "ci_lower", "ci_upper"))
                if low > high or comparison["resample_count"] != section["resamples"]: raise CustodyError("scoring_report_bootstrap_schema_invalid")
                _integer(comparison["resample_count"], "scoring_report_bootstrap_schema_invalid", positive=True)
    retrieval_bootstrap(bootstrap["overall_positive"], "overall_positive")
    retrieval_bootstrap(bootstrap["derived_hard_changing_and_implicit"], "derived_hard_changing_and_implicit")
    confidence_bootstrap = _o(bootstrap["static_p5_vs_strong_raw_abstention_confidence"], "scoring_report_bootstrap_schema_invalid")
    if set(confidence_bootstrap) != {"comparison", "unit", "metrics"} or confidence_bootstrap.get("comparison") != "static_p5_vs_strong_raw" or confidence_bootstrap.get("unit") != "paired_persona_by_declared_context": raise CustodyError("scoring_report_bootstrap_schema_invalid")
    confidence_metrics = _o(confidence_bootstrap["metrics"], "scoring_report_bootstrap_schema_invalid")
    if set(confidence_metrics) != {"auroc", "average_precision"}: raise CustodyError("scoring_report_bootstrap_schema_invalid")
    for name, comparison in confidence_metrics.items():
        comparison = _o(comparison, "scoring_report_bootstrap_schema_invalid")
        if set(comparison) != {"pre_registered_scalar", "estimate", "ci_lower", "ci_upper", "resample_count"} or comparison["pre_registered_scalar"] != name: raise CustodyError("scoring_report_bootstrap_schema_invalid")
        _estimate, low, high = (_number(comparison[key], "scoring_report_bootstrap_schema_invalid") for key in ("estimate", "ci_lower", "ci_upper"))
        if low > high or comparison["resample_count"] != protocol["bootstrap"].get("resamples"): raise CustodyError("scoring_report_bootstrap_schema_invalid")
        _integer(comparison["resample_count"], "scoring_report_bootstrap_schema_invalid", positive=True)
    return row
