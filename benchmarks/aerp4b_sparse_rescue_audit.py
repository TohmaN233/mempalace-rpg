"""Train-only, nested-LOCO audit for sparse Raw rescues over the P5 default.

This is intentionally an audit, not a product router.  It accepts only the
already opaque AERP-4 *train* receipts, rebuilds the two static rankings from
the label-free sanitized source, and evaluates a small frozen policy family by
strict nested group leave-one-conversation-out (LOCO).  No command-line option
or function argument accepts a dev ranking or dev label artifact.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from benchmarks import aerp4_locomo_paired_receipts as paired
from benchmarks import aerp4_raw_anchored_gate as gate
from mempalace_rpg import retrieval


SCHEMA = "aerp4b-sparse-rescue-train-audit-v1"
TOP_K = 10
FAMILY_ORDER = ("F0_A", "F1_A_AND_CHURN", "F2_A_AND_DISP", "F3_A_AND_INTRUSION")
RAW_WEIGHTS = {
    "raw_bm25": 2.0, "observation_bm25": 0.0, "raw_dense": 1.0,
    "observation_dense": 0.0, "checkpoint_dense": 0.0, "combo_dense": 0.0,
}
P5_WEIGHTS = {
    "raw_bm25": 2.0, "observation_bm25": 0.5, "raw_dense": 1.0,
    "observation_dense": 2.0, "checkpoint_dense": 0.0, "combo_dense": 1.0,
}
_FAMILY_PRIORITY = {name: index for index, name in enumerate(FAMILY_ORDER)}
_HEX = set("0123456789abcdef")


class UnsupportedFamilyError(ValueError):
    """A frozen policy family has no threshold satisfying sparse-support rules."""


def _sha(value: Any) -> str:
    return gate._sha(value)


def _float(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite numeric")
    return result


def _quantile_linear(values: Iterable[float], q: float) -> float:
    ordered = sorted(_float(value, "quantile value") for value in values)
    if not ordered:
        raise ValueError("quantile input is empty")
    if not 0.0 <= q <= 1.0:
        raise ValueError("quantile q is outside [0,1]")
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _unique_sorted(values: Iterable[float]) -> tuple[float, ...]:
    return tuple(sorted(set(_float(value, "grid value") for value in values)))


def _quantile_grid(values: Iterable[float], count: int) -> tuple[float, ...]:
    if count < 2:
        raise ValueError("grid count must be at least two")
    source = tuple(values)
    return _unique_sorted(_quantile_linear(source, index / (count - 1)) for index in range(count))


def _threshold_receipt(thresholds: Sequence[float]) -> list[str]:
    return [float(value).hex() for value in thresholds]


def _route_gates(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("route gate has no rows")
    groups = sorted({str(row["group_token"]) for row in rows})
    count_by_group = {group: 0 for group in groups}
    for row in rows:
        if row["route"] == "raw":
            count_by_group[str(row["group_token"])] += 1
        elif row["route"] != "p5":
            raise ValueError("route is malformed")
    overrides = sum(count_by_group.values())
    rate = overrides / len(rows)
    # ``None`` is a deliberate canonical-JSON value.  ``math.inf`` would make
    # a STOP receipt unpublishable under the gate's allow_nan=False contract.
    max_share = max(count_by_group.values()) / overrides if overrides else None
    result = {
        "override_count": overrides,
        "override_rate": rate,
        "groups": len(groups),
        "groups_covered": sum(value > 0 for value in count_by_group.values()),
        "max_group_share": max_share,
        "raw_count_by_group": count_by_group,
    }
    result["pass"] = (
        overrides >= 30
        and rate <= 0.25
        and result["groups_covered"] == len(groups)
        and max_share is not None
        and max_share <= 0.50
    )
    return result


def _metrics(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]], arm: str) -> dict[str, Any]:
    values: list[float] = []
    by_group: dict[str, list[float]] = {}
    for row in rows:
        label = labels.get(str(row["item_token"]))
        if label is None:
            raise ValueError("route row is absent from train labels")
        top = row[f"{arm}_top10"] if arm in {"raw", "p5"} else row["selected_top10"]
        value = gate._score(top, label)
        values.append(value)
        by_group.setdefault(str(row["group_token"]), []).append(value)
    if not values:
        raise ValueError("metric has no rows")
    group_means = {group: sum(group_values) / len(group_values) for group, group_values in sorted(by_group.items())}
    return {
        "question_macro_recall_at_10": sum(values) / len(values),
        "conversation_macro_recall_at_10": sum(group_means.values()) / len(group_means),
        "question_count": len(values),
        "conversation_count": len(group_means),
        "group_means": group_means,
    }


def _ranked_tokens(item: Mapping[str, Any], weights: Mapping[str, float]) -> list[str]:
    source = item["rank_source"]
    orders = source["view_token_orders"]
    views = tuple(paired._VIEWS)
    universe = list(orders[views[0]])
    if len(universe) < TOP_K:
        raise ValueError("sanitized train item has fewer than ten candidates")
    ranks = {view: {token: index for index, token in enumerate(orders[view], start=1)} for view in views}
    totals = retrieval._rrf_totals(ranks, weights, 60)
    ordered = retrieval._ordered(totals, {token: token for token in universe})
    if len(ordered) != len(universe) or len(ordered) != len(set(ordered)):
        raise ValueError("reconstructed ranking is malformed")
    evidence = source["evidence_token_by_ranking_token"]
    return [str(evidence[token]) for token in ordered]


def _features(item: Mapping[str, Any], freeze: Mapping[str, Any]) -> dict[str, Any]:
    raw_order = _ranked_tokens(item, RAW_WEIGHTS)
    p5_order = _ranked_tokens(item, P5_WEIGHTS)
    if raw_order[:TOP_K] != list(freeze["raw_top10"]) or p5_order[:TOP_K] != list(freeze["p5_top10"]):
        raise ValueError("sanitized reconstruction does not match frozen paired Top10")
    raw_top, p5_top = raw_order[:TOP_K], p5_order[:TOP_K]
    if len(raw_top) != TOP_K or len(p5_top) != TOP_K or len(set(raw_top)) != TOP_K or len(set(p5_top)) != TOP_K:
        raise ValueError("paired Top10 is not strict and unique")
    raw_rank = {token: index for index, token in enumerate(raw_order, start=1)}
    p5_rank = {token: index for index, token in enumerate(p5_order, start=1)}
    union = sorted(set(raw_top) | set(p5_top))
    n = len(raw_order)
    if n != len(p5_order) or n <= 0 or set(raw_order) != set(p5_order):
        raise ValueError("raw and P5 reconstructed universes differ")
    if raw_order != list(freeze["authorized_tokens"]):
        raise ValueError("sanitized reconstructed universe does not match frozen authorization receipt")
    a = gate._float_receipt(freeze["A_hex"], "frozen A_hex")
    churn = 1.0 - len(set(raw_top) & set(p5_top)) / TOP_K
    displacement = sum(abs(raw_rank[token] - p5_rank[token]) for token in union) / (n * len(union))
    intrusion = sum(raw_rank[token] for token in p5_top) / (TOP_K * n)
    return {
        "item_token": freeze["item_token"], "group_token": freeze["group_token"], "campaign_token": freeze["campaign_token"],
        "A": a, "churn": churn, "displacement": displacement, "intrusion": intrusion,
        "raw_top10": raw_top, "p5_top10": p5_top,
    }


def _compact_train_row(raw: Any) -> dict[str, Any]:
    """Validate one JSONL candidate receipt and retain only rank-source fields."""
    if not isinstance(raw, Mapping) or set(raw) != {
        "item_token", "group_token", "campaign_token", "query_sha256", "input_sha256",
        "encoder_identity", "view_digests", "candidates",
    }:
        raise ValueError("sanitized train shard row schema mismatch")
    for key in ("item_token", "group_token", "campaign_token", "query_sha256", "input_sha256"):
        paired._hex_token(raw.get(key), f"sanitized train {key}")
    if not isinstance(raw.get("encoder_identity"), str) or not raw["encoder_identity"]:
        raise ValueError("sanitized train encoder identity is malformed")
    views = tuple(paired._VIEWS)
    digests = raw.get("view_digests")
    candidates = raw.get("candidates")
    if not isinstance(digests, Mapping) or set(digests) != set(views) or not isinstance(candidates, list) or not candidates:
        raise ValueError("sanitized train rank source is malformed")
    for view in views:
        paired._hex_token(digests[view], f"sanitized train {view} digest")
    tokens: set[str] = set()
    evidence: dict[str, str] = {}
    ranks: dict[str, dict[str, int]] = {view: {} for view in views}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {"evidence_token", "ranking_token", "ranks"}:
            raise ValueError("sanitized train candidate schema mismatch")
        ranking = paired._hex_token(candidate.get("ranking_token"), "sanitized ranking token")
        evidence_token = paired._hex_token(candidate.get("evidence_token"), "sanitized evidence token")
        if ranking in tokens:
            raise ValueError("sanitized train candidates repeat ranking token")
        token_ranks = candidate.get("ranks")
        if not isinstance(token_ranks, Mapping) or set(token_ranks) != set(views):
            raise ValueError("sanitized train candidate ranks schema mismatch")
        tokens.add(ranking); evidence[ranking] = evidence_token
        for view in views:
            rank = token_ranks[view]
            if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
                raise ValueError("sanitized train candidate rank is malformed")
            ranks[view][ranking] = rank
    size = len(tokens)
    orders: dict[str, list[str]] = {}
    for view in views:
        values = ranks[view]
        if set(values.values()) != set(range(1, size + 1)):
            raise ValueError("sanitized train view ranks are not a complete permutation")
        orders[view] = [token for token, _rank in sorted(values.items(), key=lambda pair: pair[1])]
    return {
        "item_token": raw["item_token"], "group_token": raw["group_token"], "campaign_token": raw["campaign_token"],
        "rank_source": {"query_sha256": raw["query_sha256"], "input_sha256": raw["input_sha256"],
                        "encoder_identity": raw["encoder_identity"], "view_digests": dict(digests),
                        "view_token_orders": orders, "evidence_token_by_ranking_token": evidence},
    }


def iter_sanitized_train_shard(path: Path) -> Iterable[dict[str, Any]]:
    """Stream the sole permitted candidate source; never opens a dev shard."""
    with path.open("r", encoding="utf-8") as stream:
        for number, line in enumerate(stream, start=1):
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"sanitized train shard line {number} is invalid JSON") from exc
            yield _compact_train_row(raw)


def prepare_train_rows(
    study: Mapping[str, Any], sanitized_train: Iterable[Mapping[str, Any]], train_rankings: Mapping[str, Any], train_labels: Mapping[str, Any],
    *, train_shard_receipt: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate and join the four opaque train inputs without parsing dev content."""
    gate._validate_study(study)
    rankings = gate._parse_ranking_freeze(train_rankings, study, "train")
    labels = gate._parse_labels(train_labels, study, "train")
    frozen = {str(row["item_token"]): row for row in rankings}
    if set(frozen) != set(labels):
        raise ValueError("train ranking/label membership differs")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidate_count = 0
    for item in sanitized_train:
        token = str(item.get("item_token")) if isinstance(item, Mapping) else ""
        if token in seen:
            raise ValueError("sanitized train shard repeats item token")
        if token not in frozen:
            raise ValueError("sanitized train shard has an unfrozen item")
        seen.add(token)
        _validate_identity_receipts(item, frozen[token])
        source = item.get("rank_source")
        if not isinstance(source, Mapping) or not isinstance(source.get("view_token_orders"), Mapping):
            raise ValueError("sanitized train rank source is malformed after validation")
        candidate_count += len(source["view_token_orders"]["raw_bm25"])
        # Feature extraction is deliberately immediate: this keeps at most one
        # parsed candidate universe resident while traversing the JSONL shard.
        rows.append(_features(item, frozen[token]))
    if seen != set(frozen):
        raise ValueError("train sanitized/ranking membership differs")
    if train_shard_receipt is not None:
        if len(rows) != train_shard_receipt["item_count"] or candidate_count != train_shard_receipt["candidate_count"]:
            raise ValueError("sanitized train shard count receipt mismatch")
    rows.sort(key=lambda row: row["item_token"])
    if len({row["group_token"] for row in rows}) != 5:
        raise ValueError("AERP4b requires exactly five frozen train groups")
    return rows, labels


def _validate_identity_receipts(item: Mapping[str, Any], freeze: Mapping[str, Any]) -> None:
    """Bind every identity receipt shared by the source shard and frozen ranking."""
    source = item.get("rank_source")
    if not isinstance(source, Mapping):
        raise ValueError("sanitized train rank source is malformed for identity binding")
    for key in ("item_token", "group_token", "campaign_token"):
        if item.get(key) != freeze.get(key):
            raise ValueError(f"sanitized/frozen {key} receipt mismatch")
    for key in ("query_sha256", "input_sha256"):
        if source.get(key) != freeze.get(key):
            raise ValueError(f"sanitized/frozen {key} receipt mismatch")


def validate_sanitize_manifest(manifest: Mapping[str, Any], study: Mapping[str, Any]) -> dict[str, Any]:
    """Bind the train JSONL to safe sanitize-manifest metadata only.

    The manifest necessarily records the sibling dev shard's *digest receipt*.
    This validator deliberately never opens that path or parses its contents.
    """
    expected = {
        "schema", "status", "artifact_sha256", "artifact_bytes", "groups", "metrics", "publication",
        "question_random_split", "shards", "source_artifact_git_receipts",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != expected:
        raise ValueError("sanitize manifest schema mismatch")
    if manifest.get("schema") != "aerp4-locomo-stream-rank-manifest-v1" or manifest.get("status") != "complete":
        raise ValueError("sanitize manifest is not complete v1")
    if manifest.get("question_random_split") is not False:
        raise ValueError("sanitize manifest split protocol mismatch")
    artifact_sha = paired._hex_token(manifest.get("artifact_sha256"), "sanitize manifest source artifact sha256")
    if isinstance(manifest.get("artifact_bytes"), bool) or not isinstance(manifest.get("artifact_bytes"), int) or manifest["artifact_bytes"] <= 0:
        raise ValueError("sanitize manifest source artifact bytes are malformed")
    producer = study.get("producer") if isinstance(study, Mapping) else None
    if not isinstance(producer, Mapping):
        raise ValueError("study producer is malformed")
    if _sha(manifest) != producer.get("artifact_sha256"):
        raise ValueError("sanitize manifest/study producer artifact digest mismatch")
    gate._validate_publication(manifest.get("publication"), expected_input_sha256s=[artifact_sha])
    publication_state = manifest["publication"]["analyzer_git_state"]
    if publication_state["git_head"] != producer.get("git_head") or publication_state["git_tree"] != producer.get("git_tree"):
        raise ValueError("sanitize publication/study producer git identity mismatch")
    _validate_source_git_receipts(manifest.get("source_artifact_git_receipts"))
    shards = manifest.get("shards")
    if not isinstance(shards, list):
        raise ValueError("sanitize manifest shards are malformed")
    train = [row for row in shards if isinstance(row, Mapping) and row.get("partition") == "train"]
    if len(train) != 1:
        raise ValueError("sanitize manifest must contain exactly one train shard receipt")
    receipt = train[0]
    fields = {"partition", "path", "path_sha256", "sha256", "bytes", "item_count", "candidate_count", "crosswalk_sha256"}
    if set(receipt) != fields or receipt["path"] != "train.jsonl":
        raise ValueError("sanitize train shard receipt schema mismatch")
    for key in ("path_sha256", "sha256", "crosswalk_sha256"):
        paired._hex_token(receipt.get(key), f"sanitize train {key}")
    for key in ("bytes", "item_count", "candidate_count"):
        if isinstance(receipt.get(key), bool) or not isinstance(receipt.get(key), int) or receipt[key] <= 0:
            raise ValueError(f"sanitize train {key} is malformed")
    expected_crosswalk = gate._partition_spec(study, "train")["crosswalk_sha256"]
    if receipt["crosswalk_sha256"] != expected_crosswalk:
        raise ValueError("sanitize train manifest/study crosswalk mismatch")
    return dict(receipt)


def _validate_source_git_receipts(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {"git_state_before", "git_state_after"}:
        raise ValueError("sanitize source git receipts schema mismatch")
    before, after = value["git_state_before"], value["git_state_after"]
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        raise ValueError("sanitize source git state is malformed")
    for state in (before, after):
        expected = {"git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes"}
        if set(state) != expected:
            raise ValueError("sanitize source git state schema mismatch")
        for key, length in (("git_head", 40), ("git_tree", 40), ("worktree_status_sha256", 64), ("commit_diff_sha256", 64)):
            token = state[key]
            if not isinstance(token, str) or len(token) != length or any(char not in _HEX for char in token):
                raise ValueError(f"sanitize source {key} is malformed")
        if state["git_dirty"] is not False:
            raise ValueError("sanitize source git state is dirty")
        if isinstance(state["commit_diff_bytes"], bool) or not isinstance(state["commit_diff_bytes"], int) or state["commit_diff_bytes"] < 0:
            raise ValueError("sanitize source commit diff bytes are malformed")
    if before != after:
        raise ValueError("sanitize source git state drift")


def _grid(rows: Sequence[Mapping[str, Any]], family: str) -> dict[str, tuple[float, ...]]:
    a = _quantile_grid((float(row["A"]) for row in rows), 31)
    if family == "F0_A":
        return {"A": a}
    feature = {"F1_A_AND_CHURN": "churn", "F2_A_AND_DISP": "displacement", "F3_A_AND_INTRUSION": "intrusion"}.get(family)
    if feature is None:
        raise ValueError("unknown family")
    b = _unique_sorted(float(row[feature]) for row in rows) if family == "F1_A_AND_CHURN" else _quantile_grid((float(row[feature]) for row in rows), 21)
    return {"A": a, feature: b}


def _route(row: Mapping[str, Any], family: str, thresholds: Sequence[float]) -> str:
    a = float(thresholds[0])
    raw = float(row["A"]) < a
    if family == "F0_A":
        return "raw" if raw else "p5"
    field = {"F1_A_AND_CHURN": "churn", "F2_A_AND_DISP": "displacement", "F3_A_AND_INTRUSION": "intrusion"}.get(family)
    if field is None or len(thresholds) != 2:
        raise ValueError("family thresholds are malformed")
    return "raw" if raw and float(row[field]) >= float(thresholds[1]) else "p5"


def _routed(rows: Sequence[Mapping[str, Any]], family: str, thresholds: Sequence[float]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        copy = dict(row)
        copy["route"] = _route(row, family, thresholds)
        copy["selected_top10"] = list(copy[f"{copy['route']}_top10"])
        result.append(copy)
    return result


def _threshold_candidates(rows: Sequence[Mapping[str, Any]], family: str) -> list[tuple[float, ...]]:
    grid = _grid(rows, family)
    if family == "F0_A":
        return [(a,) for a in grid["A"]]
    field = next(key for key in grid if key != "A")
    return [(a, b) for a in grid["A"] for b in grid[field]]


def _fit(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]], family: str) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    for thresholds in _threshold_candidates(rows, family):
        routed = _routed(rows, family, thresholds)
        gates = _route_gates(routed)
        if not gates["pass"]:
            continue
        metrics = _metrics(routed, labels, "selected")
        candidates.append({"thresholds": thresholds, "metrics": metrics, "routes": routed, "gates": gates})
    if not candidates:
        raise UnsupportedFamilyError(f"{family} has no supported threshold on fitting partition")
    best = max(candidates, key=lambda candidate: (
        candidate["metrics"]["conversation_macro_recall_at_10"],
        candidate["metrics"]["question_macro_recall_at_10"],
        -candidate["gates"]["override_count"],
        tuple(candidate["thresholds"]),
    ))
    grid = _grid(rows, family)
    return {
        "family": family,
        "thresholds": tuple(best["thresholds"]),
        "metrics": best["metrics"], "gates": best["gates"], "routes": best["routes"],
        "grid": {key: _threshold_receipt(values) for key, values in grid.items()},
        "candidate_count": len(_threshold_candidates(rows, family)), "supported_candidate_count": len(candidates),
    }


def _family_inner_oof(
    outer_train: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]], family: str
) -> dict[str, Any]:
    groups = sorted({str(row["group_token"]) for row in outer_train})
    if len(groups) != 4:
        raise ValueError("outer train must have four groups")
    oof: list[dict[str, Any]] = []
    folds: list[dict[str, Any]] = []
    for held in groups:
        fitting = [row for row in outer_train if row["group_token"] != held]
        held_rows = [row for row in outer_train if row["group_token"] == held]
        fitted = _fit(fitting, labels, family)
        applied = _routed(held_rows, family, fitted["thresholds"])
        oof.extend(applied)
        folds.append({
            "held_group_token": held,
            "fit_membership_sha256": _sha(sorted(row["item_token"] for row in fitting)),
            "held_membership_sha256": _sha(sorted(row["item_token"] for row in held_rows)),
            "grid": fitted["grid"], "candidate_count": fitted["candidate_count"],
            "supported_candidate_count": fitted["supported_candidate_count"],
            "winner_thresholds": _threshold_receipt(fitted["thresholds"]), "winner_gates": fitted["gates"],
        })
    gates = _route_gates(oof)
    if not gates["pass"]:
        raise UnsupportedFamilyError(f"{family} inner OOF fails sparse-route support gates")
    metrics = _metrics(oof, labels, "selected")
    return {"family": family, "metrics": metrics, "gates": gates, "routes": oof, "inner_folds": folds}


def _choose_family(results: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    if not results:
        raise ValueError("no family survived nested train protocol")
    return max(results, key=lambda result: (
        result["metrics"]["conversation_macro_recall_at_10"],
        result["metrics"]["question_macro_recall_at_10"],
        -result["gates"]["override_count"],
        -_FAMILY_PRIORITY[str(result["family"])],
    ))


def audit_train_only(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Run the frozen five-by-four-by-three-group nested LOCO protocol."""
    all_rows = [dict(row) for row in rows]
    groups = sorted({str(row["group_token"]) for row in all_rows})
    if len(groups) != 5:
        raise ValueError("AERP4b outer LOCO requires five groups")
    if {str(row["item_token"]) for row in all_rows} != set(labels):
        raise ValueError("AERP4b rows/labels differ")
    outer_routes: list[dict[str, Any]] = []
    outer: list[dict[str, Any]] = []
    for held in groups:
        outer_train = [row for row in all_rows if row["group_token"] != held]
        held_rows = [row for row in all_rows if row["group_token"] == held]
        family_results: list[dict[str, Any]] = []
        rejected: dict[str, str] = {}
        for family in FAMILY_ORDER:
            try:
                family_results.append(_family_inner_oof(outer_train, labels, family))
            except UnsupportedFamilyError as exc:
                rejected[family] = str(exc)
        winner = _choose_family(family_results)
        refit = _fit(outer_train, labels, str(winner["family"]))
        held_route = _routed(held_rows, str(winner["family"]), refit["thresholds"])
        outer_routes.extend(held_route)
        outer.append({
            "held_group_token": held,
            "outer_train_membership_sha256": _sha(sorted(row["item_token"] for row in outer_train)),
            "outer_held_membership_sha256": _sha(sorted(row["item_token"] for row in held_rows)),
            "family_scores": [{"family": result["family"], "metrics": result["metrics"], "gates": result["gates"]} for result in family_results],
            "rejected_families": rejected,
            "winner_family": winner["family"],
            "winner_inner_oof_metrics": winner["metrics"], "winner_inner_oof_gates": winner["gates"],
            "winner_inner_folds": winner["inner_folds"],
            "outer_refit_grid": refit["grid"], "outer_refit_candidate_count": refit["candidate_count"],
            "outer_refit_supported_candidate_count": refit["supported_candidate_count"],
            "outer_refit_thresholds": _threshold_receipt(refit["thresholds"]), "outer_refit_gates": refit["gates"],
            "outer_held_route_gates": _route_gates(held_route),
        })
    sparse = _metrics(outer_routes, labels, "selected")
    p5 = _metrics(all_rows, labels, "p5")
    gates = _route_gates(outer_routes)
    performance_pass = (
        sparse["question_macro_recall_at_10"] > p5["question_macro_recall_at_10"]
        and sparse["conversation_macro_recall_at_10"] > p5["conversation_macro_recall_at_10"]
    )
    status = "complete" if gates["pass"] and performance_pass else (
        "STOP_SUPPORT_GATE" if not gates["pass"] else "STOP_PERFORMANCE_GATE"
    )
    return {
        "schema": SCHEMA, "status": status, "partition": "train", "label_access": "train_only",
        "confirmation_claim": False, "dev_eligible": False,
        "claim_boundary": "burned train-only development audit; not a confirmation result and never eligible to unlock dev evaluation",
        "protocol": {
            "outer": "strict_5_group_LOCO", "inner": "strict_4_group_LOCO", "fit": "3_group_supported_grid_search",
            "family_order": list(FAMILY_ORDER), "family_tie_break": "higher_conversation_macro,higher_question_macro,fewer_overrides,then_lower_frozen_family_priority",
            "threshold_tie_break": "higher_conversation_macro,higher_question_macro,fewer_overrides,then_lexicographically_larger_threshold_tuple",
            "support_gates": {"overrides_at_least": 30, "override_rate_at_most": 0.25, "all_groups_covered": True, "max_group_share_at_most": 0.50},
            "performance_gate": "outer_LOCO_strictly_exceeds_static_P5_in_both_question_and_conversation_macro",
        },
        "memberships": {"item_tokens_sha256": _sha(sorted(row["item_token"] for row in all_rows)), "group_tokens": groups, "group_tokens_sha256": _sha(groups)},
        "outer_folds": outer,
        "outer_routes": [{"item_token": row["item_token"], "group_token": row["group_token"], "route": row["route"], "family": next(fold["winner_family"] for fold in outer if fold["held_group_token"] == row["group_token"])} for row in sorted(outer_routes, key=lambda row: row["item_token"])],
        "outer_routes_sha256": _sha([{"item_token": row["item_token"], "group_token": row["group_token"], "route": row["route"]} for row in sorted(outer_routes, key=lambda row: row["item_token"])]),
        "metrics": {"sparse_rescue": sparse, "p5": p5}, "route_gates": gates,
        "performance_gate_pass": performance_pass,
    }


def run_train_audit(
    *, study: Mapping[str, Any], sanitized_manifest: Mapping[str, Any], sanitized_train: Iterable[Mapping[str, Any]], train_rankings: Mapping[str, Any], train_labels: Mapping[str, Any], sanitized_train_sha256: str | None = None
) -> dict[str, Any]:
    receipt = validate_sanitize_manifest(sanitized_manifest, study)
    if sanitized_train_sha256 is not None and sanitized_train_sha256 != receipt["sha256"]:
        raise ValueError("bound train shard digest disagrees with sanitize manifest")
    rows, labels = prepare_train_rows(study, sanitized_train, train_rankings, train_labels, train_shard_receipt=receipt)
    report = audit_train_only(rows, labels)
    return report | {
        "input_receipts": {
            "study_sha256": _sha(study), "sanitized_manifest_sha256": _sha(sanitized_manifest),
            "sanitized_train_source_sha256": sanitized_train_sha256 or receipt["sha256"],
            "train_rankings_sha256": _sha(train_rankings), "train_labels_sha256": _sha(train_labels),
        }
    }


class BoundTrainShard:
    """A byte-bound, JSONL-only train input compatible with gate publication."""
    def __init__(self, path: Path, expected_sha256: str) -> None:
        self.path = path.resolve()
        self.expected_sha256 = paired._hex_token(expected_sha256, "expected train shard sha256")

    @staticmethod
    def _digest(path: Path) -> str:
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
        return hasher.hexdigest()

    @classmethod
    def load(cls, path: Path | str, expected_sha256: str) -> "BoundTrainShard":
        result = cls(Path(path), expected_sha256)
        if not result.path.is_file() or result._digest(result.path) != result.expected_sha256:
            raise ValueError("sanitized train shard digest mismatch")
        return result

    def verify_unchanged(self) -> None:
        if self._digest(self.path) != self.expected_sha256:
            raise RuntimeError("sanitized train shard changed during audit")

    def verify_manifest_receipt(self, receipt: Mapping[str, Any]) -> None:
        if self.expected_sha256 != receipt["sha256"] or self.path.name != receipt["path"]:
            raise ValueError("bound train shard does not match sanitize manifest receipt")
        if self.path.stat().st_size != receipt["bytes"]:
            raise ValueError("bound train shard byte count does not match sanitize manifest receipt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("study", "sanitize-manifest", "rankings", "labels"):
        parser.add_argument(f"--{name}", required=True)
        parser.add_argument(f"--{name}-sha256", required=True)
    parser.add_argument("--train-shard", required=True)
    parser.add_argument("--train-shard-sha256", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    bound = {
        name: gate.BoundInput.load(getattr(args, name), getattr(args, f"{name}_sha256"))
        for name in ("study", "sanitize_manifest", "rankings", "labels")
    }
    train_shard = BoundTrainShard.load(args.train_shard, args.train_shard_sha256)
    manifest = bound["sanitize_manifest"].json("sanitize manifest")
    receipt = validate_sanitize_manifest(manifest, bound["study"].json("study"))
    train_shard.verify_manifest_receipt(receipt)
    report = run_train_audit(
        study=bound["study"].json("study"), sanitized_manifest=manifest, sanitized_train=iter_sanitized_train_shard(train_shard.path),
        train_rankings=bound["rankings"].json("train rankings"), train_labels=bound["labels"].json("train labels"),
        sanitized_train_sha256=train_shard.expected_sha256,
    )
    gate.publish_bound_report(
        report=report, output=args.output, inputs=[bound[name] for name in ("study", "sanitize_manifest", "rankings", "labels")] + [train_shard],
        repo=args.repo, implementation_path=Path(__file__),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
