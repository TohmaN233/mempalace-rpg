"""Top-20 story selection stays downstream of complete authorized P5 ranking."""
import hashlib

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from mempalace_rpg.retrieval import RankingResult
from mempalace_rpg.story_store import StoryStore, digest


class Encoder:
    identity = "synthetic-story-selection-test"

    def encode_query(self, text):
        return [1.0]

    def encode_passages(self, texts):
        return [[1.0] for _text in texts]


class RecordingRanker:
    def __init__(self, encoder):
        self.encoder = encoder
        self.calls = []

    def rank(self, *, query, candidates):
        identifiers = [candidate.source_event_id for candidate in candidates]
        self.calls.append((query, identifiers))
        ranked = sorted(identifiers, reverse=True)
        return RankingResult(ranked, {identifier: float(rank) for rank, identifier in enumerate(ranked)},
                             {"input_sha256": "f" * 64})


def packet(identifier):
    text = f"Original story body for {identifier}."
    return {"id": identifier, "purpose": "actor", "actorId": "orin", "representation": "original",
            "text": text, "textSha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "sources": [{"sourceId": "source", "sourceVersion": "version", "startByte": 0,
                         "endByte": 40, "sha256": "b" * 64}],
            "materialRevisionId": "revision", "materialHash": "a" * 64,
            "analysisId": None, "analysisVersion": None}


def projection(count, max_read=100_000):
    binding = {"materialRevisionId": "revision", "materialHash": "a" * 64, "channel": "actor:orin",
               "point": {"pathId": "path", "worldlineId": "world", "order": 3},
               "reviewPolicy": "allow-unreviewed-experimental"}
    entries = [{"id": f"event-{index:02d}", "sceneId": f"scene-{index:02d}", "order": index,
                "original": packet(f"event-{index:02d}"), "summaries": []}
               for index in range(count)]
    return {"binding": binding, "entries": entries,
            "corpusHash": digest({"binding": binding, "entries": entries}),
            "maxReadCharacters": max_read}


def setup_store(count, *, max_read=100_000):
    encoder = Encoder()
    store = StoryStore(encoder)
    ranker = RecordingRanker(encoder)
    store.ranker = ranker
    opened = store.open_view(projection(count, max_read=max_read))
    return store, ranker, opened["viewHandle"]


def test_exclusions_refill_twenty_after_full_p5_rank_and_keep_read_budgets():
    excluded = [f"event-{index:02d}" for index in range(20, 25)]
    selected = [f"event-{index:02d}" for index in range(19, -1, -1)]
    body_length = len("Original story body for event-00.")
    store, ranker, view_handle = setup_store(25, max_read=20 * body_length)

    result = store.search({"viewHandle": view_handle, "query": "promise", "limit": 20,
                           "excludedIds": excluded})

    full_universe = [f"event-{index:02d}" for index in range(25)]
    assert ranker.calls == [("promise", full_universe)]
    assert [row["id"] for row in result["candidates"]] == selected
    assert result["trace"]["rankedIds"] == selected
    assert result["trace"]["inputHash"] == "f" * 64
    assert result["trace"]["selectionPolicy"] == "host-source-nonoverlap-v1"
    assert result["trace"]["excludedIds"] == excluded

    entry_by_id = {entry["id"]: entry for entry in store.views[view_handle]["entries"].values()}
    for row in result["candidates"]:
        packet_read = store.read({"viewHandle": view_handle, "handle": row["handle"],
                                  "representation": "original"})["packet"]
        assert packet_read["id"] == row["id"]
        assert packet_read["text"] == entry_by_id[row["id"]]["original"]["text"]
    assert store.views[view_handle]["read"] == 20 * body_length
    with pytest.raises(EvidenceError, match="budget"):
        store.read({"viewHandle": view_handle, "handle": result["candidates"][0]["handle"],
                    "representation": "original"})
    assert store.views[view_handle]["read"] == 20 * body_length
    with pytest.raises(EvidenceError, match="unexposed"):
        store.read({"viewHandle": view_handle, "handle": "not-issued", "representation": "original"})


@pytest.mark.parametrize("excluded", [None, "event-00", ["event-00", 7], ["event-00", "event-00"], ["missing"]])
def test_malformed_duplicate_and_unauthorized_exclusions_fail_before_ranking(excluded):
    store, ranker, view_handle = setup_store(2)

    with pytest.raises(EvidenceError):
        store.search({"viewHandle": view_handle, "query": "promise", "limit": 1,
                      "excludedIds": excluded})

    assert ranker.calls == []


def test_limit_is_capped_at_twenty_and_trace_matches_selected_rows():
    store, ranker, view_handle = setup_store(12)

    with pytest.raises(EvidenceError, match="range"):
        store.search({"viewHandle": view_handle, "query": "promise", "limit": 21})
    assert ranker.calls == []

    result = store.search({"viewHandle": view_handle, "query": "promise", "limit": 1})
    assert ranker.calls == [("promise", [f"event-{index:02d}" for index in range(12)])]
    assert [row["id"] for row in result["candidates"]] == ["event-11"]
    assert result["trace"]["rankedIds"] == ["event-11"]
    assert "selectionPolicy" not in result["trace"]
    assert "excludedIds" not in result["trace"]

    top_ten = store.search({"viewHandle": view_handle, "query": "promise", "limit": 10})
    assert [row["id"] for row in top_ten["candidates"]] == [f"event-{index:02d}" for index in range(11, 1, -1)]
    assert top_ten["trace"]["rankedIds"] == [row["id"] for row in top_ten["candidates"]]
