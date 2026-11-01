from __future__ import annotations

import hashlib
from typing import Sequence

from mempalace_rpg.retrieval import (
    AuthorizedRetrievalCandidate,
    FixedP5Policy,
    SixViewRanker,
)


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [byte / 127.5 - 1.0 for byte in digest[:8]]


class CountingEncoder:
    identity = "stable-matrix-cache-test-encoder"

    def __init__(self) -> None:
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        copied = list(texts)
        self.passage_calls.append(copied)
        return [_vector(text) for text in copied]

    def encode_query(self, query: str) -> list[float]:
        self.query_calls.append(query)
        return _vector("query:" + query)


def _candidates(size: int) -> list[AuthorizedRetrievalCandidate]:
    return [
        AuthorizedRetrievalCandidate(
            source_event_id=f"event-{index:03}",
            source_scene_id=f"scene-{index:03}",
            raw_text=f"memory signal event {index:03} reveals clue {index:03}",
            observation=f"summary=memory signal clue {index:03}\nevent_type=observation",
            checkpoint_key=f"checkpoint-{index:03}",
            policy_tuple=(None, None, None),
            chronological_order_key=(index, f"time-{index:03}"),
            ranking_key=f"ranking-{index:03}",
        )
        for index in range(size)
    ]


def _ranker(encoder: CountingEncoder) -> SixViewRanker:
    return SixViewRanker(encoder, routing_policy=FixedP5Policy())


def test_matrix_cache_stays_at_four_entries_as_corpus_grows() -> None:
    encoder = CountingEncoder()
    ranker = _ranker(encoder)

    for size in range(1, 13):
        ranker.rank(query="memory signal", candidates=_candidates(size))
        assert len(ranker._passage_cache) <= 4

    assert len(encoder.passage_calls) == 12 * 4
    assert {key[1] for key in ranker._passage_cache} == {
        "checkpoint", "raw", "observation", "combo",
    }


def test_resident_matrices_avoid_reencoding_and_return_lru_hits_to_recent() -> None:
    encoder = CountingEncoder()
    ranker = _ranker(encoder)
    candidates = _candidates(5)

    first = ranker.rank(query="memory signal", candidates=candidates)
    second = ranker.rank(query="memory signal", candidates=candidates)
    assert second == first
    assert len(encoder.passage_calls) == 4
    assert encoder.query_calls == ["memory signal", "memory signal"]

    # Exercise the LRU order directly: refreshing "a" protects it when "e"
    # pushes the four-entry matrix cache over its bound.
    for view in ("a", "b", "c", "d"):
        ranker._passage_vectors(view=view, texts=[view], encoder_identity=encoder.identity)
    ranker._passage_vectors(view="a", texts=["a"], encoder_identity=encoder.identity)
    ranker._passage_vectors(view="e", texts=["e"], encoder_identity=encoder.identity)
    assert [key[1] for key in ranker._passage_cache] == ["c", "d", "a", "e"]


def test_evicted_corpus_reproduces_fresh_rank_and_trace() -> None:
    query = "memory signal"
    original_candidates = _candidates(4)
    encoder = CountingEncoder()
    ranker = _ranker(encoder)

    original = ranker.rank(query=query, candidates=original_candidates)
    ranker.rank(query="changed corpus", candidates=_candidates(9))
    assert len(ranker._passage_cache) == 4
    evicted_replay = ranker.rank(query=query, candidates=original_candidates)

    fresh = _ranker(CountingEncoder()).rank(query=query, candidates=original_candidates)
    assert evicted_replay.ranked_event_ids == fresh.ranked_event_ids == original.ranked_event_ids
    assert evicted_replay.scores == fresh.scores == original.scores
    assert evicted_replay.trace == fresh.trace == original.trace
