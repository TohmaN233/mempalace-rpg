"""Navigation exposes only immutable authorized IDs, with ordinary metered reads."""
import pytest

from mempalace_rpg.evidence_protocol import EvidenceError
from test_story_search_selection import projection, setup_store


def test_navigation_does_not_rank_and_handles_remain_view_bound():
    store, ranker, view = setup_store(3)
    opened = store.expose({"viewHandle": view, "ids": ["event-01", "event-00"]})
    assert ranker.calls == []
    assert opened["trace"]["ids"] == ["event-01", "event-00"]
    assert [row["id"] for row in opened["candidates"]] == ["event-01", "event-00"]
    first = opened["candidates"][0]
    packet = store.read({"viewHandle": view, "handle": first["handle"], "representation": "original"})["packet"]
    assert packet["id"] == "event-01"
    assert store.views[view]["read"] == len(packet["text"])
    other = store.open_view(projection(3))["viewHandle"]
    with pytest.raises(EvidenceError, match="unexposed"):
        store.read({"viewHandle": other, "handle": first["handle"], "representation": "original"})


@pytest.mark.parametrize("ids", [[], ["missing"], ["event-00", "event-00"], [7], None, "event-00"])
def test_invalid_navigation_is_atomic(ids):
    store, ranker, view = setup_store(3)
    with pytest.raises(EvidenceError):
        store.expose({"viewHandle": view, "ids": ids})
    assert store.views[view]["handles"] == {}
    assert ranker.calls == []


def test_navigation_cap_and_read_budget_are_enforced():
    store, _ranker, view = setup_store(21, max_read=0)
    with pytest.raises(EvidenceError, match="1 to 20"):
        store.expose({"viewHandle": view, "ids": [f"event-{index:02d}" for index in range(21)]})
    selected = store.expose({"viewHandle": view, "ids": ["event-00"]})["candidates"][0]
    with pytest.raises(EvidenceError, match="budget"):
        store.read({"viewHandle": view, "handle": selected["handle"], "representation": "original"})
