from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from benchmarks import aerp2_product_six_view_locomo as harness
from mempalace_rpg import RpgMemoryKernel


def _conversation() -> dict:
    # Real RetrievalBundle payloads contain only query/sessions. Conversation
    # identity lives in RetrievalBundle.item_to_conversation.
    return {"sessions": [{"opaque_session_id": "session-1", "dialogs": [{"opaque_dialog_id": "dialog-1", "speaker": "Ada", "date": "2024-01-01", "caption": "arrival", "text": "Hello answer word", "observation": "FORBIDDEN OBSERVATION", "answer": "excluded"}]}]}


def _safety() -> dict:
    return {"checks": {name: True for name in harness.SAFETY_CHECKS}, "pass": True}


def _thresholds() -> dict:
    return {"p1_delta": .05, "p2_delta": -.01, "hard_categories": [1, 2]}


def _fcd1_authorization_mapping_sha256(mapping: dict[str, str]) -> str:
    policy_sha256 = harness._canonical(["public-policy"])
    rows = [{
        "ranking_key_sha256": harness._sha256(dialog_id.encode("utf-8")),
        "checkpoint_sha256": harness._sha256(f"checkpoint:{event_id}".encode("utf-8")),
        "policy_sha256": policy_sha256,
    } for event_id, dialog_id in mapping.items()]
    return harness._canonical(sorted(rows, key=lambda row: row["ranking_key_sha256"]))


def _fcd1_ranking(mapping: dict[str, str], selected_ids: list[str] | None = None) -> dict:
    identifiers = list(mapping)
    selected_ids = identifiers[:1] if selected_ids is None else selected_ids
    ranking_key_order = {event_id: rank for rank, event_id in enumerate(sorted(mapping, key=lambda event_id: mapping[event_id]), start=1)}
    ranking_key_sha256 = {event_id: harness._sha256(mapping[event_id].encode("utf-8")) for event_id in identifiers}
    view_order_sha256 = {name: harness._canonical([ranking_key_sha256[event_id] for event_id in identifiers]) for name in harness.FROZEN_SIX_VIEW_WEIGHTS}
    component_ranks = {
        event_id: {name: rank for name in harness.FROZEN_SIX_VIEW_WEIGHTS}
        for rank, event_id in enumerate(identifiers, start=1)
    }
    fused = []
    for rank, event_id in enumerate(identifiers, start=1):
        contributions = {name: weight / (60 + rank) for name, weight in harness.FROZEN_SIX_VIEW_WEIGHTS.items()}
        fused.append({
            "source_event_id": event_id,
            "ranking_key_sha256": ranking_key_sha256[event_id],
            "ranking_key_order": ranking_key_order[event_id],
            "rank": rank,
            "final_rrf": sum(contributions.values()),
            "component_ranks": component_ranks[event_id],
            "component_rank_receipts": [{"view": name, "view_order_sha256": view_order_sha256[name], "ranking_key_sha256": ranking_key_sha256[event_id], "rank": rank} for name in harness.FROZEN_SIX_VIEW_WEIGHTS],
            "contributions": contributions,
        })
    by_id = {row["source_event_id"]: row for row in fused}
    selected = []
    for event_id in selected_ids:
        row = dict(by_id[event_id])
        for name in ("rank", "ranking_key_order", "component_rank_receipts"):
            row.pop(name)
        selected.append(row)
    input_sha256 = "b" * 64
    policy_sha256 = harness._canonical(["public-policy"])
    authorization_rows = [{"ranking_key_sha256": ranking_key_sha256[event_id], "policy_sha256": policy_sha256} for event_id in identifiers]
    view_top_50 = {
        name: [{
            "source_event_id": event_id,
            "ranking_key_sha256": ranking_key_sha256[event_id],
            "ranking_key_order": ranking_key_order[event_id],
            "rank": rank,
            "score": 1.0 / rank,
        } for rank, event_id in enumerate(identifiers[:50], start=1)]
        for name in harness.FROZEN_SIX_VIEW_WEIGHTS
    }
    ledger = {
        "schema": harness.FCD1_LEDGER_SCHEMA,
        "input_sha256": input_sha256,
        "authorization_sha256": harness._canonical(sorted(authorization_rows, key=lambda row: row["ranking_key_sha256"])),
        "view_full_order": {name: list(identifiers) for name in harness.FROZEN_SIX_VIEW_WEIGHTS},
        "view_order_sha256": view_order_sha256,
        "view_top_50": view_top_50,
        "view_top_50_sha256": {name: harness._canonical(rows) for name, rows in view_top_50.items()},
        "fused_top_50": fused[:50],
        "checkpoint_tie_group_semantics": "checkpoint_policy_rollup",
        "checkpoint_tie_groups": [{
            "group_id": "group:" + harness._canonical([harness._sha256(f"checkpoint:{event_id}".encode("utf-8")), policy_sha256]),
            "checkpoint_sha256": harness._sha256(f"checkpoint:{event_id}".encode("utf-8")),
            "policy_sha256": policy_sha256,
            "checkpoint_score": 1.0 / rank,
            "member_count": 1,
            "chronological_members": [{
                "source_event_id": event_id,
                "ranking_key_sha256": ranking_key_sha256[event_id],
            }],
        } for rank, event_id in enumerate(identifiers, start=1)],
    }
    return {
        "schema": "aerp2-product-six-view-v1", "query_sha256": "a" * 64,
        "input_sha256": input_sha256,
        "view_digests": {name: "c" * 64 for name in harness.FROZEN_SIX_VIEW_WEIGHTS},
        "encoder_identity": "encoder", "weights": dict(harness.FROZEN_SIX_VIEW_WEIGHTS),
        "rrf_k": 60, "selected": selected, "fcd1_diagnostic_ledger": ledger,
    }


def test_seed_uses_only_sanitized_dialog_fields_and_checkpoint_mapping(tmp_path):
    with RpgMemoryKernel(db_path=str(tmp_path / "locomo.sqlite3")) as kernel:
        event_to_dialog, audit = harness.seed_sanitized_conversation(kernel, _conversation(), conversation_id="conv-1")
        row = kernel._conn().execute("SELECT summary, source_span, payload_json FROM scene_event").fetchone()

    assert event_to_dialog and list(event_to_dialog.values()) == ["dialog-1"]
    assert row["summary"] == row["source_span"] == "speaker: Ada\ndate: 2024-01-01\ncaption: arrival\ntext: Hello answer word"
    assert json.loads(row["payload_json"]) == {"retrieval_checkpoint_id": "conv-1/session-1", "retrieval_ranking_key": "dialog-1"}
    assert audit["annotation_lineage"]["forbidden_field_counts"] == {field: 0 for field in harness.FORBIDDEN_ANNOTATION_FIELDS}
    assert len(audit["annotation_lineage"]["authorization_mapping_sha256"]) == 64
    assert "FORBIDDEN" not in json.dumps(audit)
    assert len(audit["ranker_texts_sha256"]) == 64


def test_lineage_and_r0_stream_mismatches_fail_closed():
    with pytest.raises(ValueError, match="annotation leakage"):
        harness.validate_annotation_lineage({"seed_ledger": {"allowed_source_fields": ["speaker", "date", "caption", "text", "session_membership"], "forbidden_field_counts": {**{field: 0 for field in harness.FORBIDDEN_ANNOTATION_FIELDS}, "answer": 1}, "dialog_count": 1, "checkpoint_ranking_mapping_sha256": "x"}})
    with pytest.raises(RuntimeError, match="raw BM25 ranking mismatch"):
        harness.validate_r0_raw_bm25({("c", "q"): ["a"]}, {("c", "q"): ["b"]})
    with pytest.raises(ValueError, match="historical join mismatch"):
        harness.validate_historical_join(
            [{"conversation_id": "c", "question_id": "q", "category": 1, "gold": ["a"], "denominator": 1, "corpus": ["a"]}],
            {("c", "q"): {"category": 2, "gold": ["a"], "denominator": 1, "corpus": ["a"]}},
        )


def test_r0_requires_full_top50_and_historical_source_is_strict():
    expected = [f"dialog-{index:02d}" for index in range(50)]
    actual = list(expected)
    actual[10] = "different-rank-eleven"
    with pytest.raises(RuntimeError, match="raw BM25 ranking mismatch"):
        harness.validate_r0_raw_bm25({("c", "q"): expected}, {("c", "q"): actual})
    with pytest.raises(ValueError, match="source pool is incomplete"):
        harness.extract_historical_ranking([], {"dialog-0"})
    with pytest.raises(ValueError, match="finite"):
        harness.extract_historical_ranking(
            [{"opaque_dialog_id": f"dialog-{index}", "score": float("nan")} for index in range(50)],
            {f"dialog-{index}" for index in range(50)},
        )


def test_official_raw_and_historical_aggregate_anchors_fail_closed():
    aggregate = {
        "raw_bm25": {"official_exact": {"overall": {"question_macro_recall_at_10": 0.5}, "hard_categories_1_2": {"question_macro_recall_at_10": 0.4}}},
        "historical_six_view": {"official_exact": {"overall": {"question_macro_recall_at_10": 0.7}, "hard_categories_1_2": {"question_macro_recall_at_10": 0.6}}},
    }
    anchors = {"raw_bm25": {"overall": 0.5, "hard": 0.4}, "historical_six_view": {"overall": 0.7, "hard": 0.6}}
    harness.validate_official_aggregate_anchors(aggregate, anchors)
    aggregate["historical_six_view"]["official_exact"]["hard_categories_1_2"]["question_macro_recall_at_10"] = 0.6000000000000011
    with pytest.raises(RuntimeError, match="historical SixView"):
        harness.validate_official_aggregate_anchors(aggregate, anchors)


def test_real_scorer_item_shaped_contract_join_is_strict():
    item = SimpleNamespace(opaque_conversation_id="c", category=1, category_name="hard", official_exact=SimpleNamespace(resolved_opaque_dialog_ids=("d",), source_evidence_item_count=1, unresolved_evidence_item_count=0), normalized_repaired=SimpleNamespace(gold_opaque_dialog_ids=("d",), unique_dialog_denominator=1, unresolved_evidence_item_count=0), corpus_opaque_dialog_ids=("d",))
    scorer = SimpleNamespace(scorer_items={"q": item}, split=SimpleNamespace(opaque_conversation_id_to_split={"c": "test"}))
    splits = harness.scorer_bundle_conversation_splits(scorer)
    historical = [{"opaque_conversation_id": "c", "opaque_question_id": "q", "split": "test", "scorer": {"category": 1, "category_name": "hard", "corpus_dialog_count": 1, "evidence_semantics": {"official_exact": {"resolved_opaque_dialog_ids": ["d"], "evidence_item_count": 1, "unresolved_evidence_item_count": 0}, "normalized_repaired": {"resolved_opaque_dialog_ids": ["d"], "evidence_item_count": 1, "unresolved_evidence_item_count": 0}}}}]
    digests = harness.validate_scorer_contract({"q": item}, historical, splits)
    assert len(digests["composite_ids_sha256"]) == 64
    historical[0]["scorer"]["evidence_semantics"]["official_exact"]["evidence_item_count"] = 2
    with pytest.raises(ValueError, match="scorer contract"):
        harness.validate_scorer_contract({"q": item}, historical, splits)
    with pytest.raises(ValueError, match="split"):
        harness.scorer_contract_records({"q": item})


def test_full_corpus_contract_and_manifest_hash_fail_closed(tmp_path):
    item = SimpleNamespace(opaque_conversation_id="c", corpus_opaque_dialog_ids=("d1", "d2"))
    historical = [{"opaque_conversation_id": "c", "corpus_dialog_count": 2, "opaque_dialog_ids": ["d1", "d2"]}]
    digest = harness.validate_corpus_contract({"q": item}, historical)
    assert len(digest["corpus_contract_sha256"]) == 64
    historical[0]["opaque_dialog_ids"][1] = "other"
    with pytest.raises(ValueError, match="full corpus"):
        harness.validate_corpus_contract({"q": item}, historical)
    copied = tmp_path / "manifest.json"; copied.write_bytes(harness.MANIFEST_PATH.read_bytes() + b" ")
    with pytest.raises(ValueError, match="SHA256"):
        harness.load_manifest(copied)


def test_strict_sixview_trace_rejects_view_and_ranking_key_corruption():
    mapping = {"e": "dialog"}
    ranking = _fcd1_ranking(mapping)
    trace = {"retrieval_ranking": ranking, "selected_evidence_ids": ["e"], "authorized_candidate_ids": ["e"], "denied_partitions": [], "candidate_generation": {"candidate_count": 1}, "candidates": [{"source_event_id": "e", "decision": "allow"}], "returned_spans": []}
    lineage = [{"conversation_id": "c", "seed_ledger": {"forbidden_field_counts": {field: 0 for field in harness.FORBIDDEN_ANNOTATION_FIELDS}, "checkpoint_ranking_mapping_sha256": "d" * 64, "authorization_mapping_sha256": _fcd1_authorization_mapping_sha256(mapping), "ranker_texts_sha256": "e" * 64}}]
    summary = harness.summarize_product_safety(traces={"q": trace}, product_rankings={"q": ["dialog"]}, product_event_maps={"c": mapping}, legacy_event_maps={"c": mapping}, question_conversations={"q": "c"}, lineage=lineage, expected_questions=1, expected_dialogs=1)
    assert summary["checks"]["ranking_schema"]
    assert summary["checks"]["fcd1_ledger"]
    ranking["view_digests"].pop("combo_dense")
    assert not harness.summarize_product_safety(traces={"q": trace}, product_rankings={"q": ["dialog"]}, product_event_maps={"c": mapping}, legacy_event_maps={"c": mapping}, question_conversations={"q": "c"}, lineage=lineage, expected_questions=1, expected_dialogs=1)["checks"]["ranking_schema"]


def test_fcd1_ledger_replay_rejects_score_identity_and_group_corruption():
    mapping = {"e": "dialog", "other": "other-dialog"}
    ranking = _fcd1_ranking(mapping)
    receipt = harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    assert receipt["schema"] == harness.FCD1_LEDGER_SCHEMA and len(receipt["sha256"]) == 64
    assert harness.FCD1_REFERENCE_PRODUCT_TOP10_SHA256 == "64007282069621bb3e603598938993ebe0907e8e84ebaa65394741ab618e5441"

    ranking["fcd1_diagnostic_ledger"]["fused_top_50"][0]["contributions"]["raw_bm25"] += .01
    with pytest.raises(ValueError, match="contributions"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    ranking = _fcd1_ranking(mapping)
    ranking["fcd1_diagnostic_ledger"]["view_top_50"]["raw_dense"][0]["ranking_key_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="identity"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    ranking = _fcd1_ranking(mapping)
    ranking["fcd1_diagnostic_ledger"]["checkpoint_tie_groups"].pop()
    with pytest.raises(ValueError, match="partition"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    ranking = _fcd1_ranking(mapping)
    ranking["fcd1_diagnostic_ledger"]["authorization_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="authorization"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    ranking = _fcd1_ranking(mapping)
    ranking["fcd1_diagnostic_ledger"]["view_top_50"]["raw_bm25"][0]["score"] = .75
    with pytest.raises(ValueError, match="score receipt"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    ranking = _fcd1_ranking(mapping)
    ranking["fcd1_diagnostic_ledger"]["fused_top_50"][:2] = reversed(ranking["fcd1_diagnostic_ledger"]["fused_top_50"][:2])
    with pytest.raises(ValueError, match="identity or rank|fused ordering"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))
    ranking = _fcd1_ranking(mapping)
    ranking["fcd1_diagnostic_ledger"]["fused_top_50"][0]["component_ranks"]["raw_dense"] = 2
    with pytest.raises(ValueError, match="component-rank receipts"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))


def test_fcd1_ledger_rejects_coordinated_receipt_and_top50_boundary_drift():
    mapping = {f"event-{index:03d}": f"dialog-{index:03d}" for index in range(51)}

    ranking = _fcd1_ranking(mapping)
    ledger = ranking["fcd1_diagnostic_ledger"]
    ledger["view_order_sha256"]["raw_dense"] = "0" * 64
    for row in ledger["fused_top_50"]:
        next(receipt for receipt in row["component_rank_receipts"] if receipt["view"] == "raw_dense")["view_order_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="full view-order receipt"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))

    ranking = _fcd1_ranking(mapping)
    ledger = ranking["fcd1_diagnostic_ledger"]
    ledger["view_full_order"]["raw_bm25"][49:51] = reversed(ledger["view_full_order"]["raw_bm25"][49:51])
    ledger["view_order_sha256"]["raw_bm25"] = harness._canonical([harness._sha256(mapping[event_id].encode("utf-8")) for event_id in ledger["view_full_order"]["raw_bm25"]])
    for row in ledger["fused_top_50"]:
        next(receipt for receipt in row["component_rank_receipts"] if receipt["view"] == "raw_bm25")["view_order_sha256"] = ledger["view_order_sha256"]["raw_bm25"]
    with pytest.raises(ValueError, match="full-order prefix"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))

    ranking = _fcd1_ranking(mapping)
    ledger = ranking["fcd1_diagnostic_ledger"]
    boundary = ledger["fused_top_50"][49]
    boundary["component_ranks"]["raw_dense"] = 51
    next(receipt for receipt in boundary["component_rank_receipts"] if receipt["view"] == "raw_dense")["rank"] = 51
    with pytest.raises(ValueError, match="full view order"):
        harness.validate_fcd1_ledger(ranking, mapping, _fcd1_authorization_mapping_sha256(mapping))

    ranking = _fcd1_ranking({"e": "dialog"})
    ledger = ranking["fcd1_diagnostic_ledger"]
    group = ledger["checkpoint_tie_groups"][0]
    group["policy_sha256"] = "0" * 64
    group["group_id"] = "group:" + harness._canonical([group["checkpoint_sha256"], group["policy_sha256"]])
    authorization_rows = [{"ranking_key_sha256": member["ranking_key_sha256"], "policy_sha256": group["policy_sha256"]} for member in group["chronological_members"]]
    ledger["authorization_sha256"] = harness._canonical(sorted(authorization_rows, key=lambda row: row["ranking_key_sha256"]))
    with pytest.raises(ValueError, match="pre-ranking seed ledger"):
        harness.validate_fcd1_ledger(ranking, {"e": "dialog"}, _fcd1_authorization_mapping_sha256({"e": "dialog"}))


def test_trace_identity_sets_and_empty_selection_fail_closed():
    mapping = {"e": "dialog", "other": "other-dialog"}
    ranking = _fcd1_ranking(mapping)
    trace = {"retrieval_ranking": ranking, "selected_evidence_ids": ["e"], "authorized_candidate_ids": list(mapping), "denied_partitions": [], "candidate_generation": {"candidate_count": 2}, "candidates": [{"source_event_id": "e", "decision": "allow"}], "returned_spans": []}
    lineage = [{"conversation_id": "c", "seed_ledger": {"forbidden_field_counts": {field: 0 for field in harness.FORBIDDEN_ANNOTATION_FIELDS}, "checkpoint_ranking_mapping_sha256": "d" * 64, "authorization_mapping_sha256": _fcd1_authorization_mapping_sha256(mapping), "ranker_texts_sha256": "e" * 64}}]
    summary = harness.summarize_product_safety(traces={"q": trace}, product_rankings={"q": ["dialog"]}, product_event_maps={"c": mapping}, legacy_event_maps={"c": mapping}, question_conversations={"q": "c"}, lineage=lineage, expected_questions=1, expected_dialogs=2)
    assert summary["checks"]["trace_identity_sets"]
    trace["candidates"] = [{"source_event_id": "other", "decision": "allow"}]
    summary = harness.summarize_product_safety(traces={"q": trace}, product_rankings={"q": ["dialog"]}, product_event_maps={"c": mapping}, legacy_event_maps={"c": mapping}, question_conversations={"q": "c"}, lineage=lineage, expected_questions=1, expected_dialogs=2)
    assert not summary["checks"]["trace_identity_sets"]
    trace["candidates"] = []; trace["selected_evidence_ids"] = []; ranking["selected"] = []
    summary = harness.summarize_product_safety(traces={"q": trace}, product_rankings={"q": []}, product_event_maps={"c": mapping}, legacy_event_maps={"c": mapping}, question_conversations={"q": "c"}, lineage=lineage, expected_questions=1, expected_dialogs=2)
    assert not summary["checks"]["nonempty_selection"]


def test_encoder_runtime_provider_receipt_is_strict(monkeypatch):
    class Session:
        def __init__(self, providers): self.providers = providers
        def get_providers(self): return self.providers
    encoder = SimpleNamespace(_session=Session(["CPUExecutionProvider"]))
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(__version__="1.20.0"))
    receipt = harness.encoder_runtime_provider_receipt(encoder, {"session_providers": ["CPUExecutionProvider"]})
    assert receipt == {"session_providers": ["CPUExecutionProvider"], "onnxruntime_version": "1.20.0"}
    encoder._session = Session(["CUDAExecutionProvider"])
    with pytest.raises(RuntimeError, match="providers"):
        harness.encoder_runtime_provider_receipt(encoder, {"session_providers": ["CPUExecutionProvider"]})


def test_metadata_module_names_are_restored_on_success_and_exception(monkeypatch):
    workspace = harness.ROOT.parent.parent
    dataset = workspace / "data" / "benchmark-data" / "locomo" / "main" / "locomo10.json"
    artifact = workspace / "artifacts" / "benchmark-runs" / "locomo_story_dense_v2_full_run1.json"
    source = harness.ROOT.parent / "mempalace"
    sentinel = object(); original = sys.modules.get("locomo_story_protocol"); sys.modules["locomo_story_protocol"] = sentinel
    try:
        harness.run_metadata_validation(dataset_path=dataset, artifact_path=artifact, source_repo=source, enforce_expected=True)
        assert sys.modules["locomo_story_protocol"] is sentinel
        monkeypatch.setattr(harness, "validate_corpus_contract", lambda *_: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            harness.run_metadata_validation(dataset_path=dataset, artifact_path=artifact, source_repo=source, enforce_expected=True)
        assert sys.modules["locomo_story_protocol"] is sentinel
    finally:
        if original is None: sys.modules.pop("locomo_story_protocol", None)
        else: sys.modules["locomo_story_protocol"] = original


def test_pinned_scorer_digests_cannot_be_advisory():
    result = {"composite_ids_sha256": "a" * 64, "scorer_contract_sha256": "b" * 64}
    with pytest.raises(ValueError, match="must be pinned"):
        harness._enforce_scorer_contract_digests(result, {"composite_ids_sha256": None, "scorer_contract_sha256": None}, enforce_expected=True)
    with pytest.raises(ValueError, match="digest mismatch"):
        harness._enforce_scorer_contract_digests(result, {"composite_ids_sha256": "c" * 64, "scorer_contract_sha256": "b" * 64}, enforce_expected=True)
    harness._enforce_scorer_contract_digests(result, result, enforce_expected=True)


def test_join_bootstrap_gates_and_atomic_output_are_deterministic(tmp_path):
    rows = [{"conversation_id": "a", "category": 1, "product": 1.0, "strong": 0.0, "historical": 1.0}, {"conversation_id": "b", "category": 3, "product": 1.0, "strong": 0.0, "historical": 1.0}]
    first = harness.paired_conversation_bootstrap(rows, "product", "strong", hard_only=False, seed=7, resamples=100)
    second = harness.paired_conversation_bootstrap(rows, "product", "strong", hard_only=False, seed=7, resamples=100)
    assert first == second and first["lower_95"] == 1.0
    gates = harness.evaluate_release_gates(rows, bootstrap_seed=7, bootstrap_resamples=100, thresholds=_thresholds(), safety_summary=_safety())
    assert gates["p1_pass"] and gates["p2_pass"] and gates["release_pass"]
    output = tmp_path / "outside" / "report.json"
    harness.atomic_json(output, {"status": "complete"})
    assert json.loads(output.read_text()) == {"status": "complete"}
    with pytest.raises(ValueError, match="clean"):
        harness.require_clean_git_state({"git_dirty": True})


@pytest.mark.parametrize("failed_check", harness.SAFETY_CHECKS)
def test_each_safety_family_is_a_p1_conjunct(failed_check):
    rows = [{"conversation_id": "a", "category": 1, "product": 1.0, "strong": 0.0, "historical": 1.0}, {"conversation_id": "b", "category": 3, "product": 1.0, "strong": 0.0, "historical": 1.0}]
    safety = _safety(); safety["checks"][failed_check] = False; safety["pass"] = False
    report = harness.evaluate_release_gates(rows, bootstrap_seed=7, bootstrap_resamples=100, thresholds=_thresholds(), safety_summary=safety)
    assert not report["p1_pass"]
    assert f"safety.{failed_check}" in report["failure_reasons"]


def test_missing_trace_fails_safety_and_threshold_drift_is_rejected():
    summary = harness.summarize_product_safety(traces={}, product_rankings={}, product_event_maps={}, legacy_event_maps={}, question_conversations={}, lineage=[], expected_questions=1, expected_dialogs=1)
    assert not summary["pass"] and not summary["checks"]["trace_count"]
    with pytest.raises(ValueError, match="threshold"):
        harness.validate_gate_thresholds({"p1_delta": .04, "p2_delta": -.01, "hard_categories": [1, 2]})


def test_gate_report_has_metrics_deltas_bootstrap_and_failure_reasons():
    rows = [{"conversation_id": "a", "category": 1, "product": 0.0, "strong": 1.0, "historical": 1.0}, {"conversation_id": "b", "category": 3, "product": 0.0, "strong": 1.0, "historical": 1.0}]
    report = harness.evaluate_release_gates(rows, bootstrap_seed=7, bootstrap_resamples=100, thresholds=_thresholds(), safety_summary=_safety())
    assert {"product", "strong", "historical", "deltas", "bootstrap", "thresholds", "p1_pass", "p2_pass", "release_pass", "failure_reasons"} <= set(report)
    assert "p1.overall.delta_below_threshold" in report["failure_reasons"]
    assert "p2.hard.delta_below_threshold" in report["failure_reasons"]


@pytest.mark.parametrize(("release_pass", "expected_exit"), [(True, 0), (False, 1)])
def test_cli_exit_code_follows_release_gate(monkeypatch, tmp_path, release_pass, expected_exit):
    monkeypatch.setattr(
        harness,
        "run_quality",
        lambda **_kwargs: {"status": "complete", "gates": {"release_pass": release_pass}},
    )
    argv = [
        "--dataset", str(tmp_path / "dataset.json"),
        "--artifact", str(tmp_path / "artifact.json"),
        "--model-dir", str(tmp_path / "model"),
        "--source-repo", str(tmp_path / "source"),
        "--output", str(tmp_path / "report.json"),
    ]
    assert harness.main(argv) == expected_exit


def test_frozen_inputs_output_scope_source_pins_snapshots_and_phase_order(tmp_path):
    input_path = tmp_path / "input.json"; input_path.write_bytes(b"frozen")
    receipt = harness.freeze_input_bytes(input_path, label="fixture", expected_sha256=harness._sha256(b"frozen"))
    harness.verify_frozen_input(receipt)
    input_path.write_bytes(b"drift")
    with pytest.raises(RuntimeError, match="changed"):
        harness.verify_frozen_input(receipt)
    with pytest.raises(ValueError, match="outside"):
        harness.require_external_output(harness.ROOT / "report.json", tmp_path)
    with pytest.raises(ValueError, match="outside"):
        harness.require_external_output(tmp_path / "report.json", tmp_path)
    with pytest.raises(ValueError, match="pin mismatch"):
        harness.validate_historical_source_pins({"historical_source": {"commit": harness.HISTORICAL_COMMIT, "files": {"x.py": "a" * 64}}}, {"benchmarks/x.py": "b" * 64})
    class Pair:
        def __init__(self, equal=True): self.equal = equal
        def to_dict(self): return {"start": {"x": 1}, "end": {"x": 1 if self.equal else 2}}
    class Encoder:
        def __init__(self, equal=True): self.equal = equal
        def finish_snapshot_pair(self): return Pair(self.equal)
    assert harness.encoder_snapshot_receipt(Encoder()) == {"start": {"x": 1}, "end": {"x": 1}}
    with pytest.raises(RuntimeError, match="snapshot drifted"):
        harness.encoder_snapshot_receipt(Encoder(False))
    ledger = []
    for phase in harness.PHASES: harness.advance_phase(ledger, phase)
    assert ledger == list(harness.PHASES)
    with pytest.raises(RuntimeError, match="phase order"):
        harness.advance_phase([], "score_gate")
    assert len(harness.adapter_implementation_digest()) == 64


def test_per_question_audit_binds_trace_mapping_streams_and_report_shape():
    item = SimpleNamespace(opaque_conversation_id="c", corpus_opaque_dialog_ids=("d",))
    scorer = SimpleNamespace(scorer_items={"q": item})
    ranking = _fcd1_ranking({"e": "d"})
    trace = {"retrieval_ranking": ranking, "selected_evidence_ids": ["e"], "authorized_candidate_ids": ["e"], "denied_partitions": [], "candidate_generation": {"candidate_count": 1}, "candidates": [{"source_event_id": "e", "decision": "allow"}], "returned_spans": []}
    rankings = {arm: {"q": ["d"]} for arm in harness.ARMS}
    pools = {arm: {"q": [f"{arm}-{index}" for index in range(50)]} for arm in ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")}
    audits = harness.build_question_audits(question_rows=[{"item_id": "q", "conversation_id": "c", "columns": {"raw_bm25": {}}}], scorer=scorer, rankings=rankings, source_pool_rankings=pools, traces={"q": trace}, product_event_maps={"c": {"e": "d"}}, lineage_by_conversation={"c": {"seed_ledger": {"checkpoint_ranking_mapping_sha256": "d" * 64, "authorization_mapping_sha256": _fcd1_authorization_mapping_sha256({"e": "d"})}}})
    assert audits[0]["composite_id"] == ["c", "q"] and audits[0]["event_dialog_mapping_sha256"]
    acceptance = {"ledger_schema": harness.FCD1_LEDGER_SCHEMA, "top_k": harness.FCD1_TOP_K, "expected_questions": 1, "reference_product_top10_sha256": harness.FCD1_REFERENCE_PRODUCT_TOP10_SHA256, "actual_product_top10_sha256": harness.FCD1_REFERENCE_PRODUCT_TOP10_SHA256, "top10_unchanged": True}
    report = {"schema": "aerp2-product-six-view-locomo", **{key: {} for key in ("manifest_sha256", "input_freeze", "model_runtime", "git_state_before", "git_state_after", "source_repo", "historical_source", "encoder_identity", "adapter_implementation_sha256", "encoder_sentinels", "encoder_snapshot_pair", "event_dialog_mapping_sha256", "aggregate", "gates")}, "phase_ledger": list(harness.PHASES), "fcd1_acceptance": acceptance, "safety_summary": {"expected_trace_count": 1}, "question_audits": audits}
    harness.validate_report_shape(report)
    report["safety_summary"]["expected_trace_count"] = 2
    with pytest.raises(RuntimeError, match="audit shape"):
        harness.validate_report_shape(report)
