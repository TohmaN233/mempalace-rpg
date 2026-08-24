"""Isolated, fail-closed executor for the AERP-7 formal protocol.

The module has two deliberately separate concerns.  Its public coordinator and
ranking workers only receive the candidate bundle and label-free protocol.  Its
custodian entrypoint is a separate process and is the sole place where custody
capabilities could be accepted.  Formal and rehearsal invocations use separate
schemas, so a synthetic capability can never select a live worker or freeze.

This is not a replacement for the upstream MemPalace product runner.  The
formal original-worker seam is where that exact public-product implementation
is connected after authorization; synthetic mode manufactures no claim about
the product or the ConvoMem results.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks import aerp7_convomem_formal as formal
from benchmarks import aerp7_original_product as original_product
from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_scoring as score
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256, validate_candidate_projection
from benchmarks import aerp5_product_paired_locomo as v1
from benchmarks.aerp5_product_paired_locomo_v2 import RssMonitor


SCHEMA = "aerp7-convomem-isolated-executor-v1"
FORMAL_SCHEMA = "aerp7-convomem-formal-executor-v1"
AUTH_SCHEMA = "aerp7-convomem-operator-authorization-v1"
FORMAL_AUTH_SCHEMA = "aerp7-convomem-formal-operator-authorization-v1"
CURRENT_PACKET_SCHEMA = "aerp7-convomem-current-worker-packet-v1"
FORMAL_CURRENT_PACKET_SCHEMA = "aerp7-convomem-formal-current-worker-packet-v1"
ORIGINAL_PACKET_SCHEMA = "aerp7-convomem-original-worker-packet-v1"
FORMAL_ORIGINAL_PACKET_SCHEMA = "aerp7-convomem-formal-original-worker-packet-v1"
FREEZE_PACKET_SCHEMA = "aerp7-convomem-public-freeze-packet-v1"
FORMAL_FREEZE_PACKET_SCHEMA = "aerp7-convomem-formal-public-freeze-packet-v1"
_PUBLIC_ROLE_FLAGS = frozenset({"--current-worker-stdin", "--original-worker-stdin"})
PUBLIC_CONFIG_KEYS = frozenset({"schema", "synthetic_test_mode", "protocol_path", "candidate_bundle", "output_dir", "authorization_path", "python_executable"})
FORMAL_PUBLIC_CONFIG_KEYS = frozenset({
    *PUBLIC_CONFIG_KEYS, "original_root", "model_dir",
})
CURRENT_CONFIG_KEYS = frozenset({"schema", "synthetic_test_mode", "protocol_path", "candidate_bundle", "worker_config", "output_path", "staging_parent", "execution_role"})
FORMAL_CURRENT_CONFIG_KEYS = frozenset({*CURRENT_CONFIG_KEYS, "model_dir"})
ORIGINAL_CONFIG_KEYS = frozenset({"schema", "synthetic_test_mode", "protocol_path", "candidate_bundle", "output_path", "build_id"})
FORMAL_ORIGINAL_CONFIG_KEYS = frozenset({
    "schema", "synthetic_test_mode", "protocol_path", "candidate_bundle",
    "output_path", "draft_path", "build_id", "original_root", "model_dir",
    "palace_path",
})


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CustodyError("executor_input_missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CustodyError("executor_input_invalid") from exc
    if not isinstance(value, Mapping):
        raise CustodyError("executor_input_invalid")
    return dict(value)


def _sync_parent(parent: Path) -> None:
    """Validate parent identity; fsync where the host exposes directory handles.

    Windows has no portable Python directory fsync.  The no-replace publication
    primitive still preserves exclusive output there; callers retain the exact
    receipt so a later retry can verify byte identity.
    """
    meta = os.lstat(parent)
    if parent.is_symlink() or not parent.is_dir() or meta.st_nlink < 1:
        raise CustodyError("executor_publish_parent_invalid")
    if os.name == "nt":
        return
    descriptor = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _write_bytes_new(path: Path, payload: bytes) -> None:
    if not path.parent.is_dir():
        raise CustodyError("executor_publish_parent_missing")
    formal.publish_nonreplace(path, payload, fsync_parent=_sync_parent)


def _write_new(path: Path, value: Mapping[str, Any]) -> None:
    """Hard-link exclusive publish: same bytes retry, conflicting output fails."""
    _write_bytes_new(path, _bytes(value))


def _runtime() -> dict[str, str]:
    return {"python": sys.version.split()[0], "platform": platform.platform(), "processor": platform.processor() or "unknown"}


def _percentiles(values: Sequence[int]) -> dict[str, int]:
    if not values or any(not isinstance(value, int) or value <= 0 for value in values):
        raise RuntimeError("executor latency sample invalid")
    rows = sorted(values)
    def at(fraction: float) -> int:
        return rows[min(len(rows) - 1, int((len(rows) - 1) * fraction))]
    return {"count": len(rows), "p50": at(.50), "p95": at(.95), "p99": at(.99), "max": rows[-1]}


def _resource(*, arm_id: str, artifact_sha256: str, denominators: Mapping[str, int], query_measurements: Sequence[Mapping[str, Any]], build_id: str | None = None, index_sha256: str | None = None, role: str | None = None, peak_rss_bytes: int = 1, allow_unfinalized_peak: bool = False, passage_embedding: Mapping[str, Any] | None = None, query_embedding: Mapping[str, Any] | None = None, measurement_mode: str = "synthetic_rehearsal", resource_comparability: str = "strict", storage_bytes: int = 0, storage_scope: str | None = None) -> dict[str, Any]:
    execution_role = role or ("fresh_build" if arm_id == "original_public_product" else "primary")
    accounting = {"primary": "primary_excludes_repeat", "repeat": "repeat_measured_separately"}[execution_role] if arm_id == "static_p5" else "not_applicable"
    measurements = [dict(item) for item in query_measurements]
    walls = [item.get("wall_ns") for item in measurements]; cpus = [item.get("cpu_ns") for item in measurements]
    if len(measurements) != int(denominators["query_count"]) or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in [*walls, *cpus]):
        raise RuntimeError("executor raw query measurements invalid")
    current_arm = arm_id != "original_public_product"
    passage = dict(passage_embedding or {"calls": 0, "texts": 0})
    query = dict(query_embedding or {"calls": 0, "texts": 0})
    if current_arm:
        for item in (passage, query):
            item.update({"measurement_kind": "encoder_adapter_api_calls", "native_embedding_observable": True, "limitation": None})
    else:
        passage.update({"measurement_kind": "public_upsert_request_proxy", "native_embedding_observable": False, "limitation": "native_internal_embedding_calls_unobservable; public upsert request ledger only"})
        query.update({"measurement_kind": "public_search_request_proxy", "native_embedding_observable": False, "limitation": "native_internal_embedding_calls_unobservable; public search request ledger only"})
    if isinstance(peak_rss_bytes, bool) or not isinstance(peak_rss_bytes, int) or peak_rss_bytes < 0 or (peak_rss_bytes == 0 and not allow_unfinalized_peak):
        raise RuntimeError("executor peak RSS observation invalid")
    receipt = {
        "schema": formal.RESOURCE_SCHEMA,
        "arm_id": arm_id,
        "execution_role": execution_role,
        "resource_semantics": "all_six_views_computed_then_raw_fusion_weights" if arm_id == "strong_raw" else "native_public_product" if arm_id == "original_public_product" else "all_six_views_computed_then_fixed_fusion",
        "measurement_scope": "rank_only_excludes_trace_and_receipt_serialization",
        "measurement_mode": measurement_mode,
        "resource_comparability": resource_comparability,
        "p5_repeat_accounting": accounting,
        "ingest_seconds": 0.0,
        "index_seconds": 0.0,
        "query_measurements": measurements,
        "query_latency_ns": {"wall": _percentiles(walls), "cpu": _percentiles(cpus)},
        "passage_embedding": passage,
        "query_embedding": query,
        "storage_scope": storage_scope or ("no_persistent_index" if current_arm else "palace_directory_after_cold_reopen"),
        "storage_bytes": int(storage_bytes),
        "peak_rss_bytes": int(peak_rss_bytes),
        "artifact_sha256": artifact_sha256,
        "build_id": build_id,
        "index_sha256": index_sha256,
        "input_denominators": dict(denominators),
        "hardware_runtime": _runtime(),
    }
    receipt["resource_sha256"] = formal.resource_digest(receipt)
    return receipt


class _SyntheticEncoder:
    """Explicitly test-only deterministic encoder; never selected in formal mode."""
    identity = "synthetic-encoder"
    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [[float(len(text) + index + 1), 1.0] for index, text in enumerate(texts)]
    def encode_query(self, text: str) -> list[float]:
        return [float(len(text) + 1), 1.0]


class _CountingSyntheticEncoder(_SyntheticEncoder):
    def __init__(self) -> None:
        self.passage_calls = self.passage_texts = self.query_calls = self.query_texts = 0
    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.passage_calls += 1; self.passage_texts += len(texts)
        return super().encode_passages(texts)
    def encode_query(self, text: str) -> list[float]:
        self.query_calls += 1; self.query_texts += 1
        return super().encode_query(text)


def _synthetic_query_measurements(projection: Mapping[str, Any], elapsed_ns: int) -> list[dict[str, Any]]:
    """Explicitly synthetic fallback timing for the original shape-oracle only.

    The current worker never uses this helper: it passes ranker's per-query
    sidecar measurements.  Keeping this separate makes a synthetic original
    receipt incapable of being misread as a live timing result.
    """
    items = sorted(validate_candidate_projection(projection)["items"], key=lambda row: row["item_id"])
    each = max(1, int(elapsed_ns) // len(items))
    return [{"item_id": item["item_id"], "query_sha256": rank._query_digest(item["query_text"]), "wall_ns": each, "cpu_ns": each} for item in items]


class _SupervisorTreeObserver(RssMonitor):
    """Supervisor-side sampled process-tree/RSS observation.

    The worker cannot prove anything about descendants.  This observer runs in
    the parent process for the complete child lifetime and records every
    descendant PID seen by the sampler.  Polling is deliberately identified as
    non-exhaustive: a missing/failed sample is never interpreted as zero
    children, and an otherwise complete polling lifecycle is not accepted as
    formal proof that a short-lived descendant never existed.
    """

    def __init__(self, root_pid: int) -> None:
        super().__init__(root_pid)
        self._descendant_pids: set[int] = set()
        self._sample_count = 0

    def _sample_once(self) -> None:
        import psutil

        try:
            root = psutil.Process(self.root_pid)
            descendants = list(root.children(recursive=True))
        except psutil.NoSuchProcess:
            return
        self._sample_count += 1
        self._descendant_pids.update(int(process.pid) for process in descendants)
        total = 0
        for process in [root, *descendants]:
            try:
                if process.is_running():
                    total += int(process.memory_info().rss)
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        self.peak_bytes = max(self.peak_bytes, total)
        if total > self.cap_bytes:
            self.error = f"RSS cap exceeded: {total} > {self.cap_bytes}"

    def receipt(self) -> dict[str, Any]:
        if self.error is not None:
            raise RuntimeError(self.error)
        if self._sample_count < 2 or self.peak_bytes <= 0:
            raise RuntimeError("supervisor process-tree observation incomplete")
        return {
            "observed_process_tree_peak_rss_bytes": int(self.peak_bytes),
            "descendant_process_count": len(self._descendant_pids),
            "descendant_processes_observed": bool(self._descendant_pids),
            "descendant_observation_method": "psutil_polling_non_exhaustive",
            "supervisor_observation_samples": self._sample_count,
            "supervisor_observation_complete": True,
        }


def _assert_synthetic(config: Mapping[str, Any]) -> None:
    if config.get("synthetic_test_mode") is not True:
        raise CustodyError("executor_formal_execution_blocked")


def _assert_original_mode(config: Mapping[str, Any]) -> bool:
    synthetic = config.get("synthetic_test_mode")
    expected = ORIGINAL_CONFIG_KEYS if synthetic is True else FORMAL_ORIGINAL_CONFIG_KEYS
    expected_schema = SCHEMA if synthetic is True else FORMAL_SCHEMA
    if set(config) != expected or config.get("schema") != expected_schema:
        raise CustodyError("executor_original_config_invalid")
    if synthetic is True:
        _assert_synthetic(config)
        return True
    if synthetic is not False:
        raise CustodyError("executor_formal_execution_blocked")
    return False


def _formal_original_resource(*, draft: original_product.OriginalProductWorkerDraft, replicate: Mapping[str, Any], denominators: Mapping[str, int], resource_comparability: str) -> dict[str, Any]:
    telemetry = draft.telemetry.get("resources")
    if not isinstance(telemetry, Mapping):
        raise CustodyError("executor_original_resource_invalid")
    try:
        measurements = original_product.validate_query_measurements(
            measurements=telemetry.get("query_measurements"),
            replicate=replicate,
            expected_count=int(denominators["query_count"]),
        )
        original_product._validate_clock_receipt(telemetry.get("clock_receipt"))
    except original_product.OriginalProductError as exc:
        raise CustodyError("executor_original_resource_invalid") from exc
    if telemetry["clock_receipt"].get("timing_source") != "stdlib":
        raise CustodyError("executor_original_clock_source_invalid")
    if telemetry.get("process_cpu_scope") != "worker_process_only_excludes_descendants" or telemetry.get("descendant_observation") != "external_supervisor_zero_required":
        raise CustodyError("executor_original_resource_descendant_process_invalid")
    # The child cannot observe a durable process-tree RSS maximum without
    # authoring its own claim.  ``0`` is an explicit pre-supervisor sentinel;
    # finalize_original_resource binds the external positive observation later.
    if telemetry.get("peak_rss_bytes") != 0:
        raise CustodyError("executor_original_resource_peak_not_unfinalized")
    storage_bytes = telemetry.get("storage_bytes")
    if isinstance(storage_bytes, bool) or not isinstance(storage_bytes, int) or storage_bytes <= 0:
        raise CustodyError("executor_original_resource_invalid")
    passage = telemetry.get("passage_embedding")
    query = telemetry.get("query_embedding")
    if not isinstance(passage, Mapping) or not isinstance(query, Mapping):
        raise CustodyError("executor_original_resource_invalid")
    for metric in (passage, query):
        if isinstance(metric.get("calls"), bool) or not isinstance(metric.get("calls"), int) or metric["calls"] <= 0:
            raise CustodyError("executor_original_resource_invalid")
        if isinstance(metric.get("texts"), bool) or not isinstance(metric.get("texts"), int) or metric["texts"] <= 0:
            raise CustodyError("executor_original_resource_invalid")
    return _resource(
        arm_id="original_public_product",
        artifact_sha256="0" * 64,
        denominators=denominators,
        query_measurements=measurements,
        build_id=str(replicate["build_id"]),
        index_sha256=str(replicate["index_sha256"]),
        peak_rss_bytes=0,
        allow_unfinalized_peak=True,
        passage_embedding={"calls": passage["calls"], "texts": passage["texts"]},
        query_embedding={"calls": query["calls"], "texts": query["texts"]},
        measurement_mode="live_original_public_product",
        resource_comparability=resource_comparability,
        storage_bytes=storage_bytes,
        storage_scope="palace_directory_after_cold_reopen",
    )


def finalize_original_resource(*, resource: Mapping[str, Any], supervisor: Mapping[str, Any], worker_pid: int) -> dict[str, Any]:
    """Bind a live original resource draft to the external RSS observation."""
    row = dict(resource)
    if row.get("arm_id") != "original_public_product" or row.get("peak_rss_bytes") != 0:
        raise CustodyError("executor_original_resource_finalize_precondition_invalid")
    required_observation = {
        "observed_process_tree_peak_rss_bytes",
        "descendant_process_count",
        "descendant_processes_observed",
        "descendant_observation_method",
        "supervisor_observation_samples",
        "supervisor_observation_complete",
    }
    if not isinstance(supervisor, Mapping) or not required_observation <= set(supervisor):
        raise CustodyError("executor_original_resource_descendant_observation_missing")
    if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0 or supervisor.get("pid") != worker_pid or supervisor.get("exit_code") != 0:
        raise CustodyError("executor_original_resource_supervisor_binding_invalid")
    if supervisor.get("descendant_processes_observed") is not False or supervisor.get("supervisor_observation_complete") is not True:
        raise CustodyError("executor_original_resource_descendant_process_invalid")
    observation_method = supervisor.get("descendant_observation_method")
    if observation_method not in {"os_enforced_complete_process_group", "psutil_polling_non_exhaustive"}:
        raise CustodyError("executor_original_resource_descendant_observation_insufficient")
    if row.get("resource_comparability") == "strict" and observation_method != "os_enforced_complete_process_group":
        raise CustodyError("executor_original_resource_strict_comparability_unavailable")
    if row.get("resource_comparability") != "strict" and observation_method == "os_enforced_complete_process_group":
        # An appendix-grade observation is allowed to be stronger than the
        # efficacy protocol requires, but it never upgrades the frozen claim.
        pass
    descendant_count = supervisor["descendant_process_count"]
    if isinstance(descendant_count, bool) or not isinstance(descendant_count, int) or descendant_count != 0:
        raise CustodyError("executor_original_resource_descendant_process_invalid")
    samples = supervisor.get("supervisor_observation_samples")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < 2:
        raise CustodyError("executor_original_resource_descendant_observation_incomplete")
    observed = supervisor.get("observed_process_tree_peak_rss_bytes")
    if isinstance(observed, bool) or not isinstance(observed, int) or observed <= 0:
        raise CustodyError("executor_original_resource_supervisor_peak_invalid")
    row["peak_rss_bytes"] = observed
    row["resource_sha256"] = formal.resource_digest(row)
    return row


def _sanitized_env() -> dict[str, str]:
    # An explicit small allowlist prevents inherited custody, API, proxy and
    # experiment variables from becoming worker capabilities.  PYTHONPATH is
    # reconstructed to the repository root because children run in an empty
    # temporary cwd.
    retained: dict[str, str] = {}
    for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value: retained[key] = value
    retained["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    retained["AERP7_EXECUTOR_PUBLIC_ROLE"] = "1"
    return retained


def assert_public_command(command: Sequence[str], env: Mapping[str, str]) -> None:
    # Paths are opaque capabilities: rejecting words such as ``scorer`` inside a
    # random temporary-directory name is both brittle and unrelated to whether a
    # worker received a private capability.  Enforce the exact executable/module/
    # role shape and a closed environment-key set instead; child config schemas
    # separately reject every extra field.
    if (
        len(command) != 4
        or Path(str(command[0])).resolve() != Path(sys.executable).resolve()
        or list(command[1:3]) != ["-m", "benchmarks.aerp7_convomem_executor"]
        or command[3] not in _PUBLIC_ROLE_FLAGS
    ):
        raise CustodyError("executor_public_command_invalid")
    allowed = {"SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "PYTHONPATH", "AERP7_EXECUTOR_PUBLIC_ROLE"}
    if set(env) - allowed or env.get("AERP7_EXECUTOR_PUBLIC_ROLE") != "1" or not env.get("PYTHONPATH"):
        raise CustodyError("executor_public_environment_invalid")


def live_executor_code_receipt() -> dict[str, Any]:
    state = v1.git_state(Path(__file__).resolve().parents[1])
    # Rehearsals bind the exact dirty-tree digest too.  Formal authorization uses
    # the protocol's clean code receipt and is checked separately.
    return {
        "head": state["git_head"], "tree": state["git_tree"],
        "diff_digest": state["worktree_diff_sha256"], "git_dirty": state["git_dirty"],
        "state_policy": "synthetic_exact_worktree_state_bound",
    }


def _current_execution_receipt(*, role: str, arm_id: str, protocol: Mapping[str, Any], projection: Mapping[str, Any], worker_config: Mapping[str, Any], encoder: Any, artifact: Mapping[str, Any], resource: Mapping[str, Any]) -> dict[str, Any]:
    """Build the non-ranking execution sidecar for one current subprocess.

    Synthetic workers bind their observed synthetic encoder explicitly.  The
    future live helper replaces these model observations with a complete native
    MiniLM tree receipt; it cannot borrow this synthetic receipt.
    """
    code = live_executor_code_receipt()
    model = {"mode": "synthetic_rehearsal", "protocol_model_sha256": _digest(protocol["model_receipt"]), "encoder_identity": getattr(encoder, "identity", None)}
    receipt = {
        "schema": formal.CURRENT_EXECUTION_RECEIPT_SCHEMA,
        "execution_mode": "synthetic_rehearsal",
        "execution_role": role,
        "arm_id": arm_id,
        "protocol_sha256": protocol["protocol_sha256"],
        "projection_sha256": canonical_sha256(projection),
        "worker_config_sha256": _digest(worker_config),
        "method_input_sha256": _digest({"arm_id": arm_id, "method_receipt": artifact["method_receipt"], "serializer_receipt": artifact["serializer_receipt"]}),
        "observed_code_before": code,
        "observed_code_after": code,
        "observed_model_before": model,
        "observed_model_after": model,
        "provider": {"mode": "synthetic", "providers": []},
        "encoder_identity": getattr(encoder, "identity", None),
        "artifact_file_sha256": hashlib.sha256(_bytes(artifact)).hexdigest(),
        "artifact_sha256": artifact["artifact_sha256"],
        "resource_sha256": resource["resource_sha256"],
        "process_id": os.getpid(),
        # Coordinator fills this with the independent supervisor digest before
        # aggregate validation; the worker output itself remains immutable.
        "supervisor_sha256": "0" * 64,
        "execution_sha256": "",
    }
    receipt["execution_sha256"] = _digest({key: item for key, item in receipt.items() if key != "execution_sha256"})
    return receipt


def _clean_protocol_code_observation(*, expected: Mapping[str, Any]) -> dict[str, Any]:
    state = v1.git_state(Path(__file__).resolve().parents[1])
    observed = {"head": state["git_head"], "tree": state["git_tree"], "diff_digest": state["worktree_diff_sha256"], "dirty_policy": "clean_required"}
    if state["git_dirty"] or observed != dict(expected):
        raise CustodyError("executor_current_live_code_drift")
    return observed


def _live_model_observation(*, model_dir: Path, expected: Mapping[str, Any]) -> tuple[dict[str, Any], Any]:
    """Bind a live native adapter to every pinned model file, fail-closed.

    The ranking receipt historically permits logical-only file entries for old
    synthetic fixtures.  This helper does not: each live receipt must name every
    relative path before it is allowed to select the native MiniLM adapter.
    """
    actual = v1.file_tree_receipt(model_dir)
    expected_files = expected.get("files") if isinstance(expected, Mapping) else None
    if not isinstance(expected_files, list) or not expected_files or any(not isinstance(row, Mapping) or set(row) != {"path_role", "relative_path", "sha256", "bytes"} for row in expected_files):
        raise CustodyError("executor_current_live_model_relative_paths_required")
    expected_tree = sorted(({"relative_path": row["relative_path"], "sha256": row["sha256"], "bytes": row["bytes"]} for row in expected_files), key=lambda row: row["relative_path"])
    if actual["files"] != expected_tree:
        raise CustodyError("executor_current_live_model_tree_drift")
    encoder = v1.native_minilm_adapter(model_dir)
    if encoder.identity != expected.get("encoder_identity"):
        raise CustodyError("executor_current_live_encoder_identity_mismatch")
    observation = {
        "model_file_tree_sha256": actual["sha256"], "model_file_tree_bytes": actual["bytes"],
        "encoder_identity": encoder.identity,
        "runtime_identity": {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"], "model_file_tree_sha256": actual["sha256"]},
    }
    return observation, encoder


def run_live_current_execution(*, role: str, protocol: Mapping[str, Any], projection: Mapping[str, Any], worker_config: Mapping[str, Any], model_dir: Path) -> dict[str, Any]:
    """Execute one live, pinned current arm; synthetic packets cannot reach it."""
    frozen = formal.validate_formal_protocol(protocol)
    if role not in {"raw", "p5_primary", "p5_repeat", "six"}:
        raise CustodyError("executor_current_role_invalid")
    arm_id = {"raw": "strong_raw", "p5_primary": "static_p5", "p5_repeat": "static_p5", "six": "six_view_secondary"}[role]
    code_before = _clean_protocol_code_observation(expected=frozen["current_code_receipt"])
    model_before, encoder = _live_model_observation(model_dir=model_dir, expected=frozen["model_receipt"])
    measurements: list[dict[str, Any]] = []
    with RssMonitor(os.getpid()) as monitor:
        artifact = rank.rank_projection(projection=projection, encoder=encoder, arm_id=arm_id, model_receipt=frozen["model_receipt"], code_receipt=frozen["current_code_receipt"], query_measurements=measurements)
    model_after, _unused = _live_model_observation(model_dir=model_dir, expected=frozen["model_receipt"])
    code_after = _clean_protocol_code_observation(expected=frozen["current_code_receipt"])
    adapter = encoder.receipt()
    resource = _resource(
        arm_id=arm_id, artifact_sha256=artifact["artifact_sha256"], denominators=formal.projection_denominators(projection),
        query_measurements=measurements, role={"p5_primary": "primary", "p5_repeat": "repeat"}.get(role),
        peak_rss_bytes=monitor.peak_bytes,
        passage_embedding={"calls": adapter["passage_call_count"], "texts": adapter["passage_text_count"]},
        query_embedding={"calls": adapter["query_call_count"], "texts": adapter["query_text_count"]},
        measurement_mode="live_native_adapter",
        resource_comparability=frozen["resource_thresholds"]["resource_comparability"],
        storage_bytes=0,
    )
    receipt = _current_execution_receipt(role=role, arm_id=arm_id, protocol=frozen, projection=projection, worker_config=worker_config, encoder=encoder, artifact=artifact, resource=resource)
    receipt.update({
        "execution_mode": "live_native_adapter",
        "observed_code_before": code_before, "observed_code_after": code_after,
        "observed_model_before": model_before, "observed_model_after": model_after,
        # Normalize away adapter cache topology.  Each isolated worker owns its
        # object lifecycle, and the formal contract is CPU provider + pinned
        # model tree, not an in-process sharing implementation detail.
        "provider": {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"], "model_file_tree_sha256": model_before["model_file_tree_sha256"]},
    })
    receipt["execution_sha256"] = _digest({key: item for key, item in receipt.items() if key != "execution_sha256"})
    return {"artifact": artifact, "resource_receipt": resource, "execution_receipt": receipt}


def _operator_secret() -> bytes:
    value = os.environ.get("AERP7_OPERATOR_AUTH_CAPABILITY")
    if value is None or len(value.encode("utf-8")) < 32:
        raise CustodyError("executor_operator_capability_missing")
    return value.encode("utf-8")


def _authorization(value: Any, *, protocol: Mapping[str, Any], output_dir: Path, capability: bytes) -> dict[str, Any]:
    required = {"schema", "mode", "synthetic_test_mode", "protocol_sha256", "executor_code_receipt", "output_dir", "nonce", "expires_at_unix", "output_absent", "authorization_sha256", "operator_hmac"}
    if not isinstance(value, Mapping) or set(value) != required:
        raise CustodyError("executor_authorization_invalid")
    row = dict(value)
    synthetic = row.get("synthetic_test_mode")
    expected_schema = AUTH_SCHEMA if synthetic is True else FORMAL_AUTH_SCHEMA
    expected_mode = "synthetic_rehearsal" if synthetic is True else "formal_live"
    if row["schema"] != expected_schema or row["mode"] != expected_mode or synthetic not in {True, False} or row["protocol_sha256"] != protocol["protocol_sha256"] or row["output_absent"] is not True or row["output_dir"] != str(output_dir.resolve()):
        raise CustodyError("executor_authorization_invalid")
    if not isinstance(row["nonce"], str) or len(row["nonce"]) < 32 or not isinstance(row["expires_at_unix"], int) or row["expires_at_unix"] <= int(time.time()):
        raise CustodyError("executor_authorization_expired")
    receipt = row["executor_code_receipt"]
    if synthetic is True:
        if receipt != live_executor_code_receipt():
            raise CustodyError("executor_authorization_code_invalid")
    elif receipt != protocol["current_code_receipt"]:
        raise CustodyError("executor_authorization_code_invalid")
    unsigned = {key: item for key, item in row.items() if key not in {"authorization_sha256", "operator_hmac"}}
    if row["authorization_sha256"] != _digest(unsigned):
        raise CustodyError("executor_authorization_digest_invalid")
    expected = hmac.new(capability, _bytes({**unsigned, "authorization_sha256": row["authorization_sha256"]}), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(row["operator_hmac"]), expected):
        raise CustodyError("executor_authorization_hmac_invalid")
    if output_dir.exists() or output_dir.is_symlink():
        raise CustodyError("executor_authorization_output_present")
    return row


def _consume_authorization(*, authorization: Mapping[str, Any], output_dir: Path) -> Path:
    marker = output_dir.parent / (".aerp7-authorization-consumed-" + str(authorization["nonce"]) + ".json")
    payload = _bytes({"schema": AUTH_SCHEMA + "-consume-v1", "authorization_sha256": authorization["authorization_sha256"], "protocol_sha256": authorization["protocol_sha256"], "nonce": authorization["nonce"], "consumed_by_pid": os.getpid()})
    result = formal.publish_nonreplace(marker, payload, fsync_parent=_sync_parent)
    # Even identical bytes mean another caller won the exclusive publication.
    # A consumed authorization is never a successful idempotent operation.
    if result["retry_idempotent"]:
        raise CustodyError("executor_authorization_already_consumed")
    return marker


def _generation_identity(path: Path) -> tuple[int, int]:
    """Return the identity of a real generation directory, never a link."""
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise CustodyError("executor_generation_missing") from exc
    if path.is_symlink() or not path.is_dir():
        raise CustodyError("executor_generation_identity_invalid")
    return (metadata.st_dev, metadata.st_ino)


def _new_staging_generation(*, output: Path, authorization: Mapping[str, Any]) -> Path:
    """Allocate the only mutable generation, as a sibling of the final output.

    The authorization is intentionally consumed before this call.  Therefore a
    failed generation is *not* retryable with the same authorization: an
    operator must issue a new, output-absent authorization after inspecting the
    failure.  This prevents a retry from quietly mixing two worker populations.
    """
    parent = output.parent
    if parent.is_symlink() or not parent.is_dir() or output.exists() or output.is_symlink():
        raise CustodyError("executor_generation_output_parent_invalid")
    nonce_digest = hashlib.sha256(str(authorization["nonce"]).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.mkdtemp(prefix=f".{output.name}.aerp7-staging-{nonce_digest}-", dir=parent))


def _discard_owned_generation(path: Path, identity: tuple[int, int]) -> None:
    """Remove only the generation this coordinator created, never a replacement."""
    if not path.exists():
        return
    if _generation_identity(path) != identity:
        raise CustodyError("executor_generation_cleanup_identity_drift")
    shutil.rmtree(path)
    if path.exists():
        raise CustodyError("executor_generation_cleanup_failed")
    _sync_parent(path.parent)


def _rename_generation_no_replace(staging: Path, output: Path) -> None:
    """Atomically publish a directory only if the final name remains absent.

    ``os.replace`` is expressly forbidden: it could overwrite a concurrently
    created generation.  Linux uses ``renameat2(RENAME_NOREPLACE)`` and Windows
    uses MoveFileEx without REPLACE_EXISTING.  Other platforms fail closed until
    an equivalent primitive is supplied.
    """
    if os.name == "nt":
        # MOVEFILE_WRITE_THROUGH asks Windows to flush the move, but it is not a
        # substitute for the durable directory-fsync proof required for formal
        # execution.  Formal mode remains disabled on this implementation.
        move_file_ex = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move_file_ex.restype = ctypes.c_int
        if not move_file_ex(str(staging), str(output), 0x00000008):  # WRITE_THROUGH; never REPLACE_EXISTING
            error = ctypes.get_last_error()
            if error in {80, 183}:  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
                raise CustodyError("executor_generation_output_present")
            raise CustodyError("executor_generation_publish_failed")
        return
    if sys.platform.startswith("linux"):
        try:
            renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
        except AttributeError as exc:
            raise CustodyError("executor_generation_atomic_publish_unavailable") from exc
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(staging), -100, os.fsencode(output), 1) != 0:  # AT_FDCWD, RENAME_NOREPLACE
            error = ctypes.get_errno()
            if error in {errno.EEXIST, errno.ENOTEMPTY}:
                raise CustodyError("executor_generation_output_present")
            raise CustodyError("executor_generation_publish_failed")
        return
    raise CustodyError("executor_generation_atomic_publish_unavailable")


def _publish_generation(*, staging: Path, staging_identity: tuple[int, int], output: Path) -> None:
    """Publish a fully validated sibling generation without a replacement path."""
    if output.exists() or output.is_symlink() or _generation_identity(staging) != staging_identity:
        raise CustodyError("executor_generation_publish_precondition_invalid")
    _sync_parent(staging)
    _sync_parent(output.parent)
    try:
        _rename_generation_no_replace(staging, output)
    except BaseException:
        # The staging name remains ours when no-replace publication fails.
        _discard_owned_generation(staging, staging_identity)
        raise
    try:
        if _generation_identity(output) != staging_identity:
            raise CustodyError("executor_generation_publish_identity_drift")
        _sync_parent(output.parent)
    except BaseException:
        # A post-rename durability failure must not be reported as a successful
        # publication.  Delete only the inode we renamed, then fail closed.
        _discard_owned_generation(output, staging_identity)
        raise


def _candidate_projection(protocol: Mapping[str, Any], bundle: Path, worker_config: Mapping[str, Any], staging_parent: Path) -> dict[str, Any]:
    return formal.load_candidate_worker_projection(worker_config=worker_config, protocol=protocol, candidate_bundle_root=bundle, staging_parent=staging_parent)


def current_worker(config: Mapping[str, Any]) -> dict[str, Any]:
    synthetic = config.get("synthetic_test_mode")
    expected_keys = CURRENT_CONFIG_KEYS if synthetic is True else FORMAL_CURRENT_CONFIG_KEYS
    expected_schema = SCHEMA if synthetic is True else FORMAL_SCHEMA
    if set(config) != expected_keys or config.get("schema") != expected_schema:
        raise CustodyError("executor_current_config_invalid")
    if synthetic is True:
        _assert_synthetic(config)
    elif synthetic is not False:
        raise CustodyError("executor_current_config_invalid")
    protocol = formal.validate_formal_protocol(_load(Path(str(config["protocol_path"]))))
    worker_config = _load(Path(str(config["worker_config"])))
    bundle = Path(str(config["candidate_bundle"])); staging_parent = Path(str(config["staging_parent"])); output = Path(str(config["output_path"]))
    role = str(config["execution_role"])
    role_to_arm = {"raw": "strong_raw", "p5_primary": "static_p5", "p5_repeat": "static_p5", "six": "six_view_secondary"}
    if role not in role_to_arm: raise CustodyError("executor_current_role_invalid")
    if synthetic is False:
        model_dir = Path(str(config["model_dir"]))
        if not model_dir.is_dir() or model_dir.is_symlink():
            raise CustodyError("executor_current_live_input_invalid")
        projection = _candidate_projection(protocol, bundle, worker_config, staging_parent)
        live = run_live_current_execution(
            role=role, protocol=protocol, projection=projection,
            worker_config=worker_config, model_dir=model_dir,
        )
        packet = {
            "schema": FORMAL_CURRENT_PACKET_SCHEMA, "execution_role": role,
            **live, "process_id": os.getpid(), "packet_sha256": "",
        }
        packet["packet_sha256"] = _digest({key: item for key, item in packet.items() if key != "packet_sha256"})
        _write_new(output, packet)
        return packet
    encoder = _CountingSyntheticEncoder(); encoder.identity = protocol["model_receipt"]["encoder_identity"]; query_measurements: list[dict[str, Any]] = []
    with RssMonitor(os.getpid()) as monitor:
        projection = _candidate_projection(protocol, bundle, worker_config, staging_parent)
        artifact = rank.rank_projection(projection=projection, encoder=encoder, arm_id=role_to_arm[role], model_receipt=protocol["model_receipt"], code_receipt=protocol["current_code_receipt"], query_measurements=query_measurements)
    denominators = formal.projection_denominators(projection)
    resource = _resource(
        arm_id=role_to_arm[role], artifact_sha256=artifact["artifact_sha256"], denominators=denominators,
        query_measurements=query_measurements, role={"p5_primary": "primary", "p5_repeat": "repeat"}.get(role),
        peak_rss_bytes=monitor.peak_bytes,
        passage_embedding={"calls": encoder.passage_calls, "texts": encoder.passage_texts},
        query_embedding={"calls": encoder.query_calls, "texts": encoder.query_texts},
    )
    execution_receipt = _current_execution_receipt(role=role, arm_id=role_to_arm[role], protocol=protocol, projection=projection, worker_config=worker_config, encoder=encoder, artifact=artifact, resource=resource)
    packet = {"schema": CURRENT_PACKET_SCHEMA, "execution_role": role, "artifact": artifact, "resource_receipt": resource, "execution_receipt": execution_receipt, "process_id": os.getpid(), "packet_sha256": ""}
    packet["packet_sha256"] = _digest({key: item for key, item in packet.items() if key != "packet_sha256"})
    _write_new(output, packet); return packet


def _synthetic_original_replicate(projection: Mapping[str, Any], protocol: Mapping[str, Any], build_id: str) -> dict[str, Any]:
    """Synthetic-only shape oracle for subprocess isolation tests, not product output."""
    encoder = _SyntheticEncoder(); encoder.identity = protocol["model_receipt"]["encoder_identity"]
    current = rank.rank_projection(projection=projection, encoder=encoder, arm_id="strong_raw", model_receipt=protocol["model_receipt"], code_receipt=protocol["current_code_receipt"])
    corpora = {corpus["corpus_id"]: corpus for corpus in projection["corpora"]}; items = {item["item_id"]: item for item in projection["items"]}
    rows = [{**{key: value for key, value in row.items() if key not in {"confidence", "confidence_receipt"}}, "candidate_input_sha256": rank._candidate_input(corpora[items[row["item_id"]]["corpus_id"]], rank.ORIGINAL_MEMPALACE_SERIALIZER), "confidence": None, "confidence_receipt": None} for row in current["rankings"]]
    traces = [{key: value for key, value in row.items() if key in {"item_id", "query_sha256", "ranked_count", "ranking_sha256"}} for row in current["trace_receipt"]]
    for row in traces:
        row["candidate_input_sha256"] = rank._candidate_input(corpora[items[row["item_id"]]["corpus_id"]], rank.ORIGINAL_MEMPALACE_SERIALIZER)
    inputs = rank._input_receipt(projection, rank.ORIGINAL_MEMPALACE_SERIALIZER)
    physical_ids = sorted(f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in projection["corpora"] for candidate in corpus["candidates"])
    physical = {"physical_count": len(physical_ids), "physical_ids_sha256": rank._digest(physical_ids), "embedding": {"count": len(physical_ids), "dimension": 384, "dtype": "float32", "float32_sha256": _digest([build_id, "synthetic-embedding"])}, "hnsw_config": rank.ORIGINAL_HNSW_CONFIG, "graph_files": [{"name": name, "bytes": 1, "sha256": _digest([build_id, name])} for name in rank.ORIGINAL_GRAPH_NAMES], "immutable_backend_sha256": _digest([build_id, "backend"]), "sqlite_semantic_sha256": _digest([build_id, "sqlite"]), "operational_delta": rank.ORIGINAL_OPERATIONAL_DELTA}
    index = {"build_id": build_id, "fresh_build": True, "collection_identity": "synthetic-collection-" + build_id, "index_identity_sha256": "", "cold_reopen": True, "call_contract": rank.ORIGINAL_CALL_CONTRACT, "input_coverage_sha256": rank._digest(inputs["item_corpora"]), "query_coverage_sha256": rank._digest([{"item_id": item["item_id"], "query_sha256": rank._query_digest(item["query_text"])} for item in sorted(projection["items"], key=lambda item: item["item_id"])]), "output_coverage_sha256": rank._digest([{"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in sorted(traces, key=lambda item: item["item_id"])]), "worker_physical_receipt": physical, "coordinator_physical_receipt": dict(physical)}
    index["index_identity_sha256"] = rank._digest({"collection_identity": index["collection_identity"], "physical": physical})
    return {"build_id": build_id, "input_receipt": inputs, "input_sha256": rank._digest(inputs), "index_receipt": index, "index_sha256": rank._digest(index), "trace_receipt": traces, "trace_sha256": rank._digest(traces), "rankings": rows}


def original_worker(config: Mapping[str, Any]) -> dict[str, Any]:
    synthetic = _assert_original_mode(config)
    protocol = formal.validate_formal_protocol(_load(Path(str(config["protocol_path"]))))
    bundle = Path(str(config["candidate_bundle"])); output = Path(str(config["output_path"])); build_id = str(config["build_id"])
    # Candidate worker config is created in memory only for reading label-free data.
    worker_config = {"role": "candidate_ranker", "projection_sha256": protocol["candidate"]["projection_canonical_sha256"], "projection_raw_sha256": protocol["candidate"]["projection_raw_sha256"], "projection_path": "projection.json", "model_receipt": protocol["model_receipt"], "code_receipt": protocol["current_code_receipt"], "staging_root": "staging", "arms": ["strong_raw", "static_p5", "six_view_secondary"], "top_k": 10, "tie_break": "stable_ranking_key_ascending", "serializer_contract": protocol["serializer_contract"]}
    staging = Path(tempfile.mkdtemp(prefix="aerp7-original-synthetic-"))
    (staging / "staging").mkdir()
    try:
        projection = _candidate_projection(protocol, bundle, worker_config, staging)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    denominators = formal.projection_denominators(projection)
    if synthetic:
        started = time.perf_counter_ns()
        with RssMonitor(os.getpid()) as monitor:
            replicate = _synthetic_original_replicate(projection, protocol, build_id)
        elapsed = max(1, time.perf_counter_ns() - started)
        resource = _resource(
            arm_id="original_public_product", artifact_sha256="0" * 64, denominators=denominators,
            query_measurements=_synthetic_query_measurements(projection, elapsed),
            build_id=replicate["build_id"], index_sha256=replicate["index_sha256"],
            peak_rss_bytes=monitor.peak_bytes,
            passage_embedding={"calls": len(projection["corpora"]), "texts": denominators["candidate_text_count"]},
            query_embedding={"calls": denominators["query_count"], "texts": denominators["query_count"]},
            storage_bytes=1,
        )
        packet = {"schema": ORIGINAL_PACKET_SCHEMA, "replicate": replicate, "resource_receipt": resource, "process_id": os.getpid(), "packet_sha256": ""}
    else:
        draft_path = Path(str(config["draft_path"])); palace_path = Path(str(config["palace_path"]))
        if output.parent.resolve() != draft_path.parent.resolve() or output.parent.resolve() != palace_path.parent.resolve() or any(path.exists() or path.is_symlink() for path in (output, draft_path, palace_path)):
            raise CustodyError("executor_original_output_capability_invalid")
        original_root, model_dir = Path(str(config["original_root"])), Path(str(config["model_dir"]))
        if not original_root.is_dir() or original_root.is_symlink() or not model_dir.is_dir() or model_dir.is_symlink():
            raise CustodyError("executor_original_live_input_invalid")
        with original_product.pinned_live_original_product(
            original_root=original_root, model_dir=model_dir, palace_path=palace_path,
        ) as (seams, live_receipt):
            observer = original_product.LiveOriginalObserver(
                palace_path=palace_path, provider=seams.encoder.runtime_identity,
                denominators=denominators, corpus_count=len(projection["corpora"]),
            )
            draft = original_product.run_original_public_replicate(
                projection=projection, build_id=build_id, collection_identity=f"{build_id}-collection",
                palace_path=palace_path, observer=observer, seams=seams, live_receipt=live_receipt,
                formal=True, resource_sink=lambda _value: None,
            )
        draft_bytes = original_product.serialize_worker_draft(draft)
        _write_bytes_new(draft_path, draft_bytes)
        replicate = draft.replicate_without_coordinator_audit
        resource = _formal_original_resource(
            draft=draft, replicate=replicate, denominators=denominators,
            resource_comparability=protocol["resource_thresholds"]["resource_comparability"],
        )
        packet = {
            "schema": FORMAL_ORIGINAL_PACKET_SCHEMA, "execution_mode": "exact_public_product_worker_draft",
            "draft_file_sha256": hashlib.sha256(draft_bytes).hexdigest(),
            "palace_path": str(palace_path.resolve()), "resource_receipt": resource,
            "process_id": os.getpid(), "packet_sha256": "",
        }
    packet["packet_sha256"] = _digest({key: item for key, item in packet.items() if key != "packet_sha256"})
    _write_new(output, packet); return packet


def coordinator_reaudit_original_worker_packet(*, packet: Mapping[str, Any], draft_path: Path, palace_path: Path, projection: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn one exact worker draft into a replica only after coordinator audit.

    This function deliberately reads the canonical draft file after the worker
    has exited.  It never trusts a worker-supplied coordinator receipt or index
    digest and retains the palace so the coordinator can remeasure it directly.
    """
    required = {"schema", "execution_mode", "draft_file_sha256", "palace_path", "resource_receipt", "process_id", "packet_sha256"}
    if set(packet) != required or packet.get("schema") != FORMAL_ORIGINAL_PACKET_SCHEMA or packet.get("execution_mode") != "exact_public_product_worker_draft":
        raise CustodyError("executor_original_worker_packet_invalid")
    if packet.get("packet_sha256") != _digest({key: item for key, item in packet.items() if key != "packet_sha256"}) or packet.get("palace_path") != str(palace_path.resolve()):
        raise CustodyError("executor_original_worker_packet_invalid")
    if not draft_path.is_file() or draft_path.is_symlink():
        raise CustodyError("executor_original_worker_draft_missing")
    draft_bytes = draft_path.read_bytes()
    if hashlib.sha256(draft_bytes).hexdigest() != packet.get("draft_file_sha256"):
        raise CustodyError("executor_original_worker_draft_digest_invalid")
    draft = original_product.load_worker_draft(draft_bytes)
    worker_replicate = draft.replicate_without_coordinator_audit
    resource = dict(packet["resource_receipt"])
    if resource.get("build_id") != worker_replicate.get("build_id") or resource.get("index_sha256") != worker_replicate.get("index_sha256"):
        raise CustodyError("executor_original_worker_resource_crossbinding_invalid")
    replicate = original_product.coordinator_reaudit_replicate(
        draft=draft, palace_path=palace_path, projection=projection,
    )
    if replicate.get("build_id") != worker_replicate.get("build_id"):
        raise CustodyError("executor_original_worker_resource_crossbinding_invalid")
    # The worker measurement first binds the worker-only index receipt.  After
    # independent re-audit, the coordinator must bind the completed index digest
    # (which necessarily changes when coordinator_physical_receipt is added).
    resource["index_sha256"] = replicate["index_sha256"]
    resource["resource_sha256"] = formal.resource_digest(resource)
    return replicate, resource


def _load_stdin() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.buffer.read())
    except (OSError, json.JSONDecodeError) as exc:
        raise CustodyError("executor_stdin_config_invalid") from exc
    if not isinstance(value, Mapping):
        raise CustodyError("executor_stdin_config_invalid")
    return dict(value)


def _run_subprocess(command: Sequence[str], *, config: Mapping[str, Any], output: Path, timeout_seconds: float = 120.0) -> dict[str, Any]:
    env = _sanitized_env(); assert_public_command(command, env)
    if Path(str(config.get("output_path", ""))).resolve() != output.resolve():
        raise CustodyError("executor_public_config_capability_invalid")
    # Config arrives over stdin from an already validated in-memory mapping.  A
    # worker cannot race-replace a launch file or inspect sibling launch configs.
    # The blank cwd/environment remain defense in depth, not an OS ACL boundary.
    cwd = Path(tempfile.mkdtemp(prefix="aerp7-public-worker-cwd-"))
    process = subprocess.Popen(list(command), cwd=cwd, env=env, stdin=subprocess.PIPE)
    with _SupervisorTreeObserver(process.pid) as monitor:
        try:
            process.communicate(_bytes(config), timeout=timeout_seconds)
            code = process.returncode
        except subprocess.TimeoutExpired:
            try:
                import psutil
                root = psutil.Process(process.pid)
                for child in root.children(recursive=True): child.kill()
            except BaseException:
                pass
            process.kill(); process.communicate(); raise TimeoutError("executor_worker_timeout")
        finally:
            shutil.rmtree(cwd, ignore_errors=True)
    if code != 0:
        raise subprocess.CalledProcessError(code, list(command))
    observation = monitor.receipt()
    if not output.is_file() or observation["observed_process_tree_peak_rss_bytes"] <= 0:
        raise RuntimeError("executor_worker_missing_output")
    packet = _load(output)
    if packet.get("process_id") != process.pid or packet.get("packet_sha256") != _digest({key: item for key, item in packet.items() if key != "packet_sha256"}):
        raise CustodyError("executor_worker_packet_identity_invalid")
    return {"pid": process.pid, "exit_code": code, "command_sha256": _digest(list(command)), "environment_keys_sha256": _digest(sorted(env)), "cwd_sha256": _digest(str(cwd)), **observation, "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "packet_sha256": packet["packet_sha256"]}


def _build_staged_generation(*, config: Mapping[str, Any], protocol: Mapping[str, Any],
                             authorization: Mapping[str, Any], staging: Path) -> dict[str, Any]:
    """Build and validate a complete public packet below an unpublished sibling."""
    synthetic = config["synthetic_test_mode"]
    protocol_path = Path(str(config["protocol_path"]))
    bundle = Path(str(config["candidate_bundle"]))
    (staging / "staging").mkdir()
    public_bundle = staging / "public-candidate"; public_bundle.mkdir()
    # Snapshot only the already committed, label-free capability.  The original
    # input directory is not supplied to any child and need not share a parent.
    for name in ("projection.json", "READY.json"):
        source = bundle / name
        if not source.is_file() or source.is_symlink(): raise CustodyError("executor_candidate_capability_invalid")
        _write_bytes_new(public_bundle / name, source.read_bytes())
    # The only persistent child configs are purpose-minimal label-free files.
    worker = formal.canonical_candidate_worker_config(protocol)
    worker_path = staging / "current-worker-config.json"; _write_new(worker_path, worker)
    executable = str(config["python_executable"])
    if Path(executable).resolve() != Path(sys.executable).resolve(): raise CustodyError("executor_python_not_pinned")
    supervisors = {}; current_packets = []
    for role in ("raw", "p5_primary", "p5_repeat", "six"):
        current_output = staging / f"current-{role}.json"
        current_config = {
            "schema": SCHEMA if synthetic else FORMAL_SCHEMA,
            "synthetic_test_mode": bool(synthetic), "protocol_path": str(protocol_path.resolve()),
            "candidate_bundle": str(public_bundle.resolve()), "worker_config": str(worker_path.resolve()),
            "output_path": str(current_output.resolve()), "staging_parent": str(staging.resolve()),
            "execution_role": role,
        }
        if not synthetic:
            current_config["model_dir"] = str(Path(str(config["model_dir"])).resolve())
        supervisors[f"current-{role}"] = _run_subprocess(
            [executable, "-m", "benchmarks.aerp7_convomem_executor", "--current-worker-stdin"],
            config=current_config, output=current_output,
        )
        current_packet = _load(current_output)
        expected_current_packet_schema = CURRENT_PACKET_SCHEMA if synthetic else FORMAL_CURRENT_PACKET_SCHEMA
        if current_packet.get("schema") != expected_current_packet_schema:
            raise CustodyError("executor_current_packet_schema_invalid")
        current_packets.append(current_packet)
    original_packets = []; original_jobs: list[tuple[Path, Path]] = []
    for number in range(5):
        path = staging / f"original-{number}.json"
        if synthetic:
            child = {"schema": SCHEMA, "synthetic_test_mode": True, "protocol_path": str(protocol_path.resolve()), "candidate_bundle": str(public_bundle.resolve()), "output_path": str(path.resolve()), "build_id": f"synthetic-build-{number}"}
        else:
            draft_path = staging / f"original-{number}-draft.json"
            palace_path = staging / f"original-{number}-palace"
            child = {
                "schema": FORMAL_SCHEMA, "synthetic_test_mode": False,
                "protocol_path": str(protocol_path.resolve()), "candidate_bundle": str(public_bundle.resolve()),
                "output_path": str(path.resolve()), "draft_path": str(draft_path.resolve()),
                "build_id": f"formal-build-{number}", "original_root": str(Path(str(config["original_root"])).resolve()),
                "model_dir": str(Path(str(config["model_dir"])).resolve()), "palace_path": str(palace_path.resolve()),
            }
            original_jobs.append((draft_path, palace_path))
        supervisors[f"original-{number}"] = _run_subprocess(
            [executable, "-m", "benchmarks.aerp7_convomem_executor", "--original-worker-stdin"],
            config=child, output=path,
        )
        original_packet = _load(path)
        expected_original_packet_schema = ORIGINAL_PACKET_SCHEMA if synthetic else FORMAL_ORIGINAL_PACKET_SCHEMA
        if original_packet.get("schema") != expected_original_packet_schema:
            raise CustodyError("executor_original_packet_schema_invalid")
        original_packets.append(original_packet)
    projection = formal.load_candidate_worker_projection(worker_config=worker, protocol=protocol, candidate_bundle_root=public_bundle, staging_parent=staging)
    if synthetic:
        original_replicates = [row["replicate"] for row in original_packets]
    else:
        if len(original_jobs) != 5:
            raise CustodyError("executor_original_worker_coverage_invalid")
        reaudited = [
            coordinator_reaudit_original_worker_packet(
                packet=packet, draft_path=draft_path, palace_path=palace_path, projection=projection,
            )
            for packet, (draft_path, palace_path) in zip(original_packets, original_jobs, strict=True)
        ]
        original_replicates = [row[0] for row in reaudited]
        rebound_packets = []
        for number, (packet, (_replicate, resource)) in enumerate(zip(original_packets, reaudited, strict=True)):
            rebound = {
                **packet,
                "resource_receipt": finalize_original_resource(
                    resource=resource,
                    supervisor=supervisors[f"original-{number}"],
                    worker_pid=packet["process_id"],
                ),
            }
            rebound["packet_sha256"] = _digest({key: item for key, item in rebound.items() if key != "packet_sha256"})
            rebound_packets.append(rebound)
        original_packets = rebound_packets
    sealed = {"replicates": original_replicates, "lifecycle": list(formal.ORIGINAL_LIFECYCLE), "original_code_before": protocol["original_code_receipt"], "original_code_after": protocol["original_code_receipt"]}
    sealed["worker_sha256"] = formal._digest(sealed)
    checked_original = formal.validate_original_worker_receipt(sealed, projection=projection, protocol=protocol)
    current_by_role = {row["execution_role"]: row for row in current_packets}
    if set(current_by_role) != {"raw", "p5_primary", "p5_repeat", "six"}: raise CustodyError("executor_current_role_coverage_invalid")
    if _bytes(current_by_role["p5_primary"]["artifact"]) != _bytes(current_by_role["p5_repeat"]["artifact"]): raise CustodyError("executor_current_p5_nondeterministic")
    current_artifacts = [current_by_role["raw"]["artifact"], current_by_role["p5_primary"]["artifact"], current_by_role["six"]["artifact"]]
    execution_receipts = []
    for role, supervisor_name in (("raw", "current-raw"), ("p5_primary", "current-p5_primary"), ("p5_repeat", "current-p5_repeat"), ("six", "current-six")):
        receipt = dict(current_by_role[role]["execution_receipt"])
        receipt["supervisor_sha256"] = _digest(supervisors[supervisor_name])
        receipt["execution_sha256"] = _digest({key: item for key, item in receipt.items() if key != "execution_sha256"})
        execution_receipts.append(receipt)
    current_receipt = {"schema": formal.CURRENT_WORKER_SCHEMA, "lifecycle": list(formal.CURRENT_LIFECYCLE), "projection_sha256": canonical_sha256(projection), "artifact_sha256": {row["arm_id"]: row["artifact_sha256"] for row in current_artifacts}, "static_p5_primary_sha256": current_by_role["p5_primary"]["artifact"]["artifact_sha256"], "static_p5_repeat_sha256": current_by_role["p5_repeat"]["artifact"]["artifact_sha256"], "static_p5_byte_identical": True, "static_p5_execution_count": 2, "execution_receipts": execution_receipts}
    current_receipt["worker_sha256"] = formal._digest(current_receipt)
    artifacts = [checked_original["artifact"], *current_artifacts]
    endpoint = formal.freeze_endpoint_manifest(projection=projection, protocol=protocol, ranking_artifacts=artifacts)
    # Rebind each original receipt to the actual aggregate artifact only after the
    # five isolated subprocess packets are accepted.
    original_resources = []
    for packet in original_packets:
        receipt = dict(packet["resource_receipt"]); receipt["artifact_sha256"] = checked_original["artifact"]["artifact_sha256"]; receipt["resource_sha256"] = formal.resource_digest(receipt); original_resources.append(receipt)
    resources = [*original_resources, *(row["resource_receipt"] for row in current_packets)]
    expected_queries = formal.projection_query_keys(projection)
    for receipt in resources:
        formal.validate_resource_receipt(receipt, arm_id=receipt["arm_id"], thresholds=protocol["resource_thresholds"], expected_denominators=formal.projection_denominators(projection), expected_query_keys=expected_queries)
    expected_resource_modes = {"synthetic_rehearsal"} if synthetic else {"live_native_adapter", "live_original_public_product"}
    if any(receipt["measurement_mode"] not in expected_resource_modes for receipt in resources):
        raise CustodyError("executor_resource_mode_invalid")
    formal.validate_current_execution_receipts(
        execution_receipts, current_worker_receipt=current_receipt, protocol=protocol, projection=projection,
        resources=resources, ranking_artifacts=current_artifacts, supervisors=supervisors, allow_synthetic=synthetic,
    )
    packet = {
        "schema": FREEZE_PACKET_SCHEMA if synthetic else FORMAL_FREEZE_PACKET_SCHEMA,
        "synthetic_test_mode": synthetic,
        "formal_eligible": not synthetic, "authorization_sha256": authorization["authorization_sha256"],
        "protocol": protocol, "projection_sha256": canonical_sha256(projection),
        "current_worker_receipt": current_receipt, "ranking_artifacts": artifacts,
        "endpoint_manifest": endpoint, "resource_receipts": resources, "supervisors": supervisors,
        "packet_sha256": "",
    }
    packet["packet_sha256"] = _digest({key: item for key, item in packet.items() if key != "packet_sha256"})
    _write_new(staging / "public-freeze.json", packet)
    if _load(staging / "public-freeze.json") != packet:
        raise CustodyError("executor_staged_freeze_validation_invalid")
    return packet


def public_coordinator(config: Mapping[str, Any]) -> dict[str, Any]:
    synthetic = config.get("synthetic_test_mode")
    expected_keys = PUBLIC_CONFIG_KEYS if synthetic is True else FORMAL_PUBLIC_CONFIG_KEYS
    expected_schema = SCHEMA if synthetic is True else FORMAL_SCHEMA
    if set(config) != expected_keys or config.get("schema") != expected_schema:
        raise CustodyError("executor_coordinator_config_invalid")
    if synthetic is True:
        _assert_synthetic(config)
    elif synthetic is not False:
        raise CustodyError("executor_formal_execution_blocked")
    protocol_path = Path(str(config["protocol_path"]))
    protocol = formal.validate_formal_protocol(_load(protocol_path))
    output = Path(str(config["output_dir"]))
    authorization = _authorization(
        _load(Path(str(config["authorization_path"]))), protocol=protocol,
        output_dir=output, capability=_operator_secret(),
    )
    if synthetic is False:
        _clean_protocol_code_observation(expected=protocol["current_code_receipt"])
    _consume_authorization(authorization=authorization, output_dir=output)
    staging = _new_staging_generation(output=output, authorization=authorization)
    staging_identity = _generation_identity(staging)
    try:
        packet = _build_staged_generation(
            config=config, protocol=protocol, authorization=authorization, staging=staging,
        )
        _publish_generation(staging=staging, staging_identity=staging_identity, output=output)
    except BaseException:
        # Before publication, this is only our private sibling directory.  A
        # failed run leaves no final output and no incomplete generation.
        if staging.exists():
            _discard_owned_generation(staging, staging_identity)
        raise
    published = _load(output / "public-freeze.json")
    if published != packet:
        raise CustodyError("executor_published_freeze_identity_invalid")
    return packet


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    role = parser.add_mutually_exclusive_group(required=True)
    role.add_argument("--current-worker")
    role.add_argument("--original-worker")
    role.add_argument("--current-worker-stdin", action="store_true")
    role.add_argument("--original-worker-stdin", action="store_true")
    role.add_argument("--coordinator")
    args = parser.parse_args(argv)
    if args.current_worker: current_worker(_load(Path(args.current_worker)))
    elif args.original_worker: original_worker(_load(Path(args.original_worker)))
    elif args.current_worker_stdin: current_worker(_load_stdin())
    elif args.original_worker_stdin: original_worker(_load_stdin())
    else: public_coordinator(_load(Path(args.coordinator)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
