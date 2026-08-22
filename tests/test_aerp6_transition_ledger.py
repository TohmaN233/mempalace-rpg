from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks import aerp6_transition_ledger as ledger


def _replay(scores: dict[str, float]) -> dict[str, object]:
    return {"scores": scores}


def test_exact_replay_rank_and_strict_boundary() -> None:
    replay = _replay({"a": 3.0, "b": 2.0, "c": 1.0, **{f"z-{index}": -float(index) for index in range(20)}})
    assert ledger._top_order(replay, 10, {event: index for index, event in enumerate(replay["scores"], 1)})[:3] == ["a", "b", "c"]
    assert ledger._score_ranks(_replay({"a": 3.0, "b": 2.0, "c": 2.0, **{f"z-{index}": -float(index) for index in range(20)}}))["b"] == 2
    assert ledger._boundary(replay)["margin"] > 0.0


def test_protocol_identity_separation() -> None:
    artifact = {
        "source_pool_stream_sha256": {ledger.MAIN_RAW_ARM: "a" * 64},
        "ranking_stream_sha256": {ledger.MAIN_RAW_ARM: "b" * 64},
    }
    protocols = ledger._protocols(artifact)
    assert protocols["main_raw_comparator"]["protocol_sha256"] != protocols["raw_full_order_prefix"]["protocol_sha256"]


@pytest.mark.parametrize(
    ("raw", "final", "expected"),
    [(1, 11, "raw-hit→full-miss"), (11, 1, "raw-miss→full-hit"), (1, 1, "hit→hit"), (None, None, "miss→miss")],
)
def test_transition_classification(raw: int | None, final: int | None, expected: str) -> None:
    assert ledger._transition_class(raw, final) == expected


def test_no_checkpoint_and_joint_removal_are_fixed_views() -> None:
    assert "checkpoint_dense" not in ledger.NO_CHECKPOINT_VIEWS
    assert "observation_dense" not in ledger.JOINT_REMOVAL_VIEWS
    assert "checkpoint_dense" not in ledger.JOINT_REMOVAL_VIEWS
    assert ledger.NO_CHECKPOINT_VIEWS == tuple(view for view in ledger.FULL_VIEW_ORDER if view != "checkpoint_dense")


def test_aerp4_anchor_inputs_fail_closed() -> None:
    with pytest.raises(ValueError, match="Top-10"):
        ledger._aerp4_anchor_inputs(_replay({f"e-{index}": 0.0 for index in range(11)}), _replay({f"e-{index}": 0.0 for index in range(11)}), {f"e-{index}": f"{index:064x}" for index in range(11)}, {f"e-{index}": index + 1 for index in range(11)})


def test_label_join_is_after_unlabeled_ranking_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[str] = []
    artifact = {"source_pool_rankings": {}, "rankings_top10": {}}
    monkeypatch.setattr(ledger.fcd2, "_load", lambda *_args: (artifact, b"artifact", "a" * 64))
    monkeypatch.setattr(ledger.fcd2, "_load_prefreeze", lambda *_args: ({"stream_receipts": {"product_top10_sha256": "b" * 64}}, b"prefreeze", "c" * 64))
    monkeypatch.setattr(ledger, "_validate_frozen_chain", lambda **_kwargs: ({}, b"fcd2", "d" * 64))
    monkeypatch.setattr(ledger.fcd2, "_validate_stream_receipts", lambda *_args, **_kwargs: calls.append("streams"))
    monkeypatch.setattr(ledger, "_protocols", lambda *_args: {"main_raw_comparator": {}, "raw_full_order_prefix": {}})
    monkeypatch.setattr(ledger, "_unlabeled_replay", lambda _artifact: (calls.append("ranking") or {}, {"raw_full_order_prefix_top10_stream_sha256": "e" * 64, "raw_full_order_prefix_score_rank_stream_sha256": "f" * 64}))
    monkeypatch.setattr(ledger, "_label_join", lambda _artifact, _rows: (calls.append("labels") or [], {}))
    monkeypatch.setattr(ledger, "_assert_expected_metrics", lambda _metrics: calls.append("metrics"))
    monkeypatch.setattr(ledger, "_mechanism_gate", lambda _evidence: {"verdict": "PASS"})
    monkeypatch.setattr(ledger.fcd2, "_unchanged", lambda *_args: calls.append("unchanged"))
    monkeypatch.setattr(ledger, "_unchanged_stream", lambda *_args: calls.append("stream"))
    report = ledger.build_transition_ledger(tmp_path / "artifact.json", expected_artifact_sha256="a" * 64, expected_git_head="e" * 40, prefreeze_receipt_path=tmp_path / "prefreeze.json", expected_prefreeze_sha256="c" * 64, fcd2_report_path=tmp_path / "fcd2.json", expected_fcd2_report_sha256="d" * 64, _artifact_bytes=b"artifact")
    assert calls.index("ranking") < calls.index("labels")
    assert report["status"] == "complete"


def test_top10_missing_order_fails_closed() -> None:
    replay = _replay({f"e-{index}": float(20 - index) for index in range(12)})
    with pytest.raises(ValueError, match="lacks"):
        ledger._top_order(replay, 10, {f"e-{index}": index + 1 for index in range(9)})


def test_mechanism_gate_pass_and_fail() -> None:
    rank = lambda raw, final: {"per_view_producer_ordinal_rank": {"raw_bm25": 1}, "fused_competition_ranks": {"raw_full_order_prefix": raw, "final": final}}
    evidence = [{"category": 5, "conversation_sha256": "a", "evidence": [{**rank(1, 11), "transition_class": "raw-hit→full-miss", "first_demotion_stage": "observation_dense"}]}, {"category": 5, "conversation_sha256": "b", "evidence": [{**rank(1, 11), "transition_class": "raw-hit→full-miss", "first_demotion_stage": "checkpoint_dense"}, {**rank(1, 11), "transition_class": "raw-hit→full-miss", "first_demotion_stage": "checkpoint_dense"}, {**rank(11, 1), "transition_class": "raw-miss→full-hit", "first_demotion_stage": None}]}]
    assert ledger._mechanism_gate(evidence)["verdict"] == "PASS"
    evidence[0]["evidence"][0]["per_view_producer_ordinal_rank"] = {"raw_bm25": 51}
    assert ledger._mechanism_gate(evidence)["verdict"] == "FAIL"


def test_producer_ordinal_and_competition_ranks_are_distinct_under_tie() -> None:
    state = {"ledger": {"view_full_order": {view: ["peer", "gold"] for view in ledger.fcd2.VIEWS}}}
    tied = ledger._score_ranks(_replay({"peer": 1.0, "gold": 1.0}))
    stage = {view: tied for view in ledger.ADD_VIEW_ORDER}
    ranks = ledger._derive_evidence_ranks(state, "gold", stage, {"raw_full_order_prefix": tied, "final": tied, "no_checkpoint": tied, "joint_removal": tied})
    assert ranks["per_view_producer_ordinal_rank"]["raw_bm25"] == 2
    assert ranks["fused_competition_ranks"]["raw_full_order_prefix"] == 1


def test_overlap_reports_asymmetric_membership_semantics() -> None:
    events = [f"e-{index}" for index in range(51)]
    ledger_value = {"view_full_order": {view: list(events[1:]) + [events[0]] for view in ledger.fcd2.VIEWS}}
    overlap = ledger._overlap({"ledger": ledger_value, "raw_full_order_prefix": _replay({event: float(51 - index) for index, event in enumerate(events)})})
    assert overlap["raw_membership_semantics"] == "strict_cutoff_score_membership_only_no_internal_order_claim"
    assert overlap["view_membership_semantics"] == "receipt_bound_producer_top50_prefix_membership"
    assert overlap["views"]["raw_bm25"] == {"shared_top50": 49, "raw_top50_fraction": .98, "jaccard": 49 / 51}


def test_safe_report_rejects_nested_plaintext_and_allows_hashes() -> None:
    for forbidden in ("query", "transcript", "answer", "text", "source_event_id", "ranking_key", "item_id"):
        with pytest.raises(ValueError, match="forbidden"):
            ledger._safe_report({"outer": [{forbidden: "forbidden"}]})
    ledger._safe_report({"item_id_sha256": "a" * 64, "evidence_id_sha256": "b" * 64})


def test_stream_recheck_is_constant_memory_and_rejects_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "frozen.bin"; path.write_bytes(b"abcdef")
    expected = ledger._sha(b"abcdef")
    monkeypatch.setattr(Path, "read_bytes", lambda _self: (_ for _ in ()).throw(AssertionError("read_bytes forbidden")))
    ledger._unchanged_stream(path, expected, 6)
    path.write_bytes(b"abcdeg")
    with pytest.raises(RuntimeError, match="changed"):
        ledger._unchanged_stream(path, expected, 6)
