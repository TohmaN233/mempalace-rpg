"""POSIX-only formal reference transport integration coverage.

The subprocess packets are deliberately injected: exercising a real MiniLM and
the independently pinned upstream product would turn this small protocol test
into a full benchmark run.  Everything after those worker seams is real: the
public coordinator, five-draft coordinator audit, custodian public validation,
authorization/release, streaming custody ingress, scoring, mapping-ledger
publication and scientific report/gate path must all accept only persisted
references.
"""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import sys
import time
from pathlib import Path

import pytest

from benchmarks import aerp7_convomem_authoring as authoring
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks import aerp7_convomem_executor as executor
from benchmarks import aerp7_convomem_formal as formal
from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_custodian_executor as custodian
from benchmarks import aerp7_original_product as original_product
from benchmarks.aerp7_convomem_confirmation import CustodyError


pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="formal durability publication requires a POSIX/ext4 host",
)


def _digest(value):
    return executor._digest(value)


def _candidate_receipt(built, candidate: Path):
    ready_raw = (candidate / "READY.json").read_bytes()
    return {
        "generation_id": built["generation_id"],
        "ready_sha256": hashlib.sha256(ready_raw).hexdigest(),
        "projection_raw_sha256": built["projection_raw_sha256"],
        "projection_canonical_sha256": built["projection_canonical_sha256"],
        "query_count": built["query_count"],
        "candidate_text_count": built["candidate_text_count"],
        "candidate_reference": original_product.candidate_projection_reference(
            bundle_path=candidate,
            generation_id=built["generation_id"],
            projection_raw_sha256=built["projection_raw_sha256"],
            projection_canonical_sha256=built["projection_canonical_sha256"],
            dataset=built["dataset"],
            query_count=built["query_count"],
            candidate_text_count=built["candidate_text_count"],
        ),
    }


def _calibration(model_receipt):
    current = {key: 1 << 20 for key in ("strong_raw", "static_p5_primary", "static_p5_repeat", "six_view_secondary")}
    original = {f"replicate_{number:02d}": 1 << 20 for number in range(1, 6)}
    row = {
        "schema": formal.PRIVATE_DISK_CALIBRATION_SCHEMA,
        "calibration_id": "reference-only-e2e-small-calibration",
        "source_manifest_sha256": authoring._digest(authoring.CENSUS_SOURCE_MANIFEST),
        "model_receipt_sha256": authoring._digest(model_receipt),
        "candidate_resident_bytes": 1 << 20,
        "custody_resident_bytes": 1 << 20,
        "custody_sqlite_store_bytes": 1 << 20,
        "shared_current_store_bytes": 1 << 20,
        "current_ranking_measurement_bytes": current,
        "original_replicate_store_bytes": original,
        "original_candidate_index_peak_bytes": 1 << 20,
        "original_chroma_peak_bytes": 1 << 20,
        "original_chroma_peak_policy": "sequential_one_build_peak_v1",
        "scoring_ephemeral_store_bytes": 1 << 20,
        "scoring_report_bytes": 1 << 20,
        "safety_margin_bytes": 1 << 20,
    }
    row["total_required_additional_bytes"] = sum(
        (
            row["candidate_resident_bytes"], row["custody_resident_bytes"],
            row["custody_sqlite_store_bytes"], row["shared_current_store_bytes"],
            row["original_candidate_index_peak_bytes"], row["original_chroma_peak_bytes"],
            row["scoring_ephemeral_store_bytes"], row["scoring_report_bytes"], row["safety_margin_bytes"],
            *current.values(), *original.values(),
        )
    )
    row["calibration_sha256"] = formal.private_disk_calibration_digest(row)
    return row


def _plan(tmp_path: Path, *, operator_secret: bytes, model_receipt, code_receipt, checkpoint: Path):
    external = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert isinstance(external, dict)
    driver = external.get("driver_code_receipt")
    original = external.get("original_execution_policy")
    assert isinstance(driver, dict) and isinstance(original, dict)
    for value in (
        driver.get("python"), original.get("original_root"), original.get("model_dir"), original.get("original_python"),
    ):
        assert isinstance(value, str) and Path(value).is_absolute()
    names = {
        "canonical_root": "canonical", "premix_root": "premix", "candidate_output_dir": "candidate",
        "custody_output_dir": "custody", "staging_root": "private-staging", "protocol_path": "protocol.json",
        "authorization_path": "authorization.json", "output_dir": "public", "custodian_public_config_path": "custodian-public.json",
        "final_output_path": "final.json", "one_shot_receipt_path": "receipt.json",
        "infrastructure_failure_receipt_path": "failure.json", "progress_receipt_path": "progress.json",
        "expected_checkpoint_path": str(checkpoint), "original_root": original["original_root"],
        "model_dir": original["model_dir"], "python_executable": driver["python"],
        "original_python": original["original_python"],
    }
    row = {
        "schema": authoring.PLAN_SCHEMA,
        **{key: str((tmp_path / value).resolve()) if not Path(value).is_absolute() else str(Path(value).resolve()) for key, value in names.items()},
        "source_manifest": authoring.CENSUS_SOURCE_MANIFEST,
        "census_semantics": authoring.CENSUS_SEMANTICS,
        "model_receipt": model_receipt,
        "preparse_current_code_receipt": code_receipt,
        "disk_preflight_calibration": _calibration(model_receipt),
        "public_authorization_nonce": "p" * 32,
        "custodian_nonce": "c" * 32,
        "custodian_expires_at_unix": int(time.time()) + 600,
    }
    Path(row["staging_root"]).mkdir()
    return authoring.sign_one_shot_plan(row, operator_capability=operator_secret)


def _source_roots(tmp_path: Path):
    """Tiny real census source; it is parsed/published by the production builder."""
    canonical, premix = tmp_path / "canonical", tmp_path / "premix"
    cases = []
    # Every persona/context stratum has all five positive endpoints plus its
    # abstention counterpart.  This is the smallest real census that keeps
    # exact-group, derived-changing/implicit, persona/context, and confidence
    # (positive-vs-abstention) denominators non-empty for the formal scorer.
    for persona in ("a", "b"):
        for group in (
            "user_evidence",
            "assistant_facts_evidence",
            "changing_evidence",
            "preference_evidence",
            "implicit_connection_evidence",
            "abstention_evidence",
        ):
            conversation = f"{persona}-{group}"
            evidence = {
                "personId": persona, "question": f"q-{persona}-{group}", "answer": f"answer-{persona}-{group}",
                "category": group, "conversations": [{"id": conversation}],
                "message_evidences": [{"speaker": "speaker", "text": "answer evidence"}],
            }
            path = canonical / "core_benchmark" / "evidence_questions" / group / "tier" / f"{persona}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"evidence_items": [evidence]}), encoding="utf-8")
            for context_size in (1, 8):
                cases.append({
                    "contextSize": context_size,
                    "evidenceItems": [{key: evidence[key] for key in ("personId", "question", "answer", "category", "conversations")}],
                    # The original public-product query contract is exact
                    # Top-10, so every independently ranked corpus supplies
                    # at least ten candidate message IDs.
                    "conversations": [{"id": conversation, "messages": [
                        {
                            "speaker": "user" if number % 2 == 0 else "assistant",
                            "text": f"candidate {number}",
                        }
                        for number in range(10)
                    ]}],
                })
    premix_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    premix_path.parent.mkdir(parents=True, exist_ok=True)
    premix_path.write_text(json.dumps(cases), encoding="utf-8")
    return canonical, premix


class _Encoder:
    def __init__(self, identity):
        self.identity = identity

    def encode_passages(self, texts):
        return [[float(len(text) + number + 1), 1.0] for number, text in enumerate(texts)]

    def encode_query(self, text):
        return [float(len(text) + 1), 1.0]


def test_formal_reference_only_path_survives_public_to_scored_release_on_posix_ext4(tmp_path, monkeypatch):
    """No post-worker object may contain inline projection, measurements, custody or ledger rows."""
    checkpoint = Path(os.environ.get("AERP7_REFERENCE_E2E_CHECKPOINT", "/root/aerp-linux/checkpoints/convomem-c3d9470-v380.json"))
    assert checkpoint.is_file(), "set AERP7_REFERENCE_E2E_CHECKPOINT to the live AERP-8 checkpoint"
    operator_secret, binding_secret = b"o" * 32, b"b" * 32
    canonical, premix = _source_roots(tmp_path)
    candidate, custody, staging = tmp_path / "candidate", tmp_path / "custody", tmp_path / "source-staging"
    staging.mkdir()
    # Match the isolated source-builder launch contract exactly: SQLite temp
    # state must stay below the owned staging root on Linux/ext4.
    monkeypatch.setenv("SQLITE_TMPDIR", str(staging.resolve()))
    built = confirmation.build_prelabel_bundle(
        canonical_root=canonical, premix_root=premix, candidate_output_dir=candidate,
        custody_output_dir=custody, staging_root=staging, secret=binding_secret,
        selection=confirmation.SelectionConfig.census_v1(),
    )
    candidate_receipt = _candidate_receipt(built, candidate)
    code = authoring.clean_code_receipt(Path(__file__).resolve().parents[1])
    files = [{"path_role": "weights", "relative_path": "weights.onnx", "sha256": hashlib.sha256(b"w").hexdigest(), "bytes": 1}]
    tree = [{key: row[key] for key in ("relative_path", "sha256", "bytes")} for row in files]
    model = {"encoder_identity": "reference-only-e2e:" + rank._digest(tree), "encoder_semantics": "deterministic-test-worker-packet", "files": files}
    plan = _plan(tmp_path, operator_secret=operator_secret, model_receipt=model, code_receipt=code, checkpoint=checkpoint)
    custody_ref = confirmation.custody_reference(candidate_bundle=candidate, custody_bundle=custody, candidate_reference=candidate_receipt["candidate_reference"])
    preflight = authoring.author_private_disk_preflight(plan=plan, candidate_reference=candidate_receipt["candidate_reference"], custody_reference=custody_ref, operator_capability=operator_secret)
    assert formal.enforce_private_disk_preflight(preflight=preflight, candidate_bundle_root=candidate, custody_bundle_root=custody, candidate_reference=candidate_receipt["candidate_reference"], staging_root=Path(plan["staging_root"]))["preflight_sha256"] == preflight["preflight_sha256"]
    protocol = authoring.author_formal_protocol(repo_root=Path(__file__).resolve().parents[1], candidate_receipt=candidate_receipt, model_receipt=model, expected_checkpoint_path=checkpoint, preparse_semantics=authoring.CENSUS_SEMANTICS, preparse_current_code_receipt=code)
    protocol_path = tmp_path / "protocol.json"; executor._write_new(protocol_path, protocol)
    output = tmp_path / "public"
    authorization = authoring.sign_operator_authorization(protocol=protocol, output_dir=output, capability=operator_secret, nonce="a" * 32, expires_at_unix=int(time.time()) + 600)
    authorization_path = tmp_path / "authorization.json"; executor._write_new(authorization_path, authorization)
    monkeypatch.setenv("AERP7_OPERATOR_AUTH_CAPABILITY", operator_secret.decode("ascii"))

    # These legacy adapters must be unreachable after the candidate/custody
    # READY files exist.  The injected workers below operate on stream cursors.
    def forbidden(*_args, **_kwargs):
        raise AssertionError("reference-only formal path materialized a legacy payload")

    monkeypatch.setattr(confirmation, "_candidate_snapshot", forbidden)
    monkeypatch.setattr(formal, "load_candidate_worker_projection", forbidden)
    monkeypatch.setattr(executor.formal, "load_candidate_worker_projection", forbidden)
    monkeypatch.setattr(confirmation, "load_sealed_custody", forbidden)
    monkeypatch.setattr(confirmation, "load_custody_for_scoring", forbidden)

    original_fixture = runpy.run_path(str(Path(__file__).with_name("test_aerp7_original_product.py")))
    real_audit = original_product.coordinator_reaudit_streaming_replicate
    monkeypatch.setattr(original_product, "coordinator_reaudit_streaming_replicate", lambda **kwargs: real_audit(**kwargs, auditor=original_fixture["fake_auditor"]))
    artifacts_by_role = {}
    current_packets = {}
    worker_pids = iter(range(30_001, 30_010))

    def current_packet(config, output_path, role, pid):
        arm = {"raw": "strong_raw", "p5_primary": "static_p5", "p5_repeat": "static_p5", "six": "six_view_secondary"}[role]
        if role not in artifacts_by_role:
            with rank.CandidateProjectionStore.open(candidate_receipt["candidate_reference"], output_path.parent, expected_bundle_root=candidate) as store:
                artifact_path = output_path.parent / f"artifact-{role}.json"
                with rank.PersistedMeasurementSink.create(directory=artifact_path.parent, stem=artifact_path.stem, generation_id=store.reference["generation_id"], projection_sha256=store.reference["projection_canonical_sha256"]) as sink:
                    artifacts_by_role[role] = rank.rank_projection_stream(store=store, encoder=_Encoder(model["encoder_identity"]), arm_id=arm, artifact_path=artifact_path, ready_path=artifact_path.with_suffix(".READY.json"), model_receipt=model, code_receipt=code, measurement_sink=sink)
        artifact = artifacts_by_role[role]
        resource = executor._resource(arm_id=arm, artifact_sha256=artifact["artifact_sha256"], denominators={"query_count": candidate_receipt["query_count"], "candidate_text_count": candidate_receipt["candidate_text_count"]}, query_measurements=artifact["measurement_reference"], role={"p5_primary": "primary", "p5_repeat": "repeat"}.get(role), peak_rss_bytes=4096, passage_embedding={"calls": 1, "texts": candidate_receipt["candidate_text_count"]}, query_embedding={"calls": candidate_receipt["query_count"], "texts": candidate_receipt["query_count"]}, measurement_mode="live_native_adapter", resource_comparability="unavailable")
        observed = {"model_file_tree_sha256": rank._digest(tree), "model_file_tree_bytes": 1, "encoder_identity": model["encoder_identity"], "runtime_identity": {"test": "injected-worker-packet"}}
        receipt = {"schema": formal.CURRENT_EXECUTION_RECEIPT_SCHEMA, "execution_mode": "live_native_adapter", "execution_role": role, "arm_id": arm, "protocol_sha256": protocol["protocol_sha256"], "projection_sha256": candidate_receipt["projection_canonical_sha256"], "worker_config_sha256": formal._digest(formal.canonical_candidate_worker_config(protocol)), "method_input_sha256": formal._digest({"arm_id": arm, "method_receipt": artifact["method_receipt"], "serializer_receipt": artifact["serializer_receipt"]}), "observed_code_before": code, "observed_code_after": code, "observed_model_before": observed, "observed_model_after": observed, "provider": {"model": "minilm", "device": "cpu", "providers": ["CPUExecutionProvider"], "model_file_tree_sha256": rank._digest(tree)}, "encoder_identity": model["encoder_identity"], "artifact_file_sha256": artifact["payload_sha256"], "artifact_sha256": artifact["artifact_sha256"], "resource_sha256": resource["resource_sha256"], "process_id": pid, "supervisor_sha256": "0" * 64, "current_interpreter": {"python": protocol["execution_checkpoint"]["driver_code_receipt"]["python"], "sha256": protocol["execution_checkpoint"]["driver_code_receipt"]["python_sha256"]}, "execution_sha256": ""}
        receipt["execution_sha256"] = _digest({key: value for key, value in receipt.items() if key != "execution_sha256"})
        packet = {"schema": executor.FORMAL_CURRENT_PACKET_SCHEMA, "execution_role": role, "artifact": artifact, "resource_receipt": resource, "execution_receipt": receipt, "process_id": pid, "packet_sha256": ""}
        packet["packet_sha256"] = _digest({key: value for key, value in packet.items() if key != "packet_sha256"})
        output_path.write_bytes(executor._bytes(packet))
        current_packets[role] = packet

    def original_packet(config, output_path, pid):
        number = int(str(config["build_id"]).rsplit("-", 1)[1])
        palace = Path(config["palace_path"]); seams, _unused_palace, _state = original_fixture["seams"]()
        draft = original_product.run_original_public_replicate_streaming(candidate_reference=candidate_receipt["candidate_reference"], build_id=str(config["build_id"]), collection_identity=f"reference-only-{number}", palace_path=palace, observer=original_fixture["Observer"](), seams=seams, staging_parent=Path(config["replicate_staging_parent"]))
        draft_bytes = original_product.serialize_worker_draft(draft); draft_path = Path(config["draft_path"]); draft_path.write_bytes(draft_bytes)
        replicate = draft.replicate_without_coordinator_audit.as_reference()
        resource = executor._formal_original_resource(draft=draft, replicate=replicate, denominators={"query_count": candidate_receipt["query_count"], "candidate_text_count": candidate_receipt["candidate_text_count"]}, resource_comparability="unavailable")
        packet = {"schema": executor.FORMAL_ORIGINAL_PACKET_SCHEMA, "execution_mode": "exact_public_product_worker_draft", "draft_file_sha256": hashlib.sha256(draft_bytes).hexdigest(), "palace_path": str(palace.resolve()), "resource_receipt": resource, "worker_execution_identity": {"original_python": protocol["execution_checkpoint"]["original_execution_policy"]["original_python"], "original_execution_policy_sha256": protocol["execution_checkpoint"]["original_execution_policy_sha256"]}, "process_id": pid, "packet_sha256": ""}
        packet["packet_sha256"] = _digest({key: value for key, value in packet.items() if key != "packet_sha256"})
        output_path.write_bytes(executor._bytes(packet))

    def inject_worker(_command, *, config, output, **_kwargs):
        pid = next(worker_pids)
        if config.get("execution_role"):
            current_packet(config, output, config["execution_role"], pid)
        else:
            original_packet(config, output, pid)
        return {"pid": pid, "exit_code": 0, "command_sha256": _digest(["injected"]), "environment_keys_sha256": _digest([]), "cwd_sha256": _digest("injected"), "observed_process_tree_peak_rss_bytes": 8192, "descendant_process_count": 0, "descendant_processes_observed": False, "descendant_observation_method": "os_enforced_complete_process_group", "supervisor_observation_samples": 2, "supervisor_observation_complete": True, "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "packet_sha256": executor._load(output)["packet_sha256"]}

    monkeypatch.setattr(executor, "_run_subprocess", inject_worker)
    config = {"schema": executor.FORMAL_SCHEMA, "synthetic_test_mode": False, "protocol_path": str(protocol_path.resolve()), "candidate_bundle": str(candidate.resolve()), "output_dir": str(output.resolve()), "authorization_path": str(authorization_path.resolve()), "python_executable": protocol["execution_checkpoint"]["driver_code_receipt"]["python"], "original_root": protocol["execution_checkpoint"]["original_execution_policy"]["original_root"], "model_dir": protocol["execution_checkpoint"]["original_execution_policy"]["model_dir"], "original_python": protocol["execution_checkpoint"]["original_execution_policy"]["original_python"]}
    freeze = executor.public_coordinator(config)
    assert freeze["candidate_store_cleanup"]["validated_artifact_count"] == 4
    assert set(current_packets) == {"raw", "p5_primary", "p5_repeat", "six"}
    assert len({packet["artifact"]["artifact_path"] for packet in current_packets.values()}) == 4
    assert len({packet["artifact"]["measurement_reference"]["measurement_path"] for packet in current_packets.values()}) == 4
    assert current_packets["p5_primary"]["artifact"]["artifact_sha256"] == current_packets["p5_repeat"]["artifact"]["artifact_sha256"]
    assert len([row for row in freeze["ranking_artifacts"] if row["arm_id"] != "original_public_product"]) == 3
    assert next(row for row in freeze["ranking_artifacts"] if row["arm_id"] == "original_public_product")["replicate_count"] == 5
    assert all("query_measurements" not in json.dumps(row, sort_keys=True) for row in freeze["ranking_artifacts"])

    # Continue through the actual public validator and separate custody scorer.
    freeze_path = output / "public-freeze.json"
    public_config = {"schema": custodian.FORMAL_PUBLIC_CONFIG_SCHEMA, "synthetic_test_mode": False, "public_freeze_packet": str(freeze_path.resolve()), "candidate_bundle": str(candidate.resolve()), "custody_bundle": str(custody.resolve()), "output_path": str((tmp_path / "final.json").resolve()), "freeze_packet_file_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(), "candidate_ready_sha256": candidate_receipt["ready_sha256"], "custody_ready_sha256": custody_ref["ready_sha256"], "custody_bundle_sha256": custody_ref["custody_raw_sha256"]}
    public = custodian.validate_public_freeze(public_config)
    assert public["candidate_reference"] == candidate_receipt["candidate_reference"]
    private = custodian.sign_private_payload({
        "schema": custodian.FORMAL_PRIVATE_SCHEMA,
        "binding_secret": binding_secret,
        "custody_capability_secret": b"k" * 32,
        "evidence_token_secret": b"e" * 32,
        "scorer_attestation_secret": b"s" * 32,
        "public_packet_sha256": freeze["packet_sha256"],
        "freeze_packet_file_sha256": public_config["freeze_packet_file_sha256"],
        "output_path": public_config["output_path"],
        "nonce": "r" * 32,
        "expires_at_unix": int(time.time()) + 600,
    }, custody_capability_secret=b"k" * 32)
    completed = custodian.execute_custodian(public_config, private)
    final = json.loads(Path(public_config["output_path"]).read_text(encoding="utf-8"))
    assert completed["packet_sha256"] == final["packet_sha256"]
    assert final["envelope"]["report"]["mapping_ledger"]["schema"].endswith("reference-v1")
    assert final["gate_decision"]["outcome"] in {"PASS", "FAIL"}

    def no_inline_payloads(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "query_measurements":
                    assert isinstance(child, dict)
                    assert child["schema"] in {
                        rank.MEASUREMENT_REFERENCE_SCHEMA,
                        "aerp7-original-product-resource-measurement-reference-v1",
                    }
                if key in {"projection", "sealed_custody", "ledger_rows"}:
                    raise AssertionError(f"public output embedded forbidden {key}")
                no_inline_payloads(child)
        elif isinstance(value, list):
            for child in value:
                no_inline_payloads(child)

    no_inline_payloads(final)
