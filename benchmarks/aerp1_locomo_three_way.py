"""Frozen same-protocol LoCoMo comparison for AERP-1 and controlled baselines."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mempalace_rpg import NullEpisodeAdapter, RpgMemoryKernel, SceneEventInput  # noqa: E402

MANIFEST_PATH = ROOT / "tests" / "fixtures" / "aerp1_locomo_three_way_manifest.json"
EXPECTED_MANIFEST_SHA256 = "e551ba6e3c71fa6f5584e700e31fc2f3fde98beab81b8b2985e8726880590822"
TRACE_REQUIRED = {
    "policy", "campaign_id", "actor_id", "actor_type", "candidate_generation",
    "candidates", "deduplication", "authorized_candidate_ids",
    "selected_evidence_ids", "returned_spans",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest() -> tuple[dict[str, Any], str]:
    digest = _sha256(MANIFEST_PATH)
    if digest != EXPECTED_MANIFEST_SHA256:
        raise ValueError("three-way manifest SHA256 mismatch")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest["protocol"]["top_k"] != manifest["protocol"]["candidate_pool_size"]:
        raise ValueError("three-way protocol requires one common top-k")
    return manifest, digest


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", f"safe.directory={root.as_posix()}", *args],
        cwd=root, capture_output=True, check=True,
    ).stdout


def git_state(root: Path) -> dict[str, Any]:
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    tree = _git(root, "rev-parse", "HEAD^{tree}").decode("ascii").strip()
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    patch = _git(
        root, "diff-tree", "--root", "--no-commit-id", "--binary", "--full-index",
        "--no-ext-diff", "--no-color", "-p", "HEAD", "--",
    )
    return {
        "git_head": head,
        "git_tree": tree,
        "git_dirty": bool(status),
        "worktree_status_sha256": hashlib.sha256(status).hexdigest(),
        "commit_diff_sha256": hashlib.sha256(patch).hexdigest(),
        "commit_diff_bytes": len(patch),
    }


def same_git_state(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """Compare every Git identity field while allowing provenance added to ``before``."""
    return all(before.get(key) == value for key, value in after.items())


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(f"_aerp1_pin_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load pinned module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_original_modules(original_root: Path, manifest: dict[str, Any]) -> tuple[Any, Any, Any, dict[str, Any]]:
    original_root = original_root.resolve()
    state = git_state(original_root)
    expected = manifest["original_mempalace"]
    if state["git_head"] != expected["commit"] or state["git_dirty"]:
        raise ValueError("original MemPalace root must be clean at the pinned commit")
    snapshots = {}
    for relative, digest in expected["source_files"].items():
        path = original_root / relative
        observed = _sha256(path)
        if observed != digest:
            raise ValueError(f"pinned original source digest mismatch: {relative}")
        snapshots[relative] = observed
    anchor = expected["raw_bm25_control_anchor"]
    anchor_path = original_root / anchor["artifact"]
    anchor_digest = _sha256(anchor_path)
    if anchor_digest != anchor["artifact_sha256"]:
        raise ValueError("pinned raw-BM25 control artifact digest mismatch")
    benchmark_root = original_root / "benchmarks"
    protocol = _load_module("locomo_story_protocol", benchmark_root / "locomo_story_protocol.py")
    candidate = _load_module("locomo_story_candidate", benchmark_root / "locomo_story_candidate.py")
    bge = _load_module("locomo_bge_encoder", benchmark_root / "locomo_bge_encoder.py")
    return protocol, candidate, bge, {
        **state,
        "source_sha256": snapshots,
        "raw_bm25_control_artifact_sha256": anchor_digest,
    }


def raw_dialogs(payload: dict[str, Any]) -> list[dict[str, str]]:
    dialogs: list[dict[str, str]] = []
    for session in payload["sessions"]:
        for dialog in session["dialogs"]:
            text = "\n".join((
                f"speaker: {dialog['speaker']}",
                f"date: {dialog['date']}",
                f"caption: {dialog['caption']}",
                f"text: {dialog['text']}",
            ))
            dialogs.append({"id": dialog["opaque_dialog_id"], "text": text})
    ids = [dialog["id"] for dialog in dialogs]
    if not dialogs or len(ids) != len(set(ids)):
        raise ValueError("sanitized conversation must contain unique dialogs")
    return dialogs


def rank_bm25(candidate: Any, payload: dict[str, Any], top_k: int) -> list[str]:
    dialogs = raw_dialogs(payload)
    query_tokens = candidate._tokenize(payload["query"])
    rows = candidate._rank_bm25(
        tuple(dialog["id"] for dialog in dialogs),
        tuple(dialog["text"] for dialog in dialogs),
        query_tokens,
        candidate.StoryCandidateConfig(),
    )
    return [dialog_id for dialog_id, _score in rows[:top_k]]


def rank_vectors(
    dialog_ids: list[str], passage_embeddings: np.ndarray, query_embedding: np.ndarray, top_k: int
) -> list[str]:
    if top_k < 1 or len(dialog_ids) != len(set(dialog_ids)):
        raise ValueError("vector ranking requires a positive top-k and unique dialog IDs")
    if (
        passage_embeddings.ndim != 2
        or passage_embeddings.shape[0] != len(dialog_ids)
        or query_embedding.ndim != 1
        or passage_embeddings.shape[1] != query_embedding.shape[0]
    ):
        raise ValueError("vector ranking inputs are not aligned")
    scores = np.asarray(passage_embeddings @ query_embedding, dtype=np.float32)
    if not np.isfinite(scores).all():
        raise ValueError("vector ranking produced a non-finite score")
    order = sorted(range(len(dialog_ids)), key=lambda index: (-float(scores[index]), dialog_ids[index]))
    return [dialog_ids[index] for index in order[:top_k]]


def seed_rpg_conversation(
    kernel: RpgMemoryKernel, conversation_id: str, payload: dict[str, Any]
) -> dict[str, str]:
    event_to_dialog: dict[str, str] = {}
    for index, dialog in enumerate(raw_dialogs(payload)):
        scene_id = f"locomo-{conversation_id}-{dialog['id']}"
        kernel.commit_scene(
            campaign_id=conversation_id,
            scene_id=scene_id,
            in_world_time=f"dialog-{index:06d}",
            transcript=dialog["text"],
            events=[SceneEventInput(
                event_type="locomo_dialog",
                summary=dialog["text"],
                branch_id="main",
                branch_status="active",
                truth_status="canonical",
                visibility="public_world",
                source_span=dialog["text"],
            )],
        )
        row = kernel._conn().execute(
            "SELECT event_id FROM scene_event WHERE scene_id=?", (scene_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError("RPG LoCoMo event was not committed")
        event_to_dialog[str(row["event_id"])] = dialog["id"]
    return event_to_dialog


def rank_rpg(
    kernel: RpgMemoryKernel,
    *,
    conversation_id: str,
    query: str,
    event_to_dialog: dict[str, str],
    top_k: int,
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
        hit_limit=top_k,
        max_chars=10_000_000,
        authorized_event_ids=set(decision.trace["authorized_candidate_ids"]),
    )
    selected = [str(item["source_event_id"]) for item in evidence]
    decision.trace["selected_evidence_ids"] = selected
    kernel._complete_product_trace(
        decision,
        campaign_id=conversation_id,
        actor_id="locomo_reader",
        actor_type="npc",
    )
    decision.trace["returned_spans"] = []
    if not TRACE_REQUIRED <= set(decision.trace) or not set(selected) <= set(decision.trace["authorized_candidate_ids"]):
        raise RuntimeError("latest RPG product trace is incomplete or unauthorized")
    try:
        ranked = [event_to_dialog[event_id] for event_id in selected]
    except KeyError as exc:
        raise RuntimeError("RPG ranker returned an unmapped event") from exc
    return ranked, decision.trace


def _conversation_items(retrieval: Any) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item_id, conversation_id in retrieval.item_to_conversation.items():
        grouped.setdefault(conversation_id, []).append(item_id)
    return {key: sorted(value) for key, value in sorted(grouped.items())}


def audit_product_trace(trace: dict[str, Any]) -> dict[str, Any]:
    """Verify that one compact trace proves a complete, disjoint ACL partition."""
    missing_keys = sorted(TRACE_REQUIRED - set(trace))
    authorized = [str(value) for value in trace.get("authorized_candidate_ids", [])]
    selected = [str(value) for value in trace.get("selected_evidence_ids", [])]
    partitions = trace.get("denied_partitions", [])
    denied = [
        str(event_id)
        for partition in partitions
        if isinstance(partition, dict)
        for event_id in partition.get("event_ids", [])
    ]
    candidate_count = trace.get("candidate_generation", {}).get("candidate_count")
    detailed_by_id = {
        str(candidate.get("source_event_id")): candidate
        for candidate in trace.get("candidates", [])
        if isinstance(candidate, dict) and candidate.get("source_event_id") is not None
    }
    unauthorized = sorted(set(selected) - set(authorized))
    selected_detail_invalid = sorted(
        event_id
        for event_id in set(selected)
        if detailed_by_id.get(event_id, {}).get("decision") != "allow"
    )
    checks = {
        "required_keys": not missing_keys,
        "authorized_unique": len(authorized) == len(set(authorized)),
        "selected_unique": len(selected) == len(set(selected)),
        "denied_unique": len(denied) == len(set(denied)),
        "authorized_denied_disjoint": not (set(authorized) & set(denied)),
        "candidate_partition_complete": (
            isinstance(candidate_count, int)
            and candidate_count == len(authorized) + len(denied)
        ),
        "selected_authorized": not unauthorized,
        "selected_allow_detail": not selected_detail_invalid,
        "no_returned_spans": trace.get("returned_spans") == [],
    }
    return {
        "complete": all(checks.values()),
        "checks": checks,
        "missing_keys": missing_keys,
        "unauthorized_selected_ids": unauthorized,
        "selected_detail_invalid_ids": selected_detail_invalid,
        "candidate_count": candidate_count,
        "authorized_count": len(authorized),
        "denied_count": len(denied),
        "selected_count": len(selected),
    }


def produce_rankings(
    retrieval: Any,
    *,
    candidate: Any,
    encoder: Any,
    top_k: int,
    db_path: str,
) -> tuple[dict[str, dict[str, list[str]]], dict[str, dict[str, Any]], dict[str, float]]:
    columns = (
        "raw_dialog_bm25",
        "pinned_original_mempalace_raw_vector_dialog",
        "latest_rpg_authorized_ranker",
    )
    rankings = {column: {} for column in columns}
    traces: dict[str, dict[str, Any]] = {}
    elapsed = {column: 0.0 for column in columns}
    with RpgMemoryKernel(db_path=db_path, episode_adapter=NullEpisodeAdapter()) as kernel:
        kernel.upsert_character_profile(
            character_id="locomo_reader", display_name="LoCoMo reader", tier="core",
            short_persona="Evaluation reader", memory_wing="wing_locomo_reader",
        )
        for conversation_id, item_ids in _conversation_items(retrieval).items():
            first_payload = retrieval.retrieval_items[item_ids[0]]
            dialogs = raw_dialogs(first_payload)
            event_map = seed_rpg_conversation(kernel, conversation_id, first_payload)
            passage_started = time.perf_counter()
            passage_vectors = encoder.encode_passages([dialog["text"] for dialog in dialogs])
            query_vectors = encoder.encode_queries([
                retrieval.retrieval_items[item_id]["query"] for item_id in item_ids
            ])
            elapsed["pinned_original_mempalace_raw_vector_dialog"] += time.perf_counter() - passage_started
            ids = [dialog["id"] for dialog in dialogs]
            for query_index, item_id in enumerate(item_ids):
                payload = retrieval.retrieval_items[item_id]
                started = time.perf_counter()
                rankings["raw_dialog_bm25"][item_id] = rank_bm25(candidate, payload, top_k)
                elapsed["raw_dialog_bm25"] += time.perf_counter() - started
                started = time.perf_counter()
                rankings["pinned_original_mempalace_raw_vector_dialog"][item_id] = rank_vectors(
                    ids, passage_vectors, query_vectors[query_index], top_k
                )
                elapsed["pinned_original_mempalace_raw_vector_dialog"] += time.perf_counter() - started
                started = time.perf_counter()
                ranked, trace = rank_rpg(
                    kernel, conversation_id=conversation_id, query=payload["query"],
                    event_to_dialog=event_map, top_k=top_k,
                )
                rankings["latest_rpg_authorized_ranker"][item_id] = ranked
                traces[item_id] = trace
                elapsed["latest_rpg_authorized_ranker"] += time.perf_counter() - started
    return rankings, traces, elapsed


def question_metrics(
    ranked_ids: list[str],
    resolved_gold_ids: Iterable[str],
    *,
    evidence_item_count: int,
    unresolved_evidence_item_count: int,
    top_k: int,
) -> dict[str, Any]:
    """Match the pinned MemPalace experiment's item-multiplicity scorer at one k."""
    resolved = tuple(str(value) for value in resolved_gold_ids)
    if top_k != 10:
        raise ValueError("the frozen three-way scorer requires top_k=10")
    if any(not value.strip() for value in resolved):
        raise ValueError("resolved gold IDs must be non-empty strings")
    if evidence_item_count < 0 or unresolved_evidence_item_count < 0:
        raise ValueError("evidence counts must be non-negative")
    if len(resolved) + unresolved_evidence_item_count != evidence_item_count:
        raise ValueError("resolved plus unresolved evidence must equal the denominator")
    if evidence_item_count == 0:
        return {
            "scored": False,
            "evidence_item_count": 0,
            "resolved_evidence_item_count": 0,
            "unresolved_evidence_item_count": 0,
            "retrieved_evidence_count_at_10": 0,
            "recall_at_10": None,
            "hit_at_10": None,
            "all_at_10": None,
            "ndcg_at_10": None,
        }
    cutoff = tuple(ranked_ids[:top_k])
    cutoff_set = frozenset(cutoff)
    found = sum(1 for dialog_id in resolved if dialog_id in cutoff_set)
    resolved_unique = frozenset(resolved)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, dialog_id in enumerate(cutoff, start=1)
        if dialog_id in resolved_unique
    )
    ideal_count = min(evidence_item_count, top_k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return {
        "scored": True,
        "evidence_item_count": evidence_item_count,
        "resolved_evidence_item_count": len(resolved),
        "unresolved_evidence_item_count": unresolved_evidence_item_count,
        "retrieved_evidence_count_at_10": found,
        "recall_at_10": found / evidence_item_count,
        "hit_at_10": float(found > 0),
        "all_at_10": float(found == evidence_item_count),
        "ndcg_at_10": dcg / ideal_dcg,
    }


def _aggregate_subset(rows: list[dict[str, Any]], column: str, semantics: str) -> dict[str, Any]:
    scored = [row for row in rows if row["columns"][column][semantics]["scored"]]
    metrics = [row["columns"][column][semantics] for row in scored]
    evidence_count = sum(metric["evidence_item_count"] for metric in metrics)
    by_conversation: dict[str, list[float]] = {}
    for row in scored:
        recall = row["columns"][column][semantics]["recall_at_10"]
        by_conversation.setdefault(row["conversation_id"], []).append(float(recall))
    return {
        "question_count": len(rows),
        "scored_question_count": len(scored),
        "evidence_item_count": evidence_count,
        "resolved_evidence_item_count": sum(
            metric["resolved_evidence_item_count"] for metric in metrics
        ),
        "unresolved_evidence_item_count": sum(
            metric["unresolved_evidence_item_count"] for metric in metrics
        ),
        "question_macro_recall_at_10": (
            sum(float(metric["recall_at_10"]) for metric in metrics) / len(metrics)
            if metrics else None
        ),
        "conversation_macro_recall_at_10": (
            sum(sum(values) / len(values) for values in by_conversation.values())
            / len(by_conversation)
            if by_conversation else None
        ),
        "evidence_micro_recall_at_10": (
            sum(metric["retrieved_evidence_count_at_10"] for metric in metrics)
            / evidence_count
            if evidence_count else None
        ),
        **{
            field: (
                sum(float(metric[field]) for metric in metrics) / len(metrics)
                if metrics else None
            )
            for field in ("hit_at_10", "all_at_10", "ndcg_at_10")
        },
    }


def score_rankings(
    scorer: Any,
    rankings: dict[str, dict[str, list[str]]],
    top_k: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    question_rows: list[dict[str, Any]] = []
    for item_id in sorted(scorer.scorer_items):
        item = scorer.scorer_items[item_id]
        row = {
            "item_id": item_id,
            "conversation_id": item.opaque_conversation_id,
            "category": item.category,
            "category_name": item.category_name,
            "gold": {
                "official_exact": {
                    "resolved_dialog_ids": list(item.official_exact.resolved_opaque_dialog_ids),
                    "evidence_item_count": item.official_exact.source_evidence_item_count,
                    "unresolved_evidence_item_count": item.official_exact.unresolved_evidence_item_count,
                },
                "normalized_repaired": {
                    "resolved_dialog_ids": list(item.normalized_repaired.gold_opaque_dialog_ids),
                    "evidence_item_count": item.normalized_repaired.unique_dialog_denominator,
                    "unresolved_evidence_item_count": item.normalized_repaired.unresolved_evidence_item_count,
                },
            },
            "columns": {},
        }
        for column, by_item in rankings.items():
            ranked = by_item[item_id]
            if (
                len(ranked) > top_k
                or len(ranked) != len(set(ranked))
                or set(ranked) - set(item.corpus_opaque_dialog_ids)
            ):
                raise ValueError(f"invalid frozen ranking for {column}:{item_id}")
            official = question_metrics(
                ranked,
                item.official_exact.resolved_opaque_dialog_ids,
                evidence_item_count=item.official_exact.source_evidence_item_count,
                unresolved_evidence_item_count=item.official_exact.unresolved_evidence_item_count,
                top_k=top_k,
            )
            normalized = question_metrics(
                ranked,
                item.normalized_repaired.gold_opaque_dialog_ids,
                evidence_item_count=item.normalized_repaired.unique_dialog_denominator,
                unresolved_evidence_item_count=item.normalized_repaired.unresolved_evidence_item_count,
                top_k=top_k,
            )
            row["columns"][column] = {
                "ranked_ids_at_10": list(ranked),
                "official_exact": official,
                "normalized_repaired": normalized,
            }
        question_rows.append(row)
    aggregate: dict[str, Any] = {}
    categories = sorted({int(row["category"]) for row in question_rows})
    for column in rankings:
        aggregate[column] = {
            semantics: {
                "overall": _aggregate_subset(question_rows, column, semantics),
                "hard_categories_1_2": _aggregate_subset(
                    [row for row in question_rows if row["category"] in {1, 2}],
                    column,
                    semantics,
                ),
                "by_category": {
                    str(category): _aggregate_subset(
                        [row for row in question_rows if row["category"] == category],
                        column,
                        semantics,
                    )
                    for category in categories
                },
            }
            for semantics in ("official_exact", "normalized_repaired")
        }
    return aggregate, question_rows


def validate_raw_bm25_control(aggregate: dict[str, Any], manifest: dict[str, Any]) -> None:
    anchor = manifest["original_mempalace"]["raw_bm25_control_anchor"]
    observed = aggregate["raw_dialog_bm25"]
    pairs = {
        "official_exact_overall_question_macro_recall_at_10": observed["official_exact"][
            "overall"
        ]["question_macro_recall_at_10"],
        "official_exact_hard_question_macro_recall_at_10": observed["official_exact"][
            "hard_categories_1_2"
        ]["question_macro_recall_at_10"],
        "normalized_repaired_overall_question_macro_recall_at_10": observed[
            "normalized_repaired"
        ]["overall"]["question_macro_recall_at_10"],
        "normalized_repaired_hard_question_macro_recall_at_10": observed[
            "normalized_repaired"
        ]["hard_categories_1_2"]["question_macro_recall_at_10"],
    }
    mismatches = {
        name: {"expected": anchor[name], "observed": value}
        for name, value in pairs.items()
        if value is None or not math.isclose(float(value), float(anchor[name]), rel_tol=0.0, abs_tol=1e-15)
    }
    if mismatches:
        raise RuntimeError(f"raw-BM25 scorer/control parity failed: {mismatches}")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run(dataset_path: Path, model_dir: Path, original_root: Path, output: Path) -> dict[str, Any]:
    manifest, manifest_sha = load_manifest()
    latest_before = git_state(ROOT)
    if latest_before["git_dirty"]:
        raise ValueError("latest RPG comparison requires a clean Git worktree")
    output = output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("comparison output must be outside the measured worktree")
    original_root = original_root.resolve()
    if output == original_root or original_root in output.parents:
        raise ValueError("comparison output must be outside the original MemPalace worktree")
    protocol, candidate, bge, original_state = load_original_modules(original_root, manifest)
    dataset = protocol.load_official_locomo10(dataset_path)
    if dataset.sha256 != manifest["dataset"]["sha256"]:
        raise ValueError("LoCoMo manifest digest mismatch")
    retrieval, scorer = protocol.prepare_hard_story_track(
        dataset,
        candidate_pool_size=manifest["protocol"]["candidate_pool_size"],
        require_official_counts=True,
    )
    if len(scorer.scorer_items) != manifest["dataset"]["expected_questions"]:
        raise ValueError("LoCoMo question denominator changed")
    conversation_count = len(_conversation_items(retrieval))
    if conversation_count != manifest["dataset"]["expected_conversations"]:
        raise ValueError("LoCoMo conversation denominator changed")
    if len(scorer.hard_item_ids) != manifest["dataset"]["expected_hard_questions"]:
        raise ValueError("LoCoMo hard-question denominator changed")
    encoder = bge.load_bge_encoder(model_dir, variant=manifest["embedding"]["variant"])
    if encoder.manifest.canonical_sha256 != manifest["embedding"]["manifest_sha256"]:
        raise ValueError("BGE manifest digest mismatch")
    with tempfile.TemporaryDirectory(prefix="aerp1-locomo-three-way-") as directory:
        rankings, traces, elapsed = produce_rankings(
            retrieval,
            candidate=candidate,
            encoder=encoder,
            top_k=manifest["protocol"]["top_k"],
            db_path=str(Path(directory) / "latest-rpg.sqlite3"),
        )
    model_pair = encoder.finish_snapshot_pair().to_dict()
    aggregate, questions = score_rankings(scorer, rankings, manifest["protocol"]["top_k"])
    validate_raw_bm25_control(aggregate, manifest)
    expected_item_ids = set(scorer.scorer_items)
    ranking_item_counts = {column: len(by_item) for column, by_item in rankings.items()}
    if any(set(by_item) != expected_item_ids for by_item in rankings.values()):
        raise RuntimeError("a comparison column did not rank the full frozen question set")
    trace_audits = {item_id: audit_product_trace(trace) for item_id, trace in traces.items()}
    complete_traces = sum(audit["complete"] for audit in trace_audits.values())
    unauthorized_output_count = sum(
        len(audit["unauthorized_selected_ids"]) for audit in trace_audits.values()
    )
    if set(traces) != expected_item_ids or complete_traces != len(expected_item_ids):
        raise RuntimeError("latest RPG trace completeness gate failed")
    if unauthorized_output_count:
        raise RuntimeError("latest RPG emitted unauthorized evidence")
    latest_after = git_state(ROOT)
    if not same_git_state(latest_before, latest_after):
        raise RuntimeError("latest Git state changed during comparison")
    original_after = git_state(original_root)
    if not same_git_state(original_state, original_after):
        raise RuntimeError("original MemPalace Git state changed during comparison")
    report = {
        "schema": "aerp1-locomo-three-way-report",
        "version": 1,
        "manifest_sha256": manifest_sha,
        "manifest": manifest,
        "runtime": {
            "latest": latest_before,
            "latest_after": latest_after,
            "original": original_state,
            "original_after": original_after,
            "python": sys.version,
        },
        "dataset": {"path": str(dataset_path.resolve()), "sha256": dataset.sha256},
        "model_snapshot": model_pair,
        "denominators": {
            "conversations": conversation_count,
            "questions": len(questions),
            "hard_questions": len(scorer.hard_item_ids),
            "top_k": manifest["protocol"]["top_k"],
        },
        "retrieval_contract": {
            "annotation_free": True,
            "raw_bm25_control_anchor_passed": True,
            "latest_interface": "authorized_evidence(compact product trace) -> _retrieve_memory_items(authorized IDs only)",
            "public_track_scope": "authorization-neutral quality only; ACL safety is gated separately by blind-180",
            "latest_trace_complete": complete_traces,
            "latest_trace_expected": len(expected_item_ids),
            "latest_unauthorized_output_count": unauthorized_output_count,
            "ranking_item_counts": ranking_item_counts,
        },
        "elapsed_seconds": elapsed,
        "aggregate": aggregate,
        "questions": questions,
        "latest_policy_traces": traces,
        "latest_policy_trace_audits": trace_audits,
    }
    _atomic_json(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--original-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = run(
        Path(args.dataset), Path(args.model_dir), Path(args.original_root), Path(args.output)
    )
    print(json.dumps({"aggregate": report["aggregate"], "denominators": report["denominators"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
