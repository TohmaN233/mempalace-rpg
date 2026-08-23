"""Fail-closed synthetic cgroup-v2 accounting for AERP-7 workers.

This module deliberately does *not* discover benchmark data, open a Docker
daemon, or enable a formal gate.  It turns a narrow, injected filesystem view
into tamper-evident descendant-inclusive CPU and container-memory receipts.
The only production-facing constructor is :meth:`CgroupV2QueryMeter.formal`;
the injectable constructor is explicitly named ``synthetic`` for tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Callable, Mapping


IDENTITY_SCHEMA = "aerp7-cgroup-v2-identity-v1"
SNAPSHOT_SCHEMA = "aerp7-cgroup-v2-snapshot-v1"
QUERY_ROW_SCHEMA = "aerp7-cgroup-v2-query-row-v1"
FINAL_RECEIPT_SCHEMA = "aerp7-cgroup-v2-container-receipt-v1"
FORMAL_CGROUP_ACCOUNTING_ENABLED = False
SYNTHETIC_CGROUP_ACCOUNTING_ONLY = True

_HEX = re.compile(r"^[0-9a-f]{64}$")
_INT = re.compile(r"^(?:0|[1-9][0-9]*)$")
_CONTROLLER = re.compile(r"^[A-Za-z0-9_.-]+$")
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
_MOUNTINFO_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
_REQUIRED_FILES = (
    "cgroup.type",
    "cgroup.controllers",
    "cpu.stat",
    "memory.current",
    "memory.peak",
    "memory.events",
)
_CPU_KEYS = ("usage_usec", "user_usec", "system_usec")
_MEMORY_EVENT_KEYS = ("low", "high", "max", "oom", "oom_kill", "oom_group_kill")


class CgroupAccountingError(RuntimeError):
    """Raised whenever cgroup-v2 evidence is absent, malformed, or inconsistent."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CgroupAccountingError("cgroup_noncanonical_value") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _raw_sha256(value: str) -> str:
    if not isinstance(value, str):
        raise CgroupAccountingError("cgroup_reader_must_return_text")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        raise CgroupAccountingError(code)
    return value


def _nonempty_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise CgroupAccountingError(code)
    return value


def _absolute_posix(value: Any, code: str) -> PurePosixPath:
    text = _nonempty_text(value, code)
    if "\\" in text:
        raise CgroupAccountingError(code)
    path = PurePosixPath(text)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise CgroupAccountingError(code)
    # PurePosixPath normalizes a repeated slash, which would otherwise hide a
    # traversal-like ambiguity in evidence.  Require already-canonical input.
    if str(path) != text:
        raise CgroupAccountingError(code)
    return path


def _path_within(path: PurePosixPath, root: PurePosixPath) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _unescape_mountinfo(value: str) -> str:
    if "\x00" in value:
        raise CgroupAccountingError("cgroup_mountinfo_nul")
    # Linux mountinfo reserves only these four octal encodings.  Do not accept
    # an arbitrary octal byte (for example ``\\057``) as a path separator.
    index = 0
    while index < len(value):
        if value[index] == "\\":
            match = _OCTAL_ESCAPE.fullmatch(value[index : index + 4]) if index + 4 <= len(value) else None
            if match is None or match.group(1) not in _MOUNTINFO_ESCAPES:
                raise CgroupAccountingError("cgroup_mountinfo_escape_invalid")
            index += 4
        else:
            index += 1
    return _OCTAL_ESCAPE.sub(lambda match: _MOUNTINFO_ESCAPES[match.group(1)], value)


def parse_unified_cgroup(raw: str) -> str:
    """Parse exactly one v2 ``0::<absolute-path>`` cgroup entry."""

    if not isinstance(raw, str) or "\x00" in raw:
        raise CgroupAccountingError("cgroup_proc_cgroup_invalid")
    if raw.endswith("\n"):
        raw = raw[:-1]
    if not raw or "\n" in raw or "\r" in raw:
        raise CgroupAccountingError("cgroup_proc_cgroup_not_single_unified_line")
    if not raw.startswith("0::"):
        raise CgroupAccountingError("cgroup_proc_cgroup_not_unified_v2")
    path = _absolute_posix(raw[3:], "cgroup_relative_path_invalid")
    return str(path)


def parse_cgroup2_mountinfo(raw: str) -> tuple[str, str]:
    """Return the sole cgroup2 mount's ``(root, mount_point)``.

    Paths are decoded using the kernel's standard mountinfo octal spelling
    before lexical containment is evaluated.
    """

    if not isinstance(raw, str) or "\x00" in raw:
        raise CgroupAccountingError("cgroup_mountinfo_invalid")
    matches: list[tuple[str, str]] = []
    lines = raw.splitlines()
    if not lines:
        raise CgroupAccountingError("cgroup_mountinfo_empty")
    for line in lines:
        if not line or "\x00" in line:
            raise CgroupAccountingError("cgroup_mountinfo_line_invalid")
        parts = line.split(" - ")
        if len(parts) != 2:
            raise CgroupAccountingError("cgroup_mountinfo_separator_invalid")
        left, right = parts
        left_fields, right_fields = left.split(), right.split()
        if len(left_fields) < 6 or len(right_fields) < 3:
            raise CgroupAccountingError("cgroup_mountinfo_shape_invalid")
        if right_fields[0] != "cgroup2":
            continue
        mount_root = _absolute_posix(_unescape_mountinfo(left_fields[3]), "cgroup_mount_root_invalid")
        mount_point = _absolute_posix(_unescape_mountinfo(left_fields[4]), "cgroup_mount_point_invalid")
        matches.append((str(mount_root), str(mount_point)))
    if len(matches) != 1:
        raise CgroupAccountingError("cgroup_mountinfo_cgroup2_not_unique")
    return matches[0]


def _resolve_cgroup_dir(*, mount_root: str, mount_point: str, relative_path: str) -> str:
    root = _absolute_posix(mount_root, "cgroup_mount_root_invalid")
    point = _absolute_posix(mount_point, "cgroup_mount_point_invalid")
    relative = _absolute_posix(relative_path, "cgroup_relative_path_invalid")
    if not _path_within(relative, root):
        raise CgroupAccountingError("cgroup_relative_path_outside_mount_root")
    suffix = relative.relative_to(root)
    resolved = point.joinpath(suffix)
    if not _path_within(resolved, point):
        raise CgroupAccountingError("cgroup_resolved_path_outside_mount_point")
    return str(resolved)


def _read_required(reader: Callable[[str], str], path: str, code: str) -> str:
    try:
        value = reader(path)
    except Exception as exc:  # reader boundary is untrusted evidence
        raise CgroupAccountingError(code) from exc
    if not isinstance(value, str):
        raise CgroupAccountingError(code)
    return value


def _parse_uint(value: str, code: str) -> int:
    if not isinstance(value, str) or not _INT.fullmatch(value):
        raise CgroupAccountingError(code)
    return int(value)


def _parse_key_values(raw: str, *, required: tuple[str, ...], label: str) -> dict[str, int]:
    if not isinstance(raw, str) or "\x00" in raw or not raw.endswith("\n"):
        raise CgroupAccountingError(f"cgroup_{label}_invalid")
    values: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise CgroupAccountingError(f"cgroup_{label}_line_invalid")
        key, number = fields
        if key in values:
            raise CgroupAccountingError(f"cgroup_{label}_duplicate_key")
        values[key] = _parse_uint(number, f"cgroup_{label}_integer_invalid")
    if not set(required).issubset(values):
        raise CgroupAccountingError(f"cgroup_{label}_missing_required_key")
    return {key: values[key] for key in required}


def parse_cpu_stat(raw: str) -> dict[str, int]:
    return _parse_key_values(raw, required=_CPU_KEYS, label="cpu_stat")


def parse_memory_value(raw: str, *, label: str) -> int:
    if not isinstance(raw, str) or "\x00" in raw or not raw.endswith("\n"):
        raise CgroupAccountingError(f"cgroup_{label}_invalid")
    return _parse_uint(raw[:-1], f"cgroup_{label}_integer_invalid")


def parse_memory_events(raw: str) -> dict[str, int]:
    return _parse_key_values(raw, required=_MEMORY_EVENT_KEYS, label="memory_events")


def _parse_cgroup_type(raw: str) -> str:
    if not isinstance(raw, str) or raw not in {"domain\n", "domain threaded\n", "threaded\n"}:
        raise CgroupAccountingError("cgroup_type_invalid")
    return raw[:-1]


def _parse_controllers(raw: str) -> list[str]:
    if not isinstance(raw, str) or "\x00" in raw or not raw.endswith("\n"):
        raise CgroupAccountingError("cgroup_controllers_invalid")
    values = raw[:-1].split(" ")
    if not values or any(not _CONTROLLER.fullmatch(value) for value in values) or len(set(values)) != len(values):
        raise CgroupAccountingError("cgroup_controllers_invalid")
    if not {"cpu", "memory"}.issubset(values):
        raise CgroupAccountingError("cgroup_required_controllers_missing")
    return sorted(values)


def _stdlib_reader(path: str) -> str:
    # This is deliberately the only non-injected filesystem reader used by
    # ``formal``.  It has no fallback polling path.
    return Path(path).read_text(encoding="utf-8")


class CgroupV2QueryMeter:
    """A non-nesting, descendant-inclusive cgroup-v2 per-query meter."""

    def __init__(self, *, reader: Callable[[str], str], stat_reader: Callable[[str], Any], platform_name: str, synthetic: bool) -> None:
        if not synthetic:
            raise CgroupAccountingError("cgroup_constructor_requires_explicit_synthetic_or_formal_factory")
        self._reader = reader
        self._stat_reader = stat_reader
        self._platform_name = platform_name
        self._active: dict[str, Any] | None = None
        self._failed = False
        self._rows: list[dict[str, Any]] = []
        self._seen_item_ids: set[str] = set()
        self._identity = self._build_identity()
        self._last_snapshot = self._snapshot()
        self._validate_initial_snapshot(self._last_snapshot)

    @classmethod
    def synthetic(
        cls,
        *,
        reader: Callable[[str], str],
        stat_reader: Callable[[str], Any],
        platform_name: str = "Linux",
    ) -> "CgroupV2QueryMeter":
        """Create a test-only meter from an explicitly injected filesystem."""

        return cls(reader=reader, stat_reader=stat_reader, platform_name=platform_name, synthetic=True)

    @classmethod
    def formal(
        cls,
        *,
        reader: Callable[[str], str] | None = None,
        stat_reader: Callable[[str], Any] | None = None,
        platform_name: str | None = None,
    ) -> "CgroupV2QueryMeter":
        """Use only stdlib Linux paths; test injection is rejected outright."""

        if reader is not None or stat_reader is not None or platform_name is not None:
            raise CgroupAccountingError("cgroup_formal_injected_reader_forbidden")
        # This checkpoint intentionally exposes the production-shaped factory
        # only so callers cannot mistake an injected test seam for production
        # evidence.  It remains unusable until the separate formal gate opens.
        if not FORMAL_CGROUP_ACCOUNTING_ENABLED or SYNTHETIC_CGROUP_ACCOUNTING_ONLY:
            raise CgroupAccountingError("cgroup_formal_accounting_disabled")
        if platform.system() != "Linux" or os.name != "posix":
            raise CgroupAccountingError("cgroup_formal_linux_required")
        instance = object.__new__(cls)
        instance._reader = _stdlib_reader
        instance._stat_reader = os.stat
        instance._platform_name = "Linux"
        instance._active = None
        instance._failed = False
        instance._rows = []
        instance._seen_item_ids = set()
        instance._identity = instance._build_identity()
        instance._last_snapshot = instance._snapshot()
        instance._validate_initial_snapshot(instance._last_snapshot)
        return instance

    @property
    def identity_receipt(self) -> dict[str, Any]:
        return dict(self._identity)

    @property
    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(row) for row in self._rows)

    def _require_linux(self) -> None:
        if self._platform_name != "Linux":
            raise CgroupAccountingError("cgroup_linux_required")

    def _build_identity(self) -> dict[str, Any]:
        self._require_linux()
        self_raw = _read_required(self._reader, "/proc/self/cgroup", "cgroup_self_read_failed")
        init_raw = _read_required(self._reader, "/proc/1/cgroup", "cgroup_init_read_failed")
        relative_path = parse_unified_cgroup(self_raw)
        if parse_unified_cgroup(init_raw) != relative_path:
            raise CgroupAccountingError("cgroup_self_init_identity_mismatch")
        mount_raw = _read_required(self._reader, "/proc/self/mountinfo", "cgroup_mountinfo_read_failed")
        mount_root, mount_point = parse_cgroup2_mountinfo(mount_raw)
        directory = _resolve_cgroup_dir(mount_root=mount_root, mount_point=mount_point, relative_path=relative_path)
        try:
            stat_value = self._stat_reader(directory)
            device, inode = getattr(stat_value, "st_dev"), getattr(stat_value, "st_ino")
        except Exception as exc:
            raise CgroupAccountingError("cgroup_directory_stat_failed") from exc
        if isinstance(device, bool) or not isinstance(device, int) or device < 0 or isinstance(inode, bool) or not isinstance(inode, int) or inode <= 0:
            raise CgroupAccountingError("cgroup_directory_inode_invalid")
        cgroup_type = _parse_cgroup_type(_read_required(self._reader, f"{directory}/cgroup.type", "cgroup_type_read_failed"))
        controllers = _parse_controllers(_read_required(self._reader, f"{directory}/cgroup.controllers", "cgroup_controllers_read_failed"))
        unsigned = {
            "schema": IDENTITY_SCHEMA,
            "self_cgroup_raw_sha256": _raw_sha256(self_raw),
            "init_cgroup_raw_sha256": _raw_sha256(init_raw),
            "mountinfo_raw_sha256": _raw_sha256(mount_raw),
            "mount_root": mount_root,
            "mount_point": mount_point,
            "relative_path": relative_path,
            "resolved_cgroup_dir": directory,
            "st_dev": device,
            "st_ino": inode,
            "cgroup_type": cgroup_type,
            "controllers": controllers,
            "required_file_set": list(_REQUIRED_FILES),
        }
        return {**unsigned, "identity_sha256": canonical_sha256(unsigned)}

    def _revalidate_identity(self) -> dict[str, Any]:
        observed = self._build_identity()
        if observed != self._identity:
            raise CgroupAccountingError("cgroup_identity_drift")
        return observed

    def _snapshot(self) -> dict[str, Any]:
        identity = self._revalidate_identity()
        directory = identity["resolved_cgroup_dir"]
        cpu = parse_cpu_stat(_read_required(self._reader, f"{directory}/cpu.stat", "cgroup_cpu_stat_read_failed"))
        # cpu.stat's three independently rounded microsecond counters can
        # differ by at most two microseconds across a sampled interval.
        if abs(cpu["usage_usec"] - (cpu["user_usec"] + cpu["system_usec"])) > 2:
            raise CgroupAccountingError("cgroup_cpu_stat_components_mismatch")
        memory_current = parse_memory_value(_read_required(self._reader, f"{directory}/memory.current", "cgroup_memory_current_read_failed"), label="memory_current")
        memory_peak = parse_memory_value(_read_required(self._reader, f"{directory}/memory.peak", "cgroup_memory_peak_read_failed"), label="memory_peak")
        events = parse_memory_events(_read_required(self._reader, f"{directory}/memory.events", "cgroup_memory_events_read_failed"))
        if memory_peak <= 0:
            raise CgroupAccountingError("cgroup_memory_peak_not_positive")
        if memory_current > memory_peak:
            raise CgroupAccountingError("cgroup_memory_current_exceeds_peak")
        unsigned = {
            "schema": SNAPSHOT_SCHEMA,
            "cgroup_identity_sha256": identity["identity_sha256"],
            "cpu_stat_usec": cpu,
            "memory_current_bytes": memory_current,
            # This is cgroup charged-memory peak, not process RSS and never a
            # per-query attribution.
            "container_memory_peak_bytes": memory_peak,
            "memory_events": events,
        }
        return {**unsigned, "snapshot_sha256": canonical_sha256(unsigned)}

    @staticmethod
    def _critical_event_delta(delta: Mapping[str, int]) -> bool:
        return any(delta[key] != 0 for key in ("max", "oom", "oom_kill", "oom_group_kill"))

    def _validate_initial_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        if self._critical_event_delta(snapshot["memory_events"]):
            raise CgroupAccountingError("cgroup_initial_memory_oom_or_max_event")

    def _validate_transition(self, before: Mapping[str, Any], after: Mapping[str, Any], *, phase: str) -> dict[str, int]:
        """Validate every edge in the one global snapshot chain.

        CPU may legitimately advance between queries, so callers decide whether
        to attribute the returned delta.  Critical memory events may never be
        hidden in such a gap and always fail the meter closed.
        """

        before_cpu, after_cpu = before["cpu_stat_usec"], after["cpu_stat_usec"]
        if any(after_cpu[key] < before_cpu[key] for key in _CPU_KEYS):
            raise CgroupAccountingError(f"cgroup_{phase}_cpu_counter_regressed")
        if after["container_memory_peak_bytes"] < before["container_memory_peak_bytes"]:
            raise CgroupAccountingError(f"cgroup_{phase}_memory_peak_counter_regressed")
        before_events, after_events = before["memory_events"], after["memory_events"]
        if any(after_events[key] < before_events[key] for key in _MEMORY_EVENT_KEYS):
            raise CgroupAccountingError(f"cgroup_{phase}_memory_events_counter_regressed")
        delta = {key: after_events[key] - before_events[key] for key in _MEMORY_EVENT_KEYS}
        if self._critical_event_delta(delta):
            raise CgroupAccountingError(f"cgroup_{phase}_memory_oom_or_max_event")
        return delta

    @staticmethod
    def _validate_query_inputs(item_id: Any, query_sha256: Any, container_id: Any, plan_sha256: Any) -> tuple[str, str, str, str]:
        item = _nonempty_text(item_id, "cgroup_item_id_invalid")
        return item, _hex(query_sha256, "cgroup_query_sha256_invalid"), _hex(container_id, "cgroup_container_id_invalid"), _hex(plan_sha256, "cgroup_plan_sha256_invalid")

    def begin(self, *, item_id: str, query_sha256: str, container_id: str, plan_sha256: str) -> None:
        if self._failed:
            raise CgroupAccountingError("cgroup_meter_failed")
        if self._active is not None:
            self._active = None
            self._failed = True
            raise CgroupAccountingError("cgroup_query_nested")
        try:
            item, query, container, plan = self._validate_query_inputs(item_id, query_sha256, container_id, plan_sha256)
            if item in self._seen_item_ids:
                raise CgroupAccountingError("cgroup_duplicate_item_id")
            before = self._snapshot()
            self._validate_transition(self._last_snapshot, before, phase="begin")
            self._last_snapshot = before
            self._active = {"item_id": item, "query_sha256": query, "container_id": container, "plan_sha256": plan, "before": before}
        except Exception:
            self._failed = True
            self._active = None
            raise

    def end(self) -> dict[str, Any]:
        if self._failed:
            raise CgroupAccountingError("cgroup_meter_failed")
        if self._active is None:
            self._failed = True
            raise CgroupAccountingError("cgroup_query_end_without_begin")
        active, self._active = self._active, None
        try:
            after = self._snapshot()
            before = active["before"]
            event_delta = self._validate_transition(before, after, phase="end")
            before_cpu, after_cpu = before["cpu_stat_usec"], after["cpu_stat_usec"]
            cpu_delta_usec = after_cpu["usage_usec"] - before_cpu["usage_usec"]
            component_delta_usec = (after_cpu["user_usec"] - before_cpu["user_usec"]) + (after_cpu["system_usec"] - before_cpu["system_usec"])
            if abs(cpu_delta_usec - component_delta_usec) > 2:
                raise CgroupAccountingError("cgroup_end_cpu_components_mismatch")
            unsigned = {
                "schema": QUERY_ROW_SCHEMA,
                "item_id": active["item_id"],
                "query_sha256": active["query_sha256"],
                "container_id": active["container_id"],
                "plan_sha256": active["plan_sha256"],
                "cgroup_identity_sha256": self._identity["identity_sha256"],
                "before_snapshot_sha256": before["snapshot_sha256"],
                "after_snapshot_sha256": after["snapshot_sha256"],
                "cpu_ns": (after_cpu["usage_usec"] - before_cpu["usage_usec"]) * 1000,
                "user_cpu_ns": (after_cpu["user_usec"] - before_cpu["user_usec"]) * 1000,
                "system_cpu_ns": (after_cpu["system_usec"] - before_cpu["system_usec"]) * 1000,
                "memory_current_before_bytes": before["memory_current_bytes"],
                "memory_current_after_bytes": after["memory_current_bytes"],
                "memory_events_delta": event_delta,
            }
            row = {**unsigned, "query_row_sha256": canonical_sha256(unsigned)}
            self._rows.append(row)
            self._seen_item_ids.add(active["item_id"])
            self._last_snapshot = after
            return dict(row)
        except Exception:
            self._failed = True
            raise

    @staticmethod
    def _validate_row(row: Mapping[str, Any]) -> dict[str, Any]:
        required = {
            "schema", "item_id", "query_sha256", "container_id", "plan_sha256", "cgroup_identity_sha256",
            "before_snapshot_sha256", "after_snapshot_sha256", "cpu_ns", "user_cpu_ns", "system_cpu_ns",
            "memory_current_before_bytes", "memory_current_after_bytes", "memory_events_delta", "query_row_sha256",
        }
        if not isinstance(row, Mapping) or set(row) != required or row.get("schema") != QUERY_ROW_SCHEMA:
            raise CgroupAccountingError("cgroup_query_row_schema_invalid")
        unsigned = {key: value for key, value in row.items() if key != "query_row_sha256"}
        if _hex(row.get("query_row_sha256"), "cgroup_query_row_digest_invalid") != canonical_sha256(unsigned):
            raise CgroupAccountingError("cgroup_query_row_tampered")
        CgroupV2QueryMeter._validate_query_inputs(row["item_id"], row["query_sha256"], row["container_id"], row["plan_sha256"])
        for key in ("cgroup_identity_sha256", "before_snapshot_sha256", "after_snapshot_sha256"):
            _hex(row[key], "cgroup_query_row_binding_invalid")
        for key in ("cpu_ns", "user_cpu_ns", "system_cpu_ns", "memory_current_before_bytes", "memory_current_after_bytes"):
            if isinstance(row[key], bool) or not isinstance(row[key], int) or row[key] < 0:
                raise CgroupAccountingError("cgroup_query_row_value_invalid")
        if not isinstance(row["memory_events_delta"], Mapping) or set(row["memory_events_delta"]) != set(_MEMORY_EVENT_KEYS):
            raise CgroupAccountingError("cgroup_query_row_events_invalid")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in row["memory_events_delta"].values()):
            raise CgroupAccountingError("cgroup_query_row_events_invalid")
        if CgroupV2QueryMeter._critical_event_delta(row["memory_events_delta"]):
            raise CgroupAccountingError("cgroup_query_row_memory_oom_or_max_event")
        if any(row[key] % 1000 != 0 for key in ("cpu_ns", "user_cpu_ns", "system_cpu_ns")):
            raise CgroupAccountingError("cgroup_query_row_cpu_unit_invalid")
        if abs(row["cpu_ns"] - (row["user_cpu_ns"] + row["system_cpu_ns"])) > 2000:
            raise CgroupAccountingError("cgroup_query_row_cpu_components_mismatch")
        return dict(row)

    def final_receipt(self, *, container_id: str, plan_sha256: str) -> dict[str, Any]:
        if self._failed:
            raise CgroupAccountingError("cgroup_meter_incomplete")
        if self._active is not None:
            self._active = None
            self._failed = True
            raise CgroupAccountingError("cgroup_meter_incomplete")
        try:
            container = _hex(container_id, "cgroup_container_id_invalid")
            plan = _hex(plan_sha256, "cgroup_plan_sha256_invalid")
            rows = [self._validate_row(row) for row in self._rows]
            if not rows:
                raise CgroupAccountingError("cgroup_final_query_coverage_empty")
            snapshot = self._snapshot()
            final_event_delta = self._validate_transition(self._last_snapshot, snapshot, phase="final")
            if any(row["container_id"] != container or row["plan_sha256"] != plan or row["cgroup_identity_sha256"] != self._identity["identity_sha256"] for row in rows):
                raise CgroupAccountingError("cgroup_final_query_binding_mismatch")
            order = [{"item_id": row["item_id"], "query_sha256": row["query_sha256"], "query_row_sha256": row["query_row_sha256"]} for row in rows]
            unsigned = {
                "schema": FINAL_RECEIPT_SCHEMA,
                "container_id": container,
                "plan_sha256": plan,
                "cgroup_identity_sha256": self._identity["identity_sha256"],
                "container_memory_peak_bytes": snapshot["container_memory_peak_bytes"],
                "memory_metric": "cgroup_v2_charged_memory",
                "per_query_memory_peak_available": False,
                "query_cpu_accounting": "cgroup_v2_descendant_inclusive",
                "query_count": len(rows),
                "query_coverage_order_sha256": canonical_sha256(order),
                "query_rows_sha256": canonical_sha256(rows),
                "final_snapshot_sha256": snapshot["snapshot_sha256"],
                "final_memory_events_delta": final_event_delta,
            }
            return {**unsigned, "receipt_sha256": canonical_sha256(unsigned)}
        except Exception:
            self._failed = True
            raise


def validate_final_receipt(receipt: Mapping[str, Any], *, rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify a final receipt against exact query rows without trusting either."""

    required = {
        "schema", "container_id", "plan_sha256", "cgroup_identity_sha256", "container_memory_peak_bytes", "memory_metric",
        "per_query_memory_peak_available", "query_cpu_accounting", "query_count", "query_coverage_order_sha256", "query_rows_sha256",
        "final_snapshot_sha256", "final_memory_events_delta", "receipt_sha256",
    }
    if not isinstance(receipt, Mapping) or set(receipt) != required or receipt.get("schema") != FINAL_RECEIPT_SCHEMA:
        raise CgroupAccountingError("cgroup_final_receipt_schema_invalid")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if _hex(receipt.get("receipt_sha256"), "cgroup_final_receipt_digest_invalid") != canonical_sha256(unsigned):
        raise CgroupAccountingError("cgroup_final_receipt_tampered")
    _hex(receipt["container_id"], "cgroup_final_receipt_container_invalid")
    _hex(receipt["plan_sha256"], "cgroup_final_receipt_plan_invalid")
    _hex(receipt["cgroup_identity_sha256"], "cgroup_final_receipt_identity_invalid")
    if receipt["memory_metric"] != "cgroup_v2_charged_memory" or receipt["per_query_memory_peak_available"] is not False:
        raise CgroupAccountingError("cgroup_final_receipt_memory_semantics_invalid")
    if receipt["query_cpu_accounting"] != "cgroup_v2_descendant_inclusive":
        raise CgroupAccountingError("cgroup_final_receipt_cpu_semantics_invalid")
    if isinstance(receipt["container_memory_peak_bytes"], bool) or not isinstance(receipt["container_memory_peak_bytes"], int) or receipt["container_memory_peak_bytes"] <= 0:
        raise CgroupAccountingError("cgroup_final_receipt_memory_peak_invalid")
    checked_rows = [CgroupV2QueryMeter._validate_row(row) for row in rows]
    if isinstance(receipt["query_count"], bool) or not isinstance(receipt["query_count"], int) or receipt["query_count"] <= 0:
        raise CgroupAccountingError("cgroup_final_receipt_query_count_invalid")
    if len({row["item_id"] for row in checked_rows}) != len(checked_rows):
        raise CgroupAccountingError("cgroup_final_receipt_duplicate_item_id")
    if receipt["query_count"] != len(checked_rows) or receipt["query_rows_sha256"] != canonical_sha256(checked_rows):
        raise CgroupAccountingError("cgroup_final_receipt_query_rows_mismatch")
    order = [{"item_id": row["item_id"], "query_sha256": row["query_sha256"], "query_row_sha256": row["query_row_sha256"]} for row in checked_rows]
    if receipt["query_coverage_order_sha256"] != canonical_sha256(order):
        raise CgroupAccountingError("cgroup_final_receipt_coverage_mismatch")
    if any(row["container_id"] != receipt["container_id"] or row["plan_sha256"] != receipt["plan_sha256"] or row["cgroup_identity_sha256"] != receipt["cgroup_identity_sha256"] for row in checked_rows):
        raise CgroupAccountingError("cgroup_final_receipt_query_binding_mismatch")
    _hex(receipt["final_snapshot_sha256"], "cgroup_final_receipt_snapshot_invalid")
    final_delta = receipt["final_memory_events_delta"]
    if not isinstance(final_delta, Mapping) or set(final_delta) != set(_MEMORY_EVENT_KEYS):
        raise CgroupAccountingError("cgroup_final_receipt_events_invalid")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in final_delta.values()):
        raise CgroupAccountingError("cgroup_final_receipt_events_invalid")
    if CgroupV2QueryMeter._critical_event_delta(final_delta):
        raise CgroupAccountingError("cgroup_final_receipt_memory_oom_or_max_event")
    if any(
        row["memory_current_before_bytes"] > receipt["container_memory_peak_bytes"]
        or row["memory_current_after_bytes"] > receipt["container_memory_peak_bytes"]
        for row in checked_rows
    ):
        raise CgroupAccountingError("cgroup_final_receipt_memory_current_exceeds_peak")
    return dict(receipt)
