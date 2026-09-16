"""Fail-closed paired public engineering rehearsal for the burned LoCoMo set.

This is deliberately a scoring-after-freeze harness.  Label-bearing scorer
objects are resident in the coordinator, but are never passed to the ranking
producer.  It is an engineering rehearsal, not a physically isolated blinded
confirmation experiment or a tuning surface.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import gc
import hashlib
import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Sequence

from benchmarks import aerp1_locomo_three_way as aerp1
from benchmarks import aerp2_product_six_view_locomo as aerp2
from mempalace_rpg import RawAnchoredP5Policy, RpgMemoryKernel, SixViewRanker
from mempalace_rpg.vendored_mempalace import (
    UPSTREAM_TREE,
    vendored_source_state,
)


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_PIN = "87e6f38377b4bee0666374b05df6e14ffd154245"
ORIGINAL_TREE = UPSTREAM_TREE
FROZEN_PROTOCOL_PATH = ROOT / "benchmarks" / "locomo_story_protocol.py"
FROZEN_PROTOCOL_SOURCE = {
    "repository": "local historical MemPalace evaluation protocol",
    "commit": "429e11ced3529a3409509026a62fb3bb5ec43c77",
    "sha256": "f0e5b3ec3045b83149d36347435b65c1ff7ee92e9597e74cdf4c0cd636394341",
}
TOP_K = 10
ORIGINAL_COLLECTION = "mempalace_drawers"
REHEARSAL_SCOPE = "engineering_rehearsal_public_nonblind"
PINNED_ORIGINAL_ENVIRONMENT = {
    "MEMPALACE_BACKEND_EXPLICIT": "chroma",
    "MEMPALACE_EMBEDDING_DEVICE": "cpu",
    "MEMPALACE_EMBEDDING_MODEL": "minilm",
}
TRACE_REQUIRED = frozenset(
    {"authorized_candidate_ids", "selected_evidence_ids", "retrieval_ranking"}
)


def _canonical(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", *args],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def git_state(root: Path) -> dict[str, Any]:
    root = root.resolve()
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    patch = _git(root, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-color")
    return {
        "path": str(root),
        "git_head": _git(root, "rev-parse", "HEAD").decode().strip(),
        "git_tree": _git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "git_dirty": bool(status),
        "worktree_status_sha256": hashlib.sha256(status).hexdigest(),
        "worktree_diff_sha256": hashlib.sha256(patch).hexdigest(),
        "worktree_diff_bytes": len(patch),
    }


def _same_state(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before == after


def original_source_state(root: Path) -> dict[str, Any]:
    """Receipt either the bundled official source or a legacy external checkout."""

    resolved = root.resolve()
    if (resolved / "mempalace" / "_upstream_source.json").is_file():
        return vendored_source_state(resolved)
    return git_state(resolved)


def require_clean_pinned_original(root: Path) -> dict[str, Any]:
    state = original_source_state(root)
    if (
        state["git_dirty"]
        or state["git_head"] != ORIGINAL_PIN
        or state["git_tree"] != ORIGINAL_TREE
    ):
        raise ValueError("original MemPalace root must be clean at exact pinned commit")
    return state


def require_external_output(output: Path, *repositories: Path) -> Path:
    resolved = output.resolve()
    if any(
        resolved == repo.resolve() or repo.resolve() in resolved.parents for repo in repositories
    ):
        raise ValueError("output must be outside both measured repositories")
    return resolved


def file_tree_receipt(root: Path) -> dict[str, Any]:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"model directory is missing: {root}")
    files = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        files.append(
            {
                "relative_path": path.relative_to(root).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
        )
    if not files:
        raise ValueError("model directory is empty")
    return {
        "path": str(root),
        "files": files,
        "sha256": _canonical(files),
        "bytes": sum(row["bytes"] for row in files),
    }


def require_file_tree_unchanged(receipt: dict[str, Any]) -> None:
    if not isinstance(receipt, dict) or not isinstance(receipt.get("path"), str):
        raise ValueError("model file-tree receipt is malformed")
    if file_tree_receipt(Path(receipt["path"])) != receipt:
        raise RuntimeError("model file-tree digest drifted during paired run")


def require_input_unchanged(path: Path, expected_sha256: str) -> None:
    if _sha256_file(path) != expected_sha256:
        raise RuntimeError("dataset digest drifted during paired run")


def directory_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


@contextmanager
def pinned_original_environment():
    """Scope every ambient original-product choice and restore it exactly."""
    before = {key: os.environ.get(key) for key in PINNED_ORIGINAL_ENVIRONMENT}
    os.environ.update(PINNED_ORIGINAL_ENVIRONMENT)
    try:
        yield {
            "values": dict(PINNED_ORIGINAL_ENVIRONMENT),
            "restoration_required": before != PINNED_ORIGINAL_ENVIRONMENT,
        }
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import original module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_original_product(original_root: Path) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Load the original product modules only after pin/clean verification."""
    root = original_root.resolve()
    state = require_clean_pinned_original(root)
    # The names are intentionally unique: this cannot silently resolve a current
    # checkout's modules.  The original packages' absolute imports are made from
    # their own root only while the producer is constructed.
    old_path = list(sys.path)
    existing = {
        name: module
        for name, module in sys.modules.items()
        if name == "mempalace" or name.startswith("mempalace.")
    }
    if existing:
        raise RuntimeError(
            "original product cannot be loaded after a different mempalace package is already imported"
        )
    sys.path.insert(0, str(root))
    try:
        import mempalace.palace as palace  # type: ignore[import-not-found]
        import mempalace.searcher as searcher  # type: ignore[import-not-found]

        protocol = _load_module("_aerp5_frozen_locomo_protocol", FROZEN_PROTOCOL_PATH)
    finally:
        sys.path[:] = old_path
    return palace, searcher, protocol, state


def reset_original_product_backends(palace_path: Path) -> dict[str, Any]:
    """Release all native Chroma references for this disposable product palace."""
    palace = sys.modules.get("mempalace.palace")
    backends = sys.modules.get("mempalace.backends")
    if palace is None or backends is None:
        raise RuntimeError("original MemPalace backend modules are unavailable")
    reset = getattr(backends, "reset_backends", None)
    if not callable(reset):
        raise RuntimeError("original MemPalace backend close hooks are unavailable")
    path_key = str(palace_path)
    registry = sys.modules.get("mempalace.backends.registry")
    instances = list(getattr(registry, "_instances", {}).values())
    default_backend = getattr(palace, "_DEFAULT_BACKEND", None)
    if default_backend is not None:
        instances.append(default_backend)
    closed_client_count = 0
    for backend in {id(item): item for item in instances}.values():
        clients = getattr(backend, "_clients", None)
        client = clients.get(path_key) if isinstance(clients, dict) else None
        if client is None:
            continue
        close = getattr(client, "close", None)
        if not callable(close):
            raise RuntimeError("original Chroma client has no explicit close hook")
        close()
        closed_client_count += 1
        clients.pop(path_key, None)
        freshness = getattr(backend, "_freshness", None)
        if isinstance(freshness, dict):
            freshness.pop(path_key, None)
    reset()
    from chromadb.api.shared_system_client import SharedSystemClient

    residual_refcount = SharedSystemClient._identifier_to_refcount.get(path_key, 0)
    # Chroma 1.5.7 can retain additional shared-system references after all
    # product backend clients are closed. This disposable benchmark palace has
    # no legal consumer after ranking freeze, so drain only its exact identifier.
    for _ in range(residual_refcount):
        SharedSystemClient._release_system(path_key)
    gc.collect()
    if (
        path_key in SharedSystemClient._identifier_to_system
        or path_key in SharedSystemClient._identifier_to_refcount
    ):
        raise RuntimeError("original Chroma shared system remained live after explicit shutdown")
    return {
        "closed_backend_client_count": closed_client_count,
        "drained_residual_shared_references": residual_refcount,
        "verified_system_released": True,
    }


class MiniLMDenseAdapter:
    """Counted adapter around the original native Chroma MiniLM function."""

    def __init__(
        self,
        embedding_function: Callable[[Sequence[str]], Sequence[Sequence[float]]],
        *,
        identity: str,
        runtime_identity: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(identity, str) or not identity:
            raise ValueError("MiniLM adapter identity is required")
        self._function = embedding_function
        self.identity = identity
        self.runtime_identity = dict(runtime_identity or {})
        self.passage_text_count = 0
        self.query_text_count = 0
        self.passage_call_count = 0
        self.query_call_count = 0

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        values = self._function(list(texts))
        result = [list(map(float, row)) for row in values]
        if len(result) != len(texts) or any(
            not row or not all(math.isfinite(value) for value in row) for row in result
        ):
            raise ValueError("native MiniLM returned malformed embeddings")
        return result

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.passage_call_count += 1
        self.passage_text_count += len(texts)
        return self._encode(texts)

    def encode_query(self, query: str) -> list[float]:
        if not isinstance(query, str) or not query:
            raise ValueError("query must be non-empty")
        self.query_call_count += 1
        self.query_text_count += 1
        return self._encode([query])[0]

    def receipt(self) -> dict[str, int]:
        return {
            "passage_text_count": self.passage_text_count,
            "query_text_count": self.query_text_count,
            "passage_call_count": self.passage_call_count,
            "query_call_count": self.query_call_count,
        }


def native_minilm_adapter(model_dir: Path) -> MiniLMDenseAdapter:
    """Wrap the exact cached original-product MiniLM object for the current arm."""
    receipt = file_tree_receipt(model_dir)
    try:
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    except ImportError as exc:
        raise RuntimeError("native Chroma MiniLM embedding function is unavailable") from exc
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = model_dir.resolve()
    embedding = importlib.import_module("mempalace.embedding")
    get_embedding_function = (
        getattr(embedding, "get_embedding_function", None) if embedding is not None else None
    )
    if not callable(get_embedding_function):
        raise RuntimeError("original MemPalace embedding factory is unavailable")
    native = get_embedding_function(device="cpu", model="minilm")
    providers = list(getattr(native, "_preferred_providers", []))
    if providers != ["CPUExecutionProvider"]:
        raise RuntimeError(f"original MiniLM did not resolve CPU-only providers: {providers!r}")
    return MiniLMDenseAdapter(
        native,
        identity=f"chromadb-native-minilm:{receipt['sha256']}",
        runtime_identity={
            "model": "minilm",
            "device": "cpu",
            "providers": providers,
            "model_path": str(model_dir.resolve()),
            "model_file_tree_sha256": receipt["sha256"],
            "same_cached_embedding_object_for_both_arms": True,
        },
    )


def assert_original_product_configuration(
    palace: Any, *, palace_path: Path, encoder: MiniLMDenseAdapter
) -> dict[str, Any]:
    """Fail closed if any ambient product setting escaped the frozen contract."""
    config_module = importlib.import_module("mempalace.config")
    config_type = getattr(config_module, "MempalaceConfig", None)
    if not callable(config_type):
        raise RuntimeError("original MemPalace configuration API is unavailable")
    config = config_type()
    backend = palace.get_backend_for_palace(str(palace_path))
    resolved = {
        "backend": getattr(backend, "name", None),
        "collection": ORIGINAL_COLLECTION,
        "embedding_model": config.embedding_model,
        "embedding_device": config.embedding_device,
        **encoder.runtime_identity,
    }
    if (
        resolved["backend"] != "chroma"
        or resolved["embedding_model"] != "minilm"
        or resolved["embedding_device"] != "cpu"
    ):
        raise RuntimeError(f"original product configuration escaped freeze: {resolved!r}")
    resolver = getattr(backend, "_resolve_embedding_function", None)
    if not callable(resolver) or resolver() is not encoder._function:
        raise RuntimeError("both arms do not share the exact cached original MiniLM object")
    return resolved


def raw_dialogs(payload: dict[str, Any]) -> list[dict[str, str]]:
    dialogs = aerp1.raw_dialogs(payload)
    if not dialogs or len(dialogs) != len({row["id"] for row in dialogs}):
        raise ValueError("opaque dialog corpus must be non-empty and unique")
    return dialogs


def _validate_top10(
    ranked: Iterable[str], corpus_ids: Iterable[str], *, arm: str, item_id: str
) -> list[str]:
    values = list(ranked)
    corpus = set(corpus_ids)
    if len(values) != TOP_K:
        raise RuntimeError(f"{arm}:{item_id} returned fewer than Top-{TOP_K}")
    if len(values) != len(set(values)):
        raise RuntimeError(f"{arm}:{item_id} returned duplicate opaque dialog IDs")
    unknown = set(values) - corpus
    if unknown:
        raise RuntimeError(f"{arm}:{item_id} returned unknown opaque dialog IDs: {sorted(unknown)}")
    return values


def original_product_ingest(
    *,
    palace: Any,
    palace_path: Path,
    conversation_id: str,
    dialogs: list[dict[str, str]],
) -> None:
    """Exercise the original product persistence seam with a fixed collection."""
    ids = [row["id"] for row in dialogs]
    texts = [row["text"] for row in dialogs]
    collection = palace.get_collection(
        str(palace_path), collection_name=ORIGINAL_COLLECTION, create=True, backend="chroma"
    )
    collection.upsert(
        documents=texts,
        ids=ids,
        metadatas=[
            {"source_file": dialog_id, "wing": "locomo", "room": conversation_id}
            for dialog_id in ids
        ],
    )


def original_product_query(
    *,
    searcher: Any,
    palace_path: Path,
    conversation_id: str,
    corpus_ids: Iterable[str],
    query: str,
    item_id: str,
) -> list[str]:
    """Exercise the original public search seam, never a direct Chroma query."""
    ids = list(corpus_ids)
    result = searcher.search_memories(
        query,
        str(palace_path),
        room=conversation_id,
        n_results=TOP_K,
        max_distance=0.0,
        candidate_strategy="vector",
        collection_name=ORIGINAL_COLLECTION,
    )
    if not isinstance(result, dict) or result.get("error"):
        raise RuntimeError(f"original product search failed: {result!r}")
    rows = result.get("results")
    if not isinstance(rows, list):
        raise RuntimeError("original product search result shape is malformed")
    ranked = []
    for row in rows:
        source = row.get("source_path") if isinstance(row, dict) else None
        if not isinstance(source, str) or source not in ids:
            raise RuntimeError(
                "original product returned unknown source_path; cannot map losslessly to opaque dialog ID"
            )
        ranked.append(source)
    return _validate_top10(ranked, ids, arm="original_product", item_id=item_id)


def original_product_rank(
    *,
    palace: Any,
    searcher: Any,
    palace_path: Path,
    conversation_id: str,
    dialogs: list[dict[str, str]],
    query: str,
    item_id: str,
) -> list[str]:
    """Convenience composition of the same seams used by the production loop."""
    original_product_ingest(
        palace=palace,
        palace_path=palace_path,
        conversation_id=conversation_id,
        dialogs=dialogs,
    )
    return original_product_query(
        searcher=searcher,
        palace_path=palace_path,
        conversation_id=conversation_id,
        corpus_ids=[row["id"] for row in dialogs],
        query=query,
        item_id=item_id,
    )


def current_product_rank(
    kernel: RpgMemoryKernel,
    *,
    conversation_id: str,
    query: str,
    event_to_dialog: dict[str, str],
    item_id: str,
) -> tuple[list[str], dict[str, Any]]:
    decision = kernel.authorized_evidence(
        campaign_id=conversation_id,
        actor_id="locomo_reader",
        actor_type="npc",
        query=query,
        active_quest_ids=[],
        budget=1000,
        _compact_product_trace=True,
    )
    evidence = kernel._retrieve_memory_items(
        campaign_id=conversation_id,
        actor_id="locomo_reader",
        actor_type="npc",
        query=query,
        active_quest_ids=[],
        location_id=None,
        hit_limit=TOP_K,
        max_chars=10_000_000,
        authorized_event_ids=set(decision.trace["authorized_candidate_ids"]),
        ranking_trace=decision.trace,
    )
    selected = [str(row["source_event_id"]) for row in evidence]
    decision.trace["selected_evidence_ids"] = selected
    kernel._complete_product_trace(
        decision, campaign_id=conversation_id, actor_id="locomo_reader", actor_type="npc"
    )
    if not TRACE_REQUIRED <= set(decision.trace):
        raise RuntimeError("current product trace is incomplete")
    audit = aerp1.audit_product_trace(decision.trace)
    if not audit["complete"]:
        raise RuntimeError("current product authorization trace is incomplete")
    try:
        ranked = [event_to_dialog[event_id] for event_id in selected]
    except KeyError as exc:
        raise RuntimeError("current product returned an unmapped event") from exc
    return _validate_top10(
        ranked, event_to_dialog.values(), arm="current_aerp4", item_id=item_id
    ), decision.trace


def _freeze_rankings_open_backends(
    *,
    retrieval: Any,
    palace: Any,
    searcher: Any,
    encoder: MiniLMDenseAdapter,
    tau: float,
    work_dir: Path,
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Produce both streams from sanitized retrieval only; scorer labels are absent."""
    if isinstance(tau, bool) or not math.isfinite(float(tau)):
        raise ValueError("--tau must be an explicit finite numeric value")
    grouped = aerp1._conversation_items(retrieval)
    rankings = {
        "original_product_native_config_matched_minilm": {},
        "current_aerp4_raw_anchored_p5": {},
    }
    traces: dict[str, dict[str, Any]] = {}
    resources = {
        "original": {"ingest_seconds": 0.0, "query_seconds": 0.0},
        "current": {"ingest_seconds": 0.0, "query_seconds": 0.0},
    }
    original_palace = work_dir / "original-palace"
    db_path = work_dir / "current.sqlite3"
    resources["original"]["resolved_configuration"] = assert_original_product_configuration(
        palace, palace_path=original_palace, encoder=encoder
    )
    ranker = SixViewRanker(
        encoder, diagnostic_ledger=True, routing_policy=RawAnchoredP5Policy(float(tau))
    )
    with RpgMemoryKernel(db_path=str(db_path), retrieval_ranker=ranker) as kernel:
        for conversation_id, item_ids in sorted(grouped.items()):
            payload0 = retrieval.retrieval_items[item_ids[0]]
            dialogs = raw_dialogs(payload0)
            if [row["text"].encode("utf-8") for row in dialogs] != [
                aerp2._dialog_text(dialog).encode("utf-8")
                for session in payload0["sessions"]
                for dialog in session["dialogs"]
            ]:
                raise RuntimeError(
                    "current candidate raw_text differs from original opaque dialog bytes"
                )
            started = time.perf_counter()
            # This is the current product's normal authorized insertion seam.
            event_to_dialog, _lineage = aerp2.seed_sanitized_conversation(
                kernel, payload0, conversation_id=conversation_id
            )
            resources["current"]["ingest_seconds"] += time.perf_counter() - started
            # Original upsert is intentionally exactly once per conversation.
            started = time.perf_counter()
            original_product_ingest(
                palace=palace,
                palace_path=original_palace,
                conversation_id=conversation_id,
                dialogs=dialogs,
            )
            resources["original"]["ingest_seconds"] += time.perf_counter() - started
            for item_id in item_ids:
                query = retrieval.retrieval_items[item_id]["query"]
                started = time.perf_counter()
                # Kept inline so this remains visibly the product search path.
                original_ranked = original_product_query(
                    searcher=searcher,
                    palace_path=original_palace,
                    conversation_id=conversation_id,
                    corpus_ids=[row["id"] for row in dialogs],
                    query=query,
                    item_id=item_id,
                )
                resources["original"]["query_seconds"] += time.perf_counter() - started
                started = time.perf_counter()
                current_ranked, trace = current_product_rank(
                    kernel,
                    conversation_id=conversation_id,
                    query=query,
                    event_to_dialog=event_to_dialog,
                    item_id=item_id,
                )
                resources["current"]["query_seconds"] += time.perf_counter() - started
                rankings["original_product_native_config_matched_minilm"][item_id] = original_ranked
                rankings["current_aerp4_raw_anchored_p5"][item_id] = current_ranked
                traces[item_id] = trace
    resources["original"]["index_bytes_before_backend_shutdown"] = (
        directory_bytes(original_palace) if original_palace.exists() else 0
    )
    resources["current"]["index_bytes"] = db_path.stat().st_size if db_path.exists() else 0
    resources["current"]["embedding"] = {
        "matched_embedding_identity": encoder.identity,
        "resolved_configuration": encoder.runtime_identity,
        **encoder.receipt(),
    }
    resources["original"]["embedding"] = {
        "matched_embedding_identity": encoder.identity,
        "resolved_configuration": encoder.runtime_identity,
        "measurement_status": "unsupported_through_public_product_interface",
        "passage_text_count": "native_product_unobservable",
        "query_text_count": "native_product_unobservable",
        "passage_call_count": "native_product_unobservable",
        "query_call_count": "native_product_unobservable",
    }
    return rankings, traces, resources


def freeze_rankings(
    *,
    retrieval: Any,
    palace: Any,
    searcher: Any,
    encoder: MiniLMDenseAdapter,
    tau: float,
    work_dir: Path,
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]], dict[str, Any]]:
    """Freeze both ranking streams and always release native product backends."""
    if isinstance(tau, bool) or not math.isfinite(float(tau)):
        raise ValueError("--tau must be an explicit finite numeric value")
    result = None
    try:
        result = _freeze_rankings_open_backends(
            retrieval=retrieval,
            palace=palace,
            searcher=searcher,
            encoder=encoder,
            tau=float(tau),
            work_dir=work_dir,
        )
    finally:
        cleanup = reset_original_product_backends(work_dir / "original-palace")
    result[2]["original"]["cleanup"] = cleanup
    return result


def paired_group_bootstrap(
    rows: list[dict[str, Any]],
    *,
    current: str,
    original: str,
    estimand: str,
    seed: int = 20260822,
    resamples: int = 2000,
) -> dict[str, Any]:
    grouped: dict[str, list[float]] = {}
    for row in rows:
        value = row["recall"][current] - row["recall"][original]
        grouped.setdefault(row["conversation_id"], []).append(value)
    if not grouped:
        raise ValueError("paired bootstrap has no scored questions")
    if estimand not in {"question_macro_cluster_bootstrap", "conversation_macro"}:
        raise ValueError("unknown paired bootstrap estimand")
    keys = sorted(grouped)
    means = {key: math.fsum(grouped[key]) / len(grouped[key]) for key in keys}

    def estimate(selected: list[str]) -> float:
        if estimand == "conversation_macro":
            return math.fsum(means[key] for key in selected) / len(selected)
        values = [value for key in selected for value in grouped[key]]
        return math.fsum(values) / len(values)

    rng = random.Random(seed)
    samples = sorted(estimate([rng.choice(keys) for _ in keys]) for _ in range(resamples))
    return {
        "estimand": estimand,
        "point_estimate": estimate(keys),
        "group_count": len(keys),
        "question_count": sum(len(values) for values in grouped.values()),
        "lower_95": samples[int(0.025 * (resamples - 1))],
        "upper_95": samples[int(0.975 * (resamples - 1))],
        "seed": seed,
        "resamples": resamples,
        "replicate_sha256": _canonical(samples),
    }


def score_after_freeze(
    *, scorer: Any, retrieval: Any, rankings: dict[str, dict[str, list[str]]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The sole scorer-label consumer; callers must have frozen every arm first."""
    arms = tuple(rankings)
    expected = set(retrieval.retrieval_items)
    if any(set(rankings[arm]) != expected for arm in arms):
        raise RuntimeError("both arms must freeze a complete identical question set before scoring")
    summary, question_rows = aerp1.score_rankings(scorer, rankings, TOP_K)
    rows = []
    for row in question_rows:
        recalls = {arm: row["columns"][arm]["official_exact"]["recall_at_10"] for arm in arms}
        if any(value is None for value in recalls.values()):
            continue
        rows.append(
            {
                "item_id": row["item_id"],
                "conversation_id": row["conversation_id"],
                "category": row["category"],
                "recall": recalls,
            }
        )
    paired = {}
    for name, predicate, aggregate_key in (
        ("overall", lambda row: True, "overall"),
        ("hard_categories_1_2", lambda row: row["category"] in {1, 2}, "hard_categories_1_2"),
        ("adversarial_category_5", lambda row: row["category"] == 5, ("by_category", "5")),
    ):
        subset = [row for row in rows if predicate(row)]
        if not subset:
            continue
        receipts = {
            estimand: paired_group_bootstrap(
                subset,
                current=arms[1],
                original=arms[0],
                estimand=estimand,
            )
            for estimand in ("question_macro_cluster_bootstrap", "conversation_macro")
        }
        if isinstance(aggregate_key, tuple):
            current_aggregate = summary[arms[1]]["official_exact"][aggregate_key[0]][
                aggregate_key[1]
            ]
            original_aggregate = summary[arms[0]]["official_exact"][aggregate_key[0]][
                aggregate_key[1]
            ]
        else:
            current_aggregate = summary[arms[1]]["official_exact"][aggregate_key]
            original_aggregate = summary[arms[0]]["official_exact"][aggregate_key]
        expected_points = {
            "question_macro_cluster_bootstrap": current_aggregate["question_macro_recall_at_10"]
            - original_aggregate["question_macro_recall_at_10"],
            "conversation_macro": current_aggregate["conversation_macro_recall_at_10"]
            - original_aggregate["conversation_macro_recall_at_10"],
        }
        for estimand, expected_point in expected_points.items():
            if not math.isclose(
                receipts[estimand]["point_estimate"],
                expected_point,
                rel_tol=0.0,
                abs_tol=1e-15,
            ):
                raise RuntimeError(f"{name} {estimand} bootstrap point disagrees with aggregate")
            receipts[estimand]["aggregate_point_verified"] = True
        paired[name] = receipts
    summary["paired_group_bootstrap"] = paired
    summary["primary_semantics"] = "official_exact"
    return summary, question_rows


def _rss_after_run_receipt() -> dict[str, Any]:
    try:
        import psutil

        return {
            "status": "measured_current_process_after_run_not_peak",
            "bytes": psutil.Process().memory_info().rss,
        }
    except ImportError:
        return {"status": "unsupported: psutil unavailable", "bytes": None}


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError("paired-run output already exists; refusing to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(
    *,
    dataset: Path,
    original_root: Path,
    minilm_model_dir: Path,
    tau: float,
    work_root: Path,
    output: Path,
) -> dict[str, Any]:
    if isinstance(tau, bool) or not math.isfinite(float(tau)):
        raise ValueError("--tau is required and must be finite")
    output = require_external_output(output, ROOT, original_root)
    work_root = require_external_output(work_root, ROOT, original_root)
    if not work_root.is_dir():
        raise ValueError(f"work root must be an existing directory: {work_root}")
    current_before = git_state(ROOT)
    if current_before["git_dirty"]:
        raise ValueError("current AERP worktree must be clean")
    model = file_tree_receipt(minilm_model_dir)
    dataset = dataset.resolve()
    dataset_sha = _sha256_file(dataset)
    with pinned_original_environment() as original_environment:
        palace, searcher, protocol, original_before = load_original_product(original_root)
        loaded = protocol.load_official_locomo10(dataset)
        retrieval, scorer = protocol.prepare_hard_story_track(
            loaded, candidate_pool_size=TOP_K, require_official_counts=True
        )
        corpus_receipt = {
            conversation_id: [
                (row["id"], _canonical(row["text"]))
                for row in raw_dialogs(retrieval.retrieval_items[item_ids[0]])
            ]
            for conversation_id, item_ids in sorted(aerp1._conversation_items(retrieval).items())
        }
        query_receipt = [
            (item_id, _canonical(retrieval.retrieval_items[item_id]["query"]))
            for item_id in sorted(retrieval.retrieval_items)
        ]
        with tempfile.TemporaryDirectory(
            prefix="aerp5-product-paired-", dir=work_root
        ) as directory:
            adapter = native_minilm_adapter(minilm_model_dir)
            rankings, traces, resources = freeze_rankings(
                retrieval=retrieval,
                palace=palace,
                searcher=searcher,
                encoder=adapter,
                tau=float(tau),
                work_dir=Path(directory),
            )
    require_input_unchanged(dataset, dataset_sha)
    require_file_tree_unchanged(model)
    ranking_digest = {arm: _canonical(rows) for arm, rows in rankings.items()}
    if ranking_digest["original_product_native_config_matched_minilm"] == "" or set(
        rankings["original_product_native_config_matched_minilm"]
    ) != set(rankings["current_aerp4_raw_anchored_p5"]):
        raise RuntimeError("paired ranking streams drifted")
    aggregate, question_rows = score_after_freeze(
        scorer=scorer, retrieval=retrieval, rankings=rankings
    )
    current_after, original_after = git_state(ROOT), original_source_state(original_root)
    if not _same_state(current_before, current_after) or not _same_state(
        original_before, original_after
    ):
        raise RuntimeError("a measured worktree changed during paired run")
    report = {
        "schema": "aerp5-product-paired-locomo-v1",
        "scope": REHEARSAL_SCOPE,
        "confirmation_claim": False,
        "resource_gate_eligible": False,
        "equal_budget_interpretation_allowed": False,
        "tau": float(tau),
        "runtime": {
            "current_before": current_before,
            "current_after": current_after,
            "original_before": original_before,
            "original_after": original_after,
            "pinned_original_environment": original_environment,
        },
        "dataset": {"path": str(dataset), "sha256": dataset_sha},
        "model_file_tree": model,
        "producer_contract": {
            "ranking_freeze_before_score_after_freeze_call": True,
            "physical_label_process_separation": False,
            "label_isolation_claim": "logical_interface_only_on_burned_rehearsal",
            "labels_resident_in_coordinator_before_freeze": True,
            "labels_passed_to_ranking_producer": False,
            "top_k": TOP_K,
            "corpus_sha256": _canonical(corpus_receipt),
            "query_sha256": _canonical(query_receipt),
            "original": {
                "interface": "mempalace.palace.get_collection(...).upsert(...) -> mempalace.searcher.search_memories(...)",
                "native_config": {
                    "n_results": TOP_K,
                    "max_distance": 0.0,
                    "candidate_strategy": "vector",
                    "collection_name": ORIGINAL_COLLECTION,
                    "closets_created": False,
                },
                "matched_embedding_identity": adapter.identity,
            },
            "current": {
                "interface": "RpgMemoryKernel authorized candidates -> SixViewRanker(RawAnchoredP5Policy(tau))",
                "trace_count": len(traces),
            },
        },
        "rankings": rankings,
        "ranking_sha256": ranking_digest,
        "traces": traces,
        "trace_sha256": _canonical(traces),
        "resources": {
            **resources,
            "formal_gate": {
                "eligible": False,
                "limitations": [
                    "original embedding call and text counts are not observable through the public interface",
                    "original storage bytes are sampled before backend shutdown",
                    "per-query latency distributions are not yet recorded",
                    "peak resident memory and complete hardware/runtime receipts are absent",
                ],
            },
            "peak_rss": {"status": "unsupported_not_instrumented", "bytes": None},
            "rss_after_run": _rss_after_run_receipt(),
        },
        "aggregate": aggregate,
        "questions": question_rows,
    }
    atomic_json(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--original-root", required=True)
    parser.add_argument("--minilm-model-dir", required=True)
    parser.add_argument("--tau", required=True, type=float)
    parser.add_argument("--work-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = run(
        dataset=Path(args.dataset),
        original_root=Path(args.original_root),
        minilm_model_dir=Path(args.minilm_model_dir),
        tau=args.tau,
        work_root=Path(args.work_root),
        output=Path(args.output),
    )
    print(
        json.dumps(
            {"scope": result["scope"], "ranking_sha256": result["ranking_sha256"]}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
