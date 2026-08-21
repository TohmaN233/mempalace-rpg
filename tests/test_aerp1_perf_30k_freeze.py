from __future__ import annotations

from collections import Counter
import copy
from pathlib import Path
import subprocess
import sys
import uuid

from aerp1_perf_30k_harness import (
    EXPECTED_MANIFEST_SHA256,
    MANIFEST_PATH,
    _expected_sample_keys,
    _file_sha256,
    aggregate_repeats,
    environment_fingerprint,
    load_manifest,
    validate_b0_report,
)


def test_aerp1_perf_30k_manifest_hash_shape_protocol_and_thresholds_are_frozen():
    manifest, digest = load_manifest()
    assert digest == EXPECTED_MANIFEST_SHA256 == _file_sha256(MANIFEST_PATH)[1]
    dataset = manifest["dataset"]
    assert dataset["scene_count"] == 3000
    assert dataset["events_per_scene"] == 10
    assert dataset["event_count"] == 30000
    assert dataset["expected_memory_item_count"] == 90000
    assert sum(dataset["visibility_mix_per_scene"].values()) == 10
    assert dataset["expected_scene_event_visibility_counts"] == {
        "public_world": 18000, "rumor_public": 3000, "witnessed_only": 3000,
        "character_private": 3000, "gm_only": 3000,
    }
    assert dataset["expected_memory_item_visibility_counts"] == {
        "public_world": 54000, "rumor_public": 9000, "witnessed_only": 9000,
        "character_private": 9000, "gm_only": 9000,
    }
    assert Counter(query["path"] for query in manifest["queries"]) == {
        "ordinary": 6, "deep": 3, "get_scene_transcript": 3,
    }
    assert sum(query["authorization_neutral"] for query in manifest["queries"]) == 5
    measurement = manifest["measurement"]
    assert (measurement["warmup_iterations"], measurement["measured_iterations"], measurement["process_repeats"]) == (20, 100, 3)
    assert measurement["timed_boundary"] == "kernel API call plus canonical JSON serialization"
    assert measurement["aggregation"] == {
        "path_percentile_gate": "compute each repeat independently, then gate on the worst repeat",
        "authorization_neutral_b0_ratio": "current worst-repeat ordinary-neutral p95 divided by the median of the three B0 repeat p95 values",
    }
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


def _repeat(manifest, digest, mode, index, *, slow_ordinary=False, duration_ns=100_000_000):
    samples = []
    for (query_id, path, neutral), count in _expected_sample_keys(mode, manifest).items():
        samples.extend({
            "query_id": query_id,
            "path": path,
            "authorization_neutral": neutral,
            "duration_ns": duration_ns,
            "response_json_bytes": 100,
        } for _ in range(count))
    if slow_ordinary:
        ordinary = [sample for sample in samples if sample["path"] == "ordinary"]
        for sample in ordinary[:10]:
            sample["duration_ns"] = 2_000_000_000
    dataset = manifest["dataset"]
    return {
        "schema": "aerp1-performance-30k-repeat",
        "version": 2,
        "mode": mode,
        "repeat_index": index,
        "manifest_sha256": digest,
        "environment": environment_fingerprint(),
        "seed": {
            "elapsed_ms": 1.0,
            "counts": {
                "scene_record": dataset["scene_count"],
                "scene_event": dataset["event_count"],
                "memory_item": dataset["expected_memory_item_count"],
            },
            "distribution": {
                "scene_event_visibility_counts": dataset["expected_scene_event_visibility_counts"],
                "memory_item_visibility_counts": dataset["expected_memory_item_visibility_counts"],
                "witnessed_actor_matches": dataset["security_expectations"]["witnessed_actor_matches"],
                "access_owner_supported": mode == "current",
                "character_private_owner_matches": (
                    dataset["security_expectations"]["current_character_private_owner_matches"]
                    if mode == "current" else None
                ),
            },
        },
        "rss_before_bytes": 1000,
        "peak_rss_bytes": 1100,
        "rss_delta_bytes": 100,
        "samples": samples,
        "commit_10_event_duration_ns": [50_000_000] * (20 if index == 0 else 0),
    }


def _state(head):
    return {
        "git_head": head,
        "git_tree": "tree",
        "git_parents": ["parent"],
        "git_dirty": False,
        "commit_diff": {"sha256": "diff"},
        "worktree_status": {"sha256": "status", "byte_count": 0},
    }


def test_aerp1_perf_gate_uses_worst_repeat_instead_of_pooled_percentile():
    manifest, digest = load_manifest()
    repeats = [_repeat(manifest, digest, "current", index, slow_ordinary=index == 2) for index in range(3)]
    report = aggregate_repeats(
        repeats,
        mode="current",
        manifest=manifest,
        manifest_sha256=digest,
        state=_state("current"),
        b0_report=None,
    )
    assert report["metrics"]["ordinary"]["p95_ms"] == 2000.0
    assert report["metrics"]["ordinary"]["pooled_p95_ms"] == 100.0
    assert any(error.startswith("ordinary:p95") for error in report["aggregate"]["gate_errors"])


def test_aerp1_perf_b0_artifact_validation_recomputes_evidence_and_fails_closed():
    manifest, digest = load_manifest()
    repeats = [_repeat(manifest, digest, "b0", index) for index in range(3)]
    report = aggregate_repeats(
        repeats,
        mode="b0",
        manifest=manifest,
        manifest_sha256=digest,
        state=_state(manifest["baseline"]["commit"]),
        b0_report=None,
    )
    report["runtime_after"] = {key: value for key, value in report["runtime"].items() if key != "environment"}
    assert validate_b0_report(report, digest, manifest) == []

    tampered = copy.deepcopy(report)
    tampered["metrics"]["authorization_neutral_ordinary_p95_ms"] = float("nan")
    assert "recomputed_metrics" in validate_b0_report(tampered, digest, manifest)

    tampered = copy.deepcopy(report)
    tampered["samples"].pop()
    assert any("sample_plan" in error for error in validate_b0_report(tampered, digest, manifest))

    tampered = copy.deepcopy(report)
    tampered["repeat_summaries"][2]["repeat_index"] = 1
    assert "repeat_indices" in validate_b0_report(tampered, digest, manifest)

    tampered = copy.deepcopy(report)
    tampered["runtime_after"]["git_dirty"] = True
    assert "git_state" in validate_b0_report(tampered, digest, manifest)

    tampered = copy.deepcopy(report)
    tampered["runtime"]["environment"]["python"] = "different"
    assert "environment" in validate_b0_report(tampered, digest, manifest)

    tampered = copy.deepcopy(report)
    tampered["repeat_summaries"][0]["seed"]["distribution"]["scene_event_visibility_counts"]["public_world"] -= 1
    assert any("seed_distribution" in error for error in validate_b0_report(tampered, digest, manifest))

    tampered = copy.deepcopy(report)
    tampered["repeat_summaries"][0]["rss_delta_bytes"] = 99
    assert any("rss_delta_consistency" in error for error in validate_b0_report(tampered, digest, manifest))


def test_aerp1_perf_b0_ratio_uses_current_worst_over_b0_median():
    manifest, digest = load_manifest()
    b0_durations = [500_000_000, 500_000_000, 1_000_000_000]
    b0_repeats = [
        _repeat(manifest, digest, "b0", index, duration_ns=duration)
        for index, duration in enumerate(b0_durations)
    ]
    b0 = aggregate_repeats(
        b0_repeats,
        mode="b0",
        manifest=manifest,
        manifest_sha256=digest,
        state=_state(manifest["baseline"]["commit"]),
        b0_report=None,
    )
    b0["runtime_after"] = {key: value for key, value in b0["runtime"].items() if key != "environment"}
    current = aggregate_repeats(
        [_repeat(manifest, digest, "current", index, duration_ns=800_000_000) for index in range(3)],
        mode="current",
        manifest=manifest,
        manifest_sha256=digest,
        state=_state("current"),
        b0_report=b0,
    )
    assert current["baseline_gate"]["b0_median_p95_ms"] == 500.0
    assert current["baseline_gate"]["ratio"] == 1.6
    assert current["baseline_gate"]["status"] == "FAIL"
    assert "authorization_neutral_ordinary_p95_ratio_above_frozen_threshold" in current["aggregate"]["gate_errors"]


def test_aerp1_perf_runner_refuses_output_inside_measured_worktree():
    root = Path(__file__).resolve().parents[1]
    forbidden = root / f".aerp1-perf-forbidden-{uuid.uuid4().hex}.json"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("run_aerp1_perf_30k.py")),
            "--output",
            str(forbidden),
            "--target-root",
            str(root),
        ],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "refusing to write" in result.stderr
    assert not forbidden.exists()
