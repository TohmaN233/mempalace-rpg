from __future__ import annotations

import numpy as np
import pytest

from benchmarks.aerp1_locomo_three_way import (
    EXPECTED_MANIFEST_SHA256,
    audit_product_trace,
    load_manifest,
    question_metrics,
    rank_rpg,
    rank_vectors,
    raw_dialogs,
    same_git_state,
    seed_rpg_conversation,
)
from mempalace_rpg import NullEpisodeAdapter, RpgMemoryKernel


def _payload() -> dict:
    return {
        "query": "banana",
        "sessions": [
            {
                "dialogs": [
                    {
                        "opaque_dialog_id": "dialog_000002",
                        "speaker": "Sam",
                        "date": "2026-01-02",
                        "caption": "fruit",
                        "text": "The banana is yellow.",
                    },
                    {
                        "opaque_dialog_id": "dialog_000001",
                        "speaker": "Alex",
                        "date": "2026-01-01",
                        "caption": "weather",
                        "text": "The rain stopped.",
                    },
                    {
                        "opaque_dialog_id": "dialog_000003",
                        "speaker": "Jo",
                        "date": "2026-01-03",
                        "caption": "travel",
                        "text": "The train arrived.",
                    },
                ]
            }
        ],
    }


def test_frozen_manifest_digest_and_denominators():
    manifest, digest = load_manifest()

    assert digest == EXPECTED_MANIFEST_SHA256
    assert manifest["dataset"]["expected_conversations"] == 10
    assert manifest["dataset"]["expected_questions"] == 1986
    assert manifest["dataset"]["expected_hard_questions"] == 603
    assert manifest["protocol"]["top_k"] == 10
    assert [column["id"] for column in manifest["columns"]] == [
        "raw_dialog_bm25",
        "pinned_original_mempalace_raw_vector_dialog",
        "latest_rpg_authorized_ranker",
    ]


def test_raw_dialog_text_is_frozen_and_annotation_free():
    dialogs = raw_dialogs(_payload())

    assert dialogs[0] == {
        "id": "dialog_000002",
        "text": (
            "speaker: Sam\n"
            "date: 2026-01-02\n"
            "caption: fruit\n"
            "text: The banana is yellow."
        ),
    }
    assert "observation" not in dialogs[0]["text"]
    assert "summary" not in dialogs[0]["text"]


def test_vector_ranking_uses_id_ascending_tie_break_and_rejects_bad_shapes():
    ids = ["dialog_b", "dialog_a", "dialog_c"]
    passages = np.asarray([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
    query = np.asarray([1.0, 0.0], dtype=np.float32)

    assert rank_vectors(ids, passages, query, 2) == ["dialog_a", "dialog_b"]
    with pytest.raises(ValueError, match="not aligned"):
        rank_vectors(ids, passages[:, :1], query, 2)
    with pytest.raises(ValueError, match="unique dialog IDs"):
        rank_vectors(["dup", "dup"], passages[:2], query, 2)


def test_trace_audit_detects_incomplete_partition_and_unauthorized_selection():
    trace = {
        "policy": "AERP-1",
        "campaign_id": "c",
        "actor_id": "a",
        "actor_type": "npc",
        "candidate_generation": {"candidate_count": 2},
        "candidates": [],
        "deduplication": {},
        "authorized_candidate_ids": ["allowed"],
        "denied_partitions": [{"event_ids": []}],
        "selected_evidence_ids": ["forbidden"],
        "returned_spans": [],
    }

    audit = audit_product_trace(trace)

    assert not audit["complete"]
    assert audit["unauthorized_selected_ids"] == ["forbidden"]
    assert not audit["checks"]["candidate_partition_complete"]


def test_official_exact_metrics_preserve_multiplicity_and_unresolved_denominator():
    metrics = question_metrics(
        ["dialog_a", "dialog_b"],
        ["dialog_a", "dialog_a"],
        evidence_item_count=3,
        unresolved_evidence_item_count=1,
        top_k=10,
    )

    assert metrics["retrieved_evidence_count_at_10"] == 2
    assert metrics["recall_at_10"] == pytest.approx(2 / 3)
    assert metrics["hit_at_10"] == 1.0
    assert metrics["all_at_10"] == 0.0


def test_zero_evidence_questions_are_explicitly_unscored():
    metrics = question_metrics(
        ["dialog_a"],
        [],
        evidence_item_count=0,
        unresolved_evidence_item_count=0,
        top_k=10,
    )

    assert not metrics["scored"]
    assert metrics["recall_at_10"] is None


def test_git_state_comparison_ignores_only_extra_provenance_on_before_state():
    before = {"git_head": "abc", "git_dirty": False, "source_sha256": {"x": "y"}}

    assert same_git_state(before, {"git_head": "abc", "git_dirty": False})
    assert not same_git_state(before, {"git_head": "changed", "git_dirty": False})


def test_latest_ranker_uses_authorized_universe_and_emits_complete_trace(tmp_path):
    with RpgMemoryKernel(
        db_path=str(tmp_path / "locomo.sqlite3"),
        episode_adapter=NullEpisodeAdapter(),
    ) as kernel:
        kernel.upsert_character_profile(
            character_id="locomo_reader",
            display_name="LoCoMo reader",
            tier="core",
            short_persona="Evaluation reader",
            memory_wing="wing_locomo_reader",
        )
        mapping = seed_rpg_conversation(kernel, "conversation_000000", _payload())
        ranked, trace = rank_rpg(
            kernel,
            conversation_id="conversation_000000",
            query="banana",
            event_to_dialog=mapping,
            top_k=2,
        )

    assert len(ranked) == 2
    assert len(set(ranked)) == 2
    assert set(ranked) <= {"dialog_000001", "dialog_000002", "dialog_000003"}
    assert len(trace["authorized_candidate_ids"]) == 3
    assert audit_product_trace(trace)["complete"]
