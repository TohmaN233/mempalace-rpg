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
    query_tokens = tuple(dict.fromkeys(_tokens(query)))
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


SIX_VIEW_WEIGHTS = {
    "raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0,
    "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0,
}
RAW_EXPERT_WEIGHTS = {
    "raw_bm25": 2.0, "observation_bm25": 0.0, "raw_dense": 1.0,
    "observation_dense": 0.0, "checkpoint_dense": 0.0, "combo_dense": 0.0,
}
P5_EXPERT_WEIGHTS = {
    "raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0,
    "observation_dense": 2.0, "checkpoint_dense": 0.0, "combo_dense": 1.0,
}


@dataclass(frozen=True)
class FusionRoutingDecision:
    """Immutable, score-only result of a routing policy over precomputed ranks."""

    route: str
    totals: tuple[tuple[str, float], ...]
    effective_weights: tuple[tuple[str, float], ...]
    raw_totals: tuple[tuple[str, float], ...] = ()
    p5_totals: tuple[tuple[str, float], ...] = ()
    anchor_numerator: float | None = None
    anchor_denominator: float | None = None
    anchor_ratio: float | None = None
    raw_top10_ranking_sha256: str | None = None
    p5_top10_ranking_sha256: str | None = None
    final_ranking_sha256: str | None = None
    config_json: str | None = None
    config_sha256: str | None = None

    @property
    def config(self) -> dict[str, Any] | None:
        if self.config_json is None:
            return None
        try:
            value = json.loads(self.config_json)
        except json.JSONDecodeError as exc:
            raise ValueError("routing policy config is invalid") from exc
        if not isinstance(value, dict):
            raise ValueError("routing policy config is invalid")
        return value


class FusionRoutingPolicy(Protocol):
    """Score-only routing seam; implementations receive no query or candidate text."""

    def decide(
        self, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
    ) -> FusionRoutingDecision: ...


class FixedSixViewPolicy:
    """The historical fixed six-view adapter, available as an explicit seam."""

    def decide(
        self, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
    ) -> FusionRoutingDecision:
        _validate_routing_inputs(ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, rrf_k=rrf_k)
        totals = _rrf_totals(ranks, SIX_VIEW_WEIGHTS, rrf_k)
        return FusionRoutingDecision(
            route="six_view", totals=tuple(totals.items()),
            effective_weights=tuple(SIX_VIEW_WEIGHTS.items()),
        )


class FixedP5Policy:
    """Explicit static P5 expert for paired product comparisons.

    This policy is deliberately separate from :class:`RawAnchoredP5Policy`.
    Static P5 is an arm of the comparison, not a threshold decision, so its
    receipt must not invent an anchor value (including ``+/-inf``) merely to
    reuse the gated policy's implementation.
    """

    schema = "aerp5-fixed-p5-v1"
    policy = "fixed_p5"

    @classmethod
    def _config(cls, rrf_k: int) -> dict[str, Any]:
        if not isinstance(rrf_k, int) or isinstance(rrf_k, bool) or rrf_k < 0:
            raise ValueError("fixed P5 rrf_k is invalid")
        return {
            "schema": cls.schema,
            "policy": cls.policy,
            "p5_weights": dict(P5_EXPERT_WEIGHTS),
            "ordinary_sum_view_order": list(SIX_VIEW_WEIGHTS),
            "rrf_k": rrf_k,
        }

    def decide(
        self, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
    ) -> FusionRoutingDecision:
        ids = _validate_routing_inputs(ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, rrf_k=rrf_k)
        totals = _rrf_totals(ranks, P5_EXPERT_WEIGHTS, rrf_k)
        config = self._config(rrf_k)
        ranking_key_sha256 = {
            identifier: hashlib.sha256(ranking_keys_by_id[identifier].encode()).hexdigest()
            for identifier in ids
        }
        final_ranking_sha256 = _digest([
            ranking_key_sha256[identifier] for identifier in _ordered(totals, ranking_keys_by_id)
        ])
        return FusionRoutingDecision(
            route="p5",
            totals=tuple(totals.items()),
            effective_weights=tuple(P5_EXPERT_WEIGHTS.items()),
            p5_totals=tuple(totals.items()),
            final_ranking_sha256=final_ranking_sha256,
            config_json=json.dumps(
                config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            ),
            config_sha256=_digest(config),
        )


class RawAnchoredP5Policy:
    """A single pre-registered raw-anchored gate between Raw and P5 experts."""

    schema = "aerp4-raw-anchored-p5-v1"
    policy = "raw_anchored_p5"

    def __init__(self, tau: float) -> None:
        value = self._validated_tau(tau)
        object.__setattr__(self, "_tau", value)
        object.__setattr__(self, "_tau_integrity_sha256", _digest({"tau": self._tau_receipt(value)}))

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"tau", "_tau", "_tau_integrity_sha256"} and hasattr(self, "_tau"):
            raise AttributeError("tau is read-only")
        object.__setattr__(self, name, value)

    @staticmethod
    def _validated_tau(tau: Any) -> float:
        if isinstance(tau, bool):
            raise ValueError("tau must be numeric")
        try:
            value = float(tau)
        except (TypeError, ValueError) as exc:
            raise ValueError("tau must be numeric") from exc
        if math.isnan(value):
            raise ValueError("tau must not be NaN")
        return value

    @staticmethod
    def _tau_receipt(tau: float) -> float | str:
        return tau if math.isfinite(tau) else ("+inf" if tau > 0 else "-inf")

    def _tau_snapshot(self) -> float:
        value = self._validated_tau(object.__getattribute__(self, "_tau"))
        if _digest({"tau": self._tau_receipt(value)}) != object.__getattribute__(self, "_tau_integrity_sha256"):
            raise ValueError("raw-anchored tau integrity mismatch")
        return value

    @property
    def tau(self) -> float:
        return self._tau_snapshot()

    def _config(self, rrf_k: int, tau: float) -> dict[str, Any]:
        return {
            "schema": self.schema, "policy": self.policy, "tau": self._tau_receipt(tau),
            "raw_weights": dict(RAW_EXPERT_WEIGHTS), "p5_weights": dict(P5_EXPERT_WEIGHTS),
            "support_top_k": 10, "comparator": ">=",
            "ordinary_sum_view_order": list(SIX_VIEW_WEIGHTS), "rrf_k": rrf_k,
        }

    def decide(
        self, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
    ) -> FusionRoutingDecision:
        tau = self._tau_snapshot()
        ids = _validate_routing_inputs(ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, rrf_k=rrf_k)
        raw_totals = _rrf_totals(ranks, RAW_EXPERT_WEIGHTS, rrf_k)
        p5_totals = _rrf_totals(ranks, P5_EXPERT_WEIGHTS, rrf_k)
        limit = min(10, len(ids))
        raw_top = _ordered(raw_totals, ranking_keys_by_id)[:limit]
        p5_top = _ordered(p5_totals, ranking_keys_by_id)[:limit]
        denominator = sum(raw_totals[identifier] for identifier in raw_top)
        numerator = sum(raw_totals[identifier] for identifier in p5_top)
        if not math.isfinite(numerator) or not math.isfinite(denominator) or denominator <= 0.0:
            raise ValueError("raw-anchored policy ratio is invalid")
        anchor_ratio = numerator / denominator
        if not math.isfinite(anchor_ratio):
            raise ValueError("raw-anchored policy ratio is non-finite")
        route, weights, totals = (
            ("p5", P5_EXPERT_WEIGHTS, p5_totals)
            if anchor_ratio >= tau else ("raw", RAW_EXPERT_WEIGHTS, raw_totals)
        )
        config = self._config(rrf_k, tau)
        ranking_key_sha256 = {
            identifier: hashlib.sha256(ranking_keys_by_id[identifier].encode()).hexdigest()
            for identifier in ids
        }
        return FusionRoutingDecision(
            route=route, totals=tuple(totals.items()), effective_weights=tuple(weights.items()),
            raw_totals=tuple(raw_totals.items()), p5_totals=tuple(p5_totals.items()),
            anchor_numerator=numerator, anchor_denominator=denominator, anchor_ratio=anchor_ratio,
            raw_top10_ranking_sha256=_digest([ranking_key_sha256[identifier] for identifier in raw_top]),
            p5_top10_ranking_sha256=_digest([ranking_key_sha256[identifier] for identifier in p5_top]),
            final_ranking_sha256=_digest([ranking_key_sha256[identifier] for identifier in _ordered(totals, ranking_keys_by_id)]),
            config_json=json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False),
            config_sha256=_digest(config),
        )


def _validate_routing_inputs(
    *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
) -> list[str]:
    if not isinstance(rrf_k, int) or isinstance(rrf_k, bool) or rrf_k < 0:
        raise ValueError("routing rrf_k is invalid")
    if set(ranks) != set(SIX_VIEW_WEIGHTS) or not ranking_keys_by_id:
        raise ValueError("routing candidate universe must be non-empty and complete")
    ids = list(ranking_keys_by_id)
    if any(not isinstance(identifier, str) or not identifier or not isinstance(key, str) or not key for identifier, key in ranking_keys_by_id.items()) or len(set(ranking_keys_by_id.values())) != len(ranking_keys_by_id):
        raise ValueError("routing ranking_key values must be unique non-empty strings")
    expected = set(range(1, len(ids) + 1))
    for view, view_ranks in ranks.items():
        if set(view_ranks) != set(ids) or set(view_ranks.values()) != expected or any(not isinstance(rank, int) or isinstance(rank, bool) for rank in view_ranks.values()):
            raise ValueError("routing ranks are invalid")
    return ids


def _rrf_totals(ranks: dict[str, dict[str, int]], weights: dict[str, float], rrf_k: int) -> dict[str, float]:
    if set(weights) != set(SIX_VIEW_WEIGHTS) or any(isinstance(weight, bool) or not isinstance(weight, (int, float)) or not math.isfinite(weight) or weight < 0.0 for weight in weights.values()):
        raise ValueError("routing weights are invalid")
    identifiers = list(next(iter(ranks.values())))
    totals = {
        identifier: sum(weights[view] / (rrf_k + ranks[view][identifier]) for view in SIX_VIEW_WEIGHTS)
        for identifier in identifiers
    }
    if not all(math.isfinite(total) for total in totals.values()):
        raise ValueError("weighted RRF score is non-finite")
    return totals


def _routing_pairs(value: Any, expected_keys: set[str], label: str) -> dict[str, float]:
    if not isinstance(value, tuple) or len(value) != len(expected_keys):
        raise ValueError("routing policy decision is invalid")
    result: dict[str, float] = {}
    for pair in value:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("routing policy decision is invalid")
        key, numeric = pair
        if not isinstance(key, str) or key in result or isinstance(numeric, bool) or not isinstance(numeric, (int, float)) or not math.isfinite(numeric):
            raise ValueError("routing policy decision is invalid")
        result[key] = float(numeric)
    if set(result) != expected_keys:
        raise ValueError("routing policy decision is invalid")
    return result


def _validated_routing_decision(
    decision: FusionRoutingDecision, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
) -> tuple[dict[str, float], dict[str, float]]:
    if not isinstance(decision, FusionRoutingDecision):
        raise ValueError("routing policy returned an invalid decision")
    totals = _routing_pairs(decision.totals, set(ranking_keys_by_id), "totals")
    weights = _routing_pairs(decision.effective_weights, set(SIX_VIEW_WEIGHTS), "weights")
    expected_weights = {"six_view": SIX_VIEW_WEIGHTS, "raw": RAW_EXPERT_WEIGHTS, "p5": P5_EXPERT_WEIGHTS}.get(decision.route)
    if expected_weights is None or weights != expected_weights:
        raise ValueError("routing policy decision is invalid")
    replayed = _rrf_totals(ranks, weights, rrf_k)
    if totals != replayed:
        raise ValueError("routing policy decision is invalid")
    return totals, weights


def _validate_raw_anchored_decision(
    policy: RawAnchoredP5Policy, decision: FusionRoutingDecision, *, ranks: dict[str, dict[str, int]], ranking_keys_by_id: dict[str, str], rrf_k: int
) -> tuple[dict[str, float], dict[str, float], list[str], list[str], dict[str, Any]]:
    raw_totals = _routing_pairs(decision.raw_totals, set(ranking_keys_by_id), "raw totals")
    p5_totals = _routing_pairs(decision.p5_totals, set(ranking_keys_by_id), "p5 totals")
    expected_raw = _rrf_totals(ranks, RAW_EXPERT_WEIGHTS, rrf_k)
    expected_p5 = _rrf_totals(ranks, P5_EXPERT_WEIGHTS, rrf_k)
    if raw_totals != expected_raw or p5_totals != expected_p5:
        raise ValueError("raw-anchored policy decision is invalid")
    limit = min(10, len(ranking_keys_by_id))
    raw_top = _ordered(expected_raw, ranking_keys_by_id)[:limit]
    p5_top = _ordered(expected_p5, ranking_keys_by_id)[:limit]
    numerator = sum(expected_raw[identifier] for identifier in p5_top)
    denominator = sum(expected_raw[identifier] for identifier in raw_top)
    ratio = numerator / denominator
    tau = policy._tau_snapshot()
    config = policy._config(rrf_k, tau)
    ranking_key_sha256 = {
        identifier: hashlib.sha256(ranking_keys_by_id[identifier].encode()).hexdigest()
        for identifier in ranking_keys_by_id
    }
    totals = expected_p5 if ratio >= tau else expected_raw
    if (
        decision.anchor_numerator != numerator or decision.anchor_denominator != denominator
        or decision.anchor_ratio != ratio or decision.config != config
        or decision.config_sha256 != _digest(config)
        or decision.route != ("p5" if ratio >= tau else "raw")
        or decision.raw_top10_ranking_sha256 != _digest([ranking_key_sha256[identifier] for identifier in raw_top])
        or decision.p5_top10_ranking_sha256 != _digest([ranking_key_sha256[identifier] for identifier in p5_top])
        or decision.final_ranking_sha256 != _digest([ranking_key_sha256[identifier] for identifier in _ordered(totals, ranking_keys_by_id)])
    ):
        raise ValueError("raw-anchored policy decision is invalid")
    return expected_raw, expected_p5, raw_top, p5_top, config


def _validate_fixed_p5_decision(
    policy: FixedP5Policy,
    decision: FusionRoutingDecision,
    *,
    ranks: dict[str, dict[str, int]],
    ranking_keys_by_id: dict[str, str],
    rrf_k: int,
) -> dict[str, Any]:
    """Validate the immutable receipt fields of the static P5 arm."""
    expected = _rrf_totals(ranks, P5_EXPERT_WEIGHTS, rrf_k)
    totals = _routing_pairs(decision.totals, set(ranking_keys_by_id), "totals")
    p5_totals = _routing_pairs(decision.p5_totals, set(ranking_keys_by_id), "p5 totals")
    config = policy._config(rrf_k)
    ranking_key_sha256 = {
        identifier: hashlib.sha256(ranking_keys_by_id[identifier].encode()).hexdigest()
        for identifier in ranking_keys_by_id
    }
    expected_final_digest = _digest([
        ranking_key_sha256[identifier] for identifier in _ordered(expected, ranking_keys_by_id)
    ])
    if (
        decision.route != "p5"
        or totals != expected
        or p5_totals != expected
        or dict(decision.effective_weights) != P5_EXPERT_WEIGHTS
        or decision.config != config
        or decision.config_sha256 != _digest(config)
        or decision.final_ranking_sha256 != expected_final_digest
    ):
        raise ValueError("fixed P5 policy decision is invalid")
    return config


class SixViewRanker:
    """Frozen weighted-RRF six-view ranker over already authorized candidates."""

    weights = SIX_VIEW_WEIGHTS
    rrf_k = 60

    def __init__(self, encoder: DenseEncoder, *, diagnostic_ledger: bool = False, routing_policy: FusionRoutingPolicy | None = None) -> None:
        if type(diagnostic_ledger) is not bool:
            raise ValueError("diagnostic_ledger must be a bool")
        self.encoder = encoder
        self.diagnostic_ledger = diagnostic_ledger
        self.routing_policy = routing_policy
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
        effective_weights: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        """Build the benchmark-only, text-free replay ledger after ranking is frozen."""
        self._fcd1_validate_score_views(score_views)
        weights = self.weights if effective_weights is None else effective_weights
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
                    "component_ranks": {name: ranks[name][identifier] for name in weights},
                    "component_rank_receipts": [
                        {
                            "view": name,
                            "view_order_sha256": view_order_sha256[name],
                            "ranking_key_sha256": ranking_key_sha256[identifier],
                            "rank": ranks[name][identifier],
                        }
                        for name in weights
                    ],
                    "contributions": {name: weights[name] / (self.rrf_k + ranks[name][identifier]) for name in weights},
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
            if self.routing_policy is not None:
                self.routing_policy.decide(ranks={}, ranking_keys_by_id={}, rrf_k=self.rrf_k)
                raise ValueError("routing candidate universe must be non-empty")
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
        effective_weights = self.weights
        decision: FusionRoutingDecision | None = None
        raw_audit: tuple[dict[str, float], dict[str, float], list[str], list[str], dict[str, Any]] | None = None
        fixed_p5_config: dict[str, Any] | None = None
        if self.routing_policy is None:
            # Preserve the frozen default arithmetic and trace path exactly.
            totals = {identifier: sum(self.weights[name] / (self.rrf_k + ranks[name][identifier]) for name in self.weights) for identifier in ids}
            if not all(math.isfinite(score) for score in totals.values()):
                raise ValueError("weighted RRF score is non-finite")
        else:
            decision = self.routing_policy.decide(
                ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, rrf_k=self.rrf_k,
            )
            totals, effective_weights = _validated_routing_decision(
                decision, ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, rrf_k=self.rrf_k,
            )
            if isinstance(self.routing_policy, RawAnchoredP5Policy):
                raw_audit = _validate_raw_anchored_decision(
                    self.routing_policy, decision, ranks=ranks,
                    ranking_keys_by_id=ranking_keys_by_id, rrf_k=self.rrf_k,
                )
            if isinstance(self.routing_policy, FixedP5Policy):
                if decision is None:
                    raise ValueError("fixed P5 policy decision is incomplete")
                fixed_p5_config = _validate_fixed_p5_decision(
                    self.routing_policy, decision, ranks=ranks,
                    ranking_keys_by_id=ranking_keys_by_id, rrf_k=self.rrf_k,
                )
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
                "component_ranks": {name: ranks[name][identifier] for name in effective_weights},
                "contributions": {name: effective_weights[name] / (self.rrf_k + ranks[name][identifier]) for name in effective_weights},
            }
            for identifier in ordered
        ]
        trace = {
            "schema": "aerp2-product-six-view-v1",
            "encoder_identity": encoder_identity,
            "weights": dict(effective_weights), "rrf_k": self.rrf_k,
            "query_sha256": query_sha256,
            "input_sha256": input_sha256,
            "view_digests": {
                name: _digest([ranking_keys_by_id[identifier] for identifier in _ordered(scores, ranking_keys_by_id)])
                for name, scores in score_views.items()
            },
            "selected": selected,
        }
        if isinstance(self.routing_policy, RawAnchoredP5Policy):
            if decision is None or raw_audit is None or decision.anchor_ratio is None or decision.anchor_numerator is None or decision.anchor_denominator is None or decision.config_sha256 is None:
                raise ValueError("raw-anchored policy decision is incomplete")
            _raw_totals, _p5_totals, raw_top, p5_top, config = raw_audit
            trace["aerp4_raw_anchored_p5"] = {
                "schema": RawAnchoredP5Policy.schema,
                "policy": RawAnchoredP5Policy.policy,
                "config": config,
                "config_sha256": decision.config_sha256,
                "numerator": decision.anchor_numerator,
                "denominator": decision.anchor_denominator,
                "A": decision.anchor_ratio,
                "raw_top10_ranking_sha256": _digest([ranking_key_sha256[identifier] for identifier in raw_top]),
                "p5_top10_ranking_sha256": _digest([ranking_key_sha256[identifier] for identifier in p5_top]),
                "route": decision.route,
                "effective_weights": dict(effective_weights),
                "final_ranking_sha256": _digest([ranking_key_sha256[identifier] for identifier in ordered]),
            }
        if isinstance(self.routing_policy, FixedP5Policy):
            if decision is None or fixed_p5_config is None or decision.config_sha256 is None or decision.final_ranking_sha256 is None:
                raise ValueError("fixed P5 policy decision is incomplete")
            trace["aerp5_fixed_p5"] = {
                "schema": FixedP5Policy.schema,
                "policy": FixedP5Policy.policy,
                "config": fixed_p5_config,
                "config_sha256": decision.config_sha256,
                "effective_weights": dict(effective_weights),
                "final_ranking_sha256": decision.final_ranking_sha256,
            }
        if self.diagnostic_ledger:
            trace["fcd1_diagnostic_ledger"] = self._fcd1_diagnostic_ledger(
                input_sha256=input_sha256, candidates=candidates,
                ranking_key_sha256=ranking_key_sha256, score_views=score_views,
                ranks=ranks, ranking_keys_by_id=ranking_keys_by_id, totals=totals,
                ordered=ordered, group_ids=group_ids, ordered_groups=ordered_groups,
                group_scores=group_scores, effective_weights=effective_weights,
            )
        return RankingResult(ordered, totals, trace)


__all__ = [
    "AuthorizedEventRanker", "AuthorizedRetrievalCandidate", "DenseEncoder",
    "FixedP5Policy", "FixedSixViewPolicy", "FusionRoutingDecision", "FusionRoutingPolicy",
    "RankingResult", "RawAnchoredP5Policy", "SixViewRanker", "structured_observation",
]
