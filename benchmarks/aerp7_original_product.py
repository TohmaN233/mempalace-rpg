"""AERP-7 exact original MemPalace public-product worker primitives.

This module deliberately contains no ConvoMem source/custody loading.  Its only
input is the already validated candidate-safe projection.  The formal executor
owns process isolation, manifests, publication and the eventual real-data run;
this module owns the product-call and persisted-index contract used by that
executor.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from benchmarks import aerp5_product_paired_locomo as v1
from benchmarks import aerp5_product_paired_locomo_v2 as v2
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks import aerp7_convomem_rank as rank
from benchmarks.aerp7_convomem_confirmation import (
    CANDIDATE_PROJECTION_REFERENCE_SCHEMA,
    CustodyError,
)
from benchmarks import aerp7_original_core as original_core


WING = "aerp7-convomem"
PHYSICAL_SEPARATOR = "::aerp7::"
TOP_K = 10
SYNTHETIC_INJECTED = "synthetic_injected"
LIVE_PINNED = "live_pinned"
PUBLIC_UPSERT_MEASUREMENT = "public_upsert_document_request_ledger_native_internal_calls_unobservable"
PUBLIC_SEARCH_MEASUREMENT = "public_search_request_ledger_native_internal_calls_unobservable"
DRAFT_SCHEMA = "aerp7-original-product-worker-draft-v1"
GENERIC_DRAFT_SCHEMA = "aerp-original-product-generic-worker-draft-v1"
CLOCK_RECEIPT_SCHEMA = "aerp7-original-product-clock-receipt-v1"
STREAM_DRAFT_SCHEMA = "aerp7-original-product-worker-draft-stream-v1"
STREAM_NAMESPACE_SCHEMA = "aerp7-original-product-identity-namespace-stream-v1"
ORIGINAL_REPLICATE_REFERENCE_SCHEMA = "aerp7-original-product-replicate-reference-v1"
ORIGINAL_REPLICATE_STORE_SCHEMA = "aerp7-original-product-replicate-store-v1"
ORIGINAL_REPLICATE_READY_SCHEMA = "aerp7-original-product-replicate-ready-v1"
ORIGINAL_STREAM_TELEMETRY_SCHEMA = "aerp7-original-product-telemetry-reference-v1"
ORIGINAL_ARTIFACT_REFERENCE_SCHEMA = "aerp7-original-product-artifact-reference-v1"
ORIGINAL_ARTIFACT_READY_SCHEMA = "aerp7-original-product-artifact-ready-v1"
CANDIDATE_INDEX_LIFECYCLE_SCHEMA = "aerp7-original-candidate-index-lifecycle-v1"
# Chroma's public ``get`` API is paginated by ``limit``/``offset``.  Keep this
# finite and versioned at the product seam: an omitted limit would silently
# reintroduce a full-census ids+embedding allocation.
ORIGINAL_CHROMA_AUDIT_BATCH_SIZE = 1024
_LIVE_CAPABILITIES: set[int] = set()


class OriginalProductError(RuntimeError):
    """Fail-closed error for an original-product contract violation."""


@dataclass(frozen=True)
class CandidateProjectionReference:
    """The only candidate input carried across the original worker boundary.

    The projection itself is intentionally absent.  ``projection_path`` and
    ``ready_path`` are fixed relative names under ``bundle_path`` so a worker
    cannot be redirected to a custody file.  The raw digest is re-read before
    every cursor pass; the canonical digest is bound through READY and the
    reference on every pass.
    """

    schema: str
    bundle_path: str
    projection_path: str
    ready_path: str
    generation_id: str
    projection_raw_sha256: str
    projection_canonical_sha256: str
    dataset: Mapping[str, Any]
    query_count: int
    candidate_text_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "bundle_path": self.bundle_path,
            "projection_path": self.projection_path,
            "ready_path": self.ready_path,
            "generation_id": self.generation_id,
            "projection_raw_sha256": self.projection_raw_sha256,
            "projection_canonical_sha256": self.projection_canonical_sha256,
            "dataset": dict(self.dataset),
            "query_count": self.query_count,
            "candidate_text_count": self.candidate_text_count,
        }


class ResourceObserver(Protocol):
    """A real observer supplied by the executor, never a fabricated estimate."""

    def checkpoint(self, phase: str) -> None: ...

    def receipt(self) -> Mapping[str, Any]: ...


class IndexAuditor(Protocol):
    def __call__(self, *, palace_path: Path, expected_namespace: Mapping[str, Any]) -> Mapping[str, Any]: ...


class LiveOriginalObserver:
    """Dataset-neutral worker-side resource observer for the public product."""
    def __init__(self, *, palace_path: Path, provider: Mapping[str, Any], denominators: Mapping[str, int], corpus_count: int) -> None:
        self._palace_path = palace_path; self._provider = dict(provider); self._denominators = dict(denominators); self._corpus_count = int(corpus_count); self._phases: list[str] = []
    def checkpoint(self, phase: str) -> None:
        expected = ["before_ingest", "after_ingest", "after_cold_close", "after_queries"]
        if len(self._phases) >= len(expected) or phase != expected[len(self._phases)]: raise RuntimeError("original product observer lifecycle invalid")
        self._phases.append(phase)
    def receipt(self) -> dict[str, Any]:
        if self._phases != ["before_ingest", "after_ingest", "after_cold_close", "after_queries"]: raise RuntimeError("original product observer lifecycle incomplete")
        storage = sum(path.stat().st_size for path in self._palace_path.rglob("*") if path.is_file() and not path.is_symlink())
        if storage <= 0: raise RuntimeError("original product resource observation incomplete")
        return {"peak_rss_bytes": 0, "storage_bytes": int(storage), "passage_embedding": {"calls": self._corpus_count, "texts": int(self._denominators["candidate_text_count"]), "measurement": PUBLIC_UPSERT_MEASUREMENT}, "query_embedding": {"calls": int(self._denominators["query_count"]), "texts": int(self._denominators["query_count"]), "measurement": PUBLIC_SEARCH_MEASUREMENT}, "provider": self._provider}


@dataclass(frozen=True)
class _LivePinnedCapability:
    """Opaque in-process provenance token minted only by the live context."""

    receipt: Mapping[str, Any]


@dataclass(frozen=True)
class OriginalProductSeams:
    """Injectable seams used only for synthetic tests/rehearsals.

    Direct construction is always synthetic.  ``pinned_live_original_product``
    is the only supported constructor of a formal-eligible seam, and installs an
    unforgeable-in-process capability alongside the pinned runtime receipt.
    """

    palace: Any
    searcher: Any
    reset_backends: Callable[[Path], Mapping[str, Any]]
    auditor: IndexAuditor
    provenance: str = SYNTHETIC_INJECTED
    encoder: Any | None = None
    _live_capability: _LivePinnedCapability | None = None


@dataclass(frozen=True)
class OriginalProductWorkerDraft:
    """Worker-only result; stream drafts carry persistent refs, never arrays."""

    projection: Mapping[str, Any]
    namespace: Mapping[str, Any]
    replicate_without_coordinator_audit: Mapping[str, Any]
    worker_physical_receipt: Mapping[str, Any]
    telemetry: Mapping[str, Any]
    # Streaming drafts carry this immutable public reference instead of an
    # in-memory projection.  The legacy projection field remains for the
    # already-frozen v1 seam and is deliberately None for stream drafts.
    candidate_reference: Mapping[str, Any] | None = None


def _digest(value: Any) -> str:
    return rank.canonical_sha256(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


_REFERENCE_KEYS = frozenset({
    "schema", "bundle_path", "projection_path", "ready_path", "generation_id",
    "projection_raw_sha256", "projection_canonical_sha256", "dataset",
    "query_count", "candidate_text_count",
})


def _hex(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise OriginalProductError(f"{label} is not a SHA-256 digest")
    return value


def _reference_token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OriginalProductError(f"{label} must be a non-empty string")
    return value


def _reference_file(path: Path, code: str) -> tuple[int, int]:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise OriginalProductError(code) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise OriginalProductError(code)
    if os.name == "nt" and bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
        raise OriginalProductError(code)
    return metadata.st_dev, metadata.st_ino


def _reference_bundle(path: Path, *, expected_root: Path | None = None) -> Path:
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise OriginalProductError("candidate reference bundle missing") from exc
    if not stat.S_ISDIR(metadata.st_mode) or path.is_symlink():
        raise OriginalProductError("candidate reference bundle is not a private directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise OriginalProductError("candidate reference bundle cannot resolve") from exc
    if expected_root is not None:
        try:
            root = Path(expected_root).resolve(strict=True)
        except OSError as exc:
            raise OriginalProductError("candidate reference configured root is unavailable") from exc
        if resolved != root:
            raise OriginalProductError("candidate reference bundle escaped configured root")
    # A publisher marker means the bundle is between generations; accepting it
    # would make the reference's path binding non-repeatable.
    if (resolved / ".aerp7-publishing").exists():
        raise OriginalProductError("candidate reference bundle is not READY")
    return resolved


def _reference_paths(reference: Mapping[str, Any], *, expected_root: Path | None = None) -> tuple[Path, Path, Path]:
    bundle = _reference_bundle(Path(str(reference["bundle_path"])), expected_root=expected_root)
    projection_name = reference.get("projection_path")
    ready_name = reference.get("ready_path")
    if projection_name != "projection.json" or ready_name != "READY.json":
        raise OriginalProductError("candidate reference paths are not fixed")
    projection, ready = bundle / projection_name, bundle / ready_name
    # Resolve only after enforcing the fixed relative names.  This prevents a
    # symlinked projection/READY from redirecting a candidate worker.
    if projection.resolve(strict=True) != projection or ready.resolve(strict=True) != ready:
        raise OriginalProductError("candidate reference file alias")
    _reference_file(projection, "candidate reference projection is unavailable")
    _reference_file(ready, "candidate reference READY is unavailable")
    if _reference_file(projection, "candidate reference projection is unavailable") == _reference_file(ready, "candidate reference READY is unavailable"):
        raise OriginalProductError("candidate reference projection/READY alias")
    return bundle, projection, ready


def _stream_file_sha256(path: Path, code: str) -> tuple[tuple[int, int], str]:
    """Hash a regular file without materializing its bytes."""
    before = _reference_file(path, code)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise OriginalProductError(code) from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != before or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode):
            raise OriginalProductError(code)
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if _reference_file(path, code) != before:
        raise OriginalProductError("candidate reference file drift")
    return before, digest.hexdigest()


def _reference_ijson():
    try:
        import ijson
    except ImportError as exc:  # pragma: no cover - environment failure
        raise OriginalProductError("candidate reference streaming JSON dependency unavailable") from exc
    return ijson


def _reference_root_value(path: Path, prefix: str, code: str) -> Any:
    parser = _reference_ijson()
    try:
        with path.open("rb") as stream:
            values = parser.items(stream, prefix)
            value = next(values)
            try:
                next(values)
            except StopIteration:
                return value
            raise OriginalProductError(code)
    except (OSError, StopIteration, ValueError) as exc:
        if isinstance(exc, OriginalProductError):
            raise
        raise OriginalProductError(code) from exc


def _validate_reference_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, CandidateProjectionReference):
        value = value.as_dict()
    if not isinstance(value, Mapping) or set(value) != _REFERENCE_KEYS:
        raise OriginalProductError("candidate reference schema is malformed")
    if value.get("schema") != CANDIDATE_PROJECTION_REFERENCE_SCHEMA:
        raise OriginalProductError("candidate reference schema is malformed")
    bundle_path = value.get("bundle_path")
    if not isinstance(bundle_path, str) or not Path(bundle_path).is_absolute():
        raise OriginalProductError("candidate reference bundle path must be absolute")
    for key in ("projection_path", "ready_path"):
        if value.get(key) not in ("projection.json", "READY.json"):
            raise OriginalProductError("candidate reference path is not fixed")
    _reference_token(value.get("generation_id"), "candidate reference generation")
    _hex(value.get("projection_raw_sha256"), "candidate reference raw digest")
    _hex(value.get("projection_canonical_sha256"), "candidate reference canonical digest")
    dataset = value.get("dataset")
    if not isinstance(dataset, Mapping) or not dataset:
        raise OriginalProductError("candidate reference dataset is malformed")
    for key, digest in dataset.items():
        if not isinstance(key, str) or not key.strip():
            raise OriginalProductError("candidate reference dataset is malformed")
        _hex(digest, "candidate reference dataset digest")
    for key in ("query_count", "candidate_text_count"):
        count = value.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise OriginalProductError("candidate reference denominator is malformed")
    return dict(value)


def _verify_candidate_reference_files(reference: Mapping[str, Any], *, expected_root: Path | None = None) -> dict[str, Any]:
    """Verify only the public projection/READY pair, never custody."""
    value = _validate_reference_payload(reference)
    bundle, projection_path, ready_path = _reference_paths(value, expected_root=expected_root)
    _ready_identity, ready_sha = _stream_file_sha256(ready_path, "candidate reference READY is unavailable")
    try:
        ready_raw = ready_path.read_bytes()
        ready = json.loads(ready_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OriginalProductError("candidate reference READY is invalid") from exc
    try:
        ready = confirmation._candidate_ready(ready)
    except Exception as exc:
        raise OriginalProductError("candidate reference READY is invalid") from exc
    _projection_identity, projection_sha = _stream_file_sha256(projection_path, "candidate reference projection is unavailable")
    if projection_sha != value["projection_raw_sha256"] or projection_sha != ready["projection"]["raw_sha256"]:
        raise OriginalProductError("candidate reference raw digest drift")
    if ready["generation_id"] != value["generation_id"] or ready["projection"]["canonical_sha256"] != value["projection_canonical_sha256"]:
        raise OriginalProductError("candidate reference canonical/generation binding drift")
    dataset = _reference_root_value(projection_path, "dataset", "candidate reference dataset unavailable")
    if dataset != value["dataset"]:
        raise OriginalProductError("candidate reference dataset drift")
    schema = _reference_root_value(projection_path, "schema", "candidate reference schema unavailable")
    if schema != "aerp7-convomem-candidate-projection-v3":
        raise OriginalProductError("candidate reference projection schema drift")
    # A second READY read closes the READY-side TOCTOU window.  The projection
    # file is independently rehashed on every cursor pass below.
    _ready_identity_after, ready_sha_after = _stream_file_sha256(ready_path, "candidate reference READY drift")
    if ready_sha_after != ready_sha:
        raise OriginalProductError("candidate reference READY drift")
    return {**value, "bundle_path": str(bundle)}


def candidate_projection_reference(
    *,
    bundle_path: Path | str,
    generation_id: str,
    projection_raw_sha256: str,
    projection_canonical_sha256: str,
    dataset: Mapping[str, Any],
    query_count: int,
    candidate_text_count: int,
    verify: bool = True,
) -> dict[str, Any]:
    """Create the exact public reference consumed by a streaming worker."""
    value = {
        "schema": CANDIDATE_PROJECTION_REFERENCE_SCHEMA,
        "bundle_path": str(Path(bundle_path).resolve()),
        "projection_path": "projection.json",
        "ready_path": "READY.json",
        "generation_id": generation_id,
        "projection_raw_sha256": projection_raw_sha256,
        "projection_canonical_sha256": projection_canonical_sha256,
        "dataset": dict(dataset),
        "query_count": query_count,
        "candidate_text_count": candidate_text_count,
    }
    if verify:
        value = _verify_candidate_reference_files(value)
    else:
        value = _validate_reference_payload(value)
    return value


def validate_candidate_projection_reference(value: Any, *, expected_bundle_root: Path | None = None, verify: bool = True) -> dict[str, Any]:
    """Validate a persisted reference and, by default, re-read its public files."""
    normalized = _validate_reference_payload(value)
    return _verify_candidate_reference_files(normalized, expected_root=expected_bundle_root) if verify else normalized


def serialize_candidate_projection_reference(value: Any) -> bytes:
    normalized = validate_candidate_projection_reference(value, verify=False)
    return _canonical_bytes(normalized)


def load_candidate_projection_reference(path: Path | str, *, expected_bundle_root: Path | None = None) -> dict[str, Any]:
    target = Path(path)
    try:
        raw = target.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OriginalProductError("candidate reference file is invalid") from exc
    if _canonical_bytes(parsed) != raw:
        raise OriginalProductError("candidate reference file is not canonical")
    return validate_candidate_projection_reference(parsed, expected_bundle_root=expected_bundle_root)


def persist_candidate_projection_reference(path: Path | str, value: Any) -> dict[str, Any]:
    target = Path(path)
    payload = serialize_candidate_projection_reference(value)
    try:
        with target.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise OriginalProductError("candidate reference publication failed") from exc
    return validate_candidate_projection_reference(json.loads(payload.decode("utf-8")))


def _validate_stream_corpus_row(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OriginalProductError("candidate reference corpus is malformed")
    row = dict(value)
    expected = {"corpus_id", "declared_context_size", "actual_conversation_count", "actual_message_count", "candidates"}
    if set(row) != expected or not isinstance(row["candidates"], list) or not row["candidates"]:
        raise OriginalProductError("candidate reference corpus is malformed")
    _reference_token(row.get("corpus_id"), "candidate reference corpus id")
    candidates = row["candidates"]
    seen: set[str] = set()
    for order, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping) or set(candidate) != {"message_id", "opaque_conversation_id", "conversation_order", "message_order", "corpus_order", "speaker", "text"}:
            raise OriginalProductError("candidate reference candidate is malformed")
        message_id = _reference_token(candidate.get("message_id"), "candidate reference message id")
        if message_id in seen or candidate.get("corpus_order") != order:
            raise OriginalProductError("candidate reference candidate ordering drift")
        seen.add(message_id)
        _reference_token(candidate.get("opaque_conversation_id"), "candidate reference conversation id")
        _reference_token(candidate.get("speaker"), "candidate reference speaker")
        if not isinstance(candidate.get("text"), str) or not candidate["text"]:
            raise OriginalProductError("candidate reference text is malformed")
    if len(seen) != row["actual_message_count"]:
        raise OriginalProductError("candidate reference candidate count drift")
    return row


def _stream_corpora(reference: Mapping[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield one candidate corpus at a time in exact product sort order.

    The temporary key/payload table is bounded to the persisted projection
    stream and is removed before the worker returns; no projection object is
    embedded in the draft.
    """
    checked = validate_candidate_projection_reference(reference)
    parser = _reference_ijson()
    with tempfile.TemporaryDirectory(prefix=".aerp7-corpus-sort-", dir=Path(checked["bundle_path"]).parent) as temporary:
        connection = sqlite3.connect(Path(temporary) / "sort.sqlite")
        try:
            connection.execute("CREATE TABLE corpus (corpus_id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
            with Path(checked["bundle_path"], checked["projection_path"]).open("rb") as stream:
                for corpus in parser.items(stream, "corpora.item"):
                    row = _validate_stream_corpus_row(corpus)
                    connection.execute("INSERT INTO corpus VALUES (?, ?)", (row["corpus_id"], _canonical_bytes(row)))
            connection.commit()
            for _corpus_id, payload in connection.execute("SELECT corpus_id, payload FROM corpus ORDER BY corpus_id"):
                yield _validate_stream_corpus_row(json.loads(bytes(payload).decode("utf-8")))
            validate_candidate_projection_reference(checked)
        except (OSError, sqlite3.Error) as exc:
            raise OriginalProductError("candidate reference projection stream unavailable") from exc
        finally:
            connection.close()


def _stream_items(reference: Mapping[str, Any], *, corpus_ids: set[str]) -> Iterator[dict[str, Any]]:
    """Yield one query item at a time in exact product sort order."""
    checked = validate_candidate_projection_reference(reference)
    parser = _reference_ijson()
    count = 0
    with tempfile.TemporaryDirectory(prefix=".aerp7-item-sort-", dir=Path(checked["bundle_path"]).parent) as temporary:
        connection = sqlite3.connect(Path(temporary) / "sort.sqlite")
        try:
            connection.execute("CREATE TABLE item (item_id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
            with Path(checked["bundle_path"], checked["projection_path"]).open("rb") as stream:
                for item in parser.items(stream, "items.item"):
                    if not isinstance(item, Mapping):
                        raise OriginalProductError("candidate reference item is malformed")
                    row = dict(item)
                    item_id = _reference_token(row.get("item_id"), "candidate reference item id")
                    required = {"item_id", "persona_id", "query_text", "corpus_id"}
                    allowed_tokens = {"selection_logical_item_id", "selection_logical_binding_witness", "selection_group_id", "selection_tier_id", "selection_variant_id"}
                    if not required <= set(row) or set(row) - required - allowed_tokens:
                        raise OriginalProductError("candidate reference item schema drift")
                    _reference_token(row.get("persona_id"), "candidate reference persona id")
                    _reference_token(row.get("corpus_id"), "candidate reference corpus id")
                    if row["corpus_id"] not in corpus_ids or not isinstance(row.get("query_text"), str) or not row["query_text"]:
                        raise OriginalProductError("candidate reference item binding drift")
                    connection.execute("INSERT INTO item VALUES (?, ?)", (item_id, _canonical_bytes(row)))
            connection.commit()
            for _item_id, payload in connection.execute("SELECT item_id, payload FROM item ORDER BY item_id"):
                count += 1
                if count > checked["query_count"]:
                    raise OriginalProductError("candidate reference query denominator drift")
                yield json.loads(bytes(payload).decode("utf-8"))
            validate_candidate_projection_reference(checked)
        except (OSError, sqlite3.Error) as exc:
            raise OriginalProductError("candidate reference item stream unavailable") from exc
        finally:
            connection.close()
    if count != checked["query_count"]:
        raise OriginalProductError("candidate reference query denominator drift")


class CandidateProjectionCursor:
    """Re-openable candidate-only cursor used by the original worker."""

    def __init__(self, reference: Mapping[str, Any], *, expected_bundle_root: Path | None = None):
        self.reference = validate_candidate_projection_reference(reference, expected_bundle_root=expected_bundle_root)

    def corpora(self) -> Iterator[dict[str, Any]]:
        return _stream_corpora(self.reference)

    def items(self, *, corpus_ids: set[str]) -> Iterator[dict[str, Any]]:
        return _stream_items(self.reference, corpus_ids=corpus_ids)

    def verify(self) -> dict[str, Any]:
        self.reference = validate_candidate_projection_reference(self.reference)
        return dict(self.reference)


_ORIGINAL_REPLICATE_REFERENCE_KEYS = frozenset({
    "schema", "store_path", "ready_path", "build_id", "candidate_reference",
    "candidate_reference_sha256", "generation_id", "state", "query_count",
    "ranking_count", "trace_count", "measurement_count", "item_corpora_count",
    "ledger_count", "input_sha256", "index_sha256", "trace_sha256",
    "replicate_sha256", "store_sha256",
})
_ORIGINAL_REPLICATE_TABLES = frozenset({"rankings", "traces", "measurements", "item_corpora", "ledger"})


class _HashSink:
    """Small file-like sink used to hash canonical JSON without buffering it."""

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def write(self, value: bytes) -> int:
        self._hash.update(value)
        return len(value)

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _emit_canonical_stream_value(sink: _HashSink, value: Any, *, array_rows: Iterator[Any] | None = None) -> None:
    """Emit a canonical JSON value, optionally replacing one array by a cursor."""
    if array_rows is not None:
        sink.write(b"[")
        first = True
        for row in array_rows:
            if not first:
                sink.write(b",")
            sink.write(_canonical_bytes(row))
            first = False
        sink.write(b"]")
        return
    if isinstance(value, Mapping):
        sink.write(b"{")
        for number, key in enumerate(sorted(value)):
            if number:
                sink.write(b",")
            sink.write(_canonical_bytes(str(key)))
            sink.write(b":")
            _emit_canonical_stream_value(sink, value[key])
        sink.write(b"}")
        return
    if isinstance(value, list):
        sink.write(b"[")
        for number, row in enumerate(value):
            if number:
                sink.write(b",")
            sink.write(_canonical_bytes(row))
        sink.write(b"]")
        return
    sink.write(_canonical_bytes(value))


def _digest_canonical_stream_value(value: Any, *, array_rows: Iterator[Any] | None = None) -> str:
    sink = _HashSink()
    _emit_canonical_stream_value(sink, value, array_rows=array_rows)
    return sink.hexdigest()


def _file_sha256(path: Path, code: str) -> str:
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
    except OSError as exc:
        raise OriginalProductError(code) from exc


_ORIGINAL_TABLE_COUNT_KEYS = {
    "rankings": "ranking_count",
    "traces": "trace_count",
    "measurements": "measurement_count",
    "item_corpora": "item_corpora_count",
    "ledger": "ledger_count",
}


class _PersistedSequence(SequenceABC):
    """READY-bound, re-openable cursor over one persisted output table.

    This is the compatibility shape exposed by ``OriginalReplicateReference``
    for callers that historically indexed ``rankings`` or ``trace_receipt``.
    Iteration validates the READY/store digest before opening SQLite and yields
    one decoded row at a time.  No legacy access path is allowed to turn a
    full ranking or measurement table into a Python list implicitly.
    """

    def __init__(self, reference: Mapping[str, Any], *, table: str, count: int) -> None:
        if table not in _ORIGINAL_TABLE_COUNT_KEYS or isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise OriginalProductError("original persisted sequence contract is malformed")
        self._reference = dict(reference)
        self.table = table
        self.count = count

    def __len__(self) -> int:
        return self.count

    def _verified_store(self) -> "OriginalReplicateStore":
        checked = validate_original_replicate_reference(self._reference)
        return OriginalReplicateStore.open(checked)

    def __iter__(self) -> Iterator[dict[str, Any]]:
        with self._verified_store() as store:
            yield from store.iter_rows(self.table)

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            # Slices are an explicit compatibility/materialization request;
            # normal formal validation uses iteration and never enters here.
            start, stop, step = index.indices(self.count)
            return tuple(row for offset, row in enumerate(self) if start <= offset < stop and (offset - start) % step == 0)
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("persisted sequence index must be an integer or slice")
        offset = index if index >= 0 else self.count + index
        if offset < 0 or offset >= self.count:
            raise IndexError("persisted sequence index out of range")
        with self._verified_store() as store:
            try:
                row = store.connection.execute(
                    f"SELECT payload_json FROM {self.table} ORDER BY seq LIMIT 1 OFFSET ?", (offset,)
                ).fetchone()
            except sqlite3.Error as exc:
                raise OriginalProductError("original persisted sequence row unavailable") from exc
            if row is None:
                raise OriginalProductError("original persisted sequence count drift")
            try:
                value = json.loads(row[0])
            except json.JSONDecodeError as exc:
                raise OriginalProductError("original persisted sequence row invalid") from exc
            if not isinstance(value, Mapping):
                raise OriginalProductError("original persisted sequence row invalid")
            return dict(value)

    def sha256(self) -> str:
        """Recompute the canonical sequence digest through the bound cursor."""
        return _digest_sequence(iter(self))

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, SequenceABC):
            if len(other) != self.count:
                return False
            return all(left == right for left, right in zip(self, other))
        return NotImplemented


class _EphemeralSQLitePeak:
    """Measure the bounded disk footprint of one temporary candidate index."""

    def __init__(self, database: Path, *, projection_bytes: int) -> None:
        self.database = database
        self.projection_bytes = int(projection_bytes)
        self.peak_bytes = 0

    def sample(self) -> int:
        total = 0
        for path in self.database.parent.glob(self.database.name + "*"):
            try:
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            except OSError as exc:
                raise OriginalProductError("candidate index footprint observation failed") from exc
        self.peak_bytes = max(self.peak_bytes, total)
        return total

    def receipt(self) -> dict[str, Any]:
        final_bytes = self.sample()
        if self.projection_bytes <= 0:
            raise OriginalProductError("candidate index projection denominator is invalid")
        return {
            "schema": CANDIDATE_INDEX_LIFECYCLE_SCHEMA,
            "database_name": self.database.name,
            "ephemeral": True,
            "cleanup_policy": "temporary_directory_removed_on_worker_exit_success_or_error",
            "cleanup_verification_scope": "worker_parent_context",
            "peak_bytes": int(self.peak_bytes),
            "final_bytes": int(final_bytes),
            "projection_bytes": int(self.projection_bytes),
            "peak_to_projection_ratio": float(self.peak_bytes / self.projection_bytes),
        }


class OriginalReplicateReference(Mapping[str, Any]):
    """Reference mapping with lazy compatibility access to logical sequences.

    The persisted reference keys are the default Mapping view so formal
    consumers can inspect ``schema``/``state`` without opening the store.  The
    historical logical keys remain available through explicit indexing, but
    large ``rankings`` and ``trace_receipt`` sequences are not read until a
    caller asks for them.  Wire packets use :meth:`as_reference` and therefore
    never trigger that materialization.
    """

    _LOGICAL_KEYS = (
        "build_id", "input_receipt", "input_sha256", "index_receipt",
        "index_sha256", "trace_receipt", "trace_sha256", "rankings",
    )

    def __init__(self, reference: Mapping[str, Any]) -> None:
        self._reference = validate_original_replicate_reference(reference)

    @property
    def reference(self) -> dict[str, Any]:
        return dict(self._reference)

    def as_reference(self) -> dict[str, Any]:
        return dict(self._reference)

    def cursor(self, table: str) -> _PersistedSequence:
        """Return a READY/digest-bound cursor for one persisted sequence."""
        count_key = _ORIGINAL_TABLE_COUNT_KEYS.get(table)
        if count_key is None:
            raise OriginalProductError("original persisted sequence table is invalid")
        return _PersistedSequence(self._reference, table=table, count=int(self._reference[count_key]))

    def ranking_cursor(self) -> _PersistedSequence:
        return self.cursor("rankings")

    def trace_cursor(self) -> _PersistedSequence:
        return self.cursor("traces")

    def measurement_cursor(self) -> _PersistedSequence:
        return self.cursor("measurements")

    def ledger_cursor(self) -> _PersistedSequence:
        return self.cursor("ledger")

    def item_corpora_cursor(self) -> _PersistedSequence:
        return self.cursor("item_corpora")

    def sequence_summary(self) -> dict[str, dict[str, Any]]:
        """Return persisted count/digest coverage without loading any rows."""
        validate_original_replicate_reference(self._reference)
        with OriginalReplicateStore.open(self._reference) as store:
            return store.sequence_summary()

    def resource_summary(self) -> dict[str, Any]:
        """Return the frozen scalar worker resource summary persisted in SQLite."""
        validate_original_replicate_reference(self._reference)
        with OriginalReplicateStore.open(self._reference) as store:
            return store.resource_summary()

    def __iter__(self) -> Iterator[str]:
        return iter(self._reference)

    def __len__(self) -> int:
        return len(self._reference)

    def __getitem__(self, key: str) -> Any:
        if key in self._reference:
            return self._reference[key]
        if key not in self._LOGICAL_KEYS:
            raise KeyError(key)
        store = OriginalReplicateStore.open(self._reference)
        try:
            if key == "build_id":
                return store.build_id
            if key == "input_receipt":
                return store.materialize_input_receipt()
            if key == "input_sha256":
                return store.input_sha256
            if key == "index_receipt":
                return store.index_receipt
            if key == "index_sha256":
                return store.index_sha256
            if key == "trace_receipt":
                return self.trace_cursor()
            if key == "trace_sha256":
                return store.trace_sha256
            return self.ranking_cursor()
        finally:
            store.close()


class OriginalReplicateStore:
    """Durable, append-only original-product output spool.

    The SQLite file is a derived run artifact, not a source dataset.  Worker
    output is first written to a dot-prefixed temporary database and published
    only after all sequence counts and digests are complete.  The coordinator
    updates the same database in one transaction and atomically replaces its
    READY sidecar, so a crash cannot be mistaken for a completed replicate.
    """

    def __init__(self, *, database: Path, ready_path: Path, reference: Mapping[str, Any], connection: sqlite3.Connection, writable: bool = False, temporary: Path | None = None) -> None:
        self.database = database
        self.ready_path = ready_path
        self.reference = dict(reference)
        self.connection = connection
        self.writable = writable
        self.temporary = temporary
        self._closed = False
        self._pending = 0
        self._next_seq = {table: 0 for table in _ORIGINAL_REPLICATE_TABLES}

    @property
    def build_id(self) -> str:
        return str(self._metadata("build_id"))

    @property
    def candidate_reference(self) -> dict[str, Any]:
        return dict(self._metadata("candidate_reference"))

    @property
    def input_sha256(self) -> str:
        return str(self._metadata("input_sha256"))

    @property
    def index_sha256(self) -> str:
        return str(self._metadata("index_sha256"))

    @property
    def trace_sha256(self) -> str:
        return str(self._metadata("trace_sha256"))

    @property
    def index_receipt(self) -> dict[str, Any]:
        return dict(self._metadata("index_receipt"))

    @classmethod
    def _paths(cls, staging_parent: Path, *, build_id: str, candidate_reference: Mapping[str, Any]) -> tuple[Path, Path, Path]:
        token = hashlib.sha256((build_id + "\0" + _digest(candidate_reference)).encode("utf-8")).hexdigest()[:24]
        database = (staging_parent / f"original-replicate-{token}.sqlite3").resolve()
        ready = (staging_parent / f"original-replicate-{token}.READY.json").resolve()
        temporary = (staging_parent / f".original-replicate-{token}.sqlite3.tmp").resolve()
        return database, ready, temporary

    @classmethod
    def create(cls, *, staging_parent: Path, build_id: str, candidate_reference: Mapping[str, Any]) -> "OriginalReplicateStore":
        reference = validate_candidate_projection_reference(candidate_reference)
        if not staging_parent.is_absolute() or staging_parent.is_symlink() or not staging_parent.is_dir():
            raise OriginalProductError("original replicate staging parent is invalid")
        database, ready, temporary = cls._paths(staging_parent.resolve(), build_id=build_id, candidate_reference=reference)
        if database.exists() or ready.exists() or temporary.exists() or database.is_symlink() or ready.is_symlink() or temporary.is_symlink():
            raise OriginalProductError("original replicate output already exists")
        connection = sqlite3.connect(temporary)
        try:
            connection.executescript(
                """
                CREATE TABLE metadata(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
                CREATE TABLE rankings(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE traces(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE measurements(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE item_corpora(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE ledger(seq INTEGER PRIMARY KEY, payload_json TEXT NOT NULL);
                """
            )
            for key, value in {
                "schema": ORIGINAL_REPLICATE_STORE_SCHEMA,
                "state": "writing",
                "build_id": build_id,
                "candidate_reference": reference,
                "candidate_reference_sha256": _digest(reference),
            }.items():
                connection.execute("INSERT INTO metadata VALUES (?, ?)", (key, _canonical_bytes(value).decode("utf-8")))
            connection.commit()
            return cls(database=database, ready_path=ready, reference={}, connection=connection, writable=True, temporary=temporary)
        except BaseException:
            connection.close()
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def open(cls, reference: Mapping[str, Any]) -> "OriginalReplicateStore":
        checked = validate_original_replicate_reference(reference, verify=False)
        database = Path(checked["store_path"]); ready = Path(checked["ready_path"])
        connection = sqlite3.connect(database)
        try:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {"metadata", "rankings", "traces", "measurements", "item_corpora", "ledger"}:
                raise OriginalProductError("original replicate store schema is malformed")
            metadata = dict(connection.execute("SELECT key, value_json FROM metadata"))
            parsed = {key: json.loads(value) for key, value in metadata.items()}
            if parsed.get("schema") != ORIGINAL_REPLICATE_STORE_SCHEMA or parsed.get("state") not in {"worker_complete", "coordinator_complete"}:
                raise OriginalProductError("original replicate store is incomplete")
            if parsed.get("candidate_reference_sha256") != _digest(parsed.get("candidate_reference")) or parsed.get("candidate_reference") != checked["candidate_reference"]:
                raise OriginalProductError("original replicate candidate binding drift")
            if parsed.get("build_id") != checked["build_id"]:
                raise OriginalProductError("original replicate build binding drift")
            store = cls(database=database, ready_path=ready, reference=checked, connection=connection, writable=False)
            store._verify_counts_and_metadata()
            return store
        except OriginalProductError:
            connection.close()
            raise
        except (sqlite3.Error, TypeError, json.JSONDecodeError) as exc:
            connection.close()
            raise OriginalProductError("original replicate store is invalid") from exc

    def _metadata(self, key: str) -> Any:
        try:
            row = self.connection.execute("SELECT value_json FROM metadata WHERE key=?", (key,)).fetchone()
        except sqlite3.Error as exc:
            raise OriginalProductError("original replicate metadata read failed") from exc
        if row is None:
            raise OriginalProductError(f"original replicate metadata missing: {key}")
        try:
            return json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise OriginalProductError("original replicate metadata is invalid") from exc

    def _set_metadata(self, key: str, value: Any) -> None:
        if not self.writable:
            raise OriginalProductError("original replicate store is read-only")
        self.connection.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (key, _canonical_bytes(value).decode("utf-8")))

    def _verify_counts_and_metadata(self) -> None:
        if self._metadata("candidate_reference") != self.reference["candidate_reference"]:
            raise OriginalProductError("original replicate candidate reference drift")
        expected = {
            "rankings": self.reference["ranking_count"],
            "traces": self.reference["trace_count"],
            "measurements": self.reference["measurement_count"],
            "item_corpora": self.reference["item_corpora_count"],
            "ledger": self.reference["ledger_count"],
        }
        for table, count in expected.items():
            actual = self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if actual != count:
                raise OriginalProductError("original replicate sequence count drift")

    def append(self, table: str, value: Mapping[str, Any]) -> None:
        if not self.writable or self._closed or table not in _ORIGINAL_REPLICATE_TABLES or not isinstance(value, Mapping):
            raise OriginalProductError("original replicate append is invalid")
        row = dict(value)
        if table == "measurements":
            _validate_query_measurement_rows([row])
        payload = _canonical_bytes(row).decode("utf-8")
        try:
            sequence = self._next_seq[table] + 1
            self.connection.execute(f"INSERT INTO {table}(seq, payload_json) VALUES (?, ?)", (sequence, payload))
            self._next_seq[table] = sequence
            self._pending += 1
            if self._pending >= 256:
                self.connection.commit(); self._pending = 0
        except sqlite3.Error as exc:
            raise OriginalProductError("original replicate append failed") from exc

    def iter_rows(self, table: str) -> Iterator[dict[str, Any]]:
        if table not in _ORIGINAL_REPLICATE_TABLES:
            raise OriginalProductError("original replicate table is invalid")
        try:
            cursor = self.connection.execute(f"SELECT payload_json FROM {table} ORDER BY seq")
            for (payload,) in cursor:
                try:
                    row = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise OriginalProductError("original replicate row is invalid") from exc
                if not isinstance(row, Mapping):
                    raise OriginalProductError("original replicate row is invalid")
                yield dict(row)
        except sqlite3.Error as exc:
            raise OriginalProductError("original replicate rows unavailable") from exc

    def iter_rankings(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows("rankings")

    def iter_traces(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows("traces")

    def iter_measurements(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows("measurements")

    def iter_item_corpora(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows("item_corpora")

    def iter_ledger(self) -> Iterator[dict[str, Any]]:
        return self.iter_rows("ledger")

    def sequence_summary(self) -> dict[str, dict[str, Any]]:
        """Compute all persisted sequence coverage digests by cursor."""
        return {
            table: {
                "count": int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]),
                "sha256": self.sequence_digest(table),
            }
            for table in sorted(_ORIGINAL_REPLICATE_TABLES)
        }

    def resource_summary(self) -> dict[str, Any]:
        """Read only the compact scalar resource receipt persisted at finalize."""
        value = self._metadata("telemetry_base")
        if not isinstance(value, Mapping) or not isinstance(value.get("resources"), Mapping):
            raise OriginalProductError("original replicate resource summary is unavailable")
        return {
            "formal_eligible": value.get("formal_eligible"),
            "resources": dict(value["resources"]),
        }

    def materialize_input_receipt(self) -> dict[str, Any]:
        rows = list(self.iter_item_corpora())
        return {
            "projection_sha256": self.candidate_reference["projection_canonical_sha256"],
            "serializer_sha256": _digest(rank.ORIGINAL_MEMPALACE_SERIALIZER),
            "item_corpora": rows,
            "item_corpus_set_sha256": _digest(rows),
        }

    def sequence_digest(self, table: str) -> str:
        return _digest_sequence(self.iter_rows(table))

    def _input_receipt_sha256(self) -> str:
        sink = _HashSink(); sink.write(b"{")
        fields = [
            ("item_corpora", None),
            ("item_corpus_set_sha256", _digest_sequence(self.iter_item_corpora())),
            ("projection_sha256", self.candidate_reference["projection_canonical_sha256"]),
            ("serializer_sha256", _digest(rank.ORIGINAL_MEMPALACE_SERIALIZER)),
        ]
        for number, (key, value) in enumerate(fields):
            if number: sink.write(b",")
            sink.write(_canonical_bytes(key)); sink.write(b":")
            if key == "item_corpora":
                _emit_canonical_stream_value(sink, None, array_rows=self.iter_item_corpora())
            else:
                sink.write(_canonical_bytes(value))
        sink.write(b"}")
        return sink.hexdigest()

    def _replicate_sha256(self) -> str:
        input_sha = self._metadata("input_sha256")
        index = self._metadata("index_receipt")
        index_sha = self._metadata("index_sha256")
        trace_sha = self._metadata("trace_sha256")
        sink = _HashSink(); sink.write(b"{")
        values: dict[str, Any] = {
            "build_id": self.build_id,
            "index_receipt": index,
            "index_sha256": index_sha,
            "input_sha256": input_sha,
            "trace_sha256": trace_sha,
        }
        keys = sorted(["build_id", "index_receipt", "index_sha256", "input_receipt", "input_sha256", "rankings", "trace_receipt", "trace_sha256"])
        for number, key in enumerate(keys):
            if number: sink.write(b",")
            sink.write(_canonical_bytes(key)); sink.write(b":")
            if key == "input_receipt":
                input_values = {
                    "item_corpora": None,
                    "item_corpus_set_sha256": _digest_sequence(self.iter_item_corpora()),
                    "projection_sha256": self.candidate_reference["projection_canonical_sha256"],
                    "serializer_sha256": _digest(rank.ORIGINAL_MEMPALACE_SERIALIZER),
                }
                _emit_canonical_stream_mapping(sink, input_values, array_key="item_corpora", array_rows=self.iter_item_corpora())
            elif key == "rankings":
                _emit_canonical_stream_value(sink, None, array_rows=self.iter_rankings())
            elif key == "trace_receipt":
                _emit_canonical_stream_value(sink, None, array_rows=self.iter_traces())
            else:
                sink.write(_canonical_bytes(values[key]))
        return sink.hexdigest()

    def _build_reference(self, *, store_sha256: str) -> dict[str, Any]:
        counts = {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in _ORIGINAL_REPLICATE_TABLES}
        return {
            "schema": ORIGINAL_REPLICATE_REFERENCE_SCHEMA,
            "store_path": str(self.database.resolve()), "ready_path": str(self.ready_path.resolve()),
            "build_id": self.build_id,
            "candidate_reference": self.candidate_reference,
            "candidate_reference_sha256": _digest(self.candidate_reference),
            "generation_id": self.candidate_reference["generation_id"],
            "state": self._metadata("state"),
            "query_count": self.candidate_reference["query_count"],
            "ranking_count": counts["rankings"], "trace_count": counts["traces"],
            "measurement_count": counts["measurements"], "item_corpora_count": counts["item_corpora"],
            "ledger_count": counts["ledger"],
            "input_sha256": self._metadata("input_sha256"), "index_sha256": self._metadata("index_sha256"),
            "trace_sha256": self._metadata("trace_sha256"), "replicate_sha256": self._metadata("replicate_sha256"),
            "store_sha256": store_sha256,
        }

    def current_reference(self) -> dict[str, Any]:
        """Rebuild the reference from the durable store metadata.

        Coordinator publication has two durable objects: the SQLite store and
        its READY sidecar.  If the database commit succeeds but the sidecar
        replace is interrupted, the old READY still names the worker state
        while the database already contains the completed coordinator state.
        This method deliberately reads only scalar metadata/counts and the
        database digest, allowing the retry path to reconcile that state
        without materializing rankings, traces, or telemetry rows.
        """
        if self._closed:
            raise OriginalProductError("original replicate store is closed")
        state = self._metadata("state")
        if state not in {"worker_complete", "coordinator_complete"}:
            raise OriginalProductError("original replicate store is incomplete")
        store_sha256 = _file_sha256(self.database, "original replicate store digest failed")
        reference = self._build_reference(store_sha256=store_sha256)
        if reference["candidate_reference"] != self.reference["candidate_reference"] or reference["build_id"] != self.reference["build_id"]:
            raise OriginalProductError("original replicate durable binding drift")
        return reference

    def _write_ready(self, reference: Mapping[str, Any], *, replace_existing: bool = False) -> None:
        ready = {"schema": ORIGINAL_REPLICATE_READY_SCHEMA, "reference": dict(reference), "reference_sha256": _digest(reference), "store_sha256": reference["store_sha256"], "ready_sha256": ""}
        ready["ready_sha256"] = _digest({key: value for key, value in ready.items() if key != "ready_sha256"})
        temporary = self.ready_path.with_name("." + self.ready_path.name + ".tmp")
        if temporary.exists() or temporary.is_symlink():
            raise OriginalProductError("original replicate READY partial output exists")
        payload = _canonical_bytes(ready)
        try:
            with temporary.open("xb") as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            if replace_existing:
                os.replace(temporary, self.ready_path)
            else:
                os.replace(temporary, self.ready_path)
        except OSError as exc:
            try: temporary.unlink()
            except FileNotFoundError: pass
            raise OriginalProductError("original replicate READY publication failed") from exc

    def republish_ready(self, reference: Mapping[str, Any]) -> OriginalReplicateReference:
        """Repair a READY sidecar after a completed database commit.

        Only a coordinator-complete reference derived from this exact store
        can be republished.  The operation is an atomic sidecar replace and
        is therefore safe to retry after an injected publication failure.
        """
        checked = validate_original_replicate_reference(reference, verify=False)
        if self._closed or self.writable or checked["store_path"] != str(self.database.resolve()) or checked["ready_path"] != str(self.ready_path.resolve()):
            raise OriginalProductError("original replicate READY repair binding is invalid")
        if self._metadata("state") != "coordinator_complete" or checked["state"] != "coordinator_complete":
            raise OriginalProductError("original replicate READY repair requires coordinator completion")
        if checked["candidate_reference"] != self.reference["candidate_reference"] or checked["build_id"] != self.reference["build_id"]:
            raise OriginalProductError("original replicate READY repair binding drift")
        if checked["store_sha256"] != _file_sha256(self.database, "original replicate store digest failed"):
            raise OriginalProductError("original replicate READY repair store drift")
        self._write_ready(checked, replace_existing=True)
        return OriginalReplicateReference(validate_original_replicate_reference(checked))

    def _finalize(self, *, state: str, index_receipt: Mapping[str, Any], telemetry_base: Mapping[str, Any] | None = None, replace_existing: bool = False) -> OriginalReplicateReference:
        if not self.writable or self._closed:
            raise OriginalProductError("original replicate store is not writable")
        self._set_metadata("state", state)
        self._set_metadata("index_receipt", dict(index_receipt))
        self._set_metadata("index_sha256", _digest(index_receipt))
        self._set_metadata("trace_sha256", _digest_sequence(self.iter_traces()))
        self._set_metadata("input_sha256", self._input_receipt_sha256())
        # The replicate digest is recomputed after all scalar metadata is set.
        self._set_metadata("replicate_sha256", "0" * 64)
        self.connection.commit()
        self._set_metadata("replicate_sha256", self._replicate_sha256())
        if telemetry_base is not None:
            self._set_metadata("telemetry_base", dict(telemetry_base))
        self.connection.commit()
        self.connection.close(); self._closed = True
        if self.temporary is not None:
            try:
                os.replace(self.temporary, self.database)
            except OSError as exc:
                try: self.temporary.unlink()
                except FileNotFoundError: pass
                raise OriginalProductError("original replicate database publication failed") from exc
        store_sha = _file_sha256(self.database, "original replicate store digest failed")
        # Re-open for the reference builder and ready publication; no row data
        # is copied into Python.
        connection = sqlite3.connect(self.database)
        self.connection = connection; self.writable = False; self.reference = {}; self._closed = False
        ref = self._build_reference(store_sha256=store_sha)
        self.reference = ref
        self._write_ready(ref, replace_existing=replace_existing)
        return OriginalReplicateReference(ref)

    def finalize_worker(self, *, index_receipt: Mapping[str, Any], telemetry_base: Mapping[str, Any]) -> OriginalReplicateReference:
        return self._finalize(state="worker_complete", index_receipt=index_receipt, telemetry_base=telemetry_base)

    def complete_coordinator(self, *, coordinator_receipt: Mapping[str, Any]) -> OriginalReplicateReference:
        if self._closed:
            raise OriginalProductError("original replicate coordinator store is closed")
        index = dict(self.index_receipt)
        if "coordinator_physical_receipt" in index:
            raise OriginalProductError("original replicate coordinator receipt already present")
        index["coordinator_physical_receipt"] = dict(coordinator_receipt)
        self.writable = True
        return self._finalize(state="coordinator_complete", index_receipt=index, replace_existing=True)

    def close(self) -> None:
        if not self._closed:
            try: self.connection.close()
            finally: self._closed = True

    def __enter__(self) -> "OriginalReplicateStore":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


def _digest_sequence(rows: Iterator[Any]) -> str:
    sink = _HashSink(); sink.write(b"["); first = True
    for row in rows:
        if not first: sink.write(b",")
        sink.write(_canonical_bytes(dict(row) if isinstance(row, Mapping) else row)); first = False
    sink.write(b"]"); return sink.hexdigest()


def _emit_canonical_stream_mapping(sink: _HashSink, values: Mapping[str, Any], *, array_key: str, array_rows: Iterator[Mapping[str, Any]]) -> None:
    sink.write(b"{")
    for number, key in enumerate(sorted(values)):
        if number: sink.write(b",")
        sink.write(_canonical_bytes(key)); sink.write(b":")
        if key == array_key:
            _emit_canonical_stream_value(sink, None, array_rows=array_rows)
        else:
            sink.write(_canonical_bytes(values[key]))
    sink.write(b"}")


def validate_original_replicate_reference(value: Any, *, verify: bool = True) -> dict[str, Any]:
    if isinstance(value, OriginalReplicateReference):
        value = value.as_reference()
    if not isinstance(value, Mapping) or set(value) != _ORIGINAL_REPLICATE_REFERENCE_KEYS:
        raise OriginalProductError("original replicate reference schema is malformed")
    row = dict(value)
    if row["schema"] != ORIGINAL_REPLICATE_REFERENCE_SCHEMA or row["state"] not in {"worker_complete", "coordinator_complete"}:
        raise OriginalProductError("original replicate reference schema is malformed")
    for key in ("store_path", "ready_path"):
        if not isinstance(row[key], str) or not Path(row[key]).is_absolute():
            raise OriginalProductError("original replicate reference path is malformed")
        if verify:
            path = Path(row[key])
            if path.is_symlink() or not path.is_file():
                raise OriginalProductError("original replicate reference path is unavailable")
    checked_candidate = validate_candidate_projection_reference(row["candidate_reference"])
    if row["candidate_reference_sha256"] != _digest(checked_candidate) or row["generation_id"] != checked_candidate["generation_id"]:
        raise OriginalProductError("original replicate candidate reference binding is invalid")
    if not isinstance(row["build_id"], str) or not row["build_id"].strip():
        raise OriginalProductError("original replicate build identity is invalid")
    for key in ("query_count", "ranking_count", "trace_count", "measurement_count", "item_corpora_count", "ledger_count"):
        if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] <= 0:
            raise OriginalProductError("original replicate reference count is invalid")
    for key in ("input_sha256", "index_sha256", "trace_sha256", "replicate_sha256", "store_sha256"):
        _hex(row[key], "original replicate reference digest")
    if verify:
        try:
            ready_raw = Path(row["ready_path"]).read_bytes()
            ready = json.loads(ready_raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OriginalProductError("original replicate READY is invalid") from exc
        if _canonical_bytes(ready) != ready_raw or not isinstance(ready, Mapping) or set(ready) != {"schema", "reference", "reference_sha256", "store_sha256", "ready_sha256"}:
            raise OriginalProductError("original replicate READY is invalid")
        if ready.get("schema") != ORIGINAL_REPLICATE_READY_SCHEMA or ready.get("reference") != row or ready.get("reference_sha256") != _digest(row) or ready.get("store_sha256") != row["store_sha256"]:
            raise OriginalProductError("original replicate READY binding drift")
        ready_unsigned = {key: item for key, item in ready.items() if key != "ready_sha256"}
        if ready.get("ready_sha256") != _digest(ready_unsigned):
            raise OriginalProductError("original replicate READY digest invalid")
        if _file_sha256(Path(row["store_path"]), "original replicate store digest failed") != row["store_sha256"]:
            raise OriginalProductError("original replicate store digest drift")
        with OriginalReplicateStore.open(row) as store:
            store._verify_counts_and_metadata()
            if store.sequence_digest("traces") != row["trace_sha256"]:
                raise OriginalProductError("original replicate trace digest drift")
            if store._metadata("replicate_sha256") != row["replicate_sha256"]:
                raise OriginalProductError("original replicate digest drift")
    return row


def load_original_replicate_reference(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    try:
        ready = json.loads(target.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OriginalProductError("original replicate READY is invalid") from exc
    if not isinstance(ready, Mapping) or "reference" not in ready:
        raise OriginalProductError("original replicate READY is invalid")
    return validate_original_replicate_reference(ready["reference"])


_ORIGINAL_ARTIFACT_REFERENCE_KEYS = frozenset({
    "schema", "artifact_path", "ready_path", "arm_id", "candidate_reference",
    "candidate_reference_sha256", "generation_id", "replicate_count",
    "replicate_references", "replicate_references_sha256", "model_receipt",
    "model_sha256", "method_receipt", "method_sha256", "source_receipt",
    "source_commit_sha256", "serializer_receipt", "serializer_sha256",
    "code_receipt", "code_sha256", "artifact_sha256",
})


def _validate_original_artifact_payload(value: Any, *, verify_replicates: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ORIGINAL_ARTIFACT_REFERENCE_KEYS:
        raise OriginalProductError("original artifact reference schema is malformed")
    row = dict(value)
    if row["schema"] != ORIGINAL_ARTIFACT_REFERENCE_SCHEMA or row["arm_id"] != "original_public_product" or row["replicate_count"] != 5:
        raise OriginalProductError("original artifact reference schema is malformed")
    for key in ("artifact_path", "ready_path"):
        if not isinstance(row[key], str) or not Path(row[key]).is_absolute() or Path(row[key]).is_symlink() or not Path(row[key]).is_file():
            raise OriginalProductError("original artifact reference path is unavailable")
    candidate = validate_candidate_projection_reference(row["candidate_reference"])
    if row["candidate_reference_sha256"] != _digest(candidate) or row["generation_id"] != candidate["generation_id"]:
        raise OriginalProductError("original artifact candidate binding is invalid")
    refs = row["replicate_references"]
    if not isinstance(refs, list) or len(refs) != 5 or row["replicate_references_sha256"] != _digest(refs):
        raise OriginalProductError("original artifact replicate references are malformed")
    builds: set[str] = set()
    for ref in refs:
        checked = validate_original_replicate_reference(ref, verify=verify_replicates)
        if checked["state"] != "coordinator_complete" or checked["candidate_reference"] != candidate:
            raise OriginalProductError("original artifact replicate binding is invalid")
        builds.add(checked["build_id"])
    if len(builds) != 5:
        raise OriginalProductError("original artifact build IDs are not fresh")
    for key in ("model_sha256", "method_sha256", "source_commit_sha256", "serializer_sha256", "code_sha256", "artifact_sha256"):
        _hex(row[key], "original artifact digest")
    for receipt_key, digest_key in (("model_receipt", "model_sha256"), ("method_receipt", "method_sha256"), ("source_receipt", "source_commit_sha256"), ("serializer_receipt", "serializer_sha256"), ("code_receipt", "code_sha256")):
        if row[digest_key] != _digest(row[receipt_key]):
            raise OriginalProductError("original artifact receipt digest mismatch")
    unsigned = dict(row); unsigned["artifact_sha256"] = "0" * 64
    if row["artifact_sha256"] != _digest(unsigned):
        raise OriginalProductError("original artifact digest invalid")
    return row


def validate_original_public_artifact_reference(value: Any, *, verify_replicates: bool = True) -> dict[str, Any]:
    row = _validate_original_artifact_payload(value, verify_replicates=verify_replicates)
    try:
        artifact_raw = Path(row["artifact_path"]).read_bytes()
        ready_raw = Path(row["ready_path"]).read_bytes()
        artifact = json.loads(artifact_raw.decode("utf-8")); ready = json.loads(ready_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OriginalProductError("original artifact publication is invalid") from exc
    if artifact_raw != _canonical_bytes(artifact) or artifact != row:
        raise OriginalProductError("original artifact payload drift")
    expected_ready = {"schema": ORIGINAL_ARTIFACT_READY_SCHEMA, "artifact": row, "artifact_sha256": row["artifact_sha256"], "ready_sha256": ""}
    expected_ready["ready_sha256"] = _digest({key: item for key, item in expected_ready.items() if key != "ready_sha256"})
    if ready != expected_ready or ready_raw != _canonical_bytes(ready):
        raise OriginalProductError("original artifact READY drift")
    return row


def load_original_public_artifact_reference(path: Path | str) -> dict[str, Any]:
    try:
        ready = json.loads(Path(path).read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OriginalProductError("original artifact READY is invalid") from exc
    if not isinstance(ready, Mapping) or ready.get("schema") != ORIGINAL_ARTIFACT_READY_SCHEMA:
        raise OriginalProductError("original artifact READY is invalid")
    return validate_original_public_artifact_reference(ready.get("artifact"))


def wrap_original_public_rankings_streaming(*, candidate_reference: Mapping[str, Any], replicate_references: Sequence[Mapping[str, Any]], model_receipt: Mapping[str, Any], code_receipt: Mapping[str, Any], artifact_path: Path, ready_path: Path) -> dict[str, Any]:
    """Publish a five-build original artifact as references, never arrays."""
    candidate = validate_candidate_projection_reference(candidate_reference)
    if not isinstance(replicate_references, Sequence) or isinstance(replicate_references, (str, bytes)) or len(replicate_references) != 5:
        raise OriginalProductError("original artifact requires five replicate references")
    refs = [validate_original_replicate_reference(ref) for ref in replicate_references]
    if any(ref["state"] != "coordinator_complete" or ref["candidate_reference"] != candidate for ref in refs):
        raise OriginalProductError("original artifact replicate reference binding is invalid")
    if len({ref["build_id"] for ref in refs}) != 5:
        raise OriginalProductError("original artifact build IDs are not fresh")
    artifact_path, ready_path = artifact_path.resolve(), ready_path.resolve()
    if artifact_path.parent != ready_path.parent or artifact_path.exists() or ready_path.exists() or artifact_path.is_symlink() or ready_path.is_symlink() or not artifact_path.parent.is_dir():
        raise OriginalProductError("original artifact output path is invalid")
    if not isinstance(model_receipt, Mapping) or not isinstance(code_receipt, Mapping):
        raise OriginalProductError("original artifact receipts are malformed")
    method = rank.ORIGINAL_METHOD
    source = rank.PROTOCOL_SOURCE
    serializer = rank.ORIGINAL_MEMPALACE_SERIALIZER
    row = {
        "schema": ORIGINAL_ARTIFACT_REFERENCE_SCHEMA, "artifact_path": str(artifact_path), "ready_path": str(ready_path),
        "arm_id": "original_public_product", "candidate_reference": candidate, "candidate_reference_sha256": _digest(candidate),
        "generation_id": candidate["generation_id"], "replicate_count": 5, "replicate_references": refs,
        "replicate_references_sha256": _digest(refs), "model_receipt": dict(model_receipt), "model_sha256": _digest(model_receipt),
        "method_receipt": method, "method_sha256": _digest(method), "source_receipt": source, "source_commit_sha256": _digest(source),
        "serializer_receipt": serializer, "serializer_sha256": _digest(serializer), "code_receipt": dict(code_receipt), "code_sha256": _digest(code_receipt),
        "artifact_sha256": "0" * 64,
    }
    row["artifact_sha256"] = _digest(row)
    payload = _canonical_bytes(row)
    ready = {"schema": ORIGINAL_ARTIFACT_READY_SCHEMA, "artifact": row, "artifact_sha256": row["artifact_sha256"], "ready_sha256": ""}
    ready["ready_sha256"] = _digest({key: item for key, item in ready.items() if key != "ready_sha256"})
    try:
        with artifact_path.open("xb") as artifact_stream:
            artifact_stream.write(payload); artifact_stream.flush(); os.fsync(artifact_stream.fileno())
        with ready_path.open("xb") as ready_stream:
            ready_stream.write(_canonical_bytes(ready)); ready_stream.flush(); os.fsync(ready_stream.fileno())
    except OSError as exc:
        for path in (artifact_path, ready_path):
            try: path.unlink()
            except FileNotFoundError: pass
        raise OriginalProductError("original artifact publication failed") from exc
    return validate_original_public_artifact_reference(row)


def _clock_receipt() -> dict[str, Any]:
    """Bind the two stdlib clocks used by the per-query sidecar.

    ``process_time`` is deliberately documented as belonging to this worker
    process only.  It is not an adapter/model counter and it does not include
    descendants; the external supervisor supplies the separate process-tree
    RSS observation after the child exits.
    """
    def one(name: str) -> dict[str, Any]:
        info = time.get_clock_info(name)
        return {
            "name": name,
            "implementation": str(info.implementation),
            "monotonic": bool(info.monotonic),
            "adjustable": bool(info.adjustable),
            "resolution_ns": int(round(float(info.resolution) * 1_000_000_000)),
        }

    return {
        "schema": CLOCK_RECEIPT_SCHEMA,
        "wall": one("perf_counter"),
        "process_cpu": one("process_time"),
        "timing_source": "stdlib",
        "process_cpu_scope": "worker_process_only_excludes_descendants",
        "descendant_observation": "external_supervisor_zero_required",
    }


def _validate_clock_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "schema", "wall", "process_cpu", "timing_source", "process_cpu_scope", "descendant_observation"
    } or value.get("schema") != CLOCK_RECEIPT_SCHEMA:
        raise OriginalProductError("resource telemetry clock receipt is malformed")
    if value.get("timing_source") not in {"stdlib", "injected_test_clock"} or value.get("process_cpu_scope") != "worker_process_only_excludes_descendants" or value.get("descendant_observation") != "external_supervisor_zero_required":
        raise OriginalProductError("resource telemetry process CPU scope is invalid")
    for key, expected_name in (("wall", "perf_counter"), ("process_cpu", "process_time")):
        clock = value.get(key)
        if not isinstance(clock, Mapping) or set(clock) != {"name", "implementation", "monotonic", "adjustable", "resolution_ns"}:
            raise OriginalProductError("resource telemetry clock receipt is malformed")
        if clock.get("name") != expected_name or not isinstance(clock.get("implementation"), str) or not clock["implementation"].strip():
            raise OriginalProductError("resource telemetry clock receipt is malformed")
        if not isinstance(clock.get("monotonic"), bool) or not isinstance(clock.get("adjustable"), bool):
            raise OriginalProductError("resource telemetry clock receipt is malformed")
        resolution = clock.get("resolution_ns")
        if isinstance(resolution, bool) or not isinstance(resolution, int) or resolution <= 0:
            raise OriginalProductError("resource telemetry clock receipt is malformed")
    return dict(value)


def _validate_query_measurement_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise OriginalProductError("resource telemetry query measurements are malformed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"item_id", "query_sha256", "wall_ns", "cpu_ns"}:
            raise OriginalProductError("resource telemetry query measurements are malformed")
        item_id, query_sha = raw.get("item_id"), raw.get("query_sha256")
        if not isinstance(item_id, str) or len(item_id) != 64 or any(ch not in "0123456789abcdef" for ch in item_id) or item_id in seen:
            raise OriginalProductError("resource telemetry query measurements are malformed")
        if not isinstance(query_sha, str) or len(query_sha) != 64 or any(ch not in "0123456789abcdef" for ch in query_sha):
            raise OriginalProductError("resource telemetry query measurements are malformed")
        for key in ("wall_ns", "cpu_ns"):
            sample = raw.get(key)
            if isinstance(sample, bool) or not isinstance(sample, int):
                raise OriginalProductError("resource telemetry query measurements are malformed")
        seen.add(item_id)
        rows.append(dict(raw))
    return rows


def validate_query_measurements(*, measurements: Any, replicate: Mapping[str, Any], expected_count: int) -> list[dict[str, Any]]:
    """Bind sidecar rows to the exact worker ranking order and query hashes."""
    rows = _validate_query_measurement_rows(measurements)
    rankings = replicate.get("rankings") if isinstance(replicate, Mapping) else None
    if not isinstance(rankings, list) or len(rankings) != expected_count or len(rows) != expected_count:
        raise OriginalProductError("resource telemetry query coverage is invalid")
    expected: list[dict[str, str]] = []
    for ranking in rankings:
        if not isinstance(ranking, Mapping) or not isinstance(ranking.get("item_id"), str) or not isinstance(ranking.get("query_sha256"), str):
            raise OriginalProductError("resource telemetry query coverage is invalid")
        expected.append({"item_id": ranking["item_id"], "query_sha256": ranking["query_sha256"]})
    actual = [{"item_id": row["item_id"], "query_sha256": row["query_sha256"]} for row in rows]
    if actual != expected or actual != sorted(actual, key=lambda row: row["item_id"]):
        raise OriginalProductError("resource telemetry query binding is invalid")
    if any(row["wall_ns"] <= 0 or row["cpu_ns"] <= 0 for row in rows):
        raise OriginalProductError("resource telemetry query timing is invalid")
    return rows


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OriginalProductError(f"{label} must be a non-empty string")
    return value.strip()


def _physical_id(corpus_id: str, message_id: str) -> str:
    corpus_id, message_id = _token(corpus_id, "corpus_id"), _token(message_id, "message_id")
    if PHYSICAL_SEPARATOR in corpus_id or PHYSICAL_SEPARATOR in message_id:
        raise OriginalProductError("corpus/message ID collides with AERP7 physical-ID separator")
    return f"{corpus_id}{PHYSICAL_SEPARATOR}{message_id}"


def convomem_lifecycle_adapter() -> original_core.LifecycleAdapter:
    """Compatibility adapter for the unchanged AERP-7 rank receipt contract."""
    def format_row(*, item: Mapping[str, Any], ranked_candidate_ids: list[str], trace: Mapping[str, Any], product_row: Mapping[str, Any]) -> Mapping[str, Any]:
        return dict(product_row)
    def completed(projection: Mapping[str, Any], replicate: Mapping[str, Any]) -> None:
        rank._original_replicate(projection, replicate)
    return original_core.LifecycleAdapter(
        validate_projection=rank.validate_candidate_projection,
        identity_namespace=original_identity_namespace,
        input_receipt=lambda projection: rank._input_receipt(projection, rank.ORIGINAL_MEMPALACE_SERIALIZER),
        format_row=format_row,
        validate_completed_replicate=completed,
        runtime_projection=lambda projection: projection,
        adapter_id="aerp7-convomem-original-lifecycle-v1",
    )


def original_identity_namespace(projection: Any) -> dict[str, Any]:
    """Create the dynamic, globally unique physical namespace for one collection."""
    frozen = rank.validate_candidate_projection(projection)
    # Compatibility wrapper: the generic core owns label-free namespace math;
    # this retains the historical AERP-7 receipt field names byte-for-byte.
    normalized = {"schema": original_core.NORMALIZED_PROJECTION_SCHEMA,
        "corpora": [{"corpus_id": corpus["corpus_id"], "candidates": [{"candidate_id": candidate["message_id"], "order": candidate["corpus_order"], "text": candidate["text"]} for candidate in corpus["candidates"]]} for corpus in frozen["corpora"]],
        "items": [{"item_id": item["item_id"], "corpus_id": item["corpus_id"], "query_text": item["query_text"]} for item in frozen["items"]]}
    try:
        generic = original_core.identity_namespace(normalized, separator=PHYSICAL_SEPARATOR, schema="aerp7-original-identity-namespace-generic-v1", require_global_candidate_ids=False)
    except original_core.OriginalCoreError as exc:
        raise OriginalProductError("AERP7 generic original namespace rejected projection") from exc
    rows = [{"corpus_id": row["corpus_id"], "message_id": row["candidate_id"], "physical_id": row["physical_id"]} for row in generic["rows"]]
    return {
        "schema": "aerp7-original-identity-namespace-v1",
        "scheme": "corpus_id::aerp7::message_id",
        "rows": rows,
        "expected_unique_count": len(rows),
        "mapping_sha256": _digest(rows),
    }


class _NamespaceRows:
    """Re-iterable SQLite-backed namespace rows (never a Python full-census list)."""

    def __init__(self, connection: sqlite3.Connection, *, count: int, owner: Any | None = None, table: str = "namespace_rows") -> None:
        self.connection = connection
        self.count = count
        self.owner = owner
        self.table = table
        self.closed = False

    def __len__(self) -> int:
        return self.count if not self.closed else 0

    def __iter__(self) -> Iterator[dict[str, str]]:
        if self.closed:
            raise OriginalProductError("original identity namespace cursor is closed")
        try:
            if self.table not in {"namespace_rows", "candidate"}:
                raise OriginalProductError("original identity namespace table is invalid")
            for corpus_id, message_id, physical_id in self.connection.execute(f"SELECT corpus_id, message_id, physical_id FROM {self.table} ORDER BY physical_id"):
                yield {"corpus_id": corpus_id, "message_id": message_id, "physical_id": physical_id}
        except sqlite3.Error as exc:
            raise OriginalProductError("original identity namespace cursor is unavailable") from exc

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.connection.close()
            finally:
                cleanup = getattr(self.owner, "cleanup", None)
                if callable(cleanup):
                    cleanup()


class _PhysicalIDRows:
    """Lazy ordered physical-ID view over namespace rows."""

    def __init__(self, rows: _NamespaceRows) -> None:
        self.rows = rows

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[str]:
        for row in self.rows:
            yield row["physical_id"]


def _namespace_by_corpus(namespace: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    rows = namespace.get("rows")
    if not isinstance(rows, (list, _NamespaceRows)) or len(rows) <= 0:
        raise OriginalProductError("original identity namespace is malformed")
    grouped: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise OriginalProductError("original identity namespace is malformed")
        corpus_id, message_id, physical_id = (_token(row.get(key), key) for key in ("corpus_id", "message_id", "physical_id"))
        if physical_id != _physical_id(corpus_id, message_id) or physical_id in seen:
            raise OriginalProductError("original identity namespace is malformed")
        seen.add(physical_id)
        if message_id in grouped.setdefault(corpus_id, {}):
            raise OriginalProductError("original identity namespace repeats a corpus message")
        grouped[corpus_id][message_id] = physical_id
    if namespace.get("expected_unique_count") != len(seen) or namespace.get("mapping_sha256") != _digest_sequence(iter(rows)):
        raise OriginalProductError("original identity namespace receipt mismatch")
    return grouped


def _validate_worker_draft_components(*, projection: Any, namespace: Any, replicate: Any, worker_physical_receipt: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Validate all worker-owned fields before coordinator audit can begin."""
    frozen = rank.validate_candidate_projection(projection)
    if not isinstance(namespace, Mapping) or dict(namespace) != original_identity_namespace(frozen):
        raise OriginalProductError("worker draft namespace does not bind projection")
    _namespace_by_corpus(namespace)
    if not isinstance(replicate, Mapping) or set(replicate) != {"build_id", "input_receipt", "input_sha256", "index_receipt", "index_sha256", "trace_receipt", "trace_sha256", "rankings"}:
        raise OriginalProductError("worker draft replicate schema is malformed")
    raw = dict(replicate); index = raw["index_receipt"]
    required_index = {"build_id", "fresh_build", "collection_identity", "index_identity_sha256", "cold_reopen", "call_contract", "input_coverage_sha256", "query_coverage_sha256", "output_coverage_sha256", "worker_physical_receipt"}
    if not isinstance(index, Mapping) or set(index) != required_index or raw["index_sha256"] != _digest(index):
        raise OriginalProductError("worker draft index receipt is malformed")
    if not isinstance(worker_physical_receipt, Mapping) or dict(worker_physical_receipt) != index["worker_physical_receipt"]:
        raise OriginalProductError("worker draft physical receipt mismatch")
    if raw["input_receipt"] != rank._input_receipt(frozen, rank.ORIGINAL_MEMPALACE_SERIALIZER) or raw["input_sha256"] != _digest(raw["input_receipt"]) or raw["trace_sha256"] != _digest(raw["trace_receipt"]):
        raise OriginalProductError("worker draft input/trace receipt mismatch")
    if index["build_id"] != raw["build_id"] or index["index_identity_sha256"] != _digest({"collection_identity": index["collection_identity"], "physical": worker_physical_receipt}):
        raise OriginalProductError("worker draft index identity mismatch")
    # A worker receipt is complete evidence in its own right, but has no
    # coordinator observation yet.  Validate it without inventing a handoff.
    rank._worker_original_replicate(frozen, raw)
    return frozen, dict(namespace), raw, dict(worker_physical_receipt)


def _validate_worker_draft_telemetry(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"formal_eligible", "live_receipt", "ledger", "resources"}:
        raise OriginalProductError("worker draft telemetry schema is malformed")
    if not isinstance(value["formal_eligible"], bool) or not isinstance(value["live_receipt"], Mapping) or not isinstance(value["ledger"], list) or not isinstance(value["resources"], Mapping):
        raise OriginalProductError("worker draft telemetry schema is malformed")
    resources = value["resources"]
    required_sidecars = {"query_measurements", "clock_receipt", "process_cpu_scope", "descendant_observation"}
    if not required_sidecars <= set(resources):
        raise OriginalProductError("worker draft telemetry sidecar schema is malformed")
    _validate_query_measurement_rows(resources["query_measurements"])
    _validate_clock_receipt(resources["clock_receipt"])
    if resources["process_cpu_scope"] != "worker_process_only_excludes_descendants" or resources["descendant_observation"] != "external_supervisor_zero_required":
        raise OriginalProductError("worker draft telemetry process CPU scope is invalid")
    return dict(value)


def _stream_sequence_reference(*, replicate_reference: Mapping[str, Any], table: str, count: int, sha256: str) -> dict[str, Any]:
    if table not in _ORIGINAL_REPLICATE_TABLES or isinstance(count, bool) or not isinstance(count, int) or count <= 0:
        raise OriginalProductError("stream telemetry sequence reference is malformed")
    _hex(sha256, "stream telemetry sequence digest")
    return {
        "schema": ORIGINAL_STREAM_TELEMETRY_SCHEMA,
        "replicate_reference_sha256": _digest(replicate_reference),
        "table": table,
        "count": count,
        "sha256": sha256,
    }


def _validate_stream_worker_telemetry(value: Any, reference: Mapping[str, Any]) -> dict[str, Any]:
    """Validate list-free telemetry carried by a stream worker draft."""
    required = {"schema", "formal_eligible", "live_receipt", "replicate_reference", "replicate_reference_sha256", "resources"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema") != ORIGINAL_STREAM_TELEMETRY_SCHEMA:
        raise OriginalProductError("stream worker telemetry schema is malformed")
    replicate_reference = validate_original_replicate_reference(value["replicate_reference"])
    if value["replicate_reference_sha256"] != _digest(replicate_reference) or replicate_reference["candidate_reference"] != dict(reference):
        raise OriginalProductError("stream worker telemetry reference binding is invalid")
    if not isinstance(value.get("formal_eligible"), bool) or not isinstance(value.get("live_receipt"), Mapping):
        raise OriginalProductError("stream worker telemetry schema is malformed")
    resources = value["resources"]
    required_resources = {
        "peak_rss_bytes", "storage_bytes", "passage_embedding", "query_embedding",
        "ingest_seconds", "index_seconds", "query_latency_seconds", "query_measurements",
        "ledger", "clock_receipt", "process_cpu_scope", "descendant_observation",
        "native_internal_embedding_calls_observable", "native_internal_embedding_limitation", "provider",
        "candidate_index",
    }
    if not isinstance(resources, Mapping) or set(resources) != required_resources:
        raise OriginalProductError("stream worker telemetry resources are malformed")
    for key in ("peak_rss_bytes", "storage_bytes"):
        _nonnegative_int(resources[key], key)
    for key in ("ingest_seconds", "index_seconds"):
        if not isinstance(resources[key], (int, float)) or isinstance(resources[key], bool) or not math.isfinite(float(resources[key])) or resources[key] < 0:
            raise OriginalProductError("stream worker telemetry timing is malformed")
    for key in ("passage_embedding", "query_embedding"):
        metric = resources[key]
        if not isinstance(metric, Mapping) or set(metric) != {"calls", "texts", "measurement"}:
            raise OriginalProductError("stream worker telemetry embedding receipt is malformed")
        _nonnegative_int(metric["calls"], f"{key}.calls"); _nonnegative_int(metric["texts"], f"{key}.texts")
        if not isinstance(metric["measurement"], str) or not metric["measurement"].strip():
            raise OriginalProductError("stream worker telemetry embedding receipt is malformed")
    provider = resources["provider"]
    if not isinstance(provider, Mapping) or set(provider) != {"model", "device", "providers"} or provider.get("device") != "cpu" or provider.get("providers") != ["CPUExecutionProvider"] or not isinstance(provider.get("model"), str):
        raise OriginalProductError("stream worker telemetry provider receipt is malformed")
    _validate_clock_receipt(resources["clock_receipt"])
    if resources["process_cpu_scope"] != "worker_process_only_excludes_descendants" or resources["descendant_observation"] != "external_supervisor_zero_required":
        raise OriginalProductError("stream worker telemetry process CPU scope is invalid")
    for key, table in (("query_latency_seconds", "measurements"), ("query_measurements", "measurements"), ("ledger", "ledger")):
        sequence = resources[key]
        if not isinstance(sequence, Mapping) or set(sequence) != {"schema", "replicate_reference_sha256", "table", "count", "sha256"}:
            raise OriginalProductError("stream worker telemetry sequence reference is malformed")
        if sequence["schema"] != ORIGINAL_STREAM_TELEMETRY_SCHEMA or sequence["table"] != table or sequence["replicate_reference_sha256"] != _digest(replicate_reference):
            raise OriginalProductError("stream worker telemetry sequence reference is malformed")
        if sequence["count"] != reference["query_count"] and table == "measurements":
            raise OriginalProductError("stream worker telemetry query count drift")
        _hex(sequence["sha256"], "stream worker telemetry sequence digest")
    if resources["native_internal_embedding_calls_observable"] is not False or not isinstance(resources["native_internal_embedding_limitation"], str):
        raise OriginalProductError("stream worker telemetry native call receipt is malformed")
    candidate_index = resources["candidate_index"]
    if not isinstance(candidate_index, Mapping) or set(candidate_index) != {
        "schema", "database_name", "ephemeral", "cleanup_policy", "cleanup_verification_scope",
        "peak_bytes", "final_bytes", "projection_bytes", "peak_to_projection_ratio",
    } or candidate_index.get("schema") != CANDIDATE_INDEX_LIFECYCLE_SCHEMA or candidate_index.get("ephemeral") is not True:
        raise OriginalProductError("stream worker candidate-index lifecycle receipt is malformed")
    for key in ("peak_bytes", "final_bytes", "projection_bytes"):
        _nonnegative_int(candidate_index.get(key), f"candidate_index.{key}")
    if candidate_index["projection_bytes"] <= 0 or candidate_index["peak_bytes"] < candidate_index["final_bytes"]:
        raise OriginalProductError("stream worker candidate-index footprint receipt is invalid")
    ratio = candidate_index.get("peak_to_projection_ratio")
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(float(ratio)) or ratio < 0:
        raise OriginalProductError("stream worker candidate-index footprint ratio is invalid")
    if not isinstance(candidate_index.get("database_name"), str) or not candidate_index["database_name"].strip() or not isinstance(candidate_index.get("cleanup_policy"), str) or not isinstance(candidate_index.get("cleanup_verification_scope"), str):
        raise OriginalProductError("stream worker candidate-index lifecycle receipt is malformed")
    return dict(value)


def _validate_stream_namespace(value: Any, reference: Mapping[str, Any]) -> dict[str, Any]:
    required = {"schema", "candidate_reference", "candidate_reference_sha256", "corpus_count", "candidate_text_count", "query_count"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema") != STREAM_NAMESPACE_SCHEMA:
        raise OriginalProductError("stream worker namespace is malformed")
    checked = validate_candidate_projection_reference(value["candidate_reference"])
    if value["candidate_reference_sha256"] != _digest(checked) or checked != dict(reference):
        raise OriginalProductError("stream worker namespace/reference binding is invalid")
    for key in ("corpus_count", "candidate_text_count", "query_count"):
        count = value.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise OriginalProductError("stream worker namespace denominator is invalid")
    if value["candidate_text_count"] != checked["candidate_text_count"] or value["query_count"] != checked["query_count"]:
        raise OriginalProductError("stream worker namespace denominator drift")
    return dict(value)


def worker_draft_packet(draft: OriginalProductWorkerDraft) -> dict[str, Any]:
    """Return a strict, digest-bound but deliberately non-publishable packet."""
    if not isinstance(draft, OriginalProductWorkerDraft):
        raise OriginalProductError("worker draft packet requires OriginalProductWorkerDraft")
    if draft.candidate_reference is not None:
        if draft.projection is not None:
            raise OriginalProductError("stream worker draft cannot embed a projection")
        reference = validate_candidate_projection_reference(draft.candidate_reference)
        namespace = _validate_stream_namespace(draft.namespace, reference)
        telemetry = _validate_stream_worker_telemetry(draft.telemetry, reference) if draft.telemetry.get("schema") == ORIGINAL_STREAM_TELEMETRY_SCHEMA else _validate_worker_draft_telemetry(draft.telemetry)
        replicate = draft.replicate_without_coordinator_audit
        if isinstance(replicate, OriginalReplicateReference):
            replicate = replicate.as_reference()
        elif not isinstance(replicate, Mapping):
            raise OriginalProductError("stream worker replicate reference is malformed")
        if isinstance(replicate, Mapping) and replicate.get("schema") == ORIGINAL_REPLICATE_REFERENCE_SCHEMA:
            checked_replicate = validate_original_replicate_reference(replicate)
            if checked_replicate["candidate_reference"] != reference:
                raise OriginalProductError("stream worker replicate/reference binding is invalid")
        packet = {
            "schema": STREAM_DRAFT_SCHEMA,
            "candidate_reference": reference,
            "candidate_reference_sha256": _digest(reference),
            "namespace": namespace,
            "namespace_sha256": _digest(namespace),
            "replicate_without_coordinator_audit": dict(replicate),
            "worker_physical_receipt": dict(draft.worker_physical_receipt),
            "worker_physical_receipt_sha256": _digest(draft.worker_physical_receipt),
            "telemetry": telemetry,
            "telemetry_sha256": _digest(telemetry),
        }
        packet["draft_sha256"] = _digest(packet)
        return packet
    frozen, namespace, replicate, worker = _validate_worker_draft_components(
        projection=draft.projection, namespace=draft.namespace,
        replicate=draft.replicate_without_coordinator_audit, worker_physical_receipt=draft.worker_physical_receipt,
    )
    telemetry = _validate_worker_draft_telemetry(draft.telemetry)
    packet = {
        "schema": DRAFT_SCHEMA,
        "projection": frozen, "projection_sha256": _digest(frozen),
        "namespace": namespace, "namespace_sha256": _digest(namespace),
        "replicate_without_coordinator_audit": replicate,
        "worker_physical_receipt": worker, "worker_physical_receipt_sha256": _digest(worker),
        "telemetry": telemetry, "telemetry_sha256": _digest(telemetry),
    }
    packet["draft_sha256"] = _digest(packet)
    return packet


def serialize_worker_draft(draft: OriginalProductWorkerDraft) -> bytes:
    """Canonical wire format for worker-to-coordinator transfer."""
    return _canonical_bytes(worker_draft_packet(draft))


def serialize_generic_worker_draft(*, draft: OriginalProductWorkerDraft, adapter_id: str) -> bytes:
    """Versioned cross-process envelope for a non-ConvoMem lifecycle adapter."""
    if not isinstance(draft, OriginalProductWorkerDraft) or not isinstance(adapter_id, str) or not adapter_id:
        raise OriginalProductError("generic worker draft input is invalid")
    value = {"schema": GENERIC_DRAFT_SCHEMA, "adapter_id": adapter_id, "projection": draft.projection,
        "namespace": draft.namespace, "replicate_without_coordinator_audit": draft.replicate_without_coordinator_audit,
        "worker_physical_receipt": draft.worker_physical_receipt, "telemetry": draft.telemetry}
    value["draft_sha256"] = _digest(value)
    return _canonical_bytes(value)


def load_generic_worker_draft(*, payload: bytes, adapter_id: str) -> OriginalProductWorkerDraft:
    if not isinstance(payload, bytes) or not isinstance(adapter_id, str) or not adapter_id:
        raise OriginalProductError("generic worker draft input is invalid")
    try: value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise OriginalProductError("generic worker draft payload is invalid") from exc
    required = {"schema", "adapter_id", "projection", "namespace", "replicate_without_coordinator_audit", "worker_physical_receipt", "telemetry", "draft_sha256"}
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema") != GENERIC_DRAFT_SCHEMA or value.get("adapter_id") != adapter_id or _canonical_bytes(value) != payload:
        raise OriginalProductError("generic worker draft schema is invalid")
    unsigned = {key: child for key, child in value.items() if key != "draft_sha256"}
    if value["draft_sha256"] != _digest(unsigned): raise OriginalProductError("generic worker draft digest is invalid")
    return OriginalProductWorkerDraft(projection=dict(value["projection"]), namespace=dict(value["namespace"]), replicate_without_coordinator_audit=dict(value["replicate_without_coordinator_audit"]), worker_physical_receipt=dict(value["worker_physical_receipt"]), telemetry=dict(value["telemetry"]))


def load_worker_draft(payload: bytes) -> OriginalProductWorkerDraft:
    """Fail closed on noncanonical bytes, schema drift, or any receipt mismatch."""
    if not isinstance(payload, bytes):
        raise OriginalProductError("worker draft payload must be canonical bytes")
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OriginalProductError("worker draft payload is not valid UTF-8 JSON") from exc
    if not isinstance(parsed, Mapping) or _canonical_bytes(parsed) != payload:
        raise OriginalProductError("worker draft payload is not canonical")
    if parsed.get("schema") == STREAM_DRAFT_SCHEMA:
        required_stream = {
            "schema", "candidate_reference", "candidate_reference_sha256", "namespace", "namespace_sha256",
            "replicate_without_coordinator_audit", "worker_physical_receipt", "worker_physical_receipt_sha256",
            "telemetry", "telemetry_sha256", "draft_sha256",
        }
        if set(parsed) != required_stream:
            raise OriginalProductError("stream worker draft packet schema is malformed")
        unsigned = {key: value for key, value in parsed.items() if key != "draft_sha256"}
        if parsed["draft_sha256"] != _digest(unsigned):
            raise OriginalProductError("stream worker draft digest mismatch")
        reference = validate_candidate_projection_reference(parsed["candidate_reference"])
        for value_key, digest_key in (
            ("candidate_reference", "candidate_reference_sha256"),
            ("namespace", "namespace_sha256"),
            ("worker_physical_receipt", "worker_physical_receipt_sha256"),
            ("telemetry", "telemetry_sha256"),
        ):
            if parsed[digest_key] != _digest(parsed[value_key]):
                raise OriginalProductError("stream worker draft component digest mismatch")
        namespace = _validate_stream_namespace(parsed["namespace"], reference)
        telemetry = _validate_stream_worker_telemetry(parsed["telemetry"], reference) if parsed["telemetry"].get("schema") == ORIGINAL_STREAM_TELEMETRY_SCHEMA else _validate_worker_draft_telemetry(parsed["telemetry"])
        replicate_value = parsed["replicate_without_coordinator_audit"]
        if isinstance(replicate_value, Mapping) and replicate_value.get("schema") == ORIGINAL_REPLICATE_REFERENCE_SCHEMA:
            if validate_original_replicate_reference(replicate_value)["candidate_reference"] != reference:
                raise OriginalProductError("stream worker replicate/reference binding is invalid")
            replicate_value = OriginalReplicateReference(replicate_value)
        return OriginalProductWorkerDraft(
            projection=None,
            candidate_reference=reference,
            namespace=namespace,
            replicate_without_coordinator_audit=replicate_value,
            worker_physical_receipt=dict(parsed["worker_physical_receipt"]),
            telemetry=telemetry,
        )
    required = {"schema", "projection", "projection_sha256", "namespace", "namespace_sha256", "replicate_without_coordinator_audit", "worker_physical_receipt", "worker_physical_receipt_sha256", "telemetry", "telemetry_sha256", "draft_sha256"}
    if set(parsed) != required or parsed.get("schema") != DRAFT_SCHEMA:
        raise OriginalProductError("worker draft packet schema is malformed")
    unsigned = {key: value for key, value in parsed.items() if key != "draft_sha256"}
    if parsed["draft_sha256"] != _digest(unsigned):
        raise OriginalProductError("worker draft digest mismatch")
    for value_key, digest_key in (("projection", "projection_sha256"), ("namespace", "namespace_sha256"), ("worker_physical_receipt", "worker_physical_receipt_sha256"), ("telemetry", "telemetry_sha256")):
        if not isinstance(parsed[digest_key], str) or parsed[digest_key] != _digest(parsed[value_key]):
            raise OriginalProductError("worker draft component digest mismatch")
    frozen, namespace, replicate, worker = _validate_worker_draft_components(
        projection=parsed["projection"], namespace=parsed["namespace"],
        replicate=parsed["replicate_without_coordinator_audit"], worker_physical_receipt=parsed["worker_physical_receipt"],
    )
    telemetry = _validate_worker_draft_telemetry(parsed["telemetry"])
    return OriginalProductWorkerDraft(projection=frozen, namespace=namespace, replicate_without_coordinator_audit=replicate, worker_physical_receipt=worker, telemetry=telemetry)


def _collection(palace: Any, palace_path: Path) -> Any:
    getter = getattr(palace, "get_collection", None)
    if not callable(getter):
        raise OriginalProductError("original palace.get_collection is unavailable")
    return getter(
        str(palace_path),
        collection_name=v1.ORIGINAL_COLLECTION,
        create=True,
        backend="chroma",
    )


def ingest_corpus_once(*, palace: Any, palace_path: Path, corpus: Mapping[str, Any], physical_by_message: Mapping[str, str], ledger: list[dict[str, Any]]) -> None:
    """Use the public ``get_collection(...).upsert`` contract exactly once/corpus."""
    corpus_id = _token(corpus.get("corpus_id"), "corpus_id")
    candidates = corpus.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise OriginalProductError("candidate corpus is malformed")
    if set(physical_by_message) != {candidate.get("message_id") for candidate in candidates}:
        raise OriginalProductError("candidate corpus/physical namespace mismatch")
    collection = _collection(palace, palace_path)
    upsert = getattr(collection, "upsert", None)
    if not callable(upsert):
        raise OriginalProductError("original collection.upsert is unavailable")
    ids, documents, metadatas = [], [], []
    for candidate in candidates:
        message_id = _token(candidate.get("message_id"), "message_id")
        text = candidate.get("text")
        speaker = candidate.get("speaker")
        if not isinstance(text, str) or not isinstance(speaker, str):
            raise OriginalProductError("candidate text/speaker is malformed")
        ids.append(physical_by_message[message_id])
        documents.append(text)
        metadatas.append({"source_file": physical_by_message[message_id], "room": corpus_id, "wing": WING, "speaker": speaker})
    upsert(ids=ids, documents=documents, metadatas=metadatas)
    ledger.append({"event": "upsert", "corpus_id": corpus_id, "physical_ids_sha256": _digest(ids), "count": len(ids)})


def _row_for_query(*, item: Mapping[str, Any], corpus: Mapping[str, Any], physical_by_message: Mapping[str, str], searcher: Any, palace_path: Path, ledger: list[dict[str, Any]], latency_seconds: float, wall_clock_ns: Callable[[], int] | None = None, cpu_clock_ns: Callable[[], int] | None = None, query_measurements: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    corpus_id = _token(item.get("corpus_id"), "item corpus_id")
    if corpus_id != corpus.get("corpus_id"):
        raise OriginalProductError("query corpus binding mismatch")
    reverse = {physical: message for message, physical in physical_by_message.items()}
    corpus_ids = list(reverse)
    query_text = _token(item.get("query_text"), "query_text")
    item_id = _token(item.get("item_id"), "item_id")
    if (wall_clock_ns is None) != (cpu_clock_ns is None):
        raise OriginalProductError("query timing clocks must be supplied as a pair")
    # Reuse the AERP5 public-product helper rather than re-implementing its
    # parameters or accepting an alternate direct Chroma path.  It calls only
    # ``searcher.search_memories(query, palace_path, room=..., n_results=10,
    # max_distance=0.0, candidate_strategy='vector', collection_name=...)``.
    wall_started = wall_clock_ns() if wall_clock_ns is not None else None
    cpu_started = cpu_clock_ns() if cpu_clock_ns is not None else None
    try:
        selected = v1.original_product_query(
            searcher=searcher, palace_path=palace_path, conversation_id=corpus_id,
            corpus_ids=corpus_ids, query=query_text, item_id=item_id,
        )
    except Exception as exc:
        raise OriginalProductError("original public-product query failed") from exc
    if wall_clock_ns is not None and cpu_clock_ns is not None:
        wall_elapsed_ns = wall_clock_ns() - wall_started
        cpu_elapsed_ns = cpu_clock_ns() - cpu_started
        if query_measurements is not None:
            query_measurements.append({
                "item_id": item["item_id"],
                "query_sha256": hashlib.sha256(item["query_text"].encode("utf-8")).hexdigest(),
                "wall_ns": wall_elapsed_ns,
                "cpu_ns": cpu_elapsed_ns,
            })
        latency_seconds = wall_elapsed_ns / 1_000_000_000
    if set(selected) - set(reverse):
        raise OriginalProductError("original searcher crossed the corpus physical universe")
    message_ids = [reverse[physical] for physical in selected]
    candidates = {candidate["message_id"]: candidate for candidate in corpus["candidates"]}
    conversations = list(dict.fromkeys(candidates[message_id]["opaque_conversation_id"] for message_id in message_ids))
    query_digest = hashlib.sha256(item["query_text"].encode("utf-8")).hexdigest()
    candidate_digest = rank._candidate_input(corpus, rank.ORIGINAL_MEMPALACE_SERIALIZER)
    ranking_digest = _digest(message_ids)
    row = {"item_id": item["item_id"], "query_sha256": query_digest, "candidate_input_sha256": candidate_digest, "ranked_message_ids": message_ids, "retrieved_conversation_ids": conversations, "confidence": None, "confidence_receipt": None}
    trace = {"item_id": item["item_id"], "query_sha256": query_digest, "candidate_input_sha256": candidate_digest, "ranked_count": len(message_ids), "ranking_sha256": ranking_digest}
    ledger.append({"event": "search", "item_id": item["item_id"], "corpus_id": corpus_id, "ranking_sha256": ranking_digest, "latency_seconds": latency_seconds})
    return row, trace


def _stream_index_schema(connection: sqlite3.Connection) -> None:
    """Create the ephemeral candidate lookup only; never a scientific output."""
    connection.executescript(
        """
        CREATE TABLE corpus (
            corpus_id TEXT PRIMARY KEY,
            declared_context_size INTEGER NOT NULL,
            actual_conversation_count INTEGER NOT NULL,
            actual_message_count INTEGER NOT NULL,
            candidate_input_sha256 TEXT NOT NULL
        );
        CREATE TABLE candidate (
            corpus_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            opaque_conversation_id TEXT NOT NULL,
            conversation_order INTEGER NOT NULL,
            message_order INTEGER NOT NULL,
            corpus_order INTEGER NOT NULL,
            speaker TEXT NOT NULL,
            text TEXT NOT NULL,
            physical_id TEXT NOT NULL UNIQUE,
            PRIMARY KEY (corpus_id, message_id)
        );
        """
    )


def _stream_corpus_from_db(connection: sqlite3.Connection, corpus_id: str) -> tuple[dict[str, Any], dict[str, str]]:
    row = connection.execute(
        "SELECT corpus_id, declared_context_size, actual_conversation_count, actual_message_count "
        "FROM corpus WHERE corpus_id=?", (corpus_id,)
    ).fetchone()
    if row is None:
        raise OriginalProductError("stream query corpus disappeared")
    candidates = []
    physical_by_message: dict[str, str] = {}
    for candidate in connection.execute(
        "SELECT message_id, opaque_conversation_id, conversation_order, message_order, corpus_order, speaker, text, physical_id "
        "FROM candidate WHERE corpus_id=? ORDER BY corpus_order", (corpus_id,)
    ):
        candidate_row = {
            "message_id": candidate[0], "opaque_conversation_id": candidate[1],
            "conversation_order": candidate[2], "message_order": candidate[3],
            "corpus_order": candidate[4], "speaker": candidate[5], "text": candidate[6],
        }
        candidates.append(candidate_row)
        physical_by_message[candidate[0]] = candidate[7]
    if len(candidates) != row[3]:
        raise OriginalProductError("stream query corpus count drift")
    return ({
        "corpus_id": row[0], "declared_context_size": row[1],
        "actual_conversation_count": row[2], "actual_message_count": row[3],
        "candidates": candidates,
    }, physical_by_message)


def _stream_namespace_from_db(connection: sqlite3.Connection) -> dict[str, Any]:
    count = int(connection.execute("SELECT COUNT(*) FROM candidate").fetchone()[0])
    if count <= 0:
        raise OriginalProductError("stream identity namespace is empty")
    rows = _NamespaceRows(connection, count=count, table="candidate")
    return {
        "schema": "aerp7-original-identity-namespace-v1",
        "scheme": "corpus_id::aerp7::message_id",
        "rows": rows,
        "expected_unique_count": len(rows),
        "mapping_sha256": _digest_sequence(iter(rows)),
    }


def run_original_public_replicate_streaming(
    *, candidate_reference: Mapping[str, Any], build_id: str, collection_identity: str,
    palace_path: Path, observer: ResourceObserver | None, seams: OriginalProductSeams,
    live_receipt: Mapping[str, Any] | None = None, formal: bool = False,
    resource_sink: Callable[[Mapping[str, Any]], None] | None = None,
    wall_clock_ns: Callable[[], int] | None = None,
    cpu_clock_ns: Callable[[], int] | None = None,
    lifecycle_adapter: original_core.LifecycleAdapter | None = None,
    staging_parent: Path | None = None,
) -> OriginalProductWorkerDraft:
    """Run the exact original product over a persisted reference, streaming input.

    The legacy mapping API remains available for synthetic seams.  This lane
    parses one corpus and one query at a time, keeps only an ephemeral SQLite
    lookup, and returns a draft whose wire packet carries the immutable public
    reference rather than the projection.
    """
    if observer is None:
        raise OriginalProductError("a real resource observer is required")
    if lifecycle_adapter is not None and lifecycle_adapter.adapter_id != "aerp7-convomem-original-lifecycle-v1":
        raise OriginalProductError("streaming original worker requires the ConvoMem lifecycle adapter")
    injected_clocks = wall_clock_ns is not None or cpu_clock_ns is not None
    if formal and injected_clocks:
        raise OriginalProductError("formal query clocks must be stdlib")
    if formal:
        _validate_live_provenance(seams=seams, live_receipt=live_receipt)
        if resource_sink is None:
            raise OriginalProductError("formal original worker requires a resource sink")
    elif seams.provenance != SYNTHETIC_INJECTED or seams._live_capability is not None:
        raise OriginalProductError("non-formal execution requires an explicitly synthetic injected seam")
    reference = validate_candidate_projection_reference(candidate_reference)
    if len({build_id, collection_identity}) != 2 or not all(isinstance(value, str) and value.strip() for value in (build_id, collection_identity)):
        raise OriginalProductError("build/collection identity must be distinct non-empty strings")
    wall_clock_ns = time.perf_counter_ns if wall_clock_ns is None else wall_clock_ns
    cpu_clock_ns = time.process_time_ns if cpu_clock_ns is None else cpu_clock_ns
    if not callable(wall_clock_ns) or not callable(cpu_clock_ns):
        raise OriginalProductError("query timing clocks must be callable")
    lifecycle = convomem_lifecycle_adapter()
    observer.checkpoint("before_ingest")
    parent = Path(staging_parent) if staging_parent is not None else palace_path.parent
    bundle = Path(reference["bundle_path"]).resolve()
    parent_resolved = parent.resolve() if parent.exists() else parent
    if not parent.is_dir() or parent_resolved == bundle or bundle in parent_resolved.parents:
        raise OriginalProductError("stream index parent is invalid")
    output_store = OriginalReplicateStore.create(staging_parent=parent.resolve(), build_id=build_id, candidate_reference=reference)
    committed = False
    try:
        with tempfile.TemporaryDirectory(prefix=f".aerp7-original-stream-{build_id[:12]}-", dir=parent) as temporary:
            candidate_index_path = Path(temporary) / "candidate-index.sqlite"
            projection_bytes = (bundle / reference["projection_path"]).stat().st_size
            candidate_index_peak = _EphemeralSQLitePeak(candidate_index_path, projection_bytes=projection_bytes)
            connection = sqlite3.connect(candidate_index_path)
            try:
                _stream_index_schema(connection)
                candidate_index_peak.sample()
                ledger_sink = type("_LedgerSink", (), {"append": lambda self, row: output_store.append("ledger", row)})()
                measurement_sink = type("_MeasurementSink", (), {"append": lambda self, row: output_store.append("measurements", row)})()
                corpus_ids: set[str] = set()
                corpus_count = candidate_count = 0
                started = time.perf_counter()
                for corpus in _stream_corpora(reference):
                    corpus_id = corpus["corpus_id"]
                    if corpus_id in corpus_ids:
                        raise OriginalProductError("stream corpus duplicate")
                    corpus_ids.add(corpus_id)
                    physical_by_message = {candidate["message_id"]: _physical_id(corpus_id, candidate["message_id"]) for candidate in corpus["candidates"]}
                    ingest_corpus_once(palace=seams.palace, palace_path=palace_path, corpus=corpus, physical_by_message=physical_by_message, ledger=ledger_sink)
                    connection.execute(
                        "INSERT INTO corpus VALUES (?, ?, ?, ?, ?)",
                        (corpus_id, corpus["declared_context_size"], corpus["actual_conversation_count"], corpus["actual_message_count"], rank._candidate_input(corpus, rank.ORIGINAL_MEMPALACE_SERIALIZER)),
                    )
                    connection.executemany(
                        "INSERT INTO candidate VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [(
                            corpus_id, candidate["message_id"], candidate["opaque_conversation_id"],
                            candidate["conversation_order"], candidate["message_order"], candidate["corpus_order"],
                            candidate["speaker"], candidate["text"], physical_by_message[candidate["message_id"]],
                        ) for candidate in corpus["candidates"]],
                    )
                    corpus_count += 1
                    candidate_count += len(corpus["candidates"])
                connection.commit()
                candidate_index_peak.sample()
                if corpus_count <= 0 or candidate_count != reference["candidate_text_count"]:
                    raise OriginalProductError("stream candidate denominator drift")
                ingest_seconds = time.perf_counter() - started
                observer.checkpoint("after_ingest")
                index_started = time.perf_counter()
                cleanup = dict(seams.reset_backends(palace_path))
                index_seconds = time.perf_counter() - index_started
                if cleanup.get("verified_system_released") is not True:
                    raise OriginalProductError("original product did not prove cold close/reset")
                ledger_sink.append({"event": "cold_reopen_barrier", "cleanup": cleanup})
                observer.checkpoint("after_cold_close")
                query_count = 0
                for item in _stream_items(reference, corpus_ids=corpus_ids):
                    corpus, physical_by_message = _stream_corpus_from_db(connection, item["corpus_id"])
                    product_row, trace = _row_for_query(
                        item=item, corpus=corpus, physical_by_message=physical_by_message,
                        searcher=seams.searcher, palace_path=palace_path, ledger=ledger_sink,
                        latency_seconds=0.0, wall_clock_ns=wall_clock_ns,
                        cpu_clock_ns=cpu_clock_ns, query_measurements=measurement_sink,
                    )
                    output_store.append("item_corpora", {"item_id": item["item_id"], "corpus_id": item["corpus_id"], "candidate_input_sha256": rank._candidate_input(corpus, rank.ORIGINAL_MEMPALACE_SERIALIZER)})
                    output_store.append("rankings", dict(lifecycle.format_row(item=item, ranked_candidate_ids=list(product_row["ranked_message_ids"]), trace=trace, product_row=product_row)))
                    output_store.append("traces", trace)
                    query_count += 1
                if query_count != reference["query_count"]:
                    raise OriginalProductError("stream query denominator drift")
                observer.checkpoint("after_queries")
                CandidateProjectionCursor(reference).verify()
                namespace = _stream_namespace_from_db(connection)
                try:
                    worker_physical = dynamic_original_index_build_receipt(palace_path=palace_path, expected_namespace=namespace, auditor=seams.auditor)
                finally:
                    namespace_rows = namespace.get("rows")
                    if isinstance(namespace_rows, _NamespaceRows):
                        namespace_rows.close()
                request_counts = _public_request_counts_stream(output_store.iter_ledger())
                resources = dict(observer.receipt())
                resources.update({
                    "ingest_seconds": ingest_seconds, "index_seconds": index_seconds,
                    "query_latency_seconds": _stream_sequence_reference(replicate_reference={"candidate_reference": reference}, table="measurements", count=reference["query_count"], sha256=_digest_sequence(output_store.iter_measurements())),
                    "query_measurements": _stream_sequence_reference(replicate_reference={"candidate_reference": reference}, table="measurements", count=reference["query_count"], sha256=_digest_sequence(output_store.iter_measurements())),
                    "clock_receipt": _clock_receipt() if not injected_clocks else {**_clock_receipt(), "timing_source": "injected_test_clock"},
                    "process_cpu_scope": "worker_process_only_excludes_descendants",
                    "descendant_observation": "external_supervisor_zero_required",
                    "native_internal_embedding_calls_observable": False,
                    "native_internal_embedding_limitation": "exact_public_product_uses_cached_native_callable; internal_embedding_calls_unobservable",
                })
                if formal:
                    runtime = getattr(seams.encoder, "runtime_identity", None)
                    expected_provider = {key: runtime[key] for key in ("model", "device", "providers")} if isinstance(runtime, Mapping) and {"model", "device", "providers"} <= set(runtime) else None
                    if expected_provider is None or resources.get("provider") != expected_provider:
                        raise OriginalProductError("formal resource provider receipt differs from native encoder")
                    passage = resources.get("passage_embedding"); query = resources.get("query_embedding")
                    if not isinstance(passage, Mapping) or not isinstance(query, Mapping) or passage.get("calls") != request_counts["passage_calls"] or passage.get("texts") != request_counts["passage_texts"] or passage.get("calls") != corpus_count or passage.get("texts") != candidate_count or query.get("calls") != request_counts["query_calls"] or query.get("texts") != request_counts["query_texts"] or query.get("calls") != reference["query_count"] or query.get("texts") != reference["query_count"]:
                        raise OriginalProductError("formal embedding telemetry differs from public request ledger")
                # The reference hash is replaced with the final persisted
                # replicate reference below; only scalar observer fields are
                # retained in the store metadata during publication.
                telemetry_base = {"formal_eligible": bool(formal), "live_receipt": dict(live_receipt or {}), "resources": {key: value for key, value in resources.items() if key not in {"query_latency_seconds", "query_measurements"}}}
                resources["candidate_index"] = candidate_index_peak.receipt()
                telemetry_base["resources"]["candidate_index"] = resources["candidate_index"]
                query_coverage = _digest_sequence({"item_id": item["item_id"], "query_sha256": hashlib.sha256(item["query_text"].encode("utf-8")).hexdigest()} for item in _stream_items(reference, corpus_ids=corpus_ids))
                output_coverage = _digest_sequence({"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in output_store.iter_traces())
                input_coverage = _digest_sequence(output_store.iter_item_corpora())
                index_receipt = {
                    "build_id": build_id, "fresh_build": True, "collection_identity": collection_identity,
                    "index_identity_sha256": _digest({"collection_identity": collection_identity, "physical": worker_physical}),
                    "cold_reopen": True, "call_contract": rank.ORIGINAL_CALL_CONTRACT,
                    "input_coverage_sha256": input_coverage, "query_coverage_sha256": query_coverage,
                    "output_coverage_sha256": output_coverage, "worker_physical_receipt": worker_physical,
                }
                published = output_store.finalize_worker(index_receipt=index_receipt, telemetry_base=telemetry_base)
                final_reference = published.as_reference()
                resources["query_latency_seconds"] = _stream_sequence_reference(replicate_reference=final_reference, table="measurements", count=reference["query_count"], sha256=_digest_sequence(output_store.iter_measurements()))
                resources["query_measurements"] = _stream_sequence_reference(replicate_reference=final_reference, table="measurements", count=reference["query_count"], sha256=_digest_sequence(output_store.iter_measurements()))
                resources["ledger"] = _stream_sequence_reference(replicate_reference=final_reference, table="ledger", count=int(final_reference["ledger_count"]), sha256=_digest_sequence(output_store.iter_ledger()))
                telemetry = {"schema": ORIGINAL_STREAM_TELEMETRY_SCHEMA, "formal_eligible": bool(formal), "live_receipt": dict(live_receipt or {}), "replicate_reference": final_reference, "replicate_reference_sha256": _digest(final_reference), "resources": resources}
                telemetry = _validate_stream_worker_telemetry(telemetry, reference)
                if resource_sink is not None:
                    resource_sink(telemetry)
                committed = True
                draft_namespace = {"schema": STREAM_NAMESPACE_SCHEMA, "candidate_reference": reference, "candidate_reference_sha256": _digest(reference), "corpus_count": corpus_count, "candidate_text_count": candidate_count, "query_count": reference["query_count"]}
                return OriginalProductWorkerDraft(projection=None, candidate_reference=reference, namespace=draft_namespace, replicate_without_coordinator_audit=published, worker_physical_receipt=worker_physical, telemetry=telemetry)
            finally:
                connection.close()
    finally:
        if not committed:
            output_store.close()
            for path in (output_store.database, output_store.ready_path, output_store.temporary):
                if path is not None:
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
        else:
            output_store.close()


def dynamic_original_index_build_receipt(*, palace_path: Path, expected_namespace: Mapping[str, Any], auditor: IndexAuditor | None = None) -> dict[str, Any]:
    """Dynamic audit with v2's direct SQLite/HNSW primitives in production.

    An injected auditor is accepted for synthetic tests only.  The result is the
    exact physical receipt required by the frozen AERP-7 rank wrapper.
    """
    namespace_rows = expected_namespace.get("rows") if isinstance(expected_namespace, Mapping) else None
    if isinstance(namespace_rows, _NamespaceRows):
        # The external SQLite ORDER BY is the same stable physical-ID order
        # used by the legacy list path.  Do not build a second corpus/message
        # dictionary for a full-census re-audit.  Chroma's public direct-read
        # seam still supplies its own ID list for byte/semantic verification.
        expected_ids = _PhysicalIDRows(namespace_rows)
        if expected_namespace.get("expected_unique_count") != len(expected_ids) or expected_namespace.get("mapping_sha256") != _digest_sequence(iter(namespace_rows)):
            raise OriginalProductError("original identity namespace receipt mismatch")
    else:
        grouped = _namespace_by_corpus(expected_namespace)
        expected_ids = sorted(physical for values in grouped.values() for physical in values.values())
    if auditor is not None:
        raw = dict(auditor(palace_path=palace_path, expected_namespace=expected_namespace))
    else:
        raw = _direct_dynamic_audit(palace_path=palace_path, expected_ids=expected_ids)
    required = {"physical_count", "physical_ids_sha256", "embedding", "hnsw_config", "graph_files", "immutable_backend_sha256", "immutable_non_length_backend_sha256", "immutable_residual_backend_sha256", "sqlite_semantic_sha256", "operational_delta", "direct_read_normalization_delta"}
    if set(raw) != required:
        raise OriginalProductError("original index audit receipt schema mismatch")
    expected = {"physical_count": len(expected_ids), "physical_ids_sha256": _digest_sequence(iter(expected_ids))}
    if any(raw[key] != value for key, value in expected.items()) or raw["hnsw_config"] != rank.ORIGINAL_HNSW_CONFIG or raw["operational_delta"] != rank.ORIGINAL_OPERATIONAL_DELTA:
        raise OriginalProductError("original index audit receipt mismatch")
    embedding = raw["embedding"]
    if not isinstance(embedding, Mapping) or embedding.get("count") != len(expected_ids) or embedding.get("dimension") != 384 or embedding.get("dtype") != "float32" or not isinstance(embedding.get("float32_sha256"), str):
        raise OriginalProductError("original embedding audit mismatch")
    names = [entry.get("name") for entry in raw["graph_files"] if isinstance(entry, Mapping)] if isinstance(raw["graph_files"], list) else []
    if names != list(rank.ORIGINAL_GRAPH_NAMES):
        raise OriginalProductError("original HNSW graph audit mismatch")
    try:
        rank._logical_original_physical_receipt(raw)
    except CustodyError as exc:
        raise OriginalProductError("original index normalization receipt mismatch") from exc
    return raw


def _hnsw_direct_read_normalization_delta(before: Any, after: Any) -> dict[str, Any] | None:
    """Classify the observed v3.8 direct-read byte normalizations.

    A fresh Chroma direct-read client may rewrite the canonical HNSW
    ``length.bin`` alone, or may rewrite the same-sized ``data_level0.bin`` and
    ``length.bin`` pair.  Neither transition is accepted unless every other
    persisted non-SQLite byte is unchanged; callers also recheck IDs, vectors,
    HNSW configuration, and SQLite semantics after the read.
    """
    if not isinstance(before, list) or not isinstance(after, list):
        return None
    required = {"path", "bytes", "sha256"}
    before_by_path = {
        row.get("path"): row
        for row in before
        if isinstance(row, Mapping) and set(row) == required and isinstance(row.get("path"), str)
    }
    after_by_path = {
        row.get("path"): row
        for row in after
        if isinstance(row, Mapping) and set(row) == required and isinstance(row.get("path"), str)
    }
    if len(before_by_path) != len(before) or len(after_by_path) != len(after) or set(before_by_path) != set(after_by_path):
        return None
    changed = [path for path in before_by_path if before_by_path[path] != after_by_path[path]]
    if not changed:
        return {
            "schema": "aerp7-hnsw-direct-read-normalization-v1",
            "status": "none",
            "path": None,
            "bytes": None,
            "before_sha256": None,
            "after_sha256": None,
        }
    data_level_paths = [
        path for path in before_by_path
        if path == "data_level0.bin" or path.endswith("/data_level0.bin")
    ]
    if len(data_level_paths) != 1:
        return None
    parent, separator, _name = data_level_paths[0].rpartition("/")
    canonical_length_path = f"{parent}{separator}length.bin"
    def same_sized_rewrite(path: str) -> bool:
        left, right = before_by_path[path], after_by_path[path]
        return (
            isinstance(left["bytes"], int)
            and not isinstance(left["bytes"], bool)
            and left["bytes"] > 0
            and left["bytes"] == right["bytes"]
            and all(
                isinstance(row["sha256"], str)
                and len(row["sha256"]) == 64
                and not any(char not in "0123456789abcdef" for char in row["sha256"])
                for row in (left, right)
            )
            and left["sha256"] != right["sha256"]
        )
    if changed == [canonical_length_path] and same_sized_rewrite(canonical_length_path):
        path = canonical_length_path
        left, right = before_by_path[path], after_by_path[path]
        return {
            "schema": "aerp7-hnsw-direct-read-normalization-v1",
            "status": "length_bin_same_size_rewrite",
            "path": path,
            "bytes": left["bytes"],
            "before_sha256": left["sha256"],
            "after_sha256": right["sha256"],
        }
    canonical_data_path = data_level_paths[0]
    if set(changed) != {canonical_data_path, canonical_length_path} or not all(
        same_sized_rewrite(path) for path in (canonical_data_path, canonical_length_path)
    ):
        return None
    # The v2 envelope retains both canonical transitions, binding each final
    # graph-file byte digest while rejecting every other file/size transition.
    path = canonical_data_path
    left, right = before_by_path[path], after_by_path[path]
    return {
        "schema": rank.PAIRED_DIRECT_READ_NORMALIZATION_SCHEMA,
        "status": "data_level0_and_length_same_size_rewrite",
        "transitions": [
            {"path": path, "bytes": left["bytes"], "before_sha256": left["sha256"], "after_sha256": right["sha256"]}
            for path, left, right in (
                (canonical_data_path, before_by_path[canonical_data_path], after_by_path[canonical_data_path]),
                (canonical_length_path, before_by_path[canonical_length_path], after_by_path[canonical_length_path]),
            )
        ],
    }


def _stream_chroma_embedding_receipt(
    *, collection: Any, expected_ids: Sequence[str], temporary_parent: Path,
) -> tuple[str, int, int, str]:
    """Read a Chroma collection in bounded pages and digest it externally.

    The v3.8 direct audit historically called ``collection.get`` once and then
    sorted two full Python lists.  That is semantically simple but makes the
    audit's memory scale with the entire Chroma collection.  A finite public
    ``get(limit, offset, include=["embeddings"])`` page is now copied into a
    temporary SQLite key/value table.  SQLite owns the external sort; Python
    only retains one Chroma page and one vector at a time.  The final digest
    byte layout is deliberately identical to v2's
    ``_float32_embedding_digest`` and the physical-ID digest is the canonical
    JSON digest of sorted IDs used by the old path.
    """
    batch_size = ORIGINAL_CHROMA_AUDIT_BATCH_SIZE
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise OriginalProductError("original Chroma audit batch size is invalid")
    parent = Path(temporary_parent)
    if not parent.is_dir():
        raise OriginalProductError("original Chroma audit temporary parent is unavailable")
    with tempfile.TemporaryDirectory(prefix=".aerp7-chroma-audit-", dir=parent) as temporary:
        database = Path(temporary) / "embeddings.sqlite3"
        connection = sqlite3.connect(database)
        try:
            # This is a disposable audit spool.  Disable rollback/WAL growth
            # and cap SQLite's page cache so the external sort remains a disk
            # operation rather than an unbounded process allocation.
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-4096")
            connection.execute(
                "CREATE TABLE embedding (physical_id TEXT PRIMARY KEY, vector BLOB NOT NULL, dimension INTEGER NOT NULL)"
            )
            offset = 0
            dimension: int | None = None
            while True:
                # Always pass a finite limit.  This is intentionally explicit
                # instead of relying on a Chroma client's default, which is
                # allowed to materialize the whole collection.
                try:
                    stored = collection.get(
                        include=["embeddings"], limit=batch_size, offset=offset,
                    )
                except TypeError as exc:
                    raise OriginalProductError("original Chroma paginated get is unavailable") from exc
                if not isinstance(stored, Mapping):
                    raise OriginalProductError("original Chroma paginated result is malformed")
                ids = stored.get("ids")
                if not isinstance(ids, list):
                    raise OriginalProductError("original Chroma physical IDs differ from dynamic namespace")
                if len(ids) > batch_size:
                    raise OriginalProductError("original Chroma paginated result exceeded finite batch")
                if not ids:
                    if offset == 0:
                        raise OriginalProductError("original Chroma stored embeddings are missing")
                    break
                embeddings = stored.get("embeddings")
                try:
                    vectors = embeddings.tolist()
                except AttributeError:
                    try:
                        vectors = list(embeddings)
                    except TypeError as exc:
                        raise OriginalProductError("original Chroma stored embeddings are missing") from exc
                except TypeError as exc:
                    raise OriginalProductError("original Chroma stored embeddings are missing") from exc
                if len(vectors) != len(ids):
                    raise OriginalProductError("original Chroma stored embeddings are missing")
                for physical_id, vector in zip(ids, vectors):
                    if not isinstance(physical_id, str) or not physical_id:
                        raise OriginalProductError("original Chroma stored physical ID is malformed")
                    try:
                        values = [float(value) for value in vector]
                    except (TypeError, ValueError) as exc:
                        raise OriginalProductError("original Chroma stored embedding is malformed") from exc
                    if not values or any(not math.isfinite(value) for value in values):
                        raise OriginalProductError("original Chroma stored embedding is malformed")
                    if dimension is None:
                        dimension = len(values)
                    elif len(values) != dimension:
                        raise OriginalProductError("original Chroma stored embedding dimensions drifted")
                    try:
                        encoded = b"".join(struct.pack("<f", value) for value in values)
                    except (OverflowError, struct.error) as exc:
                        raise OriginalProductError("original Chroma stored embedding is malformed") from exc
                    try:
                        connection.execute(
                            "INSERT INTO embedding (physical_id, vector, dimension) VALUES (?, ?, ?)",
                            (physical_id, sqlite3.Binary(encoded), len(values)),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise OriginalProductError("original Chroma physical IDs are duplicated") from exc
                offset += len(ids)
                # Chroma's paginated contract returns a short final page.
                # Do not issue a speculative second read: a fake or older
                # client may repeat the last short page rather than return an
                # empty page, which would look like a duplicate collection.
                if len(ids) < batch_size:
                    break
            if dimension is None:
                raise OriginalProductError("original Chroma stored embeddings are missing")
            connection.commit()

            id_sink = _HashSink(); id_sink.write(b"[")
            embedding_digest = hashlib.sha256()
            expected_iter = iter(expected_ids)
            first_id = True
            count = 0
            for physical_id, encoded, row_dimension in connection.execute(
                "SELECT physical_id, vector, dimension FROM embedding ORDER BY physical_id"
            ):
                if row_dimension != dimension:
                    raise OriginalProductError("original Chroma stored embedding dimensions drifted")
                try:
                    expected_id = next(expected_iter)
                except StopIteration as exc:
                    raise OriginalProductError("original Chroma physical IDs differ from dynamic namespace") from exc
                if physical_id != expected_id:
                    raise OriginalProductError("original Chroma physical IDs differ from dynamic namespace")
                if not first_id:
                    id_sink.write(b",")
                id_sink.write(_canonical_bytes(physical_id))
                first_id = False
                encoded_id = physical_id.encode("utf-8")
                embedding_digest.update(struct.pack("<I", len(encoded_id)))
                embedding_digest.update(encoded_id)
                embedding_digest.update(struct.pack("<I", dimension))
                embedding_digest.update(bytes(encoded))
                count += 1
            try:
                next(expected_iter)
            except StopIteration:
                pass
            else:
                raise OriginalProductError("original Chroma physical IDs differ from dynamic namespace")
            id_sink.write(b"]")
            return embedding_digest.hexdigest(), count, dimension, id_sink.hexdigest()
        finally:
            connection.close()


def _direct_dynamic_audit(*, palace_path: Path, expected_ids: Sequence[str]) -> dict[str, Any]:
    """Production direct audit; opens no original-product helper and proves bytes."""
    before_config = v2._sqlite_hnsw_configuration(palace_path)
    before_storage = v2._audit_storage_digest(palace_path)
    before_sqlite = v2._sqlite_semantic_snapshot(palace_path)
    try:
        import chromadb
    except ImportError as exc:  # pragma: no cover - production dependency
        raise OriginalProductError("Chroma direct read API is unavailable") from exc
    client = chromadb.PersistentClient(path=str(palace_path))
    try:
        collection = client.get_collection(v1.ORIGINAL_COLLECTION)
        vector_sha, count, dimension, physical_ids_sha = _stream_chroma_embedding_receipt(
            collection=collection, expected_ids=expected_ids, temporary_parent=palace_path.parent,
        )
    finally:
        close = getattr(client, "close", None)
        if not callable(close):
            raise OriginalProductError("Chroma direct read client has no close hook")
        close()
    after_storage = v2._audit_storage_digest(palace_path)
    after_sqlite = v2._sqlite_semantic_snapshot(palace_path)
    after_config = v2._sqlite_hnsw_configuration(palace_path)
    normalization_delta = _hnsw_direct_read_normalization_delta(
        before_storage["immutable_snapshot"], after_storage["immutable_snapshot"]
    )
    if normalization_delta is None or after_config != before_config:
        raise OriginalProductError(
            "direct original index audit mutated persisted index: "
            f"immutable_before={_canonical_bytes(before_storage['immutable_snapshot']).decode('utf-8')}; "
            f"immutable_after={_canonical_bytes(after_storage['immutable_snapshot']).decode('utf-8')}; "
            f"config_before={_canonical_bytes(before_config).decode('utf-8')}; "
            f"config_after={_canonical_bytes(after_config).decode('utf-8')}"
        )
    snapshot_by_path = {row["path"]: row for row in after_storage["immutable_snapshot"]}
    data_level_paths = [
        path for path in snapshot_by_path
        if path == "data_level0.bin" or path.endswith("/data_level0.bin")
    ]
    if len(data_level_paths) != 1:
        raise OriginalProductError("direct original index audit canonical HNSW segment mismatch")
    parent, separator, _name = data_level_paths[0].rpartition("/")
    graph_files = []
    for name in rank.ORIGINAL_GRAPH_NAMES:
        path = f"{parent}{separator}{name}"
        entry = snapshot_by_path.get(path)
        if not isinstance(entry, Mapping) or set(entry) != {"path", "bytes", "sha256"}:
            raise OriginalProductError("direct original index audit canonical HNSW graph mismatch")
        graph_files.append({"name": name, **entry})
    length_path = f"{parent}{separator}length.bin"
    non_length_snapshot = [
        row for row in after_storage["immutable_snapshot"]
        if row["path"] != length_path
    ]
    if len(non_length_snapshot) + 1 != len(after_storage["immutable_snapshot"]):
        raise OriginalProductError("direct original index audit non-length HNSW snapshot mismatch")
    residual_snapshot = [
        row for row in after_storage["immutable_snapshot"]
        if row["path"] not in {data_level_paths[0], length_path}
    ]
    if len(residual_snapshot) + 2 != len(after_storage["immutable_snapshot"]):
        raise OriginalProductError("direct original index audit residual HNSW snapshot mismatch")
    return {
        "physical_count": count, "physical_ids_sha256": physical_ids_sha,
        "embedding": {"count": count, "dimension": dimension, "dtype": "float32", "float32_sha256": vector_sha},
        "hnsw_config": after_config, "graph_files": graph_files,
        "immutable_backend_sha256": after_storage["immutable_sha256"],
        "immutable_non_length_backend_sha256": v2.canonical_sha256(non_length_snapshot),
        "immutable_residual_backend_sha256": v2.canonical_sha256(residual_snapshot),
        "sqlite_semantic_sha256": before_sqlite["semantic_sha256"],
        "operational_delta": v2._validated_acquire_write_delta(before_sqlite, after_sqlite),
        "direct_read_normalization_delta": normalization_delta,
    }


@contextmanager
def pinned_live_original_product(*, original_root: Path, model_dir: Path, palace_path: Path):
    """Yield the pinned public product while its required environment stays live."""
    with v1.pinned_original_environment() as environment:
        model_before = v1.file_tree_receipt(model_dir)
        palace, searcher, _protocol, original_state = v1.load_original_product(original_root)
        encoder = v1.native_minilm_adapter(model_dir)
        if v1.file_tree_receipt(model_dir) != model_before:
            raise OriginalProductError("native MiniLM initialization changed the pinned model tree")
        # ``assert`` forces backend/model configuration before any upsert.
        config = v1.assert_original_product_configuration(palace, palace_path=palace_path, encoder=encoder)
        receipt = {"original_git_before": original_state, "pinned_environment": environment, "product_config": config, "model": encoder.runtime_identity, "model_file_tree_before": model_before}
        capability = _LivePinnedCapability(receipt=receipt)
        _LIVE_CAPABILITIES.add(id(capability))
        seams = OriginalProductSeams(palace=palace, searcher=searcher, reset_backends=v1.reset_original_product_backends, auditor=dynamic_original_index_build_receipt, provenance=LIVE_PINNED, encoder=encoder, _live_capability=capability)
        try:
            yield seams, receipt
        finally:
            try:
                original_after, model_after = v1.git_state(original_root), v1.file_tree_receipt(model_dir)
                if original_after != original_state or model_after != model_before:
                    raise OriginalProductError("live original code/model receipt drifted during worker execution")
            finally:
                _LIVE_CAPABILITIES.discard(id(capability))


def _validate_live_provenance(*, seams: OriginalProductSeams, live_receipt: Mapping[str, Any] | None) -> None:
    capability = seams._live_capability
    if seams.provenance != LIVE_PINNED or capability is None or id(capability) not in _LIVE_CAPABILITIES:
        raise OriginalProductError("formal execution requires a live-pinned original-product seam")
    if not isinstance(live_receipt, Mapping) or dict(live_receipt) != dict(capability.receipt):
        raise OriginalProductError("formal live original receipt does not bind the live-pinned seam")
    original = live_receipt.get("original_git_before")
    config = live_receipt.get("product_config")
    model = live_receipt.get("model")
    if not isinstance(original, Mapping) or original.get("git_dirty") is not False:
        raise OriginalProductError("formal original receipt is not clean")
    if not isinstance(config, Mapping) or config.get("backend") != "chroma" or config.get("collection") != v1.ORIGINAL_COLLECTION:
        raise OriginalProductError("formal original product configuration receipt is malformed")
    if not isinstance(model, Mapping) or model.get("device") != "cpu" or model.get("providers") != ["CPUExecutionProvider"]:
        raise OriginalProductError("formal model/provider receipt is malformed")
    if seams.encoder is None or getattr(seams.encoder, "runtime_identity", None) != model:
        raise OriginalProductError("formal seam encoder differs from live model/provider receipt")


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OriginalProductError(f"resource telemetry {label} must be a non-negative integer")
    return value


def _public_request_counts(ledger: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    upserts = [entry for entry in ledger if entry.get("event") == "upsert"]
    searches = [entry for entry in ledger if entry.get("event") == "search"]
    if not upserts or not searches or any(_nonnegative_int(entry.get("count"), "upsert count") <= 0 for entry in upserts):
        raise OriginalProductError("public request ledger is incomplete")
    return {"passage_calls": len(upserts), "passage_texts": sum(_nonnegative_int(entry["count"], "upsert count") for entry in upserts), "query_calls": len(searches), "query_texts": len(searches)}


def _public_request_counts_stream(ledger: Iterator[Mapping[str, Any]]) -> dict[str, int]:
    passage_calls = passage_texts = query_calls = query_texts = 0
    for entry in ledger:
        event = entry.get("event")
        if event == "upsert":
            passage_calls += 1
            passage_texts += _nonnegative_int(entry.get("count"), "upsert count")
        elif event == "search":
            query_calls += 1
            query_texts += 1
    if passage_calls <= 0 or passage_texts <= 0 or query_calls <= 0:
        raise OriginalProductError("public request ledger is incomplete")
    return {"passage_calls": passage_calls, "passage_texts": passage_texts, "query_calls": query_calls, "query_texts": query_texts}


def _validate_resource_telemetry(*, value: Mapping[str, Any], seams: OriginalProductSeams, formal: bool, public_request_counts: Mapping[str, int], expected_query_count: int, expected_corpus_count: int, expected_candidate_count: int) -> dict[str, Any]:
    """Bind formal resource receipts to observable public product request ledger."""
    row = dict(value)
    required = {"peak_rss_bytes", "storage_bytes", "passage_embedding", "query_embedding", "provider"}
    if not required <= set(row):
        raise OriginalProductError("resource observer lacks actual RSS/storage/native-embedding/provider measurement")
    for key in ("peak_rss_bytes", "storage_bytes"):
        _nonnegative_int(row[key], key)
    required_sidecars = {"query_measurements", "clock_receipt", "process_cpu_scope", "descendant_observation"}
    if required_sidecars & set(row) and not required_sidecars <= set(row):
        raise OriginalProductError("resource telemetry sidecar schema is malformed")
    if required_sidecars <= set(row):
        _validate_query_measurement_rows(row["query_measurements"])
        _validate_clock_receipt(row["clock_receipt"])
        if row["process_cpu_scope"] != "worker_process_only_excludes_descendants" or row["descendant_observation"] != "external_supervisor_zero_required":
            raise OriginalProductError("resource telemetry process CPU scope is invalid")
    for key in ("passage_embedding", "query_embedding"):
        metric = row[key]
        if not isinstance(metric, Mapping) or set(metric) != {"calls", "texts", "measurement"}:
            raise OriginalProductError(f"resource telemetry {key} is malformed")
        _nonnegative_int(metric.get("calls"), f"{key}.calls"); _nonnegative_int(metric.get("texts"), f"{key}.texts")
        if not isinstance(metric.get("measurement"), str) or not metric["measurement"].strip():
            raise OriginalProductError(f"resource telemetry {key} measurement is missing")
    provider = row["provider"]
    if not isinstance(provider, Mapping) or set(provider) != {"model", "device", "providers"} or provider.get("device") != "cpu" or provider.get("providers") != ["CPUExecutionProvider"] or not isinstance(provider.get("model"), str):
        raise OriginalProductError("resource telemetry provider receipt is malformed")
    if formal:
        runtime = getattr(seams.encoder, "runtime_identity", None)
        if not isinstance(runtime, Mapping) or provider != {key: runtime[key] for key in ("model", "device", "providers")}:
            raise OriginalProductError("formal resource provider receipt differs from native encoder")
        expected = {
            "passage_embedding": ("passage_calls", "passage_texts", PUBLIC_UPSERT_MEASUREMENT, expected_corpus_count, expected_candidate_count),
            "query_embedding": ("query_calls", "query_texts", PUBLIC_SEARCH_MEASUREMENT, expected_query_count, expected_query_count),
        }
        for telemetry_key, (call_key, text_key, measurement, expected_calls, expected_texts) in expected.items():
            metric = row[telemetry_key]
            if metric["measurement"] != measurement or metric["calls"] != public_request_counts.get(call_key) or metric["texts"] != public_request_counts.get(text_key) or metric["calls"] != expected_calls or metric["texts"] != expected_texts:
                raise OriginalProductError("formal embedding telemetry differs from public request ledger")
        if row["passage_embedding"]["texts"] <= 0:
            raise OriginalProductError("formal passage embedding text count must be positive")
    return row


def run_original_public_replicate(*, projection: Any, build_id: str, collection_identity: str, palace_path: Path, observer: ResourceObserver | None, seams: OriginalProductSeams, live_receipt: Mapping[str, Any] | None = None, formal: bool = False, resource_sink: Callable[[Mapping[str, Any]], None] | None = None, wall_clock_ns: Callable[[], int] | None = None, cpu_clock_ns: Callable[[], int] | None = None, lifecycle_adapter: original_core.LifecycleAdapter | None = None) -> OriginalProductWorkerDraft:
    """Run the worker phase; a coordinator audit is mandatory before publication.

    Return a deliberately non-publishable draft.  The only function that creates
    an exact ``rank.wrap_original_public_rankings`` replicate is
    ``coordinator_reaudit_replicate`` below.
    """
    if observer is None:
        raise OriginalProductError("a real resource observer is required")
    injected_clocks = wall_clock_ns is not None or cpu_clock_ns is not None
    if formal and injected_clocks:
        raise OriginalProductError("formal query clocks must be stdlib")
    if formal:
        _validate_live_provenance(seams=seams, live_receipt=live_receipt)
        if resource_sink is None:
            raise OriginalProductError("formal original worker requires a resource sink")
    elif seams.provenance != SYNTHETIC_INJECTED or seams._live_capability is not None:
        raise OriginalProductError("non-formal execution requires an explicitly synthetic injected seam")
    lifecycle = convomem_lifecycle_adapter() if lifecycle_adapter is None else lifecycle_adapter
    frozen = dict(lifecycle.validate_projection(projection))
    runtime = dict(lifecycle.runtime_projection(frozen))
    if not isinstance(runtime.get("corpora"), list) or not isinstance(runtime.get("items"), list):
        raise OriginalProductError("lifecycle adapter runtime projection is malformed")
    namespace = dict(lifecycle.identity_namespace(frozen))
    by_corpus = _namespace_by_corpus(namespace)
    if len({build_id, collection_identity}) != 2 or not all(isinstance(value, str) and value.strip() for value in (build_id, collection_identity)):
        raise OriginalProductError("build/collection identity must be distinct non-empty strings")
    wall_clock_ns = time.perf_counter_ns if wall_clock_ns is None else wall_clock_ns
    cpu_clock_ns = time.process_time_ns if cpu_clock_ns is None else cpu_clock_ns
    if not callable(wall_clock_ns) or not callable(cpu_clock_ns):
        raise OriginalProductError("query timing clocks must be callable")
    ledger: list[dict[str, Any]] = []
    observer.checkpoint("before_ingest")
    started = time.perf_counter()
    for corpus in sorted(runtime["corpora"], key=lambda row: row["corpus_id"]):
        ingest_corpus_once(palace=seams.palace, palace_path=palace_path, corpus=corpus, physical_by_message=by_corpus[corpus["corpus_id"]], ledger=ledger)
    ingest_seconds = time.perf_counter() - started
    observer.checkpoint("after_ingest")
    index_started = time.perf_counter()
    cleanup = dict(seams.reset_backends(palace_path))
    index_seconds = time.perf_counter() - index_started
    if cleanup.get("verified_system_released") is not True:
        raise OriginalProductError("original product did not prove cold close/reset")
    ledger.append({"event": "cold_reopen_barrier", "cleanup": cleanup})
    observer.checkpoint("after_cold_close")
    corpora = {row["corpus_id"]: row for row in runtime["corpora"]}
    rows, traces, query_latencies, query_measurements = [], [], [], []
    for item in sorted(runtime["items"], key=lambda row: row["item_id"]):
        product_row, trace = _row_for_query(
            item=item,
            corpus=corpora[item["corpus_id"]],
            physical_by_message=by_corpus[item["corpus_id"]],
            searcher=seams.searcher,
            palace_path=palace_path,
            ledger=ledger,
            latency_seconds=0.0,
            wall_clock_ns=wall_clock_ns,
            cpu_clock_ns=cpu_clock_ns,
            query_measurements=query_measurements,
        )
        query_latencies.append(query_measurements[-1]["wall_ns"] / 1_000_000_000)
        rows.append(dict(lifecycle.format_row(item=item, ranked_candidate_ids=list(product_row["ranked_message_ids"]), trace=trace, product_row=product_row))); traces.append(trace)
    observer.checkpoint("after_queries")
    worker_physical = dynamic_original_index_build_receipt(palace_path=palace_path, expected_namespace=namespace, auditor=seams.auditor)
    request_counts = _public_request_counts(ledger)
    resources = dict(observer.receipt())
    resources.update({
        "ingest_seconds": ingest_seconds,
        "index_seconds": index_seconds,
        "query_latency_seconds": query_latencies,
        "query_measurements": sorted(query_measurements, key=lambda item: item["item_id"]),
        "clock_receipt": _clock_receipt() if not injected_clocks else {**_clock_receipt(), "timing_source": "injected_test_clock"},
        "process_cpu_scope": "worker_process_only_excludes_descendants",
        "descendant_observation": "external_supervisor_zero_required",
        "native_internal_embedding_calls_observable": False,
        "native_internal_embedding_limitation": "exact_public_product_uses_cached_native_callable; internal_embedding_calls_unobservable",
    })
    resources = _validate_resource_telemetry(value=resources, seams=seams, formal=formal, public_request_counts=request_counts, expected_query_count=len(runtime["items"]), expected_corpus_count=len(runtime["corpora"]), expected_candidate_count=sum(len(corpus["candidates"]) for corpus in runtime["corpora"]))
    input_receipt = dict(lifecycle.input_receipt(frozen))
    query_coverage = _digest([{"item_id": item["item_id"], "query_sha256": hashlib.sha256(item["query_text"].encode("utf-8")).hexdigest()} for item in sorted(frozen["items"], key=lambda row: row["item_id"])])
    output_coverage = _digest([{"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in traces])
    index_receipt = {
        "build_id": build_id, "fresh_build": True, "collection_identity": collection_identity,
        "index_identity_sha256": _digest({"collection_identity": collection_identity, "physical": worker_physical}),
        "cold_reopen": True, "call_contract": rank.ORIGINAL_CALL_CONTRACT,
        "input_coverage_sha256": _digest(input_receipt["item_corpora"]), "query_coverage_sha256": query_coverage,
        "output_coverage_sha256": output_coverage, "worker_physical_receipt": worker_physical,
    }
    value = {"build_id": build_id, "input_receipt": input_receipt, "input_sha256": _digest(input_receipt), "index_receipt": index_receipt, "index_sha256": _digest(index_receipt), "trace_receipt": traces, "trace_sha256": _digest(traces), "rankings": rows}
    telemetry = {"formal_eligible": bool(formal), "live_receipt": dict(live_receipt or {}), "ledger": ledger, "resources": resources}
    if resource_sink is not None:
        resource_sink(telemetry)
    return OriginalProductWorkerDraft(projection=frozen, namespace=namespace, replicate_without_coordinator_audit=value, worker_physical_receipt=worker_physical, telemetry=telemetry)


def coordinator_reaudit_replicate(*, draft: OriginalProductWorkerDraft, palace_path: Path, projection: Any, auditor: IndexAuditor | None = None, lifecycle_adapter: original_core.LifecycleAdapter | None = None) -> dict[str, Any]:
    """Independently remeasure, then produce the sole publishable replica.

    A worker cannot fill its own coordinator receipt: passing a Mapping/replica
    rather than the opaque draft fails closed before any audit runs.
    """
    if not isinstance(draft, OriginalProductWorkerDraft):
        raise OriginalProductError("coordinator requires an original-product worker draft")
    lifecycle = convomem_lifecycle_adapter() if lifecycle_adapter is None else lifecycle_adapter
    frozen = dict(lifecycle.validate_projection(projection))
    if frozen != draft.projection or dict(lifecycle.identity_namespace(frozen)) != draft.namespace:
        raise OriginalProductError("coordinator projection/namespace drift")
    measured = dynamic_original_index_build_receipt(palace_path=palace_path, expected_namespace=draft.namespace, auditor=auditor)
    worker = draft.worker_physical_receipt
    try:
        worker_scientific, measured_scientific = rank._joint_original_physical_receipts(worker, measured)
    except CustodyError as exc:
        try:
            worker_detail = _canonical_bytes(rank._logical_original_physical_receipt(worker)).decode("utf-8")
        except CustodyError:
            worker_detail = "<invalid>"
        try:
            measured_detail = _canonical_bytes(rank._logical_original_physical_receipt(measured)).decode("utf-8")
        except CustodyError:
            measured_detail = "<invalid>"
        raise OriginalProductError(
            "worker/coordinator physical index normalization receipt mismatch: "
            f"worker_scientific={worker_detail}; measured_scientific={measured_detail}"
        ) from exc
    if worker_scientific != measured_scientific:
        raise OriginalProductError(
            "worker/coordinator physical index receipt mismatch: "
            f"worker_scientific={_canonical_bytes(worker_scientific).decode('utf-8')}; "
            f"measured_scientific={_canonical_bytes(measured_scientific).decode('utf-8')}"
        )
    raw = dict(draft.replicate_without_coordinator_audit)
    index = raw.get("index_receipt")
    if not isinstance(index, Mapping) or set(index) != {"build_id", "fresh_build", "collection_identity", "index_identity_sha256", "cold_reopen", "call_contract", "input_coverage_sha256", "query_coverage_sha256", "output_coverage_sha256", "worker_physical_receipt"}:
        raise OriginalProductError("worker draft index receipt is malformed")
    completed_index = dict(index)
    completed_index["coordinator_physical_receipt"] = measured
    raw["index_receipt"] = completed_index
    raw["index_sha256"] = _digest(completed_index)
    # This now has exactly the frozen original-replicate schema and is the
    # required handoff to the five-build rank wrapper.
    lifecycle.validate_completed_replicate(frozen, raw)
    return raw


def _stream_reference_namespace(reference: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild physical namespace through a disk-backed ordered cursor."""
    checked = validate_candidate_projection_reference(reference)
    owner = tempfile.TemporaryDirectory(prefix=".aerp7-original-namespace-", dir=Path(checked["bundle_path"]).parent)
    connection = sqlite3.connect(Path(owner.name) / "namespace.sqlite3")
    try:
        connection.execute("CREATE TABLE namespace_rows(corpus_id TEXT NOT NULL, message_id TEXT NOT NULL, physical_id TEXT PRIMARY KEY)")
        count = 0
        for corpus in _stream_corpora(checked):
            corpus_id = corpus["corpus_id"]
            for candidate in corpus["candidates"]:
                physical_id = _physical_id(corpus_id, candidate["message_id"])
                try:
                    connection.execute("INSERT INTO namespace_rows VALUES (?, ?, ?)", (corpus_id, candidate["message_id"], physical_id))
                except sqlite3.IntegrityError as exc:
                    raise OriginalProductError("stream coordinator identity namespace duplicate") from exc
                count += 1
        connection.commit()
        if count != checked["candidate_text_count"]:
            raise OriginalProductError("stream coordinator candidate denominator drift")
        rows = _NamespaceRows(connection, count=count, owner=owner)
        namespace = {
        "schema": "aerp7-original-identity-namespace-v1",
        "scheme": "corpus_id::aerp7::message_id",
        "rows": rows,
        "expected_unique_count": count,
        "mapping_sha256": _digest_sequence(iter(rows)),
        }
        return namespace
    except BaseException:
        connection.close(); owner.cleanup()
        raise


def _validate_stream_replicate(*, replicate: Mapping[str, Any], reference: Mapping[str, Any], namespace: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(replicate, OriginalReplicateReference):
        replicate = replicate.as_reference()
    raw_reference = validate_original_replicate_reference(replicate)
    if raw_reference["candidate_reference"] != dict(reference) or raw_reference["state"] not in {"worker_complete", "coordinator_complete"}:
        raise OriginalProductError("stream worker input/reference binding mismatch")
    with OriginalReplicateStore.open(raw_reference) as store:
        if store.build_id != raw_reference["build_id"] or store.input_sha256 != raw_reference["input_sha256"] or store.trace_sha256 != raw_reference["trace_sha256"]:
            raise OriginalProductError("stream worker replicate digest mismatch")
        item_rows = store.iter_item_corpora()
        previous_item = None; item_count = 0
        for row in item_rows:
            if not isinstance(row.get("item_id"), str) or (previous_item is not None and row["item_id"] <= previous_item):
                raise OriginalProductError("stream worker input coverage mismatch")
            previous_item = row["item_id"]; item_count += 1
        if item_count != reference["query_count"]:
            raise OriginalProductError("stream worker input coverage mismatch")
        previous_rank = None; rank_count = 0
        for row in store.iter_rankings():
            if not isinstance(row.get("item_id"), str) or (previous_rank is not None and row["item_id"] <= previous_rank):
                raise OriginalProductError("stream worker ranking order mismatch")
            previous_rank = row["item_id"]; rank_count += 1
        previous_trace = None; trace_count = 0
        for row in store.iter_traces():
            if not isinstance(row.get("item_id"), str) or (previous_trace is not None and row["item_id"] <= previous_trace):
                raise OriginalProductError("stream worker trace order mismatch")
            previous_trace = row["item_id"]; trace_count += 1
        if rank_count != reference["query_count"] or trace_count != reference["query_count"]:
            raise OriginalProductError("stream worker query coverage mismatch")
        rank_iter = store.iter_rankings(); trace_iter = store.iter_traces()
        for ranking, trace in zip(rank_iter, trace_iter):
            if ranking.get("item_id") != trace.get("item_id") or ranking.get("query_sha256") != trace.get("query_sha256") or trace.get("ranking_sha256") != _digest(ranking.get("ranked_message_ids")):
                raise OriginalProductError("stream worker ranking/trace semantic mismatch")
        corpus_ids = {corpus["corpus_id"] for corpus in _stream_corpora(reference)}
        expected_query_coverage = _digest_sequence({"item_id": item["item_id"], "query_sha256": hashlib.sha256(item["query_text"].encode("utf-8")).hexdigest()} for item in _stream_items(reference, corpus_ids=corpus_ids))
        expected_output_coverage = _digest_sequence({"item_id": trace["item_id"], "ranking_sha256": trace["ranking_sha256"]} for trace in store.iter_traces())
        expected_input_coverage = _digest_sequence(iter(store.iter_item_corpora()))
    with OriginalReplicateStore.open(raw_reference) as store:
        index = store.index_receipt
    allowed_index = {"build_id", "fresh_build", "collection_identity", "index_identity_sha256", "cold_reopen", "call_contract", "input_coverage_sha256", "query_coverage_sha256", "output_coverage_sha256", "worker_physical_receipt"}
    if not isinstance(index, Mapping) or set(index) not in (allowed_index, allowed_index | {"coordinator_physical_receipt"}) or index.get("fresh_build") is not True or index.get("cold_reopen") is not True or raw_reference["index_sha256"] != _digest(index):
        raise OriginalProductError("stream worker index receipt is malformed")
    if index.get("input_coverage_sha256") != expected_input_coverage or index.get("query_coverage_sha256") != expected_query_coverage or index.get("output_coverage_sha256") != expected_output_coverage:
        raise OriginalProductError("stream worker coverage digest mismatch")
    return raw_reference


def coordinator_reaudit_streaming_replicate(
    *, draft: OriginalProductWorkerDraft, candidate_reference: Mapping[str, Any] | None = None,
    palace_path: Path, auditor: IndexAuditor | None = None,
) -> dict[str, Any]:
    """Re-open the immutable reference and produce the stream replica handoff."""
    if not isinstance(draft, OriginalProductWorkerDraft) or draft.candidate_reference is None or draft.projection is not None:
        raise OriginalProductError("stream coordinator requires a reference-only worker draft")
    supplied = draft.candidate_reference if candidate_reference is None else candidate_reference
    reference = validate_candidate_projection_reference(supplied)
    if reference != dict(draft.candidate_reference):
        raise OriginalProductError("stream coordinator reference drift")
    if draft.namespace.get("candidate_reference_sha256") != _digest(reference):
        raise OriginalProductError("stream coordinator namespace/reference drift")
    # A retry after a completed coordinator handoff is idempotent: the worker
    # draft still names the immutable worker ref, while READY now points at the
    # completed ref.  Return that exact published ref instead of mutating rows
    # a second time.
    if isinstance(draft.replicate_without_coordinator_audit, OriginalReplicateReference):
        worker_reference = draft.replicate_without_coordinator_audit.as_reference()
        try:
            validate_original_replicate_reference(worker_reference)
        except OriginalProductError:
            try:
                completed_reference = load_original_replicate_reference(worker_reference["ready_path"])
            except OriginalProductError:
                completed_reference = None
            if completed_reference is not None and completed_reference["state"] == "coordinator_complete" and completed_reference["build_id"] == worker_reference["build_id"] and completed_reference["candidate_reference"] == reference:
                return OriginalReplicateReference(completed_reference)
            # A database commit can succeed while the READY replace is
            # interrupted.  In that state the stale worker READY is not
            # sufficient to identify the durable result, but the store itself
            # is already coordinator-complete.  Rebuild the reference from
            # scalar metadata and repair the sidecar atomically; no ranking,
            # trace, or telemetry sequence is loaded into Python.
            try:
                with OriginalReplicateStore.open(worker_reference) as store:
                    current = store.current_reference()
                    if current["state"] == "coordinator_complete" and current["build_id"] == worker_reference["build_id"] and current["candidate_reference"] == reference:
                        return store.republish_ready(current)
            except OriginalProductError:
                pass
    namespace = _stream_reference_namespace(reference)
    try:
        measured = dynamic_original_index_build_receipt(palace_path=palace_path, expected_namespace=namespace, auditor=auditor)
        worker = draft.worker_physical_receipt
        try:
            worker_scientific, measured_scientific = rank._joint_original_physical_receipts(worker, measured)
        except CustodyError as exc:
            raise OriginalProductError("stream worker/coordinator physical index receipt mismatch") from exc
        if worker_scientific != measured_scientific:
            raise OriginalProductError("stream worker/coordinator physical index receipt mismatch")
        raw_reference = _validate_stream_replicate(replicate=draft.replicate_without_coordinator_audit, reference=reference, namespace=namespace)
        with OriginalReplicateStore.open(raw_reference) as store:
            return store.complete_coordinator(coordinator_receipt=measured)
    finally:
        rows = namespace.get("rows") if isinstance(namespace, Mapping) else None
        if isinstance(rows, _NamespaceRows):
            rows.close()
