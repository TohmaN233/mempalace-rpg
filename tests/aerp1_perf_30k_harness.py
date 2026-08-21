"""Frozen 30k-event AERP-1 performance corpus and measurement helpers."""
from __future__ import annotations

from contextlib import ExitStack
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import itertools
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
from statistics import median
import subprocess
import sys
import time
from typing import Any
from unittest import mock

import psutil


FREEZE_ROOT = Path(__file__).resolve().parents[1]
TARGET_ROOT = Path(os.environ.get("AERP1_PERF_TARGET_ROOT", FREEZE_ROOT)).resolve()
if str(TARGET_ROOT) not in sys.path:
    sys.path.insert(0, str(TARGET_ROOT))

from mempalace_rpg import NullEpisodeAdapter, RpgMemoryKernel, SceneEventInput  # noqa: E402
import mempalace_rpg.kernel as kernel_module  # noqa: E402


MANIFEST_PATH = Path(__file__).with_name("fixtures") / "aerp1_perf_30k_manifest.json"
EXPECTED_MANIFEST_SHA256 = "795c265419310fbd0edaeb6f2bf0d57baf698d640736083f1d1e88569f69de7e"


def _file_sha256(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def load_manifest() -> tuple[dict[str, Any], str]:
    raw, digest = _file_sha256(MANIFEST_PATH)
    if digest != EXPECTED_MANIFEST_SHA256:
        raise ValueError("frozen 30k performance manifest sha256 mismatch")
    manifest = json.loads(raw)
    dataset = manifest["dataset"]
    if dataset["scene_count"] * dataset["events_per_scene"] != dataset["event_count"]:
        raise ValueError("frozen 30k corpus denominator mismatch")
    if dataset["event_count"] * dataset["memory_items_per_event"] != dataset["expected_memory_item_count"]:
        raise ValueError("frozen 30k projection denominator mismatch")
    return manifest, digest


def _git_output(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout


def git_state(root: Path = TARGET_ROOT) -> dict[str, Any]:
    head = _git_output(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _git_output(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    revision = _git_output(root, "rev-list", "--parents", "-n", "1", "HEAD").decode("ascii").split()
    commit_patch = _git_output(
        root, "diff-tree", "--root", "--no-commit-id", "--binary", "--full-index",
        "--no-ext-diff", "--no-color", "-p", "HEAD", "--",
    )
    status = _git_output(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    return {
        "git_head": head,
        "git_tree": tree,
        "git_parents": revision[1:],
        "git_dirty": bool(status),
        "commit_diff": {
            "algorithm": "sha256",
            "canonicalization": "raw bytes from git diff-tree --root --no-commit-id --binary --full-index --no-ext-diff --no-color -p HEAD --",
            "sha256": hashlib.sha256(commit_patch).hexdigest(),
            "byte_count": len(commit_patch),
        },
        "worktree_status": {
            "algorithm": "sha256",
            "canonicalization": "raw NUL-delimited bytes from git status --porcelain=v1 -z --untracked-files=all",
            "sha256": hashlib.sha256(status).hexdigest(),
            "byte_count": len(status),
        },
    }


def validate_target(mode: str, manifest: dict[str, Any], state: dict[str, Any]) -> None:
    if mode not in {"current", "b0"}:
        raise ValueError("mode must be current or b0")
    if mode == "b0":
        baseline = manifest["baseline"]
        if state["git_head"] != baseline["commit"]:
            raise ValueError("b0 mode requires the exact pinned B0 commit")
        source = _git_output(TARGET_ROOT, "show", "HEAD:mempalace_rpg/kernel.py")
        if hashlib.sha256(source).hexdigest() != baseline["ranker_source"]["sha256"]:
            raise ValueError("b0 ranker source sha256 mismatch")


def environment_fingerprint() -> dict[str, str]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "sqlite": sqlite3.sqlite_version,
        "psutil": psutil.__version__,
    }


def _id_factory():
    counters: dict[str, itertools.count] = {}

    def deterministic_id(prefix: str) -> str:
        counter = counters.setdefault(prefix, itertools.count())
        return f"{prefix}_perf_{next(counter):012d}"

    return deterministic_id


def _clock_factory():
    counter = itertools.count()
    epoch = datetime(2026, 8, 21, tzinfo=timezone.utc)

    def deterministic_now() -> str:
        return (epoch + timedelta(microseconds=next(counter))).isoformat()

    return deterministic_now


def _event(scene_index: int, event_index: int, actor_id: str, location_id: str, quest_id: str) -> SceneEventInput:
    visibility = (
        ["public_world"] * 6
        + ["rumor_public", "witnessed_only", "character_private", "gm_only"]
    )[event_index]
    anchor = ("alpha", "beta", "gamma")[scene_index % 3]
    summary = f"public fact {scene_index:06d} detail {event_index:02d}"
    if event_index == 0:
        summary += f" authorization neutral anchor {anchor}"
    values: dict[str, Any] = {
        "event_type": "evidence",
        "summary": summary,
        "truth_status": "canonical",
        "visibility": visibility,
        "witness_set": [actor_id] if visibility == "witnessed_only" else [],
        "related_quests": [quest_id],
        "related_locations": [location_id],
        "source_span": f"PERF SPAN {scene_index:06d} {event_index:02d}",
        "importance": float(9 - event_index) / 10.0,
        "emotional_weight": float(event_index % 3) / 10.0,
        "branch_id": "main",
        "branch_status": "active",
        "access_owner_id": actor_id if visibility == "character_private" else None,
    }
    supported = SceneEventInput.__dataclass_fields__
    return SceneEventInput(**{key: value for key, value in values.items() if key in supported})


def seed_corpus(kernel: RpgMemoryKernel, manifest: dict[str, Any]) -> dict[str, Any]:
    dataset = manifest["dataset"]
    actor_id = dataset["actor_id"]
    profile = dataset["profile"]
    kernel.upsert_character_profile(
        character_id=actor_id,
        display_name=profile["display_name"],
        tier=profile["tier"],
        short_persona=profile["short_persona"],
        memory_wing=profile["memory_wing"],
    )
    started = time.perf_counter_ns()
    for scene_index in range(dataset["scene_count"]):
        location_id = f"location-perf-{scene_index % dataset['location_count']:02d}"
        quest_id = f"quest-perf-{scene_index % dataset['quest_count']:02d}"
        events = [
            _event(scene_index, event_index, actor_id, location_id, quest_id)
            for event_index in range(dataset["events_per_scene"])
        ]
        kernel.commit_scene(
            campaign_id=dataset["campaign_id"],
            scene_id=f"{dataset['scene_id_prefix']}{scene_index:0{dataset['scene_id_width']}d}",
            in_world_time=f"day-{scene_index:06d}",
            location_id=location_id,
            active_quest_ids=[quest_id],
            participants=[actor_id],
            witnesses=[actor_id],
            transcript=" | ".join(str(event.source_span) for event in events),
            events=events,
        )
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    conn = kernel._conn()
    counts = {
        "scene_record": conn.execute("SELECT COUNT(*) FROM scene_record").fetchone()[0],
        "scene_event": conn.execute("SELECT COUNT(*) FROM scene_event").fetchone()[0],
        "memory_item": conn.execute("SELECT COUNT(*) FROM memory_item").fetchone()[0],
    }
    expected = {
        "scene_record": dataset["scene_count"],
        "scene_event": dataset["event_count"],
        "memory_item": dataset["expected_memory_item_count"],
    }
    if counts != expected:
        raise AssertionError(f"seeded corpus shape mismatch: {counts!r} != {expected!r}")
    scene_visibility = {
        str(row["visibility"]): int(row["count"])
        for row in conn.execute(
            "SELECT visibility, COUNT(*) AS count FROM scene_event GROUP BY visibility"
        )
    }
    memory_visibility = {
        str(row["visibility"]): int(row["count"])
        for row in conn.execute(
            "SELECT visibility, COUNT(*) AS count FROM memory_item GROUP BY visibility"
        )
    }
    witnessed_rows = conn.execute(
        "SELECT witness_set_json FROM scene_event WHERE visibility='witnessed_only'"
    ).fetchall()
    witnessed_actor_matches = sum(
        actor_id in json.loads(row["witness_set_json"]) for row in witnessed_rows
    )
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(scene_event)")}
    access_owner_supported = "access_owner_id" in columns
    private_owner_matches = None
    if access_owner_supported:
        private_owner_matches = conn.execute(
            "SELECT COUNT(*) FROM scene_event WHERE visibility='character_private' AND access_owner_id=?",
            (actor_id,),
        ).fetchone()[0]
    distribution = {
        "scene_event_visibility_counts": scene_visibility,
        "memory_item_visibility_counts": memory_visibility,
        "witnessed_actor_matches": witnessed_actor_matches,
        "access_owner_supported": access_owner_supported,
        "character_private_owner_matches": private_owner_matches,
    }
    return {"elapsed_ms": elapsed_ms, "counts": counts, "distribution": distribution}


def _product_value(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _product_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _call(kernel: RpgMemoryKernel, case: dict[str, Any], manifest: dict[str, Any]) -> bytes:
    dataset = manifest["dataset"]
    common = {
        "actor_id": dataset["actor_id"],
        "actor_type": dataset["actor_type"],
        "query": case["query"],
    }
    path = case["path"]
    if path == "ordinary":
        function = kernel.build_memory_pack
        kwargs = {**common, "location_id": "location-perf-00", "active_quest_ids": ["quest-perf-00"]}
    elif path == "deep":
        function = kernel.deep_recall
        kwargs = {**common, "location_id": "location-perf-00", "active_quest_ids": ["quest-perf-00"]}
    elif path == "get_scene_transcript":
        function = kernel.get_scene_transcript
        kwargs = {**common, "scene_id": case["scene_id"], "mode": "snippets"}
    else:
        raise ValueError(f"unknown performance path: {path}")
    if "campaign_id" in inspect.signature(function).parameters:
        kwargs["campaign_id"] = dataset["campaign_id"]
    return _canonical_bytes(function(**kwargs))


def _measure_calls(
    kernel: RpgMemoryKernel,
    cases: list[dict[str, Any]],
    manifest: dict[str, Any],
    process: psutil.Process,
) -> tuple[list[dict[str, Any]], int]:
    measurement = manifest["measurement"]
    peak_rss = process.memory_info().rss
    for index in range(measurement["warmup_iterations"]):
        _call(kernel, cases[index % len(cases)], manifest)
        peak_rss = max(peak_rss, process.memory_info().rss)
    records: list[dict[str, Any]] = []
    for index in range(measurement["measured_iterations"]):
        case = cases[index % len(cases)]
        started = time.perf_counter_ns()
        rendered = _call(kernel, case, manifest)
        elapsed = time.perf_counter_ns() - started
        peak_rss = max(peak_rss, process.memory_info().rss)
        records.append({
            "query_id": case["id"],
            "path": case["path"],
            "authorization_neutral": bool(case["authorization_neutral"]),
            "duration_ns": elapsed,
            "response_json_bytes": len(rendered),
        })
    return records, peak_rss


def _commit_probe(kernel: RpgMemoryKernel, manifest: dict[str, Any], repeat_index: int) -> list[int]:
    dataset = manifest["dataset"]
    probe = manifest["measurement"]["commit_probe"]
    actor_id = dataset["actor_id"]
    if repeat_index >= probe["process_repeats"]:
        return []
    total = probe["warmup_iterations"] + probe["measured_iterations"]
    durations: list[int] = []
    for sample_index in range(total):
        scene_index = dataset["scene_count"] + repeat_index * total + sample_index
        location_id = "location-perf-commit"
        quest_id = "quest-perf-commit"
        events = [_event(scene_index, i, actor_id, location_id, quest_id) for i in range(probe["events_per_scene"])]
        started = time.perf_counter_ns()
        kernel.commit_scene(
            campaign_id=dataset["campaign_id"],
            scene_id=f"perf-commit-{repeat_index:02d}-{sample_index:04d}",
            in_world_time=f"commit-{sample_index:04d}",
            location_id=location_id,
            active_quest_ids=[quest_id],
            participants=[actor_id],
            witnesses=[actor_id],
            transcript=" | ".join(str(event.source_span) for event in events),
            events=events,
        )
        elapsed = time.perf_counter_ns() - started
        if sample_index >= probe["warmup_iterations"]:
            durations.append(elapsed)
    return durations


def run_repeat(db_path: str, *, mode: str, repeat_index: int) -> dict[str, Any]:
    manifest, digest = load_manifest()
    queries = manifest["queries"]
    if mode == "b0":
        queries = [case for case in queries if case["path"] == "ordinary" and case["authorization_neutral"]]
    process = psutil.Process()
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(kernel_module, "_id", _id_factory()))
        stack.enter_context(mock.patch.object(kernel_module, "_utcnow", _clock_factory()))
        kernel = RpgMemoryKernel(db_path=db_path, episode_adapter=NullEpisodeAdapter())
        stack.callback(kernel.close)
        seed = seed_corpus(kernel, manifest)
        rss_before = process.memory_info().rss
        records: list[dict[str, Any]] = []
        peak_rss = rss_before
        paths = ["ordinary"] if mode == "b0" else ["ordinary", "deep", "get_scene_transcript"]
        for path in paths:
            selected = sorted((case for case in queries if case["path"] == path), key=lambda case: case["id"])
            path_records, path_peak = _measure_calls(kernel, selected, manifest, process)
            records.extend(path_records)
            peak_rss = max(peak_rss, path_peak)
        commit_durations = _commit_probe(kernel, manifest, repeat_index)
    return {
        "schema": "aerp1-performance-30k-repeat",
        "version": 2,
        "mode": mode,
        "repeat_index": repeat_index,
        "manifest_sha256": digest,
        "environment": environment_fingerprint(),
        "seed": seed,
        "rss_before_bytes": rss_before,
        "peak_rss_bytes": peak_rss,
        "rss_delta_bytes": max(0, peak_rss - rss_before),
        "samples": records,
        "commit_10_event_duration_ns": commit_durations,
    }


def percentile_ms(values_ns: list[int], percentile: float) -> float:
    if not values_ns:
        raise ValueError("cannot calculate a percentile of an empty sample")
    ordered = sorted(values_ns)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index] / 1_000_000.0


def _is_finite_number(value: Any, *, positive: bool = False) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and (value > 0 if positive else value >= 0)
    )


def _expected_sample_keys(mode: str, manifest: dict[str, Any]) -> Counter[tuple[str, str, bool]]:
    measured = manifest["measurement"]["measured_iterations"]
    paths = ("ordinary",) if mode == "b0" else ("ordinary", "deep", "get_scene_transcript")
    expected: Counter[tuple[str, str, bool]] = Counter()
    for path in paths:
        cases = sorted(
            (
                case for case in manifest["queries"]
                if case["path"] == path
                and (mode != "b0" or bool(case["authorization_neutral"]))
            ),
            key=lambda case: case["id"],
        )
        for index in range(measured):
            case = cases[index % len(cases)]
            expected[(case["id"], path, bool(case["authorization_neutral"]))] += 1
    return expected


def validate_repeats(
    repeats: list[dict[str, Any]], mode: str, manifest: dict[str, Any], manifest_sha256: str
) -> list[str]:
    errors: list[str] = []
    expected_indices = set(range(manifest["measurement"]["process_repeats"]))
    indices = [repeat.get("repeat_index") for repeat in repeats]
    if len(repeats) != len(expected_indices) or set(indices) != expected_indices or len(indices) != len(set(indices)):
        errors.append("repeat_indices")
    expected_counts = {
        "scene_record": manifest["dataset"]["scene_count"],
        "scene_event": manifest["dataset"]["event_count"],
        "memory_item": manifest["dataset"]["expected_memory_item_count"],
    }
    expected_distribution = {
        "scene_event_visibility_counts": manifest["dataset"]["expected_scene_event_visibility_counts"],
        "memory_item_visibility_counts": manifest["dataset"]["expected_memory_item_visibility_counts"],
        "witnessed_actor_matches": manifest["dataset"]["security_expectations"]["witnessed_actor_matches"],
        "access_owner_supported": mode == "current",
        "character_private_owner_matches": (
            manifest["dataset"]["security_expectations"]["current_character_private_owner_matches"]
            if mode == "current" else None
        ),
    }
    expected_samples = _expected_sample_keys(mode, manifest)
    expected_environment = environment_fingerprint()
    commit_repeat_count = manifest["measurement"]["commit_probe"]["process_repeats"]
    commit_samples = manifest["measurement"]["commit_probe"]["measured_iterations"]
    for repeat in repeats:
        index = repeat.get("repeat_index")
        prefix = f"repeat_{index}"
        if repeat.get("schema") != "aerp1-performance-30k-repeat" or repeat.get("version") != 2:
            errors.append(f"{prefix}:schema")
        if repeat.get("mode") != mode or repeat.get("manifest_sha256") != manifest_sha256:
            errors.append(f"{prefix}:mode_or_manifest")
        if repeat.get("environment") != expected_environment:
            errors.append(f"{prefix}:environment")
        if repeat.get("seed", {}).get("counts") != expected_counts:
            errors.append(f"{prefix}:seed_counts")
        if repeat.get("seed", {}).get("distribution") != expected_distribution:
            errors.append(f"{prefix}:seed_distribution")
        if not _is_finite_number(repeat.get("seed", {}).get("elapsed_ms"), positive=True):
            errors.append(f"{prefix}:seed_elapsed")
        samples = repeat.get("samples")
        if not isinstance(samples, list):
            errors.append(f"{prefix}:samples")
            continue
        actual_samples: Counter[tuple[str, str, bool]] = Counter()
        for sample in samples:
            key = (sample.get("query_id"), sample.get("path"), sample.get("authorization_neutral"))
            actual_samples[key] += 1
            if not isinstance(sample.get("duration_ns"), int) or isinstance(sample.get("duration_ns"), bool) or sample["duration_ns"] <= 0:
                errors.append(f"{prefix}:duration")
            if not isinstance(sample.get("response_json_bytes"), int) or isinstance(sample.get("response_json_bytes"), bool) or sample["response_json_bytes"] < 0:
                errors.append(f"{prefix}:response_size")
        if actual_samples != expected_samples:
            errors.append(f"{prefix}:sample_plan")
        for name in ("rss_before_bytes", "peak_rss_bytes", "rss_delta_bytes"):
            if not isinstance(repeat.get(name), int) or isinstance(repeat.get(name), bool) or repeat[name] < 0:
                errors.append(f"{prefix}:{name}")
        if all(isinstance(repeat.get(name), int) for name in ("rss_before_bytes", "peak_rss_bytes", "rss_delta_bytes")):
            expected_delta = max(0, repeat["peak_rss_bytes"] - repeat["rss_before_bytes"])
            if repeat["rss_delta_bytes"] != expected_delta:
                errors.append(f"{prefix}:rss_delta_consistency")
        commits = repeat.get("commit_10_event_duration_ns")
        expected_commit_samples = commit_samples if isinstance(index, int) and index < commit_repeat_count else 0
        if not isinstance(commits, list) or len(commits) != expected_commit_samples or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (commits or [])
        ):
            errors.append(f"{prefix}:commit_samples")
    return sorted(set(errors))


def _repeat_metrics(repeat: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    samples = repeat["samples"]
    for path in sorted({sample["path"] for sample in samples}):
        values = [sample["duration_ns"] for sample in samples if sample["path"] == path]
        result[path] = {"p95_ms": percentile_ms(values, 0.95), "p99_ms": percentile_ms(values, 0.99)}
    neutral = [
        sample["duration_ns"] for sample in samples
        if sample["path"] == "ordinary" and sample["authorization_neutral"]
    ]
    result["authorization_neutral_ordinary_p95_ms"] = percentile_ms(neutral, 0.95)
    return result


def aggregate_repeats(
    repeats: list[dict[str, Any]],
    *,
    mode: str,
    manifest: dict[str, Any],
    manifest_sha256: str,
    state: dict[str, Any],
    b0_report: dict[str, Any] | None,
) -> dict[str, Any]:
    repeat_errors = validate_repeats(repeats, mode, manifest, manifest_sha256)
    if repeat_errors:
        raise ValueError("invalid worker evidence: " + ",".join(repeat_errors))
    repeat_metrics = {str(repeat["repeat_index"]): _repeat_metrics(repeat) for repeat in repeats}
    samples = [
        {**sample, "repeat_index": repeat["repeat_index"]}
        for repeat in repeats for sample in repeat["samples"]
    ]
    paths = ("ordinary",) if mode == "b0" else ("ordinary", "deep", "get_scene_transcript")
    metrics: dict[str, Any] = {
        "repeat_count": len(repeats),
        "sample_count": len(samples),
        "rss_delta_max_bytes": max(repeat["rss_delta_bytes"] for repeat in repeats),
        "response_json_max_bytes": max(sample["response_json_bytes"] for sample in samples),
        "commit_10_event_p95_ms": percentile_ms(
            [value for repeat in repeats for value in repeat["commit_10_event_duration_ns"]], 0.95
        ),
        "repeat_metrics": repeat_metrics,
    }
    for path in paths:
        values = [sample["duration_ns"] for sample in samples if sample["path"] == path]
        metrics[path] = {
            "p95_ms": max(item[path]["p95_ms"] for item in repeat_metrics.values()),
            "p99_ms": max(item[path]["p99_ms"] for item in repeat_metrics.values()),
            "pooled_p95_ms": percentile_ms(values, 0.95),
            "pooled_p99_ms": percentile_ms(values, 0.99),
            "gate_rule": "worst_repeat",
        }
    neutral = [
        sample["duration_ns"] for sample in samples
        if sample["path"] == "ordinary" and sample["authorization_neutral"]
    ]
    metrics["authorization_neutral_ordinary_p95_ms"] = max(
        item["authorization_neutral_ordinary_p95_ms"] for item in repeat_metrics.values()
    )
    metrics["authorization_neutral_ordinary_pooled_p95_ms"] = percentile_ms(neutral, 0.95)

    thresholds = manifest["thresholds"]
    gate_errors: list[str] = []
    for path in paths:
        for name in ("p95_ms", "p99_ms"):
            if metrics[path][name] > thresholds[path][name]:
                gate_errors.append(f"{path}:{name}:{metrics[path][name]:.6f}>{thresholds[path][name]:.6f}")
    if metrics["rss_delta_max_bytes"] > thresholds["rss_delta"]["max_bytes"]:
        gate_errors.append("rss_delta_above_frozen_threshold")
    if metrics["response_json_max_bytes"] > thresholds["response_json"]["max_bytes"]:
        gate_errors.append("response_json_above_frozen_threshold")
    if metrics["commit_10_event_p95_ms"] > thresholds["commit_10_event"]["p95_ms"]:
        gate_errors.append("commit_10_event_p95_above_frozen_threshold")

    baseline_gate: dict[str, Any]
    if mode == "current":
        if b0_report is None:
            baseline_gate = {"status": "NOT_EVALUATED", "reason": "validated_b0_report_required"}
            gate_errors.append("authorization_neutral_b0_ratio_not_evaluated")
        else:
            baseline_errors = validate_b0_report(b0_report, manifest_sha256, manifest)
            if baseline_errors:
                baseline_gate = {"status": "INVALID", "errors": baseline_errors}
                gate_errors.extend(f"invalid_b0_report:{error}" for error in baseline_errors)
            else:
                b0_repeat_p95 = sorted(
                    float(item["authorization_neutral_ordinary_p95_ms"])
                    for item in b0_report["metrics"]["repeat_metrics"].values()
                )
                b0_p95 = float(median(b0_repeat_p95))
                ratio = metrics["authorization_neutral_ordinary_p95_ms"] / b0_p95
                maximum = thresholds["authorization_neutral_ordinary"]["baseline_ratio_max"]
                passed = math.isfinite(ratio) and ratio <= maximum
                baseline_gate = {"status": "PASS" if passed else "FAIL", "b0_repeat_p95_ms": b0_repeat_p95, "b0_median_p95_ms": b0_p95, "current_worst_repeat_p95_ms": metrics["authorization_neutral_ordinary_p95_ms"], "ratio": ratio, "maximum": maximum, "gate_rule": "current_worst_repeat_over_b0_median_repeat"}
                if not passed:
                    gate_errors.append("authorization_neutral_ordinary_p95_ratio_above_frozen_threshold")
    else:
        baseline_gate = {"status": "BASELINE_MEASURED"}

    return {
        "schema": "aerp1-performance-30k-report",
        "version": 2,
        "mode": mode,
        "manifest_sha256": manifest_sha256,
        "runtime": {**state, "environment": environment_fingerprint()},
        "denominators": {
            "scenes_per_repeat": manifest["dataset"]["scene_count"],
            "events_per_repeat": manifest["dataset"]["event_count"],
            "memory_items_per_repeat": manifest["dataset"]["expected_memory_item_count"],
            "process_repeats": manifest["measurement"]["process_repeats"],
            "samples_per_path_per_repeat": manifest["measurement"]["measured_iterations"],
            "commit_process_repeats": manifest["measurement"]["commit_probe"]["process_repeats"],
            "commit_samples": manifest["measurement"]["commit_probe"]["measured_iterations"],
        },
        "thresholds": thresholds,
        "metrics": metrics,
        "baseline_gate": baseline_gate,
        "repeat_summaries": [
            {
                key: repeat[key]
                for key in (
                    "schema", "version", "mode", "repeat_index", "manifest_sha256", "environment",
                    "seed", "rss_before_bytes", "peak_rss_bytes", "rss_delta_bytes",
                    "commit_10_event_duration_ns",
                )
            }
            for repeat in repeats
        ],
        "samples": samples,
        "aggregate": {"verdict": "PASS" if not gate_errors else "FAIL", "gate_errors": gate_errors},
    }


def _same_git_state(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("git_head", "git_tree", "git_parents", "git_dirty", "commit_diff", "worktree_status")
    return all(before.get(key) == after.get(key) for key in keys)


def _reconstruct_report_repeats(report: dict[str, Any]) -> list[dict[str, Any]]:
    samples = report.get("samples")
    summaries = report.get("repeat_summaries")
    if not isinstance(samples, list) or not isinstance(summaries, list):
        return []
    repeats = []
    for summary in summaries:
        index = summary.get("repeat_index")
        repeat = dict(summary)
        repeat["samples"] = [
            {key: value for key, value in sample.items() if key != "repeat_index"}
            for sample in samples if sample.get("repeat_index") == index
        ]
        repeats.append(repeat)
    return repeats


def validate_b0_report(report: dict[str, Any], manifest_sha256: str, manifest: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("schema") != "aerp1-performance-30k-report" or report.get("version") != 2 or report.get("mode") != "b0":
        errors.append("schema_or_mode")
    if report.get("manifest_sha256") != manifest_sha256:
        errors.append("manifest_sha256")
    runtime = report.get("runtime", {})
    after = report.get("runtime_after", {})
    if runtime.get("git_head") != manifest["baseline"]["commit"]:
        errors.append("git_head")
    if runtime.get("git_dirty") is not False or after.get("git_dirty") is not False or not _same_git_state(runtime, after):
        errors.append("git_state")
    if runtime.get("environment") != environment_fingerprint():
        errors.append("environment")
    expected_denominators = {
        "scenes_per_repeat": manifest["dataset"]["scene_count"],
        "events_per_repeat": manifest["dataset"]["event_count"],
        "memory_items_per_repeat": manifest["dataset"]["expected_memory_item_count"],
        "process_repeats": manifest["measurement"]["process_repeats"],
        "samples_per_path_per_repeat": manifest["measurement"]["measured_iterations"],
        "commit_process_repeats": manifest["measurement"]["commit_probe"]["process_repeats"],
        "commit_samples": manifest["measurement"]["commit_probe"]["measured_iterations"],
    }
    if report.get("denominators") != expected_denominators:
        errors.append("denominators")
    repeats = _reconstruct_report_repeats(report)
    errors.extend(validate_repeats(repeats, "b0", manifest, manifest_sha256))
    metrics = report.get("metrics", {})
    if not errors:
        recomputed_by_index = {
            str(repeat["repeat_index"]): _repeat_metrics(repeat) for repeat in repeats
        }
        recomputed = list(recomputed_by_index.values())
        neutral = max(item["authorization_neutral_ordinary_p95_ms"] for item in recomputed)
        ordinary_p95 = max(item["ordinary"]["p95_ms"] for item in recomputed)
        ordinary_p99 = max(item["ordinary"]["p99_ms"] for item in recomputed)
        checks = (
            (metrics.get("authorization_neutral_ordinary_p95_ms"), neutral),
            (metrics.get("ordinary", {}).get("p95_ms"), ordinary_p95),
            (metrics.get("ordinary", {}).get("p99_ms"), ordinary_p99),
        )
        if any(
            not _is_finite_number(actual, positive=True)
            or not math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-12)
            for actual, expected in checks
        ):
            errors.append("recomputed_metrics")
        if metrics.get("repeat_metrics") != recomputed_by_index:
            errors.append("recomputed_repeat_metrics")
    if report.get("baseline_gate", {}).get("status") != "BASELINE_MEASURED":
        errors.append("baseline_status")
    return sorted(set(errors))


__all__ = [
    "EXPECTED_MANIFEST_SHA256", "FREEZE_ROOT", "MANIFEST_PATH", "TARGET_ROOT",
    "aggregate_repeats", "environment_fingerprint", "git_state", "load_manifest",
    "run_repeat", "validate_b0_report", "validate_repeats", "validate_target",
]
