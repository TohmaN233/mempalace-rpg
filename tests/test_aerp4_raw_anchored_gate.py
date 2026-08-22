from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path

import pytest

from benchmarks import aerp4_raw_anchored_gate as gate
from benchmarks import aerp4_raw_anchored_gate_prefreeze as prefreeze


def _token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _study(train_rows: list[dict], dev_rows: list[dict], *, gates: dict | None = None, label_options: dict | None = None) -> dict:
    def part(rows: list[dict]) -> dict:
        result = {f"{kind}_sha256": gate._sha(sorted(row[f"{kind}_token"] for row in rows)) for kind in ("campaign", "group", "item")}; result["crosswalk_sha256"] = gate._crosswalk(rows); return result
    raw_config = gate._sha(prefreeze._config("+inf")); p5_config = gate._sha(prefreeze._config("-inf")); retrieval = _token("retrieval")
    slots = {stage: {"stage": stage, "partition": partition, "path_sha256": _token("slot:" + stage)} for stage, partition in (("train_prefreeze", "train"), ("dev_prefreeze", "dev"), ("tau_select", "train"), ("dev_eval", "dev"))}
    parts = {"train": part(train_rows), "dev": part(dev_rows)}; options = label_options or {}; labels = {name: {"source_artifact_sha256": _token("label-source:" + name), "producer_sha256": _token("label-producer:" + name), "crosswalk_sha256": parts[name]["crosswalk_sha256"], "label_payload_sha256": gate._label_payload_sha([{"item_token": row["item_token"], "gold_tokens": [row["_gold"]] * int(options.get(name, {}).get("duplicate", False) and 2 or 1), "unresolved_evidence_item_count": int(options.get(name, {}).get("unresolved", 0)), "evidence_item_count": int(options.get(name, {}).get("duplicate", False) and 2 or 1) + int(options.get(name, {}).get("unresolved", 0))} for row in (train_rows if name == "train" else dev_rows)])} for name in ("train", "dev")}
    receipt = lambda implementation: {"implementation_sha256": implementation, "git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": _token("status"), "commit_diff_sha256": _token("diff"), "commit_diff_bytes": 0}
    guard_source = _token("guard-source"); guard_results = {name: gate._sha({"source_artifact_sha256": guard_source, "name": name, "pass": True}) for name in ("acl", "safety", "exact_replay", "performance")}
    return {"schema": gate.STUDY_SCHEMA, "status": "frozen", "dataset_sha256": _token("dataset"), "producer": {"artifact_sha256": _token("artifact"), "git_head": "a" * 40, "git_tree": "b" * 40, "retrieval_implementation_sha256": retrieval}, "retrieval": {"implementation_sha256": retrieval, "raw_config_sha256": raw_config, "p5_config_sha256": p5_config}, "splits": {"source_separated_crosswalk_sha256": _token("crosswalk"), "question_random_split": False}, "partitions": parts, "label_custodians": labels, "guardrail_custodian": {"source_artifact_sha256": guard_source, "producer_sha256": _token("guard-producer"), "result_payload_sha256s": guard_results}, "selection": {"bootstrap": {"seed": 17, "resamples": 40, "percentiles": [2.5, 97.5]}, "go_gates": {"min_route_fraction": 0.10, "guardrails": gates or {"acl": True, "safety": True, "exact_replay": True, "performance": True}}}, "output_slots": slots, "analyzers": {"gate": receipt(hashlib.sha256(Path(gate.__file__).read_bytes()).hexdigest()), "prefreeze": receipt(hashlib.sha256(Path(prefreeze.__file__).read_bytes()).hexdigest()), "label": receipt(_token("label-adapter")), "guardrail": receipt(_token("guard-adapter"))}}


def _row(name: str, a: float, group: str, campaign: str = "campaign", *, raw_hit: bool = False, p5_hit: bool = True) -> dict:
    gold = _token(f"gold:{name}")
    raw = [gold] if raw_hit else [_token(f"raw:{name}")]
    p5 = [gold] if p5_hit else [_token(f"p5:{name}")]
    authorized = list(dict.fromkeys(raw + p5))
    return {"item_token": _token(f"item:{name}"), "group_token": _token(f"group:{group}"), "campaign_token": _token(f"campaign:{campaign}"), "query_sha256": _token("query:" + name), "input_sha256": _token("input:" + name), "A_hex": a.hex(), "numerator": a, "denominator": 1.0, "authorized_tokens": authorized, "authorized_tokens_sha256": gate._sha(authorized), "raw_top10": raw, "p5_top10": p5, "raw_top10_sha256": gate._sha(raw), "p5_top10_sha256": gate._sha(p5), "_gold": gold}


def _freeze(study: dict, partition: str, rows: list[dict]) -> dict:
    paired = _token("paired:" + partition)
    return {"schema": gate.RANKING_FREEZE_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": partition, "producer": dict(study["producer"]), "paired_input_sha256": paired, "items": [{key: value for key, value in row.items() if key != "_gold"} for row in rows], "phase_ledger": {"status": "complete", "phase": "atomic_publish_ready", "terminal_phase": "atomic_publish_ready"}, "publication": _publication(study, [gate._sha(study), paired])}


def _publication(study: dict, shas: list[str] | None = None, *, analyzer: str = "prefreeze") -> dict:
    values = shas or [_token("input")]
    receipt = study["analyzers"][analyzer]
    return {"input_receipts": [{"path_sha256": _token("path:" + str(index)), "sha256": value} for index, value in enumerate(values)], "analyzer_git_state": {key: receipt[key] for key in ("git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes")}, "implementation_sha256": receipt["implementation_sha256"]}


def _guardrails(study: dict) -> dict:
    custodian = study["guardrail_custodian"]
    publication = _publication(study, [gate._sha(study), custodian["source_artifact_sha256"]], analyzer="guardrail")
    return {"schema": gate.GUARDRAIL_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": "dev", "source_artifact_sha256": custodian["source_artifact_sha256"], "producer_sha256": custodian["producer_sha256"], "results": [{"name": name, "pass": True, "receipt_sha256": gate._sha({"source_artifact_sha256": custodian["source_artifact_sha256"], "name": name, "pass": True})} for name in ("acl", "safety", "exact_replay", "performance")], "publication": publication}


def _formal_policy(study: dict, policy: dict) -> dict:
    result = dict(policy); publication = _publication(study, [gate._sha(study), gate._sha(policy["frozen_train_rankings"]), gate._sha(policy["frozen_train_labels"])], analyzer="gate"); result["publication"] = publication; return result


def _labels(study: dict, partition: str, rows: list[dict], *, duplicate: bool = False, unresolved: int = 0) -> dict:
    items = []
    for row in rows:
        gold = [row["_gold"], row["_gold"]] if duplicate else [row["_gold"]]
        items.append({"item_token": row["item_token"], "gold_tokens": gold, "unresolved_evidence_item_count": unresolved, "evidence_item_count": len(gold) + unresolved})
    custodian = study["label_custodians"][partition]
    publication = _publication(study, [gate._sha(study), custodian["source_artifact_sha256"]], analyzer="label")
    return {"schema": gate.LABELS_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": partition, "source_artifact_sha256": custodian["source_artifact_sha256"], "producer_sha256": custodian["producer_sha256"], "crosswalk_sha256": custodian["crosswalk_sha256"], "items": items, "publication": publication}


def _paired(study: dict, partition: str, rows: list[dict]) -> dict:
    items = []
    for row in rows:
        tokens = row["authorized_tokens"] + [_token(f"tail:{row['item_token']}:{index}") for index in range(11)]
        p5_tokens = row["p5_top10"] + [token for token in tokens if token not in row["p5_top10"]]
        raw_tokens = row["raw_top10"] + [token for token in tokens if token not in row["raw_top10"]]
        views = {view: _token(view + row["item_token"]) for view in prefreeze.VIEWS}
        def trace(route: str, ordered: list[str]) -> dict:
            weights = prefreeze.RAW_WEIGHTS if route == "raw" else prefreeze.P5_WEIGHTS
            receipt = {"schema": prefreeze.ROUTING_SCHEMA, "policy": "raw_anchored_p5", "config": prefreeze._config("+inf" if route == "raw" else "-inf"), "config_sha256": gate._sha(prefreeze._config("+inf" if route == "raw" else "-inf")), "numerator": row["numerator"], "denominator": row["denominator"], "A": row["numerator"] / row["denominator"], "raw_top10_ranking_sha256": gate._sha(raw_tokens[:10]), "p5_top10_ranking_sha256": gate._sha(p5_tokens[:10]), "route": route, "effective_weights": weights, "final_ranking_sha256": gate._sha(ordered)}
            selected = [{"source_event_id": "event-" + token, "ranking_key_sha256": token, "final_rrf": sum(weights[view] / (60 + index + 1) for view in prefreeze.VIEWS), "component_ranks": {view: index + 1 for view in prefreeze.VIEWS}, "contributions": {view: weights[view] / (60 + index + 1) for view in prefreeze.VIEWS}} for index, token in enumerate(ordered)]
            return {"schema": prefreeze.TRACE_SCHEMA, "encoder_identity": "fixture", "weights": weights, "rrf_k": 60, "query_sha256": row["query_sha256"], "input_sha256": row["input_sha256"], "view_digests": views, "selected": selected, "aerp4_raw_anchored_p5": receipt}
        items.append({"item_token": row["item_token"], "group_token": row["group_token"], "campaign_token": row["campaign_token"], "raw_trace": trace("raw", raw_tokens), "p5_trace": trace("p5", p5_tokens)})
    return {"schema": prefreeze.PREFREEZE_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": partition, "producer": dict(study["producer"]), "items": items}


def _paired_non_degenerate(study: dict, partition: str, rows: list[dict]) -> dict:
    """Production-shaped traces whose opposite expert evidence is outside Top-10."""
    items = []
    for row in rows:
        raw_primary, p5_primary = row["raw_top10"][0], row["p5_top10"][0]
        neutral = [_token(f"neutral:{row['item_token']}:{index}") for index in range(10)]
        raw_order = [raw_primary, *neutral[:9], p5_primary, neutral[9]]
        p5_order = [p5_primary, *neutral[:9], raw_primary, neutral[9]]
        views = {view: _token(view + row["item_token"]) for view in prefreeze.VIEWS}

        def trace(route: str, ordered: list[str]) -> dict:
            weights = prefreeze.RAW_WEIGHTS if route == "raw" else prefreeze.P5_WEIGHTS
            raw_digest, p5_digest = gate._sha(raw_order[:10]), gate._sha(p5_order[:10])
            receipt = {"schema": prefreeze.ROUTING_SCHEMA, "policy": "raw_anchored_p5", "config": prefreeze._config("+inf" if route == "raw" else "-inf"), "config_sha256": gate._sha(prefreeze._config("+inf" if route == "raw" else "-inf")), "numerator": row["numerator"], "denominator": row["denominator"], "A": row["numerator"] / row["denominator"], "raw_top10_ranking_sha256": raw_digest, "p5_top10_ranking_sha256": p5_digest, "route": route, "effective_weights": weights, "final_ranking_sha256": gate._sha(ordered)}
            selected = [{"source_event_id": "event-" + token, "ranking_key_sha256": token, "final_rrf": sum(weights[view] / (60 + index + 1) for view in prefreeze.VIEWS), "component_ranks": {view: index + 1 for view in prefreeze.VIEWS}, "contributions": {view: weights[view] / (60 + index + 1) for view in prefreeze.VIEWS}} for index, token in enumerate(ordered)]
            return {"schema": prefreeze.TRACE_SCHEMA, "encoder_identity": "fixture", "weights": weights, "rrf_k": 60, "query_sha256": row["query_sha256"], "input_sha256": row["input_sha256"], "view_digests": views, "selected": selected, "aerp4_raw_anchored_p5": receipt}

        items.append({"item_token": row["item_token"], "group_token": row["group_token"], "campaign_token": row["campaign_token"], "raw_trace": trace("raw", raw_order), "p5_trace": trace("p5", p5_order)})
    return {"schema": prefreeze.PREFREEZE_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": partition, "producer": dict(study["producer"]), "items": items}


def test_candidate_taus_unique_duplicate_single_hex_and_midpoint_failure():
    assert gate.candidate_taus([0.25, 0.5, 0.25]) == (-math.inf, 0.375, math.inf)
    assert gate.candidate_taus([0.25]) == (-math.inf, math.inf)
    assert gate._float_receipt((0.375).hex(), "x") == 0.375
    with pytest.raises(ValueError, match="strict float midpoint"):
        gate.candidate_taus([1.0, math.nextafter(1.0, math.inf)])
    with pytest.raises(ValueError, match="finite"):
        gate.candidate_taus([math.nan])


def test_selection_uses_conversation_objective_larger_tau_tie_and_degenerate_stop():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=True), _row("b", 0.8, "one", raw_hit=True, p5_hit=True), _row("c", 0.8, "two", raw_hit=True, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]
    study = _study(train, dev, label_options={"dev": {"duplicate": True, "unresolved": 1}})
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    # all candidates score equally at zero/one depending fixture; numerical larger wins
    assert policy["selected_tau"] == "+inf"
    assert policy["status"] == "STOP_ROUTER_DEGENERATE"


def test_threshold_equality_and_official_duplicate_unresolved_denominator():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign", raw_hit=True, p5_hit=True)]
    study = _study(train, dev, label_options={"dev": {"duplicate": True, "unresolved": 1}})
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    report = gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev, duplicate=True, unresolved=1), policy, _guardrails(study))
    assert report["routes"]["p5_fraction"] == 1.0
    assert report["metrics"]["gated"]["question_macro_recall_at_10"] == pytest.approx(2 / 3)
    assert gate._route(dev[0], 0.5) == "p5"


def test_prefreeze_is_generic_label_free_and_validates_paired_receipts():
    train = [_row("a", 0.25, "one")]; dev = [_row("d", 0.75, "two", "dev-campaign")]
    study = _study(train, dev)
    frozen = prefreeze.build_ranking_freeze(study, "train", _paired(study, "train", train))
    assert frozen["schema"] == gate.RANKING_FREEZE_SCHEMA
    assert frozen["phase_ledger"]["terminal_phase"] == "atomic_publish_ready"
    assert len(frozen["items"][0]["authorized_tokens"]) > 10
    assert "source_event_id" not in json.dumps(frozen, sort_keys=True)
    bad = _paired(study, "train", train); bad["items"][0]["p5_trace"]["input_sha256"] = _token("drift")
    with pytest.raises(ValueError, match="input_sha256"):
        prefreeze.build_ranking_freeze(study, "train", bad)
    unknown = _paired(study, "train", train); unknown["items"][0]["raw_trace"]["notes"] = "plaintext canary"
    with pytest.raises(ValueError, match="keys mismatch"):
        prefreeze.build_ranking_freeze(study, "train", unknown)
    assert "labels" not in prefreeze.build_ranking_freeze.__code__.co_varnames


def test_formal_production_prefreeze_admits_to_select_without_draft_bypass():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]; study = _study(train, dev)
    paired = _paired(study, "train", train); draft = prefreeze.build_ranking_freeze(study, "train", paired)
    with pytest.raises(ValueError, match="unknown or missing"):
        gate.select_and_freeze(study, draft, _labels(study, "train", train))
    formal = dict(draft); formal["publication"] = _publication(study, [gate._sha(study), gate._sha(paired)])
    policy = gate.select_and_freeze(study, formal, _labels(study, "train", train))
    assert policy["selected_tau"] in policy["candidate_tau_receipts"]


def test_overlap_missing_extra_duplicate_order_and_digest_fail_closed():
    train = [_row("a", 0.25, "one")]; dev = [_row("d", 0.75, "two", "dev-campaign")]
    study = _study(train, dev); ranking = _freeze(study, "train", train); labels = _labels(study, "train", train)
    duplicate = json.loads(json.dumps(ranking)); duplicate["items"].append(dict(duplicate["items"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        gate.select_and_freeze(study, duplicate, labels)
    extra = json.loads(json.dumps(labels)); extra["items"].append(dict(extra["items"][0])); extra["items"][1]["item_token"] = _token("extra")
    with pytest.raises(ValueError):
        gate.select_and_freeze(study, ranking, extra)
    policy = _formal_policy(study, gate.select_and_freeze(study, ranking, labels))
    dev_overlap = _freeze(study, "dev", dev); dev_overlap["items"][0]["group_token"] = train[0]["group_token"]
    with pytest.raises(ValueError):
        gate.evaluate_dev_once(study, dev_overlap, _labels(study, "dev", dev), policy, _guardrails(study))


def test_ranking_rejects_duplicate_top10_unauthorized_nan_and_plaintext_canary():
    train = [_row("a", 0.25, "one")]; dev = [_row("d", 0.75, "two", "dev-campaign")]
    study = _study(train, dev); labels = _labels(study, "train", train)
    duplicate = _freeze(study, "train", train); duplicate["items"][0]["raw_top10"] *= 2; duplicate["items"][0]["raw_top10_sha256"] = gate._sha(duplicate["items"][0]["raw_top10"])
    with pytest.raises(ValueError, match="unique"):
        gate.select_and_freeze(study, duplicate, labels)
    unauthorized = _freeze(study, "train", train); unauthorized["items"][0]["raw_top10"] = [_token("unauthorized")]; unauthorized["items"][0]["raw_top10_sha256"] = gate._sha(unauthorized["items"][0]["raw_top10"])
    with pytest.raises(ValueError, match="unauthorized"):
        gate.select_and_freeze(study, unauthorized, labels)
    with pytest.raises(ValueError, match="forbidden"):
        gate.select_and_freeze({**study, "query_text": "CANARY"}, _freeze(study, "train", train), labels)


def test_crosswalk_rejects_item_group_swap_when_membership_digests_still_match():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]; study = _study(train, dev); ranking = _freeze(study, "train", train)
    ranking["items"][0]["group_token"], ranking["items"][1]["group_token"] = ranking["items"][1]["group_token"], ranking["items"][0]["group_token"]
    with pytest.raises(ValueError, match="crosswalk mismatch"):
        gate.select_and_freeze(study, ranking, _labels(study, "train", train))


def test_formal_label_payload_rejects_coordinated_gold_swap():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]
    study = _study(train, dev)
    labels = _labels(study, "train", train)
    labels["items"][0]["gold_tokens"], labels["items"][1]["gold_tokens"] = labels["items"][1]["gold_tokens"], labels["items"][0]["gold_tokens"]
    with pytest.raises(ValueError, match="labels payload mismatch"):
        gate.select_and_freeze(study, _freeze(study, "train", train), labels)


@pytest.mark.parametrize("overlap_key", ["item_token", "group_token", "campaign_token"])
def test_valid_cross_partition_overlap_reaches_specific_gate(overlap_key: str):
    train = [_row("train-a", 0.2, "train-one", "train-campaign", raw_hit=True, p5_hit=False), _row("train-b", 0.8, "train-two", "train-campaign", raw_hit=False, p5_hit=True)]
    dev = [_row("dev-a", 0.2, "dev-one", "dev-campaign", raw_hit=True, p5_hit=False), _row("dev-b", 0.8, "dev-two", "dev-campaign", raw_hit=False, p5_hit=True)]
    dev[0][overlap_key] = train[0][overlap_key]
    study = _study(train, dev)
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    with pytest.raises(ValueError, match=f"train/dev {overlap_key} overlap"):
        gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, _guardrails(study))


def test_production_trace_equal_score_tie_fails_closed():
    train = [_row("a", 0.25, "one")]
    dev = [_row("d", 0.75, "two", "dev-campaign")]
    study = _study(train, dev)
    paired = _paired(study, "train", train)
    selected = paired["items"][0]["raw_trace"]["selected"]
    selected[1]["component_ranks"] = dict(selected[0]["component_ranks"])
    selected[1]["contributions"] = dict(selected[0]["contributions"])
    selected[1]["final_rrf"] = selected[0]["final_rrf"]
    with pytest.raises(ValueError, match="tie/unverifiable"):
        prefreeze.build_ranking_freeze(study, "train", paired)


def test_bootstrap_reproducible_sha_and_dev_never_calls_selection(monkeypatch: pytest.MonkeyPatch):
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.2, "three", "dev-campaign"), _row("e", 0.8, "four", "dev-campaign")]
    study = _study(train, dev); policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    report = gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, _guardrails(study))
    again = gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, _guardrails(study))
    assert report["gated_minus"]["raw"]["replicates_sha256"] == again["gated_minus"]["raw"]["replicates_sha256"]
    monkeypatch.setattr(gate, "candidate_taus", lambda _v: (_ for _ in ()).throw(AssertionError("poison")))
    monkeypatch.setattr(gate, "select_and_freeze", lambda *_a: (_ for _ in ()).throw(AssertionError("poison")))
    gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, _guardrails(study))


def test_conversation_macro_is_selection_objective_and_frozen_tau_cannot_be_forged():
    train = [_row(f"low-{index}", 0.2, "many", raw_hit=False, p5_hit=True) for index in range(10)] + [_row("high", 0.8, "one", raw_hit=True, p5_hit=False)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]
    study = _study(train, dev)
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    # Question macro would prefer -inf (10/11); equal-group conversation macro
    # ties all static choices, then protocol requires the larger tau (+inf).
    assert policy["selected_tau"] == "+inf"
    forged = json.loads(json.dumps(policy)); forged["selected_tau"] = "-inf"
    with pytest.raises(ValueError, match="selection proof mismatch"):
        gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), forged, _guardrails(study))


def test_manifest_threshold_and_stop_are_real_not_hard_coded():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]
    study = _study(train, dev); study["selection"]["go_gates"]["min_route_fraction"] = 0.6
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    assert policy["status"] == "STOP_ROUTER_DEGENERATE"
    with pytest.raises(ValueError, match="cannot enter dev"):
        gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, _guardrails(study))


def test_guardrail_evidence_is_independent_nonempty_and_fail_closed():
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]; study = _study(train, dev)
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    missing = _guardrails(study); missing["results"] = []
    with pytest.raises(ValueError, match="empty"):
        gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, missing)
    failed = _guardrails(study); failed["results"][0]["pass"] = False; failed["results"][0]["receipt_sha256"] = gate._sha({"source_artifact_sha256": failed["source_artifact_sha256"], "name": failed["results"][0]["name"], "pass": False})
    with pytest.raises(ValueError, match="result payload mismatch"):
        gate.evaluate_dev_once(study, _freeze(study, "dev", dev), _labels(study, "dev", dev), policy, failed)


def test_bound_input_publication_is_canonical_external_no_clobber_and_toctou(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "input.json"; source.write_bytes(b'{"x":1}')
    bound = gate.BoundInput.load(source, hashlib.sha256(source.read_bytes()).hexdigest())
    repo = Path(__file__).resolve().parents[1]
    state = {"git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": "c" * 64, "commit_diff_sha256": "d" * 64, "commit_diff_bytes": 0}
    monkeypatch.setattr(gate, "_git_state", lambda _repo: dict(state))
    output = tmp_path / "report.json"
    published = gate.publish_bound_report(report={"schema": "x"}, output=output, inputs=[bound], repo=repo)
    assert output.read_bytes() == gate._canonical_bytes(published)
    with pytest.raises(FileExistsError):
        gate.publish_bound_report(report={"schema": "x"}, output=output, inputs=[bound], repo=repo)
    with pytest.raises(ValueError, match="outside"):
        gate.publish_bound_report(report={"schema": "x"}, output=repo / "bad.json", inputs=[bound], repo=repo)
    source.write_bytes(b'{"x":2}')
    with pytest.raises(RuntimeError, match="TOCTOU"):
        gate.publish_bound_report(report={"schema": "x"}, output=tmp_path / "drift.json", inputs=[bound], repo=repo)
    assert not (tmp_path / "drift.json").exists()


def test_select_cli_requires_bound_canonical_inputs_and_external_no_clobber_slot(tmp_path: Path):
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.5, "dev", "dev-campaign")]; study = _study(train, dev)
    repo = tmp_path / "clean-repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True); subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True); subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "tracked.txt").write_text("ok", encoding="utf-8"); subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    (repo / "tracked.txt").write_text("ok2", encoding="utf-8"); subprocess.run(["git", "-C", str(repo), "commit", "-am", "fixture-two", "-q"], check=True)
    output = tmp_path / "external.json"; study["output_slots"]["tau_select"]["path_sha256"] = gate._slot_path_sha(output)
    files = {"study": study, "rankings": _freeze(study, "train", train), "labels": _labels(study, "train", train)}
    paths = {}
    for name, value in files.items():
        path = tmp_path / f"{name}.json"; path.write_bytes(gate._canonical_bytes(value)); paths[name] = path
    args = ["select", "--study", str(paths["study"]), "--study-sha256", _token_for_file(paths["study"]), "--rankings", str(paths["rankings"]), "--rankings-sha256", _token_for_file(paths["rankings"]), "--labels", str(paths["labels"]), "--labels-sha256", _token_for_file(paths["labels"]), "--output", str(output), "--repo", str(repo)]
    wrong = [*args]; wrong[wrong.index("--output") + 1] = str(tmp_path / "wrong-slot.json")
    with pytest.raises(ValueError, match="pre-registered"):
        gate.main(wrong)
    assert gate.main(args) == 0 and output.exists()
    with pytest.raises(FileExistsError): gate.main(args)


def test_eval_cli_consumes_formal_policy_and_independent_guardrail_artifact(tmp_path: Path):
    train = [_row("a", 0.2, "one", raw_hit=True, p5_hit=False), _row("b", 0.8, "two", raw_hit=False, p5_hit=True)]
    dev = [_row("d", 0.2, "three", "dev-campaign"), _row("e", 0.8, "four", "dev-campaign")]
    output = tmp_path / "dev-eval.json"; study = _study(train, dev); study["output_slots"]["dev_eval"]["path_sha256"] = gate._slot_path_sha(output)
    policy = _formal_policy(study, gate.select_and_freeze(study, _freeze(study, "train", train), _labels(study, "train", train)))
    repo = tmp_path / "repo"; repo.mkdir(); subprocess.run(["git", "init", "-q", str(repo)], check=True); subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True); subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "x").write_text("1", encoding="utf-8"); subprocess.run(["git", "-C", str(repo), "add", "x"], check=True); subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True); (repo / "x").write_text("2", encoding="utf-8"); subprocess.run(["git", "-C", str(repo), "commit", "-am", "two", "-q"], check=True)
    values = {"study": study, "rankings": _freeze(study, "dev", dev), "labels": _labels(study, "dev", dev), "policy": policy, "guards": _guardrails(study)}; paths = {}
    for name, value in values.items():
        path = tmp_path / f"{name}.json"; path.write_bytes(gate._canonical_bytes(value)); paths[name] = path
    args = ["evaluate", "--study", str(paths["study"]), "--study-sha256", _token_for_file(paths["study"]), "--rankings", str(paths["rankings"]), "--rankings-sha256", _token_for_file(paths["rankings"]), "--labels", str(paths["labels"]), "--labels-sha256", _token_for_file(paths["labels"]), "--policy-freeze", str(paths["policy"]), "--policy-freeze-sha256", _token_for_file(paths["policy"]), "--guardrail-evidence", str(paths["guards"]), "--guardrail-evidence-sha256", _token_for_file(paths["guards"]), "--output", str(output), "--repo", str(repo)]
    assert gate.main(args) == 0 and output.exists()


def test_formal_non_degenerate_four_stage_cli_chain(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    (repo / "tracked.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "one"], check=True)
    (repo / "tracked.txt").write_text("two", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "two", "-q"], check=True)
    analyzer_state = gate._git_state(repo)

    train = [_row("train-low", 0.2, "train-low", raw_hit=True, p5_hit=False), _row("train-high", 0.8, "train-high", raw_hit=False, p5_hit=True)]
    dev = [_row("dev-low", 0.2, "dev-low", "dev-campaign", raw_hit=True, p5_hit=False), _row("dev-high", 0.8, "dev-high", "dev-campaign", raw_hit=False, p5_hit=True)]
    outputs = {stage: tmp_path / f"{stage}.json" for stage in ("train_prefreeze", "dev_prefreeze", "tau_select", "dev_eval")}
    study = _study(train, dev)
    for receipt in study["analyzers"].values():
        for key, value in analyzer_state.items():
            receipt[key] = value
    for stage, output in outputs.items():
        study["output_slots"][stage]["path_sha256"] = gate._slot_path_sha(output)

    values = {"study": study, "train_paired": _paired_non_degenerate(study, "train", train), "dev_paired": _paired_non_degenerate(study, "dev", dev), "train_labels": _labels(study, "train", train), "dev_labels": _labels(study, "dev", dev), "guardrails": _guardrails(study)}
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = tmp_path / f"input-{name}.json"
        path.write_bytes(gate._canonical_bytes(value))
        paths[name] = path

    for partition in ("train", "dev"):
        paired = paths[f"{partition}_paired"]
        assert prefreeze.main(["--study", str(paths["study"]), "--study-sha256", _token_for_file(paths["study"]), "--partition", partition, "--paired-policy-receipts", str(paired), "--paired-policy-receipts-sha256", _token_for_file(paired), "--output", str(outputs[f"{partition}_prefreeze"]), "--repo", str(repo)]) == 0

    assert gate.main(["select", "--study", str(paths["study"]), "--study-sha256", _token_for_file(paths["study"]), "--rankings", str(outputs["train_prefreeze"]), "--rankings-sha256", _token_for_file(outputs["train_prefreeze"]), "--labels", str(paths["train_labels"]), "--labels-sha256", _token_for_file(paths["train_labels"]), "--output", str(outputs["tau_select"]), "--repo", str(repo)]) == 0
    policy = json.loads(outputs["tau_select"].read_bytes())
    assert policy["status"] == "complete"
    assert policy["selected_tau"] == (0.5).hex()
    assert policy["selected"]["route_fraction"] == 0.5

    assert gate.main(["evaluate", "--study", str(paths["study"]), "--study-sha256", _token_for_file(paths["study"]), "--rankings", str(outputs["dev_prefreeze"]), "--rankings-sha256", _token_for_file(outputs["dev_prefreeze"]), "--labels", str(paths["dev_labels"]), "--labels-sha256", _token_for_file(paths["dev_labels"]), "--policy-freeze", str(outputs["tau_select"]), "--policy-freeze-sha256", _token_for_file(outputs["tau_select"]), "--guardrail-evidence", str(paths["guardrails"]), "--guardrail-evidence-sha256", _token_for_file(paths["guardrails"]), "--output", str(outputs["dev_eval"]), "--repo", str(repo)]) == 0
    report = json.loads(outputs["dev_eval"].read_bytes())
    assert report["routes"] == {"p5_count": 1, "raw_count": 1, "p5_fraction": 0.5, "raw_fraction": 0.5, "both_routes_meet_manifest_minimum": True}
    assert report["metrics"]["gated"]["question_macro_recall_at_10"] == 1.0
    assert report["publication"]["analyzer_git_state"] == analyzer_state
    assert report["publication"]["implementation_sha256"] == study["analyzers"]["gate"]["implementation_sha256"]


def _token_for_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
