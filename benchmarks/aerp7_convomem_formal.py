"""Fail-closed coordinator contracts for the one-shot formal AERP-7 run.

This module intentionally contains no ConvoMem loader and no default executable
worker.  A candidate worker is given only a pre-validated candidate projection;
the custodian process is the only component allowed to receive a custody loader,
and only after the public ranking endpoint has frozen and a release record binds
every public receipt.  The small injected seams below are exercised solely with
synthetic fixtures until the formal resource/release gate is authorized.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_scoring as score
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256, validate_candidate_projection


FORMAL_PROTOCOL_SCHEMA = "aerp7-convomem-formal-protocol-v1"
RELEASE_SCHEMA = "aerp7-convomem-release-authorization-v1"
REHEARSAL_RELEASE_SCHEMA = "aerp7-convomem-rehearsal-release-authorization-v1"
RESOURCE_SCHEMA = "aerp7-convomem-resource-receipt-v2"
AUDIT_ENVELOPE_SCHEMA = "aerp7-convomem-formal-audit-envelope-v1"
POST_SCORE_ATTESTATION_SCHEMA = "aerp7-convomem-post-score-attestation-v1"
CURRENT_WORKER_SCHEMA = "aerp7-convomem-current-worker-receipt-v1"
CURRENT_EXECUTION_RECEIPT_SCHEMA = "aerp7-convomem-current-execution-receipt-v2"
CURRENT_LIFECYCLE = ("candidate_only", "rank_strong_raw", "rank_static_p5", "repeat_static_p5", "rank_six_view_secondary", "freeze")
ORIGINAL_LIFECYCLE = ("isolated", "ingest_all", "close", "cold_reopen", "query_all", "worker_audit", "coordinator_audit", "freeze")
_FORBIDDEN_CANDIDATE_TOKENS = frozenset({"custody", "secret", "source", "answer", "evidence", "label", "canonical", "premix", "official_root", "source_root"})
_CANDIDATE_WORKER_KEYS = frozenset({"role", "projection_sha256", "projection_raw_sha256", "projection_path", "model_receipt", "code_receipt", "staging_root", "arms", "top_k", "tie_break", "serializer_contract"})
_RESOURCE_ARMS = frozenset(score.FORMAL_ARMS)


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _obj(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return dict(value)


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise CustodyError(code)
    return value


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CustodyError(code)
    return value


def _positive_int(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CustodyError(code)
    return value


def _finite_nonnegative(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
        raise CustodyError(code)
    return float(value)


def _finite(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CustodyError(code)
    return float(value)


def _code_receipt(value: Any, code: str) -> dict[str, Any]:
    row = _obj(value, code)
    if set(row) != {"head", "tree", "diff_digest", "dirty_policy"} or row.get("dirty_policy") != "clean_required":
        raise CustodyError(code)
    for key in ("head", "tree", "diff_digest"):
        _hex(row.get(key), code)
    return row


def _model_receipt(value: Any) -> dict[str, Any]:
    # Reuse the ranker's exact model schema rather than duplicating a looser one.
    return rank._validate_model_receipt(value)


def _candidate_receipt(value: Any) -> dict[str, Any]:
    row = _obj(value, "formal_candidate_receipt_invalid")
    required = {"generation_id", "ready_sha256", "projection_raw_sha256", "projection_canonical_sha256", "query_count", "candidate_text_count"}
    if set(row) != required:
        raise CustodyError("formal_candidate_receipt_invalid")
    _text(row.get("generation_id"), "formal_candidate_receipt_invalid")
    for key in required - {"generation_id"}:
        if key in {"query_count", "candidate_text_count"}: _positive_int(row.get(key), "formal_candidate_receipt_invalid")
        else: _hex(row.get(key), "formal_candidate_receipt_invalid")
    return row


def projection_denominators(value: Any) -> dict[str, int]:
    """Single projection-derived denominator source for every formal receipt."""
    projection = validate_candidate_projection(value)
    return {"query_count": len(projection["items"]), "candidate_text_count": sum(len(corpus["candidates"]) for corpus in projection["corpora"])}


def projection_query_keys(value: Any) -> list[dict[str, str]]:
    """The text-free, canonical per-query binding used by resource sidecars."""
    projection = validate_candidate_projection(value)
    return [{"item_id": item["item_id"], "query_sha256": rank._query_digest(item["query_text"])} for item in sorted(projection["items"], key=lambda item: item["item_id"])]


def _resource_thresholds(value: Any) -> dict[str, Any]:
    row = _obj(value, "formal_resource_thresholds_invalid")
    required = {"resource_comparability", "peak_rss_bytes_max", "storage_bytes_max", "ingest_seconds_max", "index_seconds_max", "query_p95_ns_max"}
    if set(row) != required or row.get("resource_comparability") not in {"strict", "unavailable"}:
        raise CustodyError("formal_resource_thresholds_invalid")
    if row["resource_comparability"] == "unavailable":
        if any(row[key] is not None for key in required - {"resource_comparability"}):
            raise CustodyError("formal_resource_thresholds_invalid")
        return row
    for key in ("peak_rss_bytes_max", "storage_bytes_max", "query_p95_ns_max"):
        _positive_int(row.get(key), "formal_resource_thresholds_invalid")
    for key in ("ingest_seconds_max", "index_seconds_max"):
        if _finite_nonnegative(row.get(key), "formal_resource_thresholds_invalid") <= 0:
            raise CustodyError("formal_resource_thresholds_invalid")
    return row


def protocol_digest(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key != "protocol_sha256"})


def validate_formal_protocol(value: Any) -> dict[str, Any]:
    """Validate the immutable, pre-execution contract; formal mode is never synthetic."""
    row = _obj(value, "formal_protocol_invalid")
    required = {
        "schema", "synthetic_test_mode", "candidate", "current_code_receipt", "original_code_receipt", "source_receipt",
        "model_receipt", "arms", "serializer_contract", "top_k", "tie_break", "original_build_count", "p5_repeat_required",
        "bootstrap", "gates", "resource_thresholds", "protocol_sha256",
    }
    if set(row) != required or row.get("schema") != FORMAL_PROTOCOL_SCHEMA or row.get("synthetic_test_mode") is not False:
        raise CustodyError("formal_protocol_invalid")
    _candidate_receipt(row.get("candidate")); _code_receipt(row.get("current_code_receipt"), "formal_current_code_invalid")
    _code_receipt(row.get("original_code_receipt"), "formal_original_code_invalid")
    if row.get("source_receipt") != rank.PROTOCOL_SOURCE:
        raise CustodyError("formal_source_receipt_invalid")
    _model_receipt(row.get("model_receipt"))
    if tuple(row.get("arms", ())) != score.FORMAL_ARMS or row.get("serializer_contract") != {"current": rank.CURRENT_SERIALIZER, "original_public_product": rank.ORIGINAL_MEMPALACE_SERIALIZER}:
        raise CustodyError("formal_arm_contract_invalid")
    if row.get("top_k") != 10 or row.get("tie_break") != "stable_ranking_key_ascending":
        raise CustodyError("formal_ranking_contract_invalid")
    if row.get("original_build_count") != 5 or row.get("p5_repeat_required") is not True or row.get("bootstrap") != score.FORMAL_BOOTSTRAP:
        raise CustodyError("formal_repeat_bootstrap_invalid")
    gates = _obj(row.get("gates"), "formal_gates_invalid")
    if set(gates) != {"overall_delta_min", "overall_ci_lower_gt_zero", "hard_delta_min", "hard_ci_lower_min", "abstention_ci_lower_min", "guardrails_required"}:
        raise CustodyError("formal_gates_invalid")
    expected_gates = {"overall_delta_min": .01, "overall_ci_lower_gt_zero": 0.0, "hard_delta_min": 0.0, "hard_ci_lower_min": -.01, "abstention_ci_lower_min": -.01, "guardrails_required": True}
    if gates != expected_gates:
        raise CustodyError("formal_gates_invalid")
    _resource_thresholds(row.get("resource_thresholds"))
    if row.get("protocol_sha256") != protocol_digest(row):
        raise CustodyError("formal_protocol_digest_mismatch")
    return row


def validate_candidate_worker_config(value: Any, *, protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Candidate-side allowlist.  It rejects labels even when embedded deeply."""
    frozen = validate_formal_protocol(protocol)
    row = _obj(value, "candidate_worker_config_invalid")
    if set(row) != _CANDIDATE_WORKER_KEYS or row.get("role") != "candidate_ranker":
        raise CustodyError("candidate_worker_config_invalid")
    for key in row:
        normalized = key.replace("-", "_").lower()
        if any(token in normalized for token in _FORBIDDEN_CANDIDATE_TOKENS):
            raise CustodyError("candidate_worker_forbidden_field")
    if row.get("projection_sha256") != frozen["candidate"]["projection_canonical_sha256"] or row.get("projection_raw_sha256") != frozen["candidate"]["projection_raw_sha256"]:
        raise CustodyError("candidate_worker_projection_binding_invalid")
    projection_path = _text(row.get("projection_path"), "candidate_worker_config_invalid")
    staging_root = _text(row.get("staging_root"), "candidate_worker_config_invalid")
    if Path(projection_path).is_absolute() or Path(staging_root).is_absolute() or ".." in Path(projection_path).parts or ".." in Path(staging_root).parts:
        raise CustodyError("candidate_worker_path_not_confined")
    if projection_path != "projection.json":
        raise CustodyError("candidate_projection_path_invalid")
    if _model_receipt(row.get("model_receipt")) != frozen["model_receipt"] or _code_receipt(row.get("code_receipt"), "candidate_worker_code_invalid") != frozen["current_code_receipt"]:
        raise CustodyError("candidate_worker_receipt_binding_invalid")
    if tuple(row.get("arms", ())) != tuple(arm for arm in score.FORMAL_ARMS if arm != "original_public_product") or row.get("top_k") != frozen["top_k"] or row.get("tie_break") != frozen["tie_break"] or row.get("serializer_contract") != frozen["serializer_contract"]:
        raise CustodyError("candidate_worker_contract_invalid")
    return row


def canonical_candidate_worker_config(protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Only valid public current-worker configuration for this protocol."""
    frozen = validate_formal_protocol(protocol)
    return {
        "role": "candidate_ranker", "projection_sha256": frozen["candidate"]["projection_canonical_sha256"],
        "projection_raw_sha256": frozen["candidate"]["projection_raw_sha256"], "projection_path": "projection.json",
        "model_receipt": frozen["model_receipt"], "code_receipt": frozen["current_code_receipt"],
        "staging_root": "staging", "arms": ["strong_raw", "static_p5", "six_view_secondary"],
        "top_k": frozen["top_k"], "tie_break": frozen["tie_break"], "serializer_contract": frozen["serializer_contract"],
    }


def _regular_bytes(path: Path, code: str) -> bytes:
    try: meta = os.lstat(path)
    except OSError as exc: raise CustodyError(code) from exc
    if not path.is_file() or path.is_symlink() or meta.st_nlink != 1:
        raise CustodyError(code)
    try: return path.read_bytes()
    except OSError as exc: raise CustodyError(code) from exc


def _inside(root: Path, relative: str, code: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise CustodyError(code)
    try:
        resolved_root = root.resolve(strict=True)
        resolved = (resolved_root / relative).resolve(strict=True)
    except OSError as exc: raise CustodyError(code) from exc
    if resolved_root != resolved and resolved_root not in resolved.parents:
        raise CustodyError(code)
    return resolved


def load_candidate_worker_projection(*, worker_config: Mapping[str, Any], protocol: Mapping[str, Any], candidate_bundle_root: Path, staging_parent: Path) -> dict[str, Any]:
    """The real candidate-side file capability: projection+READY only, no custody path."""
    frozen = validate_formal_protocol(protocol); config = validate_candidate_worker_config(worker_config, protocol=frozen)
    if config["projection_path"] != "projection.json":
        raise CustodyError("candidate_projection_path_invalid")
    # Reuse the custody module's double-read, lstat/identity/alias/drift-safe
    # candidate snapshot.  It never names or opens any custody file.
    projection, ready, raw = confirmation._candidate_snapshot(candidate_bundle_root)
    ready_raw = confirmation._snapshot(candidate_bundle_root / "READY.json", "candidate_bundle_not_ready")[0]
    if hashlib.sha256(raw).hexdigest() != frozen["candidate"]["projection_raw_sha256"] or hashlib.sha256(ready_raw).hexdigest() != frozen["candidate"]["ready_sha256"]:
        raise CustodyError("candidate_bundle_raw_binding_invalid")
    if ready.get("generation_id") != frozen["candidate"]["generation_id"] or ready["projection"].get("raw_sha256") != frozen["candidate"]["projection_raw_sha256"] or ready["projection"].get("canonical_sha256") != frozen["candidate"]["projection_canonical_sha256"]:
        raise CustodyError("candidate_ready_binding_invalid")
    if canonical_sha256(projection) != frozen["candidate"]["projection_canonical_sha256"]:
        raise CustodyError("candidate_projection_canonical_binding_invalid")
    if {key: frozen["candidate"][key] for key in ("query_count", "candidate_text_count")} != projection_denominators(projection):
        raise CustodyError("candidate_projection_denominator_binding_invalid")
    # This validates a concrete writable staging capability without opening any other input.
    staging = _inside(staging_parent, config["staging_root"], "candidate_staging_path_invalid")
    candidate_root = candidate_bundle_root.resolve(strict=True)
    if staging == candidate_root or candidate_root in staging.parents or staging in candidate_root.parents:
        raise CustodyError("candidate_staging_custody_overlap")
    return projection


def resource_digest(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key != "resource_sha256"})


def _latency_percentiles(values: Sequence[int]) -> dict[str, int]:
    """Canonical percentile calculation from the retained raw measurements."""
    if not values or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
        raise CustodyError("formal_resource_measurements_invalid")
    rows = sorted(values)
    def at(fraction: float) -> int:
        return rows[min(len(rows) - 1, int((len(rows) - 1) * fraction))]
    return {"count": len(rows), "p50": at(.50), "p95": at(.95), "p99": at(.99), "max": rows[-1]}


def validate_resource_receipt(value: Any, *, arm_id: str, thresholds: Mapping[str, Any], expected_denominators: Mapping[str, int], expected_query_keys: Sequence[Mapping[str, str]] | None = None) -> dict[str, Any]:
    """Validate measurement coverage and enforce thresholds frozen in protocol."""
    limits = _resource_thresholds(thresholds); row = _obj(value, "formal_resource_receipt_invalid")
    required = {"schema", "arm_id", "execution_role", "resource_semantics", "measurement_scope", "measurement_mode", "resource_comparability", "p5_repeat_accounting", "ingest_seconds", "index_seconds", "query_measurements", "query_latency_ns", "passage_embedding", "query_embedding", "storage_scope", "storage_bytes", "peak_rss_bytes", "artifact_sha256", "build_id", "index_sha256", "input_denominators", "hardware_runtime", "resource_sha256"}
    if set(row) != required or row.get("schema") != RESOURCE_SCHEMA or row.get("arm_id") != arm_id or arm_id not in _RESOURCE_ARMS:
        raise CustodyError("formal_resource_receipt_invalid")
    expected_semantics = "all_six_views_computed_then_raw_fusion_weights" if arm_id == "strong_raw" else "native_public_product" if arm_id == "original_public_product" else "all_six_views_computed_then_fixed_fusion"
    if row.get("resource_semantics") != expected_semantics:
        raise CustodyError("formal_resource_semantics_invalid")
    expected_role = "fresh_build" if arm_id == "original_public_product" else "primary"
    if arm_id == "static_p5":
        expected_roles = {"primary", "repeat"}
        expected_accounting = {"primary": "primary_excludes_repeat", "repeat": "repeat_measured_separately"}
    else:
        expected_roles = {expected_role}
        expected_accounting = {expected_role: "not_applicable"}
    if row.get("execution_role") not in expected_roles or row.get("measurement_scope") != "rank_only_excludes_trace_and_receipt_serialization" or row.get("p5_repeat_accounting") != expected_accounting[row["execution_role"]]:
        raise CustodyError("formal_resource_measurement_scope_invalid")
    if row.get("measurement_mode") not in {"synthetic_rehearsal", "live_native_adapter", "live_original_public_product"}:
        raise CustodyError("formal_resource_measurement_mode_invalid")
    if row.get("resource_comparability") != limits["resource_comparability"]:
        raise CustodyError("formal_resource_comparability_invalid")
    ingest, index = _finite_nonnegative(row.get("ingest_seconds"), "formal_resource_receipt_invalid"), _finite_nonnegative(row.get("index_seconds"), "formal_resource_receipt_invalid")
    measurements = row.get("query_measurements")
    if not isinstance(measurements, list) or not measurements:
        raise CustodyError("formal_resource_measurements_invalid")
    seen_items: set[str] = set(); walls: list[int] = []; cpus: list[int] = []
    for measurement in measurements:
        value = _obj(measurement, "formal_resource_measurements_invalid")
        if set(value) != {"item_id", "query_sha256", "wall_ns", "cpu_ns"}:
            raise CustodyError("formal_resource_measurements_invalid")
        item_id, query_sha = _hex(value.get("item_id"), "formal_resource_measurements_invalid"), _hex(value.get("query_sha256"), "formal_resource_measurements_invalid")
        if item_id in seen_items:
            raise CustodyError("formal_resource_measurements_invalid")
        seen_items.add(item_id); _positive_int(value.get("wall_ns"), "formal_resource_measurements_invalid"); _positive_int(value.get("cpu_ns"), "formal_resource_measurements_invalid")
        walls.append(value["wall_ns"]); cpus.append(value["cpu_ns"])
    if expected_query_keys is not None:
        expected_keys = [dict(item) for item in expected_query_keys]
        actual_keys = [{"item_id": item["item_id"], "query_sha256": item["query_sha256"]} for item in measurements]
        if actual_keys != expected_keys:
            raise CustodyError("formal_resource_query_binding_invalid")
    latency = _obj(row.get("query_latency_ns"), "formal_resource_receipt_invalid")
    if set(latency) != {"wall", "cpu"} or latency != {"wall": _latency_percentiles(walls), "cpu": _latency_percentiles(cpus)}:
        raise CustodyError("formal_resource_percentile_recompute_invalid")
    for key in ("passage_embedding", "query_embedding"):
        calls = _obj(row.get(key), "formal_resource_receipt_invalid")
        if set(calls) != {"calls", "texts", "measurement_kind", "native_embedding_observable", "limitation"}:
            raise CustodyError("formal_resource_receipt_invalid")
        for field in ("calls", "texts"):
            if isinstance(calls[field], bool) or not isinstance(calls[field], int) or calls[field] < 0:
                raise CustodyError("formal_resource_receipt_invalid")
        if not isinstance(calls["measurement_kind"], str) or not calls["measurement_kind"] or not isinstance(calls["native_embedding_observable"], bool):
            raise CustodyError("formal_resource_receipt_invalid")
        if calls["limitation"] is not None and (not isinstance(calls["limitation"], str) or not calls["limitation"].strip()):
            raise CustodyError("formal_resource_receipt_invalid")
    denominators = _obj(row.get("input_denominators"), "formal_resource_receipt_invalid")
    if set(denominators) != {"query_count", "candidate_text_count"} or _positive_int(denominators.get("query_count"), "formal_resource_receipt_invalid") < 1 or _positive_int(denominators.get("candidate_text_count"), "formal_resource_receipt_invalid") < 1:
        raise CustodyError("formal_resource_receipt_invalid")
    if denominators != dict(expected_denominators) or latency["wall"]["count"] != denominators["query_count"] or latency["cpu"]["count"] != denominators["query_count"] or len(measurements) != denominators["query_count"]:
        raise CustodyError("formal_resource_denominator_binding_invalid")
    if row["passage_embedding"]["calls"] <= 0 or row["passage_embedding"]["texts"] < denominators["candidate_text_count"] or row["query_embedding"]["calls"] <= 0 or row["query_embedding"]["texts"] < denominators["query_count"]:
        raise CustodyError("formal_resource_call_coverage_invalid")
    if row["query_embedding"]["calls"] != denominators["query_count"] or row["query_embedding"]["texts"] != denominators["query_count"]:
        raise CustodyError("formal_resource_query_call_exactness_invalid")
    runtime = _obj(row.get("hardware_runtime"), "formal_resource_receipt_invalid")
    if set(runtime) != {"python", "platform", "processor"}:
        raise CustodyError("formal_resource_receipt_invalid")
    for item in runtime.values(): _text(item, "formal_resource_receipt_invalid")
    if arm_id == "original_public_product":
        _text(row.get("build_id"), "formal_resource_receipt_invalid"); _hex(row.get("index_sha256"), "formal_resource_receipt_invalid")
        if row.get("storage_scope") != "palace_directory_after_cold_reopen" or row["storage_bytes"] <= 0:
            raise CustodyError("formal_resource_storage_scope_invalid")
        if row["passage_embedding"]["measurement_kind"] != "public_upsert_request_proxy" or row["query_embedding"]["measurement_kind"] != "public_search_request_proxy" or row["passage_embedding"]["native_embedding_observable"] or row["query_embedding"]["native_embedding_observable"] or not row["passage_embedding"]["limitation"] or not row["query_embedding"]["limitation"]:
            raise CustodyError("formal_resource_original_embedding_semantics_invalid")
    elif row.get("build_id") is not None or row.get("index_sha256") is not None:
        raise CustodyError("formal_resource_receipt_invalid")
    else:
        if row.get("storage_scope") != "no_persistent_index" or row["passage_embedding"]["measurement_kind"] != "encoder_adapter_api_calls" or row["query_embedding"]["measurement_kind"] != "encoder_adapter_api_calls" or not row["passage_embedding"]["native_embedding_observable"] or not row["query_embedding"]["native_embedding_observable"] or row["passage_embedding"]["limitation"] is not None or row["query_embedding"]["limitation"] is not None:
            raise CustodyError("formal_resource_current_embedding_semantics_invalid")
    storage = row.get("storage_bytes")
    if isinstance(storage, bool) or not isinstance(storage, int) or storage < 0:
        raise CustodyError("formal_resource_receipt_invalid")
    rss = _positive_int(row.get("peak_rss_bytes"), "formal_resource_receipt_invalid")
    _hex(row.get("artifact_sha256"), "formal_resource_receipt_invalid")
    if row.get("resource_sha256") != resource_digest(row):
        raise CustodyError("formal_resource_digest_mismatch")
    if limits["resource_comparability"] == "strict" and (ingest > limits["ingest_seconds_max"] or index > limits["index_seconds_max"] or latency["wall"]["p95"] > limits["query_p95_ns_max"] or storage > limits["storage_bytes_max"] or rss > limits["peak_rss_bytes_max"]):
        raise CustodyError("formal_resource_threshold_exceeded")
    return row


def freeze_current_worker(*, encoder: Any, protocol: Mapping[str, Any], worker_config: Mapping[str, Any], candidate_bundle_root: Path, staging_parent: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Candidate-only current worker: exactly three arms and a byte-identical P5 repeat."""
    frozen_protocol = validate_formal_protocol(protocol)
    frozen_projection = load_candidate_worker_projection(worker_config=worker_config, protocol=frozen_protocol, candidate_bundle_root=candidate_bundle_root, staging_parent=staging_parent)
    # Do not use ``freeze_current_rankings`` here: it performs an internal P5
    # repeat, which would make the formal worker's explicit repeat a third
    # execution while its receipt claimed two.  Every ranking invocation below
    # is therefore accounted for by this receipt and its matching resource row.
    raw = rank.rank_projection(projection=frozen_projection, encoder=encoder, arm_id="strong_raw", model_receipt=frozen_protocol["model_receipt"], code_receipt=frozen_protocol["current_code_receipt"])
    p5_primary = rank.rank_projection(projection=frozen_projection, encoder=encoder, arm_id="static_p5", model_receipt=frozen_protocol["model_receipt"], code_receipt=frozen_protocol["current_code_receipt"])
    p5_repeat = rank.rank_projection(projection=frozen_projection, encoder=encoder, arm_id="static_p5", model_receipt=frozen_protocol["model_receipt"], code_receipt=frozen_protocol["current_code_receipt"])
    six = rank.rank_projection(projection=frozen_projection, encoder=encoder, arm_id="six_view_secondary", model_receipt=frozen_protocol["model_receipt"], code_receipt=frozen_protocol["current_code_receipt"])
    artifacts = [raw, p5_primary, six]
    if tuple(artifact["arm_id"] for artifact in artifacts) != ("strong_raw", "static_p5", "six_view_secondary"):
        raise CustodyError("current_worker_arm_coverage_invalid")
    for artifact in artifacts:
        rank.validate_frozen_ranking(artifact, projection=frozen_projection)
    if rank._bytes(p5_primary) != rank._bytes(p5_repeat): raise CustodyError("static_p5_repeat_nondeterministic")
    receipt = {"schema": CURRENT_WORKER_SCHEMA, "lifecycle": list(CURRENT_LIFECYCLE), "projection_sha256": canonical_sha256(frozen_projection), "artifact_sha256": {artifact["arm_id"]: artifact["artifact_sha256"] for artifact in artifacts}, "static_p5_primary_sha256": p5_primary["artifact_sha256"], "static_p5_repeat_sha256": p5_repeat["artifact_sha256"], "static_p5_byte_identical": True, "static_p5_execution_count": 2}
    receipt["worker_sha256"] = _digest(receipt)
    return artifacts, receipt


def validate_current_worker_receipt(value: Any, *, projection: Mapping[str, Any], require_execution_receipts: bool = False) -> dict[str, Any]:
    row = _obj(value, "current_worker_receipt_invalid")
    required = {"schema", "lifecycle", "projection_sha256", "artifact_sha256", "static_p5_primary_sha256", "static_p5_repeat_sha256", "static_p5_byte_identical", "static_p5_execution_count", "worker_sha256"}
    permitted = required | {"execution_receipts"}
    if set(row) not in ((permitted,) if require_execution_receipts else (required, permitted)) or row.get("schema") != CURRENT_WORKER_SCHEMA or row.get("lifecycle") != list(CURRENT_LIFECYCLE) or row.get("projection_sha256") != canonical_sha256(validate_candidate_projection(projection)):
        raise CustodyError("current_worker_receipt_invalid")
    if set(_obj(row["artifact_sha256"], "current_worker_receipt_invalid")) != {"strong_raw", "static_p5", "six_view_secondary"} or row["static_p5_primary_sha256"] != row["artifact_sha256"]["static_p5"] or row["static_p5_repeat_sha256"] != row["artifact_sha256"]["static_p5"] or row["static_p5_byte_identical"] is not True or row["static_p5_execution_count"] != 2:
        raise CustodyError("current_worker_repeat_invalid")
    for digest in [
        *row["artifact_sha256"].values(),
        row["static_p5_primary_sha256"],
        row["static_p5_repeat_sha256"],
        row["worker_sha256"],
    ]:
        _hex(digest, "current_worker_receipt_invalid")
    if row["worker_sha256"] != _digest({key: item for key, item in row.items() if key != "worker_sha256"}): raise CustodyError("current_worker_digest_mismatch")
    if "execution_receipts" in row and (not isinstance(row["execution_receipts"], list) or len(row["execution_receipts"]) != 4):
        raise CustodyError("current_worker_execution_receipts_invalid")
    return row


def validate_current_execution_receipts(value: Any, *, current_worker_receipt: Mapping[str, Any], protocol: Mapping[str, Any], projection: Mapping[str, Any], resources: Sequence[Mapping[str, Any]], ranking_artifacts: Sequence[Mapping[str, Any]], supervisors: Mapping[str, Any] | None = None, allow_synthetic: bool = False) -> list[dict[str, Any]]:
    """Validate four independently produced execution receipts before freezing.

    This deliberately validates the sidecar receipts outside the frozen ranking
    artifact: timing, process IDs, runtime and supervisor details must never
    perturb a ranking artifact's canonical bytes.
    """
    aggregate = validate_current_worker_receipt(current_worker_receipt, projection=projection, require_execution_receipts=True)
    frozen = validate_formal_protocol(protocol)
    rows = value
    if not isinstance(rows, list) or rows != aggregate["execution_receipts"] or len(rows) != 4:
        raise CustodyError("current_execution_receipt_coverage_invalid")
    expected = {
        "raw": ("strong_raw", "primary"), "p5_primary": ("static_p5", "primary"),
        "p5_repeat": ("static_p5", "repeat"), "six": ("six_view_secondary", "primary"),
    }
    artifact_by_arm = {item["arm_id"]: item for item in ranking_artifacts if isinstance(item, Mapping) and isinstance(item.get("arm_id"), str)}
    if set(artifact_by_arm) != {"strong_raw", "static_p5", "six_view_secondary"}:
        raise CustodyError("current_execution_artifact_coverage_invalid")
    seen: set[str] = set(); checked: list[dict[str, Any]] = []
    p5_artifact_files: list[str] = []
    for raw in rows:
        row = _obj(raw, "current_execution_receipt_invalid")
        required = {"schema", "execution_mode", "execution_role", "arm_id", "protocol_sha256", "projection_sha256", "worker_config_sha256", "method_input_sha256", "observed_code_before", "observed_code_after", "observed_model_before", "observed_model_after", "provider", "encoder_identity", "artifact_file_sha256", "artifact_sha256", "resource_sha256", "process_id", "supervisor_sha256", "execution_sha256"}
        if set(row) != required or row.get("schema") != CURRENT_EXECUTION_RECEIPT_SCHEMA or row.get("execution_role") not in expected or row["execution_role"] in seen:
            raise CustodyError("current_execution_receipt_invalid")
        seen.add(row["execution_role"])
        arm_id, resource_role = expected[row["execution_role"]]
        if row.get("arm_id") != arm_id or row.get("protocol_sha256") != frozen["protocol_sha256"] or row.get("projection_sha256") != aggregate["projection_sha256"] or row.get("encoder_identity") != frozen["model_receipt"]["encoder_identity"]:
            raise CustodyError("current_execution_receipt_binding_invalid")
        if not isinstance(row.get("process_id"), int) or row["process_id"] <= 0 or not isinstance(row.get("provider"), Mapping):
            raise CustodyError("current_execution_receipt_invalid")
        for name in ("worker_config_sha256", "method_input_sha256", "artifact_file_sha256", "artifact_sha256", "resource_sha256", "supervisor_sha256"):
            _hex(row.get(name), "current_execution_receipt_invalid")
        for name in ("observed_code_before", "observed_code_after", "observed_model_before", "observed_model_after"):
            if not isinstance(row.get(name), Mapping):
                raise CustodyError("current_execution_receipt_invalid")
        artifact = artifact_by_arm[arm_id]
        matched_resources = [item for item in resources if isinstance(item, Mapping) and item.get("arm_id") == arm_id and item.get("execution_role") == resource_role]
        if len(matched_resources) != 1 or matched_resources[0].get("resource_sha256") != row["resource_sha256"] or matched_resources[0].get("artifact_sha256") != row["artifact_sha256"]:
            raise CustodyError("current_execution_resource_binding_invalid")
        expected_measurement_mode = "synthetic_rehearsal" if allow_synthetic else "live_native_adapter"
        if matched_resources[0].get("measurement_mode") != expected_measurement_mode:
            raise CustodyError("current_execution_resource_mode_invalid")
        if row["artifact_file_sha256"] != hashlib.sha256(_bytes(artifact)).hexdigest() or row["artifact_sha256"] != artifact.get("artifact_sha256") or row["artifact_sha256"] != aggregate["artifact_sha256"][arm_id] or row["execution_sha256"] != _digest({key: item for key, item in row.items() if key != "execution_sha256"}):
            raise CustodyError("current_execution_receipt_digest_invalid")
        if row["worker_config_sha256"] != _digest(canonical_candidate_worker_config(frozen)) or row["method_input_sha256"] != _digest({"arm_id": arm_id, "method_receipt": artifact["method_receipt"], "serializer_receipt": artifact["serializer_receipt"]}):
            raise CustodyError("current_execution_replay_binding_invalid")
        mode = row.get("execution_mode")
        if mode == "synthetic_rehearsal":
            if not allow_synthetic:
                raise CustodyError("current_execution_synthetic_not_formal")
        elif mode == "live_native_adapter":
            if row["observed_code_before"] != frozen["current_code_receipt"] or row["observed_code_after"] != frozen["current_code_receipt"]:
                raise CustodyError("current_execution_live_code_binding_invalid")
            expected_files = frozen["model_receipt"]["files"]
            if any(set(file) != {"path_role", "relative_path", "sha256", "bytes"} for file in expected_files):
                raise CustodyError("current_execution_live_model_paths_required")
            tree = sorted(({"relative_path": file["relative_path"], "sha256": file["sha256"], "bytes": file["bytes"]} for file in expected_files), key=lambda item: item["relative_path"])
            expected_tree_sha = _digest(tree); expected_tree_bytes = sum(item["bytes"] for item in tree)
            for observed in (row["observed_model_before"], row["observed_model_after"]):
                if set(observed) != {"model_file_tree_sha256", "model_file_tree_bytes", "encoder_identity", "runtime_identity"} or observed.get("model_file_tree_sha256") != expected_tree_sha or observed.get("model_file_tree_bytes") != expected_tree_bytes or observed.get("encoder_identity") != frozen["model_receipt"]["encoder_identity"]:
                    raise CustodyError("current_execution_live_model_binding_invalid")
            if row["observed_model_before"] != row["observed_model_after"] or row["provider"] != {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"], "model_file_tree_sha256": expected_tree_sha}:
                raise CustodyError("current_execution_live_provider_invalid")
        else:
            raise CustodyError("current_execution_mode_invalid")
        if supervisors is not None and row["supervisor_sha256"] != _digest(_obj(supervisors.get("current-" + row["execution_role"]), "current_execution_supervisor_invalid")):
            raise CustodyError("current_execution_supervisor_binding_invalid")
        if row["execution_role"].startswith("p5_"):
            p5_artifact_files.append(row["artifact_file_sha256"])
        checked.append(row)
    if seen != set(expected) or len({row["process_id"] for row in checked}) != 4 or len(set(p5_artifact_files)) != 1:
        raise CustodyError("current_execution_receipt_repeat_invalid")
    return checked


def validate_original_worker_receipt(value: Any, *, projection: Any, protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Validate five isolated original builds after exact public-product execution."""
    frozen_protocol = validate_formal_protocol(protocol); frozen_projection = validate_candidate_projection(projection)
    if canonical_sha256(frozen_projection) != frozen_protocol["candidate"]["projection_canonical_sha256"]:
        raise CustodyError("original_projection_protocol_binding_invalid")
    row = _obj(value, "original_worker_receipt_invalid")
    if set(row) != {"replicates", "lifecycle", "original_code_before", "original_code_after", "worker_sha256"} or row.get("lifecycle") != list(ORIGINAL_LIFECYCLE):
        raise CustodyError("original_worker_lifecycle_invalid")
    if _code_receipt(row.get("original_code_before"), "original_live_code_invalid") != frozen_protocol["original_code_receipt"] or _code_receipt(row.get("original_code_after"), "original_live_code_invalid") != frozen_protocol["original_code_receipt"]:
        raise CustodyError("original_live_code_drift")
    replicates = row.get("replicates")
    if not isinstance(replicates, list) or len(replicates) != frozen_protocol["original_build_count"]:
        raise CustodyError("original_worker_build_count_invalid")
    artifact = rank.wrap_original_public_rankings(projection=frozen_projection, replicates=replicates, model_receipt=frozen_protocol["model_receipt"], code_receipt=frozen_protocol["original_code_receipt"])
    build_ids = [replicate["build_id"] for replicate in artifact["replicates"]]
    collection_ids = [replicate["index_receipt"]["collection_identity"] for replicate in artifact["replicates"]]
    index_ids = [replicate["index_receipt"]["index_identity_sha256"] for replicate in artifact["replicates"]]
    if len(set(build_ids)) != 5 or len(set(collection_ids)) != 5 or len(set(index_ids)) != 5:
        raise CustodyError("original_worker_identity_not_isolated")
    sealed = {"replicates": replicates, "lifecycle": list(ORIGINAL_LIFECYCLE), "original_code_before": row["original_code_before"], "original_code_after": row["original_code_after"]}
    if row.get("worker_sha256") != _digest(sealed):
        raise CustodyError("original_worker_digest_mismatch")
    return {**row, "artifact": artifact}


def freeze_endpoint_manifest(*, projection: Any, protocol: Mapping[str, Any], ranking_artifacts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Bind all public freezes before a custodian is even eligible to load labels."""
    frozen_protocol = validate_formal_protocol(protocol); frozen_projection = validate_candidate_projection(projection)
    if canonical_sha256(frozen_projection) != frozen_protocol["candidate"]["projection_canonical_sha256"]:
        raise CustodyError("endpoint_projection_protocol_binding_invalid")
    by_arm: dict[str, dict[str, Any]] = {}
    for artifact in ranking_artifacts:
        checked = rank.validate_frozen_ranking(artifact, projection=frozen_projection)
        if checked["arm_id"] in by_arm:
            raise CustodyError("endpoint_duplicate_arm")
        by_arm[checked["arm_id"]] = checked
    if tuple(by_arm) != score.FORMAL_ARMS:
        raise CustodyError("endpoint_arm_coverage_invalid")
    manifest = {
        "schema": score.MANIFEST_SCHEMA, "projection_sha256": canonical_sha256(frozen_projection), "protocol_source": rank.PROTOCOL_SOURCE,
        "serializer_contract": frozen_protocol["serializer_contract"],
        "arms": [{"arm_id": arm, "ranking_artifact_sha256": by_arm[arm]["artifact_sha256"], "confidence_contract": rank.CONFIDENCE_CONTRACT if arm in rank.CURRENT_ARMS else None} for arm in score.FORMAL_ARMS],
        "directory_endpoints": [{"directory_group": group, "endpoint": endpoint} for group, endpoint in score.UPSTREAM_GROUPS.items()],
        "bootstrap": score.FORMAL_BOOTSTRAP, "synthetic_test_mode": False, "reference_arm": "strong_raw",
    }
    manifest["manifest_sha256"] = score.endpoint_manifest_digest(manifest)
    return score.validate_endpoint_manifest(manifest, projection_sha256=manifest["projection_sha256"])


def release_digest(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key not in {"release_sha256", "capability_hmac"}})


def _capability_hmac(value: Mapping[str, Any], secret: bytes) -> str:
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise CustodyError("release_capability_secret_invalid")
    return hmac.new(secret, _bytes({key: item for key, item in value.items() if key != "capability_hmac"}), hashlib.sha256).hexdigest()


def sign_release_authorization(value: Mapping[str, Any], *, custody_capability_secret: bytes) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema") != RELEASE_SCHEMA:
        raise CustodyError("release_authorization_schema_invalid")
    row["release_sha256"] = release_digest(row)
    row["capability_hmac"] = _capability_hmac(row, custody_capability_secret)
    return row


def sign_rehearsal_release_authorization(value: Mapping[str, Any], *, custody_capability_secret: bytes) -> dict[str, Any]:
    row = dict(value)
    if row.get("schema") != REHEARSAL_RELEASE_SCHEMA:
        raise CustodyError("rehearsal_release_authorization_schema_invalid")
    row["release_sha256"] = release_digest(row)
    row["capability_hmac"] = _capability_hmac(row, custody_capability_secret)
    return row


def _resource_digest_values(value: Mapping[str, Any]) -> list[str]:
    """Flatten the fixed public resource receipt shape without accepting extras."""
    digests: list[str] = []
    for arm_id in score.FORMAL_ARMS:
        item = value[arm_id]
        if arm_id == "original_public_product":
            if not isinstance(item, list):
                raise CustodyError("release_resource_digest_shape_invalid")
            digests.extend(item)
        elif arm_id == "static_p5":
            row = _obj(item, "release_resource_digest_shape_invalid")
            if set(row) != {"primary", "repeat"}:
                raise CustodyError("release_resource_digest_shape_invalid")
            digests.extend([row["primary"], row["repeat"]])
        else:
            digests.append(item)
    return digests


def _validate_release_authorization(value: Any, *, projection: Mapping[str, Any], ranking_artifacts: Sequence[Mapping[str, Any]], current_worker_receipt: Mapping[str, Any], protocol: Mapping[str, Any], endpoint_manifest: Mapping[str, Any], resource_receipts: Sequence[Mapping[str, Any]], custody_ready_sha256: str, custody_bundle_sha256: str, custody_capability_secret: bytes, rehearsal: bool) -> dict[str, Any]:
    """Validate a custodian-only release capability; it contains no secret or labels."""
    frozen_protocol = validate_formal_protocol(protocol)
    projection = validate_candidate_projection(projection)
    if canonical_sha256(projection) != frozen_protocol["candidate"]["projection_canonical_sha256"]:
        raise CustodyError("release_projection_protocol_binding_invalid")
    if {key: frozen_protocol["candidate"][key] for key in ("query_count", "candidate_text_count")} != projection_denominators(projection):
        raise CustodyError("release_projection_denominator_binding_invalid")
    current_worker = validate_current_worker_receipt(current_worker_receipt, projection=projection, require_execution_receipts=True)
    endpoint = score.validate_endpoint_manifest(endpoint_manifest, projection_sha256=frozen_protocol["candidate"]["projection_canonical_sha256"])
    _hex(custody_ready_sha256, "release_custody_ready_invalid"); _hex(custody_bundle_sha256, "release_custody_bundle_invalid")
    expected_queries = projection_query_keys(projection)
    resources: dict[str, list[dict[str, Any]]] = {}
    for receipt in resource_receipts:
        checked = validate_resource_receipt(receipt, arm_id=_obj(receipt, "release_resource_invalid").get("arm_id"), thresholds=frozen_protocol["resource_thresholds"], expected_denominators=projection_denominators(projection), expected_query_keys=expected_queries)
        resources.setdefault(checked["arm_id"], []).append(checked)
    if set(resources) != _RESOURCE_ARMS:
        raise CustodyError("release_resource_coverage_invalid")
    artifacts: dict[str, dict[str, Any]] = {}
    for artifact in ranking_artifacts:
        checked = rank.validate_frozen_ranking(artifact, projection=projection)
        if checked["arm_id"] in artifacts: raise CustodyError("release_ranking_artifact_duplicate")
        expected_code = frozen_protocol["original_code_receipt"] if checked["arm_id"] == "original_public_product" else frozen_protocol["current_code_receipt"]
        if checked["model_receipt"] != frozen_protocol["model_receipt"] or checked["code_receipt"] != expected_code:
            raise CustodyError("release_ranking_artifact_protocol_receipt_invalid")
        artifacts[checked["arm_id"]] = checked
    if tuple(artifacts) != score.FORMAL_ARMS:
        raise CustodyError("release_ranking_artifact_coverage_invalid")
    row = _obj(value, "release_authorization_invalid")
    required = {"schema", "protocol_sha256", "endpoint_manifest_sha256", "candidate_ready_sha256", "projection_raw_sha256", "projection_canonical_sha256", "custody_ready_sha256", "custody_bundle_sha256", "ranking_artifact_sha256", "resource_sha256", "original_build_index_sha256", "current_worker_sha256", "release_sha256", "capability_hmac"}
    if set(row) != required or row.get("schema") != (REHEARSAL_RELEASE_SCHEMA if rehearsal else RELEASE_SCHEMA):
        raise CustodyError("release_authorization_invalid")
    expected_artifacts = {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in endpoint["arms"]}
    if {arm: artifacts[arm]["artifact_sha256"] for arm in score.FORMAL_ARMS} != expected_artifacts:
        raise CustodyError("release_ranking_artifact_endpoint_binding_invalid")
    current_artifacts = {arm: artifacts[arm]["artifact_sha256"] for arm in ("strong_raw", "static_p5", "six_view_secondary")}
    if current_worker["artifact_sha256"] != current_artifacts:
        raise CustodyError("release_current_worker_artifact_binding_invalid")
    if len(resources["original_public_product"]) != 5 or len(resources["strong_raw"]) != 1 or len(resources["six_view_secondary"]) != 1 or len(resources["static_p5"]) != 2:
        raise CustodyError("release_resource_replicate_coverage_invalid")
    originals = resources["original_public_product"]
    expected_original_mode = "synthetic_rehearsal" if rehearsal else "live_original_public_product"
    if any(item["measurement_mode"] != expected_original_mode for item in originals):
        raise CustodyError("release_original_resource_mode_invalid")
    if len({item["build_id"] for item in originals}) != 5 or len({item["index_sha256"] for item in originals}) != 5:
        raise CustodyError("release_resource_original_identity_invalid")
    expected_original = {replicate["build_id"]: replicate["index_sha256"] for replicate in artifacts["original_public_product"]["replicates"]}
    if len(expected_original) != 5 or {item["build_id"]: item["index_sha256"] for item in originals} != expected_original:
        raise CustodyError("release_resource_original_artifact_binding_invalid")
    for build_id, index_sha in expected_original.items(): _text(build_id, "release_resource_original_artifact_binding_invalid"); _hex(index_sha, "release_resource_original_artifact_binding_invalid")
    p5_resources = {item["execution_role"]: item for item in resources["static_p5"]}
    if set(p5_resources) != {"primary", "repeat"} or p5_resources["primary"]["artifact_sha256"] != expected_artifacts["static_p5"] or p5_resources["repeat"]["artifact_sha256"] != expected_artifacts["static_p5"]:
        raise CustodyError("release_static_p5_repeat_resource_binding_invalid")
    expected_resources = {
        "original_public_product": [item["resource_sha256"] for item in sorted(originals, key=lambda item: item["build_id"])],
        "strong_raw": resources["strong_raw"][0]["resource_sha256"],
        "static_p5": {role: p5_resources[role]["resource_sha256"] for role in ("primary", "repeat")},
        "six_view_secondary": resources["six_view_secondary"][0]["resource_sha256"],
    }
    if any(item["artifact_sha256"] != expected_artifacts[arm] for arm, items in resources.items() for item in items):
        raise CustodyError("release_resource_artifact_binding_invalid")
    validate_current_execution_receipts(
        current_worker["execution_receipts"], current_worker_receipt=current_worker,
        protocol=frozen_protocol, projection=projection, resources=resource_receipts,
        ranking_artifacts=[artifacts[arm] for arm in ("strong_raw", "static_p5", "six_view_secondary")],
        allow_synthetic=rehearsal,
    )
    if row.get("protocol_sha256") != frozen_protocol["protocol_sha256"] or row.get("endpoint_manifest_sha256") != endpoint["manifest_sha256"] or row.get("candidate_ready_sha256") != frozen_protocol["candidate"]["ready_sha256"] or row.get("projection_raw_sha256") != frozen_protocol["candidate"]["projection_raw_sha256"] or row.get("projection_canonical_sha256") != frozen_protocol["candidate"]["projection_canonical_sha256"] or row.get("custody_ready_sha256") != custody_ready_sha256 or row.get("custody_bundle_sha256") != custody_bundle_sha256 or row.get("ranking_artifact_sha256") != expected_artifacts or row.get("resource_sha256") != expected_resources or row.get("original_build_index_sha256") != expected_original or row.get("current_worker_sha256") != current_worker["worker_sha256"]:
        raise CustodyError("release_binding_invalid")
    resource_digests = _resource_digest_values(_obj(row["resource_sha256"], "release_resource_digest_shape_invalid"))
    for digest in [row["protocol_sha256"], row["endpoint_manifest_sha256"], row["candidate_ready_sha256"], row["projection_raw_sha256"], row["projection_canonical_sha256"], row["custody_ready_sha256"], row["custody_bundle_sha256"], row["current_worker_sha256"], *row["ranking_artifact_sha256"].values(), *resource_digests]:
        _hex(digest, "release_binding_invalid")
    if row.get("release_sha256") != release_digest(row):
        raise CustodyError("release_digest_mismatch")
    if not hmac.compare_digest(row.get("capability_hmac", ""), _capability_hmac(row, custody_capability_secret)):
        raise CustodyError("release_capability_invalid")
    return row


def validate_release_authorization(value: Any, **kwargs: Any) -> dict[str, Any]:
    """Formal release validation is permanently live-only."""
    return _validate_release_authorization(value, **kwargs, rehearsal=False)


def validate_rehearsal_release_authorization(value: Any, **kwargs: Any) -> dict[str, Any]:
    """Distinct synthetic-only authorization path; never a formal release."""
    return _validate_release_authorization(value, **kwargs, rehearsal=True)


def _open_custody_after_authorization(*, release_authorization: Mapping[str, Any], projection: Mapping[str, Any], ranking_artifacts: Sequence[Mapping[str, Any]], current_worker_receipt: Mapping[str, Any], protocol: Mapping[str, Any], endpoint_manifest: Mapping[str, Any], resource_receipts: Sequence[Mapping[str, Any]], custody_ready_sha256: str, custody_bundle_sha256: str, custody_capability_secret: bytes, candidate_bundle_root: Path, custody_bundle_root: Path, binding_secret: bytes, rehearsal: bool) -> Any:
    """Custodian scaffold: public gates precede an actual confirmation loader."""
    validator = validate_rehearsal_release_authorization if rehearsal else validate_release_authorization
    validator(release_authorization, projection=projection, ranking_artifacts=ranking_artifacts, current_worker_receipt=current_worker_receipt, protocol=protocol, endpoint_manifest=endpoint_manifest, resource_receipts=resource_receipts, custody_ready_sha256=custody_ready_sha256, custody_bundle_sha256=custody_bundle_sha256, custody_capability_secret=custody_capability_secret)
    candidate_ready, candidate_ready_identity, candidate_ready_sha = confirmation._snapshot(candidate_bundle_root / "READY.json", "candidate_bundle_not_ready")
    if candidate_ready_sha != release_authorization["candidate_ready_sha256"] or candidate_ready_sha != protocol["candidate"]["ready_sha256"]:
        raise CustodyError("candidate_release_ready_binding_invalid")
    before_ready, before_ready_identity, before_ready_sha = confirmation._snapshot(custody_bundle_root / "READY.json", "custody_bundle_not_ready")
    before_custody, before_custody_identity, before_custody_sha = confirmation._snapshot(custody_bundle_root / "sealed-custody.json", "custody_bundle_not_ready")
    if before_ready_sha != custody_ready_sha256 or before_custody_sha != custody_bundle_sha256 or before_ready_identity == before_custody_identity:
        raise CustodyError("custody_release_bundle_binding_invalid")
    value = confirmation.load_custody_for_scoring(candidate_bundle_root, custody_bundle_root, binding_secret=binding_secret)
    after_candidate_ready, after_candidate_ready_identity, after_candidate_ready_sha = confirmation._snapshot(candidate_bundle_root / "READY.json", "candidate_bundle_generation_drift")
    after_ready, after_ready_identity, after_ready_sha = confirmation._snapshot(custody_bundle_root / "READY.json", "custody_bundle_generation_drift")
    after_custody, after_custody_identity, after_custody_sha = confirmation._snapshot(custody_bundle_root / "sealed-custody.json", "custody_bundle_generation_drift")
    if (candidate_ready, candidate_ready_identity, candidate_ready_sha) != (after_candidate_ready, after_candidate_ready_identity, after_candidate_ready_sha) or (before_ready, before_ready_identity, before_ready_sha, before_custody, before_custody_identity, before_custody_sha) != (after_ready, after_ready_identity, after_ready_sha, after_custody, after_custody_identity, after_custody_sha):
        raise CustodyError("custody_release_bundle_drift")
    return value


def open_custody_after_release(**kwargs: Any) -> Any:
    return _open_custody_after_authorization(**kwargs, rehearsal=False)


def open_custody_after_rehearsal_release(**kwargs: Any) -> Any:
    return _open_custody_after_authorization(**kwargs, rehearsal=True)


def post_score_attestation_digest(value: Mapping[str, Any]) -> str:
    return _digest({key: item for key, item in value.items() if key not in {"attestation_sha256", "attestation_hmac"}})


def sign_post_score_attestation(value: Mapping[str, Any], *, scorer_attestation_secret: bytes) -> dict[str, Any]:
    if not isinstance(scorer_attestation_secret, bytes) or len(scorer_attestation_secret) < 32: raise CustodyError("scorer_attestation_secret_invalid")
    row = dict(value); row["attestation_sha256"] = post_score_attestation_digest(row)
    row["attestation_hmac"] = hmac.new(scorer_attestation_secret, _bytes({key: item for key, item in row.items() if key != "attestation_hmac"}), hashlib.sha256).hexdigest()
    return row


def validate_post_score_attestation(value: Any, *, release: Mapping[str, Any], report: Mapping[str, Any], protocol: Mapping[str, Any], endpoint_manifest: Mapping[str, Any], scorer_attestation_secret: bytes) -> dict[str, Any]:
    if not isinstance(scorer_attestation_secret, bytes) or len(scorer_attestation_secret) < 32: raise CustodyError("scorer_attestation_secret_invalid")
    row = _obj(value, "post_score_attestation_invalid")
    required = {"schema", "release_sha256", "report_sha256", "protocol_sha256", "endpoint_manifest_sha256", "ranking_artifact_sha256", "resource_sha256", "attestation_sha256", "attestation_hmac"}
    if set(row) != required or row.get("schema") != POST_SCORE_ATTESTATION_SCHEMA: raise CustodyError("post_score_attestation_invalid")
    expected = {"release_sha256": release["release_sha256"], "report_sha256": report["report_sha256"], "protocol_sha256": protocol["protocol_sha256"], "endpoint_manifest_sha256": endpoint_manifest["manifest_sha256"], "ranking_artifact_sha256": report["ranking_artifact_sha256"], "resource_sha256": release["resource_sha256"]}
    if any(row.get(key) != item for key, item in expected.items()) or row.get("attestation_sha256") != post_score_attestation_digest(row): raise CustodyError("post_score_attestation_binding_invalid")
    actual = hmac.new(scorer_attestation_secret, _bytes({key: item for key, item in row.items() if key != "attestation_hmac"}), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(row.get("attestation_hmac", ""), actual): raise CustodyError("post_score_attestation_hmac_invalid")
    return row


def publish_nonreplace(path: Path, payload: bytes, *, fsync_parent: Callable[[Path], None] | None = None) -> dict[str, Any]:
    """Hard-link publication under a stable parent; exact retries never overwrite."""
    if not path.parent.is_dir() or path.parent.is_symlink() or path.is_symlink():
        raise CustodyError("publish_parent_missing")
    if fsync_parent is None:
        raise CustodyError("publish_parent_fsync_required")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            try:
                existing, _identity, existing_sha = confirmation._snapshot(path, "publish_existing_output_invalid")
            except CustodyError as exc:
                raise CustodyError("publish_existing_output_conflict") from exc
            if existing_sha != hashlib.sha256(payload).hexdigest() or existing != payload:
                raise CustodyError("publish_existing_output_conflict")
            fsync_parent(path.parent)
            return {"published": False, "retry_idempotent": True, "sha256": hashlib.sha256(payload).hexdigest()}
        try:
            fsync_parent(path.parent)
        except BaseException:
            # If durability confirmation fails, remove only the link we just
            # created (same inode as the still-open temporary), never an
            # unrelated concurrent publisher's output.
            try:
                published_meta = os.lstat(path)
                temporary_meta = os.lstat(temporary)
                if (published_meta.st_dev, published_meta.st_ino) == (temporary_meta.st_dev, temporary_meta.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass
            finally:
                raise
    finally:
        temporary.unlink(missing_ok=True)
    return {"published": True, "retry_idempotent": False, "sha256": hashlib.sha256(payload).hexdigest()}


def _audit_envelope(*, report: Mapping[str, Any], post_score_attestation: Mapping[str, Any], scorer_attestation_secret: bytes, release_authorization: Mapping[str, Any], projection: Mapping[str, Any], ranking_artifacts: Sequence[Mapping[str, Any]], current_worker_receipt: Mapping[str, Any], protocol: Mapping[str, Any], endpoint_manifest: Mapping[str, Any], resource_receipts: Sequence[Mapping[str, Any]], custody_ready_sha256: str, custody_bundle_sha256: str, custody_capability_secret: bytes, rehearsal: bool) -> dict[str, Any]:
    validator = validate_rehearsal_release_authorization if rehearsal else validate_release_authorization
    release = validator(release_authorization, projection=projection, ranking_artifacts=ranking_artifacts, current_worker_receipt=current_worker_receipt, protocol=protocol, endpoint_manifest=endpoint_manifest, resource_receipts=resource_receipts, custody_ready_sha256=custody_ready_sha256, custody_bundle_sha256=custody_bundle_sha256, custody_capability_secret=custody_capability_secret)
    checked = score.validate_report(report)
    endpoint = score.validate_endpoint_manifest(endpoint_manifest, projection_sha256=protocol["candidate"]["projection_canonical_sha256"])
    if checked["projection_sha256"] != endpoint["projection_sha256"] or checked["endpoint_manifest_sha256"] != endpoint["manifest_sha256"] or checked["ranking_artifact_sha256"] != {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in endpoint["arms"]}:
        raise CustodyError("publish_report_release_binding_invalid")
    attestation = validate_post_score_attestation(post_score_attestation, release=release, report=checked, protocol=protocol, endpoint_manifest=endpoint, scorer_attestation_secret=scorer_attestation_secret)
    value = {"schema": AUDIT_ENVELOPE_SCHEMA, "protocol": validate_formal_protocol(protocol), "endpoint_manifest": endpoint, "current_worker_receipt": validate_current_worker_receipt(current_worker_receipt, projection=projection, require_execution_receipts=True), "resource_receipts": list(resource_receipts), "release_authorization": release, "report": checked, "post_score_attestation": attestation}
    value["envelope_sha256"] = _digest(value)
    return value


def audit_envelope(**kwargs: Any) -> dict[str, Any]:
    """Formal audit envelope; it cannot accept rehearsal authorization."""
    return _audit_envelope(**kwargs, rehearsal=False)


def rehearsal_audit_envelope(**kwargs: Any) -> dict[str, Any]:
    """Synthetic recovery envelope, signed under the distinct rehearsal schema."""
    return _audit_envelope(**kwargs, rehearsal=True)


def publish_report(*, path: Path, report: Mapping[str, Any], post_score_attestation: Mapping[str, Any], scorer_attestation_secret: bytes, release_authorization: Mapping[str, Any], projection: Mapping[str, Any], ranking_artifacts: Sequence[Mapping[str, Any]], current_worker_receipt: Mapping[str, Any], protocol: Mapping[str, Any], endpoint_manifest: Mapping[str, Any], resource_receipts: Sequence[Mapping[str, Any]], custody_ready_sha256: str, custody_bundle_sha256: str, custody_capability_secret: bytes, fsync_parent: Callable[[Path], None] | None = None) -> dict[str, Any]:
    envelope = audit_envelope(report=report, post_score_attestation=post_score_attestation, scorer_attestation_secret=scorer_attestation_secret, release_authorization=release_authorization, projection=projection, ranking_artifacts=ranking_artifacts, current_worker_receipt=current_worker_receipt, protocol=protocol, endpoint_manifest=endpoint_manifest, resource_receipts=resource_receipts, custody_ready_sha256=custody_ready_sha256, custody_bundle_sha256=custody_bundle_sha256, custody_capability_secret=custody_capability_secret)
    return {**publish_nonreplace(path, _bytes(envelope), fsync_parent=fsync_parent), "envelope_sha256": envelope["envelope_sha256"]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AERP-7 formal coordinator (fail closed)")
    parser.add_argument("--synthetic-test-mode", action="store_true", help="reserved for test harnesses; never authorizes formal data")
    args = parser.parse_args(argv)
    if not args.synthetic_test_mode:
        parser.error("formal execution is disabled until a separately reviewed coordinator command is authorized")
    parser.error("synthetic mode is exercised through injected tests; this CLI does not read any dataset")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
