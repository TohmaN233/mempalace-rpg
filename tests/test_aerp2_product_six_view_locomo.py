from __future__ import annotations

import json
import hashlib
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import aerp2_product_six_view_locomo as harness
from mempalace_rpg import RpgMemoryKernel


def _conversation() -> dict:
    # Real RetrievalBundle payloads contain only query/sessions. Conversation
    # identity lives in RetrievalBundle.item_to_conversation.
    return {"sessions": [{"opaque_session_id": "session-1", "dialogs": [{"opaque_dialog_id": "dialog-1", "speaker": "Ada", "date": "2024-01-01", "caption": "arrival", "text": "Hello answer word", "observation": "FORBIDDEN OBSERVATION", "answer": "excluded"}]}]}


def _safety() -> dict:
    mapping = {"count": 1, "unique_event_count": 1, "unique_dialog_count": 1, "mapping_sha256": "a" * 64, "valid": True, "one_to_one": True}
    return {"expected_trace_count": 1986, "trace_count": 1986, "audit_complete_count": 1986, "ranking_schema_complete_count": 1986, "fcd1_ledger_complete_count": 1986, "selected_match_count": 1986, "trace_identity_complete_count": 1986, "nonempty_selection_count": 1986, "unauthorized_selected_count": 0, "legacy_mapping": mapping, "product_mapping": dict(mapping), "lineage": {"count": 1, "forbidden_field_count": 0, "mapping_digest_count": 1}, "ranking_digest_count": 1986, "checks": {name: True for name in harness.SAFETY_CHECKS}, "pass": True}


def _sentinels(manifest: str = "a" * 64) -> dict:
    def row(mode: str, digest: str) -> dict:
        return {"schema": harness.ENCODER_RECEIPT_SCHEMA, "manifest_sha256": manifest, "mode": mode, "input_count": 1, "input_sha256": digest, "embedding_sha256": digest, "dtype": "float32-little-endian", "shape": [1, 3]}
    return {"query": row("query", "d" * 64), "passage": row("passage", "e" * 64)}


def _snapshot(manifest: str = "a" * 64) -> dict:
    value = {"schema": harness.ENCODER_RECEIPT_SCHEMA, "model_dir": "C:/model", "manifest_sha256": manifest, "manifest_variant": "fp32", "files": [{"relative_path": "model.onnx", "sha256": "f" * 64, "stat": {"byte_count": 1, "device": 0, "inode": 0, "modified_ns": 0}}]}
    return {"start": value, "end": dict(value)}


def _git_state() -> dict:
    return {"git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": "c" * 64, "commit_diff_sha256": "d" * 64, "commit_diff_bytes": 0}


def _thresholds() -> dict:
    return {"p1_delta": .05, "p2_delta": -.01, "hard_categories": [1, 2]}


def _fcd1_authorization_mapping_sha256(mapping: dict[str, str]) -> str:
    policy_sha256 = harness._canonical(["public-policy"])
    rows = [{
        "ranking_key_sha256": harness._sha256(dialog_id.encode("utf-8")),
        "checkpoint_sha256": harness._sha256(f"checkpoint:{dialog_id}".encode("utf-8")),
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
            "group_id": "group:" + harness._canonical([harness._sha256(f"checkpoint:{mapping[event_id]}".encode("utf-8")), policy_sha256]),
            "checkpoint_sha256": harness._sha256(f"checkpoint:{mapping[event_id]}".encode("utf-8")),
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


def test_fresh_stream_receipts_recompute_raw_component_parity_from_independent_control_streams():
    ids = ["dialog-a", "dialog-b", "dialog-c"]
    hashes = [hashlib.sha256(value.encode()).hexdigest() for value in ids]
    rows = [{"ranking_key_sha256": value} for value in hashes]
    trace = {"retrieval_ranking": {"fcd1_diagnostic_ledger": {"schema": "synthetic", "input_sha256": "a" * 64, "authorization_sha256": "b" * 64, "view_top_50": {"raw_bm25": list(rows), "raw_dense": list(rows)}, "view_top_50_sha256": {}, "view_order_sha256": {}, "fused_top_50": [], "checkpoint_tie_groups": []}}}
    rankings = {arm: {"question": ids[:2]} for arm in harness.ARMS[:-1]}
    pools = {arm: {"question": list(ids)} for arm in ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")}
    fresh = harness.FreshStreams(rankings, pools, {"question": trace}, [], {}, {}, {}, {})

    receipts = harness.fresh_stream_receipts(fresh, top_k=2, source_pool=3)
    assert receipts["raw_component_parity"]["pass"] is True
    trace["retrieval_ranking"]["fcd1_diagnostic_ledger"]["view_top_50"]["raw_dense"] = list(reversed(rows))
    assert harness.fresh_stream_receipts(fresh, top_k=2, source_pool=3)["raw_component_parity"]["pass"] is False


def test_uuid_independent_fresh_receipts_replay_same_semantics() -> None:
    ids = ["dialog-a", "dialog-b", "dialog-c"]
    hashes = [hashlib.sha256(value.encode()).hexdigest() for value in ids]
    def make(event_prefix: str) -> harness.FreshStreams:
        events = [f"{event_prefix}-{index}" for index in range(3)]
        rows = [{"source_event_id": event, "ranking_key_sha256": digest, "ranking_key_order": index + 1, "rank": index + 1, "score": float(3 - index)} for index, (event, digest) in enumerate(zip(events, hashes))]
        ledger = {"schema": "synthetic", "input_sha256": "a" * 64, "authorization_sha256": "b" * 64, "view_top_50": {"raw_bm25": list(rows), "raw_dense": list(rows)}, "view_top_50_sha256": {}, "view_order_sha256": {}, "fused_top_50": [{"source_event_id": event, "ranking_key_sha256": digest, "ranking_key_order": index + 1, "rank": index + 1, "final_rrf": 1.0, "component_ranks": {}, "component_rank_receipts": [], "contributions": {}} for index, (event, digest) in enumerate(zip(events, hashes))], "checkpoint_tie_groups": [{"group_id": "group:" + "c" * 64, "checkpoint_sha256": "d" * 64, "policy_sha256": "e" * 64, "checkpoint_score": 1.0, "member_count": 3, "chronological_members": [{"source_event_id": event, "ranking_key_sha256": digest} for event, digest in zip(events, hashes)]}]}
        rankings = {arm: {"question": ids[:2]} for arm in harness.ARMS[:-1]}; pools = {arm: {"question": list(ids)} for arm in ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")}
        lineage = [{"conversation_id": "conversation", "seed_ledger": {"authorization_mapping_sha256": "1" * 64, "checkpoint_ranking_mapping_sha256": "2" * 64, "ranker_texts_sha256": "3" * 64}}]
        return harness.FreshStreams(rankings, pools, {"question": {"retrieval_ranking": {"fcd1_diagnostic_ledger": ledger}}}, lineage, {"conversation": lineage[0]}, {"conversation": dict(zip(events, ids))}, {"conversation": dict(zip(events, ids))}, {"question": "conversation"})
    safety = {"expected_trace_count": 1, "trace_count": 1, "audit_complete_count": 1, "ranking_schema_complete_count": 1, "fcd1_ledger_complete_count": 1, "selected_match_count": 1, "trace_identity_complete_count": 1, "nonempty_selection_count": 1, "unauthorized_selected_count": 0, "legacy_mapping": {"count": 3, "unique_event_count": 3, "unique_dialog_count": 3, "mapping_sha256": "x", "valid": True, "one_to_one": True}, "product_mapping": {"count": 3, "unique_event_count": 3, "unique_dialog_count": 3, "mapping_sha256": "y", "valid": True, "one_to_one": True}, "lineage": {"count": 1}, "ranking_digest_count": 1, "checks": {name: True for name in harness.SAFETY_CHECKS}, "pass": True}
    assert harness.fresh_stream_receipts(make("uuid-a"), top_k=2, source_pool=3, safety_summary=safety) == harness.fresh_stream_receipts(make("uuid-b"), top_k=2, source_pool=3, safety_summary=safety)


def test_production_prelabel_replay_uses_real_receipt_safety_and_uuid_free_fresh_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runs differ only in generated event UUIDs; no scorer is needed pre-label."""
    state = _git_state()
    dialogs = [f"dialog-{index:02d}" for index in range(50)]
    def fresh(prefix: str) -> harness.FreshStreams:
        mapping = {f"{prefix}-event-{index}": dialog for index, dialog in enumerate(dialogs)}
        event_ids = list(mapping); ranking = _fcd1_ranking(mapping)
        trace = {"retrieval_ranking": ranking, "selected_evidence_ids": [event_ids[0]], "authorized_candidate_ids": event_ids, "candidates": [{"source_event_id": event_ids[0], "decision": "allow"}]}
        seed = {"authorization_mapping_sha256": _fcd1_authorization_mapping_sha256(mapping), "checkpoint_ranking_mapping_sha256": "a" * 64, "ranker_texts_sha256": "b" * 64, "forbidden_field_counts": {field: 0 for field in harness.FORBIDDEN_ANNOTATION_FIELDS}}
        lineage = [{"conversation_id": "conversation", "seed_ledger": seed}]
        rankings = {arm: {"question": dialogs[:10]} for arm in harness.ARMS[:-1]}; rankings["product_six_view"] = {"question": [mapping[event_ids[0]]]}
        pools = {arm: {"question": list(dialogs)} for arm in ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")}
        return harness.FreshStreams(rankings, pools, {"question": trace}, lineage, {"conversation": lineage[0]}, {"conversation": mapping}, {"conversation": mapping}, {"question": "conversation"})
    monkeypatch.setattr(harness.aerp1, "audit_product_trace", lambda _trace: {"complete": True, "unauthorized_selected_ids": []})
    first, second = fresh("uuid-one"), fresh("uuid-two")
    safety_one = harness.summarize_product_safety(traces=first.traces, product_rankings=first.rankings["product_six_view"], product_event_maps=first.product_event_maps, legacy_event_maps=first.legacy_event_maps, question_conversations=first.question_conversations, lineage=first.lineage, expected_questions=1, expected_dialogs=50)
    safety_two = harness.summarize_product_safety(traces=second.traces, product_rankings=second.rankings["product_six_view"], product_event_maps=second.product_event_maps, legacy_event_maps=second.legacy_event_maps, question_conversations=second.question_conversations, lineage=second.lineage, expected_questions=1, expected_dialogs=50)
    assert safety_one["pass"] and safety_two["pass"], [name for name, passed in safety_one["checks"].items() if not passed]
    streams_one = harness.fresh_stream_receipts(first, top_k=10, source_pool=50, safety_summary=safety_one)
    streams_two = harness.fresh_stream_receipts(second, top_k=10, source_pool=50, safety_summary=safety_two)
    assert streams_one == streams_two
    auth_drift = fresh("uuid-auth-drift"); auth_drift.lineage[0]["seed_ledger"]["authorization_mapping_sha256"] = "0" * 64
    auth_streams = harness.fresh_stream_receipts(auth_drift, top_k=10, source_pool=50, safety_summary=safety_two)
    assert auth_streams["authorization_mapping_stream_sha256"] != streams_one["authorization_mapping_stream_sha256"]
    checkpoint_drift = fresh("uuid-checkpoint-drift"); checkpoint_drift.lineage[0]["seed_ledger"]["checkpoint_ranking_mapping_sha256"] = "0" * 64
    checkpoint_streams = harness.fresh_stream_receipts(checkpoint_drift, top_k=10, source_pool=50, safety_summary=safety_two)
    assert checkpoint_streams["lineage_stream_sha256"] != streams_one["lineage_stream_sha256"]
    runtime = {"onnx_sha256": "c" * 64, "embedding_dimension": 3, "session_providers": ["CPUExecutionProvider"], "onnxruntime_version": "1"}; sentinels = _sentinels("3" * 64); snapshots = _snapshot("3" * 64)
    source = {"path": "C:/source", "pinned_commit": harness.HISTORICAL_COMMIT, "git_state": state}; historical = {"commit": harness.HISTORICAL_COMMIT, "files": {"benchmarks/locomo_bge_encoder.py": "f" * 64}}
    receipt = {"schema": harness.PREFREEZE_SCHEMA, "version": 1, "status": "complete", "manifest_sha256": "1" * 64, "phase_ledger": list(harness.PREFREEZE_PHASES), "input_freeze": {"dataset": {"label": "dataset", "path": "C:/dataset", "sha256": "2" * 64, "bytes": 1}, "model_manifest_sha256": "3" * 64}, "git_state_before": state, "git_state_after": state, "source_repo": source, "historical_source": historical, "encoder_identity": "4" * 64, "adapter_implementation_sha256": "5" * 64, "implementation_sha256": {"harness": hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest(), "ranker": hashlib.sha256((harness.ROOT / "mempalace_rpg" / "retrieval.py").read_bytes()).hexdigest(), "prefreeze_cli": hashlib.sha256((harness.ROOT / "benchmarks" / "aerp2_product_six_view_prefreeze.py").read_bytes()).hexdigest()}, "model_runtime": runtime, "encoder_sentinels": sentinels, "encoder_snapshot_pair": snapshots, "stream_receipts": streams_one, "safety_summary": safety_one, "claim_boundary": "bounded"}
    kwargs = {"manifest_sha256": "1" * 64, "dataset_sha256": "2" * 64, "expected_git_state": state, "expected_questions": 1, "expected_top_k": 10, "expected_source_pool": 50, "expected_adapter_sha256": "5" * 64, "expected_source": source, "expected_historical_source": historical, "expected_identity": "4" * 64, "expected_model_runtime": runtime, "expected_snapshot_pair": snapshots, "expected_sentinels": sentinels}
    assert harness.verify_prefreeze_replay(receipt, fresh_streams=streams_two, safety_summary=safety_two, **kwargs) == streams_two
    receipt["stream_receipts"] = {**streams_one, "product_trace_stream_sha256": "0" * 64}
    with pytest.raises(ValueError, match="aggregate digest|fresh-stream"):
        harness.verify_prefreeze_replay(receipt, fresh_streams=streams_two, safety_summary=safety_two, **kwargs)


def test_prefreeze_receipt_validator_requires_pinned_aggregate_contract_and_raw_parity():
    state = _git_state()
    arms = {arm: str(index) * 64 for index, arm in enumerate(harness.ARMS[:-1], start=1)}
    pools = {arm: str(index + 6) * 64 for index, arm in enumerate(("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense"), start=1)}
    parity_arms = {arm: {"top10_order_exact_questions": 1986, "top50_order_exact_questions": 1986, "top50_set_exact_questions": 1986, "overlap_mean": 1.0, "overlap_min": 1.0} for arm in ("raw_bm25", "raw_dense")}
    streams = {
        "expected_questions": 1986, "top_k": 10, "source_pool": 50,
        "product_top10_sha256": "b" * 64,
        "ranking_stream_sha256": arms, "source_pool_stream_sha256": pools,
        "product_trace_stream_sha256": "3" * 64, "lineage_stream_sha256": "4" * 64,
        "authorization_mapping_stream_sha256": "5" * 64, "legacy_event_mapping_sha256": "6" * 64,
        "product_event_mapping_sha256": "7" * 64, "safety_summary_sha256": "8" * 64,
        "raw_component_parity": {"pass": True, "sha256": harness._canonical({"arms": parity_arms}), "arms": parity_arms},
    }
    streams["fresh_streams_sha256"] = harness._canonical(streams)
    receipt = {
        "schema": harness.PREFREEZE_SCHEMA, "version": 1, "status": "complete", "manifest_sha256": "d" * 64,
        "phase_ledger": list(harness.PREFREEZE_PHASES), "git_state_before": state, "git_state_after": dict(state),
        "input_freeze": {"dataset": {"label": "dataset", "path": "C:/opaque-dataset", "sha256": "e" * 64, "bytes": 1}, "model_manifest_sha256": "f" * 64},
        "source_repo": {"path": "C:/opaque-source", "pinned_commit": harness.HISTORICAL_COMMIT, "git_state": state}, "historical_source": {"commit": harness.HISTORICAL_COMMIT, "files": {"benchmarks/locomo_bge_encoder.py": "1" * 64}}, "encoder_identity": "a" * 64, "adapter_implementation_sha256": "b" * 64,
        "implementation_sha256": {"harness": hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest(), "ranker": hashlib.sha256((harness.ROOT / "mempalace_rpg" / "retrieval.py").read_bytes()).hexdigest(), "prefreeze_cli": hashlib.sha256((harness.ROOT / "benchmarks" / "aerp2_product_six_view_prefreeze.py").read_bytes()).hexdigest()},
        "model_runtime": {"onnx_sha256": "c" * 64, "embedding_dimension": 3, "session_providers": ["CPUExecutionProvider"], "onnxruntime_version": "1"}, "encoder_sentinels": _sentinels("f" * 64), "encoder_snapshot_pair": _snapshot("f" * 64), "stream_receipts": streams, "safety_summary": _safety(), "claim_boundary": "bounded",
    }
    assert harness.validate_prefreeze_receipt(receipt, manifest_sha256="d" * 64, dataset_sha256="e" * 64, expected_git_state=state) == streams
    receipt["stream_receipts"]["raw_component_parity"]["arms"]["raw_dense"]["top10_order_exact_questions"] = 1985
    with pytest.raises(ValueError, match="raw parity"):
        harness.validate_prefreeze_receipt(receipt, manifest_sha256="d" * 64, dataset_sha256="e" * 64, expected_git_state=state)
    receipt["stream_receipts"]["raw_component_parity"]["arms"]["raw_dense"]["top10_order_exact_questions"] = 1986
    receipt["encoder_identity"] = "A" * 64
    with pytest.raises(ValueError, match="identity digest"):
        harness.validate_prefreeze_receipt(receipt, manifest_sha256="d" * 64, dataset_sha256="e" * 64, expected_git_state=state)


def test_prefreeze_and_staged_phase_contracts_preserve_legacy_and_gate_label_join() -> None:
    assert harness.PHASES == ("input_byte_freeze", "source_model_load", "sanitized_retrieval_construction", "fresh_streams_frozen", "artifact_parse_scorer_contract", "score_gate", "state_recheck", "atomic_publish_ready")
    assert harness.STAGED_PHASES.index("prefreeze_receipt_verified") < harness.STAGED_PHASES.index("artifact_parse_scorer_contract")
    assert harness.STAGED_PHASES.index("prelabel_safety") < harness.STAGED_PHASES.index("prefreeze_receipt_verified")
    assert harness.PREFREEZE_PHASES[-1] == "atomic_publish_ready"


def test_no_clobber_publisher_rejects_existing_output_and_input_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "input.json"; input_path.write_bytes(b"frozen")
    receipt = {"label": "input", "path": str(input_path), "sha256": hashlib.sha256(b"frozen").hexdigest(), "bytes": len(b"frozen")}
    state = {"git_dirty": False, "git_head": "a" * 40}
    monkeypatch.setattr(harness.aerp1, "git_state", lambda _root: state)
    output = tmp_path / "output.json"; output.write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="new"):
        harness.atomic_json_no_clobber(output, {"ok": True}, frozen_inputs=(receipt,), git_states=((tmp_path, state),), implementation_digests={Path(harness.__file__): hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest()})
    assert output.read_text(encoding="utf-8") == "existing"
    with pytest.raises(ValueError, match="distinct"):
        harness.atomic_json_no_clobber(input_path, {"ok": True}, frozen_inputs=(receipt,), git_states=((tmp_path, state),), implementation_digests={Path(harness.__file__): hashlib.sha256(Path(harness.__file__).read_bytes()).hexdigest()})
    assert input_path.read_bytes() == b"frozen"


def test_prefreeze_cli_has_no_historical_artifact_argument(capsys: pytest.CaptureFixture[str]) -> None:
    from benchmarks import aerp2_product_six_view_prefreeze as cli

    with pytest.raises(SystemExit) as stopped:
        cli.main(["--help"])
    assert stopped.value.code == 0
    assert "--artifact" not in capsys.readouterr().out


def test_prefreeze_output_safety_rejects_label_or_text_fields() -> None:
    harness.validate_prefreeze_output_safety({"stream_receipts": {"fresh_streams_sha256": "a" * 64}})
    with pytest.raises(ValueError, match="forbidden"):
        harness.validate_prefreeze_output_safety({"query": "x"})


def test_shared_sentinel_receipt_helper_has_one_frozen_probe_contract() -> None:
    calls: list[tuple[tuple[str, ...], str]] = []
    class Hash:
        def __init__(self, value: str) -> None: self.value = value
        def to_dict(self) -> dict[str, str]: return {"sha256": self.value}
    class Encoder:
        def sentinel_embedding_hash(self, values: list[str], *, mode: str) -> Hash:
            calls.append((tuple(values), mode)); return Hash(hashlib.sha256((mode + values[0]).encode()).hexdigest())
    runtime, published = harness.encoder_sentinel_receipts(Encoder())
    assert calls == [((harness.SENTINEL_INPUT_TEXT,), "query"), ((harness.SENTINEL_PASSAGE_TEXT,), "passage")]
    assert (harness.SENTINEL_INPUT_TEXT, harness.SENTINEL_PASSAGE_TEXT) == ("aerp2 product query sentinel", "aerp2 product passage sentinel")
    assert published == {"query": runtime["query"], "passage": runtime["passage"]}


def test_shared_sentinel_receipt_helper_normalizes_dataclass_tuple_shape() -> None:
    """The real receipt is a dataclass whose ``asdict`` preserves tuples."""
    @dataclass(frozen=True)
    class SentinelEmbeddingHash:
        schema: str
        manifest_sha256: str
        mode: str
        input_count: int
        input_sha256: str
        embedding_sha256: str
        dtype: str
        shape: tuple[int, int]

        def to_dict(self) -> dict[str, object]:
            return asdict(self)

    class Encoder:
        def sentinel_embedding_hash(self, values: list[str], *, mode: str) -> SentinelEmbeddingHash:
            digest = hashlib.sha256((mode + values[0]).encode()).hexdigest()
            return SentinelEmbeddingHash(
                schema=harness.ENCODER_RECEIPT_SCHEMA,
                manifest_sha256="a" * 64,
                mode=mode,
                input_count=1,
                input_sha256=digest,
                embedding_sha256=digest,
                dtype="float32-little-endian",
                shape=(1, 384),
            )

    runtime, published = harness.encoder_sentinel_receipts(Encoder())

    assert runtime["query"]["shape"] == [1, 384]
    assert runtime["passage"]["shape"] == [1, 384]
    assert isinstance(runtime["query"]["shape"], list)
    assert published == {"query": runtime["query"], "passage": runtime["passage"]}


def test_staged_receipt_drift_blocks_artifact_and_scorer_label_firewall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real staged ordering with only the fresh-stream seam faked."""
    state = {"git_dirty": False, "git_head": "a" * 40}
    manifest = {"inputs": {"dataset_sha256": "d" * 64, "historical_artifact_sha256": "e" * 64}, "protocol": {"source_pool": 50, "top_k": 10, "questions": 1986, "dialogs": 1}}
    dataset, artifact, prefreeze = tmp_path / "dataset", tmp_path / "artifact", tmp_path / "prefreeze"
    output, source = tmp_path / "report.json", tmp_path / "source"; source.mkdir()
    frozen = {
        "dataset": {"label": "dataset", "path": str(dataset), "sha256": "d" * 64, "bytes": 1, "data": b"dataset"},
        "artifact": {"label": "artifact", "path": str(artifact), "sha256": "e" * 64, "bytes": 1, "data": b"artifact"},
        "prefreeze": {"label": "prefreeze", "path": str(prefreeze), "sha256": "f" * 64, "bytes": 2, "data": b"{}"},
    }
    class PoisonScorer:
        @property
        def scorer_items(self): raise AssertionError("scorer labels accessed before verified receipt")
    items = {f"item-{index}": {"sessions": []} for index in range(10)}
    retrieval = SimpleNamespace(retrieval_items=items)
    protocol = SimpleNamespace(load_official_locomo10=lambda _path: object(), prepare_hard_story_track=lambda *_args, **_kwargs: (retrieval, PoisonScorer()))
    encoder = SimpleNamespace(manifest=SimpleNamespace(canonical_sha256="m" * 64))
    observed: list[str] = []
    original_loads = harness.json.loads
    def guarded_loads(raw, *args, **kwargs):
        if raw == b"artifact":
            observed.append("artifact_json"); raise AssertionError("historical artifact parsed before receipt verification")
        return original_loads(raw, *args, **kwargs)
    monkeypatch.setattr(harness, "load_manifest", lambda _path: (manifest, "c" * 64))
    monkeypatch.setattr(harness.aerp1, "git_state", lambda _root: state)
    monkeypatch.setattr(harness, "freeze_input_bytes", lambda _path, *, label, expected_sha256: frozen[label])
    monkeypatch.setattr(harness, "source_repo_receipt", lambda _root, _manifest: {"path": str(source), "git_state": state})
    def historical_modules(*_args):
        sys.path.insert(0, "poison-prelabel-path")
        return protocol, object(), encoder, {"benchmarks/locomo_bge_encoder.py": "b" * 64}, SimpleNamespace(cleanup=lambda: None), {}
    monkeypatch.setattr(harness, "_historical_modules", historical_modules)
    monkeypatch.setattr(harness, "adapter_implementation_digest", lambda: "f" * 64)
    monkeypatch.setattr(harness, "encoder_runtime_provider_receipt", lambda *_args: {})
    monkeypatch.setattr(harness, "encoder_sentinel_receipts", lambda _encoder: ({"query": {}}, {"query": {}}))
    monkeypatch.setattr(harness.aerp1, "_conversation_items", lambda _retrieval: {f"conversation-{index}": [f"item-{index}"] for index in range(10)})
    fake_fresh = SimpleNamespace(rankings={"product_six_view": {}}, source_pool_rankings={}, traces={}, lineage=[], lineage_by_conversation={}, legacy_event_maps={}, product_event_maps={}, question_conversations={})
    monkeypatch.setattr(harness, "freeze_fresh_streams", lambda **_kwargs: fake_fresh)
    poison_safety = {"expected_trace_count": 1986, "trace_count": 1986, "audit_complete_count": 1986, "ranking_schema_complete_count": 1986, "fcd1_ledger_complete_count": 1986, "selected_match_count": 1986, "trace_identity_complete_count": 1986, "nonempty_selection_count": 1986, "unauthorized_selected_count": 0, "legacy_mapping": {"count": 1, "unique_event_count": 1, "unique_dialog_count": 1, "mapping_sha256": "a" * 64, "valid": True, "one_to_one": True}, "product_mapping": {"count": 1, "unique_event_count": 1, "unique_dialog_count": 1, "mapping_sha256": "b" * 64, "valid": True, "one_to_one": True}, "lineage": {"count": 10, "forbidden_field_count": 0, "mapping_digest_count": 10}, "ranking_digest_count": 1986, "checks": {name: True for name in harness.SAFETY_CHECKS}, "pass": True}
    monkeypatch.setattr(harness, "summarize_product_safety", lambda **_kwargs: poison_safety)
    monkeypatch.setattr(harness, "fresh_stream_receipts", lambda *_args, **_kwargs: {"fresh": "replayed"})
    monkeypatch.setattr(harness, "encoder_snapshot_receipt", lambda _encoder: {})
    monkeypatch.setattr(harness, "model_runtime_receipt", lambda *_args: {})
    calls = {"validate": 0}
    def validate(*_args, **kwargs):
        calls["validate"] += 1
        if kwargs.get("expected_streams") is not None:
            raise RuntimeError("prefreeze receipt drift")
        return {"fresh": "published"}
    monkeypatch.setattr(harness, "validate_prefreeze_receipt", validate)
    monkeypatch.setattr(harness.json, "loads", guarded_loads)
    monkeypatch.setattr(harness.aerp1, "score_rankings", lambda *_args: (_ for _ in ()).throw(AssertionError("score called")))
    monkeypatch.setattr(harness, "evaluate_release_gates", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("gate called")))
    with pytest.raises(RuntimeError, match="receipt drift"):
        harness.run_quality(dataset_path=dataset, artifact_path=artifact, model_dir=tmp_path, source_repo=source, output=output, prefreeze_receipt_path=prefreeze, expected_prefreeze_sha256="f" * 64)
    assert calls["validate"] == 2
    assert observed == [] and not output.exists()
