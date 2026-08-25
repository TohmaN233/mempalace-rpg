import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import aerp7_convomem_authoring as authoring
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks import aerp_execution_checkpoint as checkpoint
from benchmarks import aerp7_convomem_rank as rank
from benchmarks.aerp7_convomem_confirmation import CustodyError


def _disk_calibration(model_receipt):
    current = {key: 100 for key in ("strong_raw", "static_p5_primary", "static_p5_repeat", "six_view_secondary")}
    originals = {f"replicate_{index:02d}": 100 for index in range(1, 6)}
    row = {
        "schema": authoring.formal.PRIVATE_DISK_CALIBRATION_SCHEMA, "calibration_id": "unit-calibration",
        "source_manifest_sha256": authoring._digest(authoring.CENSUS_SOURCE_MANIFEST),
        "model_receipt_sha256": authoring._digest(model_receipt),
        "candidate_resident_bytes": 100, "custody_resident_bytes": 100, "custody_sqlite_store_bytes": 100,
        "shared_current_store_bytes": 100, "current_ranking_measurement_bytes": current,
        "original_replicate_store_bytes": originals, "original_candidate_index_peak_bytes": 100,
        "original_chroma_peak_bytes": 100, "original_chroma_peak_policy": "sequential_one_build_peak_v1",
        "scoring_ephemeral_store_bytes": 100, "scoring_report_bytes": 100, "safety_margin_bytes": 100,
    }
    row["total_required_additional_bytes"] = sum((
        row["candidate_resident_bytes"], row["custody_resident_bytes"], row["custody_sqlite_store_bytes"],
        row["shared_current_store_bytes"], row["original_candidate_index_peak_bytes"], row["original_chroma_peak_bytes"],
        row["scoring_ephemeral_store_bytes"], row["scoring_report_bytes"], row["safety_margin_bytes"],
        *current.values(), *originals.values(),
    ))
    row["calibration_sha256"] = authoring.formal.private_disk_calibration_digest(row)
    return row


def _private_preflight(calibration, *, custody_reference_sha256="a" * 64, candidate_input=10, custody_input=11,
                       item_count=2, evidence_count=3):
    row = {
        "schema": authoring.formal.PRIVATE_DISK_PREFLIGHT_SCHEMA, "plan_sha256": "b" * 64,
        "calibration": calibration, "custody_reference_sha256": custody_reference_sha256, "generation_id": "generation",
        "candidate_input_bytes": candidate_input, "custody_input_bytes": custody_input,
        "custody_logical_item_count": item_count, "custody_evidence_span_count": evidence_count,
        "custody_sqlite_store_bytes": calibration["custody_sqlite_store_bytes"],
        "custody_peak_disk_input_and_store_bytes": candidate_input + custody_input + calibration["custody_sqlite_store_bytes"],
    }
    row["preflight_sha256"] = authoring.formal.private_disk_preflight_digest(row)
    row["preflight_hmac"] = "d" * 64
    return row


def test_private_disk_preflight_requires_calibrated_chroma_and_exact_component_arithmetic():
    model = {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": "a" * 64, "bytes": 1}]}
    calibration = _disk_calibration(model)
    row = _private_preflight(calibration)
    assert authoring.formal.validate_private_disk_preflight(row)["calibration"]["original_chroma_peak_bytes"] == 100
    missing = dict(calibration); missing["original_chroma_peak_bytes"] = 0
    missing["calibration_sha256"] = authoring.formal.private_disk_calibration_digest(missing)
    with pytest.raises(CustodyError, match="original_chroma_missing"):
        authoring.formal.validate_private_disk_calibration(missing)
    tampered = dict(calibration); tampered["total_required_additional_bytes"] += 1
    tampered["calibration_sha256"] = authoring.formal.private_disk_calibration_digest(tampered)
    with pytest.raises(CustodyError, match="arithmetic"):
        authoring.formal.validate_private_disk_calibration(tampered)


def test_private_disk_preflight_rechecks_reference_and_fails_on_insufficient_free_space(tmp_path, monkeypatch):
    model = {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": "a" * 64, "bytes": 1}]}
    calibration = _disk_calibration(model)
    candidate_root, custody_root, staging = tmp_path / "candidate", tmp_path / "custody", tmp_path / "staging"
    candidate_root.mkdir(); custody_root.mkdir(); staging.mkdir()
    (candidate_root / "projection.json").write_bytes(b"candidate")
    (custody_root / "sealed-custody.json").write_bytes(b"custody")
    candidate = {"bundle_path": str(candidate_root)}
    reference = {"generation_id": "generation", "item_count": 2, "evidence_span_count": 3}
    row = _private_preflight(calibration, custody_reference_sha256=authoring.formal._digest(reference), candidate_input=9, custody_input=7)
    monkeypatch.setattr(authoring.formal.confirmation, "custody_reference", lambda **_kwargs: reference)
    monkeypatch.setattr(authoring.formal.shutil, "disk_usage", lambda _path: SimpleNamespace(free=calibration["total_required_additional_bytes"] - 1))
    with pytest.raises(CustodyError, match="insufficient_free_bytes"):
        authoring.formal.enforce_private_disk_preflight(
            preflight=row, candidate_bundle_root=candidate_root, custody_bundle_root=custody_root,
            candidate_reference=candidate, staging_root=staging,
        )


def test_signed_one_shot_plan_is_exact_and_rejects_path_or_hmac_drift(tmp_path: Path) -> None:
    secret = b"x" * 32
    fields = {
        "schema": authoring.PLAN_SCHEMA,
        "canonical_root": str((tmp_path / "canonical").resolve()),
        "premix_root": str((tmp_path / "premix").resolve()),
        "candidate_output_dir": str((tmp_path / "candidate").resolve()),
        "custody_output_dir": str((tmp_path / "custody").resolve()),
        "staging_root": str((tmp_path / "staging").resolve()),
        "protocol_path": str((tmp_path / "protocol.json").resolve()),
        "authorization_path": str((tmp_path / "authorization.json").resolve()),
        "output_dir": str((tmp_path / "output").resolve()),
        "custodian_public_config_path": str((tmp_path / "custodian-public.json").resolve()),
        "final_output_path": str((tmp_path / "final.json").resolve()),
        "one_shot_receipt_path": str((tmp_path / "receipt.json").resolve()),
        "infrastructure_failure_receipt_path": str((tmp_path / "failure.json").resolve()),
        "progress_receipt_path": str((tmp_path / "progress.json").resolve()),
        "expected_checkpoint_path": str((tmp_path / "checkpoint.json").resolve()),
        "original_root": str((tmp_path / "original").resolve()),
        "model_dir": str((tmp_path / "model").resolve()),
        "python_executable": str((tmp_path / "python.exe").resolve()),
        "original_python": str((tmp_path / "original-python.exe").resolve()),
        "custodian_nonce": "n" * 32,
        "public_authorization_nonce": "u" * 32,
        "custodian_expires_at_unix": 2_000_000_000,
        "source_manifest": authoring.CENSUS_SOURCE_MANIFEST,
        "census_semantics": authoring.CENSUS_SEMANTICS,
        "preparse_current_code_receipt": {"head": "a" * 40, "tree": "b" * 40, "diff_digest": "c" * 64, "dirty_policy": "clean_required"},
        "model_receipt": {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": hashlib.sha256(b"w").hexdigest(), "bytes": 1}]},
    }
    fields["disk_preflight_calibration"] = _disk_calibration(fields["model_receipt"])
    plan = authoring.sign_one_shot_plan(fields, operator_capability=secret)
    signed = authoring.validate_one_shot_plan(plan, operator_capability=secret)
    assert signed["plan_sha256"] == plan["plan_sha256"]
    assert signed["census_semantics"]["selection_algorithm"] == confirmation.CENSUS_SELECTION_ALGORITHM
    assert signed["census_semantics"]["candidate_projection_transport"] == authoring.formal.CANDIDATE_TRANSPORT
    plan["output_dir"] = str((tmp_path / "other").resolve())
    with pytest.raises(CustodyError, match="plan_digest"):
        authoring.validate_one_shot_plan(plan, operator_capability=secret)


def test_signed_one_shot_plan_allows_only_a_shared_canonical_and_premix_source_root(tmp_path: Path) -> None:
    secret = b"x" * 32
    source_root = str((tmp_path / "convomem").resolve())
    fields = {
        "schema": authoring.PLAN_SCHEMA, "canonical_root": source_root, "premix_root": source_root,
        **{key: str((tmp_path / value).resolve()) for key, value in {
            "candidate_output_dir": "candidate", "custody_output_dir": "custody", "staging_root": "staging",
            "protocol_path": "protocol.json", "authorization_path": "authorization.json", "output_dir": "output",
            "custodian_public_config_path": "custodian-public.json", "final_output_path": "final.json",
            "one_shot_receipt_path": "receipt.json", "infrastructure_failure_receipt_path": "failure.json",
            "progress_receipt_path": "progress.json", "expected_checkpoint_path": "checkpoint.json", "original_root": "original",
            "model_dir": "model", "python_executable": "python", "original_python": "original-python",
        }.items()},
        "custodian_nonce": "n" * 32, "public_authorization_nonce": "u" * 32, "custodian_expires_at_unix": 2_000_000_000,
        "source_manifest": authoring.CENSUS_SOURCE_MANIFEST, "census_semantics": authoring.CENSUS_SEMANTICS,
        "preparse_current_code_receipt": {"head": "a" * 64, "tree": "b" * 64, "diff_digest": "c" * 64, "dirty_policy": "clean_required"},
        "model_receipt": {"encoder_identity": "synthetic", "encoder_semantics": "test", "files": [{"path_role": "weights", "sha256": hashlib.sha256(b"w").hexdigest(), "bytes": 1}]},
    }
    fields["disk_preflight_calibration"] = _disk_calibration(fields["model_receipt"])
    plan = authoring.sign_one_shot_plan(fields, operator_capability=secret)
    assert authoring.validate_one_shot_plan(plan, operator_capability=secret)["canonical_root"] == source_root
    collision = dict(fields); collision["output_dir"] = source_root
    collision = authoring.sign_one_shot_plan(collision, operator_capability=secret)
    with pytest.raises(CustodyError, match="path_collision"):
        authoring.validate_one_shot_plan(collision, operator_capability=secret)


def test_formal_bootstrap_domain_seed_is_stable_and_distinct() -> None:
    from benchmarks import aerp7_convomem_scoring as score
    left = score.formal_bootstrap(hashlib.sha256(b"left").hexdigest())
    assert left == score.formal_bootstrap(hashlib.sha256(b"left").hexdigest())
    assert left["seed"] != score.formal_bootstrap(hashlib.sha256(b"right").hexdigest())["seed"]
    assert left["resamples"] == 10000
    assert left["original_replicate_rule"] == "global_build_multiset_per_draw"


def test_observed_source_manifest_rejects_unregistered_root_and_byte_drift(tmp_path, monkeypatch) -> None:
    canonical = tmp_path / "canonical"; premix = tmp_path / "premix"
    canonical_file = canonical / "core_benchmark" / "evidence_questions" / "a.json"
    premix_file = premix / "core_benchmark" / "pre_mixed_testcases" / "b.json"
    canonical_file.parent.mkdir(parents=True); premix_file.parent.mkdir(parents=True)
    canonical_file.write_bytes(b"canonical"); premix_file.write_bytes(b"premix")
    upstream = {"repository": "SalesforceAIResearch/ConvoMem", "commit": "c" * 40, "tree": "t" * 40}
    protocol_source = {"repository": "SalesforceAIResearch/ConvoMem", "commit": "p" * 40, "tree": "q" * 40}
    rows = [
        {"role": "canonical", "locator": "a.json", "bytes": 9, "sha256": hashlib.sha256(b"canonical").hexdigest()},
        {"role": "premix", "locator": "b.json", "bytes": 6, "sha256": hashlib.sha256(b"premix").hexdigest()},
    ]
    expected = {"upstream": upstream, "file_count": 2, "byte_count": 15, "inventory_sha256": authoring._digest(rows)}
    monkeypatch.setattr(authoring, "CENSUS_SOURCE_MANIFEST", expected)
    monkeypatch.setattr(authoring.rank, "PROTOCOL_SOURCE", protocol_source)
    monkeypatch.setattr(authoring, "git_state", lambda _root: {"git_dirty": False, "git_head": upstream["commit"], "git_tree": upstream["tree"]})
    assert authoring.observe_source_manifest(canonical_root=canonical, premix_root=premix, expected=expected) == expected
    with pytest.raises(CustodyError, match="not_preregistered"):
        authoring.observe_source_manifest(canonical_root=canonical, premix_root=premix, expected={**expected, "byte_count": 14})
    canonical_file.write_bytes(b"changed")
    with pytest.raises(CustodyError, match="inventory_manifest_mismatch"):
        authoring.observe_source_manifest(canonical_root=canonical, premix_root=premix, expected=expected)


def test_census_manifest_uses_the_frozen_dataset_tree_not_ranker_source() -> None:
    assert authoring.CENSUS_SOURCE_MANIFEST["upstream"] == authoring.CENSUS_DATASET_SOURCE
    assert authoring.CENSUS_DATASET_SOURCE["tree"] == "ca6c89ff4c3094b721b7eeae6b0e39da68cde24d"
    assert authoring.CENSUS_SOURCE_MANIFEST["upstream"] != authoring.rank.PROTOCOL_SOURCE


def test_census_manifest_is_pinned_to_confirmation_files_filtering() -> None:
    assert authoring.CENSUS_SOURCE_MANIFEST == {
        "upstream": authoring.CENSUS_DATASET_SOURCE,
        "file_count": 2067,
        "byte_count": 26894940265,
        "inventory_sha256": "5af1c9fab4a267859342abfb03469fe8406e05b7ab04610a548791d3ea92a3ab",
    }


def test_aerp8_binding_keeps_immutable_driver_sources_live_while_sealing_new_orchestration_receipt(tmp_path, monkeypatch) -> None:
    source, python, cfg, expected = (tmp_path / name for name in ("rank.py", "python.exe", "pyvenv.cfg", "checkpoint.json"))
    source.write_bytes(b"ranker"); python.write_bytes(b"python"); cfg.write_bytes(b"cfg"); expected.write_text("{}", encoding="utf-8")
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    policy = {"policy_sha256": "p" * 64}
    driver = {"sources": [{"path": str(source.resolve()), "sha256": digest(source)}], "python": str(python.resolve()), "python_sha256": digest(python), "venv_pyvenv_cfg": str(cfg.resolve()), "venv_pyvenv_cfg_sha256": digest(cfg)}
    external = {"schema": "aerp8-current-checkpoint-v2", "driver_code_receipt": driver, "original_execution_policy": policy, "original_execution_policy_sha256": policy["policy_sha256"], "checkpoint_sha256": ""}
    class Aerp8:
        CURRENT_CHECKPOINT_SCHEMA = "aerp8-current-checkpoint-v2"
        @staticmethod
        def digest(value): return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        @staticmethod
        def _load_canonical(*_args): return external
        @staticmethod
        def _validate_driver_code_receipt(value): return dict(value)
        @staticmethod
        def _validate_original_execution_policy(value): return dict(value)
    external["checkpoint_sha256"] = Aerp8.digest({key: value for key, value in external.items() if key != "checkpoint_sha256"})
    monkeypatch.setattr(checkpoint, "_aerp8", lambda: Aerp8)
    code = {"head": "a" * 40, "tree": "b" * 40, "diff_digest": "c" * 64, "dirty_policy": "clean_required"}
    binding = checkpoint.capture_binding(expected_checkpoint_path=expected.resolve(), current_code_receipt=code)
    assert binding["aerp8_validation_scope"] == checkpoint.IMMUTABLE_SCOPE and binding["current_code_receipt"] == code
    source.write_bytes(b"drift")
    with pytest.raises(CustodyError, match="live_validation"):
        checkpoint.capture_binding(expected_checkpoint_path=expected.resolve(), current_code_receipt=code)
