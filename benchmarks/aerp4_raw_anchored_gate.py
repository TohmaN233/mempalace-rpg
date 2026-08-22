"""Frozen train-select / one-shot-dev gate for the RawAnchored P5 router.

This module intentionally accepts only opaque, label-separated receipts.  It is
not a retrieval runner and it never sees corpus text or query identities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


STUDY_SCHEMA = "aerp4-raw-anchored-gate-study-v1"
RANKING_FREEZE_SCHEMA = "aerp4-raw-anchored-ranking-freeze-v1"
LABELS_SCHEMA = "aerp4-raw-anchored-labels-v1"
TAU_FREEZE_SCHEMA = "aerp4-raw-anchored-tau-freeze-v1"
DEV_EVAL_SCHEMA = "aerp4-raw-anchored-dev-eval-v1"
GUARDRAIL_SCHEMA = "aerp4-raw-anchored-guardrail-evidence-v1"
TOP_K = 10
_HEX = set("0123456789abcdef")
_FORBIDDEN = ("query", "text", "observation", "checkpoint", "ranking_key", "raw_id", "category", "transcript")
_SAFE_RECEIPT_KEYS = {"raw", "p5", "raw_top10", "p5_top10", "raw_bm25", "raw_dense", "observation_bm25", "observation_dense", "checkpoint_dense", "combo_dense", "query_sha256", "input_sha256", "view_digests"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _exact_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    if set(value) != allowed:
        raise ValueError(f"{name} has unknown or missing fields")


def _validate_publication(value: Any, *, implementation_sha256: str | None = None, expected_analyzer_git_state: Mapping[str, Any] | None = None, expected_input_sha256s: Sequence[str] | None = None) -> None:
    publication = _mapping(value, "publication")
    _exact_keys(publication, {"input_receipts", "analyzer_git_state", "implementation_sha256"}, "publication")
    _token(publication["implementation_sha256"], "publication implementation sha")
    if implementation_sha256 is not None and publication["implementation_sha256"] != implementation_sha256:
        raise ValueError("publication implementation binding mismatch")
    state = _mapping(publication["analyzer_git_state"], "publication git state")
    _exact_keys(state, {"git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes"}, "publication git state")
    if state["git_dirty"] is not False or not isinstance(state["commit_diff_bytes"], int) or isinstance(state["commit_diff_bytes"], bool) or state["commit_diff_bytes"] < 0:
        raise ValueError("publication git state malformed")
    for key in ("worktree_status_sha256", "commit_diff_sha256"):
        _token(state[key], f"publication {key}")
    for key in ("git_head", "git_tree"):
        if not isinstance(state[key], str) or len(state[key]) != 40 or any(char not in _HEX for char in state[key]):
            raise ValueError("publication git object malformed")
    if expected_analyzer_git_state is not None and dict(state) != dict(expected_analyzer_git_state):
        raise ValueError("publication analyzer git binding mismatch")
    receipts = _list(publication["input_receipts"], "publication input receipts")
    if not receipts:
        raise ValueError("publication needs input receipts")
    receipt_shas: list[str] = []; paths: set[str] = set()
    for receipt in receipts:
        row = _mapping(receipt, "publication input receipt")
        _exact_keys(row, {"path_sha256", "sha256"}, "publication input receipt")
        path = _token(row["path_sha256"], "publication input path receipt"); digest = _token(row["sha256"], "publication input sha")
        if path in paths: raise ValueError("publication input paths are not unique")
        paths.add(path); receipt_shas.append(digest)
    if expected_input_sha256s is not None:
        expected = [_token(value, "expected publication input sha") for value in expected_input_sha256s]
        if sorted(receipt_shas) != sorted(expected): raise ValueError("publication input SHA multiset mismatch")


def _token(value: Any, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(c not in _HEX for c in value):
        raise ValueError(f"{name} must be a lowercase 64-hex token")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return float(value)


def _float_receipt(value: Any, name: str) -> float:
    if value == "+inf":
        return math.inf
    if value == "-inf":
        return -math.inf
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a float.hex receipt or infinity")
    try:
        parsed = float.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not float.hex") from exc
    if not math.isfinite(parsed) or parsed.hex() != value:
        raise ValueError(f"{name} is not a canonical finite float.hex receipt")
    return parsed


def _receipt(value: float) -> str:
    if value == math.inf:
        return "+inf"
    if value == -math.inf:
        return "-inf"
    if not math.isfinite(value):
        raise ValueError("tau must be finite or infinity")
    return value.hex()


def _no_plaintext(value: Any, name: str = "receipt") -> None:
    """Reject forbidden schema keys; payloads are allowed only opaque hashes/tokens."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} has non-string key")
            lowered = key.lower()
            if any(word in lowered for word in _FORBIDDEN) and not (lowered in _SAFE_RECEIPT_KEYS or lowered.endswith("_sha256")):
                raise ValueError(f"{name} exposes forbidden field {key!r}")
            _no_plaintext(child, f"{name}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _no_plaintext(child, f"{name}[{index}]")


def _candidate_taus_impl(anchor_values: Iterable[float]) -> tuple[float, ...]:
    """All policy-distinguishing thresholds, with exact floating receipts.

    P5 routes when ``A >= tau``.  Therefore the only candidates are infinities
    and strict binary-float midpoints of adjacent unique finite A values.
    """
    values = sorted({_finite(value, "anchor A") for value in anchor_values})
    if not values:
        raise ValueError("at least one finite anchor A is required")
    output: list[float] = [-math.inf]
    for left, right in zip(values, values[1:]):
        midpoint = (left + right) / 2.0
        if not math.isfinite(midpoint) or not left < midpoint < right:
            raise ValueError("adjacent anchor values have no strict float midpoint")
        output.append(midpoint)
    output.append(math.inf)
    return tuple(output)


def candidate_taus(anchor_values: Iterable[float]) -> tuple[float, ...]:
    return _candidate_taus_impl(anchor_values)


@dataclass(frozen=True)
class BoundInput:
    """Bytes frozen at admission; later path rereads are checked byte-for-byte."""

    path: Path
    expected_sha256: str
    raw_bytes: bytes

    @classmethod
    def load(cls, path: Path | str, expected_sha256: str) -> "BoundInput":
        resolved = Path(path).resolve()
        digest = _token(expected_sha256, "expected_sha256")
        raw = resolved.read_bytes()
        if _sha_bytes(raw) != digest:
            raise ValueError("input SHA-256 mismatch")
        return cls(resolved, digest, raw)

    def verify_unchanged(self) -> None:
        current = self.path.read_bytes()
        if current != self.raw_bytes or _sha_bytes(current) != self.expected_sha256:
            raise RuntimeError(f"TOCTOU input drift: {self.path}")

    def json(self, name: str) -> Mapping[str, Any]:
        try:
            value = json.loads(self.raw_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{name} is not valid UTF-8 JSON") from exc
        if _canonical_bytes(value) != self.raw_bytes:
            raise ValueError(f"{name} is not canonical JSON")
        return _mapping(value, name)


def _partition_spec(study: Mapping[str, Any], partition: str) -> Mapping[str, Any]:
    partitions = _mapping(study.get("partitions"), "study.partitions")
    spec = _mapping(partitions.get(partition), f"study.partitions.{partition}")
    _exact_keys(spec, {"campaign_sha256", "group_sha256", "item_sha256", "crosswalk_sha256"}, f"study.partitions.{partition}")
    for field in ("campaign_sha256", "group_sha256", "item_sha256", "crosswalk_sha256"):
        _token(spec.get(field), f"study.{partition}.{field}")
    return spec


def _crosswalk(rows: Sequence[Mapping[str, Any]]) -> str:
    return _sha(sorted(({key: row[key] for key in ("item_token", "group_token", "campaign_token", "query_sha256", "input_sha256")} for row in rows), key=lambda row: row["item_token"]))


def _label_payload_sha(items: Sequence[Mapping[str, Any]]) -> str:
    return _sha(sorted(({key: row[key] for key in ("item_token", "gold_tokens", "unresolved_evidence_item_count", "evidence_item_count")} for row in items), key=lambda row: row["item_token"]))


def _slot_path_sha(path: Path | str) -> str:
    return _sha(str(Path(path).resolve()))


def _analyzer(study: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = _mapping(_mapping(study["analyzers"], "study analyzers")[name], f"study analyzer {name}")
    _exact_keys(value, {"implementation_sha256", "git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes"}, f"study analyzer {name}")
    _token(value["implementation_sha256"], f"study analyzer {name} implementation")
    _validate_publication({"input_receipts": [{"path_sha256": "0" * 64, "sha256": "0" * 64}], "analyzer_git_state": {key: value[key] for key in ("git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes")}, "implementation_sha256": value["implementation_sha256"]})
    return value


def _slot(study: Mapping[str, Any], *, stage: str, partition: str, output: Path | str) -> None:
    slots = _mapping(study["output_slots"], "study.output_slots")
    expected = _mapping(slots.get(stage), f"study.output_slots.{stage}")
    _exact_keys(expected, {"stage", "partition", "path_sha256"}, f"study.output_slots.{stage}")
    if expected["stage"] != stage or expected["partition"] != partition or expected["path_sha256"] != _slot_path_sha(output):
        raise ValueError("output does not match pre-registered stage slot")


def _validate_study(study: Mapping[str, Any]) -> None:
    _no_plaintext(study, "study")
    if study.get("schema") != STUDY_SCHEMA or study.get("status") != "frozen":
        raise ValueError("study must be frozen v1")
    _exact_keys(study, {"schema", "status", "dataset_sha256", "producer", "retrieval", "splits", "partitions", "label_custodians", "guardrail_custodian", "selection", "output_slots", "analyzers"}, "study")
    _token(study.get("dataset_sha256"), "study.dataset_sha256")
    producer = _mapping(study["producer"], "study.producer"); _exact_keys(producer, {"artifact_sha256", "git_head", "git_tree", "retrieval_implementation_sha256"}, "study.producer"); _token(producer["artifact_sha256"], "study producer artifact"); _token(producer["retrieval_implementation_sha256"], "study producer retrieval implementation")
    for key in ("git_head", "git_tree"):
        if not isinstance(producer[key], str) or len(producer[key]) != 40 or any(char not in _HEX for char in producer[key]): raise ValueError("study producer git receipt malformed")
    retrieval = _mapping(study["retrieval"], "study.retrieval"); _exact_keys(retrieval, {"implementation_sha256", "raw_config_sha256", "p5_config_sha256"}, "study.retrieval")
    for key in retrieval: _token(retrieval[key], f"study retrieval {key}")
    if producer["retrieval_implementation_sha256"] != retrieval["implementation_sha256"]: raise ValueError("producer/retrieval implementation mismatch")
    splits = _mapping(study["splits"], "study.splits"); _exact_keys(splits, {"source_separated_crosswalk_sha256", "question_random_split"}, "study.splits"); _token(splits["source_separated_crosswalk_sha256"], "study split crosswalk")
    if splits["question_random_split"] is not False: raise ValueError("question-random split is forbidden")
    analyzers = _mapping(study["analyzers"], "study.analyzers"); _exact_keys(analyzers, {"gate", "prefreeze", "label", "guardrail"}, "study.analyzers")
    for key in analyzers: _analyzer(study, key)
    slots = _mapping(study["output_slots"], "study.output_slots"); _exact_keys(slots, {"train_prefreeze", "dev_prefreeze", "tau_select", "dev_eval"}, "study.output_slots")
    for stage, partition in (("train_prefreeze", "train"), ("dev_prefreeze", "dev"), ("tau_select", "train"), ("dev_eval", "dev")):
        expected = _mapping(slots[stage], f"study output slot {stage}"); _exact_keys(expected, {"stage", "partition", "path_sha256"}, f"study output slot {stage}")
        if expected["stage"] != stage or expected["partition"] != partition: raise ValueError("study output slot stage binding mismatch")
        _token(expected["path_sha256"], "study output slot path receipt")
    _exact_keys(_mapping(study["partitions"], "study.partitions"), {"train", "dev"}, "study.partitions")
    for partition in ("train", "dev"):
        _partition_spec(study, partition)
    custodians = _mapping(study["label_custodians"], "study.label_custodians"); _exact_keys(custodians, {"train", "dev"}, "study.label_custodians")
    for partition in ("train", "dev"):
        value = _mapping(custodians[partition], f"label custodian {partition}"); _exact_keys(value, {"source_artifact_sha256", "producer_sha256", "crosswalk_sha256", "label_payload_sha256"}, f"label custodian {partition}")
        for key in value: _token(value[key], f"label custodian {key}")
        if value["crosswalk_sha256"] != _partition_spec(study, partition)["crosswalk_sha256"]: raise ValueError("label custodian crosswalk mismatch")
    guardrail = _mapping(study["guardrail_custodian"], "study.guardrail_custodian"); _exact_keys(guardrail, {"source_artifact_sha256", "producer_sha256", "result_payload_sha256s"}, "study.guardrail_custodian")
    for key in ("source_artifact_sha256", "producer_sha256"): _token(guardrail[key], f"guardrail custodian {key}")
    result_digests = _mapping(guardrail["result_payload_sha256s"], "guardrail result payloads"); _exact_keys(result_digests, {"acl", "safety", "exact_replay", "performance"}, "guardrail result payloads")
    for value in result_digests.values(): _token(value, "guardrail result payload")
    selection = _mapping(study.get("selection"), "study.selection")
    _exact_keys(selection, {"bootstrap", "go_gates"}, "study.selection")
    bootstrap = _mapping(selection.get("bootstrap"), "study.selection.bootstrap")
    _exact_keys(bootstrap, {"seed", "resamples", "percentiles"}, "study.selection.bootstrap")
    if not isinstance(bootstrap.get("seed"), int) or isinstance(bootstrap["seed"], bool):
        raise ValueError("bootstrap.seed must be an integer")
    if not isinstance(bootstrap.get("resamples"), int) or isinstance(bootstrap["resamples"], bool) or bootstrap["resamples"] < 1:
        raise ValueError("bootstrap.resamples must be positive")
    percentiles = _list(bootstrap.get("percentiles"), "bootstrap.percentiles")
    if len(percentiles) != 2 or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= float(x) <= 100 for x in percentiles) or float(percentiles[0]) >= float(percentiles[1]):
        raise ValueError("bootstrap percentiles must be increasing [0,100]")
    gates = _mapping(selection.get("go_gates"), "study.selection.go_gates")
    _exact_keys(gates, {"min_route_fraction", "guardrails"}, "study.selection.go_gates")
    if not isinstance(gates.get("min_route_fraction"), (int, float)) or not 0.0 <= float(gates["min_route_fraction"]) <= 1.0:
        raise ValueError("go_gates.min_route_fraction must be [0,1]")
    if isinstance(gates["min_route_fraction"], bool): raise ValueError("go_gates.min_route_fraction must be numeric")
    guardrails = _mapping(gates["guardrails"], "study guardrail specs")
    _exact_keys(guardrails, {"acl", "safety", "exact_replay", "performance"}, "study guardrail specs")
    if any(value is not True for value in guardrails.values()): raise ValueError("guardrail specs must freeze required true gates")


def _parse_ranking_freeze(value: Mapping[str, Any], study: Mapping[str, Any], partition: str) -> list[dict[str, Any]]:
    _no_plaintext(value, "ranking-freeze")
    if value.get("schema") != RANKING_FREEZE_SCHEMA or value.get("status") != "complete":
        raise ValueError("ranking freeze must be complete v1")
    if set(value) != {"schema", "status", "study_sha256", "partition", "producer", "paired_input_sha256", "items", "phase_ledger", "publication"}:
        raise ValueError("ranking freeze has unknown or missing fields")
    producer = _mapping(value["producer"], "ranking freeze producer")
    _exact_keys(producer, {"artifact_sha256", "git_head", "git_tree", "retrieval_implementation_sha256"}, "ranking freeze producer")
    if producer != _mapping(study["producer"], "study producer"):
        raise ValueError("ranking freeze producer does not match study")
    _token(value["paired_input_sha256"], "ranking freeze paired input sha")
    phase = _mapping(value["phase_ledger"], "ranking freeze phase ledger")
    _exact_keys(phase, {"status", "phase", "terminal_phase"}, "ranking freeze phase ledger")
    if phase != {"status": "complete", "phase": "atomic_publish_ready", "terminal_phase": "atomic_publish_ready"}:
        raise ValueError("ranking freeze phase is not publication ready")
    analyzer = _analyzer(study, "prefreeze"); _validate_publication(value["publication"], implementation_sha256=analyzer["implementation_sha256"], expected_analyzer_git_state={key: analyzer[key] for key in analyzer if key != "implementation_sha256"}, expected_input_sha256s=[_sha(study), value["paired_input_sha256"]])
    if value.get("partition") != partition:
        raise ValueError("ranking freeze partition mismatch")
    if value.get("study_sha256") != _sha(study):
        raise ValueError("ranking freeze study binding mismatch")
    rows = _list(value.get("items"), "ranking-freeze.items")
    if not rows:
        raise ValueError("ranking freeze has no items")
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        row = dict(_mapping(raw, f"ranking-freeze.items[{index}]"))
        _exact_keys(row, {"item_token", "group_token", "campaign_token", "query_sha256", "input_sha256", "A_hex", "numerator", "denominator", "authorized_tokens", "authorized_tokens_sha256", "raw_top10", "p5_top10", "raw_top10_sha256", "p5_top10_sha256"}, "ranking row")
        for key in ("item_token", "group_token", "campaign_token"):
            _token(row.get(key), f"ranking row.{key}")
        for key in ("query_sha256", "input_sha256"):
            _token(row.get(key), f"ranking row.{key}")
        a = _float_receipt(row.get("A_hex"), "ranking row.A_hex")
        numerator = _finite(row.get("numerator"), "ranking row.numerator")
        denominator = _finite(row.get("denominator"), "ranking row.denominator")
        if denominator <= 0 or not math.isclose(a, numerator / denominator, rel_tol=0.0, abs_tol=0.0):
            raise ValueError("ranking row A/numerator/denominator mismatch")
        authorized = _list(row.get("authorized_tokens"), "ranking row.authorized_tokens")
        if not authorized or len(authorized) != len(set(authorized)):
            raise ValueError("authorized tokens must be non-empty and unique")
        for token in authorized:
            _token(token, "authorized token")
        if _token(row.get("authorized_tokens_sha256"), "ranking row.authorized_tokens_sha256") != _sha(authorized):
            raise ValueError("authorized token digest mismatch")
        for arm in ("raw", "p5"):
            tokens = _list(row.get(f"{arm}_top10"), f"ranking row.{arm}_top10")
            if len(tokens) > TOP_K or len(tokens) != len(set(tokens)):
                raise ValueError(f"{arm} top10 must be unique and <= {TOP_K}")
            for token in tokens:
                _token(token, f"ranking row.{arm} top token")
                if token not in authorized:
                    raise ValueError(f"{arm} top10 contains unauthorized token")
            digest = _token(row.get(f"{arm}_top10_sha256"), f"ranking row.{arm}_top10_sha256")
            if digest != _sha(tokens):
                raise ValueError(f"{arm} top10 digest mismatch")
        parsed.append(row)
    for key, digest_field in (("item_token", "item_sha256"), ("group_token", "group_sha256"), ("campaign_token", "campaign_sha256")):
        tokens = [row[key] for row in parsed]
        if key == "item_token" and len(tokens) != len(set(tokens)):
            raise ValueError("ranking freeze has duplicate item token")
        if _sha(sorted(tokens)) != _partition_spec(study, partition)[digest_field]:
            raise ValueError(f"ranking freeze {key} membership digest mismatch")
    if _crosswalk(parsed) != _partition_spec(study, partition)["crosswalk_sha256"]:
        raise ValueError("ranking freeze crosswalk mismatch")
    return parsed


def _parse_labels(value: Mapping[str, Any], study: Mapping[str, Any], partition: str) -> dict[str, dict[str, Any]]:
    _no_plaintext(value, "labels")
    if value.get("schema") != LABELS_SCHEMA or value.get("status") != "complete" or value.get("partition") != partition:
        raise ValueError("labels schema/status/partition mismatch")
    _exact_keys(value, {"schema", "status", "study_sha256", "partition", "source_artifact_sha256", "producer_sha256", "crosswalk_sha256", "items", "publication"}, "labels")
    if value.get("study_sha256") != _sha(study):
        raise ValueError("labels study binding mismatch")
    custodian = _mapping(_mapping(study["label_custodians"], "label custodians")[partition], "label custodian")
    for key in ("source_artifact_sha256", "producer_sha256", "crosswalk_sha256"):
        if value[key] != custodian[key]: raise ValueError("labels custodian binding mismatch")
    analyzer = _analyzer(study, "label"); _validate_publication(value["publication"], implementation_sha256=analyzer["implementation_sha256"], expected_analyzer_git_state={key: analyzer[key] for key in analyzer if key != "implementation_sha256"}, expected_input_sha256s=[_sha(study), custodian["source_artifact_sha256"]])
    rows = _list(value.get("items"), "labels.items")
    output: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(rows):
        row = dict(_mapping(raw, f"labels.items[{index}]"))
        _exact_keys(row, {"item_token", "gold_tokens", "unresolved_evidence_item_count", "evidence_item_count"}, "label row")
        token = _token(row.get("item_token"), "label item_token")
        if token in output:
            raise ValueError("labels repeat item token")
        gold = _list(row.get("gold_tokens"), "label gold_tokens")
        for candidate in gold:
            _token(candidate, "gold token")
        unresolved = row.get("unresolved_evidence_item_count")
        if not isinstance(unresolved, int) or isinstance(unresolved, bool) or unresolved < 0:
            raise ValueError("unresolved evidence count must be non-negative integer")
        denominator = row.get("evidence_item_count")
        if not isinstance(denominator, int) or isinstance(denominator, bool) or denominator != len(gold) + unresolved:
            raise ValueError("labels denominator must retain multiplicity plus unresolved")
        output[token] = row
    if _sha(sorted(output)) != _partition_spec(study, partition)["item_sha256"]:
        raise ValueError("labels item membership digest mismatch")
    if _label_payload_sha(list(output.values())) != custodian["label_payload_sha256"]:
        raise ValueError("labels payload mismatch")
    return output


def _score(top: Sequence[str], label: Mapping[str, Any]) -> float:
    denominator = int(label["evidence_item_count"])
    if denominator <= 0:
        raise ValueError("zero official-exact denominator")
    returned = set(top)
    return sum(token in returned for token in label["gold_tokens"]) / denominator


def _metrics(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]], arm: str) -> dict[str, Any]:
    values: list[float] = []
    by_group: dict[str, list[float]] = {}
    for row in rows:
        value = _score(row[f"{arm}_top10"], labels[row["item_token"]])
        values.append(value)
        by_group.setdefault(row["group_token"], []).append(value)
    group_means = {key: sum(values) / len(values) for key, values in by_group.items()}
    return {"question_macro_recall_at_10": sum(values) / len(values), "conversation_macro_recall_at_10": sum(group_means.values()) / len(group_means), "question_count": len(values), "conversation_count": len(group_means), "group_means": group_means}


def _route(row: Mapping[str, Any], tau: float) -> str:
    return "p5" if _float_receipt(row["A_hex"], "A_hex") >= tau else "raw"


def _gated_rows(rows: Sequence[Mapping[str, Any]], tau: float) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        route = _route(row, tau)
        copied = dict(row)
        copied["gated_top10"] = list(row[f"{route}_top10"])
        copied["route"] = route
        result.append(copied)
    return result


def _paired_bootstrap(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]], left: str, right: str, *, seed: int, resamples: int, percentiles: Sequence[float]) -> dict[str, Any]:
    groups: dict[str, list[float]] = {}
    for row in rows:
        delta = _score(row[f"{left}_top10"], labels[row["item_token"]]) - _score(row[f"{right}_top10"], labels[row["item_token"]])
        groups.setdefault(row["group_token"], []).append(delta)
    means = [sum(values) / len(values) for _group, values in sorted(groups.items())]
    point = sum(means) / len(means)
    rng = random.Random(seed)
    samples = [sum(rng.choice(means) for _ in means) / len(means) for _ in range(resamples)]
    ordered = sorted(samples)
    def percentile(value: float) -> float:
        position = (len(ordered) - 1) * value / 100.0
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)
    return {"point_estimate": point, "groups": len(means), "seed": seed, "resamples": resamples, "percentiles": list(percentiles), "interval": [percentile(float(percentiles[0])), percentile(float(percentiles[1]))], "replicates_sha256": _sha([float.hex(value) for value in samples])}


def _choose(rows: Sequence[Mapping[str, Any]], labels: Mapping[str, Mapping[str, Any]]) -> tuple[float, dict[str, Any], list[dict[str, Any]]]:
    candidates = _candidate_taus_impl(_float_receipt(row["A_hex"], "A_hex") for row in rows)
    scored: list[dict[str, Any]] = []
    for tau in candidates:
        gated = _gated_rows(rows, tau)
        metrics = _metrics(gated, labels, "gated")
        p5_fraction = sum(row["route"] == "p5" for row in gated) / len(gated)
        scored.append({"tau_receipt": _receipt(tau), "objective": metrics["conversation_macro_recall_at_10"], "route_fraction": p5_fraction, "question_macro_recall_at_10": metrics["question_macro_recall_at_10"]})
    # Larger numeric tau breaks a total objective tie, including +inf.
    best = max(scored, key=lambda item: (item["objective"], _float_receipt(item["tau_receipt"], "tau_receipt")))
    return _float_receipt(best["tau_receipt"], "tau_receipt"), best, scored


def select_and_freeze(study: Mapping[str, Any], train_rankings: Mapping[str, Any], train_labels: Mapping[str, Any]) -> dict[str, Any]:
    """Enumerate frozen train thresholds and publish exactly one selected tau."""
    _validate_study(study)
    rankings = _parse_ranking_freeze(train_rankings, study, "train")
    labels = _parse_labels(train_labels, study, "train")
    if set(row["item_token"] for row in rankings) != set(labels):
        raise ValueError("ranking/label train item sets differ")
    tau, best, candidate_rows = _choose(rankings, labels)
    route_fraction = float(best["route_fraction"])
    threshold = float(_mapping(_mapping(study["selection"], "selection")["go_gates"], "go_gates")["min_route_fraction"])
    status = "complete" if min(route_fraction, 1.0 - route_fraction) >= threshold else "STOP_ROUTER_DEGENERATE"
    return {
        "schema": TAU_FREEZE_SCHEMA, "status": status, "study_sha256": _sha(study),
        "partition": "train", "selection_objective": "official_exact_conversation_macro_recall_at_10",
        "comparator": "A>=tau_routes_p5", "selected_tau": _receipt(tau), "selected": best,
        "candidate_count": len(candidate_rows), "candidates_sha256": _sha(candidate_rows),
        "candidate_tau_receipts": [row["tau_receipt"] for row in candidate_rows],
        "train_ranking_sha256": _sha(train_rankings), "train_labels_sha256": _sha(train_labels),
        "frozen_train_rankings": train_rankings, "frozen_train_labels": train_labels,
        "train_membership_tokens": {key: sorted(row[key] for row in rankings) for key in ("item_token", "group_token", "campaign_token")},
        "protocol": {"candidate_space": "infinities_and_strict_adjacent_float_midpoints", "tie_break": "larger_numeric_tau", "label_access": "train_only"},
    }


def _validate_tau_freeze(policy: Mapping[str, Any], study: Mapping[str, Any]) -> float:
    _no_plaintext(policy, "tau-freeze")
    expected = {"schema", "status", "study_sha256", "partition", "selection_objective", "comparator", "selected_tau", "selected", "candidate_count", "candidates_sha256", "candidate_tau_receipts", "train_ranking_sha256", "train_labels_sha256", "frozen_train_rankings", "frozen_train_labels", "train_membership_tokens", "protocol"}
    if set(policy) != expected | {"publication"}:
        raise ValueError("tau freeze has unknown or missing fields")
    analyzer = _analyzer(study, "gate"); _validate_publication(policy["publication"], implementation_sha256=analyzer["implementation_sha256"], expected_analyzer_git_state={key: analyzer[key] for key in analyzer if key != "implementation_sha256"}, expected_input_sha256s=[_sha(study), _sha(_mapping(policy.get("frozen_train_rankings"), "tau rankings")), _sha(_mapping(policy.get("frozen_train_labels"), "tau labels"))])
    if policy.get("schema") != TAU_FREEZE_SCHEMA or policy.get("status") not in {"complete", "STOP_ROUTER_DEGENERATE"}:
        raise ValueError("invalid tau freeze")
    if policy.get("study_sha256") != _sha(study) or policy.get("partition") != "train":
        raise ValueError("tau freeze binding mismatch")
    if policy.get("selection_objective") != "official_exact_conversation_macro_recall_at_10":
        raise ValueError("tau freeze objective mismatch")
    if policy.get("comparator") != "A>=tau_routes_p5":
        raise ValueError("tau freeze comparator mismatch")
    if policy.get("protocol") != {"candidate_space": "infinities_and_strict_adjacent_float_midpoints", "tie_break": "larger_numeric_tau", "label_access": "train_only"}:
        raise ValueError("tau freeze protocol mismatch")
    candidates = _list(policy.get("candidate_tau_receipts"), "tau freeze candidate_tau_receipts")
    if len(candidates) != policy.get("candidate_count") or not candidates:
        raise ValueError("tau freeze candidate count mismatch")
    parsed = [_float_receipt(value, "tau freeze candidate") for value in candidates]
    if parsed != sorted(parsed) or len(parsed) != len(set(parsed)):
        raise ValueError("tau freeze candidates are not unique sorted thresholds")
    if policy.get("selected_tau") not in candidates:
        raise ValueError("selected tau is not a frozen candidate")
    frozen_rankings = _mapping(policy.get("frozen_train_rankings"), "tau freeze frozen_train_rankings")
    frozen_labels = _mapping(policy.get("frozen_train_labels"), "tau freeze frozen_train_labels")
    if policy.get("train_ranking_sha256") != _sha(frozen_rankings) or policy.get("train_labels_sha256") != _sha(frozen_labels):
        raise ValueError("tau freeze train input digest mismatch")
    rankings = _parse_ranking_freeze(frozen_rankings, study, "train")
    labels = _parse_labels(frozen_labels, study, "train")
    if set(row["item_token"] for row in rankings) != set(labels):
        raise ValueError("tau freeze train ranking/label set mismatch")
    chosen, selected, rows = _choose(rankings, labels)
    expected_status = "complete" if min(float(selected["route_fraction"]), 1.0 - float(selected["route_fraction"])) >= float(_mapping(_mapping(study["selection"], "selection")["go_gates"], "go_gates")["min_route_fraction"]) else "STOP_ROUTER_DEGENERATE"
    if (policy.get("selected_tau") != _receipt(chosen) or policy.get("selected") != selected or policy.get("candidate_count") != len(rows) or policy.get("candidates_sha256") != _sha(rows) or policy.get("candidate_tau_receipts") != [row["tau_receipt"] for row in rows] or policy.get("status") != expected_status):
        raise ValueError("tau freeze selection proof mismatch")
    membership = _mapping(policy.get("train_membership_tokens"), "tau freeze train membership")
    _exact_keys(membership, {"item_token", "group_token", "campaign_token"}, "tau freeze train membership")
    for key in ("item_token", "group_token", "campaign_token"):
        values = _list(membership.get(key), f"tau freeze membership.{key}")
        if not values or any(_token(value, f"tau freeze membership.{key}") != value for value in values):
            raise ValueError("tau freeze membership is malformed")
        digest = {"item_token": "item_sha256", "group_token": "group_sha256", "campaign_token": "campaign_sha256"}[key]
        if _sha(sorted(values)) != _partition_spec(study, "train")[digest]:
            raise ValueError("tau freeze membership digest mismatch")
        if values != sorted(row[key] for row in rankings):
            raise ValueError("tau freeze membership does not match frozen rankings")
    return _float_receipt(policy.get("selected_tau"), "selected_tau")


def _validate_guardrails(value: Mapping[str, Any], study: Mapping[str, Any]) -> None:
    _no_plaintext(value, "guardrail evidence")
    _exact_keys(value, {"schema", "status", "study_sha256", "partition", "source_artifact_sha256", "producer_sha256", "results", "publication"}, "guardrail evidence")
    if value["schema"] != GUARDRAIL_SCHEMA or value["status"] != "complete" or value["study_sha256"] != _sha(study) or value["partition"] != "dev":
        raise ValueError("guardrail evidence binding mismatch")
    custodian = _mapping(study["guardrail_custodian"], "guardrail custodian")
    if value["source_artifact_sha256"] != custodian["source_artifact_sha256"] or value["producer_sha256"] != custodian["producer_sha256"]: raise ValueError("guardrail custodian binding mismatch")
    analyzer = _analyzer(study, "guardrail"); _validate_publication(value["publication"], implementation_sha256=analyzer["implementation_sha256"], expected_analyzer_git_state={key: analyzer[key] for key in analyzer if key != "implementation_sha256"}, expected_input_sha256s=[_sha(study), custodian["source_artifact_sha256"]])
    results = _list(value["results"], "guardrail results")
    expected = _mapping(_mapping(study["selection"], "selection")["go_gates"], "go gates")["guardrails"]
    if not results: raise ValueError("guardrail evidence is empty")
    seen: set[str] = set()
    for raw in results:
        row = _mapping(raw, "guardrail result"); _exact_keys(row, {"name", "pass", "receipt_sha256"}, "guardrail result")
        if row["name"] not in expected or row["name"] in seen or type(row["pass"]) is not bool: raise ValueError("guardrail result malformed")
        _token(row["receipt_sha256"], "guardrail receipt")
        payload = {"source_artifact_sha256": value["source_artifact_sha256"], "name": row["name"], "pass": row["pass"]}
        if row["receipt_sha256"] != _sha(payload): raise ValueError("guardrail receipt binding mismatch")
        if row["receipt_sha256"] != custodian["result_payload_sha256s"][row["name"]]: raise ValueError("guardrail result payload mismatch")
        if row["pass"] is not True: raise ValueError(f"guardrail {row['name']} failed")
        seen.add(row["name"])
    if seen != set(expected): raise ValueError("guardrail evidence does not cover frozen gates")


def evaluate_dev_once(study: Mapping[str, Any], dev_rankings: Mapping[str, Any], dev_labels: Mapping[str, Any], policy_freeze: Mapping[str, Any], guardrail_evidence: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one already-frozen policy.  It cannot enumerate or select tau."""
    _validate_study(study)
    _validate_guardrails(guardrail_evidence, study)
    tau = _validate_tau_freeze(policy_freeze, study)
    if policy_freeze["status"] != "complete":
        raise ValueError("STOP_ROUTER_DEGENERATE policy cannot enter dev")
    rankings = _parse_ranking_freeze(dev_rankings, study, "dev")
    labels = _parse_labels(dev_labels, study, "dev")
    if set(row["item_token"] for row in rankings) != set(labels):
        raise ValueError("ranking/label dev item sets differ")
    membership = _mapping(policy_freeze["train_membership_tokens"], "tau freeze train membership")
    for key in ("campaign_token", "group_token", "item_token"):
        if set(membership[key]) & {row[key] for row in rankings}:
            raise ValueError(f"train/dev {key} overlap")
    gated = _gated_rows(rankings, tau)
    raw = _metrics(rankings, labels, "raw"); p5 = _metrics(rankings, labels, "p5"); gated_metrics = _metrics(gated, labels, "gated")
    bootstrap = _mapping(_mapping(study["selection"], "selection")["bootstrap"], "bootstrap")
    args = {"seed": int(bootstrap["seed"]), "resamples": int(bootstrap["resamples"]), "percentiles": list(bootstrap["percentiles"])}
    raw_ci = _paired_bootstrap(gated, labels, "gated", "raw", **args)
    p5_ci = _paired_bootstrap(gated, labels, "gated", "p5", **args)
    p5_route_count = sum(row["route"] == "p5" for row in gated)
    raw_route_count = len(gated) - p5_route_count
    route_fraction = p5_route_count / len(gated)
    best_static = max(raw["question_macro_recall_at_10"], p5["question_macro_recall_at_10"])
    best_static_conversation = max(raw["conversation_macro_recall_at_10"], p5["conversation_macro_recall_at_10"])
    gates = _mapping(_mapping(study["selection"], "selection")["go_gates"], "go_gates")
    guardrails = _mapping(gates["guardrails"], "go_gates.guardrails")
    guardrail_pass = True
    go = (min(route_fraction, 1.0 - route_fraction) >= float(gates["min_route_fraction"]) and gated_metrics["question_macro_recall_at_10"] > best_static and gated_metrics["conversation_macro_recall_at_10"] > best_static_conversation and raw_ci["interval"][0] > 0.0 and p5_ci["interval"][0] > 0.0 and guardrail_pass)
    return {"schema": DEV_EVAL_SCHEMA, "status": "complete", "study_sha256": _sha(study), "policy_freeze_sha256": _sha(policy_freeze), "guardrail_evidence_sha256": _sha(guardrail_evidence), "partition": "dev", "selected_tau": _receipt(tau), "metrics": {"raw": raw, "p5": p5, "gated": gated_metrics}, "gated_minus": {"raw": raw_ci, "p5": p5_ci}, "routes": {"p5_count": p5_route_count, "raw_count": raw_route_count, "p5_fraction": route_fraction, "raw_fraction": raw_route_count / len(gated), "both_routes_meet_manifest_minimum": min(route_fraction, raw_route_count / len(gated)) >= float(gates["min_route_fraction"])}, "go": go, "go_gates": {"route_fraction_at_least": float(gates["min_route_fraction"]), "gated_strictly_exceeds_best_static_both_macros": gated_metrics["question_macro_recall_at_10"] > best_static and gated_metrics["conversation_macro_recall_at_10"] > best_static_conversation, "both_paired_ci_lower_gt_zero": raw_ci["interval"][0] > 0.0 and p5_ci["interval"][0] > 0.0, "guardrails": dict(guardrails)}, "protocol": {"tau_source": "unique_frozen_train_policy", "candidate_enumeration": "forbidden_in_dev", "selection": "forbidden_in_dev"}}


def _git_state(repo: Path) -> dict[str, Any]:
    def call(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    status = call("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise RuntimeError("analyzer worktree must be clean")
    head = call("rev-parse", "HEAD").decode().strip(); tree = call("rev-parse", "HEAD^{tree}").decode().strip()
    diff = call("diff", "--binary", "--full-index", "HEAD^", "HEAD")
    if len(head) != 40 or len(tree) != 40 or any(char not in _HEX for char in head + tree):
        raise RuntimeError("git returned malformed object ID")
    return {"git_head": head, "git_tree": tree, "git_dirty": False, "worktree_status_sha256": _sha_bytes(status), "commit_diff_sha256": _sha_bytes(diff), "commit_diff_bytes": len(diff)}


def publish_bound_report(*, report: Mapping[str, Any], output: Path | str, inputs: Sequence[BoundInput], repo: Path | str, implementation_path: Path | str | None = None) -> dict[str, Any]:
    """Atomically create a non-replaceable external report bound to all inputs."""
    target = Path(output).resolve(); repository = Path(repo).resolve()
    if repository == target or repository in target.parents:
        raise ValueError("report output must be outside repository")
    if target.exists():
        raise FileExistsError("refusing to clobber existing report")
    if any(target == item.path for item in inputs):
        raise ValueError("report output must differ from every input")
    before = _git_state(repository)
    implementation = Path(implementation_path or __file__).resolve().read_bytes()
    bound = dict(report); bound["publication"] = {"input_receipts": [{"path_sha256": _sha(str(item.path)), "sha256": item.expected_sha256} for item in inputs], "analyzer_git_state": before, "implementation_sha256": _sha_bytes(implementation)}
    raw = _canonical_bytes(bound)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        for item in inputs: item.verify_unchanged()
        if _git_state(repository) != before:
            raise RuntimeError("git state drift before publication")
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise FileExistsError("refusing to clobber existing report") from None
        finally:
            if temporary.exists(): temporary.unlink()
        return bound
    except BaseException:
        if temporary.exists(): temporary.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    for name in ("select", "evaluate"):
        item = command.add_parser(name); item.add_argument("--study", required=True); item.add_argument("--study-sha256", required=True); item.add_argument("--rankings", required=True); item.add_argument("--rankings-sha256", required=True); item.add_argument("--labels", required=True); item.add_argument("--labels-sha256", required=True); item.add_argument("--output", required=True); item.add_argument("--repo", required=True)
        if name == "evaluate": item.add_argument("--policy-freeze", required=True); item.add_argument("--policy-freeze-sha256", required=True); item.add_argument("--guardrail-evidence", required=True); item.add_argument("--guardrail-evidence-sha256", required=True)
    args = parser.parse_args(argv)
    study = BoundInput.load(args.study, args.study_sha256); rankings = BoundInput.load(args.rankings, args.rankings_sha256); labels = BoundInput.load(args.labels, args.labels_sha256)
    inputs = [study, rankings, labels]
    if args.command == "select":
        result = select_and_freeze(study.json("study"), rankings.json("rankings"), labels.json("labels"))
    else:
        policy = BoundInput.load(args.policy_freeze, args.policy_freeze_sha256); guardrails = BoundInput.load(args.guardrail_evidence, args.guardrail_evidence_sha256); inputs.extend([policy, guardrails])
        result = evaluate_dev_once(study.json("study"), rankings.json("rankings"), labels.json("labels"), policy.json("policy freeze"), guardrails.json("guardrail evidence"))
    stage, partition = ("tau_select", "train") if args.command == "select" else ("dev_eval", "dev")
    _slot(study.json("study"), stage=stage, partition=partition, output=args.output)
    publish_bound_report(report=result, output=args.output, inputs=inputs, repo=args.repo, implementation_path=Path(__file__))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
