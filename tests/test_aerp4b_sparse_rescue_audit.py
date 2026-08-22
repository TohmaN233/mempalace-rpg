from __future__ import annotations

import hashlib
import inspect
import json
import os
import copy
from pathlib import Path

import pytest

from benchmarks import aerp4b_sparse_rescue_audit as audit


def _token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rows() -> tuple[list[dict], dict[str, dict]]:
    """Five groups, each with enough supported sparse rescues for inner LOCO."""
    rows: list[dict] = []
    labels: dict[str, dict] = {}
    for group in range(5):
        group_token = _token(f"group:{group}")
        for index in range(40):
            item = _token(f"item:{group}:{index}")
            raw_rescue = index < 10
            gold = _token(f"gold:{group}:{index}")
            rows.append({
                "item_token": item, "group_token": group_token, "campaign_token": _token("campaign"),
                "A": 0.2 if raw_rescue else 0.8,
                "churn": 0.5 if raw_rescue else 0.1,
                "displacement": 0.3 if raw_rescue else 0.02,
                "intrusion": 0.4 if raw_rescue else 0.03,
                "raw_top10": [gold] if raw_rescue else [_token(f"raw-miss:{item}")],
                "p5_top10": [_token(f"p5-miss:{item}")] if raw_rescue else [gold],
            })
            labels[item] = {"item_token": item, "gold_tokens": [gold], "unresolved_evidence_item_count": 0, "evidence_item_count": 1}
    return rows, labels


def test_nested_loco_prefers_frozen_simplest_family_and_improves() -> None:
    rows, labels = _rows()
    result = audit.audit_train_only(rows, labels)
    assert result["status"] == "complete"
    assert result["performance_gate_pass"] is True
    assert result["confirmation_claim"] is False
    assert result["dev_eligible"] is False
    assert result["route_gates"]["override_count"] == 50
    assert result["route_gates"]["groups_covered"] == 5
    assert result["metrics"]["sparse_rescue"]["question_macro_recall_at_10"] == 1.0
    assert result["metrics"]["p5"]["question_macro_recall_at_10"] == 0.75
    assert {fold["winner_family"] for fold in result["outer_folds"]} == {"F0_A"}
    assert {route["route"] for route in result["outer_routes"]} == {"raw", "p5"}


def test_nested_loco_is_deterministic() -> None:
    rows, labels = _rows()
    assert audit.audit_train_only(rows, labels) == audit.audit_train_only(list(reversed(rows)), labels)


def test_unexpected_family_error_propagates_instead_of_becoming_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    rows, labels = _rows()

    def corrupt(*_args: object, **_kwargs: object) -> dict:
        raise ValueError("malformed opaque receipt")

    monkeypatch.setattr(audit, "_family_inner_oof", corrupt)
    with pytest.raises(ValueError, match="malformed opaque receipt"):
        audit.audit_train_only(rows, labels)


def test_zero_override_stop_support_receipt_is_canonical_json() -> None:
    rows, _labels = _rows()
    all_p5 = [{**row, "route": "p5"} for row in rows]
    gates = audit._route_gates(all_p5)
    assert gates["pass"] is False
    assert gates["max_group_share"] is None
    report = {"schema": audit.SCHEMA, "status": "STOP_SUPPORT_GATE", "route_gates": gates}
    assert json.loads(gate_bytes := audit.gate._canonical_bytes(report)) == report
    assert gate_bytes


def test_crosswalk_identity_receipt_swaps_fail_closed() -> None:
    token = _token
    frozen = {
        "item_token": token("item"), "group_token": token("group:a"), "campaign_token": token("campaign:a"),
        "query_sha256": token("query:a"), "input_sha256": token("input:a"),
    }
    item = {key: frozen[key] for key in ("item_token", "group_token", "campaign_token")} | {
        "rank_source": {key: frozen[key] for key in ("query_sha256", "input_sha256")}
    }
    audit._validate_identity_receipts(item, frozen)
    for key, replacement in (("group_token", token("group:b")), ("campaign_token", token("campaign:b")),
                             ("query_sha256", token("query:b")), ("input_sha256", token("input:b"))):
        swapped = dict(item)
        if key in ("query_sha256", "input_sha256"):
            swapped["rank_source"] = dict(item["rank_source"])
            swapped["rank_source"][key] = replacement
        else:
            swapped[key] = replacement
        with pytest.raises(ValueError, match=key):
            audit._validate_identity_receipts(swapped, frozen)


def test_reconstructed_full_order_must_match_authorization_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = [_token(f"candidate:{index}") for index in range(12)]
    item = {"rank_source": {}}
    frozen = {
        "item_token": _token("item"), "group_token": _token("group"), "campaign_token": _token("campaign"),
        "A_hex": (0.5).hex(), "raw_top10": tokens[:10], "p5_top10": tokens[:10],
        "authorized_tokens": tokens,
    }
    below_top10_reordered = [*tokens[:10], tokens[11], tokens[10]]
    monkeypatch.setattr(audit, "_ranked_tokens", lambda _item, _weights: below_top10_reordered)
    with pytest.raises(ValueError, match="authorization receipt"):
        audit._features(item, frozen)


def _valid_manifest_and_study(crosswalk: str) -> tuple[dict, dict]:
    state = {
        "git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False,
        "worktree_status_sha256": _token("status"), "commit_diff_sha256": _token("diff"), "commit_diff_bytes": 3,
    }
    artifact = _token("artifact")
    manifest = {
        "schema": "aerp4-locomo-stream-rank-manifest-v1", "status": "complete", "artifact_sha256": artifact,
        "artifact_bytes": 1, "groups": {}, "metrics": {}, "question_random_split": False,
        "publication": {"input_receipts": [{"path_sha256": _token("path"), "sha256": artifact}],
                        "analyzer_git_state": state, "implementation_sha256": _token("implementation")},
        "source_artifact_git_receipts": {"git_state_before": dict(state), "git_state_after": dict(state)},
        "shards": [{"partition": "train", "path": "train.jsonl", "path_sha256": _token("train-path"), "sha256": _token("shard"),
                    "bytes": 1, "item_count": 1, "candidate_count": 1, "crosswalk_sha256": crosswalk}],
    }
    study = {"producer": {"artifact_sha256": audit._sha(manifest), "git_head": state["git_head"], "git_tree": state["git_tree"]}}
    return manifest, study


def test_sanitize_manifest_crosswalk_mismatch_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    crosswalk = _token("crosswalk")
    monkeypatch.setattr(audit.gate, "_partition_spec", lambda _study, _partition: {"crosswalk_sha256": crosswalk})
    manifest, study = _valid_manifest_and_study(crosswalk)
    assert audit.validate_sanitize_manifest(manifest, study)["sha256"] == _token("shard")
    manifest["shards"][0]["crosswalk_sha256"] = _token("swapped-crosswalk")
    study["producer"]["artifact_sha256"] = audit._sha(manifest)
    with pytest.raises(ValueError, match="crosswalk"):
        audit.validate_sanitize_manifest(manifest, study)


def test_sanitize_manifest_lineage_tampering_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    crosswalk = _token("crosswalk")
    monkeypatch.setattr(audit.gate, "_partition_spec", lambda _study, _partition: {"crosswalk_sha256": crosswalk})
    manifest, study = _valid_manifest_and_study(crosswalk)
    producer = copy.deepcopy(study)
    producer["producer"]["artifact_sha256"] = _token("wrong-manifest")
    with pytest.raises(ValueError, match="producer artifact"):
        audit.validate_sanitize_manifest(manifest, producer)
    cases = []
    source = copy.deepcopy(manifest)
    source["publication"]["input_receipts"][0]["sha256"] = _token("wrong-source")
    cases.append((source, study, "producer artifact"))
    git = copy.deepcopy(manifest)
    git["publication"]["analyzer_git_state"]["git_head"] = "c" * 40
    cases.append((git, study, "producer artifact"))
    drift = copy.deepcopy(manifest)
    drift["source_artifact_git_receipts"]["git_state_after"]["git_tree"] = "c" * 40
    cases.append((drift, study, "producer artifact"))
    for altered, bound_study, _message in cases:
        # Any manifest byte change first breaks the producer digest; rebinding
        # the synthetic study isolates the intended downstream receipt check.
        bound_study = copy.deepcopy(bound_study)
        bound_study["producer"]["artifact_sha256"] = audit._sha(altered)
        with pytest.raises(ValueError):
            audit.validate_sanitize_manifest(altered, bound_study)


def test_bound_train_shard_detects_tampered_bytes(tmp_path: Path) -> None:
    shard = tmp_path / "train.jsonl"
    shard.write_bytes(b'{"safe":true}\n')
    digest = hashlib.sha256(shard.read_bytes()).hexdigest()
    bound = audit.BoundTrainShard.load(shard, digest)
    bound.verify_manifest_receipt({"sha256": digest, "path": "train.jsonl", "bytes": shard.stat().st_size})
    shard.write_bytes(b'{"safe":false}\n')
    with pytest.raises(RuntimeError, match="changed"):
        bound.verify_unchanged()


def test_fail_closed_when_reconstruction_disagrees_with_frozen_top10(monkeypatch: pytest.MonkeyPatch) -> None:
    item = {"rank_source": {}}
    frozen = {
        "item_token": _token("item"), "group_token": _token("group"), "campaign_token": _token("campaign"),
        "A_hex": (0.5).hex(), "raw_top10": [_token("frozen-raw")] * 10, "p5_top10": [_token("frozen-p5")] * 10,
    }
    monkeypatch.setattr(audit, "_ranked_tokens", lambda _item, weights: [_token(str(weights))] * 10)
    with pytest.raises(ValueError, match="reconstruction does not match"):
        audit._features(item, frozen)


def test_train_only_interface_exposes_no_dev_input() -> None:
    signature = inspect.signature(audit.run_train_audit)
    assert set(signature.parameters) == {"study", "sanitized_manifest", "sanitized_train", "train_rankings", "train_labels", "sanitized_train_sha256"}
    assert "--dev" not in inspect.getsource(audit.main)


def test_run_train_audit_binds_all_train_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    rows, labels = _rows()
    monkeypatch.setattr(audit, "prepare_train_rows", lambda study, source, rankings, train_labels, **kwargs: (rows, labels))
    receipt = {"sha256": _token("shard"), "item_count": len(rows), "candidate_count": 1}
    monkeypatch.setattr(audit, "validate_sanitize_manifest", lambda manifest, study: receipt)
    study, manifest, source, rankings, train_labels = ({"study": 1}, {"manifest": 1}, rows, {"rankings": 1}, {"labels": 1})
    result = audit.run_train_audit(
        study=study, sanitized_manifest=manifest, sanitized_train=source, train_rankings=rankings, train_labels=train_labels
    )
    assert result["input_receipts"] == {
        "study_sha256": audit._sha(study), "sanitized_manifest_sha256": audit._sha(manifest), "sanitized_train_source_sha256": receipt["sha256"],
        "train_rankings_sha256": audit._sha(rankings), "train_labels_sha256": audit._sha(train_labels),
    }


_REAL_EXPECTED = {
    "status": "STOP_PERFORMANCE_GATE", "override_count": 101,
    "question_macro_recall_at_10": 0.6719088275651776,
    "conversation_macro_recall_at_10": 0.6714879345688509,
}


def test_real_train_audit_when_exact_root_is_supplied() -> None:
    """Optional production regression: only the sealed train artifacts are opened."""
    root = os.environ.get("AERP4B_REAL_TRAIN_ROOT")
    if not root:
        pytest.skip("set AERP4B_REAL_TRAIN_ROOT to run the sealed train-only regression")
    base = Path(root)
    study = json.loads((base / "custody" / "study.json").read_text(encoding="utf-8"))
    manifest = json.loads((base / "sanitize" / "manifest.json").read_text(encoding="utf-8"))
    rankings = json.loads((base / "paired-train" / "ranking-freeze.json").read_text(encoding="utf-8"))
    labels = json.loads((base / "custody" / "labels-train.json").read_text(encoding="utf-8"))
    shard = base / "sanitize" / "train.jsonl"
    report = audit.run_train_audit(
        study=study, sanitized_manifest=manifest, sanitized_train=audit.iter_sanitized_train_shard(shard),
        train_rankings=rankings, train_labels=labels, sanitized_train_sha256=_REAL_TRAIN_SHA,
    )
    assert report["status"] == _REAL_EXPECTED["status"]
    assert report["route_gates"]["override_count"] == _REAL_EXPECTED["override_count"]
    assert report["metrics"]["sparse_rescue"]["question_macro_recall_at_10"] == _REAL_EXPECTED["question_macro_recall_at_10"]
    assert report["metrics"]["sparse_rescue"]["conversation_macro_recall_at_10"] == _REAL_EXPECTED["conversation_macro_recall_at_10"]


_REAL_TRAIN_SHA = "ec63a7f7674714fcca830c0be84b26ffed2271b9d0f6e615ba92bb975a274ce1"
