"""AERP-7's live binding to the reviewed external AERP-8 checkpoint.

The checkpoint remains external deliberately: it contains host/runtime receipts
which must not be regenerated from a repository snapshot.  This adapter never
copies its policy or relaxes its checks; it only gives AERP-7 a small, typed
receipt that can be sealed in its own formal protocol.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from benchmarks import aerp7_convomem_rank as rank
from benchmarks.aerp7_convomem_confirmation import CustodyError


SCHEMA = "aerp7-execution-checkpoint-binding-v1"
IMMUTABLE_SCOPE = "immutable_driver_sources_and_live_original_policy"


def _bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_bytes(value)).hexdigest()


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise CustodyError(code)
    return value


def _clean_code_receipt(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"head", "tree", "diff_digest", "dirty_policy"} or value.get("dirty_policy") != "clean_required":
        raise CustodyError("aerp7_checkpoint_current_code_invalid")
    row = dict(value)
    for key in ("head", "tree"):
        rank._git_object_id(row.get(key), "aerp7_checkpoint_current_code_invalid")
    _hex(row.get("diff_digest"), "aerp7_checkpoint_current_code_invalid")
    return row


def _aerp8() -> Any:
    # Import lazily: candidate-only tools do not need AERP-8's original-product
    # dependencies until an operator attempts the formal path.
    from benchmarks import aerp8_membench
    return aerp8_membench


def _immutable_external_checkpoint(path: Path) -> dict[str, Any]:
    """Validate AERP-8 without conflating its orchestration Git revision.

    The external receipt remains immutable.  We revalidate every pinned driver
    source byte, interpreter/venv receipt and the live original execution
    policy.  A later AERP-7-only orchestration commit is intentionally not a
    reason to invalidate the independent ranker/original checkpoint.
    """
    aerp8 = _aerp8()
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("checkpoint path")
        external = aerp8._load_canonical(path, "aerp7_external_checkpoint_invalid")
        required = {"schema", "driver_code_receipt", "original_execution_policy", "original_execution_policy_sha256", "checkpoint_sha256"}
        if not isinstance(external, Mapping) or set(external) != required or external.get("schema") != aerp8.CURRENT_CHECKPOINT_SCHEMA:
            raise ValueError("checkpoint schema")
        if external.get("checkpoint_sha256") != aerp8.digest({key: item for key, item in external.items() if key != "checkpoint_sha256"}):
            raise ValueError("checkpoint digest")
        driver = aerp8._validate_driver_code_receipt(external["driver_code_receipt"])
        policy = aerp8._validate_original_execution_policy(external["original_execution_policy"])
        if external.get("original_execution_policy_sha256") != policy["policy_sha256"]:
            raise ValueError("original policy digest")
        # `_validate_driver_code_receipt` validates the immutable receipt shape;
        # probe the exact recorded source bytes here while deliberately leaving
        # its repository-wide git state historical evidence rather than a live
        # requirement.  That is the explicit AERP-8/AERP-7 seam.
        for source in driver["sources"]:
            source_path = Path(source["path"])
            if not source_path.is_absolute() or source_path.is_symlink() or not source_path.is_file() or hashlib.sha256(source_path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError("driver source drift")
        for path_key, digest_key in (("python", "python_sha256"), ("venv_pyvenv_cfg", "venv_pyvenv_cfg_sha256")):
            runtime_path = Path(driver[path_key])
            if not runtime_path.is_absolute() or runtime_path.is_symlink() or not runtime_path.is_file() or hashlib.sha256(runtime_path.read_bytes()).hexdigest() != driver[digest_key]:
                raise ValueError("driver runtime drift")
        return dict(external)
    except Exception as exc:
        raise CustodyError("aerp7_execution_checkpoint_live_validation_failed") from exc


def validate_binding(value: Any) -> dict[str, Any]:
    """Check the immutable protocol shape without opening the external receipt."""
    if not isinstance(value, Mapping):
        raise CustodyError("aerp7_execution_checkpoint_invalid")
    row = dict(value)
    required = {
        "schema", "expected_checkpoint_path", "checkpoint_sha256", "driver_code_receipt",
        "original_execution_policy", "original_execution_policy_sha256", "current_code_receipt",
        "aerp8_validation_scope", "binding_sha256",
    }
    if set(row) != required or row.get("schema") != SCHEMA:
        raise CustodyError("aerp7_execution_checkpoint_invalid")
    if not isinstance(row.get("expected_checkpoint_path"), str) or not Path(row["expected_checkpoint_path"]).is_absolute():
        raise CustodyError("aerp7_execution_checkpoint_invalid")
    _hex(row.get("checkpoint_sha256"), "aerp7_execution_checkpoint_invalid")
    _hex(row.get("original_execution_policy_sha256"), "aerp7_execution_checkpoint_invalid")
    _clean_code_receipt(row.get("current_code_receipt"))
    if row.get("aerp8_validation_scope") != IMMUTABLE_SCOPE:
        raise CustodyError("aerp7_execution_checkpoint_invalid")
    if not isinstance(row.get("driver_code_receipt"), Mapping) or not isinstance(row.get("original_execution_policy"), Mapping):
        raise CustodyError("aerp7_execution_checkpoint_invalid")
    if row.get("binding_sha256") != _digest({key: item for key, item in row.items() if key != "binding_sha256"}):
        raise CustodyError("aerp7_execution_checkpoint_digest_invalid")
    return row


def capture_binding(*, expected_checkpoint_path: Path, current_code_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Live-validate AERP-8, then bind it to the exact AERP-7 clean code state."""
    code = _clean_code_receipt(current_code_receipt)
    external = _immutable_external_checkpoint(expected_checkpoint_path)
    row = {
        "schema": SCHEMA,
        "expected_checkpoint_path": str(expected_checkpoint_path.resolve()),
        "checkpoint_sha256": external["checkpoint_sha256"],
        "driver_code_receipt": external["driver_code_receipt"],
        "original_execution_policy": external["original_execution_policy"],
        "original_execution_policy_sha256": external["original_execution_policy_sha256"],
        "current_code_receipt": code,
        "aerp8_validation_scope": IMMUTABLE_SCOPE,
    }
    row["binding_sha256"] = _digest(row)
    return row


def require_live_binding(value: Any, *, current_code_receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Re-probe the external checkpoint before public/custody work begins."""
    frozen = validate_binding(value)
    if _clean_code_receipt(current_code_receipt) != frozen["current_code_receipt"]:
        raise CustodyError("aerp7_execution_checkpoint_current_code_drift")
    observed = capture_binding(
        expected_checkpoint_path=Path(frozen["expected_checkpoint_path"]),
        current_code_receipt=current_code_receipt,
    )
    if observed != frozen:
        raise CustodyError("aerp7_execution_checkpoint_external_drift")
    return frozen
