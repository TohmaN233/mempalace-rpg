from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _fixture_artifact() -> dict:
    dialog_ids = ["dialog_" + chr(ord("a") + index) for index in range(50)]

    def ranking(*leading: str) -> list[dict]:
        ordered = [*leading, *(item for item in dialog_ids if item not in leading)]
        return [
            {"opaque_dialog_id": dialog_id, "score": float(len(ordered) - index)}
            for index, dialog_id in enumerate(ordered)
        ]

    def question(question_id: str, category: int, gold: str) -> dict:
        return {
            "opaque_question_id": question_id,
            "opaque_conversation_id": "conversation_000000",
            "scorer": {
                "category": category,
                "corpus_dialog_count": 3,
                "evidence_semantics": {
                    "official_exact": {
                        "evidence_item_count": 1,
                        "resolved_opaque_dialog_ids": [gold],
                        "unresolved_evidence_item_count": 0,
                    }
                },
            },
            "methods": {
                "raw_dialog_bm25": {"ranking": ranking("dialog_a", "dialog_b")},
                "raw_dense": {"ranking": ranking("dialog_b", "dialog_a")},
                "six_view_story_dense_rrf_v2": {"ranking": ranking("dialog_b", "dialog_a")},
            },
        }

    return {
        "schema_version": "mempalace.locomo_story_retrieval.v2",
        "status": "completed_exploratory_public_nonblind_dense_retrieval",
        "protocol": {"ranking_freeze_before_scorer_labels_access": True},
        "config": {"candidate_pool_size": 50, "dense_candidate": {"rrf_k": 60}},
        "provenance": {"dataset": {"expected_sha256": "dataset"}, "model": {"manifest_sha256": "model"}, "config_sha256": "config", "code": {"files": {"scorer.py": {"start_sha256": "scorer", "end_sha256": "scorer"}}}},
        "selection_freeze": {"raw_sha256": "selection", "runtime_ranking_digest_verified": True},
        "corpora": [{"opaque_conversation_id": "conversation_000000", "opaque_dialog_ids": dialog_ids}],
        "questions": [question("question_1", 1, "dialog_b"), question("question_2", 4, "dialog_a")],
    }


def _manifest_for(artifact_path: Path) -> dict:
    return {
        "schema": "aerp2-six-view-replay-manifest",
        "version": 1,
        "artifact": {
            "sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "schema_version": "mempalace.locomo_story_retrieval.v2",
            "status": "completed_exploratory_public_nonblind_dense_retrieval",
        },
        "freeze": {"dataset_sha256": "dataset", "model_manifest_sha256": "model", "config_sha256": "config", "selection_freeze_sha256": "selection", "scorer_implementation": {"file": "scorer.py", "sha256": "scorer"}},
        "protocol": {"top_k": 10, "candidate_pool_size": 50, "hard_categories": [1, 2], "rrf_k": 60},
        "anchors": {"raw_bm25": {"overall": 1.0, "hard": 1.0}, "six_view": {"overall": 1.0, "hard": 1.0}},
    }


def test_public_replay_emits_comparable_four_arm_metrics_and_digests(tmp_path):
    from benchmarks.aerp2_six_view_replay import run_replay

    artifact_path = tmp_path / "historical.json"
    _write_json(artifact_path, _fixture_artifact())
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, _manifest_for(artifact_path))

    report = run_replay(artifact_path, manifest_path)

    assert report["status"] == "complete"
    assert report["phase_order"] == ["verify_inputs", "freeze_rankings", "open_scorer_labels", "score"]
    assert report["denominators"] == {"questions": 2, "hard_questions": 1, "top_k": 10}
    assert set(report["arms"]) == {"full_six_view", "raw_bm25_plus_raw_dense", "raw_bm25", "raw_dense"}
    assert all(arm["status"] == "complete" for arm in report["arms"].values())
    assert all(len(arm["ranking_stream_sha256"]) == 64 for arm in report["arms"].values())
    assert report["arms"]["raw_bm25"]["official_exact"]["question_macro_recall_at_k"]["10"] == 1.0


def test_public_replay_fails_closed_when_raw_dense_stream_was_never_published(tmp_path):
    from benchmarks.aerp2_six_view_replay import run_replay

    artifact = _fixture_artifact()
    for question in artifact["questions"]:
        del question["methods"]["raw_dense"]
    artifact_path = tmp_path / "historical-without-raw-dense.json"
    _write_json(artifact_path, artifact)
    manifest_path = tmp_path / "manifest.json"
    _write_json(manifest_path, _manifest_for(artifact_path))

    report = run_replay(artifact_path, manifest_path)

    assert report["status"] == "partial"
    assert report["arms"]["raw_bm25"]["status"] == "complete"
    assert report["arms"]["full_six_view"]["status"] == "complete"
    for name in ("raw_dense", "raw_bm25_plus_raw_dense"):
        assert report["arms"][name] == {
            "status": "blocked_missing_immutable_ranking_stream",
            "missing_immutable_ranking_streams": ["raw_dense"],
            "ranking_stream_sha256": None,
            "official_exact": None,
        }
    assert report["claim_boundary"]["improvement_claim"] == "not made while any required ablation arm is blocked"


def test_export_r0_rejects_a_single_score_or_order_change():
    from benchmarks.aerp2_historical_export import _r0_compare

    published = _fixture_artifact()
    rerun = deepcopy(published)
    rerun["questions"][0]["methods"]["raw_dialog_bm25"]["ranking"][0]["score"] -= 0.5

    with pytest.raises(RuntimeError, match="R0 ranking mismatch"):
        _r0_compare(published, rerun)


def test_export_report_derives_cross_checked_denominators_and_carries_input_freeze():
    from benchmarks.aerp2_historical_export import _build_export_report

    published = _fixture_artifact()
    rerun = deepcopy(published)
    methods = ("raw_dialog_bm25", "raw_dense", "six_view_story_dense_rrf_v2", "raw_bm25_plus_raw_dense")
    for question in rerun["questions"]:
        question["methods"]["raw_bm25_plus_raw_dense"] = deepcopy(question["methods"]["raw_dialog_bm25"])
    rerun["aggregate_metrics"] = {
        name: {"official_exact": {"all_questions": {"question_macro_recall_at_k": {"10": 1.0}}, "hard_story": {"question_macro_recall_at_k": {"10": 1.0}}}}
        for name in methods
    }
    published_replay = {
        "status": "partial",
        "denominators": {"questions": 2, "hard_questions": 1, "top_k": 10},
        "input_freeze": {"artifact_sha256": "artifact", "manifest_sha256": "manifest", "dataset_sha256": "dataset"},
    }

    report = _build_export_report(published_replay, published, rerun, {"runner.py": "source"})

    assert report["denominators"] == published_replay["denominators"]
    assert report["input_freeze"] == published_replay["input_freeze"]
    assert report["environment_waivers"]["bypassed_historical_checks"][1]["name"] == "selection_provenance_validation"
    assert report["environment_waivers"]["independent_hard_gates"][-1] == "exact R0 equality for raw BM25 and six-view ranking streams"
