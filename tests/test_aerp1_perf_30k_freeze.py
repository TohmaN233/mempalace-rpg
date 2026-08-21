from __future__ import annotations

from collections import Counter

from aerp1_perf_30k_harness import EXPECTED_MANIFEST_SHA256, MANIFEST_PATH, _file_sha256, load_manifest


def test_aerp1_perf_30k_manifest_hash_shape_protocol_and_thresholds_are_frozen():
    manifest, digest = load_manifest()
    assert digest == EXPECTED_MANIFEST_SHA256 == _file_sha256(MANIFEST_PATH)[1]
    dataset = manifest["dataset"]
    assert dataset["scene_count"] == 3000
    assert dataset["events_per_scene"] == 10
    assert dataset["event_count"] == 30000
    assert dataset["expected_memory_item_count"] == 90000
    assert sum(dataset["visibility_mix_per_scene"].values()) == 10
    assert Counter(query["path"] for query in manifest["queries"]) == {
        "ordinary": 6, "deep": 3, "get_scene_transcript": 3,
    }
    assert sum(query["authorization_neutral"] for query in manifest["queries"]) == 5
    measurement = manifest["measurement"]
    assert (measurement["warmup_iterations"], measurement["measured_iterations"], measurement["process_repeats"]) == (20, 100, 3)
    assert measurement["timed_boundary"] == "kernel API call plus canonical JSON serialization"
    assert manifest["thresholds"] == {
        "ordinary": {"p95_ms": 1000.0, "p99_ms": 1500.0},
        "deep": {"p95_ms": 1250.0, "p99_ms": 2000.0},
        "get_scene_transcript": {"p95_ms": 100.0, "p99_ms": 150.0},
        "authorization_neutral_ordinary": {"baseline_ratio_max": 1.25, "baseline_metric": "p95_ms"},
        "rss_delta": {"max_bytes": 268435456},
        "response_json": {"max_bytes": 2097152},
        "commit_10_event": {"p95_ms": 100.0},
    }
    assert manifest["baseline"]["commit"] == "8dca38c3d23c0e7c9f36fff10944bb86e71c3819"
    assert manifest["baseline"]["ranker_source"]["sha256"] == "7f8b702bfc1923d8a59c4569e436d43a185dcef39cab31eb7b0b8c0d3073d1d2"
