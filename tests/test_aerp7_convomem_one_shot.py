import json
import time
from pathlib import Path

import pytest

from benchmarks import aerp7_convomem_authoring as authoring
from benchmarks import aerp7_convomem_one_shot as one
from benchmarks import aerp_execution_checkpoint as checkpoint
from benchmarks import aerp7_custodian_executor as custodian
from benchmarks import aerp7_convomem_executor as executor
from benchmarks.aerp7_convomem_confirmation import CustodyError


def _disk_calibration(model_receipt):
    current = {key: 100 for key in ("strong_raw", "static_p5_primary", "static_p5_repeat", "six_view_secondary")}
    originals = {f"replicate_{index:02d}": 100 for index in range(1, 6)}
    row = {
        "schema": authoring.formal.PRIVATE_DISK_CALIBRATION_SCHEMA, "calibration_id": "unit-calibration",
        "source_manifest_sha256": authoring._digest(authoring.CENSUS_SOURCE_MANIFEST), "model_receipt_sha256": authoring._digest(model_receipt),
        "candidate_resident_bytes": 100, "custody_resident_bytes": 100, "custody_sqlite_store_bytes": 100, "shared_current_store_bytes": 100,
        "current_ranking_measurement_bytes": current, "original_replicate_store_bytes": originals,
        "original_candidate_index_peak_bytes": 100, "original_chroma_peak_bytes": 100,
        "original_chroma_peak_policy": "sequential_one_build_peak_v1", "scoring_ephemeral_store_bytes": 100,
        "scoring_report_bytes": 100, "safety_margin_bytes": 100,
    }
    row["total_required_additional_bytes"] = sum((100, 100, 100, 100, 100, 100, 100, 100, 100, *current.values(), *originals.values()))
    row["calibration_sha256"] = authoring.formal.private_disk_calibration_digest(row)
    return row


def _plan(tmp_path: Path, secret: bytes):
    names = {
        "canonical_root": "canonical", "premix_root": "premix", "candidate_output_dir": "candidate", "custody_output_dir": "custody", "staging_root": "staging",
        "protocol_path": "protocol.json", "authorization_path": "authorization.json", "output_dir": "public", "custodian_public_config_path": "custodian-public.json", "final_output_path": "final.json", "one_shot_receipt_path": "receipt.json", "infrastructure_failure_receipt_path": "failure.json", "progress_receipt_path": "progress.json", "expected_checkpoint_path": "checkpoint.json", "original_root": "original", "model_dir": "model", "python_executable": "python.exe", "original_python": "original-python.exe",
    }
    row = {"schema": authoring.PLAN_SCHEMA, **{key: str((tmp_path / value).resolve()) for key, value in names.items()}, "custodian_nonce": "n" * 32, "public_authorization_nonce": "u" * 32, "custodian_expires_at_unix": 2_000_000_000}
    row.update({"source_manifest": authoring.CENSUS_SOURCE_MANIFEST, "census_semantics": authoring.CENSUS_SEMANTICS, "preparse_current_code_receipt": {"head": "1" * 64, "tree": "2" * 64, "diff_digest": "3" * 64, "dirty_policy": "clean_required"}, "model_receipt": {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": "a" * 64, "bytes": 1}]}})
    row["disk_preflight_calibration"] = _disk_calibration(row["model_receipt"])
    Path(row["staging_root"]).mkdir()
    return authoring.sign_one_shot_plan(row, operator_capability=secret)


def _private():
    return {"custody_binding_secret": b"b" * 32, "custody_capability_secret": b"c" * 32, "evidence_token_secret": b"e" * 32, "scorer_attestation_secret": b"a" * 32}


def test_candidate_receipt_builds_a_persistent_reference_without_loading_projection(tmp_path, monkeypatch):
    root = tmp_path / "candidate"; root.mkdir()
    built = {"candidate_output_dir": str(root), "generation_id": "g", "projection_raw_sha256": "a" * 64, "projection_canonical_sha256": "b" * 64, "dataset": {key: value * 64 for key, value in (("canonical_sha256", "c"), ("premix_sha256", "d"), ("revision_sha256", "e"), ("source_inventory_sha256", "f"))}, "query_count": 3, "candidate_text_count": 7}
    expected = {"reference": "stream"}; calls = []
    monkeypatch.setattr(one.confirmation, "load_candidate_projection", lambda *_args: (_ for _ in ()).throw(AssertionError("one-shot must not materialize projection")))
    monkeypatch.setattr(one.confirmation, "_snapshot", lambda path, *_args, **_kwargs: (b"{}", (1, 2), "r" * 64))
    monkeypatch.setattr(one.original_product, "candidate_projection_reference", lambda **kwargs: calls.append(kwargs) or expected)
    assert one._candidate_receipt(built) == {"generation_id": "g", "projection_raw_sha256": "a" * 64, "projection_canonical_sha256": "b" * 64, "query_count": 3, "candidate_text_count": 7, "ready_sha256": "r" * 64, "candidate_reference": expected}
    assert calls == [{"bundle_path": root, "generation_id": "g", "projection_raw_sha256": "a" * 64, "projection_canonical_sha256": "b" * 64, "dataset": built["dataset"], "query_count": 3, "candidate_text_count": 7}]


def _mark_completed_final(plan, private):
    freeze_path = Path(plan["output_dir"]) / "public-freeze.json"
    freeze = executor._load(freeze_path); freeze_sha = one._file_sha256(freeze_path)
    final_path = Path(plan["final_output_path"])
    payload = one._custodian_private_payload(plan=plan, private=private, public_freeze=freeze, freeze_file_sha256=freeze_sha)
    parsed = custodian._private(payload, packet_sha256=freeze["packet_sha256"], file_sha256=freeze_sha, output_path=final_path, formal_live=True, require_unexpired=False)
    _lock, consumed = custodian._authorization_paths(final_path, parsed["authorization_id"])
    final = json.loads(final_path.read_text(encoding="utf-8"))
    protocol = executor._load(Path(plan["protocol_path"]))
    context = custodian._consumed_marker_context(
        public={"config": {"freeze_packet_file_sha256": freeze_sha}, "packet": freeze, "protocol": protocol},
        private=parsed,
        output=final_path,
    )
    custodian._consume_authorization(
        consumed=consumed,
        context=context,
        packet_sha256=final["packet_sha256"],
        file_sha256=one._file_sha256(final_path),
        custody_capability_secret=parsed["custody_capability_secret"],
    )


def _install_success(monkeypatch, tmp_path, plan, calls):
    binding = {"checkpoint_sha256": "c" * 64}
    monkeypatch.setattr(one, "require_formal_durability", lambda: None)
    protocol = {"protocol_sha256": "a" * 64, "execution_checkpoint": binding, "candidate": {"ready_sha256": "r" * 64}}
    built = {"candidate_output_dir": plan["candidate_output_dir"], "custody_output_dir": plan["custody_output_dir"], "generation_id": "g", "projection_raw_sha256": "a" * 64, "projection_canonical_sha256": "b" * 64, "custody_raw_sha256": "d" * 64}
    monkeypatch.setattr(authoring, "clean_code_receipt", lambda _root: {"head": "1" * 64, "tree": "2" * 64, "diff_digest": "3" * 64, "dirty_policy": "clean_required"})
    monkeypatch.setattr(authoring, "observe_source_manifest", lambda **_kwargs: authoring.CENSUS_SOURCE_MANIFEST)
    monkeypatch.setattr(checkpoint, "capture_binding", lambda **_kwargs: calls.append("checkpoint") or binding)
    monkeypatch.setattr(checkpoint, "require_live_binding", lambda *_args, **_kwargs: binding)
    monkeypatch.setattr(one.confirmation, "build_prelabel_bundle", lambda **_kwargs: calls.append("source") or built)
    monkeypatch.setattr(one, "_publish_generation_seal", lambda **_kwargs: built)
    candidate_reference = {"reference": "candidate"}
    monkeypatch.setattr(one, "_candidate_receipt", lambda _built: {"generation_id": "g", "ready_sha256": "r" * 64, "projection_raw_sha256": "a" * 64, "projection_canonical_sha256": "b" * 64, "query_count": 1, "candidate_text_count": 1, "candidate_reference": candidate_reference})
    custody_reference = {"reference": "custody"}
    monkeypatch.setattr(one.confirmation, "custody_reference", lambda **_kwargs: custody_reference)
    preflight = {"preflight": "private"}
    monkeypatch.setattr(authoring, "author_private_disk_preflight", lambda **_kwargs: calls.append("preflight-author") or preflight)
    monkeypatch.setattr(authoring, "validate_private_disk_preflight", lambda value, **_kwargs: value)
    monkeypatch.setattr(one.formal, "enforce_private_disk_preflight", lambda **_kwargs: calls.append("preflight-gate") or {"checked": True})
    monkeypatch.setattr(authoring, "author_formal_protocol", lambda **_kwargs: calls.append("protocol") or protocol)
    monkeypatch.setattr(authoring, "sign_operator_authorization", lambda **_kwargs: calls.append("operator-auth") or {"auth": True})
    writes = []
    def write(path, value):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(value), encoding="utf-8"); writes.append((path, value))
    monkeypatch.setattr(one.executor, "_write_new", write)
    monkeypatch.setattr(one.custodian, "validate_completed_packet", lambda **kwargs: {"packet_sha256": kwargs["outer"]["packet_sha256"], "gate_outcome": kwargs["outer"]["gate_decision"]["outcome"]})
    def coordinator(config):
        calls.append("public")
        assert config["original_python"] == str(Path(plan["original_python"]).resolve())
        freeze = Path(config["output_dir"]) / "public-freeze.json"; freeze.parent.mkdir(parents=True, exist_ok=True)
        freeze.write_text(json.dumps({"protocol": protocol, "packet_sha256": "f" * 64}), encoding="utf-8")
    monkeypatch.setattr(one.executor, "public_coordinator", coordinator)
    real_snapshot = one.confirmation._snapshot
    def public_snapshot(path, *args, **kwargs):
        path = Path(path)
        # The fake source bundle has no public READY files, but the child-side
        # consumed authorization marker is a real public artifact and must use
        # its actual stable bytes.
        if path == Path(plan["final_output_path"]) or ".aerp7-consumed-" in path.name:
            return real_snapshot(path, *args, **kwargs)
        return b"", (1, 1), "q" * 64
    monkeypatch.setattr(
        one.confirmation, "_snapshot",
        public_snapshot,
    )
    def launch(**kwargs):
        calls.append("custodian")
        assert kwargs["private_payload"]["binding_secret"] == "b" * 32
        assert kwargs["private_payload"]["output_path"] == plan["final_output_path"]
        Path(plan["final_output_path"]).write_text(json.dumps({"packet_sha256": "f" * 64, "gate_decision": {"outcome": "PASS"}}), encoding="utf-8")
        # The real child consumes the deterministic authorization only after
        # publishing its exact final bytes.  The one-shot parent must require
        # this marker instead of trusting launcher telemetry.
        _mark_completed_final(plan, _private())
        return {"packet_sha256": "f" * 64, "output_file_sha256": one._file_sha256(Path(plan["final_output_path"]))}
    monkeypatch.setattr(one.custodian, "launch_custodian", launch)
    return writes


def test_one_shot_runs_in_order_and_rejects_any_second_formal_attempt(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    result = one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls == ["checkpoint", "source", "preflight-author", "preflight-gate", "protocol", "operator-auth", "public", "custodian"]
    assert result["gate_outcome"] == "PASS" and Path(plan["one_shot_receipt_path"]).is_file()
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls.count("custodian") == 1


def test_formal_one_shot_delegates_the_execution_body_with_formal_switches(tmp_path, monkeypatch):
    """Characterize the public caller before extracting its private body."""
    secret = b"o" * 32; plan = _plan(tmp_path, secret); observed = {}
    monkeypatch.setattr(one, "require_formal_durability", lambda: None)
    monkeypatch.setattr(one, "_require_new_formal_targets", lambda **_kwargs: None)
    monkeypatch.setattr(one, "_existing_receipt", lambda **_kwargs: None)
    monkeypatch.setattr(one, "_recover_final_without_receipt", lambda **_kwargs: None)

    def shared(**kwargs):
        observed.update(kwargs)
        return {"shared": True}

    monkeypatch.setattr(one, "_run_execution", shared)
    assert one.run_one_shot(
        signed_plan=plan, operator_capability=secret, private_capabilities=_private(),
        model_receipt=plan["model_receipt"], repo_root=tmp_path,
    ) == {"shared": True}
    assert observed["enforce_disk_preflight"] is True
    assert observed["publish_formal_receipt"] is True
    assert observed["capacity_observer"] is None


def test_capacity_calibration_reuses_execution_but_skips_only_admission_and_formal_receipt(tmp_path, monkeypatch):
    secret = b"o" * 32
    envelope = {"schema": "aerp7-convomem-capacity-envelope-v1", "payload": 1}
    envelope["envelope_sha256"] = one._digest(envelope)
    envelope_path = tmp_path / "envelope.json"; envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    collector_path = tmp_path / "collector.py"; collector_path.write_text("collector", encoding="utf-8")
    model = {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": "a" * 64, "bytes": 1}]}
    fields = {
        "schema": authoring.CAPACITY_CALIBRATION_PLAN_SCHEMA, "purpose": authoring.CAPACITY_CALIBRATION_PURPOSE,
        "formal_evidence_eligible": False, "scientific_metrics_retained": False,
        "repo_root": str(tmp_path.resolve()), "run_root": str((tmp_path / "run").resolve()),
        "canonical_root": str((tmp_path / "canonical").resolve()), "premix_root": str((tmp_path / "premix").resolve()),
        "expected_checkpoint_path": str((tmp_path / "checkpoint.json").resolve()), "original_root": str((tmp_path / "original").resolve()),
        "model_dir": str((tmp_path / "model").resolve()), "python_executable": str((tmp_path / "python").resolve()), "original_python": str((tmp_path / "original-python").resolve()),
        "source_manifest": authoring.CENSUS_SOURCE_MANIFEST, "model_receipt": model, "census_semantics": authoring.CENSUS_SEMANTICS,
        "preparse_current_code_receipt": {"head": "a" * 40, "tree": "b" * 40, "diff_digest": "c" * 64, "dirty_policy": "clean_required"},
        "capacity_envelope_path": str(envelope_path.resolve()), "capacity_envelope_file_sha256": one._file_sha256(envelope_path), "capacity_envelope_semantic_sha256": envelope["envelope_sha256"],
        "capacity_collector_path": str(collector_path.resolve()), "capacity_collector_sha256": one._file_sha256(collector_path), "capacity_launcher_path": str(collector_path.resolve()), "capacity_launcher_sha256": one._file_sha256(collector_path),
        "observation_output_path": str((tmp_path.parent / (tmp_path.name + "-observation.json")).resolve()), "calibration_receipt_path": str((tmp_path.parent / (tmp_path.name + "-receipt.json")).resolve()),
        "disk_safety_margin_bytes": 0, "rss_safety_margin_bytes": 0, "public_authorization_nonce": "u" * 32, "custodian_nonce": "n" * 32, "custodian_expires_at_unix": 2_000_000_000,
    }
    plan = authoring.sign_capacity_calibration_plan(fields, operator_capability=secret)
    monkeypatch.setattr(one, "require_formal_durability", lambda: None)
    def fake_execution(**kwargs):
        assert kwargs["enforce_disk_preflight"] is False and kwargs["publish_formal_receipt"] is False
        assert (Path(plan["run_root"]) / "CALIBRATION_ONLY.json").is_file()
        emit = kwargs["capacity_observer"]
        emit({"schema": executor.CAPACITY_EVENT_SCHEMA, "kind": "source_bundle_build", "candidate_payload_bytes": 2, "candidate_ready_bytes": 3, "custody_payload_bytes": 4, "custody_ready_bytes": 5})
        for role in ("raw", "p5_primary", "p5_repeat", "six"):
            emit({"schema": executor.CAPACITY_EVENT_SCHEMA, "kind": "current_artifact", "role": role})
        for number in range(1, 6):
            emit({"schema": executor.CAPACITY_EVENT_SCHEMA, "kind": "original_replicate", "number": number, "chroma_peak_bytes": number})
        emit({"schema": executor.CAPACITY_EVENT_SCHEMA, "kind": "custodian_scoring", "scoring_db_peak_bytes": 7, "report_bytes": 11, "mapping_ledger_bytes": 13, "components": {"candidate_store_bytes": 1}})
        receipt = {"supervisor_observation_complete": True, "observed_process_tree_peak_rss_bytes": 1}
        supervisors = {f"current-{role}": dict(receipt) for role in ("raw", "p5_primary", "p5_repeat", "six")}
        supervisors.update({f"original-{number}": dict(receipt) for number in range(5)})
        return {"binding": {"checkpoint_sha256": "c" * 64}, "protocol": {"protocol_sha256": "p" * 64}, "public_freeze_file_sha256": "f" * 64, "final": {"packet_sha256": "z" * 64}, "public_freeze": {"supervisors": supervisors}, "capacity_supervisors": {"source_bundle_build": dict(receipt), "public_coordinator": dict(receipt), "custodian_scoring": dict(receipt)}}
    monkeypatch.setattr(one, "_run_execution", fake_execution)
    result = one.run_capacity_calibration(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=model, repo_root=tmp_path)
    assert result["formal_evidence_eligible"] is False and result["scientific_metrics_retained"] is False
    assert result["disposable_run_root_removed"] is True and not Path(plan["run_root"]).exists()
    assert Path(plan["observation_output_path"]).is_file() and Path(plan["calibration_receipt_path"]).is_file()


def test_capacity_calibration_failure_keeps_marked_disposable_root_and_publishes_no_completion(tmp_path, monkeypatch):
    secret = b"o" * 32
    envelope = {"schema": "aerp7-convomem-capacity-envelope-v1", "payload": 1}; envelope["envelope_sha256"] = one._digest(envelope)
    envelope_path = tmp_path / "envelope.json"; envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    collector_path = tmp_path / "collector.py"; collector_path.write_text("collector", encoding="utf-8")
    model = {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": "a" * 64, "bytes": 1}]}
    fields = {
        "schema": authoring.CAPACITY_CALIBRATION_PLAN_SCHEMA, "purpose": authoring.CAPACITY_CALIBRATION_PURPOSE, "formal_evidence_eligible": False, "scientific_metrics_retained": False,
        "repo_root": str(tmp_path.resolve()), "run_root": str((tmp_path / "run").resolve()), "canonical_root": str((tmp_path / "canonical").resolve()), "premix_root": str((tmp_path / "premix").resolve()), "expected_checkpoint_path": str((tmp_path / "checkpoint.json").resolve()), "original_root": str((tmp_path / "original").resolve()), "model_dir": str((tmp_path / "model").resolve()), "python_executable": str((tmp_path / "python").resolve()), "original_python": str((tmp_path / "original-python").resolve()), "source_manifest": authoring.CENSUS_SOURCE_MANIFEST, "model_receipt": model, "census_semantics": authoring.CENSUS_SEMANTICS, "preparse_current_code_receipt": {"head": "a" * 40, "tree": "b" * 40, "diff_digest": "c" * 64, "dirty_policy": "clean_required"}, "capacity_envelope_path": str(envelope_path.resolve()), "capacity_envelope_file_sha256": one._file_sha256(envelope_path), "capacity_envelope_semantic_sha256": envelope["envelope_sha256"], "capacity_collector_path": str(collector_path.resolve()), "capacity_collector_sha256": one._file_sha256(collector_path), "capacity_launcher_path": str(collector_path.resolve()), "capacity_launcher_sha256": one._file_sha256(collector_path), "observation_output_path": str((tmp_path.parent / (tmp_path.name + "-observation.json")).resolve()), "calibration_receipt_path": str((tmp_path.parent / (tmp_path.name + "-receipt.json")).resolve()), "disk_safety_margin_bytes": 0, "rss_safety_margin_bytes": 0, "public_authorization_nonce": "u" * 32, "custodian_nonce": "n" * 32, "custodian_expires_at_unix": 2_000_000_000,
    }
    plan = authoring.sign_capacity_calibration_plan(fields, operator_capability=secret)
    monkeypatch.setattr(one, "require_formal_durability", lambda: None)
    monkeypatch.setattr(one, "_run_execution", lambda **_kwargs: (_ for _ in ()).throw(CustodyError("injected")))
    with pytest.raises(CustodyError, match="injected"):
        one.run_capacity_calibration(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=model, repo_root=tmp_path)
    assert not Path(plan["run_root"]).exists() and not Path(plan["observation_output_path"]).exists()
    failure = json.loads(Path(plan["calibration_receipt_path"]).read_text(encoding="utf-8"))
    assert failure["schema"] == "aerp7-convomem-capacity-calibration-failure-v1" and failure["disposable_run_root_removed"] is True


def test_capacity_coverage_rejects_one_missing_component_or_supervisor() -> None:
    receipt = {"supervisor_observation_complete": True, "observed_process_tree_peak_rss_bytes": 1}
    supervisors = {name: dict(receipt) for name in one._CAPACITY_SUPERVISORS}
    events = [
        {"kind": "source_bundle_build", "candidate_payload_bytes": 1, "candidate_ready_bytes": 1, "custody_payload_bytes": 1, "custody_ready_bytes": 1},
        *[{"kind": "current_artifact", "role": role} for role in ("raw", "p5_primary", "p5_repeat", "six")],
        *[{"kind": "original_replicate", "number": number} for number in range(1, 6)],
        {"kind": "custodian_scoring"},
    ]
    one._validate_capacity_coverage(events=events, supervisors=supervisors)
    with pytest.raises(CustodyError, match="component_coverage"):
        one._validate_capacity_coverage(events=events[:-1], supervisors=supervisors)
    supervisors.pop("original-4")
    with pytest.raises(CustodyError, match="supervisor_coverage"):
        one._validate_capacity_coverage(events=events, supervisors=supervisors)


def test_capacity_external_publish_target_rejects_existing_or_nonreal_parent(tmp_path) -> None:
    existing = tmp_path / "existing.json"; existing.write_text("occupied", encoding="utf-8")
    with pytest.raises(CustodyError, match="external_output_invalid"):
        one._require_external_publish_target(existing)
    with pytest.raises(CustodyError, match="external_output_invalid"):
        one._require_external_publish_target(tmp_path / "missing" / "output.json")


def test_source_capacity_tree_charges_payload_and_both_ready_files(tmp_path) -> None:
    candidate = tmp_path / "candidate"; custody = tmp_path / "custody"
    candidate.mkdir(); custody.mkdir()
    (candidate / "projection.json").write_bytes(b"candidate")
    (candidate / "READY.json").write_bytes(b"candidate-ready")
    (custody / "sealed-custody.json").write_bytes(b"custody")
    (custody / "READY.json").write_bytes(b"custody-ready")
    assert one._source_capacity_tree(candidate_root=candidate, custody_root=custody) == {
        "candidate_payload_bytes": 9, "candidate_ready_bytes": 15,
        "custody_payload_bytes": 7, "custody_ready_bytes": 13,
    }


@pytest.mark.parametrize("stage", ("checkpoint", "source", "public"))
def test_pre_custody_failures_never_launch_custodian(tmp_path, monkeypatch, stage):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    if stage == "checkpoint": monkeypatch.setattr(checkpoint, "capture_binding", lambda **_kwargs: (_ for _ in ()).throw(CustodyError("checkpoint")))
    elif stage == "source": monkeypatch.setattr(one.confirmation, "build_prelabel_bundle", lambda **_kwargs: (_ for _ in ()).throw(CustodyError("source")))
    else: monkeypatch.setattr(one.executor, "public_coordinator", lambda _config: (_ for _ in ()).throw(CustodyError("public")))
    with pytest.raises(CustodyError): one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert "custodian" not in calls and not Path(plan["one_shot_receipt_path"]).exists()
    failure = json.loads(Path(plan["infrastructure_failure_receipt_path"]).read_text(encoding="utf-8"))
    assert failure["stage"] == {"checkpoint": "preparse", "source": "source", "public": "public"}[stage] and failure["plan_sha256"] == plan["plan_sha256"]


def test_private_mismatch_and_custodian_failure_leave_no_final_receipt(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    bad = _private(); bad.pop("custody_binding_secret")
    with pytest.raises(CustodyError, match="private_capability"): one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=bad, model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls == []
    monkeypatch.setattr(one.custodian, "launch_custodian", lambda **_kwargs: (_ for _ in ()).throw(CustodyError("custodian")))
    with pytest.raises(CustodyError, match="custodian"): one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert not Path(plan["one_shot_receipt_path"]).exists()


def test_private_custody_contract_rejects_non_utf8_secret_before_checkpoint_or_source_parse(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    private = _private(); private["custody_binding_secret"] = b"\xff" * 32
    with pytest.raises(CustodyError, match="private_capability_encoding_invalid"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=private, model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls == []


def test_preexisting_source_bundle_is_rejected_before_source_parse(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    Path(plan["candidate_output_dir"]).mkdir()
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert "source" not in calls and "protocol" not in calls and "custodian" not in calls


def test_final_packet_replaced_after_custodian_return_never_gets_completion_receipt(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    def launch(**_kwargs):
        calls.append("custodian")
        Path(plan["final_output_path"]).write_text(json.dumps({"packet_sha256": "forged"}), encoding="utf-8")
        return {"packet_sha256": "z" * 64}
    monkeypatch.setattr(one.custodian, "launch_custodian", launch)
    # The consumed-marker boundary rejects before a forged public packet can
    # even reach completed-packet validation.
    with pytest.raises(CustodyError, match="initial_consumed_marker_invalid"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert not Path(plan["one_shot_receipt_path"]).exists()


def test_initial_promotion_rejects_final_replacement_before_launcher_telemetry(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)

    def launch(**_kwargs):
        calls.append("custodian")
        final = Path(plan["final_output_path"])
        final.write_text(json.dumps({"packet_sha256": "f" * 64, "gate_decision": {"outcome": "PASS"}}, separators=(",", ":")), encoding="utf-8")
        _mark_completed_final(plan, _private())
        # Simulate an attacker replacing the public file before the launcher
        # does its separate telemetry parse/hash and before one-shot snapshots.
        final.write_text(json.dumps({"packet_sha256": "f" * 64, "gate_decision": {"outcome": "PASS"}}, indent=2), encoding="utf-8")
        return {"packet_sha256": "f" * 64, "output_file_sha256": one._file_sha256(final)}

    monkeypatch.setattr(one.custodian, "launch_custodian", launch)
    with pytest.raises(CustodyError, match="initial_consumed_marker_invalid"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert not Path(plan["one_shot_receipt_path"]).exists()


def test_initial_promotion_rejects_joint_final_and_marker_replacement_without_capability(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)

    def launch(**_kwargs):
        calls.append("custodian")
        final = Path(plan["final_output_path"])
        final.write_text(json.dumps({"packet_sha256": "f" * 64, "gate_decision": {"outcome": "PASS"}}, separators=(",", ":")), encoding="utf-8")
        _mark_completed_final(plan, _private())
        final.write_text(json.dumps({"packet_sha256": "f" * 64, "gate_decision": {"outcome": "PASS"}}, indent=2), encoding="utf-8")
        freeze = executor._load(Path(plan["output_dir"]) / "public-freeze.json"); freeze_sha = one._file_sha256(Path(plan["output_dir"]) / "public-freeze.json")
        payload = one._custodian_private_payload(plan=plan, private=_private(), public_freeze=freeze, freeze_file_sha256=freeze_sha)
        parsed = custodian._private(payload, packet_sha256=freeze["packet_sha256"], file_sha256=freeze_sha, output_path=final, formal_live=True, require_unexpired=False)
        _lock, marker_path = custodian._authorization_paths(final, parsed["authorization_id"])
        forged = json.loads(marker_path.read_text(encoding="utf-8"))
        forged["output_file_sha256"] = one._file_sha256(final)
        forged["marker_sha256"] = custodian._digest(custodian._consumed_unsigned(forged))
        # An attacker can recompute public digests, but does not possess the
        # capability secret needed for the domain-separated HMAC.
        marker_path.write_text(json.dumps(forged), encoding="utf-8")
        return {"packet_sha256": "f" * 64, "output_file_sha256": one._file_sha256(final)}

    monkeypatch.setattr(one.custodian, "launch_custodian", launch)
    with pytest.raises(CustodyError, match="initial_consumed_marker_invalid"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert not Path(plan["one_shot_receipt_path"]).exists()


def test_final_snapshot_binds_child_bytes_when_path_is_replaced_after_read(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    prior_snapshot = one.confirmation._snapshot
    final_path = Path(plan["final_output_path"])
    def swap_after_snapshot(path, *args, **kwargs):
        result = prior_snapshot(path, *args, **kwargs)
        if Path(path) == final_path:
            final_path.write_text(json.dumps({"packet_sha256": "0" * 64, "gate_decision": {"outcome": "FAIL"}}), encoding="utf-8")
        return result
    monkeypatch.setattr(one.confirmation, "_snapshot", swap_after_snapshot)
    result = one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    receipt = json.loads(Path(plan["one_shot_receipt_path"]).read_text(encoding="utf-8"))
    assert result["custodian_packet_sha256"] == "f" * 64
    assert receipt["final_output_file_sha256"] != one._file_sha256(final_path)
    # A formal output directory is single-use; no completed artifact is reused.
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)


def test_self_hashed_receipt_without_its_final_artifacts_is_not_a_retry(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret)
    monkeypatch.setattr(one, "require_formal_durability", lambda: None)
    forged = {"schema": one.RECEIPT_SCHEMA, "plan_sha256": plan["plan_sha256"], "protocol_sha256": "p" * 64, "checkpoint_sha256": "c" * 64, "public_freeze_sha256": "f" * 64, "final_output_file_sha256": "x" * 64, "custodian_packet_sha256": "z" * 64, "gate_outcome": "PASS"}
    forged["receipt_sha256"] = one._digest(forged)
    Path(plan["one_shot_receipt_path"]).write_text(json.dumps(forged), encoding="utf-8")
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)


@pytest.mark.parametrize("field", ("protocol_sha256", "checkpoint_sha256"))
def test_existing_receipt_cannot_rebind_protocol_or_checkpoint(tmp_path, monkeypatch, field):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    receipt_path = Path(plan["one_shot_receipt_path"])
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")); receipt[field] = "0" * 64; receipt["receipt_sha256"] = one._digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)


def test_existing_receipt_rejects_semantically_identical_final_reserialization(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    final_path = Path(plan["final_output_path"])
    final = json.loads(final_path.read_text(encoding="utf-8"))
    final_path.write_text(json.dumps(final, indent=2), encoding="utf-8")
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)


def test_final_only_crash_recovers_after_expiry_without_relaunching_custodian(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    original_publish = one.executor.formal.publish_nonreplace
    def crash_receipt(path, payload, **kwargs):
        if Path(path) == Path(plan["one_shot_receipt_path"]):
            raise RuntimeError("receipt lost after final")
        return original_publish(path, payload, **kwargs)
    monkeypatch.setattr(one.executor.formal, "publish_nonreplace", crash_receipt)
    with pytest.raises(RuntimeError, match="receipt lost after final"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    monkeypatch.setattr(one.executor.formal, "publish_nonreplace", original_publish)
    _mark_completed_final(plan, _private())
    monkeypatch.setattr(one.time, "time", lambda: plan["custodian_expires_at_unix"] + 1)
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls.count("custodian") == 1


def test_final_only_recovery_rejects_raw_reserialization_after_consumption(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    original_publish = one.executor.formal.publish_nonreplace
    monkeypatch.setattr(one.executor.formal, "publish_nonreplace", lambda path, payload, **kwargs: (_ for _ in ()).throw(RuntimeError("receipt crash")) if Path(path) == Path(plan["one_shot_receipt_path"]) else original_publish(path, payload, **kwargs))
    with pytest.raises(RuntimeError, match="receipt crash"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    monkeypatch.setattr(one.executor.formal, "publish_nonreplace", original_publish)
    _mark_completed_final(plan, _private())
    final_path = Path(plan["final_output_path"])
    final_path.write_text(json.dumps(json.loads(final_path.read_text(encoding="utf-8")), indent=2), encoding="utf-8")
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)


def test_invalid_preparse_commitment_blocks_the_first_official_parser(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    plan["census_semantics"]["top_k"] = 9
    with pytest.raises(CustodyError, match="plan_digest"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls == []


def test_preparse_current_code_drift_and_source_toctou_stop_before_publication(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    monkeypatch.setattr(authoring, "clean_code_receipt", lambda _root: {"head": "9" * 64, "tree": "2" * 64, "diff_digest": "3" * 64, "dirty_policy": "clean_required"})
    with pytest.raises(CustodyError, match="preparse_code_drift"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls == []
    second = tmp_path / "second"; second.mkdir()
    plan = _plan(second, secret); calls = []; _install_success(monkeypatch, second, plan, calls)
    observed = iter((authoring.CENSUS_SOURCE_MANIFEST, {**authoring.CENSUS_SOURCE_MANIFEST, "inventory_sha256": "0" * 64}))
    monkeypatch.setattr(authoring, "observe_source_manifest", lambda **_kwargs: next(observed))
    with pytest.raises(CustodyError, match="source_toctou"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=second)
    assert "public" not in calls and "custodian" not in calls


def test_formal_durability_rejects_windows_host(monkeypatch):
    monkeypatch.setattr(one.os, "name", "nt")
    with pytest.raises(CustodyError, match="durability_host_unsupported"):
        one.require_formal_durability()


def test_untimed_worker_heartbeat_updates_a_durable_progress_receipt(tmp_path):
    path = tmp_path / "progress.json"
    with one._heartbeat(path=path, plan_sha256="p" * 64, stage="public"):
        time.sleep(1.05)
    progress = json.loads(path.read_text(encoding="utf-8"))
    assert progress["schema"] == one.PROGRESS_SCHEMA and progress["stage"] == "public" and progress["heartbeat"] >= 1


def test_post_publication_receipt_crash_cannot_resume_the_formal_attempt(tmp_path, monkeypatch):
    secret = b"o" * 32; plan = _plan(tmp_path, secret); calls = []; _install_success(monkeypatch, tmp_path, plan, calls)
    original = one.executor.formal.publish_nonreplace; attempts = {"receipt": 0}
    def fail_receipt_once(path, payload, **kwargs):
        if Path(path) == Path(plan["one_shot_receipt_path"]):
            attempts["receipt"] += 1
            if attempts["receipt"] == 1:
                raise RuntimeError("injected receipt crash")
        return original(path, payload, **kwargs)
    monkeypatch.setattr(one.executor.formal, "publish_nonreplace", fail_receipt_once)
    with pytest.raises(RuntimeError, match="receipt crash"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls.count("public") == 1 and calls.count("custodian") == 1
    _mark_completed_final(plan, _private())
    with pytest.raises(CustodyError, match="formal_output_not_new"):
        one.run_one_shot(signed_plan=plan, operator_capability=secret, private_capabilities=_private(), model_receipt=plan["model_receipt"], repo_root=tmp_path)
    assert calls.count("source") == 1 and calls.count("public") == 1 and calls.count("custodian") == 1
