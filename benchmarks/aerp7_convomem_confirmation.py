"""Fail-closed ConvoMem AERP-7 prelabel custody and candidate projection.

Only ``build_prelabel_bundle`` and this module's CLI open official roots.
Candidate callers use ``load_candidate_projection`` on a READY-published bundle.
This is prelabel plumbing only: evidence spans are deliberately not mapped/scored.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence


SCHEMA = "aerp7-convomem-candidate-projection-v3"
CUSTODY_SCHEMA = "aerp7-convomem-sealed-custody-v3"
CANDIDATE_READY_SCHEMA = "aerp7-convomem-candidate-ready-v3"
CUSTODY_READY_SCHEMA = "aerp7-convomem-custody-ready-v3"
CANDIDATE_PROJECTION_REFERENCE_SCHEMA = "aerp7-convomem-candidate-projection-reference-v1"
CENSUS_SELECTION_REFERENCE_SCHEMA = "aerp7-convomem-census-selection-reference-v1"
CUSTODY_REFERENCE_SCHEMA = "aerp7-convomem-custody-reference-v1"
SELECTION_ALGORITHM = "hmac-sha256-revision-bound-persona-group-tier-context-v1"
CENSUS_SELECTION_ALGORITHM = "aerp7-convomem-census-observed-pairs-v2"
CENSUS_CROSSWALK_SEMANTICS = "all_embedded_evidence_key_to_case_pairs_v1"
BOUND_CUSTODY_ALGORITHM = "hmac-sha256-revision-bound-custody-binding-v1"
PREMIX_EXACT_DUPLICATE_NORMALIZATION = {
    "schema": "aerp7-premix-exact-outer-conversation-normalization-v1",
    "rule": "retain_first_outer_row_per_case_conversation_id_when_canonical_content_sha256_matches",
}
PREMIX_EXACT_EMPTY_MESSAGE_TEXT_NORMALIZATION = {
    "schema": "aerp7-premix-exact-empty-message-text-normalization-v1",
    "rule": "discard_exact_empty_string_message_text_after_validating_message_and_speaker",
}
SQLITE_INDEX_EXPANSION_FACTOR = 3
STAGING_HEADROOM_BYTES = 8 * 1024 * 1024 * 1024
_HEX = set("0123456789abcdef")
_WINDOWS_POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
# ``speaker`` is deliberately candidate-visible in v3: it is an input to the
# frozen current-method observation serializer.  Everything which can reveal a
# label, a source locator, or an endpoint assignment remains capability-sealed.
_FORBIDDEN = frozenset({"answer", "message_evidences", "abstention", "category", "group", "tier", "contextsize", "context_size", "evidence_count", "rubric", "split", "source_path", "source_locator", "ordinal", "evidenceitems", "evidence_items", "canonical_item_id", "containsevidence", "contains_evidence", "model_name", "scenario_description", "conversation_id"})


class CustodyError(ValueError):
    def __init__(self, code: str, **receipt: Any) -> None:
        self.receipt = {"schema": "aerp7-convomem-custody-error-v1", "code": code, **receipt}
        super().__init__(json.dumps(self.receipt, sort_keys=True, separators=(",", ":")))


class CrosswalkError(CustodyError):
    pass


@dataclass
class StreamingIndex:
    """Externally staged, disk-backed official-source index; caller owns cleanup."""

    directory: Path
    database: Path
    canonical_digest: str
    premix_digest: str
    revision: str
    source_receipt: list[dict[str, Any]]
    directory_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    owned_files: dict[Path, tuple[int, int]]
    staging_receipt: dict[str, Any]
    database_identity: tuple[int, int]
    database_sha256: str

    def close(self, *, failure_cleanup: bool = False) -> None:
        """Identity-owned temp cleanup; staging is never placed in repo/output paths."""
        directory = self.directory
        if failure_cleanup:
            renamed = _claim_cleanup_tombstone(directory, self.directory_identity, self.parent_identity, ".aerp7-cleaning-")
            if renamed is None:
                raise CustodyError("streaming_index_cleanup_tombstone_rename_failed")
            directory = renamed
        if _directory_identity(directory, "streaming_index_cleanup_drift") != self.directory_identity:
            raise CustodyError("streaming_index_cleanup_drift")
        marker = directory / ".aerp7-owned"
        remapped = {directory / path.relative_to(self.directory): identity for path, identity in self.owned_files.items()}
        marker_identity = remapped.get(marker)
        if marker_identity is None:
            raise CustodyError("streaming_index_marker_missing")
        ordinary = {path: identity for path, identity in remapped.items() if path != marker}
        for path, identity in ordinary.items():
            try:
                current = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise CustodyError("streaming_index_cleanup_stat_failed") from exc
            if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
                raise CustodyError("streaming_index_cleanup_identity_drift")
        for path in ordinary:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise CustodyError("streaming_index_cleanup_unlink_failed") from exc
        try:
            remaining = {entry.name for entry in directory.iterdir()}
        except OSError as exc:
            raise CustodyError("streaming_index_cleanup_scan_failed") from exc
        if remaining != {marker.name}:
            raise CustodyError("streaming_index_cleanup_unknown_entry")
        try:
            current = os.lstat(marker)
        except OSError as exc:
            raise CustodyError("streaming_index_marker_missing") from exc
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != marker_identity:
            raise CustodyError("streaming_index_marker_identity_drift")
        try:
            marker.unlink()
            directory.rmdir()
        except OSError as exc:
            raise CustodyError("streaming_index_cleanup_rmdir_failed") from exc


@dataclass(frozen=True)
class CensusSelectionReference:
    """Small handle for the frozen SQL-selected census pairs.

    The selected-pairs relation remains in the owned staging database.  Callers
    stream it in ordinal order and never receive a census-sized Python list.
    """

    database: Path
    item_count: int
    schema: str = CENSUS_SELECTION_REFERENCE_SCHEMA


@dataclass(frozen=True)
class SelectionConfig:
    """Frozen selection; there are intentionally no defaults or inferred quotas."""

    seed: int | None
    persona_quota: int | str
    per_persona_group_quota: int | str
    context_rank_indices: tuple[int, ...] | str

    @classmethod
    def census_v1(cls) -> "SelectionConfig":
        """The sole formal selector: every valid candidate-visible unit, no RNG."""
        return cls(None, "ALL", "ALL", "ALL_AVAILABLE_SORTED")

    @property
    def is_census_v1(self) -> bool:
        return (
            self.seed is None
            and self.persona_quota == "ALL"
            and self.per_persona_group_quota == "ALL"
            and self.context_rank_indices == "ALL_AVAILABLE_SORTED"
        )

    def validate(self) -> None:
        if self.is_census_v1:
            return
        # Sentinel values are deliberately all-or-nothing: a partial census is
        # a hidden sample, not a conservative formal selection.
        if any(value in {None, "ALL", "ALL_AVAILABLE_SORTED"} for value in (self.seed, self.persona_quota, self.per_persona_group_quota, self.context_rank_indices)):
            raise CustodyError("invalid_selection_config", field="census_sentinel_mixed")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise CustodyError("invalid_selection_seed")
        for name, value in (("persona_quota", self.persona_quota), ("per_persona_group_quota", self.per_persona_group_quota)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise CustodyError("invalid_selection_config", field=name)
        if not isinstance(self.context_rank_indices, tuple) or not self.context_rank_indices:
            raise CustodyError("invalid_selection_config", field="context_rank_indices")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in self.context_rank_indices) or len(set(self.context_rank_indices)) != len(self.context_rank_indices):
            raise CustodyError("invalid_selection_config", field="context_rank_indices")


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _binding_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or len(secret) < 16:
        raise CustodyError("binding_secret_invalid")
    return secret


def _linux_renameat2_available() -> bool:
    if os.name != "posix" or not sys.platform.startswith("linux"):
        return False
    try:
        return hasattr(ctypes.CDLL(None, use_errno=True), "renameat2")
    except OSError:
        return False


def _require_builder_platform() -> dict[str, str]:
    """Builder contract: Windows or Linux with atomic no-replace directory rename.

    Candidate/custody loaders remain content-only and may run more broadly; only
    source ingestion, staging and publication need this OS guarantee.
    """
    if os.name == "nt":
        return {"builder_platform": "windows", "tombstone_rename": "MoveFileExW-no-replace-v1"}
    if _linux_renameat2_available():
        return {"builder_platform": "linux", "tombstone_rename": "renameat2-noreplace-v1"}
    raise CustodyError("builder_platform_unsupported", os_name=os.name, platform=sys.platform, required="windows_or_linux_renameat2")


def _custody_item_commitment(secret: bytes, revision: str, item: Mapping[str, Any], projection_item: Mapping[str, Any], corpus: Mapping[str, Any]) -> str:
    return _opaque(_binding_secret(secret), revision, "custody-item-binding", {"sealed_item": item, "projection_item": projection_item, "projection_corpus": corpus})


def _custody_commitment(secret: bytes, revision: str, custody_without_binding: Mapping[str, Any], projection: Mapping[str, Any]) -> str:
    return _opaque(_binding_secret(secret), revision, "custody-binding", {"sealed_custody": custody_without_binding, "projection": projection})


def _object(value: Any, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return value


def _list(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list):
        raise CustodyError(code)
    return value


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise CustodyError(code)
    return value


def _token(value: Any, code: str) -> str:
    value = _text(value, code)
    if len(value) != 64 or set(value) - _HEX:
        raise CustodyError(code)
    return value


def _opaque(secret: bytes, revision: str, domain: str, identity: Any) -> str:
    return hmac.new(secret, _bytes({"revision": revision, "domain": domain, "identity": identity}), hashlib.sha256).hexdigest()


def _rank(secret: bytes, revision: str, seed: int, domain: str, identity: Any) -> str:
    return hmac.new(secret, _bytes({"revision": revision, "seed": seed, "domain": domain, "identity": identity}), hashlib.sha256).hexdigest()


def _safe_existing_ancestors(path: Path, code: str) -> None:
    """Reject any reparse-point path component without resolving it away."""
    absolute = path.absolute()
    chain = [absolute, *absolute.parents]
    for candidate in reversed(chain):
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise CustodyError(code) from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise CustodyError(code)


def _subroot(root: Path, name: str) -> Path:
    # Test symlinkness before resolve; resolving first hides a symlinked input root.
    _safe_existing_ancestors(root, "official_root_invalid")
    if root.is_symlink() or not root.exists() or not root.is_dir():
        raise CustodyError("official_root_invalid")
    resolved = root.resolve(strict=True)
    base = resolved if resolved.name == "core_benchmark" else resolved / "core_benchmark"
    target = base / name
    _safe_existing_ancestors(target, "official_root_layout_invalid")
    if target.is_symlink() or not target.is_dir():
        raise CustodyError("official_root_layout_invalid", required=name)
    return target


def _files(root: Path) -> list[Path]:
    _directory_identity(root, "official_root_layout_invalid")
    found = []
    for path in root.rglob("*"):
        try:
            metadata = os.lstat(path)
        except OSError as exc:
            raise CustodyError("official_tree_invalid") from exc
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise CustodyError("official_tree_reparse")
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if path.suffix != ".json" or ({"filler_conversations", "legacy_benchmarks"} & set(path.relative_to(root).parts)):
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink < 1 or _is_reparse(metadata):
            raise CustodyError("official_json_symlink")
        # Every directory ancestor from the official subroot to this file must
        # be a real directory; rglob alone is not an authorization check.
        ancestor = root
        for component in path.relative_to(root).parts[:-1]:
            ancestor = ancestor / component
            ancestor_metadata = os.lstat(ancestor)
            if not stat.S_ISDIR(ancestor_metadata.st_mode) or stat.S_ISLNK(ancestor_metadata.st_mode) or _is_reparse(ancestor_metadata):
                raise CustodyError("official_tree_reparse")
        found.append(path)
    found.sort()
    if not found:
        raise CustodyError("official_json_missing")
    return found


def _json(path: Path, code: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError(code) from exc


def _conversation_ids(value: Any, code: str) -> tuple[str, ...]:
    rows = _list(value, code)
    ids = tuple(_text(_object(row, code).get("id"), code) for row in rows)
    if not ids or len(set(ids)) != len(ids):
        raise CustodyError(code)
    return ids


def _labels(item: Mapping[str, Any], code: str) -> tuple[str, str, tuple[str, ...], list[dict[str, str]]]:
    answer = _text(item.get("answer"), code)
    category = _text(item.get("category"), code)
    conversation_ids = _conversation_ids(item.get("conversations"), code)
    evidences = _list(item.get("message_evidences"), code)
    parsed = []
    for evidence in evidences:
        row = _object(evidence, code)
        # Exact known label shape; extra official fields remain sealed but irrelevant.
        parsed.append({"speaker": _text(row.get("speaker"), code), "text": _text(row.get("text"), code)})
    return answer, category, conversation_ids, parsed


def _crosswalk_fields(item: Mapping[str, Any], code: str) -> tuple[str, str, tuple[str, ...]]:
    """Premix embedded items carry join fields, not official evidence labels."""
    return _text(item.get("answer"), code), _text(item.get("category"), code), _conversation_ids(item.get("conversations"), code)


def _normalized_premix_outer_rows(outer: Any, *, locator: str, case_ordinal: int) -> tuple[list[tuple[str, Mapping[str, Any], int]], list[dict[str, Any]]]:
    """Keep first exact duplicate rows, retaining each row's original ordinal."""
    rows: list[tuple[str, Mapping[str, Any], int]] = []
    seen: dict[str, tuple[str, int]] = {}
    normalized: list[dict[str, Any]] = []
    for outer_ordinal, conversation in enumerate(_list(outer, "premix_conversations_invalid")):
        row = _object(conversation, "premix_conversation_invalid")
        conversation_id = _text(row.get("id"), "premix_conversation_id_invalid")
        signature = canonical_sha256(row)
        prior = seen.get(conversation_id)
        if prior is None:
            seen[conversation_id] = (signature, outer_ordinal)
            rows.append((conversation_id, row, outer_ordinal))
            continue
        prior_signature, retained_outer_ordinal = prior
        conversation_id_sha256 = canonical_sha256(conversation_id)
        if prior_signature != signature:
            raise CrosswalkError(
                "premix_outer_conversation_content_conflict",
                locator=locator,
                case_ordinal=case_ordinal,
                conversation_id_sha256=conversation_id_sha256,
                retained_outer_ordinal=retained_outer_ordinal,
                conflicting_outer_ordinal=outer_ordinal,
            )
        normalized.append({
            "conversation_id_sha256": conversation_id_sha256,
            "content_sha256": signature,
            "retained_outer_ordinal": retained_outer_ordinal,
            "duplicate_outer_ordinal": outer_ordinal,
        })
    return rows, normalized


def _normalized_premix_message_rows(
    messages: Any, *, locator: str, case_ordinal: int, conversation_id: str, conversation_ordinal: int,
) -> tuple[list[tuple[str, str, int]], list[dict[str, Any]]]:
    """Discard only official empty-string placeholders; retain raw message ordinals."""
    rows: list[tuple[str, str, int]] = []
    normalized: list[dict[str, Any]] = []
    conversation_id_sha256 = canonical_sha256(conversation_id)
    raw_messages = _list(messages, "premix_messages_invalid")
    for raw_message_ordinal, raw_message in enumerate(raw_messages):
        message = _object(raw_message, "premix_message_invalid")
        speaker = _text(message.get("speaker"), "premix_message_speaker_invalid")
        text = message.get("text")
        if text == "":
            normalized.append({
                "conversation_id_sha256": conversation_id_sha256,
                "retained_outer_ordinal": conversation_ordinal,
                "raw_message_ordinal": raw_message_ordinal,
                "speaker_sha256": canonical_sha256(speaker),
                "text_sha256": canonical_sha256(text),
            })
            continue
        if not isinstance(text, str) or not text:
            raise CustodyError(
                "premix_message_text_invalid",
                locator=locator,
                case_ordinal=case_ordinal,
                conversation_id_sha256=conversation_id_sha256,
                conversation_ordinal=conversation_ordinal,
                message_ordinal=raw_message_ordinal,
                text_type=type(text).__name__,
            )
        rows.append((speaker, text, raw_message_ordinal))
    if raw_messages and not rows:
        raise CustodyError(
            "premix_conversation_has_no_retrievable_messages",
            locator=locator,
            case_ordinal=case_ordinal,
            conversation_id_sha256=conversation_id_sha256,
            conversation_ordinal=conversation_ordinal,
        )
    return rows, normalized


def _directory(relative: Path) -> dict[str, str | None]:
    parts = relative.parts[:-1]
    # Do not infer abstention: group-to-abstention mapping is not frozen for this slice.
    return {"group": parts[0] if parts else None, "tier": parts[-1] if len(parts) > 1 else None, "record_category": None}


def _canonical(files: Sequence[dict[str, Any]], secret: bytes, revision: str) -> list[dict[str, Any]]:
    result = []
    for source_file in files:
        source = _object(_decode(source_file["raw"], "canonical_json_invalid"), "canonical_root_must_be_object")
        for ordinal, raw in enumerate(_list(source.get("evidence_items"), "canonical_evidence_items_invalid")):
            item = _object(raw, "canonical_evidence_item_invalid")
            persona = _text(item.get("personId"), "canonical_person_id_invalid")
            question = _text(item.get("question"), "canonical_question_invalid")
            answer, category, conversation_ids, evidence_labels = _labels(item, "canonical_label_schema_invalid")
            locator = {"path": source_file["locator"], "ordinal": ordinal}
            directory = _directory(Path(source_file["locator"]))
            directory["record_category"] = category
            result.append({"canonical_item_id": _opaque(secret, revision, "canonical-item", locator), "persona_id": _opaque(secret, revision, "persona", persona), "persona_source_id": persona, "question": question, "answer": answer, "category": category, "conversation_ids": conversation_ids, "source_locator": locator, "directory": directory, "labels": {"answer": answer, "message_evidences": evidence_labels}})
    if not result:
        raise CustodyError("canonical_evidence_items_empty")
    return result


def _premix(files: Sequence[dict[str, Any]], secret: bytes, revision: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cases: list[dict[str, Any]] = []; excluded = {"multi_persona_cases": 0}
    conversations_seen: dict[str, str] = {}
    for source_file in files:
        for case_ordinal, raw_case in enumerate(_list(_decode(source_file["raw"], "premix_json_invalid"), "premix_root_must_be_array")):
            case = _object(raw_case, "premix_case_invalid")
            locator = {"path": source_file["locator"], "case_ordinal": case_ordinal}
            outer_rows, _normalization = _normalized_premix_outer_rows(
                case.get("conversations"), locator=source_file["locator"], case_ordinal=case_ordinal,
            )
            for conversation_id, row, _retained_outer_ordinal in outer_rows:
                signature = canonical_sha256(row)
                prior = conversations_seen.setdefault(conversation_id, signature)
                if prior != signature: raise CustodyError("premix_conversation_content_conflict")
            embedded = _list(case.get("evidenceItems"), "premix_evidence_items_invalid")
            personas = set(); keys = []
            for raw_evidence in embedded:
                evidence = _object(raw_evidence, "premix_evidence_item_invalid")
                persona = _text(evidence.get("personId"), "premix_person_id_invalid"); personas.add(persona)
                question = _text(evidence.get("question"), "premix_question_invalid")
                answer, category, ids = _crosswalk_fields(evidence, "premix_evidence_schema_invalid")
                if any(identifier not in {row[0] for row in outer_rows} for identifier in ids):
                    raise CrosswalkError("premix_embedded_conversation_missing")
                keys.append((persona, question, answer, category, ids))
            if len(personas) != 1:
                excluded["multi_persona_cases"] += 1
                continue
            messages = []
            for conversation_order, (conversation_id, conversation, retained_outer_ordinal) in enumerate(outer_rows):
                message_rows, _message_normalizations = _normalized_premix_message_rows(
                    conversation.get("messages"), locator=source_file["locator"], case_ordinal=case_ordinal,
                    conversation_id=conversation_id, conversation_ordinal=retained_outer_ordinal,
                )
                for message_order, (speaker, text, raw_message_ordinal) in enumerate(message_rows):
                    source = {**locator, "conversation_id": conversation_id, "conversation_ordinal": retained_outer_ordinal, "message_ordinal": raw_message_ordinal}
                    messages.append({"message_id": _opaque(secret, revision, "message", {"conversation_id": conversation_id, "message_ordinal": raw_message_ordinal}), "opaque_conversation_id": _opaque(secret, revision, "conversation", conversation_id), "conversation_order": conversation_order, "message_order": message_order, "corpus_order": len(messages), "speaker": speaker, "text": text, "source_locator": source})
            if not messages:
                raise CustodyError("premix_case_has_no_messages")
            context_size = case.get("contextSize")
            if isinstance(context_size, bool) or not isinstance(context_size, int) or context_size <= 0:
                raise CustodyError("premix_context_size_invalid")
            cases.append({"corpus_id": _opaque(secret, revision, "corpus", locator), "locator": locator, "locator_digest": canonical_sha256(locator), "context_size": context_size, "actual_conversation_count": len(outer_rows), "actual_message_count": len(messages), "keys": keys, "messages": messages})
    if not cases:
        raise CustodyError("premix_cases_empty")
    return cases, excluded


def _crosswalk(canonical: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[tuple[str, str, str, str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for case in cases:
        for key in case["keys"]:
            index.setdefault(key, []).append(case)
    result = {}
    for item in canonical:
        key = (item["persona_source_id"], item["question"], item["answer"], item["category"], item["conversation_ids"])
        matches = index.get(key, [])
        # Same logical case cannot be duplicated through repeated embedded evidence rows.
        unique = {case["corpus_id"]: case for case in matches}
        result[item["canonical_item_id"]] = sorted(unique.values(), key=lambda case: (case["context_size"], case["locator_digest"]))
    return result


def _select(canonical: list[dict[str, Any]], crosswalk: dict[str, list[dict[str, Any]]], secret: bytes, revision: str, config: SelectionConfig, excluded: dict[str, int]) -> tuple[list[tuple[dict[str, Any], dict[str, Any], bool]], dict[str, Any]]:
    config.validate()
    groups = sorted({item["directory"]["group"] for item in canonical})
    if None in groups or not groups:
        raise CustodyError("canonical_group_invalid")
    by_persona: dict[str, list[dict[str, Any]]] = {}
    for item in canonical:
        by_persona.setdefault(item["persona_id"], []).append(item)
    eligible = [rows for rows in by_persona.values() if {item["directory"]["group"] for item in rows} == set(groups)]
    ordered_personas = sorted(eligible, key=lambda rows: (_rank(secret, revision, config.seed, "persona", rows[0]["persona_id"]), rows[0]["persona_id"]))
    if len(ordered_personas) < config.persona_quota:
        raise CustodyError("persona_quota_unavailable", available=len(ordered_personas), quota=config.persona_quota)
    context_values = sorted({case["context_size"] for cases in crosswalk.values() for case in cases})
    if any(index >= len(context_values) for index in config.context_rank_indices): raise CustodyError("context_rank_unavailable")
    desired_contexts = [context_values[index] for index in config.context_rank_indices]
    selected: list[tuple[dict[str, Any], dict[str, Any]]] = []; supplement_count = 0; missing_crosswalk = 0; variants = []
    for persona_rows in ordered_personas[:config.persona_quota]:
        for group in groups:
            group_rows = [item for item in persona_rows if item["directory"]["group"] == group]
            by_tier: dict[str | None, list[dict[str, Any]]] = {}
            for item in group_rows:
                by_tier.setdefault(item["directory"]["tier"], []).append(item)
            tiers = sorted(by_tier, key=lambda tier: (_rank(secret, revision, config.seed, "tier", [persona_rows[0]["persona_id"], group, tier]), str(tier)))
            ordered_items = {tier: sorted(rows, key=lambda row: (_rank(secret, revision, config.seed, "item", row["canonical_item_id"]), row["canonical_item_id"])) for tier, rows in by_tier.items()}
            picked = 0
            while picked < config.per_persona_group_quota:
                progressed = False
                for tier in tiers:
                    if not ordered_items[tier]: continue
                    item = ordered_items[tier].pop(0); copies = crosswalk[item["canonical_item_id"]]
                    by_size = {size: [case for case in copies if case["context_size"] == size] for size in desired_contexts}
                    if not copies: missing_crosswalk += 1; supplement_count += 1; continue
                    if any(not by_size[size] for size in desired_contexts): supplement_count += 1; continue
                    for size in desired_contexts:
                        chosen = sorted(by_size[size], key=lambda case: (_rank(secret, revision, config.seed, "context-variant", case["locator"]), case["locator_digest"]))[0]
                        selected.append((item, chosen)); variants.append(chosen["corpus_id"])
                    picked += 1; progressed = True
                    if picked == config.per_persona_group_quota: break
                if not progressed:
                    raise CustodyError("group_quota_unavailable", group=group)
    receipt = {"algorithm": SELECTION_ALGORITHM, "seed": config.seed, "persona_quota": config.persona_quota, "per_persona_group_quota": config.per_persona_group_quota, "context_rank_indices": list(config.context_rank_indices), "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": canonical_sha256([rows[0]["persona_id"] for rows in ordered_personas[:config.persona_quota]]), "holdout_persona_set_sha256": canonical_sha256(sorted(rows[0]["persona_id"] for rows in ordered_personas[:config.persona_quota])), "group_values_sha256": canonical_sha256(groups), "tier_values_sha256": canonical_sha256(sorted({item["directory"]["tier"] for item in canonical})), "context_values_sha256": canonical_sha256(context_values), "desired_context_values_sha256": canonical_sha256(desired_contexts), "variant_selection_sha256": canonical_sha256(variants), "selected_item_context_count": len(selected), "item_supplement_count": supplement_count, "exclusion_counts": {**excluded, "missing_crosswalk": missing_crosswalk}}
    return selected, receipt


def _audit(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str) or key.replace("-", "_").lower() in _FORBIDDEN:
                raise CustodyError("projection_forbidden_key", key=key)
            _audit(child)
    elif isinstance(value, list):
        for child in value:
            _audit(child)


def _census_logical_variant_pair_rows(items: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Canonical public witness for every observed logical-item × case pair."""
    rows = [
        {
            "selection_logical_item_id": item["selection_logical_item_id"],
            "selection_logical_binding_witness": item["selection_logical_binding_witness"],
            "selection_variant_id": item["selection_variant_id"],
            "item_id": item["item_id"],
            "corpus_id": item["corpus_id"],
        }
        for item in items
    ]
    return sorted(rows, key=lambda row: (row["selection_logical_item_id"], row["selection_variant_id"], row["item_id"], row["corpus_id"]))


def validate_candidate_projection(value: Any) -> dict[str, Any]:
    projection = _object(value, "projection_root_invalid")
    if set(projection) != {"schema", "dataset", "selection_receipt", "corpora", "items"} or projection.get("schema") != SCHEMA:
        raise CustodyError("projection_schema_invalid")
    dataset = _object(projection.get("dataset"), "projection_dataset_invalid")
    if set(dataset) != {"canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256"}:
        raise CustodyError("projection_dataset_schema_invalid")
    for digest in dataset.values(): _token(digest, "projection_digest_invalid")
    receipt = _object(projection.get("selection_receipt"), "projection_selection_receipt_invalid")
    if receipt.get("algorithm") == CENSUS_SELECTION_ALGORITHM:
        required_census = {"algorithm", "seed", "persona_quota", "per_persona_group_quota", "context_rank_indices", "context_rank_semantics", "selected_persona_ids_sha256", "holdout_persona_set_sha256", "group_values_sha256", "tier_values_sha256", "context_values_sha256", "desired_context_values_sha256", "variant_selection_sha256", "logical_variant_pairs_sha256", "corpus_ids_sha256", "selected_item_context_count", "candidate_visible_query_count", "candidate_visible_persona_count", "candidate_visible_context_count", "candidate_visible_corpus_count", "group_count", "selected_item_ids_sha256", "per_context_denominators", "denominators_sha256", "item_supplement_count", "observed_crosswalk", "exclusion_counts", "quarantine_reason_digests", "quarantine_ledger_sha256"}
        if set(receipt) != required_census or (receipt.get("seed"), receipt.get("persona_quota"), receipt.get("per_persona_group_quota"), receipt.get("context_rank_indices"), receipt.get("context_rank_semantics")) != (None, "ALL", "ALL", "ALL_AVAILABLE_SORTED", "all_observed_item_context_pairs"):
            raise CustodyError("projection_census_selection_receipt_invalid")
        digest_keys = {"selected_persona_ids_sha256", "holdout_persona_set_sha256", "group_values_sha256", "tier_values_sha256", "context_values_sha256", "desired_context_values_sha256", "variant_selection_sha256", "logical_variant_pairs_sha256", "corpus_ids_sha256", "selected_item_ids_sha256", "denominators_sha256", "quarantine_ledger_sha256"}
        if any(not isinstance(receipt.get(key), str) or len(receipt[key]) != 64 for key in digest_keys): raise CustodyError("projection_census_selection_receipt_invalid")
        count_keys = {"selected_item_context_count", "candidate_visible_query_count", "candidate_visible_persona_count", "candidate_visible_context_count", "candidate_visible_corpus_count", "group_count", "item_supplement_count"}
        if any(isinstance(receipt.get(key), bool) or not isinstance(receipt.get(key), int) or receipt[key] < 0 for key in count_keys) or receipt["item_supplement_count"] != 0:
            raise CustodyError("projection_census_selection_receipt_invalid")
        expected_exclusions = {"multi_persona_cases", "missing_crosswalk", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes"}
        if not isinstance(receipt.get("exclusion_counts"), Mapping) or set(receipt["exclusion_counts"]) != expected_exclusions or any(value != 0 for value in receipt["exclusion_counts"].values()): raise CustodyError("projection_census_quarantine_nonempty")
        if not isinstance(receipt.get("quarantine_reason_digests"), Mapping) or set(receipt["quarantine_reason_digests"]) != {"multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes"}: raise CustodyError("projection_census_selection_receipt_invalid")
        observed_crosswalk = receipt.get("observed_crosswalk")
        if not isinstance(observed_crosswalk, Mapping) or set(observed_crosswalk) != {"semantics", "multi_persona_corpus_count", "sparse_query_context_count", "multi_case_query_context_count"} or observed_crosswalk.get("semantics") != CENSUS_CROSSWALK_SEMANTICS or any(isinstance(observed_crosswalk.get(key), bool) or not isinstance(observed_crosswalk.get(key), int) or observed_crosswalk[key] < 0 for key in ("multi_persona_corpus_count", "sparse_query_context_count", "multi_case_query_context_count")):
            raise CustodyError("projection_census_selection_receipt_invalid")
        contexts = receipt.get("per_context_denominators")
        if not isinstance(contexts, list) or not contexts or any(not isinstance(row, Mapping) or set(row) != {"context_rank", "declared_context_size", "item_count", "item_ids_sha256"} for row in contexts): raise CustodyError("projection_census_selection_receipt_invalid")
        if [row["context_rank"] for row in contexts] != list(range(len(contexts))) or any(isinstance(row["declared_context_size"], bool) or not isinstance(row["declared_context_size"], int) or row["declared_context_size"] <= 0 or isinstance(row["item_count"], bool) or not isinstance(row["item_count"], int) or row["item_count"] <= 0 or not isinstance(row["item_ids_sha256"], str) or len(row["item_ids_sha256"]) != 64 for row in contexts): raise CustodyError("projection_census_selection_receipt_invalid")
        # Continue through the common corpus/item audit, then check receipts
        # against the actual candidate-safe projection below.
        census_receipt = receipt
    else:
        census_receipt = None
    required = {"algorithm", "seed", "persona_quota", "per_persona_group_quota", "context_rank_indices", "context_rank_semantics", "selected_persona_ids_sha256", "holdout_persona_set_sha256", "group_values_sha256", "tier_values_sha256", "context_values_sha256", "desired_context_values_sha256", "variant_selection_sha256", "selected_item_context_count", "item_supplement_count", "exclusion_counts", "quarantine_reason_digests", "quarantine_ledger_sha256"}
    if census_receipt is None and (set(receipt) != required or receipt.get("algorithm") != SELECTION_ALGORITHM):
        raise CustodyError("projection_selection_receipt_schema_invalid")
    if census_receipt is None and (isinstance(receipt.get("seed"), bool) or not isinstance(receipt.get("seed"), int) or any(isinstance(receipt.get(key), bool) or not isinstance(receipt.get(key), int) or receipt[key] <= 0 for key in ("persona_quota", "per_persona_group_quota"))):
        raise CustodyError("projection_selection_receipt_value_invalid")
    indices = receipt.get("context_rank_indices")
    if census_receipt is None and (not isinstance(indices, list) or not indices or any(isinstance(index, bool) or not isinstance(index, int) or index < 0 for index in indices) or len(set(indices)) != len(indices)):
        raise CustodyError("projection_selection_receipt_value_invalid")
    if census_receipt is None and receipt.get("context_rank_semantics") != "zero_based_unique_sorted_values": raise CustodyError("projection_selection_receipt_value_invalid")
    for key in ("selected_persona_ids_sha256", "holdout_persona_set_sha256", "group_values_sha256", "tier_values_sha256", "context_values_sha256", "desired_context_values_sha256", "variant_selection_sha256", "quarantine_ledger_sha256"):
        _token(receipt.get(key), "projection_selection_receipt_digest_invalid")
    expected_exclusions = {"multi_persona_cases", "missing_crosswalk", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes"}
    if census_receipt is None and (isinstance(receipt.get("selected_item_context_count"), bool) or not isinstance(receipt.get("selected_item_context_count"), int) or receipt["selected_item_context_count"] < 0 or isinstance(receipt.get("item_supplement_count"), bool) or not isinstance(receipt.get("item_supplement_count"), int) or receipt["item_supplement_count"] < 0 or not isinstance(receipt.get("exclusion_counts"), Mapping) or set(receipt["exclusion_counts"]) != expected_exclusions or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in receipt["exclusion_counts"].values())):
        raise CustodyError("projection_selection_receipt_value_invalid")
    quarantine_digests = receipt.get("quarantine_reason_digests")
    if not isinstance(quarantine_digests, Mapping) or set(quarantine_digests) != {"multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes"}:
        raise CustodyError("projection_selection_receipt_value_invalid")
    for digest in quarantine_digests.values():
        _token(digest, "projection_selection_receipt_digest_invalid")
    corpora = _list(projection.get("corpora"), "projection_corpora_invalid"); corpus_ids = set()
    for corpus in corpora:
        row = _object(corpus, "projection_corpus_invalid")
        if set(row) != {"corpus_id", "declared_context_size", "actual_conversation_count", "actual_message_count", "candidates"}: raise CustodyError("projection_corpus_schema_invalid")
        corpus_id = _token(row.get("corpus_id"), "projection_corpus_id_invalid")
        if corpus_id in corpus_ids: raise CustodyError("projection_corpus_duplicate")
        corpus_ids.add(corpus_id)
        declared_context_size = row.get("declared_context_size")
        if isinstance(declared_context_size, bool) or not isinstance(declared_context_size, int) or declared_context_size <= 0:
            raise CustodyError("projection_declared_context_size_invalid")
        if any(isinstance(row.get(name), bool) or not isinstance(row.get(name), int) or row[name] <= 0 for name in ("actual_conversation_count", "actual_message_count")):
            raise CustodyError("projection_corpus_count_invalid")
        messages = set(); conversations: dict[str, list[tuple[int, int]]] = {}; expected_corpus_order = 0
        for candidate in _list(row.get("candidates"), "projection_candidates_invalid"):
            item = _object(candidate, "projection_candidate_invalid")
            if set(item) != {"message_id", "opaque_conversation_id", "conversation_order", "message_order", "corpus_order", "speaker", "text"}: raise CustodyError("projection_candidate_schema_invalid")
            message_id = _token(item.get("message_id"), "projection_message_id_invalid")
            if message_id in messages: raise CustodyError("projection_message_duplicate")
            messages.add(message_id); conversation_id = _token(item.get("opaque_conversation_id"), "projection_conversation_id_invalid"); _text(item.get("speaker"), "projection_message_speaker_invalid"); _text(item.get("text"), "projection_message_text_invalid")
            if any(isinstance(item.get(name), bool) or not isinstance(item.get(name), int) or item[name] < 0 for name in ("conversation_order", "message_order", "corpus_order")):
                raise CustodyError("projection_message_order_invalid")
            if item["corpus_order"] != expected_corpus_order:
                raise CustodyError("projection_corpus_order_invalid")
            conversations.setdefault(conversation_id, []).append((item["conversation_order"], item["message_order"]))
            expected_corpus_order += 1
        if not messages or len(messages) != row["actual_message_count"] or len(conversations) != row["actual_conversation_count"]:
            raise CustodyError("projection_candidates_count_invalid")
        if sorted({pair[0] for rows in conversations.values() for pair in rows}) != list(range(row["actual_conversation_count"])):
            raise CustodyError("projection_conversation_order_invalid")
        if any(sorted(pair[1] for pair in rows) != list(range(len(rows))) or len({pair[0] for pair in rows}) != 1 for rows in conversations.values()):
            raise CustodyError("projection_message_order_invalid")
    seen = set()
    for item in _list(projection.get("items"), "projection_items_invalid"):
        row = _object(item, "projection_item_invalid")
        expected_item_keys = {"item_id", "persona_id", "query_text", "corpus_id"}
        selection_tokens = {"selection_logical_item_id", "selection_logical_binding_witness", "selection_group_id", "selection_tier_id", "selection_variant_id"}
        present_selection_tokens = set(row) & selection_tokens
        if census_receipt is not None:
            # This opaque, revision-bound token is the only candidate-safe
            # witness of selected group membership.  It permits recomputing
            # the census group denominator without revealing a source group.
            expected_item_keys |= selection_tokens
        elif present_selection_tokens:
            expected_item_keys |= selection_tokens
        if set(row) != expected_item_keys: raise CustodyError("projection_item_schema_invalid")
        item_id = _token(row.get("item_id"), "projection_item_id_invalid")
        if item_id in seen: raise CustodyError("projection_item_duplicate")
        seen.add(item_id); _token(row.get("persona_id"), "projection_persona_id_invalid"); _text(row.get("query_text"), "projection_query_invalid")
        corpus_id = _token(row.get("corpus_id"), "projection_corpus_id_invalid")
        if corpus_id not in corpus_ids: raise CustodyError("projection_context_binding_invalid")
        if census_receipt is not None or present_selection_tokens:
            for key in selection_tokens:
                _token(row.get(key), "projection_selection_token_invalid")
    if len(seen) != receipt.get("selected_item_context_count"): raise CustodyError("projection_item_count_invalid")
    if census_receipt is not None:
        referenced_corpus_ids = {item["corpus_id"] for item in projection["items"]}
        if referenced_corpus_ids != corpus_ids:
            raise CustodyError("projection_census_orphan_corpus")
        logical_bindings: dict[str, tuple[str, str, str, str]] = {}
        logical_witnesses: dict[str, str] = {}
        witness_logical_items: dict[str, str] = {}
        variant_corpora: dict[str, str] = {}
        corpus_variants: dict[str, str] = {}
        logical_variant_pairs: set[tuple[str, str]] = set()
        for item in projection["items"]:
            logical_item_id = item["selection_logical_item_id"]
            binding = (item["persona_id"], item["query_text"], item["selection_group_id"], item["selection_tier_id"])
            prior_binding = logical_bindings.setdefault(logical_item_id, binding)
            if prior_binding != binding:
                raise CustodyError("projection_census_logical_item_binding_invalid")
            witness = item["selection_logical_binding_witness"]
            prior_witness = logical_witnesses.setdefault(logical_item_id, witness)
            if prior_witness != witness:
                raise CustodyError("projection_census_logical_item_witness_binding_invalid")
            prior_logical_item_id = witness_logical_items.setdefault(witness, logical_item_id)
            if prior_logical_item_id != logical_item_id:
                raise CustodyError("projection_census_binding_logical_item_split")
            variant_id = item["selection_variant_id"]
            prior_corpus = variant_corpora.setdefault(variant_id, item["corpus_id"])
            if prior_corpus != item["corpus_id"]:
                raise CustodyError("projection_census_variant_corpus_binding_invalid")
            prior_variant = corpus_variants.setdefault(item["corpus_id"], variant_id)
            if prior_variant != variant_id:
                raise CustodyError("projection_census_corpus_variant_binding_invalid")
            pair = (logical_item_id, variant_id)
            if pair in logical_variant_pairs:
                raise CustodyError("projection_census_logical_variant_duplicate")
            logical_variant_pairs.add(pair)
        items_by_context: dict[int, list[str]] = {}
        corpora_by_id = {row["corpus_id"]: row for row in projection["corpora"]}
        persona_ids = sorted({item["persona_id"] for item in projection["items"]})
        selection_groups = {item["selection_group_id"] for item in projection["items"]}
        selection_tiers = {item["selection_tier_id"] for item in projection["items"]}
        selection_variants = sorted(item["selection_variant_id"] for item in projection["items"])
        candidate_queries = {item["selection_logical_item_id"] for item in projection["items"]}
        for item in projection["items"]:
            items_by_context.setdefault(corpora_by_id[item["corpus_id"]]["declared_context_size"], []).append(item["item_id"])
        expected_contexts = [
            {"context_rank": rank, "declared_context_size": context, "item_count": len(sorted(ids)), "item_ids_sha256": canonical_sha256(sorted(ids))}
            for rank, (context, ids) in enumerate(sorted(items_by_context.items()))
        ]
        empty_reason_digest = canonical_sha256([])
        expected_reasons = {
            reason: {"count": 0, "keys_sha256": empty_reason_digest}
            for reason in ("ambiguous_canonical_keys", "unmatched_premix_keys", "canonical_zero_logical_matches", "multiple_logical_matches_or_variants", "missing_requested_context_sizes", "multi_persona_cases")
        }
        expected_quarantine_ledger = {
            "schema": "aerp7-convomem-quarantine-ledger-v3",
            "dataset_revision_sha256": projection["dataset"]["revision_sha256"],
            # SQL stores the frozen pre-mix selector values as floats, whereas
            # candidate corpora canonically expose integral declared sizes.
            "desired_context_values_sha256": canonical_sha256([float(row["declared_context_size"]) for row in expected_contexts]),
            "matching_semantics": CENSUS_CROSSWALK_SEMANTICS,
            "reasons": expected_reasons,
        }
        query_context_corpora: dict[tuple[str, int], set[str]] = {}
        personas_by_corpus: dict[str, set[str]] = {}
        contexts_by_query: dict[str, set[int]] = {}
        for item in projection["items"]:
            context_size = corpora_by_id[item["corpus_id"]]["declared_context_size"]
            logical_item_id = item["selection_logical_item_id"]
            query_context_corpora.setdefault((logical_item_id, context_size), set()).add(item["corpus_id"])
            contexts_by_query.setdefault(logical_item_id, set()).add(context_size)
            personas_by_corpus.setdefault(item["corpus_id"], set()).add(item["persona_id"])
        expected_observed_crosswalk = {
            "semantics": CENSUS_CROSSWALK_SEMANTICS,
            "multi_persona_corpus_count": sum(len(personas) > 1 for personas in personas_by_corpus.values()),
            "sparse_query_context_count": sum(contexts != {row["declared_context_size"] for row in expected_contexts} for contexts in contexts_by_query.values()),
            "multi_case_query_context_count": sum(len(corpora) > 1 for corpora in query_context_corpora.values()),
        }
        if (
            receipt["selected_item_ids_sha256"] != canonical_sha256(sorted(seen))
            or receipt["selected_persona_ids_sha256"] != canonical_sha256(persona_ids)
            or receipt["holdout_persona_set_sha256"] != canonical_sha256(persona_ids)
            or receipt["candidate_visible_query_count"] != len(candidate_queries)
            or receipt["candidate_visible_persona_count"] != len(persona_ids)
            or receipt["candidate_visible_context_count"] != len(expected_contexts)
            or receipt["candidate_visible_corpus_count"] != len(corpus_ids)
            or receipt["group_values_sha256"] != canonical_sha256(sorted(selection_groups))
            or receipt["tier_values_sha256"] != canonical_sha256(sorted(selection_tiers))
            or receipt["variant_selection_sha256"] != canonical_sha256(selection_variants)
            or receipt["logical_variant_pairs_sha256"] != canonical_sha256(_census_logical_variant_pair_rows(projection["items"]))
            or receipt["corpus_ids_sha256"] != canonical_sha256(sorted(corpus_ids))
            or receipt["context_values_sha256"] != canonical_sha256([row["declared_context_size"] for row in expected_contexts])
            or receipt["desired_context_values_sha256"] != canonical_sha256([row["declared_context_size"] for row in expected_contexts])
            or receipt["observed_crosswalk"] != expected_observed_crosswalk
            or receipt["quarantine_reason_digests"] != {reason: empty_reason_digest for reason in expected_reasons}
            or receipt["quarantine_ledger_sha256"] != canonical_sha256(expected_quarantine_ledger)
            or receipt["per_context_denominators"] != expected_contexts
            or receipt["group_count"] != len(selection_groups)
            or receipt["denominators_sha256"] != canonical_sha256({"query_count": len(seen), "candidate_visible_query_count": len(candidate_queries), "persona_count": len(persona_ids), "context_count": len(expected_contexts), "corpus_count": len(corpus_ids), "group_count": len(selection_groups), "logical_variant_pairs_sha256": canonical_sha256(_census_logical_variant_pair_rows(projection["items"])), "corpus_ids_sha256": canonical_sha256(sorted(corpus_ids)), "per_context_denominators": expected_contexts})
        ):
            raise CustodyError("projection_census_denominator_binding_invalid")
    _audit(projection)
    return dict(projection)


def materialize_candidate_items(value: Any) -> list[dict[str, Any]]:
    projection = validate_candidate_projection(value); corpora = {row["corpus_id"]: row for row in projection["corpora"]}
    return [{"item_id": row["item_id"], "persona_id": row["persona_id"], "query_text": row["query_text"], "corpus_id": row["corpus_id"], "declared_context_size": corpora[row["corpus_id"]]["declared_context_size"], "actual_conversation_count": corpora[row["corpus_id"]]["actual_conversation_count"], "actual_message_count": corpora[row["corpus_id"]]["actual_message_count"], "candidates": corpora[row["corpus_id"]]["candidates"]} for row in projection["items"]]


def _write(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_bytes(value).decode("utf-8")); stream.flush(); os.fsync(stream.fileno())


def _is_reparse(metadata: os.stat_result) -> bool:
    return os.name == "nt" and bool(getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _directory_identity(path: Path, code: str) -> tuple[int, int]:
    try: metadata = os.lstat(path)
    except OSError as exc: raise CustodyError(code) from exc
    if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata): raise CustodyError(code)
    return metadata.st_dev, metadata.st_ino


def _snapshot(path: Path, code: str, *, retain: bool = True) -> tuple[bytes, tuple[int, int], str]:
    """Read one regular, non-linked file from a stable lstat/open/fstat snapshot."""
    try: before = os.lstat(path)
    except OSError as exc: raise CustodyError(code) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or _is_reparse(before): raise CustodyError(code)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(path, flags)
    except OSError as exc: raise CustodyError(code) from exc
    try:
        opened = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino)
        if (opened.st_dev, opened.st_ino) != identity or opened.st_nlink != 1 or not stat.S_ISREG(opened.st_mode): raise CustodyError(code)
        chunks = []; digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk: break
            if retain: chunks.append(chunk)
            digest.update(chunk)
    finally:
        os.close(descriptor)
    try: after = os.lstat(path)
    except OSError as exc: raise CustodyError(code) from exc
    if (after.st_dev, after.st_ino) != identity or after.st_nlink != 1 or _is_reparse(after): raise CustodyError(code)
    return b"".join(chunks), identity, digest.hexdigest()


def _source_snapshot(path: Path, code: str, *, retain: bool = True) -> tuple[bytes, tuple[int, int], str]:
    """Read an official source file from a stable regular-file snapshot.

    Official inputs may be ordinary hardlinks to a Git LFS object cache.  This
    is intentionally separate from ``_snapshot``: all staging, index, output,
    and publication artifacts remain private files with exactly one link.
    """
    try: before = os.lstat(path)
    except OSError as exc: raise CustodyError(code) from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or _is_reparse(before): raise CustodyError(code)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try: descriptor = os.open(path, flags)
    except OSError as exc: raise CustodyError(code) from exc
    identity = (before.st_dev, before.st_ino)
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_nlink) != (before.st_dev, before.st_ino, before.st_size, before.st_nlink) or _is_reparse(opened)):
            raise CustodyError(code)
        chunks = []; digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk: break
            if retain: chunks.append(chunk)
            digest.update(chunk)
    finally:
        os.close(descriptor)
    try: after = os.lstat(path)
    except OSError as exc: raise CustodyError(code) from exc
    if (not stat.S_ISREG(after.st_mode) or _is_reparse(after) or (after.st_dev, after.st_ino, after.st_size, after.st_nlink) != (before.st_dev, before.st_ino, before.st_size, before.st_nlink)):
        raise CustodyError(code)
    return b"".join(chunks), identity, digest.hexdigest()


def _decode(raw: bytes, code: str) -> Any:
    try: return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise CustodyError(code) from exc


def _ijson() -> Any:
    try:
        import ijson
    except ImportError as exc:
        raise CustodyError("streaming_json_dependency_unavailable") from exc
    return ijson


def _copy_source_once(path: Path, destination: Path, *, on_staged_created: Callable[[tuple[int, int]], None] | None = None) -> tuple[tuple[int, int], str]:
    """Stream one official descriptor into staging while hashing its exact byte snapshot."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or _is_reparse(before): raise CustodyError("official_source_file_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags); digest = hashlib.sha256(); staged_identity: tuple[int, int] | None = None; identity = (before.st_dev, before.st_ino)
    try:
        try:
            opened = os.fstat(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != identity or opened.st_nlink != before.st_nlink or opened.st_size != before.st_size or _is_reparse(opened)): raise CustodyError("official_source_file_invalid")
            with destination.open("xb") as staged:
                created = os.fstat(staged.fileno())
                if not stat.S_ISREG(created.st_mode) or created.st_nlink != 1 or _is_reparse(created):
                    raise CustodyError("streaming_stage_invalid")
                staged_identity = (created.st_dev, created.st_ino)
                if on_staged_created is not None:
                    on_staged_created(staged_identity)
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk: break
                    digest.update(chunk); staged.write(chunk)
                staged.flush(); os.fsync(staged.fileno())
            after = os.lstat(path)
            if (not stat.S_ISREG(after.st_mode) or (after.st_dev, after.st_ino) != identity or after.st_nlink != before.st_nlink or after.st_size != before.st_size or _is_reparse(after)): raise CustodyError("official_source_drift")
            return identity, digest.hexdigest()
        except Exception:
            if staged_identity is not None:
                try:
                    current = os.lstat(destination)
                    if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == staged_identity:
                        destination.unlink()
                    else:
                        raise CustodyError("streaming_stage_cleanup_identity_drift")
                except FileNotFoundError:
                    pass
                except CustodyError:
                    raise
                except OSError as cleanup_error:
                    raise CustodyError("streaming_stage_cleanup_failed") from cleanup_error
            raise
    finally:
        os.close(descriptor)


class _DigestingReader:
    """A transparent parser reader that commits to every byte consumed."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        value = self._stream.read(size)
        self._digest.update(value)
        return value

    def readinto(self, buffer: Any) -> int:
        count = self._stream.readinto(buffer)
        if count:
            self._digest.update(memoryview(buffer)[:count])
        return count

    def readable(self) -> bool:
        return True

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _verify_index_bytes(index: StreamingIndex, code: str) -> None:
    _raw, identity, digest = _snapshot(index.database, code, retain=False)
    if identity != index.database_identity or digest != index.database_sha256:
        raise CustodyError(code)


def _source_size_inventory(canonical_root: Path, premix_root: Path) -> dict[str, Any]:
    """Safely inventory exact source bytes before allocating a large index."""
    total = 0; maximum = 0; count = 0; rows: list[dict[str, Any]] = []
    for role, root in (("canonical", canonical_root), ("premix", premix_root)):
        for path in _files(root):
            before = os.lstat(path)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or _is_reparse(before):
                raise CustodyError("source_size_inventory_invalid")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or opened.st_size != before.st_size or opened.st_nlink != before.st_nlink or _is_reparse(opened)):
                raise CustodyError("source_size_inventory_invalid")
            after = os.lstat(path)
            if (not stat.S_ISREG(after.st_mode) or _is_reparse(after) or (after.st_dev, after.st_ino, after.st_size, after.st_nlink) != (before.st_dev, before.st_ino, before.st_size, before.st_nlink)):
                raise CustodyError("source_size_inventory_drift")
            total += before.st_size; maximum = max(maximum, before.st_size); count += 1
            rows.append({"role": role, "locator": path.relative_to(root).as_posix(), "identity": [before.st_dev, before.st_ino], "size": before.st_size})
    if count == 0:
        raise CustodyError("source_size_inventory_empty")
    return {"source_file_count": count, "source_total_bytes": total, "source_max_file_bytes": maximum, "source_files": rows, "source_inventory_sha256": canonical_sha256(rows)}


def _staging_receipt(staging_root: Path, inventory: Mapping[str, int]) -> tuple[Path, dict[str, Any]]:
    if not staging_root.is_absolute() or staging_root.is_symlink() or not staging_root.is_dir():
        raise CustodyError("staging_root_invalid")
    _safe_existing_ancestors(staging_root, "staging_root_invalid")
    identity = _directory_identity(staging_root, "staging_root_invalid")
    try:
        free_bytes = shutil.disk_usage(staging_root).free
    except OSError as exc:
        raise CustodyError("staging_root_invalid") from exc
    required = inventory["source_total_bytes"] * SQLITE_INDEX_EXPANSION_FACTOR + inventory["source_max_file_bytes"] + STAGING_HEADROOM_BYTES
    if free_bytes < required:
        raise CustodyError("staging_free_space_unavailable", available_bytes=free_bytes, required_bytes=required)
    return staging_root, {"schema": "aerp7-convomem-staging-preflight-v4", "root": str(staging_root), "directory_identity": list(identity), "volume_device": identity[0], **dict(inventory), "sqlite_index_expansion_factor": SQLITE_INDEX_EXPANSION_FACTOR, "headroom_bytes": STAGING_HEADROOM_BYTES, "formula": "source_total_bytes*sqlite_index_expansion_factor+source_max_file_bytes+headroom_bytes", "required_bytes": required, "available_bytes": free_bytes, "sqlite_temp_policy": "launch-environment-verified-file-v1"}


def _normalized_path(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _windows_temp_path() -> str:
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetTempPathW(len(buffer), buffer)
    if length <= 0 or length >= len(buffer):
        raise CustodyError("sqlite_temp_environment_mismatch")
    return buffer.value


def _verify_sqlite_temp_environment(staging_root: Path) -> dict[str, str]:
    """Verify the isolated process was launched with SQLite temp on staging.

    SQLite's documented Windows VFS path is GetTempPath(); supported Linux uses
    SQLITE_TMPDIR before TMPDIR.  This code never mutates those global inputs.
    """
    expected = _normalized_path(staging_root)
    if os.name == "nt":
        actual = _normalized_path(_windows_temp_path())
        rule = "windows-gettemppathw-v1"
    elif os.name == "posix" and sys.platform.startswith("linux"):
        configured = os.environ.get("SQLITE_TMPDIR")
        if not configured:
            raise CustodyError("sqlite_temp_environment_mismatch")
        actual = _normalized_path(configured)
        rule = "linux-sqlite_tmpdir-v1"
    else:
        raise CustodyError("sqlite_temp_environment_mismatch")
    if actual != expected:
        raise CustodyError("sqlite_temp_environment_mismatch", expected_temp_path=expected, actual_temp_path=actual)
    return {"os_rule": rule, "resolved_temp_path": actual}


def _configure_sqlite_temp(connection: sqlite3.Connection, staging_root: Path) -> dict[str, str]:
    """Require FILE temp mode after the process-level VFS precondition."""
    environment = _verify_sqlite_temp_environment(staging_root)
    try:
        connection.execute("PRAGMA temp_store=FILE")
        mode = connection.execute("PRAGMA temp_store").fetchone()
        if mode is None or mode[0] != 1:
            raise CustodyError("sqlite_temp_memory_or_unknown")
    except sqlite3.Error as exc:
        raise CustodyError("sqlite_temp_file_mode_unavailable") from exc
    return {"policy": "launch-environment-verified-file-v1", **environment}


def _index_connection(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database)
    try:
        _configure_sqlite_temp(connection, database.parent.parent)
        return connection
    except Exception:
        connection.close()
        raise


def _streaming_index(canonical_root: Path, premix_root: Path, staging_root: Path, *, staging_preflight: Mapping[str, Any] | None = None) -> StreamingIndex:
    """Build a bounded-memory index from one verified read of every source file.

    The temporary JSON copy exists only while its parser consumes it, then is
    unlinked.  SQLite stores structured fields, never a whole JSON document, so
    peak temporary disk is the database plus one source file and Python never
    owns a complete official file/case corpus.
    """
    _require_builder_platform()
    ijson = _ijson()
    if staging_preflight is None:
        inventory = _source_size_inventory(canonical_root, premix_root)
        staging_root, staging_receipt = _staging_receipt(staging_root, inventory)
    else:
        staging_receipt = dict(staging_preflight)
        inventory = {key: staging_receipt[key] for key in ("source_file_count", "source_total_bytes", "source_max_file_bytes", "source_files", "source_inventory_sha256")}
        if staging_receipt.get("root") != str(staging_root):
            raise CustodyError("staging_preflight_root_drift")
    expected_identity_raw = staging_receipt.get("directory_identity")
    if not isinstance(expected_identity_raw, list) or len(expected_identity_raw) != 2 or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_identity_raw):
        raise CustodyError("staging_preflight_identity_invalid")
    staging_identity = (expected_identity_raw[0], expected_identity_raw[1])
    if staging_receipt.get("volume_device") != staging_identity[0]:
        raise CustodyError("staging_preflight_identity_invalid")
    if _directory_identity(staging_root, "staging_root_identity_drift") != staging_identity:
        raise CustodyError("staging_root_identity_drift")
    directory = Path(tempfile.mkdtemp(prefix="aerp7-convomem-index-", dir=staging_root))
    if _directory_identity(staging_root, "staging_root_identity_drift") != staging_identity:
        # ``mkdtemp`` created this empty directory.  Remove it only if its live
        # identity still matches the exact object we just created; a raced
        # replacement is left observable rather than recursively touched.
        directory_identity = _directory_identity(directory, "streaming_index_directory_invalid")
        if _same_directory(directory, directory_identity):
            try:
                directory.rmdir()
            except OSError:
                pass
        raise CustodyError("staging_root_identity_drift")
    directory_identity = _directory_identity(directory, "streaming_index_directory_invalid")
    marker = directory / ".aerp7-owned"
    marker.write_text("owned", encoding="ascii")
    database = directory / "index.sqlite3"
    owned_files = {marker: _snapshot(marker, "streaming_index_marker_invalid", retain=False)[1]}
    receipt: list[dict[str, Any]] = []
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database)
        sqlite_temp_placement = _configure_sqlite_temp(connection, staging_root)
        staging_receipt["sqlite_temp_placement"] = dict(sqlite_temp_placement)
        owned_files[database] = _snapshot(database, "streaming_index_database_invalid", retain=False)[1]
        connection.executescript("""
            CREATE TABLE canonical_items(key_sha TEXT NOT NULL, locator TEXT NOT NULL, ordinal INTEGER NOT NULL, persona TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL, category TEXT NOT NULL, conversations TEXT NOT NULL, labels TEXT NOT NULL, directory TEXT NOT NULL);
            CREATE UNIQUE INDEX canonical_locator ON canonical_items(locator, ordinal);
            CREATE INDEX canonical_key ON canonical_items(key_sha);
            CREATE TABLE premix_cases(case_sha TEXT PRIMARY KEY, locator TEXT NOT NULL, context_size REAL NOT NULL, messages TEXT NOT NULL);
            CREATE TABLE premix_keys(key_sha TEXT NOT NULL, case_sha TEXT NOT NULL);
            CREATE INDEX premix_key ON premix_keys(key_sha);
            CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY, content_sha TEXT NOT NULL);
            CREATE TABLE premix_exact_duplicate_normalizations(case_sha TEXT NOT NULL, locator TEXT NOT NULL, case_ordinal INTEGER NOT NULL, conversation_id_sha256 TEXT NOT NULL, content_sha TEXT NOT NULL, retained_outer_ordinal INTEGER NOT NULL, duplicate_outer_ordinal INTEGER NOT NULL, PRIMARY KEY(case_sha, conversation_id_sha256, duplicate_outer_ordinal));
            CREATE TABLE premix_exact_empty_message_text_normalizations(case_sha TEXT NOT NULL, locator TEXT NOT NULL, case_ordinal INTEGER NOT NULL, conversation_id_sha256 TEXT NOT NULL, retained_outer_ordinal INTEGER NOT NULL, raw_message_ordinal INTEGER NOT NULL, speaker_sha TEXT NOT NULL, text_sha TEXT NOT NULL, PRIMARY KEY(case_sha, conversation_id_sha256, retained_outer_ordinal, raw_message_ordinal));
            CREATE TABLE explicit_quarantine(reason TEXT NOT NULL, key_sha TEXT NOT NULL, PRIMARY KEY(reason, key_sha));
        """)
        digests = {}
        for role, root, prefix in (("canonical", canonical_root, "evidence_items.item"), ("premix", premix_root, "item")):
            expected = [row for row in inventory["source_files"] if row["role"] == role]
            files = _files(root)
            if [path.relative_to(root).as_posix() for path in files] != [row["locator"] for row in expected]:
                raise CustodyError("source_inventory_membership_drift")
            rows = []
            for number, source in enumerate(files):
                expected_row = expected[number]
                source_stat = os.lstat(source)
                if [source_stat.st_dev, source_stat.st_ino] != expected_row["identity"] or source_stat.st_size != expected_row["size"]:
                    raise CustodyError("source_inventory_identity_drift")
                staged = directory / f"{role}-{number:06d}.json"
                def register_stage(stage_identity: tuple[int, int], path: Path = staged) -> None:
                    owned_files[path] = stage_identity
                identity, digest = _copy_source_once(source, staged, on_staged_created=register_stage)
                source_after_copy = os.lstat(source)
                if list(identity) != expected_row["identity"] or source_after_copy.st_size != expected_row["size"] or [source_after_copy.st_dev, source_after_copy.st_ino] != expected_row["identity"]:
                    raise CustodyError("source_inventory_identity_drift")
                staged_snapshot_identity = _snapshot(staged, "streaming_stage_invalid", retain=False)[1]
                if owned_files.get(staged) != staged_snapshot_identity:
                    raise CustodyError("streaming_stage_identity_drift")
                locator = source.relative_to(root).as_posix(); rows.append({"locator": locator, "sha256": digest, "identity": identity, "size": source_stat.st_size, "source": source, "staged": staged})
                try:
                    with staged.open("rb") as stream:
                        parser_reader = _DigestingReader(stream)
                        # use_float=True makes numbers predictable (not Decimal)
                        # across ijson backends, and rejects non-finite values.
                        for ordinal, raw in enumerate(ijson.items(parser_reader, prefix, use_float=True)):
                            if role == "canonical":
                                item = _object(raw, "canonical_evidence_item_invalid")
                                persona = _text(item.get("personId"), "canonical_person_id_invalid")
                                question = _text(item.get("question"), "canonical_question_invalid")
                                answer, category, conversations, labels = _labels(item, "canonical_label_schema_invalid")
                                key = canonical_sha256([persona, question, answer, category, conversations])
                                directory_labels = _directory(Path(locator)); directory_labels["record_category"] = category
                                connection.execute("INSERT INTO canonical_items VALUES(?,?,?,?,?,?,?,?,?,?)", (key, locator, ordinal, persona, question, answer, category, _bytes(list(conversations)).decode(), _bytes(labels).decode(), _bytes(directory_labels).decode()))
                            else:
                                case = _object(raw, "premix_case_invalid")
                                context_size = case.get("contextSize")
                                if isinstance(context_size, bool) or not isinstance(context_size, int) or context_size <= 0:
                                    raise CustodyError("premix_context_size_invalid")
                                locator_case = {"path": locator, "case_ordinal": ordinal}; case_sha = canonical_sha256(locator_case)
                                outer_rows, duplicate_normalizations = _normalized_premix_outer_rows(
                                    case.get("conversations"), locator=locator, case_ordinal=ordinal,
                                )
                                outer_ids = {conversation_id for conversation_id, _row, _retained_outer_ordinal in outer_rows}; messages = []
                                for duplicate in duplicate_normalizations:
                                    connection.execute(
                                        "INSERT INTO premix_exact_duplicate_normalizations VALUES(?,?,?,?,?,?,?)",
                                        (case_sha, locator, ordinal, duplicate["conversation_id_sha256"], duplicate["content_sha256"], duplicate["retained_outer_ordinal"], duplicate["duplicate_outer_ordinal"]),
                                    )
                                for conversation_order, (conversation_id, row, retained_outer_ordinal) in enumerate(outer_rows):
                                    content_sha = canonical_sha256(row)
                                    prior = connection.execute("SELECT content_sha FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
                                    if prior is None:
                                        connection.execute("INSERT INTO conversations VALUES(?,?)", (conversation_id, content_sha))
                                    elif prior[0] != content_sha:
                                        raise CustodyError("premix_conversation_content_conflict")
                                    message_rows, message_normalizations = _normalized_premix_message_rows(
                                        row.get("messages"), locator=locator, case_ordinal=ordinal,
                                        conversation_id=conversation_id, conversation_ordinal=retained_outer_ordinal,
                                    )
                                    for normalization in message_normalizations:
                                        connection.execute(
                                            "INSERT INTO premix_exact_empty_message_text_normalizations VALUES(?,?,?,?,?,?,?,?)",
                                            (case_sha, locator, ordinal, normalization["conversation_id_sha256"], normalization["retained_outer_ordinal"], normalization["raw_message_ordinal"], normalization["speaker_sha256"], normalization["text_sha256"]),
                                        )
                                    for message_order, (speaker, text, raw_message_ordinal) in enumerate(message_rows):
                                        messages.append({"conversation_id": conversation_id, "conversation_ordinal": conversation_order, "message_ordinal": message_order, "raw_message_ordinal": raw_message_ordinal, "corpus_ordinal": len(messages), "speaker": speaker, "text": text, "source_locator": {**locator_case, "conversation_id": conversation_id, "conversation_ordinal": retained_outer_ordinal, "message_ordinal": raw_message_ordinal}})
                                if not messages:
                                    raise CustodyError("premix_case_has_no_messages")
                                keys = []
                                for embedded in _list(case.get("evidenceItems"), "premix_evidence_items_invalid"):
                                    evidence = _object(embedded, "premix_evidence_item_invalid")
                                    persona = _text(evidence.get("personId"), "premix_person_id_invalid")
                                    question = _text(evidence.get("question"), "premix_question_invalid")
                                    answer, category, conversations = _crosswalk_fields(evidence, "premix_evidence_schema_invalid")
                                    if any(conversation_id not in outer_ids for conversation_id in conversations):
                                        raise CrosswalkError("premix_embedded_conversation_missing")
                                    keys.append(canonical_sha256([persona, question, answer, category, conversations]))
                                personas = {_text(_object(raw, "premix_evidence_item_invalid").get("personId"), "premix_person_id_invalid") for raw in _list(case.get("evidenceItems"), "premix_evidence_items_invalid")}
                                if not personas:
                                    raise CustodyError("premix_case_has_no_embedded_evidence")
                                if len(personas) != 1:
                                    connection.execute("INSERT INTO explicit_quarantine VALUES(?,?)", ("multi_persona_cases", case_sha))
                                connection.execute("INSERT INTO premix_cases VALUES(?,?,?,?)", (case_sha, _bytes(locator_case).decode(), float(context_size), _bytes(messages).decode()))
                                for key in sorted(set(keys)):
                                    connection.execute("INSERT INTO premix_keys VALUES(?,?)", (key, case_sha))
                        # A parser may finish the selected prefix before EOF;
                        # byte binding must cover trailing bytes too.
                        while parser_reader.read(1024 * 1024):
                            pass
                        if parser_reader.hexdigest != digest:
                            raise CustodyError("streaming_stage_digest_mismatch")
                finally:
                    # The staged copy is never part of the persistent index.
                    staged_metadata = os.lstat(staged)
                    if not stat.S_ISREG(staged_metadata.st_mode) or staged_metadata.st_nlink != 1 or _is_reparse(staged_metadata) or (staged_metadata.st_dev, staged_metadata.st_ino) != owned_files[staged]:
                        raise CustodyError("streaming_stage_invalid")
                    staged.unlink()
                    owned_files.pop(staged)
            digests[role] = canonical_sha256([{"locator": row["locator"], "sha256": row["sha256"]} for row in rows]); receipt.extend({"role": role, "locator": row["locator"], "sha256": row["sha256"], "identity": list(row["identity"]), "size": row["size"]} for row in rows)
        normalization_digest = hashlib.sha256(); normalization_count = 0
        for row in connection.execute("SELECT locator, case_ordinal, conversation_id_sha256, content_sha, retained_outer_ordinal, duplicate_outer_ordinal FROM premix_exact_duplicate_normalizations ORDER BY locator, case_ordinal, conversation_id_sha256, duplicate_outer_ordinal"):
            normalization_digest.update(_bytes({"locator": row[0], "case_ordinal": row[1], "conversation_id_sha256": row[2], "content_sha256": row[3], "retained_outer_ordinal": row[4], "duplicate_outer_ordinal": row[5]}))
            normalization_digest.update(b"\n")
            normalization_count += 1
        normalization_cases = connection.execute("SELECT count(DISTINCT case_sha) FROM premix_exact_duplicate_normalizations").fetchone()[0]
        staging_receipt["premix_exact_duplicate_normalization"] = {
            **PREMIX_EXACT_DUPLICATE_NORMALIZATION,
            "duplicate_case_count": normalization_cases,
            "duplicate_extra_row_count": normalization_count,
            "normalization_stream_sha256": normalization_digest.hexdigest(),
        }
        empty_normalization_digest = hashlib.sha256(); empty_normalization_count = 0
        for row in connection.execute("SELECT locator, case_ordinal, conversation_id_sha256, retained_outer_ordinal, raw_message_ordinal, speaker_sha, text_sha FROM premix_exact_empty_message_text_normalizations ORDER BY locator, case_ordinal, conversation_id_sha256, retained_outer_ordinal, raw_message_ordinal"):
            empty_normalization_digest.update(_bytes({"locator": row[0], "case_ordinal": row[1], "conversation_id_sha256": row[2], "retained_outer_ordinal": row[3], "raw_message_ordinal": row[4], "speaker_sha256": row[5], "text_sha256": row[6]}))
            empty_normalization_digest.update(b"\n")
            empty_normalization_count += 1
        empty_normalization_cases = connection.execute("SELECT count(DISTINCT case_sha) FROM premix_exact_empty_message_text_normalizations").fetchone()[0]
        empty_normalization_conversations = connection.execute("SELECT count(DISTINCT case_sha || ':' || conversation_id_sha256 || ':' || retained_outer_ordinal) FROM premix_exact_empty_message_text_normalizations").fetchone()[0]
        staging_receipt["premix_exact_empty_message_text_normalization"] = {
            **PREMIX_EXACT_EMPTY_MESSAGE_TEXT_NORMALIZATION,
            "normalized_case_count": empty_normalization_cases,
            "normalized_conversation_count": empty_normalization_conversations,
            "normalized_extra_row_count": empty_normalization_count,
            "normalization_stream_sha256": empty_normalization_digest.hexdigest(),
        }
        connection.commit(); connection.close(); connection = None
        _raw, database_identity, database_sha256 = _snapshot(database, "streaming_index_database_invalid", retain=False)
        owned_files[database] = database_identity
        staging_receipt["sqlite_raw_sha256"] = database_sha256
        revision = canonical_sha256({"canonical": digests["canonical"], "premix": digests["premix"]})
        return StreamingIndex(directory, database, digests["canonical"], digests["premix"], revision, receipt, directory_identity, staging_identity, owned_files, staging_receipt, database_identity, database_sha256)
    except Exception as exc:
        if connection is not None:
            try: connection.close()
            except sqlite3.Error as close_error: raise CustodyError("streaming_index_connection_close_failed") from close_error
        cleanup = StreamingIndex(directory, database, "", "", "", [], directory_identity, staging_identity, owned_files, staging_receipt, (0, 0), "")
        try:
            cleanup.close(failure_cleanup=True)
        except CustodyError as cleanup_error:
            raise cleanup_error from exc
        raise


def _quarantine_ledger(database: Path, revision: str, desired_contexts: Sequence[float] = (), *, census_observed_pairs: bool = False) -> dict[str, Any]:
    """Global SQL-only join audit; no selection-path encounter may hide a bad key."""
    connection = _index_connection(database)
    try:
        queries = {
            "ambiguous_canonical_keys": "SELECT key_sha FROM canonical_items GROUP BY key_sha HAVING count(*) != 1",
            "unmatched_premix_keys": "SELECT DISTINCT p.key_sha FROM premix_keys p LEFT JOIN canonical_items c ON c.key_sha=p.key_sha WHERE c.key_sha IS NULL",
            "canonical_zero_logical_matches": "SELECT c.key_sha FROM canonical_items c LEFT JOIN premix_keys p ON p.key_sha=c.key_sha GROUP BY c.key_sha HAVING count(p.case_sha)=0",
        }
        reasons = {}
        for reason, query in queries.items():
            keys = sorted({row[0] for row in connection.execute(query)})
            reasons[reason] = {"count": len(keys), "keys_sha256": canonical_sha256(keys)}
        if census_observed_pairs:
            # The formal estimand is every observed embedded-evidence key ×
            # pre-mix case pair.  Official sparse context coverage, multiple
            # cases at one context, and multi-persona cases are therefore
            # disclosed selection structure, not unresolved crosswalk faults.
            for reason in ("multiple_logical_matches_or_variants", "missing_requested_context_sizes", "multi_persona_cases"):
                reasons[reason] = {"count": 0, "keys_sha256": canonical_sha256([])}
            receipt = {
                "schema": "aerp7-convomem-quarantine-ledger-v3",
                "dataset_revision_sha256": revision,
                "desired_context_values_sha256": canonical_sha256(list(desired_contexts)),
                "matching_semantics": CENSUS_CROSSWALK_SEMANTICS,
                "reasons": reasons,
            }
        else:
            variant_keys = sorted({row[0] for row in connection.execute("SELECT c.key_sha FROM canonical_items c JOIN premix_keys p ON p.key_sha=c.key_sha JOIN premix_cases pc ON pc.case_sha=p.case_sha GROUP BY c.key_sha, pc.context_size HAVING count(DISTINCT pc.case_sha)>1")})
            reasons["multiple_logical_matches_or_variants"] = {"count": len(variant_keys), "keys_sha256": canonical_sha256(variant_keys)}
            missing = []
            for row in connection.execute("SELECT DISTINCT key_sha FROM canonical_items"):
                key = row[0]
                available = {candidate[0] for candidate in connection.execute("SELECT DISTINCT pc.context_size FROM premix_keys p JOIN premix_cases pc ON pc.case_sha=p.case_sha WHERE p.key_sha=?", (key,))}
                if any(float(context) not in available for context in desired_contexts): missing.append(key)
            reasons["missing_requested_context_sizes"] = {"count": len(missing), "keys_sha256": canonical_sha256(sorted(missing))}
            multi = sorted(row[0] for row in connection.execute("SELECT key_sha FROM explicit_quarantine WHERE reason='multi_persona_cases'"))
            reasons["multi_persona_cases"] = {"count": len(multi), "keys_sha256": canonical_sha256(multi)}
            receipt = {"schema": "aerp7-convomem-quarantine-ledger-v2", "dataset_revision_sha256": revision, "desired_context_values_sha256": canonical_sha256(list(desired_contexts)), "reasons": reasons}
        receipt["ledger_sha256"] = canonical_sha256(receipt)
        return receipt
    finally:
        connection.close()


def _validate_quarantine_ledger(value: Any, revision: str) -> dict[str, Any]:
    ledger = _object(value, "quarantine_ledger_invalid")
    v2 = {"schema", "dataset_revision_sha256", "desired_context_values_sha256", "reasons", "ledger_sha256"}
    v3 = v2 | {"matching_semantics"}
    if set(ledger) not in (v2, v3) or ledger.get("dataset_revision_sha256") != revision:
        raise CustodyError("quarantine_ledger_invalid")
    if ledger.get("schema") == "aerp7-convomem-quarantine-ledger-v3":
        if set(ledger) != v3 or ledger.get("matching_semantics") != CENSUS_CROSSWALK_SEMANTICS:
            raise CustodyError("quarantine_ledger_invalid")
    elif ledger.get("schema") != "aerp7-convomem-quarantine-ledger-v2" or set(ledger) != v2:
        raise CustodyError("quarantine_ledger_invalid")
    reasons = _object(ledger.get("reasons"), "quarantine_ledger_invalid")
    if set(reasons) != {"ambiguous_canonical_keys", "unmatched_premix_keys", "canonical_zero_logical_matches", "multiple_logical_matches_or_variants", "missing_requested_context_sizes", "multi_persona_cases"}:
        raise CustodyError("quarantine_ledger_invalid")
    for row in reasons.values():
        data = _object(row, "quarantine_ledger_invalid")
        if set(data) != {"count", "keys_sha256"} or isinstance(data.get("count"), bool) or not isinstance(data.get("count"), int) or data["count"] < 0:
            raise CustodyError("quarantine_ledger_invalid")
        _token(data.get("keys_sha256"), "quarantine_ledger_invalid")
    _token(ledger.get("desired_context_values_sha256"), "quarantine_ledger_invalid")
    bound_keys = ("schema", "dataset_revision_sha256", "desired_context_values_sha256", "matching_semantics", "reasons") if ledger.get("schema") == "aerp7-convomem-quarantine-ledger-v3" else ("schema", "dataset_revision_sha256", "desired_context_values_sha256", "reasons")
    if ledger.get("ledger_sha256") != canonical_sha256({key: ledger[key] for key in bound_keys}):
        raise CustodyError("quarantine_ledger_invalid")
    return dict(ledger)


def _verify_streaming_sources(canonical_root: Path, premix_root: Path, index: StreamingIndex) -> None:
    """Re-list and re-hash the exact source set immediately before publish."""
    for role, root, code in (("canonical", canonical_root, "canonical_source_drift"), ("premix", premix_root, "premix_source_drift")):
        expected = [row for row in index.source_receipt if row["role"] == role]
        files = _files(root)
        if len(files) != len(expected) or [path.relative_to(root).as_posix() for path in files] != [row["locator"] for row in expected]:
            raise CustodyError(code)
        for path, row in zip(files, expected):
            _raw, identity, digest = _source_snapshot(path, code, retain=False)
            if list(identity) != row["identity"] or path.stat().st_size != row["size"] or digest != row["sha256"]:
                raise CustodyError(code)


def _indexed_context_values(database: Path) -> list[float]:
    connection = _index_connection(database)
    try:
        values = [float(row[0]) for row in connection.execute("SELECT DISTINCT context_size FROM premix_cases ORDER BY context_size")]
    finally:
        connection.close()
    if not values:
        raise CustodyError("premix_context_values_empty")
    return values


def _bad_key_set(database: Path) -> set[str]:
    """Derive exactly the key classes that selection is forbidden to use."""
    connection = _index_connection(database)
    try:
        queries = (
            "SELECT key_sha FROM canonical_items GROUP BY key_sha HAVING count(*) != 1",
            "SELECT c.key_sha FROM canonical_items c LEFT JOIN premix_keys p ON p.key_sha=c.key_sha GROUP BY c.key_sha HAVING count(p.case_sha)=0",
            "SELECT c.key_sha FROM canonical_items c JOIN premix_keys p ON p.key_sha=c.key_sha JOIN premix_cases pc ON pc.case_sha=p.case_sha GROUP BY c.key_sha, pc.context_size HAVING count(DISTINCT pc.case_sha)>1",
        )
        bad = set()
        for query in queries:
            bad.update(row[0] for row in connection.execute(query))
        return bad
    finally:
        connection.close()


def _selection_rows_sql(database: Path, secret: bytes, revision: str, config: SelectionConfig, desired_contexts: Sequence[float], ledger: Mapping[str, Any]) -> tuple[Sequence[tuple[dict[str, Any], dict[str, Any]]] | CensusSelectionReference, dict[str, Any]]:
    """Choose only SQL-indexed, globally eligible rows; raw labels/messages stay unread."""
    config.validate()
    connection = _index_connection(database)
    connection.row_factory = sqlite3.Row
    try:
        groups = [row[0] for row in connection.execute("SELECT DISTINCT json_extract(directory, '$.group') FROM canonical_items ORDER BY 1")]
        if not groups or any(group is None for group in groups):
            raise CustodyError("canonical_group_invalid")
        connection.execute("CREATE TEMP TABLE forbidden_keys(key_sha TEXT PRIMARY KEY)")
        forbidden_queries = [
            "INSERT OR IGNORE INTO forbidden_keys SELECT key_sha FROM canonical_items GROUP BY key_sha HAVING count(*) != 1",
            "INSERT OR IGNORE INTO forbidden_keys SELECT c.key_sha FROM canonical_items c LEFT JOIN premix_keys p ON p.key_sha=c.key_sha GROUP BY c.key_sha HAVING count(p.case_sha)=0",
        ]
        if not config.is_census_v1:
            forbidden_queries.append("INSERT OR IGNORE INTO forbidden_keys SELECT c.key_sha FROM canonical_items c JOIN premix_keys p ON p.key_sha=c.key_sha JOIN premix_cases pc ON pc.case_sha=p.case_sha GROUP BY c.key_sha, pc.context_size HAVING count(DISTINCT pc.case_sha)>1")
        for query in forbidden_queries:
            connection.execute(query)
        if not config.is_census_v1:
            # Sample selection retains its strict complete-context eligibility
            # contract.  The formal census instead publishes all observed
            # pairs and binds their non-rectangular structure in its receipt.
            missing: list[str] = []
            for row in connection.execute("SELECT DISTINCT key_sha FROM canonical_items"):
                available = {float(value[0]) for value in connection.execute("SELECT DISTINCT pc.context_size FROM premix_keys p JOIN premix_cases pc ON pc.case_sha=p.case_sha WHERE p.key_sha=?", (row[0],))}
                if any(float(size) not in available for size in desired_contexts):
                    missing.append(row[0])
            missing = sorted(missing)
            if canonical_sha256(missing) != ledger["reasons"]["missing_requested_context_sizes"]["keys_sha256"]:
                raise CustodyError("quarantine_ledger_drift")
            connection.executemany("INSERT OR IGNORE INTO forbidden_keys VALUES(?)", ((key,) for key in missing))
        if config.is_census_v1:
            nonzero = {reason: int(data["count"]) for reason, data in ledger["reasons"].items() if int(data["count"]) != 0}
            if nonzero:
                # A census cannot turn known malformed / unmatched official
                # units into a zeroed "exclusion" statistic.  The committed
                # ledger names every failed class by count and opaque digest.
                raise CrosswalkError("census_quarantine_nonempty", counts=nonzero, ledger_sha256=ledger["ledger_sha256"])
            forbidden_count = connection.execute("SELECT count(*) FROM forbidden_keys").fetchone()[0]
            if forbidden_count:
                raise CrosswalkError("census_candidate_custody_crosswalk_invalid", forbidden_key_count=forbidden_count)
            # Every canonical query is a member of the estimand.  The frozen
            # relation holds only rowids; canonical/case payloads are decoded
            # later, one pair at a time, by the materializer cursor.
            connection.executescript("""
                CREATE TABLE selected_pairs(
                    ordinal INTEGER PRIMARY KEY,
                    canonical_rowid INTEGER NOT NULL,
                    case_rowid INTEGER NOT NULL,
                    UNIQUE(canonical_rowid, case_rowid)
                );
                CREATE INDEX selected_pairs_canonical ON selected_pairs(canonical_rowid);
                CREATE INDEX selected_pairs_case ON selected_pairs(case_rowid);
            """)
            connection.execute("""
                INSERT INTO selected_pairs(ordinal, canonical_rowid, case_rowid)
                WITH pairs AS (
                    SELECT DISTINCT c.rowid AS canonical_rowid, pc.rowid AS case_rowid,
                        c.persona AS persona, json_extract(c.directory, '$.group') AS directory_group,
                        c.locator AS locator, c.ordinal AS canonical_ordinal,
                        pc.context_size AS context_size, pc.case_sha AS case_sha
                    FROM canonical_items c
                    JOIN premix_keys p ON p.key_sha=c.key_sha
                    JOIN premix_cases pc ON pc.case_sha=p.case_sha
                    WHERE NOT EXISTS (SELECT 1 FROM forbidden_keys f WHERE f.key_sha=c.key_sha)
                )
                SELECT row_number() OVER (ORDER BY persona, directory_group, locator, canonical_ordinal, context_size, case_sha), canonical_rowid, case_rowid
                FROM pairs
            """)
            selected_count = connection.execute("SELECT count(*) FROM selected_pairs").fetchone()[0]
            if selected_count <= 0:
                raise CustodyError("census_selection_empty")
            connection.execute("CREATE TEMP TABLE selected_personas(persona_id TEXT PRIMARY KEY)")
            for persona_row in connection.execute("SELECT DISTINCT c.persona FROM selected_pairs s JOIN canonical_items c ON c.rowid=s.canonical_rowid"):
                connection.execute("INSERT INTO selected_personas VALUES(?)", (_opaque(secret, revision, "persona", persona_row[0]),))
            def array_sha(query: str, mapper: Callable[[sqlite3.Row], Any] = lambda row: row[0]) -> str:
                return _stream_array_sha256((_bytes(mapper(row)) for row in connection.execute(query)))
            personas_sha = array_sha("SELECT persona_id FROM selected_personas ORDER BY persona_id")
            variants_sha = array_sha("SELECT pc.case_sha FROM selected_pairs s JOIN premix_cases pc ON pc.rowid=s.case_rowid ORDER BY s.ordinal")
            persona_count = connection.execute("SELECT count(*) FROM selected_personas").fetchone()[0]
            query_count = connection.execute("SELECT count(*) FROM canonical_items").fetchone()[0]
            indexed_contexts = [float(row[0]) for row in connection.execute("SELECT DISTINCT context_size FROM premix_cases ORDER BY context_size")]
            all_tiers = [row[0] for row in connection.execute("SELECT DISTINCT json_extract(directory, '$.tier') FROM canonical_items ORDER BY 1")]
            receipt = {
                "algorithm": CENSUS_SELECTION_ALGORITHM,
                "seed": None,
                "persona_quota": "ALL",
                "per_persona_group_quota": "ALL",
                "context_rank_indices": "ALL_AVAILABLE_SORTED",
                "context_rank_semantics": "all_observed_item_context_pairs",
                # The public validator recomputes this from opaque IDs; the
                # selector's raw-persona traversal must not leak into or alter
                # the census seal.
                "selected_persona_ids_sha256": personas_sha,
                "holdout_persona_set_sha256": personas_sha,
                "group_values_sha256": canonical_sha256(groups),
                "tier_values_sha256": canonical_sha256(all_tiers),
                "context_values_sha256": canonical_sha256(indexed_contexts),
                "desired_context_values_sha256": canonical_sha256(list(desired_contexts)),
                "variant_selection_sha256": variants_sha,
                "selected_item_context_count": selected_count,
                "candidate_visible_query_count": query_count,
                "candidate_visible_persona_count": persona_count,
                "candidate_visible_context_count": len(desired_contexts),
                "denominators_sha256": canonical_sha256({"query_count": selected_count, "candidate_visible_query_count": query_count, "persona_count": persona_count, "context_count": len(desired_contexts)}),
                "item_supplement_count": 0,
                "observed_crosswalk": {
                    "semantics": CENSUS_CROSSWALK_SEMANTICS,
                    "multi_persona_corpus_count": connection.execute("SELECT count(*) FROM (SELECT s.case_rowid FROM selected_pairs s JOIN canonical_items c ON c.rowid=s.canonical_rowid GROUP BY s.case_rowid HAVING count(DISTINCT c.persona)>1)").fetchone()[0],
                    "sparse_query_context_count": connection.execute("SELECT count(*) FROM (SELECT s.canonical_rowid FROM selected_pairs s JOIN premix_cases pc ON pc.rowid=s.case_rowid GROUP BY s.canonical_rowid HAVING count(DISTINCT pc.context_size) != ?)", (len(desired_contexts),)).fetchone()[0],
                    "multi_case_query_context_count": connection.execute("SELECT count(*) FROM (SELECT s.canonical_rowid, pc.context_size FROM selected_pairs s JOIN premix_cases pc ON pc.rowid=s.case_rowid GROUP BY s.canonical_rowid, pc.context_size HAVING count(DISTINCT s.case_rowid)>1)").fetchone()[0],
                },
                "exclusion_counts": {
                    "multi_persona_cases": ledger["reasons"]["multi_persona_cases"]["count"],
                    "missing_crosswalk": ledger["reasons"]["canonical_zero_logical_matches"]["count"],
                    "ambiguous_canonical_keys": ledger["reasons"]["ambiguous_canonical_keys"]["count"],
                    "unmatched_premix_keys": ledger["reasons"]["unmatched_premix_keys"]["count"],
                    "multiple_logical_matches_or_variants": ledger["reasons"]["multiple_logical_matches_or_variants"]["count"],
                    "missing_requested_context_sizes": ledger["reasons"]["missing_requested_context_sizes"]["count"],
                },
                "quarantine_reason_digests": {reason: ledger["reasons"][reason]["keys_sha256"] for reason in ledger["reasons"]},
                "quarantine_ledger_sha256": ledger["ledger_sha256"],
            }
            connection.commit()
            return CensusSelectionReference(database=database, item_count=selected_count), receipt
        rows = connection.execute(
            "SELECT c.key_sha, c.locator, c.ordinal, c.persona, c.question, c.directory FROM canonical_items c WHERE NOT EXISTS (SELECT 1 FROM forbidden_keys f WHERE f.key_sha=c.key_sha)",
        ).fetchall()
        by_persona: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_persona.setdefault(row["persona"], []).append(row)
        eligible: list[tuple[str, list[sqlite3.Row]]] = []
        for persona, persona_rows in by_persona.items():
            if {json.loads(row["directory"])["group"] for row in persona_rows} == set(groups):
                eligible.append((persona, persona_rows))
        ordered = sorted(eligible, key=lambda pair: (_rank(secret, revision, config.seed, "persona", _opaque(secret, revision, "persona", pair[0])), _opaque(secret, revision, "persona", pair[0])))
        selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
        selected_personas: list[str] = []
        variants: list[str] = []
        supplements = 0
        for persona, persona_rows in ordered:
            if len(selected_personas) == config.persona_quota:
                break
            persona_id = _opaque(secret, revision, "persona", persona)
            persona_selected: list[tuple[dict[str, Any], dict[str, Any]]] = []
            persona_variants: list[str] = []
            viable = True
            for group in groups:
                group_rows = [row for row in persona_rows if json.loads(row["directory"])["group"] == group]
                by_tier: dict[str | None, list[sqlite3.Row]] = {}
                for row in group_rows:
                    by_tier.setdefault(json.loads(row["directory"])["tier"], []).append(row)
                tiers = sorted(by_tier, key=lambda tier: (_rank(secret, revision, config.seed, "tier", [persona_id, group, tier]), str(tier)))
                ordered_items = {tier: sorted(value, key=lambda row: (_rank(secret, revision, config.seed, "item", _opaque(secret, revision, "canonical-item", {"path": row["locator"], "ordinal": row["ordinal"]})), row["locator"], row["ordinal"])) for tier, value in by_tier.items()}
                picked = 0
                while picked < config.per_persona_group_quota:
                    progressed = False
                    for tier in tiers:
                        if not ordered_items[tier]:
                            continue
                        row = ordered_items[tier].pop(0)
                        cases = connection.execute(
                            "SELECT pc.case_sha, pc.locator, pc.context_size FROM premix_keys p JOIN premix_cases pc ON pc.case_sha=p.case_sha WHERE p.key_sha=? AND pc.context_size IN (" + ",".join("?" for _ in desired_contexts) + ") ORDER BY pc.context_size, pc.case_sha",
                            (row["key_sha"], *desired_contexts),
                        ).fetchall()
                        if len(cases) != len(desired_contexts) or {float(case["context_size"]) for case in cases} != {float(size) for size in desired_contexts}:
                            raise CustodyError("eligible_context_lookup_invalid")
                        canonical = {"key_sha": row["key_sha"], "locator": row["locator"], "ordinal": row["ordinal"], "persona": persona, "persona_id": persona_id, "question": row["question"], "directory": json.loads(row["directory"])}
                        for case in cases:
                            item_case = {"case_sha": case["case_sha"], "locator": json.loads(case["locator"]), "context_size": float(case["context_size"])}
                            persona_selected.append((canonical, item_case)); persona_variants.append(case["case_sha"])
                        picked += 1; progressed = True
                        if picked == config.per_persona_group_quota:
                            break
                    if not progressed:
                        viable = False; break
                if not viable:
                    break
            if viable:
                selected.extend(persona_selected); variants.extend(persona_variants); selected_personas.append(persona_id)
            else:
                supplements += 1
        if len(selected_personas) != config.persona_quota:
            raise CustodyError("persona_quota_unavailable", available=len(selected_personas), quota=config.persona_quota)
        quarantine_counts = {"multi_persona_cases": ledger["reasons"]["multi_persona_cases"]["count"], "missing_crosswalk": ledger["reasons"]["canonical_zero_logical_matches"]["count"], "ambiguous_canonical_keys": ledger["reasons"]["ambiguous_canonical_keys"]["count"], "unmatched_premix_keys": ledger["reasons"]["unmatched_premix_keys"]["count"], "multiple_logical_matches_or_variants": ledger["reasons"]["multiple_logical_matches_or_variants"]["count"], "missing_requested_context_sizes": ledger["reasons"]["missing_requested_context_sizes"]["count"]}
        all_tiers = [row[0] for row in connection.execute("SELECT DISTINCT json_extract(directory, '$.tier') FROM canonical_items ORDER BY 1")]
        receipt = {"algorithm": SELECTION_ALGORITHM, "seed": config.seed, "persona_quota": config.persona_quota, "per_persona_group_quota": config.per_persona_group_quota, "context_rank_indices": list(config.context_rank_indices), "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": canonical_sha256(selected_personas), "holdout_persona_set_sha256": canonical_sha256(sorted(selected_personas)), "group_values_sha256": canonical_sha256(groups), "tier_values_sha256": canonical_sha256(all_tiers), "context_values_sha256": canonical_sha256(_indexed_context_values(database)), "desired_context_values_sha256": canonical_sha256(list(desired_contexts)), "variant_selection_sha256": canonical_sha256(variants), "selected_item_context_count": len(selected), "item_supplement_count": supplements, "exclusion_counts": quarantine_counts, "quarantine_reason_digests": {reason: ledger["reasons"][reason]["keys_sha256"] for reason in ledger["reasons"]}, "quarantine_ledger_sha256": ledger["ledger_sha256"]}
        return selected, receipt
    finally:
        connection.close()


def _iter_selected_payloads_sql(database: Path, selected: Sequence[tuple[dict[str, Any], dict[str, Any]]] | CensusSelectionReference, secret: bytes, revision: str) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Yield one decoded selected payload at a time; callers choose retention."""
    connection = _index_connection(database)
    connection.row_factory = sqlite3.Row
    try:
        if isinstance(selected, CensusSelectionReference):
            if selected.database != database:
                raise CustodyError("census_selection_reference_database_drift")
            selected_rows: Iterator[Any] = iter(connection.execute("""
                SELECT c.key_sha, c.locator, c.ordinal, c.persona, c.question, c.directory,
                    c.answer, c.category, c.conversations, c.labels,
                    pc.case_sha, pc.locator AS case_locator, pc.context_size, pc.messages
                FROM selected_pairs s
                JOIN canonical_items c ON c.rowid=s.canonical_rowid
                JOIN premix_cases pc ON pc.rowid=s.case_rowid
                ORDER BY s.ordinal
            """))
            for selected_row in selected_rows:
                item = {"key_sha": selected_row["key_sha"], "locator": selected_row["locator"], "ordinal": selected_row["ordinal"], "persona": selected_row["persona"], "persona_id": _opaque(secret, revision, "persona", selected_row["persona"]), "question": selected_row["question"], "directory": json.loads(selected_row["directory"])}
                case = {"case_sha": selected_row["case_sha"], "locator": json.loads(selected_row["case_locator"]), "context_size": float(selected_row["context_size"])}
                canonical_row = selected_row
                case_row = selected_row
                yield from _selected_payload_row(item, case, canonical_row, case_row, secret, revision)
            return
        for item, case in selected:
            canonical_row = connection.execute("SELECT answer, category, conversations, labels, directory FROM canonical_items WHERE key_sha=? AND locator=? AND ordinal=?", (item["key_sha"], item["locator"], item["ordinal"])).fetchone()
            case_row = connection.execute("SELECT messages FROM premix_cases WHERE case_sha=?", (case["case_sha"],)).fetchone()
            if canonical_row is None or case_row is None:
                raise CustodyError("selected_index_row_missing")
            yield from _selected_payload_row(item, case, canonical_row, case_row, secret, revision)
    finally:
        connection.close()


def _selected_payload_row(item: Mapping[str, Any], case: Mapping[str, Any], canonical_row: Mapping[str, Any], case_row: Mapping[str, Any], secret: bytes, revision: str) -> Iterator[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]:
    """Build one selected pair; shared by legacy and persistent cursors."""
    locator = {"path": item["locator"], "ordinal": item["ordinal"]}
    canonical_id = _opaque(secret, revision, "canonical-item", locator)
    messages = json.loads(case_row["messages"])
    corpus_id = _opaque(secret, revision, "corpus", case["locator"])
    candidates = []
    for message in messages:
        message_id = _opaque(secret, revision, "message", {"conversation_id": message["conversation_id"], "message_ordinal": message["raw_message_ordinal"]})
        candidates.append({"message_id": message_id, "opaque_conversation_id": _opaque(secret, revision, "conversation", message["conversation_id"]), "conversation_order": message["conversation_ordinal"], "message_order": message["message_ordinal"], "corpus_order": message["corpus_ordinal"], "speaker": message["speaker"], "text": message["text"]})
    declared_context_size = case["context_size"]
    if isinstance(declared_context_size, bool) or not isinstance(declared_context_size, (int, float)) or not float(declared_context_size).is_integer() or int(declared_context_size) <= 0:
        raise CustodyError("premix_context_size_invalid")
    corpus = {"corpus_id": corpus_id, "declared_context_size": int(declared_context_size), "actual_conversation_count": len({message["conversation_id"] for message in messages}), "actual_message_count": len(messages), "candidates": candidates}
    context_id = _opaque(secret, revision, "item-context", {"canonical_item_id": canonical_id, "corpus_id": corpus_id})
    directory = json.loads(canonical_row["directory"])
    projection_item = {"item_id": context_id, "persona_id": item["persona_id"], "query_text": item["question"], "corpus_id": corpus_id, "selection_logical_item_id": _opaque(secret, revision, "selection-logical-item", locator), "selection_logical_binding_witness": _opaque(secret, revision, "selection-logical-binding-witness", locator), "selection_group_id": _opaque(secret, revision, "selection-group", item["directory"]["group"]), "selection_tier_id": _opaque(secret, revision, "selection-tier", directory["tier"]), "selection_variant_id": _opaque(secret, revision, "selection-variant", case["case_sha"])}
    if len(candidates) != len(messages):
        raise CustodyError("selected_message_count_invalid")
    evidence_conversation_ids = [_opaque(secret, revision, "conversation", conversation_id) for conversation_id in json.loads(canonical_row["conversations"])]
    candidate_conversation_ids = {candidate["opaque_conversation_id"] for candidate in candidates}
    if not set(evidence_conversation_ids) <= candidate_conversation_ids:
        raise CustodyError("evidence_conversation_not_in_corpus")
    custody_item = {"item_id": context_id, "canonical_item_id": canonical_id, "persona_id": item["persona_id"], "persona_source_id": item["persona"], "corpus_id": corpus_id, "source_locator": {"canonical": locator, "case": case["locator"]}, "directory": directory, "labels": {"answer": canonical_row["answer"], "message_evidences": json.loads(canonical_row["labels"])}, "evidence_conversation_ids": evidence_conversation_ids, "messages": [{"message_id": candidate["message_id"], "source_locator": message["source_locator"]} for candidate, message in zip(candidates, messages)]}
    yield corpus, projection_item, custody_item


def _selected_payloads_sql(database: Path, selected: Sequence[tuple[dict[str, Any], dict[str, Any]]], secret: bytes, revision: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compatibility materializer for small callers; formal publishing streams."""
    corpora: dict[str, dict[str, Any]] = {}
    projection_items: list[dict[str, Any]] = []; custody_items: list[dict[str, Any]] = []
    for corpus, projection_item, custody_item in _iter_selected_payloads_sql(database, selected, secret, revision):
        corpora.setdefault(corpus["corpus_id"], corpus)
        projection_items.append(projection_item); custody_items.append(custody_item)
    return [corpora[key] for key in sorted(corpora)], projection_items, custody_items


def _stream_array_sha256(rows: Iterator[bytes]) -> str:
    """Digest a canonical JSON array without retaining its rows."""
    digest = hashlib.sha256(); digest.update(b"["); first = True
    for row in rows:
        if not first: digest.update(b",")
        digest.update(row); first = False
    digest.update(b"]")
    return digest.hexdigest()


def _persist_census_payloads_sql(database: Path, selected: Sequence[tuple[dict[str, Any], dict[str, Any]]] | CensusSelectionReference, secret: bytes, revision: str) -> int:
    """Spool exact public rows in SQLite; no selected payload survives a turn."""
    connection = _index_connection(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE materialized_corpora(corpus_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE materialized_projection_items(ordinal INTEGER PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE materialized_custody_items(ordinal INTEGER PRIMARY KEY, payload TEXT NOT NULL);
        """)
        count = 0
        for ordinal, (corpus, projection_item, custody_item) in enumerate(_iter_selected_payloads_sql(database, selected, secret, revision)):
            custody_item["binding_commitment"] = _custody_item_commitment(secret, revision, custody_item, projection_item, corpus)
            connection.execute("INSERT OR IGNORE INTO materialized_corpora VALUES(?,?)", (corpus["corpus_id"], _bytes(corpus).decode("utf-8")))
            connection.execute("INSERT INTO materialized_projection_items VALUES(?,?)", (ordinal, _bytes(projection_item).decode("utf-8")))
            connection.execute("INSERT INTO materialized_custody_items VALUES(?,?)", (ordinal, _bytes(custody_item).decode("utf-8")))
            count += 1
        connection.commit()
        if count == 0:
            raise CustodyError("census_selection_empty")
        return count
    finally:
        connection.close()


def _stream_sql_rows(write: Callable[[bytes], Any], connection: sqlite3.Connection, query: str) -> None:
    write(b"["); first = True
    for row in connection.execute(query):
        if not first: write(b",")
        write(row[0].encode("utf-8")); first = False
    write(b"]")


def _stream_census_projection(write: Callable[[bytes], Any], database: Path, dataset: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    connection = _index_connection(database)
    try:
        write(b'{"corpora":'); _stream_sql_rows(write, connection, "SELECT payload FROM materialized_corpora ORDER BY corpus_id")
        write(b',"dataset":'); write(_bytes(dataset))
        write(b',"items":'); _stream_sql_rows(write, connection, "SELECT payload FROM materialized_projection_items ORDER BY ordinal")
        write(b',"schema":'); write(_bytes(SCHEMA))
        write(b',"selection_receipt":'); write(_bytes(receipt)); write(b"}")
    finally:
        connection.close()


def _stream_census_custody_without_binding(write: Callable[[bytes], Any], database: Path, dataset: Mapping[str, Any], receipt: Mapping[str, Any], projection_sha256: str, count: int) -> None:
    mapping_status = {"status": "not_attempted", "scoring_permitted": False, "reason": "aerp7_prelabel_slice_has_no_span_mapping", "unresolved_item_context_count": count}
    connection = _index_connection(database)
    try:
        write(b'{"dataset":'); write(_bytes(dataset))
        write(b',"items":'); _stream_sql_rows(write, connection, "SELECT payload FROM materialized_custody_items ORDER BY ordinal")
        write(b',"mapping_status":'); write(_bytes(mapping_status))
        write(b',"projection_sha256":'); write(_bytes(projection_sha256))
        write(b',"schema":'); write(_bytes(CUSTODY_SCHEMA))
        write(b',"selection_receipt":'); write(_bytes(receipt)); write(b"}")
    finally:
        connection.close()


def _stream_census_custody(write: Callable[[bytes], Any], database: Path, dataset: Mapping[str, Any], receipt: Mapping[str, Any], projection_sha256: str, count: int, commitment: str) -> None:
    binding = {"algorithm": BOUND_CUSTODY_ALGORITHM, "revision_sha256": dataset["revision_sha256"], "commitment": commitment}
    mapping_status = {"status": "not_attempted", "scoring_permitted": False, "reason": "aerp7_prelabel_slice_has_no_span_mapping", "unresolved_item_context_count": count}
    connection = _index_connection(database)
    try:
        write(b'{"binding":'); write(_bytes(binding))
        write(b',"dataset":'); write(_bytes(dataset))
        write(b',"items":'); _stream_sql_rows(write, connection, "SELECT payload FROM materialized_custody_items ORDER BY ordinal")
        write(b',"mapping_status":'); write(_bytes(mapping_status))
        write(b',"projection_sha256":'); write(_bytes(projection_sha256))
        write(b',"schema":'); write(_bytes(CUSTODY_SCHEMA))
        write(b',"selection_receipt":'); write(_bytes(receipt)); write(b"}")
    finally:
        connection.close()


def _stream_to_file(path: Path, emit: Callable[[Callable[[bytes], Any]], None]) -> str:
    digest = hashlib.sha256()
    with path.open("xb") as stream:
        def write(value: bytes) -> None:
            stream.write(value); digest.update(value)
        emit(write); stream.flush(); os.fsync(stream.fileno())
    return digest.hexdigest()


def _census_payload_sha256(emit: Callable[[Callable[[bytes], Any]], None]) -> str:
    digest = hashlib.sha256(); emit(digest.update); return digest.hexdigest()


def _spooled_census_receipt(database: Path, receipt: dict[str, Any], revision: str) -> dict[str, Any]:
    """Bind census denominators directly to persisted candidate-safe rows."""
    connection = _index_connection(database)
    try:
        def array_sha(query: str, mapper: Callable[[sqlite3.Row], Any], parameters: tuple[Any, ...] = ()) -> str:
            return _stream_array_sha256((_bytes(mapper(row)) for row in connection.execute(query, parameters)))
        corpus_ids = array_sha("SELECT corpus_id FROM materialized_corpora ORDER BY corpus_id", lambda row: row[0])
        item_ids = array_sha("SELECT json_extract(payload, '$.item_id') FROM materialized_projection_items ORDER BY json_extract(payload, '$.item_id')", lambda row: row[0])
        personas = array_sha("SELECT DISTINCT json_extract(payload, '$.persona_id') FROM materialized_projection_items ORDER BY 1", lambda row: row[0])
        groups = array_sha("SELECT DISTINCT json_extract(payload, '$.selection_group_id') FROM materialized_projection_items ORDER BY 1", lambda row: row[0])
        tiers = array_sha("SELECT DISTINCT json_extract(payload, '$.selection_tier_id') FROM materialized_projection_items ORDER BY 1", lambda row: row[0])
        variants = array_sha("SELECT json_extract(payload, '$.selection_variant_id') FROM materialized_projection_items ORDER BY 1", lambda row: row[0])
        pairs = array_sha("SELECT payload FROM materialized_projection_items ORDER BY json_extract(payload, '$.selection_logical_item_id'), json_extract(payload, '$.selection_variant_id'), json_extract(payload, '$.item_id'), json_extract(payload, '$.corpus_id')", lambda row: {key: json.loads(row[0])[key] for key in ("selection_logical_item_id", "selection_logical_binding_witness", "selection_variant_id", "item_id", "corpus_id")})
        contexts = [int(row[0]) for row in connection.execute("SELECT DISTINCT json_extract(payload, '$.declared_context_size') FROM materialized_corpora ORDER BY 1")]
        per_context = []
        for rank, context in enumerate(contexts):
            item_query = "SELECT json_extract(p.payload, '$.item_id') FROM materialized_projection_items p JOIN materialized_corpora c ON json_extract(p.payload, '$.corpus_id')=c.corpus_id WHERE json_extract(c.payload, '$.declared_context_size')=? ORDER BY 1"
            count = connection.execute("SELECT count(*) FROM (" + item_query + ")", (context,)).fetchone()[0]
            per_context.append({"context_rank": rank, "declared_context_size": context, "item_count": count, "item_ids_sha256": array_sha(item_query, lambda row: row[0], (context,))})
        item_count = connection.execute("SELECT count(*) FROM materialized_projection_items").fetchone()[0]
        query_count = connection.execute("SELECT count(DISTINCT json_extract(payload, '$.selection_logical_item_id')) FROM materialized_projection_items").fetchone()[0]
        persona_count = connection.execute("SELECT count(DISTINCT json_extract(payload, '$.persona_id')) FROM materialized_projection_items").fetchone()[0]
        group_count = connection.execute("SELECT count(DISTINCT json_extract(payload, '$.selection_group_id')) FROM materialized_projection_items").fetchone()[0]
        corpus_count = connection.execute("SELECT count(*) FROM materialized_corpora").fetchone()[0]
        observed = {
            "semantics": CENSUS_CROSSWALK_SEMANTICS,
            "multi_persona_corpus_count": connection.execute("SELECT count(*) FROM (SELECT json_extract(payload, '$.corpus_id') FROM materialized_projection_items GROUP BY 1 HAVING count(DISTINCT json_extract(payload, '$.persona_id')) > 1)").fetchone()[0],
            "sparse_query_context_count": connection.execute("SELECT count(*) FROM (SELECT json_extract(p.payload, '$.selection_logical_item_id') FROM materialized_projection_items p JOIN materialized_corpora c ON json_extract(p.payload, '$.corpus_id')=c.corpus_id GROUP BY 1 HAVING count(DISTINCT json_extract(c.payload, '$.declared_context_size')) != ?)", (len(contexts),)).fetchone()[0],
            "multi_case_query_context_count": connection.execute("SELECT count(*) FROM (SELECT json_extract(p.payload, '$.selection_logical_item_id'), json_extract(p.payload, '$.corpus_id') FROM materialized_projection_items p JOIN materialized_corpora c ON json_extract(p.payload, '$.corpus_id')=c.corpus_id GROUP BY json_extract(p.payload, '$.selection_logical_item_id'), json_extract(c.payload, '$.declared_context_size') HAVING count(DISTINCT json_extract(p.payload, '$.corpus_id')) > 1)").fetchone()[0],
        }
    finally:
        connection.close()
    empty = canonical_sha256([])
    reasons = {reason: {"count": 0, "keys_sha256": empty} for reason in ("ambiguous_canonical_keys", "unmatched_premix_keys", "canonical_zero_logical_matches", "multiple_logical_matches_or_variants", "missing_requested_context_sizes", "multi_persona_cases")}
    ledger = {"schema": "aerp7-convomem-quarantine-ledger-v3", "dataset_revision_sha256": revision, "desired_context_values_sha256": canonical_sha256([float(context) for context in contexts]), "matching_semantics": CENSUS_CROSSWALK_SEMANTICS, "reasons": reasons}
    receipt.update({
        "selected_persona_ids_sha256": personas, "holdout_persona_set_sha256": personas,
        "group_values_sha256": groups, "tier_values_sha256": tiers,
        "context_values_sha256": canonical_sha256(contexts), "desired_context_values_sha256": canonical_sha256(contexts),
        "variant_selection_sha256": variants, "selected_item_context_count": item_count,
        "candidate_visible_query_count": query_count, "candidate_visible_persona_count": persona_count,
        "candidate_visible_context_count": len(contexts), "candidate_visible_corpus_count": corpus_count,
        "group_count": group_count, "selected_item_ids_sha256": item_ids, "per_context_denominators": per_context,
        "logical_variant_pairs_sha256": pairs, "corpus_ids_sha256": corpus_ids, "observed_crosswalk": observed,
        "quarantine_reason_digests": {reason: empty for reason in reasons},
        "quarantine_ledger_sha256": canonical_sha256(ledger),
    })
    receipt["denominators_sha256"] = canonical_sha256({"query_count": receipt["selected_item_context_count"], "candidate_visible_query_count": query_count, "persona_count": persona_count, "context_count": len(contexts), "corpus_count": corpus_count, "group_count": group_count, "logical_variant_pairs_sha256": pairs, "corpus_ids_sha256": corpus_ids, "per_context_denominators": per_context})
    return receipt


def _spool_census_publications(staging_root: Path, database: Path, dataset: Mapping[str, Any], receipt: Mapping[str, Any], secret: bytes, count: int) -> tuple[Path, str, Path, str]:
    candidate_fd, candidate_name = tempfile.mkstemp(prefix=".aerp7-census-projection-", suffix=".json", dir=staging_root)
    os.close(candidate_fd); candidate_path = Path(candidate_name); candidate_path.unlink()
    try:
        projection_sha256 = _stream_to_file(candidate_path, lambda write: _stream_census_projection(write, database, dataset, receipt))
        binding = hmac.new(secret, digestmod=hashlib.sha256)
        binding.update(b'{"domain":'); binding.update(_bytes("custody-binding")); binding.update(b',"identity":{"projection":')
        _stream_census_projection(binding.update, database, dataset, receipt)
        binding.update(b',"sealed_custody":')
        _stream_census_custody_without_binding(binding.update, database, dataset, receipt, projection_sha256, count)
        binding.update(b'},"revision":'); binding.update(_bytes(dataset["revision_sha256"])); binding.update(b"}")
        commitment = binding.hexdigest()
        custody_fd, custody_name = tempfile.mkstemp(prefix=".aerp7-census-custody-", suffix=".json", dir=staging_root)
        os.close(custody_fd); custody_path = Path(custody_name); custody_path.unlink()
        try:
            custody_sha256 = _stream_to_file(custody_path, lambda write: _stream_census_custody(write, database, dataset, receipt, projection_sha256, count, commitment))
        except Exception:
            custody_path.unlink(missing_ok=True); raise
        return candidate_path, projection_sha256, custody_path, custody_sha256
    except Exception:
        candidate_path.unlink(missing_ok=True); raise


def _pinned_sources(root: Path, code: str) -> tuple[list[dict[str, Any]], str]:
    """One read snapshot supplies both digest and parse; later state drift is fatal."""
    snapshot = []
    for path in _files(root):
        raw, identity, digest = _snapshot(path, code)
        snapshot.append({"path": path, "locator": path.relative_to(root).as_posix(), "raw": raw, "identity": identity, "sha256": digest})
    return snapshot, canonical_sha256([{"locator": row["locator"], "sha256": row["sha256"]} for row in snapshot])


def _verify_pinned_sources(root: Path, snapshot: Sequence[dict[str, Any]], code: str) -> None:
    current = _files(root)
    if [path.relative_to(root).as_posix() for path in current] != [row["locator"] for row in snapshot]:
        raise CustodyError(code)
    for row in snapshot:
        _raw, identity, digest = _snapshot(row["path"], code, retain=False)
        if identity != row["identity"] or digest != row["sha256"]: raise CustodyError(code)


def _durability(value: Any) -> Mapping[str, Any]:
    durability = _object(value, "ready_durability_invalid")
    if set(durability) != {"platform", "directory_fsync_guaranteed", "steps"} or not isinstance(durability.get("platform"), str) or not isinstance(durability.get("directory_fsync_guaranteed"), bool) or not isinstance(durability.get("steps"), list) or any(not isinstance(step, str) for step in durability["steps"]):
        raise CustodyError("ready_durability_invalid")
    if durability["platform"] == "linux" and (not durability["directory_fsync_guaranteed"] or durability["steps"] != ["payload_entries", "ready_hardlink", "ready_temp_unlink"]):
        raise CustodyError("ready_durability_invalid")
    if durability["platform"] != "linux" and durability["directory_fsync_guaranteed"]:
        raise CustodyError("ready_durability_invalid")
    return durability


def _candidate_ready(value: Any) -> Mapping[str, Any]:
    row = _object(value, "ready_invalid")
    if set(row) != {"schema", "generation_id", "projection", "durability"} or row.get("schema") != CANDIDATE_READY_SCHEMA:
        raise CustodyError("ready_schema_invalid")
    _token(row.get("generation_id"), "ready_generation_invalid")
    file = _object(row.get("projection"), "ready_file_invalid")
    if set(file) != {"raw_sha256", "canonical_sha256"}: raise CustodyError("ready_file_invalid")
    _token(file.get("raw_sha256"), "ready_digest_invalid"); _token(file.get("canonical_sha256"), "ready_digest_invalid")
    _durability(row.get("durability"))
    return row


def _custody_ready(value: Any) -> Mapping[str, Any]:
    row = _object(value, "custody_ready_invalid")
    if set(row) != {"schema", "generation_id", "candidate_projection", "custody", "durability"} or row.get("schema") != CUSTODY_READY_SCHEMA:
        raise CustodyError("custody_ready_schema_invalid")
    _token(row.get("generation_id"), "custody_ready_generation_invalid")
    for name in ("candidate_projection", "custody"):
        file = _object(row.get(name), "custody_ready_file_invalid")
        if set(file) != {"raw_sha256", "canonical_sha256"}: raise CustodyError("custody_ready_file_invalid")
        _token(file.get("raw_sha256"), "custody_ready_digest_invalid"); _token(file.get("canonical_sha256"), "custody_ready_digest_invalid")
    _durability(row.get("durability"))
    return row


def _candidate_snapshot(bundle: Path) -> tuple[dict[str, Any], Mapping[str, Any], bytes]:
    """Candidate-only read path: never names, stats, or opens custody files."""
    bundle_identity = _directory_identity(bundle, "candidate_bundle_path_invalid")
    try:
        os.lstat(bundle / ".aerp7-publishing")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CustodyError("candidate_bundle_not_ready") from exc
    else:
        raise CustodyError("candidate_bundle_not_ready")
    ready_path, projection_path = bundle / "READY.json", bundle / "projection.json"
    try:
        ready_raw, ready_identity, ready_sha = _snapshot(ready_path, "candidate_bundle_not_ready")
        projection_raw, projection_identity, projection_sha = _snapshot(projection_path, "candidate_bundle_not_ready")
    except CustodyError as exc:
        raise CustodyError("candidate_bundle_not_ready") from exc
    if ready_identity == projection_identity: raise CustodyError("candidate_bundle_file_alias")
    ready = _candidate_ready(_decode(ready_raw, "ready_json_invalid"))
    if projection_sha != ready["projection"]["raw_sha256"]: raise CustodyError("candidate_projection_raw_digest_invalid")
    projection = validate_candidate_projection(_decode(projection_raw, "candidate_projection_json_invalid"))
    if canonical_sha256(projection) != ready["projection"]["canonical_sha256"]: raise CustodyError("candidate_projection_canonical_digest_invalid")
    repeated_raw, repeated_identity, repeated_sha = _snapshot(ready_path, "candidate_bundle_generation_drift")
    if repeated_identity != ready_identity or repeated_sha != ready_sha or repeated_raw != ready_raw:
        raise CustodyError("candidate_bundle_generation_drift")
    repeat_projection_raw, repeat_projection_identity, repeat_projection_sha = _snapshot(projection_path, "candidate_bundle_generation_drift")
    if repeat_projection_identity != projection_identity or repeat_projection_sha != projection_sha or repeat_projection_raw != projection_raw:
        raise CustodyError("candidate_bundle_generation_drift")
    if _directory_identity(bundle, "candidate_bundle_path_invalid") != bundle_identity: raise CustodyError("candidate_bundle_identity_drift")
    return projection, ready, projection_raw


def load_candidate_projection(bundle: Path) -> dict[str, Any]:
    """Candidate-facing reader; it never accepts or exposes binding material."""
    projection, _ready, _projection_raw = _candidate_snapshot(bundle)
    return projection


_CUSTODY_REFERENCE_KEYS = frozenset({"schema", "bundle_path", "candidate_reference", "custody_path", "ready_path", "generation_id", "custody_raw_sha256", "custody_canonical_sha256", "dataset", "item_count", "evidence_span_count", "ready_sha256"})


def _stream_file_digest(path: Path, code: str) -> tuple[str, int]:
    try:
        before = os.lstat(path)
        if path.is_symlink() or not stat.S_ISREG(before.st_mode): raise CustodyError(code)
        digest = hashlib.sha256(); size = 0
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk); size += len(chunk)
        after = os.lstat(path)
    except OSError as exc:
        raise CustodyError(code) from exc
    if path.is_symlink() or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or before.st_size != after.st_size:
        raise CustodyError(code)
    return digest.hexdigest(), size


def _stream_custody_commitment(secret: bytes, revision: str, candidate_path: Path, custody_path: Path) -> str:
    """Recreate the keyed custody binding without decoding either large JSON body."""
    try:
        with custody_path.open("rb") as stream:
            prefix = stream.read(1024 * 1024)
    except OSError as exc:
        raise CustodyError("custody_stream_binding_invalid") from exc
    marker = b'{"binding":'
    if not prefix.startswith(marker):
        raise CustodyError("custody_stream_binding_invalid")
    depth = 0; quoted = False; escaped = False; end = None
    for offset, byte in enumerate(prefix[len(marker):], start=len(marker)):
        if quoted:
            if escaped: escaped = False
            elif byte == 92: escaped = True
            elif byte == 34: quoted = False
            continue
        if byte == 34: quoted = True
        elif byte in (123, 91): depth += 1
        elif byte in (125, 93):
            depth -= 1
            if depth == 0:
                end = offset + 1; break
    if end is None or end >= len(prefix) or prefix[end:end + 1] != b",":
        raise CustodyError("custody_stream_binding_invalid")
    binding = hmac.new(_binding_secret(secret), digestmod=hashlib.sha256)
    binding.update(b'{"domain":'); binding.update(_bytes("custody-binding")); binding.update(b',"identity":{"projection":')
    try:
        with candidate_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""): binding.update(chunk)
        binding.update(b',"sealed_custody":{')
        with custody_path.open("rb") as stream:
            stream.seek(end + 1)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""): binding.update(chunk)
    except OSError as exc:
        raise CustodyError("custody_stream_binding_invalid") from exc
    binding.update(b'},"revision":'); binding.update(_bytes(revision)); binding.update(b"}")
    return binding.hexdigest()


def _candidate_reference_local(value: Any) -> dict[str, Any]:
    row = dict(_object(value, "custody_reference_candidate_invalid"))
    keys = {"schema", "bundle_path", "projection_path", "ready_path", "generation_id", "projection_raw_sha256", "projection_canonical_sha256", "dataset", "query_count", "candidate_text_count"}
    if set(row) != keys or row["schema"] != CANDIDATE_PROJECTION_REFERENCE_SCHEMA or row["projection_path"] != "projection.json" or row["ready_path"] != "READY.json" or not isinstance(row["bundle_path"], str) or not Path(row["bundle_path"]).is_absolute():
        raise CustodyError("custody_reference_candidate_invalid")
    for key in ("generation_id", "projection_raw_sha256", "projection_canonical_sha256"):
        _token(row[key], "custody_reference_candidate_invalid")
    dataset = _object(row["dataset"], "custody_reference_candidate_invalid")
    if set(dataset) != {"canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256"}:
        raise CustodyError("custody_reference_candidate_invalid")
    for value in dataset.values(): _token(value, "custody_reference_candidate_invalid")
    if any(isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] <= 0 for key in ("query_count", "candidate_text_count")):
        raise CustodyError("custody_reference_candidate_invalid")
    return row


def custody_reference(*, candidate_bundle: Path, custody_bundle: Path, candidate_reference: Mapping[str, Any]) -> dict[str, Any]:
    """Create a small READY-bound custody capability; it contains no labels."""
    candidate = _candidate_reference_local(candidate_reference)
    candidate_root, custody_root = candidate_bundle.resolve(strict=True), custody_bundle.resolve(strict=True)
    if candidate_root != Path(candidate["bundle_path"]).resolve(strict=True) or candidate_root.is_symlink() or custody_root.is_symlink():
        raise CustodyError("custody_reference_path_invalid")
    candidate_ready = _candidate_ready(_decode(_snapshot(candidate_root / "READY.json", "custody_reference_candidate_invalid")[0], "custody_reference_candidate_invalid"))
    if candidate_ready["generation_id"] != candidate["generation_id"] or candidate_ready["projection"] != {"raw_sha256": candidate["projection_raw_sha256"], "canonical_sha256": candidate["projection_canonical_sha256"]}:
        raise CustodyError("custody_reference_candidate_binding_invalid")
    candidate_sha, _candidate_size = _stream_file_digest(candidate_root / "projection.json", "custody_reference_candidate_invalid")
    if candidate_sha != candidate["projection_raw_sha256"] or candidate_sha != candidate["projection_canonical_sha256"]:
        raise CustodyError("custody_reference_candidate_binding_invalid")
    ready_raw, _identity, ready_sha = _snapshot(custody_root / "READY.json", "custody_reference_ready_invalid")
    ready = _custody_ready(_decode(ready_raw, "custody_reference_ready_invalid"))
    custody_path = custody_root / "sealed-custody.json"
    custody_sha, _bytes_count = _stream_file_digest(custody_path, "custody_reference_custody_invalid")
    if ready["generation_id"] != candidate["generation_id"] or ready["candidate_projection"] != candidate_ready["projection"] or ready["custody"]["raw_sha256"] != custody_sha or ready["custody"]["canonical_sha256"] != custody_sha:
        raise CustodyError("custody_reference_ready_binding_invalid")
    # The top-level metadata is intentionally small; item rows are never decoded
    # here and are admitted only by CustodyStore's streaming ingress.
    ijson = _ijson()
    try:
        with custody_path.open("rb") as stream:
            schema = next(ijson.items(stream, "schema", use_float=True))
        with custody_path.open("rb") as stream:
            dataset = next(ijson.items(stream, "dataset", use_float=True))
        with custody_path.open("rb") as stream:
            count = sum(1 for _ in ijson.items(stream, "items.item", use_float=True))
        with custody_path.open("rb") as stream:
            spans = sum(len(_object(item.get("labels"), "custody_reference_item_invalid").get("message_evidences", [])) for item in ijson.items(stream, "items.item", use_float=True))
    except (OSError, ValueError, StopIteration) as exc:
        raise CustodyError("custody_reference_custody_invalid") from exc
    if schema != CUSTODY_SCHEMA or dataset != candidate["dataset"] or count != candidate["query_count"]:
        raise CustodyError("custody_reference_custody_binding_invalid")
    return {"schema": CUSTODY_REFERENCE_SCHEMA, "bundle_path": str(custody_root), "candidate_reference": candidate, "custody_path": str(custody_path), "ready_path": str(custody_root / "READY.json"), "generation_id": candidate["generation_id"], "custody_raw_sha256": custody_sha, "custody_canonical_sha256": custody_sha, "dataset": dict(candidate["dataset"]), "item_count": count, "evidence_span_count": spans, "ready_sha256": ready_sha}


class CustodyStore:
    """Ephemeral SQLite ingress for a verified candidate/custody generation."""

    def __init__(self, reference: Mapping[str, Any], directory: Path, connection: sqlite3.Connection, receipt: Mapping[str, Any]) -> None:
        self.reference, self.directory, self.connection, self._receipt = dict(reference), directory, connection, dict(receipt)

    @classmethod
    def open(cls, reference: Mapping[str, Any], *, staging_parent: Path, binding_secret: bytes) -> "CustodyStore":
        ref = custody_reference(candidate_bundle=Path(_candidate_reference_local(_object(reference, "custody_reference_invalid").get("candidate_reference"))["bundle_path"]), custody_bundle=Path(_object(reference, "custody_reference_invalid").get("bundle_path", "")), candidate_reference=_object(reference, "custody_reference_invalid").get("candidate_reference"))
        if dict(reference) != ref:
            raise CustodyError("custody_reference_drift")
        directory = Path(tempfile.mkdtemp(prefix="aerp7-custody-store-", dir=staging_parent)); database = directory / "custody.sqlite3"
        connection = sqlite3.connect(database); connection.row_factory = sqlite3.Row
        try:
            connection.executescript("CREATE TABLE corpora(corpus_id TEXT PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE projection_items(item_id TEXT PRIMARY KEY,payload TEXT NOT NULL); CREATE TABLE custody_items(item_id TEXT PRIMARY KEY,payload TEXT NOT NULL);")
            ijson = _ijson(); candidate_path = Path(ref["candidate_reference"]["bundle_path"]) / "projection.json"
            candidate_bytes = custody_bytes = evidence_span_count = 0
            try:
                with Path(ref["custody_path"]).open("rb") as stream:
                    binding = next(ijson.items(stream, "binding", use_float=True))
            except (OSError, ValueError, StopIteration) as exc:
                raise CustodyError("custody_store_binding_invalid") from exc
            if not isinstance(binding, Mapping) or not hmac.compare_digest(str(binding.get("commitment", "")), _stream_custody_commitment(binding_secret, ref["dataset"]["revision_sha256"], candidate_path, Path(ref["custody_path"]))):
                raise CustodyError("custody_store_binding_invalid")
            with candidate_path.open("rb") as stream:
                for row in ijson.items(stream, "corpora.item", use_float=True):
                    payload = _bytes(row).decode("utf-8"); connection.execute("INSERT INTO corpora VALUES(?,?)", (row["corpus_id"], payload)); candidate_bytes += len(payload.encode("utf-8"))
            with candidate_path.open("rb") as stream:
                for row in ijson.items(stream, "items.item", use_float=True):
                    payload = _bytes(row).decode("utf-8"); connection.execute("INSERT INTO projection_items VALUES(?,?)", (row["item_id"], payload)); candidate_bytes += len(payload.encode("utf-8"))
            with Path(ref["custody_path"]).open("rb") as stream:
                for row in ijson.items(stream, "items.item", use_float=True):
                    item_id = _token(row.get("item_id"), "custody_store_item_invalid"); payload = _bytes(row).decode("utf-8")
                    candidate_item = connection.execute("SELECT payload FROM projection_items WHERE item_id=?", (item_id,)).fetchone()
                    if candidate_item is None: raise CustodyError("custody_store_projection_binding_invalid")
                    candidate_item_value = json.loads(candidate_item[0]); corpus = connection.execute("SELECT payload FROM corpora WHERE corpus_id=?", (candidate_item_value["corpus_id"],)).fetchone()
                    sealed_without_commitment = {key: value for key, value in row.items() if key != "binding_commitment"}
                    if corpus is None or row.get("corpus_id") != candidate_item_value["corpus_id"] or row.get("binding_commitment") != _custody_item_commitment(binding_secret, ref["dataset"]["revision_sha256"], sealed_without_commitment, candidate_item_value, json.loads(corpus[0])):
                        raise CustodyError("custody_store_item_binding_invalid")
                    connection.execute("INSERT INTO custody_items VALUES(?,?)", (item_id, payload)); custody_bytes += len(payload.encode("utf-8"))
                    evidence_span_count += len(_object(row.get("labels"), "custody_store_item_invalid").get("message_evidences", []))
            item_count = connection.execute("SELECT count(*) FROM custody_items").fetchone()[0]
            if item_count != ref["item_count"] or evidence_span_count != ref["evidence_span_count"] or connection.execute("SELECT count(*) FROM projection_items").fetchone()[0] != item_count:
                raise CustodyError("custody_store_item_count_invalid")
            connection.commit()
            sqlite_store_bytes = sum(path.stat().st_size for path in (database, database.with_name(database.name + "-wal"), database.with_name(database.name + "-shm")) if path.exists())
            candidate_input_bytes = candidate_path.stat().st_size; custody_input_bytes = Path(ref["custody_path"]).stat().st_size
            return cls(ref, directory, connection, {"schema": "aerp7-convomem-custody-store-receipt-v1", "item_count": item_count, "candidate_input_bytes": candidate_input_bytes, "custody_input_bytes": custody_input_bytes, "candidate_store_bytes": candidate_bytes, "custody_store_bytes": custody_bytes, "sqlite_store_bytes": sqlite_store_bytes, "peak_disk_input_and_store_bytes": candidate_input_bytes + custody_input_bytes + sqlite_store_bytes})
        except Exception:
            connection.close(); shutil.rmtree(directory, ignore_errors=True); raise

    def receipt(self) -> dict[str, Any]: return dict(self._receipt)

    def iter_scoring_items(self) -> Iterator[dict[str, Any]]:
        for row in self.connection.execute("SELECT c.payload,p.payload FROM custody_items c JOIN projection_items p ON p.item_id=c.item_id ORDER BY c.item_id"):
            custody_item, projection_item = json.loads(row[0]), json.loads(row[1]); directory = _object(custody_item["directory"], "custody_store_directory_invalid"); group = _text(directory.get("group"), "custody_store_directory_invalid")
            spans = [{"speaker": _text(item["speaker"], "custody_store_evidence_invalid"), "text": _text(item["text"], "custody_store_evidence_invalid")} for item in _list(_object(custody_item["labels"], "custody_store_evidence_invalid").get("message_evidences"), "custody_store_evidence_invalid")]
            if group == "abstention_evidence":
                if spans: raise CustodyError("abstention_evidence_labels_invalid")
                conversations: list[str] = []
            else: conversations = list(custody_item["evidence_conversation_ids"])
            if custody_item["corpus_id"] != projection_item["corpus_id"]: raise CustodyError("custody_store_projection_binding_invalid")
            yield {"item_id": custody_item["item_id"], "directory_group": group, "evidence_conversation_ids": conversations, "evidence_spans": spans}

    def close(self) -> None:
        self.connection.close(); shutil.rmtree(self.directory)

    def __enter__(self) -> "CustodyStore": return self
    def __exit__(self, *_args: Any) -> None: self.close()


def load_sealed_custody(candidate_bundle: Path, custody_bundle: Path, *, binding_secret: bytes) -> dict[str, Any]:
    """Custodian-only reader that verifies both READY bindings before exposing labels."""
    projection, candidate_ready, candidate_raw = _candidate_snapshot(candidate_bundle)
    bundle_identity = _directory_identity(custody_bundle, "custody_bundle_path_invalid")
    try:
        os.lstat(custody_bundle / ".aerp7-publishing")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise CustodyError("custody_bundle_not_ready") from exc
    else:
        raise CustodyError("custody_bundle_not_ready")
    try:
        ready_raw, ready_identity, ready_sha = _snapshot(custody_bundle / "READY.json", "custody_bundle_not_ready")
        custody_raw, custody_identity, custody_sha = _snapshot(custody_bundle / "sealed-custody.json", "custody_bundle_not_ready")
    except CustodyError as exc:
        raise CustodyError("custody_bundle_not_ready") from exc
    if ready_identity == custody_identity: raise CustodyError("custody_bundle_file_alias")
    ready = _custody_ready(_decode(ready_raw, "custody_ready_json_invalid"))
    if ready["generation_id"] != candidate_ready["generation_id"] or ready["candidate_projection"]["raw_sha256"] != hashlib.sha256(candidate_raw).hexdigest() or ready["candidate_projection"]["canonical_sha256"] != canonical_sha256(projection):
        raise CustodyError("custody_candidate_generation_binding_invalid")
    if custody_sha != ready["custody"]["raw_sha256"]: raise CustodyError("custody_raw_digest_invalid")
    custody = _validate_custody(_decode(custody_raw, "custody_json_invalid"), projection, binding_secret=binding_secret)
    if canonical_sha256(custody) != ready["custody"]["canonical_sha256"]:
        raise CustodyError("custody_canonical_digest_invalid")
    repeated_raw, repeated_identity, repeated_sha = _snapshot(custody_bundle / "READY.json", "custody_bundle_generation_drift")
    repeat_custody_raw, repeat_custody_identity, repeat_custody_sha = _snapshot(custody_bundle / "sealed-custody.json", "custody_bundle_generation_drift")
    if repeated_raw != ready_raw or repeated_identity != ready_identity or repeated_sha != ready_sha or repeat_custody_raw != custody_raw or repeat_custody_identity != custody_identity or repeat_custody_sha != custody_sha or _directory_identity(custody_bundle, "custody_bundle_path_invalid") != bundle_identity:
        raise CustodyError("custody_bundle_generation_drift")
    return dict(custody)


def load_custody_for_scoring(candidate_bundle: Path, custody_bundle: Path, *, binding_secret: bytes) -> dict[str, Any]:
    """Privileged, minimal adapter from a fully verified sealed bundle.

    This is intentionally the only bridge from AERP-7A custody to AERP-7B.
    It does not make source locators, answers, tiers, or raw custody available
    to the scorer.  Callers must invoke it only after every public ranking
    freeze has validated.
    """
    sealed = load_sealed_custody(candidate_bundle, custody_bundle, binding_secret=binding_secret)
    items = []
    for row in sealed["items"]:
        directory = _object(row["directory"], "custody_directory_invalid")
        group = _text(directory.get("group"), "custody_directory_group_invalid")
        spans = []
        for evidence in _list(_object(row["labels"], "custody_labels_invalid").get("message_evidences"), "custody_evidence_labels_invalid"):
            evidence_row = _object(evidence, "custody_evidence_label_invalid")
            spans.append({"speaker": _text(evidence_row.get("speaker"), "custody_evidence_label_invalid"), "text": _text(evidence_row.get("text"), "custody_evidence_label_invalid")})
        # The sealed custody binds every selected case to its source
        # conversations, including abstention cases.  Those source bindings are
        # not positive evidence labels.  The privileged scoring projection must
        # therefore expose an empty evidence set for the official abstention
        # endpoint while retaining the sealed source binding internally.
        if group == "abstention_evidence":
            if spans:
                raise CustodyError("abstention_evidence_labels_invalid")
            evidence_conversation_ids: list[str] = []
        else:
            evidence_conversation_ids = list(row["evidence_conversation_ids"])
        items.append({"item_id": row["item_id"], "directory_group": group, "evidence_conversation_ids": evidence_conversation_ids, "evidence_spans": spans})
    return {"schema": "aerp7-convomem-custody-for-scoring-v2", "projection_sha256": sealed["projection_sha256"], "items": items}


def _validate_custody(value: Any, projection: dict[str, Any], *, binding_secret: bytes) -> Mapping[str, Any]:
    custody = _object(value, "custody_root_invalid")
    secret = _binding_secret(binding_secret)
    if set(custody) != {"schema", "projection_sha256", "dataset", "selection_receipt", "mapping_status", "items", "binding"} or custody.get("schema") != CUSTODY_SCHEMA: raise CustodyError("custody_schema_invalid")
    if custody.get("projection_sha256") != canonical_sha256(projection) or custody.get("dataset") != projection["dataset"] or custody.get("selection_receipt") != projection["selection_receipt"]: raise CustodyError("custody_projection_binding_invalid")
    if custody.get("mapping_status") != {"status": "not_attempted", "scoring_permitted": False, "reason": "aerp7_prelabel_slice_has_no_span_mapping", "unresolved_item_context_count": len(projection["items"])}: raise CustodyError("custody_mapping_status_invalid")
    expected = {row["item_id"]: row for row in projection["items"]}
    corpora = {row["corpus_id"]: row for row in projection["corpora"]}
    corpus_messages = {corpus_id: {candidate["message_id"] for candidate in row["candidates"]} for corpus_id, row in corpora.items()}
    seen_items = set(); seen_canonical_context = set()
    rows = _list(custody.get("items"), "custody_items_invalid")
    if len(rows) != len(expected): raise CustodyError("custody_item_count_invalid")
    for raw in rows:
        row = _object(raw, "custody_item_invalid")
        if set(row) != {"item_id", "canonical_item_id", "persona_id", "persona_source_id", "corpus_id", "source_locator", "directory", "labels", "evidence_conversation_ids", "messages", "binding_commitment"}: raise CustodyError("custody_item_schema_invalid")
        item_id = _token(row.get("item_id"), "custody_item_id_invalid")
        corpus_id = _token(row.get("corpus_id"), "custody_corpus_id_invalid")
        if item_id not in expected or row.get("persona_id") != expected[item_id]["persona_id"] or corpus_id != expected[item_id]["corpus_id"]: raise CustodyError("custody_item_projection_binding_invalid")
        canonical_item_id = _token(row.get("canonical_item_id"), "custody_canonical_item_id_invalid"); _text(row.get("persona_source_id"), "custody_persona_source_invalid")
        if item_id in seen_items or (canonical_item_id, corpus_id) in seen_canonical_context: raise CustodyError("custody_item_duplicate")
        seen_items.add(item_id); seen_canonical_context.add((canonical_item_id, corpus_id)); _object(row.get("source_locator"), "custody_source_locator_invalid")
        directory = _object(row.get("directory"), "custody_directory_invalid")
        if set(directory) != {"group", "tier", "record_category"}: raise CustodyError("custody_directory_schema_invalid")
        if any(value is not None and not isinstance(value, str) for value in directory.values()): raise CustodyError("custody_directory_schema_invalid")
        labels = _object(row.get("labels"), "custody_labels_invalid")
        if set(labels) != {"answer", "message_evidences"}: raise CustodyError("custody_labels_schema_invalid")
        _text(labels.get("answer"), "custody_answer_invalid")
        for evidence in _list(labels.get("message_evidences"), "custody_evidence_labels_invalid"):
            evidence_row = _object(evidence, "custody_evidence_label_invalid")
            if set(evidence_row) != {"speaker", "text"}: raise CustodyError("custody_evidence_label_schema_invalid")
            _text(evidence_row.get("speaker"), "custody_evidence_label_invalid"); _text(evidence_row.get("text"), "custody_evidence_label_invalid")
        evidence_conversations = _list(row.get("evidence_conversation_ids"), "custody_evidence_conversation_invalid")
        if not evidence_conversations or len(evidence_conversations) != len(set(evidence_conversations)):
            raise CustodyError("custody_evidence_conversation_invalid")
        if not all(isinstance(value, str) and _token(value, "custody_evidence_conversation_invalid") for value in evidence_conversations):
            raise CustodyError("custody_evidence_conversation_invalid")
        if not set(evidence_conversations) <= {candidate["opaque_conversation_id"] for candidate in corpora[corpus_id]["candidates"]}:
            raise CustodyError("custody_evidence_conversation_invalid")
        messages = _list(row.get("messages"), "custody_messages_invalid"); ids = set()
        for message in messages:
            message_row = _object(message, "custody_message_invalid")
            if set(message_row) != {"message_id", "source_locator"}: raise CustodyError("custody_message_schema_invalid")
            ids.add(_token(message_row.get("message_id"), "custody_message_id_invalid")); _object(message_row.get("source_locator"), "custody_message_locator_invalid")
        if len(ids) != len(messages) or ids != corpus_messages.get(corpus_id): raise CustodyError("custody_message_binding_invalid")
        sealed_without_commitment = {key: row[key] for key in row if key != "binding_commitment"}
        if row.get("binding_commitment") != _custody_item_commitment(secret, projection["dataset"]["revision_sha256"], sealed_without_commitment, expected[item_id], corpora[corpus_id]):
            raise CustodyError("custody_item_binding_invalid")
    binding = _object(custody.get("binding"), "custody_binding_invalid")
    if set(binding) != {"algorithm", "revision_sha256", "commitment"} or binding.get("algorithm") != BOUND_CUSTODY_ALGORITHM or binding.get("revision_sha256") != projection["dataset"]["revision_sha256"]:
        raise CustodyError("custody_binding_invalid")
    _token(binding.get("commitment"), "custody_binding_invalid")
    custody_without_binding = {key: custody[key] for key in custody if key != "binding"}
    if binding["commitment"] != _custody_commitment(secret, projection["dataset"]["revision_sha256"], custody_without_binding, projection):
        raise CustodyError("custody_binding_invalid")
    return custody


def _output(output: Path) -> tuple[Path, Path, tuple[int, int]]:
    if not output.is_absolute() or output.is_symlink() or output.exists(): raise CustodyError("output_already_exists")
    parent = output.parent
    _safe_existing_ancestors(parent, "output_parent_invalid")
    parent_identity = _directory_identity(parent, "output_parent_invalid")
    repository = Path(__file__).resolve().parents[1]
    try: output.resolve(strict=False).relative_to(repository)
    except ValueError: pass
    else: raise CustodyError("output_must_be_external")
    return output, parent, parent_identity


def _distinct_outputs(candidate_output: Path, custody_output: Path) -> None:
    _output(candidate_output); _output(custody_output)
    candidate = candidate_output.resolve(strict=False)
    custody = custody_output.resolve(strict=False)
    try:
        candidate.relative_to(custody)
    except ValueError:
        pass
    else:
        raise CustodyError("output_paths_not_separate")
    try:
        custody.relative_to(candidate)
    except ValueError:
        return
    raise CustodyError("output_paths_not_separate")


def _same_directory(path: Path, identity: tuple[int, int]) -> bool:
    try: return _directory_identity(path, "publication_target_identity_drift") == identity
    except CustodyError: return False


def validated_wsl_interop() -> str | None:
    """Return only a live WSL interop socket; inherited text is never enough."""
    value = os.environ.get("WSL_INTEROP")
    if not value or "\x00" in value or os.name != "posix" or not sys.platform.startswith("linux"):
        return None
    path = Path(value)
    try:
        metadata = os.lstat(path)
    except OSError:
        return None
    if not path.is_absolute() or path.parent != Path("/run/WSL") or not stat.S_ISSOCK(metadata.st_mode):
        return None
    return str(path)


def _drvfs_mount(path: Path) -> tuple[str, str] | None:
    try:
        resolved = path.resolve(strict=True)
        device = os.stat(resolved).st_dev
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except (OSError, RuntimeError, ValueError):
        return None
    candidates: list[tuple[list[str], str, str, str, Path]] = []
    for line in lines:
        try:
            left, right = line.split(" - ", 1)
            fields = left.split()
            filesystem, source, options = right.split()[:3]
            mountpoint = Path(fields[4])
        except (ValueError, IndexError):
            return None
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        candidates.append((fields, filesystem, source, options, mountpoint))
    if not candidates:
        return None
    deepest = max(len(mountpoint.parts) for *_record, mountpoint in candidates)
    selected = [record for record in candidates if len(record[-1].parts) == deepest]
    if len(selected) != 1:
        return None
    fields, filesystem, source, options, mountpoint = selected[0]
    try:
        major_text, minor_text = fields[2].split(":", 1)
        if not major_text.isdecimal() or not minor_text.isdecimal():
            return None
        device_matches = (os.major(device), os.minor(device)) == (int(major_text), int(minor_text))
    except (IndexError, ValueError, OSError):
        return None
    mount_parts = mountpoint.parts
    is_drive_mountpoint = (
        fields[3] == "/"
        and len(mount_parts) == 3
        and mount_parts[:2] == ("/", "mnt")
        and len(mount_parts[2]) == 1
        and mount_parts[2].isalpha()
    )
    option_tokens = [part for option in options.split(",") for part in option.split(";")]
    drvfs_names = [token.removeprefix("aname=") for token in option_tokens if token.startswith("aname=")]
    drvfs_paths = [token.removeprefix("path=") for token in option_tokens if token.startswith("path=")]
    expected_drive_root = mount_parts[2].upper() + ":\\" if is_drive_mountpoint else ""
    if filesystem != "9p" or not is_drive_mountpoint or not device_matches or drvfs_names != ["drvfs"] or drvfs_paths != [expected_drive_root]:
        return None
    return str(mountpoint), source


def _verified_wsl_drvfs_interop(source: Path, destination: Path) -> str | None:
    """Allow one sibling move through Windows only on verified WSL DrvFs."""
    interop = validated_wsl_interop()
    if interop is None or not source.is_absolute() or not destination.is_absolute():
        return None
    try:
        metadata = os.lstat(source)
        source_parent = source.parent.resolve(strict=True)
        destination_parent = destination.parent.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode) or source_parent != destination_parent:
        return None
    source_mount = _drvfs_mount(source_parent)
    if source_mount is None or source_mount != _drvfs_mount(destination_parent):
        return None
    return interop


def _windows_drvfs_path(path: Path) -> str:
    parts = path.parts
    if not path.is_absolute() or len(parts) < 3 or parts[:2] != ("/", "mnt") or len(parts[2]) != 1 or not parts[2].isalpha():
        raise CustodyError("cleanup_tombstone_windows_fallback_path_invalid")
    return parts[2].upper() + ":\\" + "\\".join(parts[3:])


def _windows_directory_move_noreplace(source: Path, destination: Path, interop: str) -> bool:
    """Directory.Move refuses overwrite; a raced failure remains fail-closed."""
    if destination.exists() or destination.is_symlink():
        return False
    if _WINDOWS_POWERSHELL.is_symlink() or not _WINDOWS_POWERSHELL.is_file():
        raise CustodyError("cleanup_tombstone_windows_fallback_unavailable")
    command = "$ErrorActionPreference='Stop';try{[System.IO.Directory]::Move($env:AERP7_WSL_MOVE_SOURCE,$env:AERP7_WSL_MOVE_DESTINATION);exit 0}catch [System.IO.IOException]{if([System.IO.Directory]::Exists($env:AERP7_WSL_MOVE_DESTINATION)){exit 3};exit 4}catch{exit 4}"
    try:
        result = subprocess.run(
            [str(_WINDOWS_POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command],
            env={"WSL_INTEROP": interop, "WSLENV": "AERP7_WSL_MOVE_SOURCE:AERP7_WSL_MOVE_DESTINATION", "AERP7_WSL_MOVE_SOURCE": _windows_drvfs_path(source), "AERP7_WSL_MOVE_DESTINATION": _windows_drvfs_path(destination)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    except OSError as exc:
        raise CustodyError("cleanup_tombstone_windows_fallback_failed") from exc
    if result.returncode == 3:
        return False
    if result.returncode != 0:
        raise CustodyError("cleanup_tombstone_windows_fallback_failed", returncode=result.returncode)
    return True


def _rename_noreplace(source: Path, destination: Path) -> bool:
    """Atomically move a directory only when its generated destination is free."""
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_bool
        ctypes.set_last_error(0)
        if move(str(source), str(destination), 0):
            return True
        error = ctypes.get_last_error()
        if error in {80, 183}:
            return False
        raise CustodyError("cleanup_tombstone_rename_failed", platform="windows", winerror=error)
    try:
        library = ctypes.CDLL(None, use_errno=True)
        renameat2 = library.renameat2
    except (AttributeError, OSError) as exc:
        raise CustodyError("cleanup_tombstone_rename_unavailable") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1) == 0:
        return True
    error = ctypes.get_errno()
    if error == 17:
        return False
    if error == errno.EINVAL:
        interop = _verified_wsl_drvfs_interop(source, destination)
        if interop is not None:
            return _windows_directory_move_noreplace(source, destination, interop)
    raise CustodyError("cleanup_tombstone_rename_failed", platform="linux", errno=error)


def _claim_cleanup_tombstone(target: Path, identity: tuple[int, int], parent_identity: tuple[int, int], prefix: str) -> Path | None:
    parent = target.parent
    if _directory_identity(parent, "cleanup_parent_identity_drift") != parent_identity or not _same_directory(target, identity):
        return None
    for _attempt in range(32):
        tombstone = parent / (prefix + secrets.token_hex(16))
        try:
            os.lstat(tombstone)
        except FileNotFoundError:
            pass
        except OSError:
            return None
        else:
            continue
        try:
            moved = _rename_noreplace(target, tombstone)
        except CustodyError:
            raise
        except OSError:
            return None
        if not moved:
            continue
        if _directory_identity(parent, "cleanup_parent_identity_drift") != parent_identity or _directory_identity(tombstone, "cleanup_tombstone_identity_drift") != identity:
            return None
        return tombstone
    return None


def _cleanup_owned_target(target: Path, identity: tuple[int, int], created: dict[Path, tuple[int, int]], *, failure_tombstone: bool = False, parent_identity: tuple[int, int] | None = None) -> bool:
    """Remove only our exact files; never recursively delete a raced/foreign directory."""
    original = target
    if failure_tombstone:
        if parent_identity is None:
            return False
        renamed = _claim_cleanup_tombstone(target, identity, parent_identity, ".aerp7-incomplete-")
        if renamed is None:
            return False
        target = renamed
        created = {target / path.relative_to(original): expected for path, expected in created.items()}
    if not _same_directory(target, identity): return False
    marker = target / ".aerp7-publishing"
    marker_identity = created.get(marker)
    # The marker is the capability revocation token: it must survive any
    # identity/unlink/unknown-entry failure until all label-bearing entries are
    # gone and the directory contains only this owned final marker.
    if marker_identity is None: return False
    ordinary = {path: expected for path, expected in created.items() if path != marker}
    for path, expected in ordinary.items():
        try:
            current = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if (current.st_dev, current.st_ino) != expected or not stat.S_ISREG(current.st_mode): return False
        try: path.unlink()
        except OSError: return False
    try:
        remaining = {entry.name for entry in target.iterdir()}
    except OSError:
        return False
    if remaining != {marker.name}: return False
    try:
        current = os.lstat(marker)
    except OSError:
        return False
    if (current.st_dev, current.st_ino) != marker_identity or not stat.S_ISREG(current.st_mode): return False
    try:
        marker.unlink()
        target.rmdir()
    except OSError:
        return False
    return True


def _fsync_directory(path: Path, *, code: str) -> bool:
    if os.name != "posix":
        return False
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    except OSError as exc:
        raise CustodyError(code) from exc
    return True


def _durability_receipt() -> dict[str, Any]:
    return {"platform": "linux", "directory_fsync_guaranteed": True, "steps": ["payload_entries", "ready_hardlink", "ready_temp_unlink"]} if _linux_renameat2_available() else {"platform": "windows" if os.name == "nt" else sys.platform, "directory_fsync_guaranteed": False, "steps": []}


def _publish_single(output: Path, *, payload_name: str, payload: dict[str, Any], ready: dict[str, Any], payload_binding: Mapping[str, Any], validator: Any) -> dict[str, Any]:
    target, parent, parent_identity = _output(output)
    if _directory_identity(parent, "output_parent_identity_drift") != parent_identity:
        raise CustodyError("output_parent_identity_drift")
    try: os.mkdir(target)
    except FileExistsError:
        # A crash after custody publication but before candidate publication is
        # recoverable only when every published byte exactly matches this rebuilt
        # deterministic generation.  Never adopt a partial or foreign directory.
        if target.is_symlink() or not target.is_dir() or {entry.name for entry in target.iterdir()} != {payload_name, "READY.json"}:
            raise CustodyError("output_already_exists")
        payload_path, ready_path = target / payload_name, target / "READY.json"
        raw, _identity, raw_sha = _snapshot(payload_path, "publication_existing_invalid")
        checked = validator(_decode(raw, "publication_existing_invalid"))
        ready_raw, _ready_identity, _ready_sha = _snapshot(ready_path, "publication_existing_invalid")
        existing_ready = _decode(ready_raw, "publication_existing_invalid")
        if raw != _bytes(payload) or raw_sha != payload_binding["raw_sha256"] or canonical_sha256(checked) != payload_binding["canonical_sha256"] or existing_ready != ready:
            raise CustodyError("publication_existing_conflict")
        return dict(ready)
    except OSError as exc: raise CustodyError("output_claim_failed") from exc
    identity = _directory_identity(target, "publication_target_identity_drift")
    created: dict[Path, tuple[int, int]] = {}
    try:
        if _directory_identity(parent, "output_parent_identity_drift") != parent_identity:
            raise CustodyError("output_parent_identity_drift")
        _fsync_directory(parent, code="publication_target_directory_entry_fsync_failed")
        _fsync_directory(parent, code="publication_parent_fsync_failed")
        publishing = target / ".aerp7-publishing"
        _write(publishing, {"schema": "aerp7-convomem-publishing-marker-v1"})
        created[publishing] = _snapshot(publishing, "publication_file_invalid")[1]
        payload_path = target / payload_name
        _write(payload_path, payload); metadata = os.lstat(payload_path); created[payload_path] = (metadata.st_dev, metadata.st_ino)
        if metadata.st_nlink != 1: raise CustodyError("publication_file_alias_invalid")
        payload_raw, _payload_id, payload_sha = _snapshot(payload_path, "publication_file_invalid")
        validated = validator(_decode(payload_raw, "publication_payload_invalid"))
        if payload_sha != payload_binding["raw_sha256"] or canonical_sha256(validated) != payload_binding["canonical_sha256"]:
            raise CustodyError("publication_payload_binding_invalid")
        _fsync_directory(target, code="publication_payload_directory_fsync_failed")
        temporary = target / ".READY.json.tmp"
        _write(temporary, ready); created[temporary] = _snapshot(temporary, "publication_file_invalid")[1]
        # link is an atomic no-replace publication. Until it succeeds, READY is absent.
        ready_path = target / "READY.json"
        os.link(temporary, ready_path)
        # A hard link is the same inode as the verified temporary READY.  Record
        # that known identity before any post-link syscall can fail.
        created[ready_path] = created[temporary]
        _fsync_directory(target, code="publication_ready_directory_fsync_failed")
        linked = os.lstat(ready_path)
        if (linked.st_dev, linked.st_ino) != created[ready_path] or not stat.S_ISREG(linked.st_mode):
            raise CustodyError("publication_file_alias_invalid")
        os.unlink(temporary); created.pop(temporary)
        _fsync_directory(target, code="publication_ready_cleanup_directory_fsync_failed")
        created[ready_path] = _snapshot(ready_path, "publication_file_invalid")[1]
        marker_identity = created[publishing]
        marker = os.lstat(publishing)
        if (marker.st_dev, marker.st_ino) != marker_identity or not stat.S_ISREG(marker.st_mode):
            raise CustodyError("publication_file_alias_invalid")
        final_files = {path: value for path, value in created.items() if path != publishing}
        if len(set(final_files.values())) != 2 or not _same_directory(target, identity) or _directory_identity(parent, "output_parent_identity_drift") != parent_identity: raise CustodyError("publication_file_alias_invalid")
        os.unlink(publishing)
        created.pop(publishing)
        return ready
    except Exception as exc:
        if not _cleanup_owned_target(target, identity, created, failure_tombstone=True, parent_identity=parent_identity):
            raise CustodyError("publication_cleanup_identity_drift") from exc
        _fsync_directory(parent, code="publication_cleanup_parent_fsync_failed")
        raise


def _copy_streamed_payload(source: Path, target: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        while block := incoming.read(1024 * 1024):
            outgoing.write(block); digest.update(block)
        outgoing.flush(); os.fsync(outgoing.fileno())
    return digest.hexdigest()


def _publish_streamed_single(output: Path, *, payload_name: str, source: Path, ready: dict[str, Any], payload_binding: Mapping[str, Any]) -> dict[str, Any]:
    """Publish a prevalidated canonical stream with the normal READY protocol."""
    target, parent, parent_identity = _output(output)
    if _directory_identity(parent, "output_parent_identity_drift") != parent_identity:
        raise CustodyError("output_parent_identity_drift")
    try:
        os.mkdir(target)
    except FileExistsError as exc:
        raise CustodyError("output_already_exists") from exc
    except OSError as exc:
        raise CustodyError("output_claim_failed") from exc
    identity = _directory_identity(target, "publication_target_identity_drift")
    created: dict[Path, tuple[int, int]] = {}
    try:
        _fsync_directory(parent, code="publication_target_directory_entry_fsync_failed")
        _fsync_directory(parent, code="publication_parent_fsync_failed")
        publishing = target / ".aerp7-publishing"
        _write(publishing, {"schema": "aerp7-convomem-publishing-marker-v1"})
        created[publishing] = _snapshot(publishing, "publication_file_invalid", retain=False)[1]
        payload_path = target / payload_name
        if _copy_streamed_payload(source, payload_path) != payload_binding["raw_sha256"]:
            raise CustodyError("publication_payload_binding_invalid")
        metadata = os.lstat(payload_path); created[payload_path] = (metadata.st_dev, metadata.st_ino)
        if metadata.st_nlink != 1: raise CustodyError("publication_file_alias_invalid")
        _fsync_directory(target, code="publication_payload_directory_fsync_failed")
        temporary = target / ".READY.json.tmp"
        _write(temporary, ready); created[temporary] = _snapshot(temporary, "publication_file_invalid", retain=False)[1]
        ready_path = target / "READY.json"; os.link(temporary, ready_path); created[ready_path] = created[temporary]
        _fsync_directory(target, code="publication_ready_directory_fsync_failed")
        os.unlink(temporary); created.pop(temporary)
        _fsync_directory(target, code="publication_ready_cleanup_directory_fsync_failed")
        created[ready_path] = _snapshot(ready_path, "publication_file_invalid", retain=False)[1]
        if len(set(created.values())) != 3 or not _same_directory(target, identity) or _directory_identity(parent, "output_parent_identity_drift") != parent_identity:
            raise CustodyError("publication_file_alias_invalid")
        os.unlink(publishing); created.pop(publishing)
        return ready
    except Exception as exc:
        if not _cleanup_owned_target(target, identity, created, failure_tombstone=True, parent_identity=parent_identity):
            raise CustodyError("publication_cleanup_identity_drift") from exc
        _fsync_directory(parent, code="publication_cleanup_parent_fsync_failed")
        raise


def _candidate_ready_value(projection: Mapping[str, Any], generation_id: str) -> dict[str, Any]:
    return {"schema": CANDIDATE_READY_SCHEMA, "generation_id": generation_id, "projection": {"raw_sha256": hashlib.sha256(_bytes(projection)).hexdigest(), "canonical_sha256": canonical_sha256(projection)}, "durability": _durability_receipt()}


def _publish_candidate(output: Path, projection: dict[str, Any], ready: dict[str, Any]) -> dict[str, Any]:
    _candidate_ready(ready)
    # Generic publisher binds the single payload under the stable key name.
    return _publish_single(output, payload_name="projection.json", payload=projection, ready=ready, payload_binding=ready["projection"], validator=validate_candidate_projection)


def resume_candidate_ready(*, output: Path, projection: dict[str, Any], ready: dict[str, Any]) -> dict[str, Any]:
    """Finish only the candidate READY half of a custody-first publication.

    The caller must already have authenticated custody's matching public
    commitment.  This routine never reads custody data; it requires the sole
    pre-existing candidate byte to equal the supplied safe projection and then
    publishes READY with the same non-replace hard-link/fsync boundaries as the
    regular publisher.
    """
    _candidate_ready(ready)
    target = output.resolve(strict=True)
    parent = target.parent
    parent_identity = _directory_identity(parent, "output_parent_identity_drift")
    if not output.is_absolute() or _directory_identity(parent, "output_parent_identity_drift") != parent_identity or target.is_symlink() or not target.is_dir() or {entry.name for entry in target.iterdir()} != {"projection.json"}:
        raise CustodyError("candidate_ready_resume_invalid")
    raw, _identity, raw_sha = _snapshot(target / "projection.json", "candidate_ready_resume_invalid")
    checked = validate_candidate_projection(_decode(raw, "candidate_ready_resume_invalid"))
    if raw != _bytes(projection) or raw_sha != ready["projection"]["raw_sha256"] or canonical_sha256(checked) != ready["projection"]["canonical_sha256"]:
        raise CustodyError("candidate_ready_resume_binding_invalid")
    temporary, final = target / ".READY.json.tmp", target / "READY.json"
    try:
        _write(temporary, ready)
        temp_identity = _snapshot(temporary, "candidate_ready_resume_invalid")[1]
        os.link(temporary, final)
        _fsync_directory(target, code="candidate_ready_resume_directory_fsync_failed")
        linked = os.lstat(final)
        if (linked.st_dev, linked.st_ino) != temp_identity or not stat.S_ISREG(linked.st_mode):
            raise CustodyError("candidate_ready_resume_alias_invalid")
        os.unlink(temporary)
        _fsync_directory(target, code="candidate_ready_resume_cleanup_fsync_failed")
        _candidate_ready(_decode(_snapshot(final, "candidate_ready_resume_invalid")[0], "candidate_ready_resume_invalid"))
    except FileExistsError as exc:
        raise CustodyError("candidate_ready_resume_collision") from exc
    except Exception:
        # A remaining temporary is non-loadable and must not be adopted on a
        # subsequent retry; exact-byte retry begins from the trusted custody
        # generation, not a half-written candidate marker.
        if temporary.exists():
            temporary.unlink()
        raise
    return dict(ready)


def _publish_custody(output: Path, custody: dict[str, Any], projection: dict[str, Any], candidate_ready: Mapping[str, Any], *, binding_secret: bytes) -> dict[str, Any]:
    ready = {"schema": CUSTODY_READY_SCHEMA, "generation_id": candidate_ready["generation_id"], "candidate_projection": dict(candidate_ready["projection"]), "custody": {"raw_sha256": hashlib.sha256(_bytes(custody)).hexdigest(), "canonical_sha256": canonical_sha256(custody)}, "durability": _durability_receipt()}
    return _publish_single(output, payload_name="sealed-custody.json", payload=custody, ready=ready, payload_binding=ready["custody"], validator=lambda value: _validate_custody(value, projection, binding_secret=binding_secret))


def build_prelabel_bundle(*, canonical_root: Path, premix_root: Path, candidate_output_dir: Path, custody_output_dir: Path, staging_root: Path, secret: bytes, selection: SelectionConfig) -> dict[str, Any]:
    if not isinstance(secret, bytes) or len(secret) < 16: raise CustodyError("secret_key_invalid")
    _require_builder_platform()
    _distinct_outputs(candidate_output_dir, custody_output_dir)
    canonical_dir, premix_dir = _subroot(canonical_root, "evidence_questions"), _subroot(premix_root, "pre_mixed_testcases")
    selection.validate()
    inventory = _source_size_inventory(canonical_dir, premix_dir)
    staging_root, staging_preflight = _staging_receipt(staging_root, inventory)
    index = _streaming_index(canonical_dir, premix_dir, staging_root, staging_preflight=staging_preflight)
    index_live = True
    try:
        _verify_index_bytes(index, "sqlite_index_preselection_drift")
        context_values = _indexed_context_values(index.database)
        if selection.is_census_v1:
            desired_contexts = context_values
        else:
            if any(rank >= len(context_values) for rank in selection.context_rank_indices):
                raise CustodyError("context_rank_unavailable")
            desired_contexts = [context_values[rank] for rank in selection.context_rank_indices]
        ledger = _validate_quarantine_ledger(
            _quarantine_ledger(index.database, index.revision, desired_contexts, census_observed_pairs=selection.is_census_v1),
            index.revision,
        )
        chosen, receipt = _selection_rows_sql(index.database, secret, index.revision, selection, desired_contexts, ledger)
        if selection.is_census_v1:
            count = _persist_census_payloads_sql(index.database, chosen, secret, index.revision)
            receipt = _spooled_census_receipt(index.database, receipt, index.revision)
            connection = _index_connection(index.database)
            try:
                candidate_text_count = connection.execute("SELECT COALESCE(SUM(json_array_length(payload, '$.candidates')), 0) FROM materialized_corpora").fetchone()[0]
            finally:
                connection.close()
            if not isinstance(candidate_text_count, int) or candidate_text_count <= 0:
                raise CustodyError("census_candidate_text_count_invalid")
            _raw, index.database_identity, index.database_sha256 = _snapshot(index.database, "sqlite_index_postmaterialization_drift", retain=False)
            index.owned_files[index.database] = index.database_identity
            _verify_index_bytes(index, "sqlite_index_postmaterialization_drift")
            dataset = {"canonical_sha256": index.canonical_digest, "premix_sha256": index.premix_digest, "revision_sha256": index.revision, "source_inventory_sha256": index.staging_receipt["source_inventory_sha256"]}
            candidate_source, projection_sha256, custody_source, custody_sha256 = _spool_census_publications(staging_root, index.database, dataset, receipt, secret, count)
            try:
                _verify_streaming_sources(canonical_dir, premix_dir, index)
                index.close(failure_cleanup=True); index_live = False
                generation_id = _opaque(secret, index.revision, "published-generation", receipt)
                candidate_ready = _candidate_ready({"schema": CANDIDATE_READY_SCHEMA, "generation_id": generation_id, "projection": {"raw_sha256": projection_sha256, "canonical_sha256": projection_sha256}, "durability": _durability_receipt()})
                custody_ready = _custody_ready({"schema": CUSTODY_READY_SCHEMA, "generation_id": generation_id, "candidate_projection": dict(candidate_ready["projection"]), "custody": {"raw_sha256": custody_sha256, "canonical_sha256": custody_sha256}, "durability": _durability_receipt()})
                published_custody_ready = _publish_streamed_single(custody_output_dir, payload_name="sealed-custody.json", source=custody_source, ready=custody_ready, payload_binding=custody_ready["custody"])
                published_candidate_ready = _publish_streamed_single(candidate_output_dir, payload_name="projection.json", source=candidate_source, ready=candidate_ready, payload_binding=candidate_ready["projection"])
            finally:
                candidate_source.unlink(missing_ok=True); custody_source.unlink(missing_ok=True)
            return {"candidate_output_dir": str(candidate_output_dir), "custody_output_dir": str(custody_output_dir), "dataset": dataset, "query_count": receipt["selected_item_context_count"], "candidate_text_count": candidate_text_count, "projection_sha256": projection_sha256, "projection_raw_sha256": published_candidate_ready["projection"]["raw_sha256"], "projection_canonical_sha256": published_candidate_ready["projection"]["canonical_sha256"], "custody_raw_sha256": published_custody_ready["custody"]["raw_sha256"], "custody_canonical_sha256": published_custody_ready["custody"]["canonical_sha256"], "generation_id": generation_id, "durability": published_candidate_ready["durability"], "staging_preflight": index.staging_receipt, "selection_receipt": receipt}
        corpora, projection_items, custody_items = _selected_payloads_sql(index.database, chosen, secret, index.revision)
        _verify_index_bytes(index, "sqlite_index_postmaterialization_drift")
        dataset = {"canonical_sha256": index.canonical_digest, "premix_sha256": index.premix_digest, "revision_sha256": index.revision, "source_inventory_sha256": index.staging_receipt["source_inventory_sha256"]}
        projection = {"schema": SCHEMA, "dataset": dataset, "selection_receipt": receipt, "corpora": corpora, "items": projection_items}
        validate_candidate_projection(projection)
        custody = {"schema": CUSTODY_SCHEMA, "projection_sha256": canonical_sha256(projection), "dataset": dataset, "selection_receipt": receipt, "mapping_status": {"status": "not_attempted", "scoring_permitted": False, "reason": "aerp7_prelabel_slice_has_no_span_mapping", "unresolved_item_context_count": len(projection_items)}, "items": custody_items}
        projection_by_item = {row["item_id"]: row for row in projection_items}; corpus_by_id = {row["corpus_id"]: row for row in corpora}
        for item in custody_items:
            item["binding_commitment"] = _custody_item_commitment(secret, dataset["revision_sha256"], item, projection_by_item[item["item_id"]], corpus_by_id[item["corpus_id"]])
        custody["binding"] = {"algorithm": BOUND_CUSTODY_ALGORITHM, "revision_sha256": dataset["revision_sha256"], "commitment": _custody_commitment(secret, dataset["revision_sha256"], custody, projection)}
        _validate_custody(custody, projection, binding_secret=secret)
        _verify_streaming_sources(canonical_dir, premix_dir, index)
        # No READY can be published while label-bearing index state remains at
        # its original location.  Success cleanup uses the same tombstone
        # protocol as failure cleanup, then revokes the live handle exactly once.
        try:
            index.close(failure_cleanup=True)
        except Exception:
            index_live = False
            raise
        index_live = False
        # Deterministic across a crash/retry: it is derived from the already
        # frozen source revision and full selection receipt, never a result.
        generation_id = _opaque(secret, index.revision, "published-generation", receipt)
        candidate_ready = _candidate_ready_value(projection, generation_id)
        # Custody is published first.  A subsequent candidate-publication failure
        # leaves a non-clobbering, unusable custody generation rather than a
        # candidate-visible label file.
        custody_ready = _publish_custody(custody_output_dir, custody, projection, candidate_ready, binding_secret=secret)
        published_candidate_ready = _publish_candidate(candidate_output_dir, projection, candidate_ready)
    except Exception as primary_error:
        if index_live:
            try:
                index.close(failure_cleanup=True)
            except Exception as cleanup_error:
                primary_receipt = primary_error.receipt if isinstance(primary_error, CustodyError) else {"type": type(primary_error).__name__, "message": str(primary_error)}
                cleanup_receipt = cleanup_error.receipt if isinstance(cleanup_error, CustodyError) else {"type": type(cleanup_error).__name__, "message": str(cleanup_error)}
                raise CustodyError("build_primary_and_index_cleanup_failed", primary_error=primary_receipt, cleanup_error=cleanup_receipt) from primary_error
        raise
    else:
        return {"candidate_output_dir": str(candidate_output_dir), "custody_output_dir": str(custody_output_dir), "dataset": dataset, "query_count": len(projection_items), "candidate_text_count": sum(len(corpus["candidates"]) for corpus in corpora), "projection_sha256": custody["projection_sha256"], "projection_raw_sha256": published_candidate_ready["projection"]["raw_sha256"], "projection_canonical_sha256": published_candidate_ready["projection"]["canonical_sha256"], "custody_raw_sha256": custody_ready["custody"]["raw_sha256"], "custody_canonical_sha256": custody_ready["custody"]["canonical_sha256"], "generation_id": generation_id, "durability": published_candidate_ready["durability"], "staging_preflight": index.staging_receipt, "selection_receipt": receipt}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish AERP-7 candidate-safe ConvoMem prelabels.")
    for flag in ("canonical-root", "premix-root", "candidate-output-dir", "custody-output-dir", "staging-root", "secret-key-file"): parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--census-v1", action="store_true", help="formal selector: all candidate-visible units; forbids RNG")
    parser.add_argument("--seed", type=int); parser.add_argument("--persona-quota", type=int); parser.add_argument("--per-persona-group-quota", type=int); parser.add_argument("--context-rank-index", type=int, action="append")
    args = parser.parse_args(argv)
    try:
        _safe_existing_ancestors(args.secret_key_file, "secret_key_path_invalid")
        if args.secret_key_file.is_symlink() or not args.secret_key_file.is_file(): raise CustodyError("secret_key_path_invalid")
        secret_raw, _secret_identity, _secret_digest = _snapshot(args.secret_key_file, "secret_key_snapshot_invalid")
        if args.census_v1:
            if any(value is not None for value in (args.seed, args.persona_quota, args.per_persona_group_quota, args.context_rank_index)):
                raise CustodyError("invalid_selection_config", field="census_cli_mixed")
            selection = SelectionConfig.census_v1()
        else:
            if args.seed is None or args.persona_quota is None or args.per_persona_group_quota is None or not args.context_rank_index:
                raise CustodyError("invalid_selection_config", field="sample_cli_incomplete")
            selection = SelectionConfig(args.seed, args.persona_quota, args.per_persona_group_quota, tuple(args.context_rank_index))
        result = build_prelabel_bundle(canonical_root=args.canonical_root, premix_root=args.premix_root, candidate_output_dir=args.candidate_output_dir, custody_output_dir=args.custody_output_dir, staging_root=args.staging_root, secret=secret_raw, selection=selection)
    except CustodyError as exc:
        print(json.dumps({"status": "failed", "receipt": exc.receipt}, sort_keys=True, separators=(",", ":"))); return 2
    print(json.dumps({"status": "published", **result}, sort_keys=True, separators=(",", ":"))); return 0


if __name__ == "__main__": raise SystemExit(main())
