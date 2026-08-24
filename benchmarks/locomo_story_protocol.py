"""Strict, leak-resistant protocol primitives for the LoCoMo story track.

The public LoCoMo snapshot is not a blind test set.  This module freezes a
prospective local development/evaluation split so that subsequent candidate
work does not move conversations between partitions.  It does not claim that
the local evaluation half is an untouched benchmark test set.

Retrieval inputs are built from an allowlist.  A retriever can see only the
question, opaque IDs, raw dialog text, speakers, dates, image captions, the
official generated observation *text*, and official session summaries.
Source sample/session/dialog IDs, QA categories, answers, evidence labels, and
event summaries exist only in the physically separate scorer bundle.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union


LOCOMO10_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
SPLIT_SEED = "20260726"
CATEGORY_NAMES = {
    1: "multi-hop",
    2: "temporal",
    3: "open-domain",
    4: "single-hop",
    5: "adversarial",
}
HARD_CATEGORIES = frozenset({1, 2})
HARD_TRACK_ITEM_COUNT = 603
FINAL_K_VALUES = (1, 3, 5, 10)
DEFAULT_CANDIDATE_POOL_SIZE = 20

_TOP_LEVEL_KEYS = {
    "qa",
    "conversation",
    "event_summary",
    "observation",
    "session_summary",
    "sample_id",
}
_TURN_REQUIRED_KEYS = {"speaker", "dia_id", "text"}
_TURN_OPTIONAL_KEYS = {"img_url", "blip_caption", "query", "re-download"}
_SESSION_RE = re.compile(r"session_(\d+)")
_SESSION_DATE_RE = re.compile(r"session_(\d+)_date_time")
_OBSERVATION_KEY_RE = re.compile(r"session_(\d+)_observation")
_SUMMARY_KEY_RE = re.compile(r"session_(\d+)_summary")
_EVENT_KEY_RE = re.compile(r"events_session_(\d+)")
_DIALOG_ID_RE = re.compile(r"D(\d+):(\d+)")
_COMPOUND_PROVENANCE_RE = re.compile(r"D\d+:\d+(?:\s*,\s*D\d+:\d+)*")


@dataclass(frozen=True)
class LoadedLocomoDataset:
    """A strict-decoded dataset tied to the pinned raw-file digest."""

    records: Tuple[Dict[str, Any], ...]
    sha256: str
    source_path: str


@dataclass(frozen=True)
class FrozenConversationSplit:
    """Scorer-only record of the frozen public, non-blind local split."""

    seed: str
    policy: str
    dev_source_sample_ids: Tuple[str, ...]
    eval_source_sample_ids: Tuple[str, ...]
    source_sample_id_to_split: Dict[str, str]
    source_sample_id_to_digest: Dict[str, str]
    opaque_conversation_id_to_split: Dict[str, str]


@dataclass(frozen=True)
class RepairLedgerEntry:
    """One pinned scorer-only normalization of an upstream evidence item."""

    source_evidence_index: int
    source_evidence: str
    normalized_source_dialog_ids: Tuple[str, ...]
    code: str
    reason: str


@dataclass(frozen=True)
class OfficialExactGold:
    """Upstream evidence semantics used for published-number comparability.

    ``resolved_opaque_dialog_ids`` preserves source order and multiplicity.
    Malformed or nonexistent evidence strings remain in the denominator and
    therefore can never be retrieved, matching exact-ID upstream semantics.
    """

    resolved_opaque_dialog_ids: Tuple[str, ...]
    source_evidence_item_count: int
    unresolved_evidence_item_count: int


@dataclass(frozen=True)
class NormalizedRepairedGold:
    """Pinned, fully resolved gold used by the local causal retrieval gate."""

    gold_opaque_dialog_ids: Tuple[str, ...]
    gold_opaque_session_ids: Tuple[str, ...]
    unique_dialog_denominator: int
    unique_session_denominator: int
    unresolved_evidence_item_count: int
    repair_codes: Tuple[str, ...]


@dataclass(frozen=True)
class ScorerItem:
    """Labels and mappings that must never be passed to a retriever."""

    opaque_item_id: str
    opaque_conversation_id: str
    source_sample_id: str
    source_question_index: int
    category: int
    category_name: str
    source_answers: Dict[str, Any]
    source_evidence: Tuple[str, ...]
    official_exact: OfficialExactGold
    normalized_repaired: NormalizedRepairedGold
    repair_ledger: Tuple[RepairLedgerEntry, ...]
    corpus_opaque_dialog_ids: Tuple[str, ...]
    corpus_opaque_dialog_count: int
    corpus_opaque_session_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RetrievalBundle:
    """The complete retrieval-visible side of the protocol."""

    retrieval_items: Dict[str, Dict[str, Any]]
    item_to_conversation: Dict[str, str]
    candidate_pool_size: int


@dataclass(frozen=True)
class ScorerBundle:
    """The physically separate scorer-only side of the protocol."""

    scorer_items: Dict[str, ScorerItem]
    conversation_mappings: Dict[str, Dict[str, Any]]
    hard_item_ids: Tuple[str, ...]
    split: FrozenConversationSplit
    final_k_values: Tuple[int, ...]
    dataset_sha256: Optional[str]
    repair_ledger: Tuple[RepairLedgerEntry, ...]


@dataclass(frozen=True)
class StoryRetrievalMetrics:
    """Binary-relevance retrieval metrics at one final cutoff."""

    k: int
    hit_at_k: float
    recall_at_k: float
    all_at_k: float
    ndcg_at_k: float
    gold_count: int
    relevant_count_at_k: int
    retrieved_count_at_k: int
    ranked_ids_at_k: Tuple[str, ...]


# These repairs are valid only for the pinned raw snapshot above.  They were
# established by a read-only annotation audit, not inferred from a question or
# answer at runtime.  Any new malformed label fails instead of being guessed.
_PINNED_EVIDENCE_REPAIRS = {
    ("conv-26", 37, "D8:6; D9:17"): (
        ("D8:6", "D9:17"),
        "split_semicolon_evidence",
        "Upstream stored two complete dialog IDs in one evidence item.",
    ),
    ("conv-42", 58, "D10:19"): (
        ("D20:15",),
        "repair_nonexistent_dialog",
        "Pinned annotation audit located the recommendation at D20:15.",
    ),
    ("conv-42", 88, "D"): (
        ("D1:16",),
        "repair_truncated_dialog",
        "Pinned annotation audit located the movie recommendation at D1:16.",
    ),
    ("conv-43", 18, "D:11:26"): (
        ("D11:26",),
        "repair_extra_colon",
        "Remove the upstream extra colon in the dialog ID.",
    ),
    ("conv-47", 38, "D4:36"): (
        ("D13:3",),
        "repair_nonexistent_dialog",
        "Pinned annotation audit located the dream-job evidence at D13:3.",
    ),
    ("conv-49", 31, "D9:1 D4:4 D4:6"): (
        ("D9:1", "D4:4", "D4:6"),
        "split_whitespace_evidence",
        "Upstream stored three complete dialog IDs in one evidence item.",
    ),
    ("conv-49", 38, "D22:1 D22:2 D9:10 D9:11"): (
        ("D22:1", "D22:2", "D9:10", "D9:11"),
        "split_whitespace_evidence",
        "Upstream stored four complete dialog IDs in one evidence item.",
    ),
    ("conv-49", 46, "D21:18 D21:22 D11:15 D11:19"): (
        ("D21:18", "D21:22", "D11:15", "D11:19"),
        "split_whitespace_evidence",
        "Upstream stored four complete dialog IDs in one evidence item.",
    ),
    ("conv-50", 69, "D30:05"): (
        ("D30:5",),
        "repair_zero_padded_turn",
        "Normalize the upstream zero-padded turn number.",
    ),
}


def strict_json_loads(text: str) -> Any:
    """Decode strict JSON, rejecting duplicate keys and non-finite numbers."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON number is forbidden: {}".format(value))

    def unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key: {!r}".format(key))
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_constant,
    )


def load_official_locomo10(path: Union[str, Path]) -> LoadedLocomoDataset:
    """Load and validate the one pinned LoCoMo snapshot, byte for byte."""

    source_path = Path(path)
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != LOCOMO10_SHA256:
        raise ValueError(
            "LoCoMo dataset SHA256 mismatch: expected {}, got {}".format(LOCOMO10_SHA256, digest)
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("LoCoMo dataset must be valid UTF-8") from exc
    decoded = strict_json_loads(text)
    records = validate_locomo_dataset(decoded)
    return LoadedLocomoDataset(records=records, sha256=digest, source_path=str(source_path))


def validate_locomo_dataset(dataset: Any) -> Tuple[Dict[str, Any], ...]:
    """Validate the complete source schema without weakening unknown fields."""

    _reject_nonfinite(dataset, "dataset")
    if type(dataset) is not list or not dataset:
        raise ValueError("dataset must be a non-empty JSON array")

    seen_sample_ids = set()
    records: List[Dict[str, Any]] = []
    for sample_index, sample in enumerate(dataset):
        path = "dataset[{}]".format(sample_index)
        _require_exact_object_keys(sample, _TOP_LEVEL_KEYS, path)
        sample_id = _require_non_empty_string(sample["sample_id"], path + ".sample_id")
        if sample_id in seen_sample_ids:
            raise ValueError("duplicate sample_id: {!r}".format(sample_id))
        seen_sample_ids.add(sample_id)
        _validate_sample(sample, path)
        records.append(sample)
    return tuple(records)


def build_frozen_conversation_split(
    source_sample_ids: Iterable[str],
    *,
    source_to_opaque_conversation: Optional[Mapping[str, str]] = None,
) -> FrozenConversationSplit:
    """Apply the frozen public, non-blind prospective local split.

    Conversations are sorted by ``SHA256(b"20260726\\0" + sample_id)``.
    The first five are development conversations and the remaining five are
    local evaluation conversations.  With fewer than ten toy conversations,
    the same literal first-five rule is retained; official preparation requires
    exactly ten.
    """

    if isinstance(source_sample_ids, (str, bytes)):
        raise TypeError("source_sample_ids must be an iterable of strings")
    values = [
        _require_non_empty_string(value, "source_sample_ids[{}]".format(index))
        for index, value in enumerate(source_sample_ids)
    ]
    if not values:
        raise ValueError("source_sample_ids must not be empty")
    if len(values) != len(set(values)):
        raise ValueError("source_sample_ids must be unique")

    digests = {
        value: hashlib.sha256(
            SPLIT_SEED.encode("utf-8") + b"\0" + value.encode("utf-8")
        ).hexdigest()
        for value in values
    }
    ordered = sorted(values, key=lambda value: (digests[value], value))
    dev = tuple(ordered[:5])
    evaluation = tuple(ordered[5:])
    source_split = {value: ("dev" if value in set(dev) else "eval") for value in ordered}

    opaque_split: Dict[str, str] = {}
    if source_to_opaque_conversation is not None:
        if set(source_to_opaque_conversation) != set(values):
            raise ValueError("source_to_opaque_conversation must cover every sample exactly")
        opaque_values = list(source_to_opaque_conversation.values())
        if len(opaque_values) != len(set(opaque_values)):
            raise ValueError("opaque conversation IDs must be unique")
        opaque_split = {
            source_to_opaque_conversation[source_id]: split
            for source_id, split in source_split.items()
        }

    return FrozenConversationSplit(
        seed=SPLIT_SEED,
        policy=(
            "public_nonblind_prospective_local_split: sort by "
            "SHA256(seed + NUL + sample_id), first five dev, remaining five eval"
        ),
        dev_source_sample_ids=dev,
        eval_source_sample_ids=evaluation,
        source_sample_id_to_split=source_split,
        source_sample_id_to_digest=dict(sorted(digests.items())),
        opaque_conversation_id_to_split=dict(sorted(opaque_split.items())),
    )


def prepare_hard_story_track(
    dataset: Union[LoadedLocomoDataset, Sequence[Mapping[str, Any]]],
    *,
    candidate_pool_size: int = DEFAULT_CANDIDATE_POOL_SIZE,
    require_official_counts: bool = True,
) -> Tuple[RetrievalBundle, ScorerBundle]:
    """Build all QA retrieval items and a scorer-only hard-story partition.

    All official QA items are emitted so one frozen ranking run can support
    upstream-compatible aggregate reporting.  ``ScorerBundle.hard_item_ids``
    fixes the difficult story track to categories 1 and 2 (603 official items).
    Candidate pool size ``P`` is retrieval-side configuration; final metric
    cutoffs ``k`` remain separately frozen in the scorer bundle.
    """

    pool_size = _validate_candidate_pool_size(candidate_pool_size)
    if isinstance(dataset, LoadedLocomoDataset):
        records = dataset.records
        dataset_sha256: Optional[str] = dataset.sha256
    else:
        records = validate_locomo_dataset(list(dataset))
        dataset_sha256 = None

    if require_official_counts:
        if dataset_sha256 != LOCOMO10_SHA256:
            raise ValueError(
                "official preparation requires load_official_locomo10 and the pinned SHA256"
            )
        if len(records) != 10:
            raise ValueError("official LoCoMo track must contain exactly 10 conversations")

    source_to_opaque_conversation = {
        sample["sample_id"]: "conversation_{:06d}".format(index)
        for index, sample in enumerate(records)
    }
    split = build_frozen_conversation_split(
        (sample["sample_id"] for sample in records),
        source_to_opaque_conversation=source_to_opaque_conversation,
    )

    retrieval_items: Dict[str, Dict[str, Any]] = {}
    item_to_conversation: Dict[str, str] = {}
    scorer_items: Dict[str, ScorerItem] = {}
    conversation_mappings: Dict[str, Dict[str, Any]] = {}
    hard_item_ids: List[str] = []
    complete_ledger: List[RepairLedgerEntry] = []
    item_index = 0

    allow_pinned_repairs = dataset_sha256 == LOCOMO10_SHA256
    for sample in records:
        source_sample_id = sample["sample_id"]
        opaque_conversation_id = source_to_opaque_conversation[source_sample_id]
        sanitized_sessions, mapping = _sanitize_conversation(sample)
        conversation_mappings[opaque_conversation_id] = mapping
        corpus_dialog_ids = tuple(mapping["opaque_dialog_id_to_source_dialog_id"])
        corpus_session_ids = tuple(mapping["opaque_session_id_to_source_session_key"])

        for question_index, qa in enumerate(sample["qa"]):
            opaque_item_id = "item_{:06d}".format(item_index)
            item_index += 1
            payload = {
                "query": qa["question"],
                "sessions": sanitized_sessions,
            }
            retrieval_items[opaque_item_id] = payload
            item_to_conversation[opaque_item_id] = opaque_conversation_id

            scorer_item = _build_scorer_item(
                qa=qa,
                source_sample_id=source_sample_id,
                source_question_index=question_index,
                opaque_item_id=opaque_item_id,
                opaque_conversation_id=opaque_conversation_id,
                mapping=mapping,
                corpus_dialog_ids=corpus_dialog_ids,
                corpus_session_ids=corpus_session_ids,
                allow_pinned_repairs=allow_pinned_repairs,
            )
            scorer_items[opaque_item_id] = scorer_item
            complete_ledger.extend(scorer_item.repair_ledger)
            if scorer_item.category in HARD_CATEGORIES:
                hard_item_ids.append(opaque_item_id)

    if require_official_counts and len(hard_item_ids) != HARD_TRACK_ITEM_COUNT:
        raise ValueError(
            "official hard track must contain {} category-1/2 items, got {}".format(
                HARD_TRACK_ITEM_COUNT, len(hard_item_ids)
            )
        )
    if require_official_counts and len(complete_ledger) != len(_PINNED_EVIDENCE_REPAIRS):
        raise ValueError(
            "official repair ledger must contain exactly {} entries, got {}".format(
                len(_PINNED_EVIDENCE_REPAIRS), len(complete_ledger)
            )
        )

    retrieval = RetrievalBundle(
        retrieval_items=retrieval_items,
        item_to_conversation=item_to_conversation,
        candidate_pool_size=pool_size,
    )
    scorer = ScorerBundle(
        scorer_items=scorer_items,
        conversation_mappings=conversation_mappings,
        hard_item_ids=tuple(hard_item_ids),
        split=split,
        final_k_values=FINAL_K_VALUES,
        dataset_sha256=dataset_sha256,
        repair_ledger=tuple(complete_ledger),
    )
    return retrieval, scorer


def evaluate_story_retrieval(
    ranked_ids: Iterable[str],
    gold_ids: Iterable[str],
    *,
    final_k: int,
    candidate_pool_size: int,
    allowed_ids: Iterable[str],
) -> StoryRetrievalMetrics:
    """Compute strict Hit/Recall/All/NDCG at a frozen final cutoff."""

    if isinstance(final_k, bool) or not isinstance(final_k, int):
        raise TypeError("final_k must be an integer")
    if final_k not in FINAL_K_VALUES:
        raise ValueError("final_k must be one of {}".format(FINAL_K_VALUES))
    pool_size = _validate_candidate_pool_size(candidate_pool_size)
    if final_k > pool_size:
        raise ValueError("final_k cannot exceed candidate_pool_size")

    ranked = _strict_unique_ids(ranked_ids, "ranked_ids")
    gold = _strict_unique_ids(gold_ids, "gold_ids")
    allowed = set(_strict_unique_ids(allowed_ids, "allowed_ids"))
    if len(ranked) > pool_size:
        raise ValueError("ranked_ids exceeds candidate_pool_size")
    unknown_ranked = sorted(set(ranked) - allowed)
    if unknown_ranked:
        raise ValueError(
            "ranked_ids contains IDs outside the allowed corpus: {}".format(unknown_ranked)
        )
    unknown_gold = sorted(set(gold) - allowed)
    if unknown_gold:
        raise ValueError(
            "gold_ids contains IDs outside the allowed corpus: {}".format(unknown_gold)
        )

    top_k = ranked[:final_k]
    if not gold:
        return StoryRetrievalMetrics(
            k=final_k,
            hit_at_k=0.0,
            recall_at_k=0.0,
            all_at_k=0.0,
            ndcg_at_k=0.0,
            gold_count=0,
            relevant_count_at_k=0,
            retrieved_count_at_k=len(top_k),
            ranked_ids_at_k=tuple(top_k),
        )

    gold_set = set(gold)
    relevances = [1.0 if value in gold_set else 0.0 for value in top_k]
    relevant_count = int(sum(relevances))
    dcg = sum(relevance / math.log2(rank + 1) for rank, relevance in enumerate(relevances, start=1))
    ideal_count = min(len(gold), final_k)
    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return StoryRetrievalMetrics(
        k=final_k,
        hit_at_k=float(relevant_count > 0),
        recall_at_k=relevant_count / len(gold),
        all_at_k=float(relevant_count == len(gold)),
        ndcg_at_k=dcg / idcg,
        gold_count=len(gold),
        relevant_count_at_k=relevant_count,
        retrieved_count_at_k=len(top_k),
        ranked_ids_at_k=tuple(top_k),
    )


def evaluate_story_retrieval_at_all_k(
    ranked_ids: Iterable[str],
    gold_ids: Iterable[str],
    *,
    candidate_pool_size: int,
    allowed_ids: Iterable[str],
) -> Dict[int, StoryRetrievalMetrics]:
    """Evaluate the same frozen candidate ranking at every final cutoff."""

    ranked = tuple(ranked_ids)
    gold = tuple(gold_ids)
    allowed = tuple(allowed_ids)
    return {
        k: evaluate_story_retrieval(
            ranked,
            gold,
            final_k=k,
            candidate_pool_size=candidate_pool_size,
            allowed_ids=allowed,
        )
        for k in FINAL_K_VALUES
    }


def _validate_sample(sample: Dict[str, Any], path: str) -> None:
    conversation = sample["conversation"]
    if type(conversation) is not dict:
        raise ValueError(path + ".conversation must be an object")
    speakers = (
        _require_non_empty_string(conversation.get("speaker_a"), path + ".conversation.speaker_a"),
        _require_non_empty_string(conversation.get("speaker_b"), path + ".conversation.speaker_b"),
    )
    if speakers[0] == speakers[1]:
        raise ValueError(path + ".conversation speakers must be distinct")

    session_numbers = []
    date_numbers = set()
    for key, value in conversation.items():
        session_match = _SESSION_RE.fullmatch(key)
        date_match = _SESSION_DATE_RE.fullmatch(key)
        if key in {"speaker_a", "speaker_b"}:
            continue
        if session_match:
            session_number = int(session_match.group(1))
            session_numbers.append(session_number)
            _validate_dialogs(value, session_number, speakers, path + ".conversation." + key)
        elif date_match:
            date_numbers.add(int(date_match.group(1)))
            _require_non_empty_string(value, path + ".conversation." + key)
        else:
            raise ValueError(path + ".conversation has unknown key {!r}".format(key))

    if not session_numbers:
        raise ValueError(path + ".conversation must contain at least one session")
    ordered_sessions = sorted(session_numbers)
    if ordered_sessions != list(range(1, max(ordered_sessions) + 1)):
        raise ValueError(path + ".conversation session numbers must be contiguous from one")
    if not set(session_numbers).issubset(date_numbers):
        raise ValueError(path + ".conversation is missing a date for a session")

    _validate_observations(
        sample["observation"], conversation, ordered_sessions, speakers, path + ".observation"
    )
    _validate_session_summaries(
        sample["session_summary"], ordered_sessions, path + ".session_summary"
    )
    _validate_event_summaries(
        sample["event_summary"], ordered_sessions, speakers, path + ".event_summary"
    )
    _validate_qa(sample["qa"], path + ".qa")


def _validate_dialogs(
    dialogs: Any,
    session_number: int,
    speakers: Tuple[str, str],
    path: str,
) -> None:
    if type(dialogs) is not list or not dialogs:
        raise ValueError(path + " must be a non-empty array")
    for turn_index, dialog in enumerate(dialogs, start=1):
        turn_path = "{}[{}]".format(path, turn_index - 1)
        if type(dialog) is not dict:
            raise ValueError(turn_path + " must be an object")
        keys = set(dialog)
        missing = _TURN_REQUIRED_KEYS - keys
        unknown = keys - _TURN_REQUIRED_KEYS - _TURN_OPTIONAL_KEYS
        if missing or unknown:
            raise ValueError(
                "{} schema mismatch; missing={}, unknown={}".format(
                    turn_path, sorted(missing), sorted(unknown)
                )
            )
        speaker = _require_non_empty_string(dialog["speaker"], turn_path + ".speaker")
        if speaker not in speakers:
            raise ValueError(turn_path + ".speaker is not a conversation speaker")
        expected_id = "D{}:{}".format(session_number, turn_index)
        dialog_id = _require_non_empty_string(dialog["dia_id"], turn_path + ".dia_id")
        if dialog_id != expected_id:
            raise ValueError(
                "{} must be {!r}, got {!r}".format(turn_path + ".dia_id", expected_id, dialog_id)
            )
        _require_non_empty_string(dialog["text"], turn_path + ".text")
        if "blip_caption" in dialog:
            _require_non_empty_string(dialog["blip_caption"], turn_path + ".blip_caption")
        if "query" in dialog:
            _require_non_empty_string(dialog["query"], turn_path + ".query")
        if "img_url" in dialog:
            urls = dialog["img_url"]
            if type(urls) is not list or not urls:
                raise ValueError(turn_path + ".img_url must be a non-empty array")
            for url_index, url in enumerate(urls):
                _require_non_empty_string(url, "{}.img_url[{}]".format(turn_path, url_index))
        if "re-download" in dialog and type(dialog["re-download"]) is not bool:
            raise ValueError(turn_path + ".re-download must be a boolean")


def _validate_observations(
    observations: Any,
    conversation: Dict[str, Any],
    session_numbers: Sequence[int],
    speakers: Tuple[str, str],
    path: str,
) -> None:
    if type(observations) is not dict:
        raise ValueError(path + " must be an object")
    expected_keys = {"session_{}_observation".format(number) for number in session_numbers}
    if set(observations) != expected_keys:
        raise ValueError(path + " must cover every actual session exactly")
    for key, value in observations.items():
        match = _OBSERVATION_KEY_RE.fullmatch(key)
        if match is None:
            raise ValueError(path + " has invalid key {!r}".format(key))
        session_number = int(match.group(1))
        if type(value) is not dict or set(value) != set(speakers):
            raise ValueError(path + "." + key + " must contain both speakers exactly")
        source_dialog_ids = {
            dialog["dia_id"] for dialog in conversation["session_{}".format(session_number)]
        }
        for speaker, rows in value.items():
            if type(rows) is not list:
                raise ValueError(path + "." + key + "." + speaker + " must be an array")
            for row_index, row in enumerate(rows):
                row_path = "{}.{}.{}[{}]".format(path, key, speaker, row_index)
                if type(row) is not list or len(row) != 2:
                    raise ValueError(row_path + " must be [observation_text, provenance]")
                _require_non_empty_string(row[0], row_path + "[0]")
                provenance_ids = _parse_observation_provenance(row[1], row_path + "[1]")
                unknown = sorted(set(provenance_ids) - source_dialog_ids)
                if unknown:
                    raise ValueError(
                        row_path + " references unknown dialog IDs: {}".format(unknown)
                    )


def _validate_session_summaries(summaries: Any, session_numbers: Sequence[int], path: str) -> None:
    if type(summaries) is not dict:
        raise ValueError(path + " must be an object")
    expected_keys = {"session_{}_summary".format(number) for number in session_numbers}
    if set(summaries) != expected_keys:
        raise ValueError(path + " must cover every actual session exactly")
    for key, value in summaries.items():
        if _SUMMARY_KEY_RE.fullmatch(key) is None:
            raise ValueError(path + " has invalid key {!r}".format(key))
        _require_non_empty_string(value, path + "." + key)


def _validate_event_summaries(
    events: Any,
    session_numbers: Sequence[int],
    speakers: Tuple[str, str],
    path: str,
) -> None:
    if type(events) is not dict:
        raise ValueError(path + " must be an object")
    expected_keys = {"events_session_{}".format(number) for number in session_numbers}
    if set(events) != expected_keys:
        raise ValueError(path + " must cover every actual session exactly")
    for key, value in events.items():
        if _EVENT_KEY_RE.fullmatch(key) is None or type(value) is not dict:
            raise ValueError(path + "." + key + " has invalid schema")
        if set(value) != {speakers[0], speakers[1], "date"}:
            raise ValueError(path + "." + key + " must contain both speakers and date")
        _require_non_empty_string(value["date"], path + "." + key + ".date")
        for speaker in speakers:
            rows = value[speaker]
            if type(rows) is not list:
                raise ValueError(path + "." + key + "." + speaker + " must be an array")
            for row_index, row in enumerate(rows):
                if not isinstance(row, str):
                    raise ValueError(
                        "{}.{}.{}[{}] must be a string".format(path, key, speaker, row_index)
                    )


def _validate_qa(qa_items: Any, path: str) -> None:
    if type(qa_items) is not list or not qa_items:
        raise ValueError(path + " must be a non-empty array")
    for index, qa in enumerate(qa_items):
        item_path = "{}[{}]".format(path, index)
        if type(qa) is not dict:
            raise ValueError(item_path + " must be an object")
        required = {"question", "evidence", "category"}
        allowed = required | {"answer", "adversarial_answer"}
        missing = required - set(qa)
        unknown = set(qa) - allowed
        if missing or unknown:
            raise ValueError(
                "{} schema mismatch; missing={}, unknown={}".format(
                    item_path, sorted(missing), sorted(unknown)
                )
            )
        _require_non_empty_string(qa["question"], item_path + ".question")
        category = qa["category"]
        if type(category) is not int or category not in CATEGORY_NAMES:
            raise ValueError(item_path + ".category must be an integer from 1 through 5")
        if category == 5:
            if "adversarial_answer" not in qa:
                raise ValueError(item_path + " category 5 requires adversarial_answer")
        elif "answer" not in qa or "adversarial_answer" in qa:
            raise ValueError(item_path + " category 1-4 requires only answer")
        for answer_key in ("answer", "adversarial_answer"):
            if answer_key in qa:
                answer = qa[answer_key]
                if type(answer) is str:
                    _require_non_empty_string(answer, item_path + "." + answer_key)
                elif type(answer) is not int:
                    raise ValueError(item_path + "." + answer_key + " must be a string or integer")
        evidence = qa["evidence"]
        if type(evidence) is not list:
            raise ValueError(item_path + ".evidence must be an array")
        for evidence_index, value in enumerate(evidence):
            _require_non_empty_string(value, "{}.evidence[{}]".format(item_path, evidence_index))


def _sanitize_conversation(
    sample: Mapping[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    conversation = sample["conversation"]
    session_numbers = sorted(
        int(match.group(1))
        for key in conversation
        for match in [_SESSION_RE.fullmatch(key)]
        if match is not None
    )
    source_session_to_opaque: Dict[str, str] = {}
    opaque_session_to_source: Dict[str, str] = {}
    source_dialog_to_opaque: Dict[str, str] = {}
    opaque_dialog_to_source: Dict[str, str] = {}
    source_dialog_to_opaque_session: Dict[str, str] = {}
    sanitized_sessions: List[Dict[str, Any]] = []

    next_dialog_index = 0
    for session_index, session_number in enumerate(session_numbers):
        source_session_key = "session_{}".format(session_number)
        opaque_session_id = "session_{:06d}".format(session_index)
        source_session_to_opaque[source_session_key] = opaque_session_id
        opaque_session_to_source[opaque_session_id] = source_session_key
        source_dialogs = conversation[source_session_key]
        for dialog in source_dialogs:
            opaque_dialog_id = "dialog_{:06d}".format(next_dialog_index)
            next_dialog_index += 1
            source_dialog_to_opaque[dialog["dia_id"]] = opaque_dialog_id
            opaque_dialog_to_source[opaque_dialog_id] = dialog["dia_id"]
            source_dialog_to_opaque_session[dialog["dia_id"]] = opaque_session_id

    for session_number in session_numbers:
        source_session_key = "session_{}".format(session_number)
        opaque_session_id = source_session_to_opaque[source_session_key]
        observation_texts: Dict[str, List[str]] = {
            dialog["dia_id"]: [] for dialog in conversation[source_session_key]
        }
        source_observations = sample["observation"]["session_{}_observation".format(session_number)]
        for rows in source_observations.values():
            for observation_text, provenance in rows:
                for source_dialog_id in _parse_observation_provenance(
                    provenance, "observation provenance"
                ):
                    observation_texts[source_dialog_id].append(observation_text)

        dialogs = []
        date = conversation["session_{}_date_time".format(session_number)]
        for dialog in conversation[source_session_key]:
            dialogs.append(
                {
                    "opaque_dialog_id": source_dialog_to_opaque[dialog["dia_id"]],
                    "text": dialog["text"],
                    "speaker": dialog["speaker"],
                    "date": date,
                    "caption": dialog.get("blip_caption", ""),
                    "observations": list(observation_texts[dialog["dia_id"]]),
                }
            )
        sanitized_sessions.append(
            {
                "opaque_session_id": opaque_session_id,
                "session_summary": sample["session_summary"][
                    "session_{}_summary".format(session_number)
                ],
                "dialogs": dialogs,
            }
        )

    mapping = {
        "source_sample_id": sample["sample_id"],
        "source_session_key_to_opaque_session_id": source_session_to_opaque,
        "opaque_session_id_to_source_session_key": opaque_session_to_source,
        "source_dialog_id_to_opaque_dialog_id": source_dialog_to_opaque,
        "opaque_dialog_id_to_source_dialog_id": opaque_dialog_to_source,
        "source_dialog_id_to_opaque_session_id": source_dialog_to_opaque_session,
    }
    return sanitized_sessions, mapping


def _build_scorer_item(
    *,
    qa: Mapping[str, Any],
    source_sample_id: str,
    source_question_index: int,
    opaque_item_id: str,
    opaque_conversation_id: str,
    mapping: Mapping[str, Any],
    corpus_dialog_ids: Tuple[str, ...],
    corpus_session_ids: Tuple[str, ...],
    allow_pinned_repairs: bool,
) -> ScorerItem:
    source_dialog_to_opaque = mapping["source_dialog_id_to_opaque_dialog_id"]
    source_dialog_to_session = mapping["source_dialog_id_to_opaque_session_id"]
    source_evidence = tuple(qa["evidence"])
    official_resolved = tuple(
        source_dialog_to_opaque[value]
        for value in source_evidence
        if value in source_dialog_to_opaque
    )
    official_exact = OfficialExactGold(
        resolved_opaque_dialog_ids=official_resolved,
        source_evidence_item_count=len(source_evidence),
        unresolved_evidence_item_count=len(source_evidence) - len(official_resolved),
    )

    normalized_source_dialog_ids: List[str] = []
    ledger: List[RepairLedgerEntry] = []
    for evidence_index, source_value in enumerate(source_evidence):
        if source_value in source_dialog_to_opaque:
            replacements = (source_value,)
        else:
            repair_key = (source_sample_id, source_question_index, source_value)
            if not allow_pinned_repairs or repair_key not in _PINNED_EVIDENCE_REPAIRS:
                raise ValueError(
                    "unrecognized evidence anomaly at sample {!r}, QA {}, evidence {}: {!r}".format(
                        source_sample_id, source_question_index, evidence_index, source_value
                    )
                )
            replacements, code, reason = _PINNED_EVIDENCE_REPAIRS[repair_key]
            ledger.append(
                RepairLedgerEntry(
                    source_evidence_index=evidence_index,
                    source_evidence=source_value,
                    normalized_source_dialog_ids=replacements,
                    code=code,
                    reason=reason,
                )
            )
        unknown = [value for value in replacements if value not in source_dialog_to_opaque]
        if unknown:
            raise ValueError(
                "pinned evidence repair targets unknown corpus dialogs: {}".format(unknown)
            )
        normalized_source_dialog_ids.extend(replacements)

    unique_source_dialog_ids = _deduplicate(normalized_source_dialog_ids)
    gold_dialog_ids = tuple(source_dialog_to_opaque[value] for value in unique_source_dialog_ids)
    gold_session_ids = tuple(
        _deduplicate(source_dialog_to_session[value] for value in unique_source_dialog_ids)
    )
    normalized = NormalizedRepairedGold(
        gold_opaque_dialog_ids=gold_dialog_ids,
        gold_opaque_session_ids=gold_session_ids,
        unique_dialog_denominator=len(gold_dialog_ids),
        unique_session_denominator=len(gold_session_ids),
        unresolved_evidence_item_count=0,
        repair_codes=tuple(entry.code for entry in ledger),
    )
    source_answers = {key: qa[key] for key in ("answer", "adversarial_answer") if key in qa}
    return ScorerItem(
        opaque_item_id=opaque_item_id,
        opaque_conversation_id=opaque_conversation_id,
        source_sample_id=source_sample_id,
        source_question_index=source_question_index,
        category=qa["category"],
        category_name=CATEGORY_NAMES[qa["category"]],
        source_answers=source_answers,
        source_evidence=source_evidence,
        official_exact=official_exact,
        normalized_repaired=normalized,
        repair_ledger=tuple(ledger),
        corpus_opaque_dialog_ids=corpus_dialog_ids,
        corpus_opaque_dialog_count=len(corpus_dialog_ids),
        corpus_opaque_session_ids=corpus_session_ids,
    )


def _parse_observation_provenance(value: Any, path: str) -> Tuple[str, ...]:
    if type(value) is str:
        _require_non_empty_string(value, path)
        if _COMPOUND_PROVENANCE_RE.fullmatch(value) is None:
            raise ValueError(path + " must contain complete comma-separated dialog IDs")
        values = tuple(part.strip() for part in value.split(","))
    elif type(value) is list and value:
        values = tuple(
            _require_non_empty_string(item, "{}[{}]".format(path, index))
            for index, item in enumerate(value)
        )
        if any(_DIALOG_ID_RE.fullmatch(item) is None for item in values):
            raise ValueError(path + " list items must be complete dialog IDs")
    else:
        raise ValueError(path + " must be a dialog ID or non-empty dialog-ID array")
    if len(values) != len(set(values)):
        raise ValueError(path + " contains duplicate dialog IDs")
    return values


def _validate_candidate_pool_size(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("candidate_pool_size must be an integer")
    if value < max(FINAL_K_VALUES):
        raise ValueError(
            "candidate_pool_size must be at least the largest final k ({})".format(
                max(FINAL_K_VALUES)
            )
        )
    return value


def _strict_unique_ids(values: Iterable[str], name: str) -> List[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError(name + " must be an iterable of IDs")
    result = [
        _require_non_empty_string(value, "{}[{}]".format(name, index))
        for index, value in enumerate(values)
    ]
    if len(result) != len(set(result)):
        raise ValueError(name + " must not contain duplicate IDs")
    return result


def _deduplicate(values: Iterable[str]) -> List[str]:
    result = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _require_exact_object_keys(value: Any, expected: set, path: str) -> None:
    if type(value) is not dict:
        raise ValueError(path + " must be an object")
    missing = expected - set(value)
    unknown = set(value) - expected
    if missing or unknown:
        raise ValueError(
            "{} schema mismatch; missing={}, unknown={}".format(
                path, sorted(missing), sorted(unknown)
            )
        )


def _require_non_empty_string(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(path + " must be a non-empty string")
    return value


def _reject_nonfinite(value: Any, path: str) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(path + " contains a non-finite number")
    if type(value) is list:
        for index, item in enumerate(value):
            _reject_nonfinite(item, "{}[{}]".format(path, index))
    elif type(value) is dict:
        for key, item in value.items():
            _reject_nonfinite(item, "{}.{}".format(path, key))
