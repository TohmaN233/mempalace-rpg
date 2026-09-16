import copy
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from benchmarks import aerp8_membench as m


@pytest.fixture(autouse=True)
def synthetic_model_tree_receipt(monkeypatch):
    """Formal provenance tests use paths only; never materialize a model tree."""
    monkeypatch.setattr(m.original_product.v1, "file_tree_receipt", lambda _path: {"sha256": m.ORIGINAL_MODEL_TREE})


class Encoder:
    identity = "aerp8-synthetic-fixed-encoder"
    def encode_passages(self, texts): return [[float(len(text)), 1.0] for text in texts]
    def encode_query(self, text): return [float(len(text)), 1.0]


def source(*, tid="shared", target_step_id=0, question_type="factual"):
    return {question_type: {"scenario-a": [{"tid": tid, "message_list": ["alpha step", "beta step"], "QA": {"question": "alpha", "time": "t0", "choices": ["a", "b"], "ground_truth": "a", "target_step_id": target_step_id}}]}}


def current_and_original(candidate):
    current = m.run_candidate_arms(candidate=candidate, encoder=Encoder())
    def worker(projection, build): return {"build": build, "rankings": current["strong_raw"]["rankings"]}
    def audit(draft):
        build = draft["build"]
        return {"build_id": build, "index_sha256": hashlib.sha256((build + "-index").encode()).hexdigest(), "draft_sha256": hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest(), "rankings": draft["rankings"]}
    return current, m.run_synthetic_original_five(candidate=candidate, original_runner=worker, coordinator_reaudit=audit)


def public(candidate, current, original, tmp_path):
    ready = {arm: m.publish_ready_payload(output_dir=tmp_path, arm_id=arm, payload=payload) for arm, payload in {**current, "original_public_product": original}.items()}
    return m.freeze_synthetic_public_results(candidate=candidate, current=current, original=original, ready_receipts=ready, model_receipt={"model": "synthetic-test"}, code_receipt={"code": "synthetic-test"})


def threshold_protocol():
    value = {"schema": m.THRESHOLD_PROTOCOL_SCHEMA, "source_roles": sorted(m.FORMAL_SOURCE_ROLES), "seed": 20260823, "resamples": 5000, "overall_delta_min": .01, "overall_ci_lower_min": 0, "hard_delta_min": 0, "hard_ci_lower_min": -.01}
    value["threshold_sha256"] = m.digest(value)
    return value


def test_venv_python_uses_the_host_family_layout(tmp_path):
    assert m._venv_python(tmp_path, os_name="nt") == tmp_path / ".venv" / "Scripts" / "python.exe"
    assert m._venv_python(tmp_path, os_name="posix") == tmp_path / ".venv" / "bin" / "python"
    with pytest.raises(m.MemBenchError, match="driver_worker_python_invalid"):
        m._venv_python(tmp_path, os_name="unsupported")


def test_formal_primary_original_pin_is_official_mempalace_v380() -> None:
    assert (m.ORIGINAL_COMMIT, m.ORIGINAL_TREE) == (
        "87e6f38377b4bee0666374b05df6e14ffd154245",
        "639b2a849816fd4853072920405822824464e9c6",
    )
    assert (m.ORIGINAL_COMMIT, m.ORIGINAL_TREE) != (
        "72ccd2f3653ab902e419d15bb542c88045342b04",
        "5e4ad9cf1d6387cebe16dd03b6da8355d899f70c",
    )


def original_execution_policy():
    root=m.BUNDLED_ORIGINAL_ROOT
    return m.capture_original_execution_policy(
        original_root=root,
        original_python=m._venv_python(root),
        model_dir=root,
        git_capability=m._driver_code_receipt()["git_capability"],
    )


def checkpoint_fixture(checkpoint_sha256="c" * 64):
    code=m._driver_code_receipt(); policy=original_execution_policy()
    return {"checkpoint_sha256":checkpoint_sha256,"driver_code_receipt":code,"original_execution_policy":policy,"original_execution_policy_sha256":policy["policy_sha256"]}


def formal_original(candidate, original):
    artifact = dict(original)
    checkpoint=checkpoint_fixture(); code=checkpoint["driver_code_receipt"]; policy=checkpoint["original_execution_policy"]; driver=Path(m.__file__).resolve(); root=Path(policy["original_root"])
    runtime={"schema":"aerp8-pinned-original-runtime-v2","original_commit":m.ORIGINAL_COMMIT,"original_tree":m.ORIGINAL_TREE,"original_git_state":{"git_dirty":False,"git_head":m.ORIGINAL_COMMIT,"git_tree":m.ORIGINAL_TREE},"original_root":policy["original_root"],"original_python":policy["original_python"],"original_python_sha256":policy["original_python_sha256"],"mempalace_file":policy["mempalace_file"],"mempalace_file_sha256":policy["mempalace_file_sha256"],"driver_file":str(driver),"driver_file_sha256":hashlib.sha256(driver.read_bytes()).hexdigest(),"model_dir":policy["model_dir"],"model_file_tree_sha256":m.ORIGINAL_MODEL_TREE,"git_capability":code["git_capability"],"worker_home_path":str(root),"checkpoint_sha256":checkpoint["checkpoint_sha256"],"code_receipt_sha256":code["code_sha256"],"original_execution_policy_sha256":policy["policy_sha256"]}
    runtime["runtime_sha256"]=m.digest(runtime)
    artifact["runtime_receipts"] = [dict(runtime) for _ in range(5)]
    artifact["coordinator_code_receipt"] = code
    artifact["checkpoint_sha256"] = checkpoint["checkpoint_sha256"]
    artifact["original_execution_policy_sha256"] = policy["policy_sha256"]
    artifact["worker_receipts"]=[{"schema":"aerp8-original-worker-receipt-v1","build_id":row["build_id"],"draft_path":str(root/f"{row['build_id']}.draft"),"ready_path":str(root/f"{row['build_id']}.READY"),"draft_bytes_sha256":row["draft_sha256"],"ready_sha256":"r" * 64,"checkpoint_sha256":checkpoint["checkpoint_sha256"],"original_execution_policy_sha256":policy["policy_sha256"],"runtime_receipt":dict(runtime)} for row in artifact["replicates"]]
    artifact["artifact_sha256"] = m.digest({key: value for key, value in artifact.items() if key != "artifact_sha256"})
    return artifact


def public_worker_receipts(files, checkpoint_sha256="c" * 64):
    code=m._driver_code_receipt(); driver=Path(m.__file__).resolve(); python=Path(code["python"]); root=Path(code["rpg_root"])
    receipts = {}
    for role, arm in m.CURRENT_ROLES.items():
        artifact = m._load_canonical(Path(files[role]["artifact_path"]), "x")
        ready = m._load_canonical(Path(files[role]["ready_path"]), "x")
        runtime={"schema":"aerp8-pinned-current-runtime-v1","rpg_root":str(root),"rpg_python":str(python),"rpg_python_sha256":hashlib.sha256(python.read_bytes()).hexdigest(),"driver_file":str(driver),"driver_file_sha256":hashlib.sha256(driver.read_bytes()).hexdigest(),"model_dir":str(root),"model_file_tree_sha256":m.ORIGINAL_MODEL_TREE,"git_capability":code["git_capability"],"worker_home_path":str(Path(files[role]["artifact_path"]).parent),"checkpoint_sha256":checkpoint_sha256,"code_receipt_sha256":code["code_sha256"],"cpu_provider_policy":{"device":"cpu","providers":["CPUExecutionProvider"]}}
        runtime["runtime_sha256"]=m.digest(runtime)
        ready["runtime_sha256"]=runtime["runtime_sha256"]
        ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"})
        Path(files[role]["ready_path"]).write_bytes(m._bytes(ready))
        receipts[role] = {"schema":"aerp8-current-worker-receipt-v1","execution_role":role,"arm_id":arm,"artifact_path":files[role]["artifact_path"],"ready_path":files[role]["ready_path"],"payload_sha256":ready["payload_sha256"],"artifact_sha256":artifact["artifact_sha256"],"ready_sha256":ready["ready_sha256"],"checkpoint_sha256":checkpoint_sha256,"runtime_receipt":runtime}
    return receipts


def formal_bundle():
    profiles=[]; source_bytes={}
    for profile_id, inventory in m.FROZEN_PROFILE_INVENTORY.items():
        for role in sorted(m.FORMAL_SOURCE_ROLES):
            profiles.append({"profile_id":profile_id,"context_label":inventory["context_label"],"source_role":role,"inventory_relative_path":inventory["paths"][role]})
            source_bytes[(profile_id,role)]=m._bytes(source(tid="same-tid"))
    manifest={"schema":m.SOURCE_MANIFEST_SCHEMA,"official_commit":m.SOURCE_COMMIT,"official_tree":m.SOURCE_TREE,"selected_profiles":["0","100"],"profiles":profiles}; manifest["manifest_sha256"]=m.digest(manifest)
    files=[{"profile_id":row["profile_id"],"source_role":row["source_role"],"inventory_relative_path":row["inventory_relative_path"],"source_file_sha256":hashlib.sha256(source_bytes[(row["profile_id"],row["source_role"])]).hexdigest(),"archive_sha256":"a"*64} for row in profiles]
    acquisition={"acquisition_sha256":"b"*64,"files":files}
    return m._build_formal_bundles(manifest=manifest,source_bytes=source_bytes,acquisition=acquisition,opacity_secret=b"o"*32)


def test_official_mapping_shape_and_tid_collision_are_label_free():
    assert m.SOURCE_RECEIPT["official_commit"] == "f66d8d1028d3f68627d00f77a967b93fbb8694b6"
    candidate, custody = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    other, _ = m.build_bundles(source_role="role-b", source=source(), opacity_secret=b"o" * 32)
    assert candidate["items"][0]["group_id"] != other["items"][0]["group_id"]
    assert candidate["items"][0]["candidates"][0]["text"] == "alpha step"
    candidate_text = json.dumps(candidate["items"], sort_keys=True)
    assert all(field not in candidate_text for field in ("target_step_id", "ground_truth", "choices", "raw_tid", "source_file_role"))
    bad = source(); bad["factual"]["scenario-a"][0]["message_list"] = [{"content": "invented"}]
    with pytest.raises(m.MemBenchError, match="message_list_invalid"): m.build_bundles(source_role="role-a", source=bad, opacity_secret=b"o" * 32)
    bad = source(); bad["factual"]["scenario-a"][0]["QA"] = [bad["factual"]["scenario-a"][0]["QA"]]
    with pytest.raises(m.MemBenchError, match="qa_invalid"): m.build_bundles(source_role="role-a", source=bad, opacity_secret=b"o" * 32)


def test_real_fixed_rank_policy_and_candidate_labels_are_rejected():
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    artifacts = m.run_candidate_arms(candidate=candidate, encoder=Encoder())
    assert artifacts["strong_raw"]["method"]["routing_policy"] == "FixedRawPolicy"
    assert artifacts["static_p5"]["method"]["routing_policy"] == "FixedP5Policy"
    assert artifacts["six_view_secondary"]["method"]["implementation"] == "mempalace_rpg.retrieval.SixViewRanker"
    assert m._bytes(artifacts["static_p5"]) == m._bytes(artifacts["static_p5_repeat"])
    leaked = copy.deepcopy(candidate); leaked["items"][0]["ground_truth"] = "a"; leaked["projection_sha256"] = m.digest({key: value for key, value in leaked.items() if key != "projection_sha256"})
    with pytest.raises(m.MemBenchError, match="label_leakage"): m.validate_candidate_projection(leaked)


def test_original_normalized_projection_is_label_free_and_uses_opaque_item_corpora():
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    normalized = m.original_normalized_projection(candidate)
    assert normalized["items"][0]["corpus_id"] == candidate["items"][0]["item_id"]
    assert "ground_truth" not in json.dumps(normalized, sort_keys=True)
    adapter = m.original_lifecycle_adapter(candidate)
    assert adapter.input_receipt(normalized)["schema"] == "aerp8-original-input-receipt-v1"
    assert adapter.format_row(item=normalized["items"][0], ranked_candidate_ids=[normalized["corpora"][0]["candidates"][0]["candidate_id"]], trace={"event": "test"}, product_row={})["item_id"] == normalized["items"][0]["item_id"]
    runtime = adapter.runtime_projection(normalized)
    assert adapter.adapter_id == "aerp8-membench-original-lifecycle-v1"
    assert "source_file_role" not in json.dumps(runtime, sort_keys=True)
    assert runtime["corpora"][0]["candidates"][0]["speaker"] == ""


def test_ranking_validation_treats_top_k_as_a_maximum():
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    artifact = m.run_candidate_arms(candidate=candidate, encoder=Encoder())["strong_raw"]
    shortened = copy.deepcopy(artifact)
    shortened["rankings"][0]["ranked_candidate_ids"] = shortened["rankings"][0]["ranked_candidate_ids"][:1]
    shortened["artifact_sha256"] = m.digest({key: value for key, value in shortened.items() if key != "artifact_sha256"})

    assert m._validate_artifact(shortened, candidate, "strong_raw") == shortened


def test_five_original_receipts_and_release_bind_every_public_result(tmp_path):
    candidate, custody = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    current, original = current_and_original(candidate)
    assert len(original["replicates"]) == 5
    assert len({row["build_id"] for row in original["replicates"]}) == 5
    with pytest.raises(m.MemBenchError, match="requires_isolated_worker_contract"):
        m.run_original_five(candidate=candidate, original_runner=lambda *_: {}, coordinator_reaudit=lambda _: {})
    results = public(candidate, current, original, tmp_path)
    release = m.mint_synthetic_release(candidate=candidate, custody=custody, public_results=results, capability_secret=b"c" * 32)
    assert m.open_synthetic_custody_after_release(release=release, candidate=candidate, custody=custody, public_results=results, capability_secret=b"c" * 32)["custody_sha256"] == custody["custody_sha256"]
    changed = copy.deepcopy(results); changed["model_receipt"] = {"model": "substituted"}; changed["public_results_sha256"] = m.digest({k: v for k, v in changed.items() if k != "public_results_sha256"})
    with pytest.raises(m.MemBenchError, match="release_binding"): m.open_synthetic_custody_after_release(release=release, candidate=candidate, custody=custody, public_results=changed, capability_secret=b"c" * 32)


def test_artifact_validation_and_hierarchical_dataset_separated_score(tmp_path):
    one, one_c = m.build_bundles(source_role="dataset-one", source=source(target_step_id=[0, 1]), opacity_secret=b"o" * 32)
    many_source = source(target_step_id=list(range(12))); many_source["factual"]["scenario-a"][0]["message_list"] = [f"alpha {n}" for n in range(12)]
    two, two_c = m.build_bundles(source_role="dataset-two", source=many_source, opacity_secret=b"o" * 32)
    candidate = {"schema": m.CANDIDATE_SCHEMA, "source_receipt": m.SOURCE_RECEIPT, "items": [*one["items"], *two["items"]]}; candidate["projection_sha256"] = m.digest(candidate)
    custody = {"schema": m.CUSTODY_SCHEMA, "source_receipt": m.SOURCE_RECEIPT, "records": [*one_c["records"], *two_c["records"]]}; custody["custody_sha256"] = m.digest(custody)
    current, original = current_and_original(candidate)
    report = m.score_synthetic_frozen(artifacts=[current["strong_raw"], current["static_p5"], current["six_view_secondary"], original], candidate=candidate, custody=custody, thresholds_by_dataset={"dataset-one": {"overall_delta_min": 0.0}, "dataset-two": {"overall_delta_min": 0.1}}, resamples=20)
    assert set(report["dataset_reports"]) == {"dataset-one", "dataset-two"}
    assert report["dataset_reports"]["dataset-two"]["rows"][0]["metrics"]["static_p5"]["gold_denominator"] == 12.0
    assert report["dataset_reports"]["dataset-two"]["bootstrap"]["original_replicate_layer"] is True
    tampered = copy.deepcopy(current["static_p5"]); tampered["rankings"][0]["ranked_candidate_ids"] *= 2; tampered["artifact_sha256"] = m.digest({k: v for k, v in tampered.items() if k != "artifact_sha256"})
    with pytest.raises(m.MemBenchError, match="ranking_invalid"):
        m.score_synthetic_frozen(artifacts=[current["strong_raw"], tampered, current["six_view_secondary"], original], candidate=candidate, custody=custody, thresholds_by_dataset={"dataset-one": {"overall_delta_min": 0.0}, "dataset-two": {"overall_delta_min": 0.0}}, resamples=2)


def test_consumed_release_retry_is_exact_and_publish_failure_is_atomic(tmp_path):
    candidate, custody = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32); current, original = current_and_original(candidate); results = public(candidate, current, original, tmp_path)
    release = m.mint_synthetic_release(candidate=candidate, custody=custody, public_results=results, capability_secret=b"c" * 32); marker = tmp_path / "consumed.json"
    assert m.consume_synthetic_release(marker_path=marker, release=release)["published"] is True
    assert m.consume_synthetic_release(marker_path=marker, release=release)["retry_idempotent"] is True
    with pytest.raises(m.MemBenchError, match="publish_conflict"): m.publish_nonreplace(path=marker, payload=b"different")
    target = tmp_path / "atomic.json"
    with pytest.raises(RuntimeError, match="injected"): m.publish_nonreplace(path=target, payload=b"x", before_publish=lambda: (_ for _ in ()).throw(RuntimeError("injected")))
    assert not target.exists()


def test_worker_subprocess_is_label_free_and_refuses_synthetic_fallback():
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    with pytest.raises(m.MemBenchError, match="formal_current_requires_isolated_worker_contract"):
        m.run_worker_subprocess(role="static_p5", candidate=candidate, encoder_spec={"resolver": "synthetic"})


def test_formal_preflight_is_receipt_only_and_refuses_missing_pinned_receipts(tmp_path):
    candidate, custody = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    current, original = current_and_original(candidate)
    results = public(candidate, current, original, tmp_path)
    release = m.mint_synthetic_release(candidate=candidate, custody=custody, public_results=results, capability_secret=b"c" * 32)
    with pytest.raises(m.MemBenchError, match="formal_preflight"):
        m.formal_preflight({"schema": m.FORMAL_PREFLIGHT_SCHEMA})
    with pytest.raises(m.MemBenchError, match="formal_preflight_public_results_invalid"):
        m.formal_preflight({"schema": m.FORMAL_PREFLIGHT_SCHEMA, "candidate": candidate, "public_results": results, "release": release, "source_receipt": m.SOURCE_RECEIPT, "current_commit": {"clean": True}, "original_commit": {"clean": True}, "model_receipt": {"model": "synthetic-test"}, "code_receipt": {"code": "synthetic-test"}})


def test_formal_freeze_reads_exact_artifact_and_ready_bytes(tmp_path):
    candidate, _ = formal_bundle()
    current, original = current_and_original(candidate); original = formal_original(candidate, original)
    files = {}
    for role, arm, artifact in (("raw", "strong_raw", current["strong_raw"]), ("p5_primary", "static_p5", current["static_p5"]), ("p5_repeat", "static_p5", current["static_p5_repeat"]), ("six", "six_view_secondary", current["six_view_secondary"])):
        artifact_path = tmp_path / f"{role}.json"; ready_path = tmp_path / f"{role}.ready.json"; payload = m._bytes(artifact)
        m.publish_nonreplace(path=artifact_path, payload=payload)
        ready = {"schema": m.READY_SCHEMA, "arm_id": arm, "execution_role": role, "payload_sha256": hashlib.sha256(payload).hexdigest(), "artifact_sha256": artifact["artifact_sha256"], "runtime_sha256": "r" * 64}; ready["ready_sha256"] = m.digest(ready)
        m.publish_nonreplace(path=ready_path, payload=m._bytes(ready)); files[role] = {"artifact_path": str(artifact_path), "ready_path": str(ready_path), "execution_role": role}
    original_path = tmp_path / "original.json"; original_ready_path = tmp_path / "original.ready.json"; original_payload = m._bytes(original)
    m.publish_nonreplace(path=original_path, payload=original_payload)
    original_ready = {"schema": m.READY_SCHEMA, "arm_id": "original_public_product", "payload_sha256": hashlib.sha256(original_payload).hexdigest(),"original_execution_policy_sha256":original["original_execution_policy_sha256"]}; original_ready["ready_sha256"] = m.digest(original_ready)
    m.publish_nonreplace(path=original_ready_path, payload=m._bytes(original_ready))
    code_receipt = m._driver_code_receipt(); model_receipt = {"sha256": m.ORIGINAL_MODEL_TREE}
    original["coordinator_code_receipt"] = code_receipt; original["artifact_sha256"] = m.digest({key: value for key, value in original.items() if key != "artifact_sha256"}); original_payload = m._bytes(original); original_path.write_bytes(original_payload)
    original_ready = {"schema": m.READY_SCHEMA, "arm_id": "original_public_product", "payload_sha256": hashlib.sha256(original_payload).hexdigest(),"original_execution_policy_sha256":original["original_execution_policy_sha256"]}; original_ready["ready_sha256"] = m.digest(original_ready); original_ready_path.write_bytes(m._bytes(original_ready))
    result = m.freeze_public_results(candidate=candidate, current_files=files, current_worker_receipts=public_worker_receipts(files), original_file={"artifact_path": str(original_path), "ready_path": str(original_ready_path)}, sealed_custody_sha256="c" * 64, threshold_protocol=threshold_protocol(), model_receipt=model_receipt, code_receipt=code_receipt, checkpoint=checkpoint_fixture())
    assert result["schema"] == m.PUBLIC_RESULTS_SCHEMA
    bad_ready_path = tmp_path / "bad.ready.json"; bad = dict(original_ready); bad["payload_sha256"] = "0" * 64; bad["ready_sha256"] = m.digest({key: value for key, value in bad.items() if key != "ready_sha256"}); m.publish_nonreplace(path=bad_ready_path, payload=m._bytes(bad))
    with pytest.raises(m.MemBenchError, match="public_ready_invalid"):
        m.freeze_public_results(candidate=candidate, current_files=files, current_worker_receipts=public_worker_receipts(files), original_file={"artifact_path": str(original_path), "ready_path": str(bad_ready_path)}, sealed_custody_sha256="c" * 64, threshold_protocol=threshold_protocol(), model_receipt=model_receipt, code_receipt=code_receipt, checkpoint=checkpoint_fixture())


def _formal_custodian_inputs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    candidate, custody = formal_bundle(); current, original = current_and_original(candidate); original = formal_original(candidate, original)
    files = {}
    for role, arm, artifact in (("raw", "strong_raw", current["strong_raw"]), ("p5_primary", "static_p5", current["static_p5"]), ("p5_repeat", "static_p5", current["static_p5_repeat"]), ("six", "six_view_secondary", current["six_view_secondary"])):
        artifact_path = tmp_path / f"{role}.artifact.json"; ready_path = tmp_path / f"{role}.READY.json"; payload = m._bytes(artifact)
        m.publish_nonreplace(path=artifact_path, payload=payload)
        ready = {"schema": m.READY_SCHEMA, "arm_id": arm, "execution_role": role, "payload_sha256": hashlib.sha256(payload).hexdigest(), "artifact_sha256": artifact["artifact_sha256"], "runtime_sha256": "r" * 64}; ready["ready_sha256"] = m.digest(ready)
        m.publish_nonreplace(path=ready_path, payload=m._bytes(ready)); files[role] = {"artifact_path": str(artifact_path), "ready_path": str(ready_path), "execution_role": role}
    original_path = tmp_path / "original.artifact.json"; original_ready_path = tmp_path / "original.READY.json"; original_payload = m._bytes(original)
    m.publish_nonreplace(path=original_path, payload=original_payload)
    original_ready = {"schema": m.READY_SCHEMA, "arm_id": "original_public_product", "payload_sha256": hashlib.sha256(original_payload).hexdigest(),"original_execution_policy_sha256":original["original_execution_policy_sha256"]}; original_ready["ready_sha256"] = m.digest(original_ready)
    m.publish_nonreplace(path=original_ready_path, payload=m._bytes(original_ready))
    custody_path = tmp_path / "sealed-custody.json"; custody_ready_path = tmp_path / "sealed-custody.READY.json"; candidate_path = tmp_path / "candidate.json"; candidate_ready_path = tmp_path / "candidate.READY.json"; marker_path = tmp_path / "source-builder-consumed.json"; checkpoint_path = tmp_path / "expected-checkpoint.json"; threshold_path = tmp_path / "threshold.json"; public_path = tmp_path / "public.json"; secret_path = tmp_path / "capability.secret"; operator_secret_path=tmp_path/"operator.secret"; release_path = tmp_path / "release.json"
    for path, value in ((candidate_path, candidate), (custody_path, custody), (threshold_path, threshold_protocol())): m.publish_nonreplace(path=path, payload=m._bytes(value))
    code_receipt=m._driver_code_receipt(); marker = {"schema":"aerp8-membench-source-builder-consumed-v1","authorization_sha256":"a"*64,"candidate_payload_sha256":hashlib.sha256(candidate_path.read_bytes()).hexdigest(),"custody_payload_sha256":hashlib.sha256(custody_path.read_bytes()).hexdigest(),"candidate_projection_sha256":candidate["projection_sha256"],"custody_sha256":custody["custody_sha256"],"source_receipt_sha256":m.digest(candidate["source_receipt"]),"acquisition_sha256":candidate["source_receipt"]["acquisition_sha256"],"profile_role_file_map_sha256":candidate["source_receipt"]["profile_role_file_map_sha256"],"checkpoint_sha256":"c"*64,"code_receipt_sha256":code_receipt["code_sha256"]}; marker["generation_hmac"]=m._opaque(b"k"*32,marker)
    m.publish_nonreplace(path=marker_path,payload=m._bytes(marker)); marker_sha=hashlib.sha256(m._bytes(marker)).hexdigest()
    candidate_ready = {"schema":m.READY_SCHEMA,"arm_id":"formal_source_candidate","payload_sha256":hashlib.sha256(candidate_path.read_bytes()).hexdigest(),"projection_sha256":candidate["projection_sha256"],"source_receipt_sha256":m.digest(candidate["source_receipt"]),"source_builder_marker_sha256":marker_sha}; candidate_ready["ready_sha256"]=m.digest(candidate_ready); m.publish_nonreplace(path=candidate_ready_path,payload=m._bytes(candidate_ready))
    custody_ready = {"schema": m.READY_SCHEMA, "arm_id": "formal_sealed_custody", "payload_sha256": hashlib.sha256(custody_path.read_bytes()).hexdigest(), "custody_sha256": custody["custody_sha256"],"source_receipt_sha256":m.digest(candidate["source_receipt"]),"source_builder_marker_sha256":marker_sha}; custody_ready["ready_sha256"] = m.digest(custody_ready); m.publish_nonreplace(path=custody_ready_path, payload=m._bytes(custody_ready))
    checkpoint_path.write_bytes(m._bytes({"fixture":"must-be-monkeypatched"}))
    secret_path.write_bytes(b"s" * 32)
    operator_secret_path.write_bytes(b"k"*32)
    public = m.freeze_public_results(candidate=candidate, current_files=files, current_worker_receipts=public_worker_receipts(files), original_file={"artifact_path": str(original_path), "ready_path": str(original_ready_path)}, sealed_custody_sha256=hashlib.sha256(custody_path.read_bytes()).hexdigest(), threshold_protocol=threshold_protocol(), model_receipt={"sha256": m.ORIGINAL_MODEL_TREE}, code_receipt=code_receipt, checkpoint=checkpoint_fixture())
    m.publish_nonreplace(path=public_path, payload=m._bytes(public))
    release = m.mint_formal_release(candidate_path=candidate_path, custody_ready_path=custody_ready_path, public_results_path=public_path, threshold_protocol_path=threshold_path, capability_secret_path=secret_path)
    m.publish_nonreplace(path=release_path, payload=m._bytes(release))
    output = tmp_path / "custodian-output"; output.mkdir()
    config = {"schema": m.CUSTODIAN_CONFIG_SCHEMA, "candidate_path": str(candidate_path), "candidate_ready_path":str(candidate_ready_path),"source_builder_marker_path":str(marker_path), "custody_path": str(custody_path), "custody_ready_path": str(custody_ready_path), "public_results_path": str(public_path), "release_path": str(release_path), "threshold_protocol_path": str(threshold_path), "capability_secret_path": str(secret_path),"operator_capability_secret_path":str(operator_secret_path),"expected_checkpoint_path":str(checkpoint_path), "report_path": str(output / "report.json"), "ready_path": str(output / "report.READY.json"), "consumed_marker_path": str(output / "consumed.json")}
    return config, custody_path


def test_formal_custodian_validates_before_opening_custody_and_is_byte_idempotent(tmp_path, monkeypatch):
    config, custody_path = _formal_custodian_inputs(tmp_path)
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    observed = []; real_read_bytes = Path.read_bytes
    def tracked_read(path, *args, **kwargs):
        if path == custody_path: observed.append(path)
        return real_read_bytes(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", tracked_read)
    first = m.run_formal_custodian(config)
    assert first["opened_custody"] is True and len(observed) == 1
    assert set(first["report"]["cross_source_roles"]) == m.FORMAL_SOURCE_ROLES
    assert first["report"]["aggregates"]["overall"]["n_rows"] == 8
    assert first["report"]["gate"]["adversarial"].startswith("N/A")
    observed.clear(); monkeypatch.setattr(m, "_formal_rows", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("retry rescored")))
    real_stat = Path.stat
    def no_custody_read(path, *args, **kwargs):
        if path == custody_path: raise AssertionError("retry read custody")
        return real_read_bytes(path, *args, **kwargs)
    def no_custody_stat(path, *args, **kwargs):
        if path == custody_path: raise AssertionError("retry stat custody")
        return real_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_bytes", no_custody_read); monkeypatch.setattr(Path, "stat", no_custody_stat)
    retry = m.run_formal_custodian(config)
    assert retry["retry_idempotent"] is True and retry["opened_custody"] is False and observed == []


def test_formal_threshold_and_four_source_roles_fail_closed():
    protocol = threshold_protocol(); protocol["source_roles"] = protocol["source_roles"][:-1]; protocol["threshold_sha256"] = m.digest({key: value for key, value in protocol.items() if key != "threshold_sha256"})
    with pytest.raises(m.MemBenchError, match="threshold_protocol_invalid"): m.validate_threshold_protocol(protocol)
    _, custody = formal_bundle(); custody["records"] = [row for row in custody["records"] if row["source_file_role"] != "observation_factual"]; custody["custody_sha256"] = m.digest({key: value for key, value in custody.items() if key != "custody_sha256"})
    with pytest.raises(m.MemBenchError, match="role_coverage_invalid"): m._formal_custody(custody)


def test_formal_manifest_and_candidate_custody_denominator_are_exact_not_self_declared():
    candidate, custody = formal_bundle()
    assert candidate["source_receipt"]["selected_profiles_sha256"] == m.digest(["0", "100"])
    m._cross_bind_candidate_custody(candidate=candidate, custody=custody)
    missing = copy.deepcopy(custody); missing["records"].pop(); missing["custody_sha256"] = m.digest({key:value for key,value in missing.items() if key != "custody_sha256"})
    with pytest.raises(m.MemBenchError, match="denominator_mismatch"):
        m._cross_bind_candidate_custody(candidate=candidate, custody=missing)
    duplicate = copy.deepcopy(custody); duplicate["records"].append(copy.deepcopy(duplicate["records"][0])); duplicate["custody_sha256"] = m.digest({key:value for key,value in duplicate.items() if key != "custody_sha256"})
    with pytest.raises(m.MemBenchError, match="record_duplicate"):
        m._cross_bind_candidate_custody(candidate=candidate, custody=duplicate)
    bad_gold = copy.deepcopy(custody); bad_gold["records"][0]["gold_candidate_ids"]=["not-a-candidate"]; bad_gold["custody_sha256"] = m.digest({key:value for key,value in bad_gold.items() if key != "custody_sha256"})
    with pytest.raises(m.MemBenchError, match="gold_crosswalk_invalid"):
        m._cross_bind_candidate_custody(candidate=candidate, custody=bad_gold)
    swapped=copy.deepcopy(custody); swapped["records"][0]["profile_id"]="100" if swapped["records"][0]["profile_id"]=="0" else "0"; swapped["custody_sha256"]=m.digest({key:value for key,value in swapped.items() if key!="custody_sha256"})
    with pytest.raises(m.MemBenchError,match="crosswalk_mismatch"):
        m._cross_bind_candidate_custody(candidate=candidate,custody=swapped)
    old=copy.deepcopy(custody); old["source_receipt"]=dict(old["source_receipt"],acquisition_sha256="0"*64); old["custody_sha256"]=m.digest({key:value for key,value in old.items() if key!="custody_sha256"})
    with pytest.raises(m.MemBenchError,match="source_receipt_mismatch"):
        m._cross_bind_candidate_custody(candidate=candidate,custody=old)


def test_driver_receipt_fails_closed_on_import_origin_and_checkpoint_policy_drift(tmp_path, monkeypatch):
    fake=types.SimpleNamespace(__file__=str(tmp_path/"site-packages"/"retrieval.py"))
    monkeypatch.setitem(sys.modules,"mempalace_rpg.retrieval",fake)
    with pytest.raises(m.MemBenchError, match="import_origin_outside_root"):
        m._driver_code_receipt()
    monkeypatch.delitem(sys.modules,"mempalace_rpg.retrieval")
    good=m._driver_code_receipt()
    checkpoint={"schema":m.CURRENT_CHECKPOINT_SCHEMA,"driver_code_receipt":good,"original_execution_policy":original_execution_policy()}; checkpoint["original_execution_policy_sha256"]=checkpoint["original_execution_policy"]["policy_sha256"]; checkpoint["checkpoint_sha256"]=m.digest(checkpoint)
    path=tmp_path/"checkpoint.json"; path.write_bytes(m._bytes(checkpoint))
    monkeypatch.setattr(m,"_driver_code_receipt",lambda:good)
    dirty=copy.deepcopy(good); dirty["git"]=dict(dirty["git"],git_dirty=True); dirty["code_sha256"]=m.digest({key:value for key,value in dirty.items() if key!="code_sha256"}); bad={"schema":m.CURRENT_CHECKPOINT_SCHEMA,"driver_code_receipt":dirty,"original_execution_policy":original_execution_policy()}; bad["original_execution_policy_sha256"]=bad["original_execution_policy"]["policy_sha256"]; bad["checkpoint_sha256"]=m.digest(bad); path.write_bytes(m._bytes(bad))
    with pytest.raises(m.MemBenchError, match="expected_checkpoint_invalid"):
        m._require_external_current_checkpoint(path)


def test_formal_public_exact_schema_and_resealed_embedded_receipts_fail(tmp_path):
    config,_=_formal_custodian_inputs(tmp_path)
    candidate=m._load_canonical(Path(config["candidate_path"]),"x"); public=m._load_canonical(Path(config["public_results_path"]),"x"); threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x"); custody_sha=m._sealed_custody_ready_bytes_sha(Path(config["custody_ready_path"]))
    m._formal_public(public,candidate=candidate,custody_sha256=custody_sha,threshold=threshold)
    extra=copy.deepcopy(public); extra["unexpected"]=True; extra["public_results_sha256"]=m.digest({key:value for key,value in extra.items() if key!="public_results_sha256"})
    with pytest.raises(m.MemBenchError,match="formal_public_results_invalid"):
        m._formal_public(extra,candidate=candidate,custody_sha256=custody_sha,threshold=threshold)
    tampered=copy.deepcopy(public); tampered["ready_receipts"]["raw"]["runtime_sha256"]="0"*64; tampered["public_results_sha256"]=m.digest({key:value for key,value in tampered.items() if key!="public_results_sha256"})
    with pytest.raises(m.MemBenchError,match="formal_public_results_invalid"):
        m._formal_public(tampered,candidate=candidate,custody_sha256=custody_sha,threshold=threshold)
    tampered=copy.deepcopy(public); tampered["original_replicate_receipts"][0]["build_id"]="resealed-swap"; tampered["public_results_sha256"]=m.digest({key:value for key,value in tampered.items() if key!="public_results_sha256"})
    with pytest.raises(m.MemBenchError,match="formal_public_results_invalid"):
        m._formal_public(tampered,candidate=candidate,custody_sha256=custody_sha,threshold=threshold)


def test_formal_statistics_are_profiled_macro_and_row_order_invariant(tmp_path):
    config,_=_formal_custodian_inputs(tmp_path)
    candidate=m._load_canonical(Path(config["candidate_path"]),"x"); custody=m._load_canonical(Path(config["custody_path"]),"x"); public=m._load_canonical(Path(config["public_results_path"]),"x"); threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x"); custody_sha=m._sealed_custody_ready_bytes_sha(Path(config["custody_ready_path"]))
    checked=m._formal_public(public,candidate=candidate,custody_sha256=custody_sha,threshold=threshold)
    report=m._formal_rows(artifacts=checked["artifacts"],custody=custody,threshold=threshold)
    assert set(report["profile_secondary"])=={"0","100"}
    assert "question_macro" in report["aggregates"]["overall"] and "leakage_unit_macro" in report["aggregates"]["overall"]
    assert report["aggregates"]["overall"]["p5_minus_original_recall_at_10"]["original_replicate_layer"]=="complete_build_draw"
    reversed_custody=copy.deepcopy(custody); reversed_custody["records"].reverse(); reversed_custody["custody_sha256"]=m.digest({key:value for key,value in reversed_custody.items() if key!="custody_sha256"})
    assert m._formal_rows(artifacts=checked["artifacts"],custody=reversed_custody,threshold=threshold)==report


def test_two_way_profile_swap_resealed_through_generation_receipts_fails_before_scoring(tmp_path,monkeypatch):
    config,_=_formal_custodian_inputs(tmp_path); monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    candidate=m._load_canonical(Path(config["candidate_path"]),"x"); custody=m._load_canonical(Path(config["custody_path"]),"x")
    left,right=[row for row in custody["records"] if row["source_file_role"]=="participation_factual"]
    # Swap the visible profile metadata in both directions: every profile×role
    # count remains one, and all ordinary custody digests are recomputed.
    for key in ("profile_id",): left[key],right[key]=right[key],left[key]
    left["source_locator"]["profile_id"],right["source_locator"]["profile_id"]=right["source_locator"]["profile_id"],left["source_locator"]["profile_id"]
    custody["custody_sha256"]=m.digest({key:value for key,value in custody.items() if key!="custody_sha256"}); custody_path=Path(config["custody_path"]); custody_path.write_bytes(m._bytes(custody))
    marker_path=Path(config["source_builder_marker_path"]); marker=m._load_canonical(marker_path,"x"); marker["custody_payload_sha256"]=hashlib.sha256(custody_path.read_bytes()).hexdigest(); marker["custody_sha256"]=custody["custody_sha256"]; marker_path.write_bytes(m._bytes(marker)); marker_sha=hashlib.sha256(marker_path.read_bytes()).hexdigest()
    candidate_ready_path=Path(config["candidate_ready_path"]); candidate_ready=m._load_canonical(candidate_ready_path,"x"); candidate_ready["source_builder_marker_sha256"]=marker_sha; candidate_ready["ready_sha256"]=m.digest({key:value for key,value in candidate_ready.items() if key!="ready_sha256"}); candidate_ready_path.write_bytes(m._bytes(candidate_ready))
    custody_ready_path=Path(config["custody_ready_path"]); custody_ready=m._load_canonical(custody_ready_path,"x"); custody_ready["payload_sha256"]=hashlib.sha256(custody_path.read_bytes()).hexdigest(); custody_ready["custody_sha256"]=custody["custody_sha256"]; custody_ready["source_builder_marker_sha256"]=marker_sha; custody_ready["ready_sha256"]=m.digest({key:value for key,value in custody_ready.items() if key!="ready_sha256"}); custody_ready_path.write_bytes(m._bytes(custody_ready))
    threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x"); old_public=m._load_canonical(Path(config["public_results_path"]),"x")
    public=m.freeze_public_results(candidate=candidate,current_files={key:old_public["artifact_files"][key] for key in m.CURRENT_ROLES},current_worker_receipts=old_public["current_worker_receipts"],original_file=old_public["artifact_files"]["original_public_product"],sealed_custody_sha256=hashlib.sha256(custody_path.read_bytes()).hexdigest(),threshold_protocol=threshold,model_receipt=old_public["model_receipt"],code_receipt=old_public["code_receipt"],checkpoint=checkpoint_fixture()); Path(config["public_results_path"]).write_bytes(m._bytes(public))
    release=m.mint_formal_release(candidate_path=Path(config["candidate_path"]),custody_ready_path=custody_ready_path,public_results_path=Path(config["public_results_path"]),threshold_protocol_path=Path(config["threshold_protocol_path"]),capability_secret_path=Path(config["capability_secret_path"])); Path(config["release_path"]).write_bytes(m._bytes(release))
    monkeypatch.setattr(m,"_formal_rows",lambda **_kwargs: (_ for _ in ()).throw(AssertionError("scoring reached")))
    with pytest.raises(m.MemBenchError,match="source_generation_receipt_invalid"):
        m.run_formal_custodian(config)


def test_old_generation_candidate_ready_marker_mix_fails_before_custody_open(tmp_path):
    first,_=_formal_custodian_inputs(tmp_path/"first"); second,_=_formal_custodian_inputs(tmp_path/"second")
    candidate=m._load_canonical(Path(first["candidate_path"]),"x")
    candidate["items"][0]["query_text"]="old-generation-different"; candidate["projection_sha256"]=m.digest({key:value for key,value in candidate.items() if key!="projection_sha256"}); Path(first["candidate_path"]).write_bytes(m._bytes(candidate))
    with pytest.raises(m.MemBenchError,match="source_generation_receipt_invalid"):
        m._verify_source_generation_receipts(candidate=candidate,candidate_ready_path=Path(first["candidate_ready_path"]),custody_ready_path=Path(second["custody_ready_path"]),marker_path=Path(second["source_builder_marker_path"]),operator_secret=b"k"*32,checkpoint=checkpoint_fixture(),code_receipt=m._driver_code_receipt())


def test_generation_seal_rejects_rehashed_candidate_or_matching_custody_deletion_before_open(tmp_path, monkeypatch):
    def reseal_ordinary(config, *, mutate):
        candidate_path, custody_path = Path(config["candidate_path"]), Path(config["custody_path"])
        candidate, custody = m._load_canonical(candidate_path, "x"), m._load_canonical(custody_path, "x")
        mutate(candidate, custody)
        candidate["projection_sha256"] = m.digest({key:value for key,value in candidate.items() if key != "projection_sha256"})
        custody["custody_sha256"] = m.digest({key:value for key,value in custody.items() if key != "custody_sha256"})
        candidate_path.write_bytes(m._bytes(candidate)); custody_path.write_bytes(m._bytes(custody))
        marker_path=Path(config["source_builder_marker_path"]); marker=m._load_canonical(marker_path,"x")
        # An ordinary writer can recompute every visible receipt and READY
        # digest, but cannot manufacture the operator generation capability.
        marker["candidate_payload_sha256"] = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        marker["candidate_projection_sha256"] = candidate["projection_sha256"]
        marker["custody_payload_sha256"] = hashlib.sha256(custody_path.read_bytes()).hexdigest()
        marker["custody_sha256"] = custody["custody_sha256"]
        marker_path.write_bytes(m._bytes(marker)); marker_sha=hashlib.sha256(marker_path.read_bytes()).hexdigest()
        candidate_ready_path=Path(config["candidate_ready_path"]); ready=m._load_canonical(candidate_ready_path,"x")
        ready.update({"payload_sha256":hashlib.sha256(candidate_path.read_bytes()).hexdigest(),"projection_sha256":candidate["projection_sha256"],"source_builder_marker_sha256":marker_sha})
        ready["ready_sha256"] = m.digest({key:value for key,value in ready.items() if key != "ready_sha256"}); candidate_ready_path.write_bytes(m._bytes(ready))
        custody_ready_path=Path(config["custody_ready_path"]); ready=m._load_canonical(custody_ready_path,"x")
        ready.update({"payload_sha256":hashlib.sha256(custody_path.read_bytes()).hexdigest(),"custody_sha256":custody["custody_sha256"],"source_builder_marker_sha256":marker_sha})
        ready["ready_sha256"] = m.digest({key:value for key,value in ready.items() if key != "ready_sha256"}); custody_ready_path.write_bytes(m._bytes(ready))

    raw_read=Path.read_bytes
    for suffix, mutate in (
        ("candidate-text", lambda candidate, _custody: candidate["items"][0].__setitem__("query_text", "rewritten-but-ordinary-rehashed")),
        ("matching-delete", lambda candidate, custody: (candidate["items"].pop(0), custody["records"].pop(0))),
    ):
        config, custody_path = _formal_custodian_inputs(tmp_path / suffix)
        monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
        reseal_ordinary(config, mutate=mutate)
        observed=[]; real_read=raw_read
        def tracked(path,*args,**kwargs):
            if path == custody_path: observed.append(path)
            return real_read(path,*args,**kwargs)
        monkeypatch.setattr(Path,"read_bytes",tracked)
        with pytest.raises(m.MemBenchError,match="source_generation_receipt_invalid"):
            m.run_formal_custodian(config)
        assert observed == []


def test_custodian_rejects_valid_receipt_switch_before_custody_open(tmp_path, monkeypatch):
    config, custody_path = _formal_custodian_inputs(tmp_path)
    # The packet was frozen under A.  Simulate a separately valid clean B
    # receipt returned by the external checkpoint verifier; no custody read is
    # permitted merely because both receipts are otherwise well formed.
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture("b" * 64))
    observed=[]; real_read=Path.read_bytes
    def tracked(path,*args,**kwargs):
        if path == custody_path: observed.append(path)
        return real_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_bytes",tracked)
    with pytest.raises(m.MemBenchError,match="checkpoint_public_mismatch"):
        m.run_formal_custodian(config)
    assert observed == []


def test_public_runtime_provenance_rejects_resealed_current_and_original_tamper(tmp_path):
    config, _ = _formal_custodian_inputs(tmp_path)
    candidate=m._load_canonical(Path(config["candidate_path"]),"x"); threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x"); public=m._load_canonical(Path(config["public_results_path"]),"x"); code=public["code_receipt"]
    files={key:public["artifact_files"][key] for key in m.CURRENT_ROLES}; original_file=public["artifact_files"]["original_public_product"]
    # A self-consistent current runtime/READY/worker receipt still cannot claim
    # a different reviewed root: the shared validator rechecks provenance.
    current=copy.deepcopy(public["current_worker_receipts"]); runtime=current["raw"]["runtime_receipt"]; runtime["rpg_root"]=str(tmp_path); runtime["runtime_sha256"]=m.digest({key:value for key,value in runtime.items() if key!="runtime_sha256"}); current["raw"]["runtime_receipt"]=runtime
    ready_path=Path(files["raw"]["ready_path"]); ready=m._load_canonical(ready_path,"x"); ready["runtime_sha256"]=runtime["runtime_sha256"]; ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); ready_path.write_bytes(m._bytes(ready)); current["raw"]["ready_sha256"]=ready["ready_sha256"]
    with pytest.raises(m.MemBenchError,match="current_runtime_receipt_invalid"):
        m.freeze_public_results(candidate=candidate,current_files=files,current_worker_receipts=current,original_file=original_file,sealed_custody_sha256=public["sealed_custody_sha256"],threshold_protocol=threshold,model_receipt=public["model_receipt"],code_receipt=code,checkpoint=checkpoint_fixture())

    config, _ = _formal_custodian_inputs(tmp_path / "original")
    candidate=m._load_canonical(Path(config["candidate_path"]),"x"); threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x"); public=m._load_canonical(Path(config["public_results_path"]),"x"); code=public["code_receipt"]
    original_path=Path(public["artifact_files"]["original_public_product"]["artifact_path"]); original=m._load_canonical(original_path,"x"); original["runtime_receipts"][0]["checkpoint_sha256"]="b"*64; original["runtime_receipts"][0]["runtime_sha256"]=m.digest({key:value for key,value in original["runtime_receipts"][0].items() if key!="runtime_sha256"}); original["artifact_sha256"]=m.digest({key:value for key,value in original.items() if key!="artifact_sha256"}); original_path.write_bytes(m._bytes(original))
    original_ready_path=Path(public["artifact_files"]["original_public_product"]["ready_path"]); ready=m._load_canonical(original_ready_path,"x"); ready["payload_sha256"]=hashlib.sha256(original_path.read_bytes()).hexdigest(); ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); original_ready_path.write_bytes(m._bytes(ready))
    with pytest.raises(m.MemBenchError,match="original_runtime_receipt_invalid"):
        m.freeze_public_results(candidate=candidate,current_files={key:public["artifact_files"][key] for key in m.CURRENT_ROLES},current_worker_receipts=public["current_worker_receipts"],original_file=public["artifact_files"]["original_public_product"],sealed_custody_sha256=public["sealed_custody_sha256"],threshold_protocol=threshold,model_receipt=public["model_receipt"],code_receipt=code,checkpoint=checkpoint_fixture())


def test_runtime_tamper_cannot_reach_custodian_or_open_custody(tmp_path,monkeypatch):
    config,custody_path=_formal_custodian_inputs(tmp_path); monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    public=m._load_canonical(Path(config["public_results_path"]),"x"); original_path=Path(public["artifact_files"]["original_public_product"]["artifact_path"]); original=m._load_canonical(original_path,"x")
    # This is the P0 switch: all ordinary nested and artifact hashes are
    # recomputed, while the external/public checkpoint remains A.
    runtime=original["runtime_receipts"][0]; runtime["checkpoint_sha256"]="b"*64; runtime["runtime_sha256"]=m.digest({key:value for key,value in runtime.items() if key!="runtime_sha256"}); original["artifact_sha256"]=m.digest({key:value for key,value in original.items() if key!="artifact_sha256"}); original_path.write_bytes(m._bytes(original))
    ready_path=Path(public["artifact_files"]["original_public_product"]["ready_path"]); ready=m._load_canonical(ready_path,"x"); ready["payload_sha256"]=hashlib.sha256(original_path.read_bytes()).hexdigest(); ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); ready_path.write_bytes(m._bytes(ready))
    observed=[]; real_read=Path.read_bytes
    def tracked(file,*args,**kwargs):
        if file==custody_path: observed.append(file)
        return real_read(file,*args,**kwargs)
    monkeypatch.setattr(Path,"read_bytes",tracked)
    with pytest.raises(m.MemBenchError,match="original_runtime_receipt_invalid"): m.run_formal_custodian(config)
    assert observed==[]


def test_current_runtime_field_tamper_without_reseal_is_rejected(tmp_path):
    config,_=_formal_custodian_inputs(tmp_path); public=m._load_canonical(Path(config["public_results_path"]),"x")
    bad=copy.deepcopy(public["current_worker_receipts"]); bad["raw"]["runtime_receipt"]["rpg_root"]=str(tmp_path)
    with pytest.raises(m.MemBenchError,match="current_runtime_receipt_invalid"):
        m._public_current_worker_receipts(receipts=bad,files={key:public["artifact_files"][key] for key in m.CURRENT_ROLES},loaded={role:m._load_public_artifact_file(capability=public["artifact_files"][role],candidate=m._load_canonical(Path(config["candidate_path"]),"x"),arm=arm,role=role) for role,arm in m.CURRENT_ROLES.items()},checkpoint_sha256="c"*64,code_receipt=public["code_receipt"])


def test_same_checkpoint_environment_replacement_fails_at_public_freeze(tmp_path):
    config,_=_formal_custodian_inputs(tmp_path); candidate=m._load_canonical(Path(config["candidate_path"]),"x"); threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x"); public=m._load_canonical(Path(config["public_results_path"]),"x"); code=public["code_receipt"]
    files={key:public["artifact_files"][key] for key in m.CURRENT_ROLES}; current=copy.deepcopy(public["current_worker_receipts"])
    runtime=current["raw"]["runtime_receipt"]; runtime["model_dir"]=str(tmp_path/"missing-model-b"); runtime["runtime_sha256"]=m.digest({key:value for key,value in runtime.items() if key!="runtime_sha256"})
    ready_path=Path(files["raw"]["ready_path"]); ready=m._load_canonical(ready_path,"x"); ready["runtime_sha256"]=runtime["runtime_sha256"]; ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); ready_path.write_bytes(m._bytes(ready)); current["raw"]["ready_sha256"]=ready["ready_sha256"]
    with pytest.raises(m.MemBenchError,match="current_runtime_receipt_invalid"):
        m.freeze_public_results(candidate=candidate,current_files=files,current_worker_receipts=current,original_file=public["artifact_files"]["original_public_product"],sealed_custody_sha256=public["sealed_custody_sha256"],threshold_protocol=threshold,model_receipt=public["model_receipt"],code_receipt=code,checkpoint=checkpoint_fixture())


@pytest.mark.parametrize("field,replacement",[
    ("original_python", lambda policy: Path(policy["original_root"]) / "README.md"),
    ("original_python", lambda _policy: Path(sys.executable)),
    ("mempalace_file", lambda policy: Path(policy["original_root"]) / "README.md"),
])
def test_same_checkpoint_original_execution_policy_reseal_never_mints_or_opens_custody(tmp_path,monkeypatch,field,replacement):
    config,custody_path=_formal_custodian_inputs(tmp_path); checkpoint=checkpoint_fixture()
    public=m._load_canonical(Path(config["public_results_path"]),"x"); original_path=Path(public["artifact_files"]["original_public_product"]["artifact_path"]); original=m._load_canonical(original_path,"x")
    for index,runtime in enumerate(original["runtime_receipts"]):
        target=replacement(checkpoint["original_execution_policy"]); runtime[field]=str(target.resolve());
        if field=="original_python": runtime["original_python_sha256"]=hashlib.sha256(target.read_bytes()).hexdigest()
        else: runtime["mempalace_file_sha256"]=hashlib.sha256(target.read_bytes()).hexdigest()
        runtime["runtime_sha256"]=m.digest({key:value for key,value in runtime.items() if key!="runtime_sha256"})
        original["worker_receipts"][index]["runtime_receipt"]=copy.deepcopy(runtime)
    original["artifact_sha256"]=m.digest({key:value for key,value in original.items() if key!="artifact_sha256"}); original_path.write_bytes(m._bytes(original))
    original_ready_path=Path(public["artifact_files"]["original_public_product"]["ready_path"]); ready=m._load_canonical(original_ready_path,"x"); ready["payload_sha256"]=hashlib.sha256(original_path.read_bytes()).hexdigest(); ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); original_ready_path.write_bytes(m._bytes(ready))
    candidate=m._load_canonical(Path(config["candidate_path"]),"x"); threshold=m._load_canonical(Path(config["threshold_protocol_path"]),"x")
    with pytest.raises(m.MemBenchError,match="original_runtime_receipt_invalid"):
        m.freeze_public_results(candidate=candidate,current_files={key:public["artifact_files"][key] for key in m.CURRENT_ROLES},current_worker_receipts=public["current_worker_receipts"],original_file=public["artifact_files"]["original_public_product"],sealed_custody_sha256=public["sealed_custody_sha256"],threshold_protocol=threshold,model_receipt=public["model_receipt"],code_receipt=public["code_receipt"],checkpoint=checkpoint)
    # Even a capability-holder who reseals the public packet and release cannot
    # pass the external policy gate or make the custodian touch custody.
    public["original_artifact_sha256"]=original["artifact_sha256"]; public["ready_receipts"]["original_public_product"]=ready; public["public_results_sha256"]=m.digest({key:value for key,value in public.items() if key!="public_results_sha256"}); Path(config["public_results_path"]).write_bytes(m._bytes(public))
    with pytest.raises(m.MemBenchError,match="original_runtime_receipt_invalid"):
        m.mint_formal_release(candidate_path=Path(config["candidate_path"]),custody_ready_path=Path(config["custody_ready_path"]),public_results_path=Path(config["public_results_path"]),threshold_protocol_path=Path(config["threshold_protocol_path"]),capability_secret_path=Path(config["capability_secret_path"]))
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint); monkeypatch.setattr(m,"_formal_rows",lambda **_kwargs: (_ for _ in ()).throw(AssertionError("scoring reached")))
    observed=[]; real_read=Path.read_bytes
    def tracked(path,*args,**kwargs):
        if path==custody_path: observed.append(path)
        return real_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_bytes",tracked)
    with pytest.raises(m.MemBenchError,match="original_runtime_receipt_invalid"):
        m.run_formal_custodian(config)
    assert observed==[]


@pytest.mark.parametrize("path",[("aggregates","overall","question_macro","static_p5","recall_at_10"),("aggregates","overall","evidence_micro_recall_at_10","static_p5"),("profile_secondary","0","n_rows"),("cross_source_roles","participation_factual","metrics","static_p5","recall_at_10"),("aggregates","overall","p5_minus_original_recall_at_10","ci_lower"),("gate","primary","outcome"),("gate","hard_reflective","outcome")])
def test_completed_report_nested_semantic_tamper_never_opens_custody(tmp_path,monkeypatch,path):
    config,custody_path=_formal_custodian_inputs(tmp_path); monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture()); m.run_formal_custodian(config)
    report_path=Path(config["report_path"]); report=m._load_canonical(report_path,"x"); node=report
    for key in path[:-1]: node=node[key]
    leaf=path[-1]; node[leaf]="TAMPER" if isinstance(node[leaf],str) else node[leaf]+1
    report["report_sha256"]=m.digest({key:value for key,value in report.items() if key not in {"report_sha256","report_hmac"}}); report_path.write_bytes(m._bytes(report)); payload_sha=hashlib.sha256(report_path.read_bytes()).hexdigest()
    ready_path=Path(config["ready_path"]); ready=m._load_canonical(ready_path,"x"); ready["payload_sha256"]=payload_sha; ready["report_sha256"]=report["report_sha256"]; ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); ready_path.write_bytes(m._bytes(ready))
    marker_path=Path(config["consumed_marker_path"]); marker=m._load_canonical(marker_path,"x"); marker["report_sha256"]=payload_sha; marker_path.write_bytes(m._bytes(marker))
    observed=[]; real_read=Path.read_bytes
    def tracked(file,*args,**kwargs):
        if file==custody_path: observed.append(file)
        return real_read(file,*args,**kwargs)
    monkeypatch.setattr(Path,"read_bytes",tracked)
    with pytest.raises(m.MemBenchError,match="custodian_report_invalid"): m.run_formal_custodian(config)
    assert observed==[]


def test_complete_build_bootstrap_draw_is_global_under_strong_build_effect():
    def row(group,size):
        return {"group_id":group,"metrics":{"static_p5":{"recall_at_10":1.0}},"original_replicates":[{"recall_at_10":value} for value in (0.0,0.2,0.4,0.6,0.8)],"size":size}
    # Unequal group sizes make this unlike a trivial single-row fixture; each
    # original build nevertheless affects every row identically in a draw.
    rows=[row("same-tid-profile-0",1),row("same-tid-profile-100",1),row("other-role-same-tid",2)]
    draw,value=m._complete_build_delta(rows,random.Random(20260823))
    assert draw==[3, 1, 2, 0, 2]
    assert value==pytest.approx(1.0-(0.6+0.2+0.4+0.0+0.4)/5)
    # Same role/tid across profiles is one leakage group in the formal builder;
    # the same tid in another source role is deliberately separate.
    candidate,custody=formal_bundle(); matching=[record for record in custody["records"] if record["raw_tid"]=="same-tid"]
    assert len({record["group_id"] for record in matching if record["source_file_role"]=="participation_factual"})==1
    assert len({record["group_id"] for record in matching if record["profile_id"]=="0" and record["source_file_role"] in {"participation_factual","observation_factual"}})==2


def test_evidence_micro_primitives_preserve_raw_hits_for_unequal_denominators():
    first=m._metrics(["a","b"],["a"])
    second=m._metrics([str(number) for number in range(10)],[str(number) for number in range(12)])
    assert (first["gold_hits_at_10"],first["gold_denominator"])==(1.0,1.0)
    assert (second["gold_hits_at_10"],second["gold_denominator"])==(10.0,12.0)
    assert (first["recall_at_10"]+second["recall_at_10"])/2 != pytest.approx((first["gold_hits_at_10"]+second["gold_hits_at_10"])/(first["gold_denominator"]+second["gold_denominator"]))


def test_formal_evidence_micro_uses_integer_counts_and_is_row_order_invariant():
    _candidate, custody=formal_bundle(); custody=copy.deepcopy(custody)
    special=custody["records"][0]; special["gold_candidate_ids"]=[f"g{number}" for number in range(12)]
    rankings=[]
    for record in custody["records"]:
        rankings.append({"item_id":record["item_id"],"ranked_candidate_ids":[f"g{number}" for number in range(10)] if record is special else ["one"]})
        if record is not special: record["gold_candidate_ids"]=["one"]
    artifacts={arm:{"rankings":copy.deepcopy(rankings)} for arm in m.CURRENT_ARMS}
    artifacts["original_public_product"]={"replicates":[{"rankings":copy.deepcopy(rankings)} for _ in range(5)]}
    threshold={"seed":9,"resamples":7,"overall_delta_min":.01,"overall_ci_lower_min":0,"hard_delta_min":0,"hard_ci_lower_min":-.01}
    first=m._formal_rows(artifacts=artifacts,custody=custody,threshold=threshold)
    reversed_custody=copy.deepcopy(custody); reversed_custody["records"].reverse()
    second=m._formal_rows(artifacts=artifacts,custody=reversed_custody,threshold=threshold)
    metric=first["aggregates"]["overall"]
    assert metric["evidence_micro_recall_at_10"]["static_p5"] == pytest.approx((10 + 7) / (12 + 7))
    assert metric["question_macro"]["static_p5"]["recall_at_10"] != metric["evidence_micro_recall_at_10"]["static_p5"]
    assert isinstance(metric["metrics"]["static_p5"]["gold_hits_at_10"],float)
    assert isinstance(m._metrics(["a"],["a"])["gold_hits_at_10"],int)
    assert m._bytes(first)==m._bytes(second)


def test_completed_retry_resealed_semantic_tamper_never_opens_custody(tmp_path,monkeypatch):
    config,custody_path=_formal_custodian_inputs(tmp_path); monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    first=m.run_formal_custodian(config); report_path=Path(config["report_path"]); ready_path=Path(config["ready_path"]); marker_path=Path(config["consumed_marker_path"])
    report=m._load_canonical(report_path,"x"); report["candidate_projection_sha256"]="0"*64; report["report_sha256"]=m.digest({key:value for key,value in report.items() if key!="report_sha256"}); report_path.write_bytes(m._bytes(report)); payload_sha=hashlib.sha256(report_path.read_bytes()).hexdigest()
    ready=m._load_canonical(ready_path,"x"); ready["payload_sha256"]=payload_sha; ready["report_sha256"]=report["report_sha256"]; ready["ready_sha256"]=m.digest({key:value for key,value in ready.items() if key!="ready_sha256"}); ready_path.write_bytes(m._bytes(ready))
    marker=m._load_canonical(marker_path,"x"); marker["report_sha256"]=payload_sha; marker_path.write_bytes(m._bytes(marker))
    observed=[]; real_read=Path.read_bytes
    def tracked(path,*args,**kwargs):
        if path==custody_path: observed.append(path)
        return real_read(path,*args,**kwargs)
    monkeypatch.setattr(Path,"read_bytes",tracked)
    with pytest.raises(m.MemBenchError,match="custodian_report_invalid"):
        m.run_formal_custodian(config)
    assert observed==[] and first["retry_idempotent"] is False


def test_formal_custodian_cli_refuses_unfrozen_checkpoint(tmp_path):
    config, _ = _formal_custodian_inputs(tmp_path)
    result = subprocess.run([sys.executable, "-m", "benchmarks.aerp8_membench", "--formal-custodian"], input=m._bytes(config), cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert result.returncode == 2
    assert b"expected_checkpoint" in result.stderr


def _source_builder_inputs(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True); profiles = []; files=[]; data_root=tmp_path/"external-data"; source_root=Path(__file__).resolve().parents[2]/"Membench"
    for profile_id, inventory in m.FROZEN_PROFILE_INVENTORY.items():
        for role in sorted(m.FORMAL_SOURCE_ROLES):
            relative=inventory["paths"][role]; path=data_root/relative; path.parent.mkdir(parents=True,exist_ok=True); raw=m._bytes(source(tid="same-tid")); path.write_bytes(raw)
            profiles.append({"profile_id":profile_id,"context_label":inventory["context_label"],"source_role":role,"inventory_relative_path":relative})
            files.append({"profile_id":profile_id,"source_role":role,"inventory_relative_path":relative,"source_file_sha256":hashlib.sha256(raw).hexdigest(),"archive_sha256":"a"*64})
    manifest = {"schema": m.SOURCE_MANIFEST_SCHEMA, "official_commit": m.SOURCE_COMMIT, "official_tree": m.SOURCE_TREE, "selected_profiles": ["0","100"], "profiles": profiles}; manifest["manifest_sha256"] = m.digest(manifest)
    manifest_path = tmp_path / "manifest.json"; manifest_path.write_bytes(m._bytes(manifest))
    secret_path = tmp_path / "opacity.secret"; secret_path.write_bytes(b"o" * 32)
    operator_secret_path = tmp_path / "operator-capability.secret"; operator_secret_path.write_bytes(b"k" * 32)
    output = tmp_path / "builder-output"; output.mkdir()
    authorization_path = tmp_path / "authorization.json"
    acquisition={"schema":"aerp8-membench-external-acquisition-v1","manifest_sha256":manifest["manifest_sha256"],"source_checkout":str(source_root),"data_root":str(data_root),"files":files,"authorization_nonce":"fixture-acquisition-authorization-nonce-0001"}; acquisition["acquisition_sha256"]=m.digest(acquisition); acquisition["acquisition_hmac"]=m._opaque(b"k"*32,{key:value for key,value in acquisition.items() if key!="acquisition_hmac"}); acquisition_path=tmp_path/"acquisition.json"; acquisition_path.write_bytes(m._bytes(acquisition))
    checkpoint_path=tmp_path/"expected-checkpoint.json"; checkpoint_path.write_bytes(m._bytes({"fixture":"must-be-monkeypatched"}))
    config = {"schema": m.SOURCE_BUILDER_CONFIG_SCHEMA, "manifest_path": str(manifest_path),"acquisition_receipt_path":str(acquisition_path), "opacity_secret_path": str(secret_path), "operator_capability_secret_path": str(operator_secret_path), "authorization_path": str(authorization_path), "candidate_path": str(output / "candidate.json"), "candidate_ready_path": str(output / "candidate.READY.json"), "custody_path": str(output / "custody.json"), "custody_ready_path": str(output / "custody.READY.json"), "consumed_marker_path": str(output / "consumed.json"),"expected_checkpoint_path":str(checkpoint_path), "code_receipt": m._driver_code_receipt()}
    authorization = {"schema": m.SOURCE_BUILDER_AUTH_SCHEMA, "manifest_sha256": manifest["manifest_sha256"], "code_sha256": config["code_receipt"]["code_sha256"], "authorization_nonce": "fixture-once-authorization-nonce-0001", "expires_at_unix": int(time.time()) + 3600, "output_absent": True, **{key: config[key] for key in ("candidate_path", "candidate_ready_path", "custody_path", "custody_ready_path", "consumed_marker_path")}}
    authorization["authorization_sha256"] = m.digest(authorization); authorization["authorization_hmac"] = m._opaque(b"k" * 32, {key: value for key, value in authorization.items() if key != "authorization_hmac"})
    authorization_path.write_bytes(m._bytes(authorization))
    return config, manifest, profiles


def test_formal_source_builder_profiles_share_group_without_candidate_leakage(tmp_path, monkeypatch):
    config, manifest, _ = _source_builder_inputs(tmp_path)
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    result = m.run_formal_source_builder(config)
    candidate = result["candidate"]; custody = json.loads(Path(config["custody_path"]).read_bytes())
    assert result["read_source_files"] is True and len(candidate["items"]) == 8
    same_role = [row for row in custody["records"] if row["source_file_role"] == "participation_factual"]
    assert len({row["group_id"] for row in same_role}) == 1 and len({row["item_id"] for row in same_role}) == 2
    candidate_text = json.dumps(candidate, sort_keys=True)
    assert all(field not in candidate_text for field in ("source_role", "source_path", "profile_id", "raw_tid", "ground_truth", "target_step_id", "strata"))
    assert candidate["source_receipt"]["source_manifest_sha256"] == manifest["manifest_sha256"]
    monkeypatch.setattr(m, "_build_formal_bundles", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("retry reread source")))
    retry = m.run_formal_source_builder(config)
    assert retry["retry_idempotent"] is True and retry["read_source_files"] is False


def test_formal_source_manifest_and_builder_fail_closed_and_clean_partial_outputs(tmp_path, monkeypatch):
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    config, manifest, profiles = _source_builder_inputs(tmp_path / "hash")
    bad = dict(manifest); bad["profiles"] = [dict(row) for row in profiles]; bad["profiles"][0]["inventory_relative_path"] = "data/other.json"; bad["manifest_sha256"] = m.digest({key: value for key, value in bad.items() if key != "manifest_sha256"}); Path(config["manifest_path"]).write_bytes(m._bytes(bad))
    with pytest.raises(m.MemBenchError, match="source_manifest_invalid"):
        m.run_formal_source_builder(config)
    assert not Path(config["candidate_path"]).exists()
    config, manifest, profiles = _source_builder_inputs(tmp_path / "file-hash")
    acquisition_path=Path(config["acquisition_receipt_path"]); acquisition=json.loads(acquisition_path.read_bytes()); acquisition["files"][0]["source_file_sha256"]="0"*64; unsigned={key:value for key,value in acquisition.items() if key not in {"acquisition_sha256","acquisition_hmac"}}; acquisition["acquisition_sha256"]=m.digest(unsigned); acquisition["acquisition_hmac"]=m._opaque(b"k"*32,{**unsigned,"acquisition_sha256":acquisition["acquisition_sha256"]}); acquisition_path.write_bytes(m._bytes(acquisition))
    with pytest.raises(m.MemBenchError, match="source_file_digest_invalid"):
        m.run_formal_source_builder(config)
    assert not Path(config["candidate_path"]).exists()
    config, _manifest, _ = _source_builder_inputs(tmp_path / "shape")
    acquisition_path=Path(config["acquisition_receipt_path"]); acquisition=json.loads(acquisition_path.read_bytes()); row=acquisition["files"][0]; source_path=Path(acquisition["data_root"])/row["inventory_relative_path"]; raw=b'{"bad":true}'; source_path.write_bytes(raw); row["source_file_sha256"]=hashlib.sha256(raw).hexdigest(); unsigned={key:value for key,value in acquisition.items() if key not in {"acquisition_sha256","acquisition_hmac"}}; acquisition["acquisition_sha256"]=m.digest(unsigned); acquisition["acquisition_hmac"]=m._opaque(b"k"*32,{**unsigned,"acquisition_sha256":acquisition["acquisition_sha256"]}); acquisition_path.write_bytes(m._bytes(acquisition))
    with pytest.raises(m.MemBenchError, match="source_shape_invalid"):
        m.run_formal_source_builder(config)
    assert not any(Path(config[key]).exists() for key in ("candidate_path", "candidate_ready_path", "custody_path", "custody_ready_path", "consumed_marker_path"))
    config, _manifest, _ = _source_builder_inputs(tmp_path / "partial")
    real_publish = m.publish_nonreplace
    def fail_marker(*, path, payload, before_publish=None):
        if path == Path(config["consumed_marker_path"]): raise RuntimeError("injected marker")
        return real_publish(path=path, payload=payload, before_publish=before_publish)
    monkeypatch.setattr(m, "publish_nonreplace", fail_marker)
    with pytest.raises(RuntimeError, match="injected marker"):
        m.run_formal_source_builder(config)
    assert not any(Path(config[key]).exists() for key in ("candidate_path", "candidate_ready_path", "custody_path", "custody_ready_path", "consumed_marker_path"))


def test_formal_source_manifest_requires_complete_role_coverage_and_cli_refusal(tmp_path):
    config, manifest, _ = _source_builder_inputs(tmp_path)
    bad = dict(manifest); bad["profiles"] = list(manifest["profiles"][:-1]); bad["manifest_sha256"] = m.digest({key: value for key, value in bad.items() if key != "manifest_sha256"})
    with pytest.raises(m.MemBenchError, match="coverage_invalid"): m.validate_source_manifest(bad)
    result = subprocess.run([sys.executable, "-m", "benchmarks.aerp8_membench", "--formal-source-builder"], input=m._bytes(config), cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert result.returncode == 2
    assert b"expected_checkpoint" in result.stderr


def test_formal_source_builder_rejects_tampered_expired_or_wrong_operator_capability(tmp_path, monkeypatch):
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    def resign(auth, secret=b"k" * 32):
        unsigned = {key: value for key, value in auth.items() if key not in {"authorization_sha256", "authorization_hmac"}}
        auth["authorization_sha256"] = m.digest(unsigned); auth["authorization_hmac"] = m._opaque(secret, {**unsigned, "authorization_sha256": auth["authorization_sha256"]})
    config, _, _ = _source_builder_inputs(tmp_path / "tampered")
    path = Path(config["authorization_path"]); auth = json.loads(path.read_bytes()); auth["authorization_hmac"] = "0" * 64; path.write_bytes(m._bytes(auth))
    with pytest.raises(m.MemBenchError, match="authorization_invalid"): m.run_formal_source_builder(config)
    config, _, _ = _source_builder_inputs(tmp_path / "expired")
    path = Path(config["authorization_path"]); auth = json.loads(path.read_bytes()); auth["expires_at_unix"] = int(time.time()); resign(auth); path.write_bytes(m._bytes(auth))
    with pytest.raises(m.MemBenchError, match="authorization_invalid"): m.run_formal_source_builder(config)
    config, _, _ = _source_builder_inputs(tmp_path / "short-nonce")
    path = Path(config["authorization_path"]); auth = json.loads(path.read_bytes()); auth["authorization_nonce"] = "short"; resign(auth); path.write_bytes(m._bytes(auth))
    with pytest.raises(m.MemBenchError, match="authorization_invalid"): m.run_formal_source_builder(config)
    config, _, _ = _source_builder_inputs(tmp_path / "wrong-secret")
    Path(config["operator_capability_secret_path"]).write_bytes(b"z" * 32)
    with pytest.raises(m.MemBenchError, match="acquisition_receipt_invalid"): m.run_formal_source_builder(config)


def test_formal_custodian_rejects_tamper_without_custody_open_and_cleans_failure(tmp_path, monkeypatch):
    config, custody_path = _formal_custodian_inputs(tmp_path)
    monkeypatch.setattr(m,"_require_external_current_checkpoint",lambda _path:checkpoint_fixture())
    public_path = Path(config["public_results_path"]); public_path.write_bytes(b"{}")
    real_load = m._load_canonical; observed = []
    def tracked(path, code):
        if path == custody_path: observed.append(path)
        return real_load(path, code)
    monkeypatch.setattr(m, "_load_canonical", tracked)
    with pytest.raises(m.MemBenchError, match="checkpoint_public_mismatch"):
        m.run_formal_custodian(config)
    assert observed == []
    # Re-create the input packet and prove a scoring failure cannot publish any
    # completion-looking output files.
    config, _ = _formal_custodian_inputs(tmp_path / "second")
    real_rows = m._formal_rows
    monkeypatch.setattr(m, "_formal_rows", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError, match="injected"):
        m.run_formal_custodian(config)
    assert not any(Path(config[key]).exists() for key in ("report_path", "ready_path", "consumed_marker_path"))
    # The formal API has no injected seam; this test intercepts the durable
    # publisher after score construction to prove partial report/READY states
    # are removed before the exception escapes.
    config, _ = _formal_custodian_inputs(tmp_path / "third")
    monkeypatch.setattr(m, "_formal_rows", real_rows)
    real_publish = m.publish_nonreplace
    def fail_before_marker(*, path, payload, before_publish=None):
        if path == Path(config["consumed_marker_path"]): raise RuntimeError("marker injected")
        return real_publish(path=path, payload=payload, before_publish=before_publish)
    monkeypatch.setattr(m, "publish_nonreplace", fail_before_marker)
    with pytest.raises(RuntimeError, match="marker injected"):
        m.run_formal_custodian(config)
    assert not any(Path(config[key]).exists() for key in ("report_path", "ready_path", "consumed_marker_path"))


def test_pinned_original_runtime_rejects_untrusted_interpreter(tmp_path):
    git = m._git_capability()
    with pytest.raises(m.MemBenchError, match="runtime_path_invalid"):
        m.pinned_original_runtime_preflight(original_root=tmp_path, original_python=tmp_path / "missing.exe", rpg_root=tmp_path, model_dir=tmp_path, git_executable=Path(git["executable"]), git_sha256=git["sha256"], git_version=git["version"], git_system32_required=git["system32_required"], worker_home_path=tmp_path, execution_policy=original_execution_policy())


def test_git_capability_survives_path_isolation_and_tamper_fails_closed():
    git = m._git_capability()
    env = m._scrubbed_original_env(original_python=Path(sys.executable), rpg_root=Path(__file__).resolve().parents[1], git_executable=Path(git["executable"]), git_system32_required=git["system32_required"], home_dir=Path(__file__).resolve().parents[1])
    assert os.environ.get("PATH") != env["PATH"]
    assert str(Path(git["executable"]).parent) in env["PATH"].split(os.pathsep)
    assert env["HOME"] == str(Path(__file__).resolve().parents[1]) and env["USERPROFILE"] == env["HOME"]
    isolated = subprocess.run([sys.executable, "-c", "from benchmarks import aerp8_membench as m; print(m._driver_code_receipt()['git_capability']['version'])"], cwd=Path(__file__).resolve().parents[1], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True)
    assert isolated.returncode == 0, isolated.stderr
    assert isolated.stdout.strip() == git["version"]
    with pytest.raises(m.MemBenchError, match="git_capability_drift"):
        m._verify_git_capability(executable=Path(git["executable"]), sha256="0" * 64, version=git["version"], system32_required=git["system32_required"])


def test_original_worker_config_rejects_labelled_or_preexisting_output(tmp_path):
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    projection = m.original_normalized_projection(candidate); path = tmp_path / "projection.json"; path.write_text(json.dumps(projection), encoding="utf-8")
    driver = m._driver_code_receipt(); git = driver["git_capability"]; policy=original_execution_policy()
    config = {"schema": m.ORIGINAL_WORKER_CONFIG_SCHEMA, "adapter_id": "aerp8-membench-original-lifecycle-v1", "projection_path": str(path), "projection_sha256": m.original_core.canonical_sha256(projection), "original_root": policy["original_root"], "original_commit": m.ORIGINAL_COMMIT, "original_tree": m.ORIGINAL_TREE, "original_python": policy["original_python"], "model_dir": policy["model_dir"], "model_tree_sha256": m.ORIGINAL_MODEL_TREE, "git_executable": git["executable"], "git_sha256": git["sha256"], "git_version": git["version"], "git_system32_required": git["system32_required"], "worker_home_path": str(tmp_path), "palace_path": str(tmp_path / "palace"), "build_id": "build", "collection_identity": "collection", "draft_path": str(tmp_path / "draft.json"), "ready_path": str(tmp_path / "draft.ready"), "driver_code_receipt": driver, "checkpoint_sha256":"c" * 64,"original_execution_policy":policy,"original_execution_policy_sha256":policy["policy_sha256"], "resource_comparability": "unavailable"}
    config_text = json.dumps(config, sort_keys=True)
    assert all(field not in config_text for field in ("target_step_id", "ground_truth", "choices", "strata", "source_locator", "raw_tid", "source_file_role"))
    assert m.validate_original_worker_config(config)["build_id"] == "build"
    (tmp_path / "draft.json").write_text("present", encoding="utf-8")
    with pytest.raises(m.MemBenchError, match="output_present"):
        m.validate_original_worker_config(config)


def test_formal_worker_rejects_labelled_projection_and_cleans_only_its_partial_outputs(tmp_path, monkeypatch):
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    projection = m.original_normalized_projection(candidate)
    path = tmp_path / "projection.json"; path.write_text(json.dumps(projection), encoding="utf-8")
    driver = m._driver_code_receipt(); git = driver["git_capability"]; policy=original_execution_policy()
    config = {"schema": m.ORIGINAL_WORKER_CONFIG_SCHEMA, "adapter_id": "aerp8-membench-original-lifecycle-v1", "projection_path": str(path), "projection_sha256": m.original_core.canonical_sha256(projection), "original_root": policy["original_root"], "original_commit": m.ORIGINAL_COMMIT, "original_tree": m.ORIGINAL_TREE, "original_python": policy["original_python"], "model_dir": policy["model_dir"], "model_tree_sha256": m.ORIGINAL_MODEL_TREE, "git_executable": git["executable"], "git_sha256": git["sha256"], "git_version": git["version"], "git_system32_required": git["system32_required"], "worker_home_path": str(tmp_path), "palace_path": str(tmp_path / "palace"), "build_id": "build", "collection_identity": "collection", "draft_path": str(tmp_path / "draft.json"), "ready_path": str(tmp_path / "draft.ready"), "driver_code_receipt": driver, "checkpoint_sha256":"c" * 64,"original_execution_policy":policy,"original_execution_policy_sha256":policy["policy_sha256"], "resource_comparability": "unavailable"}
    leaked = copy.deepcopy(projection); leaked["items"][0]["ground_truth"] = "forbidden"; path.write_text(json.dumps(leaked), encoding="utf-8")
    with pytest.raises(m.MemBenchError, match="projection_invalid"):
        m.validate_original_worker_config(config)
    path.write_text(json.dumps(projection), encoding="utf-8")
    @contextmanager
    def fail_after_palace(**_kwargs):
        raise RuntimeError("injected")
        yield None
    monkeypatch.setattr(m, "pinned_original_runtime_preflight", lambda **_kwargs: {"runtime": "test"})
    monkeypatch.setattr(m.original_product, "pinned_live_original_product", fail_after_palace)
    with pytest.raises(RuntimeError, match="injected"):
        m.run_original_worker(config)
    assert not (tmp_path / "palace").exists()
    assert not (tmp_path / "draft.json").exists()
    assert not (tmp_path / "draft.ready").exists()


def test_current_worker_config_is_label_free_and_failure_leaves_no_payload(tmp_path, monkeypatch):
    candidate, _ = m.build_bundles(source_role="role-a", source=source(), opacity_secret=b"o" * 32)
    projection = tmp_path / "candidate.json"; projection.write_bytes(m._bytes(candidate))
    driver = m._driver_code_receipt(); git = driver["git_capability"]; rpg_root = Path(__file__).resolve().parents[1]; rpg_python = m._venv_python(rpg_root)
    config = {"schema": m.CURRENT_WORKER_CONFIG_SCHEMA, "execution_role": "p5_primary", "projection_path": str(projection), "projection_sha256": candidate["projection_sha256"], "rpg_root": str(rpg_root), "rpg_python": str(rpg_python), "model_dir": str(tmp_path), "model_tree_sha256": m.ORIGINAL_MODEL_TREE, "git_executable": git["executable"], "git_sha256": git["sha256"], "git_version": git["version"], "git_system32_required": git["system32_required"], "worker_home_path": str(tmp_path), "artifact_path": str(tmp_path / "artifact.json"), "ready_path": str(tmp_path / "artifact.ready.json"), "driver_code_receipt": driver, "checkpoint_sha256":"c" * 64, "resource_comparability": "unavailable"}
    assert all(field not in json.dumps(config, sort_keys=True) for field in ("target_step_id", "ground_truth", "choices", "strata", "source_locator", "raw_tid", "source_file_role"))
    assert m.validate_current_worker_config(config)["execution_role"] == "p5_primary"
    monkeypatch.setattr(m, "_current_runtime_preflight", lambda **_kwargs: {"runtime_sha256": "r" * 64})
    monkeypatch.setattr(m.original_product.v1, "native_minilm_adapter", lambda _path: (_ for _ in ()).throw(RuntimeError("injected")))
    with pytest.raises(RuntimeError, match="injected"):
        m.run_current_worker(config)
    assert not (tmp_path / "artifact.json").exists()
    assert not (tmp_path / "artifact.ready.json").exists()
