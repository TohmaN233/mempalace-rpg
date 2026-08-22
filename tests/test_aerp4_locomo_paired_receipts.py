from __future__ import annotations

import hashlib
import inspect
import math

import pytest

from benchmarks import aerp4_locomo_paired_receipts as paired
from benchmarks import aerp4_locomo_custody as custody
from benchmarks import aerp4_raw_anchored_gate as gate
from benchmarks import aerp4_raw_anchored_gate_prefreeze as prefreeze
from mempalace_rpg.retrieval import FusionRoutingDecision


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rank_item(name: str, *, size: int = 12, raw_dense_order: list[str] | None = None) -> dict[str, object]:
    ranking_receipts = [_h(f"{name}:ranking-key:{index}") for index in range(size)]
    tokens = [paired._token("aerp4:ranking", receipt) for receipt in ranking_receipts]
    orders = {view: list(tokens) for view in prefreeze.VIEWS}
    orders["raw_dense"] = list(tokens if raw_dense_order is None else raw_dense_order)
    # Vary the positive P5 views while retaining valid full-order permutations.
    orders["observation_bm25"] = tokens[1:] + tokens[:1]
    orders["observation_dense"] = tokens[2:] + tokens[:2]
    orders["combo_dense"] = tokens[3:] + tokens[:3]
    return {
        "item_token": _h(f"item:{name}"),
        "group_token": _h(f"group:{name}"),
        "campaign_token": _h(f"campaign:{name}"),
        "rank_source": {
            "query_sha256": _h(f"query:{name}"),
            "input_sha256": _h(f"input:{name}"),
            "view_digests": {view: _h(f"digest:{name}:{view}") for view in prefreeze.VIEWS},
            "encoder_identity": "frozen-encoder-v1",
            "view_token_orders": orders,
            "evidence_token_by_ranking_token": {
                token: paired.evidence_token_from_ranking_key_sha256(receipt)
                for token, receipt in zip(tokens, ranking_receipts)
            },
        },
    }


def _analyzer() -> dict[str, object]:
    return {
        "implementation_sha256": _h("implementation"),
        "git_head": "a" * 40,
        "git_tree": "b" * 40,
        "git_dirty": False,
        "worktree_status_sha256": _h("status"),
        "commit_diff_sha256": _h("diff"),
        "commit_diff_bytes": 0,
    }


def _study(source: dict[str, object], *, excluded: set[str] | None = None) -> dict[str, object]:
    partitions: dict[str, dict[str, str]] = {}
    source_partitions = source["partitions"]
    assert isinstance(source_partitions, dict)
    for partition in ("train", "dev"):
        members = [row for row in source_partitions[partition]["items"] if row["item_token"] not in (excluded or set())]
        partitions[partition] = {
            "item_sha256": gate._sha(sorted(row["item_token"] for row in members)),
            "group_sha256": gate._sha(sorted(row["group_token"] for row in members)),
            "campaign_sha256": gate._sha(sorted(row["campaign_token"] for row in members)),
            "crosswalk_sha256": gate._crosswalk([
                {key: row[key] for key in ("item_token", "group_token", "campaign_token")}
                | {key: row["rank_source"][key] for key in ("query_sha256", "input_sha256")}
                for row in members
            ]),
        }
    joint = gate._sha({name: partitions[name]["crosswalk_sha256"] for name in ("train", "dev")})
    analyzer = _analyzer()
    producer = {
        "artifact_sha256": _h("artifact"),
        "git_head": "c" * 40,
        "git_tree": "d" * 40,
        "retrieval_implementation_sha256": analyzer["implementation_sha256"],
    }
    return {
        "schema": gate.STUDY_SCHEMA,
        "status": "frozen",
        "dataset_sha256": _h("dataset"),
        "producer": producer,
        "retrieval": {
            "implementation_sha256": analyzer["implementation_sha256"],
            "raw_config_sha256": prefreeze._sha(prefreeze._config("+inf")),
            "p5_config_sha256": prefreeze._sha(prefreeze._config("-inf")),
        },
        "splits": {"source_separated_crosswalk_sha256": joint, "question_random_split": False},
        "partitions": partitions,
        "label_custodians": {
            partition: {
                "source_artifact_sha256": _h("label-source"),
                "producer_sha256": _h("label-producer"),
                "crosswalk_sha256": partitions[partition]["crosswalk_sha256"],
                "label_payload_sha256": _h(f"label-payload:{partition}"),
            }
            for partition in ("train", "dev")
        },
        "guardrail_custodian": {
            "source_artifact_sha256": _h("guardrail-source"),
            "producer_sha256": _h("guardrail-producer"),
            "result_payload_sha256s": {name: _h(f"guardrail:{name}") for name in ("acl", "safety", "exact_replay", "performance")},
        },
        "selection": {
            "bootstrap": {"seed": 7, "resamples": 10, "percentiles": [2.5, 97.5]},
            "go_gates": {"min_route_fraction": 0.0, "guardrails": {name: True for name in ("acl", "safety", "exact_replay", "performance")}},
        },
        "output_slots": {
            "train_prefreeze": {"stage": "train_prefreeze", "partition": "train", "path_sha256": _h("train-freeze")},
            "dev_prefreeze": {"stage": "dev_prefreeze", "partition": "dev", "path_sha256": _h("dev-freeze")},
            "tau_select": {"stage": "tau_select", "partition": "train", "path_sha256": _h("tau")},
            "dev_eval": {"stage": "dev_eval", "partition": "dev", "path_sha256": _h("eval")},
        },
        "analyzers": {name: dict(analyzer) for name in ("gate", "prefreeze", "label", "guardrail")},
    }


def _source(*items: dict[str, object]) -> dict[str, object]:
    train = list(items)
    dev = [_rank_item("dev")]
    return {
        "schema": paired.SCHEMA,
        "status": "complete",
        "question_random_split": False,
        "zero_evidence_membership": "unknown_label_custody_required",
        "partitions": {
            name: {
                "crosswalk_sha256": gate._crosswalk([
                    {key: row[key] for key in ("item_token", "group_token", "campaign_token")}
                    | {key: row["rank_source"][key] for key in ("query_sha256", "input_sha256")}
                    for row in values
                ]),
                "items": values,
            }
            for name, values in (("train", train), ("dev", dev))
        },
    }


def _membership(excluded: set[str], source: dict[str, object]) -> dict[str, object]:
    return {
        "schema": "aerp4-locomo-label-custody-v1",
        "source_artifact_sha256": _h("label-source"),
        "producer_sha256": _h("label-producer"),
        "sanitized_source_sha256": gate._sha(source),
        "excluded_item_tokens": sorted(excluded),
        "excluded_count": 4,
        "frozen_member_count": 1982,
    }


def _full_source() -> tuple[dict[str, object], set[str]]:
    train = [_rank_item(f"train-{index}") for index in range(993)]
    dev = [_rank_item(f"dev-{index}") for index in range(993)]
    source = _source(*train)
    source["partitions"]["dev"]["items"] = dev
    source["partitions"]["dev"]["crosswalk_sha256"] = gate._crosswalk([
        {key: row[key] for key in ("item_token", "group_token", "campaign_token")}
        | {key: row["rank_source"][key] for key in ("query_sha256", "input_sha256")}
        for row in dev
    ])
    return source, {train[0]["item_token"], train[1]["item_token"], dev[0]["item_token"], dev[1]["item_token"]}


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_walk_keys(child) for child in value.values()))
    if isinstance(value, list):
        return set().union(*(_walk_keys(child) for child in value)) if value else set()
    return set()


def test_sanitizer_poison_labels_do_not_change_rank_only_output(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [f"event-{index}" for index in range(12)]
    ledger = {
        "checkpoint_tie_groups": [{"chronological_members": [{"source_event_id": event, "ranking_key_sha256": _h(event)} for event in events]}],
        "view_full_order": {view: list(events) for view in prefreeze.VIEWS},
    }
    monkeypatch.setattr(paired.fcd2, "_ledger", lambda trace: ledger)
    questions: list[dict[str, object]] = []
    traces: dict[str, object] = {}
    for index in range(1986):
        item = f"raw-item-{index}"
        questions.append({"item_id": item, "conversation_id": f"conversation-{index % 10}", "category": "poison-a", "gold": {"official_exact": ["poison"]}})
        traces[item] = {"retrieval_ranking": {"query_sha256": _h(f"q{index}"), "input_sha256": _h(f"i{index}"), "view_digests": {view: _h(f"{view}:{index}") for view in prefreeze.VIEWS}, "encoder_identity": "frozen"}}
    clean = paired.sanitize_rank_source({"questions": questions, "product_traces": traces})
    for row in questions:
        row["category"] = "poison-b"
        row["gold"] = {"official_exact": ["different"]}
    poisoned = paired.sanitize_rank_source({"questions": questions, "product_traces": traces})
    assert clean == poisoned
    forbidden = {"category", "gold", "query", "answer", "text", "source_event_id", "ranking_key", "item_id", "conversation_id"}
    assert not (_walk_keys(clean) & forbidden)
    train_groups = {row["group_token"] for row in clean["partitions"]["train"]["items"]}
    dev_groups = {row["group_token"] for row in clean["partitions"]["dev"]["items"]}
    assert len(train_groups) == len(dev_groups) == 5
    assert train_groups.isdisjoint(dev_groups)


def test_paired_envelope_replays_raw_and_p5_with_identical_anchor_and_universe() -> None:
    source, excluded = _full_source()
    envelope, freeze = paired.build_paired_envelope(_study(source, excluded=excluded), "train", source, _membership(excluded, source))
    assert envelope["schema"] == paired.PAIR_SCHEMA
    assert len(freeze["items"]) == 991
    for item, frozen in zip(envelope["items"], freeze["items"]):
        raw = prefreeze._production_trace(item["raw_trace"], route="raw")
        p5 = prefreeze._production_trace(item["p5_trace"], route="p5")
        assert raw["A"] == p5["A"]
        assert set(raw["tokens"]) == set(p5["tokens"])
        assert item["raw_trace"]["aerp4_raw_anchored_p5"]["config"] == prefreeze._config("+inf")
        assert item["p5_trace"]["aerp4_raw_anchored_p5"]["config"] == prefreeze._config("-inf")
        assert frozen["A_hex"] == raw["A"].hex()


def test_evidence_token_helper_is_the_trace_and_prefreeze_token() -> None:
    item = _rank_item("evidence", size=12)
    source = item["rank_source"]
    rank_token = source["view_token_orders"]["raw_bm25"][0]
    original_ranking_key = _h("evidence:ranking-key:0")
    expected = paired.evidence_token_from_ranking_key_sha256(original_ranking_key)
    assert source["evidence_token_by_ranking_token"][rank_token] == expected
    trace = paired._trace(item, policy=paired.RawAnchoredP5Policy(math.inf), route="raw")
    selected = next(row for row in trace["selected"] if row["source_event_id"] == rank_token)
    assert selected["ranking_key_sha256"] == expected


def test_source_crosswalk_uses_gate_identity_and_input_receipts() -> None:
    source = _source(_rank_item("a"), _rank_item("b"))
    rows = source["partitions"]["train"]["items"]
    expected = gate._crosswalk([
        {key: row[key] for key in ("item_token", "group_token", "campaign_token")}
        | {key: row["rank_source"][key] for key in ("query_sha256", "input_sha256")}
        for row in rows
    ])
    assert source["partitions"]["train"]["crosswalk_sha256"] == expected


def test_membership_receipt_is_required_and_fails_closed_for_forged_exclusions() -> None:
    source, excluded = _full_source()
    study = _study(source, excluded=excluded)
    assert inspect.signature(paired.build_paired_envelope).parameters["membership_receipt"].default is inspect.Parameter.empty
    forged = _membership(excluded, source)
    forged["source_artifact_sha256"] = _h("forged")
    with pytest.raises(ValueError, match="does not bind"):
        paired.build_paired_envelope(study, "train", source, forged)
    wrong = _membership(excluded, source)
    wrong["excluded_item_tokens"] = list(excluded)[:3] + [_h("not-a-source-member")]
    with pytest.raises(ValueError, match="not source members"):
        paired.build_paired_envelope(study, "train", source, wrong)


def test_custody_produced_exclusion_receipt_binds_the_real_paired_build() -> None:
    source, expected_excluded = _full_source()
    label_rows = []
    for partition in ("train", "dev"):
        for row in source["partitions"][partition]["items"]:
            excluded = row["item_token"] in expected_excluded
            label_rows.append({
                "item_token": row["item_token"],
                "group_token": row["group_token"],
                "campaign_token": row["campaign_token"],
                "category": "poison-not-consumed-by-paired",
                "official_exact": {
                    "resolved_dialog_ids": [] if excluded else ["opaque-label-token"],
                    "unresolved_evidence_item_count": 0,
                    "evidence_item_count": 0 if excluded else 1,
                },
            })
    built = custody.build_label_custody(
        source,
        {"items": label_rows},
        source_artifact_sha256=_h("label-source"),
        producer_sha256=_h("label-producer"),
    )
    receipt = built["exclusion_receipt"]
    assert set(receipt["excluded_item_tokens"]) == expected_excluded
    envelope, freeze = paired.build_paired_envelope(_study(source, excluded=expected_excluded), "dev", source, receipt)
    assert len(envelope["items"]) == len(freeze["items"]) == 991


def test_real_rrf_tail_tie_is_allowed_when_top10_boundary_is_strict() -> None:
    item = _rank_item("tail", size=50)
    tokens = item["rank_source"]["view_token_orders"]["raw_bm25"]
    dense = [None] * 50
    dense[29] = tokens[9]   # (raw_bm25, raw_dense) rank pair (10, 30)
    dense[23] = tokens[11]  # (12, 24), exactly equal in producer float arithmetic.
    remaining = [token for token in tokens if token not in {tokens[9], tokens[11]}]
    for index in range(50):
        if dense[index] is None:
            dense[index] = remaining.pop(0)
    item["rank_source"]["view_token_orders"]["raw_dense"] = dense
    trace = paired._trace(item, policy=paired.RawAnchoredP5Policy(math.inf), route="raw")
    scores = [row["final_rrf"] for row in trace["selected"]]
    assert any(scores[index] == scores[index + 1] for index in range(10, len(scores) - 1))
    assert scores[9] > scores[10]
    prefreeze._production_trace(trace, route="raw")


def test_top10_cutoff_tie_fails_closed_before_trace_serialization() -> None:
    item = _rank_item("cutoff", size=11)

    class TiedPolicy:
        tau = math.inf

        def decide(self, *, ranks: object, ranking_keys_by_id: dict[str, str], rrf_k: int) -> FusionRoutingDecision:
            identifiers = list(ranking_keys_by_id)
            totals = {identifier: float(20 - index) for index, identifier in enumerate(identifiers)}
            totals[identifiers[9]] = totals[identifiers[10]] = 1.0
            return FusionRoutingDecision(route="raw", totals=tuple(totals.items()), effective_weights=tuple(prefreeze.RAW_WEIGHTS.items()))

    with pytest.raises(ValueError, match="Top10 cutoff tie"):
        paired._trace(item, policy=TiedPolicy(), route="raw")  # type: ignore[arg-type]


def test_paired_builder_has_no_label_input_or_local() -> None:
    signature = inspect.signature(paired.build_paired_envelope)
    assert not ({"labels", "category", "gold"} & set(signature.parameters))
    assert not ({"labels", "category", "gold"} & set(paired.build_paired_envelope.__code__.co_varnames))
