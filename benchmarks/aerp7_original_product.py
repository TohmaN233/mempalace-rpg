"""AERP-7 exact original MemPalace public-product worker primitives.

This module deliberately contains no ConvoMem source/custody loading.  Its only
input is the already validated candidate-safe projection.  The formal executor
owns process isolation, manifests, publication and the eventual real-data run;
this module owns the product-call and persisted-index contract used by that
executor.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from benchmarks import aerp5_product_paired_locomo as v1
from benchmarks import aerp5_product_paired_locomo_v2 as v2
from benchmarks import aerp7_convomem_rank as rank
from benchmarks.aerp7_convomem_confirmation import CustodyError
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
_LIVE_CAPABILITIES: set[int] = set()


class OriginalProductError(RuntimeError):
    """Fail-closed error for an original-product contract violation."""


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
    """Worker-only result. It cannot be passed to frozen ranking publication."""

    projection: Mapping[str, Any]
    namespace: Mapping[str, Any]
    replicate_without_coordinator_audit: Mapping[str, Any]
    worker_physical_receipt: Mapping[str, Any]
    telemetry: Mapping[str, Any]


def _digest(value: Any) -> str:
    return rank.canonical_sha256(value)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


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


def _namespace_by_corpus(namespace: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    rows = namespace.get("rows")
    if not isinstance(rows, list) or not rows:
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
    if namespace.get("expected_unique_count") != len(seen) or namespace.get("mapping_sha256") != _digest(list(rows)):
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


def worker_draft_packet(draft: OriginalProductWorkerDraft) -> dict[str, Any]:
    """Return a strict, digest-bound but deliberately non-publishable packet."""
    if not isinstance(draft, OriginalProductWorkerDraft):
        raise OriginalProductError("worker draft packet requires OriginalProductWorkerDraft")
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


def dynamic_original_index_build_receipt(*, palace_path: Path, expected_namespace: Mapping[str, Any], auditor: IndexAuditor | None = None) -> dict[str, Any]:
    """Dynamic audit with v2's direct SQLite/HNSW primitives in production.

    An injected auditor is accepted for synthetic tests only.  The result is the
    exact physical receipt required by the frozen AERP-7 rank wrapper.
    """
    grouped = _namespace_by_corpus(expected_namespace)
    expected_ids = sorted(physical for values in grouped.values() for physical in values.values())
    if auditor is not None:
        raw = dict(auditor(palace_path=palace_path, expected_namespace=expected_namespace))
    else:
        raw = _direct_dynamic_audit(palace_path=palace_path, expected_ids=expected_ids)
    required = {"physical_count", "physical_ids_sha256", "embedding", "hnsw_config", "graph_files", "immutable_backend_sha256", "immutable_non_length_backend_sha256", "immutable_residual_backend_sha256", "sqlite_semantic_sha256", "operational_delta", "direct_read_normalization_delta"}
    if set(raw) != required:
        raise OriginalProductError("original index audit receipt schema mismatch")
    expected = {"physical_count": len(expected_ids), "physical_ids_sha256": _digest(expected_ids)}
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
        stored = collection.get(include=["embeddings"])
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
    ids, embeddings = stored.get("ids"), stored.get("embeddings")
    if not isinstance(ids, list) or sorted(ids) != list(expected_ids):
        raise OriginalProductError("original Chroma physical IDs differ from dynamic namespace")
    vector_sha, count, dimension = v2._float32_embedding_digest(ids, embeddings)
    return {
        "physical_count": len(ids), "physical_ids_sha256": _digest(sorted(ids)),
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
