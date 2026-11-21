"""Strict JSON and JSON-RPC primitives for play-memory."""
from __future__ import annotations

import hashlib
import json
from typing import Any

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
