"""Synthetic protocol/algorithm boundary checks; no semantic or encoder claim."""
import hashlib
import io

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from mempalace_rpg.story_read_server import serve
from mempalace_rpg.retrieval import FixedP5Policy
from mempalace_rpg.story_store import StoryStore, digest


class Encoder:
    identity = "synthetic-engineering-only"

    def encode_query(self, text):
        return [float("promise" in text.lower()), float("violet" in text.lower()), 1.0]

    def encode_passages(self, texts):
        return [self.encode_query(text) for text in texts]


def packet(text, representation="original"):
    return {"id": "retelling", "purpose": "actor", "actorId": "orin", "representation": representation,
            "text": text, "textSha256": hashlib.sha256(text.encode()).hexdigest(), "sources": [{"sourceId": "source", "sourceVersion": "version", "startByte": 0, "endByte": 40, "sha256": "b" * 64}],
            "materialRevisionId": "revision", "materialHash": "a" * 64,
            "analysisId": None if representation == "original" else "summary", "analysisVersion": None if representation == "original" else 1}


def projection(max_read=1000):
    binding = {"materialRevisionId": "revision", "materialHash": "a" * 64, "channel": "actor:orin",
               "point": {"pathId": "path", "worldlineId": "world", "order": 3},
               "reviewPolicy": "allow-unreviewed-experimental"}
    entries = [{"id": "retelling", "sceneId": "scene", "order": 2,
                "original": packet("Orin heard a promise.🌙"),
                "summaries": [packet("He heard a promise, no password.", "event-summary")]}]
    return {"binding": binding, "entries": entries,
            "corpusHash": digest({"binding": binding, "entries": entries}), "maxReadCharacters": max_read}


def test_actual_fixed_p5_authorized_order_and_immutable_summary_read():
    store = StoryStore(Encoder())
    assert isinstance(store.ranker.routing_policy, FixedP5Policy)
    source = projection()
    opened = store.open_view(source)
    source["entries"][0]["original"]["text"] = "SECRET violet"
    ranked = store.search({"viewHandle": opened["viewHandle"], "query": "promise", "limit": 10})
    assert ranked["trace"]["profile"] == "static-p5"
    assert ranked["trace"]["rankedIds"] == ["retelling"]
    handle = ranked["candidates"][0]["handle"]
    result = store.read({"viewHandle": opened["viewHandle"], "handle": handle, "representation": "event-summary"})
    assert result["packet"]["text"] == "He heard a promise, no password."
    assert result["packet"]["representation"] == "event-summary"
    assert "violet" not in str(result)
    assert store.close_view({"viewHandle": opened["viewHandle"]}) == {"closed": True}
    assert store.views == {}


def test_bound_handles_hash_corruption_and_atomic_unicode_read_budget():
    store = StoryStore(Encoder())
    corrupt = projection()
    corrupt["entries"][0]["summaries"][0]["text"] = "Changed"
    corrupt["corpusHash"] = digest({"binding": corrupt["binding"], "entries": corrupt["entries"]})
    with pytest.raises(ValueError, match="hash mismatch"):
        store.open_view(corrupt)
    source = projection(max_read=1)
    opened = store.open_view(source)
    with pytest.raises(EvidenceError, match="unexposed"):
        store.read({"viewHandle": opened["viewHandle"], "handle": "not-issued", "representation": "original"})
    ranked = store.search({"viewHandle": opened["viewHandle"], "query": "promise", "limit": 10})
    with pytest.raises(EvidenceError, match="budget"):
        store.read({"viewHandle": opened["viewHandle"], "handle": ranked["candidates"][0]["handle"], "representation": "original"})
    assert store.views[opened["viewHandle"]]["read"] == 0
    other = store.open_view(projection())
    with pytest.raises(EvidenceError, match="unexposed"):
        store.read({"viewHandle": other["viewHandle"], "handle": ranked["candidates"][0]["handle"], "representation": "original"})


def test_empty_view_legal_and_protocol_failure_is_not_empty_success():
    store = StoryStore(Encoder())
    source = projection()
    source["entries"] = []
    source["corpusHash"] = digest({"binding": source["binding"], "entries": []})
    opened = store.open_view(source)
    assert store.search({"viewHandle": opened["viewHandle"], "query": "promise", "limit": 1})["candidates"] == []
    output = io.StringIO()
    serve(store, input_stream=io.StringIO('{"jsonrpc":"2.0","id":1,"method":"read","params":{"viewHandle":"invalid","handle":"hidden","representation":"original"}}\n'),
          output_stream=output, diagnostic_stream=io.StringIO())
    assert '"error"' in output.getvalue()
    assert '"result"' not in output.getvalue()
    assert store.views == {}
