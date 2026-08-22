"""Read-only FCD-2 causal-ablation analysis over a frozen FCD-1 artifact."""
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
SCHEMA = "aerp3-fcd2-causal-ablation"
FCD1_SCHEMA = "aerp3-fcd1-replay-ledger-v1"
EXPECTED_QUESTIONS = 1986
EXPECTED_POOL = 50
TOP_K = 10
PRODUCT_ARM = "product_six_view"
RAW_ARMS = ("raw_bm25", "raw_dense")
BASELINE_ARM = "raw_bm25_plus_raw_dense"
TOP10_ARMS = ("raw_bm25", "raw_dense", "raw_bm25_plus_raw_dense", "legacy_rpg", "product_six_view", "historical_six_view")
VIEWS = ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")
ADD_VIEW_ORDER = ("raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense")
RRF_K = 60
FROZEN_WEIGHTS = {"raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0, "observation_dense": 2.0, "checkpoint_dense": 2.0, "combo_dense": 1.0}
P1_MIN_DELTA = 0.05
P2_MIN_DELTA = -0.01
STRICT_MAJORITY_MULTIPLIER = 2
EXPECTED_CONVERSATIONS = 10
MIN_RECOVERY_CONVERSATIONS = 8
PRODUCT_TOP10_SHA256 = "64007282069621bb3e603598938993ebe0907e8e84ebaa65394741ab618e5441"
FORBIDDEN = frozenset({"query", "transcript", "answer", "text"})
FCD1_LEDGER_FIELDS = frozenset({"schema", "input_sha256", "authorization_sha256", "view_top_50", "view_top_50_sha256", "view_full_order", "view_order_sha256", "fused_top_50", "checkpoint_tie_group_semantics", "checkpoint_tie_groups"})
FCD1_VIEW_ROW_FIELDS = frozenset({"source_event_id", "ranking_key_sha256", "ranking_key_order", "rank", "score"})


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest(value: Any) -> str:
    return _sha(_canonical(value))


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _digest_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _git_head(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{label} must be a lowercase Git commit ID")
    return value


def _ids(value: Any, label: str, size: int) -> list[str]:
    if not isinstance(value, list) or len(value) != size or any(not isinstance(item, str) or not item for item in value) or len(set(value)) != size:
        raise ValueError(f"{label} must contain {size} unique opaque IDs")
    return value


def _load(path: Path, expected_sha: str, raw: bytes | None = None) -> tuple[dict[str, Any], bytes, str]:
    raw = path.read_bytes() if raw is None else raw
    receipt = _sha(raw)
    if receipt != _digest_value(expected_sha, "expected artifact SHA-256"):
        raise ValueError("artifact SHA-256 mismatch")
    try:
        artifact = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("artifact is not valid JSON") from error
    artifact = _mapping(artifact, "artifact")
    if artifact.get("schema") != "aerp2-product-six-view-locomo" or artifact.get("status") != "complete":
        raise ValueError("artifact schema or status mismatch")
    return artifact, raw, receipt


def _unchanged(path: Path, raw: bytes) -> None:
    if path.read_bytes() != raw:
        raise RuntimeError("artifact bytes changed during analysis")


def _validate_header(artifact: dict[str, Any], expected_head: str) -> None:
    expected_head = _git_head(expected_head, "expected Git head")
    before = _mapping(artifact.get("git_state_before"), "artifact Git state before")
    after = _mapping(artifact.get("git_state_after"), "artifact Git state after")
    if before != after or before.get("git_dirty") is not False or _git_head(before.get("git_head"), "artifact Git head") != expected_head:
        raise ValueError("artifact Git receipt mismatch")
    acceptance = _mapping(artifact.get("fcd1_acceptance"), "artifact FCD-1 acceptance")
    expected_acceptance = {
        "ledger_schema": FCD1_SCHEMA, "top_k": EXPECTED_POOL, "expected_questions": EXPECTED_QUESTIONS,
        "reference_product_top10_sha256": PRODUCT_TOP10_SHA256, "actual_product_top10_sha256": PRODUCT_TOP10_SHA256,
        "top10_unchanged": True,
    }
    if acceptance != expected_acceptance:
        raise ValueError("artifact FCD-1 acceptance mismatch")
    safety = _mapping(artifact.get("safety_summary"), "artifact safety summary")
    if safety.get("pass") is not True or safety.get("expected_trace_count") != EXPECTED_QUESTIONS or safety.get("trace_count") != EXPECTED_QUESTIONS or safety.get("fcd1_ledger_complete_count") != EXPECTED_QUESTIONS or safety.get("unauthorized_selected_count") != 0:
        raise ValueError("artifact safety receipt mismatch")


def _validate_stream_receipts(artifact: dict[str, Any], pools: dict[str, Any], top10: dict[str, Any]) -> None:
    pool_receipts = _mapping(artifact.get("source_pool_stream_sha256"), "source-pool stream receipts")
    ranking_receipts = _mapping(artifact.get("ranking_stream_sha256"), "ranking stream receipts")
    if set(pool_receipts) != set(pools) or set(ranking_receipts) != set(top10):
        raise ValueError("artifact stream receipt arms differ")
    for arm, stream in pools.items():
        if _digest_value(pool_receipts[arm], f"{arm} source-pool receipt") != _digest(stream):
            raise ValueError("artifact source-pool receipt mismatch")
    for arm, stream in top10.items():
        if _digest_value(ranking_receipts[arm], f"{arm} top-10 receipt") != _digest(stream):
            raise ValueError("artifact top-10 receipt mismatch")
    if _digest(top10[PRODUCT_ARM]) != PRODUCT_TOP10_SHA256:
        raise ValueError("artifact Product top-10 stream differs from FCD-1 freeze")


def _validate_question_audits(artifact: dict[str, Any], questions: list[Any], pools: dict[str, Any], top10: dict[str, Any], traces: dict[str, Any]) -> None:
    audits = artifact.get("question_audits")
    if not isinstance(audits, list) or len(audits) != EXPECTED_QUESTIONS:
        raise ValueError("artifact question-audit count differs")
    question_pairs: set[tuple[str, str]] = set()
    for question in questions:
        question = _mapping(question, "question")
        item, conversation = question.get("item_id"), question.get("conversation_id")
        if not isinstance(item, str) or not item or not isinstance(conversation, str) or not conversation or (conversation, item) in question_pairs:
            raise ValueError("artifact question identities are malformed")
        question_pairs.add((conversation, item))
    audit_pairs: set[tuple[str, str]] = set()
    for audit in audits:
        audit = _mapping(audit, "question audit")
        composite = audit.get("composite_id")
        if not isinstance(composite, list) or len(composite) != 2 or any(not isinstance(value, str) or not value for value in composite):
            raise ValueError("artifact question-audit identity is malformed")
        pair = (composite[0], composite[1])
        source = _mapping(audit.get("source_pool"), "question-audit source pools")
        streams = _mapping(audit.get("top10"), "question-audit top-10")
        product_retrieval = _mapping(audit.get("product_retrieval"), "question-audit Product retrieval")
        if set(source) != set(pools) or set(streams) != set(top10):
            raise ValueError("question-audit arm schema differs")
        for arm in pools:
            receipt = _mapping(source[arm], "question-audit source-pool receipt")
            if receipt != {"count": EXPECTED_POOL, "sha256": _digest(pools[arm].get(pair[1]))}:
                raise ValueError("question-audit source-pool receipt mismatch")
        for arm in top10:
            if streams[arm] != top10[arm].get(pair[1]):
                raise ValueError("question-audit top-10 differs")
        ledger = _ledger(_mapping(traces.get(pair[1]), "question-audit trace"))
        if _digest_value(product_retrieval.get("fcd1_diagnostic_ledger_sha256"), "question-audit FCD-1 ledger receipt") != _digest(ledger):
            raise ValueError("question-audit FCD-1 ledger receipt mismatch")
        audit_pairs.add(pair)
    if audit_pairs != question_pairs:
        raise ValueError("question-audit identities differ")


def _metric(ranking: list[str], gold: list[str], unresolved: int) -> float | None:
    denominator = len(gold) + unresolved
    if denominator == 0:
        return None
    cutoff = set(ranking[:TOP_K])
    return sum(item in cutoff for item in gold) / denominator


def _aggregate(rows: list[tuple[int, list[str], int, list[str]]]) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for name, predicate in (("overall", lambda category: True), ("hard", lambda category: category in {1, 2}), ("adversarial", lambda category: category == 5)):
        values = [_metric(ranking, gold, unresolved) for category, gold, unresolved, ranking in rows if predicate(category)]
        scored = [value for value in values if value is not None]
        result[name] = math.fsum(scored) / len(scored) if scored else None
    return result


def _aggregate_union(rows: list[tuple[int, list[str], int, set[str]]]) -> dict[str, float | None]:
    """Official-exact recall for a candidate union, not an invented ordering."""
    result: dict[str, float | None] = {}
    for name, predicate in (("overall", lambda category: True), ("hard", lambda category: category in {1, 2}), ("adversarial", lambda category: category == 5)):
        values: list[float] = []
        for category, gold, unresolved, candidates in rows:
            if not predicate(category):
                continue
            denominator = len(gold) + unresolved
            if denominator:
                values.append(sum(identifier in candidates for identifier in gold) / denominator)
        result[name] = math.fsum(values) / len(values) if values else None
    return result


def _difference(left: dict[str, float | None], right: dict[str, float | None]) -> dict[str, float | None]:
    return {name: None if left[name] is None or right[name] is None else left[name] - right[name] for name in left}


def _p1_pass(deltas: dict[str, float | None]) -> bool:
    return all(deltas[name] is not None and deltas[name] >= P1_MIN_DELTA for name in ("overall", "hard"))


def _p2_pass(deltas: dict[str, float | None]) -> bool:
    return all(deltas[name] is not None and deltas[name] >= P2_MIN_DELTA for name in ("overall", "hard"))


def _gold(question: dict[str, Any]) -> tuple[list[str], int]:
    official = _mapping(_mapping(question.get("gold"), "question gold").get("official_exact"), "question official-exact gold")
    resolved = official.get("resolved_dialog_ids")
    unresolved = official.get("unresolved_evidence_item_count")
    evidence = official.get("evidence_item_count")
    if not isinstance(resolved, list) or any(not isinstance(item, str) or not item for item in resolved) or not isinstance(unresolved, int) or isinstance(unresolved, bool) or unresolved < 0 or evidence != len(resolved) + unresolved:
        raise ValueError("question official-exact denominator is invalid")
    return resolved, unresolved


def _ledger(trace: dict[str, Any]) -> dict[str, Any]:
    ranking = _mapping(trace.get("retrieval_ranking"), "question retrieval ranking")
    ledger = _mapping(ranking.get("fcd1_diagnostic_ledger"), "question FCD-1 ledger")
    if set(ledger) != FCD1_LEDGER_FIELDS or ledger.get("schema") != FCD1_SCHEMA:
        raise ValueError("question FCD-1 ledger schema mismatch")
    views = _mapping(ledger.get("view_top_50"), "question FCD-1 views")
    if set(views) != set(VIEWS):
        raise ValueError("question FCD-1 view schema mismatch")
    top_receipts = _mapping(ledger.get("view_top_50_sha256"), "question FCD-1 top-50 receipts")
    full_orders = _mapping(ledger.get("view_full_order"), "question FCD-1 full view orders")
    order_receipts = _mapping(ledger.get("view_order_sha256"), "question FCD-1 full-order receipts")
    if set(top_receipts) != set(VIEWS) or set(full_orders) != set(VIEWS) or set(order_receipts) != set(VIEWS):
        raise ValueError("question FCD-1 per-view receipt schema mismatch")
    _digest_value(ledger.get("input_sha256"), "question FCD-1 input receipt")
    _digest_value(ledger.get("authorization_sha256"), "question FCD-1 authorization receipt")
    groups = ledger.get("checkpoint_tie_groups")
    if ledger.get("checkpoint_tie_group_semantics") != "checkpoint_policy_rollup" or not isinstance(ledger.get("fused_top_50"), list) or not isinstance(groups, list) or not groups:
        raise ValueError("question FCD-1 supporting ledger fields are malformed")
    event_hashes: dict[str, str] = {}
    checkpoint_scores: dict[str, float] = {}
    authorization_rows: list[dict[str, str]] = []
    for group in groups:
        group = _mapping(group, "question FCD-1 checkpoint group")
        if set(group) != {"group_id", "checkpoint_sha256", "policy_sha256", "checkpoint_score", "member_count", "chronological_members"}:
            raise ValueError("question FCD-1 checkpoint group schema is malformed")
        checkpoint = _digest_value(group.get("checkpoint_sha256"), "question FCD-1 checkpoint receipt")
        policy = _digest_value(group.get("policy_sha256"), "question FCD-1 policy receipt")
        if group.get("group_id") != "group:" + _digest([checkpoint, policy]):
            raise ValueError("question FCD-1 checkpoint group identity is malformed")
        score = group.get("checkpoint_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("question FCD-1 checkpoint group score is malformed")
        members = group.get("chronological_members")
        if not isinstance(members, list) or not members or group.get("member_count") != len(members):
            raise ValueError("question FCD-1 checkpoint group members are malformed")
        for member in members:
            member = _mapping(member, "question FCD-1 checkpoint member")
            if set(member) != {"source_event_id", "ranking_key_sha256"} or not isinstance(member.get("source_event_id"), str) or not member["source_event_id"]:
                raise ValueError("question FCD-1 checkpoint member schema is malformed")
            event = member["source_event_id"]
            if event in event_hashes:
                raise ValueError("question FCD-1 checkpoint members do not partition events")
            event_hashes[event] = _digest_value(member.get("ranking_key_sha256"), "question FCD-1 checkpoint member receipt")
            checkpoint_scores[event] = float(score)
            authorization_rows.append({"ranking_key_sha256": event_hashes[event], "policy_sha256": policy})
    if _digest(sorted(authorization_rows, key=lambda row: row["ranking_key_sha256"])) != ledger["authorization_sha256"]:
        raise ValueError("question FCD-1 authorization receipt does not replay")
    full_orders_by_view: dict[str, list[str]] = {}
    ranking_orders: dict[str, int] = {}
    for view in VIEWS:
        rows = _ids_rows(views[view], f"{view} top-50")
        if _digest_value(top_receipts[view], f"{view} top-50 receipt") != _digest(rows):
            raise ValueError("question FCD-1 top-50 receipt does not replay")
        full_order = full_orders[view]
        if not isinstance(full_order, list) or len(full_order) != len(event_hashes) or any(not isinstance(item, str) or not item for item in full_order) or len(full_order) != len(set(full_order)) or set(full_order) != set(event_hashes):
            raise ValueError("question FCD-1 full view order is malformed")
        if [row["source_event_id"] for row in rows] != full_order[:EXPECTED_POOL]:
            raise ValueError("question FCD-1 top-50 is not a full-order prefix")
        if _digest_value(order_receipts[view], f"{view} full-order receipt") != _digest([event_hashes[event] for event in full_order]):
            raise ValueError("question FCD-1 full-order receipt does not replay")
        if any(event_hashes[row["source_event_id"]] != row["ranking_key_sha256"] for row in rows):
            raise ValueError("question FCD-1 top-50 event receipt differs from checkpoint groups")
        if rows != sorted(rows, key=lambda row: (-float(row["score"]), row["ranking_key_order"])):
            raise ValueError("question FCD-1 top-50 score ordering does not replay")
        for row in rows:
            event, order = row["source_event_id"], row["ranking_key_order"]
            if event in ranking_orders and ranking_orders[event] != order:
                raise ValueError("question FCD-1 ranking-key order differs across views")
            ranking_orders[event] = order
        if view == "checkpoint_dense" and any(not math.isclose(float(row["score"]), checkpoint_scores[row["source_event_id"]], rel_tol=0.0, abs_tol=1e-15) for row in rows):
            raise ValueError("question FCD-1 checkpoint view does not replay checkpoint groups")
        full_orders_by_view[view] = full_order

    universe_size = len(event_hashes)
    if any(order > universe_size for order in ranking_orders.values()) or len(set(ranking_orders.values())) != len(ranking_orders):
        raise ValueError("question FCD-1 visible ranking-key orders are not injective in range")
    if len(ranking_orders) == universe_size and set(ranking_orders.values()) != set(range(1, universe_size + 1)):
        raise ValueError("question FCD-1 ranking-key orders are not a continuous global order")

    fused = ledger["fused_top_50"]
    fused_fields = {"source_event_id", "ranking_key_sha256", "ranking_key_order", "rank", "final_rrf", "component_ranks", "component_rank_receipts", "contributions"}
    if len(fused) != EXPECTED_POOL:
        raise ValueError("question FCD-1 fused top-50 count is malformed")
    fused_ids: list[str] = []
    for rank, row in enumerate(fused, start=1):
        row = _mapping(row, "question FCD-1 fused row")
        if set(row) != fused_fields or row.get("rank") != rank:
            raise ValueError("question FCD-1 fused row schema is malformed")
        event = row.get("source_event_id")
        if not isinstance(event, str) or event not in event_hashes or row.get("ranking_key_sha256") != event_hashes[event]:
            raise ValueError("question FCD-1 fused identity is malformed")
        order = row.get("ranking_key_order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ValueError("question FCD-1 fused ranking-key order is malformed")
        if event in ranking_orders and ranking_orders[event] != order:
            raise ValueError("question FCD-1 fused ranking-key order differs from views")
        if order > universe_size or (event not in ranking_orders and order in set(ranking_orders.values())):
            raise ValueError("question FCD-1 fused ranking-key order is not injective in range")
        ranking_orders[event] = order
        ranks = _mapping(row.get("component_ranks"), "question FCD-1 component ranks")
        contributions = _mapping(row.get("contributions"), "question FCD-1 contributions")
        receipts = row.get("component_rank_receipts")
        if set(ranks) != set(VIEWS) or set(contributions) != set(VIEWS) or not isinstance(receipts, list) or len(receipts) != len(VIEWS):
            raise ValueError("question FCD-1 fused components are malformed")
        receipts_by_view: dict[str, dict[str, Any]] = {}
        for view_receipt in receipts:
            view_receipt = _mapping(view_receipt, "question FCD-1 component-rank receipt")
            if set(view_receipt) != {"view", "view_order_sha256", "ranking_key_sha256", "rank"} or not isinstance(view_receipt.get("view"), str):
                raise ValueError("question FCD-1 component-rank receipt schema is malformed")
            if view_receipt["view"] in receipts_by_view:
                raise ValueError("question FCD-1 component-rank receipt is duplicated")
            receipts_by_view[view_receipt["view"]] = view_receipt
        if set(receipts_by_view) != set(VIEWS):
            raise ValueError("question FCD-1 component-rank receipt views differ")
        expected_contributions: dict[str, float] = {}
        for view in VIEWS:
            component_rank = ranks[view]
            if not isinstance(component_rank, int) or isinstance(component_rank, bool) or not 1 <= component_rank <= len(full_orders_by_view[view]) or full_orders_by_view[view][component_rank - 1] != event:
                raise ValueError("question FCD-1 component rank does not replay full order")
            if receipts_by_view[view] != {"view": view, "view_order_sha256": order_receipts[view], "ranking_key_sha256": event_hashes[event], "rank": component_rank}:
                raise ValueError("question FCD-1 component-rank receipt does not replay")
            expected = FROZEN_WEIGHTS[view] / (RRF_K + component_rank)
            contribution = contributions[view]
            if isinstance(contribution, bool) or not isinstance(contribution, (int, float)) or not math.isfinite(contribution) or not math.isclose(float(contribution), expected, rel_tol=0.0, abs_tol=1e-15):
                raise ValueError("question FCD-1 contribution does not replay")
            expected_contributions[view] = expected
        final = row.get("final_rrf")
        if isinstance(final, bool) or not isinstance(final, (int, float)) or not math.isfinite(final) or not math.isclose(float(final), math.fsum(expected_contributions.values()), rel_tol=0.0, abs_tol=1e-15):
            raise ValueError("question FCD-1 fused score does not replay")
        fused_ids.append(event)
    if len(set(fused_ids)) != EXPECTED_POOL:
        raise ValueError("question FCD-1 fused identities are not unique")
    if len(ranking_orders) == universe_size and set(ranking_orders.values()) != set(range(1, universe_size + 1)):
        raise ValueError("question FCD-1 visible and fused ranking-key orders are not continuous")
    if fused_ids != [row["source_event_id"] for row in sorted(fused, key=lambda row: (-float(row["final_rrf"]), row["ranking_key_order"]))]:
        raise ValueError("question FCD-1 fused ordering does not replay")
    return ledger


def _view_hashes(ledger: dict[str, Any], view: str) -> list[str]:
    rows = _view_rows(ledger, view)
    return [row["ranking_key_sha256"] for row in rows]


def _view_rows(ledger: dict[str, Any], view: str) -> list[dict[str, Any]]:
    return _ids_rows(_mapping(ledger["view_top_50"], "views").get(view), f"{view} top-50")


def _ids_rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) != EXPECTED_POOL:
        raise ValueError(f"{label} must contain exactly {EXPECTED_POOL} rows")
    result: list[dict[str, Any]] = []
    for rank, row in enumerate(value, start=1):
        row = _mapping(row, label)
        if set(row) != FCD1_VIEW_ROW_FIELDS:
            raise ValueError(f"{label} row schema is malformed")
        if row.get("rank") != rank or not isinstance(row.get("source_event_id"), str) or not row["source_event_id"]:
            raise ValueError(f"{label} row is malformed")
        _digest_value(row.get("ranking_key_sha256"), label)
        order = row.get("ranking_key_order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise ValueError(f"{label} ranking-key order is invalid")
        score = row.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(score):
            raise ValueError(f"{label} score is invalid")
        result.append(row)
    if len({row["source_event_id"] for row in result}) != EXPECTED_POOL or len({row["ranking_key_sha256"] for row in result}) != EXPECTED_POOL:
        raise ValueError(f"{label} identities are not unique")
    return result


def _rrf(views: dict[str, list[dict[str, Any]]]) -> list[str]:
    scores: dict[str, float] = {}
    orders: dict[str, int] = {}
    for view in VIEWS:
        for rank, row in enumerate(views[view], start=1):
            identifier = row["ranking_key_sha256"]
            order = row["ranking_key_order"]
            if identifier in orders and orders[identifier] != order:
                raise ValueError("FCD-1 ranking-key order differs across views")
            orders[identifier] = order
            scores[identifier] = scores.get(identifier, 0.0) + FROZEN_WEIGHTS[view] / (RRF_K + rank)
    return [identifier for identifier, _ in sorted(scores.items(), key=lambda item: (-item[1], orders[item[0]]))[:EXPECTED_POOL]]


def analyze(artifact_path: Path | str, *, expected_artifact_sha256: str, expected_git_head: str, _artifact_bytes: bytes | None = None) -> dict[str, Any]:
    artifact_file = Path(artifact_path).resolve()
    artifact, raw, receipt = _load(artifact_file, expected_artifact_sha256, _artifact_bytes)
    _validate_header(artifact, expected_git_head)
    questions = artifact.get("questions")
    traces = _mapping(artifact.get("product_traces"), "artifact product traces")
    pools = _mapping(artifact.get("source_pool_rankings"), "artifact source pools")
    top10 = _mapping(artifact.get("rankings_top10"), "artifact top-10 streams")
    if not isinstance(questions, list) or len(questions) != EXPECTED_QUESTIONS or set(pools) != {"raw_bm25", "raw_dense", BASELINE_ARM} or set(top10) != set(TOP10_ARMS):
        raise ValueError("artifact question or source-pool schema mismatch")
    item_ids = [question.get("item_id") if isinstance(question, dict) else None for question in questions]
    if any(not isinstance(item, str) or not item for item in item_ids) or len(set(item_ids)) != EXPECTED_QUESTIONS:
        raise ValueError("artifact item identities are malformed")
    if set(traces) != set(item_ids) or any(set(stream) != set(item_ids) for stream in pools.values()) or any(set(stream) != set(item_ids) for stream in top10.values()):
        raise ValueError("artifact trace or stream identities mismatch")
    _validate_stream_receipts(artifact, pools, top10)
    _validate_question_audits(artifact, questions, pools, top10, traces)

    parity: dict[str, Any] = {"arms": {}}
    raw_structural_ok = True
    raw_rankings: dict[str, dict[str, tuple[list[str], list[str]]]] = {arm: {} for arm in RAW_ARMS}
    for arm in RAW_ARMS:
        exact50 = exact10 = set50 = 0
        overlaps: list[float] = []
        for question in questions:
            item = question.get("item_id")
            if not isinstance(item, str) or item not in traces or item not in pools[arm] or item not in top10[arm]:
                raise ValueError("artifact question identities differ")
            external = _ids(_mapping(pools[arm], "source pool").get(item), f"{arm} pool", EXPECTED_POOL)
            external_top10 = _ids(_mapping(top10[arm], "top-10").get(item), f"{arm} top-10", TOP_K)
            if external[:TOP_K] != external_top10:
                raise ValueError("artifact raw top-10 does not match source pool")
            internal_hashes = _view_hashes(_ledger(_mapping(traces[item], "product trace")), arm)
            external_hashes = [hashlib.sha256(identifier.encode()).hexdigest() for identifier in external]
            raw_rankings[arm][item] = (internal_hashes, external_hashes)
            exact50 += internal_hashes == external_hashes
            exact10 += internal_hashes[:TOP_K] == external_hashes[:TOP_K]
            set50 += set(internal_hashes) == set(external_hashes)
            overlaps.append(len(set(internal_hashes) & set(external_hashes)) / EXPECTED_POOL)
        parity["arms"][arm] = {"top50_order_exact_questions": exact50, "top10_order_exact_questions": exact10, "top50_set_exact_questions": set50, "overlap_mean": math.fsum(overlaps) / len(overlaps), "overlap_min": min(overlaps)}
        raw_structural_ok = raw_structural_ok and exact10 == EXPECTED_QUESTIONS and set50 == EXPECTED_QUESTIONS

    semantics: dict[str, Any] = {"status": "not_run_due_to_component_parity"}
    views_by_item: dict[str, dict[str, list[dict[str, Any]]]] = {}
    semantics_ok = False
    if raw_structural_ok:
        exact = 0
        for item in item_ids:
            ledger = _ledger(_mapping(traces[item], "product trace"))
            view_records = {view: _view_rows(ledger, view) for view in VIEWS}
            product_hashes = [hashlib.sha256(identifier.encode()).hexdigest() for identifier in _ids(_mapping(top10[PRODUCT_ARM], "product top-10").get(item), "product top-10", TOP_K)]
            exact += _rrf(view_records)[:TOP_K] == product_hashes
            views_by_item[item] = view_records
        semantics = {"status": "complete", "expected_questions": EXPECTED_QUESTIONS, "top10_order_exact_questions": exact}
        semantics_ok = exact == EXPECTED_QUESTIONS

    base = {
        "schema": SCHEMA, "version": 1, "status": "complete",
        "protocol": {
            "official_exact": "resolved_dialog_ids_plus_unresolved_evidence_item_count",
            "top_k": TOP_K, "pool_size": EXPECTED_POOL, "raw_component_arms": list(RAW_ARMS),
            "raw_parity_gate": "top10_order_and_top50_set_and_recall_delta_zero",
            "fusion_semantics_gate": "top50_truncated_weighted_rrf_matches_frozen_product_top10_order",
            "oracle": "six_view_top50_union_membership", "rrf_k": RRF_K,
            "weights": FROZEN_WEIGHTS, "add_view_order": list(ADD_VIEW_ORDER),
            "p1_min_delta": P1_MIN_DELTA, "p2_min_delta": P2_MIN_DELTA,
            "strict_majority_multiplier": STRICT_MAJORITY_MULTIPLIER,
            "expected_conversations": EXPECTED_CONVERSATIONS,
            "min_recovery_conversations": MIN_RECOVERY_CONVERSATIONS,
        },
        "input_receipt": {"artifact_sha256": receipt, "git_head": expected_git_head, "analyzer_implementation_sha256": _sha(Path(__file__).read_bytes())},
        "raw_component_parity": parity,
        "fusion_semantics_parity": semantics,
    }
    if not raw_structural_ok:
        report = {**base, "verdict": "COMPONENT_PARITY_FAILED", "later_stages": "not_run_due_to_component_parity", "claim_boundary": "No label-derived causal conclusion is made when raw component parity fails."}
        _unchanged(artifact_file, raw)
        return report
    if not semantics_ok:
        report = {**base, "verdict": "FUSION_SEMANTICS_PARITY_FAILED", "later_stages": "not_run_due_to_fusion_semantics_parity", "claim_boundary": "No label-derived causal conclusion is made when frozen fusion semantics do not replay."}
        _unchanged(artifact_file, raw)
        return report

    # The only permitted labels before oracle/ablation now establish the
    # pre-registered raw-control metric parity after both unlabeled replays.
    raw_metric_ok = True
    for arm in RAW_ARMS:
        internal_rows: list[tuple[int, list[str], int, list[str]]] = []
        external_rows: list[tuple[int, list[str], int, list[str]]] = []
        for question in questions:
            item, category = question.get("item_id"), question.get("category")
            if not isinstance(item, str) or not isinstance(category, int) or isinstance(category, bool):
                raise ValueError("artifact question metadata is invalid")
            gold, unresolved = _gold(question)
            gold_hashes = [hashlib.sha256(identifier.encode()).hexdigest() for identifier in gold]
            internal, external = raw_rankings[arm][item]
            internal_rows.append((category, gold_hashes, unresolved, internal))
            external_rows.append((category, gold_hashes, unresolved, external))
        internal_metrics, external_metrics = _aggregate(internal_rows), _aggregate(external_rows)
        delta = _difference(internal_metrics, external_metrics)
        parity["arms"][arm]["internal_official_exact_recall_at_10"] = internal_metrics
        parity["arms"][arm]["external_control_official_exact_recall_at_10"] = external_metrics
        parity["arms"][arm]["official_exact_recall_at_10_delta"] = delta
        raw_metric_ok = raw_metric_ok and all(delta[name] == 0.0 for name in ("overall", "hard", "adversarial"))
    if not raw_metric_ok:
        report = {**base, "verdict": "COMPONENT_PARITY_FAILED", "later_stages": "not_run_due_to_component_parity", "claim_boundary": "No oracle or ablation conclusion is made when raw component metric parity fails."}
        _unchanged(artifact_file, raw)
        return report

    oracle_rows: list[tuple[int, list[str], int, set[str]]] = []
    view_rows: dict[str, list[tuple[int, list[str], int, list[str]]]] = {view: [] for view in VIEWS}
    baseline_rows: list[tuple[int, list[str], int, list[str]]] = []
    product_rows: list[tuple[int, list[str], int, list[str]]] = []
    metadata_by_item: dict[str, tuple[int, list[str], int]] = {}
    recoverable: dict[str, list[int]] = {}
    for question in questions:
        item, category, conversation = question.get("item_id"), question.get("category"), question.get("conversation_id")
        if not isinstance(item, str) or not isinstance(category, int) or isinstance(category, bool) or not isinstance(conversation, str) or not conversation:
            raise ValueError("artifact question metadata is invalid")
        gold, unresolved = _gold(question)
        gold_hashes = [hashlib.sha256(identifier.encode()).hexdigest() for identifier in gold]
        view_records = views_by_item[item]
        views = {view: [row["ranking_key_sha256"] for row in view_records[view]] for view in VIEWS}
        union = set(identifier for view in VIEWS for identifier in views[view])
        product = [hashlib.sha256(identifier.encode()).hexdigest() for identifier in _ids(_mapping(top10[PRODUCT_ARM], "product top-10").get(item), "product top-10", TOP_K)]
        baseline = [hashlib.sha256(identifier.encode()).hexdigest() for identifier in _ids(_mapping(top10[BASELINE_ARM], "baseline top-10").get(item), "baseline top-10", TOP_K)]
        oracle_rows.append((category, gold_hashes, unresolved, union)); product_rows.append((category, gold_hashes, unresolved, product)); baseline_rows.append((category, gold_hashes, unresolved, baseline))
        for view in VIEWS: view_rows[view].append((category, gold_hashes, unresolved, views[view]))
        metadata_by_item[item] = (category, gold_hashes, unresolved)
        bucket = recoverable.setdefault(_sha(conversation.encode()), [0, 0])
        # The frozen official denominator preserves evidence multiplicity, so
        # recovery counts do too.  One partially recovered multi-evidence item
        # must not be promoted to a wholly recovered question.
        for identifier in gold_hashes:
            if identifier not in product:
                bucket[0 if identifier in union else 1] += 1
    oracle, baseline, product = _aggregate_union(oracle_rows), _aggregate(baseline_rows), _aggregate(product_rows)
    deltas = {"oracle_minus_raw_fusion": _difference(oracle, baseline), "product_minus_raw_fusion": _difference(product, baseline)}
    p1 = _p1_pass(deltas["oracle_minus_raw_fusion"])
    p2 = _p2_pass(deltas["product_minus_raw_fusion"])
    ablations = {"add_view": {}, "leave_one_out": {}}
    for index, view in enumerate(ADD_VIEW_ORDER, start=1):
        ablations["add_view"][view] = _aggregate([
            (*metadata_by_item[item], _rrf({name: records if name in ADD_VIEW_ORDER[:index] else [] for name, records in views_by_item[item].items()}))
            for item in item_ids
        ])
        ablations["leave_one_out"][view] = _aggregate([
            (*metadata_by_item[item], _rrf({name: records if name != view else [] for name, records in views_by_item[item].items()}))
            for item in item_ids
        ])
    if len(recoverable) != EXPECTED_CONVERSATIONS:
        raise ValueError("artifact conversation denominator differs")
    recovered_total = sum(item[0] for item in recoverable.values()); missed_total = recovered_total + sum(item[1] for item in recoverable.values())
    strict_majority = recovered_total * STRICT_MAJORITY_MULTIPLIER > missed_total
    concentration_count = sum(recovered >= nonrecoverable for recovered, nonrecoverable in recoverable.values())
    concentration = concentration_count >= MIN_RECOVERY_CONVERSATIONS
    fusion = p1 and p2 and strict_majority and concentration
    report = {**base, "verdict": "FUSION_SUPPORTED" if fusion else "REPRESENTATION_SUPPORTED", "stages": {"oracle": oracle, "raw_fusion": baseline, "product": product, "deltas": deltas, "view_metrics": {view: _aggregate(values) for view, values in view_rows.items()}, "ablations": ablations, "gates": {"p1": p1, "p2": p2, "strict_majority": strict_majority, "recovery_conversation_count": concentration_count, "recovery_conversation_pass": concentration}, "hashed_conversation_recovery": {key: {"recoverable": value[0], "nonrecoverable": value[1]} for key, value in sorted(recoverable.items())}}, "claim_boundary": "Frozen ranking analysis only; it does not establish causal behavior outside this artifact."}
    _unchanged(artifact_file, raw)
    return report


def _safe_output(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or key.casefold() in FORBIDDEN:
                raise ValueError("report contains forbidden field")
            _safe_output(child)
    elif isinstance(value, list):
        for child in value: _safe_output(child)
    elif isinstance(value, str) and any(word in value.casefold() for word in FORBIDDEN):
        raise ValueError("report contains forbidden content")


def atomic_json(output: Path | str, report: dict[str, Any], *, artifact_path: Path | str, expected_artifact_bytes: bytes) -> None:
    output, artifact = Path(output).resolve(), Path(artifact_path).resolve()
    if output == artifact or output == ROOT or ROOT in output.parents:
        raise ValueError("output must be external and distinct from artifact")
    if report.get("schema") != SCHEMA or report.get("status") != "complete":
        raise ValueError("report schema or status mismatch")
    report_receipt = _mapping(report.get("input_receipt"), "report input receipt")
    if _sha(expected_artifact_bytes) != _digest_value(report_receipt.get("artifact_sha256"), "report artifact receipt"):
        raise ValueError("report artifact receipt mismatch")
    if _digest_value(report_receipt.get("analyzer_implementation_sha256"), "report analyzer receipt") != _sha(Path(__file__).read_bytes()):
        raise ValueError("report analyzer receipt mismatch")
    _safe_output(report); output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="wb", dir=output.parent, prefix=f".{output.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(_canonical(report)); handle.flush(); os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True); raise
    try:
        _unchanged(artifact, expected_artifact_bytes); os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True); raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True); parser.add_argument("--expected-artifact-sha256", required=True); parser.add_argument("--expected-git-head", required=True); parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        artifact = Path(args.artifact).resolve(); raw = artifact.read_bytes()
        report = analyze(artifact, expected_artifact_sha256=args.expected_artifact_sha256, expected_git_head=args.expected_git_head, _artifact_bytes=raw)
        atomic_json(args.output, report, artifact_path=artifact, expected_artifact_bytes=raw)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, sort_keys=True), file=sys.stderr); return 2
    print(json.dumps({"status": report["status"], "verdict": report["verdict"]}, sort_keys=True)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
