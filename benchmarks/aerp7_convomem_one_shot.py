"""The ordered, capability-separated AERP-7 formal execution driver."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from benchmarks import aerp7_convomem_authoring as authoring
from benchmarks import aerp7_convomem_confirmation as confirmation
from benchmarks import aerp7_convomem_executor as executor
from benchmarks import aerp7_convomem_formal as formal
from benchmarks import aerp7_original_product as original_product
from benchmarks import aerp7_custodian_executor as custodian
from benchmarks.aerp7_convomem_confirmation import CustodyError

RECEIPT_SCHEMA = "aerp7-convomem-one-shot-receipt-v1"
INFRASTRUCTURE_FAILURE_SCHEMA = "aerp7-convomem-one-shot-infrastructure-failure-v1"
PROGRESS_SCHEMA = "aerp7-convomem-one-shot-progress-v1"
GENERATION_SEAL_SCHEMA = "aerp7-convomem-generation-seal-v2"


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _final_snapshot(path: Path, *, code: str) -> tuple[dict[str, Any], str]:
    """One stable read supplies final JSON and the exact bytes it binds."""
    raw, _identity, sha256 = confirmation._snapshot(path, code)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError(code) from exc
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return dict(value), sha256


def require_formal_durability() -> None:
    """The formal path needs a filesystem with durable file+directory fsync."""
    if os.name != "posix" or not os.uname().sysname.lower().startswith("linux"):
        raise CustodyError("aerp7_formal_durability_host_unsupported")


def _publish_progress(*, path: Path, plan_sha256: str, stage: str, heartbeat: int = 0) -> None:
    """Durable, non-secret liveness observation; never a scientific artifact."""
    row = {"schema": PROGRESS_SCHEMA, "plan_sha256": plan_sha256, "stage": stage, "heartbeat": heartbeat, "updated_at_unix": int(time.time())}
    row["progress_sha256"] = _digest(row)
    if not path.parent.is_dir():
        raise CustodyError("aerp7_one_shot_progress_parent_missing")
    temporary = path.with_name("." + path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    with temporary.open("xb") as handle:
        handle.write(_bytes(row)); handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary, path)
    executor._sync_parent(path.parent)


@contextmanager
def _heartbeat(*, path: Path, plan_sha256: str, stage: str):
    """Keep a durable liveness receipt fresh while an untimed subprocess runs."""
    stopped = threading.Event()
    failure: list[BaseException] = []
    def tick() -> None:
        count = 0
        while not stopped.wait(1.0):
            count += 1
            try:
                _publish_progress(path=path, plan_sha256=plan_sha256, stage=stage, heartbeat=count)
            except BaseException as exc:  # surfaced before the next stage
                failure.append(exc); stopped.set()
                return
    worker = threading.Thread(target=tick, name="aerp7-one-shot-heartbeat", daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set(); worker.join(timeout=2.0)
    if failure:
        raise CustodyError("aerp7_one_shot_progress_heartbeat_failed") from failure[0]


def _publish_failure(*, path: Path, plan_sha256: str, stage: str, error: BaseException) -> None:
    """Leave a no-secret, exact retry witness when an authorized run aborts."""
    code = error.receipt["code"] if isinstance(error, CustodyError) else type(error).__name__
    row = {"schema": INFRASTRUCTURE_FAILURE_SCHEMA, "plan_sha256": plan_sha256, "stage": stage, "error_code": code}
    row["failure_sha256"] = _digest(row)
    executor.formal.publish_nonreplace(path, _bytes(row), fsync_parent=executor._sync_parent)


def _seal_hmac(value: Mapping[str, Any], *, custody_binding_secret: bytes) -> str:
    return hmac.new(custody_binding_secret, _bytes(value), hashlib.sha256).hexdigest()


def _json_snapshot(path: Path, code: str) -> tuple[dict[str, Any], str]:
    raw, _identity, sha256 = confirmation._snapshot(path, code)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError(code) from exc
    if not isinstance(value, Mapping):
        raise CustodyError(code)
    return dict(value), sha256


def _generation_bindings(*, candidate_root: Path, custody_root: Path) -> dict[str, Any]:
    """Snapshot the four published generation bytes before sealing or reuse."""
    projection, candidate_ready, projection_raw = confirmation._candidate_snapshot(candidate_root)
    custody, custody_raw_sha256 = _json_snapshot(custody_root / "sealed-custody.json", "aerp7_one_shot_generation_custody_invalid")
    _custody_ready, custody_ready_sha256 = _json_snapshot(custody_root / "READY.json", "aerp7_one_shot_generation_custody_ready_invalid")
    candidate_ready_raw, _candidate_ready_sha256 = _json_snapshot(candidate_root / "READY.json", "aerp7_one_shot_generation_candidate_ready_invalid")
    if candidate_ready_raw != candidate_ready:
        raise CustodyError("aerp7_one_shot_generation_candidate_ready_invalid")
    return {
        "generation_id": candidate_ready["generation_id"],
        "candidate": {
            "ready_sha256": _candidate_ready_sha256,
            "projection_raw_sha256": hashlib.sha256(projection_raw).hexdigest(),
            "projection_canonical_sha256": confirmation.canonical_sha256(projection),
        },
        "custody": {
            "ready_sha256": custody_ready_sha256,
            "raw_sha256": custody_raw_sha256,
            "canonical_sha256": confirmation.canonical_sha256(custody),
        },
        "selection_receipt": projection["selection_receipt"],
    }


def _generation_seal_unsigned(*, plan: Mapping[str, Any], checkpoint_binding: Mapping[str, Any], current_code_receipt: Mapping[str, Any], source_before: Mapping[str, Any], source_after: Mapping[str, Any], bindings: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": GENERATION_SEAL_SCHEMA,
        "plan_sha256": plan["plan_sha256"],
        "checkpoint_sha256": checkpoint_binding["checkpoint_sha256"],
        "source_manifest_pre": dict(source_before),
        "source_manifest_post": dict(source_after),
        "census_semantics": plan["census_semantics"],
        "selection_receipt": bindings["selection_receipt"],
        "generation_id": bindings["generation_id"],
        "candidate": dict(bindings["candidate"]),
        "custody": dict(bindings["custody"]),
        "current_code_receipt": dict(current_code_receipt),
    }


def _validate_generation_seal(*, seal: Any, plan: Mapping[str, Any], checkpoint_binding: Mapping[str, Any], current_code_receipt: Mapping[str, Any], source_before: Mapping[str, Any], source_after: Mapping[str, Any], bindings: Mapping[str, Any], custody_binding_secret: bytes) -> dict[str, Any]:
    if not isinstance(seal, Mapping):
        raise CustodyError("aerp7_one_shot_generation_seal_invalid")
    row = dict(seal)
    required = {
        "schema", "plan_sha256", "checkpoint_sha256", "source_manifest_pre", "source_manifest_post", "census_semantics",
        "selection_receipt", "generation_id", "candidate", "custody", "current_code_receipt", "seal_hmac",
    }
    if set(row) != required or row.get("schema") != GENERATION_SEAL_SCHEMA:
        raise CustodyError("aerp7_one_shot_generation_seal_invalid")
    unsigned = {key: value for key, value in row.items() if key != "seal_hmac"}
    expected = _seal_hmac(unsigned, custody_binding_secret=custody_binding_secret)
    if not isinstance(row["seal_hmac"], str) or not hmac.compare_digest(row["seal_hmac"], expected):
        raise CustodyError("aerp7_one_shot_generation_seal_hmac_invalid")
    expected_unsigned = _generation_seal_unsigned(
        plan=plan, checkpoint_binding=checkpoint_binding, current_code_receipt=current_code_receipt,
        source_before=source_before, source_after=source_after, bindings=bindings,
    )
    if unsigned != expected_unsigned:
        raise CustodyError("aerp7_one_shot_generation_seal_binding_invalid")
    selection = row["selection_receipt"]
    if not isinstance(selection, Mapping) or selection.get("algorithm") != confirmation.CENSUS_SELECTION_ALGORITHM:
        raise CustodyError("aerp7_one_shot_generation_seal_non_census")
    return row


def _publish_generation_seal(*, plan: Mapping[str, Any], checkpoint_binding: Mapping[str, Any], current_code_receipt: Mapping[str, Any], source_before: Mapping[str, Any], source_after: Mapping[str, Any], custody_binding_secret: bytes) -> dict[str, Any]:
    candidate_root, custody_root = Path(plan["candidate_output_dir"]), Path(plan["custody_output_dir"])
    bindings = _generation_bindings(candidate_root=candidate_root, custody_root=custody_root)
    unsigned = _generation_seal_unsigned(
        plan=plan, checkpoint_binding=checkpoint_binding, current_code_receipt=current_code_receipt,
        source_before=source_before, source_after=source_after, bindings=bindings,
    )
    seal = {**unsigned, "seal_hmac": _seal_hmac(unsigned, custody_binding_secret=custody_binding_secret)}
    for root in (candidate_root, custody_root):
        executor.formal.publish_nonreplace(root / "generation-v2.json", _bytes(seal), fsync_parent=executor._sync_parent)
    # Fresh output and a later retry share exactly this validator.
    _validate_generation_seal(
        seal=_json_snapshot(candidate_root / "generation-v2.json", "aerp7_one_shot_generation_seal_invalid")[0],
        plan=plan, checkpoint_binding=checkpoint_binding, current_code_receipt=current_code_receipt,
        source_before=source_before, source_after=source_after, bindings=bindings,
        custody_binding_secret=custody_binding_secret,
    )
    custody_seal, _custody_seal_sha = _json_snapshot(custody_root / "generation-v2.json", "aerp7_one_shot_generation_seal_invalid")
    if custody_seal != seal:
        raise CustodyError("aerp7_one_shot_generation_seal_bundle_mismatch")
    return bindings


def _resumable_source_bundle(*, plan: Mapping[str, Any], checkpoint_binding: Mapping[str, Any], current_code_receipt: Mapping[str, Any], source_before: Mapping[str, Any], source_after: Mapping[str, Any], custody_binding_secret: bytes) -> dict[str, Any] | None:
    """Reuse only a complete source generation authenticated for this retry."""
    candidate_root, custody_root = Path(plan["candidate_output_dir"]), Path(plan["custody_output_dir"])
    if not candidate_root.exists() and not custody_root.exists():
        return None
    if not candidate_root.is_dir() or candidate_root.is_symlink() or not custody_root.is_dir() or custody_root.is_symlink():
        raise CustodyError("aerp7_one_shot_source_resume_invalid")
    candidate_seal, _candidate_seal_sha = _json_snapshot(candidate_root / "generation-v2.json", "aerp7_one_shot_generation_seal_invalid")
    custody_seal, _custody_seal_sha = _json_snapshot(custody_root / "generation-v2.json", "aerp7_one_shot_generation_seal_invalid")
    if candidate_seal != custody_seal:
        raise CustodyError("aerp7_one_shot_generation_seal_bundle_mismatch")
    bindings = _generation_bindings(candidate_root=candidate_root, custody_root=custody_root)
    _validate_generation_seal(
        seal=candidate_seal, plan=plan, checkpoint_binding=checkpoint_binding,
        current_code_receipt=current_code_receipt, source_before=source_before,
        source_after=source_after, bindings=bindings, custody_binding_secret=custody_binding_secret,
    )
    return {
        "candidate_output_dir": str(candidate_root), "custody_output_dir": str(custody_root),
        "generation_id": bindings["generation_id"],
        "projection_raw_sha256": bindings["candidate"]["projection_raw_sha256"],
        "projection_canonical_sha256": bindings["candidate"]["projection_canonical_sha256"],
        "custody_raw_sha256": bindings["custody"]["raw_sha256"],
        "custody_canonical_sha256": bindings["custody"]["canonical_sha256"],
        "selection_receipt": bindings["selection_receipt"],
    }


def _secret(value: Any, code: str, *, minimum: int = 32) -> bytes:
    if not isinstance(value, bytes) or len(value) < minimum: raise CustodyError(code)
    return value


def _private(value: Mapping[str, Any]) -> dict[str, bytes]:
    names = {"custody_binding_secret", "custody_capability_secret", "evidence_token_secret", "scorer_attestation_secret"}
    if not isinstance(value, Mapping) or set(value) != names: raise CustodyError("aerp7_one_shot_private_capability_invalid")
    result = {name: _secret(value[name], "aerp7_one_shot_private_capability_invalid") for name in names}
    # The custodian stdin schema is textual.  Freeze that encoding contract
    # before the first source stat/read, rather than failing after ranking.
    for secret in result.values():
        try: secret.decode("utf-8")
        except UnicodeDecodeError as exc: raise CustodyError("aerp7_one_shot_private_capability_encoding_invalid") from exc
    return result


def _require_new_formal_targets(*, plan: Mapping[str, Any]) -> None:
    """A formal experiment is one fresh attempt; it never resumes artifacts."""
    for key in ("candidate_output_dir", "custody_output_dir", "output_dir"):
        path = Path(plan[key])
        if path.exists() or path.is_symlink():
            raise CustodyError("aerp7_one_shot_formal_output_not_new", output_key=key)
    staging = Path(plan["staging_root"])
    if staging.is_symlink() or not staging.is_dir() or any(staging.iterdir()):
        raise CustodyError("aerp7_one_shot_formal_staging_not_empty")
    for key in (
        "protocol_path", "authorization_path", "custodian_public_config_path", "final_output_path",
        "one_shot_receipt_path", "infrastructure_failure_receipt_path", "progress_receipt_path",
    ):
        path = Path(plan[key])
        if path.exists() or path.is_symlink():
            raise CustodyError("aerp7_one_shot_formal_output_not_new", output_key=key)


@contextmanager
def _operator_environment(capability: bytes):
    try: text = capability.decode("utf-8")
    except UnicodeDecodeError as exc: raise CustodyError("aerp7_one_shot_operator_capability_invalid") from exc
    if len(capability) < 32: raise CustodyError("aerp7_one_shot_operator_capability_invalid")
    prior = os.environ.get("AERP7_OPERATOR_AUTH_CAPABILITY"); os.environ["AERP7_OPERATOR_AUTH_CAPABILITY"] = text
    try: yield
    finally:
        if prior is None: os.environ.pop("AERP7_OPERATOR_AUTH_CAPABILITY", None)
        else: os.environ["AERP7_OPERATOR_AUTH_CAPABILITY"] = prior


def _existing_receipt(*, path: Path, plan: Mapping[str, Any], repo_root: Path, private: Mapping[str, bytes]) -> dict[str, Any] | None:
    if not path.exists(): return None
    if not path.is_file() or path.is_symlink(): raise CustodyError("aerp7_one_shot_existing_receipt_invalid")
    try: row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc: raise CustodyError("aerp7_one_shot_existing_receipt_invalid") from exc
    required = {"schema", "plan_sha256", "protocol_sha256", "checkpoint_sha256", "public_freeze_sha256", "final_output_file_sha256", "custodian_packet_sha256", "gate_outcome", "receipt_sha256"}
    if not isinstance(row, Mapping) or set(row) != required or row.get("schema") != RECEIPT_SCHEMA or row.get("plan_sha256") != plan["plan_sha256"] or row.get("receipt_sha256") != _digest({key: item for key, item in row.items() if key != "receipt_sha256"}): raise CustodyError("aerp7_one_shot_existing_receipt_conflict")
    final_path = Path(plan["final_output_path"])
    final, final_sha = _final_snapshot(final_path, code="aerp7_one_shot_existing_final_missing")
    freeze_path = Path(plan["output_dir"]) / "public-freeze.json"
    # A self-hash is merely integrity of attacker-controlled bytes.  A completed
    # run additionally proves the live external checkpoint, public freeze and
    # complete custody-signed final envelope before this retry short-circuit.
    from benchmarks import aerp_execution_checkpoint as checkpoint
    protocol = executor._load(Path(plan["protocol_path"]))
    binding = checkpoint.require_live_binding(
        protocol["execution_checkpoint"],
        current_code_receipt=authoring.clean_code_receipt(repo_root),
    )
    config = executor._load(Path(plan["custodian_public_config_path"]))
    public_freeze = executor._load(freeze_path)
    private_payload = _custodian_private_payload(plan=plan, private=private, public_freeze=public_freeze, freeze_file_sha256=_file_sha256(freeze_path))
    _require_consumed_final_binding(
        final_path=final_path,
        final=final,
        final_sha256=final_sha,
        public_freeze=public_freeze,
        protocol=protocol,
        freeze_file_sha256=_file_sha256(freeze_path),
        private_payload=private_payload,
        code="aerp7_one_shot_existing_receipt_consumed_marker_invalid",
    )
    validated = custodian.validate_completed_packet(config=config, outer=final, private_payload=private_payload)
    if (
        not freeze_path.is_file()
        or _file_sha256(freeze_path) != row["public_freeze_sha256"]
        or final_sha != row["final_output_file_sha256"]
        or final.get("packet_sha256") != row["custodian_packet_sha256"]
        or validated["gate_outcome"] != row["gate_outcome"]
        or protocol.get("protocol_sha256") != row["protocol_sha256"]
        or binding.get("checkpoint_sha256") != row["checkpoint_sha256"]
    ):
        raise CustodyError("aerp7_one_shot_existing_receipt_artifact_drift")
    return {"published": False, "retry_idempotent": True, **dict(row)}


def _candidate_receipt(built: Mapping[str, Any]) -> dict[str, Any]:
    required = {"candidate_output_dir", "generation_id", "projection_raw_sha256", "projection_canonical_sha256", "dataset", "query_count", "candidate_text_count"}
    if not required <= set(built):
        raise CustodyError("aerp7_one_shot_candidate_receipt_invalid")
    root = Path(str(built["candidate_output_dir"]))
    reference = original_product.candidate_projection_reference(
        bundle_path=root, generation_id=built["generation_id"],
        projection_raw_sha256=built["projection_raw_sha256"], projection_canonical_sha256=built["projection_canonical_sha256"],
        dataset=built["dataset"], query_count=built["query_count"], candidate_text_count=built["candidate_text_count"],
    )
    return {**{key: built[key] for key in ("generation_id", "projection_raw_sha256", "projection_canonical_sha256", "query_count", "candidate_text_count")}, "ready_sha256": confirmation._snapshot(root / "READY.json", "aerp7_one_shot_candidate_ready", retain=False)[2], "candidate_reference": reference}


def _custodian_private_payload(*, plan: Mapping[str, Any], private: Mapping[str, bytes], public_freeze: Mapping[str, Any], freeze_file_sha256: str) -> dict[str, Any]:
    text: dict[str, str] = {}
    for name in ("custody_binding_secret", "custody_capability_secret", "evidence_token_secret", "scorer_attestation_secret"):
        try: text[name] = private[name].decode("utf-8")
        except UnicodeDecodeError as exc: raise CustodyError("aerp7_one_shot_private_capability_encoding_invalid") from exc
    unsigned = {"schema": custodian.FORMAL_PRIVATE_SCHEMA, "binding_secret": text.pop("custody_binding_secret"), **text, "public_packet_sha256": public_freeze["packet_sha256"], "freeze_packet_file_sha256": freeze_file_sha256, "output_path": str(Path(plan["final_output_path"]).resolve()), "nonce": plan["custodian_nonce"], "expires_at_unix": plan["custodian_expires_at_unix"]}
    return custodian.sign_private_payload(unsigned, custody_capability_secret=private["custody_capability_secret"])


def _require_consumed_final_binding(*, final_path: Path, final: Mapping[str, Any], final_sha256: str, public_freeze: Mapping[str, Any], protocol: Mapping[str, Any], freeze_file_sha256: str, private_payload: Mapping[str, Any], code: str) -> None:
    """Require the child-created, deterministic authorization marker for bytes.

    Launcher output is transport telemetry and may have observed a different
    pathname generation.  The custodian's consumed marker instead binds the
    signed private authorization ID to the exact nonreplace publication bytes.
    """
    parsed_private = custodian._private(
        private_payload,
        packet_sha256=public_freeze["packet_sha256"],
        file_sha256=freeze_file_sha256,
        output_path=final_path,
        formal_live=True,
        require_unexpired=False,
    )
    context = custodian._consumed_marker_context(
        public={
            "config": {"freeze_packet_file_sha256": freeze_file_sha256},
            "packet": public_freeze,
            "protocol": protocol,
        },
        private=parsed_private,
        output=final_path,
    )
    _lock, consumed_path = custodian._authorization_paths(final_path, parsed_private["authorization_id"])
    try:
        consumed = custodian._validated_consumed_marker(
            consumed=consumed_path,
            context=context,
            custody_capability_secret=parsed_private["custody_capability_secret"],
        )
    except CustodyError as exc:
        # One-shot's public receipt boundary reports its stage-local failure;
        # the custodian retains the discriminating HMAC failure in its own log.
        raise CustodyError(code) from exc
    if (
        consumed is None
        or consumed["packet_sha256"] != final.get("packet_sha256")
        or consumed["output_file_sha256"] != final_sha256
    ):
        raise CustodyError(code)


def _recover_final_without_receipt(*, plan: Mapping[str, Any], private: Mapping[str, bytes], repo_root: Path) -> dict[str, Any] | None:
    """Finish only a validated post-custodian/pre-receipt crash window.

    This is deliberately available after the execution lease expires: it uses
    the already-signed capability solely to verify immutable completed bytes,
    never to launch, authorize, or reopen custody.
    """
    final_path = Path(plan["final_output_path"])
    if not final_path.exists():
        return None
    protocol = executor._load(Path(plan["protocol_path"]))
    from benchmarks import aerp_execution_checkpoint as checkpoint
    binding = checkpoint.require_live_binding(protocol["execution_checkpoint"], current_code_receipt=authoring.clean_code_receipt(repo_root))
    freeze_path = Path(plan["output_dir"]) / "public-freeze.json"
    public_freeze = executor._load(freeze_path)
    config = executor._load(Path(plan["custodian_public_config_path"]))
    freeze_sha = _file_sha256(freeze_path)
    private_payload = _custodian_private_payload(plan=plan, private=private, public_freeze=public_freeze, freeze_file_sha256=freeze_sha)
    final, final_sha = _final_snapshot(final_path, code="aerp7_one_shot_final_packet_invalid")
    _require_consumed_final_binding(
        final_path=final_path,
        final=final,
        final_sha256=final_sha,
        public_freeze=public_freeze,
        protocol=protocol,
        freeze_file_sha256=freeze_sha,
        private_payload=private_payload,
        code="aerp7_one_shot_final_recovery_consumed_marker_invalid",
    )
    validated = custodian.validate_completed_packet(config=config, outer=final, private_payload=private_payload)
    if protocol.get("protocol_sha256") is None or binding.get("checkpoint_sha256") is None or validated["packet_sha256"] != final.get("packet_sha256") or validated["gate_outcome"] not in {"PASS", "FAIL"}:
        raise CustodyError("aerp7_one_shot_final_packet_invalid")
    receipt = {
        "schema": RECEIPT_SCHEMA, "plan_sha256": plan["plan_sha256"],
        "protocol_sha256": protocol["protocol_sha256"], "checkpoint_sha256": binding["checkpoint_sha256"],
        "public_freeze_sha256": freeze_sha, "final_output_file_sha256": final_sha,
        "custodian_packet_sha256": final["packet_sha256"], "gate_outcome": validated["gate_outcome"],
    }
    receipt["receipt_sha256"] = _digest(receipt)
    published = executor.formal.publish_nonreplace(Path(plan["one_shot_receipt_path"]), _bytes(receipt), fsync_parent=executor._sync_parent)
    return {**published, **receipt}


def run_one_shot(*, signed_plan: Mapping[str, Any], operator_capability: bytes, private_capabilities: Mapping[str, Any], model_receipt: Mapping[str, Any], repo_root: Path) -> dict[str, Any]:
    """Run all public and custody stages once; private capabilities use stdin only."""
    plan = authoring.validate_one_shot_plan(signed_plan, operator_capability=operator_capability); private = _private(private_capabilities)
    require_formal_durability()
    if dict(model_receipt) != plan["model_receipt"]: raise CustodyError("aerp7_one_shot_model_preparse_binding_invalid")
    _require_new_formal_targets(plan=plan)
    receipt_path = Path(plan["one_shot_receipt_path"]); existing = _existing_receipt(path=receipt_path, plan=plan, repo_root=repo_root, private=private)
    if existing is not None: return existing
    recovered = _recover_final_without_receipt(plan=plan, private=private, repo_root=repo_root)
    if recovered is not None: return recovered
    if int(time.time()) >= plan["custodian_expires_at_unix"]: raise CustodyError("aerp7_one_shot_custodian_capability_expired")
    stage = "preparse"
    _publish_progress(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage)
    try:
        # Signed-plan verification above occurs before this first legal parser
        # entrypoint.  It commits source commit/tree+inventory, model receipt,
        # census choice and all formal evaluation semantics.
        code = authoring.clean_code_receipt(repo_root)
        if code != plan["preparse_current_code_receipt"]:
            raise CustodyError("aerp7_one_shot_preparse_code_drift")
        from benchmarks import aerp_execution_checkpoint as checkpoint
        binding = checkpoint.capture_binding(expected_checkpoint_path=Path(plan["expected_checkpoint_path"]), current_code_receipt=code)
        stage = "source"; _publish_progress(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage)
        source_before = authoring.observe_source_manifest(canonical_root=Path(plan["canonical_root"]), premix_root=Path(plan["premix_root"]), expected=plan["source_manifest"])
        built = confirmation.build_prelabel_bundle(canonical_root=Path(plan["canonical_root"]), premix_root=Path(plan["premix_root"]), candidate_output_dir=Path(plan["candidate_output_dir"]), custody_output_dir=Path(plan["custody_output_dir"]), staging_root=Path(plan["staging_root"]), secret=private["custody_binding_secret"], selection=confirmation.SelectionConfig.census_v1())
        source_after = authoring.observe_source_manifest(canonical_root=Path(plan["canonical_root"]), premix_root=Path(plan["premix_root"]), expected=plan["source_manifest"])
        if source_before != source_after:
            raise CustodyError("aerp7_one_shot_source_toctou")
        # Capacity is a private coordinator gate.  It binds the generation's
        # custody receipt only after both bundles are published, but strictly
        # before the public coordinator can launch its first ranking worker.
        candidate_receipt = _candidate_receipt(built)
        custody_reference = confirmation.custody_reference(
            candidate_bundle=Path(plan["candidate_output_dir"]), custody_bundle=Path(plan["custody_output_dir"]),
            candidate_reference=candidate_receipt["candidate_reference"],
        )
        private_preflight = authoring.author_private_disk_preflight(
            plan=plan, candidate_reference=candidate_receipt["candidate_reference"], custody_reference=custody_reference,
            operator_capability=operator_capability,
        )
        checked_preflight = authoring.validate_private_disk_preflight(
            private_preflight, operator_capability=operator_capability, expected_plan_sha256=plan["plan_sha256"],
        )
        formal.enforce_private_disk_preflight(
            preflight=checked_preflight, candidate_bundle_root=Path(plan["candidate_output_dir"]),
            custody_bundle_root=Path(plan["custody_output_dir"]), candidate_reference=candidate_receipt["candidate_reference"],
            staging_root=Path(plan["staging_root"]),
        )
        stage = "protocol"; _publish_progress(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage)
        protocol = authoring.author_formal_protocol(repo_root=repo_root, candidate_receipt=candidate_receipt, model_receipt=model_receipt, expected_checkpoint_path=Path(plan["expected_checkpoint_path"]), preparse_semantics=plan["census_semantics"], preparse_current_code_receipt=plan["preparse_current_code_receipt"])
        if protocol["execution_checkpoint"] != binding: raise CustodyError("aerp7_one_shot_checkpoint_drift")
        protocol_path, authorization_path = Path(plan["protocol_path"]), Path(plan["authorization_path"])
        executor._write_new(protocol_path, protocol)
        authorization = authoring.sign_operator_authorization(protocol=protocol, output_dir=Path(plan["output_dir"]), capability=operator_capability, nonce=plan["public_authorization_nonce"], expires_at_unix=plan["custodian_expires_at_unix"])
        executor._write_new(authorization_path, authorization)
        config = {"schema": executor.FORMAL_SCHEMA, "synthetic_test_mode": False, "protocol_path": str(protocol_path.resolve()), "candidate_bundle": str(Path(plan["candidate_output_dir"]).resolve()), "output_dir": str(Path(plan["output_dir"]).resolve()), "authorization_path": str(authorization_path.resolve()), "python_executable": str(Path(plan["python_executable"]).resolve()), "original_root": str(Path(plan["original_root"]).resolve()), "model_dir": str(Path(plan["model_dir"]).resolve()), "original_python": str(Path(plan["original_python"]).resolve())}
        freeze_path = Path(plan["output_dir"]) / "public-freeze.json"
        stage = "public"; _publish_progress(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage)
        if freeze_path.exists():
            public_freeze = executor._load(freeze_path)
        else:
            with _heartbeat(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage):
                with _operator_environment(operator_capability): executor.public_coordinator(config)
            public_freeze = executor._load(freeze_path)
        freeze_file_sha256 = _file_sha256(freeze_path)
        if public_freeze.get("protocol", {}).get("protocol_sha256") != protocol["protocol_sha256"]: raise CustodyError("aerp7_one_shot_public_freeze_protocol_mismatch")
        custody_root = Path(plan["custody_output_dir"])
        custody_ready_sha256 = confirmation._snapshot(custody_root / "READY.json", "aerp7_one_shot_custody_ready")[2]
        public_config = {"schema": custodian.FORMAL_PUBLIC_CONFIG_SCHEMA, "synthetic_test_mode": False, "public_freeze_packet": str(freeze_path.resolve()), "candidate_bundle": str(Path(plan["candidate_output_dir"]).resolve()), "custody_bundle": str(custody_root.resolve()), "output_path": str(Path(plan["final_output_path"]).resolve()), "freeze_packet_file_sha256": freeze_file_sha256, "candidate_ready_sha256": protocol["candidate"]["ready_sha256"], "custody_ready_sha256": custody_ready_sha256, "custody_bundle_sha256": built["custody_raw_sha256"]}
        public_config_path = Path(plan["custodian_public_config_path"]); executor._write_new(public_config_path, public_config)
        stage = "custodian"; _publish_progress(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage)
        private_payload = _custodian_private_payload(plan=plan, private=private, public_freeze=public_freeze, freeze_file_sha256=freeze_file_sha256)
        with _heartbeat(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage):
            custodian.launch_custodian(public_config_path=public_config_path, private_payload=private_payload, timeout_seconds=None, python_executable=plan["python_executable"])
        final_path = Path(plan["final_output_path"])
        final, final_sha = _final_snapshot(final_path, code="aerp7_one_shot_final_packet_invalid")
        # Child-return metadata is only transport telemetry.  First bind this
        # stable public snapshot to the child's immutable consumed marker, then
        # rerun the full final-packet verifier before publishing a receipt.
        _require_consumed_final_binding(
            final_path=final_path,
            final=final,
            final_sha256=final_sha,
            public_freeze=public_freeze,
            protocol=protocol,
            freeze_file_sha256=freeze_file_sha256,
            private_payload=private_payload,
            code="aerp7_one_shot_initial_consumed_marker_invalid",
        )
        validated_final = custodian.validate_completed_packet(config=public_config, outer=final, private_payload=private_payload)
        if validated_final["packet_sha256"] != final.get("packet_sha256") or validated_final["gate_outcome"] not in {"PASS", "FAIL"}:
            raise CustodyError("aerp7_one_shot_final_packet_invalid")
        receipt = {"schema": RECEIPT_SCHEMA, "plan_sha256": plan["plan_sha256"], "protocol_sha256": protocol["protocol_sha256"], "checkpoint_sha256": binding["checkpoint_sha256"], "public_freeze_sha256": freeze_file_sha256, "final_output_file_sha256": final_sha, "custodian_packet_sha256": final["packet_sha256"], "gate_outcome": final["gate_decision"]["outcome"]}; receipt["receipt_sha256"] = _digest(receipt)
        published = executor.formal.publish_nonreplace(receipt_path, _bytes(receipt), fsync_parent=executor._sync_parent)
        _publish_progress(path=Path(plan["progress_receipt_path"]), plan_sha256=plan["plan_sha256"], stage="complete")
        return {**published, **receipt}
    except BaseException as exc:
        _publish_failure(path=Path(plan["infrastructure_failure_receipt_path"]), plan_sha256=plan["plan_sha256"], stage=stage, error=exc)
        raise
