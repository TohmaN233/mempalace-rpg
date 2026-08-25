"""Synthetic subprocess tests for the AERP-7 custodian boundary.

These fixtures intentionally manufacture a tiny label source only through the
same confirmation bundle publisher used by the sealed-data path.  They never
discover, enumerate, digest, or open the official ConvoMem source tree.
"""
from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks import aerp7_custodian_executor as custodian
from benchmarks import aerp7_convomem_executor as executor
from benchmarks import aerp7_convomem_scoring as score
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256


def test_capacity_scoring_observation_accepts_only_instrumented_scalar_fields(tmp_path):
    assert custodian._capacity_scoring_observation({
        "schema": executor.CAPACITY_EVENT_SCHEMA, "kind": "scoring",
        "scoring_db_peak_bytes": 7, "report_bytes": 11, "mapping_ledger_bytes": 13,
    }) == {"scoring_db_peak_bytes": 7, "report_bytes": 11, "mapping_ledger_bytes": 13}
    with pytest.raises(CustodyError, match="capacity_scoring_sidecar_invalid"):
        custodian._capacity_scoring_observation({
            "schema": executor.CAPACITY_EVENT_SCHEMA, "kind": "scoring",
            "scoring_db_peak_bytes": 0, "report_bytes": 11, "mapping_ledger_bytes": 13,
        })


def test_capacity_scoring_sidecar_rejects_wrong_role_or_binding(tmp_path):
    binding = {
        "plan_sha256": "a" * 64, "generation_id": "generation", "projection_sha256": "b" * 64,
        "public_freeze_packet_sha256": "c" * 64, "authorization_id": "authorization",
        "report_sha256": "d" * 64, "final_packet_sha256": "e" * 64,
    }
    row = {
        "schema": custodian.CAPACITY_SCORING_SIDECAR_SCHEMA, "kind": "custodian_scoring",
        "binding": binding,
        "components": {"custody_ephemeral_sqlite_bytes": 1, "candidate_store_bytes": 2, "mapping_ledger_ready_bytes": 3, "final_packet_bytes": 4, "report_bytes": 5},
        "scoring_db_peak_bytes": 6, "report_bytes": 5, "mapping_ledger_bytes": 3,
    }
    row["sidecar_sha256"] = custodian._digest(row)
    sidecar = tmp_path / "capacity.json"; sidecar.write_text(json.dumps(row), encoding="utf-8")
    assert custodian.load_capacity_scoring_sidecar(sidecar)["binding"] == binding
    row["kind"] = "scoring"; sidecar.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(CustodyError, match="capacity_scoring_sidecar_invalid"):
        custodian.load_capacity_scoring_sidecar(sidecar)
    row["kind"] = "custodian_scoring"
    row["binding"] = {**binding, "public_packet_sha256": binding["public_freeze_packet_sha256"]}
    row["binding"].pop("public_freeze_packet_sha256")
    row["sidecar_sha256"] = custodian._digest({key: value for key, value in row.items() if key != "sidecar_sha256"})
    sidecar.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(CustodyError, match="capacity_scoring_sidecar_invalid"):
        custodian.load_capacity_scoring_sidecar(sidecar)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _candidate_receipt(candidate: Path, projection):
    ready_raw = (candidate / "READY.json").read_bytes()
    projection_raw = (candidate / "projection.json").read_bytes()
    ready = json.loads(ready_raw)
    return {
        "generation_id": ready["generation_id"],
        "ready_sha256": hashlib.sha256(ready_raw).hexdigest(),
        "projection_raw_sha256": hashlib.sha256(projection_raw).hexdigest(),
        "projection_canonical_sha256": canonical_sha256(projection),
        "query_count": len(projection["items"]),
        "candidate_text_count": sum(len(corpus["candidates"]) for corpus in projection["corpora"]),
        "candidate_reference": {
            "schema": confirmation.CANDIDATE_PROJECTION_REFERENCE_SCHEMA,
            "bundle_path": str(candidate.resolve()), "projection_path": "projection.json",
            "ready_path": "READY.json", "generation_id": ready["generation_id"],
            "projection_raw_sha256": hashlib.sha256(projection_raw).hexdigest(),
            "projection_canonical_sha256": canonical_sha256(projection),
            "dataset": dict(projection["dataset"]), "query_count": len(projection["items"]),
            "candidate_text_count": sum(len(corpus["candidates"]) for corpus in projection["corpora"]),
        },
    }


def _published_bundle(tmp_path: Path, monkeypatch):
    """A real prelabel publish with synthetic labels and known upstream groups."""
    canonical = tmp_path / "labels"; premix = tmp_path / "premix"
    candidate = tmp_path / "candidate"; custody = tmp_path / "custody"; staging = tmp_path / "staging"
    cases = []
    for persona in ("p-a", "p-b"):
        for group in score.UPSTREAM_GROUPS:
            conversation = f"{persona}-{group}"
            evidence = {
                "personId": persona, "question": f"q-{persona}-{group}",
                "answer": f"SYNTHETIC-ANSWER-{persona}-{group}", "category": group,
                "conversations": [{"id": conversation}],
                "message_evidences": [],
            }
            if group != "abstention_evidence":
                evidence["message_evidences"] = [{"speaker": "synthetic-secret-speaker", "text": "synthetic-secret-evidence"}]
            source = canonical / "core_benchmark" / "evidence_questions" / group / "tier-1" / f"{persona}.json"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(json.dumps({"evidence_items": [evidence]}), encoding="utf-8")
            for size in (1, 8, 13):
                cases.append({
                    "contextSize": size,
                    "evidenceItems": [{key: evidence[key] for key in ("personId", "question", "answer", "category", "conversations")}],
                    "conversations": [{"id": conversation, "messages": [
                        {"speaker": "speaker", "text": f"candidate-a-{conversation}"},
                        {"speaker": "speaker", "text": f"candidate-b-{conversation}"},
                    ]}],
                })
    premix_file = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    premix_file.parent.mkdir(parents=True, exist_ok=True); premix_file.write_text(json.dumps(cases), encoding="utf-8")
    monkeypatch.setattr(confirmation, "_verify_sqlite_temp_environment", lambda root: {"os_rule": "synthetic", "resolved_temp_path": str(root)})
    staging.mkdir()
    binding_secret = b"aerp7-custodian-synthetic-binding-secret"
    confirmation.build_prelabel_bundle(
        canonical_root=canonical, premix_root=premix, candidate_output_dir=candidate,
        custody_output_dir=custody, staging_root=staging, secret=binding_secret,
        selection=confirmation.SelectionConfig(seed=7, persona_quota=1, per_persona_group_quota=1, context_rank_indices=(0, 2)),
    )
    return candidate, custody, binding_secret


def _public_run(tmp_path: Path, monkeypatch):
    fixture = runpy.run_path("tests/test_aerp7_convomem_formal.py")
    candidate, custody, binding_secret = _published_bundle(tmp_path, monkeypatch)
    projection = confirmation.load_candidate_projection(candidate)
    protocol = fixture["protocol"](projection, _candidate_receipt(candidate, projection))
    protocol_path = tmp_path / "protocol.json"; protocol_path.write_bytes(executor._bytes(protocol))
    output = tmp_path / "public-output"; operator_secret = "o" * 40
    authorization = fixture.get("_authorization")
    if authorization is None:
        executor_tests = runpy.run_path("tests/test_aerp7_convomem_executor.py")
        authorization = executor_tests["_authorization"]
    auth = authorization(protocol, output, executor.live_executor_code_receipt(), operator_secret.encode())
    auth_path = tmp_path / "operator-authorization.json"; auth_path.write_bytes(executor._bytes(auth))
    monkeypatch.setenv("AERP7_OPERATOR_AUTH_CAPABILITY", operator_secret)
    freeze = executor.public_coordinator({
        "schema": executor.SCHEMA, "synthetic_test_mode": True,
        "protocol_path": str(protocol_path), "candidate_bundle": str(candidate), "output_dir": str(output),
        "authorization_path": str(auth_path), "python_executable": sys.executable,
    })
    freeze_path = output / "public-freeze.json"
    public_config = {
        "schema": custodian.PUBLIC_CONFIG_SCHEMA, "synthetic_test_mode": True,
        "public_freeze_packet": str(freeze_path), "candidate_bundle": str(candidate), "custody_bundle": str(custody),
        "output_path": str(tmp_path / "custodian-packet.json"),
        "freeze_packet_file_sha256": hashlib.sha256(freeze_path.read_bytes()).hexdigest(),
        "candidate_ready_sha256": _candidate_receipt(candidate, projection)["ready_sha256"],
        "custody_ready_sha256": hashlib.sha256((custody / "READY.json").read_bytes()).hexdigest(),
        "custody_bundle_sha256": hashlib.sha256((custody / "sealed-custody.json").read_bytes()).hexdigest(),
    }
    config_path = tmp_path / "custodian-public.json"; config_path.write_bytes(custodian._bytes(public_config))
    capability = "c" * 40
    private = {
        "schema": custodian.PRIVATE_SCHEMA, "binding_secret": binding_secret.decode(),
        "custody_capability_secret": capability, "evidence_token_secret": "e" * 40,
        "scorer_attestation_secret": "s" * 40,
        "public_packet_sha256": freeze["packet_sha256"],
        "freeze_packet_file_sha256": public_config["freeze_packet_file_sha256"],
        "output_path": str(Path(public_config["output_path"]).resolve()),
        "nonce": "n" * 32, "expires_at_unix": int(time.time()) + 300,
    }
    private = custodian.sign_private_payload(private, custody_capability_secret=capability.encode())
    return public_config, config_path, private, freeze


def test_public_packet_failure_precedes_custody_open_and_leaves_no_output(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    opened = []
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: opened.append(True))
    config = dict(config); config["freeze_packet_file_sha256"] = "0" * 64
    with pytest.raises(CustodyError, match="freeze_packet_file_digest"):
        custodian.execute_custodian(config, private)
    assert opened == []
    assert not Path(config["output_path"]).exists()


def test_rehearsal_authorization_cannot_validate_as_a_formal_release(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    public = custodian.validate_public_freeze(config)
    rehearsal = custodian._release(
        public=public, custody_ready_sha256=config["custody_ready_sha256"],
        custody_bundle_sha256=config["custody_bundle_sha256"], capability=private["custody_capability_secret"].encode(),
        formal_live=False,
    )
    assert rehearsal["schema"] == custodian.formal.REHEARSAL_RELEASE_SCHEMA
    assert custodian.formal.validate_rehearsal_release_authorization(
        rehearsal, projection=public["projection"], ranking_artifacts=public["ranking_artifacts"],
        current_worker_receipt=public["current_worker_receipt"], protocol=public["protocol"],
        endpoint_manifest=public["endpoint_manifest"], resource_receipts=public["resource_receipts"],
        custody_ready_sha256=config["custody_ready_sha256"], custody_bundle_sha256=config["custody_bundle_sha256"],
        custody_capability_secret=private["custody_capability_secret"].encode(),
    )["schema"] == custodian.formal.REHEARSAL_RELEASE_SCHEMA
    with pytest.raises(CustodyError, match="release_authorization_invalid"):
        custodian.formal.validate_release_authorization(
            rehearsal, projection=public["projection"], ranking_artifacts=public["ranking_artifacts"],
            current_worker_receipt=public["current_worker_receipt"], protocol=public["protocol"],
            endpoint_manifest=public["endpoint_manifest"], resource_receipts=public["resource_receipts"],
            custody_ready_sha256=config["custody_ready_sha256"], custody_bundle_sha256=config["custody_bundle_sha256"],
            custody_capability_secret=private["custody_capability_secret"].encode(),
        )


def test_resealed_supervisor_tamper_is_rejected_by_public_freeze(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    freeze_path = Path(config["public_freeze_packet"]); packet = json.loads(freeze_path.read_text(encoding="utf-8"))
    execution = packet["current_worker_receipt"]["execution_receipts"]
    execution[0]["supervisor_sha256"] = _h("forged-supervisor")
    execution[0]["execution_sha256"] = custodian.formal._digest({key: value for key, value in execution[0].items() if key != "execution_sha256"})
    packet["current_worker_receipt"]["worker_sha256"] = custodian.formal._digest({key: value for key, value in packet["current_worker_receipt"].items() if key != "worker_sha256"})
    packet["packet_sha256"] = executor._digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    freeze_path.write_bytes(executor._bytes(packet)); config = dict(config)
    config["freeze_packet_file_sha256"] = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
    private = dict(private); private["public_packet_sha256"] = packet["packet_sha256"]; private["freeze_packet_file_sha256"] = config["freeze_packet_file_sha256"]
    private = custodian.sign_private_payload(private, custody_capability_secret=private["custody_capability_secret"].encode())
    with pytest.raises(CustodyError, match="supervisor_binding_invalid"):
        custodian.validate_public_freeze(config)


def test_inconsistent_descendant_observation_is_rejected_before_custody(tmp_path, monkeypatch):
    config, _config_path, _private, _freeze = _public_run(tmp_path, monkeypatch)
    freeze_path = Path(config["public_freeze_packet"])
    packet = json.loads(freeze_path.read_text(encoding="utf-8"))
    supervisor = packet["supervisors"]["original-0"]
    supervisor["descendant_process_count"] = 0
    supervisor["descendant_processes_observed"] = True
    packet["packet_sha256"] = executor._digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    freeze_path.write_bytes(executor._bytes(packet))
    config = dict(config)
    config["freeze_packet_file_sha256"] = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
    with pytest.raises(CustodyError, match="custodian_supervisor_receipt_invalid"):
        custodian.validate_public_freeze(config)


def test_real_synthetic_bundle_scores_in_a_distinct_subprocess_and_is_idempotent(tmp_path, monkeypatch):
    config, config_path, private, freeze = _public_run(tmp_path, monkeypatch)
    # This is a real builder -> custody-open -> release/scoring integration,
    # not a mocked one-shot transport test.  The one formal binding secret is
    # used for both the source commitments and privileged custody verification.
    sealed = confirmation.load_sealed_custody(
        Path(config["candidate_bundle"]),
        Path(config["custody_bundle"]),
        binding_secret=private["binding_secret"].encode("utf-8"),
    )
    assert sealed["projection_sha256"] == confirmation.canonical_sha256(confirmation.load_candidate_projection(Path(config["candidate_bundle"])))
    launched = custodian.launch_custodian(public_config_path=config_path, private_payload=private)
    assert launched["exit_code"] == 0, launched["stderr"].decode("utf-8", "replace")
    output = Path(config["output_path"])
    packet = json.loads(output.read_text(encoding="utf-8"))
    assert packet["formal_eligible"] is False
    assert packet["public_freeze_packet_sha256"] == freeze["packet_sha256"]
    assert packet["gate_decision"]["outcome"] in {"PASS", "FAIL"}
    assert launched["pid"] not in {item["pid"] for item in freeze["supervisors"].values()}
    assert launched["packet_sha256"] == packet["packet_sha256"]
    assert launched["output_file_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    initial = output.read_bytes()
    retry = custodian.launch_custodian(public_config_path=config_path, private_payload=private)
    assert retry["exit_code"] == 0, retry["stderr"].decode("utf-8", "replace")
    assert output.read_bytes() == initial
    assert not list(output.parent.glob(".custodian-packet.json.tmp-*"))


def test_observerless_custodian_launcher_does_not_construct_capacity_supervisor(tmp_path, monkeypatch):
    config, config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    monkeypatch.setattr(
        custodian.executor, "_SupervisorTreeObserver",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("capacity supervisor")),
    )
    launched = custodian.launch_custodian(public_config_path=config_path, private_payload=private)
    assert launched["exit_code"] == 0
    assert "supervisor_receipt" not in launched


def test_consumed_marker_requires_capability_hmac_and_rejects_tampering(tmp_path, monkeypatch):
    config, _config_path, private_payload, _freeze = _public_run(tmp_path, monkeypatch)
    result = custodian.execute_custodian(config, private_payload)
    public = custodian.validate_public_freeze(config)
    output = Path(config["output_path"])
    parsed = custodian._private(
        private_payload,
        packet_sha256=public["packet"]["packet_sha256"],
        file_sha256=config["freeze_packet_file_sha256"],
        output_path=output,
        formal_live=False,
        require_unexpired=False,
    )
    context = custodian._consumed_marker_context(public=public, private=parsed, output=output)
    _lock, marker_path = custodian._authorization_paths(output, parsed["authorization_id"])
    original = marker_path.read_bytes()
    assert custodian._validated_consumed_marker(
        consumed=marker_path,
        context=context,
        custody_capability_secret=parsed["custody_capability_secret"],
    )["packet_sha256"] == result["packet_sha256"]
    forged = json.loads(original.decode("utf-8")); forged["marker_hmac"] = "0" * 64
    marker_path.write_bytes(custodian._bytes(forged))
    with pytest.raises(CustodyError, match="authorization_consumed_invalid"):
        custodian._validated_consumed_marker(
            consumed=marker_path,
            context=context,
            custody_capability_secret=parsed["custody_capability_secret"],
        )
    marker_path.write_bytes(original)
    with pytest.raises(CustodyError, match="authorization_consumed_invalid"):
        custodian._validated_consumed_marker(
            consumed=marker_path,
            context=context,
            custody_capability_secret=b"wrong-custody-capability-secret-value",
        )


def test_same_authorization_replay_does_not_snapshot_or_open_custody(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    first = custodian.execute_custodian(config, private)
    assert first["published"] is True
    original_snapshot = confirmation._snapshot
    custody_root = Path(config["custody_bundle"]).resolve(); custody_reads = []
    def spy(path, *args, **kwargs):
        if Path(path).resolve().parent == custody_root:
            custody_reads.append(Path(path))
        return original_snapshot(path, *args, **kwargs)
    monkeypatch.setattr(confirmation, "_snapshot", spy)
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("replay must not open custody")))
    replay = custodian.execute_custodian(config, private)
    assert replay["retry_idempotent"] is True
    assert replay["packet_sha256"] == first["packet_sha256"]
    assert custody_reads == []


@pytest.mark.parametrize("field", ("envelope", "custodian_post_score_attestation"))
def test_forged_existing_final_packet_is_not_a_retry_success(tmp_path, monkeypatch, field):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    custodian.execute_custodian(config, private)
    output = Path(config["output_path"])
    packet = json.loads(output.read_text(encoding="utf-8"))
    if field == "envelope":
        packet[field]["release_authorization"]["release_hmac"] = "0" * 64
    else:
        packet[field]["attestation_hmac"] = "0" * 64
    packet["packet_sha256"] = custodian._digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    output.write_bytes(custodian._bytes(packet))
    with pytest.raises(CustodyError, match="completed_packet_(envelope|attestation)_invalid"):
        custodian.execute_custodian(config, private)


def test_consumed_authorization_with_deleted_result_never_reopens_custody(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    custodian.execute_custodian(config, private)
    Path(config["output_path"]).unlink()
    real_snapshot = confirmation._snapshot; custody_root = Path(config["custody_bundle"]).resolve()
    def reject_custody_snapshot(path, *args, **kwargs):
        if Path(path).resolve().parent == custody_root:
            raise AssertionError("consumed replay must not snapshot custody")
        return real_snapshot(path, *args, **kwargs)
    monkeypatch.setattr(confirmation, "_snapshot", reject_custody_snapshot)
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("consumed replay must not open custody")))
    with pytest.raises(CustodyError, match="consumed_result_missing"):
        custodian.execute_custodian(config, private)


def test_completed_packet_replay_remains_verifiable_after_execution_expiry(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    first = custodian.execute_custodian(config, private)
    # Preserve the original signed identity; only wall time advances past its
    # execution lease.  Re-signing a changed expiry would be a new capability.
    monkeypatch.setattr(custodian.time, "time", lambda: private["expires_at_unix"] + 1)
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("completed retry must not reopen custody")))
    replay = custodian.execute_custodian(config, private)
    assert replay["retry_idempotent"] is True and replay["packet_sha256"] == first["packet_sha256"]


def test_publication_before_consumed_marker_crash_is_healed_without_rescore(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    real_consume = custodian._consume_authorization
    monkeypatch.setattr(custodian, "_consume_authorization", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("marker crash")))
    with pytest.raises(RuntimeError, match="marker crash"):
        custodian.execute_custodian(config, private)
    assert Path(config["output_path"]).is_file()
    monkeypatch.setattr(custodian, "_consume_authorization", real_consume)
    real_snapshot = confirmation._snapshot; custody_root = Path(config["custody_bundle"]).resolve()
    def reject_custody_snapshot(path, *args, **kwargs):
        if Path(path).resolve().parent == custody_root:
            raise AssertionError("healing retry must not snapshot custody")
        return real_snapshot(path, *args, **kwargs)
    monkeypatch.setattr(confirmation, "_snapshot", reject_custody_snapshot)
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("healing retry must not open custody")))
    replay = custodian.execute_custodian(config, private)
    assert replay["retry_idempotent"] is True


def test_old_execution_cannot_remove_replacement_authorization_lock(tmp_path):
    output = tmp_path / "result.json"; authorization_id = "a" * 64
    lock, _consumed, payload, identity = custodian._acquire_authorization_lock(
        output=output, authorization_id=authorization_id,
    )
    replacement = tmp_path / "replacement-lock.json"; replacement.write_bytes(payload)
    replacement_identity = (os.lstat(replacement).st_dev, os.lstat(replacement).st_ino)
    assert replacement_identity != identity
    custodian._remove_owned_lock(lock, payload, identity)
    replacement.rename(lock)
    with pytest.raises(CustodyError, match="lock_identity_drift"):
        custodian._remove_owned_lock(lock, payload, identity)
    assert lock.is_file() and lock.read_bytes() == payload


def test_expired_authorization_is_rejected_before_custody_or_marker(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    private = dict(private); private["expires_at_unix"] = int(time.time()) - 1
    private = custodian.sign_private_payload(private, custody_capability_secret=private["custody_capability_secret"].encode())
    opened = []
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: opened.append(True))
    with pytest.raises(CustodyError, match="capability_expired"):
        custodian.execute_custodian(config, private)
    output = Path(config["output_path"])
    assert opened == [] and not output.exists()
    assert not list(output.parent.glob(".custodian-packet.json.aerp7-*-*.json"))


def test_wrong_private_hmac_never_publishes_and_subprocess_output_has_no_secret(tmp_path, monkeypatch):
    config, config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    private = dict(private); private["authorization_hmac"] = "0" * 64
    opened = []
    monkeypatch.setattr(custodian.formal, "open_custody_after_rehearsal_release", lambda **_kwargs: opened.append(True))
    with pytest.raises(CustodyError, match="private_capability_hmac"):
        custodian.execute_custodian(config, private)
    assert opened == []
    with pytest.raises(subprocess.CalledProcessError) as failed:
        custodian.launch_custodian(public_config_path=config_path, private_payload=private)
    assert not Path(config["output_path"]).exists()
    assert not list(Path(config["output_path"]).parent.glob(".custodian-packet.json.tmp-*"))
    combined = failed.value.output + failed.value.stderr
    for value in (private["binding_secret"], private["custody_capability_secret"], private["evidence_token_secret"], private["scorer_attestation_secret"]):
        assert value.encode() not in combined


def test_tampered_resource_chain_is_rejected_before_any_custody_snapshot(tmp_path, monkeypatch):
    config, config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    freeze_path = Path(config["public_freeze_packet"])
    packet = json.loads(freeze_path.read_text(encoding="utf-8"))
    receipt = next(item for item in packet["resource_receipts"] if item["arm_id"] == "strong_raw")
    receipt["artifact_sha256"] = "0" * 64
    receipt["resource_sha256"] = custodian.formal.resource_digest(receipt)
    packet["packet_sha256"] = executor._digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    freeze_path.write_bytes(executor._bytes(packet))
    config = dict(config)
    config["freeze_packet_file_sha256"] = hashlib.sha256(freeze_path.read_bytes()).hexdigest()
    config_path.write_bytes(custodian._bytes(config))
    private = dict(private)
    private["public_packet_sha256"] = packet["packet_sha256"]
    private["freeze_packet_file_sha256"] = config["freeze_packet_file_sha256"]
    private = custodian.sign_private_payload(private, custody_capability_secret=private["custody_capability_secret"].encode())
    original_snapshot = confirmation._snapshot; custody_snapshots = []
    custody_root = Path(config["custody_bundle"]).resolve()
    def spy(path, *args, **kwargs):
        if Path(path).resolve().parent == custody_root:
            custody_snapshots.append(Path(path))
        return original_snapshot(path, *args, **kwargs)
    monkeypatch.setattr(confirmation, "_snapshot", spy)
    with pytest.raises(CustodyError, match="current_execution_resource_binding_invalid"):
        custodian.execute_custodian(config, private)
    assert custody_snapshots == []
    assert not Path(config["output_path"]).exists()


def test_malformed_private_subprocess_input_fails_without_output(tmp_path, monkeypatch):
    config, config_path, _private, _freeze = _public_run(tmp_path, monkeypatch)
    with pytest.raises(subprocess.CalledProcessError):
        custodian.launch_custodian(public_config_path=config_path, private_payload={})
    output = Path(config["output_path"])
    assert not output.exists()
    assert not list(output.parent.glob(".custodian-packet.json.tmp-*"))


def test_after_score_exception_cannot_publish_a_partial_packet(tmp_path, monkeypatch):
    config, _config_path, private, _freeze = _public_run(tmp_path, monkeypatch)
    monkeypatch.setattr(custodian.score, "score_frozen", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic scorer crash")))
    with pytest.raises(RuntimeError, match="synthetic scorer crash"):
        custodian.execute_custodian(config, private)
    output = Path(config["output_path"])
    assert not output.exists()
    assert not list(output.parent.glob(".custodian-packet.json.tmp-*"))


def test_custodian_formal_and_rehearsal_schemas_are_not_interchangeable():
    rehearsal = {
        "schema": custodian.PUBLIC_CONFIG_SCHEMA, "synthetic_test_mode": True,
        "public_freeze_packet": "freeze.json", "candidate_bundle": "candidate", "custody_bundle": "custody",
        "output_path": "out.json", "freeze_packet_file_sha256": "a" * 64,
        "candidate_ready_sha256": "b" * 64, "custody_ready_sha256": "c" * 64,
        "custody_bundle_sha256": "d" * 64,
    }
    assert custodian._public_config(rehearsal)["synthetic_test_mode"] is True
    formal = dict(rehearsal, schema=custodian.FORMAL_PUBLIC_CONFIG_SCHEMA, synthetic_test_mode=False)
    assert custodian._public_config(formal)["synthetic_test_mode"] is False
    with pytest.raises(CustodyError, match="public_config_invalid"):
        custodian._public_config(dict(formal, schema=custodian.PUBLIC_CONFIG_SCHEMA))
