"""Fail-closed AERP-2 replay of the historical six-view LoCoMo artifact.

This is deliberately an evaluator, not a product retrieval implementation.  It
replays only immutable ranking streams and keeps the historical experiment,
annotation-free productization, and improvement claims separate.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST_PATH = ROOT / "tests" / "fixtures" / "aerp2_six_view_replay_manifest.json"
EXPECTED_DEFAULT_MANIFEST_SHA256 = "09c322aab88eaac207257d86a3fabece1e48f8188086d502e74bd278446f5020"

_DIRECT_ARMS = {
    "full_six_view": "six_view_story_dense_rrf_v2",
    "raw_bm25": "raw_dialog_bm25",
    "raw_dense": "raw_dense",
}


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def _load_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} root must be an object")
    return value, raw


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _validate_manifest_bindings(artifact: dict[str, Any], artifact_raw: bytes, manifest: dict[str, Any]) -> None:
    if manifest.get("schema") != "aerp2-six-view-replay-manifest" or manifest.get("version") != 1:
        raise ValueError("unsupported AERP-2 replay manifest")
    pinned = _require_mapping(manifest.get("artifact"), "manifest.artifact")
    if _sha256_bytes(artifact_raw) != _require_digest(pinned.get("sha256"), "manifest.artifact.sha256"):
        raise ValueError("historical artifact SHA-256 mismatch")
    for name in ("schema_version", "status"):
        if artifact.get(name) != pinned.get(name):
            raise ValueError(f"historical artifact {name} mismatch")

    protocol = _require_mapping(manifest.get("protocol"), "manifest.protocol")
    if protocol.get("top_k") != 10 or protocol.get("candidate_pool_size") != 50:
        raise ValueError("AERP-2 requires frozen top-10 from a top-50 ranking stream")
    if protocol.get("hard_categories") != [1, 2] or protocol.get("rrf_k") != 60:
        raise ValueError("AERP-2 protocol binding mismatch")
    artifact_protocol = _require_mapping(artifact.get("protocol"), "artifact.protocol")
    if artifact_protocol.get("ranking_freeze_before_scorer_labels_access") is not True:
        raise ValueError("historical artifact does not attest ranking freeze before scorer labels")
    config = _require_mapping(artifact.get("config"), "artifact.config")
    if config.get("candidate_pool_size") != protocol["candidate_pool_size"]:
        raise ValueError("artifact candidate pool differs from the frozen protocol")

    freeze = _require_mapping(manifest.get("freeze"), "manifest.freeze")
    provenance = _require_mapping(artifact.get("provenance"), "artifact.provenance")
    if provenance.get("config_sha256") != freeze.get("config_sha256"):
        raise ValueError("historical config digest mismatch")
    if _require_mapping(provenance.get("dataset"), "artifact.provenance.dataset").get("expected_sha256") != freeze.get("dataset_sha256"):
        raise ValueError("historical dataset digest mismatch")
    if _require_mapping(provenance.get("model"), "artifact.provenance.model").get("manifest_sha256") != freeze.get("model_manifest_sha256"):
        raise ValueError("historical model manifest digest mismatch")
    selection = _require_mapping(artifact.get("selection_freeze"), "artifact.selection_freeze")
    if selection.get("raw_sha256") != freeze.get("selection_freeze_sha256") or selection.get("runtime_ranking_digest_verified") is not True:
        raise ValueError("historical selection freeze is not bound and verified")
    scorer = _require_mapping(freeze.get("scorer_implementation"), "manifest.freeze.scorer_implementation")
    files = _require_mapping(_require_mapping(provenance.get("code"), "artifact.provenance.code").get("files"), "artifact.provenance.code.files")
    recorded = _require_mapping(files.get(scorer.get("file")), "historical scorer implementation")
    if recorded.get("start_sha256") != scorer.get("sha256") or recorded.get("end_sha256") != scorer.get("sha256"):
        raise ValueError("historical scorer implementation digest mismatch")


def _corpora(artifact: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for index, value in enumerate(_require_list(artifact.get("corpora"), "artifact.corpora")):
        row = _require_mapping(value, f"artifact.corpora[{index}]")
        conversation = row.get("opaque_conversation_id")
        dialog_ids = _require_list(row.get("opaque_dialog_ids"), f"artifact.corpora[{index}].opaque_dialog_ids")
        if not isinstance(conversation, str) or not conversation or conversation in result:
            raise ValueError("artifact corpora must have unique non-empty conversation IDs")
        if not all(isinstance(item, str) and item for item in dialog_ids) or len(dialog_ids) != len(set(dialog_ids)):
            raise ValueError("artifact corpus must have unique non-empty dialog IDs")
        result[conversation] = set(dialog_ids)
    if not result:
        raise ValueError("artifact has no corpora")
    return result


def _freeze_source_ranking(question: dict[str, Any], method: str, corpus: set[str], pool_size: int) -> list[dict[str, Any]]:
    methods = _require_mapping(question.get("methods"), "question.methods")
    entry = methods.get(method)
    if entry is None:
        raise KeyError(method)
    rows = _require_list(_require_mapping(entry, f"question.methods.{method}").get("ranking"), f"question.methods.{method}.ranking")
    if len(rows) != pool_size:
        raise ValueError(f"{method} ranking does not preserve the frozen top-{pool_size} stream")
    frozen: list[dict[str, Any]] = []
    ids: list[str] = []
    previous: float | None = None
    for index, value in enumerate(rows):
        row = _require_mapping(value, f"{method}.ranking[{index}]")
        dialog_id, score = row.get("opaque_dialog_id"), row.get("score")
        if not isinstance(dialog_id, str) or dialog_id not in corpus:
            raise ValueError(f"{method} ranking contains an unknown dialog ID")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            raise ValueError(f"{method} ranking contains a non-finite score")
        numeric = float(score)
        if previous is not None and numeric > previous:
            raise ValueError(f"{method} ranking scores are not non-increasing")
        previous = numeric
        ids.append(dialog_id)
        frozen.append({"opaque_dialog_id": dialog_id, "score": numeric})
    if len(ids) != len(set(ids)):
        raise ValueError(f"{method} ranking contains duplicate dialog IDs")
    return frozen


def _fuse_rrf(left: list[dict[str, Any]], right: list[dict[str, Any]], rrf_k: int) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    for weight, ranking in ((2.0, left), (1.0, right)):
        for rank, row in enumerate(ranking, start=1):
            dialog_id = row["opaque_dialog_id"]
            scores[dialog_id] = scores.get(dialog_id, 0.0) + weight / (rrf_k + rank)
    return [
        {"opaque_dialog_id": dialog_id, "score": score}
        for dialog_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    ]


def _freeze_arms(questions: list[dict[str, Any]], corpora: dict[str, set[str]], pool_size: int, rrf_k: int) -> dict[str, dict[str, Any]]:
    frozen: dict[str, dict[str, Any]] = {name: {"rankings": []} for name in (*_DIRECT_ARMS, "raw_bm25_plus_raw_dense")}
    missing: dict[str, set[str]] = {name: set() for name in frozen}
    for question in questions:
        conversation = question.get("opaque_conversation_id")
        question_id = question.get("opaque_question_id")
        if not isinstance(conversation, str) or conversation not in corpora or not isinstance(question_id, str) or not question_id:
            raise ValueError("question does not bind to a known corpus and ID")
        source: dict[str, list[dict[str, Any]]] = {}
        for method in set(_DIRECT_ARMS.values()):
            try:
                source[method] = _freeze_source_ranking(question, method, corpora[conversation], pool_size)
            except KeyError:
                pass
        for arm, method in _DIRECT_ARMS.items():
            if method not in source:
                missing[arm].add(method)
            else:
                frozen[arm]["rankings"].append((conversation, question_id, source[method]))
        if "raw_dialog_bm25" not in source or "raw_dense" not in source:
            missing["raw_bm25_plus_raw_dense"].add("raw_dialog_bm25" if "raw_dialog_bm25" not in source else "raw_dense")
        else:
            frozen["raw_bm25_plus_raw_dense"]["rankings"].append((conversation, question_id, _fuse_rrf(source["raw_dialog_bm25"], source["raw_dense"], rrf_k)[:pool_size]))
    for arm, entry in frozen.items():
        if missing[arm]:
            entry.clear()
            entry.update({"status": "blocked_missing_immutable_ranking_stream", "missing_immutable_ranking_streams": sorted(missing[arm]), "ranking_stream_sha256": None, "official_exact": None})
            continue
        records = entry["rankings"]
        if len(records) != len(questions):
            raise ValueError(f"{arm} did not freeze every question ranking")
        entry["status"] = "complete"
        entry["ranking_stream_sha256"] = _canonical_sha256(records)
    return frozen


def _scored_recall(ranking: list[dict[str, Any]], scorer: dict[str, Any], top_k: int) -> float | None:
    semantics = _require_mapping(_require_mapping(scorer.get("evidence_semantics"), "question.scorer.evidence_semantics").get("official_exact"), "question official exact scorer")
    evidence_count = semantics.get("evidence_item_count")
    unresolved = semantics.get("unresolved_evidence_item_count")
    gold = _require_list(semantics.get("resolved_opaque_dialog_ids"), "question official exact gold")
    if not isinstance(evidence_count, int) or isinstance(evidence_count, bool) or evidence_count < 0 or not isinstance(unresolved, int) or isinstance(unresolved, bool) or unresolved < 0 or len(gold) + unresolved != evidence_count:
        raise ValueError("invalid official-exact scorer denominator")
    if not all(isinstance(item, str) and item for item in gold):
        raise ValueError("invalid official-exact gold dialog ID")
    if evidence_count == 0:
        return None
    cutoff = {row["opaque_dialog_id"] for row in ranking[:top_k]}
    return sum(dialog_id in cutoff for dialog_id in gold) / evidence_count


def _score_arm(records: Iterable[tuple[str, str, list[dict[str, Any]]]], questions: list[dict[str, Any]], hard_categories: set[int], top_k: int) -> dict[str, Any]:
    ranking_by_question = {(conversation, question_id): ranking for conversation, question_id, ranking in records}
    overall: list[float] = []
    hard: list[float] = []
    for question in questions:
        key = (question["opaque_conversation_id"], question["opaque_question_id"])
        recall = _scored_recall(ranking_by_question[key], _require_mapping(question.get("scorer"), "question.scorer"), top_k)
        if recall is not None:
            overall.append(recall)
            if question["scorer"].get("category") in hard_categories:
                hard.append(recall)
    key = str(top_k)
    return {
        "scored_question_count": len(overall),
        "hard_scored_question_count": len(hard),
        "question_macro_recall_at_k": {key: sum(overall) / len(overall) if overall else None},
        "hard_question_macro_recall_at_k": {key: sum(hard) / len(hard) if hard else None},
    }


def _validate_anchors(arms: dict[str, dict[str, Any]], manifest: dict[str, Any], top_k: int) -> None:
    anchors = _require_mapping(manifest.get("anchors"), "manifest.anchors")
    for arm_name, anchor_name in (("raw_bm25", "raw_bm25"), ("full_six_view", "six_view")):
        expected = _require_mapping(anchors.get(anchor_name), f"manifest.anchors.{anchor_name}")
        observed = arms[arm_name]["official_exact"]
        if observed is None:
            raise RuntimeError(f"required anchored arm is unavailable: {arm_name}")
        for name, field in (("overall", "question_macro_recall_at_k"), ("hard", "hard_question_macro_recall_at_k")):
            actual = observed[field][str(top_k)]
            if actual is None or not math.isclose(actual, expected.get(name), rel_tol=0.0, abs_tol=1e-15):
                raise RuntimeError(f"{arm_name} {name} official-exact anchor mismatch")


def run_replay(artifact_path: Path | str, manifest_path: Path | str = DEFAULT_MANIFEST_PATH) -> dict[str, Any]:
    """Verify the immutable historical stream; unavailable views remain blocked."""
    artifact_file = Path(artifact_path).resolve()
    manifest_file = Path(manifest_path).resolve()
    manifest, manifest_raw = _load_json(manifest_file, "AERP-2 replay manifest")
    if manifest_file == DEFAULT_MANIFEST_PATH.resolve() and _sha256_bytes(manifest_raw) != EXPECTED_DEFAULT_MANIFEST_SHA256:
        raise ValueError("AERP-2 frozen manifest SHA-256 mismatch")
    artifact, artifact_raw = _load_json(artifact_file, "historical AERP-2 artifact")
    _validate_manifest_bindings(artifact, artifact_raw, manifest)
    corpora = _corpora(artifact)
    questions = [_require_mapping(value, f"artifact.questions[{index}]") for index, value in enumerate(_require_list(artifact.get("questions"), "artifact.questions"))]
    if len({(row.get("opaque_conversation_id"), row.get("opaque_question_id")) for row in questions}) != len(questions):
        raise ValueError("artifact repeats a question identity")
    protocol = _require_mapping(manifest["protocol"], "manifest.protocol")
    frozen = _freeze_arms(questions, corpora, protocol["candidate_pool_size"], protocol["rrf_k"])
    for arm in frozen.values():
        if arm["status"] == "complete":
            arm["official_exact"] = _score_arm(arm.pop("rankings"), questions, set(protocol["hard_categories"]), protocol["top_k"])
    _validate_anchors(frozen, manifest, protocol["top_k"])
    hard_questions = sum(_require_mapping(row.get("scorer"), "question.scorer").get("category") in set(protocol["hard_categories"]) for row in questions)
    blocked = [name for name, arm in frozen.items() if arm["status"] != "complete"]
    return {
        "schema": "aerp2-six-view-replay-report",
        "version": 1,
        "status": "partial" if blocked else "complete",
        "phase_order": ["verify_inputs", "freeze_rankings", "open_scorer_labels", "score"],
        "input_freeze": {"artifact_sha256": _sha256_bytes(artifact_raw), "manifest_sha256": _sha256_bytes(manifest_raw), **manifest["freeze"]},
        "denominators": {"questions": len(questions), "hard_questions": hard_questions, "top_k": protocol["top_k"]},
        "arms": frozen,
        "claim_boundary": {
            "exact_replay": "verified only for complete immutable ranking streams",
            "annotation_free_productization": "not evaluated by this historical replay",
            "improvement_claim": "not made while any required ablation arm is blocked",
        },
    }
