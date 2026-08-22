from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path

import pytest


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_production_protocol_pins_precede_synthetic_monkeypatching() -> None:
    from benchmarks import aerp3_fcd2_causal_ablation as module

    assert module.SCHEMA == "aerp3-fcd2-causal-ablation"
    assert module.FCD1_SCHEMA == "aerp3-fcd1-replay-ledger-v1"
    assert module.EXPECTED_QUESTIONS == 1986
    assert module.EXPECTED_POOL == 50
    assert module.TOP_K == 10
    assert module.PRODUCT_ARM == "product_six_view"
    assert module.RAW_ARMS == ("raw_bm25", "raw_dense")
    assert module.BASELINE_ARM == "raw_bm25_plus_raw_dense"
    assert module.VIEWS == ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")
    assert module.ADD_VIEW_ORDER == module.VIEWS
    assert module.PRODUCT_FUSION_VIEW_ORDER == ("raw_bm25", "observation_bm25", "raw_dense", "observation_dense", "checkpoint_dense", "combo_dense")
    assert module.ADD_VIEW_ORDER != module.PRODUCT_FUSION_VIEW_ORDER
    assert module.RRF_K == 60
    assert module.FROZEN_WEIGHTS == {"raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0}
    assert module.P1_MIN_DELTA == 0.05
    assert module.P2_MIN_DELTA == -0.01
    assert module.STRICT_MAJORITY_MULTIPLIER == 2
    assert module.EXPECTED_CONVERSATIONS == 10
    assert module.MIN_RECOVERY_CONVERSATIONS == 8
    assert module.EXPECTED_QUESTIONS * 2 * len(module.ADD_VIEW_ORDER) == 23832
    assert module.PRODUCT_TOP10_SHA256 == "64007282069621bb3e603598938993ebe0907e8e84ebaa65394741ab618e5441"


def test_preregistered_delta_comparators_include_equality_and_reject_one_step_below(module) -> None:
    assert module._p1_pass({"overall": 0.05, "hard": 0.05, "adversarial": None})
    assert module._p2_pass({"overall": -0.01, "hard": -0.01, "adversarial": None})
    assert not module._p1_pass({"overall": math.nextafter(0.05, -math.inf), "hard": 0.05, "adversarial": None})
    assert not module._p2_pass({"overall": math.nextafter(-0.01, -math.inf), "hard": -0.01, "adversarial": None})


def _row(identifier: str, rank: int) -> dict[str, object]:
    prefix = identifier[0]
    if prefix in {"b", "d", "g", "z"}:
        order = {"b": 1, "d": 2, "g": 3, "z": 4}[prefix]
    else:
        order = 5 + int(identifier.split("-")[1])
    return {
        "source_event_id": f"event:{identifier}",
        "ranking_key_sha256": _sha(identifier),
        "ranking_key_order": order,
        "rank": rank,
        "score": float(100 - rank),
    }


def _ledger(views: dict[str, list[dict[str, object]]], *, full_orders: dict[str, list[str]] | None = None, weights: dict[str, float] | None = None) -> dict:
    weights = weights or {"raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0}
    product_order = ("raw_bm25", "observation_bm25", "raw_dense", "observation_dense", "checkpoint_dense", "combo_dense")
    full_orders = full_orders or {name: [row["source_event_id"] for row in rows] for name, rows in views.items()}
    known_rows = {row["source_event_id"]: row for rows in views.values() for row in rows}
    event_hash = lambda event: known_rows[event]["ranking_key_sha256"] if event in known_rows else _sha(event.removeprefix("event:"))
    order_receipts = {name: _canonical_sha256([event_hash(event) for event in order]) for name, order in full_orders.items()}
    policy = "4" * 64
    event_rows = {event: known_rows.get(event, {"ranking_key_sha256": event_hash(event), "ranking_key_order": rank + 1, "score": float(-rank)}) for rank, event in enumerate(full_orders["raw_bm25"])}
    checkpoint_rows = {row["source_event_id"]: row for row in views["checkpoint_dense"]}
    groups = []
    for event, row in event_rows.items():
        checkpoint = _sha(f"checkpoint:{event}")
        groups.append({
            "group_id": "group:" + _canonical_sha256([checkpoint, policy]),
            "checkpoint_sha256": checkpoint,
            "policy_sha256": policy,
            "checkpoint_score": checkpoint_rows.get(event, {"score": event_rows[event]["score"]})["score"],
            "member_count": 1,
            "chronological_members": [{"source_event_id": event, "ranking_key_sha256": row["ranking_key_sha256"]}],
        })
    authorization = _canonical_sha256(sorted([
        {"ranking_key_sha256": row["ranking_key_sha256"], "policy_sha256": policy}
        for row in event_rows.values()
    ], key=lambda row: row["ranking_key_sha256"]))
    fused = []
    for event, row in event_rows.items():
        component_ranks = {name: full_orders[name].index(event) + 1 for name in views}
        contributions = {name: weights[name] / (60 + component_ranks[name]) for name in product_order}
        final = sum(contributions[name] for name in product_order)
        fused.append({
            "source_event_id": event,
            "ranking_key_sha256": row["ranking_key_sha256"],
            "ranking_key_order": row["ranking_key_order"],
            "rank": 0,
            "final_rrf": final,
            "component_ranks": component_ranks,
            "component_rank_receipts": [{"view": name, "view_order_sha256": order_receipts[name], "ranking_key_sha256": row["ranking_key_sha256"], "rank": component_ranks[name]} for name in views],
            "contributions": contributions,
        })
    fused.sort(key=lambda row: (-float(row["final_rrf"]), row["ranking_key_order"]))
    fused = fused[:len(views["raw_bm25"])]
    for rank, row in enumerate(fused, start=1):
        row["rank"] = rank
    return {
        "schema": "aerp3-fcd1-replay-ledger-v1",
        "input_sha256": "1" * 64,
        "authorization_sha256": authorization,
        "view_top_50": views,
        "view_top_50_sha256": {name: _canonical_sha256(rows) for name, rows in views.items()},
        "view_full_order": full_orders,
        "view_order_sha256": order_receipts,
        "fused_top_50": fused,
        "checkpoint_tie_group_semantics": "checkpoint_policy_rollup",
        "checkpoint_tie_groups": groups,
    }


def _artifact(kind: str = "fusion", *, duplicate_gold: bool = False) -> dict:
    """A ten-question FCD-1-shaped artifact; fixture IDs never enter the report."""
    items = [f"item-{index}" for index in range(10)]
    pool = {item: [f"b-{item}", f"d-{item}", f"g-{item}", f"z-{item}"] for item in items}
    if kind == "representation":
        golds = {item: f"outside-{item}" for item in items}
    elif kind == "boundary_half":
        golds = {item: (f"g-{item}" if index < 5 else f"outside-{item}") for index, item in enumerate(items)}
    elif kind == "boundary_low_concentration":
        golds = {item: (f"g-{item}" if index < 6 else f"outside-{item}") for index, item in enumerate(items)}
    elif kind == "boundary_eight":
        golds = {item: (f"g-{item}" if index < 8 else f"outside-{item}") for index, item in enumerate(items)}
    else:
        golds = {item: (f"b-{item}" if kind == "nonidentical" else f"g-{item}") for item in items}
    product = {item: pool[item][:2] for item in items}
    top10 = {
        "raw_bm25": {item: pool[item][:2] for item in items},
        "raw_dense": {item: pool[item][:2] for item in items},
        "raw_bm25_plus_raw_dense": {item: pool[item][:2] for item in items},
        "legacy_rpg": {item: pool[item][:2] for item in items},
        "product_six_view": product,
        "historical_six_view": {item: pool[item][:2] for item in items},
    }
    source = {arm: {item: list(pool[item]) for item in items} for arm in ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")}
    if kind == "semantic":
        product = {item: [pool[item][1], pool[item][0]] for item in items}
        top10["product_six_view"] = product
    if kind == "nonidentical":
        source["raw_dense"] = {item: [pool[item][1], pool[item][0], pool[item][2], pool[item][3]] for item in items}
        top10["raw_dense"] = {item: source["raw_dense"][item][:2] for item in items}
        product = {item: [pool[item][1], pool[item][2]] for item in items}
        top10["product_six_view"] = product
    traces: dict[str, object] = {}
    for item in items:
        views = {name: [_row(identifier, rank) for rank, identifier in enumerate(pool[item], start=1)] for name in ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")}
        if kind == "component":
            views["raw_dense"] = [_row(identifier, rank) for rank, identifier in enumerate([pool[item][1], pool[item][0], pool[item][2], pool[item][3]], start=1)]
        if kind == "tail_tie":
            views["raw_dense"] = [_row(identifier, rank) for rank, identifier in enumerate([pool[item][0], pool[item][1], pool[item][3], pool[item][2]], start=1)]
        if kind == "nonidentical":
            views = {
                "raw_bm25": [_row(identifier, rank) for rank, identifier in enumerate([pool[item][0], pool[item][1], pool[item][2], pool[item][3]], start=1)],
                "raw_dense": [_row(identifier, rank) for rank, identifier in enumerate([pool[item][1], pool[item][0], pool[item][2], pool[item][3]], start=1)],
                "observation_bm25": [_row(identifier, rank) for rank, identifier in enumerate([pool[item][2], pool[item][0], pool[item][1], pool[item][3]], start=1)],
                "observation_dense": [_row(identifier, rank) for rank, identifier in enumerate([pool[item][1], pool[item][2], pool[item][0], pool[item][3]], start=1)],
                "checkpoint_dense": [_row(identifier, rank) for rank, identifier in enumerate([pool[item][2], pool[item][1], pool[item][0], pool[item][3]], start=1)],
                "combo_dense": [_row(identifier, rank) for rank, identifier in enumerate([pool[item][0], pool[item][2], pool[item][1], pool[item][3]], start=1)],
            }
        tails = [f"tail-{index:02d}-{item}" for index in range(56)]
        full_orders = {
            name: [row["source_event_id"] for row in rows] + [f"event:{tail}" for tail in tails]
            for name, rows in views.items()
        }
        traces[item] = {"retrieval_ranking": {"fcd1_diagnostic_ledger": _ledger(views, full_orders=full_orders)}}
    questions = []
    for index, item in enumerate(items):
        resolved = [golds[item], golds[item]] if duplicate_gold and index == 0 else [golds[item]]
        unresolved = 1 if duplicate_gold and index == 0 else 0
        questions.append({
            "item_id": item,
            "conversation_id": f"conversation-{index}",
            "category": 1 if index < 5 else 5,
            "gold": {"official_exact": {"resolved_dialog_ids": resolved, "unresolved_evidence_item_count": unresolved, "evidence_item_count": len(resolved) + unresolved}},
        })
    audits = []
    for question in questions:
        item = question["item_id"]
        audits.append({
            "composite_id": [question["conversation_id"], item],
            "top10": {arm: top10[arm][item] for arm in top10},
            "source_pool": {arm: {"count": 4, "sha256": _canonical_sha256(source[arm][item])} for arm in source},
            "product_retrieval": {"fcd1_diagnostic_ledger_sha256": _canonical_sha256(traces[item]["retrieval_ranking"]["fcd1_diagnostic_ledger"])},
        })
    product_digest = _canonical_sha256(product)
    state = {"git_head": "a" * 40, "git_dirty": False}
    return {
        "schema": "aerp2-product-six-view-locomo",
        "status": "complete",
        "git_state_before": state,
        "git_state_after": deepcopy(state),
        "fcd1_acceptance": {"ledger_schema": "aerp3-fcd1-replay-ledger-v1", "top_k": 4, "expected_questions": 10, "reference_product_top10_sha256": product_digest, "actual_product_top10_sha256": product_digest, "top10_unchanged": True},
        "safety_summary": {"pass": True, "expected_trace_count": 10, "trace_count": 10, "fcd1_ledger_complete_count": 10, "unauthorized_selected_count": 0},
        "rankings_top10": top10,
        "source_pool_rankings": source,
        "ranking_stream_sha256": {arm: _canonical_sha256(stream) for arm, stream in top10.items()},
        "source_pool_stream_sha256": {arm: _canonical_sha256(stream) for arm, stream in source.items()},
        "product_traces": traces,
        "question_audits": audits,
        "questions": questions,
    }


def _configured(module, monkeypatch: pytest.MonkeyPatch, artifact: dict | None = None) -> None:
    artifact = _artifact() if artifact is None else artifact
    monkeypatch.setattr(module, "EXPECTED_QUESTIONS", 10)
    monkeypatch.setattr(module, "EXPECTED_POOL", 4)
    monkeypatch.setattr(module, "TOP_K", 2)
    monkeypatch.setattr(module, "PRODUCT_TOP10_SHA256", artifact["fcd1_acceptance"]["actual_product_top10_sha256"])
    monkeypatch.setattr(module, "_clean_git_head", lambda _path: "a" * 40)
    analyzer_state = {"git_head": "b" * 40, "git_tree": "c" * 40, "git_dirty": False, "worktree_status_sha256": "d" * 64, "commit_diff_sha256": "e" * 64, "commit_diff_bytes": 0}
    monkeypatch.setattr(module, "_analyzer_git_state", lambda: dict(analyzer_state))


def _rebind_artifact_receipts(artifact: dict) -> None:
    artifact["ranking_stream_sha256"] = {arm: _canonical_sha256(stream) for arm, stream in artifact["rankings_top10"].items()}
    artifact["source_pool_stream_sha256"] = {arm: _canonical_sha256(stream) for arm, stream in artifact["source_pool_rankings"].items()}
    product_digest = _canonical_sha256(artifact["rankings_top10"]["product_six_view"])
    artifact["fcd1_acceptance"]["reference_product_top10_sha256"] = product_digest
    artifact["fcd1_acceptance"]["actual_product_top10_sha256"] = product_digest
    for audit in artifact["question_audits"]:
        item = audit["composite_id"][1]
        for arm, stream in artifact["source_pool_rankings"].items():
            audit["source_pool"][arm]["sha256"] = _canonical_sha256(stream[item])
        audit["top10"] = {arm: stream[item] for arm, stream in artifact["rankings_top10"].items()}
        ledger = artifact["product_traces"][item]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
        audit["product_retrieval"]["fcd1_diagnostic_ledger_sha256"] = _canonical_sha256(ledger)


def _internal_tie_artifact() -> tuple[dict, dict[str, float]]:
    """Valid FCD-1-shaped data with a non-cutoff fusion tie in producer order."""
    artifact = _artifact()
    weights = {view: 1.0 for view in ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")}
    for index in range(10):
        item = f"item-{index}"
        identifiers = [f"b-{item}", f"d-{item}", f"g-{item}", f"z-{item}"]
        reverse = [identifiers[1], identifiers[0], identifiers[2], identifiers[3]]
        views = {
            view: [_row(identifier, rank) for rank, identifier in enumerate(reverse if view in {"raw_bm25", "raw_dense", "observation_bm25"} else identifiers, start=1)]
            for view in ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")
        }
        tails = [f"tail-{tail:02d}-{item}" for tail in range(56)]
        full_orders = {view: [row["source_event_id"] for row in rows] + [f"event:{tail}" for tail in tails] for view, rows in views.items()}
        artifact["product_traces"][item] = {"retrieval_ranking": {"fcd1_diagnostic_ledger": _ledger(views, full_orders=full_orders, weights=weights)}}
        for arm in ("raw_bm25", "raw_dense"):
            artifact["source_pool_rankings"][arm][item] = reverse
            artifact["rankings_top10"][arm][item] = reverse[:2]
    _rebind_artifact_receipts(artifact)
    return artifact, weights


def _analyze(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str = "fusion", *, duplicate_gold: bool = False) -> tuple[dict, Path, bytes]:
    artifact = _artifact(kind, duplicate_gold=duplicate_gold)
    _configured(module, monkeypatch, artifact)
    path = tmp_path / "fcd1.json"
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40), path, raw


@pytest.mark.parametrize(("kind", "verdict"), [("component", "COMPONENT_PARITY_FAILED"), ("representation", "REPRESENTATION_SUPPORTED"), ("fusion", "FUSION_SUPPORTED")])
def test_three_frozen_verdict_paths(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, verdict: str) -> None:
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, kind)

    assert report["verdict"] == verdict
    if kind == "component":
        assert report["later_stages"] == "not_run_due_to_component_parity"
        assert report["raw_component_parity"]["arms"]["raw_dense"]["top10_order_exact_questions"] == 0
    else:
        assert report["raw_component_parity"]["arms"]["raw_bm25"]["top50_set_exact_questions"] == 10
        assert report["raw_component_parity"]["arms"]["raw_dense"]["official_exact_recall_at_10_delta"] == {"overall": 0.0, "hard": 0.0, "adversarial": 0.0}


def test_report_separates_artifact_producer_and_analyzer_git_provenance(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, _artifact, _raw = _analyze(module, monkeypatch, tmp_path)

    receipt = report["input_receipt"]
    assert receipt["artifact_producer_git_head"] == "a" * 40
    assert set(receipt["analyzer_git_state"]) == {"git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes"}
    assert receipt["analyzer_git_state"]["git_dirty"] is False
    assert report["protocol"]["oracle"] == "six_view_per_view_top50_candidate_coverage"


def test_duplicate_gold_and_unresolved_are_counted_without_id_leakage(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, duplicate_gold=True)

    assert report["verdict"] == "FUSION_SUPPORTED"
    assert report["stages"]["oracle"]["overall"] == pytest.approx((2 / 3 + 9) / 10)
    first_conversation = _sha("conversation-0")
    assert report["stages"]["hashed_budgeted_recovery"][first_conversation] == {"recoverable": 2, "nonrecoverable": 0}
    serialized = json.dumps(report, sort_keys=True)
    assert "item-" not in serialized and "conversation-" not in serialized and "g-item" not in serialized
    assert "query" not in serialized.casefold() and "transcript" not in serialized.casefold() and "answer" not in serialized.casefold()


def test_top50_tail_order_difference_is_reported_but_not_a_parity_blocker(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, "tail_tie")

    dense = report["raw_component_parity"]["arms"]["raw_dense"]
    assert report["verdict"] == "FUSION_SUPPORTED"
    assert dense["top50_order_exact_questions"] == 0
    assert dense["top10_order_exact_questions"] == 10
    assert dense["top50_set_exact_questions"] == 10


def test_rrf_and_aggregate_ablations_replay_a_nonidentical_fixture(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, "nonidentical")
    artifact = _artifact("nonidentical")
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    expected = [_sha("d-item-0"), _sha("g-item-0"), _sha("b-item-0"), _sha("z-item-0")]
    event_hashes = {
        member["source_event_id"]: member["ranking_key_sha256"]
        for group in ledger["checkpoint_tie_groups"]
        for member in group["chronological_members"]
    }

    replay = module.replay_weighted_rrf(ledger)
    assert "ordered_events" not in replay
    assert {event_hashes[event] for event in module._top_membership(replay, 4)} == set(expected)
    assert report["fusion_semantics_parity"] == {
        "status": "complete", "expected_questions": 10,
        "full_order_receipts_exact_questions": 10,
        "full_fused_top50_membership_exact_questions": 10,
        "stored_fused_top50_order_consistent_questions": 10,
        "product_top10_stored_order_exact_questions": 10,
        "strict_top50_boundary_questions": 10,
        "strict_top10_boundary_questions": 10,
        "tie_break_unprovable_questions": 0,
    }
    assert report["protocol"]["fusion_view_accumulation_order"] == list(module.PRODUCT_FUSION_VIEW_ORDER)
    assert report["protocol"]["add_view_order"] == list(module.ADD_VIEW_ORDER)
    assert report["protocol"]["ablation"] == "full_order_rank_counterfactual"
    assert report["ablation_replay"] == {
        "status": "complete", "full_authorized_universe": True,
        "expected_scenario_question_checks": 120,
        "scenario_question_checks": 120,
        "strict_top10_boundary_questions": 120,
        "all_scenarios_strict_top10_boundary": True,
    }
    assert {view: metric["overall"] for view, metric in report["stages"]["ablations"]["add_view"].items()} == {
        "raw_bm25": 1.0, "raw_dense": 1.0, "observation_bm25": 1.0,
        "observation_dense": 1.0, "checkpoint_dense": 0.0, "combo_dense": 0.0,
    }
    assert {view: metric["overall"] for view, metric in report["stages"]["ablations"]["leave_one_out"].items()} == {
        "raw_bm25": 0.0, "raw_dense": 0.0, "observation_bm25": 1.0,
        "observation_dense": 1.0, "checkpoint_dense": 1.0, "combo_dense": 0.0,
    }


def _full_replay_ledger(size: int = 60) -> dict:
    events = [f"event:{index:03d}" for index in range(size)]
    return {"view_full_order": {view: list(events) for view in ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")}}


def test_full_order_replay_uses_candidates_beyond_top50_and_counterfactual_tail_membership(module) -> None:
    baseline = _full_replay_ledger()
    replay = module.replay_weighted_rrf(baseline)
    assert len(replay["scores"]) == 60
    assert module._strict_cutoff(replay, 50) and module._strict_cutoff(replay, 10)

    boundary_changed = deepcopy(baseline)
    raw = boundary_changed["view_full_order"]["raw_bm25"]
    raw[49], raw[50] = raw[50], raw[49]
    changed = module.replay_weighted_rrf(boundary_changed)
    assert changed["scores"]["event:049"] != replay["scores"]["event:049"]

    counterfactual_changed = deepcopy(baseline)
    raw = counterfactual_changed["view_full_order"]["raw_bm25"]
    raw[9], raw[10] = raw[10], raw[9]
    before = module.replay_weighted_rrf(baseline, active_views=("raw_bm25",))
    after = module.replay_weighted_rrf(counterfactual_changed, active_views=("raw_bm25",))
    assert module._top_membership(before, 10) != module._top_membership(after, 10)


def test_oracle_is_per_view_top50_coverage_not_full_order_universe(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configured(module, monkeypatch)
    artifact = _artifact()
    first = artifact["questions"][0]
    first["gold"]["official_exact"] = {"resolved_dialog_ids": ["tail-00-item-0"], "unresolved_evidence_item_count": 0, "evidence_item_count": 1}
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    assert "event:tail-00-item-0" in ledger["view_full_order"]["raw_bm25"]
    assert "event:tail-00-item-0" not in [row["source_event_id"] for row in ledger["view_top_50"]["raw_bm25"]]
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "tail-gold.json"; path.write_bytes(raw)
    report = module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40)
    assert report["protocol"]["oracle"] == "six_view_per_view_top50_candidate_coverage"
    assert report["stages"]["oracle"]["overall"] == pytest.approx(0.9)


@pytest.mark.parametrize("cutoff", [10, 50])
def test_full_order_cutoff_ties_are_explicitly_unprovable_not_hash_ordered(module, monkeypatch: pytest.MonkeyPatch, cutoff: int) -> None:
    monkeypatch.setattr(module, "FROZEN_WEIGHTS", {view: 1.0 for view in module.VIEWS})
    ledger = _full_replay_ledger()
    first, second = ledger["view_full_order"]["raw_bm25"], ledger["view_full_order"]["raw_dense"]
    first[cutoff - 1], first[cutoff] = first[cutoff], first[cutoff - 1]
    replay = module.replay_weighted_rrf(ledger, active_views=("raw_bm25", "raw_dense"))
    assert not module._strict_cutoff(replay, cutoff)


def test_tie_break_unprovable_stops_before_gold_join(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = module.replay_weighted_rrf
    def tie_at_product_top10(ledger, *, active_views=None):
        replay = original(ledger, active_views=active_views)
        if active_views is None:
            events = list(replay["scores"])
            replay["scores"][events[module.TOP_K - 1]] = replay["scores"][events[module.TOP_K]]
        return replay
    monkeypatch.setattr(module, "replay_weighted_rrf", tie_at_product_top10)
    monkeypatch.setattr(module, "_gold", lambda _question: (_ for _ in ()).throw(AssertionError("tie gate accessed gold")))
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path)
    assert report["verdict"] == "FUSION_TIEBREAK_UNPROVABLE"
    assert report["fusion_semantics_parity"]["tie_break_unprovable_questions"] == 10
    assert report["ablation_replay"] == {
        "status": "not_run_due_to_fusion_semantics_parity",
        "full_authorized_universe": True,
        "expected_scenario_question_checks": 120,
        "scenario_question_checks": 0,
        "strict_top10_boundary_questions": 0,
        "all_scenarios_strict_top10_boundary": False,
    }


def test_scenario_only_tie_stops_before_gold_without_rewriting_baseline_semantics(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original = module.replay_weighted_rrf

    def tie_only_counterfactuals(ledger, *, active_views=None):
        replay = original(ledger, active_views=active_views)
        if active_views is not None:
            events = list(replay["scores"])
            replay["scores"][events[module.TOP_K - 1]] = replay["scores"][events[module.TOP_K]]
        return replay

    monkeypatch.setattr(module, "replay_weighted_rrf", tie_only_counterfactuals)
    monkeypatch.setattr(module, "_gold", lambda _question: (_ for _ in ()).throw(AssertionError("ablation tie accessed gold")))
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path)

    assert report["verdict"] == "ABLATION_TIEBREAK_UNPROVABLE"
    assert report["later_stages"] == "not_run_due_to_ablation_replay"
    assert report["fusion_semantics_parity"]["status"] == "complete"
    assert report["fusion_semantics_parity"]["tie_break_unprovable_questions"] == 0
    assert report["ablation_replay"] == {
        "status": "FUSION_TIEBREAK_UNPROVABLE",
        "full_authorized_universe": True,
        "expected_scenario_question_checks": 120,
        "scenario_question_checks": 120,
        "strict_top10_boundary_questions": 0,
        "all_scenarios_strict_top10_boundary": False,
    }


def test_internal_fusion_tie_uses_stored_producer_order_not_raw_view_or_replay_order(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifact, weights = _internal_tie_artifact()
    _configured(module, monkeypatch, artifact)
    monkeypatch.setattr(module, "FROZEN_WEIGHTS", weights)
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    stored = [row["source_event_id"] for row in ledger["fused_top_50"]]
    raw = [row["source_event_id"] for row in ledger["view_top_50"]["raw_bm25"]]
    assert raw[:2] == list(reversed(stored[:2]))
    assert ledger["fused_top_50"][0]["final_rrf"] == ledger["fused_top_50"][1]["final_rrf"]
    raw_bytes = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "internal-tie.json"; path.write_bytes(raw_bytes)

    report = module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw_bytes).hexdigest(), expected_git_head="a" * 40)

    semantics = report["fusion_semantics_parity"]
    assert report["verdict"] == "FUSION_SUPPORTED"
    assert semantics["full_fused_top50_membership_exact_questions"] == 10
    assert semantics["stored_fused_top50_order_consistent_questions"] == 10
    assert semantics["product_top10_stored_order_exact_questions"] == 10
    assert semantics["strict_top50_boundary_questions"] == 10
    assert semantics["strict_top10_boundary_questions"] == 10


def test_one_ulp_fusion_score_gap_controls_cutoff_membership_without_tie_order(module) -> None:
    lower = math.nextafter(1.0, -math.inf)
    replay = {"scores": {"opaque-a": 1.0, "opaque-b": lower, "opaque-c": 0.5}}
    assert module._strict_cutoff(replay, 1)
    assert module._top_membership(replay, 1) == {"opaque-a"}
    replay["scores"]["opaque-b"] = 1.0
    assert not module._strict_cutoff(replay, 1)


def test_fusion_semantics_failure_stops_before_later_label_stages(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden_gold(_question):
        raise AssertionError("fusion-semantic failure must precede label access")

    monkeypatch.setattr(module, "_gold", forbidden_gold)
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, "semantic")

    assert report["verdict"] == "FUSION_SEMANTICS_PARITY_FAILED"
    assert report["later_stages"] == "not_run_due_to_fusion_semantics_parity"
    assert report["fusion_semantics_parity"]["product_top10_stored_order_exact_questions"] == 0


def test_raw_parity_failure_never_reads_gold(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[object] = []
    original = module._gold

    def spy(question):
        calls.append(question)
        return original(question)

    monkeypatch.setattr(module, "_gold", spy)
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, "component")

    assert report["verdict"] == "COMPONENT_PARITY_FAILED"
    assert calls == []


@pytest.mark.parametrize(("kind", "verdict", "strict_majority", "conversation_pass"), [
    ("boundary_half", "REPRESENTATION_SUPPORTED", False, False),
    ("boundary_low_concentration", "REPRESENTATION_SUPPORTED", True, False),
    ("boundary_eight", "FUSION_SUPPORTED", True, True),
])
def test_preregistered_recovery_boundaries(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, verdict: str, strict_majority: bool, conversation_pass: bool) -> None:
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, kind)

    assert report["verdict"] == verdict
    assert report["protocol"]["expected_conversations"] == 10
    assert report["stages"]["gates"]["strict_majority"] is strict_majority
    assert report["stages"]["gates"]["budgeted_recovery_conversation_pass"] is conversation_pass


@pytest.mark.parametrize("mutate", [
    lambda artifact: artifact.__setitem__("schema", "wrong"),
    lambda artifact: artifact.__setitem__("status", "partial"),
    lambda artifact: artifact["git_state_after"].__setitem__("git_dirty", True),
    lambda artifact: artifact["fcd1_acceptance"].__setitem__("top_k", 99),
    lambda artifact: artifact["safety_summary"].__setitem__("unauthorized_selected_count", 1),
    lambda artifact: artifact["safety_summary"].__setitem__("fcd1_ledger_complete_count", 9),
    lambda artifact: artifact["source_pool_stream_sha256"].__setitem__("raw_bm25", "0" * 64),
    lambda artifact: artifact["question_audits"][0]["product_retrieval"].__setitem__("fcd1_diagnostic_ledger_sha256", "0" * 64),
])
def test_schema_identity_acceptance_safety_and_receipt_drift_fail_closed(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutate) -> None:
    _configured(module, monkeypatch)
    artifact = _artifact()
    mutate(artifact)
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "drift.json"; path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40)


def test_ledger_view_receipt_drift_fails_even_when_audit_digest_is_rebound(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _configured(module, monkeypatch)
    artifact = _artifact()
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    ledger["view_top_50_sha256"]["raw_bm25"] = "0" * 64
    artifact["question_audits"][0]["product_retrieval"]["fcd1_diagnostic_ledger_sha256"] = _canonical_sha256(ledger)
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "ledger-drift.json"; path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40)


@pytest.mark.parametrize("mutate", [
    lambda ledger: ledger.__setitem__("authorization_sha256", "0" * 64),
    lambda ledger: ledger["fused_top_50"][0].__setitem__("final_rrf", 0.0),
    lambda ledger: ledger["fused_top_50"][0]["component_rank_receipts"][0].__setitem__("rank", 99),
    lambda ledger: ledger["view_full_order"]["raw_bm25"].__setitem__(50, ledger["view_full_order"]["raw_bm25"][49]),
    lambda ledger: ledger["view_order_sha256"].__setitem__("raw_bm25", "0" * 64),
    lambda ledger: ledger["fused_top_50"][0]["contributions"].__setitem__("raw_bm25", 0.0),
    lambda ledger: ledger["fused_top_50"][0]["contributions"].__setitem__("raw_bm25", math.nextafter(ledger["fused_top_50"][0]["contributions"]["raw_bm25"], math.inf)),
    lambda ledger: ledger["fused_top_50"][0].__setitem__("final_rrf", math.nextafter(ledger["fused_top_50"][0]["final_rrf"], math.inf)),
])
def test_rebound_audit_cannot_hide_authorization_or_fused_ledger_mutation(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mutate) -> None:
    _configured(module, monkeypatch)
    artifact = _artifact()
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    mutate(ledger)
    artifact["question_audits"][0]["product_retrieval"]["fcd1_diagnostic_ledger_sha256"] = _canonical_sha256(ledger)
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / "fused-drift.json"; path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40)


@pytest.mark.parametrize(("field", "value"), [("RRF_K", 59), ("FROZEN_WEIGHTS", {"raw_bm25": 3.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0})])
def test_frozen_rrf_parameters_cannot_drift(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: object) -> None:
    _configured(module, monkeypatch)
    artifact = _artifact()
    monkeypatch.setattr(module, field, value)
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / f"{field}.json"; path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40)


def _rebind_view_receipts_and_audit(artifact: dict) -> None:
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    ledger["view_top_50_sha256"] = {name: _canonical_sha256(rows) for name, rows in ledger["view_top_50"].items()}
    artifact["question_audits"][0]["product_retrieval"]["fcd1_diagnostic_ledger_sha256"] = _canonical_sha256(ledger)


@pytest.mark.parametrize("mode", ["score", "duplicate_order", "out_of_range_order"])
def test_rebound_view_receipts_cannot_hide_score_or_order_contract_drift(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str) -> None:
    _configured(module, monkeypatch)
    artifact = _artifact()
    ledger = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]
    if mode == "score":
        ledger["view_top_50"]["raw_bm25"][0]["score"] = -999.0
    else:
        target = "event:d-item-0"
        value = 1 if mode == "duplicate_order" else 99
        for rows in ledger["view_top_50"].values():
            for row in rows:
                if row["source_event_id"] == target:
                    row["ranking_key_order"] = value
        for row in ledger["fused_top_50"]:
            if row["source_event_id"] == target:
                row["ranking_key_order"] = value
    _rebind_view_receipts_and_audit(artifact)
    raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path = tmp_path / f"{mode}.json"; path.write_bytes(raw)
    with pytest.raises(ValueError):
        module.analyze(path, expected_artifact_sha256=hashlib.sha256(raw).hexdigest(), expected_git_head="a" * 40)


def test_atomic_publisher_rejects_mutation_and_preserves_input(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, artifact, raw = _analyze(module, monkeypatch, tmp_path)
    output = tmp_path / "report.json"
    bad_receipt = deepcopy(report); bad_receipt["input_receipt"]["artifact_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        module.atomic_json(output, bad_receipt, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()
    bad_analyzer = deepcopy(report); bad_analyzer["input_receipt"]["analyzer_implementation_sha256"] = "0" * 64
    with pytest.raises(ValueError):
        module.atomic_json(output, bad_analyzer, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()
    bad_provenance = deepcopy(report); bad_provenance["input_receipt"]["git_head"] = "a" * 40
    with pytest.raises(ValueError, match="input receipt shape"):
        module.atomic_json(output, bad_provenance, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()
    forged_producer = deepcopy(report); forged_producer["input_receipt"]["artifact_producer_git_head"] = "f" * 40
    with pytest.raises(ValueError, match="artifact producer Git head"):
        module.atomic_json(output, forged_producer, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()
    unchanged = artifact.read_bytes()
    with pytest.raises(ValueError):
        module.atomic_json(artifact, report, artifact_path=artifact, expected_artifact_bytes=raw)
    assert artifact.read_bytes() == unchanged
    forbidden = deepcopy(report); forbidden["nested"] = {"query": "x"}
    with pytest.raises(ValueError):
        module.atomic_json(output, forbidden, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()
    artifact.write_bytes(raw + b" ")
    with pytest.raises(RuntimeError):
        module.atomic_json(output, report, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()
    artifact.write_bytes(raw)
    module.atomic_json(output, report, artifact_path=artifact, expected_artifact_bytes=raw)
    assert json.loads(output.read_text(encoding="utf-8"))["verdict"] == "FUSION_SUPPORTED"


@pytest.mark.parametrize(("field", "value"), [
    ("git_head", "f" * 40), ("git_tree", "f" * 40),
    ("worktree_status_sha256", "f" * 64), ("commit_diff_sha256", "f" * 64),
    ("commit_diff_bytes", 1),
])
def test_atomic_publisher_rejects_any_analyzer_git_state_drift(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str, value: object) -> None:
    report, artifact, raw = _analyze(module, monkeypatch, tmp_path)
    changed = deepcopy(report["input_receipt"]["analyzer_git_state"])
    changed[field] = value
    monkeypatch.setattr(module, "_analyzer_git_state", lambda: changed)

    output = tmp_path / f"analyzer-{field}.json"
    with pytest.raises(RuntimeError, match="analyzer Git state changed"):
        module.atomic_json(output, report, artifact_path=artifact, expected_artifact_bytes=raw)
    assert not output.exists()


def test_staged_prefreeze_receipt_binds_product_stream_and_both_input_receipts(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from benchmarks import aerp2_product_six_view_locomo as harness
    artifact = _artifact()
    _configured(module, monkeypatch, artifact)
    source_repo = tmp_path / "source-repo"; source_repo.mkdir()
    artifact["safety_summary"] = {
        "expected_trace_count": 10, "trace_count": 10, "audit_complete_count": 10, "ranking_schema_complete_count": 10,
        "fcd1_ledger_complete_count": 10, "selected_match_count": 10, "trace_identity_complete_count": 10,
        "nonempty_selection_count": 10, "unauthorized_selected_count": 0,
        "legacy_mapping": {"count": 4, "unique_event_count": 4, "unique_dialog_count": 4, "mapping_sha256": "a" * 64, "valid": True, "one_to_one": True},
        "product_mapping": {"count": 4, "unique_event_count": 4, "unique_dialog_count": 4, "mapping_sha256": "b" * 64, "valid": True, "one_to_one": True},
        "lineage": {"count": 10, "forbidden_field_count": 0, "mapping_digest_count": 10}, "ranking_digest_count": 10,
        "checks": {name: True for name in harness.SAFETY_CHECKS}, "pass": True,
    }
    artifact["manifest_sha256"] = "7" * 64
    artifact["input_freeze"] = {"dataset": {"label": "dataset", "path": str(tmp_path / "dataset"), "sha256": "1" * 64, "bytes": 1}, "model_manifest_sha256": "2" * 64}
    artifact["historical_source"] = {"commit": harness.HISTORICAL_COMMIT, "files": {"benchmarks/locomo_bge_encoder.py": "9" * 64}}
    artifact["source_repo"] = {"pinned_commit": harness.HISTORICAL_COMMIT, "path": str(source_repo), "git_state": {"git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": "c" * 64, "commit_diff_sha256": "d" * 64, "commit_diff_bytes": 0}}
    artifact["adapter_implementation_sha256"] = "3" * 64
    artifact["encoder_identity"] = "4" * 64
    artifact["model_runtime"] = {"onnx_sha256": "5" * 64, "embedding_dimension": 3, "session_providers": ["CPUExecutionProvider"], "onnxruntime_version": "1"}
    def sentinel(role: str, native_mode: str, digest: str) -> dict:
        native = {"schema": harness.ENCODER_RECEIPT_SCHEMA, "manifest_sha256": "2" * 64, "mode": native_mode, "input_count": 1, "input_sha256": digest, "embedding_sha256": digest, "dtype": "float32-little-endian", "shape": [1, 3]}
        return {"schema": harness.STAGED_ENCODER_SENTINEL_PROJECTION_SCHEMA, "role": role, "native_receipt_sha256": harness._canonical(native), **{key: value for key, value in native.items() if key not in {"schema", "mode"}}}
    artifact["encoder_sentinels"] = {"input_encoder": sentinel("input", "query", "5" * 64), "passage_encoder": sentinel("passage", "passage", "6" * 64)}
    snapshot = {"schema": harness.ENCODER_RECEIPT_SCHEMA, "model_dir": "C:/model", "manifest_sha256": "2" * 64, "manifest_variant": "fp32", "files": [{"relative_path": "model.onnx", "sha256": "7" * 64, "stat": {"byte_count": 1, "device": 0, "inode": 0, "modified_ns": 0}}]}
    artifact["encoder_snapshot_pair"] = {"start": snapshot, "end": dict(snapshot)}
    streams = {
        "expected_questions": 10, "top_k": 2, "source_pool": 4,
        "product_top10_sha256": artifact["fcd1_acceptance"]["actual_product_top10_sha256"],
        "ranking_stream_sha256": {arm: artifact["ranking_stream_sha256"][arm] for arm in artifact["rankings_top10"] if arm != "historical_six_view"},
        "source_pool_stream_sha256": artifact["source_pool_stream_sha256"],
        "product_trace_stream_sha256": "0" * 64, "lineage_stream_sha256": "1" * 64,
        "authorization_mapping_stream_sha256": "2" * 64, "legacy_event_mapping_sha256": "3" * 64,
        "product_event_mapping_sha256": "4" * 64, "safety_summary_sha256": harness.stable_safety_receipt(artifact["safety_summary"]),
        "raw_component_parity": {"pass": True, "arms": {arm: {"top10_order_exact_questions": 10, "top50_order_exact_questions": 10, "top50_set_exact_questions": 10, "overlap_mean": 1.0, "overlap_min": 1.0} for arm in ("raw_bm25", "raw_dense")}},
    }
    streams["raw_component_parity"]["sha256"] = _canonical_sha256({"arms": streams["raw_component_parity"]["arms"]})
    streams["fresh_streams_sha256"] = _canonical_sha256(streams)
    # Event maps are UUID-bearing per-run receipts; their consistency anchor is
    # this artifact's safety summary, not the cross-run stable stream digests.
    artifact["event_dialog_mapping_sha256"] = {
        "legacy": artifact["safety_summary"]["legacy_mapping"]["mapping_sha256"],
        "product": artifact["safety_summary"]["product_mapping"]["mapping_sha256"],
    }
    prefreeze = {"schema": "aerp2-product-six-view-prefreeze-v1", "version": 1, "status": "complete", "manifest_sha256": "7" * 64, "phase_ledger": ["input_byte_freeze", "source_model_load", "sanitized_retrieval_construction", "fresh_streams_frozen", "prelabel_safety", "state_recheck", "atomic_publish_ready"], "stream_receipts": streams, "input_freeze": artifact["input_freeze"], "historical_source": artifact["historical_source"], "source_repo": artifact["source_repo"], "encoder_identity": artifact["encoder_identity"], "adapter_implementation_sha256": artifact["adapter_implementation_sha256"], "model_runtime": artifact["model_runtime"], "encoder_sentinels": artifact["encoder_sentinels"], "encoder_snapshot_pair": artifact["encoder_snapshot_pair"], "safety_summary": artifact["safety_summary"], "claim_boundary": "bounded", "git_state_before": artifact["git_state_before"], "git_state_after": artifact["git_state_after"], "implementation_sha256": {"harness": hashlib.sha256((module.ROOT / "benchmarks" / "aerp2_product_six_view_locomo.py").read_bytes()).hexdigest(), "ranker": hashlib.sha256((module.ROOT / "mempalace_rpg" / "retrieval.py").read_bytes()).hexdigest(), "prefreeze_cli": hashlib.sha256((module.ROOT / "benchmarks" / "aerp2_product_six_view_prefreeze.py").read_bytes()).hexdigest()}}
    prefreeze_raw = json.dumps(prefreeze, sort_keys=True, separators=(",", ":")).encode("utf-8")
    prefreeze_sha = hashlib.sha256(prefreeze_raw).hexdigest()
    artifact.pop("fcd1_acceptance")
    artifact["fcd1_prefreeze_consumption"] = {
        "schema": "aerp3-fcd1-prefreeze-consumption-v1", "prefreeze_sha256": prefreeze_sha,
        "prefrozen_product_top10_sha256": streams["product_top10_sha256"], "current_product_top10_sha256": streams["product_top10_sha256"],
        "fresh_ranking_stream_sha256": streams["ranking_stream_sha256"], "fresh_source_pool_stream_sha256": streams["source_pool_stream_sha256"],
        "raw_component_parity_sha256": streams["raw_component_parity"]["sha256"], "raw_component_parity_pass": True,
        **{name: streams[name] for name in ("product_trace_stream_sha256", "lineage_stream_sha256", "authorization_mapping_stream_sha256", "legacy_event_mapping_sha256", "product_event_mapping_sha256", "safety_summary_sha256", "fresh_streams_sha256")},
    }
    artifact_raw = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode("utf-8")
    artifact_path = tmp_path / "staged-artifact.json"; artifact_path.write_bytes(artifact_raw)
    prefreeze_path = tmp_path / "prefreeze.json"; prefreeze_path.write_bytes(prefreeze_raw)

    report = module.analyze(artifact_path, expected_artifact_sha256=hashlib.sha256(artifact_raw).hexdigest(), expected_git_head="a" * 40, prefreeze_receipt_path=prefreeze_path, expected_prefreeze_sha256=prefreeze_sha)
    assert report["input_receipt"]["prefreeze_sha256"] == prefreeze_sha
    assert report["input_receipt"]["artifact_producer_git_head"] == "a" * 40
    assert report["input_receipt"]["analyzer_git_state"]["git_head"] == "b" * 40
    raw_map_drift = json.loads(artifact_raw)
    raw_map_drift["event_dialog_mapping_sha256"]["legacy"] = "0" * 64
    raw_map_drift_bytes = json.dumps(raw_map_drift, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw_map_drift_path = tmp_path / "raw-map-drift.json"; raw_map_drift_path.write_bytes(raw_map_drift_bytes)
    with pytest.raises(ValueError, match="event mapping is inconsistent with artifact safety"):
        module.analyze(raw_map_drift_path, expected_artifact_sha256=hashlib.sha256(raw_map_drift_bytes).hexdigest(), expected_git_head="a" * 40, prefreeze_receipt_path=prefreeze_path, expected_prefreeze_sha256=prefreeze_sha)
    stable_consumption_drift = json.loads(artifact_raw)
    stable_consumption_drift["fcd1_prefreeze_consumption"]["legacy_event_mapping_sha256"] = "0" * 64
    stable_consumption_drift_bytes = json.dumps(stable_consumption_drift, sort_keys=True, separators=(",", ":")).encode("utf-8")
    stable_consumption_drift_path = tmp_path / "stable-map-drift.json"; stable_consumption_drift_path.write_bytes(stable_consumption_drift_bytes)
    with pytest.raises(ValueError, match="prefreeze consumption mismatch"):
        module.analyze(stable_consumption_drift_path, expected_artifact_sha256=hashlib.sha256(stable_consumption_drift_bytes).hexdigest(), expected_git_head="a" * 40, prefreeze_receipt_path=prefreeze_path, expected_prefreeze_sha256=prefreeze_sha)
    output = tmp_path / "staged-report.json"
    with pytest.raises(ValueError, match="external"):
        module.atomic_json(source_repo / "forbidden-report.json", report, artifact_path=artifact_path, expected_artifact_bytes=artifact_raw, prefreeze_path=prefreeze_path, expected_prefreeze_bytes=prefreeze_raw)
    assert not (source_repo / "forbidden-report.json").exists()
    prefreeze_path.write_bytes(prefreeze_raw + b" ")
    with pytest.raises(RuntimeError):
        module.atomic_json(output, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_raw, prefreeze_path=prefreeze_path, expected_prefreeze_bytes=prefreeze_raw)
    assert not output.exists()
    prefreeze_path.write_bytes(prefreeze_raw)
    source_drift = tmp_path / "source-drift.json"
    monkeypatch.setattr(module, "_clean_git_head", lambda path: "b" * 40 if Path(path).resolve() == source_repo.resolve() else "a" * 40)
    with pytest.raises(RuntimeError, match="source Git head"):
        module.atomic_json(source_drift, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_raw, prefreeze_path=prefreeze_path, expected_prefreeze_bytes=prefreeze_raw)
    assert not source_drift.exists()
    analyzer_after_source = tmp_path / "analyzer-after-source.json"
    analyzer_drift = deepcopy(report["input_receipt"]["analyzer_git_state"])
    analyzer_drift["git_tree"] = "f" * 40
    source_checked = {"value": False}
    def source_check_then_drift(path: Path | str) -> str:
        if Path(path).resolve() == source_repo.resolve():
            source_checked["value"] = True
        return "a" * 40
    monkeypatch.setattr(module, "_clean_git_head", source_check_then_drift)
    monkeypatch.setattr(module, "_analyzer_git_state", lambda: analyzer_drift if source_checked["value"] else report["input_receipt"]["analyzer_git_state"])
    with pytest.raises(RuntimeError, match="analyzer Git state changed"):
        module.atomic_json(analyzer_after_source, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_raw, prefreeze_path=prefreeze_path, expected_prefreeze_bytes=prefreeze_raw)
    assert source_checked["value"] is True
    assert not analyzer_after_source.exists()
    monkeypatch.setattr(module, "_clean_git_head", lambda _path: "a" * 40)
    monkeypatch.setattr(module, "_analyzer_git_state", lambda: dict(report["input_receipt"]["analyzer_git_state"]))
    module.atomic_json(output, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_raw, prefreeze_path=prefreeze_path, expected_prefreeze_bytes=prefreeze_raw)
    assert output.exists()
    with pytest.raises(ValueError):
        module.analyze(artifact_path, expected_artifact_sha256=hashlib.sha256(artifact_raw).hexdigest(), expected_git_head="a" * 40, prefreeze_receipt_path=prefreeze_path)


def test_fcd2_publisher_never_clobbers_existing_output(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, artifact, raw = _analyze(module, monkeypatch, tmp_path)
    output = tmp_path / "existing.json"; output.write_text("immutable", encoding="utf-8")
    with pytest.raises(ValueError, match="distinct"):
        module.atomic_json(output, report, artifact_path=artifact, expected_artifact_bytes=raw)
    assert output.read_text(encoding="utf-8") == "immutable"


@pytest.fixture
def module():
    from benchmarks import aerp3_fcd2_causal_ablation
    return aerp3_fcd2_causal_ablation
