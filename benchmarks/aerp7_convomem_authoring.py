"""Author the immutable, no-secret AERP-7 formal protocol and capabilities."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from benchmarks import aerp7_convomem_executor as executor
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks import aerp7_convomem_formal as formal
from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp_execution_checkpoint as checkpoint
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256
from benchmarks.aerp5_product_paired_locomo import git_state


PLAN_SCHEMA = "aerp7-convomem-one-shot-plan-v1"
CENSUS_DATASET_SOURCE = {
    "repository": "SalesforceAIResearch/ConvoMem",
    "commit": "e3e9b39115b02346824c70d349350de738f8be41",
    "tree": "ca6c89ff4c3094b721b7eeae6b0e39da68cde24d",
}
CENSUS_SOURCE_MANIFEST = {
    "upstream": CENSUS_DATASET_SOURCE,
    "file_count": 2067,
    "byte_count": 26894940265,
    "inventory_sha256": "5af1c9fab4a267859342abfb03469fe8406e05b7ab04610a548791d3ea92a3ab",
}
CENSUS_SEMANTICS = {
    "selection_algorithm": confirmation.CENSUS_SELECTION_ALGORITHM, "selection_seed": None,
    "candidate_projection_transport": formal.CANDIDATE_TRANSPORT,
    "persona_quota": "ALL", "per_persona_group_quota": "ALL",
    "context_rank_indices": "ALL_AVAILABLE_SORTED", "source_receipt": rank.PROTOCOL_SOURCE,
    "primary_current_arm": {"arm_id": "six_view_secondary", "config_sha256": formal._digest(rank._arm_method("six_view_secondary")), "ranker_code_sha256": formal._ranker_code_sha256()},
    "arms": list(formal.score.FORMAL_ARMS), "serializer_contract": {"current": rank.CURRENT_SERIALIZER, "original_public_product": rank.ORIGINAL_MEMPALACE_SERIALIZER},
    "top_k": 10, "tie_break": "stable_ranking_key_ascending", "original_build_count": 5,
    "p5_repeat_required": True, "bootstrap": formal.score.FORMAL_BOOTSTRAP,
    "gates": {"primary_delta_min": .01, "primary_ci_lower_gt_zero": 0.0},
    "resource_thresholds": {"resource_comparability": "unavailable", "peak_rss_bytes_max": None, "storage_bytes_max": None, "ingest_seconds_max": None, "index_seconds_max": None, "query_p95_ns_max": None},
}


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def clean_code_receipt(repo_root: Path) -> dict[str, Any]:
    state = git_state(repo_root)
    if state["git_dirty"]:
        raise CustodyError("aerp7_authoring_dirty_worktree")
    return {"head": state["git_head"], "tree": state["git_tree"], "diff_digest": state["worktree_diff_sha256"], "dirty_policy": "clean_required"}


def clean_code_receipt_shape(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != {"head", "tree", "diff_digest", "dirty_policy"} or value.get("dirty_policy") != "clean_required":
        return None
    row = dict(value)
    try:
        rank._git_object_id(row.get("head"), "aerp7_one_shot_plan_invalid")
        rank._git_object_id(row.get("tree"), "aerp7_one_shot_plan_invalid")
        rank._hex(row.get("diff_digest"), "aerp7_one_shot_plan_invalid")
    except CustodyError:
        return None
    return row


def observe_source_manifest(*, canonical_root: Path, premix_root: Path, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Hash the official source bytes without parsing payload JSON.

    The signed manifest is the only authority.  This routine rejects arbitrary
    roots before the builder gets a parser capability and is repeated after it
    returns to make a source TOCTOU abort the run rather than alter its census.
    """
    if dict(expected) != CENSUS_SOURCE_MANIFEST:
        raise CustodyError("aerp7_source_manifest_not_preregistered")
    canonical_dir = confirmation._subroot(canonical_root, "evidence_questions")
    premix_dir = confirmation._subroot(premix_root, "pre_mixed_testcases")
    rows: list[dict[str, Any]] = []
    for role, root in (("canonical", canonical_dir), ("premix", premix_dir)):
        for path in confirmation._files(root):
            before = path.stat()
            raw = path.read_bytes()
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise CustodyError("aerp7_source_inventory_toctou")
            rows.append({"role": role, "locator": path.relative_to(root).as_posix(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    rows.sort(key=lambda item: (item["role"], item["locator"]))
    try:
        canonical_git, premix_git = git_state(canonical_root), git_state(premix_root)
    except Exception as exc:
        raise CustodyError("aerp7_source_git_observation_invalid") from exc
    upstream = expected["upstream"]
    for state in (canonical_git, premix_git):
        if state.get("git_dirty") or state.get("git_head") != upstream["commit"] or state.get("git_tree") != upstream["tree"]:
            raise CustodyError("aerp7_source_git_manifest_mismatch")
    observed = {"upstream": upstream, "file_count": len(rows), "byte_count": sum(row["bytes"] for row in rows), "inventory_sha256": _digest(rows)}
    if observed != dict(expected):
        raise CustodyError("aerp7_source_inventory_manifest_mismatch")
    return observed


def author_formal_protocol(*, repo_root: Path, candidate_receipt: Mapping[str, Any], model_receipt: Mapping[str, Any], expected_checkpoint_path: Path,
                           preparse_semantics: Mapping[str, Any], preparse_current_code_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Freeze the only primary arm before any ranking artifact can exist."""
    if dict(preparse_semantics) != CENSUS_SEMANTICS:
        raise CustodyError("aerp7_authoring_preparse_semantics_invalid")
    code = clean_code_receipt(repo_root)
    if code != dict(preparse_current_code_receipt):
        raise CustodyError("aerp7_authoring_preparse_code_drift")
    binding = checkpoint.capture_binding(expected_checkpoint_path=expected_checkpoint_path, current_code_receipt=code)
    # AERP-8's immutable policy is the live, externally reviewed source of
    # truth for the original product.  The AERP-7 orchestration receipt must
    # never pretend that its own repository revision is the original build.
    original_policy = binding["original_execution_policy"]
    original_code = {
        "head": original_policy["original_commit"],
        "tree": original_policy["original_tree"],
        "diff_digest": binding["original_execution_policy_sha256"],
        "dirty_policy": "clean_required",
    }
    receipt = formal._candidate_receipt(candidate_receipt)
    row = {
        "schema": formal.FORMAL_PROTOCOL_SCHEMA, "synthetic_test_mode": False,
        "candidate": receipt, "current_code_receipt": code, "original_code_receipt": original_code,
        "execution_checkpoint": binding, "source_receipt": rank.PROTOCOL_SOURCE,
        "model_receipt": dict(model_receipt), "candidate_transport": dict(preparse_semantics["candidate_projection_transport"]), "arms": list(formal.score.FORMAL_ARMS),
        "primary_current_arm": dict(preparse_semantics["primary_current_arm"]),
        "serializer_contract": {"current": rank.CURRENT_SERIALIZER, "original_public_product": rank.ORIGINAL_MEMPALACE_SERIALIZER},
        "top_k": 10, "tie_break": "stable_ranking_key_ascending", "original_build_count": 5,
        "p5_repeat_required": True, "bootstrap": formal.score.FORMAL_BOOTSTRAP,
        "gates": {"primary_delta_min": .01, "primary_ci_lower_gt_zero": 0.0},
        "resource_thresholds": {"resource_comparability": "unavailable", "peak_rss_bytes_max": None, "storage_bytes_max": None, "ingest_seconds_max": None, "index_seconds_max": None, "query_p95_ns_max": None},
    }
    row["protocol_sha256"] = formal.protocol_digest(row)
    return formal.validate_formal_protocol(row)


def sign_operator_authorization(*, protocol: Mapping[str, Any], output_dir: Path, capability: bytes, nonce: str, expires_at_unix: int) -> dict[str, Any]:
    frozen = formal.validate_formal_protocol(protocol)
    if not isinstance(nonce, str) or len(nonce.encode("utf-8")) < 32 or isinstance(expires_at_unix, bool) or not isinstance(expires_at_unix, int):
        raise CustodyError("aerp7_authoring_authorization_inputs_invalid")
    unsigned = {"schema": executor.FORMAL_AUTH_SCHEMA, "mode": "formal_live", "synthetic_test_mode": False, "protocol_sha256": frozen["protocol_sha256"], "executor_code_receipt": frozen["current_code_receipt"], "output_dir": str(output_dir.resolve()), "nonce": nonce, "expires_at_unix": expires_at_unix, "output_absent": True}
    digest = executor._digest(unsigned)
    return {**unsigned, "authorization_sha256": digest, "operator_hmac": hmac.new(capability, executor._bytes({**unsigned, "authorization_sha256": digest}), hashlib.sha256).hexdigest()}


def author_private_disk_preflight(*, plan: Mapping[str, Any], candidate_reference: Mapping[str, Any],
                                  custody_reference: Mapping[str, Any], operator_capability: bytes) -> dict[str, Any]:
    """Issue the coordinator-private capacity contract after generation publication.

    The custody reference cannot be known while the pre-parse plan is authored.
    Its capacity calibration is nevertheless signed in that plan, and this
    second signed receipt binds it to the exact published generation before any
    public rank worker is launched.
    """
    signed_plan = validate_one_shot_plan(plan, operator_capability=operator_capability)
    candidate = rank.validate_candidate_projection_reference(candidate_reference)
    custody = dict(custody_reference)
    required_custody = {
        "schema", "bundle_path", "candidate_reference", "custody_path", "ready_path", "generation_id",
        "custody_raw_sha256", "custody_canonical_sha256", "dataset", "item_count", "evidence_span_count", "ready_sha256",
    }
    if set(custody) != required_custody or custody.get("candidate_reference") != candidate:
        raise CustodyError("private_disk_preflight_custody_reference_invalid")
    calibration = formal.validate_private_disk_calibration(signed_plan["disk_preflight_calibration"])
    candidate_path, custody_path = Path(candidate["bundle_path"]) / "projection.json", Path(custody["custody_path"])
    try:
        candidate_input, custody_input = candidate_path.stat().st_size, custody_path.stat().st_size
    except OSError as exc:
        raise CustodyError("private_disk_preflight_input_unavailable") from exc
    row = {
        "schema": formal.PRIVATE_DISK_PREFLIGHT_SCHEMA, "plan_sha256": signed_plan["plan_sha256"],
        "calibration": calibration, "custody_reference_sha256": _digest(custody),
        "generation_id": candidate["generation_id"], "candidate_input_bytes": candidate_input,
        "custody_input_bytes": custody_input, "custody_logical_item_count": custody["item_count"],
        "custody_evidence_span_count": custody["evidence_span_count"],
        # This is a signed calibration, not an unobserved zero/default.  The
        # executor later validates its complete input+SQLite peak arithmetic.
        "custody_sqlite_store_bytes": calibration["custody_sqlite_store_bytes"],
        "custody_peak_disk_input_and_store_bytes": candidate_input + custody_input + calibration["custody_sqlite_store_bytes"],
    }
    row["preflight_sha256"] = formal.private_disk_preflight_digest(row)
    row["preflight_hmac"] = hmac.new(operator_capability, _bytes({key: item for key, item in row.items() if key != "preflight_hmac"}), hashlib.sha256).hexdigest()
    return validate_private_disk_preflight(row, operator_capability=operator_capability, expected_plan_sha256=signed_plan["plan_sha256"])


def validate_private_disk_preflight(value: Any, *, operator_capability: bytes,
                                    expected_plan_sha256: str | None = None) -> dict[str, Any]:
    row = formal.validate_private_disk_preflight(value)
    if expected_plan_sha256 is not None and row["plan_sha256"] != expected_plan_sha256:
        raise CustodyError("private_disk_preflight_plan_binding_invalid")
    expected = hmac.new(operator_capability, _bytes({key: item for key, item in row.items() if key != "preflight_hmac"}), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(row["preflight_hmac"], expected):
        raise CustodyError("private_disk_preflight_hmac_invalid")
    return row


def sign_one_shot_plan(value: Mapping[str, Any], *, operator_capability: bytes) -> dict[str, Any]:
    """Authenticate an operator's pre-parse plan; secrets stay out of the plan."""
    row = dict(value)
    if row.get("schema") != PLAN_SCHEMA or "plan_sha256" in row or "plan_hmac" in row:
        raise CustodyError("aerp7_one_shot_plan_invalid")
    row["plan_sha256"] = _digest(row)
    row["plan_hmac"] = hmac.new(operator_capability, _bytes({key: item for key, item in row.items() if key != "plan_hmac"}), hashlib.sha256).hexdigest()
    return row


def validate_one_shot_plan(value: Any, *, operator_capability: bytes) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise CustodyError("aerp7_one_shot_plan_invalid")
    row = dict(value)
    required = {"schema", "canonical_root", "premix_root", "candidate_output_dir", "custody_output_dir", "staging_root", "protocol_path", "authorization_path", "output_dir", "custodian_public_config_path", "final_output_path", "one_shot_receipt_path", "infrastructure_failure_receipt_path", "progress_receipt_path", "expected_checkpoint_path", "original_root", "model_dir", "python_executable", "original_python", "source_manifest", "model_receipt", "census_semantics", "preparse_current_code_receipt", "disk_preflight_calibration", "public_authorization_nonce", "custodian_nonce", "custodian_expires_at_unix", "plan_sha256", "plan_hmac"}
    if set(row) != required or row.get("schema") != PLAN_SCHEMA: raise CustodyError("aerp7_one_shot_plan_invalid")
    path_keys = required - {"schema", "plan_sha256", "plan_hmac", "custodian_nonce", "public_authorization_nonce", "custodian_expires_at_unix", "source_manifest", "model_receipt", "census_semantics", "preparse_current_code_receipt", "disk_preflight_calibration"}
    if any(not isinstance(row[key], str) or not Path(row[key]).is_absolute() for key in path_keys): raise CustodyError("aerp7_one_shot_plan_invalid")
    paths: dict[str, set[str]] = {}
    for key in path_keys:
        paths.setdefault(str(Path(row[key])), set()).add(key)
    if any(keys != {"canonical_root", "premix_root"} for keys in paths.values() if len(keys) > 1):
        raise CustodyError("aerp7_one_shot_plan_path_collision")
    if any(not isinstance(row[key], str) or len(row[key].encode("utf-8")) < 32 for key in ("custodian_nonce", "public_authorization_nonce")) or isinstance(row["custodian_expires_at_unix"], bool) or not isinstance(row["custodian_expires_at_unix"], int): raise CustodyError("aerp7_one_shot_plan_invalid")
    if row.get("source_manifest") != CENSUS_SOURCE_MANIFEST or row.get("census_semantics") != CENSUS_SEMANTICS:
        raise CustodyError("aerp7_one_shot_preparse_freeze_invalid")
    formal._model_receipt(row.get("model_receipt"))
    calibration = formal.validate_private_disk_calibration(row.get("disk_preflight_calibration"))
    if calibration["source_manifest_sha256"] != _digest(CENSUS_SOURCE_MANIFEST) or calibration["model_receipt_sha256"] != _digest(row["model_receipt"]):
        raise CustodyError("private_disk_calibration_identity_drift")
    if clean_code_receipt_shape(row.get("preparse_current_code_receipt")) is None: raise CustodyError("aerp7_one_shot_plan_invalid")
    if row["plan_sha256"] != _digest({key: item for key, item in row.items() if key not in {"plan_sha256", "plan_hmac"}}): raise CustodyError("aerp7_one_shot_plan_digest_invalid")
    expected = hmac.new(operator_capability, _bytes({key: item for key, item in row.items() if key != "plan_hmac"}), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(str(row["plan_hmac"]), expected): raise CustodyError("aerp7_one_shot_plan_hmac_invalid")
    return row
