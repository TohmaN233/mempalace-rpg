from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path

import pytest


FROZEN_ANALYZER_SHA256 = "06ae6b02fbd41b1d30c61e2a1d8c77a5b523a8a408c5f8dca196c5e4f5b5c6ea"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def test_production_pins_are_asserted_before_synthetic_monkeypatching():
    from benchmarks import aerp2_fusion_error_decomposition as module

    assert module.SCHEMA == "aerp2-fusion-error-decomposition"
    assert module.SOURCE_ARMS == ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")
    assert module.TOP10_ARMS == ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense", "legacy_rpg", "product_six_view", "historical_six_view")
    assert module.PRODUCT_ARM == "product_six_view"
    assert module.BASELINE_ARM == "raw_bm25_plus_raw_dense"
    assert module.EXPECTED_QUESTIONS == 1986
    assert module.EXPECTED_TOP_K == 10
    assert module.EXPECTED_POOL_SIZE == 50
    assert module.EXPECTED_RESOLVED == 2806
    assert module.EXPECTED_UNRESOLVED == 9
    assert module.EXPECTED_COUNTS == {
        "shared_hit": 1335,
        "fusion_promotion": 251,
        "fusion_demotion": 138,
        "shared_miss_candidate_available": 578,
        "raw_pool_miss": 504,
    }
    assert hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() == FROZEN_ANALYZER_SHA256


def _synthetic_artifact() -> dict:
    raw_bm25 = ["b0", "b1", "b2"]
    raw_dense = ["d0", "d1", "d2"]
    raw_fusion = ["b0", "d0", "b1"]
    cases = [
        ("item_shared", 1, "conversation_alpha", "b0", ["b0", "d1"]),
        ("item_promotion", 1, "conversation_alpha", "b2", ["b2", "d1"]),
        ("item_demotion", 2, "conversation_beta", "b0", ["d1", "d2"]),
        ("item_available", 2, "conversation_beta", "b2", ["d1", "d2"]),
        ("item_miss", 3, "conversation_gamma", "outside_raw_pool", ["d1", "d2"]),
    ]
    source_pool_rankings = {
        "raw_bm25": {item_id: list(raw_bm25) for item_id, *_rest in cases},
        "raw_dense": {item_id: list(raw_dense) for item_id, *_rest in cases},
        "raw_bm25_plus_raw_dense": {item_id: list(raw_fusion) for item_id, *_rest in cases},
    }
    rankings_top10 = {
        "raw_bm25": {item_id: raw_bm25[:2] for item_id, *_rest in cases},
        "raw_dense": {item_id: raw_dense[:2] for item_id, *_rest in cases},
        "raw_bm25_plus_raw_dense": {item_id: raw_fusion[:2] for item_id, *_rest in cases},
        "legacy_rpg": {item_id: raw_dense[:2] for item_id, *_rest in cases},
        "product_six_view": {item_id: product for item_id, _category, _conversation, _gold, product in cases},
        "historical_six_view": {item_id: raw_dense[:2] for item_id, *_rest in cases},
    }

    def metrics(resolved: list[str], ranking: list[str], unresolved: int = 0) -> dict:
        evidence = len(resolved) + unresolved
        if evidence == 0:
            return {
                "scored": False,
                "evidence_item_count": 0,
                "resolved_evidence_item_count": 0,
                "unresolved_evidence_item_count": 0,
                "retrieved_evidence_count_at_10": 0,
                "recall_at_10": None,
                "hit_at_10": None,
                "all_at_10": None,
                "ndcg_at_10": None,
            }
        found = sum(gold in set(ranking) for gold in resolved)
        relevant = frozenset(resolved)
        dcg = sum(1.0 / math.log2(rank + 1) for rank, dialog_id in enumerate(ranking, start=1) if dialog_id in relevant)
        ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(evidence, len(ranking)) + 1))
        return {
            "scored": True,
            "evidence_item_count": evidence,
            "resolved_evidence_item_count": len(resolved),
            "unresolved_evidence_item_count": unresolved,
            "retrieved_evidence_count_at_10": found,
            "recall_at_10": found / evidence,
            "hit_at_10": float(found > 0),
            "all_at_10": float(found == evidence),
            "ndcg_at_10": dcg / ideal_dcg,
        }

    questions = [
        {
            "item_id": item_id,
            "category": category,
            "conversation_id": conversation_id,
            "columns": {
                "product_six_view": {"ranked_ids_at_10": product, "official_exact": metrics([gold], product)},
                "raw_bm25_plus_raw_dense": {"ranked_ids_at_10": raw_fusion[:2], "official_exact": metrics([gold], raw_fusion[:2])},
            },
            "gold": {"official_exact": {"evidence_item_count": 1, "resolved_dialog_ids": [gold], "unresolved_evidence_item_count": 0}},
        }
        for item_id, category, conversation_id, gold, product in cases
    ]
    receipt = {
        "git_head": "a" * 40,
        "git_tree": "b" * 40,
        "commit_diff_sha256": "c" * 64,
        "commit_diff_bytes": 0,
        "worktree_status_sha256": "d" * 64,
        "git_dirty": False,
    }
    return {
        "schema": "aerp2-product-six-view-locomo",
        "status": "complete",
        "git_state_before": receipt,
        "git_state_after": deepcopy(receipt),
        "source_pool_rankings": source_pool_rankings,
        "source_pool_stream_sha256": {arm: _canonical_sha256(stream) for arm, stream in source_pool_rankings.items()},
        "rankings_top10": rankings_top10,
        "ranking_stream_sha256": {arm: _canonical_sha256(stream) for arm, stream in rankings_top10.items()},
        "questions": questions,
    }


@pytest.fixture
def fcd_module(monkeypatch):
    from benchmarks import aerp2_fusion_error_decomposition as module

    monkeypatch.setattr(module, "EXPECTED_QUESTIONS", 5)
    monkeypatch.setattr(module, "EXPECTED_TOP_K", 2)
    monkeypatch.setattr(module, "EXPECTED_POOL_SIZE", 3)
    monkeypatch.setattr(module, "EXPECTED_RESOLVED", 5)
    monkeypatch.setattr(module, "EXPECTED_UNRESOLVED", 0)
    monkeypatch.setattr(module, "EXPECTED_COUNTS", {
        "shared_hit": 1,
        "fusion_promotion": 1,
        "fusion_demotion": 1,
        "shared_miss_candidate_available": 1,
        "raw_pool_miss": 1,
    })
    return module


def _write_artifact(path, artifact: dict) -> str:
    path.write_bytes(json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_synthetic_fcd_zero_reconstructs_counts_safe_canonical_aggregates_and_receipts(tmp_path, fcd_module):
    artifact_path = tmp_path / "frozen.json"
    artifact_sha256 = _write_artifact(artifact_path, _synthetic_artifact())
    report = fcd_module.decompose(artifact_path, expected_artifact_sha256=artifact_sha256, expected_git_head="a" * 40)

    assert report["decomposition"] == {
        "shared_hit": 1,
        "fusion_promotion": 1,
        "fusion_demotion": 1,
        "shared_miss_candidate_available": 1,
        "raw_pool_miss": 1,
        "product_hits": 2,
        "raw_fusion_hits": 2,
    }
    assert report["by_category"] == [
        {"category": 1, "shared_hit": 1, "fusion_promotion": 1, "fusion_demotion": 0, "shared_miss_candidate_available": 0, "raw_pool_miss": 0},
        {"category": 2, "shared_hit": 0, "fusion_promotion": 0, "fusion_demotion": 1, "shared_miss_candidate_available": 1, "raw_pool_miss": 0},
        {"category": 3, "shared_hit": 0, "fusion_promotion": 0, "fusion_demotion": 0, "shared_miss_candidate_available": 0, "raw_pool_miss": 1},
    ]
    alpha = hashlib.sha256(b"conversation_alpha").hexdigest()
    beta = hashlib.sha256(b"conversation_beta").hexdigest()
    gamma = hashlib.sha256(b"conversation_gamma").hexdigest()
    assert report["by_conversation"] == {
        alpha: {"shared_hit": 1, "fusion_promotion": 1, "fusion_demotion": 0, "shared_miss_candidate_available": 0, "raw_pool_miss": 0},
        beta: {"shared_hit": 0, "fusion_promotion": 0, "fusion_demotion": 1, "shared_miss_candidate_available": 1, "raw_pool_miss": 0},
        gamma: {"shared_hit": 0, "fusion_promotion": 0, "fusion_demotion": 0, "shared_miss_candidate_available": 0, "raw_pool_miss": 1},
    }
    assert report["by_category_conversation"] == [
        {"category": 1, "conversations": {alpha: report["by_conversation"][alpha]}},
        {"category": 2, "conversations": {beta: report["by_conversation"][beta]}},
        {"category": 3, "conversations": {gamma: report["by_conversation"][gamma]}},
    ]
    assert report["input_receipt"]["artifact_sha256"] == artifact_sha256
    assert report["input_receipt"]["analyzer_implementation_sha256"] == FROZEN_ANALYZER_SHA256
    assert report["protocol"] == {
        "evidence_semantics": "official_exact",
        "product_arm": "product_six_view",
        "baseline_arm": "raw_bm25_plus_raw_dense",
        "raw_pool_arms": ["raw_bm25", "raw_dense"],
        "top_k": 2,
        "raw_candidate_pool_size": 3,
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    fcd_module.atomic_json(first, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_path.read_bytes())
    fcd_module.atomic_json(second, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_path.read_bytes())
    assert first.read_bytes() == second.read_bytes()
    serialized = first.read_text(encoding="utf-8").casefold()
    assert all(word not in serialized for word in ("transcript", "query", "answer", "text"))
    assert "cannot establish product view-pool miss or fusion causality" in report["claim_boundary"].casefold()


def test_input_digest_and_raw_topk_drift_fail_closed(tmp_path, fcd_module):
    artifact = _synthetic_artifact()
    artifact_path = tmp_path / "frozen.json"
    artifact_sha256 = _write_artifact(artifact_path, artifact)
    with pytest.raises(ValueError, match="artifact SHA-256 mismatch"):
        fcd_module.decompose(artifact_path, expected_artifact_sha256="0" * 64, expected_git_head="a" * 40)

    artifact["rankings_top10"]["raw_bm25"]["item_shared"] = ["b1", "b0"]
    artifact["ranking_stream_sha256"] = {arm: _canonical_sha256(stream) for arm, stream in artifact["rankings_top10"].items()}
    artifact_sha256 = _write_artifact(artifact_path, artifact)
    with pytest.raises(ValueError, match="top-k stream does not match frozen raw pool"):
        fcd_module.decompose(artifact_path, expected_artifact_sha256=artifact_sha256, expected_git_head="a" * 40)



@pytest.mark.parametrize(
    ("arm", "field", "drifted_value"),
    [
        (arm, field, value)
        for arm in ("product_six_view", "raw_bm25_plus_raw_dense")
        for field, value in (
            ("scored", False),
            ("evidence_item_count", 0),
            ("resolved_evidence_item_count", 0),
            ("unresolved_evidence_item_count", 1),
            ("retrieved_evidence_count_at_10", 0),
            ("recall_at_10", 0.0),
            ("hit_at_10", 0.0),
            ("all_at_10", 0.0),
            ("ndcg_at_10", 0.0),
        )
    ],
)
def test_every_reported_official_exact_metric_field_fails_closed_on_drift(tmp_path, fcd_module, arm, field, drifted_value):
    artifact = _synthetic_artifact()
    artifact["questions"][0]["columns"][arm]["official_exact"][field] = drifted_value
    artifact_path = tmp_path / "frozen.json"
    artifact_sha256 = _write_artifact(artifact_path, artifact)
    with pytest.raises(ValueError, match="reported official-exact metrics drift"):
        fcd_module.decompose(artifact_path, expected_artifact_sha256=artifact_sha256, expected_git_head="a" * 40)


def test_duplicate_official_gold_preserves_multiplicity_and_uses_unique_rank_relevance(tmp_path, fcd_module, monkeypatch):
    artifact = _synthetic_artifact()
    question = artifact["questions"][0]
    resolved = ["b0", "b0"]
    question["gold"]["official_exact"] = {
        "evidence_item_count": 2,
        "resolved_dialog_ids": resolved,
        "unresolved_evidence_item_count": 0,
    }
    expected_ndcg = 1.0 / (1.0 + 1.0 / math.log2(3))
    for arm in ("product_six_view", "raw_bm25_plus_raw_dense"):
        question["columns"][arm]["official_exact"] = {
            "scored": True,
            "evidence_item_count": 2,
            "resolved_evidence_item_count": 2,
            "unresolved_evidence_item_count": 0,
            "retrieved_evidence_count_at_10": 2,
            "recall_at_10": 1.0,
            "hit_at_10": 1.0,
            "all_at_10": 1.0,
            "ndcg_at_10": expected_ndcg,
        }
    monkeypatch.setattr(fcd_module, "EXPECTED_RESOLVED", 6)
    monkeypatch.setattr(fcd_module, "EXPECTED_COUNTS", {
        "shared_hit": 2,
        "fusion_promotion": 1,
        "fusion_demotion": 1,
        "shared_miss_candidate_available": 1,
        "raw_pool_miss": 1,
    })
    artifact_path = tmp_path / "duplicate.json"
    artifact_sha256 = _write_artifact(artifact_path, artifact)
    report = fcd_module.decompose(artifact_path, expected_artifact_sha256=artifact_sha256, expected_git_head="a" * 40)
    assert report["decomposition"]["shared_hit"] == 2
    assert question["columns"]["product_six_view"]["official_exact"]["ndcg_at_10"] == expected_ndcg


def _valid_report(tmp_path, fcd_module):
    artifact_path = tmp_path / "artifact.json"
    artifact_sha256 = _write_artifact(artifact_path, _synthetic_artifact())
    report = fcd_module.decompose(artifact_path, expected_artifact_sha256=artifact_sha256, expected_git_head="a" * 40)
    return artifact_path, artifact_path.read_bytes(), report


def test_atomic_publish_rechecks_bound_artifact_bytes_and_cli_returns_nonzero(tmp_path, fcd_module, capsys):
    artifact_path, original_bytes, report = _valid_report(tmp_path, fcd_module)
    report_path = tmp_path / "report.json"
    artifact_path.write_bytes(b"after")
    with pytest.raises(RuntimeError, match="artifact bytes changed during analysis"):
        fcd_module.atomic_json(report_path, report, artifact_path=artifact_path, expected_artifact_bytes=original_bytes)

    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
    code = fcd_module.main([
        "--artifact", str(invalid),
        "--expected-artifact-sha256", hashlib.sha256(invalid.read_bytes()).hexdigest(),
        "--expected-git-head", "a" * 40,
        "--output", str(report_path),
    ])
    assert code == 2
    assert not report_path.exists()
    assert "artifact schema or status mismatch" in capsys.readouterr().err


def test_atomic_publish_rejects_mutated_receipts_artifact_overwrite_and_forbidden_output(tmp_path, fcd_module):
    artifact_path, artifact_bytes, report = _valid_report(tmp_path, fcd_module)

    bad_artifact_receipt = deepcopy(report)
    bad_artifact_receipt["input_receipt"]["artifact_sha256"] = "0" * 64
    artifact_receipt_output = tmp_path / "artifact-receipt.json"
    with pytest.raises(ValueError, match="report artifact receipt mismatch"):
        fcd_module.atomic_json(artifact_receipt_output, bad_artifact_receipt, artifact_path=artifact_path, expected_artifact_bytes=artifact_bytes)
    assert not artifact_receipt_output.exists()

    bad_analyzer_receipt = deepcopy(report)
    bad_analyzer_receipt["input_receipt"]["analyzer_implementation_sha256"] = "0" * 64
    analyzer_receipt_output = tmp_path / "analyzer-receipt.json"
    with pytest.raises(ValueError, match="report analyzer receipt mismatch"):
        fcd_module.atomic_json(analyzer_receipt_output, bad_analyzer_receipt, artifact_path=artifact_path, expected_artifact_bytes=artifact_bytes)
    assert not analyzer_receipt_output.exists()

    before = artifact_path.read_bytes()
    with pytest.raises(ValueError, match="output must not overwrite the artifact"):
        fcd_module.atomic_json(artifact_path, report, artifact_path=artifact_path, expected_artifact_bytes=artifact_bytes)
    assert artifact_path.read_bytes() == before

    forbidden_report = deepcopy(report)
    forbidden_report["claim_boundary"] = "answer"
    forbidden_output = tmp_path / "forbidden.json"
    with pytest.raises(RuntimeError, match="forbidden text"):
        fcd_module.atomic_json(forbidden_output, forbidden_report, artifact_path=artifact_path, expected_artifact_bytes=artifact_bytes)
    assert not forbidden_output.exists()
