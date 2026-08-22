from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from benchmarks import aerp5_product_paired_locomo as runner


def _dialogs(count: int = 10):
    return [{"id": f"dialog-{index}", "text": f"text {index}"} for index in range(count)]


class _Collection:
    def __init__(self):
        self.upserts = []

    def upsert(self, **kwargs):
        self.upserts.append(kwargs)


class _Palace:
    def __init__(self):
        self.collection = _Collection()
        self.calls = []

    def get_collection(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.collection


class _Searcher:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def search_memories(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"results": self.rows}


def test_original_arm_uses_product_collection_upsert_and_searcher_not_direct_chroma(tmp_path):
    palace = _Palace()
    dialogs = _dialogs()
    searcher = _Searcher([{"source_path": row["id"]} for row in dialogs])
    ranked = runner.original_product_rank(
        palace=palace,
        searcher=searcher,
        palace_path=tmp_path / "palace",
        conversation_id="conv",
        dialogs=dialogs,
        query="q",
        item_id="item",
    )
    assert ranked == [row["id"] for row in dialogs]
    assert len(palace.calls) == len(palace.collection.upserts) == len(searcher.calls) == 1
    assert palace.collection.upserts[0]["metadatas"][0]["source_file"] == "dialog-0"
    assert searcher.calls[0][1] == {
        "room": "conv",
        "n_results": 10,
        "max_distance": 0.0,
        "candidate_strategy": "vector",
        "collection_name": runner.ORIGINAL_COLLECTION,
    }
    assert palace.calls[0][1] == {
        "collection_name": runner.ORIGINAL_COLLECTION,
        "create": True,
        "backend": "chroma",
    }


@pytest.mark.parametrize(
    "rows, message",
    [
        ([{"source_path": "unknown"}] * 10, "unknown"),
        ([{"source_path": "dialog-0"}] * 10, "duplicate"),
        ([{"source_path": "dialog-0"}] * 9, "fewer"),
    ],
)
def test_original_mapping_failures_are_closed(tmp_path, rows, message):
    with pytest.raises(RuntimeError, match=message):
        runner.original_product_rank(
            palace=_Palace(),
            searcher=_Searcher(rows),
            palace_path=tmp_path / "palace",
            conversation_id="conv",
            dialogs=_dialogs(),
            query="q",
            item_id="item",
        )


def test_tau_has_no_default_and_is_not_selected(monkeypatch, tmp_path):
    parser = pytest.raises(
        SystemExit,
        runner.main,
        [
            "--dataset",
            "d",
            "--original-root",
            "o",
            "--minilm-model-dir",
            "m",
            "--work-root",
            "w",
            "--output",
            "x",
        ],
    )
    assert parser
    with pytest.raises(ValueError, match="tau"):
        runner.freeze_rankings(
            retrieval=SimpleNamespace(),
            palace=None,
            searcher=None,
            encoder=None,
            tau=float("nan"),
            work_dir=tmp_path,
        )


def test_labels_accessed_only_after_all_rankings_are_complete():
    class Item:
        opaque_conversation_id = "conv"
        category = 1
        category_name = "single-hop"
        corpus_opaque_dialog_ids = tuple(f"dialog-{index}" for index in range(10))

        @property
        def official_exact(self):
            return SimpleNamespace(
                source_evidence_item_count=1,
                unresolved_evidence_item_count=0,
                resolved_opaque_dialog_ids=("dialog-0",),
            )

        @property
        def normalized_repaired(self):
            return SimpleNamespace(
                unique_dialog_denominator=1,
                unresolved_evidence_item_count=0,
                gold_opaque_dialog_ids=("dialog-0",),
            )

    retrieval = SimpleNamespace(retrieval_items={"item": {}})
    scorer = SimpleNamespace(scorer_items={"item": Item()})
    rankings = {
        "original": {"item": [f"dialog-{i}" for i in range(10)]},
        "current": {"item": [f"dialog-{i}" for i in range(10)]},
    }
    aggregate, rows = runner.score_after_freeze(
        scorer=scorer, retrieval=retrieval, rankings=rankings
    )
    assert aggregate["primary_semantics"] == "official_exact" and rows
    with pytest.raises(RuntimeError, match="complete identical"):
        runner.score_after_freeze(
            scorer=scorer,
            retrieval=retrieval,
            rankings={"original": {"item": rankings["original"]["item"]}, "current": {}},
        )


def test_original_pin_dirty_and_external_output_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(
        runner, "git_state", lambda root: {"git_dirty": True, "git_head": runner.ORIGINAL_PIN}
    )
    with pytest.raises(ValueError, match="clean"):
        runner.require_clean_pinned_original(tmp_path)
    monkeypatch.setattr(runner, "git_state", lambda root: {"git_dirty": False, "git_head": "bad"})
    with pytest.raises(ValueError, match="pinned"):
        runner.require_clean_pinned_original(tmp_path)
    with pytest.raises(ValueError, match="outside"):
        runner.require_external_output(runner.ROOT / "receipt.json", runner.ROOT, tmp_path)


def test_freeze_rankings_releases_original_backends_on_failure(monkeypatch, tmp_path):
    calls = []

    def fail(**_kwargs):
        calls.append("produce")
        raise RuntimeError("producer failed")

    monkeypatch.setattr(runner, "_freeze_rankings_open_backends", fail)
    monkeypatch.setattr(
        runner,
        "reset_original_product_backends",
        lambda _path: calls.append("reset") or {"verified_system_released": True},
    )
    with pytest.raises(RuntimeError, match="producer failed"):
        runner.freeze_rankings(
            retrieval=SimpleNamespace(),
            palace=None,
            searcher=None,
            encoder=None,
            tau=0.5,
            work_dir=tmp_path,
        )
    assert calls == ["produce", "reset"]


def test_digest_and_resource_receipts_are_stable(tmp_path):
    rows = {"a": {"item": ["x"]}, "b": {"item": ["y"]}}
    assert runner._canonical(rows) == runner._canonical(rows)
    model = tmp_path / "model"
    model.mkdir()
    (model / "model.onnx").write_bytes(b"model")
    assert runner.file_tree_receipt(model) == runner.file_tree_receipt(model)
    receipt = runner.file_tree_receipt(model)
    runner.require_file_tree_unchanged(receipt)
    (model / "model.onnx").write_bytes(b"drift")
    with pytest.raises(RuntimeError, match="drift"):
        runner.require_file_tree_unchanged(receipt)
    adapter = runner.MiniLMDenseAdapter(
        lambda texts: [[float(len(text))] for text in texts], identity="fake"
    )
    adapter.encode_passages(["one", "two"])
    adapter.encode_query("q")
    assert adapter.receipt() == {
        "passage_text_count": 2,
        "query_text_count": 1,
        "passage_call_count": 1,
        "query_call_count": 1,
    }


def test_pinned_original_environment_overrides_hostile_values_and_restores(monkeypatch):
    hostile = {
        "MEMPALACE_BACKEND_EXPLICIT": "qdrant",
        "MEMPALACE_EMBEDDING_DEVICE": "cuda",
        "MEMPALACE_EMBEDDING_MODEL": "embeddinggemma",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    with runner.pinned_original_environment() as receipt:
        assert {key: os.environ[key] for key in hostile} == runner.PINNED_ORIGINAL_ENVIRONMENT
        assert receipt["values"] == runner.PINNED_ORIGINAL_ENVIRONMENT
    assert {key: os.environ[key] for key in hostile} == hostile


def test_paired_bootstrap_names_distinct_question_and_conversation_estimands():
    rows = [
        {"conversation_id": "large", "recall": {"original": 0.0, "current": 1.0}},
        {"conversation_id": "large", "recall": {"original": 0.0, "current": 1.0}},
        {"conversation_id": "small", "recall": {"original": 1.0, "current": 0.0}},
    ]
    question = runner.paired_group_bootstrap(
        rows,
        current="current",
        original="original",
        estimand="question_macro_cluster_bootstrap",
        resamples=50,
    )
    conversation = runner.paired_group_bootstrap(
        rows,
        current="current",
        original="original",
        estimand="conversation_macro",
        resamples=50,
    )
    assert question["point_estimate"] == pytest.approx(1 / 3)
    assert conversation["point_estimate"] == pytest.approx(0.0)
    assert question["estimand"] != conversation["estimand"]
    assert question["group_count"] == conversation["group_count"] == 2
    assert question["question_count"] == conversation["question_count"] == 3
