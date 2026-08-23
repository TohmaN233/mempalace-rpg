"""Nine-worker synthetic-isolation planning for AERP-7.

This is deliberately a *planning and host-observation* checkpoint.  It never
opens formal benchmark data, invokes Docker, or makes a formal-release claim.
The worker helpers, pinned image, Docker daemon, and cgroup observations still
need an independently witnessed live rehearsal before this can feed a formal
executor.  The host observer detects accidental/racing output changes during
one callback, but cannot defend against a same-user malicious host process;
that residual boundary is another reason this checkpoint remains non-formal.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks.aerp7_worker_isolation import (
    IsolationError,
    build_isolation_plan,
    canonical_plan_digest,
    canonical_sha256,
    validate_isolation_plan,
)


MANIFEST_SCHEMA = "aerp7-isolated-nine-worker-manifest-v1"
LAUNCH_CONFIG_SCHEMA = "aerp7-isolated-worker-launch-config-v1"
FORMAL_ELIGIBLE = False
SYNTHETIC_TEST_MODE = True
EXPECTED_PACKET_FILENAME = "packet.json"
RELEASE_FILENAME = "RELEASE"

_HEX = re.compile(r"^[0-9a-f]{64}$")
_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE_FIELD_KEYS = frozenset(
    {
        "custody",
        "scorer",
        "secret",
        "secrets",
        "answer",
        "answers",
        "evidence",
        "binding_secret",
        "custody_capability_secret",
        "evidence_token_secret",
        "scorer_attestation_secret",
        "authorization_hmac",
        "custody_bundle",
        "custody_path",
        "message_evidences",
        "evidence_spans",
        "evidence_items",
        "evidence_ids",
        "source_locator",
        "source_path",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "synthetic_test_mode",
        "formal_eligible",
        "image",
        "image_digest",
        "image_config_digest",
        "helper_contract_sha256",
        "resource_limits",
        "user",
        "workdir",
        "protocol_root",
        "candidate_root",
        "model_root",
        "source_root",
        "capability_root",
        "staging_generation_root",
        "private_forbidden_roots",
        "role_order",
        "workers",
        "plan_digests",
        "launch_config_digests",
        "manifest_sha256",
    }
)
_WORKER_KEYS = frozenset({"supervisor_key", "plan", "launch_config"})
_LAUNCH_CONFIG_KEYS = frozenset(
    {
        "schema",
        "synthetic_test_mode",
        "formal_eligible",
        "supervisor_key",
        "plan_role",
        "packet_kind",
        "execution_role",
        "build_id",
        "plan_sha256",
        "image",
        "image_config_digest",
        "helper_contract_sha256",
        "container_paths",
        "expected_packet_filename",
        "release_filename",
        "launch_config_sha256",
    }
)


@dataclass(frozen=True)
class WorkerLaunchSpec:
    """Immutable identity for one and only one isolation worker."""

    supervisor_key: str
    plan_role: str
    packet_kind: str
    execution_role: str | None = None
    build_id: str | None = None

    def __post_init__(self) -> None:
        if (self.execution_role is None) == (self.build_id is None):
            raise ValueError("worker_spec_requires_exactly_one_execution_identity")


WORKER_LAUNCH_SPECS = (
    WorkerLaunchSpec("current-raw", "current_raw", "current", execution_role="raw"),
    WorkerLaunchSpec("current-p5_primary", "current_p5_primary", "current", execution_role="p5_primary"),
    WorkerLaunchSpec("current-p5_repeat", "current_p5_repeat", "current", execution_role="p5_repeat"),
    WorkerLaunchSpec("current-six", "current_six", "current", execution_role="six"),
    WorkerLaunchSpec("original-0", "original_0", "original", build_id="original_0"),
    WorkerLaunchSpec("original-1", "original_1", "original", build_id="original_1"),
    WorkerLaunchSpec("original-2", "original_2", "original", build_id="original_2"),
    WorkerLaunchSpec("original-3", "original_3", "original", build_id="original_3"),
    WorkerLaunchSpec("original-4", "original_4", "original", build_id="original_4"),
)


def _fail(code: str) -> None:
    raise IsolationError(code)


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _HEX.fullmatch(value):
        _fail(code)
    return value


def _path(value: Any, code: str) -> Path:
    path = _absolute_lexical_path(value, code)
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise IsolationError(code) from exc


def _absolute_lexical_path(value: Any, code: str) -> Path:
    """Return the caller-supplied absolute path without resolving links."""

    if not isinstance(value, str) or not value or "\x00" in value:
        _fail(code)
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        _fail(code)
    return path


def _link_or_reparse(result: os.stat_result) -> bool:
    """Identify both POSIX links and Windows links/junction-style reparse points."""

    if stat_module.S_ISLNK(result.st_mode):
        return True
    reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(result, "st_file_attributes", 0)
    return bool(reparse_flag and isinstance(attributes, int) and attributes & reparse_flag)


def _reject_lexical_link_components(path: Path, code: str) -> None:
    """Fail closed if any existing supplied component is a link/reparse point.

    The supplied path is intentionally inspected before ``resolve``: resolving
    first would erase the evidence that the caller selected a symlink.  Windows
    junctions are reparse points, so ``st_file_attributes`` is checked in
    addition to ``S_ISLNK``.  An unreadable component also fails closed.
    """

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            result = os.lstat(current)
        except FileNotFoundError:
            # The later strict resolution yields the same fail-closed error;
            # do not skip a later existing component by continuing.
            _fail(code)
        except OSError as exc:
            raise IsolationError(code) from exc
        if _link_or_reparse(result):
            _fail(code)


def _existing_real_dir(value: Any, code: str) -> Path:
    raw = _absolute_lexical_path(value, code)
    _reject_lexical_link_components(raw, code)
    try:
        resolved = raw.resolve(strict=True)
        resolved_lstat = os.lstat(resolved)
    except OSError as exc:
        raise IsolationError(code) from exc
    if _link_or_reparse(resolved_lstat) or not stat_module.S_ISDIR(resolved_lstat.st_mode):
        _fail(code)
    return resolved


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(first: Path, second: Path) -> bool:
    return _within(first, second) or _within(second, first)


def _require_disjoint(roots: Sequence[Path], code: str) -> None:
    for index, first in enumerate(roots):
        if any(_overlaps(first, second) for second in roots[index + 1 :]):
            _fail(code)


def _spec_by_supervisor_key(value: Any) -> WorkerLaunchSpec:
    if not isinstance(value, str):
        _fail("isolated_coordinator_role_invalid")
    for spec in WORKER_LAUNCH_SPECS:
        if spec.supervisor_key == value:
            return spec
    _fail("isolated_coordinator_role_invalid")


def _validate_specs() -> None:
    if len(WORKER_LAUNCH_SPECS) != 9:
        _fail("isolated_coordinator_spec_count_invalid")
    fields = (
        [spec.supervisor_key for spec in WORKER_LAUNCH_SPECS],
        [spec.plan_role for spec in WORKER_LAUNCH_SPECS],
        [spec.execution_role or spec.build_id for spec in WORKER_LAUNCH_SPECS],
    )
    if any(len(values) != len(set(values)) for values in fields):
        _fail("isolated_coordinator_spec_duplicate")
    if any(spec.packet_kind not in {"current", "original"} for spec in WORKER_LAUNCH_SPECS):
        _fail("isolated_coordinator_spec_packet_kind_invalid")


def _role_paths(capability_root: Path, spec: WorkerLaunchSpec) -> dict[str, Path]:
    role_root = capability_root / spec.supervisor_key
    paths = {"config": role_root / "config", "output": role_root / "output"}
    if spec.build_id is not None:
        paths["palace"] = role_root / "palace"
    return paths


def _container_paths(spec: WorkerLaunchSpec) -> dict[str, str]:
    result = {
        "protocol": "/inputs/protocol",
        "candidate": "/inputs/candidate",
        "config": "/inputs/config",
        "model": "/inputs/model",
        "output": "/outputs/result",
    }
    if spec.build_id is not None:
        result.update({"source": "/inputs/source", "palace": "/outputs/palace"})
    return result


def _unsigned(value: Mapping[str, Any], digest_key: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != digest_key}


def canonical_launch_config_digest(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        _fail("isolated_coordinator_launch_config_invalid")
    return canonical_sha256(_unsigned(value, "launch_config_sha256"))


def canonical_manifest_digest(value: Mapping[str, Any]) -> str:
    if not isinstance(value, Mapping):
        _fail("isolated_coordinator_manifest_invalid")
    return canonical_sha256(_unsigned(value, "manifest_sha256"))


def _validate_filename(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or Path(value).name != value or "\x00" in value:
        _fail(code)
    return value


def _scan_private(value: Any, private_roots: Sequence[Path]) -> None:
    """Reject structured private fields and absolute values under private roots.

    Key matching is exact (case-insensitive), deliberately not a substring
    heuristic: normal paths such as ``/tmp/answerable-run`` are not rejected.
    """

    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                _fail("isolated_coordinator_private_field_invalid")
            if key.casefold() in _PRIVATE_FIELD_KEYS:
                _fail("isolated_coordinator_private_field_invalid")
            _scan_private(child, private_roots)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _scan_private(child, private_roots)
    elif isinstance(value, str):
        path = Path(value)
        if path.is_absolute():
            try:
                resolved = path.resolve(strict=False)
            except OSError as exc:
                raise IsolationError("isolated_coordinator_private_path_invalid") from exc
            if any(_within(resolved, root) or _within(root, resolved) for root in private_roots):
                _fail("isolated_coordinator_private_path_invalid")


def _launch_config(spec: WorkerLaunchSpec, plan: Mapping[str, Any], helper_contract_sha256: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "schema": LAUNCH_CONFIG_SCHEMA,
        "synthetic_test_mode": True,
        "formal_eligible": False,
        "supervisor_key": spec.supervisor_key,
        "plan_role": spec.plan_role,
        "packet_kind": spec.packet_kind,
        "execution_role": spec.execution_role,
        "build_id": spec.build_id,
        "plan_sha256": plan["plan_sha256"],
        "image": plan["image"],
        "image_config_digest": plan["image_config_digest"],
        "helper_contract_sha256": helper_contract_sha256,
        "container_paths": _container_paths(spec),
        "expected_packet_filename": EXPECTED_PACKET_FILENAME,
        "release_filename": RELEASE_FILENAME,
    }
    config["launch_config_sha256"] = canonical_launch_config_digest(config)
    return config


def validate_launch_config(
    value: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    expected_helper_contract_sha256: str,
    private_forbidden_roots: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate one exact, role-bound, non-formal launch configuration."""

    if not isinstance(value, Mapping):
        _fail("isolated_coordinator_launch_config_invalid")
    config = dict(value)
    _hex(expected_helper_contract_sha256, "isolated_coordinator_helper_contract_digest_invalid")
    private_roots = tuple(_path(item, "isolated_coordinator_private_root_invalid") for item in private_forbidden_roots)
    # Scan before exact-schema rejection so a known private canary reports as
    # such even when an adversary tried to smuggle it in as an extra field.
    _scan_private(config, private_roots)
    if set(config) != _LAUNCH_CONFIG_KEYS:
        _fail("isolated_coordinator_launch_config_invalid")
    checked_plan = validate_isolation_plan(plan)
    spec = _spec_by_supervisor_key(config.get("supervisor_key"))
    if config.get("schema") != LAUNCH_CONFIG_SCHEMA or config.get("synthetic_test_mode") is not True or config.get("formal_eligible") is not False:
        _fail("isolated_coordinator_launch_config_mode_invalid")
    if config.get("plan_role") != spec.plan_role or config.get("packet_kind") != spec.packet_kind:
        _fail("isolated_coordinator_launch_config_role_invalid")
    if config.get("execution_role") != spec.execution_role or config.get("build_id") != spec.build_id:
        _fail("isolated_coordinator_launch_config_identity_invalid")
    if checked_plan["role"] != spec.plan_role or config.get("plan_sha256") != checked_plan["plan_sha256"]:
        _fail("isolated_coordinator_launch_config_plan_binding_invalid")
    if config.get("image") != checked_plan["image"] or config.get("image_config_digest") != checked_plan["image_config_digest"]:
        _fail("isolated_coordinator_launch_config_image_binding_invalid")
    if config.get("helper_contract_sha256") != expected_helper_contract_sha256:
        _fail("isolated_coordinator_launch_config_helper_binding_invalid")
    if config.get("container_paths") != _container_paths(spec):
        _fail("isolated_coordinator_launch_config_paths_invalid")
    if _validate_filename(config.get("expected_packet_filename"), "isolated_coordinator_packet_filename_invalid") != EXPECTED_PACKET_FILENAME:
        _fail("isolated_coordinator_packet_filename_invalid")
    if _validate_filename(config.get("release_filename"), "isolated_coordinator_release_filename_invalid") != RELEASE_FILENAME:
        _fail("isolated_coordinator_release_filename_invalid")
    if config.get("launch_config_sha256") != canonical_launch_config_digest(config):
        _fail("isolated_coordinator_launch_config_digest_invalid")
    return config


def _manifest_roots(manifest: Mapping[str, Any]) -> tuple[Path, Path, tuple[Path, ...]]:
    capability = _existing_real_dir(manifest.get("capability_root"), "isolated_coordinator_capability_root_invalid")
    staging = _path(manifest.get("staging_generation_root"), "isolated_coordinator_staging_root_invalid")
    private_raw = manifest.get("private_forbidden_roots")
    if not isinstance(private_raw, list) or not private_raw:
        _fail("isolated_coordinator_private_roots_invalid")
    private = tuple(_path(item, "isolated_coordinator_private_root_invalid") for item in private_raw)
    _require_disjoint((capability, staging, *private), "isolated_coordinator_root_overlap")
    return capability, staging, private


def _expected_forbidden_roots(
    *, specs: Sequence[WorkerLaunchSpec], capability_root: Path, staging_root: Path, private_roots: Sequence[Path], this_spec: WorkerLaunchSpec
) -> list[str]:
    sibling_paths: list[Path] = []
    for spec in specs:
        if spec == this_spec:
            continue
        sibling_paths.extend(_role_paths(capability_root, spec).values())
    values = [staging_root, *private_roots, *sibling_paths]
    _require_disjoint(values, "isolated_coordinator_forbidden_root_overlap")
    return sorted(str(item) for item in values)


def build_nine_worker_manifest(
    *,
    image: str,
    image_config_digest: str,
    protocol_root: str,
    candidate_root: str,
    model_root: str,
    source_root: str,
    capability_root: str,
    staging_generation_root: str,
    private_forbidden_roots: Sequence[str],
    resource_limits: Mapping[str, Any],
    helper_contract_sha256: str,
) -> dict[str, Any]:
    """Build all nine deterministic, non-formal worker plans without I/O writes."""

    _validate_specs()
    if not isinstance(resource_limits, Mapping) or set(resource_limits) != {"pids", "memory", "cpus"}:
        _fail("isolated_coordinator_resource_limits_invalid")
    _hex(helper_contract_sha256, "isolated_coordinator_helper_contract_digest_invalid")
    protocol = _existing_real_dir(protocol_root, "isolated_coordinator_protocol_root_invalid")
    candidate = _existing_real_dir(candidate_root, "isolated_coordinator_candidate_root_invalid")
    model = _existing_real_dir(model_root, "isolated_coordinator_model_root_invalid")
    source = _existing_real_dir(source_root, "isolated_coordinator_source_root_invalid")
    capability = _existing_real_dir(capability_root, "isolated_coordinator_capability_root_invalid")
    staging = _path(staging_generation_root, "isolated_coordinator_staging_root_invalid")
    if not isinstance(private_forbidden_roots, Sequence) or isinstance(private_forbidden_roots, (str, bytes)) or not private_forbidden_roots:
        _fail("isolated_coordinator_private_roots_invalid")
    private = tuple(_path(item, "isolated_coordinator_private_root_invalid") for item in private_forbidden_roots)
    _require_disjoint((capability, staging, *private), "isolated_coordinator_root_overlap")
    _require_disjoint((protocol, candidate, model, source), "isolated_coordinator_input_root_overlap")
    if any(_overlaps(input_root, capability) for input_root in (protocol, candidate, model, source)):
        _fail("isolated_coordinator_input_root_capability_overlap")
    for root in (protocol, candidate, model, source):
        if any(_overlaps(root, forbidden) for forbidden in (staging, *private)):
            _fail("isolated_coordinator_source_root_forbidden")

    role_paths = {spec.supervisor_key: _role_paths(capability, spec) for spec in WORKER_LAUNCH_SPECS}
    all_role_roots = [path for paths in role_paths.values() for path in paths.values()]
    _require_disjoint(all_role_roots, "isolated_coordinator_role_root_overlap")
    for input_root in (protocol, candidate, model, source):
        if any(_overlaps(input_root, role_root) for role_root in all_role_roots):
            _fail("isolated_coordinator_source_root_forbidden")
    resolved_role_paths: dict[str, dict[str, Path]] = {}
    for key, paths in role_paths.items():
        resolved_role_paths[key] = {
            kind: _existing_real_dir(str(path), "isolated_coordinator_role_root_invalid") for kind, path in paths.items()
        }

    workers: list[dict[str, Any]] = []
    for spec in WORKER_LAUNCH_SPECS:
        roots = resolved_role_paths[spec.supervisor_key]
        mounts = [
            {"kind": "protocol", "source": str(protocol), "target": "/inputs/protocol", "mode": "ro"},
            {"kind": "candidate", "source": str(candidate), "target": "/inputs/candidate", "mode": "ro"},
            {"kind": "config", "source": str(roots["config"]), "target": "/inputs/config", "mode": "ro"},
            {"kind": "model", "source": str(model), "target": "/inputs/model", "mode": "ro"},
            {"kind": "output", "source": str(roots["output"]), "target": "/outputs/result", "mode": "rw"},
        ]
        if spec.build_id is not None:
            mounts.extend(
                [
                    {"kind": "source", "source": str(source), "target": "/inputs/source", "mode": "ro"},
                    {"kind": "palace", "source": str(roots["palace"]), "target": "/outputs/palace", "mode": "rw"},
                ]
            )
        plan = build_isolation_plan(
            role=spec.plan_role,
            image=image,
            image_config_digest=image_config_digest,
            container_argv=(
                ["/opt/aerp7/bin/current-worker", "--role", spec.plan_role]
                if spec.execution_role is not None
                else ["/opt/aerp7/bin/original-worker", "--build-id", spec.plan_role]
            ),
            mounts=mounts,
            memory=resource_limits["memory"],
            cpus=resource_limits["cpus"],
            pids=resource_limits["pids"],
            staging_generation_root=str(staging),
            forbidden_source_roots=_expected_forbidden_roots(
                specs=WORKER_LAUNCH_SPECS,
                capability_root=capability,
                staging_root=staging,
                private_roots=private,
                this_spec=spec,
            ),
        )
        config = _launch_config(spec, plan, helper_contract_sha256)
        workers.append({"supervisor_key": spec.supervisor_key, "plan": plan, "launch_config": config})

    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "synthetic_test_mode": True,
        "formal_eligible": False,
        "image": image,
        "image_digest": image.rsplit("@sha256:", 1)[1] if isinstance(image, str) and "@sha256:" in image else None,
        "image_config_digest": image_config_digest,
        "helper_contract_sha256": helper_contract_sha256,
        "resource_limits": dict(workers[0]["plan"]["resource_limits"]),
        "user": {"uid": 65532, "gid": 65532},
        "workdir": "/work",
        "protocol_root": str(protocol),
        "candidate_root": str(candidate),
        "model_root": str(model),
        "source_root": str(source),
        "capability_root": str(capability),
        "staging_generation_root": str(staging),
        "private_forbidden_roots": sorted(str(item) for item in private),
        "role_order": [spec.supervisor_key for spec in WORKER_LAUNCH_SPECS],
        "workers": workers,
        "plan_digests": {row["supervisor_key"]: row["plan"]["plan_sha256"] for row in workers},
        "launch_config_digests": {row["supervisor_key"]: row["launch_config"]["launch_config_sha256"] for row in workers},
    }
    manifest["manifest_sha256"] = canonical_manifest_digest(manifest)
    return validate_nine_worker_manifest(manifest)


def validate_nine_worker_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Fail closed on every digest, role, mount, and private-root binding."""

    _validate_specs()
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_KEYS:
        _fail("isolated_coordinator_manifest_invalid")
    manifest = dict(value)
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("synthetic_test_mode") is not True or manifest.get("formal_eligible") is not False:
        _fail("isolated_coordinator_manifest_mode_invalid")
    image = manifest.get("image")
    image_digest = manifest.get("image_digest")
    if not isinstance(image, str) or not image.endswith("@sha256:" + str(image_digest)) or not _HEX.fullmatch(str(image_digest)):
        _fail("isolated_coordinator_manifest_image_invalid")
    _hex(manifest.get("image_config_digest"), "isolated_coordinator_manifest_config_digest_invalid")
    _hex(manifest.get("helper_contract_sha256"), "isolated_coordinator_helper_contract_digest_invalid")
    resource_policy = manifest.get("resource_limits")
    if not isinstance(resource_policy, Mapping) or set(resource_policy) != {"pids", "memory", "cpus"}:
        _fail("isolated_coordinator_resource_limits_invalid")
    user_policy = manifest.get("user")
    if user_policy != {"uid": 65532, "gid": 65532} or manifest.get("workdir") != "/work":
        _fail("isolated_coordinator_runtime_policy_invalid")
    protocol = _existing_real_dir(manifest.get("protocol_root"), "isolated_coordinator_protocol_root_invalid")
    candidate = _existing_real_dir(manifest.get("candidate_root"), "isolated_coordinator_candidate_root_invalid")
    model = _existing_real_dir(manifest.get("model_root"), "isolated_coordinator_model_root_invalid")
    source = _existing_real_dir(manifest.get("source_root"), "isolated_coordinator_source_root_invalid")
    _require_disjoint((protocol, candidate, model, source), "isolated_coordinator_input_root_overlap")
    capability, staging, private = _manifest_roots(manifest)
    if any(_overlaps(input_root, capability) for input_root in (protocol, candidate, model, source)):
        _fail("isolated_coordinator_input_root_capability_overlap")
    expected_order = [spec.supervisor_key for spec in WORKER_LAUNCH_SPECS]
    if manifest.get("role_order") != expected_order:
        _fail("isolated_coordinator_manifest_role_order_invalid")
    workers = manifest.get("workers")
    if not isinstance(workers, list) or len(workers) != len(WORKER_LAUNCH_SPECS):
        _fail("isolated_coordinator_manifest_workers_invalid")
    actual_keys: list[str] = []
    role_roots: dict[str, dict[str, Path]] = {}
    for spec in WORKER_LAUNCH_SPECS:
        role_roots[spec.supervisor_key] = {
            kind: _existing_real_dir(str(path), "isolated_coordinator_role_root_invalid")
            for kind, path in _role_paths(capability, spec).items()
        }
    _require_disjoint([path for paths in role_roots.values() for path in paths.values()], "isolated_coordinator_role_root_overlap")
    all_role_roots = [path for paths in role_roots.values() for path in paths.values()]
    for input_root in (protocol, candidate, model, source):
        if any(_overlaps(input_root, root) for root in (staging, *private, *all_role_roots)):
            _fail("isolated_coordinator_source_root_forbidden")
    expected_plan_digests: dict[str, str] = {}
    expected_config_digests: dict[str, str] = {}
    for row, spec in zip(workers, WORKER_LAUNCH_SPECS):
        if not isinstance(row, Mapping) or set(row) != _WORKER_KEYS or row.get("supervisor_key") != spec.supervisor_key:
            _fail("isolated_coordinator_manifest_worker_invalid")
        actual_keys.append(spec.supervisor_key)
        plan = validate_isolation_plan(row.get("plan"))
        if plan["role"] != spec.plan_role or plan["image"] != image or plan["image_config_digest"] != manifest["image_config_digest"]:
            _fail("isolated_coordinator_manifest_plan_binding_invalid")
        if plan["resource_limits"] != dict(resource_policy) or plan["user"] != user_policy or plan["workdir"] != manifest["workdir"]:
            _fail("isolated_coordinator_runtime_policy_invalid")
        expected_forbidden = _expected_forbidden_roots(
            specs=WORKER_LAUNCH_SPECS,
            capability_root=capability,
            staging_root=staging,
            private_roots=private,
            this_spec=spec,
        )
        if plan["staging_generation_root"] != str(staging) or plan["forbidden_source_roots"] != expected_forbidden:
            _fail("isolated_coordinator_manifest_forbidden_roots_invalid")
        expected_mount_sources = {
            "protocol": str(protocol),
            "candidate": str(candidate),
            "config": str(role_roots[spec.supervisor_key]["config"]),
            "model": str(model),
            "output": str(role_roots[spec.supervisor_key]["output"]),
        }
        if spec.build_id is not None:
            expected_mount_sources.update({"source": str(source), "palace": str(role_roots[spec.supervisor_key]["palace"])})
        actual_mount_sources = {mount["kind"]: mount["source"] for mount in plan["mounts"]}
        if any(actual_mount_sources.get(kind) != source for kind, source in expected_mount_sources.items()):
            _fail("isolated_coordinator_manifest_mount_binding_invalid")
        config = validate_launch_config(
            row.get("launch_config"),
            plan=plan,
            expected_helper_contract_sha256=manifest["helper_contract_sha256"],
            private_forbidden_roots=[str(item) for item in private],
        )
        if config["image"] != image or config["image_config_digest"] != manifest["image_config_digest"]:
            _fail("isolated_coordinator_manifest_config_binding_invalid")
        expected_plan_digests[spec.supervisor_key] = canonical_plan_digest(plan)
        expected_config_digests[spec.supervisor_key] = canonical_launch_config_digest(config)
    if actual_keys != expected_order or len(actual_keys) != len(set(actual_keys)):
        _fail("isolated_coordinator_manifest_worker_order_invalid")
    if manifest.get("plan_digests") != expected_plan_digests or manifest.get("launch_config_digests") != expected_config_digests:
        _fail("isolated_coordinator_manifest_digest_bindings_invalid")
    if manifest.get("manifest_sha256") != canonical_manifest_digest(manifest):
        _fail("isolated_coordinator_manifest_digest_invalid")
    return manifest


def _file_state(path: Path, stat_fn: Callable[[Path], os.stat_result]) -> os.stat_result:
    try:
        result = stat_fn(path)
    except OSError as exc:
        raise IsolationError("isolated_coordinator_output_stat_failed") from exc
    return result


def _identity(result: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        result.st_dev,
        result.st_ino,
        result.st_mode,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
        result.st_nlink,
    )


def _require_safe_output_file(result: os.stat_result) -> None:
    if (
        stat_module.S_ISLNK(result.st_mode)
        or not stat_module.S_ISREG(result.st_mode)
        or result.st_nlink != 1
        or result.st_size <= 0
    ):
        _fail("isolated_coordinator_output_file_invalid")


def make_host_output_observer(
    output_root: str,
    expected_packet_filename: str = EXPECTED_PACKET_FILENAME,
    expected_container_id: str | None = None,
    *,
    reader: Callable[[Path], bytes] | None = None,
    stat_fn: Callable[[Path], os.stat_result] = os.lstat,
) -> Callable[[str, str], dict[str, Any]]:
    """Return a stateful exact-output observer for the isolation runner.

    The callback's container ID is bound at construction when supplied; if it
    is not supplied, the first call binds it permanently.  No path derives
    from that ID, avoiding a path-selection escape hatch.
    """

    packet = _validate_filename(expected_packet_filename, "isolated_coordinator_packet_filename_invalid")
    if packet != EXPECTED_PACKET_FILENAME:
        _fail("isolated_coordinator_packet_filename_invalid")
    if expected_container_id is not None and (not isinstance(expected_container_id, str) or not _CONTAINER_ID.fullmatch(expected_container_id)):
        _fail("isolated_coordinator_container_id_invalid")
    root = _existing_real_dir(output_root, "isolated_coordinator_output_root_invalid")
    read = reader or (lambda path: path.read_bytes())
    bound_container = expected_container_id

    def observe(container_id: str, phase: str) -> dict[str, Any]:
        nonlocal bound_container
        if not isinstance(container_id, str) or not _CONTAINER_ID.fullmatch(container_id):
            _fail("isolated_coordinator_container_id_invalid")
        if bound_container is None:
            bound_container = container_id
        elif container_id != bound_container:
            _fail("isolated_coordinator_container_id_mismatch")
        if phase not in {"before_release", "after_wait"}:
            _fail("isolated_coordinator_output_phase_invalid")
        root_before = _file_state(root, stat_fn)
        if not stat_module.S_ISDIR(root_before.st_mode) or stat_module.S_ISLNK(root_before.st_mode):
            _fail("isolated_coordinator_output_root_invalid")
        try:
            names = sorted(entry.name for entry in root.iterdir())
        except OSError as exc:
            raise IsolationError("isolated_coordinator_output_listing_failed") from exc
        root_after = _file_state(root, stat_fn)
        if _identity(root_before) != _identity(root_after):
            _fail("isolated_coordinator_output_race_detected")
        release = root / RELEASE_FILENAME
        output = root / packet
        if phase == "before_release":
            if names or release.exists() or output.exists():
                _fail("isolated_coordinator_output_not_absent_before_release")
            return {"release_exists": False, "output_exists": False}
        if names != sorted([RELEASE_FILENAME, packet]):
            _fail("isolated_coordinator_output_membership_invalid")
        result: dict[str, str] = {}
        observed_identities: dict[str, tuple[int, int, int, int, int, int, int]] = {}
        for name, path, digest_key in (
            (RELEASE_FILENAME, release, "release_content_sha256"),
            (packet, output, "output_sha256"),
        ):
            before = _file_state(path, stat_fn)
            _require_safe_output_file(before)
            try:
                content = read(path)
            except OSError as exc:
                raise IsolationError("isolated_coordinator_output_read_failed") from exc
            if not isinstance(content, bytes) or not content:
                _fail("isolated_coordinator_output_file_invalid")
            after = _file_state(path, stat_fn)
            if _identity(before) != _identity(after):
                _fail("isolated_coordinator_output_race_detected")
            observed_identities[name] = _identity(after)
            result[digest_key] = hashlib.sha256(content).hexdigest()
        # Recheck the complete directory *after both reads*.  Per-file checks
        # above cannot catch a replacement of RELEASE while packet.json is
        # being read; the final pass does, along with a late extra artifact.
        final_root_before = _file_state(root, stat_fn)
        try:
            final_names = sorted(entry.name for entry in root.iterdir())
        except OSError as exc:
            raise IsolationError("isolated_coordinator_output_listing_failed") from exc
        final_root_after = _file_state(root, stat_fn)
        if (
            final_names != sorted([RELEASE_FILENAME, packet])
            or _identity(final_root_before) != _identity(final_root_after)
            or _identity(root_after) != _identity(final_root_after)
        ):
            _fail("isolated_coordinator_output_race_detected")
        for name, path in ((RELEASE_FILENAME, release), (packet, output)):
            final_file = _file_state(path, stat_fn)
            _require_safe_output_file(final_file)
            if _identity(final_file) != observed_identities[name]:
                _fail("isolated_coordinator_output_race_detected")
        return {"release_exists": True, "output_exists": True, **result}

    return observe
