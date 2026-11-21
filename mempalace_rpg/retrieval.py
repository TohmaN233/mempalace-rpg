"""Types and lexical scoring shared by the gameplay retrieval profiles."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
import re
from typing import Any, Protocol, Sequence


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


class DenseEncoder(Protocol):
    """Split encoding contract; query and passage prefixes stay separate."""

    identity: str

    def encode_passages(self, texts: Sequence[str]) -> Sequence[Sequence[float]]: ...

    def encode_query(self, query: str) -> Sequence[float]: ...


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
