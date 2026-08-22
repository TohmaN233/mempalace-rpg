"""Frozen, annotation-free AERP-2 Product Six-View LoCoMo quality harness.

This module owns the fail-closed quality contract.  The expensive model execution
is deliberately injected by the pinned historical-source adapter: no tokenizer,
ONNX setup, or scorer implementation is duplicated here.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import random
import tempfile
import sys
from pathlib import Path
from typing import Any, Iterable

from mempalace_rpg import RpgMemoryKernel, SceneEventInput, SixViewRanker
from benchmarks import aerp1_locomo_three_way as aerp1
from benchmarks.aerp2_historical_export import HISTORICAL_COMMIT, _extract_historical_source


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "fixtures" / "aerp2_product_six_view_locomo_manifest.json"
EXPECTED_MANIFEST_SHA256 = "a67352b1f9d9001cfaeedc6b0635d3284603f8f7da7768cf4e6c05c4a9f2d2ea"
FORBIDDEN_ANNOTATION_FIELDS = ["observation", "session_summary", "event_summary", "answer", "evidence", "category", "adversarial_answer"]
ARMS = ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense", "legacy_rpg", "product_six_view", "historical_six_view")
RANKING_TRACE_REQUIRED = frozenset({"schema", "query_sha256", "input_sha256", "view_digests", "encoder_identity", "weights", "rrf_k", "selected"})
FROZEN_SIX_VIEW_WEIGHTS = {"raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0}
SAFETY_CHECKS = (
    "trace_count", "audit_complete", "ranking_schema", "selected_matches_product_output",
    "unauthorized_selected", "legacy_mapping_1to1", "product_mapping_1to1",
    "lineage_forbidden", "lineage_mapping_digest", "ranking_key_mapping_digest",
    "trace_identity_sets", "nonempty_selection",
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode())


def load_manifest(path: Path = MANIFEST_PATH) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    if _sha256(raw) != EXPECTED_MANIFEST_SHA256:
        raise ValueError("product LoCoMo manifest SHA256 mismatch")
    manifest = json.loads(raw)
    if manifest.get("schema") != "aerp2-product-six-view-locomo-manifest":
        raise ValueError("unexpected product LoCoMo manifest schema")
    text = json.dumps(manifest)
    if ":\\" in text or "\"/" in text:
        raise ValueError("product LoCoMo manifest must not contain absolute paths")
    protocol = manifest["protocol"]
    if (protocol["conversations"], protocol["sessions"], protocol["dialogs"], protocol["questions"], protocol["hard_questions"], protocol["top_k"], protocol["source_pool"], protocol["rrf_k"]) != (10, 272, 5882, 1986, 603, 10, 50, 60):
        raise ValueError("frozen product LoCoMo denominators changed")
    validate_gate_thresholds(manifest.get("gates"))
    return manifest, _sha256(raw)


def _dialog_text(dialog: dict[str, Any]) -> str:
    required = ("opaque_dialog_id", "speaker", "date", "caption", "text")
    if any(not isinstance(dialog.get(name), str) for name in required):
        raise ValueError("sanitized dialog fields are malformed")
    return "\n".join(f"{name}: {dialog[name]}" for name in ("speaker", "date", "caption", "text"))


def validate_annotation_lineage(audit: dict[str, Any]) -> None:
    ledger = audit.get("seed_ledger")
    if not isinstance(ledger, dict) or ledger.get("allowed_source_fields") != ["speaker", "date", "caption", "text", "session_membership"]:
        raise ValueError("annotation lineage ledger is malformed")
    forbidden = ledger.get("forbidden_field_counts")
    if not isinstance(forbidden, dict) or set(forbidden) != set(FORBIDDEN_ANNOTATION_FIELDS) or any(not isinstance(forbidden[field], int) or forbidden[field] != 0 for field in FORBIDDEN_ANNOTATION_FIELDS):
        raise ValueError("annotation leakage into ranker inputs")
    if not isinstance(ledger.get("checkpoint_ranking_mapping_sha256"), str) or len(ledger["checkpoint_ranking_mapping_sha256"]) != 64 or not isinstance(ledger.get("ranker_texts_sha256"), str) or len(ledger["ranker_texts_sha256"]) != 64 or not isinstance(ledger.get("dialog_count"), int):
        raise ValueError("annotation lineage mapping is malformed")


def seed_sanitized_conversation(
    kernel: RpgMemoryKernel,
    conversation: dict[str, Any],
    *,
    conversation_id: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Seed one retrieval payload using its bundle-owned conversation identity."""
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("sanitized conversation id is required")
    event_to_dialog: dict[str, str] = {}
    mapping: list[tuple[str, str]] = []; ranker_texts: list[tuple[str, str]] = []
    for session_index, session in enumerate(conversation.get("sessions", [])):
        session_id = session.get("opaque_session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("sanitized session id is required")
        for dialog_index, dialog in enumerate(session.get("dialogs", [])):
            text = _dialog_text(dialog)
            dialog_id = dialog["opaque_dialog_id"]
            scene_id = f"locomo-{conversation_id}-{dialog_id}"
            kernel.commit_scene(campaign_id=conversation_id, scene_id=scene_id, in_world_time=dialog["date"], transcript=text, events=[SceneEventInput(event_type="locomo_dialog", summary=text, branch_id="main", branch_status="active", truth_status="canonical", visibility="public_world", actor_id=dialog["speaker"], source_span=text, payload={"retrieval_ranking_key": dialog_id, "retrieval_checkpoint_id": f"{conversation_id}/{session_id}"})])
            row = kernel._conn().execute("SELECT event_id FROM scene_event WHERE scene_id=?", (scene_id,)).fetchone()
            if row is None:
                raise RuntimeError("LoCoMo event seeding failed")
            event_to_dialog[str(row["event_id"])] = dialog_id
            mapping.append((dialog_id, f"{conversation_id}/{session_id}"))
            ranker_texts.append((dialog_id, text))
    if len(event_to_dialog) != len(set(event_to_dialog.values())):
        raise ValueError("LoCoMo event-to-dialog mapping is not one-to-one")
    ledger = {"conversation_count": 1, "session_count": len(conversation.get("sessions", [])), "dialog_count": len(mapping), "allowed_source_fields": ["speaker", "date", "caption", "text", "session_membership"], "forbidden_field_counts": {field: 0 for field in FORBIDDEN_ANNOTATION_FIELDS}, "checkpoint_ranking_mapping_sha256": _canonical(mapping), "ranker_texts_sha256": _canonical(ranker_texts)}
    audit = {"seed_ledger": ledger}
    validate_annotation_lineage(audit)
    return event_to_dialog, {"annotation_lineage": ledger, "ranker_texts_sha256": ledger["ranker_texts_sha256"], "seed_ledger": ledger}


def validate_r0_raw_bm25(expected: dict[tuple[str, str], list[str]], actual: dict[tuple[str, str], list[str]]) -> None:
    if set(expected) != set(actual):
        raise RuntimeError("raw BM25 question identities differ")
    for key in sorted(expected):
        if expected[key] != actual[key]:
            raise RuntimeError(f"raw BM25 ranking mismatch: {key[0]}:{key[1]}")


def validate_historical_join(rows: Iterable[dict[str, Any]], historical: dict[tuple[str, str], dict[str, Any]]) -> None:
    local = {(row["conversation_id"], row["question_id"]): row for row in rows}
    if set(local) != set(historical):
        raise ValueError("historical join identities differ")
    for key in local:
        for field in ("category", "gold", "denominator", "corpus"):
            if local[key].get(field) != historical[key].get(field):
                raise ValueError(f"historical join mismatch for {field}")


def scorer_contract_records(scorer_items: dict[str, Any], splits: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Canonical local rows matching the independently published scorer fields.

    The artifact publishes the corpus count, rather than its full corpus ID list;
    this comparison deliberately does not fabricate unrecorded historical IDs.
    """
    records = []
    for item_id, item in sorted(scorer_items.items()):
        if not isinstance(item_id, str) or not item_id:
            raise ValueError("local scorer item id is malformed")
        split = (splits or {}).get(item.opaque_conversation_id)
        if not isinstance(split, str) or not split:
            raise ValueError("scorer bundle conversation split is missing")
        official, normalized = item.official_exact, item.normalized_repaired
        corpus_ids = list(item.corpus_opaque_dialog_ids)
        if len(corpus_ids) != len(set(corpus_ids)):
            raise ValueError("local scorer corpus IDs are not unique")
        records.append({"composite_id": [item.opaque_conversation_id, item_id], "category": item.category, "category_name": item.category_name, "split": split, "official_exact": {"resolved_ids": list(official.resolved_opaque_dialog_ids), "count": official.source_evidence_item_count, "unresolved": official.unresolved_evidence_item_count}, "normalized_repaired": {"resolved_ids": list(normalized.gold_opaque_dialog_ids), "count": normalized.unique_dialog_denominator, "unresolved": normalized.unresolved_evidence_item_count}, "corpus_count": len(corpus_ids)})
    return records


def validate_scorer_contract(scorer_items: dict[str, Any], historical_rows: list[dict[str, Any]], splits: dict[str, str] | None = None) -> dict[str, str]:
    local = scorer_contract_records(scorer_items, splits)
    historical = []
    for row in historical_rows:
        if not isinstance(row, dict) or not isinstance(row.get("opaque_conversation_id"), str) or not isinstance(row.get("opaque_question_id"), str) or not isinstance(row.get("split"), str):
            raise ValueError("historical scorer identity or split is malformed")
        scorer = row.get("scorer")
        if not isinstance(scorer, dict): raise ValueError("historical scorer contract is missing")
        semantics = scorer.get("evidence_semantics")
        if not isinstance(semantics, dict): raise ValueError("historical scorer semantics are missing")
        def contract(name: str) -> dict[str, Any]:
            value = semantics.get(name)
            if not isinstance(value, dict): raise ValueError("historical scorer semantics are malformed")
            resolved = value.get("resolved_opaque_dialog_ids")
            # Official evidence items may legitimately point at the same dialog
            # more than once; preserve that evidence multiplicity exactly.
            if not isinstance(resolved, list) or not all(isinstance(value, str) and value for value in resolved):
                raise ValueError("historical scorer resolved IDs are malformed")
            count, unresolved = value.get("evidence_item_count"), value.get("unresolved_evidence_item_count")
            if not isinstance(count, int) or count < 0 or not isinstance(unresolved, int) or unresolved < 0:
                raise ValueError("historical scorer evidence counts are malformed")
            return {"resolved_ids": resolved, "count": count, "unresolved": unresolved}
        if not isinstance(scorer.get("corpus_dialog_count"), int) or scorer["corpus_dialog_count"] < 0:
            raise ValueError("historical scorer corpus count is malformed")
        historical.append({"composite_id": [row.get("opaque_conversation_id"), row.get("opaque_question_id")], "category": scorer.get("category"), "category_name": scorer.get("category_name"), "split": row.get("split"), "official_exact": contract("official_exact"), "normalized_repaired": contract("normalized_repaired"), "corpus_count": scorer.get("corpus_dialog_count")})
    if len(historical) != len({tuple(row["composite_id"]) for row in historical}):
        raise ValueError("historical scorer composite IDs are not unique")
    if _canonical(local) != _canonical(historical): raise ValueError("historical scorer contract mismatch")
    return {"composite_ids_sha256": _canonical([row["composite_id"] for row in local]), "scorer_contract_sha256": _canonical(local)}


def corpus_contract_records(scorer_items: dict[str, Any]) -> list[dict[str, Any]]:
    by_conversation: dict[str, tuple[str, ...]] = {}
    for item in scorer_items.values():
        conversation_id = item.opaque_conversation_id
        corpus = tuple(item.corpus_opaque_dialog_ids)
        if not isinstance(conversation_id, str) or not conversation_id or not corpus or len(corpus) != len(set(corpus)):
            raise ValueError("local corpus contract is malformed")
        if conversation_id in by_conversation and by_conversation[conversation_id] != corpus:
            raise ValueError("local question corpus differs within a conversation")
        by_conversation[conversation_id] = corpus
    return [{"opaque_conversation_id": conversation_id, "corpus_dialog_count": len(corpus), "opaque_dialog_ids": list(corpus)} for conversation_id, corpus in sorted(by_conversation.items())]


def validate_corpus_contract(scorer_items: dict[str, Any], historical_corpora: Any) -> dict[str, str]:
    local = corpus_contract_records(scorer_items)
    if not isinstance(historical_corpora, list):
        raise ValueError("historical corpora contract is missing")
    historical = []
    for row in historical_corpora:
        if not isinstance(row, dict) or not isinstance(row.get("opaque_conversation_id"), str) or not isinstance(row.get("corpus_dialog_count"), int) or not isinstance(row.get("opaque_dialog_ids"), list):
            raise ValueError("historical corpus row is malformed")
        ids = row["opaque_dialog_ids"]
        if row["corpus_dialog_count"] != len(ids) or len(ids) != len(set(ids)) or not all(isinstance(value, str) and value for value in ids):
            raise ValueError("historical corpus IDs are malformed")
        historical.append({"opaque_conversation_id": row["opaque_conversation_id"], "corpus_dialog_count": row["corpus_dialog_count"], "opaque_dialog_ids": ids})
    if len(historical) != len({row["opaque_conversation_id"] for row in historical}) or _canonical(local) != _canonical(sorted(historical, key=lambda row: row["opaque_conversation_id"])):
        raise ValueError("historical full corpus contract mismatch")
    return {"corpus_contract_sha256": _canonical(local)}


def scorer_bundle_conversation_splits(scorer: Any) -> dict[str, str]:
    """Derive the split from the scorer bundle, never from a ScorerItem."""
    frozen_split = getattr(scorer, "split", None)
    split = getattr(frozen_split, "opaque_conversation_id_to_split", None)
    if not isinstance(split, dict):
        raise ValueError("scorer bundle conversation split is missing")
    conversations = {item.opaque_conversation_id for item in scorer.scorer_items.values()}
    if not conversations or any(not isinstance(value, str) or not value for value in conversations):
        raise ValueError("scorer bundle conversation identities are malformed")
    if set(split) != conversations or any(not isinstance(value, str) or not value for value in split.values()):
        raise ValueError("scorer bundle conversation split does not cover scorer items")
    return dict(split)


def _enforce_scorer_contract_digests(result: dict[str, str], expected: Any, *, enforce_expected: bool) -> None:
    if not enforce_expected:
        return
    if not isinstance(expected, dict) or any(not isinstance(expected.get(key), str) or len(expected[key]) != 64 for key in result):
        raise ValueError("manifest scorer-contract digests must be pinned before enforcement")
    if any(expected[key] != value for key, value in result.items()):
        raise ValueError("manifest scorer-contract digest mismatch")


def run_metadata_validation(*, dataset_path: Path, artifact_path: Path, source_repo: Path, manifest_path: Path = MANIFEST_PATH, enforce_expected: bool = True) -> dict[str, str]:
    """Recompute scorer pins with exact protocol blobs; deliberately loads no model."""
    manifest, _ = load_manifest(manifest_path)
    if _sha256(dataset_path.read_bytes()) != manifest["inputs"]["dataset_sha256"] or _sha256(artifact_path.read_bytes()) != manifest["inputs"]["historical_artifact_sha256"]: raise ValueError("metadata input digest mismatch")
    with tempfile.TemporaryDirectory(prefix="aerp2-product-metadata-") as name:
        root = Path(name); observed = _extract_historical_source(source_repo.resolve(), root); validate_historical_source_pins(manifest, observed)
        saved = {module: sys.modules.pop(module, None) for module in _HISTORICAL_MODULES}; sys.path.insert(0, str(root / "benchmarks"))
        try:
            import importlib
            protocol = importlib.import_module("locomo_story_protocol")
            dataset = protocol.load_official_locomo10(dataset_path)
            _retrieval, scorer = protocol.prepare_hard_story_track(dataset, candidate_pool_size=manifest["protocol"]["source_pool"], require_official_counts=True)
            published = json.loads(artifact_path.read_text(encoding="utf-8"))
            result = {**validate_scorer_contract(scorer.scorer_items, published["questions"], scorer_bundle_conversation_splits(scorer)), **validate_corpus_contract(scorer.scorer_items, published.get("corpora"))}
        finally:
            sys.path.pop(0)
            for module in _HISTORICAL_MODULES: sys.modules.pop(module, None)
            sys.modules.update({module: value for module, value in saved.items() if value is not None})
    _enforce_scorer_contract_digests(result, manifest.get("scorer_contract"), enforce_expected=enforce_expected)
    return result


def validate_denominators(*, conversations: int, sessions: int, dialogs: int, questions: int, hard_questions: int, evidence_bearing: int) -> None:
    if (conversations, sessions, dialogs, questions, hard_questions, evidence_bearing) != (10, 272, 5882, 1986, 603, 1982):
        raise ValueError("frozen LoCoMo denominator mismatch")


def paired_conversation_bootstrap(rows: list[dict[str, Any]], product: str, control: str, *, hard_only: bool, seed: int, resamples: int, hard_categories: frozenset[int] = frozenset({1, 2})) -> dict[str, float]:
    selected = [row for row in rows if not hard_only or row["category"] in hard_categories]
    grouped: dict[str, list[float]] = {}
    for row in selected:
        grouped.setdefault(row["conversation_id"], []).append(float(row[product]) - float(row[control]))
    if not grouped or resamples <= 0:
        raise ValueError("paired bootstrap needs observations and positive resamples")
    means = {key: math.fsum(values) / len(values) for key, values in grouped.items()}
    keys = sorted(means)
    rng = random.Random(seed)
    samples = sorted(math.fsum(means[rng.choice(keys)] for _ in keys) / len(keys) for _ in range(resamples))
    def percentile(q: float) -> float:
        position = q * (len(samples) - 1); low = math.floor(position); high = math.ceil(position)
        return samples[low] + (samples[high] - samples[low]) * (position - low)
    return {"estimate": math.fsum(means.values()) / len(means), "lower_95": percentile(.025), "upper_95": percentile(.975), "replicate_sha256": _canonical(samples)}


def _macro(rows: list[dict[str, Any]], arm: str, hard: bool, hard_categories: frozenset[int] = frozenset({1, 2})) -> float:
    values = [float(row[arm]) for row in rows if not hard or row["category"] in hard_categories]
    if not values:
        raise ValueError("metric subset has no questions")
    return math.fsum(values) / len(values)


def validate_gate_thresholds(gates: Any) -> dict[str, float]:
    if not isinstance(gates, dict):
        raise ValueError("frozen gate thresholds are missing")
    values = {name: gates.get(name) for name in ("p1_delta", "p2_delta")}
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) for value in values.values()):
        raise ValueError("frozen gate thresholds are malformed")
    if float(values["p1_delta"]) != .05 or float(values["p2_delta"]) != -.01:
        raise ValueError("frozen gate thresholds changed")
    if gates.get("hard_categories") != [1, 2]:
        raise ValueError("frozen hard categories changed")
    return {**{name: float(value) for name, value in values.items()}, "hard_categories": [1, 2]}


def _mapping_safety(maps: dict[str, dict[str, str]], *, expected_dialogs: int) -> dict[str, Any]:
    rows = [(conversation_id, event_id, dialog_id) for conversation_id, mapping in sorted(maps.items()) for event_id, dialog_id in sorted(mapping.items())]
    event_ids = [row[1] for row in rows]
    dialog_ids = [(row[0], row[2]) for row in rows]
    valid = all(isinstance(value, str) and value for row in rows for value in row[1:])
    return {"count": len(rows), "unique_event_count": len(set(event_ids)), "unique_dialog_count": len(set(dialog_ids)), "mapping_sha256": _canonical(rows), "valid": valid, "one_to_one": valid and len(rows) == expected_dialogs and len(rows) == len(set(event_ids)) == len(set(dialog_ids))}


def summarize_product_safety(*, traces: dict[str, Any], product_rankings: dict[str, list[str]], product_event_maps: dict[str, dict[str, str]], legacy_event_maps: dict[str, dict[str, str]], question_conversations: dict[str, str], lineage: list[dict[str, Any]], expected_questions: int, expected_dialogs: int) -> dict[str, Any]:
    """Turn every Product call and seed mapping into fail-closed P1 evidence."""
    audits = []; ranking_complete = 0; selected_matches = 0; unauthorized = 0; ranking_digest_count = 0; identity_complete = 0; nonempty_selection = 0
    for item_id, trace in traces.items():
        audit = aerp1.audit_product_trace(trace) if isinstance(trace, dict) else {"complete": False, "unauthorized_selected_ids": []}
        audits.append(audit); unauthorized += len(audit.get("unauthorized_selected_ids", []))
        ranking = trace.get("retrieval_ranking") if isinstance(trace, dict) else None
        conversation_id = question_conversations.get(item_id)
        mapping = product_event_maps.get(conversation_id, {})
        schema_ok = isinstance(ranking, dict) and ranking.get("schema") == "aerp2-product-six-view-v1" and RANKING_TRACE_REQUIRED <= set(ranking) and isinstance(ranking.get("encoder_identity"), str) and bool(ranking["encoder_identity"]) and ranking.get("weights") == FROZEN_SIX_VIEW_WEIGHTS and ranking.get("rrf_k") == 60
        view_digests = ranking.get("view_digests") if isinstance(ranking, dict) else None
        views_ok = isinstance(view_digests, dict) and set(view_digests) == set(FROZEN_SIX_VIEW_WEIGHTS) and all(isinstance(value, str) and len(value) == 64 for value in view_digests.values())
        selected_rows = ranking.get("selected") if isinstance(ranking, dict) else None
        selected_key_ok = isinstance(selected_rows, list) and all(isinstance(row, dict) and row.get("source_event_id") in mapping and row.get("ranking_key_sha256") == _sha256(mapping[row["source_event_id"]].encode("utf-8")) for row in selected_rows)
        digest_ok = schema_ok and views_ok and selected_key_ok and all(isinstance(ranking.get(name), str) and len(ranking[name]) == 64 for name in ("query_sha256", "input_sha256"))
        ranking_complete += int(digest_ok); ranking_digest_count += int(digest_ok)
        selected_ids = trace.get("selected_evidence_ids") if isinstance(trace, dict) else None
        authorized = trace.get("authorized_candidate_ids") if isinstance(trace, dict) else None
        candidates = trace.get("candidates") if isinstance(trace, dict) else None
        candidate_ids = [row.get("source_event_id") for row in candidates] if isinstance(candidates, list) and all(isinstance(row, dict) for row in candidates) else []
        selected_row_ids = [row.get("source_event_id") for row in selected_rows] if isinstance(selected_rows, list) and all(isinstance(row, dict) for row in selected_rows) else []
        identity_ok = isinstance(authorized, list) and len(authorized) == len(set(authorized)) and set(authorized) == set(mapping) and isinstance(candidates, list) and len(candidate_ids) == len(set(candidate_ids)) and set(candidate_ids) == set(mapping)
        identity_complete += int(identity_ok)
        nonempty_ok = isinstance(selected_ids, list) and bool(selected_ids) and isinstance(selected_rows, list) and bool(selected_rows) and selected_row_ids == selected_ids
        nonempty_selection += int(nonempty_ok)
        if identity_ok and nonempty_ok and all(isinstance(event_id, str) and event_id in mapping for event_id in selected_ids):
            selected_matches += int([mapping[event_id] for event_id in selected_ids] == product_rankings.get(item_id))
    legacy_mapping = _mapping_safety(legacy_event_maps, expected_dialogs=expected_dialogs)
    product_mapping = _mapping_safety(product_event_maps, expected_dialogs=expected_dialogs)
    ledgers = [row.get("seed_ledger") for row in lineage if isinstance(row, dict)]
    forbidden_ok = all(isinstance(ledger, dict) and isinstance(ledger.get("forbidden_field_counts"), dict) and set(ledger["forbidden_field_counts"]) == set(FORBIDDEN_ANNOTATION_FIELDS) and all(ledger["forbidden_field_counts"].get(field) == 0 for field in FORBIDDEN_ANNOTATION_FIELDS) for ledger in ledgers)
    forbidden_count = sum(sum(value for value in ledger.get("forbidden_field_counts", {}).values() if isinstance(value, int)) for ledger in ledgers if isinstance(ledger, dict))
    mapping_digest_count = sum(int(isinstance(ledger, dict) and isinstance(ledger.get("checkpoint_ranking_mapping_sha256"), str) and len(ledger["checkpoint_ranking_mapping_sha256"]) == 64 and isinstance(ledger.get("ranker_texts_sha256"), str) and len(ledger["ranker_texts_sha256"]) == 64) for ledger in ledgers)
    checks = {
        "trace_count": len(traces) == expected_questions,
        "audit_complete": len(audits) == expected_questions and sum(int(audit.get("complete") is True) for audit in audits) == expected_questions,
        "ranking_schema": ranking_complete == expected_questions,
        "selected_matches_product_output": selected_matches == expected_questions,
        "unauthorized_selected": unauthorized == 0,
        "legacy_mapping_1to1": legacy_mapping["one_to_one"],
        "product_mapping_1to1": product_mapping["one_to_one"],
        "lineage_forbidden": len(ledgers) == len(product_event_maps) and forbidden_ok,
        "lineage_mapping_digest": len(ledgers) == len(product_event_maps) and mapping_digest_count == len(ledgers),
        "ranking_key_mapping_digest": ranking_digest_count == expected_questions and len(product_mapping["mapping_sha256"]) == 64,
        "trace_identity_sets": set(traces) == set(question_conversations) == set(product_rankings) and identity_complete == expected_questions,
        "nonempty_selection": nonempty_selection == expected_questions,
    }
    return {"expected_trace_count": expected_questions, "trace_count": len(traces), "audit_complete_count": sum(int(audit.get("complete") is True) for audit in audits), "ranking_schema_complete_count": ranking_complete, "selected_match_count": selected_matches, "trace_identity_complete_count": identity_complete, "nonempty_selection_count": nonempty_selection, "unauthorized_selected_count": unauthorized, "legacy_mapping": legacy_mapping, "product_mapping": product_mapping, "lineage": {"count": len(ledgers), "forbidden_field_count": forbidden_count, "mapping_digest_count": mapping_digest_count}, "ranking_digest_count": ranking_digest_count, "checks": checks, "pass": all(checks.values())}


def evaluate_release_gates(rows: list[dict[str, Any]], *, bootstrap_seed: int, bootstrap_resamples: int, thresholds: dict[str, Any], safety_summary: dict[str, Any]) -> dict[str, Any]:
    thresholds = validate_gate_thresholds(thresholds)
    hard_categories = frozenset(thresholds["hard_categories"])
    p1 = {subset: paired_conversation_bootstrap(rows, "product", "strong", hard_only=subset == "hard", seed=bootstrap_seed, resamples=bootstrap_resamples, hard_categories=hard_categories) for subset in ("overall", "hard")}
    product = {subset: _macro(rows, "product", subset == "hard", hard_categories) for subset in ("overall", "hard")}
    strong = {subset: _macro(rows, "strong", subset == "hard", hard_categories) for subset in ("overall", "hard")}
    historical = {subset: _macro(rows, "historical", subset == "hard", hard_categories) for subset in ("overall", "hard")}
    deltas = {"product_minus_strong": {subset: product[subset] - strong[subset] for subset in product}, "product_minus_historical": {subset: product[subset] - historical[subset] for subset in product}}
    checks = safety_summary.get("checks", {}) if isinstance(safety_summary, dict) else {}
    safety_reasons = [f"safety.{name}" for name in SAFETY_CHECKS if checks.get(name) is not True]
    reasons = list(safety_reasons)
    for subset in ("overall", "hard"):
        if deltas["product_minus_strong"][subset] < thresholds["p1_delta"]: reasons.append(f"p1.{subset}.delta_below_threshold")
        if p1[subset]["lower_95"] <= 0: reasons.append(f"p1.{subset}.bootstrap_lower_not_positive")
        if deltas["product_minus_historical"][subset] < thresholds["p2_delta"]: reasons.append(f"p2.{subset}.delta_below_threshold")
    p1_pass = not safety_reasons and all(deltas["product_minus_strong"][subset] >= thresholds["p1_delta"] and p1[subset]["lower_95"] > 0 for subset in ("overall", "hard"))
    p2_pass = all(deltas["product_minus_historical"][subset] >= thresholds["p2_delta"] for subset in ("overall", "hard"))
    return {"product": product, "strong": strong, "historical": historical, "deltas": deltas, "bootstrap": p1, "thresholds": thresholds, "safety": safety_summary, "p1_pass": p1_pass, "p2_pass": p2_pass, "release_pass": p1_pass and p2_pass, "failure_reasons": reasons}


def require_clean_git_state(state: dict[str, Any]) -> None:
    if state.get("git_dirty"):
        raise ValueError("quality harness requires a clean Git worktree")


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name); json.dump(value, stream, sort_keys=True, separators=(",", ":")); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path); temporary = None
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


PHASES = ("input_byte_freeze", "source_model_load", "sanitized_retrieval_construction", "fresh_streams_frozen", "artifact_parse_scorer_contract", "score_gate", "state_recheck", "atomic_publish_ready")


def advance_phase(ledger: list[str], phase: str) -> None:
    if phase not in PHASES or len(ledger) >= len(PHASES) or PHASES[len(ledger)] != phase:
        raise RuntimeError("quality phase order is invalid")
    ledger.append(phase)


def freeze_input_bytes(path: Path, *, label: str, expected_sha256: str) -> dict[str, Any]:
    data = path.read_bytes()
    digest = _sha256(data)
    if digest != expected_sha256:
        raise ValueError(f"{label} input digest mismatch")
    return {"label": label, "path": str(path.resolve()), "sha256": digest, "bytes": len(data), "data": data}


def verify_frozen_input(receipt: dict[str, Any]) -> None:
    data = Path(receipt["path"]).read_bytes()
    if len(data) != receipt["bytes"] or _sha256(data) != receipt["sha256"]:
        raise RuntimeError(f"{receipt['label']} input changed during quality run")


def require_external_output(output: Path, source_repo: Path) -> Path:
    resolved = output.resolve()
    for repository in (ROOT.resolve(), source_repo.resolve()):
        if resolved == repository or repository in resolved.parents:
            raise ValueError("output must be outside source repositories")
    return resolved


def validate_historical_source_pins(manifest: dict[str, Any], observed: dict[str, str]) -> None:
    expected = manifest.get("historical_source")
    if not isinstance(expected, dict) or expected.get("commit") != HISTORICAL_COMMIT or not isinstance(expected.get("files"), dict):
        raise ValueError("historical source manifest pin is malformed")
    for filename, digest in expected["files"].items():
        actual = observed.get(filename, observed.get(f"benchmarks/{filename}"))
        if actual != digest:
            raise ValueError(f"historical source pin mismatch: {filename}")


def source_repo_receipt(source_repo: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    root = source_repo.resolve()
    try:
        aerp1._git(root, "cat-file", "-e", f"{HISTORICAL_COMMIT}^{{commit}}")
    except Exception as exc:
        raise ValueError("pinned historical source commit is unavailable") from exc
    return {"path": str(root), "pinned_commit": HISTORICAL_COMMIT, "git_state": aerp1.git_state(root)}


def adapter_implementation_digest() -> str:
    return _sha256(inspect.getsource(HistoricalBgeAdapter).encode("utf-8"))


def encoder_snapshot_receipt(encoder: Any) -> dict[str, Any]:
    pair = encoder.finish_snapshot_pair().to_dict()
    if not isinstance(pair, dict) or pair.get("start") != pair.get("end"):
        raise RuntimeError("historical encoder model snapshot drifted")
    return pair


def model_runtime_receipt(snapshot_pair: dict[str, Any], sentinels: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    start = snapshot_pair.get("start") if isinstance(snapshot_pair, dict) else None
    files = start.get("files") if isinstance(start, dict) else None
    if not isinstance(files, list):
        raise RuntimeError("model snapshot files are unavailable")
    onnx = [row for row in files if isinstance(row, dict) and row.get("relative_path") == "model.onnx"]
    query = sentinels.get("query") if isinstance(sentinels, dict) else None
    if len(onnx) != 1 or onnx[0].get("sha256") != inputs.get("model_onnx_sha256") or not isinstance(query, dict) or tuple(query.get("shape", ())) != (1, inputs.get("embedding_dimension")):
        raise RuntimeError("model ONNX identity or embedding dimension mismatch")
    return {"onnx_sha256": onnx[0]["sha256"], "embedding_dimension": inputs["embedding_dimension"]}


def encoder_runtime_provider_receipt(encoder: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    """Strict local probe of the exact historical encoder session runtime."""
    expected = inputs.get("session_providers")
    session = getattr(encoder, "_session", None)
    get_providers = getattr(session, "get_providers", None)
    if not isinstance(expected, list) or not expected or not callable(get_providers):
        raise RuntimeError("historical encoder provider contract is unavailable")
    providers = get_providers()
    if not isinstance(providers, (list, tuple)) or list(providers) != expected:
        raise RuntimeError("historical encoder session providers do not match manifest")
    try:
        import onnxruntime
        version = onnxruntime.__version__
    except (ImportError, AttributeError) as exc:
        raise RuntimeError("onnxruntime version is unavailable") from exc
    if not isinstance(version, str) or not version:
        raise RuntimeError("onnxruntime version is malformed")
    return {"session_providers": list(providers), "onnxruntime_version": version}


class HistoricalBgeAdapter:
    """Thin product adapter over the byte-pinned historical BGE encoder."""
    def __init__(self, encoder: Any, identity: str) -> None: self._encoder, self.identity = encoder, identity
    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, row)) for row in self._encoder.encode_passages(list(texts))]
    def encode_query(self, query: str) -> list[float]:
        return list(map(float, self._encoder.encode_queries([query])[0]))


_HISTORICAL_MODULES = ("locomo_story_protocol", "locomo_story_candidate", "locomo_bge_encoder")


def _historical_modules(source_repo: Path, model_dir: Path, manifest: dict[str, Any]) -> tuple[Any, Any, Any, dict[str, str], tempfile.TemporaryDirectory[str], dict[str, Any]]:
    directory = tempfile.TemporaryDirectory(prefix="aerp2-product-pinned-")
    root = Path(directory.name)
    digests = _extract_historical_source(source_repo.resolve(), root)
    validate_historical_source_pins(manifest, digests)
    saved = {name: sys.modules.pop(name, None) for name in _HISTORICAL_MODULES}
    sys.path.insert(0, str(root / "benchmarks"))
    try:
        import importlib
        protocol = importlib.import_module("locomo_story_protocol")
        candidate = importlib.import_module("locomo_story_candidate")
        bge = importlib.import_module("locomo_bge_encoder")
        encoder = bge.load_bge_encoder(model_dir, variant="fp32")
        if encoder.manifest.canonical_sha256 != manifest["inputs"]["model_manifest_sha256"]:
            raise ValueError("pinned BGE manifest mismatch")
        return protocol, candidate, encoder, digests, directory, saved
    except Exception:
        sys.path.pop(0)
        for name in _HISTORICAL_MODULES: sys.modules.pop(name, None)
        sys.modules.update({name: module for name, module in saved.items() if module is not None})
        directory.cleanup(); raise


def extract_historical_ranking(rows: Any, corpus: set[str], pool: int = 50) -> list[str]:
    if not isinstance(rows, list) or len(rows) < pool: raise ValueError("historical ranking source pool is incomplete")
    output: list[str] = []; previous = math.inf
    for row in rows[:pool]:
        if not isinstance(row, dict) or not isinstance(row.get("opaque_dialog_id"), str) or not isinstance(row.get("score"), (int, float)):
            raise ValueError("historical ranking row is malformed")
        if not math.isfinite(float(row["score"])):
            raise ValueError("historical ranking score is non-finite")
        if float(row["score"]) > previous or row["opaque_dialog_id"] not in corpus or row["opaque_dialog_id"] in output: raise ValueError("historical ranking is not monotone unique in-corpus")
        previous = float(row["score"]); output.append(row["opaque_dialog_id"])
    return output


def _rrf(left: list[str], right: list[str], *, k: int = 60) -> list[str]:
    score: dict[str, float] = {}
    for weight, ranking in ((2.0, left), (1.0, right)):
        for rank, item in enumerate(ranking, 1): score[item] = score.get(item, 0.0) + weight / (k + rank)
    return [item for item, _ in sorted(score.items(), key=lambda pair: (-pair[1], pair[0]))]


def _raw_bm25_pool(candidate: Any, payload: dict[str, Any], pool: int) -> list[str]:
    dialogs = aerp1.raw_dialogs(payload)
    rows = candidate._rank_bm25(tuple(row["id"] for row in dialogs), tuple(row["text"] for row in dialogs), candidate._tokenize(payload["query"]), candidate.StoryCandidateConfig())
    return [row[0] for row in rows[:pool]]


def validate_source_pool(ranking: list[str], *, pool: int, name: str) -> None:
    if len(ranking) != pool or len(ranking) != len(set(ranking)) or any(not isinstance(value, str) or not value for value in ranking):
        raise RuntimeError(f"{name} source-pool stream is malformed")


def validate_official_aggregate_anchors(aggregate: dict[str, Any], anchors: dict[str, Any]) -> None:
    for arm, label in (("raw_bm25", "raw BM25 aggregate R0"), ("historical_six_view", "historical SixView aggregate")):
        try:
            metrics = aggregate[arm]["official_exact"]
            expected = anchors[arm]
            overall = float(metrics["overall"]["question_macro_recall_at_10"])
            hard = float(metrics["hard_categories_1_2"]["question_macro_recall_at_10"])
            expected_overall, expected_hard = float(expected["overall"]), float(expected["hard"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"{label} anchor contract is malformed") from exc
        if not all(math.isfinite(value) for value in (overall, hard, expected_overall, expected_hard)):
            raise RuntimeError(f"{label} anchor contract is non-finite")
        if not math.isclose(overall, expected_overall, rel_tol=0.0, abs_tol=1e-15) or not math.isclose(hard, expected_hard, rel_tol=0.0, abs_tol=1e-15):
            raise RuntimeError(f"{label} anchor mismatch")


def _product_rank(kernel: RpgMemoryKernel, conversation_id: str, query: str, event_to_dialog: dict[str, str], top_k: int) -> tuple[list[str], dict[str, Any]]:
    decision = kernel.authorized_evidence(campaign_id=conversation_id, actor_id="locomo_reader", actor_type="npc", query=query, active_quest_ids=[], budget=1000, _compact_product_trace=True)
    evidence = kernel._retrieve_memory_items(campaign_id=conversation_id, actor_id="locomo_reader", actor_type="npc", query=query, active_quest_ids=[], location_id=None, hit_limit=top_k, max_chars=10_000_000, authorized_event_ids=set(decision.trace["authorized_candidate_ids"]), ranking_trace=decision.trace)
    selected = [str(item["source_event_id"]) for item in evidence]
    decision.trace["selected_evidence_ids"] = selected
    kernel._complete_product_trace(decision, campaign_id=conversation_id, actor_id="locomo_reader", actor_type="npc")
    ranking = decision.trace.get("retrieval_ranking")
    required = {"schema", "query_sha256", "input_sha256", "view_digests", "encoder_identity", "weights", "rrf_k", "selected"}
    if not isinstance(ranking, dict) or ranking.get("schema") != "aerp2-product-six-view-v1" or not required <= set(ranking) or [row.get("source_event_id") for row in ranking["selected"]] != selected: raise RuntimeError("product ranking trace is incomplete")
    audit = aerp1.audit_product_trace(decision.trace)
    if not audit["complete"] or len(selected) != len(set(selected)) or not set(selected) <= set(decision.trace["authorized_candidate_ids"]): raise RuntimeError("product ACL trace is incomplete")
    return [event_to_dialog[item] for item in selected], decision.trace


def build_question_audits(*, question_rows: list[dict[str, Any]], scorer: Any, rankings: dict[str, dict[str, list[str]]], source_pool_rankings: dict[str, dict[str, list[str]]], traces: dict[str, Any], product_event_maps: dict[str, dict[str, str]], lineage_by_conversation: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Bind each scored question to frozen streams and non-transcript trace receipts."""
    audits: list[dict[str, Any]] = []
    for row in question_rows:
        item_id, conversation_id = row.get("item_id"), row.get("conversation_id")
        item = scorer.scorer_items.get(item_id)
        trace = traces.get(item_id)
        mapping = product_event_maps.get(conversation_id)
        ledger = lineage_by_conversation.get(conversation_id, {}).get("seed_ledger")
        ranking = trace.get("retrieval_ranking") if isinstance(trace, dict) else None
        if not isinstance(item_id, str) or item is None or item.opaque_conversation_id != conversation_id or not isinstance(trace, dict) or not isinstance(mapping, dict) or not isinstance(ledger, dict) or not isinstance(ranking, dict):
            raise RuntimeError("per-question audit inputs are incomplete")
        selected = trace.get("selected_evidence_ids")
        authorized = trace.get("authorized_candidate_ids")
        if not isinstance(selected, list) or not isinstance(authorized, list) or any(event_id not in mapping for event_id in selected):
            raise RuntimeError("per-question trace mapping is malformed")
        if set(rankings) != set(ARMS) or any(item_id not in rankings[arm] for arm in ARMS):
            raise RuntimeError("per-question frozen top10 is incomplete")
        source_digests = {arm: {"count": len(source_pool_rankings[arm].get(item_id, [])), "sha256": _canonical(source_pool_rankings[arm].get(item_id))} for arm in source_pool_rankings}
        if any(value["count"] != 50 for value in source_digests.values()):
            raise RuntimeError("per-question source-pool audit is incomplete")
        audit = {
            "composite_id": [conversation_id, item_id], "query_sha256": ranking.get("query_sha256"),
            "corpus": {"count": len(item.corpus_opaque_dialog_ids), "sha256": _canonical(list(item.corpus_opaque_dialog_ids))},
            "checkpoint_ranking_mapping_sha256": ledger.get("checkpoint_ranking_mapping_sha256"),
            "event_dialog_mapping_sha256": _mapping_safety({conversation_id: mapping}, expected_dialogs=len(mapping))["mapping_sha256"],
            "authorized_candidates": {"count": len(authorized), "sha256": _canonical(sorted(str(value) for value in authorized))},
            "product_retrieval": {name: ranking.get(name) for name in ("schema", "query_sha256", "input_sha256", "view_digests", "encoder_identity", "weights", "rrf_k")},
            "trace_audit": aerp1.audit_product_trace(trace), "top10": {arm: rankings[arm][item_id] for arm in ARMS},
            "source_pool": source_digests, "scored_metrics": row["columns"],
        }
        if not isinstance(audit["checkpoint_ranking_mapping_sha256"], str) or len(audit["checkpoint_ranking_mapping_sha256"]) != 64:
            raise RuntimeError("per-question checkpoint mapping digest is missing")
        audits.append(audit)
    if len(audits) != len(question_rows) or len({tuple(audit["composite_id"]) for audit in audits}) != len(audits):
        raise RuntimeError("per-question audit identities are not complete")
    return audits


def split_diagnostics(question_rows: list[dict[str, Any]], splits: dict[str, str]) -> dict[str, Any]:
    """Supplemental public-split metrics; never used in release gates."""
    result: dict[str, Any] = {}
    for semantics in ("official_exact", "normalized_repaired"):
        result[semantics] = {}
        for arm in ARMS:
            values: dict[str, list[float]] = {"dev_overall": [], "eval_overall": [], "eval_hard": []}
            for row in question_rows:
                split = splits.get(row["conversation_id"])
                metric = row["columns"][arm][semantics]
                if metric["scored"] and split in {"dev", "eval"}:
                    values[f"{split}_overall"].append(float(metric["recall_at_10"]))
                    if split == "eval" and row["category"] in {1, 2}:
                        values["eval_hard"].append(float(metric["recall_at_10"]))
            result[semantics][arm] = {name: (math.fsum(items) / len(items) if items else None) for name, items in values.items()}
    return result


def validate_report_shape(report: dict[str, Any]) -> None:
    required = {"manifest_sha256", "input_freeze", "model_runtime", "git_state_before", "git_state_after", "source_repo", "historical_source", "encoder_identity", "adapter_implementation_sha256", "encoder_sentinels", "encoder_snapshot_pair", "phase_ledger", "event_dialog_mapping_sha256", "question_audits", "safety_summary", "aggregate", "gates"}
    if not isinstance(report, dict) or report.get("schema") != "aerp2-product-six-view-locomo" or not required <= set(report):
        raise RuntimeError("quality report provenance shape is incomplete")
    expected_questions = report.get("safety_summary", {}).get("expected_trace_count") if isinstance(report.get("safety_summary"), dict) else None
    if report["phase_ledger"] != list(PHASES) or not isinstance(expected_questions, int) or len(report["question_audits"]) != expected_questions:
        raise RuntimeError("quality report audit shape is incomplete")


def run_quality(*, dataset_path: Path, artifact_path: Path, model_dir: Path, source_repo: Path, output: Path, manifest_path: Path = MANIFEST_PATH) -> dict[str, Any]:
    """Execute all six frozen arms; scorer labels are accessed only after streams freeze."""
    manifest, manifest_sha = load_manifest(manifest_path)
    state_before = aerp1.git_state(ROOT); require_clean_git_state(state_before)
    output = require_external_output(output, source_repo)
    phases: list[str] = []
    dataset_receipt = freeze_input_bytes(dataset_path, label="dataset", expected_sha256=manifest["inputs"]["dataset_sha256"])
    artifact_receipt = freeze_input_bytes(artifact_path, label="artifact", expected_sha256=manifest["inputs"]["historical_artifact_sha256"])
    artifact_bytes = artifact_receipt["data"]  # Parse only after fresh streams freeze.
    advance_phase(phases, "input_byte_freeze")
    source_receipt = source_repo_receipt(source_repo, manifest)
    protocol, candidate, encoder, source_digests, directory, saved_modules = _historical_modules(source_repo, model_dir, manifest)
    try:
        adapter_digest = adapter_implementation_digest()
        runtime_provider = encoder_runtime_provider_receipt(encoder, manifest["inputs"])
        identity = _canonical({"model": encoder.manifest.canonical_sha256, "historical_encoder": source_digests["benchmarks/locomo_bge_encoder.py"], "adapter": adapter_digest, "runtime_provider": runtime_provider})
        encoder_sentinels = {"query": encoder.sentinel_embedding_hash(["aerp2 product query sentinel"], mode="query").to_dict(), "passage": encoder.sentinel_embedding_hash(["aerp2 product passage sentinel"], mode="passage").to_dict()}
        advance_phase(phases, "source_model_load")
        dataset = protocol.load_official_locomo10(dataset_path)
        retrieval, scorer = protocol.prepare_hard_story_track(dataset, candidate_pool_size=manifest["protocol"]["source_pool"], require_official_counts=True)
        advance_phase(phases, "sanitized_retrieval_construction")
        item_ids = aerp1._conversation_items(retrieval)
        if len(item_ids) != 10: raise ValueError("conversation denominator mismatch")
        session_count = sum(len(retrieval.retrieval_items[ids[0]]["sessions"]) for ids in item_ids.values())
        dialog_count = sum(len(dialog["dialogs"]) for ids in item_ids.values() for dialog in retrieval.retrieval_items[ids[0]]["sessions"])
        rankings = {arm: {} for arm in ARMS[:-1]}; source_pool_rankings = {"raw_bm25": {}, "raw_dense": {}, "raw_bm25_plus_raw_dense": {}}; traces: dict[str, Any] = {}; lineage: list[dict[str, Any]] = []; lineage_by_conversation: dict[str, dict[str, Any]] = {}; legacy_event_maps: dict[str, dict[str, str]] = {}; product_event_maps: dict[str, dict[str, str]] = {}; question_conversations: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="aerp2-product-kernel-") as temp:
            legacy = RpgMemoryKernel(db_path=str(Path(temp) / "legacy.sqlite")); product = RpgMemoryKernel(db_path=str(Path(temp) / "product.sqlite"), retrieval_ranker=SixViewRanker(HistoricalBgeAdapter(encoder, identity)))
            try:
                for conversation_id, ids in sorted(item_ids.items()):
                    payload0 = retrieval.retrieval_items[ids[0]]; legacy_map, audit = seed_sanitized_conversation(legacy, payload0, conversation_id=conversation_id); product_map, product_audit = seed_sanitized_conversation(product, payload0, conversation_id=conversation_id)
                    if audit["seed_ledger"] != product_audit["seed_ledger"]: raise RuntimeError("legacy/product seed lineage differs")
                    audit = {"conversation_id": conversation_id, **audit}; lineage.append(audit); lineage_by_conversation[conversation_id] = audit; legacy_event_maps[conversation_id] = legacy_map; product_event_maps[conversation_id] = product_map
                    dialogs = aerp1.raw_dialogs(payload0); dialog_ids = [row["id"] for row in dialogs]; passages = encoder.encode_passages([row["text"] for row in dialogs]); queries = encoder.encode_queries([retrieval.retrieval_items[item]["query"] for item in ids])
                    for index, item_id in enumerate(ids):
                        question_conversations[item_id] = conversation_id
                        payload = retrieval.retrieval_items[item_id]; raw_pool = _raw_bm25_pool(candidate, payload, manifest["protocol"]["source_pool"]); dense_pool = aerp1.rank_vectors(dialog_ids, passages, queries[index], manifest["protocol"]["source_pool"]); fused_pool = _rrf(raw_pool, dense_pool)[:manifest["protocol"]["source_pool"]]
                        validate_source_pool(raw_pool, pool=manifest["protocol"]["source_pool"], name="raw BM25"); validate_source_pool(dense_pool, pool=manifest["protocol"]["source_pool"], name="raw dense"); validate_source_pool(fused_pool, pool=manifest["protocol"]["source_pool"], name="raw BM25+dense RRF")
                        source_pool_rankings["raw_bm25"][item_id] = raw_pool; source_pool_rankings["raw_dense"][item_id] = dense_pool; source_pool_rankings["raw_bm25_plus_raw_dense"][item_id] = fused_pool
                        rankings["raw_bm25"][item_id] = raw_pool[:10]; rankings["raw_dense"][item_id] = dense_pool[:10]; rankings["raw_bm25_plus_raw_dense"][item_id] = fused_pool[:10]
                        rankings["legacy_rpg"][item_id] = aerp1.rank_rpg(legacy, conversation_id=conversation_id, query=payload["query"], event_to_dialog=legacy_map, top_k=10)[0]
                        rankings["product_six_view"][item_id], traces[item_id] = _product_rank(product, conversation_id, payload["query"], product_map, 10)
            finally: legacy.close(); product.close()
        # Ranking freeze ends here.  Only now may scorer labels and historical reference be joined.
        encoder_snapshots = encoder_snapshot_receipt(encoder)
        model_runtime = model_runtime_receipt(encoder_snapshots, encoder_sentinels, manifest["inputs"])
        advance_phase(phases, "fresh_streams_frozen")
        published = json.loads(artifact_bytes)
        historical_by_key = {(row["opaque_conversation_id"], row["opaque_question_id"]): row for row in published["questions"]}
        scorer_splits = scorer_bundle_conversation_splits(scorer)
        scorer_digests = {**validate_scorer_contract(scorer.scorer_items, published["questions"], scorer_splits), **validate_corpus_contract(scorer.scorer_items, published.get("corpora"))}
        _enforce_scorer_contract_digests(scorer_digests, manifest.get("scorer_contract"), enforce_expected=True)
        advance_phase(phases, "artifact_parse_scorer_contract")
        key_to_item = {(scorer.scorer_items[item].opaque_conversation_id, item): item for item in scorer.scorer_items}
        if set(key_to_item) != set(historical_by_key): raise ValueError("historical scorer join identities differ")
        rankings["historical_six_view"] = {item: extract_historical_ranking(historical_by_key[key]["methods"]["six_view_story_dense_rrf_v2"]["ranking"], set(scorer.scorer_items[item].corpus_opaque_dialog_ids))[:10] for key, item in key_to_item.items()}
        expected_raw = {key: extract_historical_ranking(historical_by_key[key]["methods"]["raw_dialog_bm25"]["ranking"], set(scorer.scorer_items[item].corpus_opaque_dialog_ids), manifest["protocol"]["source_pool"]) for key, item in key_to_item.items()}
        actual_raw = {key: source_pool_rankings["raw_bm25"][item] for key, item in key_to_item.items()}
        validate_r0_raw_bm25(expected_raw, actual_raw)
        aggregate, question_rows = aerp1.score_rankings(scorer, rankings, 10)
        validate_denominators(conversations=len(item_ids), sessions=session_count, dialogs=dialog_count, questions=len(question_rows), hard_questions=sum(row["category"] in {1, 2} for row in question_rows), evidence_bearing=sum(row["columns"]["raw_bm25"]["official_exact"]["scored"] for row in question_rows))
        validate_official_aggregate_anchors(aggregate, manifest["anchors"])
        compact = [{"conversation_id": row["conversation_id"], "category": row["category"], "product": row["columns"]["product_six_view"]["official_exact"]["recall_at_10"], "strong": row["columns"]["raw_bm25_plus_raw_dense"]["official_exact"]["recall_at_10"], "historical": row["columns"]["historical_six_view"]["official_exact"]["recall_at_10"]} for row in question_rows if row["columns"]["product_six_view"]["official_exact"]["scored"]]
        safety = summarize_product_safety(traces=traces, product_rankings=rankings["product_six_view"], product_event_maps=product_event_maps, legacy_event_maps=legacy_event_maps, question_conversations=question_conversations, lineage=lineage, expected_questions=manifest["protocol"]["questions"], expected_dialogs=manifest["protocol"]["dialogs"])
        gates = evaluate_release_gates(compact, bootstrap_seed=manifest["protocol"]["bootstrap_seed"], bootstrap_resamples=manifest["protocol"]["bootstrap_resamples"], thresholds=manifest["gates"], safety_summary=safety)
        question_audits = build_question_audits(question_rows=question_rows, scorer=scorer, rankings=rankings, source_pool_rankings=source_pool_rankings, traces=traces, product_event_maps=product_event_maps, lineage_by_conversation=lineage_by_conversation)
        diagnostics = split_diagnostics(question_rows, scorer_splits)
        advance_phase(phases, "score_gate")
        if not safety["pass"]: raise RuntimeError("product safety/trace summary failed")
        verify_frozen_input(dataset_receipt); verify_frozen_input(artifact_receipt)
        state_after = aerp1.git_state(ROOT)
        if not aerp1.same_git_state(state_before, state_after): raise RuntimeError("worktree changed during quality run")
        advance_phase(phases, "state_recheck")
        advance_phase(phases, "atomic_publish_ready")
        report = {"schema": "aerp2-product-six-view-locomo", "status": "complete", "manifest_sha256": manifest_sha, "input_freeze": {"dataset": {key: value for key, value in dataset_receipt.items() if key != "data"}, "artifact": {key: value for key, value in artifact_receipt.items() if key != "data"}, "model_manifest_sha256": encoder.manifest.canonical_sha256, **scorer_digests}, "model_runtime": {**model_runtime, **runtime_provider}, "git_state_before": state_before, "git_state_after": state_after, "source_repo": source_receipt, "historical_source": {"commit": HISTORICAL_COMMIT, "files": source_digests}, "encoder_identity": identity, "adapter_implementation_sha256": adapter_digest, "encoder_sentinels": encoder_sentinels, "encoder_snapshot_pair": encoder_snapshots, "phase_ledger": phases, "annotation_lineage": lineage, "event_dialog_mapping_sha256": {"legacy": _mapping_safety(legacy_event_maps, expected_dialogs=manifest["protocol"]["dialogs"])["mapping_sha256"], "product": _mapping_safety(product_event_maps, expected_dialogs=manifest["protocol"]["dialogs"])["mapping_sha256"]}, "ranking_stream_sha256": {arm: _canonical(rankings[arm]) for arm in rankings}, "source_pool_stream_sha256": {arm: _canonical(source_pool_rankings[arm]) for arm in source_pool_rankings}, "rankings_top10": rankings, "source_pool_rankings": source_pool_rankings, "product_traces": traces, "safety_summary": safety, "question_audits": question_audits, "questions": question_rows, "aggregate": aggregate, "split_diagnostics_non_gating": diagnostics, "gates": gates, "claim_boundary": "public/non-blind engineering regression; QA-annotation-free; caption is upstream metadata, not hidden-set generalization"}
        validate_report_shape(report)
        atomic_json(output, report); return report
    finally:
        sys.path.pop(0)
        for name in _HISTORICAL_MODULES: sys.modules.pop(name, None)
        sys.modules.update({name: module for name, module in saved_modules.items() if module is not None})
        directory.cleanup()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True); parser.add_argument("--artifact", required=True); parser.add_argument("--model-dir"); parser.add_argument("--source-repo", required=True); parser.add_argument("--output"); parser.add_argument("--manifest", default=str(MANIFEST_PATH)); parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args(argv)
    if args.metadata_only:
        print(json.dumps(run_metadata_validation(dataset_path=Path(args.dataset), artifact_path=Path(args.artifact), source_repo=Path(args.source_repo), manifest_path=Path(args.manifest)), sort_keys=True))
        return 0
    if not args.model_dir or not args.output:
        parser.error("--model-dir and --output are required unless --metadata-only is used")
    output = Path(args.output).resolve()
    if ROOT in output.parents or output == ROOT: raise ValueError("output must be outside repository")
    report = run_quality(dataset_path=Path(args.dataset), artifact_path=Path(args.artifact), model_dir=Path(args.model_dir), source_repo=Path(args.source_repo), output=output, manifest_path=Path(args.manifest))
    print(json.dumps({"status": report["status"], "gates": report["gates"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
