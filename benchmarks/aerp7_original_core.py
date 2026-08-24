"""Dataset-neutral, label-free original-product normalization primitives.

The core deliberately knows neither ConvoMem custody nor MemBench labels.  A
benchmark adapter supplies opaque corpus/item/candidate identifiers and text;
product-specific workers may then use this namespace for exact public calls.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

NORMALIZED_PROJECTION_SCHEMA = "aerp-original-normalized-retrieval-v1"
_FORBIDDEN = frozenset({"target_step_id", "ground_truth", "choices", "question_type", "scenario", "source_path", "source_file_role", "raw_tid", "tid", "crosswalk", "strata"})


class OriginalCoreError(RuntimeError):
    pass


class ProjectionAdapter(Protocol):
    """Benchmark-owned hooks used by the shared public-product lifecycle.

    The lifecycle owns all calls to the pinned original product.  An adapter
    only validates a label-free projection and translates product rows/receipts
    at its boundary; it cannot see custody data.
    """
    def validate_projection(self, value: Any) -> Mapping[str, Any]: ...
    def identity_namespace(self, projection: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def input_receipt(self, projection: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def format_row(self, *, item: Mapping[str, Any], ranked_candidate_ids: list[str], trace: Mapping[str, Any], product_row: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def validate_completed_replicate(self, projection: Mapping[str, Any], replicate: Mapping[str, Any]) -> None: ...
    def runtime_projection(self, projection: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def adapter_id(self) -> str: ...


@dataclass(frozen=True)
class LifecycleAdapter:
    """Concrete callable adapter, deliberately free of dataset labels."""
    validate_projection: Callable[[Any], Mapping[str, Any]]
    identity_namespace: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    input_receipt: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    format_row: Callable[..., Mapping[str, Any]]
    validate_completed_replicate: Callable[[Mapping[str, Any], Mapping[str, Any]], None]
    runtime_projection: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    adapter_id: str


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _token(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef"):
        raise OriginalCoreError(code)
    return value


def _walk(value: Any) -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN & set(value)
        if forbidden:
            raise OriginalCoreError("normalized_projection_forbidden_label")
        for child in value.values(): _walk(child)
    elif isinstance(value, list):
        for child in value: _walk(child)


def validate_normalized_projection(value: Any, *, require_global_candidate_ids: bool = True) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"schema", "corpora", "items"} or value.get("schema") != NORMALIZED_PROJECTION_SCHEMA:
        raise OriginalCoreError("normalized_projection_schema_invalid")
    _walk(value); corpora = value["corpora"]; items = value["items"]
    if not isinstance(corpora, list) or not corpora or not isinstance(items, list) or not items:
        raise OriginalCoreError("normalized_projection_invalid")
    corpus_ids: set[str] = set(); candidate_ids: set[str] = set()
    for corpus in corpora:
        if not isinstance(corpus, Mapping) or set(corpus) != {"corpus_id", "candidates"}: raise OriginalCoreError("normalized_corpus_invalid")
        corpus_id = _token(corpus.get("corpus_id"), "normalized_corpus_id_invalid")
        if corpus_id in corpus_ids: raise OriginalCoreError("normalized_corpus_duplicate")
        corpus_ids.add(corpus_id); candidates = corpus["candidates"]
        if not isinstance(candidates, list) or not candidates: raise OriginalCoreError("normalized_candidates_invalid")
        for order, candidate in enumerate(candidates):
            if not isinstance(candidate, Mapping) or set(candidate) != {"candidate_id", "order", "text"} or candidate.get("order") != order or not isinstance(candidate.get("text"), str) or not candidate["text"].strip(): raise OriginalCoreError("normalized_candidate_invalid")
            candidate_id = _token(candidate.get("candidate_id"), "normalized_candidate_id_invalid")
            if candidate_id in candidate_ids and require_global_candidate_ids: raise OriginalCoreError("normalized_candidate_id_not_global")
            candidate_ids.add(candidate_id)
    item_ids: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"item_id", "corpus_id", "query_text"} or not isinstance(item.get("query_text"), str) or not item["query_text"].strip(): raise OriginalCoreError("normalized_item_invalid")
        item_id = _token(item.get("item_id"), "normalized_item_id_invalid"); corpus_id = _token(item.get("corpus_id"), "normalized_corpus_id_invalid")
        if item_id in item_ids or corpus_id not in corpus_ids: raise OriginalCoreError("normalized_item_binding_invalid")
        item_ids.add(item_id)
    return {"schema": NORMALIZED_PROJECTION_SCHEMA, "corpora": [dict(row) for row in corpora], "items": [dict(row) for row in items]}


def identity_namespace(projection: Any, *, separator: str, schema: str, require_global_candidate_ids: bool = True) -> dict[str, Any]:
    frozen = validate_normalized_projection(projection, require_global_candidate_ids=require_global_candidate_ids)
    if not isinstance(separator, str) or not separator or not isinstance(schema, str) or not schema:
        raise OriginalCoreError("normalized_namespace_config_invalid")
    rows = []
    for corpus in frozen["corpora"]:
        if separator in corpus["corpus_id"]: raise OriginalCoreError("normalized_namespace_separator_collision")
        for candidate in corpus["candidates"]:
            if separator in candidate["candidate_id"]: raise OriginalCoreError("normalized_namespace_separator_collision")
            rows.append({"corpus_id": corpus["corpus_id"], "candidate_id": candidate["candidate_id"], "physical_id": corpus["corpus_id"] + separator + candidate["candidate_id"]})
    rows.sort(key=lambda row: row["physical_id"])
    if len({row["physical_id"] for row in rows}) != len(rows): raise OriginalCoreError("normalized_namespace_collision")
    return {"schema": schema, "scheme": "corpus_id" + separator + "candidate_id", "rows": rows, "expected_unique_count": len(rows), "mapping_sha256": canonical_sha256(rows)}
