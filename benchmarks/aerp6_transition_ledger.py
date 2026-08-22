"""Read-only AERP-6 transition ledger over the frozen staged LoCoMo artifact.

This analyzer deliberately does not modify product retrieval.  It validates the
pre-freeze ranking inputs and all full-order replays before it reads the
official-exact evidence fields; the one guard arm is consequently a burned-data
engineering diagnostic, never a ranking-time label policy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import tempfile
from pathlib import Path
from typing import Any

from benchmarks import aerp3_fcd2_causal_ablation as fcd2


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aerp6-transition-ledger-v1"
EXPECTED_QUESTIONS = 1986
TOP_K = 10
ADD_VIEW_ORDER = fcd2.ADD_VIEW_ORDER
FULL_VIEW_ORDER = fcd2.PRODUCT_FUSION_VIEW_ORDER
RAW_FULL_ORDER_VIEWS = ADD_VIEW_ORDER[:2]
NO_CHECKPOINT_VIEWS = tuple(view for view in FULL_VIEW_ORDER if view != "checkpoint_dense")
JOINT_REMOVAL_VIEWS = tuple(view for view in FULL_VIEW_ORDER if view not in {"observation_dense", "checkpoint_dense"})
MAIN_RAW_ARM = "raw_bm25_plus_raw_dense"
EXPECTED_REPLAY = {
    "main_raw_comparator": {"overall": 0.6243429043748013, "hard": 0.5251058257735399, "adversarial": 0.6917040358744395},
    "raw_full_order_prefix": {"overall": 0.6350382168085539, "hard": 0.5264423968862304, "adversarial": 0.7062780269058296},
    "final": {"overall": 0.6470064018957622, "hard": 0.6092153974060232, "adversarial": 0.5795964125560538},
    "no_checkpoint": {"overall": 0.6574714890309499, "hard": 0.6122990620175748, "adversarial": 0.5885650224215246},
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _unchanged_stream(path: Path, expected_sha256: str, expected_bytes: int) -> None:
    """Constant-memory publication recheck for the large frozen inputs."""
    if not isinstance(expected_bytes, int) or expected_bytes < 0:
        raise ValueError("expected input byte count is invalid")
    digest = hashlib.sha256(); count = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block); count += len(block)
    if count != expected_bytes or digest.hexdigest() != expected_sha256:
        raise RuntimeError("frozen input changed during analysis")


def _ranking_key_orders(ledger: dict[str, Any]) -> dict[str, int]:
    """Reconstruct only the frozen, receipt-bearing lexical tie order."""
    universe = _event_hashes(ledger)
    orders: dict[str, int] = {}
    for rows in [*ledger["view_top_50"].values(), ledger["fused_top_50"]]:
        for row in rows:
            event, order = row["source_event_id"], row["ranking_key_order"]
            if event not in universe or not isinstance(order, int) or isinstance(order, bool) or not 1 <= order <= len(universe):
                raise ValueError("ranking-key order receipt is invalid")
            if event in orders and orders[event] != order:
                raise ValueError("ranking-key order receipt conflicts")
            orders[event] = order
    if len(set(orders.values())) != len(orders):
        raise ValueError("ranking-key order receipt is not injective")
    return orders


def _top_order(replay: dict[str, Any], count: int, orders: dict[str, int]) -> list[str]:
    if not fcd2._strict_cutoff(replay, count):
        raise ValueError(f"Top-{count} membership is unprovable")
    scores = replay["scores"]; threshold = sorted(scores.values(), reverse=True)[count - 1]
    members = [event for event, score in scores.items() if score >= threshold]
    if len(members) != count or any(event not in orders for event in members):
        raise ValueError(f"Top-{count} selected event lacks ranking-key order receipt")
    return sorted(members, key=lambda event: (-scores[event], orders[event]))


def _score_ranks(replay: dict[str, Any]) -> dict[str, int]:
    """Competition ranks (1 + strictly-higher scores), never fabricated ties."""
    scores = replay["scores"]
    ranks: dict[float, int] = {}
    previous: float | None = None
    for position, score in enumerate(sorted(scores.values(), reverse=True), start=1):
        if previous is None or score != previous:
            ranks[score] = position
            previous = score
    return {event: ranks[score] for event, score in scores.items()}


def _boundary(replay: dict[str, Any]) -> dict[str, float]:
    if not fcd2._strict_cutoff(replay, TOP_K):
        raise ValueError("Top-10 boundary is not strict")
    ordered = sorted(replay["scores"].values(), reverse=True)
    tenth, eleventh = ordered[TOP_K - 1], ordered[TOP_K]
    return {"rank_10_score": tenth, "rank_11_score": eleventh, "margin": tenth - eleventh}


def _event_hashes(ledger: dict[str, Any]) -> dict[str, str]:
    result = {
        member["source_event_id"]: member["ranking_key_sha256"]
        for group in ledger["checkpoint_tie_groups"]
        for member in group["chronological_members"]
    }
    if not result or len(result) != len(set(result.values())):
        raise ValueError("checkpoint event-to-ranking-key mapping is not one-to-one")
    return result


def _overlap(replays: dict[str, dict[str, Any]]) -> dict[str, Any]:
    raw = replays["raw_full_order_prefix"]
    if not fcd2._strict_cutoff(raw, 50):
        raise ValueError("raw Top-50 membership is unprovable")
    threshold = sorted(raw["scores"].values(), reverse=True)[49]
    raw_top50 = {event for event, score in raw["scores"].items() if score >= threshold}
    if len(raw_top50) != 50:
        raise ValueError("raw Top-50 membership is not unique")
    result: dict[str, dict[str, float | int]] = {}
    for view in fcd2.VIEWS:
        view_top50 = set(replays["ledger"]["view_full_order"][view][:50])
        shared = len(raw_top50 & view_top50)
        result[view] = {"shared_top50": shared, "raw_top50_fraction": shared / 50.0, "jaccard": shared / len(raw_top50 | view_top50)}
    return {"raw_membership_semantics": "strict_cutoff_score_membership_only_no_internal_order_claim", "view_membership_semantics": "receipt_bound_producer_top50_prefix_membership", "views": result}


def _derive_evidence_ranks(state: dict[str, Any], event: str | None, stage_rank_maps: dict[str, dict[str, int]], rank_maps: dict[str, dict[str, int]]) -> dict[str, Any]:
    """Derive producer ordinal and score competition ranks from their sources."""
    ordinal = {view: (state["ledger"]["view_full_order"][view].index(event) + 1 if event is not None else None) for view in fcd2.VIEWS}
    return {"per_view_producer_ordinal_rank": ordinal, "fused_competition_ranks": {"raw_full_order_prefix": rank_maps["raw_full_order_prefix"].get(event) if event is not None else None, "add_view_stages": {view: stage_rank_maps[view].get(event) if event is not None else None for view in ADD_VIEW_ORDER}, "final": rank_maps["final"].get(event) if event is not None else None, "no_checkpoint": rank_maps["no_checkpoint"].get(event) if event is not None else None, "joint_removal_observation_dense_plus_checkpoint_dense": rank_maps["joint_removal"].get(event) if event is not None else None}}


def _aerp4_anchor_inputs(raw_replay: dict[str, Any], p5_replay: dict[str, Any], event_hashes: dict[str, str], orders: dict[str, int]) -> dict[str, Any]:
    """Rank-only inputs to the already-authorized AERP-4 RawAnchoredP5 seam."""
    raw_order, p5_order = _top_order(raw_replay, TOP_K, orders), _top_order(p5_replay, TOP_K, orders)
    raw_scores = raw_replay["scores"]
    numerator = sum(raw_scores[event] for event in p5_order[:TOP_K])
    denominator = sum(raw_scores[event] for event in raw_order[:TOP_K])
    if denominator <= 0.0:
        raise ValueError("AERP-4 raw anchor denominator is invalid")
    return {
        "policy": "raw_anchored_p5",
        "raw_top10_ranking_sha256": fcd2._digest([event_hashes[event] for event in raw_order[:TOP_K]]),
        "p5_top10_ranking_sha256": fcd2._digest([event_hashes[event] for event in p5_order[:TOP_K]]),
        "anchor_numerator": numerator,
        "anchor_denominator": denominator,
        "anchor_ratio": numerator / denominator,
    }


def _transition_class(raw_rank: int | None, final_rank: int | None) -> str:
    raw_hit, final_hit = raw_rank is not None and raw_rank <= TOP_K, final_rank is not None and final_rank <= TOP_K
    if raw_hit and not final_hit:
        return "raw-hit→full-miss"
    if not raw_hit and final_hit:
        return "raw-miss→full-hit"
    return "hit→hit" if raw_hit else "miss→miss"


def _safe_report(value: Any) -> None:
    forbidden = {"query", "transcript", "answer", "text", "source_event_id", "ranking_key", "item_id"}
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in forbidden:
                raise ValueError("report contains forbidden plaintext field")
            _safe_report(child)
    elif isinstance(value, list):
        for child in value:
            _safe_report(child)


def _protocols(artifact: dict[str, Any], raw_stream: dict[str, str] | None = None) -> dict[str, Any]:
    pools = artifact["source_pool_stream_sha256"]
    top10 = artifact["ranking_stream_sha256"]
    main = {
        "name": "main_raw_comparator",
        "artifact_arm": MAIN_RAW_ARM,
        "source_pool_stream_sha256": pools[MAIN_RAW_ARM],
        "top10_stream_sha256": top10[MAIN_RAW_ARM],
        "semantics": "published_main_table_raw_fusion_control",
    }
    full = {
        "name": "raw_full_order_prefix",
        "active_views": list(RAW_FULL_ORDER_VIEWS),
        "weights": {view: fcd2.FROZEN_WEIGHTS[view] for view in RAW_FULL_ORDER_VIEWS},
        "rrf_k": fcd2.RRF_K,
        "semantics": "full_authorized_universe_weighted_rrf_prefix",
    }
    if raw_stream is not None:
        full.update(raw_stream)
    main["protocol_sha256"] = fcd2._digest(main)
    full["protocol_sha256"] = fcd2._digest(full)
    if main["protocol_sha256"] == full["protocol_sha256"]:
        raise ValueError("raw protocol identities must remain distinct")
    return {"main_raw_comparator": main, "raw_full_order_prefix": full}


def _unlabeled_replay(artifact: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Validate and replay every ranking before any official-exact field is read."""
    traces, top10 = artifact["product_traces"], artifact["rankings_top10"]
    item_ids = [question.get("item_id") if isinstance(question, dict) else None for question in artifact["questions"]]
    if len(item_ids) != EXPECTED_QUESTIONS or any(not isinstance(item, str) or not item for item in item_ids) or len(set(item_ids)) != EXPECTED_QUESTIONS:
        raise ValueError("question identity denominator changed")
    if set(traces) != set(item_ids):
        raise ValueError("trace identities differ from questions")
    rows: dict[str, dict[str, Any]] = {}
    for item in item_ids:
        ledger = fcd2._ledger(fcd2._mapping(traces[item], "product trace"))
        stages = {view: fcd2.replay_weighted_rrf(ledger, active_views=ADD_VIEW_ORDER[:index]) for index, view in enumerate(ADD_VIEW_ORDER, start=1)}
        raw = stages[RAW_FULL_ORDER_VIEWS[-1]]
        final = stages[ADD_VIEW_ORDER[-1]]
        no_checkpoint = fcd2.replay_weighted_rrf(ledger, active_views=NO_CHECKPOINT_VIEWS)
        joint_removal = fcd2.replay_weighted_rrf(ledger, active_views=JOINT_REMOVAL_VIEWS)
        ranking_orders = _ranking_key_orders(ledger)
        orders = {"raw_full_order_prefix": _top_order(raw, TOP_K, ranking_orders), "final": _top_order(final, TOP_K, ranking_orders), "no_checkpoint": _top_order(no_checkpoint, TOP_K, ranking_orders), "joint_removal": _top_order(joint_removal, TOP_K, ranking_orders)}
        for replay in stages.values():
            _boundary(replay)
        event_hashes = _event_hashes(ledger)
        product_hashes = [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in fcd2._ids(top10["product_six_view"][item], "product top-10", TOP_K)]
        if [event_hashes[event] for event in orders["final"][:TOP_K]] != product_hashes:
            raise ValueError("final full-order replay differs from frozen Product ranking")
        rows[item] = {"ledger": ledger, "stages": stages, "replays": {"raw_full_order_prefix": raw, "final": final, "no_checkpoint": no_checkpoint, "joint_removal": joint_removal}, "orders": orders, "ranking_orders": ranking_orders, "event_hashes": event_hashes, "aerp4_anchor_inputs": _aerp4_anchor_inputs(raw, no_checkpoint, event_hashes, ranking_orders)}
    raw_top10 = {item: [value["event_hashes"][event] for event in value["orders"]["raw_full_order_prefix"][:TOP_K]] for item, value in rows.items()}
    raw_ranks = {item: sorted((value["event_hashes"][event], rank) for event, rank in _score_ranks(value["replays"]["raw_full_order_prefix"]).items()) for item, value in rows.items()}
    digest = fcd2._digest({item: {"raw": raw_top10[item], "final": [value["event_hashes"][event] for event in value["orders"]["final"][:TOP_K]]} for item, value in rows.items()})
    return rows, {"expected_questions": EXPECTED_QUESTIONS, "full_order_replayed_questions": len(rows), "ranking_freeze_sha256": digest, "raw_full_order_prefix_top10_stream_sha256": fcd2._digest(raw_top10), "raw_full_order_prefix_score_rank_stream_sha256": fcd2._digest(raw_ranks)}


def _label_join(artifact: dict[str, Any], replay_rows: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Join official-exact evidence only after the rank-only freeze has completed."""
    questions = artifact["questions"]
    fcd2._validate_question_audits(artifact, questions, artifact["source_pool_rankings"], artifact["rankings_top10"], artifact["product_traces"])
    outputs: list[dict[str, Any]] = []
    metric_rows = {name: [] for name in ("main_raw_comparator", "raw_full_order_prefix", "final", "no_checkpoint", "joint_removal")}
    for question in questions:
        item, category = question["item_id"], question["category"]
        gold, unresolved = fcd2._gold(question)
        state = replay_rows[item]
        event_by_hash = {digest: event for event, digest in state["event_hashes"].items()}
        stage_rank_maps = {view: _score_ranks(replay) for view, replay in state["stages"].items()}
        rank_maps = {name: _score_ranks(replay) for name, replay in state["replays"].items()}
        boundaries = {view: _boundary(replay) for view, replay in state["stages"].items()}
        boundaries["no_checkpoint"] = _boundary(fcd2.replay_weighted_rrf(state["ledger"], active_views=NO_CHECKPOINT_VIEWS))
        boundaries["joint_removal_observation_dense_plus_checkpoint_dense"] = _boundary(fcd2.replay_weighted_rrf(state["ledger"], active_views=JOINT_REMOVAL_VIEWS))
        evidence_rows = []
        for index, dialog_id in enumerate(gold):
            digest = hashlib.sha256(dialog_id.encode("utf-8")).hexdigest()
            event = event_by_hash.get(digest)
            ranks = _derive_evidence_ranks(state, event, stage_rank_maps, rank_maps)
            raw_rank = ranks["fused_competition_ranks"]["raw_full_order_prefix"]
            final_rank = ranks["fused_competition_ranks"]["final"]
            transition = _transition_class(raw_rank, final_rank)
            first_demotion = next((view for view in ADD_VIEW_ORDER[2:] if stage_rank_maps[view].get(event, TOP_K + 1) > TOP_K), None) if transition == "raw-hit→full-miss" else None
            evidence_rows.append({"evidence_index": index, "evidence_id_sha256": digest, **ranks, "transition_class": transition, "first_demotion_stage": first_demotion, "top10_boundary_margin": boundaries})
        for index in range(len(gold), len(gold) + unresolved):
            evidence_rows.append({"evidence_index": index, "evidence_id_sha256": None, "per_view_producer_ordinal_rank": {view: None for view in fcd2.VIEWS}, "fused_competition_ranks": {"raw_full_order_prefix": None, "add_view_stages": {view: None for view in ADD_VIEW_ORDER}, "final": None, "no_checkpoint": None, "joint_removal_observation_dense_plus_checkpoint_dense": None}, "transition_class": "miss→miss", "first_demotion_stage": None, "top10_boundary_margin": boundaries})
        hashed_gold = [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in gold]
        main = [hashlib.sha256(value.encode("utf-8")).hexdigest() for value in artifact["rankings_top10"][MAIN_RAW_ARM][item]]
        rankings = {"main_raw_comparator": main, "raw_full_order_prefix": [state["event_hashes"][event] for event in state["orders"]["raw_full_order_prefix"][:TOP_K]], "final": [state["event_hashes"][event] for event in state["orders"]["final"][:TOP_K]], "no_checkpoint": [state["event_hashes"][event] for event in state["orders"]["no_checkpoint"][:TOP_K]], "joint_removal": [state["event_hashes"][event] for event in state["orders"]["joint_removal"][:TOP_K]]}
        for name, ranking in rankings.items():
            metric_rows[name].append((category, hashed_gold, unresolved, ranking))
        outputs.append({"item_id_sha256": _sha(item.encode("utf-8")), "category": category, "conversation_sha256": _sha(question["conversation_id"].encode("utf-8")), "evidence": evidence_rows, "per_view_top50_overlap_with_raw_full_order": _overlap({"ledger": state["ledger"], "raw_full_order_prefix": state["stages"][RAW_FULL_ORDER_VIEWS[-1]]}), "aerp4_raw_anchored_p5_inputs": state["aerp4_anchor_inputs"]})
    metrics = {name: fcd2._aggregate(rows) for name, rows in metric_rows.items()}
    return outputs, metrics


def _assert_expected_metrics(metrics: dict[str, Any]) -> None:
    for name, expected in EXPECTED_REPLAY.items():
        if metrics[name] != expected:
            raise ValueError(f"{name} aggregate replay differs from frozen reference")


def _mechanism_gate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    cat5 = [evidence for row in rows if row["category"] == 5 for evidence in row["evidence"]]
    misses = [row for row in cat5 if row["fused_competition_ranks"]["final"] is None or row["fused_competition_ranks"]["final"] > TOP_K]
    union_present = sum(any(rank is not None and rank <= 50 for rank in row["per_view_producer_ordinal_rank"].values()) for row in misses)
    demotions = [row for row in cat5 if row["transition_class"] == "raw-hit→full-miss"]
    promotions = sum(row["transition_class"] == "raw-miss→full-hit" for row in cat5)
    first_dense = sum(row["first_demotion_stage"] in {"observation_dense", "checkpoint_dense"} for row in demotions)
    by_group: dict[str, list[float]] = {}
    for row in rows:
        if row["category"] != 5:
            continue
        values = [(1.0 if e["fused_competition_ranks"]["raw_full_order_prefix"] is not None and e["fused_competition_ranks"]["raw_full_order_prefix"] <= TOP_K else 0.0) - (1.0 if e["fused_competition_ranks"]["final"] is not None and e["fused_competition_ranks"]["final"] <= TOP_K else 0.0) for e in row["evidence"]]
        by_group.setdefault(row["conversation_sha256"], []).append(sum(values) / len(values))
    groups = [sum(values) / len(values) for _, values in sorted(by_group.items())]
    if not misses or not demotions or not groups:
        raise ValueError("Cat-5 mechanism-gate denominator is empty")
    rng = random.Random(20260822); samples = [sum(rng.choice(groups) for _ in groups) / len(groups) for _ in range(10000)]; ordered = sorted(samples)
    position = (len(ordered) - 1) * .025; low, high = int(position), int(position) + 1; lower = ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    checks = {"union_coverage_at_least_80pct": union_present / len(misses) >= .8, "demotions_strictly_exceed_promotions": len(demotions) > promotions, "paired_group_bootstrap_lower_gt_zero": lower > 0.0, "dense_first_demotion_at_least_50pct": first_dense / len(demotions) >= .5}
    return {"verdict": "PASS" if all(checks.values()) else "FAIL", "checks": checks, "cat5_final_miss": {"denominator": len(misses), "six_view_top50_union_present": union_present, "fraction": union_present / len(misses)}, "transitions": {"raw_hit_to_full_miss": len(demotions), "raw_miss_to_full_hit": promotions}, "first_demotion": {"denominator": len(demotions), "observation_dense_or_checkpoint_dense": first_dense, "fraction": first_dense / len(demotions)}, "paired_group_bootstrap": {"estimand": "Cat-5 conversation-macro(raw_full_order_prefix_hit_at_10-final_hit_at_10)", "groups": len(groups), "seed": 20260822, "resamples": 10000, "lower_95": lower, "replicates_sha256": fcd2._digest([float.hex(value) for value in samples])}}


def _validate_frozen_chain(*, artifact_sha256: str, prefreeze_sha256: str, expected_git_head: str, fcd2_report_path: Path | str, expected_fcd2_report_sha256: str) -> tuple[dict[str, Any], bytes, str]:
    """Use the historical FCD-2 receipt when current source hashes have moved on."""
    path = Path(fcd2_report_path).resolve(); raw = path.read_bytes(); receipt = _sha(raw)
    if receipt != expected_fcd2_report_sha256:
        raise ValueError("FCD-2 report SHA-256 mismatch")
    try:
        report = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("FCD-2 report is not valid JSON") from error
    if not isinstance(report, dict) or report.get("schema") != fcd2.SCHEMA or report.get("status") != "complete":
        raise ValueError("FCD-2 report schema or status mismatch")
    input_receipt = report.get("input_receipt")
    parity = report.get("fusion_semantics_parity")
    ablation = report.get("ablation_replay")
    if not isinstance(input_receipt, dict) or input_receipt.get("artifact_sha256") != artifact_sha256 or input_receipt.get("prefreeze_sha256") != prefreeze_sha256 or input_receipt.get("artifact_producer_git_head") != expected_git_head:
        raise ValueError("FCD-2 report frozen-input binding mismatch")
    if not isinstance(parity, dict) or parity.get("status") != "complete" or parity.get("full_order_receipts_exact_questions") != EXPECTED_QUESTIONS or parity.get("product_top10_stored_order_exact_questions") != EXPECTED_QUESTIONS:
        raise ValueError("FCD-2 full-order replay gate is incomplete")
    if not isinstance(ablation, dict) or ablation.get("status") != "complete" or ablation.get("strict_top10_boundary_questions") != EXPECTED_QUESTIONS * 2 * len(ADD_VIEW_ORDER):
        raise ValueError("FCD-2 ablation replay gate is incomplete")
    return report, raw, receipt


def build_transition_ledger(artifact_path: Path | str, *, expected_artifact_sha256: str, expected_git_head: str, prefreeze_receipt_path: Path | str, expected_prefreeze_sha256: str, fcd2_report_path: Path | str, expected_fcd2_report_sha256: str, _artifact_bytes: bytes | None = None) -> dict[str, Any]:
    artifact_file = Path(artifact_path).resolve()
    artifact, raw, receipt = fcd2._load(artifact_file, expected_artifact_sha256, _artifact_bytes)
    prefreeze, prefreeze_raw, prefreeze_sha = fcd2._load_prefreeze(Path(prefreeze_receipt_path).resolve(), expected_prefreeze_sha256)
    _fcd2_report, fcd2_raw, fcd2_sha = _validate_frozen_chain(artifact_sha256=receipt, prefreeze_sha256=prefreeze_sha, expected_git_head=expected_git_head, fcd2_report_path=fcd2_report_path, expected_fcd2_report_sha256=expected_fcd2_report_sha256)
    fcd2._validate_stream_receipts(artifact, artifact["source_pool_rankings"], artifact["rankings_top10"], expected_product_digest=prefreeze["stream_receipts"]["product_top10_sha256"])
    replay_rows, rank_gate = _unlabeled_replay(artifact)
    protocols = _protocols(artifact, {"top10_stream_sha256": rank_gate["raw_full_order_prefix_top10_stream_sha256"], "score_rank_stream_sha256": rank_gate["raw_full_order_prefix_score_rank_stream_sha256"]})
    evidence, metrics = _label_join(artifact, replay_rows)
    _assert_expected_metrics(metrics)
    mechanism_gate = _mechanism_gate(evidence)
    report = {"schema": SCHEMA, "version": 1, "status": "complete", "phase_ledger": ["artifact_bytes_frozen", "prefreeze_receipt_verified", "historical_fcd2_gate_verified", "unlabeled_ranking_replay", "ranking_digest_validated", "postfreeze_label_join", "mechanism_gate_evaluated", "offline_diagnostics_complete", "input_rechecked"], "input_receipt": {"artifact_sha256": receipt, "prefreeze_sha256": prefreeze_sha, "fcd2_report_sha256": fcd2_sha, "artifact_producer_git_head": expected_git_head, "analyzer_implementation_sha256": _sha(Path(__file__).read_bytes())}, "protocols": protocols, "rank_gate": rank_gate, "metrics": metrics, "mechanism_gate": mechanism_gate, "transition_ledger": evidence, "offline_diagnostics": {"no_checkpoint": {"active_views": list(NO_CHECKPOINT_VIEWS), "metric_arm": "no_checkpoint"}, "joint_removal": {"removed_views": ["observation_dense", "checkpoint_dense"], "active_views": list(JOINT_REMOVAL_VIEWS), "metric_arm": "joint_removal"}, "aerp4_raw_anchored_p5_preparation": {"policy": "RawAnchoredP5Policy", "raw_arm": "raw_full_order_prefix", "p5_arm": "no_checkpoint", "inputs": "rank-only raw/P5 top-10 digests and anchor ratio", "label_access": "none", "status": "existing_authorized_guard_inputs_only_no_tau_selected"}}, "claim_boundary": "Read-only frozen-artifact analysis. It prepares rank-only inputs for the existing AERP-4 RawAnchoredP5 policy but neither selects tau nor changes live ranking."}
    _safe_report(report)
    _unchanged_stream(artifact_file, receipt, len(raw))
    _unchanged_stream(Path(prefreeze_receipt_path).resolve(), prefreeze_sha, len(prefreeze_raw))
    _unchanged_stream(Path(fcd2_report_path).resolve(), fcd2_sha, len(fcd2_raw))
    return report


def atomic_json(output: Path | str, report: dict[str, Any], *, artifact_path: Path | str, expected_artifact_bytes: bytes, prefreeze_path: Path | str, expected_prefreeze_bytes: bytes, fcd2_report_path: Path | str, expected_fcd2_report_sha256: str, expected_fcd2_report_bytes: int) -> None:
    target, artifact, prefreeze = Path(output).resolve(), Path(artifact_path).resolve(), Path(prefreeze_path).resolve()
    if target.exists() or target == artifact or target == prefreeze or target == ROOT or ROOT in target.parents:
        raise ValueError("output must be a new file outside the repository and inputs")
    if report.get("schema") != SCHEMA or report.get("status") != "complete" or report.get("input_receipt", {}).get("artifact_sha256") != _sha(expected_artifact_bytes) or report["input_receipt"].get("prefreeze_sha256") != _sha(expected_prefreeze_bytes):
        raise ValueError("report publication binding is invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")); handle.flush(); os.fsync(handle.fileno())
    try:
        _unchanged_stream(artifact, _sha(expected_artifact_bytes), len(expected_artifact_bytes)); _unchanged_stream(prefreeze, _sha(expected_prefreeze_bytes), len(expected_prefreeze_bytes)); _unchanged_stream(Path(fcd2_report_path).resolve(), expected_fcd2_report_sha256, expected_fcd2_report_bytes)
        os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True); parser.add_argument("--expected-artifact-sha256", required=True); parser.add_argument("--expected-git-head", required=True)
    parser.add_argument("--prefreeze-receipt", required=True); parser.add_argument("--expected-prefreeze-sha256", required=True)
    parser.add_argument("--fcd2-report", required=True); parser.add_argument("--expected-fcd2-report-sha256", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        artifact, prefreeze, fcd2_report = Path(args.artifact).resolve(), Path(args.prefreeze_receipt).resolve(), Path(args.fcd2_report).resolve()
        artifact_raw, prefreeze_raw = artifact.read_bytes(), prefreeze.read_bytes(); fcd2_size = fcd2_report.stat().st_size
        report = build_transition_ledger(artifact, expected_artifact_sha256=args.expected_artifact_sha256, expected_git_head=args.expected_git_head, prefreeze_receipt_path=prefreeze, expected_prefreeze_sha256=args.expected_prefreeze_sha256, fcd2_report_path=fcd2_report, expected_fcd2_report_sha256=args.expected_fcd2_report_sha256, _artifact_bytes=artifact_raw)
        atomic_json(args.output, report, artifact_path=artifact, expected_artifact_bytes=artifact_raw, prefreeze_path=prefreeze, expected_prefreeze_bytes=prefreeze_raw, fcd2_report_path=fcd2_report, expected_fcd2_report_sha256=args.expected_fcd2_report_sha256, expected_fcd2_report_bytes=fcd2_size)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True), file=os.sys.stderr); return 2
    print(json.dumps({"status": "complete", "rank_gate": report["rank_gate"]}, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
