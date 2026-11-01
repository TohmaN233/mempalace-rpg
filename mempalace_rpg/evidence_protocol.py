"""Strict JSON and JSON-RPC primitives for memory-evidence-v1."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

PROTOCOL_VERSION = "memory-evidence-v1"
PROJECTION_VERSION = "memory-evidence-projection-v1"
METHODS = ("capabilities", "open_snapshot", "search", "catalogue", "read_event",
           "read_scene_window", "close_snapshot")


class EvidenceError(Exception):
    def __init__(self, code: int, message: str, *, kind: str):
        super().__init__(message)
        self.code = code
        self.kind = kind

    def wire(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "data": {"kind": self.kind}}


def invalid(message: str) -> EvidenceError:
    return EvidenceError(-32602, message, kind="invalid-params")


def strict_json_loads(raw: str | bytes) -> Any:
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"invalid JSON numeric constant: {value}")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def fields(value: Any, required: set[str], optional: set[str] | None = None,
           *, label: str = "params") -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - required - (optional or set()) or required - set(value):
        raise invalid(f"{label} has missing or unknown fields")
    return value


def string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value or re.search(r"[\x00-\x1f\x7f]", value):
        raise invalid(f"{label} must be a non-empty string")
    unicode_text(value, label)
    return value


def integer(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or value > 9007199254740991 or (maximum is not None and value > maximum):
        raise invalid(f"{label} must be an integer in range")
    return value


def unicode_text(value: Any, label: str, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise invalid(f"{label} must be a Unicode string")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise invalid(f"{label} contains a surrogate") from exc
    return value


def sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise invalid(f"{label} must be a lowercase SHA-256")
    return value


def timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise invalid(f"{label} must be an RFC3339 timestamp")
    if value[-1] != "Z" and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise invalid(f"{label} has an invalid timezone offset")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise invalid(f"{label} has an invalid date or time") from exc


BINDING_KEYS = {"campaignId", "actorId", "branchId", "checkpointId", "storyTime",
                "asOf", "epoch", "configHash", "sourceIndexVersion"}


def binding(value: Any) -> dict[str, Any]:
    fields(value, BINDING_KEYS, label="binding")
    for key in BINDING_KEYS - {"storyTime", "asOf", "epoch", "configHash"}:
        string(value[key], key)
    timestamp(value["storyTime"], "storyTime")
    timestamp(value["asOf"], "asOf")
    integer(value["epoch"], "epoch")
    sha256(value["configHash"], "configHash")
    return dict(value)


def string_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise invalid(f"{label} must be a string list")
    for item in value:
        string(item, label)
    if len(set(value)) != len(value):
        raise invalid(f"{label} must not contain duplicates")
    return value


def parse_rpc(value: Any) -> tuple[str | int, str, dict[str, Any]]:
    try:
        fields(value, {"jsonrpc", "id", "method", "params"}, label="request")
    except EvidenceError as exc:
        raise EvidenceError(-32600, "invalid JSON-RPC request", kind="invalid-request") from exc
    if value["jsonrpc"] != "2.0" or type(value["id"]) not in (str, int):
        raise EvidenceError(-32600, "invalid JSON-RPC request", kind="invalid-request")
    try:
        method = string(value["method"], "method")
    except EvidenceError as exc:
        raise EvidenceError(-32600, "invalid JSON-RPC method", kind="invalid-request") from exc
    if method not in METHODS:
        raise EvidenceError(-32601, "method not found", kind="method-not-found")
    if not isinstance(value["params"], dict):
        raise invalid("params must be an object")
    return value["id"], method, value["params"]
