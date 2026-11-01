"""Authorized summary768 cosine + original lexical navigation ranker.

Provenance: Foundation qwen-semantic-search-service/ranking.py supplies float32
cosine and summary-SHA ties; qwen-interleaved-navigation/catalog.ts supplies
lexical-first, next-unseen owner weaving. This new profile uses retrieval._bm25
(whole original, its existing tokenizer/k1), not the frozen TypeScript window
BM25 or need-linked question renderer. It makes no Qwen17 end-to-end parity or
semantic efficacy claim. Only supplied authorized observations are embedded;
originals are lexical input and never become embeddings or a new event tree.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import logging
from pathlib import Path
import threading
import time

from .play_memory_encoder import (
    DIMENSIONS, LocalQwenEncoder, QwenRankerConfig, canonical_hash, digest, render_input,
)
from .retrieval import DenseEncoder, RankingResult, _bm25
from .play_memory_vectors import PassageVectorCache, validate_vector

logger = logging.getLogger(__name__)
PROFILE = "play-memory-qwen-summary768-lexical-first-v1"


def vector_cache_key(profile: str, text: str, *, query: bool) -> str:
    return canonical_hash({"profile": profile, "role": "query" if query else "passage",
                           "textSha256": digest(text)})


def weave_owners(lexical, semantic):
    """Exact lexical-first next-unseen weave, one vote per supplied source ID."""
    for lane in (lexical, semantic):
        if len(set(lane)) != len(lane) or any(not isinstance(item, str) or not item.strip() for item in lane):
            raise ValueError("Qwen owner lanes require unique nonblank source IDs")
    lanes, positions, seen, result = (lexical, semantic), [0, 0], set(), []
    while any(positions[i] < len(lanes[i]) for i in (0, 1)):
        for i in (0, 1):
            while positions[i] < len(lanes[i]) and lanes[i][positions[i]] in seen:
                positions[i] += 1
            if positions[i] < len(lanes[i]):
                identifier = lanes[i][positions[i]]
                positions[i] += 1
                seen.add(identifier)
                result.append(identifier)
    return result


class QwenSummaryRanker:
    def __init__(self, config: QwenRankerConfig, *, encoder: DenseEncoder | None = None):
        if not isinstance(config, QwenRankerConfig):
            raise TypeError("Qwen ranker requires explicit QwenRankerConfig")
        self.config = config
        self.encoder = LocalQwenEncoder(config) if encoder is None else encoder
        if not isinstance(self.encoder.identity, str) or not self.encoder.identity.strip():
            raise ValueError("Qwen encoder requires an explicit identity")
        self._encoder_identity = self.encoder.identity
        self.profile = {"schema": PROFILE, "encoderIdentity": self.encoder.identity,
                        "modelTreeSha256": config.model_tree_sha256, "revision": config.revision,
                        "device": config.device, "dtype": config.dtype,
                        "summaryRepresentation": "candidate.observation-verbatim",
                        "queryRepresentation": "query-verbatim", "dimensions": DIMENSIONS,
                        "lexical": "retrieval._bm25-whole-raw-text-k1-1.5",
                        "fusion": "lexical-positive-first-next-unseen",
                        "injectedEncoder": encoder is not None,
                        "implementationSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.identity = PROFILE + ":" + canonical_hash(self.profile)
        self._cache = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False
        self._vector_cache = (PassageVectorCache(config.vector_cache_path, profile_identity=self.identity)
                              if config.vector_cache_path is not None else None)

    def _vectors(self, texts, *, query):
        keys = [vector_cache_key(self.identity, text, query=query) for text in texts]
        missing = list(dict.fromkeys(key for key in keys if key not in self._cache))
        by_key = dict(zip(keys, texts))
        persistent_rows = {}
        text_sha_by_key = {}
        if not query and self._vector_cache is not None and missing:
            text_sha_by_key = {key: digest(by_key[key]) for key in missing}
            persisted_by_sha = self._vector_cache.get_many(list(text_sha_by_key.values()))
            persistent_rows = {key: persisted_by_sha[text_sha]
                               for key, text_sha in text_sha_by_key.items()
                               if text_sha in persisted_by_sha}
            missing = [key for key in missing if key not in persistent_rows]
        if missing:
            if query:
                encoded = [self.encoder.encode_query(by_key[key]) for key in missing]
            else:
                encoded = self.encoder.encode_passages([by_key[key] for key in missing])
            if len(encoded) != len(missing):
                raise ValueError("Qwen encoder output count mismatch")
            # Validate the whole batch before caching any new row.
            new_rows = {key: validate_vector(value) for key, value in zip(missing, encoded)}
            if not query and self._vector_cache is not None:
                if not text_sha_by_key:
                    text_sha_by_key = {key: digest(by_key[key]) for key in missing}
                persisted_by_sha = self._vector_cache.put_many(
                    {text_sha_by_key[key]: vector for key, vector in new_rows.items()})
                new_rows = {key: persisted_by_sha[text_sha_by_key[key]] for key in new_rows}
        else:
            new_rows = {}
        # Retain result references before bounded eviction; input can exceed cap.
        result = [self._cache[key] if key in self._cache else
                  persistent_rows[key] if key in persistent_rows else new_rows[key] for key in keys]
        for key, vector in zip(keys, result):
            self._cache[key] = vector
            self._cache.move_to_end(key)
            while len(self._cache) > self.config.cache_capacity:
                self._cache.popitem(last=False)
        return result, len(keys) - len(missing), len(missing)

    @staticmethod
    def _validated_candidates(candidates):
        candidates = tuple(candidates)
        identifiers = [candidate.source_event_id for candidate in candidates]
        if len(set(identifiers)) != len(identifiers) or any(not isinstance(i, str) or not i.strip() for i in identifiers):
            raise ValueError("Qwen candidates require unique nonblank source IDs")
        for candidate in candidates:
            render_input(candidate.observation, query=False)
            if not isinstance(candidate.raw_text, str):
                raise ValueError("Qwen candidate original must be text")
            candidate.raw_text.encode("utf-8", errors="strict")
        return candidates

    def _assert_profile(self):
        if self._closed:
            raise RuntimeError("Qwen ranker is closed")
        if self.encoder.identity != self._encoder_identity:
            raise ValueError("Qwen encoder identity changed after profile binding")

    def prepare(self, *, candidates):
        """Encode only authorized passage observations into the local vector cache.

        Candidate membership and authorization remain the caller's responsibility;
        this method never accepts a query, ranks, reads records, or creates memory.
        """
        started = time.perf_counter()
        candidates = self._validated_candidates(candidates)
        with self._lock:
            self._assert_profile()
            _vectors, cache_hits, cache_misses = self._vectors(
                [candidate.observation for candidate in candidates], query=False)
            return {"profile": self.identity, "candidateCount": len(candidates),
                    "cacheHits": cache_hits, "cacheMisses": cache_misses,
                    "cacheSize": len(self._cache), "cacheCapacity": self.config.cache_capacity,
                    "elapsedMs": max(0.0, (time.perf_counter() - started) * 1000.0)}

    def rank(self, *, query, candidates):
        import numpy as np

        started = time.perf_counter()
        render_input(query, query=True)  # Validate even empty authorized universes.
        candidates = self._validated_candidates(candidates)
        identifiers = [candidate.source_event_id for candidate in candidates]
        with self._lock:
            self._assert_profile()
            if not candidates:
                return RankingResult([], {}, {"profile": self.identity, "candidateCount": 0})
            summaries = [candidate.observation for candidate in candidates]
            vectors, passage_hits, passage_misses = self._vectors(summaries, query=False)
            query_vectors, query_hits, query_misses = self._vectors([query], query=True)
            # Same float32 matrix-vector operation and SHA ordering as frozen ranking.py.
            dense_values = np.stack(vectors) @ query_vectors[0]
            if not np.isfinite(dense_values).all():
                raise ValueError("Qwen cosine ranking produced nonfinite scores")
            dense = {identifier: float(score) for identifier, score in zip(identifiers, dense_values)}
            text_keys = {candidate.source_event_id: digest(candidate.observation) for candidate in candidates}
            semantic = sorted(identifiers, key=lambda i: (-dense[i], text_keys[i], i))
            lexical_scores = _bm25(query, [candidate.raw_text for candidate in candidates], identifiers)
            lexical = sorted((i for i in identifiers if lexical_scores[i] > 0), key=lambda i: (-lexical_scores[i], i))
            ordered = weave_owners(lexical, semantic)
            if set(ordered) != set(identifiers) or len(ordered) != len(identifiers):
                raise ValueError("Qwen ranking escaped the supplied authorized universe")
            trace = {"profile": self.identity, "candidateCount": len(candidates), "querySha256": digest(query),
                     "semanticOrder": semantic, "lexicalOrder": lexical, "displayOrder": ordered,
                     "denseScores": dense, "lexicalScores": lexical_scores,
                     "summarySha256": text_keys, "cacheHits": passage_hits + query_hits,
                     "cacheMisses": passage_misses + query_misses, "cacheSize": len(self._cache),
                     "navigationOnly": True}
            logger.info("Qwen authorized ranking profile=%s candidates=%d hits=%d misses=%d seconds=%.6f",
                        self.identity, len(candidates), trace["cacheHits"], trace["cacheMisses"], time.perf_counter() - started)
            return RankingResult(ordered, {identifier: 1.0 / (index + 1) for index, identifier in enumerate(ordered)}, trace)

    def close(self):
        with self._lock:
            self._closed = True
            self._cache.clear()
            first_error = None
            if self._vector_cache is not None:
                try:
                    self._vector_cache.close()
                except BaseException as error:
                    first_error = error
            close = getattr(self.encoder, "close", None)
            if close is not None:
                try:
                    close()
                except BaseException as error:
                    if first_error is not None:
                        raise first_error from error
                    raise
            if first_error is not None:
                raise first_error


def create_qwen_ranker(config: QwenRankerConfig, *, encoder: DenseEncoder | None = None):
    return QwenSummaryRanker(config, encoder=encoder)
