"""AERP-5 v2: formal public-nonblind paired product runner for LoCoMo.

The implementation deliberately separates a label-free ranking worker from the
coordinator that is later allowed to score frozen output.  It is a public-data
engineering confirmation, therefore ``confirmation_claim`` is permanently
false; it is nevertheless fail-closed enough to be a reproducible product
comparison.  Resource-gate eligibility remains a separate, later system audit.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import inspect
import json
import math
import os
from pathlib import Path
import platform
import random
import sqlite3
import stat
import statistics
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from benchmarks import aerp1_locomo_three_way as aerp1
from benchmarks import aerp2_product_six_view_locomo as aerp2
from benchmarks import aerp4_locomo_paired_receipts as a4paired
from benchmarks import aerp5_product_paired_locomo as v1
from mempalace_rpg import FixedP5Policy, FixedSixViewPolicy, RpgMemoryKernel, SixViewRanker
from mempalace_rpg.retrieval import SIX_VIEW_WEIGHTS


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_MANIFEST_PATH = Path(__file__).with_name("aerp5_product_paired_locomo_v2_manifest.json")
TOP_K = 10
EXPECTED_ORIGINAL_DIALOG_COUNT = 5882
CURRENT_REPEATS = 2
ORIGINAL_REPEATS = 5
RSS_CAP_BYTES = 2_147_483_648
SCHEMA = "aerp5-product-paired-locomo-v2"
EXPECTED_DEFAULT_MANIFEST_SHA256 = "1f76246e627909bbb67adcb8f3809de2b40c3432b0feeb80ed38425884dac388"
EXPECTED_SCIENTIFIC_GATES = {
    "projected_member_count": 1982,
    "original_dialog_count": EXPECTED_ORIGINAL_DIALOG_COUNT,
    "strict_top_k": 10,
    "aerp4_lineage_membership_query_input_and_policy_bound": True,
    "minilm_p5_checkpoint_before_label_custody": True,
    "coordinator_never_opens_aerp4_study_or_custody": True,
    "rerun_projection_digest_equal": True,
    "score_only_after_freeze": True,
    "original_index_build_replicates": ORIGINAL_REPEATS,
    "current_deterministic_repeats": CURRENT_REPEATS,
    "original_estimand": "per_query_arithmetic_mean_over_five_fresh_index_build_replicates",
    "hierarchical_bootstrap_seed": 20260822,
    "hierarchical_bootstrap_resamples": 5000,
    "p5_vs_original_question_ci_lower_strictly_gt": 0.0,
    "p5_vs_original_conversation_ci_lower_strictly_gt": 0.0,
    "hard_cat1_2_and_cat5_report_required": True,
    "public_nonblind": True,
    "confirmation_claim": False,
    "resource_gate_after_engineering_only": True,
}
ARM_ORIGINAL = "original_public_product_minilm"
ARM_P5 = "current_fixed_p5_product"
ARM_SIX_VIEW = "current_fixed_six_view_product_secondary"
ALL_ARMS = (ARM_ORIGINAL, ARM_P5, ARM_SIX_VIEW)
REPEATS_BY_ARM = {ARM_ORIGINAL: ORIGINAL_REPEATS, ARM_P5: CURRENT_REPEATS, ARM_SIX_VIEW: CURRENT_REPEATS}
PROJECTION_SCHEMA = SCHEMA + "-projection-v2"
# AERP4's historical frozen P5 stream used the byte-pinned BGE fp32 adapter.
# AERP5's fair product comparison instead pins every primary arm to the native
# MiniLM product encoder.  These are deliberately distinct identities: an
# exact ranking equality check across them is not a reproducibility check.
AERP4_HISTORICAL_BGE = {
    "family": "historical_bge",
    "precision": "fp32",
    "model_manifest_sha256": "454d14761089976b0437135fd70c6da49fc836ad9344d43b0a693d343fc0ea85",
    "model_onnx_sha256": "828e1496d7fabb79cfa4dcd84fa38625c0d3d21da474a00f08db0f559940cf35",
}
MINILM_P5_CHECKPOINT_SCHEMA = "aerp5-minilm-p5-checkpoint-v1"
# This asserts implementation availability, never experimental completion.
END_TO_END_PRODUCT_WORKER_IMPLEMENTED = True


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _publish_nonreplace(path: Path, payload: bytes) -> None:
    """Publish without the replace race: hard-link creation is exclusive."""
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with open(temporary, "xb") as handle: handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path)  # fails if another coordinator published first
    except FileExistsError:
        raise FileExistsError("refusing to clobber existing formal output")
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


@dataclass(frozen=True)
class PinnedJson:
    path: Path
    sha256: str

    @classmethod
    def pin(cls, path: Path | str, expected: str) -> "PinnedJson":
        target = Path(path).resolve()
        if not target.is_file() or sha256_file(target) != expected:
            raise ValueError("pinned JSON digest mismatch")
        return cls(target, expected)

    def load(self) -> dict[str, Any]:
        if sha256_file(self.path) != self.sha256:
            raise RuntimeError("pinned JSON drifted during run")
        return _json(self.path)


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or CANONICAL_MANIFEST_PATH
    if sha256_file(manifest_path) != EXPECTED_DEFAULT_MANIFEST_SHA256:
        raise ValueError("AERP5 v2 canonical manifest bytes drifted")
    manifest = _json(manifest_path)
    required = {"schema", "dataset", "projection", "original", "aerp4", "run", "arms", "scorer_contract", "scientific_gates"}
    if set(manifest) != required or manifest["schema"] != SCHEMA:
        raise ValueError("AERP5 v2 manifest schema is malformed")
    if manifest["run"].get("repeats_by_arm") != REPEATS_BY_ARM or manifest["run"].get("top_k") != TOP_K:
        raise ValueError("AERP5 v2 repeat/TopK contract drifted")
    if manifest["run"].get("rss_cap_bytes") != RSS_CAP_BYTES:
        raise ValueError("AERP5 v2 RSS contract drifted")
    projection = manifest.get("projection")
    if not isinstance(projection, Mapping) or set(projection) != {
        "path", "bytes", "file_sha256", "content_sha256", "schema", "conversation_count", "item_count"
    } or projection.get("schema") != PROJECTION_SCHEMA or projection.get("conversation_count") != 10 or projection.get("item_count") != 1982:
        raise ValueError("AERP5 v2 canonical label-free projection pin is malformed")
    if not isinstance(projection.get("path"), str) or not isinstance(projection.get("bytes"), int) or projection["bytes"] <= 0:
        raise ValueError("AERP5 v2 canonical label-free projection path/bytes are malformed")
    _token(projection.get("file_sha256"), "canonical label-free projection file digest")
    _token(projection.get("content_sha256"), "canonical label-free projection content digest")
    scorer_contract = manifest.get("scorer_contract")
    if not isinstance(scorer_contract, Mapping) or set(scorer_contract) != {"receipt", "receipt_sha256"}:
        raise ValueError("AERP5 v2 canonical scorer contract is malformed")
    if not isinstance(scorer_contract["receipt"], Mapping):
        raise ValueError("AERP5 v2 canonical scorer contract receipt is malformed")
    _token(scorer_contract["receipt_sha256"], "canonical scorer contract receipt digest")
    if canonical_sha256(scorer_contract["receipt"]) != scorer_contract["receipt_sha256"]:
        raise ValueError("AERP5 v2 canonical scorer contract receipt digest drifted")
    if set(manifest["arms"]) != set(ALL_ARMS) or manifest["arms"].get(ARM_P5, {}).get("primary") is not True:
        raise ValueError("AERP5 v2 arms contract is malformed")
    if manifest.get("scientific_gates") != EXPECTED_SCIENTIFIC_GATES:
        raise ValueError("AERP5 v2 scientific gates differ from the canonical zero-threshold contract")
    if "tau" in _canonical(manifest).decode("ascii") or "router" in _canonical(manifest).decode("ascii"):
        raise ValueError("AERP5 v2 must not admit tunable tau/router inputs")
    return manifest


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a SHA-256 token")
    return value


def _freeze_items(value: Mapping[str, Any], partition: str) -> list[dict[str, Any]]:
    if value.get("partition") != partition or value.get("status") != "complete":
        raise ValueError(f"AERP4 {partition} ranking freeze is malformed")
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("AERP4 ranking freeze items are missing")
    result: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            raise ValueError("AERP4 ranking freeze item is malformed")
        _token(row.get("item_token"), "item token")
        for field in ("raw_top10", "p5_top10"):
            ranked = row.get(field)
            if not isinstance(ranked, list) or len(ranked) != TOP_K or len(set(ranked)) != TOP_K:
                raise ValueError(f"AERP4 {field} must be strict Top-10")
        result.append(row)
    return result


def _pinned_value(value: PinnedJson | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(value, PinnedJson):
        return value.load(), value.sha256
    if not isinstance(value, Mapping):
        raise TypeError("AERP4 input must be a pinned JSON receipt or object")
    copied = dict(value)
    return copied, canonical_sha256(copied)


def validate_aerp4_membership(
    *, train_freeze: PinnedJson | Mapping[str, Any], dev_freeze: PinnedJson | Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate exact AERP4 membership using only label-free ranking freezes."""
    pins = manifest["aerp4"]
    train_freeze, train_sha = _pinned_value(train_freeze); dev_freeze, dev_sha = _pinned_value(dev_freeze)
    if train_sha != pins["train_ranking_freeze_sha256"]:
        raise ValueError("AERP4 train ranking freeze digest mismatch")
    if dev_sha != pins["dev_ranking_freeze_sha256"]:
        raise ValueError("AERP4 dev ranking freeze digest mismatch")
    if train_freeze.get("study_sha256") != pins["study_sha256"] or dev_freeze.get("study_sha256") != pins["study_sha256"]:
        raise ValueError("AERP4 ranking freeze does not bind the canonical study")
    excluded = pins.get("excluded_item_tokens")
    if not isinstance(excluded, list) or any(_token(token, "AERP4 exclusion token") != token for token in excluded):
        raise ValueError("AERP4 manifest exclusion receipt is malformed")
    if len(excluded) != 4 or len(set(excluded)) != 4:
        raise ValueError("AERP4 exclusion count must equal four")
    train = _freeze_items(train_freeze, "train")
    dev = _freeze_items(dev_freeze, "dev")
    tokens = [row["item_token"] for row in train + dev]
    if len(tokens) != 1982 or len(set(tokens)) != 1982 or set(tokens) & set(excluded):
        raise ValueError("AERP4 frozen membership is not exactly 1,982 allowed items")
    return {"item_tokens": tuple(sorted(tokens)), "excluded_item_tokens": tuple(sorted(excluded)), "count": len(tokens)}


def _fixed_p5_semantics() -> dict[str, Any]:
    """The static P5 policy contract shared by the historical lineage and v2."""
    return {
        "schema": FixedP5Policy.schema,
        "policy": FixedP5Policy.policy,
        "p5_weights": dict(FixedP5Policy._config(60)["p5_weights"]),
        "ordinary_sum_view_order": list(SIX_VIEW_WEIGHTS),
        "rrf_k": 60,
        "tie_break": "descending_rrf_then_lexicographic_ranking_key",
    }


def expected_minilm_encoder_identity(model_sha256: str) -> str:
    _token(model_sha256, "MiniLM model digest")
    return f"chromadb-native-minilm:{model_sha256}"


def _aerp4_query_input_digests(frozen_rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for row in frozen_rows:
        if not isinstance(row, Mapping):
            raise ValueError("AERP4 lineage row is malformed")
        token = row.get("item_token")
        _token(token, "AERP4 lineage item token")
        if token in result:
            raise ValueError("AERP4 lineage has duplicate item tokens")
        result[token] = {
            "query_sha256": _token(row.get("query_sha256"), "AERP4 query digest"),
            "input_sha256": _token(row.get("input_sha256"), "AERP4 input digest"),
        }
    if len(result) != 1982:
        raise ValueError("AERP4 lineage must contain exactly 1,982 query/input digests")
    return result


def _current_p5_query_input_digests(freeze: Mapping[str, Any], *, expected_model_sha256: str) -> dict[str, dict[str, str]]:
    expected_encoder = expected_minilm_encoder_identity(expected_model_sha256)
    trace_items = freeze.get("trace_receipt", {}).get("items")
    if not isinstance(trace_items, Mapping) or len(trace_items) != 1982:
        raise ValueError("current P5 trace items are unavailable for checkpointing")
    result: dict[str, dict[str, str]] = {}
    for token, receipt in trace_items.items():
        _token(token, "current P5 item token")
        if not isinstance(receipt, Mapping):
            raise ValueError("current P5 trace item is malformed")
        retrieval = receipt.get("stable_trace", {}).get("retrieval_ranking")
        if not isinstance(retrieval, Mapping) or retrieval.get("encoder_identity") != expected_encoder:
            raise RuntimeError("current P5 encoder identity is not the pinned native MiniLM identity")
        result[token] = {
            "query_sha256": _token(retrieval.get("query_sha256"), "current P5 query digest"),
            "input_sha256": _token(retrieval.get("input_sha256"), "current P5 input digest"),
        }
    return result


def validate_aerp4_lineage(
    *, frozen_rows: Iterable[Mapping[str, Any]], current_p5: Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Bind AERP4's historical inputs/semantics without comparing BGE and MiniLM rankings."""
    pins = manifest.get("aerp4")
    if not isinstance(pins, Mapping):
        raise ValueError("AERP4 lineage pins are malformed")
    if pins.get("historical_encoder") != AERP4_HISTORICAL_BGE:
        raise ValueError("AERP4 historical BGE identity/pins differ from the frozen lineage")
    if pins.get("fixed_p5_semantics") != _fixed_p5_semantics():
        raise ValueError("AERP4 fixed P5 semantics differ from the frozen lineage")
    historical = _aerp4_query_input_digests(frozen_rows)
    current = _current_p5_query_input_digests(
        current_p5, expected_model_sha256=manifest["run"]["model"]["file_tree_sha256"]
    )
    if set(historical) != set(current):
        raise RuntimeError("current P5 membership differs from the AERP4 lineage")
    for token in historical:
        if historical[token] != current[token]:
            raise RuntimeError(f"current P5 query/input differs from AERP4 lineage: {token}")
    return {
        "schema": "aerp5-aerp4-bge-lineage-v1",
        "historical_encoder": dict(AERP4_HISTORICAL_BGE),
        "fixed_p5_semantics": _fixed_p5_semantics(),
        "membership_sha256": canonical_sha256(sorted(historical)),
        "query_input_sha256": canonical_sha256(historical),
    }


def validate_aerp4_lineage_anchor(receipt: Any, *, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Compare a candidate lineage receipt to the immutable manifest anchor."""
    if not isinstance(receipt, Mapping):
        raise ValueError("AERP4 lineage receipt is malformed")
    pins = manifest.get("aerp4")
    anchor = pins.get("lineage_anchor") if isinstance(pins, Mapping) else None
    if not isinstance(anchor, Mapping) or set(anchor) != {
        "schema", "membership_sha256", "query_input_sha256", "receipt_sha256"
    }:
        raise ValueError("canonical AERP4 lineage anchor is malformed")
    expected = {
        "schema": "aerp5-aerp4-bge-lineage-v1",
        "membership_sha256": receipt.get("membership_sha256"),
        "query_input_sha256": receipt.get("query_input_sha256"),
        "receipt_sha256": canonical_sha256(dict(receipt)),
    }
    for name in ("membership_sha256", "query_input_sha256", "receipt_sha256"):
        _token(anchor.get(name), f"canonical AERP4 lineage anchor {name}")
    if expected != dict(anchor):
        raise RuntimeError("AERP4 lineage receipt differs from the canonical manifest anchor")
    return dict(receipt)


def _reject_projection_label_or_scorer_fields(value: Any) -> None:
    """Reject labels at the projection boundary, including nested metadata."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("public projection field names must be strings")
            if "label" in key.casefold() or "scorer" in key.casefold():
                raise ValueError("public projection contains forbidden label/scorer field")
            _reject_projection_label_or_scorer_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_projection_label_or_scorer_fields(child)


def _validate_public_projection(value: Any) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return the exact item/conversation views of the canonical compact input.

    The corpus is intentionally stored once per conversation.  This is both a
    memory contract (workers must not retain 1,982 copies) and an audit
    contract: each item contains only its question identity and a
    ``conversation_id`` reference.
    """
    _reject_projection_label_or_scorer_fields(value)
    if not isinstance(value, Mapping) or set(value) != {"schema", "conversations", "items"}:
        raise ValueError("public projection schema is malformed")
    if value.get("schema") != PROJECTION_SCHEMA:
        raise ValueError("public projection schema version drifted")
    conversations_raw, items_raw = value.get("conversations"), value.get("items")
    if not isinstance(conversations_raw, list) or len(conversations_raw) != 10 or not isinstance(items_raw, list) or len(items_raw) != 1982:
        raise ValueError("public projection must contain exact 10 conversations and 1,982 items")
    conversations: dict[str, dict[str, Any]] = {}
    for conversation in conversations_raw:
        if not isinstance(conversation, Mapping) or set(conversation) != {"conversation_id", "conversation_token", "sessions"}:
            raise ValueError("public projection conversation is malformed")
        conversation_id = conversation.get("conversation_id")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("public projection conversation id is malformed")
        _token(conversation.get("conversation_token"), "public conversation token")
        if not isinstance(conversation.get("sessions"), list):
            raise ValueError("public projection conversation sessions are malformed")
        if conversation_id in conversations:
            raise ValueError("public projection has duplicate conversation corpus")
        conversations[conversation_id] = dict(conversation)
    items: list[dict[str, Any]] = []
    tokens: set[str] = set(); item_ids: set[str] = set()
    for item in items_raw:
        if not isinstance(item, Mapping) or set(item) != {"item_token", "item_id", "conversation_id", "query"}:
            raise ValueError("public projection item must only reference conversation_id")
        token = _token(item.get("item_token"), "public item token")
        item_id, conversation_id, query = item.get("item_id"), item.get("conversation_id"), item.get("query")
        if token in tokens or not isinstance(item_id, str) or not item_id or item_id in item_ids or not isinstance(conversation_id, str) or conversation_id not in conversations or not isinstance(query, str):
            raise ValueError("public projection item membership/query is malformed")
        tokens.add(token); item_ids.add(item_id)
        items.append(dict(item))
    if len(tokens) != 1982:
        raise ValueError("public projection item tokens are not exact 1,982")
    if items != sorted(items, key=lambda row: row["item_token"]) or list(conversations) != sorted(conversations):
        raise ValueError("public projection ordering is not canonical")
    return items, conversations


def project_public_items(
    *, public_items: Iterable[Mapping[str, Any]], allowed_item_tokens: Iterable[str]
) -> dict[str, Any]:
    """Normalize official public inputs into a one-corpus-per-conversation projection.

    Every repeated source copy of a conversation must have byte-identical
    sanitized sessions and the same opaque conversation token.  The resulting
    item rows deliberately do not contain corpus/session fields.
    """
    allowed = set(allowed_item_tokens)
    projected: list[dict[str, Any]] = []
    conversations: dict[str, dict[str, Any]] = {}
    for source in public_items:
        _reject_projection_label_or_scorer_fields(source)
        token = _token(source.get("item_token"), "public item token")
        fields = {key: source.get(key) for key in ("item_token", "item_id", "conversation_id", "conversation_token", "query", "sessions")}
        if not isinstance(fields["query"], str) or not isinstance(fields["sessions"], list) or not isinstance(fields["item_id"], str) or not fields["item_id"] or not isinstance(fields["conversation_id"], str) or not fields["conversation_id"]:
            raise ValueError("public item lacks query/session projection")
        _token(fields["conversation_token"], "public conversation token")
        conversation = {
            "conversation_id": fields["conversation_id"],
            "conversation_token": fields["conversation_token"],
            "sessions": fields["sessions"],
        }
        prior = conversations.get(fields["conversation_id"])
        if prior is None:
            conversations[fields["conversation_id"]] = conversation
        elif canonical_sha256(prior) != canonical_sha256(conversation):
            raise ValueError("repeated public conversation sessions/token diverged")
        if token not in allowed:
            continue
        projected.append({key: fields[key] for key in ("item_token", "item_id", "conversation_id", "query")})
    if len(projected) != len(allowed) or {row["item_token"] for row in projected} != allowed:
        raise ValueError("official bundle projection does not exactly match AERP4 membership")
    normalized = {
        "schema": PROJECTION_SCHEMA,
        "conversations": [conversations[key] for key in sorted(conversations)],
        "items": sorted(projected, key=lambda row: row["item_token"]),
    }
    _validate_public_projection(normalized)
    return normalized


def build_public_projection(*, dataset: Path, original_root: Path, allowed_item_tokens: Iterable[str]) -> dict[str, Any]:
    """Offline projection producer only; formal coordinators never call it."""
    _palace, _searcher, protocol, _state = v1.load_original_product(original_root)
    loaded = protocol.load_official_locomo10(dataset)
    allowed = set(allowed_item_tokens); result = []; ordinal = 0
    for conversation_index, sample in enumerate(loaded.records):
        conversation_id = f"conversation_{conversation_index:06d}"
        sessions, _mapping = protocol._sanitize_conversation(sample)
        for qa in sample["qa"]:
            item_id = f"item_{ordinal:06d}"; ordinal += 1
            token = a4paired._token("aerp4:item", item_id)
            result.append({"item_token": token, "item_id": item_id, "conversation_id": conversation_id, "conversation_token": a4paired._token("aerp4:group", conversation_id), "query": qa["question"], "sessions": sessions})
    if ordinal != 1986:
        raise ValueError("official LoCoMo source denominator drifted")
    return project_public_items(public_items=result, allowed_item_tokens=allowed)


def load_pinned_projection(*, projection_path: Path, manifest: Mapping[str, Any]) -> tuple[dict[str, Any], str, str]:
    """Load the one immutable label-free worker input permitted to formal runs."""
    pin = manifest.get("projection")
    if not isinstance(pin, Mapping):
        raise ValueError("canonical label-free projection pin is missing")
    expected_path = Path(str(pin.get("path"))).resolve()
    actual_path = projection_path.resolve()
    if actual_path != expected_path:
        raise ValueError("formal coordinator requires the canonical label-free projection path")
    if not actual_path.is_file() or actual_path.stat().st_size != pin.get("bytes"):
        raise RuntimeError("canonical label-free projection bytes drifted")
    file_sha256 = sha256_file(actual_path)
    if file_sha256 != pin.get("file_sha256"):
        raise RuntimeError("canonical label-free projection file digest drifted")
    try:
        projection = _json(actual_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("canonical label-free projection is not valid JSON") from exc
    items, conversations = _validate_public_projection(projection)
    content_sha256 = canonical_sha256(projection)
    if (
        content_sha256 != pin.get("content_sha256")
        or projection.get("schema") != pin.get("schema")
        or len(items) != pin.get("item_count")
        or len(conversations) != pin.get("conversation_count")
    ):
        raise RuntimeError("canonical label-free projection content/schema drifted")
    return projection, file_sha256, content_sha256


def _evidence_tokens(dialog_ids: Sequence[str]) -> list[str]:
    return [a4paired.evidence_token_from_ranking_key_sha256(hashlib.sha256(dialog.encode("utf-8")).hexdigest()) for dialog in dialog_ids]


def actual_onnx_session_providers(native_embedding: Any) -> list[str]:
    """Read the provider from the live ONNX session after an encode occurred."""
    seen: set[int] = set(); queue = [native_embedding]
    while queue:
        value = queue.pop(0)
        if id(value) in seen: continue
        seen.add(id(value))
        getter = getattr(value, "get_providers", None)
        if callable(getter):
            providers = getter()
            if not isinstance(providers, list) or providers != ["CPUExecutionProvider"]: raise RuntimeError(f"live ONNX providers escaped CPU freeze: {providers!r}")
            return providers
        for name in ("_session", "session", "_model", "model", "_ort_session"):
            child = getattr(value, name, None)
            if child is not None: queue.append(child)
    raise RuntimeError("native Chroma embedding did not expose a live ONNX session")


IDENTITY_NAMESPACE_SCHEME = "conversation_id::aerp5::local_opaque_dialog_id"
IDENTITY_NAMESPACE_SEPARATOR = "::aerp5::"


def original_identity_namespace(conversations: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Create the global physical-ID namespace required by one shared Chroma collection."""
    rows: list[dict[str, str]] = []
    for conversation_id, conversation in sorted(conversations.items()):
        if IDENTITY_NAMESPACE_SEPARATOR in conversation_id:
            raise ValueError("conversation ID collides with original physical-ID separator")
        dialogs = v1.raw_dialogs({"sessions": conversation["sessions"]})
        for dialog in dialogs:
            local_id = dialog["id"]
            if IDENTITY_NAMESPACE_SEPARATOR in local_id:
                raise ValueError("local dialog ID collides with original physical-ID separator")
            rows.append({
                "conversation_id": conversation_id,
                "local_dialog_id": local_id,
                "physical_id": conversation_id + IDENTITY_NAMESPACE_SEPARATOR + local_id,
            })
    rows.sort(key=lambda row: row["physical_id"])
    if len(rows) != EXPECTED_ORIGINAL_DIALOG_COUNT:
        raise RuntimeError(f"original physical-ID corpus must contain exactly {EXPECTED_ORIGINAL_DIALOG_COUNT} dialogs")
    if len({row["physical_id"] for row in rows}) != len(rows):
        raise RuntimeError("original physical IDs are not globally unique")
    return {
        "schema": "aerp5-original-identity-namespace-v1",
        "scheme": IDENTITY_NAMESPACE_SCHEME,
        "rows": rows,
        "expected_unique_count": len(rows),
        "mapping_sha256": canonical_sha256(rows),
    }


def validate_original_identity_namespace_receipt(receipt: Any, *, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Validate worker-reported collection count against a projection-derived namespace."""
    if not isinstance(receipt, Mapping) or receipt.get("schema") != "aerp5-original-identity-namespace-v1":
        raise ValueError("original identity namespace receipt is malformed")
    if (
        receipt.get("scheme") != IDENTITY_NAMESPACE_SCHEME
        or receipt.get("expected_unique_count") != expected["expected_unique_count"]
        or receipt.get("mapping_sha256") != expected["mapping_sha256"]
        or receipt.get("actual_collection_count") != expected["expected_unique_count"]
    ):
        raise ValueError("original identity namespace receipt does not bind the projected corpus")
    return dict(receipt)


def _sqlite_embedding_count(palace_path: Path) -> int:
    database = palace_path / "chroma.sqlite3"
    if not database.is_file():
        raise RuntimeError("original Chroma SQLite catalog is missing after worker exit")
    with _readonly_sqlite_connection(database) as connection:
        row = connection.execute("SELECT COUNT(*) FROM embeddings").fetchone()
    if row is None or not isinstance(row[0], int):
        raise RuntimeError("original Chroma SQLite embedding count is malformed")
    return row[0]


def _readonly_sqlite_connection(database: Path) -> sqlite3.Connection:
    """Open a catalog in read-only mode; receipt inspection must not create locks/WAL."""
    return sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)


def _sqlite_hnsw_configuration(palace_path: Path) -> dict[str, Any]:
    database = palace_path / "chroma.sqlite3"
    if not database.is_file():
        raise RuntimeError("original Chroma SQLite catalog is missing")
    with _readonly_sqlite_connection(database) as connection:
        row = connection.execute(
            "SELECT schema_str FROM collections WHERE name = ?", (v1.ORIGINAL_COLLECTION,)
        ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise RuntimeError("original Chroma collection schema is missing")
    try:
        schema = json.loads(row[0])
        config = schema["keys"]["#embedding"]["float_list"]["vector_index"]["config"]
        hnsw = config["hnsw"]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("original Chroma collection schema does not expose HNSW configuration") from exc
    resolved = {key: config[key] for key in ("space",)} | {
        key: hnsw[key] for key in ("ef_construction", "ef_search", "max_neighbors", "num_threads", "batch_size", "sync_threshold", "resize_factor")
    }
    expected = _sqlite_hnsw_configuration_expected()
    if resolved != expected:
        raise RuntimeError(
            "original Chroma resolved HNSW configuration drifted: "
            f"resolved={_canonical(resolved).decode('utf-8')}; "
            f"expected={_canonical(expected).decode('utf-8')}"
        )
    return resolved


def _float32_embedding_digest(ids: Sequence[str], embeddings: Any) -> tuple[str, int, int]:
    try:
        vectors = embeddings.tolist()
    except AttributeError:
        vectors = list(embeddings)
    if len(vectors) != len(ids) or not vectors:
        raise RuntimeError("original Chroma stored embeddings are missing")
    rows = sorted(zip(ids, vectors), key=lambda pair: pair[0])
    digest = hashlib.sha256(); dimension: int | None = None
    for physical_id, vector in rows:
        if not isinstance(physical_id, str) or not physical_id:
            raise RuntimeError("original Chroma stored physical ID is malformed")
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            raise RuntimeError("original Chroma stored embedding is malformed")
        if dimension is None: dimension = len(values)
        if len(values) != dimension: raise RuntimeError("original Chroma stored embedding dimensions drifted")
        encoded = physical_id.encode("utf-8")
        digest.update(struct.pack("<I", len(encoded))); digest.update(encoded)
        digest.update(struct.pack("<I", len(values)))
        for value in values: digest.update(struct.pack("<f", value))
    return digest.hexdigest(), len(rows), int(dimension)


def _audit_storage_digest(palace_path: Path) -> dict[str, Any]:
    """Persisted bytes that a receipt audit is forbidden to modify."""
    database = palace_path / "chroma.sqlite3"
    if not database.is_file(): raise RuntimeError("original Chroma SQLite catalog is missing")
    segments = sorted({path.parent for path in palace_path.rglob("data_level0.bin")})
    if len(segments) != 1: raise RuntimeError("original Chroma must retain exactly one HNSW segment")
    # Snapshot every persisted backend file, rather than only the graph components
    # we report.  A product-open that mutates WAL/catalog/segment adjunct files must
    # be detected even if the four canonical HNSW files happen not to change.
    snapshot = []
    for path in sorted((item for item in palace_path.rglob("*") if item.is_file()), key=lambda item: item.relative_to(palace_path).as_posix()):
        relative = path.relative_to(palace_path).as_posix()
        snapshot.append({"path": relative, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    if not any(row["path"] == "chroma.sqlite3" for row in snapshot):
        raise RuntimeError("original Chroma backend snapshot lost SQLite catalog")
    rows = []
    for name in ("data_level0.bin", "header.bin", "length.bin", "link_lists.bin"):
        path = segments[0] / name
        if not path.is_file(): raise RuntimeError(f"original Chroma HNSW component missing: {name}")
        rows.append({"name": name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    immutable_snapshot = [row for row in snapshot if row["path"] != "chroma.sqlite3"]
    sqlite_row = next(row for row in snapshot if row["path"] == "chroma.sqlite3")
    return {
        "files": rows,
        "snapshot": snapshot,
        "immutable_snapshot": immutable_snapshot,
        "immutable_sha256": canonical_sha256(immutable_snapshot),
        "sqlite_file_sha256": sqlite_row["sha256"],
    }


def _quote_sqlite_identifier(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeError("SQLite identifier is malformed")
    return '"' + value.replace('"', '""') + '"'


def _sqlite_typed_value(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, int) and not isinstance(value, bool):
        return ["integer", value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RuntimeError("SQLite semantic snapshot contains non-finite real")
        return ["real", value]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    raise RuntimeError(f"SQLite semantic snapshot has unsupported value type: {type(value).__name__}")


def _sqlite_semantic_snapshot(palace_path: Path) -> dict[str, Any]:
    """Canonical SQLite state, excluding *only* acquire_write row values.

    Chroma 1.5.7 increments that lock counter when a direct client opens.  The
    table's schema remains bound; its values are checked separately against the
    sole permitted operational transition.
    """
    database = palace_path / "chroma.sqlite3"
    if not database.is_file():
        raise RuntimeError("original Chroma SQLite catalog is missing")
    with _readonly_sqlite_connection(database) as connection:
        masters = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        ).fetchall()
        schema = [[_sqlite_typed_value(value) for value in row] for row in masters]
        tables = [str(row[1]) for row in masters if row[0] == "table"]
        if "acquire_write" not in tables:
            raise RuntimeError("original Chroma SQLite acquire_write table is missing")
        contents: list[dict[str, Any]] = []
        acquire_rows: list[list[Any]] | None = None
        for name in tables:
            columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({_quote_sqlite_identifier(name)})")]
            if not columns:
                raise RuntimeError(f"SQLite semantic snapshot lacks columns for {name}")
            ordering = ", ".join(_quote_sqlite_identifier(column) for column in columns)
            rows = connection.execute(
                f"SELECT * FROM {_quote_sqlite_identifier(name)} ORDER BY {ordering}"
            ).fetchall()
            canonical_rows = [[_sqlite_typed_value(value) for value in row] for row in rows]
            if name == "acquire_write":
                acquire_rows = canonical_rows
            else:
                contents.append({"name": name, "columns": columns, "rows": canonical_rows})
    if acquire_rows is None:
        raise RuntimeError("original Chroma SQLite acquire_write rows are missing")
    semantic = {"schema": schema, "tables": contents}
    return {"semantic_sha256": canonical_sha256(semantic), "acquire_write_rows": acquire_rows}


def _validated_acquire_write_delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    if before.get("semantic_sha256") != after.get("semantic_sha256"):
        raise RuntimeError("original index receipt audit changed SQLite schema or non-operational content")
    initial = before.get("acquire_write_rows"); final = after.get("acquire_write_rows")
    if not isinstance(initial, list) or not isinstance(final, list):
        raise RuntimeError("original index receipt audit acquire_write rows are malformed")
    # A shared Chroma system can satisfy the read through an existing client (0),
    # otherwise a direct client may append exactly one next-id lock row (1).
    if final != initial:
        if len(final) != len(initial) + 1 or final[:-1] != initial:
            raise RuntimeError("original index receipt audit changed acquire_write beyond one append")
        previous_ids = [row[0][1] for row in initial if isinstance(row, list) and len(row) == 2 and row[0][0] == "integer"]
        appended = final[-1]
        if (
            not isinstance(appended, list) or appended != [["integer", (max(previous_ids) + 1 if previous_ids else 1)], ["integer", 1]]
        ):
            raise RuntimeError("original index receipt audit acquire_write append is not the permitted next-id lock")
    return {
        "schema": "aerp5-chroma-operational-delta-v1",
        "excluded_table": "acquire_write",
        "permitted_transition": "unchanged_or_append_next_integer_id_lock_status_1",
        "validation": "passed",
    }


def original_index_build_receipt(*, palace_path: Path, expected_namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Non-mutating receipt audit using direct Chroma reads, with byte invariance proof."""
    # These direct SQLite/file reads bind the persisted configuration and all
    # backend bytes before any Chroma object is opened.  Do not replace them with
    # Palace.get_collection(): that product helper pins HNSW settings via modify().
    configuration_before = _sqlite_hnsw_configuration(palace_path)
    before = _audit_storage_digest(palace_path)
    sqlite_before = _sqlite_semantic_snapshot(palace_path)
    try:
        import chromadb
    except ImportError as exc:
        raise RuntimeError("Chroma direct read API is unavailable for index receipt audit") from exc
    client = chromadb.PersistentClient(path=str(palace_path))
    try:
        collection = client.get_collection(v1.ORIGINAL_COLLECTION)
        stored = collection.get(include=["embeddings"])
    finally:
        close = getattr(client, "close", None)
        if not callable(close): raise RuntimeError("Chroma direct read client has no close hook")
        close()
    after = _audit_storage_digest(palace_path)
    sqlite_after = _sqlite_semantic_snapshot(palace_path)
    configuration_after = _sqlite_hnsw_configuration(palace_path)
    if after["immutable_snapshot"] != before["immutable_snapshot"]:
        raise RuntimeError("original index receipt audit mutated persisted HNSW or non-SQLite backend bytes")
    if configuration_after != configuration_before:
        raise RuntimeError("original index receipt audit mutated resolved HNSW configuration")
    operational_delta = _validated_acquire_write_delta(sqlite_before, sqlite_after)
    ids, embeddings = stored.get("ids"), stored.get("embeddings")
    expected_ids = sorted(row["physical_id"] for row in expected_namespace["rows"])
    if not isinstance(ids, list) or sorted(ids) != expected_ids:
        raise RuntimeError("original Chroma stored physical-ID set differs from the projection namespace")
    embedding_sha256, embedding_count, embedding_dimension = _float32_embedding_digest(ids, embeddings)
    if embedding_count != EXPECTED_ORIGINAL_DIALOG_COUNT:
        raise RuntimeError("original Chroma embedding count differs from the fixed corpus count")
    graph = [row for row in before["files"] if row["name"] != "chroma.sqlite3"]
    return {
        "schema": "aerp5-original-index-build-receipt-v1",
        "physical_id_count": len(ids), "physical_id_sha256": canonical_sha256(sorted(ids)),
        "embedding_count": embedding_count, "embedding_dimension": embedding_dimension, "embedding_float32_sha256": embedding_sha256,
        "hnsw_configuration": configuration_before, "hnsw_graph_files": graph,
        "immutable_backend_sha256": before["immutable_sha256"],
        "sqlite_semantic_sha256": sqlite_before["semantic_sha256"],
        "sqlite_operational_delta": operational_delta,
    }


def validate_original_index_build_receipt(receipt: Any, *, expected_namespace: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or receipt.get("schema") != "aerp5-original-index-build-receipt-v1":
        raise ValueError("original index-build receipt is malformed")
    expected_ids = sorted(row["physical_id"] for row in expected_namespace["rows"])
    if receipt.get("physical_id_count") != EXPECTED_ORIGINAL_DIALOG_COUNT or receipt.get("physical_id_sha256") != canonical_sha256(expected_ids):
        raise ValueError("original index-build physical-ID receipt drifted")
    if receipt.get("embedding_count") != EXPECTED_ORIGINAL_DIALOG_COUNT or receipt.get("embedding_dimension") != 384:
        raise ValueError("original index-build embedding count/dimension drifted")
    _token(receipt.get("embedding_float32_sha256"), "original index-build embedding digest")
    _token(receipt.get("immutable_backend_sha256"), "original immutable backend digest")
    _token(receipt.get("sqlite_semantic_sha256"), "original SQLite semantic digest")
    if receipt.get("sqlite_operational_delta") != {
        "schema": "aerp5-chroma-operational-delta-v1",
        "excluded_table": "acquire_write",
        "permitted_transition": "unchanged_or_append_next_integer_id_lock_status_1",
        "validation": "passed",
    }:
        raise ValueError("original index-build operational SQLite delta receipt is malformed")
    expected_config = _sqlite_hnsw_configuration_expected()
    if receipt.get("hnsw_configuration") != expected_config:
        raise ValueError("original index-build HNSW configuration drifted")
    graph = receipt.get("hnsw_graph_files")
    if not isinstance(graph, list) or [row.get("name") for row in graph if isinstance(row, Mapping)] != ["data_level0.bin", "header.bin", "length.bin", "link_lists.bin"]:
        raise ValueError("original index-build HNSW graph receipt is malformed")
    for row in graph:
        if not isinstance(row.get("bytes"), int) or row["bytes"] < 0: raise ValueError("original index-build HNSW graph byte count is malformed")
        _token(row.get("sha256"), "original index-build HNSW graph digest")
    return dict(receipt)


def _sqlite_hnsw_configuration_expected() -> dict[str, Any]:
    return {"space": "cosine", "ef_construction": 100, "ef_search": 100, "max_neighbors": 16, "num_threads": 1, "batch_size": 2, "sync_threshold": 2, "resize_factor": 1.2}


def coordinator_original_index_build_receipt(*, palace_path: Path, expected_namespace: Mapping[str, Any]) -> dict[str, Any]:
    """Independent direct audit: it never loads the original product package."""
    return original_index_build_receipt(palace_path=palace_path, expected_namespace=expected_namespace)


def original_product_ingest_namespaced(*, palace: Any, palace_path: Path, conversation_id: str, dialogs: Sequence[Mapping[str, str]], physical_by_local: Mapping[str, str]) -> None:
    """Use the unchanged public Chroma upsert seam with globally unique physical IDs."""
    local_ids = [str(dialog["id"]) for dialog in dialogs]
    physical_ids = [physical_by_local[local_id] for local_id in local_ids]
    if len(physical_ids) != len(set(physical_ids)):
        raise RuntimeError("original conversation physical IDs are not unique")
    collection = palace.get_collection(str(palace_path), collection_name=v1.ORIGINAL_COLLECTION, create=True, backend="chroma")
    collection.upsert(
        documents=[str(dialog["text"]) for dialog in dialogs],
        ids=physical_ids,
        metadatas=[{"source_file": physical_id, "wing": "locomo", "room": conversation_id} for physical_id in physical_ids],
    )


def original_product_query_namespaced(*, searcher: Any, palace_path: Path, conversation_id: str, local_corpus_ids: Sequence[str], physical_by_local: Mapping[str, str], local_by_physical: Mapping[str, str], query: str, item_id: str) -> list[str]:
    """Search through the original public seam and remove physical namespace before scoring."""
    physical_corpus = [physical_by_local[local_id] for local_id in local_corpus_ids]
    physical_result = v1.original_product_query(
        searcher=searcher, palace_path=palace_path, conversation_id=conversation_id,
        corpus_ids=physical_corpus, query=query, item_id=item_id,
    )
    prefix = conversation_id + IDENTITY_NAMESPACE_SEPARATOR
    if any(not physical_id.startswith(prefix) for physical_id in physical_result):
        raise RuntimeError("original public search returned a physical ID outside the requested conversation")
    try:
        local_result = [local_by_physical[physical_id] for physical_id in physical_result]
    except KeyError as exc:
        raise RuntimeError("original public search returned an unknown physical ID") from exc
    if len(local_result) != TOP_K or len(set(local_result)) != TOP_K or any(local_id not in local_corpus_ids for local_id in local_result):
        raise RuntimeError("original public search cannot losslessly map strict Top-10 to the local corpus")
    return local_result


def policy_receipt_from_ranking_trace(
    *, arm: str, ranking: Mapping[str, Any], selected_ranking_keys: Sequence[str]
) -> tuple[dict[str, Any], str | None]:
    """Extract a frozen policy receipt from the trace fields the ranker really emits."""
    selected_digest = canonical_sha256(list(selected_ranking_keys))
    if arm == ARM_P5:
        fixed = ranking.get("aerp5_fixed_p5")
        expected = _expected_policy_receipt(arm)
        if (
            not isinstance(fixed, Mapping)
            or fixed.get("schema") != FixedP5Policy.schema
            or fixed.get("policy") != FixedP5Policy.policy
            or fixed.get("config") != FixedP5Policy._config(60)
            or fixed.get("config_sha256") != expected["config_sha256"]
            or fixed.get("effective_weights") != expected["effective_weights"]
            or ranking.get("weights") != expected["effective_weights"]
        ):
            raise RuntimeError("fixed P5 policy receipt missing or inconsistent")
        final_digest = _token(fixed.get("final_ranking_sha256"), "fixed P5 final ranking digest")
        return {
            **expected,
            "final_ranking_sha256": final_digest,
            "selected_top10_ranking_sha256": selected_digest,
        }, final_digest
    if arm == ARM_SIX_VIEW:
        expected = _expected_policy_receipt(arm)
        if ranking.get("weights") != expected["effective_weights"] or "aerp5_fixed_p5" in ranking:
            raise RuntimeError("fixed six-view policy receipt missing or inconsistent")
        return {**expected, "selected_top10_ranking_sha256": selected_digest}, None
    raise ValueError("policy receipts exist only for current-product arms")


def stable_product_trace_receipt(
    trace: Mapping[str, Any], *, event_to_dialog: Mapping[str, str]
) -> dict[str, Any]:
    """Project a complete product trace onto backend-independent ranking identities."""
    audit = aerp1.audit_product_trace(dict(trace))
    if audit.get("complete") is not True:
        raise RuntimeError("current product authorization trace is incomplete")
    ranking = trace.get("retrieval_ranking")
    if not isinstance(ranking, Mapping):
        raise RuntimeError("current product retrieval ranking trace is missing")
    authorized_ids = [str(value) for value in trace["authorized_candidate_ids"]]
    selected_ids = [str(value) for value in trace["selected_evidence_ids"]]
    try:
        authorized_dialogs = [event_to_dialog[value] for value in authorized_ids]
        selected_dialogs = [event_to_dialog[value] for value in selected_ids]
    except KeyError as exc:
        raise RuntimeError("authorization trace contains an unmapped backend event") from exc
    selected = ranking.get("selected")
    if not isinstance(selected, list):
        raise RuntimeError("current product selected ranking trace is missing")
    stable_selected = []
    for entry in selected:
        if not isinstance(entry, Mapping):
            raise RuntimeError("current product selected ranking trace is malformed")
        stable_selected.append({
            key: entry.get(key)
            for key in ("ranking_key_sha256", "final_rrf", "component_ranks", "contributions")
        })
    retrieval = {
        key: ranking.get(key)
        for key in ("schema", "encoder_identity", "weights", "rrf_k", "query_sha256", "input_sha256", "view_digests")
    }
    retrieval["selected"] = stable_selected
    for name in ("aerp5_fixed_p5",):
        if name in ranking:
            retrieval[name] = ranking[name]
    return {
        "schema": "aerp5-stable-product-trace-v1",
        "authorization_audit": audit,
        "authorized_dialog_universe_sha256": canonical_sha256(sorted(authorized_dialogs)),
        "selected_dialog_order_sha256": canonical_sha256(selected_dialogs),
        "retrieval_ranking": retrieval,
    }


def worker_run(config: Mapping[str, Any]) -> dict[str, Any]:
    """Real, label-free product worker for one arm/repeat with a fresh backend."""
    arm = config.get("arm"); projection_path = Path(str(config.get("projection"))); output = Path(str(config.get("output"))); backend = Path(str(config.get("temporary_backend")))
    if arm not in ALL_ARMS or output.exists() or backend.exists(): raise ValueError("invalid worker arm/output/fresh backend")
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection_items, projection_conversations = _validate_public_projection(projection)
    backend.mkdir(parents=True); model = Path(str(config["model_dir"])); original_root = Path(str(config["original_root"]));
    started = time.monotonic(); by_conversation: dict[str, list[dict[str, Any]]] = {}
    for row in projection_items: by_conversation.setdefault(str(row["conversation_id"]), []).append(dict(row))
    rankings: dict[str, dict[str, list[str]]] = {}; traces: dict[str, dict[str, Any]] = {}; policies: dict[str, dict[str, Any]] = {}; query_ns: list[int] = []; ingest_ns: list[int] = []
    with RssMonitor(os.getpid()) as monitor, v1.pinned_original_environment():
        palace, searcher, _protocol, _state = v1.load_original_product(original_root); encoder = v1.native_minilm_adapter(model)
        original_palace = backend / "original-palace"; db_path = backend / "current.sqlite3"
        original_configuration = v1.assert_original_product_configuration(palace, palace_path=original_palace, encoder=encoder)
        if arm == ARM_ORIGINAL:
            namespace = original_identity_namespace(projection_conversations)
            physical_by_conversation: dict[str, dict[str, str]] = {}
            local_by_physical: dict[str, str] = {}
            for namespace_row in namespace["rows"]:
                physical_by_conversation.setdefault(namespace_row["conversation_id"], {})[namespace_row["local_dialog_id"]] = namespace_row["physical_id"]
                local_by_physical[namespace_row["physical_id"]] = namespace_row["local_dialog_id"]
            # Lifecycle barrier: all ten public upsert batches finish before a cold reopen
            # and any public search.  This avoids interleaving write visibility with query.
            for conversation_id in sorted(by_conversation):
                dialogs = v1.raw_dialogs({"sessions": projection_conversations[conversation_id]["sessions"]})
                start = time.perf_counter_ns()
                original_product_ingest_namespaced(
                    palace=palace, palace_path=original_palace, conversation_id=conversation_id,
                    dialogs=dialogs, physical_by_local=physical_by_conversation[conversation_id],
                )
                ingest_ns.append(time.perf_counter_ns() - start)
            actual_collection_count = palace.get_collection(
                str(original_palace), collection_name=v1.ORIGINAL_COLLECTION, create=False, backend="chroma"
            ).count()
            if actual_collection_count != namespace["expected_unique_count"]:
                raise RuntimeError("original Chroma collection count does not equal all ten conversation dialogs")
            identity_namespace_receipt = {
                "schema": "aerp5-original-identity-namespace-v1", "scheme": IDENTITY_NAMESPACE_SCHEME,
                "expected_unique_count": namespace["expected_unique_count"], "actual_collection_count": actual_collection_count,
                "mapping_sha256": namespace["mapping_sha256"],
            }
            cold_reopen_cleanup = v1.reset_original_product_backends(original_palace)
            # Reset only the Chroma client/system; the original module handles stay loaded.
            # ``load_original_product`` is intentionally one-shot per process.
            original_configuration = v1.assert_original_product_configuration(palace, palace_path=original_palace, encoder=encoder)
            for conversation_id, rows in sorted(by_conversation.items()):
                dialogs = v1.raw_dialogs({"sessions": projection_conversations[conversation_id]["sessions"]})
                local_ids = [dialog["id"] for dialog in dialogs]
                for row in rows:
                    start = time.perf_counter_ns()
                    dialog_top10 = original_product_query_namespaced(
                        searcher=searcher, palace_path=original_palace, conversation_id=conversation_id,
                        local_corpus_ids=local_ids, physical_by_local=physical_by_conversation[conversation_id],
                        local_by_physical=local_by_physical, query=row["query"], item_id=row["item_id"],
                    )
                    query_ns.append(time.perf_counter_ns() - start)
                    rankings[row["item_token"]] = {"dialog_top10": dialog_top10, "evidence_top10": _evidence_tokens(dialog_top10)}
            index_build_receipt = original_index_build_receipt(palace_path=original_palace, expected_namespace=namespace)
            lifecycle_receipt = {"all_ingest_before_query": True, "cold_reopen_before_query": True, "query_latency_boundary": "public_search_return_through_namespace_validation"}
        else:
            identity_namespace_receipt = "not_applicable"; cold_reopen_cleanup = "not_applicable"; index_build_receipt = "not_applicable"
            ranker = SixViewRanker(encoder, diagnostic_ledger=True, routing_policy=FixedP5Policy() if arm == ARM_P5 else FixedSixViewPolicy())
            event_maps: dict[str, dict[str, str]] = {}
            with RpgMemoryKernel(db_path=str(db_path), retrieval_ranker=ranker) as kernel:
                for conversation_id, rows in sorted(by_conversation.items()):
                    payload = {"sessions": projection_conversations[conversation_id]["sessions"]}; dialogs = v1.raw_dialogs(payload)
                    start = time.perf_counter_ns(); event_map, _ = aerp2.seed_sanitized_conversation(kernel, payload, conversation_id=conversation_id); event_maps[conversation_id] = event_map
                    ingest_ns.append(time.perf_counter_ns() - start)
            # Current arms share the original arm's all-ingest then cold-reopen lifecycle.
            with RpgMemoryKernel(db_path=str(db_path), retrieval_ranker=ranker) as kernel:
                for conversation_id, rows in sorted(by_conversation.items()):
                    event_map = event_maps[conversation_id]
                    for row in rows:
                        start = time.perf_counter_ns()
                        dialog_top10, _trace = v1.current_product_rank(kernel, conversation_id=conversation_id, query=row["query"], event_to_dialog=event_map, item_id=row["item_id"])
                        query_elapsed = time.perf_counter_ns() - start
                        if not v1.TRACE_REQUIRED <= set(_trace): raise RuntimeError("current product compact trace incomplete")
                        ranking = _trace.get("retrieval_ranking", {})
                        selected = ranking.get("selected")
                        if not isinstance(selected, list) or len(selected) != TOP_K: raise RuntimeError("current ranking selected trace is not strict Top-10")
                        selected_evidence = [a4paired.evidence_token_from_ranking_key_sha256(str(entry.get("ranking_key_sha256"))) for entry in selected if isinstance(entry, Mapping)]
                        if len(selected_evidence) != TOP_K: raise RuntimeError("current ranking selected trace is malformed")
                        selected_ranking_keys = [str(entry["ranking_key_sha256"]) for entry in selected]
                        stable_trace = stable_product_trace_receipt(_trace, event_to_dialog=event_map)
                        trace_item = {
                            "trace_sha256": canonical_sha256(stable_trace),
                            "stable_trace": stable_trace,
                            "selected_count": len(_trace["selected_evidence_ids"]),
                            "selected_evidence_top10": selected_evidence,
                            "selected_ranking_key_sha256": selected_ranking_keys,
                            "selected_ranking_keys_sha256": canonical_sha256(selected_ranking_keys),
                            "complete": True,
                        }
                        policy_receipt, final_digest = policy_receipt_from_ranking_trace(
                            arm=arm,
                            ranking=ranking,
                            selected_ranking_keys=selected_ranking_keys,
                        )
                        if arm == ARM_P5:
                            if final_digest is None:
                                raise RuntimeError("fixed P5 final ranking receipt disappeared")
                            trace_item["policy_final_ranking_sha256"] = final_digest
                        policies[row["item_token"]] = policy_receipt
                        traces[row["item_token"]] = trace_item
                        evidence_top10 = _evidence_tokens(dialog_top10)
                        if evidence_top10 != selected_evidence: raise RuntimeError("current output evidence tokens do not bind selected ranking trace")
                        query_ns.append(query_elapsed); rankings[row["item_token"]] = {"dialog_top10": dialog_top10, "evidence_top10": evidence_top10}
            lifecycle_receipt = {"all_ingest_before_query": True, "cold_reopen_before_query": True, "query_latency_boundary": "current_product_rank_return_only"}
        provider = actual_onnx_session_providers(encoder._function)
        cleanup = v1.reset_original_product_backends(original_palace)
    if len(rankings) != 1982: raise RuntimeError("worker did not rank every projected item")
    trace_receipt = {"supported": arm != ARM_ORIGINAL, "complete_count": len(traces) if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "expected_count": 1982 if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "trace_sha256": canonical_sha256(traces) if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "items": traces if arm != ARM_ORIGINAL else {}}
    if arm != ARM_ORIGINAL and len(traces) != 1982: raise RuntimeError("current trace completeness is not 100%")
    with RssMonitor(os.getpid()) as publish_monitor:
        if arm != ARM_ORIGINAL and len(policies) != 1982: raise RuntimeError("policy receipt completeness is not 100%")
        report = {"schema": SCHEMA + "-worker", "arm": arm, "items": rankings, "input_projection_sha256": sha256_file(projection_path), "input_projection_content_sha256": canonical_sha256(projection), "dialog_ranking_sha256": canonical_sha256({key: row["dialog_top10"] for key, row in rankings.items()}), "evidence_ranking_sha256": canonical_sha256({key: row["evidence_top10"] for key, row in rankings.items()}), "projection_sha256": canonical_sha256(rankings), "latency": latency_receipt(query_ns), "conversation_ingest": distribution_receipt(ingest_ns, expected_count=10, label="conversation ingest"), "lifecycle_receipt": lifecycle_receipt, "trace_receipt": trace_receipt, "policy_receipts": policies if arm != ARM_ORIGINAL else {}, "policy_receipts_sha256": canonical_sha256(policies) if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "original_product_configuration": original_configuration, "identity_namespace_receipt": identity_namespace_receipt, "original_index_build_receipt": index_build_receipt, "cold_reopen_cleanup": cold_reopen_cleanup, "worker_self_rss_diagnostic": {"authoritative": False, "reason": "coordinator supervisor owns spawn-through-exit peak", "load_through_ranking_peak_bytes": monitor.peak_bytes, "receipt_serialize_publish_peak_bytes": publish_monitor.peak_bytes}, "onnx_providers": provider, "model_file_tree_sha256": v1.file_tree_receipt(model)["sha256"], "cleanup": cleanup, "elapsed_seconds": time.monotonic() - started}
        _publish_nonreplace(output, _canonical(report))
    return report


class _NullContext:
    def __enter__(self): return None
    def __exit__(self, *_: Any) -> None: return None


def build_minilm_p5_checkpoint(
    repeats: Sequence[Mapping[str, Any]], *, expected_model_sha256: str, expected_item_tokens: Iterable[str]
) -> dict[str, Any]:
    """Freeze the two exact label-free MiniLM P5 repeats before custody may score."""
    validate_repeat_identity(ARM_P5, repeats)
    if len(repeats) != CURRENT_REPEATS:
        raise RuntimeError("MiniLM P5 checkpoint requires exactly two repeats")
    expected_tokens = tuple(sorted(expected_item_tokens))
    first = repeats[0]
    if set(first.get("items", {})) != set(expected_tokens):
        raise RuntimeError("MiniLM P5 checkpoint membership is not the projected 1,982 items")
    query_input = _current_p5_query_input_digests(first, expected_model_sha256=expected_model_sha256)
    if set(query_input) != set(expected_tokens):
        raise RuntimeError("MiniLM P5 checkpoint query/input membership drifted")
    fields = {
        "input_projection_sha256": first.get("input_projection_sha256"),
        "input_projection_content_sha256": first.get("input_projection_content_sha256"),
        "ranking_projection_sha256": first.get("projection_sha256"),
        "dialog_ranking_sha256": first.get("dialog_ranking_sha256"),
        "evidence_ranking_sha256": first.get("evidence_ranking_sha256"),
        "trace_sha256": first.get("trace_receipt", {}).get("trace_sha256"),
        "policy_receipts_sha256": first.get("policy_receipts_sha256"),
    }
    for name, value in fields.items():
        _token(value, f"MiniLM P5 checkpoint {name}")
    if first.get("model_file_tree_sha256") != expected_model_sha256:
        raise RuntimeError("MiniLM P5 checkpoint model drifted")
    expected_encoder = expected_minilm_encoder_identity(expected_model_sha256)
    if any(_current_p5_query_input_digests(repeat, expected_model_sha256=expected_model_sha256) != query_input for repeat in repeats[1:]):
        raise RuntimeError("MiniLM P5 checkpoint query/input repeat drift")
    return {
        "schema": MINILM_P5_CHECKPOINT_SCHEMA,
        "arm": ARM_P5,
        "repeat_count": CURRENT_REPEATS,
        "encoder_identity": expected_encoder,
        "model_file_tree_sha256": expected_model_sha256,
        "membership_sha256": canonical_sha256(list(expected_tokens)),
        "query_input_sha256": canonical_sha256(query_input),
        "fixed_p5_semantics_sha256": canonical_sha256(_fixed_p5_semantics()),
        **fields,
    }


def validate_minilm_p5_checkpoint(
    checkpoint: Any, *, expected_checkpoint_sha256: Any, repeats: Sequence[Mapping[str, Any]],
    expected_model_sha256: str, expected_item_tokens: Iterable[str],
) -> dict[str, Any]:
    """Fail closed if custody's declared MiniLM checkpoint differs from frozen workers."""
    if not isinstance(checkpoint, Mapping):
        raise ValueError("custodian MiniLM P5 checkpoint is missing")
    rebuilt = build_minilm_p5_checkpoint(
        repeats, expected_model_sha256=expected_model_sha256, expected_item_tokens=expected_item_tokens
    )
    if dict(checkpoint) != rebuilt:
        raise RuntimeError("custodian MiniLM P5 checkpoint differs from frozen repeats")
    if expected_checkpoint_sha256 != canonical_sha256(rebuilt):
        raise RuntimeError("custodian MiniLM P5 checkpoint digest drifted")
    return rebuilt


def validate_aerp4_minilm_checkpoint_binding(
    aerp4_lineage: Mapping[str, Any], minilm_p5_checkpoint: Mapping[str, Any]
) -> None:
    """Bind the historical lineage and live MiniLM checkpoint before custody.

    The two receipts are independently rebuilt: AERP4 proves historical BGE
    provenance while the checkpoint proves the two native-MiniLM executions.
    Neither may substitute for the other.  Their shared item/query-input
    identity, and the fixed-P5 semantic digest carried by the checkpoint, are
    therefore checked explicitly at the label-custody boundary.
    """
    for field in ("membership_sha256", "query_input_sha256"):
        lineage_value = aerp4_lineage.get(field)
        checkpoint_value = minilm_p5_checkpoint.get(field)
        _token(lineage_value, f"AERP4 lineage {field}")
        _token(checkpoint_value, f"MiniLM P5 checkpoint {field}")
        if lineage_value != checkpoint_value:
            raise RuntimeError(f"AERP4 lineage and MiniLM P5 checkpoint {field} differ")
    if minilm_p5_checkpoint.get("fixed_p5_semantics_sha256") != canonical_sha256(_fixed_p5_semantics()):
        raise RuntimeError("MiniLM P5 checkpoint fixed-P5 semantics drifted")


def validate_aerp4_lineage_receipt(
    receipt: Any, *, manifest: Mapping[str, Any], expected_item_tokens: Iterable[str]
) -> dict[str, Any]:
    """Require the coordinator's BGE lineage receipt before the custodian opens labels."""
    if not isinstance(receipt, Mapping):
        raise ValueError("custodian AERP4 lineage receipt is missing")
    expected_membership = canonical_sha256(sorted(expected_item_tokens))
    if (
        receipt.get("schema") != "aerp5-aerp4-bge-lineage-v1"
        or receipt.get("historical_encoder") != AERP4_HISTORICAL_BGE
        or receipt.get("fixed_p5_semantics") != _fixed_p5_semantics()
        or receipt.get("membership_sha256") != expected_membership
    ):
        raise RuntimeError("custodian AERP4 BGE lineage receipt is malformed")
    _token(receipt.get("query_input_sha256"), "AERP4 lineage query/input digest")
    return validate_aerp4_lineage_anchor(receipt, manifest=manifest)


def _expected_policy_receipt(arm: str) -> dict[str, Any]:
    if arm == ARM_P5:
        config = FixedP5Policy._config(60)
        return {
            "route": "p5",
            "config_sha256": canonical_sha256(config),
            "effective_weights": config["p5_weights"],
        }
    if arm == ARM_SIX_VIEW:
        effective_weights = dict(SIX_VIEW_WEIGHTS)
        return {
            "route": "six_view",
            "config_sha256": canonical_sha256(
                {"route": "six_view", "effective_weights": effective_weights}
            ),
            "effective_weights": effective_weights,
        }
    raise ValueError("the original arm has no current-product policy receipt")


def parse_worker_freeze(
    value: Mapping[str, Any],
    *,
    arm: str,
    projection_sha256: str,
    projection_content_sha256: str,
    item_tokens: Iterable[str],
    model_sha256: str,
    identity_namespace: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Strictly validate one completed worker receipt before custody sees it."""
    if (
        value.get("schema") != SCHEMA + "-worker"
        or value.get("arm") != arm
        or value.get("input_projection_sha256") != projection_sha256
        or value.get("input_projection_content_sha256") != projection_content_sha256
    ):
        raise ValueError("worker freeze identity/projection mismatch")
    if value.get("model_file_tree_sha256") != model_sha256 or value.get("onnx_providers") != ["CPUExecutionProvider"]:
        raise ValueError("worker freeze model/provider mismatch")
    items = value.get("items")
    expected_tokens = set(item_tokens)
    if not isinstance(items, Mapping) or len(items) != 1982 or set(items) != expected_tokens:
        raise ValueError("worker freeze membership is not 1,982")
    dialogs = {}; evidence = {}
    for token, row in items.items():
        _token(token, "worker item token")
        if not isinstance(row, Mapping): raise ValueError("worker item receipt malformed")
        for name, target in (("dialog_top10", dialogs), ("evidence_top10", evidence)):
            ranking = row.get(name)
            if not isinstance(ranking, list) or len(ranking) != TOP_K or len(set(ranking)) != TOP_K: raise ValueError("worker ranking is not strict Top-10")
            target[token] = ranking
        if row["evidence_top10"] != _evidence_tokens(row["dialog_top10"]):
            raise ValueError("worker dialog/evidence Top-10 linkage mismatch")
    if value.get("dialog_ranking_sha256") != canonical_sha256(dialogs) or value.get("evidence_ranking_sha256") != canonical_sha256(evidence) or value.get("projection_sha256") != canonical_sha256(items):
        raise ValueError("worker ranking digest mismatch")
    latency = value.get("latency")
    if not isinstance(latency, Mapping) or not isinstance(latency.get("samples_ns"), list) or dict(latency) != latency_receipt(latency["samples_ns"]):
        raise ValueError("worker latency receipt is not replayable")
    ingest = value.get("conversation_ingest")
    if not isinstance(ingest, Mapping) or not isinstance(ingest.get("samples_ns"), list) or dict(ingest) != distribution_receipt(ingest["samples_ns"], expected_count=10, label="conversation ingest"):
        raise ValueError("worker conversation-ingest receipt is not replayable")
    expected_boundary = "public_search_return_through_namespace_validation" if arm == ARM_ORIGINAL else "current_product_rank_return_only"
    if value.get("lifecycle_receipt") != {"all_ingest_before_query": True, "cold_reopen_before_query": True, "query_latency_boundary": expected_boundary}:
        raise ValueError("worker lifecycle/latency boundary receipt is malformed")
    configuration = value.get("original_product_configuration")
    if not isinstance(configuration, Mapping) or (
        configuration.get("backend") != "chroma"
        or configuration.get("collection") != v1.ORIGINAL_COLLECTION
        or configuration.get("embedding_model") != "minilm"
        or configuration.get("embedding_device") != "cpu"
        or configuration.get("providers") != ["CPUExecutionProvider"]
        or configuration.get("model_file_tree_sha256") != model_sha256
        or configuration.get("same_cached_embedding_object_for_both_arms") is not True
    ):
        raise ValueError("original product resolved configuration mismatch")
    trace = value.get("trace_receipt", {})
    if arm == ARM_ORIGINAL:
        unsupported = "unsupported_through_original_public_interface"
        if trace != {
            "supported": False,
            "complete_count": unsupported,
            "expected_count": unsupported,
            "trace_sha256": unsupported,
            "items": {},
        }:
            raise ValueError("original worker trace support receipt is malformed")
        if value.get("policy_receipts") != {} or value.get("policy_receipts_sha256") != unsupported:
            raise ValueError("original worker must not claim a current-product policy receipt")
        if identity_namespace is None:
            raise ValueError("original worker parser requires projection-derived identity namespace")
        validate_original_identity_namespace_receipt(
            value.get("identity_namespace_receipt"), expected=identity_namespace
        )
        validate_original_index_build_receipt(
            value.get("original_index_build_receipt"), expected_namespace=identity_namespace
        )
        if not isinstance(value.get("cold_reopen_cleanup"), Mapping) or value["cold_reopen_cleanup"].get("verified_system_released") is not True:
            raise ValueError("original worker cold-reopen cleanup receipt is malformed")
    else:
        if value.get("identity_namespace_receipt") != "not_applicable" or value.get("original_index_build_receipt") != "not_applicable" or value.get("cold_reopen_cleanup") != "not_applicable":
            raise ValueError("current worker must not claim an original identity namespace receipt")
        trace_items = trace.get("items")
        if (
            trace.get("supported") is not True
            or trace.get("complete_count") != 1982
            or trace.get("expected_count") != 1982
            or not isinstance(trace_items, Mapping)
            or set(trace_items) != expected_tokens
            or trace.get("trace_sha256") != canonical_sha256(trace_items)
        ):
            raise ValueError("current worker trace receipt incomplete")
        for token, receipt in trace_items.items():
            if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
                raise ValueError("current worker trace item is malformed")
            expected_trace_fields = {
                "trace_sha256", "stable_trace", "selected_count", "selected_evidence_top10",
                "selected_ranking_key_sha256", "selected_ranking_keys_sha256", "complete",
            } | ({"policy_final_ranking_sha256"} if arm == ARM_P5 else set())
            if set(receipt) != expected_trace_fields:
                raise ValueError("current worker trace item fields are not canonical")
            _token(receipt.get("trace_sha256"), "current trace digest")
            stable_trace = receipt.get("stable_trace")
            if (
                not isinstance(stable_trace, Mapping)
                or stable_trace.get("schema") != "aerp5-stable-product-trace-v1"
                or stable_trace.get("authorization_audit", {}).get("complete") is not True
                or receipt["trace_sha256"] != canonical_sha256(stable_trace)
                or stable_trace.get("selected_dialog_order_sha256") != canonical_sha256(dialogs[token])
            ):
                raise ValueError("current worker stable trace receipt is malformed")
            retrieval = stable_trace.get("retrieval_ranking", {})
            if not isinstance(retrieval, Mapping) or retrieval.get("encoder_identity") != expected_minilm_encoder_identity(model_sha256):
                raise ValueError("current worker encoder identity is not the pinned native MiniLM identity")
            _token(stable_trace.get("authorized_dialog_universe_sha256"), "authorized dialog universe digest")
            selected_evidence = receipt.get("selected_evidence_top10")
            selected_keys = receipt.get("selected_ranking_key_sha256")
            if (
                selected_evidence != evidence[token]
                or receipt.get("selected_count") != TOP_K
                or not isinstance(selected_keys, list)
                or len(selected_keys) != TOP_K
                or len(set(selected_keys)) != TOP_K
                or any(_token(key, "selected ranking key digest") != key for key in selected_keys)
                or [a4paired.evidence_token_from_ranking_key_sha256(key) for key in selected_keys] != selected_evidence
                or receipt.get("selected_ranking_keys_sha256") != canonical_sha256(selected_keys)
                or [entry.get("ranking_key_sha256") for entry in stable_trace.get("retrieval_ranking", {}).get("selected", [])] != selected_keys
            ):
                raise ValueError("current worker trace selection does not bind the frozen Top-10")
    if arm != ARM_ORIGINAL:
        policies = value.get("policy_receipts")
        if not isinstance(policies, Mapping) or set(policies) != set(items) or value.get("policy_receipts_sha256") != canonical_sha256(policies): raise ValueError("worker policy receipt digest/membership mismatch")
        expected = _expected_policy_receipt(arm)
        for token, receipt in policies.items():
            if not isinstance(receipt, Mapping):
                raise ValueError("worker fixed policy receipt differs from frozen configuration")
            static = {key: receipt.get(key) for key in expected}
            if static != expected or receipt.get("selected_top10_ranking_sha256") != trace_items[token].get("selected_ranking_keys_sha256"):
                raise ValueError("worker fixed policy receipt differs from frozen configuration")
            if arm == ARM_P5:
                if set(receipt) != {*expected, "selected_top10_ranking_sha256", "final_ranking_sha256"}:
                    raise ValueError("fixed P5 policy receipt has unexpected fields")
                if _token(receipt.get("final_ranking_sha256"), "fixed P5 final ranking digest") != trace_items[token].get("policy_final_ranking_sha256"):
                    raise ValueError("fixed P5 final ranking receipt is not trace-bound")
            elif set(receipt) != {*expected, "selected_top10_ranking_sha256"}:
                raise ValueError("fixed six-view policy receipt has unexpected fields")
    return dict(value)


def validate_repeat_identity(arm: str, repeats: Sequence[Mapping[str, Any]]) -> None:
    expected_repeats = REPEATS_BY_ARM[arm]
    if len(repeats) != expected_repeats:
        raise RuntimeError(f"{arm} requires exactly {expected_repeats} fresh repeats")
    scalar_fields = (
        "input_projection_sha256",
        "input_projection_content_sha256",
    )
    if any(repeats[0].get(field) != repeat.get(field) for repeat in repeats[1:] for field in scalar_fields):
        raise RuntimeError(f"{arm} repeat identity drift")
    if any(canonical_sha256(repeats[0].get("original_product_configuration")) != canonical_sha256(repeat.get("original_product_configuration")) for repeat in repeats[1:]):
        raise RuntimeError(f"{arm} repeat original-product configuration drift")
    if arm == ARM_ORIGINAL:
        if any(repeats[0].get("identity_namespace_receipt") != repeat.get("identity_namespace_receipt") for repeat in repeats[1:]):
            raise RuntimeError("original repeat identity namespace drift")
        fixed_index_fields = ("physical_id_count", "physical_id_sha256", "embedding_count", "embedding_dimension", "embedding_float32_sha256", "hnsw_configuration")
        first_index = repeats[0].get("original_index_build_receipt", {})
        if any(any(repeat.get("original_index_build_receipt", {}).get(field) != first_index.get(field) for field in fixed_index_fields) for repeat in repeats[1:]):
            raise RuntimeError("original repeat index-build corpus/configuration drift")
        # Fresh original Chroma batch index builds are the pre-registered stochastic
        # replicate unit. Their rankings may differ; their inputs/configuration may not.
        return
    scalar_fields += ("projection_sha256", "dialog_ranking_sha256", "evidence_ranking_sha256", "policy_receipts_sha256")
    if any(repeats[0].get(field) != repeat.get(field) for repeat in repeats[1:] for field in scalar_fields):
        raise RuntimeError(f"{arm} repeat identity drift")
    if any(repeats[0].get("trace_receipt", {}).get("trace_sha256") != repeat.get("trace_receipt", {}).get("trace_sha256") for repeat in repeats[1:]):
        raise RuntimeError(f"{arm} repeat trace drift")


def supervise_worker(command: Sequence[str], *, sidecar: Path, freeze_path: Path) -> dict[str, Any]:
    """Coordinator-owned process-tree RSS authority for a worker lifetime."""
    if sidecar.exists(): raise FileExistsError("worker supervisor sidecar already exists")
    process = subprocess.Popen(list(command), cwd=ROOT)
    with RssMonitor(process.pid) as monitor:
        code = process.wait()
    if code: raise subprocess.CalledProcessError(code, list(command))
    if not freeze_path.is_file(): raise RuntimeError("worker succeeded without a frozen receipt")
    if monitor.peak_bytes <= 0: raise RuntimeError("worker supervisor obtained no RSS sample")
    receipt = {"pid": process.pid, "exit_code": code, "command_sha256": canonical_sha256(list(command)), "freeze_path": str(freeze_path), "freeze_sha256": sha256_file(freeze_path), "observed_process_tree_peak_rss_bytes": monitor.peak_bytes, "rss_cap_bytes": RSS_CAP_BYTES}
    _publish_nonreplace(sidecar, _canonical(receipt))
    return receipt


def latency_receipt(samples_ns: Sequence[int]) -> dict[str, Any]:
    raw_values = [int(value) for value in samples_ns]
    values = sorted(raw_values)
    if len(values) != 1982 or any(value <= 0 for value in values):
        raise ValueError("AERP5 requires exactly 1,982 positive per-query latency samples")
    def pct(fraction: float) -> int:
        return values[math.ceil((len(values) - 1) * fraction)]
    return {"count": len(values), "cold_first_ns": raw_values[0], "p50_ns": pct(.50), "p95_ns": pct(.95), "p99_ns": pct(.99), "mean_ns": statistics.fmean(values), "max_ns": values[-1], "samples_ns": raw_values, "samples_sha256": canonical_sha256(raw_values), "cold_first_query_included": True}


def distribution_receipt(samples_ns: Sequence[int], *, expected_count: int, label: str) -> dict[str, Any]:
    raw_values = [int(value) for value in samples_ns]
    values = sorted(raw_values)
    if len(values) != expected_count or any(value <= 0 for value in values): raise ValueError(f"{label} sample count/value mismatch")
    def pct(value: float) -> int: return values[math.ceil((len(values) - 1) * value)]
    return {"count": len(values), "p50_ns": pct(.50), "p95_ns": pct(.95), "p99_ns": pct(.99), "mean_ns": statistics.fmean(values), "max_ns": values[-1], "samples_ns": raw_values, "samples_sha256": canonical_sha256(raw_values)}


class RssMonitor:
    """Fail-closed process-tree RSS sampler, spanning load through serialization."""
    def __init__(self, root_pid: int, cap_bytes: int = RSS_CAP_BYTES) -> None:
        self.root_pid, self.cap_bytes, self.peak_bytes = root_pid, cap_bytes, 0
        self.error: str | None = None; self._stop = threading.Event(); self._thread: threading.Thread | None = None

    def _sample_once(self) -> None:
        import psutil
        try: root = psutil.Process(self.root_pid); processes = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess: return
        total = 0
        for process in processes:
            try:
                if process.is_running():
                    total += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        self.peak_bytes = max(self.peak_bytes, total)
        if total > self.cap_bytes: self.error = f"RSS cap exceeded: {total} > {self.cap_bytes}"

    def _sample(self) -> None:
        try:
            while not self._stop.wait(.02):
                self._sample_once()
                if self.error: return
        except BaseException as exc:  # measurement unavailable is a formal failure
            self.error = f"RSS monitor failed: {type(exc).__name__}: {exc}"

    def __enter__(self) -> "RssMonitor":
        try: self._sample_once()
        except BaseException as exc: self.error = f"RSS monitor failed: {type(exc).__name__}: {exc}"
        if self.error: raise RuntimeError(self.error)
        self._thread = threading.Thread(target=self._sample, daemon=True); self._thread.start(); return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None: self._thread.join()
        if self.error is None:
            try: self._sample_once()
            except BaseException as exc: self.error = f"RSS monitor failed: {type(exc).__name__}: {exc}"
        if self.error: raise RuntimeError(self.error)


def coordinator_run(*, projection_path: Path, dataset: Path, original_root: Path, model_dir: Path, train: PinnedJson, dev: PinnedJson, work: Path, output: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Launch label-free workers from the immutable projection, then hand off scoring."""
    canonical_manifest = load_manifest()
    if dict(manifest) != canonical_manifest:
        raise ValueError("coordinator requires the canonical AERP5 v2 manifest")
    v1.require_external_output(work, ROOT, original_root); v1.require_external_output(output, ROOT, original_root)
    before_current, before_original = v1.git_state(ROOT), v1.require_clean_pinned_original(original_root)
    model_before = v1.file_tree_receipt(model_dir)
    membership = validate_aerp4_membership(train_freeze=train, dev_freeze=dev, manifest=manifest)
    receipt = environment_receipt(model_dir=model_dir, original_root=original_root); validate_environment_receipt(receipt, manifest)
    if work.exists() or output.exists(): raise FileExistsError("AERP5 coordinator refuses to clobber work/output")
    projection, projection_file_sha256, projection_content_sha256 = load_pinned_projection(
        projection_path=projection_path, manifest=manifest
    )
    work.mkdir(parents=True)
    projection_tokens, _projection_conversations = _validate_public_projection(projection)
    projection_tokens = tuple(row["item_token"] for row in projection_tokens)
    if tuple(sorted(projection_tokens)) != membership["item_tokens"]:
        raise RuntimeError("canonical label-free projection membership differs from AERP4 lineage")
    identity_namespace = original_identity_namespace(_projection_conversations)
    coordinator_index_receipts: list[dict[str, Any]] = []
    outputs: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ALL_ARMS}
    try:
        for arm in ALL_ARMS:
            for repeat in range(REPEATS_BY_ARM[arm]):
                path = work / f"{arm}-{repeat}.json"; backend = work / f"{arm}-{repeat}-backend"
                config = {"arm": arm, "projection": str(projection_path), "output": str(path), "temporary_backend": str(backend), "original_root": str(original_root), "model_dir": str(model_dir)}
                config_path = work / f"{arm}-{repeat}-worker.json"; config_path.write_bytes(_canonical(config))
                sidecar = work / f"{arm}-{repeat}-supervisor.json"
                supervisor = supervise_worker([sys.executable, "-m", "benchmarks.aerp5_product_paired_locomo_v2", "--worker-config", str(config_path)], sidecar=sidecar, freeze_path=path)
                raw_freeze = _json(path)
                if arm == ARM_ORIGINAL:
                    if _sqlite_embedding_count(backend / "original-palace") != identity_namespace["expected_unique_count"]:
                        raise RuntimeError("exited original backend does not retain all namespaced dialogs")
                    coordinator_receipt = coordinator_original_index_build_receipt(
                        palace_path=backend / "original-palace", expected_namespace=identity_namespace,
                    )
                    if raw_freeze.get("original_index_build_receipt") != coordinator_receipt:
                        raise RuntimeError("worker original index-build receipt differs from coordinator remeasurement")
                    coordinator_index_receipts.append(coordinator_receipt)
                parsed = parse_worker_freeze(
                    raw_freeze,
                    arm=arm,
                    projection_sha256=projection_file_sha256,
                    projection_content_sha256=projection_content_sha256,
                    item_tokens=projection_tokens,
                    model_sha256=manifest["run"]["model"]["file_tree_sha256"],
                    identity_namespace=identity_namespace if arm == ARM_ORIGINAL else None,
                )
                parsed["supervisor"] = supervisor; outputs[arm].append(parsed)
        for arm, repeats in outputs.items():
            validate_repeat_identity(arm, repeats)
        frozen_rows = [*_freeze_items(train.load(), "train"), *_freeze_items(dev.load(), "dev")]
        aerp4_lineage = validate_aerp4_lineage(
            frozen_rows=frozen_rows, current_p5=outputs[ARM_P5][0], manifest=manifest
        )
        validate_aerp4_lineage_anchor(aerp4_lineage, manifest=manifest)
        minilm_p5_checkpoint = build_minilm_p5_checkpoint(
            outputs[ARM_P5], expected_model_sha256=manifest["run"]["model"]["file_tree_sha256"],
            expected_item_tokens=projection_tokens,
        )
        validate_aerp4_minilm_checkpoint_binding(aerp4_lineage, minilm_p5_checkpoint)
        minilm_p5_checkpoint_sha256 = canonical_sha256(minilm_p5_checkpoint)
        freeze_paths = {arm: [str(work / f"{arm}-{repeat}.json") for repeat in range(REPEATS_BY_ARM[arm])] for arm in ALL_ARMS}; freeze_hashes = {arm: [sha256_file(Path(path)) for path in paths] for arm, paths in freeze_paths.items()}
        scorer_receipt = validate_scorer_contract(manifest=manifest, original_root=original_root)
        score_path = work / "custodian-score.json"; score_config = {"projection": str(projection_path), "expected_projection_sha256": projection_file_sha256, "expected_projection_content_sha256": projection_content_sha256, "expected_model_sha256": manifest["run"]["model"]["file_tree_sha256"], "freezes": freeze_paths, "work": str(work.resolve()), "expected_freeze_sha256": freeze_hashes, "expected_original_index_receipts": coordinator_index_receipts, "expected_original_index_receipts_sha256": canonical_sha256(coordinator_index_receipts), "aerp4_lineage": aerp4_lineage, "minilm_p5_checkpoint": minilm_p5_checkpoint, "expected_minilm_p5_checkpoint_sha256": minilm_p5_checkpoint_sha256, "dataset": str(dataset), "expected_dataset_sha256": manifest["dataset"]["sha256"], "scorer_implementation_receipt": scorer_receipt, "original_root": str(original_root), "scientific_gates": manifest["scientific_gates"], "expected_scientific_gates_sha256": canonical_sha256(manifest["scientific_gates"]), "manifest_sha256": canonical_sha256(manifest), "output": str(score_path)}
        score_config_path = work / "custodian-config.json"; score_config_path.write_bytes(_canonical(score_config))
        subprocess.run([sys.executable, "-m", "benchmarks.aerp5_product_paired_locomo_v2", "--score-config", str(score_config_path)], cwd=ROOT, check=True)
        after_current, after_original = v1.git_state(ROOT), v1.git_state(original_root)
        if after_current != before_current or after_original != before_original: raise RuntimeError("measured repository state drifted during formal run")
        if v1.file_tree_receipt(model_dir) != model_before: raise RuntimeError("model file tree drifted during formal run")
        score = _json(score_path)
        expected_score_receipts = {"dataset_sha256": manifest["dataset"]["sha256"], "projection_sha256": projection_file_sha256, "projection_content_sha256": projection_content_sha256, "freeze_sha256": freeze_hashes, "original_index_receipts_sha256": canonical_sha256(coordinator_index_receipts), "aerp4_lineage_sha256": canonical_sha256(aerp4_lineage), "minilm_p5_checkpoint_sha256": minilm_p5_checkpoint_sha256, "scientific_gates_sha256": canonical_sha256(manifest["scientific_gates"]), "manifest_sha256": canonical_sha256(manifest)}
        if score.get("input_receipts") != expected_score_receipts: raise RuntimeError("custodian score receipts do not match pre-score inputs")
        if score.get("scorer_implementation_receipt") != scorer_receipt: raise RuntimeError("custodian scorer implementation receipt drifted")
        report = {"engineering_gates_passed": True, "environment": receipt, "repository_state_before": {"current": before_current, "original": before_original}, "repository_state_after": {"current": after_current, "original": after_original}, "projection_sha256": projection_file_sha256, "aerp4_lineage": aerp4_lineage, "minilm_p5_checkpoint": minilm_p5_checkpoint, "freezes": outputs, "coordinator_original_index_receipts": coordinator_index_receipts, "score": score, "freeze_file_sha256": freeze_hashes}
        publish_report(output=output, manifest=manifest, report=report); return report
    except BaseException:
        # No partial formal report is published; work is retained for diagnosis.
        raise


def environment_receipt(*, model_dir: Path, original_root: Path) -> dict[str, Any]:
    package_names = ("chromadb", "onnxruntime", "psutil", "mempalace-rpg")
    packages = {}
    for name in package_names:
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = "not-installed"
    try:
        import psutil; hardware = {"machine": platform.machine(), "processor": platform.processor(), "logical_cores": os.cpu_count(), "physical_cores": psutil.cpu_count(logical=False), "total_ram_bytes": psutil.virtual_memory().total}
    except ImportError: raise RuntimeError("psutil is required for formal hardware receipt")
    try:
        import numpy; numpy_version = numpy.__version__
    except ImportError: numpy_version = "not-installed"
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "hardware": hardware, "packages": packages | {"numpy": numpy_version, "sqlite": sqlite3.sqlite_version}, "model": v1.file_tree_receipt(model_dir), "original": v1.git_state(original_root), "latest": v1.git_state(ROOT), "lockfiles": {path.name: sha256_file(path) for path in sorted(ROOT.glob("*lock*")) if path.is_file()}}


def validate_environment_receipt(receipt: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """Reject unpinned model/provider/repository environments before custody."""
    model = receipt.get("model", {})
    if model.get("sha256") != manifest["run"]["model"]["file_tree_sha256"]:
        raise ValueError("MiniLM file tree digest mismatch")
    original = receipt.get("original", {})
    if original.get("git_head") != manifest["original"]["commit"] or original.get("git_tree") != manifest["original"]["tree"] or original.get("git_dirty"):
        raise ValueError("original public-product checkout is not the clean pinned source")
    if receipt.get("latest", {}).get("git_dirty"):
        raise ValueError("current product checkout must be clean for a formal AERP5 run")


def paired_cluster_bootstrap(rows: list[dict[str, Any]], *, current: str, original: str, estimand: str) -> dict[str, Any]:
    """Deterministic bootstrap over conversation clusters and original index replicates."""
    conversations = sorted({str(row["conversation_id"]) for row in rows})
    if not conversations or any(len(row.get("original_replicate_recall", [])) != ORIGINAL_REPEATS for row in rows):
        raise ValueError("hierarchical bootstrap requires every original index-build replicate")
    by_conversation = {conversation: [row for row in rows if row["conversation_id"] == conversation] for conversation in conversations}
    # Pre-aggregate before resampling.  This keeps the 5,000-resample protocol
    # proportional to clusters/replicates rather than 5,000 x 1,982 Python rows.
    aggregates = {
        conversation: {
            "count": len(group),
            "current_sum": sum(float(row["recall"][current]) for row in group),
            "original_sums": [sum(float(row["original_replicate_recall"][replicate]) for row in group) for replicate in range(ORIGINAL_REPEATS)],
        }
        for conversation, group in by_conversation.items()
    }
    seed, resamples = 20260822, 5000
    generator = random.Random(seed)
    plan = [
        {"conversations": [conversations[generator.randrange(len(conversations))] for _ in conversations], "replicates": [generator.randrange(ORIGINAL_REPEATS) for _ in range(ORIGINAL_REPEATS)]}
        for _ in range(resamples)
    ]
    def estimate(sampled_conversations: Sequence[str], sampled_replicates: Sequence[int]) -> float:
        if estimand == "conversation_macro":
            current_value = statistics.fmean(aggregates[conversation]["current_sum"] / aggregates[conversation]["count"] for conversation in sampled_conversations)
            original_value = statistics.fmean(statistics.fmean(aggregates[conversation]["original_sums"][replicate] / aggregates[conversation]["count"] for replicate in sampled_replicates) for conversation in sampled_conversations)
        elif estimand == "question_macro_cluster_bootstrap":
            denominator = sum(aggregates[conversation]["count"] for conversation in sampled_conversations)
            current_value = sum(aggregates[conversation]["current_sum"] for conversation in sampled_conversations) / denominator
            original_value = sum(statistics.fmean(aggregates[conversation]["original_sums"][replicate] for replicate in sampled_replicates) for conversation in sampled_conversations) / denominator
        else:
            raise ValueError("unknown paired bootstrap estimand")
        return current_value - original_value
    point = estimate(conversations, list(range(ORIGINAL_REPEATS)))
    values = sorted(estimate(entry["conversations"], entry["replicates"]) for entry in plan)
    return {
        "estimand": estimand, "point_estimate": point, "lower_95": values[math.floor(.025 * (resamples - 1))],
        "upper_95": values[math.ceil(.975 * (resamples - 1))], "seed": seed, "resamples": resamples,
        "conversation_count": len(conversations), "question_count": len(rows), "original_replicate_count": ORIGINAL_REPEATS,
        "bootstrap_sha256": canonical_sha256(plan),
    }


def _normalized_function_sha256(function: Any) -> str:
    source = inspect.getsource(function).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def build_scientific_decision(
    aggregate: Mapping[str, Any], gates: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply the frozen, manifest-bound primary comparison decision rule."""
    question = aggregate["paired_cluster_bootstrap"]["p5_vs_original_question"]
    conversation = aggregate["paired_cluster_bootstrap"]["p5_vs_original_conversation"]
    return {
        "rule": "both P5-vs-original overall paired CI lower bounds strictly exceed frozen zero",
        "question_ci_lower": question["lower_95"],
        "conversation_ci_lower": conversation["lower_95"],
        "go": (
            question["lower_95"] > gates["p5_vs_original_question_ci_lower_strictly_gt"]
            and conversation["lower_95"] > gates["p5_vs_original_conversation_ci_lower_strictly_gt"]
        ),
    }


def scorer_implementation_receipt(original_root: Path) -> dict[str, Any]:
    """Independent semantic anchor for every function that consumes score labels.

    This deliberately does not hash the complete runner: that would create a
    circular pin around unrelated orchestration code.  It hashes normalized
    source for the scoring/aggregation/decision functions and the two external
    scorer dependencies that can affect official metrics.
    """
    protocol = original_root / "benchmarks" / "locomo_story_protocol.py"
    return {
        "schema": "aerp5-v2-scorer-contract-v1",
        "runner_functions": {
            name: _normalized_function_sha256(function)
            for name, function in (
                ("score_after_all_freezes", score_after_all_freezes),
                ("summarize_scored_rows", summarize_scored_rows),
                ("paired_cluster_bootstrap", paired_cluster_bootstrap),
                ("original_replicate_reports", original_replicate_reports),
                ("build_scientific_decision", build_scientific_decision),
                ("assemble_custodian_scored_report", assemble_custodian_scored_report),
            )
        },
        "dependencies": {
            "aerp1_scorer_sha256": sha256_file(Path(aerp1.__file__)),
            "original_locomo_story_protocol_sha256": sha256_file(protocol),
        },
        "contract": {
            "schemas": {
                "runner": SCHEMA,
                "custodian_score": SCHEMA + "-custodian-score",
                "minilm_checkpoint": MINILM_P5_CHECKPOINT_SCHEMA,
            },
            "top_k": TOP_K,
            "repeats_by_arm": dict(REPEATS_BY_ARM),
            "bootstrap": {"seed": 20260822, "resamples": 5000},
            "primary_decision": "p5_vs_original_question_and_conversation_ci_lower_strictly_gt_frozen_zero",
        },
    }


def validate_scorer_contract(*, manifest: Mapping[str, Any], original_root: Path) -> dict[str, Any]:
    """Require the live label consumer to equal the independent manifest anchor."""
    anchor = manifest.get("scorer_contract")
    if not isinstance(anchor, Mapping) or set(anchor) != {"receipt", "receipt_sha256"}:
        raise ValueError("canonical scorer contract anchor is malformed")
    expected = anchor["receipt"]
    if canonical_sha256(expected) != anchor["receipt_sha256"]:
        raise RuntimeError("canonical scorer contract anchor digest drifted")
    actual = scorer_implementation_receipt(original_root)
    if actual != expected:
        raise RuntimeError("live scorer contract differs from the canonical manifest anchor")
    return actual


def validate_custodian_canonical_inputs(
    config: Mapping[str, Any], *, manifest: Mapping[str, Any]
) -> tuple[Path, Path, Path]:
    """Reject mutable score-config aliases before the custodian can read labels.

    A score config is transport metadata, not an authority to redirect the
    formal run.  Every public projection, official dataset, MiniLM identity,
    and original-product root is re-bound to the canonical manifest here.  In
    particular, a matching hash supplied beside an alternate path cannot make
    that path authoritative.
    """
    projection_pin = manifest["projection"]
    dataset_pin = manifest["dataset"]
    canonical_projection = Path(str(projection_pin["path"])).resolve()
    canonical_dataset = Path(str(dataset_pin["path"])).resolve()
    canonical_original = Path(str(manifest["original"]["repo"])).resolve()
    projection_path = Path(str(config.get("projection"))).resolve()
    dataset_path = Path(str(config.get("dataset"))).resolve()
    configured_original = Path(str(config.get("original_root"))).resolve()
    if projection_path != canonical_projection:
        raise RuntimeError("custodian projection path is not the canonical manifest artifact")
    if config.get("expected_projection_sha256") != projection_pin["file_sha256"]:
        raise RuntimeError("custodian projection file digest is not the canonical manifest pin")
    if config.get("expected_projection_content_sha256") != projection_pin["content_sha256"]:
        raise RuntimeError("custodian projection content digest is not the canonical manifest pin")
    if dataset_path != canonical_dataset:
        raise RuntimeError("custodian dataset path is not the canonical manifest artifact")
    if config.get("expected_dataset_sha256") != dataset_pin["sha256"]:
        raise RuntimeError("custodian dataset digest is not the canonical manifest pin")
    if config.get("expected_model_sha256") != manifest["run"]["model"]["file_tree_sha256"]:
        raise RuntimeError("custodian MiniLM model digest is not the canonical manifest pin")
    if configured_original != canonical_original:
        raise RuntimeError("custodian original root is not the canonical manifest repository")
    original_state = v1.git_state(canonical_original)
    if (
        original_state.get("git_head") != manifest["original"]["commit"]
        or original_state.get("git_tree") != manifest["original"]["tree"]
        or original_state.get("git_dirty") is not False
    ):
        raise RuntimeError("custodian original repository is not the clean canonical manifest checkout")
    return projection_path, dataset_path, canonical_original


def preflight_custodian_freeze_paths(
    config: Mapping[str, Any], *, projection_path: Path, dataset_path: Path, canonical_original_root: Path
) -> tuple[Path, dict[str, list[Path]]]:
    """Validate the complete dynamic freeze batch before reading any of it.

    Worker output is dynamic, but it cannot choose an arbitrary source path.
    Every freeze must be a unique, ordinary file in the coordinator's external
    work namespace.  Metadata-only identity checks reject symlink/hard-link
    aliases to protected static inputs before any hash, JSON parse, or label
    access can occur.
    """
    raw_work = config.get("work")
    if not isinstance(raw_work, str) or not raw_work:
        raise ValueError("custodian requires the coordinator work namespace")
    work = Path(raw_work).resolve()
    v1.require_external_output(work, ROOT, canonical_original_root)
    if not work.is_dir():
        raise ValueError("custodian work namespace is missing")
    raw_freezes = config.get("freezes")
    if not isinstance(raw_freezes, Mapping) or set(raw_freezes) != set(ALL_ARMS):
        raise ValueError("custodian input contract is malformed")
    protected_inputs = (
        projection_path,
        dataset_path,
        CANONICAL_MANIFEST_PATH.resolve(),
        Path(__file__).resolve(),
        Path(aerp1.__file__).resolve(),
        (canonical_original_root / "benchmarks" / "locomo_story_protocol.py").resolve(),
    )
    normalized: dict[str, list[Path]] = {}
    for arm in ALL_ARMS:
        raw_paths = raw_freezes[arm]
        if not isinstance(raw_paths, list) or len(raw_paths) != REPEATS_BY_ARM[arm]:
            raise ValueError("custodian freeze repeat count is malformed")
        paths: list[Path] = []
        for repeat, raw_path in enumerate(raw_paths):
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError("custodian freeze path is malformed")
            declared = Path(raw_path)
            path = declared.resolve()
            expected_name = f"{arm}-{repeat}.json"
            if path.parent != work or path.name != expected_name:
                raise RuntimeError("custodian freeze path is outside the coordinator work namespace")
            if declared.is_symlink() or path.is_symlink():
                raise RuntimeError("custodian freeze path must not be a symlink")
            try:
                metadata = path.lstat()
            except FileNotFoundError as exc:
                raise RuntimeError("custodian freeze file is missing") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("custodian freeze path must be a regular file")
            if metadata.st_nlink != 1:
                raise RuntimeError("custodian freeze file must not be hard-linked")
            for protected in protected_inputs:
                try:
                    if os.path.samefile(path, protected):
                        raise RuntimeError("custodian freeze file aliases a protected static input")
                except FileNotFoundError as exc:
                    raise RuntimeError("custodian protected static input is missing") from exc
            paths.append(path)
        normalized[arm] = paths
    return work, normalized


def score_after_all_freezes(*, scorer: Any, freezes: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Only legal scoring entry point; current repeats are exact, original has five frozen builds."""
    if set(freezes) != set(ALL_ARMS): raise RuntimeError("all arms must freeze before scoring")
    rankings: dict[str, dict[str, list[str]]] = {}
    for arm, repeats in freezes.items():
        if len(repeats) != REPEATS_BY_ARM[arm]:
            raise RuntimeError("all arm reruns must complete with the frozen repeat counts")
        if arm != ARM_ORIGINAL and any(
            repeats[0]["projection_sha256"] != repeat["projection_sha256"]
            for repeat in repeats[1:]
        ):
            raise RuntimeError("current arm reruns must complete with stable ranking digests")
        rankings[arm] = dict(repeats[0]["items"])
    # The custodian API is intentionally explicit; no label object appears in rank/freeze APIs.
    rows: list[dict[str, Any]] = []
    for token, item in scorer.scorer_items.items():
        official = item.official_exact
        metrics = {arm: aerp1.question_metrics(ranking[token]["dialog_top10"], official.resolved_opaque_dialog_ids, evidence_item_count=official.source_evidence_item_count, unresolved_evidence_item_count=official.unresolved_evidence_item_count, top_k=TOP_K) for arm, ranking in rankings.items()}
        original_replicate_metrics = [aerp1.question_metrics(repeat["items"][token]["dialog_top10"], official.resolved_opaque_dialog_ids, evidence_item_count=official.source_evidence_item_count, unresolved_evidence_item_count=official.unresolved_evidence_item_count, top_k=TOP_K) for repeat in freezes[ARM_ORIGINAL]]
        # The primary original estimand is the arithmetic per-query mean across all five
        # fresh batch-index builds, not the first replicate or a pooled query list.
        metrics[ARM_ORIGINAL] = {key: statistics.fmean(float(metric[key]) for metric in original_replicate_metrics) if isinstance(original_replicate_metrics[0][key], (int, float)) and not isinstance(original_replicate_metrics[0][key], bool) else original_replicate_metrics[0][key] for key in original_replicate_metrics[0]}
        if not all(metric["scored"] for metric in metrics.values()): raise ValueError("AERP4 membership must not contain a zero-evidence score row")
        recall = {arm: float(metric["recall_at_10"]) for arm, metric in metrics.items()}
        rows.append({"conversation_id": item.opaque_conversation_id, "item_token": token, "category": item.category, "unresolved": official.unresolved_evidence_item_count, "official_metrics": metrics, "original_replicate_metrics": original_replicate_metrics, "original_replicate_recall": [float(metric["recall_at_10"]) for metric in original_replicate_metrics], "recall": recall})
    if len(rows) != 1982: raise RuntimeError("scoring denominator must remain 1,982")
    return {"question_macro": {arm: statistics.fmean(row["recall"][arm] for row in rows) for arm in ALL_ARMS}, "paired_cluster_bootstrap": {"p5_vs_original_question": paired_cluster_bootstrap(rows, current=ARM_P5, original=ARM_ORIGINAL, estimand="question_macro_cluster_bootstrap"), "p5_vs_original_conversation": paired_cluster_bootstrap(rows, current=ARM_P5, original=ARM_ORIGINAL, estimand="conversation_macro")}}, rows


def summarize_scored_rows(subset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate every frozen official endpoint for one named report slice."""
    if not subset:
        return {"count": 0}
    conversations = sorted({str(row["conversation_id"]) for row in subset})
    arm_metrics: dict[str, dict[str, Any]] = {}
    for arm in ALL_ARMS:
        metrics = [row["official_metrics"][arm] for row in subset]
        denominator = sum(metric["evidence_item_count"] for metric in metrics)
        if denominator <= 0:
            raise ValueError("scored report slice has no evidence denominator")
        arm_metrics[arm] = {
            "question_macro_recall_at_10": statistics.fmean(float(metric["recall_at_10"]) for metric in metrics),
            "conversation_macro_recall_at_10": statistics.fmean(
                statistics.fmean(float(row["recall"][arm]) for row in subset if row["conversation_id"] == conversation)
                for conversation in conversations
            ),
            "evidence_micro_recall_at_10": sum(metric["retrieved_evidence_count_at_10"] for metric in metrics) / denominator,
            "hit_at_10": statistics.fmean(float(metric["hit_at_10"]) for metric in metrics),
            "all_at_10": statistics.fmean(float(metric["all_at_10"]) for metric in metrics),
            "ndcg_at_10": statistics.fmean(float(metric["ndcg_at_10"]) for metric in metrics),
            "evidence_item_count": denominator,
            "resolved_evidence_item_count": sum(metric["resolved_evidence_item_count"] for metric in metrics),
            "unresolved_evidence_item_count": sum(metric["unresolved_evidence_item_count"] for metric in metrics),
        }
    return {
        "count": len(subset),
        "arms": arm_metrics,
        "paired": {
            "question": paired_cluster_bootstrap(list(subset), current=ARM_P5, original=ARM_ORIGINAL, estimand="question_macro_cluster_bootstrap"),
            "conversation": paired_cluster_bootstrap(list(subset), current=ARM_P5, original=ARM_ORIGINAL, estimand="conversation_macro"),
        },
    }


def original_replicate_reports(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Report original batch-index variation explicitly for every required score slice."""
    def original_metrics(subset: Sequence[Mapping[str, Any]], replicate: int) -> dict[str, Any]:
        if not subset:
            return {"count": 0}
        conversations = sorted({str(row["conversation_id"]) for row in subset})
        metrics = [row["original_replicate_metrics"][replicate] for row in subset]
        denominator = sum(metric["evidence_item_count"] for metric in metrics)
        return {
            "count": len(subset), "question_macro_recall_at_10": statistics.fmean(float(metric["recall_at_10"]) for metric in metrics),
            "conversation_macro_recall_at_10": statistics.fmean(statistics.fmean(float(row["original_replicate_metrics"][replicate]["recall_at_10"]) for row in subset if row["conversation_id"] == conversation) for conversation in conversations),
            "evidence_micro_recall_at_10": sum(metric["retrieved_evidence_count_at_10"] for metric in metrics) / denominator,
            "hit_at_10": statistics.fmean(float(metric["hit_at_10"]) for metric in metrics),
            "all_at_10": statistics.fmean(float(metric["all_at_10"]) for metric in metrics),
            "ndcg_at_10": statistics.fmean(float(metric["ndcg_at_10"]) for metric in metrics),
            "evidence_item_count": denominator,
        }
    slices = {
        "overall": rows,
        "hard_cat1_2": [row for row in rows if row["category"] in {1, 2}],
        "cat5": [row for row in rows if row["category"] == 5],
        **{f"category_{category}": [row for row in rows if row["category"] == category] for category in sorted({row["category"] for row in rows})},
    }
    replicates = [{"replicate": replicate, "slices": {name: original_metrics(subset, replicate) for name, subset in slices.items()}} for replicate in range(ORIGINAL_REPEATS)]
    variability: dict[str, dict[str, dict[str, float]]] = {}
    for name in slices:
        numeric = ("question_macro_recall_at_10", "conversation_macro_recall_at_10", "evidence_micro_recall_at_10", "hit_at_10", "all_at_10", "ndcg_at_10")
        variability[name] = {
            metric: {"min": min(float(replica["slices"][name][metric]) for replica in replicates), "max": max(float(replica["slices"][name][metric]) for replica in replicates), "range": max(float(replica["slices"][name][metric]) for replica in replicates) - min(float(replica["slices"][name][metric]) for replica in replicates), "std": statistics.pstdev(float(replica["slices"][name][metric]) for replica in replicates)}
            for metric in numeric if replicates[0]["slices"][name].get("count", 0)
        }
    return {"replicate_count": ORIGINAL_REPEATS, "replicates": replicates, "variability": variability}


def assemble_custodian_scored_report(
    *,
    scorer: Any,
    projection_items: Sequence[Mapping[str, Any]],
    freezes: Mapping[str, Sequence[Mapping[str, Any]]],
    gates: Mapping[str, Any],
    scorer_receipt: Mapping[str, Any],
    input_receipts: Mapping[str, Any],
    aerp4_lineage: Mapping[str, Any],
    minilm_p5_checkpoint: Mapping[str, Any],
    coordinator_original_index_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Pure post-label score core: join, score, aggregate, decide, and report.

    All filesystem validation and label loading remain in the custodian shell.
    This function receives only already-loaded scorer objects and frozen,
    explicit receipts, which makes every operation that can change reported
    quality part of the independent scorer contract.
    """
    item_to_token = {row["item_id"]: row["item_token"] for row in projection_items}
    scorer_items = {
        item_to_token[item_id]: item
        for item_id, item in scorer.scorer_items.items()
        if item_id in item_to_token
    }
    scorer_view = type("ScorerView", (), {"scorer_items": scorer_items})()
    aggregate, rows = score_after_all_freezes(scorer=scorer_view, freezes=freezes)
    unresolved_from_rows = sum(row["unresolved"] for row in rows)
    unresolved_from_ledger = sum(
        scorer.scorer_items[item_id].official_exact.unresolved_evidence_item_count
        for item_id in item_to_token
    )
    if unresolved_from_rows != unresolved_from_ledger:
        raise RuntimeError("unresolved evidence disappeared from score ledger")
    categories = {
        str(category): summarize_scored_rows([row for row in rows if row["category"] == category])
        for category in sorted({row["category"] for row in rows})
    }
    return {
        "schema": SCHEMA + "-custodian-score",
        "scorer_implementation_receipt": dict(scorer_receipt),
        "input_receipts": dict(input_receipts),
        "aerp4_lineage": dict(aerp4_lineage),
        "minilm_p5_checkpoint": dict(minilm_p5_checkpoint),
        "coordinator_original_index_receipts": [dict(receipt) for receipt in coordinator_original_index_receipts],
        "aggregate": aggregate,
        "overall": summarize_scored_rows(rows),
        "hard_cat1_2": summarize_scored_rows([row for row in rows if row["category"] in {1, 2}]),
        "cat5": summarize_scored_rows([row for row in rows if row["category"] == 5]),
        "per_category": categories,
        "original_replicate_variability": original_replicate_reports(rows),
        "scientific_decision": build_scientific_decision(aggregate, gates),
        "unresolved_evidence_item_count": unresolved_from_rows,
        "freeze_digests": dict(input_receipts["freeze_sha256"]),
    }


def custodian_score_run(config: Mapping[str, Any]) -> dict[str, Any]:
    """The only stage that reads official evidence labels, after all freezes."""
    gates = config.get("scientific_gates")
    if not isinstance(gates, Mapping) or dict(gates) != EXPECTED_SCIENTIFIC_GATES:
        raise ValueError("custodian needs the canonical frozen scientific gates")
    if canonical_sha256(gates) != config.get("expected_scientific_gates_sha256"):
        raise RuntimeError("custodian scientific gates drifted")
    canonical_manifest = load_manifest()
    if config.get("manifest_sha256") != canonical_sha256(canonical_manifest):
        raise RuntimeError("custodian canonical manifest receipt drifted")
    projection_path, dataset_path, canonical_original_root = validate_custodian_canonical_inputs(
        config, manifest=canonical_manifest
    )
    _work, freeze_paths = preflight_custodian_freeze_paths(
        config, projection_path=projection_path, dataset_path=dataset_path,
        canonical_original_root=canonical_original_root,
    )
    scorer_receipt = validate_scorer_contract(manifest=canonical_manifest, original_root=canonical_original_root)
    if scorer_receipt != config.get("scorer_implementation_receipt"): raise RuntimeError("scorer implementation receipt drift before scoring")
    if sha256_file(projection_path) != config.get("expected_projection_sha256"):
        raise RuntimeError("custodian projection drift before scoring")
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projection_items, _projection_conversations = _validate_public_projection(projection)
    projection_content_sha256 = canonical_sha256(projection)
    if projection_content_sha256 != config.get("expected_projection_content_sha256"):
        raise RuntimeError("custodian projection content drift before scoring")
    projection_tokens = tuple(row["item_token"] for row in projection_items)
    identity_namespace = original_identity_namespace(_projection_conversations)
    expected_index_receipts = config.get("expected_original_index_receipts")
    if not isinstance(expected_index_receipts, list) or len(expected_index_receipts) != ORIGINAL_REPEATS or config.get("expected_original_index_receipts_sha256") != canonical_sha256(expected_index_receipts):
        raise RuntimeError("custodian original index-build receipts are incomplete or drifted")
    freezes: dict[str, list[dict[str, Any]]] = {}
    for arm, paths in freeze_paths.items():
        if not isinstance(paths, list) or len(paths) != REPEATS_BY_ARM[arm]: raise ValueError("custodian freeze repeat count is malformed")
        if [sha256_file(Path(path)) for path in paths] != config.get("expected_freeze_sha256", {}).get(arm): raise RuntimeError("freeze drift before scoring")
        repeats = [parse_worker_freeze(_json(Path(path)), arm=arm, projection_sha256=config["expected_projection_sha256"], projection_content_sha256=projection_content_sha256, item_tokens=projection_tokens, model_sha256=config["expected_model_sha256"], identity_namespace=identity_namespace if arm == ARM_ORIGINAL else None) for path in paths]
        if arm == ARM_ORIGINAL and [repeat["original_index_build_receipt"] for repeat in repeats] != expected_index_receipts:
            raise RuntimeError("custodian frozen original index-build receipts differ from coordinator receipts")
        validate_repeat_identity(arm, repeats)
        freezes[arm] = repeats
    # These label-free lineage/checkpoint gates deliberately precede every
    # original-protocol load, which is the first operation that can expose labels.
    aerp4_lineage = validate_aerp4_lineage_receipt(
        config.get("aerp4_lineage"), manifest=canonical_manifest,
        expected_item_tokens=projection_tokens,
    )
    minilm_p5_checkpoint = validate_minilm_p5_checkpoint(
        config.get("minilm_p5_checkpoint"),
        expected_checkpoint_sha256=config.get("expected_minilm_p5_checkpoint_sha256"),
        repeats=freezes[ARM_P5], expected_model_sha256=config["expected_model_sha256"],
        expected_item_tokens=projection_tokens,
    )
    validate_aerp4_minilm_checkpoint_binding(aerp4_lineage, minilm_p5_checkpoint)
    # This is deliberately after complete-rerun and label-free checkpoint validation.
    if sha256_file(dataset_path) != config.get("expected_dataset_sha256"):
        raise RuntimeError("custodian dataset drift before scoring")
    input_receipts = {
        "dataset_sha256": sha256_file(dataset_path),
        "projection_sha256": sha256_file(projection_path),
        "projection_content_sha256": projection_content_sha256,
        "freeze_sha256": {arm: [sha256_file(path) for path in paths] for arm, paths in freeze_paths.items()},
        "original_index_receipts_sha256": canonical_sha256(expected_index_receipts),
        "aerp4_lineage_sha256": canonical_sha256(aerp4_lineage),
        "minilm_p5_checkpoint_sha256": canonical_sha256(minilm_p5_checkpoint),
        "scientific_gates_sha256": canonical_sha256(gates),
        "manifest_sha256": config.get("manifest_sha256"),
    }
    _palace, _searcher, protocol, _state = v1.load_original_product(canonical_original_root)
    dataset = protocol.load_official_locomo10(dataset_path)
    _retrieval, scorer = protocol.prepare_hard_story_track(
        dataset, candidate_pool_size=TOP_K, require_official_counts=True
    )
    result = assemble_custodian_scored_report(
        scorer=scorer, projection_items=projection_items, freezes=freezes, gates=gates,
        scorer_receipt=scorer_receipt, input_receipts=input_receipts,
        aerp4_lineage=aerp4_lineage, minilm_p5_checkpoint=minilm_p5_checkpoint,
        coordinator_original_index_receipts=expected_index_receipts,
    )
    if validate_scorer_contract(manifest=canonical_manifest, original_root=canonical_original_root) != scorer_receipt: raise RuntimeError("scorer implementation receipt drift after scoring")
    if sha256_file(projection_path) != config.get("expected_projection_sha256") or sha256_file(dataset_path) != config.get("expected_dataset_sha256") or any([sha256_file(path) for path in paths] != config["expected_freeze_sha256"][arm] for arm, paths in freeze_paths.items()): raise RuntimeError("custodian input drift after scoring")
    _publish_nonreplace(Path(str(config["output"])), _canonical(result)); return result


def publish_report(*, output: Path, manifest: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    if output.exists(): raise FileExistsError("refusing to clobber formal AERP5 report")
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    if temporary.exists(): raise FileExistsError("stale temporary report exists")
    payload = {"schema": SCHEMA + "-report", "manifest_sha256": canonical_sha256(manifest), "public_nonblind": True, "confirmation_claim": False, "product_comparison_complete": bool(report.get("engineering_gates_passed")), "resource_gate_eligible": False, "resource_gate_blockers": ["mixed-visibility", "blind-180", "30k-event", "SQLite/drawer failure-injection", "frozen resource thresholds"], "report": dict(report)}
    temporary.unlink(missing_ok=True)
    _publish_nonreplace(output, _canonical(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("aerp5_product_paired_locomo_v2_manifest.json"))
    parser.add_argument("--tau", help=argparse.SUPPRESS)
    parser.add_argument("--router", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker-config", type=Path)
    parser.add_argument("--score-config", type=Path)
    parser.add_argument("--projection", type=Path); parser.add_argument("--dataset", type=Path); parser.add_argument("--original-root", type=Path); parser.add_argument("--model-dir", type=Path); parser.add_argument("--train-freeze", type=Path); parser.add_argument("--dev-freeze", type=Path); parser.add_argument("--work", type=Path)
    args = parser.parse_args(argv)
    if args.tau is not None or args.router is not None: raise ValueError("AERP5 v2 rejects tau/router inputs")
    load_manifest(args.manifest)
    if args.worker_config is not None:
        worker_run(_json(args.worker_config)); return 0
    if args.score_config is not None:
        custodian_score_run(_json(args.score_config)); return 0
    required = (args.projection, args.dataset, args.original_root, args.model_dir, args.train_freeze, args.dev_freeze, args.work, args.output)
    if any(value is None for value in required): parser.error("coordinator requires pinned projection/dataset/original/model/ranking-freezes/work/output")
    manifest = load_manifest(args.manifest)
    coordinator_run(projection_path=args.projection, dataset=args.dataset, original_root=args.original_root, model_dir=args.model_dir, train=PinnedJson.pin(args.train_freeze, manifest["aerp4"]["train_ranking_freeze_sha256"]), dev=PinnedJson.pin(args.dev_freeze, manifest["aerp4"]["dev_ranking_freeze_sha256"]), work=args.work, output=args.output, manifest=manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
