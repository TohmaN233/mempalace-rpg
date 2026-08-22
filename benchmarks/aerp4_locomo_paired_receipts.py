"""Label-free, source-separated LoCoMo rank receipts for the AERP-4 gate.

This adapter intentionally accepts only the producer artifact and an already
frozen study. It validates the producer's FCD-1 ledger before replacing every
event identity with an opaque receipt, so the ranking producer has no route to
official evidence or question content.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Mapping

from benchmarks import aerp3_fcd2_causal_ablation as fcd2
from benchmarks import aerp4_raw_anchored_gate as gate
from benchmarks import aerp4_raw_anchored_gate_prefreeze as prefreeze
from mempalace_rpg import retrieval
from mempalace_rpg.retrieval import RawAnchoredP5Policy


SCHEMA = "aerp4-locomo-sanitized-rank-source-v1"
PAIR_SCHEMA = "aerp4-raw-anchored-paired-policy-prefreeze-v1"
_VIEWS = tuple(prefreeze.VIEWS)
_HEX = set("0123456789abcdef")


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    ).hexdigest()


def _token(domain: str, value: str) -> str:
    """Create a domain-separated opaque identifier from a source identity."""
    if not isinstance(value, str) or not value:
        raise ValueError("opaque-token source must be a non-empty string")
    return hashlib.sha256((domain + "\0" + value).encode("utf-8")).hexdigest()


def evidence_token_from_ranking_key_sha256(ranking_key_sha256: str) -> str:
    """Return the sole opaque evidence token used in prefreeze outputs.

    The FCD-1 ranking-key receipt is not itself an authorization token.  The
    two-step domain separation is intentionally stable: it is the same value
    carried by ``authorized_tokens`` and both Top-10 arms after trace replay.
    """
    receipt = _hex_token(ranking_key_sha256, "FCD-1 ranking-key receipt")
    source_token = _token("aerp4:ranking", receipt)
    return hashlib.sha256(source_token.encode("utf-8")).hexdigest()


def _hex_token(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError(f"{name} must be a lowercase 64-hex receipt")
    return value


def _exact(value: Any, keys: set[str], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} schema mismatch")
    return value


def _no_sensitive_fields(value: Any, name: str = "sanitized receipt") -> None:
    """Reject plaintext labels and source identities in serialized receipts."""
    forbidden = {
        "category", "gold", "query", "answer", "text", "source_event_id",
        "ranking_key", "item_id", "conversation_id", "evidence_item_count",
    }
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} has a non-string key")
            if key.lower() in forbidden:
                raise ValueError(f"{name} exposes forbidden field {key!r}")
            _no_sensitive_fields(child, f"{name}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _no_sensitive_fields(child, f"{name}[{index}]")


def _sanitized_item(question: Mapping[str, Any], trace: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one FCD-1 trace, then immediately discard raw identities."""
    item = question.get("item_id")
    conversation = question.get("conversation_id")
    if not isinstance(item, str) or not item or not isinstance(conversation, str) or not conversation:
        raise ValueError("staged identity is malformed")
    # _ledger is the frozen FCD-1 validator. Passing the whole trace rather
    # than its ranking subobject is deliberate: the validator owns that shape.
    ledger = fcd2._ledger(dict(trace))
    ranking = trace.get("retrieval_ranking")
    if not isinstance(ranking, Mapping):
        raise ValueError("product retrieval ranking is missing")
    groups = ledger.get("checkpoint_tie_groups")
    if not isinstance(groups, list):
        raise ValueError("FCD-1 checkpoint receipt is missing")
    event_tokens: dict[str, str] = {}
    evidence_tokens: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, Mapping) or not isinstance(group.get("chronological_members"), list):
            raise ValueError("FCD-1 checkpoint receipt is malformed")
        for member in group["chronological_members"]:
            if not isinstance(member, Mapping):
                raise ValueError("FCD-1 checkpoint member is malformed")
            event = member.get("source_event_id")
            ranking_hash = _hex_token(member.get("ranking_key_sha256"), "FCD-1 ranking-key receipt")
            if not isinstance(event, str) or not event or event in event_tokens:
                raise ValueError("FCD-1 event partition is malformed")
            event_tokens[event] = _token("aerp4:ranking", ranking_hash)
            evidence_tokens[event] = evidence_token_from_ranking_key_sha256(ranking_hash)
    full_orders = ledger.get("view_full_order")
    if not isinstance(full_orders, Mapping) or set(full_orders) != set(_VIEWS):
        raise ValueError("FCD-1 view-order receipt is malformed")
    view_token_orders: dict[str, list[str]] = {}
    expected_universe: set[str] | None = None
    for view in _VIEWS:
        order = full_orders[view]
        if not isinstance(order, list) or any(not isinstance(event, str) or event not in event_tokens for event in order):
            raise ValueError("FCD-1 full order contains an unknown event")
        tokens = [event_tokens[event] for event in order]
        if len(tokens) != len(set(tokens)):
            raise ValueError("FCD-1 full order is not a permutation")
        token_set = set(tokens)
        if expected_universe is None:
            expected_universe = token_set
        elif token_set != expected_universe:
            raise ValueError("FCD-1 view universes differ")
        view_token_orders[view] = tokens
    if expected_universe is None or len(expected_universe) != len(event_tokens):
        raise ValueError("FCD-1 rank universe is incomplete")
    view_digests = ranking.get("view_digests")
    if not isinstance(view_digests, Mapping) or set(view_digests) != set(_VIEWS):
        raise ValueError("product trace view-digest schema is malformed")
    safe_digests = {view: _hex_token(view_digests[view], f"{view} digest") for view in _VIEWS}
    encoder = ranking.get("encoder_identity")
    if not isinstance(encoder, str) or not encoder:
        raise ValueError("product trace encoder identity is malformed")
    return {
        "item_token": _token("aerp4:item", item),
        "group_token": _token("aerp4:group", conversation),
        "campaign_token": _token("aerp4:campaign", conversation),
        "rank_source": {
            "query_sha256": _hex_token(ranking.get("query_sha256"), "query digest"),
            "input_sha256": _hex_token(ranking.get("input_sha256"), "input digest"),
            "view_digests": safe_digests,
            "encoder_identity": encoder,
            "view_token_orders": view_token_orders,
            "evidence_token_by_ranking_token": {
                event_tokens[event]: evidence_tokens[event]
                for event in sorted(event_tokens)
            },
        },
    }


def sanitize_rank_source(artifact: Mapping[str, Any]) -> dict[str, Any]:
    """Produce the rank-only 1,986-item source layer with a fixed 5/5 split.

    It deliberately does not inspect official evidence or infer zero-evidence
    membership. That label-owned decision occurs in custody and supplies the
    1,982-item study membership which this module subsequently verifies.
    """
    questions = artifact.get("questions")
    traces = artifact.get("product_traces")
    if not isinstance(questions, list) or not isinstance(traces, Mapping):
        raise ValueError("staged rank source is malformed")
    rows: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for question in questions:
        if not isinstance(question, Mapping):
            raise ValueError("staged question is malformed")
        item = question.get("item_id")
        if not isinstance(item, str) or item in seen_items:
            raise ValueError("staged item identities are malformed")
        trace = traces.get(item)
        if not isinstance(trace, Mapping):
            raise ValueError("staged product trace is missing")
        rows.append(_sanitized_item(question, trace))
        seen_items.add(item)
    groups = sorted({row["group_token"] for row in rows})
    if len(rows) != 1986 or len(groups) != 10:
        raise ValueError("frozen LoCoMo rank denominator or conversation count drift")
    train_groups = set(groups[:5])
    partitions = {
        name: [row for row in rows if (row["group_token"] in train_groups) == (name == "train")]
        for name in ("train", "dev")
    }
    result = {
        "schema": SCHEMA,
        "status": "complete",
        "question_random_split": False,
        "zero_evidence_membership": "unknown_label_custody_required",
        "partitions": {
            name: {
                "crosswalk_sha256": gate._crosswalk(_crosswalk_rows(members)),
                "items": members,
            }
            for name, members in partitions.items()
        },
    }
    _no_sensitive_fields(result)
    return result


def _crosswalk_rows(items: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The gate crosswalk binds identities and the rank-source input receipts."""
    rows: list[dict[str, str]] = []
    for item in items:
        rank_source = item["rank_source"]
        rows.append({
            "item_token": item["item_token"],
            "group_token": item["group_token"],
            "campaign_token": item["campaign_token"],
            "query_sha256": rank_source["query_sha256"],
            "input_sha256": rank_source["input_sha256"],
        })
    return rows


def _parse_partition(source: Mapping[str, Any], partition: str) -> list[Mapping[str, Any]]:
    _exact(source, {"schema", "status", "question_random_split", "zero_evidence_membership", "partitions"}, "sanitized source")
    if source["schema"] != SCHEMA or source["status"] != "complete" or source["question_random_split"] is not False or source["zero_evidence_membership"] != "unknown_label_custody_required":
        raise ValueError("sanitized source identity mismatch")
    partitions = source["partitions"]
    if not isinstance(partitions, Mapping) or set(partitions) != {"train", "dev"}:
        raise ValueError("sanitized source partitions are malformed")
    part = _exact(partitions.get(partition), {"crosswalk_sha256", "items"}, "sanitized partition")
    _hex_token(part["crosswalk_sha256"], "sanitized crosswalk")
    items = part["items"]
    if not isinstance(items, list) or not items:
        raise ValueError("sanitized partition items are malformed")
    parsed: list[Mapping[str, Any]] = []
    for item in items:
        row = _exact(item, {"item_token", "group_token", "campaign_token", "rank_source"}, "sanitized item")
        for key in ("item_token", "group_token", "campaign_token"):
            _hex_token(row[key], key)
        rank_source = _exact(row["rank_source"], {"query_sha256", "input_sha256", "view_digests", "encoder_identity", "view_token_orders", "evidence_token_by_ranking_token"}, "sanitized rank source")
        _hex_token(rank_source["query_sha256"], "query digest")
        _hex_token(rank_source["input_sha256"], "input digest")
        if not isinstance(rank_source["encoder_identity"], str) or not rank_source["encoder_identity"]:
            raise ValueError("sanitized encoder identity is malformed")
        digests = rank_source["view_digests"]
        orders = rank_source["view_token_orders"]
        evidence = rank_source["evidence_token_by_ranking_token"]
        if not isinstance(digests, Mapping) or not isinstance(orders, Mapping) or set(digests) != set(_VIEWS) or set(orders) != set(_VIEWS):
            raise ValueError("sanitized view source schema is malformed")
        if not isinstance(evidence, Mapping):
            raise ValueError("sanitized evidence-token map is malformed")
        universe: set[str] | None = None
        for view in _VIEWS:
            _hex_token(digests[view], f"{view} digest")
            order = orders[view]
            if not isinstance(order, list) or not order or any(_hex_token(token, f"{view} ranking token") != token for token in order) or len(order) != len(set(order)):
                raise ValueError("sanitized view ranking order is malformed")
            if universe is None:
                universe = set(order)
            elif universe != set(order):
                raise ValueError("sanitized views have different universes")
        if universe is None or set(evidence) != universe:
            raise ValueError("sanitized evidence-token map universe differs")
        for rank_token, evidence_token in evidence.items():
            _hex_token(rank_token, "ranking token")
            # The second hash is the frozen producer's trace representation;
            # require it rather than accepting any convenient opaque mapping.
            if _hex_token(evidence_token, "evidence token") != hashlib.sha256(rank_token.encode("utf-8")).hexdigest():
                raise ValueError("sanitized evidence-token map is not replayable")
        parsed.append(row)
    if len({row["item_token"] for row in parsed}) != len(parsed):
        raise ValueError("sanitized partition repeats item tokens")
    if gate._crosswalk(_crosswalk_rows(parsed)) != part["crosswalk_sha256"]:
        raise ValueError("sanitized partition crosswalk mismatch")
    _no_sensitive_fields(source)
    return parsed


def _component_contributions(ranks: dict[str, dict[str, int]], weights: Mapping[str, float]) -> dict[str, dict[str, float]]:
    """Obtain per-view values through product RRF, never a copied formula."""
    contributions: dict[str, dict[str, float]] = {}
    for view in _VIEWS:
        one_view = {candidate_view: float(weights[candidate_view]) if candidate_view == view else 0.0 for candidate_view in _VIEWS}
        contributions[view] = retrieval._rrf_totals(ranks, one_view, 60)
    return contributions


def _trace(item: Mapping[str, Any], *, policy: RawAnchoredP5Policy, route: str) -> dict[str, Any]:
    source = item["rank_source"]
    orders = source["view_token_orders"]
    universe = list(orders["raw_bm25"])
    evidence_tokens = source["evidence_token_by_ranking_token"]
    ranks = {view: {token: index for index, token in enumerate(orders[view], start=1)} for view in _VIEWS}
    ranking_keys = {token: token for token in universe}
    decision = policy.decide(ranks=ranks, ranking_keys_by_id=ranking_keys, rrf_k=60)
    if decision.route != route:
        raise ValueError("paired policy forced route did not replay")
    totals = dict(decision.totals)
    selected = retrieval._ordered(totals, ranking_keys)
    if len(selected) > 10 and not totals[selected[9]] > totals[selected[10]]:
        raise ValueError("Top10 cutoff tie is unprovable")
    weights = dict(decision.effective_weights)
    contributions = _component_contributions(ranks, weights)
    ranking_key_receipts = {token: evidence_tokens[token] for token in universe}
    receipt = {
        "schema": prefreeze.ROUTING_SCHEMA,
        "policy": "raw_anchored_p5",
        "config": policy._config(60, policy.tau),
        "config_sha256": decision.config_sha256,
        "numerator": decision.anchor_numerator,
        "denominator": decision.anchor_denominator,
        "A": decision.anchor_ratio,
        "raw_top10_ranking_sha256": decision.raw_top10_ranking_sha256,
        "p5_top10_ranking_sha256": decision.p5_top10_ranking_sha256,
        "route": route,
        "effective_weights": weights,
        "final_ranking_sha256": decision.final_ranking_sha256,
    }
    trace = {
        "schema": prefreeze.TRACE_SCHEMA,
        "encoder_identity": source["encoder_identity"],
        "weights": weights,
        "rrf_k": 60,
        "query_sha256": source["query_sha256"],
        "input_sha256": source["input_sha256"],
        "view_digests": source["view_digests"],
        "selected": [
            {
                "source_event_id": token,
                "ranking_key_sha256": ranking_key_receipts[token],
                "final_rrf": totals[token],
                "component_ranks": {view: ranks[view][token] for view in _VIEWS},
                "contributions": {view: contributions[view][token] for view in _VIEWS},
            }
            for token in selected
        ],
        "aerp4_raw_anchored_p5": receipt,
    }
    prefreeze._production_trace(trace, route=route)
    return trace


def _membership_exclusions(
    study: Mapping[str, Any],
    sanitized_source: Mapping[str, Any],
    membership_receipt: Mapping[str, Any],
) -> set[str]:
    """Validate the custody-owned, global four-item exclusion receipt."""
    receipt = _exact(
        membership_receipt,
        {"schema", "source_artifact_sha256", "producer_sha256", "sanitized_source_sha256", "excluded_item_tokens", "excluded_count", "frozen_member_count"},
        "membership exclusion receipt",
    )
    if receipt["schema"] != "aerp4-locomo-label-custody-v1" or receipt["excluded_count"] != 4 or receipt["frozen_member_count"] != 1982:
        raise ValueError("membership exclusion receipt identity mismatch")
    source_sha = _hex_token(receipt["source_artifact_sha256"], "membership source artifact")
    producer_sha = _hex_token(receipt["producer_sha256"], "membership producer")
    if _hex_token(receipt["sanitized_source_sha256"], "membership sanitized source") != gate._sha(sanitized_source):
        raise ValueError("membership receipt does not bind the supplied sanitized source")
    excluded_raw = receipt["excluded_item_tokens"]
    if not isinstance(excluded_raw, list) or len(excluded_raw) != 4:
        raise ValueError("membership exclusions must contain exactly four items")
    excluded = {_hex_token(token, "excluded item token") for token in excluded_raw}
    if len(excluded) != 4:
        raise ValueError("membership exclusions must be unique")
    custodians = study["label_custodians"]
    for partition in ("train", "dev"):
        custodian = custodians[partition]
        if custodian["source_artifact_sha256"] != source_sha or custodian["producer_sha256"] != producer_sha:
            raise ValueError("membership receipt does not bind the frozen custody source")
    return excluded


def build_paired_envelope(
    study: Mapping[str, Any],
    partition: str,
    sanitized_source: Mapping[str, Any],
    membership_receipt: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build Raw(+inf)/P5(-inf) traces and immediately validate their freeze."""
    gate._validate_study(study)
    if partition not in {"train", "dev"}:
        raise ValueError("partition must be train or dev")
    all_partitions = {name: _parse_partition(sanitized_source, name) for name in ("train", "dev")}
    all_items = [item for name in ("train", "dev") for item in all_partitions[name]]
    if len(all_items) != 1986 or len({item["item_token"] for item in all_items}) != 1986:
        raise ValueError("sanitized source must retain exactly 1,986 unique items")
    excluded = _membership_exclusions(study, sanitized_source, membership_receipt)
    source_items = {item["item_token"] for item in all_items}
    if not excluded <= source_items:
        raise ValueError("membership exclusions are not source members")
    filtered_partitions = {
        name: [item for item in all_partitions[name] if item["item_token"] not in excluded]
        for name in ("train", "dev")
    }
    if sum(len(items) for items in filtered_partitions.values()) != 1982:
        raise ValueError("membership exclusions do not produce the frozen 1,982-item universe")
    for name, members in filtered_partitions.items():
        if not members:
            raise ValueError("membership exclusions empty a frozen partition")
        spec = gate._partition_spec(study, name)
        for key, digest_field in (("item_token", "item_sha256"), ("group_token", "group_sha256"), ("campaign_token", "campaign_sha256")):
            values = [row[key] for row in members]
            if key == "item_token" and len(values) != len(set(values)):
                raise ValueError("sanitized source item membership repeats")
            if gate._sha(sorted(values)) != spec[digest_field]:
                raise ValueError("sanitized source is not the frozen study membership")
        if gate._crosswalk(_crosswalk_rows(members)) != spec["crosswalk_sha256"]:
            raise ValueError("filtered source crosswalk is not the frozen study crosswalk")
    items = filtered_partitions[partition]
    output_items = []
    for item in items:
        output_items.append({
            "item_token": item["item_token"],
            "group_token": item["group_token"],
            "campaign_token": item["campaign_token"],
            "raw_trace": _trace(item, policy=RawAnchoredP5Policy(math.inf), route="raw"),
            "p5_trace": _trace(item, policy=RawAnchoredP5Policy(-math.inf), route="p5"),
        })
    envelope = {
        "schema": PAIR_SCHEMA,
        "status": "complete",
        "study_sha256": gate._sha(study),
        "partition": partition,
        "producer": dict(study["producer"]),
        "items": output_items,
    }
    return envelope, prefreeze.build_ranking_freeze(study, partition, envelope)
