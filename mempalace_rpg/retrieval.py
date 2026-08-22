"""ACL-after-authorization product retrieval; no model runtime starts here."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Protocol, Sequence


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class AuthorizedRetrievalCandidate:
    source_event_id: str
    source_scene_id: str
    raw_text: str
    observation: str
    checkpoint_key: str
    policy_tuple: tuple[str | None, ...]
    chronological_order_key: tuple[int, str]
    ranking_key: str


@dataclass(frozen=True)
class RankingResult:
    ranked_event_ids: list[str]
    scores: dict[str, float]
    trace: dict[str, Any]


class AuthorizedEventRanker(Protocol):
    def rank(self, *, query: str, candidates: Sequence[AuthorizedRetrievalCandidate]) -> RankingResult: ...


class DenseEncoder(Protocol):
    """BGE-compatible split encoding contract; query and passage prefixes stay separate."""

    identity: str

    def encode_passages(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def encode_query(self, query: str) -> Sequence[float]: ...


def _semantic_list(values: Sequence[str] | None, name: str) -> str:
    if values is None:
        return "[]"
    if isinstance(values, (str, bytes)) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{name} must be a sequence of strings")
    return json.dumps(sorted(set(values)), ensure_ascii=False, separators=(",", ":"))


def structured_observation(
    *,
    summary: str,
    event_type: str | None,
    actor_id: str | None,
    target_id: str | None,
    related_entities: Sequence[str] | None,
    related_quests: Sequence[str] | None,
    related_locations: Sequence[str] | None,
    in_world_time: str | None,
    location_id: str | None,
) -> str:
    """Stable event semantics. ACL policy stays outside encoder-visible text."""
    return "\n".join((
        "summary=" + summary,
        "event_type=" + (event_type or ""),
        "actor_id=" + (actor_id or ""),
        "target_id=" + (target_id or ""),
        "related_entities=" + _semantic_list(related_entities, "related_entities"),
        "related_quests=" + _semantic_list(related_quests, "related_quests"),
        "related_locations=" + _semantic_list(related_locations, "related_locations"),
        "in_world_time=" + (in_world_time or ""),
        "location_id=" + (location_id or ""),
    ))


def _tokens(text: str) -> list[str]:
    """Split each CJK ideograph, while retaining ordinary word tokens."""
    return re.findall(r"[a-z0-9_]+|[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text.casefold())


def _bm25(query: str, texts: Sequence[str], ids: Sequence[str]) -> dict[str, float]:
    query_tokens = _tokens(query)
    documents = [_tokens(text) for text in texts]
    frequencies = [Counter(document) for document in documents]
    document_frequencies = Counter(token for document in documents for token in set(document))
    average = sum(len(document) for document in documents) / len(documents) if documents else 0.0
    result: dict[str, float] = {}
    for identifier, document, counts in zip(ids, documents, frequencies):
        score = 0.0
        for token in query_tokens:
            frequency = counts[token]
            if not frequency:
                continue
            document_frequency = document_frequencies[token]
            inverse = math.log(1.0 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5))
            score += inverse * frequency * 2.5 / (frequency + 1.5 * (1.0 - 0.75 + 0.75 * len(document) / max(average, 1.0)))
        result[identifier] = score
    return result


def _validated_vectors(values: Any, expected_count: int, label: str) -> list[list[float]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence) or len(values) != expected_count:
        raise ValueError(f"{label} encoder output count mismatch")
    output: list[list[float]] = []
    dimension: int | None = None
    for index, vector in enumerate(values):
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            raise ValueError(f"{label} encoder vector {index} is not numeric")
        try:
            numeric = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} encoder vector {index} is not numeric") from exc
        if not numeric or not all(math.isfinite(value) for value in numeric):
            raise ValueError(f"{label} encoder vector {index} is non-finite or empty")
        if dimension is None:
            dimension = len(numeric)
        elif len(numeric) != dimension:
            raise ValueError(f"{label} encoder dimensions differ")
        output.append(numeric)
    return output


def _ordered(scores: dict[str, float], ranking_keys: dict[str, str]) -> list[str]:
    return [identifier for identifier, _score in sorted(
        scores.items(), key=lambda pair: (-pair[1], ranking_keys[pair[0]])
    )]


class SixViewRanker:
    """Frozen weighted-RRF six-view ranker over already authorized candidates."""

    weights = {
        "raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0,
        "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0,
    }
    rrf_k = 60

    def __init__(self, encoder: DenseEncoder, *, diagnostic_ledger: bool = False) -> None:
        if type(diagnostic_ledger) is not bool:
            raise ValueError("diagnostic_ledger must be a bool")
        self.encoder = encoder
        self.diagnostic_ledger = diagnostic_ledger
        self._passage_cache: dict[tuple[str, str, str], tuple[tuple[float, ...], ...]] = {}

    def _encoder_identity(self) -> str:
        identity = getattr(self.encoder, "identity")
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("dense encoder identity must be a non-empty string")
        return identity.strip()

    def _passage_vectors(self, *, view: str, texts: Sequence[str], encoder_identity: str) -> list[list[float]]:
        content_digest = _digest({"view": view, "texts": list(texts)})
        cache_key = (encoder_identity, view, content_digest)
        cached = self._passage_cache.get(cache_key)
        if cached is None:
            values = _validated_vectors(self.encoder.encode_passages(texts), len(texts), view)
            cached = tuple(tuple(vector) for vector in values)
            self._passage_cache[cache_key] = cached
        return [list(vector) for vector in cached]

    @staticmethod
    def _dense_scores(query_vector: Sequence[float], passage_vectors: Sequence[Sequence[float]], ids: Sequence[str], label: str) -> dict[str, float]:
        if len(passage_vectors) != len(ids):
            raise ValueError(f"{label} encoder output count mismatch")
        scores: dict[str, float] = {}
        for identifier, vector in zip(ids, passage_vectors):
            if len(vector) != len(query_vector):
                raise ValueError(f"{label} encoder query dimensions differ")
            score = sum(left * right for left, right in zip(query_vector, vector))
            if not math.isfinite(score):
                raise ValueError(f"{label} dense score is non-finite")
            scores[identifier] = score
        return scores

    def _fcd1_diagnostic_ledger(
        self,
        *,
        input_sha256: str,
        candidates: Sequence[AuthorizedRetrievalCandidate],
        ranking_key_sha256: dict[str, str],
        score_views: dict[str, dict[str, float]],
        ranks: dict[str, dict[str, int]],
        ranking_keys_by_id: dict[str, str],
        totals: dict[str, float],
        ordered: list[str],
        group_ids: list[str],
        ordered_groups: list[tuple[tuple[str, tuple[str | None, ...]], list[AuthorizedRetrievalCandidate]]],
        group_scores: dict[str, float],
    ) -> dict[str, Any]:
        """Build the benchmark-only, text-free replay ledger after ranking is frozen."""
        self._fcd1_validate_score_views(score_views)
        ranking_key_order = {
            candidate.source_event_id: index
            for index, candidate in enumerate(candidates, start=1)
        }
        view_orders = {
            name: [ranking_key_sha256[identifier] for identifier in _ordered(scores, ranking_keys_by_id)]
            for name, scores in score_views.items()
        }
        view_order_sha256 = {
            name: _digest(order)
            for name, order in view_orders.items()
        }
        checkpoint_groups = [
            {
                "checkpoint_sha256": hashlib.sha256(key[0].encode()).hexdigest(),
                "policy_sha256": _digest(list(key[1])),
                "checkpoint_score": group_scores[group_id],
                "member_count": len(members),
                "chronological_members": [
                    {
                        "source_event_id": member.source_event_id,
                        "ranking_key_sha256": ranking_key_sha256[member.source_event_id],
                    }
                    for member in sorted(members, key=lambda member: member.chronological_order_key)
                ],
            }
            for group_id, (key, members) in zip(group_ids, ordered_groups)
        ]
        for group in checkpoint_groups:
            group["group_id"] = "group:" + _digest([
                group["checkpoint_sha256"], group["policy_sha256"],
            ])
        authorization_rows = sorted(
            (
                {
                    "ranking_key_sha256": member["ranking_key_sha256"],
                    "policy_sha256": group["policy_sha256"],
                }
                for group in checkpoint_groups
                for member in group["chronological_members"]
            ),
            key=lambda row: row["ranking_key_sha256"],
        )
        view_top_50 = {
            name: [
                {
                    "source_event_id": identifier,
                    "ranking_key_sha256": ranking_key_sha256[identifier],
                    "ranking_key_order": ranking_key_order[identifier],
                    "rank": ranks[name][identifier],
                    "score": score_views[name][identifier],
                }
                for identifier in _ordered(scores, ranking_keys_by_id)[:50]
            ]
            for name, scores in score_views.items()
        }
        return {
            "schema": "aerp3-fcd1-replay-ledger-v1",
            "input_sha256": input_sha256,
            "authorization_sha256": _digest(authorization_rows),
            "view_order_sha256": view_order_sha256,
            "view_top_50_sha256": {name: _digest(rows) for name, rows in view_top_50.items()},
            "view_full_order": {
                name: _ordered(scores, ranking_keys_by_id)
                for name, scores in score_views.items()
            },
            "view_top_50": view_top_50,
            "fused_top_50": [
                {
                    "source_event_id": identifier,
                    "ranking_key_sha256": ranking_key_sha256[identifier],
                    "ranking_key_order": ranking_key_order[identifier],
                    "rank": rank,
                    "final_rrf": totals[identifier],
                    "component_ranks": {name: ranks[name][identifier] for name in self.weights},
                    "component_rank_receipts": [
                        {
                            "view": name,
                            "view_order_sha256": view_order_sha256[name],
                            "ranking_key_sha256": ranking_key_sha256[identifier],
                            "rank": ranks[name][identifier],
                        }
                        for name in self.weights
                    ],
                    "contributions": {name: self.weights[name] / (self.rrf_k + ranks[name][identifier]) for name in self.weights},
                }
                for rank, identifier in enumerate(ordered[:50], start=1)
            ],
            "checkpoint_tie_group_semantics": "checkpoint_policy_rollup",
            "checkpoint_tie_groups": checkpoint_groups,
        }

    @staticmethod
    def _fcd1_validate_score_views(score_views: dict[str, dict[str, float]]) -> None:
        if any(not math.isfinite(score) for scores in score_views.values() for score in scores.values()):
            raise ValueError("six-view diagnostic score is non-finite")

    def rank(self, *, query: str, candidates: Sequence[AuthorizedRetrievalCandidate]) -> RankingResult:
        query_sha256 = hashlib.sha256(query.encode()).hexdigest()
        encoder_identity = self._encoder_identity()
        if not candidates:
            trace = {
                "schema": "aerp2-product-six-view-v1",
                "encoder_identity": encoder_identity,
                "weights": dict(self.weights),
                "rrf_k": self.rrf_k,
                "input_sha256": _digest([]),
                "query_sha256": query_sha256,
                "view_digests": {},
                "selected": [],
            }
            if self.diagnostic_ledger:
                trace["fcd1_diagnostic_ledger"] = {
                    "schema": "aerp3-fcd1-replay-ledger-v1",
                    "input_sha256": _digest([]),
                    "authorization_sha256": _digest([]),
                    "view_order_sha256": {name: _digest([]) for name in self.weights},
                    "view_top_50_sha256": {name: _digest([]) for name in self.weights},
                    "view_full_order": {name: [] for name in self.weights},
                    "view_top_50": {name: [] for name in self.weights},
                    "fused_top_50": [],
                    "checkpoint_tie_group_semantics": "checkpoint_policy_rollup",
                    "checkpoint_tie_groups": [],
                }
            return RankingResult([], {}, trace)
        ids = [candidate.source_event_id for candidate in candidates]
        if len(ids) != len(set(ids)) or any(not identifier for identifier in ids):
            raise ValueError("ranker candidates must have unique non-empty source_event_id values")
        ranking_keys = [candidate.ranking_key for candidate in candidates]
        if any(not isinstance(key, str) or not key.strip() for key in ranking_keys):
            raise ValueError("ranker candidates must have unique non-empty ranking_key values")
        if len(ranking_keys) != len(set(ranking_keys)):
            raise ValueError("ranker candidates must have unique non-empty ranking_key values")
        candidates = sorted(candidates, key=lambda candidate: candidate.ranking_key)
        ids = [candidate.source_event_id for candidate in candidates]
        ranking_keys_by_id = {candidate.source_event_id: candidate.ranking_key for candidate in candidates}
        query_vector = _validated_vectors([self.encoder.encode_query(query)], 1, "query")[0]
        raw = [candidate.raw_text for candidate in candidates]
        observation = [candidate.observation for candidate in candidates]
        combo = [left + "\n" + right for left, right in zip(raw, observation)]
        groups: dict[tuple[str, tuple[str | None, ...]], list[AuthorizedRetrievalCandidate]] = {}
        for candidate in candidates:
            checkpoint = candidate.checkpoint_key.strip()
            if not checkpoint:
                raise ValueError("candidate checkpoint_key must be non-empty")
            groups.setdefault((checkpoint, candidate.policy_tuple), []).append(candidate)
        ordered_groups = sorted(groups.items(), key=lambda item: _digest([item[0][0], list(item[0][1])]))
        group_ids = ["group:" + _digest([key[0], list(key[1])]) for key, _members in ordered_groups]
        group_texts = [
            "\n".join(
                member.observation
                for member in sorted(
                    members,
                    key=lambda member: (member.chronological_order_key[0], member.ranking_key),
                )
            )
            for _key, members in ordered_groups
        ]
        unique_group_texts = list(dict.fromkeys(group_texts))
        unique_group_vectors = self._passage_vectors(view="checkpoint", texts=unique_group_texts, encoder_identity=encoder_identity)
        group_vectors_by_text = dict(zip(unique_group_texts, unique_group_vectors))
        group_scores = self._dense_scores(query_vector, [group_vectors_by_text[text] for text in group_texts], group_ids, "checkpoint")
        checkpoint_scores = {
            member.source_event_id: group_scores[group_id]
            for (group_id, (_key, members)) in zip(group_ids, ordered_groups) for member in members
        }
        score_views = {
            "raw_bm25": _bm25(query, raw, ids),
            "observation_bm25": _bm25(query, observation, ids),
            "raw_dense": self._dense_scores(query_vector, self._passage_vectors(view="raw", texts=raw, encoder_identity=encoder_identity), ids, "raw"),
            "observation_dense": self._dense_scores(query_vector, self._passage_vectors(view="observation", texts=observation, encoder_identity=encoder_identity), ids, "observation"),
            "checkpoint_dense": checkpoint_scores,
            "combo_dense": self._dense_scores(query_vector, self._passage_vectors(view="combo", texts=combo, encoder_identity=encoder_identity), ids, "combo"),
        }
        ranks = {
            name: {
                identifier: rank
                for rank, identifier in enumerate(_ordered(scores, ranking_keys_by_id), start=1)
            }
            for name, scores in score_views.items()
        }
        totals = {identifier: sum(self.weights[name] / (self.rrf_k + ranks[name][identifier]) for name in self.weights) for identifier in ids}
        if not all(math.isfinite(score) for score in totals.values()):
            raise ValueError("weighted RRF score is non-finite")
        ordered = _ordered(totals, ranking_keys_by_id)
        ranking_key_sha256 = {
            identifier: hashlib.sha256(ranking_keys_by_id[identifier].encode()).hexdigest()
            for identifier in ids
        }
        input_sha256 = _digest([{
            "ranking_key": candidate.ranking_key,
            "raw_sha256": hashlib.sha256(candidate.raw_text.encode()).hexdigest(),
            "observation_sha256": hashlib.sha256(candidate.observation.encode()).hexdigest(),
            "checkpoint": candidate.checkpoint_key.strip(),
            "policy": candidate.policy_tuple,
            "scene_time_sort": candidate.chronological_order_key[0],
        } for candidate in candidates])
        selected = [
            {
                "source_event_id": identifier,
                "ranking_key_sha256": ranking_key_sha256[identifier],
                "final_rrf": totals[identifier],
                "component_ranks": {name: ranks[name][identifier] for name in self.weights},
                "contributions": {name: self.weights[name] / (self.rrf_k + ranks[name][identifier]) for name in self.weights},
            }
            for identifier in ordered
        ]
        trace = {
            "schema": "aerp2-product-six-view-v1",
            "encoder_identity": encoder_identity,
            "weights": dict(self.weights), "rrf_k": self.rrf_k,
            "query_sha256": query_sha256,
            "input_sha256": input_sha256,
            "view_digests": {
                name: _digest([ranking_keys_by_id[identifier] for identifier in _ordered(scores, ranking_keys_by_id)])
                for name, scores in score_views.items()
            },
            "selected": selected,
        }
        if self.diagnostic_ledger:
            trace["fcd1_diagnostic_ledger"] = self._fcd1_diagnostic_ledger(
                input_sha256=input_sha256, candidates=candidates,
                ranking_key_sha256=ranking_key_sha256, score_views=score_views,
                ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, totals=totals,
                ordered=ordered, group_ids=group_ids, ordered_groups=ordered_groups,
                group_scores=group_scores,
            )
        return RankingResult(ordered, totals, trace)


__all__ = ["AuthorizedEventRanker", "AuthorizedRetrievalCandidate", "DenseEncoder", "RankingResult", "SixViewRanker", "structured_observation"]
