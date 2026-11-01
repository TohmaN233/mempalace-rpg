"""Pinned local Chroma MiniLM runtime. No network or embedding fallback path."""
from __future__ import annotations

import hashlib
import inspect
import importlib.metadata
import json
import logging
import math
from pathlib import Path
import platform
import re
import threading
import time
from typing import Sequence

from .passage_cache import (
    CACHE_ALGORITHM_ID,
    VECTOR_SERIALIZATION_ID,
    PassageVectorCache,
    content_sha256,
    validate_passage_cache_path,
)
from .retrieval import FixedP5Policy, SixViewRanker

logger = logging.getLogger(__name__)

MODEL_FILES = (
    "config.json", "model.onnx", "special_tokens_map.json",
    "tokenizer_config.json", "tokenizer.json", "vocab.txt",
)


def model_tree_hash(model_dir: Path) -> str:
    """Same relative-path/size/SHA tree encoding as the research manifest."""
    if model_dir.is_symlink():
        raise ValueError("MiniLM model root must not be linked")
    root = model_dir.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("MiniLM model root must be a real directory")
    for name in MODEL_FILES:
        path = root / "onnx" / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"local MiniLM model file is missing or linked: {name}")
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("MiniLM tree must not contain links")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            files.append({"relative_path": path.relative_to(root).as_posix(),
                          "bytes": path.stat().st_size, "sha256": digest.hexdigest()})
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class LocalMiniLMEncoder:
    def __init__(
        self,
        native,
        *,
        identity: str,
        passage_cache: PassageVectorCache | None = None,
    ) -> None:
        self._native = native
        self.identity = identity
        if passage_cache is None:
            memory_namespace = hashlib.sha256(
                f"memory-only:{CACHE_ALGORITHM_ID}:{identity}".encode("utf-8")
            ).hexdigest()
            passage_cache = PassageVectorCache(namespace=memory_namespace)
        self._passage_cache = passage_cache
        self._lock = threading.RLock()
        self._closed = False

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        if any(not isinstance(text, str) for text in texts):
            raise TypeError("local MiniLM inputs must be strings")
        vectors = [[float(value) for value in row] for row in self._native(list(texts))]
        if len(vectors) != len(texts) or any(
            len(row) != 384 or any(not math.isfinite(value) for value in row)
            for row in vectors
        ):
            raise ValueError("local MiniLM returned invalid 384-dimensional embeddings")
        return vectors

    def encode_query(self, query: str) -> list[float]:
        started = time.perf_counter_ns()
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("local MiniLM encoder is closed")
                vector = self._encode([query])[0]
        except BaseException as exc:
            logger.error(
                "local_minilm_query_embedding_failed cached=false elapsed_ms=%.3f error_type=%s",
                (time.perf_counter_ns() - started) / 1_000_000,
                type(exc).__name__,
            )
            raise
        logger.info(
            "local_minilm_query_embedding_completed cached=false elapsed_ms=%.3f",
            (time.perf_counter_ns() - started) / 1_000_000,
        )
        return vector

    def encode_passages(self, texts: Sequence[str]) -> list[list[float]]:
        started = time.perf_counter_ns()
        passage_texts = list(texts)
        digests: list[str] = []
        unique_texts: dict[str, str] = {}
        for text in passage_texts:
            digest = content_sha256(text)
            existing = unique_texts.get(digest)
            if existing is not None and existing != text:
                raise RuntimeError("passage content SHA-256 collision")
            unique_texts[digest] = text
            digests.append(digest)

        lookup_elapsed = 0
        encode_elapsed = 0
        write_elapsed = 0
        missing_digests: list[str] = []
        try:
            with self._lock:
                if self._closed:
                    raise RuntimeError("local MiniLM encoder is closed")
                lookup_started = time.perf_counter_ns()
                vectors_by_digest = self._passage_cache.get_many(list(unique_texts))
                lookup_elapsed = time.perf_counter_ns() - lookup_started
                missing_digests = [digest for digest in unique_texts if digest not in vectors_by_digest]
                if missing_digests:
                    encode_started = time.perf_counter_ns()
                    # Encode each distinct miss once. Nothing is cached unless the
                    # whole native batch returns valid vectors.
                    encoded = self._encode([unique_texts[digest] for digest in missing_digests])
                    encode_elapsed = time.perf_counter_ns() - encode_started
                    additions = dict(zip(missing_digests, encoded))
                    write_started = time.perf_counter_ns()
                    self._passage_cache.put_many(additions)
                    write_elapsed = time.perf_counter_ns() - write_started
                    vectors_by_digest.update(additions)
        except BaseException as exc:
            logger.error(
                "local_minilm_passage_embedding_failed input_count=%d unique_count=%d "
                "miss_count=%d lookup_ms=%.3f encode_ms=%.3f write_ms=%.3f "
                "elapsed_ms=%.3f error_type=%s",
                len(passage_texts), len(unique_texts), len(missing_digests),
                lookup_elapsed / 1_000_000, encode_elapsed / 1_000_000,
                write_elapsed / 1_000_000, (time.perf_counter_ns() - started) / 1_000_000,
                type(exc).__name__,
            )
            raise

        results = [list(vectors_by_digest[digest]) for digest in digests]
        logger.info(
            "local_minilm_passage_embedding_completed input_count=%d unique_count=%d "
            "hit_count=%d miss_count=%d lookup_ms=%.3f encode_ms=%.3f "
            "write_ms=%.3f elapsed_ms=%.3f",
            len(passage_texts), len(unique_texts), len(unique_texts) - len(missing_digests),
            len(missing_digests), lookup_elapsed / 1_000_000, encode_elapsed / 1_000_000,
            write_elapsed / 1_000_000, (time.perf_counter_ns() - started) / 1_000_000,
        )
        return results

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._passage_cache.close()
            self._closed = True


def _source_sha256(value: object, *, label: str) -> str:
    try:
        source = inspect.getsource(value)
    except (OSError, TypeError) as exc:
        raise RuntimeError(f"unable to fingerprint local MiniLM {label} implementation") from exc
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _passage_cache_namespace(*, model_sha256: str, native_type: type) -> str:
    distributions = ("chromadb", "onnxruntime", "tokenizers", "numpy")
    dependency_versions = {
        distribution: importlib.metadata.version(distribution)
        for distribution in distributions
    }
    try:
        base_type = next(base for base in native_type.__mro__[1:] if base.__name__ == "ONNXMiniLM_L6_V2")
    except StopIteration as exc:
        raise RuntimeError("local MiniLM inference class is not the pinned Chroma implementation") from exc
    namespace_data = {
        "schema": "minilm-passage-cache-namespace-v1",
        "model_tree_sha256": model_sha256,
        "cache_algorithm": CACHE_ALGORITHM_ID,
        "vector_serialization": VECTOR_SERIALIZATION_ID,
        "dimensions": 384,
        "provider": "CPUExecutionProvider",
        "python_version": platform.python_version(),
        "inference_class": f"{native_type.__module__}.{native_type.__qualname__}",
        "inference_class_source_sha256": _source_sha256(native_type, label="class"),
        "base_class_source_sha256": _source_sha256(base_type, label="base class"),
        "encoder_source_sha256": _source_sha256(LocalMiniLMEncoder._encode, label="encoder"),
        "dependencies": dependency_versions,
    }
    return hashlib.sha256(
        json.dumps(namespace_data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


def create_encoder(
    *,
    model_dir: str | Path,
    expected_tree_sha256: str,
    passage_cache_path: str | Path | None = None,
) -> LocalMiniLMEncoder:
    if not isinstance(expected_tree_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_tree_sha256):
        raise ValueError("an explicit MiniLM manifest tree SHA-256 is required")
    validated_cache_path = validate_passage_cache_path(passage_cache_path)
    requested = Path(model_dir)
    if requested.is_symlink():
        raise ValueError("MiniLM model root must not be linked")
    root = requested.resolve(strict=True)
    actual = model_tree_hash(root)
    if actual != expected_tree_sha256:
        raise ValueError(f"MiniLM manifest tree mismatch: expected {expected_tree_sha256}, actual {actual}")
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

    class OfflineMiniLM(ONNXMiniLM_L6_V2):
        # Override both download entries; even disappearance after startup cannot
        # invoke Chroma's implicit download/extraction or mutate the model tree.
        def _download(self, *args, **kwargs):
            raise RuntimeError("MiniLM downloads are forbidden")

        def _download_model_if_not_exists(self):
            for name in MODEL_FILES:
                path = self.DOWNLOAD_PATH / "onnx" / name
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError(f"local MiniLM file disappeared: {name}")

    native = OfflineMiniLM(preferred_providers=["CPUExecutionProvider"])
    native.DOWNLOAD_PATH = root
    native._download_model_if_not_exists()
    # Eager initialization makes missing dependencies / malformed model fail at
    # startup, before capabilities claim that the semantic runtime is available.
    native.tokenizer
    native.model
    namespace = _passage_cache_namespace(model_sha256=actual, native_type=type(native))
    cache = PassageVectorCache(namespace=namespace, path=validated_cache_path)
    return LocalMiniLMEncoder(
        native, identity=f"chromadb-native-minilm:{actual}", passage_cache=cache,
    )


def create_ranker(*, model_dir: str | Path, expected_tree_sha256: str) -> SixViewRanker:
    return SixViewRanker(create_encoder(model_dir=model_dir, expected_tree_sha256=expected_tree_sha256),
                         diagnostic_ledger=False, routing_policy=FixedP5Policy())
