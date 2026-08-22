"""Fail-closed label custody and engineering guardrails for AERP-4 LoCoMo.

This module deliberately never opens the 744 MiB staged ranking artifact.  It
accepts the already-sanitized rank source and a separately staged label source.
"""
from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Mapping

from benchmarks import aerp4_raw_anchored_gate as gate

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aerp4-locomo-label-custody-v1"
GUARDRAIL_MANIFEST_SCHEMA = "aerp4-locomo-guardrail-source-manifest-v1"
_HEX = set("0123456789abcdef")
_NAMES = ("acl", "safety", "exact_replay", "performance")
_VIS = Path(r"C:\Users\tgy23\.codex\visualizations\2026\08\21\01a02540-3972-7943-8ca8-a6a40330df66")

DEFAULT_GUARDRAIL_ARTIFACTS = {
    "acl_blind": (_VIS / "mempalace-aerp1-blind-180-e8ca3a7.json", "81b7e344c5b3ed9d2c3c41f28d202ac03d5c889056a53a1d86fb504ffa84a6d0"),
    "safety_junit": (_VIS / "mempalace-aerp1-pytest-a3de2e9.xml", "e62b16b064bba9a7805f953cdce49c2289be0cc21272418ae845a5c9c9170a62"),
    "safety_audit": (_VIS / "mempalace-aerp1-audit-a3de2e9.json", "91ebccf40a51b40732b5874da78edf128546597adf6056cacd99f514448c637a"),
    "performance": (_VIS / "mempalace-aerp1-perf-30k-current-e8ca3a7.json", "645fd86705da425672f5815b3ceb641834aa8d1696989bc0439209af815aec4e"),
    "performance_b0": (_VIS / "mempalace-aerp1-perf-30k-b0-v2.json", "4a14d657dc0f1df55b6f733184619687bed64d8b0d8a63d8ff978dd723649df3"),
    "exact_fcd2": (_VIS / "mempalace-aerp3-fcd2-7a87602.json", "85606c43c2ca4e7da1da8921881d7758c0c9c62d4ca251dadbf5575c036c554a"),
    "exact_derived": (_VIS / "mempalace-aerp6-transition-ledger-derived-7bc92fd.json", "8125a7572913dc858105505204a6a7d5488090a60616fddf60d18728839b7088"),
    "exact_checkpoint": (_VIS / "mempalace-aerp6-checkpoint-e42dfd1.json", "5ad36ead87aaccea9f8a96778ec857481689570ac35459338688b81bb50d5491"),
}


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _token(value: Any, name: str = "token") -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError(f"{name} must be a lowercase 64-hex token")
    return value


def _opaque(domain: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{domain} identity is malformed")
    return hashlib.sha256((domain + "\0" + value).encode()).hexdigest()


def _object(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _artifact_pairs(artifacts: Mapping[str, tuple[Path | str, str]] | None) -> Mapping[str, tuple[Path, str]]:
    raw = DEFAULT_GUARDRAIL_ARTIFACTS if artifacts is None else artifacts
    if set(raw) != set(DEFAULT_GUARDRAIL_ARTIFACTS):
        raise ValueError("guardrail artifact set is incomplete")
    pairs: dict[str, tuple[Path, str]] = {}
    for name, pair in raw.items():
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("guardrail artifact pin is malformed")
        pairs[name] = (Path(pair[0]).resolve(), _token(pair[1], f"{name} digest"))
    return pairs


def _eq(value: Any, expected: Any, name: str) -> None:
    if value != expected:
        raise ValueError(f"{name} failed")


def _validate_acl(value: Mapping[str, Any]) -> None:
    _eq(value.get("schema"), "aerp1-blind-180-report", "ACL schema")
    aggregate = _object(value.get("aggregate"), "ACL aggregate")
    _eq(aggregate.get("verdict", aggregate.get("status")), "PASS", "ACL aggregate")
    denominators = _object(value.get("denominators"), "ACL denominators")
    metrics = _object(value.get("metrics"), "ACL metrics")
    for key, expected in (("logical_cases", 180), ("product_calls", 360), ("positive_cases", 90)):
        _eq(denominators.get(key), expected, f"ACL denominator {key}")
    for key, expected in (("positive_authorized_candidate_coverage", 90), ("negative_nontelemetry_event_id_leaks", 0), ("negative_nontelemetry_rendered_leaks", 0), ("negative_nontelemetry_span_leaks", 0), ("complete_policy_traces", 360), ("neutral_gold_rank_not_worse", 60)):
        _eq(metrics.get(key), expected, f"ACL metric {key}")


def _validate_junit(raw: bytes) -> None:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError("safety JUnit is malformed") from exc
    if root.tag == "testsuite":
        totals = {name: int(root.attrib.get(name, "0")) for name in ("tests", "errors", "failures", "skipped")}
    else:
        totals = {name: sum(int(suite.attrib.get(name, "0")) for suite in root.findall(".//testsuite")) for name in ("tests", "errors", "failures", "skipped")}
    if totals != {"tests": 111, "errors": 0, "failures": 0, "skipped": 0}:
        raise ValueError("safety JUnit totals failed")
    cases = [case.attrib.get("name", "") for case in root.findall(".//testcase")]
    mixed = "test_aerp1_get_scene_transcript_mixed_visibility_returns_only_allowed_exact_span"
    if mixed not in cases:
        raise ValueError("safety mixed-visibility case is absent")
    atomic = "test_single_failure_leaves_no_half_state_and_retry_is_idempotent"
    if sum(name == atomic or name.startswith(atomic + "[") for name in cases) != 6:
        raise ValueError("safety atomicity cases failed")
    required = {"test_same_scene_id_with_different_payload_fails_before_drawer_write", "test_post_commit_failure_is_observable_and_retry_recovers_without_duplicates", "test_cleanup_failure_raises_observable_compound_error", "test_mempalace_adapter_deletes_by_deterministic_drawer_id"}
    if not required <= set(cases):
        raise ValueError("safety atomicity companion cases failed")
    atomic_class = "tests.test_aerp1_commit_atomicity"
    if sum(case.attrib.get("classname") == atomic_class for case in root.findall(".//testcase")) != 10:
        raise ValueError("safety atomicity class count failed")


def _validate_audit(value: Mapping[str, Any]) -> None:
    atomic = _object(_object(value.get("gates"), "checkpoint audit gates").get("sqlite_drawer_atomicity_and_idempotency"), "checkpoint audit atomicity")
    _eq(atomic.get("verdict"), "PASS", "checkpoint atomicity")
    _eq(atomic.get("pytest_cases"), 10, "checkpoint atomicity cases")


def _validate_performance(value: Mapping[str, Any], b0_sha: str) -> None:
    _eq(value.get("schema"), "aerp1-performance-30k-report", "performance schema")
    _eq(_object(value.get("aggregate"), "performance aggregate").get("verdict"), "PASS", "performance aggregate")
    _eq(_object(value.get("denominators"), "performance denominators").get("events_per_repeat"), 30000, "performance events")
    baseline = _object(value.get("baseline_gate"), "performance baseline")
    _eq(baseline.get("status"), "PASS", "performance baseline")
    _eq(baseline.get("artifact_sha256"), b0_sha, "performance B0 binding")


def _validate_b0(value: Mapping[str, Any]) -> None:
    _eq(value.get("schema"), "aerp1-performance-30k-report", "B0 schema")
    _eq(value.get("mode"), "b0", "B0 mode")
    _eq(_object(value.get("aggregate"), "B0 aggregate").get("verdict"), "PASS", "B0 aggregate")
    baseline = _object(value.get("baseline_gate"), "B0 baseline gate")
    _eq(baseline.get("status"), "BASELINE_MEASURED", "B0 baseline gate")
    if "artifact_sha256" in baseline:
        raise ValueError("B0 receipt must not bind a current baseline artifact")


def _validate_fcd2(value: Mapping[str, Any]) -> None:
    _eq(value.get("schema"), "aerp3-fcd2-causal-ablation", "FCD2 schema")
    _eq(value.get("status"), "complete", "FCD2 status")
    _eq(value.get("verdict"), "FUSION_SUPPORTED", "FCD2 verdict")
    parity = _object(value.get("fusion_semantics_parity"), "FCD2 parity")
    for key in ("expected_questions", "full_order_receipts_exact_questions", "product_top10_stored_order_exact_questions", "strict_top10_boundary_questions", "strict_top50_boundary_questions"):
        _eq(parity.get(key), 1986, f"FCD2 {key}")
    replay = _object(value.get("ablation_replay"), "FCD2 ablation replay")
    for key, expected in (("status", "complete"), ("expected_scenario_question_checks", 23832), ("scenario_question_checks", 23832), ("strict_top10_boundary_questions", 23832)):
        _eq(replay.get(key), expected, f"FCD2 {key}")
    _eq(replay.get("all_scenarios_strict_top10_boundary"), True, "FCD2 scenario strict cutoff")


def _validate_derived(value: Mapping[str, Any]) -> None:
    _eq(value.get("schema"), "aerp6-transition-ledger-v1", "AERP6 derived schema")
    _eq(value.get("status"), "complete", "AERP6 derived status")
    _eq(_object(value.get("rank_gate"), "AERP6 rank gate").get("full_order_replayed_questions"), 1986, "AERP6 replay denominator")
    _eq(_object(value.get("mechanism_gate"), "AERP6 mechanism").get("verdict"), "PASS", "AERP6 mechanism")


def _validate_checkpoint(value: Mapping[str, Any], derived_sha: str) -> None:
    _eq(value.get("schema"), "aerp6-checkpoint-binding-v1", "checkpoint schema")
    _eq(value.get("status"), "complete", "checkpoint status")
    _eq(_object(value.get("git"), "checkpoint git").get("commit"), "e42dfd1d8cf353b6acd36e5687ead4307d540e64", "checkpoint commit")
    _eq(_object(value.get("artifact"), "checkpoint artifact").get("sha256"), derived_sha, "checkpoint derived binding")


def build_guardrail_source_manifest(*, artifacts: Mapping[str, tuple[Path | str, str]] | None = None) -> dict[str, Any]:
    """Re-hash and validate every engineering source before binding gate results."""
    pairs = _artifact_pairs(artifacts)
    raw = {name: path.read_bytes() for name, (path, _digest) in pairs.items()}
    for name, (_path, digest) in pairs.items():
        if _sha_bytes(raw[name]) != digest:
            raise ValueError(f"{name} SHA-256 drift")
    jsons = {name: _object(json.loads(raw[name]), name) for name in pairs if name != "safety_junit"}
    _validate_acl(jsons["acl_blind"]); _validate_junit(raw["safety_junit"]); _validate_audit(jsons["safety_audit"])
    _validate_performance(jsons["performance"], pairs["performance_b0"][1]); _validate_b0(jsons["performance_b0"]); _validate_fcd2(jsons["exact_fcd2"])
    _validate_derived(jsons["exact_derived"]); _validate_checkpoint(jsons["exact_checkpoint"], pairs["exact_derived"][1])
    entries = [{"name": name, "path": str(path), "sha256": digest} for name, (path, digest) in sorted(pairs.items())]
    manifest = {"schema": GUARDRAIL_MANIFEST_SCHEMA, "artifacts": entries, "pass_selectors": {"acl": "blind_180_pass", "safety": "junit_and_checkpoint_pass", "exact_replay": "current_aerp_ranking_replay_only", "performance": "current_30k_and_b0_pass"}, "provenance": {"exact_replay_scope": "current_aerp_only", "original_public_product_exact_replay": "absent_non_covered", "recheck_required_at_final_method_checkpoint": True}}
    return {"manifest": manifest, "source_artifact_sha256": gate._sha(manifest)}


def _validate_guardrail_manifest(value: Mapping[str, Any]) -> str:
    if set(value) != {"manifest", "source_artifact_sha256"}:
        raise ValueError("guardrail source envelope is malformed")
    manifest = _object(value.get("manifest"), "guardrail manifest")
    if set(manifest) != {"schema", "artifacts", "pass_selectors", "provenance"} or manifest.get("schema") != GUARDRAIL_MANIFEST_SCHEMA:
        raise ValueError("guardrail manifest schema is malformed")
    if gate._sha(manifest) != _token(value.get("source_artifact_sha256"), "guardrail source digest"):
        raise ValueError("guardrail manifest canonical digest drift")
    selectors = _object(manifest.get("pass_selectors"), "guardrail pass selectors")
    if selectors != {"acl": "blind_180_pass", "safety": "junit_and_checkpoint_pass", "exact_replay": "current_aerp_ranking_replay_only", "performance": "current_30k_and_b0_pass"}:
        raise ValueError("guardrail selector scope drift")
    provenance = _object(manifest.get("provenance"), "guardrail provenance")
    if provenance != {"exact_replay_scope": "current_aerp_only", "original_public_product_exact_replay": "absent_non_covered", "recheck_required_at_final_method_checkpoint": True}:
        raise ValueError("guardrail provenance scope drift")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or {entry.get("name") for entry in artifacts if isinstance(entry, Mapping)} != set(DEFAULT_GUARDRAIL_ARTIFACTS):
        raise ValueError("guardrail manifest artifact membership drift")
    for entry in artifacts:
        row = _object(entry, "guardrail manifest artifact")
        if set(row) != {"name", "path", "sha256"}: raise ValueError("guardrail manifest artifact schema drift")
        _token(row.get("sha256"), "guardrail manifest artifact digest")
    return value["source_artifact_sha256"]


def _source_rows(source: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    if source.get("schema") != "aerp4-locomo-sanitized-rank-source-v1" or source.get("status") != "complete" or source.get("question_random_split") is not False:
        raise ValueError("sanitized rank source contract failed")
    partitions = _object(source.get("partitions"), "sanitized partitions")
    if set(partitions) != {"train", "dev"}:
        raise ValueError("sanitized partitions differ")
    result: dict[str, list[dict[str, str]]] = {}
    for partition in ("train", "dev"):
        section = _object(partitions[partition], f"{partition} source"); rows = section.get("items")
        if not isinstance(rows, list) or not rows: raise ValueError("sanitized source rows are missing")
        parsed: list[dict[str, str]] = []
        for row in rows:
            data = _object(row, "sanitized source row"); rank = _object(data.get("rank_source"), "sanitized rank receipt")
            parsed.append({key: _token(data.get(key), key) for key in ("item_token", "group_token", "campaign_token")} | {key: _token(rank.get(key), key) for key in ("query_sha256", "input_sha256")})
        if len({row["item_token"] for row in parsed}) != len(parsed) or gate._crosswalk(parsed) != section.get("crosswalk_sha256"):
            raise ValueError("sanitized source crosswalk drift")
        result[partition] = parsed
    for key in ("item_token", "group_token", "campaign_token"):
        if {row[key] for row in result["train"]} & {row[key] for row in result["dev"]}: raise ValueError(f"sanitized train/dev {key} overlap")
    return result


def _staged_label_rows(staged: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = staged.get("items", staged.get("questions"))
    if not isinstance(rows, list): raise ValueError("staged labels must contain items")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        data = _object(row, "staged label row")
        item = data.get("item_token") if data.get("item_token") is not None else _opaque("aerp4:item", data.get("item_id"))
        _token(item, "staged item token")
        if item in result: raise ValueError("staged labels repeat item")
        result[item] = data
    return result


def _label_item(row: Mapping[str, Any], item_token: str) -> dict[str, Any]:
    gold = _object(row.get("gold", {}), "staged gold")
    official = _object(row.get("official_exact", gold.get("official_exact")), "official exact labels")
    resolved = official.get("resolved_dialog_ids", official.get("resolved_ids"))
    unresolved, denominator = official.get("unresolved_evidence_item_count", official.get("unresolved")), official.get("evidence_item_count", official.get("count"))
    if not isinstance(resolved, list) or any(not isinstance(value, str) or not value for value in resolved): raise ValueError("official resolved labels are malformed")
    if not isinstance(unresolved, int) or isinstance(unresolved, bool) or unresolved < 0 or not isinstance(denominator, int) or isinstance(denominator, bool) or denominator < 0: raise ValueError("official label denominator is malformed")
    if len(resolved) + unresolved != denominator: raise ValueError("official label denominator drift")
    from benchmarks import aerp4_locomo_paired_receipts as paired

    gold_tokens = []
    for dialog_id in resolved:
        ranking_key_sha256 = hashlib.sha256(dialog_id.encode("utf-8")).hexdigest()
        gold_tokens.append(_token(paired.evidence_token_from_ranking_key_sha256(ranking_key_sha256), "paired evidence token"))
    return {"item_token": item_token, "gold_tokens": gold_tokens, "unresolved_evidence_item_count": unresolved, "evidence_item_count": denominator}


def _label_crosswalk_matches(source_row: Mapping[str, str], label_row: Mapping[str, Any]) -> bool:
    group = label_row.get("group_token")
    campaign = label_row.get("campaign_token")
    conversation = label_row.get("conversation_id")
    if conversation is not None:
        if group is not None or campaign is not None:
            return False
        group = _opaque("aerp4:group", conversation)
        campaign = _opaque("aerp4:campaign", conversation)
    return group == source_row["group_token"] and campaign == source_row["campaign_token"]


def build_label_custody(sanitized_source: Mapping[str, Any], staged_labels: Mapping[str, Any], *, source_artifact_sha256: str, producer_sha256: str) -> dict[str, Any]:
    """Join labels by opaque item identity, exclude exactly four zero-denominator items."""
    source, labels = _source_rows(sanitized_source), _staged_label_rows(staged_labels)
    all_source = [row for part in source.values() for row in part]
    if len(all_source) != 1986: raise ValueError("frozen LoCoMo source denominator drift")
    excluded: list[str] = []; output: dict[str, dict[str, Any]] = {}
    for partition, rows in source.items():
        if any(row["item_token"] not in labels for row in rows): raise ValueError("label/source join failed")
        if any(not _label_crosswalk_matches(row, labels[row["item_token"]]) for row in rows): raise ValueError("label/source item-conversation crosswalk drift")
        payload = [_label_item(labels[row["item_token"]], row["item_token"]) for row in rows]
        excluded.extend(row["item_token"] for row in payload if row["evidence_item_count"] == 0)
        output[partition] = {"schema": gate.LABELS_SCHEMA, "status": "complete", "partition": partition, "items": [row for row in payload if row["evidence_item_count"] > 0]}
    if len(excluded) != 4 or sum(len(value["items"]) for value in output.values()) != 1982: raise ValueError("expected four zero-evidence exclusions and 1982 members")
    receipt = {"schema": SCHEMA, "source_artifact_sha256": _token(source_artifact_sha256), "producer_sha256": _token(producer_sha256), "sanitized_source_sha256": gate._sha(sanitized_source), "excluded_item_tokens": sorted(excluded), "excluded_count": 4, "frozen_member_count": 1982}
    return {"labels": output, "exclusion_receipt": receipt}


def _git_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    keys = {"implementation_sha256", "git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes"}
    if set(value) != keys or value.get("git_dirty") is not False or not isinstance(value.get("commit_diff_bytes"), int) or value["commit_diff_bytes"] < 0: raise ValueError("analyzer receipt must be clean and complete")
    for key in ("implementation_sha256", "worktree_status_sha256", "commit_diff_sha256"): _token(value.get(key), key)
    for key in ("git_head", "git_tree"):
        if not isinstance(value.get(key), str) or len(value[key]) != 40 or any(char not in _HEX for char in value[key]): raise ValueError("analyzer Git object is malformed")
    return dict(value)


def build_study(*, sanitized_source: Mapping[str, Any], label_custody: Mapping[str, Any], dataset_sha256: str, producer: Mapping[str, Any], retrieval: Mapping[str, Any], output_slots: Mapping[str, Path | str], analyzers: Mapping[str, Mapping[str, Any]], guardrail_source: Mapping[str, Any], guardrail_source_artifact_sha256: str | None = None, bootstrap_seed: int = 17, bootstrap_resamples: int = 10000, min_route_fraction: float = .10) -> dict[str, Any]:
    """Build every field of ``gate.STUDY_SCHEMA`` from independently bound inputs."""
    source, custody = _source_rows(sanitized_source), _object(label_custody, "label custody")
    labels_by_partition, exclusion = _object(custody.get("labels"), "custody labels"), _object(custody.get("exclusion_receipt"), "custody exclusion receipt")
    required_receipt = {"schema", "source_artifact_sha256", "producer_sha256", "sanitized_source_sha256", "excluded_item_tokens", "excluded_count", "frozen_member_count"}
    if set(exclusion) != required_receipt or exclusion.get("schema") != SCHEMA or exclusion.get("excluded_count") != 4 or exclusion.get("frozen_member_count") != 1982 or exclusion.get("sanitized_source_sha256") != gate._sha(sanitized_source) or not isinstance(exclusion.get("excluded_item_tokens"), list): raise ValueError("custody exclusion receipt drift")
    excluded = set(exclusion["excluded_item_tokens"])
    if len(excluded) != 4 or any(_token(value, "excluded item token") not in {row["item_token"] for row in source["train"] + source["dev"]} for value in excluded): raise ValueError("custody exclusion membership drift")
    parts: dict[str, dict[str, str]] = {}; custodians: dict[str, dict[str, str]] = {}
    for partition in ("train", "dev"):
        rows = [row for row in source[partition] if row["item_token"] not in excluded]; label = _object(labels_by_partition.get(partition), f"{partition} custody label"); items = label.get("items")
        if not isinstance(items, list) or set(row["item_token"] for row in rows) != {row.get("item_token") for row in items}: raise ValueError("custody label membership mismatch")
        parts[partition] = {f"{kind}_sha256": gate._sha(sorted(row[f"{kind}_token"] for row in rows)) for kind in ("campaign", "group", "item")}; parts[partition]["crosswalk_sha256"] = gate._crosswalk(rows)
        custodians[partition] = {"source_artifact_sha256": _token(exclusion.get("source_artifact_sha256")), "producer_sha256": _token(exclusion.get("producer_sha256")), "crosswalk_sha256": parts[partition]["crosswalk_sha256"], "label_payload_sha256": gate._label_payload_sha(items)}
    if sum(len([row for row in source[p] if row["item_token"] not in excluded]) for p in ("train", "dev")) != 1982: raise ValueError("study membership denominator drift")
    slot_partitions = {"train_prefreeze": "train", "dev_prefreeze": "dev", "tau_select": "train", "dev_eval": "dev"}
    if set(output_slots) != set(slot_partitions): raise ValueError("four output slots are required")
    paths = [Path(path).resolve() for path in output_slots.values()]
    if len(set(paths)) != 4 or any(not path.is_absolute() or path.exists() or ROOT in path.parents for path in paths): raise ValueError("output slots must be distinct, new external absolute paths")
    producer_value, retrieval_value = _object(producer, "producer"), _object(retrieval, "retrieval")
    if set(producer_value) != {"artifact_sha256", "git_head", "git_tree", "retrieval_implementation_sha256"} or set(retrieval_value) != {"implementation_sha256", "raw_config_sha256", "p5_config_sha256"} or producer_value["retrieval_implementation_sha256"] != retrieval_value["implementation_sha256"]: raise ValueError("producer/retrieval receipt malformed")
    analyzers_value = {name: _git_receipt(_object(analyzers.get(name), f"{name} analyzer")) for name in ("gate", "prefreeze", "label", "guardrail")}
    manifest_sha = _validate_guardrail_manifest(_object(guardrail_source, "guardrail source"))
    # A source manifest has an object digest; the custody receipt must instead
    # pin the exact external file admitted to this run.  Older callers may omit
    # the file digest, in which case their object receipt remains the source.
    source_sha = _token(guardrail_source_artifact_sha256) if guardrail_source_artifact_sha256 is not None else manifest_sha
    study = {"schema": gate.STUDY_SCHEMA, "status": "frozen", "dataset_sha256": _token(dataset_sha256), "producer": dict(producer_value), "retrieval": dict(retrieval_value), "splits": {"source_separated_crosswalk_sha256": gate._sha({name: parts[name]["crosswalk_sha256"] for name in ("train", "dev")}), "question_random_split": False}, "partitions": parts, "label_custodians": custodians, "guardrail_custodian": {"source_artifact_sha256": source_sha, "producer_sha256": analyzers_value["guardrail"]["implementation_sha256"], "result_payload_sha256s": {name: gate._sha({"source_artifact_sha256": source_sha, "name": name, "pass": True}) for name in _NAMES}}, "selection": {"bootstrap": {"seed": bootstrap_seed, "resamples": bootstrap_resamples, "percentiles": [2.5, 97.5]}, "go_gates": {"min_route_fraction": min_route_fraction, "guardrails": {name: True for name in _NAMES}}}, "output_slots": {stage: {"stage": stage, "partition": partition, "path_sha256": gate._slot_path_sha(output_slots[stage])} for stage, partition in slot_partitions.items()}, "analyzers": analyzers_value}
    gate._validate_study(study)
    return study


def build_guardrail_evidence(*, study: Mapping[str, Any], guardrail_source: Mapping[str, Any], guardrail_source_artifact_sha256: str | None = None) -> dict[str, Any]:
    """Derive, rather than hand-copy, the four gate receipts accepted by AERP-4."""
    gate._validate_study(study)
    manifest_sha = _validate_guardrail_manifest(_object(guardrail_source, "guardrail source")); custodian = _object(study.get("guardrail_custodian"), "guardrail custodian")
    source_sha = _token(guardrail_source_artifact_sha256) if guardrail_source_artifact_sha256 is not None else manifest_sha
    if source_sha != custodian.get("source_artifact_sha256"): raise ValueError("guardrail source/study binding mismatch")
    analyzer = _object(_object(study.get("analyzers"), "analyzers").get("guardrail"), "guardrail analyzer")
    publication = {"input_receipts": [{"path_sha256": gate._slot_path_sha("guardrail-source"), "sha256": gate._sha(study)}, {"path_sha256": gate._slot_path_sha("guardrail-manifest"), "sha256": source_sha}], "analyzer_git_state": {key: analyzer[key] for key in ("git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes")}, "implementation_sha256": analyzer["implementation_sha256"]}
    return {"schema": gate.GUARDRAIL_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": "dev", "source_artifact_sha256": source_sha, "producer_sha256": custodian["producer_sha256"], "results": [{"name": name, "pass": True, "receipt_sha256": gate._sha({"source_artifact_sha256": source_sha, "name": name, "pass": True})} for name in _NAMES], "publication": publication}


def build_label_artifact(*, study: Mapping[str, Any], label_custody: Mapping[str, Any], partition: str) -> dict[str, Any]:
    """Bind one label-free custody payload to a frozen study for gate admission."""
    gate._validate_study(study)
    if partition not in {"train", "dev"}:
        raise ValueError("label partition is invalid")
    custody = _object(label_custody, "label custody")
    payload = _object(_object(custody.get("labels"), "custody labels").get(partition), "custody label payload")
    items = payload.get("items")
    if payload.get("schema") != gate.LABELS_SCHEMA or payload.get("status") != "complete" or payload.get("partition") != partition or not isinstance(items, list):
        raise ValueError("custody label payload is malformed")
    if any(set(_object(item, "custody label item")) != {"item_token", "gold_tokens", "unresolved_evidence_item_count", "evidence_item_count"} for item in items):
        raise ValueError("custody label payload exposes non-label fields")
    custodian = _object(_object(study.get("label_custodians"), "study label custodians").get(partition), "study label custodian")
    if gate._label_payload_sha(items) != custodian.get("label_payload_sha256"):
        raise ValueError("custody label payload digest drift")
    analyzer = _object(_object(study.get("analyzers"), "study analyzers").get("label"), "label analyzer")
    publication = {"input_receipts": [{"path_sha256": gate._slot_path_sha("study"), "sha256": gate._sha(study)}, {"path_sha256": gate._slot_path_sha("label-source"), "sha256": custodian["source_artifact_sha256"]}], "analyzer_git_state": {key: analyzer[key] for key in ("git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes")}, "implementation_sha256": analyzer["implementation_sha256"]}
    return {"schema": gate.LABELS_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": partition, "source_artifact_sha256": custodian["source_artifact_sha256"], "producer_sha256": custodian["producer_sha256"], "crosswalk_sha256": custodian["crosswalk_sha256"], "items": items, "publication": publication}
