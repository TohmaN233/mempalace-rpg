"""Label-blind, independently auditable AERP-7 ranking freezes.

Artifacts bind a frozen top-10 to its exact candidate-side inputs. They do not
pretend that a digest can re-execute an encoder.
"""
from __future__ import annotations

import hashlib
import os
import json
import math
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.aerp_mempalace_v380_runtime_contract import original_hnsw_configuration
from benchmarks.aerp7_convomem_confirmation import CustodyError, _candidate_ready, canonical_sha256, validate_candidate_projection
from benchmarks.aerp7_convomem_confirmation import CANDIDATE_PROJECTION_REFERENCE_SCHEMA
from mempalace_rpg.retrieval import (
    AuthorizedRetrievalCandidate, FixedP5Policy, FusionRoutingDecision,
    P5_EXPERT_WEIGHTS, RAW_EXPERT_WEIGHTS, SIX_VIEW_WEIGHTS, SixViewRanker,
    structured_observation,
)

RANKING_SCHEMA = "aerp7-convomem-frozen-ranking-v3"
RANKING_ARTIFACT_REFERENCE_SCHEMA = "aerp7-convomem-ranking-artifact-reference-v1"
RANKING_ARTIFACT_READY_SCHEMA = "aerp7-convomem-ranking-artifact-ready-v1"
INPUT_RECEIPT_REFERENCE_SCHEMA = "aerp7-convomem-input-receipt-reference-v1"
MEASUREMENT_REFERENCE_SCHEMA = "aerp7-convomem-measurement-reference-v1"
MEASUREMENT_SEQUENCE_SCHEMA = "aerp7-convomem-measurement-sequence-v1"
MEASUREMENT_READY_SCHEMA = "aerp7-convomem-measurement-ready-v1"
CURRENT_ARMS = frozenset({"strong_raw", "static_p5", "six_view_secondary"})
CONFIDENCE_CONTRACT = "normalized_top_margin_v1"
PROTOCOL_SOURCE = {
    "repository": "SalesforceAIResearch/ConvoMem",
    "commit": "624f582ecf0d336ae1d4539d19186089800774b1",
    "tree": "1699a58948e7ac4e3263110a40d06bab457bcf8b",
    "files": {
        "LongContextMemoryAnswerer.scala": "857c90034aeaf167422c092515332293bc5548c9e90ecd03f57b7973ee9a0155",
        "MultithreadedEvaluator.scala": "e83194fb5eadf9c65c1a8d120445f6bee692fb1e45dd573113ecbeb90c039594",
        "AnsweringEvaluation.scala": "da951b2b96fd29ef87467c13b593764f9924f26a3bb49fbd97e0f830e1ed6b08",
        "BatchedTestCasesGenerator.scala": "43466f4c9d3d087e9ff8ef22f9f4c189468d6c31d4a1f6e819fde5233f23c6be",
    },
}
CURRENT_SERIALIZER = {
    "name": "aerp7-current-structured-observation-v1",
    "observation": "structured_observation(summary=text,event_type=conversation_message,actor_id=speaker,target_id=None,related_entities=None,related_quests=None,related_locations=None,in_world_time=None,location_id=None)",
}
ORIGINAL_MEMPALACE_SERIALIZER = {
    "name": "mempalace-public-product-text-only-v1",
    "document": "text",
    "metadata": "speaker metadata only; not encoder-visible",
}
ORIGINAL_METHOD = {
    "arm_id": "original_public_product", "runner": "mempalace_public_product",
    "candidate_strategy": "vector", "top_k": 10,
    "replicate_aggregation": "per_query_arithmetic_mean", "replicate_seed_rule": "manifest_fixed",
}
ORIGINAL_CALL_CONTRACT = {
    "candidate_strategy": "vector", "top_k": 10, "room_scope": "corpus_id",
    "collection_name": "mempalace_drawers", "cold_reopen": True,
}
ORIGINAL_HNSW_CONFIG = original_hnsw_configuration()
ORIGINAL_OPERATIONAL_DELTA = {"schema": "aerp5-chroma-operational-delta-v1", "excluded_table": "acquire_write", "permitted_transition": "unchanged_or_append_next_integer_id_lock_status_1", "validation": "passed"}
ORIGINAL_GRAPH_NAMES = ("data_level0.bin", "header.bin", "length.bin", "link_lists.bin")
DIRECT_READ_NORMALIZATION_SCHEMA = "aerp7-hnsw-direct-read-normalization-v1"
PAIRED_DIRECT_READ_NORMALIZATION_SCHEMA = "aerp7-hnsw-direct-read-normalization-v2"


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _canonical_graph_path(path: Any, name: str) -> bool:
    """Accept the root-level and segment-relative forms emitted by snapshots."""
    return isinstance(path, str) and (path == name or path.endswith("/" + name))


def _logical_original_physical_receipt(value: Any) -> dict[str, Any]:
    """Drop only a validated direct-read normalization observation from scientific state."""
    receipt = _object(value, "original_physical_receipt_invalid")
    delta = _object(receipt.get("direct_read_normalization_delta"), "original_physical_receipt_invalid")
    schema = delta.get("schema")
    paired = schema == PAIRED_DIRECT_READ_NORMALIZATION_SCHEMA
    if schema == DIRECT_READ_NORMALIZATION_SCHEMA:
        if set(delta) != {"schema", "status", "path", "bytes", "before_sha256", "after_sha256"}:
            raise CustodyError("original_physical_receipt_invalid")
    elif paired:
        if set(delta) != {"schema", "status", "transitions"} or delta.get("status") != "data_level0_and_length_same_size_rewrite":
            raise CustodyError("original_physical_receipt_invalid")
    else:
        raise CustodyError("original_physical_receipt_invalid")
    if schema == DIRECT_READ_NORMALIZATION_SCHEMA and delta.get("status") == "none":
        if any(delta.get(key) is not None for key in ("path", "bytes", "before_sha256", "after_sha256")):
            raise CustodyError("original_physical_receipt_invalid")
    elif schema == DIRECT_READ_NORMALIZATION_SCHEMA and delta.get("status") == "length_bin_same_size_rewrite":
        path = delta.get("path")
        if (
            not _canonical_graph_path(path, "length.bin")
            or _int(delta.get("bytes"), "original_physical_receipt_invalid", positive=True) <= 0
            or _hex(delta.get("before_sha256"), "original_physical_receipt_invalid")
            == _hex(delta.get("after_sha256"), "original_physical_receipt_invalid")
        ):
            raise CustodyError("original_physical_receipt_invalid")
        graph_files = receipt.get("graph_files")
        if not isinstance(graph_files, list):
            raise CustodyError("original_physical_receipt_invalid")
        length_entries = [
            entry for entry in graph_files
            if isinstance(entry, Mapping) and entry.get("name") == "length.bin"
        ]
        if len(length_entries) != 1:
            raise CustodyError("original_physical_receipt_invalid")
        length_entry = length_entries[0]
        if (
            set(length_entry) != {"name", "path", "bytes", "sha256"}
            or length_entry.get("path") != path
            or length_entry.get("bytes") != delta.get("bytes")
            or length_entry.get("sha256") != delta.get("after_sha256")
        ):
            raise CustodyError("original_physical_receipt_invalid")
    elif paired:
        graph_files = receipt.get("graph_files")
        if not isinstance(graph_files, list):
            raise CustodyError("original_physical_receipt_invalid")
        transitions = delta.get("transitions")
        if not isinstance(transitions, list) or len(transitions) != 2:
            raise CustodyError("original_physical_receipt_invalid")
        expected_names = ("data_level0.bin", "length.bin")
        parent: str | None = None
        for expected_name, transition in zip(expected_names, transitions):
            if not isinstance(transition, Mapping) or set(transition) != {"path", "bytes", "before_sha256", "after_sha256"}:
                raise CustodyError("original_physical_receipt_invalid")
            path = transition.get("path")
            if not _canonical_graph_path(path, expected_name):
                raise CustodyError("original_physical_receipt_invalid")
            path_parent, separator, basename = path.rpartition("/")
            if basename != expected_name or (parent is not None and path_parent != parent):
                raise CustodyError("original_physical_receipt_invalid")
            parent = path_parent
            if (
                _int(transition.get("bytes"), "original_physical_receipt_invalid", positive=True) <= 0
                or _hex(transition.get("before_sha256"), "original_physical_receipt_invalid")
                == _hex(transition.get("after_sha256"), "original_physical_receipt_invalid")
            ):
                raise CustodyError("original_physical_receipt_invalid")
            entries = [
                entry for entry in graph_files
                if isinstance(entry, Mapping) and entry.get("name") == expected_name
            ]
            if len(entries) != 1:
                raise CustodyError("original_physical_receipt_invalid")
            entry = entries[0]
            if (
                set(entry) != {"name", "path", "bytes", "sha256"}
                or entry.get("path") != path
                or entry.get("bytes") != transition.get("bytes")
                or entry.get("sha256") != transition.get("after_sha256")
            ):
                raise CustodyError("original_physical_receipt_invalid")
    else:
        raise CustodyError("original_physical_receipt_invalid")
    graph_files = receipt.get("graph_files")
    if not isinstance(graph_files, list):
        raise CustodyError("original_physical_receipt_invalid")
    length_entries = [
        entry for entry in graph_files
        if isinstance(entry, Mapping) and entry.get("name") == "length.bin"
    ]
    if len(length_entries) != 1:
        raise CustodyError("original_physical_receipt_invalid")
    length_entry = length_entries[0]
    if (
        set(length_entry) != {"name", "path", "bytes", "sha256"}
        or not isinstance(length_entry.get("path"), str)
        or not _canonical_graph_path(length_entry["path"], "length.bin")
        or _int(length_entry.get("bytes"), "original_physical_receipt_invalid", positive=True) <= 0
    ):
        raise CustodyError("original_physical_receipt_invalid")
    _hex(length_entry["sha256"], "original_physical_receipt_invalid")
    data_entries = [
        entry for entry in graph_files
        if isinstance(entry, Mapping) and entry.get("name") == "data_level0.bin"
    ]
    if len(data_entries) != 1:
        raise CustodyError("original_physical_receipt_invalid")
    data_entry = data_entries[0]
    if (
        set(data_entry) != {"name", "path", "bytes", "sha256"}
        or not isinstance(data_entry.get("path"), str)
        or not _canonical_graph_path(data_entry["path"], "data_level0.bin")
        or _int(data_entry.get("bytes"), "original_physical_receipt_invalid", positive=True) <= 0
    ):
        raise CustodyError("original_physical_receipt_invalid")
    _hex(data_entry["sha256"], "original_physical_receipt_invalid")
    _hex(receipt.get("immutable_backend_sha256"), "original_physical_receipt_invalid")
    _hex(receipt.get("immutable_non_length_backend_sha256"), "original_physical_receipt_invalid")
    residual = receipt.get("immutable_residual_backend_sha256")
    _hex(residual, "original_physical_receipt_invalid")
    scientific = {
        key: child for key, child in receipt.items()
        if key not in {
            "direct_read_normalization_delta", "immutable_backend_sha256",
            "immutable_non_length_backend_sha256", "immutable_residual_backend_sha256",
        }
    }
    # The residual aggregate excludes only the two canonical files that a v2
    # direct read may rewrite. It retains header, link lists, and all other
    # immutable backend bytes in the scientific comparison.
    scientific["immutable_residual_backend_sha256"] = residual
    scientific["graph_files"] = [
        {key: child for key, child in entry.items() if key != "sha256"}
        if isinstance(entry, Mapping)
        and entry.get("name") in {"length.bin", "data_level0.bin"}
        else entry
        for entry in graph_files
    ]
    return scientific


def _joint_original_physical_receipts(worker_raw: Mapping[str, Any], coordinator_raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate scientific equivalence and the physical direct-read handoff."""
    worker = _logical_original_physical_receipt(worker_raw)
    coordinator = _logical_original_physical_receipt(coordinator_raw)
    if worker != coordinator:
        raise CustodyError("original_physical_receipt_invalid")

    def graph(receipt: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        rows = receipt.get("graph_files")
        if not isinstance(rows, list):
            raise CustodyError("original_physical_receipt_invalid")
        result = {str(row.get("name")): row for row in rows if isinstance(row, Mapping) and row.get("name") in {"data_level0.bin", "length.bin"}}
        if set(result) != {"data_level0.bin", "length.bin"}:
            raise CustodyError("original_physical_receipt_invalid")
        for name, row in result.items():
            if set(row) != {"name", "path", "bytes", "sha256"} or not _canonical_graph_path(row.get("path"), name):
                raise CustodyError("original_physical_receipt_invalid")
            _int(row.get("bytes"), "original_physical_receipt_invalid", positive=True)
            _hex(row.get("sha256"), "original_physical_receipt_invalid")
        return result

    def transitions(receipt: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        delta = receipt.get("direct_read_normalization_delta")
        if not isinstance(delta, Mapping):
            raise CustodyError("original_physical_receipt_invalid")
        if delta.get("schema") == DIRECT_READ_NORMALIZATION_SCHEMA:
            if delta.get("status") == "none": return {}
            if delta.get("status") == "length_bin_same_size_rewrite": return {"length.bin": delta}
        if delta.get("schema") == PAIRED_DIRECT_READ_NORMALIZATION_SCHEMA and delta.get("status") == "data_level0_and_length_same_size_rewrite":
            rows = delta.get("transitions")
            if isinstance(rows, list) and len(rows) == 2:
                return {"data_level0.bin": rows[0], "length.bin": rows[1]}
        raise CustodyError("original_physical_receipt_invalid")

    worker_graph, coordinator_graph = graph(worker_raw), graph(coordinator_raw)
    worker_transitions, coordinator_transitions = transitions(worker_raw), transitions(coordinator_raw)
    if "data_level0.bin" not in worker_transitions and "data_level0.bin" not in coordinator_transitions:
        if worker_raw.get("immutable_non_length_backend_sha256") != coordinator_raw.get("immutable_non_length_backend_sha256"):
            raise CustodyError("original_physical_receipt_raw_non_length_drift")
    for name in ("data_level0.bin", "length.bin"):
        worker_final = worker_transitions[name]["after_sha256"] if name in worker_transitions else worker_graph[name]["sha256"]
        coordinator_initial = coordinator_transitions[name]["before_sha256"] if name in coordinator_transitions else coordinator_graph[name]["sha256"]
        if worker_final != coordinator_initial:
            raise CustodyError("original_physical_receipt_transition_handoff_invalid")
    return worker, coordinator


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise CustodyError(code)
    return value


def _git_object_id(value: Any, code: str) -> str:
    """Validate Git SHA-1/SHA-256 object IDs without weakening content hashes."""
    if not isinstance(value, str) or len(value) not in (40, 64) or any(char not in "0123456789abcdef" for char in value):
        raise CustodyError(code)
    return value


def _int(value: Any, code: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0):
        raise CustodyError(code)
    return value


def _object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return dict(value)


def _finite(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise CustodyError(code)
    return float(value)


def _query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


_CANDIDATE_REFERENCE_KEYS = frozenset({
    "schema", "bundle_path", "projection_path", "ready_path", "generation_id",
    "projection_raw_sha256", "projection_canonical_sha256", "dataset",
    "query_count", "candidate_text_count",
})


def validate_candidate_projection_reference(value: Any) -> dict[str, Any]:
    """Validate the small, public, candidate-only projection capability.

    A reference is deliberately not a projection.  It contains no candidate
    text and no custody path; workers must open it through
    :class:`CandidateProjectionStore`, which rechecks the two published files
    before each cursor operation.
    """
    row = _object(value, "candidate_projection_reference_invalid")
    if set(row) != _CANDIDATE_REFERENCE_KEYS or row.get("schema") != CANDIDATE_PROJECTION_REFERENCE_SCHEMA:
        raise CustodyError("candidate_projection_reference_schema_invalid")
    if not isinstance(row.get("bundle_path"), str) or not row["bundle_path"].strip() or not Path(row["bundle_path"]).is_absolute():
        raise CustodyError("candidate_projection_reference_path_invalid")
    if row.get("projection_path") != "projection.json" or row.get("ready_path") != "READY.json":
        raise CustodyError("candidate_projection_reference_path_invalid")
    if not isinstance(row.get("generation_id"), str) or not row["generation_id"].strip():
        raise CustodyError("candidate_projection_reference_generation_invalid")
    for key in ("projection_raw_sha256", "projection_canonical_sha256"):
        _hex(row.get(key), "candidate_projection_reference_digest_invalid")
    dataset = _object(row.get("dataset"), "candidate_projection_reference_dataset_invalid")
    if set(dataset) != {"canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256"}:
        raise CustodyError("candidate_projection_reference_dataset_invalid")
    for key in dataset:
        _hex(dataset[key], "candidate_projection_reference_dataset_invalid")
    _int(row.get("query_count"), "candidate_projection_reference_denominator_invalid", positive=True)
    _int(row.get("candidate_text_count"), "candidate_projection_reference_denominator_invalid", positive=True)
    return row


def _stream_file_sha256(path: Path, code: str) -> tuple[str, tuple[int, int], int]:
    """Hash a regular file without ever retaining its payload in memory."""
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CustodyError(code) from exc
    if path.is_symlink() or not path.is_file() or before.st_nlink != 1:
        raise CustodyError(code)
    digest = hashlib.sha256(); size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk); size += len(chunk)
        after = os.lstat(path)
    except OSError as exc:
        raise CustodyError(code) from exc
    if path.is_symlink() or not path.is_file() or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or before.st_size != after.st_size:
        raise CustodyError(code)
    return digest.hexdigest(), (before.st_dev, before.st_ino), size


def _stream_projection_digest(path: Path, code: str) -> str:
    """Return the canonical digest of a READY-published projection.

    Candidate publication writes the projection with the benchmark's canonical
    JSON encoder.  Re-hashing that byte stream is therefore both the raw and
    canonical digest, and crucially does not create a second multi-gigabyte
    JSON object.  The READY binding below rejects any non-canonical generation
    before this helper is used by a cursor.
    """
    digest, _identity, _size = _stream_file_sha256(path, code)
    return digest


def _ijson() -> Any:
    try:
        import ijson
    except ImportError as exc:  # pragma: no cover - dependency is pinned in pyproject
        raise CustodyError("candidate_streaming_dependency_missing") from exc
    return ijson


class CandidateProjectionCursor:
    """Bounded JSON cursors over the published candidate projection.

    Each iterator performs a fresh reference/READY/file binding check before
    opening the projection.  The cursor only yields one corpus or item at a
    time; it never calls ``json.loads`` on the projection payload.
    """

    def __init__(self, reference: Mapping[str, Any], *, expected_bundle_root: Path | None = None) -> None:
        self.reference = validate_candidate_projection_reference(reference)
        bundle = Path(self.reference["bundle_path"])
        try:
            resolved = bundle.resolve(strict=True)
        except OSError as exc:
            raise CustodyError("candidate_projection_reference_path_invalid") from exc
        if bundle.is_symlink() or not bundle.is_dir():
            raise CustodyError("candidate_projection_reference_path_invalid")
        if expected_bundle_root is not None and resolved != expected_bundle_root.resolve(strict=True):
            raise CustodyError("candidate_projection_reference_root_drift")
        projection = resolved / self.reference["projection_path"]
        ready = resolved / self.reference["ready_path"]
        if projection.parent != resolved or ready.parent != resolved:
            raise CustodyError("candidate_projection_reference_path_invalid")
        self.bundle_path = resolved
        self.projection_path = projection
        self.ready_path = ready

    def _ready(self) -> dict[str, Any]:
        raw, identity, _size = self._read_regular(self.ready_path, "candidate_projection_ready_invalid")
        try:
            ready = dict(_candidate_ready(json.loads(raw)))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CustodyError("candidate_projection_ready_invalid") from exc
        projection = ready.get("projection")
        if (
            ready.get("generation_id") != self.reference["generation_id"]
            or not isinstance(projection, Mapping)
            or projection.get("raw_sha256") != self.reference["projection_raw_sha256"]
            or projection.get("canonical_sha256") != self.reference["projection_canonical_sha256"]
        ):
            raise CustodyError("candidate_projection_ready_binding_invalid")
        try:
            after = os.lstat(self.ready_path)
        except OSError as exc:
            raise CustodyError("candidate_projection_ready_invalid") from exc
        if self.ready_path.is_symlink() or (identity[0], identity[1]) != (after.st_dev, after.st_ino):
            raise CustodyError("candidate_projection_ready_identity_drift")
        return dict(ready)

    @staticmethod
    def _read_regular(path: Path, code: str) -> tuple[bytes, tuple[int, int], int]:
        try:
            before = os.lstat(path)
        except OSError as exc:
            raise CustodyError(code) from exc
        if path.is_symlink() or not path.is_file() or before.st_nlink != 1:
            raise CustodyError(code)
        try:
            raw = path.read_bytes()
            after = os.lstat(path)
        except OSError as exc:
            raise CustodyError(code) from exc
        if path.is_symlink() or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or before.st_size != after.st_size:
            raise CustodyError(code)
        return raw, (before.st_dev, before.st_ino), before.st_size

    def reverify(self) -> dict[str, Any]:
        """Recheck READY, raw bytes, canonical bytes and dataset binding."""
        self._ready()
        raw_sha, _identity, _size = _stream_file_sha256(self.projection_path, "candidate_projection_raw_drift")
        if raw_sha != self.reference["projection_raw_sha256"]:
            raise CustodyError("candidate_projection_raw_drift")
        canonical_sha = _stream_projection_digest(self.projection_path, "candidate_projection_canonical_drift")
        if canonical_sha != self.reference["projection_canonical_sha256"]:
            raise CustodyError("candidate_projection_canonical_drift")
        ijson = _ijson()
        try:
            with self.projection_path.open("rb") as handle:
                datasets = list(ijson.items(handle, "dataset", use_float=True))
        except (OSError, ValueError) as exc:
            raise CustodyError("candidate_projection_dataset_invalid") from exc
        if len(datasets) != 1 or datasets[0] != self.reference["dataset"]:
            raise CustodyError("candidate_projection_dataset_drift")
        return self.reference

    def iter_corpora(self) -> Any:
        self.reverify()
        ijson = _ijson()
        try:
            with self.projection_path.open("rb") as handle:
                for corpus in ijson.items(handle, "corpora.item", use_float=True):
                    if not isinstance(corpus, Mapping):
                        raise CustodyError("candidate_projection_corpus_invalid")
                    yield dict(corpus)
        except (OSError, ValueError) as exc:
            if isinstance(exc, CustodyError):
                raise
            raise CustodyError("candidate_projection_corpus_invalid") from exc

    def iter_items(self) -> Any:
        self.reverify()
        ijson = _ijson()
        try:
            with self.projection_path.open("rb") as handle:
                for item in ijson.items(handle, "items.item", use_float=True):
                    if not isinstance(item, Mapping):
                        raise CustodyError("candidate_projection_item_invalid")
                    yield dict(item)
        except (OSError, ValueError) as exc:
            if isinstance(exc, CustodyError):
                raise
            raise CustodyError("candidate_projection_item_invalid") from exc


class CandidateProjectionStore:
    """Persistent, candidate-only SQLite materialization behind a reference.

    The store is a derived worker cache, never a scientific input or custody
    artifact.  Its metadata binds the exact reference; all public reads call
    ``cursor.reverify`` first, so byte/path/generation drift fails closed.
    """

    SCHEMA = "aerp7-convomem-candidate-projection-store-v1"

    def __init__(self, *, reference: Mapping[str, Any], database: Path, cursor: CandidateProjectionCursor, connection: sqlite3.Connection) -> None:
        self.reference = validate_candidate_projection_reference(reference)
        self.database = database
        self.cursor = cursor
        self.connection = connection
        self._run_active = False
        self._closed = False
        self._cleanup_receipt: dict[str, Any] | None = None

    @classmethod
    def open(cls, reference: Mapping[str, Any], staging_parent: Path, *, expected_bundle_root: Path | None = None, database_name: str = "candidate-projection.sqlite3") -> "CandidateProjectionStore":
        cursor = CandidateProjectionCursor(reference, expected_bundle_root=expected_bundle_root)
        if not staging_parent.is_absolute() or staging_parent.is_symlink() or not staging_parent.is_dir():
            raise CustodyError("candidate_store_staging_invalid")
        database = (staging_parent / database_name).resolve()
        if database.parent != staging_parent.resolve() or database.name != database_name or database.is_symlink():
            raise CustodyError("candidate_store_path_invalid")
        cursor.reverify()
        if database.exists():
            if not database.is_file():
                raise CustodyError("candidate_store_path_invalid")
            connection = sqlite3.connect(database)
            try:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != {"metadata", "corpora", "candidates", "items"}:
                    raise CustodyError("candidate_store_schema_invalid")
                metadata = dict(connection.execute("SELECT key, value_json FROM metadata"))
                if set(metadata) != {"schema", "reference"} or json.loads(metadata["schema"]) != cls.SCHEMA:
                    raise CustodyError("candidate_store_schema_invalid")
                if json.loads(metadata["reference"]) != cursor.reference:
                    raise CustodyError("candidate_store_reference_drift")
            except CustodyError:
                connection.close()
                raise
            except (sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
                connection.close()
                raise CustodyError("candidate_store_invalid") from exc
            store = cls(reference=cursor.reference, database=database, cursor=cursor, connection=connection)
            store._before_read()
            return store
        temporary = database.with_name("." + database.name + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise CustodyError("candidate_store_partial_present")
        connection = sqlite3.connect(temporary)
        try:
            cls._initialize(connection)
            cls._materialize(connection, cursor)
            connection.commit()
            connection.close()
            os.replace(temporary, database)
        except BaseException:
            try:
                connection.close()
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            raise
        connection = sqlite3.connect(database)
        store = cls(reference=cursor.reference, database=database, cursor=cursor, connection=connection)
        store._before_read()
        return store

    @staticmethod
    def _initialize(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE corpora(corpus_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
            CREATE TABLE candidates(corpus_id TEXT NOT NULL, message_id TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(corpus_id, message_id));
            CREATE TABLE items(item_id TEXT PRIMARY KEY, corpus_id TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE INDEX candidates_by_corpus ON candidates(corpus_id, message_id);
            CREATE INDEX items_by_id ON items(item_id);
        """)

    @classmethod
    def _materialize(cls, connection: sqlite3.Connection, cursor: CandidateProjectionCursor) -> None:
        connection.execute("INSERT INTO metadata VALUES (?, ?)", ("schema", _bytes(cls.SCHEMA).decode("utf-8")))
        connection.execute("INSERT INTO metadata VALUES (?, ?)", ("reference", _bytes(cursor.reference).decode("utf-8")))
        corpora_count = candidate_count = 0
        for corpus in cursor.iter_corpora():
            corpus_id = corpus.get("corpus_id")
            candidates = corpus.get("candidates")
            if not isinstance(corpus_id, str) or not corpus_id or not isinstance(candidates, list):
                raise CustodyError("candidate_store_corpus_invalid")
            if connection.execute("SELECT 1 FROM corpora WHERE corpus_id=?", (corpus_id,)).fetchone() is not None:
                raise CustodyError("candidate_store_corpus_duplicate")
            metadata = {key: value for key, value in corpus.items() if key != "candidates"}
            connection.execute("INSERT INTO corpora VALUES (?, ?)", (corpus_id, _bytes(metadata).decode("utf-8")))
            corpora_count += 1
            for candidate in candidates:
                if not isinstance(candidate, Mapping) or not isinstance(candidate.get("message_id"), str) or not candidate["message_id"]:
                    raise CustodyError("candidate_store_candidate_invalid")
                try:
                    connection.execute("INSERT INTO candidates VALUES (?, ?, ?)", (corpus_id, candidate["message_id"], _bytes(dict(candidate)).decode("utf-8")))
                except sqlite3.IntegrityError as exc:
                    raise CustodyError("candidate_store_candidate_duplicate") from exc
                candidate_count += 1
        item_count = 0
        for item in cursor.iter_items():
            if not isinstance(item.get("item_id"), str) or not item["item_id"] or not isinstance(item.get("corpus_id"), str) or not item["corpus_id"]:
                raise CustodyError("candidate_store_item_invalid")
            try:
                connection.execute("INSERT INTO items VALUES (?, ?, ?)", (item["item_id"], item["corpus_id"], _bytes(item).decode("utf-8")))
            except sqlite3.IntegrityError as exc:
                raise CustodyError("candidate_store_item_duplicate") from exc
            item_count += 1
        if corpora_count <= 0 or item_count != cursor.reference["query_count"] or candidate_count != cursor.reference["candidate_text_count"]:
            raise CustodyError("candidate_store_denominator_invalid")
        cursor.reverify()

    def _before_read(self) -> None:
        if self._closed or self.connection is None:
            raise CustodyError("candidate_store_closed")
        if not self._run_active:
            self.cursor.reverify()
        try:
            bound = self.connection.execute("SELECT value_json FROM metadata WHERE key='reference'").fetchone()
        except sqlite3.Error as exc:
            raise CustodyError("candidate_store_invalid") from exc
        if bound is None:
            raise CustodyError("candidate_store_reference_missing")
        try:
            if json.loads(bound[0]) != self.reference:
                raise CustodyError("candidate_store_reference_drift")
        except json.JSONDecodeError as exc:
            raise CustodyError("candidate_store_reference_invalid") from exc
        try:
            corpus_count = self.connection.execute("SELECT COUNT(*) FROM corpora").fetchone()[0]
            candidate_count = self.connection.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
            item_count = self.connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        except sqlite3.Error as exc:
            raise CustodyError("candidate_store_invalid") from exc
        if (
            not isinstance(corpus_count, int) or corpus_count <= 0
            or candidate_count != self.reference["candidate_text_count"]
            or item_count != self.reference["query_count"]
        ):
            raise CustodyError("candidate_store_denominator_invalid")

    def iter_items(self) -> Any:
        self._before_read()
        for (payload,) in self.connection.execute("SELECT payload_json FROM items ORDER BY item_id"):
            try:
                yield json.loads(payload)
            except json.JSONDecodeError as exc:
                raise CustodyError("candidate_store_item_invalid") from exc

    def corpus(self, corpus_id: str) -> dict[str, Any]:
        self._before_read()
        row = self.connection.execute("SELECT payload_json FROM corpora WHERE corpus_id=?", (corpus_id,)).fetchone()
        if row is None:
            raise CustodyError("candidate_store_corpus_missing")
        candidates = [json.loads(payload) for (payload,) in self.connection.execute("SELECT payload_json FROM candidates WHERE corpus_id=? ORDER BY CAST(json_extract(payload_json, '$.corpus_order') AS INTEGER), message_id", (corpus_id,))]
        try:
            corpus = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise CustodyError("candidate_store_corpus_invalid") from exc
        corpus["candidates"] = candidates
        return corpus

    def corpus_ids(self) -> list[str]:
        self._before_read()
        return [row[0] for row in self.connection.execute("SELECT corpus_id FROM corpora ORDER BY corpus_id")]

    def projection_metadata(self) -> dict[str, Any]:
        self._before_read()
        dataset = self.reference["dataset"]
        return {"schema": "aerp7-convomem-candidate-projection-v3", "dataset": dict(dataset), "reference": dict(self.reference)}

    def begin_run(self) -> None:
        """Bind one ranking run to the source generation before opening cursors."""
        if self._closed or self.connection is None:
            raise CustodyError("candidate_store_closed")
        if self._run_active:
            raise CustodyError("candidate_store_run_already_active")
        self.cursor.reverify()
        self._run_active = True

    def end_run(self) -> None:
        """Recheck the source generation after the final cursor read."""
        if self._closed or self.connection is None:
            raise CustodyError("candidate_store_closed")
        if not self._run_active:
            return
        self._run_active = False
        self.cursor.reverify()

    def cleanup_after_artifacts_validated(
        self, artifact_references: Sequence[Mapping[str, Any]], *, expected_count: int = 4,
    ) -> dict[str, Any]:
        """Remove only the derived candidate SQLite store after four refs pass.

        The coordinator calls this after every current artifact (including the
        P5 repeat) has been validated.  The candidate projection and external
        input/measurement receipt payloads are deliberately not touched.
        Calling this method again returns the original cleanup receipt.
        """
        cached = getattr(self, "_cleanup_receipt", None)
        if cached is not None:
            return dict(cached)
        if self._closed:
            raise CustodyError("candidate_store_cleanup_closed")
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0:
            raise CustodyError("candidate_store_cleanup_count_invalid")
        if not isinstance(artifact_references, Sequence) or isinstance(artifact_references, (str, bytes)) or len(artifact_references) != expected_count:
            raise CustodyError("candidate_store_cleanup_artifact_count_invalid")
        if self._run_active:
            raise CustodyError("candidate_store_cleanup_run_active")
        self._before_read()
        validated: list[dict[str, Any]] = []
        artifact_paths: set[Path] = set()
        for reference in artifact_references:
            checked = validate_ranking_artifact_reference(
                reference,
                expected_projection_sha256=self.reference["projection_canonical_sha256"],
            )
            if checked["generation_id"] != self.reference["generation_id"]:
                raise CustodyError("candidate_store_cleanup_generation_invalid")
            artifact_path = Path(checked["artifact_path"]).resolve(strict=True)
            ready_path = Path(checked["ready_path"]).resolve(strict=True)
            if artifact_path.parent != self.database.parent or ready_path.parent != self.database.parent:
                raise CustodyError("candidate_store_cleanup_artifact_parent_invalid")
            if artifact_path in artifact_paths:
                raise CustodyError("candidate_store_cleanup_artifact_duplicate")
            artifact_paths.add(artifact_path)
            validated.append(checked)
        targets = (
            self.database,
            self.database.with_name(self.database.name + "-wal"),
            self.database.with_name(self.database.name + "-shm"),
            self.database.with_name("." + self.database.name + ".tmp"),
        )
        sizes: dict[Path, int] = {}
        for target in targets:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise CustodyError("candidate_store_cleanup_target_invalid")
            if target.is_file():
                sizes[target] = target.stat().st_size
        if self._run_active:
            raise CustodyError("candidate_store_cleanup_run_active")
        try:
            self.connection.close()
            self._closed = True
            self.connection = None
            for target in targets:
                if target.exists() or target.is_symlink():
                    if target.is_symlink() or not target.is_file():
                        raise CustodyError("candidate_store_cleanup_target_invalid")
                    target.unlink()
            leftovers = [str(target) for target in targets if target.exists() or target.is_symlink()]
            if leftovers:
                raise CustodyError("candidate_store_cleanup_incomplete")
        except (OSError, sqlite3.Error) as exc:
            raise CustodyError("candidate_store_cleanup_failed") from exc
        receipt = {
            "schema": "aerp7-convomem-candidate-store-cleanup-v1",
            "database_path": str(self.database),
            "validated_artifact_count": len(validated),
            "validated_artifact_sha256": _digest(sorted(row["artifact_sha256"] for row in validated)),
            "candidate_text_count": self.reference["candidate_text_count"],
            "query_count": self.reference["query_count"],
            "store_bytes": sum(sizes.values()),
            "store_bytes_per_candidate_text": sum(sizes.values()) / self.reference["candidate_text_count"],
            "removed_files": [
                {"path": str(path), "bytes": sizes.get(path, 0)}
                for path in targets if path in sizes
            ],
            "removed_bytes": sum(sizes.values()),
        }
        self._cleanup_receipt = dict(receipt)
        return receipt

    def close(self) -> None:
        if self._closed:
            return
        if self._run_active:
            self.end_run()
        self.connection.close()
        self.connection = None
        self._closed = True

    def __enter__(self) -> "CandidateProjectionStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def _candidate_input(corpus: Mapping[str, Any], serializer: Mapping[str, Any]) -> str:
    return _digest({"serializer": serializer, "corpus_id": corpus["corpus_id"], "candidates": corpus["candidates"]})


def _input_receipt(projection: Mapping[str, Any], serializer: Mapping[str, Any]) -> dict[str, Any]:
    corpora = {row["corpus_id"]: row for row in projection["corpora"]}
    rows = [{"item_id": item["item_id"], "corpus_id": item["corpus_id"], "candidate_input_sha256": _candidate_input(corpora[item["corpus_id"]], serializer)} for item in sorted(projection["items"], key=lambda row: row["item_id"])]
    return {"projection_sha256": canonical_sha256(projection), "serializer_sha256": _digest(serializer), "item_corpora": rows, "item_corpus_set_sha256": _digest(rows)}


_INPUT_RECEIPT_REFERENCE_KEYS = frozenset({
    "schema", "item_corpora_path", "item_corpus_set_sha256", "item_count",
    "projection_sha256", "serializer_sha256", "legacy_input_sha256",
})


def _input_receipt_legacy_fields(*, projection_sha256: str, serializer_sha256: str, item_corpus_set_sha256: str, item_corpora_path: Path) -> str:
    """Hash the exact legacy mapping while splicing its external rows array."""
    fields = {
        "projection_sha256": projection_sha256,
        "serializer_sha256": serializer_sha256,
        "item_corpora": None,
        "item_corpus_set_sha256": item_corpus_set_sha256,
    }
    writer = _HashWriter()
    _emit_artifact_mapping(writer, fields, array_files={"item_corpora": item_corpora_path})
    return writer.hexdigest()


def validate_input_receipt_reference(value: Any, *, base_path: Path, expected_projection_sha256: str | None = None, expected_serializer_sha256: str | None = None) -> dict[str, Any]:
    """Validate a persisted input-receipt relation without loading its rows."""
    row = _object(value, "input_receipt_reference_invalid")
    if set(row) != _INPUT_RECEIPT_REFERENCE_KEYS or row.get("schema") != INPUT_RECEIPT_REFERENCE_SCHEMA:
        raise CustodyError("input_receipt_reference_schema_invalid")
    relative = row.get("item_corpora_path")
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise CustodyError("input_receipt_reference_path_invalid")
    base = base_path.resolve(strict=True)
    item_path = (base / relative).resolve(strict=True)
    if item_path.parent != base or item_path.is_symlink():
        raise CustodyError("input_receipt_reference_path_invalid")
    for key in ("item_corpus_set_sha256", "projection_sha256", "serializer_sha256", "legacy_input_sha256"):
        _hex(row.get(key), "input_receipt_reference_digest_invalid")
    _int(row.get("item_count"), "input_receipt_reference_count_invalid")
    if expected_projection_sha256 is not None and row["projection_sha256"] != expected_projection_sha256:
        raise CustodyError("input_receipt_reference_projection_drift")
    if expected_serializer_sha256 is not None and row["serializer_sha256"] != expected_serializer_sha256:
        raise CustodyError("input_receipt_reference_serializer_drift")
    payload_sha, _identity, _size = _stream_file_sha256(item_path, "input_receipt_reference_payload_invalid")
    if payload_sha != row["item_corpus_set_sha256"]:
        raise CustodyError("input_receipt_reference_payload_drift")
    ijson = _ijson(); previous_item_id: str | None = None; count = 0
    try:
        with item_path.open("rb") as handle:
            for item in ijson.items(handle, "item", use_float=True):
                checked = _object(item, "input_receipt_reference_row_invalid")
                if set(checked) != {"item_id", "corpus_id", "candidate_input_sha256"}:
                    raise CustodyError("input_receipt_reference_row_invalid")
                if not isinstance(checked["item_id"], str) or not checked["item_id"] or (previous_item_id is not None and checked["item_id"] <= previous_item_id):
                    raise CustodyError("input_receipt_reference_order_invalid")
                _hex(checked["candidate_input_sha256"], "input_receipt_reference_row_invalid")
                previous_item_id = checked["item_id"]; count += 1
    except CustodyError:
        raise
    except (OSError, ValueError) as exc:
        raise CustodyError("input_receipt_reference_payload_invalid") from exc
    if count != row["item_count"]:
        raise CustodyError("input_receipt_reference_count_invalid")
    if _input_receipt_legacy_fields(
        projection_sha256=row["projection_sha256"], serializer_sha256=row["serializer_sha256"],
        item_corpus_set_sha256=row["item_corpus_set_sha256"], item_corpora_path=item_path,
    ) != row["legacy_input_sha256"]:
        raise CustodyError("input_receipt_reference_legacy_digest_invalid")
    return row


def materialize_input_receipt_reference(value: Mapping[str, Any], *, base_path: Path) -> dict[str, Any]:
    """Explicit legacy adapter; callers opt into retaining only query rows."""
    reference = validate_input_receipt_reference(value, base_path=base_path)
    item_path = (base_path.resolve(strict=True) / reference["item_corpora_path"]).resolve(strict=True)
    try:
        rows = json.loads(item_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("input_receipt_reference_payload_invalid") from exc
    if not isinstance(rows, list):
        raise CustodyError("input_receipt_reference_payload_invalid")
    return {
        "projection_sha256": reference["projection_sha256"],
        "serializer_sha256": reference["serializer_sha256"],
        "item_corpora": rows,
        "item_corpus_set_sha256": reference["item_corpus_set_sha256"],
    }


def _input_receipt_store(store: CandidateProjectionStore, serializer: Mapping[str, Any], *, receipt_path: Path | None = None) -> dict[str, Any]:
    """Persist the input-receipt relation; return a bounded reference mapping."""
    if receipt_path is None:
        receipt_path = store.database.with_name("." + store.database.stem + ".input-receipt-items.json")
    receipt_path = receipt_path.resolve()
    if receipt_path.is_symlink() or not receipt_path.parent.is_dir():
        raise CustodyError("input_receipt_reference_output_present")
    if receipt_path.exists():
        # Sequential current workers may share the immutable relation file.
        # Re-derive its reference from the external bytes instead of copying
        # or rebuilding the query-sized array, and bind it to a fresh source
        # reverify for this worker.
        store.begin_run()
        try:
            payload_sha = _file_sha256(receipt_path, "input_receipt_reference_payload_invalid")
            count = 0
            parser = _ijson()
            try:
                with receipt_path.open("rb") as stream:
                    for _row in parser.items(stream, "item", use_float=True):
                        count += 1
            except (OSError, ValueError) as exc:
                raise CustodyError("input_receipt_reference_payload_invalid") from exc
            projection_sha256 = store.reference["projection_canonical_sha256"]
            serializer_sha256 = _digest(serializer)
            reference = {
                "schema": INPUT_RECEIPT_REFERENCE_SCHEMA,
                "item_corpora_path": receipt_path.name,
                "item_corpus_set_sha256": payload_sha,
                "item_count": count,
                "projection_sha256": projection_sha256,
                "serializer_sha256": serializer_sha256,
                "legacy_input_sha256": _input_receipt_legacy_fields(
                    projection_sha256=projection_sha256,
                    serializer_sha256=serializer_sha256,
                    item_corpus_set_sha256=payload_sha,
                    item_corpora_path=receipt_path,
                ),
            }
            return validate_input_receipt_reference(reference, base_path=receipt_path.parent)
        finally:
            store.end_run()
    table = "aerp7_input_receipt_candidate_inputs"
    store.begin_run()
    try:
        connection = store.connection
        try:
            connection.execute(f"CREATE TEMP TABLE {table}(corpus_id TEXT PRIMARY KEY, candidate_input_sha256 TEXT NOT NULL)")
            for (corpus_id,) in connection.execute("SELECT corpus_id FROM corpora ORDER BY corpus_id"):
                corpus = store.corpus(corpus_id)
                connection.execute(
                    f"INSERT INTO {table}(corpus_id, candidate_input_sha256) VALUES (?, ?)",
                    (corpus_id, _candidate_input(corpus, serializer)),
                )
            digest = hashlib.sha256(); digest.update(b"["); first = True; item_count = 0
            with receipt_path.open("xb") as stream:
                stream.write(b"[")
                for item_id, corpus_id, candidate_input_sha256 in connection.execute(
                    f"SELECT i.item_id, i.corpus_id, c.candidate_input_sha256 FROM items AS i JOIN {table} AS c ON c.corpus_id = i.corpus_id ORDER BY i.item_id"
                ):
                    row = {"item_id": item_id, "corpus_id": corpus_id, "candidate_input_sha256": candidate_input_sha256}
                    payload = _bytes(row)
                    if not first:
                        stream.write(b","); digest.update(b",")
                    stream.write(payload); digest.update(payload); first = False; item_count += 1
                stream.write(b"]"); digest.update(b"]"); stream.flush(); os.fsync(stream.fileno())
            item_corpus_set_sha256 = digest.hexdigest()
        finally:
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        projection_sha256 = store.reference["projection_canonical_sha256"]
        serializer_sha256 = _digest(serializer)
        reference = {
            "schema": INPUT_RECEIPT_REFERENCE_SCHEMA,
            "item_corpora_path": receipt_path.name,
            "item_corpus_set_sha256": item_corpus_set_sha256,
            "item_count": item_count,
            "projection_sha256": projection_sha256,
            "serializer_sha256": serializer_sha256,
            "legacy_input_sha256": _input_receipt_legacy_fields(
                projection_sha256=projection_sha256, serializer_sha256=serializer_sha256,
                item_corpus_set_sha256=item_corpus_set_sha256, item_corpora_path=receipt_path,
            ),
        }
        return validate_input_receipt_reference(reference, base_path=receipt_path.parent)
    except BaseException:
        if receipt_path.exists() and not receipt_path.is_symlink():
            try:
                receipt_path.unlink()
            except OSError:
                pass
        raise
    finally:
        store.end_run()


def _validate_model_receipt(value: Any) -> dict[str, Any]:
    row = _object(value, "model_receipt_invalid")
    if set(row) != {"encoder_identity", "encoder_semantics", "files"}:
        raise CustodyError("model_receipt_schema_invalid")
    if not isinstance(row["encoder_identity"], str) or not row["encoder_identity"].strip() or not isinstance(row["encoder_semantics"], str) or not row["encoder_semantics"].strip():
        raise CustodyError("model_receipt_identity_invalid")
    files = row["files"]
    if not isinstance(files, list) or not files:
        raise CustodyError("model_receipt_files_invalid")
    roles: set[str] = set()
    for item in files:
        item = _object(item, "model_receipt_file_invalid")
        # Historical synthetic receipts deliberately contain only a logical file
        # role.  A live executor must additionally require ``relative_path`` so
        # it can bind this receipt to a real model tree; accepting both here
        # keeps frozen synthetic fixtures readable without weakening live mode.
        if set(item) not in ({"path_role", "sha256", "bytes"}, {"path_role", "relative_path", "sha256", "bytes"}) or not isinstance(item["path_role"], str) or not item["path_role"].strip() or item["path_role"] in roles:
            raise CustodyError("model_receipt_file_invalid")
        if "relative_path" in item and (not isinstance(item["relative_path"], str) or not item["relative_path"].strip() or Path(item["relative_path"]).is_absolute() or ".." in Path(item["relative_path"]).parts):
            raise CustodyError("model_receipt_file_invalid")
        roles.add(item["path_role"]); _hex(item["sha256"], "model_receipt_file_invalid"); _int(item["bytes"], "model_receipt_file_invalid", positive=True)
    return row


def _validate_code_receipt(value: Any) -> dict[str, Any]:
    row = _object(value, "code_receipt_invalid")
    if set(row) != {"head", "tree", "diff_digest", "dirty_policy"} or row.get("dirty_policy") != "clean_required":
        raise CustodyError("code_receipt_formal_clean_required")
    for key in ("head", "tree"):
        _git_object_id(row.get(key), "code_receipt_invalid")
    _hex(row.get("diff_digest"), "code_receipt_invalid")
    return row


class FixedRawPolicy:
    def decide(self, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int) -> FusionRoutingDecision:
        if set(ranks) != set(SIX_VIEW_WEIGHTS) or set(ranking_keys_by_id) != set(next(iter(ranks.values()), {})):
            raise ValueError("raw ranking inputs are invalid")
        totals = {identifier: sum(weight / (rrf_k + ranks[name][identifier]) for name, weight in RAW_EXPERT_WEIGHTS.items()) for identifier in ranking_keys_by_id}
        return FusionRoutingDecision(route="raw", totals=tuple(totals.items()), effective_weights=tuple(RAW_EXPERT_WEIGHTS.items()))


def authorized_candidates(projection: Any) -> dict[str, list[AuthorizedRetrievalCandidate]]:
    if isinstance(projection, CandidateProjectionStore):
        projection.begin_run()
        try:
            return {
                corpus_id: authorized_candidates_for_corpus(projection.corpus(corpus_id), projection.reference["dataset"]["revision_sha256"])
                for corpus_id in projection.corpus_ids()
            }
        finally:
            projection.end_run()
    frozen = validate_candidate_projection(projection); revision = frozen["dataset"]["revision_sha256"]
    result: dict[str, list[AuthorizedRetrievalCandidate]] = {}
    for corpus in frozen["corpora"]:
        result[corpus["corpus_id"]] = authorized_candidates_for_corpus(corpus, revision)
    return result


def authorized_candidates_for_corpus(corpus: Mapping[str, Any], revision: str) -> list[AuthorizedRetrievalCandidate]:
    """Convert one candidate-visible corpus without materializing the projection."""
    return [AuthorizedRetrievalCandidate(
            source_event_id=row["message_id"], source_scene_id=corpus["corpus_id"], raw_text=row["text"],
            observation=structured_observation(summary=row["text"], event_type="conversation_message", actor_id=row["speaker"], target_id=None, related_entities=None, related_quests=None, related_locations=None, in_world_time=None, location_id=None),
            checkpoint_key=row["opaque_conversation_id"], policy_tuple=(), chronological_order_key=(row["corpus_order"], row["message_id"]),
            ranking_key=_digest({"revision": revision, "corpus_id": corpus["corpus_id"], "message_id": row["message_id"], "corpus_order": row["corpus_order"]}),
        ) for row in corpus["candidates"]]


def _ranker(encoder: Any, arm_id: str) -> SixViewRanker:
    if arm_id == "strong_raw": return SixViewRanker(encoder, diagnostic_ledger=True, routing_policy=FixedRawPolicy())
    if arm_id == "static_p5": return SixViewRanker(encoder, diagnostic_ledger=True, routing_policy=FixedP5Policy())
    if arm_id == "six_view_secondary": return SixViewRanker(encoder, diagnostic_ledger=True)
    raise CustodyError("ranking_arm_unknown")


def _arm_method(arm_id: str) -> dict[str, Any]:
    weights = {"strong_raw": RAW_EXPERT_WEIGHTS, "static_p5": P5_EXPERT_WEIGHTS, "six_view_secondary": SIX_VIEW_WEIGHTS}.get(arm_id)
    if weights is None: raise CustodyError("ranking_arm_unknown")
    return {"arm_id": arm_id, "ranker": "SixViewRanker", "rrf_k": SixViewRanker.rrf_k, "weights": weights, "confidence_contract": CONFIDENCE_CONTRACT}


def _margin(top_two_scores: Sequence[Any]) -> float:
    if not isinstance(top_two_scores, list) or len(top_two_scores) != 2:
        raise CustodyError("ranking_confidence_receipt_invalid")
    first, second = (_finite(value, "ranking_confidence_receipt_invalid") for value in top_two_scores)
    if first <= 0 or second < 0 or second > first:
        raise CustodyError("ranking_confidence_receipt_invalid")
    value = 1.0 - second / first
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise CustodyError("ranking_confidence_receipt_invalid")
    return value


def _current_row(item: Mapping[str, Any], corpus: Mapping[str, Any], result: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    ranked = list(result.ranked_event_ids[:10]); allowed = {row["message_id"]: row for row in corpus["candidates"]}
    if len(ranked) != min(10, len(allowed)) or len(ranked) != len(set(ranked)) or set(ranked) - set(allowed):
        raise CustodyError("ranking_top10_invalid")
    scores = [float(result.scores[item_id]) for item_id in ranked[:2]]
    confidence = _margin(scores)
    row = {"item_id": item["item_id"], "query_sha256": _query_digest(item["query_text"]), "candidate_input_sha256": _candidate_input(corpus, CURRENT_SERIALIZER), "ranked_message_ids": ranked, "retrieved_conversation_ids": list(dict.fromkeys(allowed[item_id]["opaque_conversation_id"] for item_id in ranked)), "confidence": confidence, "confidence_receipt": {"contract": CONFIDENCE_CONTRACT, "top_two_scores": scores}}
    # ``result.trace`` and its FCD1 ledger are text-free (hashes, IDs, scores,
    # ranks and receipts only).  Store them verbatim so validation can replay
    # semantics rather than trusting a hash supplied by the ranker.
    trace = {"item_id": item["item_id"], "query_sha256": row["query_sha256"], "candidate_input_sha256": row["candidate_input_sha256"], "ranked_count": len(ranked), "ranking_sha256": _digest(ranked), "ranker_trace_sha256": _digest(result.trace), "ranking_trace": result.trace}
    return row, trace


def rank_projection(*, projection: Any, encoder: Any, arm_id: str, model_receipt: Mapping[str, Any] | None = None, code_receipt: Mapping[str, Any] | None = None, query_measurements: list[dict[str, Any]] | None = None, measurement_sink: PersistedMeasurementSink | None = None, artifact_path: Path | None = None, ready_path: Path | None = None) -> dict[str, Any]:
    if isinstance(projection, CandidateProjectionStore):
        if artifact_path is None or ready_path is None:
            raise CustodyError("ranking_stream_artifact_output_missing")
        return rank_projection_stream(store=projection, encoder=encoder, arm_id=arm_id, artifact_path=artifact_path, ready_path=ready_path, model_receipt=model_receipt, code_receipt=code_receipt, query_measurements=query_measurements, measurement_sink=measurement_sink)
    if isinstance(projection, Mapping) and projection.get("schema") == CANDIDATE_PROJECTION_REFERENCE_SCHEMA:
        raise CustodyError("ranking_candidate_reference_requires_store")
    frozen = validate_candidate_projection(projection)
    if arm_id not in CURRENT_ARMS or model_receipt is None or code_receipt is None: raise CustodyError("ranking_receipt_required")
    model = _validate_model_receipt(model_receipt); code = _validate_code_receipt(code_receipt)
    # A valid receipt is not an assertion about an arbitrary encoder instance.
    # The identity check is deliberately before ranker construction, so an
    # accidental fallback encoder cannot emit even a partial artifact.
    if getattr(encoder, "identity", None) != model["encoder_identity"]:
        raise CustodyError("ranking_encoder_identity_mismatch")
    if query_measurements is not None and query_measurements:
        raise CustodyError("ranking_query_measurement_prefilled")
    ranker = _ranker(encoder, arm_id)
    candidates = authorized_candidates(frozen); corpora = {row["corpus_id"]: row for row in frozen["corpora"]}; rows = []; trace = []
    for item in sorted(frozen["items"], key=lambda row: row["item_id"]):
        started_ns = time.perf_counter_ns(); cpu_started_ns = time.process_time_ns()
        result = ranker.rank(query=item["query_text"], candidates=candidates[item["corpus_id"]])
        elapsed_ns = max(1, time.perf_counter_ns() - started_ns)
        cpu_elapsed_ns = max(1, time.process_time_ns() - cpu_started_ns)
        if query_measurements is not None:
            query_measurements.append({
                "item_id": item["item_id"],
                "query_sha256": _query_digest(item["query_text"]),
                "wall_ns": elapsed_ns,
                "cpu_ns": cpu_elapsed_ns,
            })
        row, trace_row = _current_row(item, corpora[item["corpus_id"]], result)
        rows.append(row); trace.append(trace_row)
    value = {"schema": RANKING_SCHEMA, "arm_id": arm_id, "projection_sha256": canonical_sha256(frozen), "input_receipt": _input_receipt(frozen, CURRENT_SERIALIZER), "model_receipt": model, "method_receipt": _arm_method(arm_id), "source_receipt": PROTOCOL_SOURCE, "serializer_receipt": CURRENT_SERIALIZER, "code_receipt": code, "trace_receipt": trace, "rankings": rows}
    for field, receipt in (("input_sha256", "input_receipt"), ("model_sha256", "model_receipt"), ("method_sha256", "method_receipt"), ("source_commit_sha256", "source_receipt"), ("serializer_sha256", "serializer_receipt"), ("code_sha256", "code_receipt"), ("trace_sha256", "trace_receipt")):
        value[field] = _digest(value[receipt])
    value["artifact_sha256"] = _digest(value); return value


class _HashWriter:
    def __init__(self) -> None:
        self.digest = hashlib.sha256()

    def write(self, payload: bytes) -> int:
        self.digest.update(payload)
        return len(payload)

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def _emit_artifact_mapping(writer: Any, fields: Mapping[str, Any], *, array_files: Mapping[str, Path] | None = None) -> None:
    """Emit one canonical JSON object while splicing large array files."""
    array_files = dict(array_files or {})
    writer.write(b"{")
    for index, key in enumerate(sorted(fields)):
        if index:
            writer.write(b",")
        writer.write(_bytes(key)); writer.write(b":")
        path = array_files.get(key)
        if path is None:
            writer.write(_bytes(fields[key]))
        else:
            try:
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        writer.write(chunk)
            except OSError as exc:
                raise CustodyError("ranking_artifact_stream_read_failed") from exc
    writer.write(b"}")


def _emit_array_item(path: Path, item: Mapping[str, Any], *, first: bool) -> None:
    try:
        with path.open("ab") as stream:
            if not first:
                stream.write(b",")
            stream.write(_bytes(item))
    except OSError as exc:
        raise CustodyError("ranking_artifact_stream_write_failed") from exc


def _begin_array(path: Path) -> None:
    try:
        with path.open("wb") as stream:
            stream.write(b"[")
    except OSError as exc:
        raise CustodyError("ranking_artifact_stream_write_failed") from exc


def _finish_array(path: Path) -> None:
    try:
        with path.open("ab") as stream:
            stream.write(b"]")
    except OSError as exc:
        raise CustodyError("ranking_artifact_stream_write_failed") from exc


def _file_sha256(path: Path, code: str) -> str:
    return _stream_file_sha256(path, code)[0]


def _new_temp_path(*, directory: Path, prefix: str, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    os.close(descriptor)
    return Path(raw_path)


def _publish_bytes_exclusive(path: Path, payload: bytes, code: str) -> None:
    if path.exists() or path.is_symlink() or not path.parent.is_dir():
        raise CustodyError(code)
    try:
        with path.open("xb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    except (FileExistsError, OSError) as exc:
        raise CustodyError(code) from exc


_MEASUREMENT_REFERENCE_KEYS = frozenset({
    "schema", "measurement_path", "ready_path", "sequence_schema",
    "generation_id", "projection_sha256", "count", "sequence_sha256",
    "coverage_sha256", "wall_summary", "cpu_summary", "payload_sha256",
})
_MEASUREMENT_SUMMARY_KEYS = frozenset({"count", "p50", "p95", "p99", "max"})


def _measurement_payload_prefix(*, generation_id: str, projection_sha256: str) -> bytes:
    """Canonical prefix for the persisted measurement sequence object."""
    return (
        b'{"generation_id":' + _bytes(generation_id)
        + b',"projection_sha256":' + _bytes(projection_sha256)
        + b',"rows":['
    )


def _measurement_payload_suffix() -> bytes:
    return b'],"schema":' + _bytes(MEASUREMENT_SEQUENCE_SCHEMA) + b'}'


def _measurement_row(value: Any) -> dict[str, Any]:
    row = _object(value, "ranking_measurement_row_invalid")
    if set(row) != {"item_id", "query_sha256", "wall_ns", "cpu_ns"}:
        raise CustodyError("ranking_measurement_row_invalid")
    _hex(row.get("item_id"), "ranking_measurement_row_invalid")
    _hex(row.get("query_sha256"), "ranking_measurement_row_invalid")
    for key in ("wall_ns", "cpu_ns"):
        _int(row.get(key), "ranking_measurement_row_invalid", positive=True)
    return row


def _measurement_summary(connection: sqlite3.Connection, column: str, count: int) -> dict[str, int]:
    if column not in {"wall_ns", "cpu_ns"} or count <= 0:
        raise CustodyError("ranking_measurement_summary_invalid")
    values: dict[str, int] = {"count": count}
    for label, fraction in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
        offset = int((count - 1) * fraction)
        row = connection.execute(
            f"SELECT {column} FROM measurements ORDER BY {column}, seq LIMIT 1 OFFSET ?",
            (offset,),
        ).fetchone()
        if row is None:
            raise CustodyError("ranking_measurement_summary_invalid")
        values[label] = int(row[0])
    row = connection.execute(f"SELECT MAX({column}) FROM measurements").fetchone()
    if row is None or row[0] is None:
        raise CustodyError("ranking_measurement_summary_invalid")
    values["max"] = int(row[0])
    return values


def _measurement_summary_valid(value: Any, *, count: int) -> dict[str, int]:
    row = _object(value, "ranking_measurement_summary_invalid")
    if set(row) != _MEASUREMENT_SUMMARY_KEYS or row.get("count") != count:
        raise CustodyError("ranking_measurement_summary_invalid")
    previous = 0
    for key in ("p50", "p95", "p99", "max"):
        current = _int(row.get(key), "ranking_measurement_summary_invalid", positive=True)
        if current < previous:
            raise CustodyError("ranking_measurement_summary_invalid")
        previous = current
    return {key: int(row[key]) for key in ("count", "p50", "p95", "p99", "max")}


class PersistedMeasurementSink:
    """Append-only, disk-backed per-query timing sink.

    ``append`` retains no query-sized Python collection.  The sequence is
    written as canonical JSON, while a temporary SQLite table supplies exact
    legacy percentile semantics without retaining all timings in the worker.
    ``finalize`` publishes the measurement file and its READY-bound reference;
    callers must retain the reference and explicitly clean temporary state.
    """

    def __init__(self, *, measurement_path: Path, ready_path: Path,
                 generation_id: str, projection_sha256: str) -> None:
        self.measurement_path = measurement_path.resolve()
        self.ready_path = ready_path.resolve()
        self.generation_id = generation_id
        self.projection_sha256 = projection_sha256
        if (
            self.measurement_path.parent != self.ready_path.parent
            or not self.measurement_path.parent.is_dir()
            or self.measurement_path.exists() or self.ready_path.exists()
            or self.measurement_path.is_symlink() or self.ready_path.is_symlink()
        ):
            raise CustodyError("ranking_measurement_output_present")
        self._aggregate_path = self.measurement_path.with_name(
            "." + self.measurement_path.name + ".aggregate.sqlite3"
        )
        if self._aggregate_path.exists() or self._aggregate_path.is_symlink():
            raise CustodyError("ranking_measurement_aggregate_present")
        self._stream = None
        self._connection = None
        self._count = 0
        self._previous_item_id: str | None = None
        self._first = True
        self._sequence = hashlib.sha256(b"[")
        self._coverage = hashlib.sha256(b"[")
        self._finalized = False
        self._aborted = False
        try:
            self._stream = self.measurement_path.open("xb")
            self._stream.write(_measurement_payload_prefix(
                generation_id=self.generation_id,
                projection_sha256=self.projection_sha256,
            ))
            self._connection = sqlite3.connect(self._aggregate_path)
            self._connection.execute(
                "CREATE TABLE measurements(seq INTEGER PRIMARY KEY, item_id TEXT NOT NULL, query_sha256 TEXT NOT NULL, wall_ns INTEGER NOT NULL, cpu_ns INTEGER NOT NULL)"
            )
            self._connection.commit()
        except (OSError, sqlite3.Error) as exc:
            self.abort()
            raise CustodyError("ranking_measurement_sink_open_failed") from exc

    @classmethod
    def create(cls, *, directory: Path, stem: str, generation_id: str,
               projection_sha256: str) -> "PersistedMeasurementSink":
        directory = directory.resolve()
        if not directory.is_dir() or directory.is_symlink() or not stem:
            raise CustodyError("ranking_measurement_output_parent_invalid")
        return cls(
            measurement_path=directory / f".{stem}.measurements.json",
            ready_path=directory / f".{stem}.measurements.READY.json",
            generation_id=generation_id,
            projection_sha256=projection_sha256,
        )

    def append(self, value: Mapping[str, Any]) -> None:
        if self._finalized or self._aborted or self._stream is None or self._connection is None:
            raise CustodyError("ranking_measurement_sink_closed")
        row = _measurement_row(value)
        if self._previous_item_id is not None and row["item_id"] <= self._previous_item_id:
            raise CustodyError("ranking_measurement_order_invalid")
        payload = _bytes(row)
        coverage = _bytes({"item_id": row["item_id"], "query_sha256": row["query_sha256"]})
        try:
            if not self._first:
                self._stream.write(b",")
                self._sequence.update(b",")
                self._coverage.update(b",")
            self._stream.write(payload)
            self._sequence.update(payload)
            self._coverage.update(coverage)
            self._connection.execute(
                "INSERT INTO measurements VALUES (?, ?, ?, ?, ?)",
                (self._count + 1, row["item_id"], row["query_sha256"], row["wall_ns"], row["cpu_ns"]),
            )
            self._count += 1
            self._previous_item_id = row["item_id"]
            self._first = False
            if self._count % 1024 == 0:
                self._connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise CustodyError("ranking_measurement_sink_write_failed") from exc

    def finalize(self, *, expected_count: int) -> dict[str, Any]:
        if self._finalized or self._aborted or self._stream is None or self._connection is None:
            raise CustodyError("ranking_measurement_sink_closed")
        if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count <= 0 or self._count != expected_count:
            raise CustodyError("ranking_measurement_count_invalid")
        try:
            self._connection.commit()
            self._stream.write(_measurement_payload_suffix())
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._stream.close()
            self._stream = None
            self._sequence.update(b"]")
            self._coverage.update(b"]")
            wall_summary = _measurement_summary(self._connection, "wall_ns", self._count)
            cpu_summary = _measurement_summary(self._connection, "cpu_ns", self._count)
            payload_sha = _file_sha256(self.measurement_path, "ranking_measurement_payload_invalid")
            reference = {
                "schema": MEASUREMENT_REFERENCE_SCHEMA,
                "measurement_path": self.measurement_path.name,
                "ready_path": self.ready_path.name,
                "sequence_schema": MEASUREMENT_SEQUENCE_SCHEMA,
                "generation_id": self.generation_id,
                "projection_sha256": self.projection_sha256,
                "count": self._count,
                "sequence_sha256": self._sequence.hexdigest(),
                "coverage_sha256": self._coverage.hexdigest(),
                "wall_summary": wall_summary,
                "cpu_summary": cpu_summary,
                "payload_sha256": payload_sha,
            }
            ready = {
                "schema": MEASUREMENT_READY_SCHEMA,
                "reference": reference,
                "reference_sha256": _digest(reference),
                "payload_sha256": payload_sha,
                "ready_sha256": "",
            }
            ready["ready_sha256"] = _digest({key: item for key, item in ready.items() if key != "ready_sha256"})
            _publish_bytes_exclusive(self.ready_path, _bytes(ready), "ranking_measurement_ready_output_present")
            self._finalized = True
            self._close_aggregate()
            return reference
        except (OSError, sqlite3.Error) as exc:
            self.abort()
            raise CustodyError("ranking_measurement_finalize_failed") from exc

    def _close_aggregate(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        try:
            self._aggregate_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CustodyError("ranking_measurement_aggregate_cleanup_failed") from exc

    def abort(self) -> None:
        if self._aborted:
            return
        self._aborted = True
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None
        for path in (self.measurement_path, self.ready_path, self._aggregate_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def __enter__(self) -> "PersistedMeasurementSink":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        if not self._finalized:
            self.abort()


def _measurement_sidecar_path(value: Any, *, base_path: Path, key: str) -> Path:
    relative = value.get(key) if isinstance(value, Mapping) else None
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise CustodyError("ranking_measurement_path_invalid")
    base = base_path.resolve(strict=True)
    path = (base / relative).resolve(strict=True)
    if path.parent != base or path.is_symlink() or not path.is_file():
        raise CustodyError("ranking_measurement_path_invalid")
    return path


def _scan_measurement_sequence(path: Path, *, generation_id: str,
                               projection_sha256: str, expected_count: int) -> dict[str, Any]:
    """Stream-validate one sequence and aggregate exact percentile inputs."""
    ijson = _ijson()
    sequence = hashlib.sha256(b"[")
    coverage = hashlib.sha256(b"[")
    payload = _HashWriter()
    payload.write(_measurement_payload_prefix(generation_id=generation_id, projection_sha256=projection_sha256))
    aggregate_path = _new_temp_path(directory=path.parent, prefix=".aerp7-measurement-validate-", suffix=".sqlite3")
    connection: sqlite3.Connection | None = None
    first = True
    count = 0
    previous_item_id: str | None = None
    try:
        connection = sqlite3.connect(aggregate_path)
        connection.execute("CREATE TABLE measurements(seq INTEGER PRIMARY KEY, item_id TEXT NOT NULL, query_sha256 TEXT NOT NULL, wall_ns INTEGER NOT NULL, cpu_ns INTEGER NOT NULL)")
        with path.open("rb") as stream:
            for raw in ijson.items(stream, "rows.item", use_float=True):
                row = _measurement_row(raw)
                if previous_item_id is not None and row["item_id"] <= previous_item_id:
                    raise CustodyError("ranking_measurement_order_invalid")
                row_payload = _bytes(row)
                coverage_payload = _bytes({"item_id": row["item_id"], "query_sha256": row["query_sha256"]})
                if not first:
                    sequence.update(b","); coverage.update(b","); payload.write(b",")
                sequence.update(row_payload); coverage.update(coverage_payload); payload.write(row_payload)
                connection.execute("INSERT INTO measurements VALUES (?, ?, ?, ?, ?)", (count + 1, row["item_id"], row["query_sha256"], row["wall_ns"], row["cpu_ns"]))
                count += 1; previous_item_id = row["item_id"]; first = False
        if count != expected_count:
            raise CustodyError("ranking_measurement_count_invalid")
        sequence.update(b"]"); coverage.update(b"]"); payload.write(_measurement_payload_suffix())
        connection.commit()
        return {
            "count": count,
            "sequence_sha256": sequence.hexdigest(),
            "coverage_sha256": coverage.hexdigest(),
            "wall_summary": _measurement_summary(connection, "wall_ns", count),
            "cpu_summary": _measurement_summary(connection, "cpu_ns", count),
            "payload_sha256": payload.hexdigest(),
        }
    except (OSError, sqlite3.Error, ValueError) as exc:
        if isinstance(exc, CustodyError):
            raise
        raise CustodyError("ranking_measurement_payload_invalid") from exc
    finally:
        if connection is not None:
            connection.close()
        try:
            aggregate_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def validate_measurement_reference(value: Any, *, base_path: Path,
                                   expected_projection_sha256: str | None = None,
                                   expected_generation_id: str | None = None,
                                   expected_count: int | None = None) -> dict[str, Any]:
    """Validate a persisted measurement reference without materializing rows."""
    row = _object(value, "ranking_measurement_reference_invalid")
    if set(row) != _MEASUREMENT_REFERENCE_KEYS or row.get("schema") != MEASUREMENT_REFERENCE_SCHEMA or row.get("sequence_schema") != MEASUREMENT_SEQUENCE_SCHEMA:
        raise CustodyError("ranking_measurement_reference_schema_invalid")
    for key in ("generation_id",):
        if not isinstance(row.get(key), str) or not row[key].strip():
            raise CustodyError("ranking_measurement_reference_generation_invalid")
    _hex(row.get("projection_sha256"), "ranking_measurement_reference_digest_invalid")
    for key in ("sequence_sha256", "coverage_sha256", "payload_sha256"):
        _hex(row.get(key), "ranking_measurement_reference_digest_invalid")
    count = _int(row.get("count"), "ranking_measurement_reference_count_invalid", positive=True)
    if expected_count is not None and count != expected_count:
        raise CustodyError("ranking_measurement_reference_count_invalid")
    if expected_projection_sha256 is not None and row["projection_sha256"] != expected_projection_sha256:
        raise CustodyError("ranking_measurement_reference_projection_drift")
    if expected_generation_id is not None and row["generation_id"] != expected_generation_id:
        raise CustodyError("ranking_measurement_reference_generation_drift")
    wall_summary = _measurement_summary_valid(row.get("wall_summary"), count=count)
    cpu_summary = _measurement_summary_valid(row.get("cpu_summary"), count=count)
    measurement_path = _measurement_sidecar_path(row, base_path=base_path, key="measurement_path")
    ready_path = _measurement_sidecar_path(row, base_path=base_path, key="ready_path")
    if measurement_path == ready_path:
        raise CustodyError("ranking_measurement_path_invalid")
    try:
        ready_raw = ready_path.read_bytes()
        ready = json.loads(ready_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("ranking_measurement_ready_invalid") from exc
    expected_ready = {"schema": MEASUREMENT_READY_SCHEMA, "reference": row, "reference_sha256": _digest(row), "payload_sha256": row["payload_sha256"], "ready_sha256": ""}
    expected_ready["ready_sha256"] = _digest({key: item for key, item in expected_ready.items() if key != "ready_sha256"})
    if ready != expected_ready or ready_raw != _bytes(ready):
        raise CustodyError("ranking_measurement_ready_binding_invalid")
    checked = _scan_measurement_sequence(
        measurement_path,
        generation_id=row["generation_id"],
        projection_sha256=row["projection_sha256"],
        expected_count=count,
    )
    if any(checked[key] != row[key] for key in ("count", "sequence_sha256", "coverage_sha256", "wall_summary", "cpu_summary", "payload_sha256")):
        raise CustodyError("ranking_measurement_payload_drift")
    return {**row, "wall_summary": wall_summary, "cpu_summary": cpu_summary}


def validate_ranking_artifact_reference(value: Any, *, expected_projection_sha256: str | None = None) -> dict[str, Any]:
    """Validate a small ref; payload validation remains a streaming operation."""
    row = _object(value, "ranking_artifact_reference_invalid")
    required = {"schema", "artifact_path", "ready_path", "arm_id", "projection_sha256", "generation_id", "payload_sha256", "artifact_sha256", "ready_sha256", "method_receipt", "serializer_receipt", "input_receipt", "input_sha256", "model_receipt", "model_sha256", "source_receipt", "source_commit_sha256", "serializer_sha256", "code_receipt", "code_sha256", "trace_sha256"}
    allowed = required | {"measurement_reference"}
    if set(row) not in (required, allowed) or row.get("schema") != RANKING_ARTIFACT_REFERENCE_SCHEMA or row.get("arm_id") not in CURRENT_ARMS:
        raise CustodyError("ranking_artifact_reference_schema_invalid")
    for key in ("artifact_path", "ready_path"):
        if not isinstance(row.get(key), str) or not row[key] or not Path(row[key]).is_absolute():
            raise CustodyError("ranking_artifact_reference_path_invalid")
    if Path(row["artifact_path"]).parent != Path(row["ready_path"]).parent or Path(row["artifact_path"]).is_symlink() or Path(row["ready_path"]).is_symlink():
        raise CustodyError("ranking_artifact_reference_path_invalid")
    if not isinstance(row.get("generation_id"), str) or not row["generation_id"].strip():
        raise CustodyError("ranking_artifact_reference_generation_invalid")
    for key in ("projection_sha256", "payload_sha256", "artifact_sha256", "ready_sha256", "input_sha256", "model_sha256", "source_commit_sha256", "serializer_sha256", "code_sha256", "trace_sha256"):
        _hex(row.get(key), "ranking_artifact_reference_digest_invalid")
    if expected_projection_sha256 is not None and row["projection_sha256"] != expected_projection_sha256:
        raise CustodyError("ranking_artifact_reference_projection_drift")
    artifact_path = Path(row["artifact_path"]); ready_path = Path(row["ready_path"])
    if not isinstance(row.get("input_receipt"), Mapping) or row["input_receipt"].get("schema") != INPUT_RECEIPT_REFERENCE_SCHEMA:
        raise CustodyError("ranking_artifact_reference_input_invalid")
    validate_input_receipt_reference(
        row["input_receipt"], base_path=artifact_path.parent,
        expected_projection_sha256=row["projection_sha256"],
        expected_serializer_sha256=row["serializer_sha256"],
    )
    if row["input_sha256"] != row["input_receipt"]["legacy_input_sha256"]:
        raise CustodyError("ranking_artifact_reference_input_digest_invalid")
    measurement_reference = row.get("measurement_reference")
    if measurement_reference is not None:
        validate_measurement_reference(
            measurement_reference,
            base_path=artifact_path.parent,
            expected_projection_sha256=row["projection_sha256"],
            expected_generation_id=row["generation_id"],
        )
    try:
        ready_raw, ready_identity, _ready_size = CandidateProjectionCursor._read_regular(ready_path, "ranking_artifact_ready_invalid")
        ready = json.loads(ready_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("ranking_artifact_ready_invalid") from exc
    checked_ready = _stream_artifact_ready(ready, path=ready_path)
    if any(checked_ready.get(key) != row[key] for key in ("arm_id", "generation_id", "projection_sha256", "payload_sha256", "artifact_sha256", "ready_sha256")):
        raise CustodyError("ranking_artifact_ready_binding_invalid")
    if measurement_reference is not None and checked_ready.get("measurement_reference") != measurement_reference:
        raise CustodyError("ranking_artifact_ready_measurement_binding_invalid")
    payload_sha = _file_sha256(artifact_path, "ranking_artifact_payload_invalid")
    if payload_sha != row["payload_sha256"]:
        raise CustodyError("ranking_artifact_payload_drift")
    try:
        after = os.lstat(ready_path)
    except OSError as exc:
        raise CustodyError("ranking_artifact_ready_invalid") from exc
    if (after.st_dev, after.st_ino) != ready_identity:
        raise CustodyError("ranking_artifact_ready_identity_drift")
    return row


def _stream_artifact_ready(value: Mapping[str, Any], *, path: Path) -> dict[str, Any]:
    row = _object(value, "ranking_artifact_ready_invalid")
    required = {"schema", "arm_id", "generation_id", "projection_sha256", "payload_sha256", "artifact_sha256", "ready_sha256"}
    if set(row) not in (required, required | {"measurement_reference"}) or row.get("schema") != RANKING_ARTIFACT_READY_SCHEMA:
        raise CustodyError("ranking_artifact_ready_invalid")
    for key in required - {"schema", "arm_id", "generation_id"}:
        _hex(row.get(key), "ranking_artifact_ready_invalid")
    if row.get("ready_sha256") != _digest({key: item for key, item in row.items() if key != "ready_sha256"}):
        raise CustodyError("ranking_artifact_ready_invalid")
    return row


def rank_projection_stream(*, store: CandidateProjectionStore, encoder: Any, arm_id: str, artifact_path: Path, ready_path: Path, model_receipt: Mapping[str, Any] | None = None, code_receipt: Mapping[str, Any] | None = None, query_measurements: list[dict[str, Any]] | None = None, measurement_sink: PersistedMeasurementSink | None = None) -> dict[str, Any]:
    """Rank one candidate store while keeping projection/artifact on disk.

    The per-query candidate list and encoder vectors are bounded by one corpus;
    trace/ranking arrays are spooled to files and then composed into one
    canonical artifact.  The returned value is a small artifact reference.
    """
    if not isinstance(store, CandidateProjectionStore):
        raise CustodyError("ranking_candidate_store_required")
    if arm_id not in CURRENT_ARMS or model_receipt is None or code_receipt is None:
        raise CustodyError("ranking_receipt_required")
    model = _validate_model_receipt(model_receipt); code = _validate_code_receipt(code_receipt)
    if getattr(encoder, "identity", None) != model["encoder_identity"]:
        raise CustodyError("ranking_encoder_identity_mismatch")
    if query_measurements is not None:
        raise CustodyError("ranking_query_measurement_sink_required")
    if measurement_sink is not None and not isinstance(measurement_sink, PersistedMeasurementSink):
        raise CustodyError("ranking_measurement_sink_invalid")
    artifact_path = artifact_path.resolve(); ready_path = ready_path.resolve()
    if artifact_path.parent != ready_path.parent or artifact_path.exists() or ready_path.exists() or artifact_path.is_symlink() or ready_path.is_symlink():
        raise CustodyError("ranking_artifact_output_present")
    if not artifact_path.parent.is_dir():
        raise CustodyError("ranking_artifact_output_parent_invalid")
    ranker = _ranker(encoder, arm_id)
    temporary_paths: list[Path] = []
    trace_file = rankings_file = artifact_tmp = None
    measurement_sink = measurement_sink or PersistedMeasurementSink.create(
        directory=artifact_path.parent,
        stem=artifact_path.stem,
        generation_id=store.reference["generation_id"],
        projection_sha256=store.reference["projection_canonical_sha256"],
    )
    if measurement_sink.measurement_path.parent != artifact_path.parent or measurement_sink.ready_path.parent != artifact_path.parent:
        measurement_sink.abort()
        raise CustodyError("ranking_measurement_sink_path_invalid")
    # The same candidate relation is shared by the sequential current arms so
    # deterministic P5 repeats retain identical artifact digests.  It remains
    # external to every artifact and is revalidated before each use.
    receipt_path = store.database.with_name("." + store.database.stem + ".input-receipt-items.json")
    receipt_preexisting = receipt_path.exists()
    try:
        input_receipt = _input_receipt_store(store, CURRENT_SERIALIZER, receipt_path=receipt_path)
        # Keep the relation beside the published artifact on success; it is
        # an external receipt payload, not a temporary scratch file.
        if not receipt_preexisting:
            temporary_paths.append(receipt_path)
        trace_file = _new_temp_path(directory=artifact_path.parent, prefix=".aerp7-trace-", suffix=".json"); temporary_paths.append(trace_file)
        rankings_file = _new_temp_path(directory=artifact_path.parent, prefix=".aerp7-rankings-", suffix=".json"); temporary_paths.append(rankings_file)
        _begin_array(trace_file); _begin_array(rankings_file)
        trace_first = rankings_first = True
        active_corpus_id: str | None = None
        active_corpus: dict[str, Any] | None = None
        store.begin_run()
        try:
            for item in store.iter_items():
                corpus_id = item.get("corpus_id")
                if corpus_id != active_corpus_id:
                    active_corpus_id = corpus_id
                    active_corpus = store.corpus(corpus_id)
                if active_corpus is None:
                    raise CustodyError("ranking_candidate_corpus_missing")
                corpus = active_corpus
                candidates = authorized_candidates_for_corpus(corpus, store.reference["dataset"]["revision_sha256"])
                started_ns = time.perf_counter_ns(); cpu_started_ns = time.process_time_ns()
                result = ranker.rank(query=item["query_text"], candidates=candidates)
                elapsed_ns = max(1, time.perf_counter_ns() - started_ns); cpu_elapsed_ns = max(1, time.process_time_ns() - cpu_started_ns)
                measurement_sink.append({"item_id": item["item_id"], "query_sha256": _query_digest(item["query_text"]), "wall_ns": elapsed_ns, "cpu_ns": cpu_elapsed_ns})
                row, trace_row = _current_row(item, corpus, result)
                _emit_array_item(trace_file, trace_row, first=trace_first); trace_first = False
                _emit_array_item(rankings_file, row, first=rankings_first); rankings_first = False
                # The old ranker caches all passage vectors by corpus.  A
                # streaming worker must not accumulate one cache per corpus.
                if hasattr(ranker, "_passage_cache"):
                    ranker._passage_cache.clear()
        finally:
            # Do not let the last corpus survive into artifact composition or
            # the post-run source reverify.  At most one corpus is resident.
            active_corpus = None
            active_corpus_id = None
            store.end_run()
        measurement_reference = measurement_sink.finalize(expected_count=store.reference["query_count"])
        _finish_array(trace_file); _finish_array(rankings_file)
        trace_sha = _file_sha256(trace_file, "ranking_trace_stream_read_failed")
        headers: dict[str, Any] = {
            "schema": RANKING_SCHEMA, "arm_id": arm_id,
            "projection_sha256": store.reference["projection_canonical_sha256"],
            # The stream artifact carries a persisted relation reference. Keep
            # the public input digest byte-identical to the legacy inline
            # receipt so downstream replay/paired comparisons remain stable.
            "input_receipt": input_receipt, "input_sha256": input_receipt["legacy_input_sha256"],
            "model_receipt": model, "model_sha256": _digest(model),
            "method_receipt": _arm_method(arm_id), "method_sha256": _digest(_arm_method(arm_id)),
            "source_receipt": PROTOCOL_SOURCE, "source_commit_sha256": _digest(PROTOCOL_SOURCE),
            "serializer_receipt": CURRENT_SERIALIZER, "serializer_sha256": _digest(CURRENT_SERIALIZER),
            "code_receipt": code, "code_sha256": _digest(code),
            "trace_receipt": None, "trace_sha256": trace_sha, "rankings": None,
        }
        no_artifact = {key: value for key, value in headers.items() if key not in {"trace_receipt", "rankings"}}
        no_artifact.update({"trace_receipt": None, "rankings": None})
        # Hash the exact canonical object while splicing the two large arrays.
        digest_writer = _HashWriter()
        _emit_artifact_mapping(digest_writer, no_artifact, array_files={"trace_receipt": trace_file, "rankings": rankings_file})
        artifact_sha = digest_writer.hexdigest()
        full_fields = dict(no_artifact); full_fields["artifact_sha256"] = artifact_sha
        # Ensure the output is committed atomically from an owned temporary.
        artifact_tmp = _new_temp_path(directory=artifact_path.parent, prefix=".aerp7-artifact-", suffix=".json"); temporary_paths.append(artifact_tmp)
        with artifact_tmp.open("wb") as target:
            _emit_artifact_mapping(target, full_fields, array_files={"trace_receipt": trace_file, "rankings": rankings_file})
            target.flush(); os.fsync(target.fileno())
        payload_sha = _file_sha256(artifact_tmp, "ranking_artifact_stream_read_failed")
        os.replace(artifact_tmp, artifact_path); temporary_paths.remove(artifact_tmp)
        ready = {"schema": RANKING_ARTIFACT_READY_SCHEMA, "arm_id": arm_id, "generation_id": store.reference["generation_id"], "projection_sha256": store.reference["projection_canonical_sha256"], "payload_sha256": payload_sha, "artifact_sha256": artifact_sha, "measurement_reference": measurement_reference, "ready_sha256": ""}
        ready["ready_sha256"] = _digest({key: value for key, value in ready.items() if key != "ready_sha256"})
        _publish_bytes_exclusive(ready_path, _bytes(ready), "ranking_ready_output_present")
        if not receipt_preexisting:
            temporary_paths.remove(receipt_path)
        return validate_ranking_artifact_reference({
            "schema": RANKING_ARTIFACT_REFERENCE_SCHEMA, "artifact_path": str(artifact_path), "ready_path": str(ready_path), "arm_id": arm_id,
            "projection_sha256": store.reference["projection_canonical_sha256"], "generation_id": store.reference["generation_id"],
            "payload_sha256": payload_sha, "artifact_sha256": artifact_sha, "ready_sha256": ready["ready_sha256"],
            "measurement_reference": measurement_reference,
            "method_receipt": headers["method_receipt"], "serializer_receipt": headers["serializer_receipt"], "input_receipt": input_receipt, "input_sha256": headers["input_sha256"],
            "model_receipt": model, "model_sha256": headers["model_sha256"], "source_receipt": PROTOCOL_SOURCE, "source_commit_sha256": headers["source_commit_sha256"],
            "serializer_sha256": headers["serializer_sha256"], "code_receipt": code, "code_sha256": headers["code_sha256"], "trace_sha256": trace_sha,
        })
    except BaseException:
        if measurement_sink is not None:
            measurement_sink.abort()
        if artifact_path.exists() and not artifact_path.is_symlink():
            try: artifact_path.unlink()
            except OSError: pass
        if ready_path.exists() and not ready_path.is_symlink():
            try: ready_path.unlink()
            except OSError: pass
        raise
    finally:
        for path in temporary_paths:
            try: path.unlink()
            except FileNotFoundError: pass
            except OSError: pass


def freeze_current_rankings(*, projection: Any, encoder: Any, model_receipt: Mapping[str, Any] | None = None, code_receipt: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    raw = rank_projection(projection=projection, encoder=encoder, arm_id="strong_raw", model_receipt=model_receipt, code_receipt=code_receipt)
    first = rank_projection(projection=projection, encoder=encoder, arm_id="static_p5", model_receipt=model_receipt, code_receipt=code_receipt)
    second = rank_projection(projection=projection, encoder=encoder, arm_id="static_p5", model_receipt=model_receipt, code_receipt=code_receipt)
    if _bytes(first) != _bytes(second): raise CustodyError("static_p5_repeat_nondeterministic")
    return [raw, first, rank_projection(projection=projection, encoder=encoder, arm_id="six_view_secondary", model_receipt=model_receipt, code_receipt=code_receipt)]


def _validate_original_worker_physical_receipt(projection: Mapping[str, Any], value: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate one worker's complete physical receipt without a handoff claim."""
    raw = _object(value, "original_physical_receipt_invalid")
    required = {"physical_count", "physical_ids_sha256", "embedding", "hnsw_config", "graph_files", "immutable_backend_sha256", "immutable_non_length_backend_sha256", "immutable_residual_backend_sha256", "sqlite_semantic_sha256", "operational_delta", "direct_read_normalization_delta"}
    if set(raw) != required:
        raise CustodyError("original_physical_receipt_invalid")
    physical_ids = [f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in projection["corpora"] for candidate in corpus["candidates"]]
    expected = {"physical_count": len(physical_ids), "physical_ids_sha256": _digest(sorted(physical_ids))}
    scientific = _logical_original_physical_receipt(raw)
    if any(scientific[key] != expected_value for key, expected_value in expected.items()) or scientific.get("hnsw_config") != ORIGINAL_HNSW_CONFIG:
        raise CustodyError("original_physical_receipt_invalid")
    embedding = _object(scientific["embedding"], "original_physical_receipt_invalid")
    if set(embedding) != {"count", "dimension", "dtype", "float32_sha256"} or embedding.get("count") != len(physical_ids) or embedding.get("dimension") != 384 or embedding.get("dtype") != "float32":
        raise CustodyError("original_physical_receipt_invalid")
    _hex(embedding.get("float32_sha256"), "original_physical_receipt_invalid")
    _hex(raw.get("immutable_backend_sha256"), "original_physical_receipt_invalid")
    _hex(raw.get("immutable_non_length_backend_sha256"), "original_physical_receipt_invalid")
    _hex(scientific.get("immutable_residual_backend_sha256"), "original_physical_receipt_invalid")
    _hex(scientific.get("sqlite_semantic_sha256"), "original_physical_receipt_invalid")
    graphs = raw.get("graph_files")
    if not isinstance(graphs, list) or [entry.get("name") for entry in graphs if isinstance(entry, Mapping)] != list(ORIGINAL_GRAPH_NAMES):
        raise CustodyError("original_physical_receipt_invalid")
    graph_parents = set()
    for graph in graphs:
        graph = _object(graph, "original_physical_receipt_invalid")
        if set(graph) != {"name", "path", "bytes", "sha256"} or not isinstance(graph["name"], str) or graph["name"] not in ORIGINAL_GRAPH_NAMES or not _canonical_graph_path(graph.get("path"), graph["name"]) or _int(graph["bytes"], "original_physical_receipt_invalid", positive=True) < 1:
            raise CustodyError("original_physical_receipt_invalid")
        parent, separator, basename = graph["path"].rpartition("/")
        if basename != graph["name"]:
            raise CustodyError("original_physical_receipt_invalid")
        graph_parents.add(f"{parent}{separator}")
        _hex(graph["sha256"], "original_physical_receipt_invalid")
    if len(graph_parents) != 1 or scientific.get("operational_delta") != ORIGINAL_OPERATIONAL_DELTA:
        raise CustodyError("original_physical_receipt_invalid")
    return raw, scientific


def _worker_original_replicate(projection: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """Validate a draft's single worker receipt, before coordinator handoff exists."""
    row = _object(value, "original_replicate_invalid")
    if set(row) != {"build_id", "input_receipt", "input_sha256", "index_receipt", "index_sha256", "trace_receipt", "trace_sha256", "rankings"} or not isinstance(row.get("build_id"), str) or not row["build_id"].strip():
        raise CustodyError("original_replicate_schema_invalid")
    expected_input = _input_receipt(projection, ORIGINAL_MEMPALACE_SERIALIZER)
    if row["input_receipt"] != expected_input:
        raise CustodyError("original_input_receipt_invalid")
    for digest_key, receipt_key in (("input_sha256", "input_receipt"), ("index_sha256", "index_receipt"), ("trace_sha256", "trace_receipt")):
        _hex(row.get(digest_key), "original_replicate_digest_invalid")
        if row[digest_key] != _digest(row[receipt_key]):
            raise CustodyError("original_replicate_digest_invalid")
    index = _object(row["index_receipt"], "original_index_receipt_invalid")
    required_index = {"build_id", "fresh_build", "collection_identity", "index_identity_sha256", "cold_reopen", "call_contract", "input_coverage_sha256", "query_coverage_sha256", "output_coverage_sha256", "worker_physical_receipt"}
    if set(index) != required_index or index["build_id"] != row["build_id"] or index["fresh_build"] is not True or index["cold_reopen"] is not True or index["call_contract"] != ORIGINAL_CALL_CONTRACT or not isinstance(index["collection_identity"], str) or not index["collection_identity"].strip():
        raise CustodyError("original_index_receipt_invalid")
    expected_queries = _digest([{"item_id": item["item_id"], "query_sha256": _query_digest(item["query_text"])} for item in sorted(projection["items"], key=lambda item: item["item_id"])])
    expected_outputs = _digest([{"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in sorted(row["trace_receipt"], key=lambda trace: trace["item_id"])])
    for key, expected in (("index_identity_sha256", None), ("input_coverage_sha256", _digest(expected_input["item_corpora"])), ("query_coverage_sha256", expected_queries), ("output_coverage_sha256", expected_outputs)):
        _hex(index.get(key), "original_index_receipt_invalid")
        if expected is not None and index[key] != expected:
            raise CustodyError("original_index_receipt_invalid")
    worker_raw, _scientific = _validate_original_worker_physical_receipt(projection, index["worker_physical_receipt"])
    if index["index_identity_sha256"] != _digest({"collection_identity": index["collection_identity"], "physical": worker_raw}):
        raise CustodyError("original_index_receipt_invalid")
    return row


def _original_replicate(projection: Mapping[str, Any], value: Any) -> dict[str, Any]:
    row = _object(value, "original_replicate_invalid")
    if set(row) != {"build_id", "input_receipt", "input_sha256", "index_receipt", "index_sha256", "trace_receipt", "trace_sha256", "rankings"} or not isinstance(row.get("build_id"), str) or not row["build_id"].strip():
        raise CustodyError("original_replicate_schema_invalid")
    expected_input = _input_receipt(projection, ORIGINAL_MEMPALACE_SERIALIZER)
    if row["input_receipt"] != expected_input: raise CustodyError("original_input_receipt_invalid")
    for digest_key, receipt_key in (("input_sha256", "input_receipt"), ("index_sha256", "index_receipt"), ("trace_sha256", "trace_receipt")):
        _hex(row.get(digest_key), "original_replicate_digest_invalid")
        if row[digest_key] != _digest(row[receipt_key]): raise CustodyError("original_replicate_digest_invalid")
    index = _object(row["index_receipt"], "original_index_receipt_invalid")
    expected_input = _input_receipt(projection, ORIGINAL_MEMPALACE_SERIALIZER)
    expected_queries = _digest([{"item_id": item["item_id"], "query_sha256": _query_digest(item["query_text"])} for item in sorted(projection["items"], key=lambda item: item["item_id"])])
    expected_outputs = _digest([{"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in sorted(row["trace_receipt"], key=lambda trace: trace["item_id"])])
    required_index = {"build_id", "fresh_build", "collection_identity", "index_identity_sha256", "cold_reopen", "call_contract", "input_coverage_sha256", "query_coverage_sha256", "output_coverage_sha256", "worker_physical_receipt", "coordinator_physical_receipt"}
    if set(index) != required_index or index["build_id"] != row["build_id"] or index["fresh_build"] is not True or index["cold_reopen"] is not True or index["call_contract"] != ORIGINAL_CALL_CONTRACT:
        raise CustodyError("original_index_receipt_invalid")
    if not isinstance(index["collection_identity"], str) or not index["collection_identity"].strip(): raise CustodyError("original_index_receipt_invalid")
    for key, expected in (("index_identity_sha256", None), ("input_coverage_sha256", _digest(expected_input["item_corpora"])), ("query_coverage_sha256", expected_queries), ("output_coverage_sha256", expected_outputs)):
        _hex(index[key], "original_index_receipt_invalid")
        if expected is not None and index[key] != expected: raise CustodyError("original_index_receipt_invalid")
    physical_ids = [f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in projection["corpora"] for candidate in corpus["candidates"]]
    expected_physical = {"physical_count": len(physical_ids), "physical_ids_sha256": _digest(sorted(physical_ids))}
    worker_raw, _worker_single = _validate_original_worker_physical_receipt(projection, index["worker_physical_receipt"])
    coordinator_raw, _coordinator_single = _validate_original_worker_physical_receipt(projection, index["coordinator_physical_receipt"])
    worker, coordinator = _joint_original_physical_receipts(worker_raw, coordinator_raw)
    if worker != coordinator or any(worker[key] != expected for key, expected in expected_physical.items()) or worker.get("hnsw_config") != ORIGINAL_HNSW_CONFIG:
        raise CustodyError("original_physical_receipt_invalid")
    embedding = _object(worker["embedding"], "original_physical_receipt_invalid")
    if set(embedding) != {"count", "dimension", "dtype", "float32_sha256"} or embedding.get("count") != len(physical_ids) or embedding.get("dimension") != 384 or embedding.get("dtype") != "float32": raise CustodyError("original_physical_receipt_invalid")
    _hex(embedding.get("float32_sha256"), "original_physical_receipt_invalid"); _hex(worker_raw.get("immutable_backend_sha256"), "original_physical_receipt_invalid"); _hex(worker.get("immutable_residual_backend_sha256"), "original_physical_receipt_invalid"); _hex(worker.get("sqlite_semantic_sha256"), "original_physical_receipt_invalid")
    # Validate raw graph evidence, not the logical projection which removes the
    # two direct-read-normalized SHA fields after joint handoff verification.
    graphs = worker_raw.get("graph_files")
    if not isinstance(graphs, list) or [entry.get("name") for entry in graphs if isinstance(entry, Mapping)] != list(ORIGINAL_GRAPH_NAMES): raise CustodyError("original_physical_receipt_invalid")
    graph_parents = set()
    for graph in graphs:
        graph = _object(graph, "original_physical_receipt_invalid")
        if set(graph) != {"name", "path", "bytes", "sha256"} or not isinstance(graph["name"], str) or not graph["name"] or not isinstance(graph["path"], str) or not graph["path"] or _int(graph["bytes"], "original_physical_receipt_invalid", positive=True) < 1: raise CustodyError("original_physical_receipt_invalid")
        parent, separator, basename = graph["path"].rpartition("/")
        if basename != graph["name"]: raise CustodyError("original_physical_receipt_invalid")
        graph_parents.add(f"{parent}{separator}")
        _hex(graph["sha256"], "original_physical_receipt_invalid")
    if len(graph_parents) != 1: raise CustodyError("original_physical_receipt_invalid")
    if worker["operational_delta"] != ORIGINAL_OPERATIONAL_DELTA: raise CustodyError("original_physical_receipt_invalid")
    if index["index_identity_sha256"] != _digest({"collection_identity": index["collection_identity"], "physical": worker_raw}): raise CustodyError("original_index_receipt_invalid")
    return row


def wrap_original_public_rankings(*, projection: Any, replicates: Sequence[Mapping[str, Any]], model_receipt: Mapping[str, Any], code_receipt: Mapping[str, Any]) -> dict[str, Any]:
    frozen = validate_candidate_projection(projection); model = _validate_model_receipt(model_receipt); code = _validate_code_receipt(code_receipt)
    if not isinstance(replicates, Sequence) or isinstance(replicates, (str, bytes)) or len(replicates) != 5: raise CustodyError("original_replicate_contract_invalid")
    records = [_original_replicate(frozen, row) for row in replicates]
    if len({row["build_id"] for row in records}) != 5: raise CustodyError("original_build_id_duplicate")
    value = {"schema": RANKING_SCHEMA, "arm_id": "original_public_product", "projection_sha256": canonical_sha256(frozen), "input_receipt": _input_receipt(frozen, ORIGINAL_MEMPALACE_SERIALIZER), "model_receipt": model, "method_receipt": ORIGINAL_METHOD, "source_receipt": PROTOCOL_SOURCE, "serializer_receipt": ORIGINAL_MEMPALACE_SERIALIZER, "code_receipt": code, "replicates": records}
    for field, receipt in (("input_sha256", "input_receipt"), ("model_sha256", "model_receipt"), ("method_sha256", "method_receipt"), ("source_commit_sha256", "source_receipt"), ("serializer_sha256", "serializer_receipt"), ("code_sha256", "code_receipt")):
        value[field] = _digest(value[receipt])
    value["artifact_sha256"] = _digest(value)
    # Reject an externally supplied five-run envelope at publication time too;
    # downstream scoring repeats this verification before custody is opened.
    return validate_frozen_ranking(value, projection=frozen)


def _validate_receipts(value: Mapping[str, Any], projection: Mapping[str, Any], *, arm_id: str) -> None:
    if value["projection_sha256"] != canonical_sha256(projection): raise CustodyError("ranking_projection_digest_invalid")
    for field, receipt in (("input_sha256", "input_receipt"), ("model_sha256", "model_receipt"), ("method_sha256", "method_receipt"), ("source_commit_sha256", "source_receipt"), ("serializer_sha256", "serializer_receipt"), ("code_sha256", "code_receipt")):
        _hex(value.get(field), "ranking_artifact_digest_invalid")
        if _digest(value[receipt]) != value[field]: raise CustodyError("ranking_receipt_digest_mismatch")
    _validate_model_receipt(value["model_receipt"]); _validate_code_receipt(value["code_receipt"])
    if value["source_receipt"] != PROTOCOL_SOURCE: raise CustodyError("ranking_source_receipt_invalid")
    serializer = ORIGINAL_MEMPALACE_SERIALIZER if arm_id == "original_public_product" else CURRENT_SERIALIZER
    method = ORIGINAL_METHOD if arm_id == "original_public_product" else _arm_method(arm_id)
    if value["serializer_receipt"] != serializer or value["method_receipt"] != method or value["input_receipt"] != _input_receipt(projection, serializer): raise CustodyError("ranking_contract_receipt_invalid")


def _validate_fcd1(ledger: Any, trace: Any, candidates: Sequence[AuthorizedRetrievalCandidate], weights: Mapping[str, float], ranked: Sequence[str]) -> None:
    """Replay the complete benchmark-only FCD1 receipt without plaintext.

    This is deliberately local rather than the LoCoMo helper: all three frozen
    arms have different valid weight vectors while the FCD1 structure is shared.
    """
    ledger = _object(ledger, "fcd1_ledger_invalid"); trace = _object(trace, "fcd1_trace_invalid")
    fields = {"schema", "input_sha256", "authorization_sha256", "view_top_50", "view_top_50_sha256", "view_full_order", "view_order_sha256", "fused_top_50", "checkpoint_tie_group_semantics", "checkpoint_tie_groups"}
    base_trace = {"schema", "encoder_identity", "weights", "rrf_k", "query_sha256", "input_sha256", "view_digests", "selected", "fcd1_diagnostic_ledger"}
    permitted = base_trace | ({"aerp5_fixed_p5"} if weights == P5_EXPERT_WEIGHTS else set())
    if set(trace) != permitted or trace.get("schema") != "aerp2-product-six-view-v1" or not isinstance(trace.get("encoder_identity"), str) or not trace["encoder_identity"].strip() or set(ledger) != fields or ledger.get("schema") != "aerp3-fcd1-replay-ledger-v1" or ledger.get("input_sha256") != trace.get("input_sha256") or trace.get("weights") != dict(weights) or trace.get("rrf_k") != SixViewRanker.rrf_k:
        raise CustodyError("fcd1_ledger_contract_invalid")
    candidates = sorted(candidates, key=lambda candidate: candidate.ranking_key)
    ids = [candidate.source_event_id for candidate in candidates]
    keys = {candidate.source_event_id: candidate.ranking_key for candidate in candidates}
    key_hash = {identifier: hashlib.sha256(key.encode("utf-8")).hexdigest() for identifier, key in keys.items()}
    key_order = {identifier: number for number, identifier in enumerate(ids, 1)}
    names = set(SIX_VIEW_WEIGHTS); top_count = min(50, len(ids))
    expected_input = _digest([{"ranking_key": candidate.ranking_key, "raw_sha256": hashlib.sha256(candidate.raw_text.encode("utf-8")).hexdigest(), "observation_sha256": hashlib.sha256(candidate.observation.encode("utf-8")).hexdigest(), "checkpoint": candidate.checkpoint_key.strip(), "policy": candidate.policy_tuple, "scene_time_sort": candidate.chronological_order_key[0]} for candidate in candidates])
    if trace["input_sha256"] != expected_input: raise CustodyError("fcd1_input_replay_invalid")
    views = ledger.get("view_top_50"); orders = ledger.get("view_full_order"); order_hashes = ledger.get("view_order_sha256"); top_hashes = ledger.get("view_top_50_sha256")
    if not all(isinstance(value, Mapping) and set(value) == names for value in (views, orders, order_hashes, top_hashes)):
        raise CustodyError("fcd1_view_schema_invalid")
    for name in names:
        full = orders[name]; top = views[name]
        if not isinstance(full, list) or len(full) != len(ids) or len(set(full)) != len(full) or set(full) != set(ids) or order_hashes[name] != _digest([key_hash[identifier] for identifier in full]):
            raise CustodyError("fcd1_view_order_invalid")
        if trace.get("view_digests", {}).get(name) != _digest([keys[identifier] for identifier in full]): raise CustodyError("fcd1_view_digest_invalid")
        if not isinstance(top, list) or len(top) != top_count or top_hashes[name] != _digest(top): raise CustodyError("fcd1_view_top_invalid")
        top_ids = []
        for number, record in enumerate(top, 1):
            record = _object(record, "fcd1_view_row_invalid")
            if set(record) != {"source_event_id", "ranking_key_sha256", "ranking_key_order", "rank", "score"} or record.get("source_event_id") not in keys or record.get("ranking_key_sha256") != key_hash[record["source_event_id"]] or record.get("ranking_key_order") != key_order[record["source_event_id"]] or record.get("rank") != number:
                raise CustodyError("fcd1_view_row_invalid")
            _finite(record.get("score"), "fcd1_view_score_invalid"); top_ids.append(record["source_event_id"])
        if top_ids != full[:top_count] or top_ids != [record["source_event_id"] for record in sorted(top, key=lambda record: (-float(record["score"]), int(record["ranking_key_order"])) )]: raise CustodyError("fcd1_view_semantics_invalid")
    groups = ledger.get("checkpoint_tie_groups")
    if ledger.get("checkpoint_tie_group_semantics") != "checkpoint_policy_rollup" or not isinstance(groups, list) or not groups: raise CustodyError("fcd1_checkpoint_schema_invalid")
    candidate_by_id = {candidate.source_event_id: candidate for candidate in candidates}; group_score = {}; authorization = []; group_members = []
    for group in groups:
        group = _object(group, "fcd1_checkpoint_group_invalid")
        if set(group) != {"group_id", "checkpoint_sha256", "policy_sha256", "checkpoint_score", "member_count", "chronological_members"}: raise CustodyError("fcd1_checkpoint_group_invalid")
        members = group.get("chronological_members")
        if not isinstance(members, list) or not members or group.get("member_count") != len(members) or group.get("policy_sha256") != _digest([]): raise CustodyError("fcd1_checkpoint_group_invalid")
        checkpoint = group.get("checkpoint_sha256"); policy = group.get("policy_sha256")
        _hex(checkpoint, "fcd1_checkpoint_group_invalid"); _hex(policy, "fcd1_checkpoint_group_invalid")
        if group.get("group_id") != "group:" + _digest([checkpoint, policy]): raise CustodyError("fcd1_checkpoint_group_invalid")
        score = _finite(group.get("checkpoint_score"), "fcd1_checkpoint_group_invalid")
        expected_members = []
        for member in members:
            member = _object(member, "fcd1_checkpoint_member_invalid"); identifier = member.get("source_event_id")
            if set(member) != {"source_event_id", "ranking_key_sha256"} or identifier not in candidate_by_id or member.get("ranking_key_sha256") != key_hash[identifier] or checkpoint != hashlib.sha256(candidate_by_id[identifier].checkpoint_key.encode("utf-8")).hexdigest(): raise CustodyError("fcd1_checkpoint_member_invalid")
            expected_members.append(identifier); group_members.append(identifier); group_score[identifier] = score; authorization.append({"ranking_key_sha256": key_hash[identifier], "policy_sha256": policy})
        ordered_members = [candidate.source_event_id for candidate in sorted((candidate_by_id[identifier] for identifier in expected_members), key=lambda candidate: candidate.chronological_order_key)]
        if expected_members != ordered_members: raise CustodyError("fcd1_checkpoint_chronology_invalid")
    if len(group_members) != len(set(group_members)) or set(group_members) != set(ids) or ledger.get("authorization_sha256") != _digest(sorted(authorization, key=lambda record: record["ranking_key_sha256"])): raise CustodyError("fcd1_authorization_invalid")
    checkpoint_rows = {record["source_event_id"]: record for record in views["checkpoint_dense"]}
    if any(not math.isclose(float(record["score"]), group_score[identifier], rel_tol=0.0, abs_tol=1e-15) for identifier, record in checkpoint_rows.items()): raise CustodyError("fcd1_checkpoint_score_invalid")
    fused = ledger.get("fused_top_50")
    if not isinstance(fused, list) or len(fused) != top_count: raise CustodyError("fcd1_fusion_schema_invalid")
    fused_ids = []
    for number, record in enumerate(fused, 1):
        record = _object(record, "fcd1_fusion_row_invalid"); identifier = record.get("source_event_id")
        required = {"source_event_id", "ranking_key_sha256", "ranking_key_order", "rank", "final_rrf", "component_ranks", "component_rank_receipts", "contributions"}
        if set(record) != required or identifier not in keys or record.get("ranking_key_sha256") != key_hash[identifier] or record.get("ranking_key_order") != key_order[identifier] or record.get("rank") != number: raise CustodyError("fcd1_fusion_row_invalid")
        component = _object(record.get("component_ranks"), "fcd1_fusion_row_invalid"); contribution = _object(record.get("contributions"), "fcd1_fusion_row_invalid")
        if set(component) != set(weights) or set(contribution) != set(weights): raise CustodyError("fcd1_fusion_weights_invalid")
        expected = {}
        for name, component_rank in component.items():
            _int(component_rank, "fcd1_component_rank_invalid", positive=True)
            if component_rank > len(ids) or orders[name][component_rank - 1] != identifier: raise CustodyError("fcd1_component_rank_invalid")
            expected[name] = float(weights[name]) / (SixViewRanker.rrf_k + component_rank)
            if not math.isclose(_finite(contribution[name], "fcd1_contribution_invalid"), expected[name], rel_tol=0.0, abs_tol=1e-15): raise CustodyError("fcd1_contribution_invalid")
        receipts = record.get("component_rank_receipts")
        if not isinstance(receipts, list) or len(receipts) != len(weights) or {entry.get("view") for entry in receipts if isinstance(entry, Mapping)} != set(weights): raise CustodyError("fcd1_component_receipt_invalid")
        for entry in receipts:
            entry = _object(entry, "fcd1_component_receipt_invalid")
            if set(entry) != {"view", "view_order_sha256", "ranking_key_sha256", "rank"} or entry["view_order_sha256"] != order_hashes[entry["view"]] or entry["ranking_key_sha256"] != key_hash[identifier] or entry["rank"] != component[entry["view"]]: raise CustodyError("fcd1_component_receipt_invalid")
        if not math.isclose(_finite(record.get("final_rrf"), "fcd1_fusion_score_invalid"), math.fsum(expected.values()), rel_tol=0.0, abs_tol=1e-15): raise CustodyError("fcd1_fusion_score_invalid")
        fused_ids.append(identifier)
    if fused_ids != [record["source_event_id"] for record in sorted(fused, key=lambda record: (-float(record["final_rrf"]), int(record["ranking_key_order"])))] or list(ranked) != fused_ids[:len(ranked)]: raise CustodyError("fcd1_fusion_order_invalid")
    selected = trace.get("selected")
    if not isinstance(selected, list) or len(selected) != len(ids): raise CustodyError("fcd1_selected_schema_invalid")
    for selected_row, fused_row in zip(selected[:top_count], fused):
        replay = dict(fused_row)
        for key in ("rank", "ranking_key_order", "component_rank_receipts"): replay.pop(key)
        if selected_row != replay: raise CustodyError("fcd1_selected_prefix_invalid")
    rank_by_view = {
        name: {identifier: rank for rank, identifier in enumerate(order, 1)}
        for name, order in orders.items()
    }
    full_selected = []
    for identifier in ids:
        ranks = {name: rank_by_view[name][identifier] for name in weights}
        contributions = {name: float(weights[name]) / (SixViewRanker.rrf_k + ranks[name]) for name in weights}
        full_selected.append({"source_event_id": identifier, "ranking_key_sha256": key_hash[identifier], "final_rrf": math.fsum(contributions.values()), "component_ranks": ranks, "contributions": contributions})
    full_selected.sort(key=lambda record: (-record["final_rrf"], key_order[record["source_event_id"]]))
    if selected != full_selected: raise CustodyError("fcd1_selected_full_replay_invalid")
    if weights == P5_EXPERT_WEIGHTS:
        p5 = _object(trace.get("aerp5_fixed_p5"), "fcd1_p5_receipt_invalid")
        expected_config = FixedP5Policy._config(SixViewRanker.rrf_k)
        if set(p5) != {"schema", "policy", "config", "config_sha256", "effective_weights", "final_ranking_sha256"} or p5.get("schema") != "aerp5-fixed-p5-v1" or p5.get("policy") != "fixed_p5" or p5.get("config") != expected_config or p5.get("effective_weights") != dict(weights) or p5.get("config_sha256") != _digest(expected_config) or p5.get("final_ranking_sha256") != _digest([key_hash[record["source_event_id"]] for record in selected]): raise CustodyError("fcd1_p5_receipt_invalid")


def _validate_rows(rows: Any, projection: Mapping[str, Any], serializer: Mapping[str, Any], *, confidence_required: bool, trace: Any, arm_id: str | None = None, encoder_identity: str | None = None) -> None:
    if not isinstance(rows, list) or not isinstance(trace, list): raise CustodyError("ranking_rows_invalid")
    corpora = {row["corpus_id"]: row for row in projection["corpora"]}; items = {row["item_id"]: row for row in projection["items"]}; seen = set(); trace_by_item = {}
    for entry in trace:
        entry = _object(entry, "ranking_trace_row_invalid")
        expected_trace = {"item_id", "query_sha256", "candidate_input_sha256", "ranked_count", "ranking_sha256", "ranker_trace_sha256", "ranking_trace"} if confidence_required else {"item_id", "query_sha256", "candidate_input_sha256", "ranked_count", "ranking_sha256"}
        if set(entry) != expected_trace: raise CustodyError("ranking_trace_schema_invalid")
        for key in expected_trace - {"ranked_count", "ranking_trace"}: _hex(entry.get(key), "ranking_trace_schema_invalid")
        _int(entry.get("ranked_count"), "ranking_trace_schema_invalid", positive=True)
        if entry["item_id"] in trace_by_item: raise CustodyError("ranking_trace_duplicate")
        trace_by_item[entry["item_id"]] = entry
    for row in rows:
        row = _object(row, "ranking_row_invalid")
        required = {"item_id", "query_sha256", "candidate_input_sha256", "ranked_message_ids", "retrieved_conversation_ids", "confidence", "confidence_receipt"}
        if set(row) != required or row.get("item_id") not in items or row["item_id"] in seen: raise CustodyError("ranking_row_schema_invalid")
        seen.add(row["item_id"]); item = items[row["item_id"]]; corpus = corpora[item["corpus_id"]]; allowed = {candidate["message_id"]: candidate for candidate in corpus["candidates"]}
        if row["query_sha256"] != _query_digest(item["query_text"]) or row["candidate_input_sha256"] != _candidate_input(corpus, serializer): raise CustodyError("ranking_row_input_mismatch")
        ids = row["ranked_message_ids"]
        if not isinstance(ids, list) or len(ids) != min(10, len(allowed)) or len(set(ids)) != len(ids) or set(ids) - set(allowed): raise CustodyError("ranking_top10_invalid")
        conversations = list(dict.fromkeys(allowed[item_id]["opaque_conversation_id"] for item_id in ids))
        if row["retrieved_conversation_ids"] != conversations: raise CustodyError("ranking_conversations_invalid")
        if confidence_required:
            receipt = _object(row["confidence_receipt"], "ranking_confidence_receipt_invalid")
            if set(receipt) != {"contract", "top_two_scores"} or receipt["contract"] != CONFIDENCE_CONTRACT or _margin(receipt["top_two_scores"]) != _finite(row["confidence"], "ranking_confidence_invalid"):
                raise CustodyError("ranking_confidence_invalid")
        elif row["confidence"] is not None or row["confidence_receipt"] is not None: raise CustodyError("ranking_confidence_contract_invalid")
        entry = trace_by_item.get(row["item_id"])
        if entry is None or entry["query_sha256"] != row["query_sha256"] or entry["candidate_input_sha256"] != row["candidate_input_sha256"] or entry["ranked_count"] != len(ids) or entry["ranking_sha256"] != _digest(ids): raise CustodyError("ranking_trace_binding_invalid")
        if confidence_required:
            trace_value = _object(entry["ranking_trace"], "ranking_trace_schema_invalid")
            if entry["ranker_trace_sha256"] != _digest(trace_value) or trace_value.get("query_sha256") != row["query_sha256"] or trace_value.get("encoder_identity") != encoder_identity: raise CustodyError("ranking_trace_binding_invalid")
            if arm_id not in CURRENT_ARMS: raise CustodyError("ranking_trace_arm_invalid")
            candidates = authorized_candidates(projection)[item["corpus_id"]]
            _validate_fcd1(trace_value.get("fcd1_diagnostic_ledger"), trace_value, candidates, _arm_method(arm_id)["weights"], ids)
    if seen != set(items) or set(trace_by_item) != set(items): raise CustodyError("ranking_item_coverage_invalid")


def validate_frozen_ranking(value: Any, *, projection: Any) -> dict[str, Any]:
    frozen = validate_candidate_projection(projection); row = _object(value, "ranking_artifact_invalid"); arm_id = row.get("arm_id")
    common = {"schema", "arm_id", "projection_sha256", "input_receipt", "input_sha256", "model_receipt", "model_sha256", "method_receipt", "method_sha256", "source_receipt", "source_commit_sha256", "serializer_receipt", "serializer_sha256", "code_receipt", "code_sha256", "artifact_sha256"}
    if arm_id in CURRENT_ARMS: required = common | {"trace_receipt", "trace_sha256", "rankings"}
    elif arm_id == "original_public_product": required = common | {"replicates"}
    else: raise CustodyError("ranking_arm_unknown")
    if set(row) != required or row.get("schema") != RANKING_SCHEMA: raise CustodyError("ranking_artifact_schema_invalid")
    _validate_receipts(row, frozen, arm_id=arm_id); _hex(row.get("artifact_sha256"), "ranking_artifact_digest_invalid")
    if _digest({key: item for key, item in row.items() if key != "artifact_sha256"}) != row["artifact_sha256"]: raise CustodyError("ranking_artifact_digest_mismatch")
    if arm_id in CURRENT_ARMS:
        _hex(row.get("trace_sha256"), "ranking_artifact_digest_invalid")
        if _digest(row["trace_receipt"]) != row["trace_sha256"]: raise CustodyError("ranking_receipt_digest_mismatch")
        _validate_rows(row["rankings"], frozen, CURRENT_SERIALIZER, confidence_required=True, trace=row["trace_receipt"], arm_id=arm_id, encoder_identity=row["model_receipt"]["encoder_identity"])
    else:
        if not isinstance(row["replicates"], list) or len(row["replicates"]) != 5: raise CustodyError("original_replicate_contract_invalid")
        builds = set(); collections = set(); indexes = set()
        for replica in row["replicates"]:
            replica = _original_replicate(frozen, replica)
            if replica["build_id"] in builds: raise CustodyError("original_build_id_duplicate")
            collection = replica["index_receipt"]["collection_identity"]; identity = replica["index_receipt"]["index_identity_sha256"]
            if collection in collections or identity in indexes: raise CustodyError("original_index_identity_duplicate")
            builds.add(replica["build_id"]); collections.add(collection); indexes.add(identity); _validate_rows(replica["rankings"], frozen, ORIGINAL_MEMPALACE_SERIALIZER, confidence_required=False, trace=replica["trace_receipt"])
    return row
