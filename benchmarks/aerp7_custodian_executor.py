"""Synthetic-only isolated custodian/scorer subprocess for AERP-7.

The coordinator and all rank workers produce a public freeze packet without a
custody capability.  This module is deliberately the *only* process which can
accept the four private capabilities needed to open the sealed confirmation
bundle and score it.  The capabilities arrive once on stdin; they are neither
accepted in argv/environment/configuration nor written to a receipt.

``FORMAL_CUSTODIAN_ENABLED`` is intentionally false.  This module is an E2E
rehearsal seam, not permission to inspect official ConvoMem data.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks import aerp7_convomem_executor as executor
from benchmarks import aerp7_convomem_formal as formal
from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_scoring as score
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256


SCHEMA = "aerp7-convomem-custodian-executor-v1"
PUBLIC_CONFIG_SCHEMA = "aerp7-convomem-custodian-public-config-v1"
PRIVATE_SCHEMA = "aerp7-convomem-custodian-private-capability-v1"
PACKET_SCHEMA = "aerp7-convomem-custodian-packet-v1"
GATE_SCHEMA = "aerp7-convomem-scientific-gate-decision-v1"
OUTER_ATTESTATION_SCHEMA = "aerp7-convomem-custodian-post-score-attestation-v1"
FORMAL_CUSTODIAN_ENABLED = False
SYNTHETIC_CUSTODIAN_ONLY = True

PUBLIC_CONFIG_KEYS = frozenset({
    "schema", "synthetic_test_mode", "public_freeze_packet", "candidate_bundle",
    "custody_bundle", "output_path", "freeze_packet_file_sha256",
    "candidate_ready_sha256", "custody_ready_sha256", "custody_bundle_sha256",
})
PRIVATE_KEYS = frozenset({
    "schema", "binding_secret", "custody_capability_secret", "evidence_token_secret",
    "scorer_attestation_secret", "public_packet_sha256", "freeze_packet_file_sha256",
    "output_path", "nonce", "expires_at_unix", "authorization_sha256", "authorization_hmac",
})
SUPERVISOR_KEYS = frozenset({
    "pid", "exit_code", "command_sha256", "environment_keys_sha256", "cwd_sha256",
    "observed_process_tree_peak_rss_bytes", "output_sha256", "packet_sha256",
})
EXPECTED_SUPERVISORS = frozenset({
    "current-raw", "current-p5_primary", "current-p5_repeat", "current-six",
    "original-0", "original-1", "original-2", "original-3", "original-4",
})
_UNSAFE_PUBLIC_FIELDS = frozenset({
    "answer", "answers", "evidence_spans", "evidence_conversation_ids", "message_id",
    "message_ids", "ranked_message_ids", "replicate_ranked_message_ids", "text",
    "message_evidences", "canonical_item_id", "source_path", "source_locator",
})


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _load_public(path: Path, *, code: str) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise CustodyError(code)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CustodyError(code) from exc
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return dict(value)


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CustodyError(code)
    try:
        int(value, 16)
    except ValueError as exc:
        raise CustodyError(code) from exc
    return value


def _secret(value: Any, code: str) -> bytes:
    if not isinstance(value, str):
        raise CustodyError(code)
    encoded = value.encode("utf-8")
    if len(encoded) < 32:
        raise CustodyError(code)
    return encoded


def live_custodian_code_receipt() -> dict[str, Any]:
    """Receipt binds this scorer process, not merely the public executor."""
    state = executor.live_executor_code_receipt()
    return {
        "executor_git": state,
        "module_sha256": _file_sha256(Path(__file__)),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
    }


def _private_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in {"authorization_sha256", "authorization_hmac"}}


def _private_hmac(value: Mapping[str, Any], *, secret: bytes) -> str:
    unsigned = _private_unsigned(value)
    return hmac.new(secret, _bytes({**unsigned, "authorization_sha256": _digest(unsigned)}), hashlib.sha256).hexdigest()


def _authorization_id(value: Mapping[str, Any]) -> str:
    return _digest({key: value[key] for key in ("public_packet_sha256", "freeze_packet_file_sha256", "output_path", "nonce", "expires_at_unix")})


def sign_private_payload(value: Mapping[str, Any], *, custody_capability_secret: bytes) -> dict[str, Any]:
    """Test/operator helper; this returns a stdin-only exact-once capability."""
    row = dict(value)
    row["authorization_sha256"] = _digest(_private_unsigned(row))
    row["authorization_hmac"] = _private_hmac(row, secret=custody_capability_secret)
    return row


def _private(value: Any, *, packet_sha256: str, file_sha256: str, output_path: Path) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != PRIVATE_KEYS or value.get("schema") != PRIVATE_SCHEMA:
        raise CustodyError("custodian_private_capability_invalid")
    row = dict(value)
    result: dict[str, Any] = {name: _secret(row[name], "custodian_private_capability_invalid") for name in (
        "binding_secret", "custody_capability_secret", "evidence_token_secret", "scorer_attestation_secret",
    )}
    if row.get("public_packet_sha256") != packet_sha256 or row.get("freeze_packet_file_sha256") != file_sha256 or row.get("output_path") != str(output_path.resolve()):
        raise CustodyError("custodian_private_public_binding_invalid")
    if not isinstance(row.get("nonce"), str) or len(row["nonce"].encode("utf-8")) < 32 or isinstance(row.get("expires_at_unix"), bool) or not isinstance(row.get("expires_at_unix"), int) or row["expires_at_unix"] <= int(time.time()):
        raise CustodyError("custodian_private_capability_expired")
    unsigned = _private_unsigned(row)
    if row.get("authorization_sha256") != _digest(unsigned):
        raise CustodyError("custodian_private_capability_digest_invalid")
    expected = _private_hmac(row, secret=result["custody_capability_secret"])
    if not isinstance(row["authorization_hmac"], str) or not hmac.compare_digest(row["authorization_hmac"], expected):
        raise CustodyError("custodian_private_capability_hmac_invalid")
    result["authorization_id"] = _authorization_id(row)
    return result


def _public_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != PUBLIC_CONFIG_KEYS or value.get("schema") != PUBLIC_CONFIG_SCHEMA:
        raise CustodyError("custodian_public_config_invalid")
    row = dict(value)
    if row.get("synthetic_test_mode") is not True or not SYNTHETIC_CUSTODIAN_ONLY or FORMAL_CUSTODIAN_ENABLED:
        raise CustodyError("custodian_formal_execution_blocked")
    for key in ("public_freeze_packet", "candidate_bundle", "custody_bundle", "output_path"):
        if not isinstance(row.get(key), str) or not row[key]:
            raise CustodyError("custodian_public_config_invalid")
    for key in ("freeze_packet_file_sha256", "candidate_ready_sha256", "custody_ready_sha256", "custody_bundle_sha256"):
        _hex(row.get(key), "custodian_public_config_digest_invalid")
    return row


def _worker_config(protocol: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "role": "candidate_ranker",
        "projection_sha256": protocol["candidate"]["projection_canonical_sha256"],
        "projection_raw_sha256": protocol["candidate"]["projection_raw_sha256"],
        "projection_path": "projection.json",
        "model_receipt": protocol["model_receipt"],
        "code_receipt": protocol["current_code_receipt"],
        "staging_root": "staging",
        "arms": ["strong_raw", "static_p5", "six_view_secondary"],
        "top_k": 10,
        "tie_break": "stable_ranking_key_ascending",
        "serializer_contract": protocol["serializer_contract"],
    }


def _validate_supervisors(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != EXPECTED_SUPERVISORS:
        raise CustodyError("custodian_supervisor_coverage_invalid")
    rows = {str(key): dict(item) if isinstance(item, Mapping) else None for key, item in value.items()}
    if any(row is None or set(row) != SUPERVISOR_KEYS for row in rows.values()):
        raise CustodyError("custodian_supervisor_receipt_invalid")
    pids: set[int] = set()
    for row in rows.values():
        assert row is not None
        if isinstance(row["pid"], bool) or not isinstance(row["pid"], int) or row["pid"] <= 0 or row["exit_code"] != 0:
            raise CustodyError("custodian_supervisor_receipt_invalid")
        pids.add(row["pid"])
        for key in ("command_sha256", "environment_keys_sha256", "cwd_sha256", "output_sha256", "packet_sha256"):
            _hex(row[key], "custodian_supervisor_receipt_invalid")
        if isinstance(row["observed_process_tree_peak_rss_bytes"], bool) or not isinstance(row["observed_process_tree_peak_rss_bytes"], int) or row["observed_process_tree_peak_rss_bytes"] <= 0:
            raise CustodyError("custodian_supervisor_receipt_invalid")
    if len(pids) != len(rows):
        raise CustodyError("custodian_supervisor_pid_collision")
    return rows


def validate_public_freeze(config: Mapping[str, Any]) -> dict[str, Any]:
    """Complete label-free validation.  This must run before any custody read."""
    cfg = _public_config(config)
    freeze_path = Path(cfg["public_freeze_packet"])
    if _file_sha256(freeze_path) != cfg["freeze_packet_file_sha256"]:
        raise CustodyError("custodian_freeze_packet_file_digest_invalid")
    packet = _load_public(freeze_path, code="custodian_freeze_packet_invalid")
    required = {
        "schema", "synthetic_test_mode", "formal_eligible", "authorization_sha256", "protocol",
        "projection_sha256", "current_worker_receipt", "ranking_artifacts", "endpoint_manifest",
        "resource_receipts", "supervisors", "packet_sha256",
    }
    if set(packet) != required or packet.get("schema") != executor.FREEZE_PACKET_SCHEMA or packet.get("synthetic_test_mode") is not True or packet.get("formal_eligible") is not False:
        raise CustodyError("custodian_freeze_packet_invalid")
    if packet.get("packet_sha256") != _digest({key: item for key, item in packet.items() if key != "packet_sha256"}):
        raise CustodyError("custodian_freeze_packet_digest_invalid")
    _hex(packet["packet_sha256"], "custodian_freeze_packet_digest_invalid")
    _hex(packet["authorization_sha256"], "custodian_freeze_packet_invalid")
    protocol = formal.validate_formal_protocol(packet["protocol"])
    candidate_root = Path(cfg["candidate_bundle"])
    staging_parent = Path(tempfile.mkdtemp(prefix="aerp7-custodian-public-"))
    (staging_parent / "staging").mkdir()
    try:
        projection = formal.load_candidate_worker_projection(
            worker_config=_worker_config(protocol), protocol=protocol,
            candidate_bundle_root=candidate_root, staging_parent=staging_parent,
        )
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)
    if canonical_sha256(projection) != packet["projection_sha256"] or canonical_sha256(projection) != protocol["candidate"]["projection_canonical_sha256"]:
        raise CustodyError("custodian_projection_packet_binding_invalid")
    if protocol["candidate"]["ready_sha256"] != cfg["candidate_ready_sha256"]:
        raise CustodyError("custodian_candidate_ready_public_binding_invalid")
    current = formal.validate_current_worker_receipt(packet["current_worker_receipt"], projection=projection)
    artifacts = [rank.validate_frozen_ranking(item, projection=projection) for item in packet["ranking_artifacts"]]
    if tuple(item["arm_id"] for item in artifacts) != score.FORMAL_ARMS:
        raise CustodyError("custodian_ranking_arm_coverage_invalid")
    endpoint = score.validate_endpoint_manifest(packet["endpoint_manifest"], projection_sha256=canonical_sha256(projection))
    artifact_digests = {item["arm_id"]: item["artifact_sha256"] for item in artifacts}
    if {item["arm_id"]: item["ranking_artifact_sha256"] for item in endpoint["arms"]} != artifact_digests or current["artifact_sha256"] != {arm: artifact_digests[arm] for arm in ("strong_raw", "static_p5", "six_view_secondary")}:
        raise CustodyError("custodian_endpoint_artifact_binding_invalid")
    resources = list(packet["resource_receipts"])
    expected = formal.projection_denominators(projection)
    for receipt in resources:
        if not isinstance(receipt, Mapping):
            raise CustodyError("custodian_resource_receipt_invalid")
        formal.validate_resource_receipt(receipt, arm_id=receipt.get("arm_id"), thresholds=protocol["resource_thresholds"], expected_denominators=expected)
    # Release validation also verifies every resource/replicate cross-binding;
    # this local coverage check catches incomplete public packets before custody.
    by_arm: dict[str, list[Mapping[str, Any]]] = {}
    for receipt in resources:
        by_arm.setdefault(str(receipt["arm_id"]), []).append(receipt)
    if set(by_arm) != set(score.FORMAL_ARMS) or len(by_arm["original_public_product"]) != 5 or len(by_arm["strong_raw"]) != 1 or len(by_arm["six_view_secondary"]) != 1 or len(by_arm["static_p5"]) != 2:
        raise CustodyError("custodian_resource_coverage_invalid")
    supervisors = _validate_supervisors(packet["supervisors"])
    return {"config": cfg, "packet": packet, "protocol": protocol, "projection": projection, "current_worker_receipt": current, "ranking_artifacts": artifacts, "endpoint_manifest": endpoint, "resource_receipts": resources, "supervisors": supervisors}


def _resource_map(resources: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_arm: dict[str, list[Mapping[str, Any]]] = {}
    for row in resources:
        by_arm.setdefault(str(row["arm_id"]), []).append(row)
    p5 = {str(row["execution_role"]): str(row["resource_sha256"]) for row in by_arm["static_p5"]}
    return {
        "original_public_product": [str(row["resource_sha256"]) for row in sorted(by_arm["original_public_product"], key=lambda item: str(item["build_id"]))],
        "strong_raw": str(by_arm["strong_raw"][0]["resource_sha256"]),
        "static_p5": {role: p5[role] for role in ("primary", "repeat")},
        "six_view_secondary": str(by_arm["six_view_secondary"][0]["resource_sha256"]),
    }


def _release(*, public: Mapping[str, Any], custody_ready_sha256: str, custody_bundle_sha256: str, capability: bytes) -> dict[str, Any]:
    protocol, endpoint = public["protocol"], public["endpoint_manifest"]
    resources, artifacts = public["resource_receipts"], public["ranking_artifacts"]
    originals = next(item for item in artifacts if item["arm_id"] == "original_public_product")["replicates"]
    unsigned = {
        "schema": formal.RELEASE_SCHEMA,
        "protocol_sha256": protocol["protocol_sha256"],
        "endpoint_manifest_sha256": endpoint["manifest_sha256"],
        "candidate_ready_sha256": protocol["candidate"]["ready_sha256"],
        "projection_raw_sha256": protocol["candidate"]["projection_raw_sha256"],
        "projection_canonical_sha256": protocol["candidate"]["projection_canonical_sha256"],
        "custody_ready_sha256": custody_ready_sha256,
        "custody_bundle_sha256": custody_bundle_sha256,
        "ranking_artifact_sha256": {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in endpoint["arms"]},
        "resource_sha256": _resource_map(resources),
        "original_build_index_sha256": {item["build_id"]: item["index_sha256"] for item in originals},
        "current_worker_sha256": public["current_worker_receipt"]["worker_sha256"],
    }
    return formal.sign_release_authorization(unsigned, custody_capability_secret=capability)


def scientific_gate_decision(report: Mapping[str, Any], protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Mechanical policy evaluation; a scientific failure is published as FAIL."""
    checked, frozen = score.validate_report(report), formal.validate_formal_protocol(protocol)
    gates = frozen["gates"]
    bootstrap = checked["paired_bootstrap"]
    overall = bootstrap["overall_positive"]["paired_deltas"]["static_p5"]["vs_original_public_product"]
    hard = bootstrap["derived_hard_changing_and_implicit"]["paired_deltas"]["static_p5"]["vs_original_public_product"]
    abstention = bootstrap["static_p5_vs_strong_raw_abstention_confidence"]["metrics"]
    checks = {
        "overall_delta_min": float(overall["estimate"]) >= float(gates["overall_delta_min"]),
        "overall_ci_lower_gt_zero": float(overall["ci_lower"]) > float(gates["overall_ci_lower_gt_zero"]),
        "hard_delta_min": float(hard["estimate"]) >= float(gates["hard_delta_min"]),
        "hard_ci_lower_min": float(hard["ci_lower"]) >= float(gates["hard_ci_lower_min"]),
        "abstention_auroc_ci_lower_min": float(abstention["auroc"]["ci_lower"]) >= float(gates["abstention_ci_lower_min"]),
        "abstention_average_precision_ci_lower_min": float(abstention["average_precision"]["ci_lower"]) >= float(gates["abstention_ci_lower_min"]),
    }
    # Guardrails are strict public report invariants plus complete resolution of
    # positive evidence.  Abstention rows legitimately carry zero evidence.
    positive = [arm["positive"]["overall"]["question_macro"] for arm in checked["arms"].values()]
    guardrails_ok = all(item["unresolved_evidence_item_count"] == 0 for item in positive)
    if gates["guardrails_required"]:
        checks["guardrails"] = guardrails_ok
    decision = {
        "schema": GATE_SCHEMA,
        "report_sha256": checked["report_sha256"],
        "protocol_sha256": frozen["protocol_sha256"],
        "reference_arm": "original_public_product",
        "candidate_arm": "static_p5",
        "measurements": {"overall": overall, "derived_hard": hard, "abstention_confidence": abstention},
        "checks": checks,
        "outcome": "PASS" if all(checks.values()) else "FAIL",
    }
    decision["decision_sha256"] = _digest(decision)
    return decision


def _outer_attestation(*, envelope: Mapping[str, Any], decision: Mapping[str, Any], scorer_before: Mapping[str, Any], scorer_after: Mapping[str, Any], public_packet_sha256: str, secret: bytes) -> dict[str, Any]:
    unsigned = {
        "schema": OUTER_ATTESTATION_SCHEMA,
        "envelope_sha256": envelope["envelope_sha256"],
        "gate_decision_sha256": decision["decision_sha256"],
        "scorer_code_before_sha256": _digest(scorer_before),
        "scorer_code_after_sha256": _digest(scorer_after),
        "public_packet_sha256": public_packet_sha256,
    }
    return {**unsigned, "attestation_hmac": hmac.new(secret, _bytes(unsigned), hashlib.sha256).hexdigest()}


def _scan_public(value: Any, *, private_values: Sequence[bytes]) -> None:
    """Reject public fields/text which would turn a science packet into a leak."""
    score.validate_report(value["envelope"]["report"])
    def walk(item: Any) -> None:
        if isinstance(item, Mapping):
            if _UNSAFE_PUBLIC_FIELDS & set(item):
                raise CustodyError("custodian_public_packet_leakage")
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            raw = item.encode("utf-8")
            if any(secret in raw for secret in private_values):
                raise CustodyError("custodian_public_packet_secret_leakage")
    walk(value)


def _authorization_paths(output: Path, authorization_id: str) -> tuple[Path, Path]:
    _hex(authorization_id, "custodian_authorization_id_invalid")
    return (
        output.with_name(f".{output.name}.aerp7-lock-{authorization_id}.json"),
        output.with_name(f".{output.name}.aerp7-consumed-{authorization_id}.json"),
    )


def _validated_existing_result(*, output: Path, authorization_id: str, packet_sha256: str, file_sha256: str) -> dict[str, Any] | None:
    """Authenticate an existing successful publication without opening custody."""
    if not output.exists():
        return None
    if not output.is_file() or output.is_symlink():
        raise CustodyError("custodian_existing_output_invalid")
    outer = _load_public(output, code="custodian_existing_output_invalid")
    if outer.get("schema") != PACKET_SCHEMA or outer.get("packet_sha256") != _digest({key: item for key, item in outer.items() if key != "packet_sha256"}) or outer.get("custodian_authorization_id") != authorization_id or outer.get("public_freeze_packet_sha256") != packet_sha256 or outer.get("public_freeze_file_sha256") != file_sha256:
        raise CustodyError("custodian_existing_output_conflict")
    return {"published": False, "retry_idempotent": True, "sha256": _file_sha256(output), "packet_sha256": outer["packet_sha256"], "gate_outcome": outer["gate_decision"]["outcome"]}


def _acquire_authorization_lock(*, output: Path, authorization_id: str) -> tuple[Path, Path, bytes, tuple[int, int]]:
    lock, consumed = _authorization_paths(output, authorization_id)
    payload = _bytes({"schema": "aerp7-convomem-custodian-lock-v1", "authorization_id": authorization_id})
    result = formal.publish_nonreplace(lock, payload, fsync_parent=executor._sync_parent)
    if result["retry_idempotent"]:
        raise CustodyError("custodian_authorization_in_progress")
    metadata = os.lstat(lock)
    if lock.is_symlink() or not lock.is_file() or metadata.st_nlink != 1:
        raise CustodyError("custodian_authorization_lock_identity_invalid")
    return lock, consumed, payload, (metadata.st_dev, metadata.st_ino)


def _remove_owned_lock(lock: Path, payload: bytes, identity: tuple[int, int]) -> None:
    try:
        if not lock.exists():
            return
        before = os.lstat(lock)
        if lock.is_symlink() or not lock.is_file() or (before.st_dev, before.st_ino) != identity:
            raise CustodyError("custodian_authorization_lock_identity_drift")
        if lock.read_bytes() != payload:
            raise CustodyError("custodian_authorization_lock_identity_drift")
        after = os.lstat(lock)
        if (after.st_dev, after.st_ino) != identity:
            raise CustodyError("custodian_authorization_lock_identity_drift")
        lock.unlink()
        executor._sync_parent(lock.parent)
    except OSError as exc:
        raise CustodyError("custodian_authorization_lock_cleanup_failed") from exc


def _consume_authorization(*, consumed: Path, authorization_id: str, packet_sha256: str, file_sha256: str) -> None:
    payload = _bytes({
        "schema": "aerp7-convomem-custodian-consumed-v1", "authorization_id": authorization_id,
        "packet_sha256": packet_sha256, "output_file_sha256": file_sha256,
    })
    result = formal.publish_nonreplace(consumed, payload, fsync_parent=executor._sync_parent)
    if result["retry_idempotent"] and consumed.read_bytes() != payload:
        raise CustodyError("custodian_authorization_consumed_conflict")


def _validated_consumed_marker(*, consumed: Path, authorization_id: str) -> dict[str, Any] | None:
    if not consumed.exists():
        return None
    if not consumed.is_file() or consumed.is_symlink():
        raise CustodyError("custodian_authorization_consumed_invalid")
    row = _load_public(consumed, code="custodian_authorization_consumed_invalid")
    required = {"schema", "authorization_id", "packet_sha256", "output_file_sha256"}
    if set(row) != required or row.get("schema") != "aerp7-convomem-custodian-consumed-v1" or row.get("authorization_id") != authorization_id:
        raise CustodyError("custodian_authorization_consumed_invalid")
    _hex(row.get("packet_sha256"), "custodian_authorization_consumed_invalid")
    _hex(row.get("output_file_sha256"), "custodian_authorization_consumed_invalid")
    return row


def _execute_authorized(*, public: Mapping[str, Any], private: Mapping[str, Any]) -> dict[str, Any]:
    """The one permitted custody-open path, called only while the lock is held."""
    cfg, packet = public["config"], public["packet"]
    if os.getpid() in {row["pid"] for row in public["supervisors"].values()}:
        raise CustodyError("custodian_pid_not_isolated")
    # The release's custody digests are public commitments made before opening
    # the sealed bundle.  Validate the entire resource/artifact/original-build
    # chain now, while no custody path has been snapshotted or opened.  The
    # later actual-byte comparison prevents a substituted custody generation.
    release = _release(
        public=public, custody_ready_sha256=cfg["custody_ready_sha256"],
        custody_bundle_sha256=cfg["custody_bundle_sha256"], capability=private["custody_capability_secret"],
    )
    formal.validate_release_authorization(
        release, projection=public["projection"], ranking_artifacts=public["ranking_artifacts"],
        current_worker_receipt=public["current_worker_receipt"], protocol=public["protocol"],
        endpoint_manifest=public["endpoint_manifest"], resource_receipts=public["resource_receipts"],
        custody_ready_sha256=cfg["custody_ready_sha256"], custody_bundle_sha256=cfg["custody_bundle_sha256"],
        custody_capability_secret=private["custody_capability_secret"],
    )
    custody_root = Path(cfg["custody_bundle"])
    # These are the *actual* sealed bytes.  Only after public validation and
    # private capability verification do we snapshot them and mint the release.
    _ready_bytes, _ready_identity, custody_ready = confirmation._snapshot(custody_root / "READY.json", "custodian_custody_bundle_not_ready")
    _sealed_bytes, _sealed_identity, custody_bundle = confirmation._snapshot(custody_root / "sealed-custody.json", "custodian_custody_bundle_not_ready")
    if custody_ready != cfg["custody_ready_sha256"] or custody_bundle != cfg["custody_bundle_sha256"]:
        raise CustodyError("custodian_custody_public_binding_invalid")
    if release["custody_ready_sha256"] != custody_ready or release["custody_bundle_sha256"] != custody_bundle:
        raise CustodyError("custodian_release_actual_custody_binding_invalid")
    scorer_before = live_custodian_code_receipt()
    opened = formal.open_custody_after_release(
        release_authorization=release, projection=public["projection"], ranking_artifacts=public["ranking_artifacts"],
        current_worker_receipt=public["current_worker_receipt"], protocol=public["protocol"],
        endpoint_manifest=public["endpoint_manifest"], resource_receipts=public["resource_receipts"],
        custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle,
        custody_capability_secret=private["custody_capability_secret"], candidate_bundle_root=Path(cfg["candidate_bundle"]),
        custody_bundle_root=custody_root, binding_secret=private["binding_secret"],
    )
    # ``opened`` is the single minimal view held by this process.  The loader
    # closure has no path and cannot cause a second arbitrary file read.
    used = False
    def custody_loader() -> Any:
        nonlocal used
        if used:
            raise CustodyError("custodian_custody_loader_reused")
        used = True
        return opened
    report = score.score_frozen(
        projection=public["projection"], endpoint_manifest=public["endpoint_manifest"],
        ranking_artifacts=public["ranking_artifacts"], custody_loader=custody_loader,
        evidence_token_secret=private["evidence_token_secret"],
    )
    if not used:
        raise CustodyError("custodian_score_did_not_open_custody")
    decision = scientific_gate_decision(report, public["protocol"])
    post_unsigned = {
        "schema": formal.POST_SCORE_ATTESTATION_SCHEMA,
        "release_sha256": release["release_sha256"], "report_sha256": report["report_sha256"],
        "protocol_sha256": public["protocol"]["protocol_sha256"],
        "endpoint_manifest_sha256": public["endpoint_manifest"]["manifest_sha256"],
        "ranking_artifact_sha256": report["ranking_artifact_sha256"], "resource_sha256": release["resource_sha256"],
    }
    post = formal.sign_post_score_attestation(post_unsigned, scorer_attestation_secret=private["scorer_attestation_secret"])
    scorer_after = live_custodian_code_receipt()
    if scorer_before != scorer_after:
        raise CustodyError("custodian_live_code_drift")
    envelope = formal.audit_envelope(
        report=report, post_score_attestation=post, scorer_attestation_secret=private["scorer_attestation_secret"],
        release_authorization=release, projection=public["projection"], ranking_artifacts=public["ranking_artifacts"],
        current_worker_receipt=public["current_worker_receipt"], protocol=public["protocol"],
        endpoint_manifest=public["endpoint_manifest"], resource_receipts=public["resource_receipts"],
        custody_ready_sha256=custody_ready, custody_bundle_sha256=custody_bundle,
        custody_capability_secret=private["custody_capability_secret"],
    )
    outer = {
        "schema": PACKET_SCHEMA, "synthetic_test_mode": True, "formal_eligible": False,
        "public_freeze_packet_sha256": packet["packet_sha256"], "public_freeze_file_sha256": cfg["freeze_packet_file_sha256"],
        "envelope": envelope, "gate_decision": decision, "scorer_code_before": scorer_before,
        "scorer_code_after": scorer_after, "custodian_authorization_id": private["authorization_id"],
    }
    outer["custodian_post_score_attestation"] = _outer_attestation(
        envelope=envelope, decision=decision, scorer_before=scorer_before, scorer_after=scorer_after,
        public_packet_sha256=packet["packet_sha256"], secret=private["scorer_attestation_secret"],
    )
    outer["packet_sha256"] = _digest(outer)
    _scan_public(outer, private_values=[private[name] for name in ("binding_secret", "custody_capability_secret", "evidence_token_secret", "scorer_attestation_secret")])
    output = Path(cfg["output_path"])
    result = formal.publish_nonreplace(output, _bytes(outer), fsync_parent=executor._sync_parent)
    return {**result, "packet_sha256": outer["packet_sha256"], "gate_outcome": decision["outcome"]}


def execute_custodian(config: Mapping[str, Any], private_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Run one exact-once synthetic custodian authorization; formal data stays blocked."""
    public = validate_public_freeze(config)
    cfg, packet = public["config"], public["packet"]
    output = Path(cfg["output_path"])
    private = _private(
        private_payload, packet_sha256=packet["packet_sha256"], file_sha256=cfg["freeze_packet_file_sha256"], output_path=output,
    )
    _lock_path, consumed_path = _authorization_paths(output, private["authorization_id"])
    consumed_marker = _validated_consumed_marker(consumed=consumed_path, authorization_id=private["authorization_id"])
    existing = _validated_existing_result(
        output=output, authorization_id=private["authorization_id"], packet_sha256=packet["packet_sha256"], file_sha256=cfg["freeze_packet_file_sha256"],
    )
    if consumed_marker is not None:
        if existing is None:
            raise CustodyError("custodian_authorization_consumed_result_missing")
        if consumed_marker["packet_sha256"] != existing["packet_sha256"] or consumed_marker["output_file_sha256"] != existing["sha256"]:
            raise CustodyError("custodian_authorization_consumed_conflict")
        return existing
    if existing is not None:
        # Heal the crash window where publication completed but the consumed
        # marker was not yet durably written.  No custody access is needed.
        _consume_authorization(
            consumed=consumed_path, authorization_id=private["authorization_id"],
            packet_sha256=existing["packet_sha256"], file_sha256=existing["sha256"],
        )
        return existing
    lock, consumed, lock_payload, lock_identity = _acquire_authorization_lock(output=output, authorization_id=private["authorization_id"])
    try:
        consumed_marker = _validated_consumed_marker(consumed=consumed, authorization_id=private["authorization_id"])
        # A concurrent first execution may have published between the initial
        # output check and lock acquisition.  Authenticate it, never rescore.
        existing = _validated_existing_result(
            output=output, authorization_id=private["authorization_id"], packet_sha256=packet["packet_sha256"], file_sha256=cfg["freeze_packet_file_sha256"],
        )
        if consumed_marker is not None:
            if existing is None:
                raise CustodyError("custodian_authorization_consumed_result_missing")
            if consumed_marker["packet_sha256"] != existing["packet_sha256"] or consumed_marker["output_file_sha256"] != existing["sha256"]:
                raise CustodyError("custodian_authorization_consumed_conflict")
            return existing
        if existing is not None:
            _consume_authorization(
                consumed=consumed, authorization_id=private["authorization_id"],
                packet_sha256=existing["packet_sha256"], file_sha256=existing["sha256"],
            )
            return existing
        result = _execute_authorized(public=public, private=private)
        _consume_authorization(
            consumed=consumed, authorization_id=private["authorization_id"], packet_sha256=result["packet_sha256"], file_sha256=result["sha256"],
        )
        return result
    finally:
        # A successfully consumed authorization leaves its immutable marker;
        # only this execution's inode-bound in-progress lock is removed, once.
        _remove_owned_lock(lock, lock_payload, lock_identity)


def _sanitized_env() -> dict[str, str]:
    retained: dict[str, str] = {}
    for key in ("SystemRoot", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP"):
        value = os.environ.get(key)
        if value:
            retained[key] = value
    retained["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    retained["AERP7_CUSTODIAN_PUBLIC_ROLE"] = "1"
    return retained


def launch_custodian(*, public_config_path: Path, private_payload: Mapping[str, Any], timeout_seconds: float = 120.0, python_executable: str | None = None) -> dict[str, Any]:
    """Test harness launcher: private JSON is stdin only; child env is public."""
    executable = python_executable or sys.executable
    if Path(executable).resolve() != Path(sys.executable).resolve() or timeout_seconds <= 0:
        raise CustodyError("custodian_launcher_invalid")
    public_config = _public_config(_load_public(public_config_path, code="custodian_public_config_invalid"))
    command = [executable, "-m", "benchmarks.aerp7_custodian_executor", "--custodian", str(public_config_path.resolve())]
    env = _sanitized_env()
    cwd = Path(tempfile.mkdtemp(prefix="aerp7-custodian-cwd-"))
    payload = _bytes(private_payload)
    try:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(payload, timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                import psutil
                root = psutil.Process(process.pid)
                for child in root.children(recursive=True):
                    child.kill()
            except BaseException:
                pass
            process.kill(); stdout, stderr = process.communicate()
            raise TimeoutError("custodian_subprocess_timeout")
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    secret_values = [str(value).encode("utf-8") for key, value in private_payload.items() if key.endswith("secret")]
    if any(secret and secret in stdout + stderr for secret in secret_values):
        raise CustodyError("custodian_subprocess_secret_leakage")
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command, output=stdout, stderr=stderr)
    output_path = Path(public_config["output_path"])
    if not output_path.is_file() or output_path.is_symlink():
        raise CustodyError("custodian_subprocess_missing_output")
    packet = _load_public(output_path, code="custodian_subprocess_output_invalid")
    required = {
        "schema", "synthetic_test_mode", "formal_eligible", "public_freeze_packet_sha256",
        "public_freeze_file_sha256", "envelope", "gate_decision", "scorer_code_before",
        "scorer_code_after", "custodian_authorization_id", "custodian_post_score_attestation", "packet_sha256",
    }
    if set(packet) != required or packet.get("schema") != PACKET_SCHEMA or packet.get("synthetic_test_mode") is not True or packet.get("formal_eligible") is not False or packet.get("packet_sha256") != _digest({key: item for key, item in packet.items() if key != "packet_sha256"}):
        raise CustodyError("custodian_subprocess_output_invalid")
    if packet["public_freeze_file_sha256"] != public_config["freeze_packet_file_sha256"]:
        raise CustodyError("custodian_subprocess_output_public_binding_invalid")
    return {"pid": process.pid, "exit_code": process.returncode, "stdout": stdout, "stderr": stderr, "command_sha256": _digest(command), "environment_keys": sorted(env), "output_file_sha256": _file_sha256(output_path), "packet_sha256": packet["packet_sha256"]}


def _read_private_stdin() -> dict[str, Any]:
    try:
        value = json.loads(sys.stdin.buffer.read())
    except (json.JSONDecodeError, OSError) as exc:
        raise CustodyError("custodian_private_stdin_invalid") from exc
    if not isinstance(value, Mapping):
        raise CustodyError("custodian_private_stdin_invalid")
    return dict(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--custodian", required=True)
    args = parser.parse_args(argv)
    execute_custodian(_load_public(Path(args.custodian), code="custodian_public_config_invalid"), _read_private_stdin())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
