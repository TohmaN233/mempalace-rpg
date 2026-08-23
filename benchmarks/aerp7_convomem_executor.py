"""Isolated, fail-closed executor for the AERP-7 formal protocol.

The module has two deliberately separate concerns.  Its public coordinator and
ranking workers only receive the candidate bundle and label-free protocol.  Its
custodian entrypoint is a separate process and is the sole place where custody
capabilities could be accepted.  The formal switch is intentionally disabled:
the executable paths are exercised with synthetic fixtures only until a fresh
review authorizes the one-shot data run.

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
AUTH_SCHEMA = "aerp7-convomem-operator-authorization-v1"
CURRENT_PACKET_SCHEMA = "aerp7-convomem-current-worker-packet-v1"
ORIGINAL_PACKET_SCHEMA = "aerp7-convomem-original-worker-packet-v1"
FREEZE_PACKET_SCHEMA = "aerp7-convomem-public-freeze-packet-v1"
SYNTHETIC_EXECUTION_ONLY = True
FORMAL_EXECUTION_ENABLED = False
FORMAL_CURRENT_EXECUTION_ENABLED = False
_PUBLIC_ROLE_FLAGS = frozenset({"--current-worker-stdin", "--original-worker-stdin"})
PUBLIC_CONFIG_KEYS = frozenset({"schema", "synthetic_test_mode", "protocol_path", "candidate_bundle", "output_dir", "authorization_path", "python_executable"})
FORMAL_PUBLIC_CONFIG_KEYS = frozenset({
    *PUBLIC_CONFIG_KEYS, "original_root", "model_dir", "worker_isolation_attestation",
})
CURRENT_CONFIG_KEYS = frozenset({"schema", "synthetic_test_mode", "protocol_path", "candidate_bundle", "worker_config", "output_path", "staging_parent", "execution_role"})
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

    Windows has no portable Python directory fsync.  Because this executor's
    only enabled mode is synthetic, that platform is accepted only for test
    packets; the future formal enable gate must require a durable implementation.
    """
    meta = os.lstat(parent)
    if parent.is_symlink() or not parent.is_dir() or meta.st_nlink < 1:
        raise CustodyError("executor_publish_parent_invalid")
    if os.name == "nt":
        if not SYNTHETIC_EXECUTION_ONLY:
            raise CustodyError("executor_parent_fsync_unavailable")
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


def _resource(*, arm_id: str, artifact_sha256: str, denominators: Mapping[str, int], elapsed_ns: int, build_id: str | None = None, index_sha256: str | None = None, role: str | None = None, peak_rss_bytes: int = 1) -> dict[str, Any]:
    execution_role = role or ("fresh_build" if arm_id == "original_public_product" else "primary")
    accounting = {"primary": "primary_excludes_repeat", "repeat": "repeat_measured_separately"}[execution_role] if arm_id == "static_p5" else "not_applicable"
    count = int(denominators["query_count"])
    # The synthetic worker has one measured ranking pass.  It has no per-query
    # wall-clock API, so it records an even conservative allocation rather than
    # inventing a query trace.  Formal integration must replace this seam with
    # exact upstream per-query timing before the constant is enabled.
    each = max(1, int(elapsed_ns) // count)
    receipt = {
        "schema": formal.RESOURCE_SCHEMA,
        "arm_id": arm_id,
        "execution_role": execution_role,
        "resource_semantics": "all_six_views_computed_then_raw_fusion_weights" if arm_id == "strong_raw" else "native_public_product" if arm_id == "original_public_product" else "all_six_views_computed_then_fixed_fusion",
        "measurement_scope": "rank_only_excludes_trace_and_receipt_serialization",
        "p5_repeat_accounting": accounting,
        "ingest_seconds": 0.0,
        "index_seconds": 0.0,
        "query_latency_ns": _percentiles([each] * count),
        "passage_embedding": {"calls": 1, "texts": int(denominators["candidate_text_count"])},
        "query_embedding": {"calls": count, "texts": count},
        "storage_bytes": 1,
        "peak_rss_bytes": max(1, int(peak_rss_bytes)),
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


class _LiveOriginalObserver:
    """Actual RSS/storage observer plus the honest public-product request ledger.

    The upstream product caches its native embedding callable, so internal model
    invocations are not observable without changing that product.  Counts here
    are therefore explicitly the public upsert/search request contract; the
    original-product module independently cross-checks them against its ledger.
    """

    def __init__(self, *, palace_path: Path, monitor: RssMonitor, provider: Mapping[str, Any], denominators: Mapping[str, int], corpus_count: int) -> None:
        self._palace_path = palace_path
        self._monitor = monitor
        self._provider = dict(provider)
        self._denominators = dict(denominators)
        self._corpus_count = int(corpus_count)
        self._phases: list[str] = []

    def checkpoint(self, phase: str) -> None:
        expected = ["before_ingest", "after_ingest", "after_cold_close", "after_queries"]
        if len(self._phases) >= len(expected) or phase != expected[len(self._phases)]:
            raise RuntimeError("original product observer lifecycle invalid")
        self._phases.append(phase)

    def receipt(self) -> dict[str, Any]:
        if self._phases != ["before_ingest", "after_ingest", "after_cold_close", "after_queries"]:
            raise RuntimeError("original product observer lifecycle incomplete")
        storage = sum(path.stat().st_size for path in self._palace_path.rglob("*") if path.is_file() and not path.is_symlink())
        if self._monitor.peak_bytes <= 0 or storage <= 0:
            raise RuntimeError("original product resource observation incomplete")
        return {
            "peak_rss_bytes": int(self._monitor.peak_bytes),
            "storage_bytes": int(storage),
            "passage_embedding": {
                "calls": self._corpus_count,
                "texts": int(self._denominators["candidate_text_count"]),
                "measurement": original_product.PUBLIC_UPSERT_MEASUREMENT,
            },
            "query_embedding": {
                "calls": int(self._denominators["query_count"]),
                "texts": int(self._denominators["query_count"]),
                "measurement": original_product.PUBLIC_SEARCH_MEASUREMENT,
            },
            "provider": self._provider,
        }


def _assert_synthetic(config: Mapping[str, Any]) -> None:
    if not SYNTHETIC_EXECUTION_ONLY or config.get("synthetic_test_mode") is not True:
        raise CustodyError("executor_formal_execution_blocked")


def _assert_original_mode(config: Mapping[str, Any]) -> bool:
    synthetic = config.get("synthetic_test_mode")
    expected = ORIGINAL_CONFIG_KEYS if synthetic is True else FORMAL_ORIGINAL_CONFIG_KEYS
    if set(config) != expected or config.get("schema") != SCHEMA:
        raise CustodyError("executor_original_config_invalid")
    if synthetic is True:
        _assert_synthetic(config)
        return True
    if synthetic is not False or SYNTHETIC_EXECUTION_ONLY or not FORMAL_EXECUTION_ENABLED:
        raise CustodyError("executor_formal_execution_blocked")
    return False


def _formal_original_resource(*, draft: original_product.OriginalProductWorkerDraft, replicate: Mapping[str, Any], denominators: Mapping[str, int]) -> dict[str, Any]:
    telemetry = draft.telemetry.get("resources")
    if not isinstance(telemetry, Mapping):
        raise CustodyError("executor_original_resource_invalid")
    latencies = telemetry.get("query_latency_seconds")
    if not isinstance(latencies, list) or len(latencies) != int(denominators["query_count"]):
        raise CustodyError("executor_original_resource_invalid")
    latency_ns = [max(1, int(float(value) * 1_000_000_000)) for value in latencies]
    passage, query = telemetry.get("passage_embedding"), telemetry.get("query_embedding")
    if not isinstance(passage, Mapping) or not isinstance(query, Mapping):
        raise CustodyError("executor_original_resource_invalid")
    receipt = {
        "schema": formal.RESOURCE_SCHEMA,
        "arm_id": "original_public_product",
        "execution_role": "fresh_build",
        "resource_semantics": "native_public_product",
        "measurement_scope": "rank_only_excludes_trace_and_receipt_serialization",
        "p5_repeat_accounting": "not_applicable",
        "ingest_seconds": float(telemetry["ingest_seconds"]),
        "index_seconds": float(telemetry["index_seconds"]),
        "query_latency_ns": _percentiles(latency_ns),
        "passage_embedding": {"calls": int(passage["calls"]), "texts": int(passage["texts"])},
        "query_embedding": {"calls": int(query["calls"]), "texts": int(query["texts"])},
        "storage_bytes": int(telemetry["storage_bytes"]),
        "peak_rss_bytes": int(telemetry["peak_rss_bytes"]),
        "artifact_sha256": "0" * 64,
        "build_id": str(replicate["build_id"]),
        "index_sha256": str(replicate["index_sha256"]),
        "input_denominators": dict(denominators),
        "hardware_runtime": _runtime(),
    }
    receipt["resource_sha256"] = formal.resource_digest(receipt)
    return receipt


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
    # Rehearsals bind the exact dirty-tree digest too.  Formal authorization is
    # separately impossible while FORMAL_EXECUTION_ENABLED is false and, when
    # enabled later, must additionally require a clean tree.
    return {
        "head": state["git_head"], "tree": state["git_tree"],
        "diff_digest": state["worktree_diff_sha256"], "git_dirty": state["git_dirty"],
        "state_policy": "synthetic_exact_worktree_state_bound",
    }


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
    # Synthetic rehearsal and the future one-shot formal authorization are
    # distinct capabilities.  This code accepts only rehearsal while formal is
    # disabled, so a valid synthetic HMAC can never authorize real data.
    if row["schema"] != AUTH_SCHEMA or row["mode"] != "synthetic_rehearsal" or row["synthetic_test_mode"] is not True or row["protocol_sha256"] != protocol["protocol_sha256"] or row["output_absent"] is not True or row["output_dir"] != str(output_dir.resolve()):
        raise CustodyError("executor_authorization_invalid")
    if not isinstance(row["nonce"], str) or len(row["nonce"]) < 32 or not isinstance(row["expires_at_unix"], int) or row["expires_at_unix"] <= int(time.time()):
        raise CustodyError("executor_authorization_expired")
    receipt = row["executor_code_receipt"]
    if receipt != live_executor_code_receipt():
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
    if set(config) != CURRENT_CONFIG_KEYS or config.get("schema") != SCHEMA:
        raise CustodyError("executor_current_config_invalid")
    _assert_synthetic(config)
    protocol = formal.validate_formal_protocol(_load(Path(str(config["protocol_path"]))))
    worker_config = _load(Path(str(config["worker_config"])))
    bundle = Path(str(config["candidate_bundle"])); staging_parent = Path(str(config["staging_parent"])); output = Path(str(config["output_path"]))
    role = str(config["execution_role"])
    role_to_arm = {"raw": "strong_raw", "p5_primary": "static_p5", "p5_repeat": "static_p5", "six": "six_view_secondary"}
    if role not in role_to_arm: raise CustodyError("executor_current_role_invalid")
    started = time.perf_counter_ns(); encoder = _CountingSyntheticEncoder()
    with RssMonitor(os.getpid()) as monitor:
        projection = _candidate_projection(protocol, bundle, worker_config, staging_parent)
        artifact = rank.rank_projection(projection=projection, encoder=encoder, arm_id=role_to_arm[role], model_receipt=protocol["model_receipt"], code_receipt=protocol["current_code_receipt"])
    elapsed = max(1, time.perf_counter_ns() - started)
    denominators = formal.projection_denominators(projection)
    resource = _resource(arm_id=role_to_arm[role], artifact_sha256=artifact["artifact_sha256"], denominators=denominators, elapsed_ns=elapsed, role={"p5_primary": "primary", "p5_repeat": "repeat"}.get(role), peak_rss_bytes=monitor.peak_bytes)
    resource["passage_embedding"] = {"calls": encoder.passage_calls, "texts": encoder.passage_texts}; resource["query_embedding"] = {"calls": encoder.query_calls, "texts": encoder.query_texts}; resource["resource_sha256"] = formal.resource_digest(resource)
    packet = {"schema": CURRENT_PACKET_SCHEMA, "execution_role": role, "artifact": artifact, "resource_receipt": resource, "process_id": os.getpid(), "packet_sha256": ""}
    packet["packet_sha256"] = _digest({key: item for key, item in packet.items() if key != "packet_sha256"})
    _write_new(output, packet); return packet


def _synthetic_original_replicate(projection: Mapping[str, Any], protocol: Mapping[str, Any], build_id: str) -> dict[str, Any]:
    """Synthetic-only shape oracle for subprocess isolation tests, not product output."""
    current = rank.rank_projection(projection=projection, encoder=_SyntheticEncoder(), arm_id="strong_raw", model_receipt=protocol["model_receipt"], code_receipt=protocol["current_code_receipt"])
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
        resource = _resource(arm_id="original_public_product", artifact_sha256="0" * 64, denominators=denominators, elapsed_ns=elapsed, build_id=replicate["build_id"], index_sha256=replicate["index_sha256"], peak_rss_bytes=monitor.peak_bytes)
        packet = {"schema": ORIGINAL_PACKET_SCHEMA, "replicate": replicate, "resource_receipt": resource, "process_id": os.getpid(), "packet_sha256": ""}
    else:
        draft_path = Path(str(config["draft_path"])); palace_path = Path(str(config["palace_path"]))
        if output.parent.resolve() != draft_path.parent.resolve() or output.parent.resolve() != palace_path.parent.resolve() or any(path.exists() or path.is_symlink() for path in (output, draft_path, palace_path)):
            raise CustodyError("executor_original_output_capability_invalid")
        original_root, model_dir = Path(str(config["original_root"])), Path(str(config["model_dir"]))
        if not original_root.is_dir() or original_root.is_symlink() or not model_dir.is_dir() or model_dir.is_symlink():
            raise CustodyError("executor_original_live_input_invalid")
        with RssMonitor(os.getpid()) as monitor, original_product.pinned_live_original_product(
            original_root=original_root, model_dir=model_dir, palace_path=palace_path,
        ) as (seams, live_receipt):
            observer = _LiveOriginalObserver(
                palace_path=palace_path, monitor=monitor, provider=seams.encoder.runtime_identity,
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
        resource = _formal_original_resource(draft=draft, replicate=replicate, denominators=denominators)
        packet = {
            "schema": ORIGINAL_PACKET_SCHEMA, "execution_mode": "exact_public_product_worker_draft",
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
    if set(packet) != required or packet.get("schema") != ORIGINAL_PACKET_SCHEMA or packet.get("execution_mode") != "exact_public_product_worker_draft":
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
    with RssMonitor(process.pid) as monitor:
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
    if not output.is_file() or monitor.peak_bytes <= 0:
        raise RuntimeError("executor_worker_missing_output")
    packet = _load(output)
    if packet.get("process_id") != process.pid or packet.get("packet_sha256") != _digest({key: item for key, item in packet.items() if key != "packet_sha256"}):
        raise CustodyError("executor_worker_packet_identity_invalid")
    return {"pid": process.pid, "exit_code": code, "command_sha256": _digest(list(command)), "environment_keys_sha256": _digest(sorted(env)), "cwd_sha256": _digest(str(cwd)), "observed_process_tree_peak_rss_bytes": monitor.peak_bytes, "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "packet_sha256": packet["packet_sha256"]}


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
    worker = {"role": "candidate_ranker", "projection_sha256": protocol["candidate"]["projection_canonical_sha256"], "projection_raw_sha256": protocol["candidate"]["projection_raw_sha256"], "projection_path": "projection.json", "model_receipt": protocol["model_receipt"], "code_receipt": protocol["current_code_receipt"], "staging_root": "staging", "arms": ["strong_raw", "static_p5", "six_view_secondary"], "top_k": 10, "tie_break": "stable_ranking_key_ascending", "serializer_contract": protocol["serializer_contract"]}
    worker_path = staging / "current-worker-config.json"; _write_new(worker_path, worker)
    executable = str(config["python_executable"])
    if Path(executable).resolve() != Path(sys.executable).resolve(): raise CustodyError("executor_python_not_pinned")
    supervisors = {}; current_packets = []
    for role in ("raw", "p5_primary", "p5_repeat", "six"):
        current_output = staging / f"current-{role}.json"
        current_config = {"schema": SCHEMA, "synthetic_test_mode": bool(synthetic), "protocol_path": str(protocol_path.resolve()), "candidate_bundle": str(public_bundle.resolve()), "worker_config": str(worker_path.resolve()), "output_path": str(current_output.resolve()), "staging_parent": str(staging.resolve()), "execution_role": role}
        supervisors[f"current-{role}"] = _run_subprocess(
            [executable, "-m", "benchmarks.aerp7_convomem_executor", "--current-worker-stdin"],
            config=current_config, output=current_output,
        )
        current_packets.append(_load(current_output))
    original_packets = []; original_jobs: list[tuple[Path, Path]] = []
    for number in range(5):
        path = staging / f"original-{number}.json"
        if synthetic:
            child = {"schema": SCHEMA, "synthetic_test_mode": True, "protocol_path": str(protocol_path.resolve()), "candidate_bundle": str(public_bundle.resolve()), "output_path": str(path.resolve()), "build_id": f"synthetic-build-{number}"}
        else:
            draft_path = staging / f"original-{number}-draft.json"
            palace_path = staging / f"original-{number}-palace"
            child = {
                "schema": SCHEMA, "synthetic_test_mode": False,
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
        original_packets.append(_load(path))
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
        original_packets = [{**packet, "resource_receipt": resource} for packet, (_replicate, resource) in zip(original_packets, reaudited, strict=True)]
    sealed = {"replicates": original_replicates, "lifecycle": list(formal.ORIGINAL_LIFECYCLE), "original_code_before": protocol["original_code_receipt"], "original_code_after": protocol["original_code_receipt"]}
    sealed["worker_sha256"] = formal._digest(sealed)
    checked_original = formal.validate_original_worker_receipt(sealed, projection=projection, protocol=protocol)
    current_by_role = {row["execution_role"]: row for row in current_packets}
    if set(current_by_role) != {"raw", "p5_primary", "p5_repeat", "six"}: raise CustodyError("executor_current_role_coverage_invalid")
    if _bytes(current_by_role["p5_primary"]["artifact"]) != _bytes(current_by_role["p5_repeat"]["artifact"]): raise CustodyError("executor_current_p5_nondeterministic")
    current_artifacts = [current_by_role["raw"]["artifact"], current_by_role["p5_primary"]["artifact"], current_by_role["six"]["artifact"]]
    current_receipt = {"schema": formal.CURRENT_WORKER_SCHEMA, "lifecycle": list(formal.CURRENT_LIFECYCLE), "projection_sha256": canonical_sha256(projection), "artifact_sha256": {row["arm_id"]: row["artifact_sha256"] for row in current_artifacts}, "static_p5_primary_sha256": current_by_role["p5_primary"]["artifact"]["artifact_sha256"], "static_p5_repeat_sha256": current_by_role["p5_repeat"]["artifact"]["artifact_sha256"], "static_p5_byte_identical": True, "static_p5_execution_count": 2}
    current_receipt["worker_sha256"] = formal._digest(current_receipt)
    artifacts = [checked_original["artifact"], *current_artifacts]
    endpoint = formal.freeze_endpoint_manifest(projection=projection, protocol=protocol, ranking_artifacts=artifacts)
    # Rebind each original receipt to the actual aggregate artifact only after the
    # five isolated subprocess packets are accepted.
    original_resources = []
    for packet in original_packets:
        receipt = dict(packet["resource_receipt"]); receipt["artifact_sha256"] = checked_original["artifact"]["artifact_sha256"]; receipt["resource_sha256"] = formal.resource_digest(receipt); original_resources.append(receipt)
    resources = [*original_resources, *(row["resource_receipt"] for row in current_packets)]
    for receipt in resources:
        formal.validate_resource_receipt(receipt, arm_id=receipt["arm_id"], thresholds=protocol["resource_thresholds"], expected_denominators=formal.projection_denominators(projection))
    packet = {
        "schema": FREEZE_PACKET_SCHEMA, "synthetic_test_mode": True,
        "formal_eligible": False, "authorization_sha256": authorization["authorization_sha256"],
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
    if set(config) != expected_keys or config.get("schema") != SCHEMA:
        raise CustodyError("executor_coordinator_config_invalid")
    if synthetic is True:
        _assert_synthetic(config)
    elif synthetic is not False or SYNTHETIC_EXECUTION_ONLY or not FORMAL_EXECUTION_ENABLED or not FORMAL_CURRENT_EXECUTION_ENABLED:
        raise CustodyError("executor_formal_execution_blocked")
    protocol_path = Path(str(config["protocol_path"]))
    protocol = formal.validate_formal_protocol(_load(protocol_path))
    output = Path(str(config["output_dir"]))
    authorization = _authorization(
        _load(Path(str(config["authorization_path"]))), protocol=protocol,
        output_dir=output, capability=_operator_secret(),
    )
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
