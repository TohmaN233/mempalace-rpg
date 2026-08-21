"""Shape, physical phase isolation, and digest checks; runner stays opt-in."""
from collections import Counter

import aerp1_blind_180_harness as harness
from aerp1_blind_180_harness import (
    ALL_CATEGORIES,
    EXPECTED_B0_COMMIT,
    EXPECTED_B0_RANKER_SHA256,
    EXPECTED_ORACLE_SHA256,
    EXPECTED_QUERY_SHA256,
    EXPECTED_SPEC_SHA256,
    NEUTRAL,
    ORACLE_PATH,
    QUERY_PATH,
    SPEC_PATH,
    _file_sha256,
    load_evaluator_oracle,
    load_query_bundle,
    validate_freeze_spec,
)


def test_aerp1_blind_180_freeze_shape_hashes_denominators_and_physical_oracle_isolation(monkeypatch):
    opened = []
    original = harness._file_sha256

    def observed(path):
        opened.append(path)
        return original(path)

    monkeypatch.setattr(harness, "_file_sha256", observed)
    metadata, bundle = load_query_bundle()
    assert ORACLE_PATH not in opened
    assert opened == [SPEC_PATH, QUERY_PATH]
    oracle = load_evaluator_oracle()
    assert opened[-2:] == [SPEC_PATH, ORACLE_PATH]
    cases = bundle["cases"]
    expected = oracle["cases"]
    assert validate_freeze_spec(metadata, bundle) == []
    assert _file_sha256(SPEC_PATH)[1] == EXPECTED_SPEC_SHA256
    assert metadata["query_bundle_sha256"] == EXPECTED_QUERY_SHA256
    assert _file_sha256(QUERY_PATH)[1] == EXPECTED_QUERY_SHA256
    assert _file_sha256(ORACLE_PATH)[1] == EXPECTED_ORACLE_SHA256
    assert metadata["baseline"]["commit"] == EXPECTED_B0_COMMIT
    assert metadata["baseline"]["ranker_source"]["sha256"] == EXPECTED_B0_RANKER_SHA256
    assert len(cases) == len(expected) == 180
    assert Counter(case["category"] for case in cases) == {category: 15 for category in ALL_CATEGORIES}
    assert sum(item["polarity"] == "positive" for item in expected) == 90
    assert sum(item["polarity"] == "negative" for item in expected) == 90
    neutral = [item for item in expected if item["authorization_neutral"]]
    assert len(neutral) == 60
    assert {item["case_id"].rsplit("-", 1)[0] for item in neutral} == NEUTRAL
    assert all({"gold_rank", "recall_at_24", "ndcg_at_24"} <= set(item["b0"]) for item in neutral)
    assert sum(item["b0"]["recall_at_24"] for item in neutral) == 45.0
