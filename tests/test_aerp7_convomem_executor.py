import hashlib
import hmac
import os
import time
import runpy
import sys
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from benchmarks import aerp7_convomem_executor as executor
from benchmarks.aerp7_convomem_confirmation import CustodyError


def _digest(value):
    return executor._digest(value)


def _authorization(protocol, output, receipt, capability=b"x" * 32, *, expires_at_unix=None):
    unsigned = {
        "schema": executor.AUTH_SCHEMA,
        "mode": "synthetic_rehearsal",
        "synthetic_test_mode": True,
        "protocol_sha256": protocol["protocol_sha256"],
        "executor_code_receipt": receipt,
        "output_dir": str(output.resolve()),
        "nonce": "n" * 32,
        "expires_at_unix": int(time.time()) + 60 if expires_at_unix is None else expires_at_unix,
        "output_absent": True,
    }
    authorization_sha256 = _digest(unsigned)
    return {**unsigned, "authorization_sha256": authorization_sha256, "operator_hmac": hmac.new(capability, executor._bytes({**unsigned, "authorization_sha256": authorization_sha256}), hashlib.sha256).hexdigest()}


def _coordinator_inputs(tmp_path, monkeypatch):
    """Build a label-free synthetic execution only; never a custody fixture."""
    fixture = runpy.run_path("tests/test_aerp7_convomem_formal.py")
    projection = fixture["projection"]()
    candidate_root, _staging, candidate = fixture["bundle"](tmp_path, projection)
    protocol = fixture["protocol"](projection, candidate)
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_bytes(executor._bytes(protocol))
    output = tmp_path / "public-output"
    secret = "z" * 40
    authorization = _authorization(protocol, output, executor.live_executor_code_receipt(), secret.encode())
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_bytes(executor._bytes(authorization))
    monkeypatch.setenv("AERP7_OPERATOR_AUTH_CAPABILITY", secret)
    return (
        {
            "schema": executor.SCHEMA, "synthetic_test_mode": True,
            "protocol_path": str(protocol_path), "candidate_bundle": str(candidate_root),
            "output_dir": str(output), "authorization_path": str(authorization_path),
            "python_executable": sys.executable,
        },
        output,
    )


def _assert_no_incomplete_generation(tmp_path, output):
    assert not output.exists()
    assert not list(tmp_path.glob(f".{output.name}.aerp7-staging-*"))


def test_public_environment_is_an_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("AERP7_CUSTODY_PATH", "canary-custody")
    monkeypatch.setenv("AERP7_BINDING_SECRET", "canary-binding")
    monkeypatch.setenv("OPENAI_API_KEY", "canary-api")
    env = executor._sanitized_env()
    assert "AERP7_CUSTODY_PATH" not in env
    assert "AERP7_BINDING_SECRET" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["AERP7_EXECUTOR_PUBLIC_ROLE"] == "1"
    with pytest.raises(CustodyError):
        executor.assert_public_command(["python", "custody.json"], env)


def test_authorization_requires_live_hmac_expiry_and_single_consume(tmp_path, monkeypatch):
    receipt = {"head": "a" * 64, "tree": "b" * 64, "diff_digest": "c" * 64, "dirty_policy": "clean_required"}
    protocol = {"protocol_sha256": "d" * 64}; output = tmp_path / "out"; secret = b"x" * 32
    monkeypatch.setattr(executor, "live_executor_code_receipt", lambda: receipt)
    auth = _authorization(protocol, output, receipt, secret)
    checked = executor._authorization(auth, protocol=protocol, output_dir=output, capability=secret)
    marker = executor._consume_authorization(authorization=checked, output_dir=output)
    assert marker.is_file()
    with pytest.raises(CustodyError, match="already_consumed"):
        executor._consume_authorization(authorization=checked, output_dir=output)
    bad = dict(auth); bad["operator_hmac"] = "0" * 64
    with pytest.raises(CustodyError, match="hmac"):
        executor._authorization(bad, protocol=protocol, output_dir=output, capability=secret)
    expired = _authorization(protocol, output, receipt, secret, expires_at_unix=int(time.time()) - 1)
    with pytest.raises(CustodyError, match="expired"):
        executor._authorization(expired, protocol=protocol, output_dir=output, capability=secret)


def test_formal_authorization_uses_a_distinct_live_schema(tmp_path):
    receipt = {"head": "a" * 64, "tree": "b" * 64, "diff_digest": "c" * 64, "dirty_policy": "clean_required"}
    protocol = {"protocol_sha256": "d" * 64, "current_code_receipt": receipt}
    output = tmp_path / "out"; secret = b"x" * 32
    unsigned = {
        "schema": executor.FORMAL_AUTH_SCHEMA, "mode": "formal_live", "synthetic_test_mode": False,
        "protocol_sha256": protocol["protocol_sha256"], "executor_code_receipt": receipt,
        "output_dir": str(output.resolve()), "nonce": "f" * 32,
        "expires_at_unix": int(time.time()) + 60, "output_absent": True,
    }
    digest = _digest(unsigned)
    formal = {**unsigned, "authorization_sha256": digest, "operator_hmac": hmac.new(secret, executor._bytes({**unsigned, "authorization_sha256": digest}), hashlib.sha256).hexdigest()}
    assert executor._authorization(formal, protocol=protocol, output_dir=output, capability=secret)["mode"] == "formal_live"
    rehearsal = dict(formal, schema=executor.AUTH_SCHEMA, mode="synthetic_rehearsal", synthetic_test_mode=True)
    rehearsal["authorization_sha256"] = _digest({key: value for key, value in rehearsal.items() if key not in {"authorization_sha256", "operator_hmac"}})
    rehearsal["operator_hmac"] = hmac.new(secret, executor._bytes({key: value for key, value in rehearsal.items() if key != "operator_hmac"}), hashlib.sha256).hexdigest()
    with pytest.raises(CustodyError, match="code_invalid"):
        executor._authorization(rehearsal, protocol=protocol, output_dir=output, capability=secret)


def test_formal_original_worker_command_is_pinned_to_its_distinct_interpreter(tmp_path):
    original_python = (tmp_path / "original-python.exe").resolve()
    command = [str(original_python), "-m", "benchmarks.aerp7_convomem_executor", "--original-worker-stdin"]
    executor.assert_public_command(command, executor._sanitized_env(), expected_python=original_python)
    with pytest.raises(CustodyError, match="public_command"):
        executor.assert_public_command([sys.executable, *command[1:]], executor._sanitized_env(), expected_python=original_python)


def test_concurrent_same_nonce_has_exactly_one_authorization_consumer(tmp_path):
    authorization = {"nonce": "r" * 32, "authorization_sha256": "a" * 64, "protocol_sha256": "b" * 64}
    output = tmp_path / "out"
    def consume():
        try:
            return executor._consume_authorization(authorization=authorization, output_dir=output)
        except CustodyError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: consume(), range(2)))
    assert sum(result is not None for result in results) == 1


def test_exclusive_publication_does_not_replace_conflicting_output(tmp_path):
    path = tmp_path / "packet.json"
    executor._write_bytes_new(path, b'{"one":1}')
    assert path.read_bytes() == b'{"one":1}'
    assert executor.formal.publish_nonreplace(path, b'{"one":1}', fsync_parent=executor._sync_parent)["retry_idempotent"] is True
    with pytest.raises(CustodyError, match="conflict"):
        executor._write_bytes_new(path, b'{"two":2}')


def test_exact_original_worker_packet_requires_cross_process_draft_and_coordinator_reaudit(tmp_path, monkeypatch):
    draft_path = tmp_path / "draft.json"; draft_bytes = b'{"canonical":"draft"}'; draft_path.write_bytes(draft_bytes)
    palace_path = tmp_path / "palace"; palace_path.mkdir()
    worker_index_sha256 = "b" * 64
    opaque_draft = SimpleNamespace(replicate_without_coordinator_audit={"build_id": "build-0", "index_sha256": worker_index_sha256}); projection = {"label_free": True}
    replicate = {"build_id": "build-0", "index_sha256": "a" * 64}
    resource = {"build_id": "build-0", "index_sha256": worker_index_sha256, "resource_sha256": "worker-digest"}
    calls = []
    monkeypatch.setattr(executor.original_product, "load_worker_draft", lambda payload: opaque_draft if payload == draft_bytes else None)
    monkeypatch.setattr(
        executor.original_product, "coordinator_reaudit_replicate",
        lambda **kwargs: calls.append(kwargs) or replicate,
    )
    packet = {
        "schema": executor.FORMAL_ORIGINAL_PACKET_SCHEMA,
        "execution_mode": "exact_public_product_worker_draft",
        "draft_file_sha256": hashlib.sha256(draft_bytes).hexdigest(),
            "palace_path": str(palace_path.resolve()),
            "resource_receipt": resource,
            "worker_execution_identity": {"original_python": str((tmp_path / "original-python.exe").resolve()), "original_execution_policy_sha256": "c" * 64},
            "process_id": 123,
        "packet_sha256": "",
    }
    packet["packet_sha256"] = _digest({key: value for key, value in packet.items() if key != "packet_sha256"})
    checked_replicate, checked_resource = executor.coordinator_reaudit_original_worker_packet(
        packet=packet, draft_path=draft_path, palace_path=palace_path, projection=projection,
    )
    assert checked_replicate == replicate
    assert checked_resource["index_sha256"] == replicate["index_sha256"]
    assert checked_resource["resource_sha256"] == executor.formal.resource_digest(checked_resource)
    assert calls == [{"draft": opaque_draft, "palace_path": palace_path, "projection": projection}]
    tampered = dict(packet); tampered["draft_file_sha256"] = "0" * 64
    with pytest.raises(CustodyError, match="packet_invalid"):
        executor.coordinator_reaudit_original_worker_packet(
            packet=tampered, draft_path=draft_path, palace_path=palace_path, projection=projection,
        )


def test_synthetic_public_coordinator_launches_nine_isolated_workers(tmp_path, monkeypatch):
    # Reuse the established candidate/protocol fixture without importing any
    # canonical or custody source; this test creates only label-free synthetic
    # projection bytes.
    config, output = _coordinator_inputs(tmp_path, monkeypatch)
    monkeypatch.setenv("AERP7_CUSTODY_CANARY", "never-in-child")
    monkeypatch.setenv("AERP7_BINDING_SECRET_CANARY", "never-in-child")
    packet = executor.public_coordinator(config)
    assert packet["formal_eligible"] is False
    assert set(packet["supervisors"]) == {"current-raw", "current-p5_primary", "current-p5_repeat", "current-six", "original-0", "original-1", "original-2", "original-3", "original-4"}
    assert len({row["pid"] for row in packet["supervisors"].values()}) == 9
    assert all(isinstance(row["descendant_process_count"], int) and row["descendant_process_count"] >= 0 for row in packet["supervisors"].values())
    assert all(row["descendant_processes_observed"] is (row["descendant_process_count"] > 0) for row in packet["supervisors"].values())
    assert all(row["descendant_observation_method"] == "psutil_polling_non_exhaustive" for row in packet["supervisors"].values())
    assert all(row["supervisor_observation_complete"] is True and row["supervisor_observation_samples"] >= 2 for row in packet["supervisors"].values())
    assert packet["current_worker_receipt"]["static_p5_execution_count"] == 2
    assert packet["current_worker_receipt"]["static_p5_primary_sha256"] == packet["current_worker_receipt"]["static_p5_repeat_sha256"]
    execution = packet["current_worker_receipt"]["execution_receipts"]
    assert [row["execution_role"] for row in execution] == ["raw", "p5_primary", "p5_repeat", "six"]
    assert len({row["process_id"] for row in execution}) == 4
    assert execution[1]["artifact_file_sha256"] == execution[2]["artifact_file_sha256"]
    projection = runpy.run_path("tests/test_aerp7_convomem_formal.py")["projection"]()
    current_artifacts = [row for row in packet["ranking_artifacts"] if row["arm_id"] != "original_public_product"]
    assert executor.formal.validate_current_execution_receipts(execution, current_worker_receipt=packet["current_worker_receipt"], protocol=packet["protocol"], projection=projection, resources=packet["resource_receipts"], ranking_artifacts=current_artifacts, supervisors=packet["supervisors"], allow_synthetic=True) == execution
    tampered = [dict(row) for row in execution]; tampered[0]["supervisor_sha256"] = "0" * 64; tampered[0]["execution_sha256"] = executor._digest({key: value for key, value in tampered[0].items() if key != "execution_sha256"})
    with pytest.raises(CustodyError, match="receipt_coverage_invalid"):
        executor.formal.validate_current_execution_receipts(tampered, current_worker_receipt=packet["current_worker_receipt"], protocol=packet["protocol"], projection=projection, resources=packet["resource_receipts"], ranking_artifacts=current_artifacts, supervisors=packet["supervisors"], allow_synthetic=True)
    assert (output / "public-freeze.json").is_file()
    assert all("CUSTODY" not in key and "BINDING" not in key for row in packet["supervisors"].values() for key in [])
    with pytest.raises(CustodyError, match="output_present"):
        executor.public_coordinator(config)


def test_formal_current_worker_uses_live_runner_without_synthetic_fallback(tmp_path, monkeypatch):
    protocol = {"protocol_sha256": "a" * 64, "execution_checkpoint": {"synthetic": "checkpoint"}, "current_code_receipt": {"synthetic": "code"}}
    worker_config = {"worker": "config"}; projection = {"projection": "live"}
    model_dir = tmp_path / "model"; model_dir.mkdir()
    output = tmp_path / "current.json"; calls = []
    monkeypatch.setattr(executor.formal, "validate_formal_protocol", lambda value: protocol)
    monkeypatch.setattr(executor.execution_checkpoint, "require_live_binding", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(executor, "_clean_protocol_code_observation", lambda **_kwargs: protocol["current_code_receipt"])
    monkeypatch.setattr(executor, "_load", lambda path: protocol if path.name == "protocol.json" else worker_config)
    monkeypatch.setattr(executor, "_candidate_projection", lambda *args: projection)
    monkeypatch.setattr(
        executor, "run_live_current_execution",
        lambda **kwargs: calls.append(kwargs) or {
            "artifact": {"artifact_sha256": "b" * 64},
            "resource_receipt": {"resource_sha256": "c" * 64},
            "execution_receipt": {"execution_sha256": "d" * 64},
        },
    )
    config = {
        "schema": executor.FORMAL_SCHEMA, "synthetic_test_mode": False,
        "protocol_path": str(tmp_path / "protocol.json"), "candidate_bundle": str(tmp_path / "candidate"),
        "worker_config": str(tmp_path / "worker.json"), "output_path": str(output),
        "staging_parent": str(tmp_path), "execution_role": "raw", "model_dir": str(model_dir),
    }
    packet = executor.current_worker(config)
    assert packet["schema"] == executor.FORMAL_CURRENT_PACKET_SCHEMA
    assert calls == [{"role": "raw", "protocol": protocol, "projection": projection, "worker_config": worker_config, "model_dir": model_dir}]


def test_formal_public_config_omits_and_rejects_worker_isolation_attestation():
    assert "worker_isolation_attestation" not in executor.FORMAL_PUBLIC_CONFIG_KEYS
    config = {
        "schema": executor.FORMAL_SCHEMA, "synthetic_test_mode": False,
        "protocol_path": "protocol.json", "candidate_bundle": "candidate", "output_dir": "output",
        "authorization_path": "authorization.json", "python_executable": sys.executable,
        "original_root": "original", "model_dir": "model",
        "worker_isolation_attestation": "forbidden",
    }
    with pytest.raises(CustodyError, match="coordinator_config_invalid"):
        executor.public_coordinator(config)


def test_original_resource_requires_external_supervisor_rebind_before_publish():
    measurements = [
        {"item_id": "1" * 64, "query_sha256": "2" * 64, "wall_ns": 11, "cpu_ns": 5},
        {"item_id": "3" * 64, "query_sha256": "4" * 64, "wall_ns": 17, "cpu_ns": 7},
    ]
    resource = executor._resource(
        arm_id="original_public_product",
        artifact_sha256="0" * 64,
        denominators={"query_count": 2, "candidate_text_count": 2},
        query_measurements=measurements,
        build_id="build-0",
        index_sha256="5" * 64,
        peak_rss_bytes=0,
        allow_unfinalized_peak=True,
        passage_embedding={"calls": 1, "texts": 2},
        query_embedding={"calls": 2, "texts": 2},
        measurement_mode="live_original_public_product",
        storage_bytes=13,
    )
    rebound = executor.finalize_original_resource(
        resource=resource,
        supervisor={
            "pid": 321,
            "exit_code": 0,
            "observed_process_tree_peak_rss_bytes": 97,
            "descendant_process_count": 0,
            "descendant_processes_observed": False,
            "descendant_observation_method": "os_enforced_complete_process_group",
            "supervisor_observation_samples": 3,
            "supervisor_observation_complete": True,
        },
        worker_pid=321,
    )
    assert rebound["peak_rss_bytes"] == 97
    assert rebound["resource_sha256"] == executor.formal.resource_digest(rebound)
    with pytest.raises(CustodyError, match="descendant_observation_missing"):
        executor.finalize_original_resource(
            resource=resource,
            supervisor={"observed_process_tree_peak_rss_bytes": 97},
            worker_pid=321,
        )
    with pytest.raises(CustodyError, match="descendant_process"):
        executor.finalize_original_resource(
            resource=resource,
            supervisor={
                "pid": 321,
                "exit_code": 0,
                "observed_process_tree_peak_rss_bytes": 97,
                "descendant_process_count": 1,
                "descendant_processes_observed": True,
                "descendant_observation_method": "os_enforced_complete_process_group",
                "supervisor_observation_samples": 3,
                "supervisor_observation_complete": True,
            },
            worker_pid=321,
        )
    with pytest.raises(CustodyError, match="supervisor_peak_invalid"):
        executor.finalize_original_resource(
            resource=resource,
            supervisor={
                "pid": 321,
                "exit_code": 0,
                "observed_process_tree_peak_rss_bytes": 0,
                "descendant_process_count": 0,
                "descendant_processes_observed": False,
                "descendant_observation_method": "os_enforced_complete_process_group",
                "supervisor_observation_samples": 3,
                "supervisor_observation_complete": True,
            },
            worker_pid=321,
        )


def test_supervisor_observer_records_transient_descendants_and_non_exhaustive_no_child_sample(monkeypatch):
    class NoSuchProcess(Exception):
        pass

    class ZombieProcess(Exception):
        pass

    class Child:
        pid = 222

        def is_running(self):
            return True

        def memory_info(self):
            return SimpleNamespace(rss=3)

    class Root:
        def __init__(self, with_transient_child):
            self.calls = 0
            self.with_transient_child = with_transient_child

        def children(self, recursive=True):
            self.calls += 1
            if self.with_transient_child and self.calls >= 2:
                return [Child()]
            return []

        def is_running(self):
            return True

        def memory_info(self):
            return SimpleNamespace(rss=7)

    def run(with_transient_child):
        root = Root(with_transient_child)
        fake_psutil = SimpleNamespace(
            NoSuchProcess=NoSuchProcess,
            ZombieProcess=ZombieProcess,
            Process=lambda _pid: root,
        )
        monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
        observer = executor._SupervisorTreeObserver(111)
        with observer:
            time.sleep(0.06)
        return observer.receipt()

    transient = run(True)
    assert transient["descendant_process_count"] == 1
    assert transient["descendant_processes_observed"] is True
    assert transient["descendant_observation_method"] == "psutil_polling_non_exhaustive"
    assert transient["supervisor_observation_complete"] is True
    no_child = run(False)
    assert no_child["descendant_process_count"] == 0
    assert no_child["descendant_processes_observed"] is False
    assert no_child["descendant_observation_method"] == "psutil_polling_non_exhaustive"
    assert no_child["supervisor_observation_samples"] >= 2


def test_original_resource_requires_unavailable_comparability_for_polling_observation():
    resource = executor._resource(
        arm_id="original_public_product",
        artifact_sha256="4" * 64,
        denominators={"query_count": 2, "candidate_text_count": 2},
        query_measurements=[
            {"item_id": "item-1", "query_sha256": "1" * 64, "wall_ns": 1, "cpu_ns": 1},
            {"item_id": "item-2", "query_sha256": "2" * 64, "wall_ns": 1, "cpu_ns": 1},
        ],
        build_id="build-1",
        index_sha256="5" * 64,
        peak_rss_bytes=0,
        allow_unfinalized_peak=True,
        passage_embedding={"calls": 1, "texts": 2},
        query_embedding={"calls": 2, "texts": 2},
        measurement_mode="live_original_public_product",
        storage_bytes=13,
    )
    with pytest.raises(CustodyError, match="strict_comparability_unavailable"):
        executor.finalize_original_resource(
            resource=resource,
            supervisor={
                "pid": 321,
                "exit_code": 0,
                "observed_process_tree_peak_rss_bytes": 97,
                "descendant_process_count": 0,
                "descendant_processes_observed": False,
                "descendant_observation_method": "psutil_polling_non_exhaustive",
                "supervisor_observation_samples": 3,
                "supervisor_observation_complete": True,
            },
            worker_pid=321,
        )
    unavailable = dict(resource)
    unavailable["resource_comparability"] = "unavailable"
    unavailable["resource_sha256"] = executor.formal.resource_digest(unavailable)
    assert executor.finalize_original_resource(
        resource=unavailable,
        supervisor={
            "pid": 321, "exit_code": 0, "observed_process_tree_peak_rss_bytes": 97,
            "descendant_process_count": 0, "descendant_processes_observed": False,
            "descendant_observation_method": "psutil_polling_non_exhaustive",
            "supervisor_observation_samples": 3, "supervisor_observation_complete": True,
        }, worker_pid=321,
    )["peak_rss_bytes"] == 97


def test_worker_failure_discards_staging_and_consumes_authorization(tmp_path, monkeypatch):
    config, output = _coordinator_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(executor, "_run_subprocess", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected_worker_failure")))
    with pytest.raises(RuntimeError, match="injected_worker_failure"):
        executor.public_coordinator(config)
    _assert_no_incomplete_generation(tmp_path, output)
    with pytest.raises(CustodyError, match="already_consumed"):
        executor.public_coordinator(config)


def test_validation_failure_discards_staging_before_publication(tmp_path, monkeypatch):
    config, output = _coordinator_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(executor.formal, "freeze_endpoint_manifest", lambda **_kwargs: (_ for _ in ()).throw(CustodyError("injected_validation_failure")))
    with pytest.raises(CustodyError, match="injected_validation_failure"):
        executor.public_coordinator(config)
    _assert_no_incomplete_generation(tmp_path, output)


def test_publish_failure_discards_completed_staging_before_final_name(tmp_path, monkeypatch):
    config, output = _coordinator_inputs(tmp_path, monkeypatch)
    monkeypatch.setattr(executor, "_rename_generation_no_replace", lambda *_args: (_ for _ in ()).throw(CustodyError("injected_publish_failure")))
    with pytest.raises(CustodyError, match="injected_publish_failure"):
        executor.public_coordinator(config)
    _assert_no_incomplete_generation(tmp_path, output)
