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
    assert module.RRF_K == 60
    assert module.FROZEN_WEIGHTS == {"raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0}
    assert module.P1_MIN_DELTA == 0.05
    assert module.P2_MIN_DELTA == -0.01
    assert module.STRICT_MAJORITY_MULTIPLIER == 2
    assert module.EXPECTED_CONVERSATIONS == 10
    assert module.MIN_RECOVERY_CONVERSATIONS == 8
    assert module.PRODUCT_TOP10_SHA256 == "64007282069621bb3e603598938993ebe0907e8e84ebaa65394741ab618e5441"


def test_preregistered_delta_comparators_include_equality_and_reject_one_step_below(module) -> None:
    assert module._p1_pass({"overall": 0.05, "hard": 0.05, "adversarial": None})
    assert module._p2_pass({"overall": -0.01, "hard": -0.01, "adversarial": None})
    assert not module._p1_pass({"overall": math.nextafter(0.05, -math.inf), "hard": 0.05, "adversarial": None})
    assert not module._p2_pass({"overall": math.nextafter(-0.01, -math.inf), "hard": -0.01, "adversarial": None})


def _row(identifier: str, rank: int) -> dict[str, object]:
    return {
        "source_event_id": f"event:{identifier}",
        "ranking_key_sha256": _sha(identifier),
        # The producer order is the global opaque ranking-key lexical order:
        # b < d < g < z, continuously numbered across the full universe.
        "ranking_key_order": {"b": 1, "d": 2, "g": 3, "z": 4}[identifier[0]],
        "rank": rank,
        "score": float(100 - rank),
    }


def _ledger(views: dict[str, list[dict[str, object]]]) -> dict:
    weights = {"raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0}
    order_receipts = {name: _canonical_sha256([row["ranking_key_sha256"] for row in rows]) for name, rows in views.items()}
    full_orders = {name: [row["source_event_id"] for row in rows] for name, rows in views.items()}
    policy = "4" * 64
    event_rows = {row["source_event_id"]: row for row in views["raw_bm25"]}
    checkpoint_rows = {row["source_event_id"]: row for row in views["checkpoint_dense"]}
    groups = []
    for event, row in event_rows.items():
        checkpoint = _sha(f"checkpoint:{event}")
        groups.append({
            "group_id": "group:" + _canonical_sha256([checkpoint, policy]),
            "checkpoint_sha256": checkpoint,
            "policy_sha256": policy,
            "checkpoint_score": checkpoint_rows[event]["score"],
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
        contributions = {name: weights[name] / (60 + component_ranks[name]) for name in views}
        fused.append({
            "source_event_id": event,
            "ranking_key_sha256": row["ranking_key_sha256"],
            "ranking_key_order": row["ranking_key_order"],
            "rank": 0,
            "final_rrf": sum(contributions.values()),
            "component_ranks": component_ranks,
            "component_rank_receipts": [{"view": name, "view_order_sha256": order_receipts[name], "ranking_key_sha256": row["ranking_key_sha256"], "rank": component_ranks[name]} for name in views],
            "contributions": contributions,
        })
    fused.sort(key=lambda row: (-float(row["final_rrf"]), row["ranking_key_order"]))
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
        traces[item] = {"retrieval_ranking": {"fcd1_diagnostic_ledger": _ledger(views)}}
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


def test_duplicate_gold_and_unresolved_are_counted_without_id_leakage(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, duplicate_gold=True)

    assert report["verdict"] == "FUSION_SUPPORTED"
    assert report["stages"]["oracle"]["overall"] == pytest.approx((2 / 3 + 9) / 10)
    first_conversation = _sha("conversation-0")
    assert report["stages"]["hashed_conversation_recovery"][first_conversation] == {"recoverable": 2, "nonrecoverable": 0}
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
    first = artifact["product_traces"]["item-0"]["retrieval_ranking"]["fcd1_diagnostic_ledger"]["view_top_50"]
    expected = [_sha("d-item-0"), _sha("g-item-0"), _sha("b-item-0"), _sha("z-item-0")]

    assert module._rrf(first) == expected
    assert report["fusion_semantics_parity"] == {"status": "complete", "expected_questions": 10, "top10_order_exact_questions": 10}
    assert {view: metric["overall"] for view, metric in report["stages"]["ablations"]["add_view"].items()} == {
        "raw_bm25": 1.0, "raw_dense": 1.0, "observation_bm25": 1.0,
        "observation_dense": 1.0, "checkpoint_dense": 0.0, "combo_dense": 0.0,
    }
    assert {view: metric["overall"] for view, metric in report["stages"]["ablations"]["leave_one_out"].items()} == {
        "raw_bm25": 0.0, "raw_dense": 0.0, "observation_bm25": 1.0,
        "observation_dense": 1.0, "checkpoint_dense": 1.0, "combo_dense": 0.0,
    }


def test_fusion_semantics_failure_stops_before_later_label_stages(module, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden_gold(_question):
        raise AssertionError("fusion-semantic failure must precede label access")

    monkeypatch.setattr(module, "_gold", forbidden_gold)
    report, _path, _raw = _analyze(module, monkeypatch, tmp_path, "semantic")

    assert report["verdict"] == "FUSION_SEMANTICS_PARITY_FAILED"
    assert report["later_stages"] == "not_run_due_to_fusion_semantics_parity"
    assert report["fusion_semantics_parity"]["top10_order_exact_questions"] == 0


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
    assert report["stages"]["gates"]["recovery_conversation_pass"] is conversation_pass


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


@pytest.fixture
def module():
    from benchmarks import aerp3_fcd2_causal_ablation
    return aerp3_fcd2_causal_ablation
