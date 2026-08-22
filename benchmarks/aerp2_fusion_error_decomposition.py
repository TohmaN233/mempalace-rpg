"""Read-only, fail-closed AERP-2 Product-vs-raw-fusion error decomposition."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aerp2-fusion-error-decomposition"
SOURCE_ARMS = ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense")
TOP10_ARMS = ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense", "legacy_rpg", "product_six_view", "historical_six_view")
PRODUCT_ARM = "product_six_view"
BASELINE_ARM = "raw_bm25_plus_raw_dense"
EXPECTED_QUESTIONS = 1986
EXPECTED_TOP_K = 10
EXPECTED_POOL_SIZE = 50
EXPECTED_RESOLVED = 2806
EXPECTED_UNRESOLVED = 9
EXPECTED_COUNTS = {
    "shared_hit": 1335,
    "fusion_promotion": 251,
    "fusion_demotion": 138,
    "shared_miss_candidate_available": 578,
    "raw_pool_miss": 504,
}
FORBIDDEN_OUTPUT_WORDS = frozenset({"transcript", "query", "answer", "text"})


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_git_head(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be a lowercase Git commit ID")
    return value


def _require_id_list(value: Any, label: str, expected_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) != expected_length:
        raise ValueError(f"{label} has an unexpected length")
    if any(not isinstance(item, str) or not item for item in value) or len(set(value)) != len(value):
        raise ValueError(f"{label} must contain unique non-empty opaque IDs")
    return value


def _load_artifact(path: Path, expected_artifact_sha256: str, raw: bytes | None = None) -> tuple[dict[str, Any], bytes, str]:
    raw = path.read_bytes() if raw is None else raw
    receipt = _sha256(raw)
    if receipt != _require_digest(expected_artifact_sha256, "expected artifact SHA-256"):
        raise ValueError("artifact SHA-256 mismatch")
    try:
        artifact = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("artifact is not valid JSON") from error
    artifact = _require_mapping(artifact, "artifact")
    if artifact.get("schema") != "aerp2-product-six-view-locomo" or artifact.get("status") != "complete":
        raise ValueError("artifact schema or status mismatch")
    return artifact, raw, receipt


def _assert_artifact_unchanged(path: Path, expected_bytes: bytes) -> None:
    if path.read_bytes() != expected_bytes:
        raise RuntimeError("artifact bytes changed during analysis")


def _validate_git_receipt(artifact: dict[str, Any], expected_git_head: str) -> dict[str, Any]:
    expected_git_head = _require_git_head(expected_git_head, "expected Git head")
    before = _require_mapping(artifact.get("git_state_before"), "artifact.git_state_before")
    after = _require_mapping(artifact.get("git_state_after"), "artifact.git_state_after")
    if before != after or before.get("git_dirty") is not False or _require_git_head(before.get("git_head"), "artifact Git receipt git_head") != expected_git_head:
        raise ValueError("artifact Git receipt mismatch")
    _require_git_head(before.get("git_tree"), "artifact Git receipt git_tree")
    for field in ("commit_diff_sha256", "worktree_status_sha256"):
        _require_digest(before.get(field), f"artifact Git receipt {field}")
    if not isinstance(before.get("commit_diff_bytes"), int) or isinstance(before["commit_diff_bytes"], bool) or before["commit_diff_bytes"] < 0:
        raise ValueError("artifact Git receipt commit_diff_bytes is invalid")
    return {
        "git_head": before["git_head"],
        "git_tree": before["git_tree"],
        "commit_diff_sha256": before["commit_diff_sha256"],
        "commit_diff_bytes": before["commit_diff_bytes"],
        "worktree_status_sha256": before["worktree_status_sha256"],
    }


def _validate_streams(artifact: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    streams = _require_mapping(artifact.get("source_pool_rankings"), "artifact.source_pool_rankings")
    recorded = _require_mapping(artifact.get("source_pool_stream_sha256"), "artifact.source_pool_stream_sha256")
    if set(streams) != set(SOURCE_ARMS) or set(recorded) != set(SOURCE_ARMS):
        raise ValueError("artifact source stream schema mismatch")
    normalized: dict[str, dict[str, list[str]]] = {}
    question_ids: set[str] | None = None
    for arm in SOURCE_ARMS:
        stream = _require_mapping(streams[arm], f"artifact source stream {arm}")
        if _sha256(_canonical_bytes(stream)) != _require_digest(recorded[arm], f"artifact source stream receipt {arm}"):
            raise ValueError(f"artifact source stream receipt mismatch: {arm}")
        if question_ids is None:
            question_ids = set(stream)
        elif set(stream) != question_ids:
            raise ValueError("artifact source streams have different question IDs")
        normalized[arm] = {item_id: _require_id_list(ranking, f"artifact source stream {arm}", EXPECTED_POOL_SIZE) for item_id, ranking in stream.items()}
    if question_ids is None or len(question_ids) != EXPECTED_QUESTIONS:
        raise ValueError("artifact source stream question denominator drift")
    return normalized


def _validate_top10_streams(artifact: dict[str, Any], question_ids: set[str]) -> dict[str, dict[str, list[str]]]:
    streams = _require_mapping(artifact.get("rankings_top10"), "artifact top-k streams")
    recorded = _require_mapping(artifact.get("ranking_stream_sha256"), "artifact top-k stream receipts")
    if set(streams) != set(TOP10_ARMS) or set(recorded) != set(TOP10_ARMS):
        raise ValueError("artifact top-k stream schema mismatch")
    normalized: dict[str, dict[str, list[str]]] = {}
    for arm in TOP10_ARMS:
        stream = _require_mapping(streams[arm], f"artifact top-k stream {arm}")
        if set(stream) != question_ids:
            raise ValueError("artifact top-k stream question identities differ")
        if _sha256(_canonical_bytes(stream)) != _require_digest(recorded[arm], f"artifact top-k stream receipt {arm}"):
            raise ValueError(f"artifact top-k stream receipt mismatch: {arm}")
        normalized[arm] = {item_id: _require_id_list(ranking, f"artifact top-k stream {arm}", EXPECTED_TOP_K) for item_id, ranking in stream.items()}
    return normalized


def _ranked_ids(question: dict[str, Any], arm: str) -> list[str]:
    columns = _require_mapping(question.get("columns"), "question columns")
    arm_columns = _require_mapping(columns.get(arm), f"question {arm} columns")
    return _require_id_list(arm_columns.get("ranked_ids_at_10"), f"question {arm} top-k", EXPECTED_TOP_K)


def _official_exact(question: dict[str, Any]) -> tuple[list[str], int]:
    gold = _require_mapping(question.get("gold"), "question gold")
    official = _require_mapping(gold.get("official_exact"), "question official-exact gold")
    resolved = official.get("resolved_dialog_ids")
    unresolved = official.get("unresolved_evidence_item_count")
    evidence = official.get("evidence_item_count")
    if not isinstance(resolved, list) or any(not isinstance(item, str) or not item for item in resolved):
        raise ValueError("question official-exact resolved IDs are invalid")
    if not isinstance(unresolved, int) or isinstance(unresolved, bool) or unresolved < 0:
        raise ValueError("question official-exact unresolved denominator is invalid")
    if not isinstance(evidence, int) or isinstance(evidence, bool) or evidence < 0 or evidence != len(resolved) + unresolved:
        raise ValueError("question official-exact denominator is invalid")
    return resolved, unresolved


def _expected_official_exact_metrics(ranked_ids: list[str], resolved: list[str], unresolved: int) -> dict[str, Any]:
    evidence = len(resolved) + unresolved
    if evidence == 0:
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
    cutoff = tuple(ranked_ids[:EXPECTED_TOP_K])
    cutoff_set = frozenset(cutoff)
    found = sum(dialog_id in cutoff_set for dialog_id in resolved)
    resolved_unique = frozenset(resolved)
    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, dialog_id in enumerate(cutoff, start=1)
        if dialog_id in resolved_unique
    )
    ideal_count = min(evidence, EXPECTED_TOP_K)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return {
        "scored": True,
        "evidence_item_count": evidence,
        "resolved_evidence_item_count": len(resolved),
        "unresolved_evidence_item_count": unresolved,
        "retrieved_evidence_count_at_10": found,
        "recall_at_10": found / evidence,
        "hit_at_10": float(found > 0),
        "all_at_10": float(found == evidence),
        "ndcg_at_10": dcg / ideal_dcg,
    }


def _validate_reported_official_exact(question: dict[str, Any], arm: str, ranked_ids: list[str], resolved: list[str], unresolved: int) -> None:
    columns = _require_mapping(question.get("columns"), "question columns")
    arm_columns = _require_mapping(columns.get(arm), f"question {arm} columns")
    reported = _require_mapping(arm_columns.get("official_exact"), f"question {arm} official-exact metrics")
    expected = _expected_official_exact_metrics(ranked_ids, resolved, unresolved)
    if type(reported.get("scored")) is not bool or reported["scored"] is not expected["scored"]:
        raise ValueError(f"question {arm} reported official-exact metrics drift")
    for field in ("evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count", "retrieved_evidence_count_at_10"):
        expected_value = expected[field]
        actual = reported.get(field)
        if not isinstance(actual, int) or isinstance(actual, bool) or actual != expected_value:
            raise ValueError(f"question {arm} reported official-exact metrics drift")
    for field in ("recall_at_10", "hit_at_10", "all_at_10", "ndcg_at_10"):
        expected_value = expected[field]
        actual = reported.get(field)
        if expected_value is None:
            valid = actual is None
        else:
            valid = type(actual) is float and math.isfinite(actual) and actual == expected_value
        if not valid:
            raise ValueError(f"question {arm} reported official-exact metrics drift")


def _validate_output_safety(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in FORBIDDEN_OUTPUT_WORDS:
                raise RuntimeError("report output schema contains forbidden text field")
            _validate_output_safety(child)
    elif isinstance(value, list):
        for child in value:
            _validate_output_safety(child)
    elif isinstance(value, str) and any(word in value.casefold() for word in FORBIDDEN_OUTPUT_WORDS):
        raise RuntimeError("report output contains forbidden text")


def decompose(artifact_path: Path | str, *, expected_artifact_sha256: str, expected_git_head: str, _artifact_bytes: bytes | None = None) -> dict[str, Any]:
    """Classify frozen product-vs-raw-fusion outcomes without reranking anything."""
    artifact_file = Path(artifact_path).resolve()
    artifact, artifact_bytes, artifact_sha256 = _load_artifact(artifact_file, expected_artifact_sha256, _artifact_bytes)
    git_receipt = _validate_git_receipt(artifact, expected_git_head)
    streams = _validate_streams(artifact)
    top10_streams = _validate_top10_streams(artifact, set(streams["raw_bm25"]))
    questions = artifact.get("questions")
    if not isinstance(questions, list) or len(questions) != EXPECTED_QUESTIONS:
        raise ValueError("artifact question denominator drift")

    counts = {name: 0 for name in EXPECTED_COUNTS}
    by_category: dict[int, dict[str, int]] = {}
    by_conversation: dict[str, dict[str, int]] = {}
    by_category_conversation: dict[int, dict[str, dict[str, int]]] = {}
    question_ids: set[str] = set()
    resolved_count = 0
    unresolved_count = 0
    product_hits = 0
    baseline_hits = 0
    for raw_question in questions:
        question = _require_mapping(raw_question, "artifact question")
        item_id = question.get("item_id")
        if not isinstance(item_id, str) or not item_id or item_id in question_ids:
            raise ValueError("artifact question IDs are invalid")
        question_ids.add(item_id)
        category = question.get("category")
        conversation_id = question.get("conversation_id")
        if not isinstance(category, int) or isinstance(category, bool):
            raise ValueError("artifact question category is invalid")
        if not isinstance(conversation_id, str) or not conversation_id:
            raise ValueError("artifact question conversation ID is invalid")
        if item_id not in streams["raw_bm25"]:
            raise ValueError("artifact question does not bind to frozen raw streams")
        for arm in SOURCE_ARMS:
            if top10_streams[arm][item_id] != streams[arm][item_id][:EXPECTED_TOP_K]:
                raise ValueError("artifact top-k stream does not match frozen raw pool")
        product_ids = _ranked_ids(question, PRODUCT_ARM)
        baseline_ids = _ranked_ids(question, BASELINE_ARM)
        if product_ids != top10_streams[PRODUCT_ARM][item_id] or baseline_ids != top10_streams[BASELINE_ARM][item_id]:
            raise ValueError("artifact question top-k columns do not match frozen streams")
        product = set(product_ids)
        baseline = set(baseline_ids)
        raw_pool = set(streams["raw_bm25"][item_id]) | set(streams["raw_dense"][item_id])
        if not baseline <= raw_pool:
            raise ValueError("artifact raw-fusion top-k is outside the raw candidate pool")
        resolved, unresolved = _official_exact(question)
        _validate_reported_official_exact(question, PRODUCT_ARM, product_ids, resolved, unresolved)
        _validate_reported_official_exact(question, BASELINE_ARM, baseline_ids, resolved, unresolved)
        category_counts = by_category.setdefault(category, {name: 0 for name in EXPECTED_COUNTS})
        conversation_hash = _sha256(conversation_id.encode("utf-8"))
        conversation_counts = by_conversation.setdefault(conversation_hash, {name: 0 for name in EXPECTED_COUNTS})
        category_conversation_counts = by_category_conversation.setdefault(category, {}).setdefault(
            conversation_hash,
            {name: 0 for name in EXPECTED_COUNTS},
        )
        unresolved_count += unresolved
        for dialog_id in resolved:
            resolved_count += 1
            product_hit = dialog_id in product
            baseline_hit = dialog_id in baseline
            product_hits += product_hit
            baseline_hits += baseline_hit
            if product_hit and baseline_hit:
                bucket = "shared_hit"
            elif product_hit:
                bucket = "fusion_promotion"
            elif baseline_hit:
                bucket = "fusion_demotion"
            elif dialog_id in raw_pool:
                bucket = "shared_miss_candidate_available"
            else:
                bucket = "raw_pool_miss"
            counts[bucket] += 1
            category_counts[bucket] += 1
            conversation_counts[bucket] += 1
            category_conversation_counts[bucket] += 1
    if question_ids != set(streams["raw_bm25"]):
        raise ValueError("artifact question and raw stream identities differ")
    if resolved_count != EXPECTED_RESOLVED or unresolved_count != EXPECTED_UNRESOLVED:
        raise ValueError("artifact official-exact denominator drift")
    if counts != EXPECTED_COUNTS:
        raise ValueError("artifact frozen decomposition count drift")

    report = {
        "schema": SCHEMA,
        "version": 1,
        "status": "complete",
        "input_receipt": {
            "artifact_sha256": artifact_sha256,
            "analyzer_implementation_sha256": _sha256(Path(__file__).read_bytes()),
            "git": git_receipt,
            "source_pool_stream_sha256": {arm: artifact["source_pool_stream_sha256"][arm] for arm in SOURCE_ARMS},
            "top10_stream_sha256": {arm: artifact["ranking_stream_sha256"][arm] for arm in (PRODUCT_ARM, BASELINE_ARM)},
        },
        "denominators": {
            "questions": len(questions),
            "top_k": EXPECTED_TOP_K,
            "raw_candidate_pool_size": EXPECTED_POOL_SIZE,
            "resolved_official_exact_evidence_items": resolved_count,
            "unresolved_official_exact_evidence_items": unresolved_count,
        },
        "protocol": {
            "evidence_semantics": "official_exact",
            "product_arm": PRODUCT_ARM,
            "baseline_arm": BASELINE_ARM,
            "raw_pool_arms": ["raw_bm25", "raw_dense"],
            "top_k": EXPECTED_TOP_K,
            "raw_candidate_pool_size": EXPECTED_POOL_SIZE,
        },
        "decomposition": {**counts, "product_hits": product_hits, "raw_fusion_hits": baseline_hits},
        "by_category": [{"category": category, **by_category[category]} for category in sorted(by_category)],
        "by_conversation": {conversation_hash: by_conversation[conversation_hash] for conversation_hash in sorted(by_conversation)},
        "by_category_conversation": [
            {
                "category": category,
                "conversations": {
                    conversation_hash: by_category_conversation[category][conversation_hash]
                    for conversation_hash in sorted(by_category_conversation[category])
                },
            }
            for category in sorted(by_category_conversation)
        ],
        "claim_boundary": "Classification is limited to product-versus-raw-fusion top-k outcomes and raw-pool availability; it cannot establish Product view-pool miss or fusion causality.",
    }
    _assert_artifact_unchanged(artifact_file, artifact_bytes)
    _validate_output_safety(report)
    return report


def atomic_json(path: Path | str, report: dict[str, Any], *, artifact_path: Path | str, expected_artifact_bytes: bytes) -> None:
    output = Path(path).resolve()
    artifact = Path(artifact_path).resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("output must be outside the repository")
    if output == artifact:
        raise ValueError("output must not overwrite the artifact")
    if not isinstance(expected_artifact_bytes, bytes):
        raise ValueError("artifact publication receipt is invalid")
    report = _require_mapping(report, "report")
    if report.get("schema") != SCHEMA or report.get("version") != 1 or report.get("status") != "complete":
        raise ValueError("report schema or status mismatch")
    receipt = _require_mapping(report.get("input_receipt"), "report input receipt")
    if _require_digest(receipt.get("artifact_sha256"), "report artifact receipt") != _sha256(expected_artifact_bytes):
        raise ValueError("report artifact receipt mismatch")
    if _require_digest(receipt.get("analyzer_implementation_sha256"), "report analyzer receipt") != _sha256(Path(__file__).read_bytes()):
        raise ValueError("report analyzer receipt mismatch")
    _validate_output_safety(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=output.parent, prefix=f".{output.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(_canonical_bytes(report))
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        _assert_artifact_unchanged(artifact, expected_artifact_bytes)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--expected-artifact-sha256", required=True)
    parser.add_argument("--expected-git-head", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        artifact_file = Path(args.artifact).resolve()
        artifact_bytes = artifact_file.read_bytes()
        report = decompose(artifact_file, expected_artifact_sha256=args.expected_artifact_sha256, expected_git_head=args.expected_git_head, _artifact_bytes=artifact_bytes)
        atomic_json(args.output, report, artifact_path=artifact_file, expected_artifact_bytes=artifact_bytes)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "decomposition": report["decomposition"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
