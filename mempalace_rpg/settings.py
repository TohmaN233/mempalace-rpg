"""Configurable conflict controls for RPG memory.

Host games often already own mechanical state such as levels, inventory,
combat HP, money, and quest lifecycle.  ``memo_setting.json`` lets a host keep
those systems authoritative while still using the external kernel for narrative
memory, ACL, evidence, and subjective beliefs.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_MEMO_SETTINGS: dict[str, Any] = {
    "version": 1,
    "enabled": True,
    "host_state_ownership": {},
    "write": {
        "enabled": True,
        "domains": {
            "canon": True,
            "player": True,
            "quest": True,
            "location": True,
            "faction": True,
            "character": True,
            "item": True,
        },
        "event_types": {},
        "facts": True,
        "beliefs": True,
    },
    "recall": {
        "enabled": True,
        "sections": {
            "profile": True,
            "current_state": True,
            "world_truth": True,
            "actor_belief": True,
            "evidence": True,
        },
        "domains": {
            "canon": True,
            "player": True,
            "quest": True,
            "location": True,
            "faction": True,
            "character": True,
            "item": True,
        },
    },
    "conflict_policy": {
        "host_owned_state_paths": [],
        "host_authoritative_tools": [],
        "do_not_store_current_values_for": [],
    },
}

_FALSE_STRINGS = {"0", "false", "off", "no", "disabled", "disable", "none", "skip"}
_TRUE_STRINGS = {"1", "true", "on", "yes", "enabled", "enable", "full", "narrative_only"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_memo_settings(
    path: str | None = None,
    data: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """Load settings from a JSON file and/or inline data."""

    loaded: dict[str, Any] = {}
    resolved_path: str | None = None
    if path:
        resolved_path = str(Path(path).expanduser())
        with open(resolved_path, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict):
            raise ValueError("memo settings JSON root must be an object")
    if data:
        loaded = _deep_merge(loaded, data)
    return _deep_merge(DEFAULT_MEMO_SETTINGS, loaded), resolved_path


def is_enabled(value: Any, default: bool = True) -> bool:
    """Interpret booleans and human-readable mode strings."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _FALSE_STRINGS:
            return False
        if normalized in _TRUE_STRINGS:
            return True
    return default


def nested(settings: dict[str, Any], *keys: str, default: Any = None) -> Any:
    cur: Any = settings
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def write_enabled(settings: dict[str, Any]) -> bool:
    return is_enabled(nested(settings, "enabled"), True) and is_enabled(nested(settings, "write", "enabled"), True)


def recall_enabled(settings: dict[str, Any]) -> bool:
    return is_enabled(nested(settings, "enabled"), True) and is_enabled(nested(settings, "recall", "enabled"), True)


def domain_write_enabled(settings: dict[str, Any], domain: str) -> bool:
    domains = nested(settings, "write", "domains", default={}) or {}
    return is_enabled(domains.get(domain), True)


def domain_recall_enabled(settings: dict[str, Any], domain: str) -> bool:
    domains = nested(settings, "recall", "domains", default={}) or {}
    return is_enabled(domains.get(domain), True)


def event_type_write_enabled(settings: dict[str, Any], event_type: str) -> bool:
    event_types = nested(settings, "write", "event_types", default={}) or {}
    return is_enabled(event_types.get(event_type), True)


def recall_section_enabled(settings: dict[str, Any], section: str) -> bool:
    sections = nested(settings, "recall", "sections", default={}) or {}
    return is_enabled(sections.get(section), True)


def fact_write_enabled(settings: dict[str, Any]) -> bool:
    return is_enabled(nested(settings, "write", "facts"), True)


def belief_write_enabled(settings: dict[str, Any]) -> bool:
    return is_enabled(nested(settings, "write", "beliefs"), True)


def public_summary(settings: dict[str, Any], path: str | None = None) -> dict[str, Any]:
    return {
        "path": path,
        "enabled": is_enabled(nested(settings, "enabled"), True),
        "host_state_ownership": nested(settings, "host_state_ownership", default={}) or {},
        "write": nested(settings, "write", default={}) or {},
        "recall": nested(settings, "recall", default={}) or {},
        "conflict_policy": nested(settings, "conflict_policy", default={}) or {},
    }
