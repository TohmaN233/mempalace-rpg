from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks import aerp4_locomo_custody as custody
from benchmarks import aerp4_locomo_paired_receipts as paired
from benchmarks import aerp4_raw_anchored_gate as gate


def _token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _source() -> tuple[dict, dict]:
    parts = {"train": [], "dev": []}; labels = []
    for index in range(1986):
        partition = "train" if index < 993 else "dev"
        group = f"{partition}-conversation-{index % 5}"
        row = {"item_token": _token(f"item-{index}"), "group_token": _token(group), "campaign_token": _token("campaign-" + group), "rank_source": {"query_sha256": _token(f"query-{index}"), "input_sha256": _token(f"input-{index}")}}
        parts[partition].append(row)
        labels.append({"item_token": row["item_token"], "group_token": row["group_token"], "campaign_token": row["campaign_token"], "category": "poison", "official_exact": {"resolved_dialog_ids": [f"dialog-{index}"] if index >= 4 else [], "unresolved_evidence_item_count": 1 if index == 4 else 0, "evidence_item_count": 0 if index < 4 else 2 if index == 4 else 1}})
    source = {"schema": "aerp4-locomo-sanitized-rank-source-v1", "status": "complete", "question_random_split": False, "partitions": {name: {"items": rows, "crosswalk_sha256": gate._crosswalk([{**{key: row[key] for key in ("item_token", "group_token", "campaign_token")}, **row["rank_source"]} for row in rows])} for name, rows in parts.items()}}
    return source, {"items": labels}


def _receipt(value: str) -> dict:
    return {"implementation_sha256": _token(value), "git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": _token(value + "status"), "commit_diff_sha256": _token(value + "diff"), "commit_diff_bytes": 0}


def _guardrail_files(tmp_path: Path) -> dict:
    files = {
        "acl_blind": {"schema": "aerp1-blind-180-report", "aggregate": {"verdict": "PASS"}, "denominators": {"logical_cases": 180, "product_calls": 360, "positive_cases": 90}, "metrics": {"positive_authorized_candidate_coverage": 90, "negative_nontelemetry_event_id_leaks": 0, "negative_nontelemetry_rendered_leaks": 0, "negative_nontelemetry_span_leaks": 0, "complete_policy_traces": 360, "neutral_gold_rank_not_worse": 60}},
        "safety_audit": {"gates": {"sqlite_drawer_atomicity_and_idempotency": {"verdict": "PASS", "pytest_cases": 10}}},
        "performance": {"schema": "aerp1-performance-30k-report", "aggregate": {"verdict": "PASS"}, "denominators": {"events_per_repeat": 30000}, "baseline_gate": {"status": "PASS", "artifact_sha256": "B0"}},
        "performance_b0": {"schema": "aerp1-performance-30k-report", "mode": "b0", "aggregate": {"verdict": "PASS"}, "baseline_gate": {"status": "BASELINE_MEASURED"}},
        "exact_fcd2": {"schema": "aerp3-fcd2-causal-ablation", "status": "complete", "verdict": "FUSION_SUPPORTED", "fusion_semantics_parity": {"expected_questions": 1986, "full_order_receipts_exact_questions": 1986, "product_top10_stored_order_exact_questions": 1986, "strict_top10_boundary_questions": 1986, "strict_top50_boundary_questions": 1986}, "ablation_replay": {"status": "complete", "expected_scenario_question_checks": 23832, "scenario_question_checks": 23832, "strict_top10_boundary_questions": 23832, "all_scenarios_strict_top10_boundary": True}},
        "exact_derived": {"schema": "aerp6-transition-ledger-v1", "status": "complete", "rank_gate": {"full_order_replayed_questions": 1986}, "mechanism_gate": {"verdict": "PASS"}},
        "exact_checkpoint": {"schema": "aerp6-checkpoint-binding-v1", "status": "complete", "git": {"commit": "e42dfd1d8cf353b6acd36e5687ead4307d540e64"}, "artifact": {"sha256": "DERIVED"}},
    }
    paths = {}
    for name, payload in files.items():
        path = tmp_path / f"{name}.json"; path.write_text(json.dumps(payload), encoding="utf-8"); paths[name] = path
    junit = tmp_path / "safety.xml"
    cases = [("tests.test_aerp1_authorized_evidence_audit", "test_aerp1_get_scene_transcript_mixed_visibility_returns_only_allowed_exact_span"), *[("tests.test_aerp1_commit_atomicity", "test_single_failure_leaves_no_half_state_and_retry_is_idempotent") for _ in range(6)], *[("tests.test_aerp1_commit_atomicity", name) for name in ("test_same_scene_id_with_different_payload_fails_before_drawer_write", "test_post_commit_failure_is_observable_and_retry_recovers_without_duplicates", "test_cleanup_failure_raises_observable_compound_error", "test_mempalace_adapter_deletes_by_deterministic_drawer_id")], *[("other", f"other-{i}") for i in range(100)]]
    junit.write_text('<testsuite tests="111" errors="0" failures="0" skipped="0">' + "".join(f'<testcase classname="{klass}" name="{name}"/>' for klass, name in cases) + "</testsuite>", encoding="utf-8")
    paths["safety_junit"] = junit
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    files["performance"]["baseline_gate"]["artifact_sha256"] = digests["performance_b0"]
    paths["performance"].write_text(json.dumps(files["performance"]), encoding="utf-8")
    files["exact_checkpoint"]["artifact"]["sha256"] = digests["exact_derived"]
    paths["exact_checkpoint"].write_text(json.dumps(files["exact_checkpoint"]), encoding="utf-8")
    digests = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}
    return {name: (path, digests[name]) for name, path in paths.items()}


def test_custody_excludes_zero_evidence_retains_multiplicity_and_hides_category() -> None:
    source, labels = _source()
    built = custody.build_label_custody(source, labels, source_artifact_sha256=_token("source"), producer_sha256=_token("producer"))
    assert built["exclusion_receipt"]["excluded_count"] == 4
    assert sum(len(value["items"]) for value in built["labels"].values()) == 1982
    first = built["labels"]["train"]["items"][0]
    assert set(first) == {"item_token", "gold_tokens", "unresolved_evidence_item_count", "evidence_item_count"}
    assert built["labels"]["train"]["items"][0]["evidence_item_count"] == 2
    expected = paired.evidence_token_from_ranking_key_sha256(hashlib.sha256("dialog-4".encode()).hexdigest())
    assert built["labels"]["train"]["items"][0]["gold_tokens"] == [expected]
    assert "category" not in json.dumps(built)


def test_custody_rejects_joint_item_conversation_crosswalk_swap() -> None:
    source, labels = _source()
    labels["items"][10]["group_token"], labels["items"][11]["group_token"] = labels["items"][11]["group_token"], labels["items"][10]["group_token"]
    with pytest.raises(ValueError, match="item-conversation crosswalk"):
        custody.build_label_custody(source, labels, source_artifact_sha256=_token("source"), producer_sha256=_token("producer"))


def test_study_has_disjoint_five_by_five_crosswalk_and_gate_schema(tmp_path: Path) -> None:
    source, labels = _source(); custody_labels = custody.build_label_custody(source, labels, source_artifact_sha256=_token("source"), producer_sha256=_token("producer"))
    guard = custody.build_guardrail_source_manifest(artifacts=_guardrail_files(tmp_path))
    study = custody.build_study(sanitized_source=source, label_custody=custody_labels, dataset_sha256=_token("dataset"), producer={"artifact_sha256": _token("artifact"), "git_head": "c" * 40, "git_tree": "d" * 40, "retrieval_implementation_sha256": _token("retrieval")}, retrieval={"implementation_sha256": _token("retrieval"), "raw_config_sha256": _token("raw"), "p5_config_sha256": _token("p5")}, output_slots={name: tmp_path / (name + ".json") for name in ("train_prefreeze", "dev_prefreeze", "tau_select", "dev_eval")}, analyzers={name: _receipt(name) for name in ("gate", "prefreeze", "label", "guardrail")}, guardrail_source=guard, bootstrap_resamples=20)
    gate._validate_study(study)
    assert len(gate._parse_labels(custody.build_label_artifact(study=study, label_custody=custody_labels, partition="train"), study, "train")) == 989
    assert study["splits"]["question_random_split"] is False
    assert study["partitions"]["train"]["group_sha256"] != study["partitions"]["dev"]["group_sha256"]


def test_guardrail_manifest_rehashes_shape_and_evidence_is_gate_acceptable(tmp_path: Path) -> None:
    manifest = custody.build_guardrail_source_manifest(artifacts=_guardrail_files(tmp_path))
    assert manifest["manifest"]["provenance"]["original_public_product_exact_replay"] == "absent_non_covered"
    source, labels = _source(); label_custody = custody.build_label_custody(source, labels, source_artifact_sha256=_token("source"), producer_sha256=_token("producer"))
    study = custody.build_study(sanitized_source=source, label_custody=label_custody, dataset_sha256=_token("dataset"), producer={"artifact_sha256": _token("artifact"), "git_head": "c" * 40, "git_tree": "d" * 40, "retrieval_implementation_sha256": _token("retrieval")}, retrieval={"implementation_sha256": _token("retrieval"), "raw_config_sha256": _token("raw"), "p5_config_sha256": _token("p5")}, output_slots={name: tmp_path / ("new-" + name) for name in ("train_prefreeze", "dev_prefreeze", "tau_select", "dev_eval")}, analyzers={name: _receipt(name) for name in ("gate", "prefreeze", "label", "guardrail")}, guardrail_source=manifest, bootstrap_resamples=20)
    evidence = custody.build_guardrail_evidence(study=study, guardrail_source=manifest)
    gate._validate_guardrails(evidence, study)


def test_guardrail_receipt_drift_fails_closed(tmp_path: Path) -> None:
    files = _guardrail_files(tmp_path)
    path, digest = files["exact_fcd2"]
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 drift"):
        custody.build_guardrail_source_manifest(artifacts=files)


def test_guardrail_b0_shape_drift_fails_closed_after_rehash(tmp_path: Path) -> None:
    files = _guardrail_files(tmp_path)
    b0_path, _digest = files["performance_b0"]
    b0 = json.loads(b0_path.read_text(encoding="utf-8")); b0["mode"] = "current"; b0_path.write_text(json.dumps(b0), encoding="utf-8")
    b0_sha = hashlib.sha256(b0_path.read_bytes()).hexdigest()
    perf_path, _digest = files["performance"]
    perf = json.loads(perf_path.read_text(encoding="utf-8")); perf["baseline_gate"]["artifact_sha256"] = b0_sha; perf_path.write_text(json.dumps(perf), encoding="utf-8")
    files["performance_b0"] = (b0_path, b0_sha); files["performance"] = (perf_path, hashlib.sha256(perf_path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="B0 mode"):
        custody.build_guardrail_source_manifest(artifacts=files)


def test_exclusion_receipt_source_binding_and_unknown_member_fail_closed(tmp_path: Path) -> None:
    source, labels = _source(); labels_custody = custody.build_label_custody(source, labels, source_artifact_sha256=_token("source"), producer_sha256=_token("producer"))
    labels_custody["exclusion_receipt"]["sanitized_source_sha256"] = _token("drift")
    guard = custody.build_guardrail_source_manifest(artifacts=_guardrail_files(tmp_path))
    kwargs = {"sanitized_source": source, "label_custody": labels_custody, "dataset_sha256": _token("dataset"), "producer": {"artifact_sha256": _token("artifact"), "git_head": "c" * 40, "git_tree": "d" * 40, "retrieval_implementation_sha256": _token("retrieval")}, "retrieval": {"implementation_sha256": _token("retrieval"), "raw_config_sha256": _token("raw"), "p5_config_sha256": _token("p5")}, "output_slots": {name: tmp_path / ("bind-" + name) for name in ("train_prefreeze", "dev_prefreeze", "tau_select", "dev_eval")}, "analyzers": {name: _receipt(name) for name in ("gate", "prefreeze", "label", "guardrail")}, "guardrail_source": guard, "bootstrap_resamples": 20}
    with pytest.raises(ValueError, match="exclusion receipt drift"):
        custody.build_study(**kwargs)
    labels_custody["exclusion_receipt"]["sanitized_source_sha256"] = gate._sha(source)
    labels_custody["exclusion_receipt"]["excluded_item_tokens"][0] = _token("unknown")
    with pytest.raises(ValueError, match="exclusion membership"):
        custody.build_study(**kwargs)


def test_guardrail_manifest_content_tamper_fails_closed(tmp_path: Path) -> None:
    manifest = custody.build_guardrail_source_manifest(artifacts=_guardrail_files(tmp_path))
    manifest["manifest"]["provenance"]["original_public_product_exact_replay"] = "covered"
    with pytest.raises(ValueError, match="canonical digest drift"):
        custody._validate_guardrail_manifest(manifest)
