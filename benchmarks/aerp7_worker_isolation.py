"""Fail-closed Docker isolation plans for the AERP-7 worker boundary.

This module is deliberately small and dependency-free.  It describes the
container boundary and verifies observations made by a caller; it does not
launch a fallback subprocess and it never discovers or opens benchmark data.

The formal gate is intentionally closed in this checkpoint.  The public
functions are a synthetic rehearsal seam for the coordinator to use when the
real Docker engine, image, and OS boundary have been independently approved.

The important design rule is that a Docker invocation is a *projection of a
validated structured plan*.  There is no ``extra_args`` escape hatch.  This
keeps a caller from adding ``--privileged``, a second bind mount, or another
network mode after the plan has been attested.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


PLAN_SCHEMA = "aerp7-worker-isolation-plan-v1"
ATTESTATION_SCHEMA = "aerp7-worker-isolation-attestation-v1"
CANARY_SCHEMA = "aerp7-worker-isolation-denial-canary-live-v1"
SYNTHETIC_CANARY_SCHEMA = "aerp7-worker-isolation-denial-canary-plan-only-v1"
SYNTHETIC_ATTESTATION_SCHEMA = "aerp7-worker-isolation-rehearsal-attestation-v1"
LIVE_REHEARSAL_SCHEMA = "aerp7-worker-isolation-live-rehearsal-v2"
PRE_RELEASE_GATE_SCHEMA = "aerp7-worker-isolation-pre-release-gate-v1"
RELEASE_RECEIPT_SCHEMA = "aerp7-worker-isolation-release-receipt-v1"
FORMAL_ISOLATION_ENABLED = False
SYNTHETIC_ISOLATION_ONLY = True

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CONFIG_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}$")
_MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:[bBkKmMgGtTpPeE](?:iB?)?)?$")
_MEMORY_COMPONENT_RE = re.compile(r"^([1-9][0-9]*)([bBkKmMgGtTpPeE](?:iB?)?)?$")
_CPU_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")

_INPUT_KINDS = frozenset({"protocol", "candidate", "config", "model", "source"})
_WRITE_KINDS = frozenset({"output", "palace"})
_MOUNT_KINDS = _INPUT_KINDS | _WRITE_KINDS
_MOUNT_MODES = {kind: "ro" for kind in _INPUT_KINDS} | {kind: "rw" for kind in _WRITE_KINDS}
_MOUNT_TARGETS = {
    "protocol": "/inputs/protocol",
    "candidate": "/inputs/candidate",
    "config": "/inputs/config",
    "model": "/inputs/model",
    "source": "/inputs/source",
    "output": "/outputs/result",
    "palace": "/outputs/palace",
}
_CURRENT_ROLES = frozenset(
    {
        "current",
        "candidate",
        "six",
        "candidate_ranker",
        "candidate-ranker",
        "current_raw",
        "current-raw",
        "current_p5_primary",
        "current-p5_primary",
        "current_p5_repeat",
        "current-p5_repeat",
        "current_six",
        "current-six",
        "six_view_secondary",
        "six-view-secondary",
    }
)
_ORIGINAL_ROLES = frozenset(
    {
        "original_product",
        "original-product",
        "original_0",
        "original_1",
        "original_2",
        "original_3",
        "original_4",
        "original-0",
        "original-1",
        "original-2",
        "original-3",
        "original-4",
    }
)
_ROLE_NAMES = _CURRENT_ROLES | _ORIGINAL_ROLES
_ROLE_REQUIRED_MOUNTS = {
    **{role: frozenset({"protocol", "candidate", "config", "model", "output"}) for role in _CURRENT_ROLES},
    **{role: frozenset(_MOUNT_KINDS) for role in _ORIGINAL_ROLES},
}
_CURRENT_ENTRYPOINT = "/opt/aerp7/bin/current-worker"
_ORIGINAL_ENTRYPOINT = "/opt/aerp7/bin/original-worker"
_FORBIDDEN_PATH_PARTS = frozenset(
    {
        "custody",
        "scorer",
        "secret",
        "secrets",
        "sibling",
        "siblings",
    }
)
_CANARY_TARGET = "/inputs/forbidden-secret"
_CANARY_DENIED_OUTPUT = b"aerp7-denial-canary:denied\n"
_READY_OUTPUT = b"AERP7 READY\n"
_WAIT_SUCCESS_OUTPUT = b"0\n"
_CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")

_PLAN_KEYS = frozenset(
    {
        "schema",
        "role",
        "engine",
        "image",
        "image_digest",
        "image_config_digest",
        "container_argv",
        "workdir",
        "user",
        "network_mode",
        "rootfs_read_only",
        "no_new_privileges",
        "cap_drop",
        "resource_limits",
        "mounts",
        "staging_generation_root",
        "forbidden_source_roots",
        "plan_sha256",
    }
)
_MOUNT_KEYS = frozenset({"kind", "source", "target", "mode"})
_RESOURCE_KEYS = frozenset({"pids", "memory", "cpus"})
_INSPECT_RESOURCE_KEYS = frozenset({"pids", "memory_bytes", "nano_cpus"})
_ATTESTATION_KEYS = frozenset(
    {
        "schema",
        "plan_sha256",
        "engine_observed_digest",
        "repo_observed_digest",
        "config_image_observed_digest",
        "inspect_summary",
        "inspect_sha256",
        "denial_canary",
        "denial_canary_output_sha256",
        "attestation_sha256",
    }
)
_CANARY_KEYS = frozenset({"schema", "mode", "target", "attempted", "denied", "readable", "output_sha256"})
_SYNTHETIC_CANARY_KEYS = frozenset({"schema", "mode", "target", "live_evidence", "plan_target_unmounted"})
_SYNTHETIC_ATTESTATION_KEYS = frozenset(
    {"schema", "synthetic_test_mode", "formal_eligible", "plan_sha256", "denial_canary", "attestation_sha256"}
)
_LIVE_REHEARSAL_KEYS = frozenset(
    {
        "schema",
        "synthetic_test_mode",
        "formal_eligible",
        "plan_sha256",
        "container_id",
        "host_init_pid",
        "image_observation",
        "image_inspect_sha256",
        "created_inspect_summary",
        "created_inspect_sha256",
        "running_inspect_summary",
        "running_inspect_sha256",
        "ready_output_sha256",
        "denial_canary",
        "pre_release_gate",
        "pre_release_gate_sha256",
        "release_receipt",
        "release_receipt_sha256",
        "release_content_sha256",
        "release_output_sha256",
        "wait_output_sha256",
        "output_validation",
        "output_validation_sha256",
        "attestation_sha256",
    }
)
_IMAGE_OBSERVATION_KEYS = frozenset({"repo_digest", "config_image_id"})
_OUTPUT_ABSENT_KEYS = frozenset({"release_exists", "output_exists"})
_OUTPUT_PRESENT_KEYS = frozenset({"release_exists", "output_exists", "release_content_sha256", "output_sha256"})
_PRE_RELEASE_GATE_KEYS = frozenset(
    {
        "schema", "plan_sha256", "container_id", "host_init_pid", "launch_config_sha256",
        "image_observation", "image_inspect_sha256", "created_inspect_summary", "created_inspect_sha256",
        "running_inspect_summary", "running_inspect_sha256", "ready_output_sha256", "denial_canary",
        "output_absence", "output_absence_sha256", "pre_release_gate_sha256",
    }
)
_RELEASE_RECEIPT_KEYS = frozenset(
    {"schema", "plan_sha256", "container_id", "pre_release_gate_sha256", "launch_config_sha256", "release_content_sha256"}
)
_INSPECT_KEYS = frozenset(
    {
        "repo_digest",
        "config_image_id",
        "configured_image",
        "path",
        "args",
        "working_dir",
        "network_mode",
        "user",
        "rootfs_read_only",
        "no_new_privileges",
        "security_options",
        "restart_policy",
        "privileged",
        "cap_drop",
        "resource_limits",
        "mounts",
    }
)


class IsolationError(RuntimeError):
    """Raised whenever an isolation invariant is not proven."""


class DockerUnavailable(IsolationError):
    """The Docker CLI/daemon/image could not be proven usable."""


def _bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IsolationError("isolation_value_not_canonical_json") from exc


def canonical_sha256(value: Any) -> str:
    """Return the digest of the repository's canonical JSON representation."""

    return hashlib.sha256(_bytes(value)).hexdigest()


def _hex(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise IsolationError(code)
    return value


def _config_digest(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _CONFIG_ID_RE.fullmatch(value):
        raise IsolationError(code)
    return value[len("sha256:") :]


def _as_path(value: Any, code: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or "," in value:
        raise IsolationError(code)
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        raise IsolationError(code)
    # ``resolve(strict=False)`` does not read directory contents and gives us
    # a stable lexical comparison for overlap/forbidden-root checks.
    try:
        return path.resolve(strict=False)
    except OSError as exc:
        raise IsolationError(code) from exc


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_parts(path: Path) -> set[str]:
    return {part.casefold() for part in path.parts if part not in {path.anchor, "\\", "/"}}


def _validate_image(image: Any, image_digest: Any) -> tuple[str, str]:
    if not isinstance(image, str) or not _IMAGE_RE.fullmatch(image):
        raise IsolationError("isolation_image_must_be_digest_pinned")
    digest = image.rsplit("@sha256:", 1)[1]
    # A repository tag before ``@`` is redundant and makes the pin less
    # auditable.  Registry ports remain valid because they are not in the last
    # path component.
    image_name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    if ":" in image_name:
        raise IsolationError("isolation_image_must_be_digest_pinned")
    if image_digest != digest:
        raise IsolationError("isolation_image_digest_mismatch")
    return image, digest


def _validate_decimal(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _CPU_RE.fullmatch(value):
        raise IsolationError(code)
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise IsolationError(code) from exc
    if not number.is_finite() or number <= 0:
        raise IsolationError(code)
    return value


def _validate_memory(value: Any, code: str) -> str:
    if not isinstance(value, str) or not _MEMORY_RE.fullmatch(value):
        raise IsolationError(code)
    return value


def _memory_to_bytes(value: str) -> int:
    """Normalize Docker's plan memory spelling to the inspect byte count.

    Unsuffixed and ``b`` values are bytes.  SI suffixes (``k``, ``m``, ``g``)
    use powers of 1000, while explicit ``i`` suffixes use powers of 1024.  The
    plan grammar is integer-only, so conversion is exact and replayable.
    """

    match = _MEMORY_COMPONENT_RE.fullmatch(value)
    if match is None:
        raise IsolationError("isolation_memory_limit_invalid")
    amount, suffix = int(match.group(1)), (match.group(2) or "b").lower()
    if suffix.endswith("ib") or suffix.endswith("i"):
        base, power = 1024, "bkmgtpe".index(suffix[0])
    else:
        base, power = 1000, "bkmgtpe".index(suffix[-1])
    return amount * (base**power)


def _cpu_to_nano(value: str) -> int:
    try:
        number = Decimal(value) * Decimal(1_000_000_000)
    except InvalidOperation as exc:
        raise IsolationError("isolation_cpu_limit_invalid") from exc
    if not number.is_finite() or number <= 0 or number != number.to_integral_value():
        raise IsolationError("isolation_cpu_limit_invalid")
    return int(number)


def _validate_argv(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise IsolationError("isolation_container_argv_invalid")
    result = list(value)
    if any(not isinstance(item, str) or not item or "\x00" in item for item in result):
        raise IsolationError("isolation_container_argv_invalid")
    if not result[0].startswith("/"):
        raise IsolationError("isolation_container_executable_not_absolute")
    # These options belong to the Docker CLI and must never be smuggled in as
    # a worker argument.  A worker can still receive ordinary ``--foo`` flags.
    forbidden = {"--privileged", "--network", "--user", "--mount", "--volume", "-v"}
    if any(item in forbidden for item in result):
        raise IsolationError("isolation_container_argv_docker_option")
    return result


def expected_worker_argv(role: str) -> list[str]:
    """Return the only container identity allowed for ``role``."""

    if role in _CURRENT_ROLES:
        return [_CURRENT_ENTRYPOINT, "--role", role]
    if role in _ORIGINAL_ROLES:
        return [_ORIGINAL_ENTRYPOINT, "--build-id", role]
    raise IsolationError("isolation_role_invalid")


def _validate_role_argv(role: str, value: Any) -> list[str]:
    actual = _validate_argv(value)
    if actual != expected_worker_argv(role):
        raise IsolationError("isolation_role_worker_identity_mismatch")
    return actual


def _validate_mounts(value: Any, *, role: str, staging_root: Path | None, forbidden_roots: Sequence[Path]) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        raise IsolationError("isolation_mounts_invalid")
    rows: list[dict[str, str]] = []
    seen_kinds: set[str] = set()
    seen_targets: set[str] = set()
    seen_sources: list[Path] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _MOUNT_KEYS:
            raise IsolationError("isolation_mount_invalid")
        kind, mode, target = raw.get("kind"), raw.get("mode"), raw.get("target")
        if kind not in _MOUNT_KINDS or kind in seen_kinds:
            raise IsolationError("isolation_mount_kind_invalid")
        if mode != _MOUNT_MODES[kind] or target != _MOUNT_TARGETS[kind]:
            raise IsolationError("isolation_mount_policy_invalid")
        if target in seen_targets:
            raise IsolationError("isolation_mount_target_collision")
        if not isinstance(target, str) or not target.startswith("/") or "\x00" in target or "," in target:
            raise IsolationError("isolation_mount_target_invalid")
        source = _as_path(raw.get("source"), "isolation_mount_source_invalid")
        parts = _path_parts(source)
        if parts & _FORBIDDEN_PATH_PARTS:
            raise IsolationError("isolation_forbidden_mount_path")
        if staging_root is not None and (_within(source, staging_root) or _within(staging_root, source)):
            raise IsolationError("isolation_staging_generation_mount_forbidden")
        if any(_within(source, root) or _within(root, source) for root in forbidden_roots):
            raise IsolationError("isolation_forbidden_mount_root")
        if any(_within(source, prior) or _within(prior, source) for prior in seen_sources):
            # Overlapping host mounts let one mount expose a sibling output or
            # an unlisted secret below the other mount.
            raise IsolationError("isolation_overlapping_mounts")
        seen_kinds.add(str(kind)); seen_targets.add(str(target)); seen_sources.append(source)
        rows.append({"kind": str(kind), "source": str(source), "target": str(target), "mode": str(mode)})
    if {row["kind"] for row in rows} != _ROLE_REQUIRED_MOUNTS[role]:
        raise IsolationError("isolation_mount_coverage_invalid")
    rows.sort(key=lambda row: row["kind"])
    return rows


def _unsigned_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "plan_sha256"}


def canonical_plan_digest(plan: Mapping[str, Any]) -> str:
    """Compute a plan digest without trusting a supplied digest field."""

    if not isinstance(plan, Mapping):
        raise IsolationError("isolation_plan_invalid")
    return canonical_sha256(_unsigned_plan(plan))


def build_isolation_plan(
    *,
    role: str,
    image: str,
    image_config_digest: str,
    container_argv: Sequence[str],
    mounts: Sequence[Mapping[str, Any]],
    uid: int = 65532,
    gid: int = 65532,
    memory: str = "1g",
    cpus: str = "1.0",
    pids: int = 256,
    workdir: str = "/work",
    staging_generation_root: str | None = None,
    forbidden_source_roots: Sequence[str] = (),
) -> dict[str, Any]:
    """Create and validate one canonical role-specific Docker plan.

    ``mounts`` is intentionally explicit.  The validator, rather than the
    caller, decides which kinds may be writable and which container targets
    are legal.  ``staging_generation_root`` and forbidden roots are metadata
    commitments used to reject broad or sibling mounts without traversing
    their contents.
    """

    if role not in _ROLE_NAMES:
        raise IsolationError("isolation_role_invalid")
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0 or not isinstance(gid, int) or isinstance(gid, bool) or gid <= 0:
        raise IsolationError("isolation_user_must_be_nonroot_numeric")
    image, image_digest = _validate_image(image, image.rsplit("@sha256:", 1)[1] if isinstance(image, str) and "@sha256:" in image else None)
    _hex(image_config_digest, "isolation_image_config_digest_invalid")
    _validate_role_argv(role, container_argv)
    if not isinstance(workdir, str) or not workdir.startswith("/") or "\x00" in workdir or "," in workdir:
        raise IsolationError("isolation_workdir_invalid")
    if isinstance(pids, bool) or not isinstance(pids, int) or pids <= 0:
        raise IsolationError("isolation_pids_limit_invalid")
    if pids > 65536:
        raise IsolationError("isolation_pids_limit_too_large")
    resource_limits = {"pids": pids, "memory": _validate_memory(memory, "isolation_memory_limit_invalid"), "cpus": _validate_decimal(cpus, "isolation_cpu_limit_invalid")}
    staging = _as_path(staging_generation_root, "isolation_staging_root_invalid") if staging_generation_root is not None else None
    forbidden = tuple(_as_path(item, "isolation_forbidden_root_invalid") for item in forbidden_source_roots)
    canonical_mounts = _validate_mounts(mounts, role=role, staging_root=staging, forbidden_roots=forbidden)
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "role": role,
        "engine": "docker",
        "image": image,
        "image_digest": image_digest,
        "image_config_digest": image_config_digest,
        "container_argv": list(container_argv),
        "workdir": workdir,
        "user": {"uid": uid, "gid": gid},
        "network_mode": "none",
        "rootfs_read_only": True,
        "no_new_privileges": True,
        "cap_drop": ["ALL"],
        "resource_limits": resource_limits,
        "mounts": canonical_mounts,
        "staging_generation_root": str(staging) if staging is not None else None,
        "forbidden_source_roots": [str(item) for item in forbidden],
    }
    # Validate once before minting the digest, then retain canonicalized paths
    # and mount ordering in the returned object.
    checked = validate_isolation_plan({**plan, "plan_sha256": canonical_plan_digest(plan)})
    return checked


def validate_isolation_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate every plan invariant and recompute its canonical digest."""

    if not isinstance(value, Mapping) or set(value) != _PLAN_KEYS:
        raise IsolationError("isolation_plan_invalid")
    plan = dict(value)
    if plan.get("schema") != PLAN_SCHEMA or plan.get("engine") != "docker":
        raise IsolationError("isolation_plan_schema_invalid")
    if plan.get("role") not in _ROLE_NAMES:
        raise IsolationError("isolation_role_invalid")
    _validate_image(plan.get("image"), plan.get("image_digest"))
    _hex(plan.get("image_config_digest"), "isolation_image_config_digest_invalid")
    _validate_role_argv(str(plan["role"]), plan.get("container_argv"))
    if not isinstance(plan.get("workdir"), str) or not plan["workdir"].startswith("/") or "\x00" in plan["workdir"]:
        raise IsolationError("isolation_workdir_invalid")
    user = plan.get("user")
    if not isinstance(user, Mapping) or set(user) != {"uid", "gid"}:
        raise IsolationError("isolation_user_must_be_nonroot_numeric")
    uid, gid = user.get("uid"), user.get("gid")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in (uid, gid)):
        raise IsolationError("isolation_user_must_be_nonroot_numeric")
    if plan.get("network_mode") != "none" or plan.get("rootfs_read_only") is not True or plan.get("no_new_privileges") is not True:
        raise IsolationError("isolation_runtime_policy_invalid")
    if plan.get("cap_drop") != ["ALL"]:
        raise IsolationError("isolation_capability_policy_invalid")
    resources = plan.get("resource_limits")
    if not isinstance(resources, Mapping) or set(resources) != _RESOURCE_KEYS:
        raise IsolationError("isolation_resource_limits_invalid")
    pids = resources.get("pids")
    if isinstance(pids, bool) or not isinstance(pids, int) or pids <= 0 or pids > 65536:
        raise IsolationError("isolation_pids_limit_invalid")
    _validate_memory(resources.get("memory"), "isolation_memory_limit_invalid")
    _validate_decimal(resources.get("cpus"), "isolation_cpu_limit_invalid")
    staging_raw = plan.get("staging_generation_root")
    staging = _as_path(staging_raw, "isolation_staging_root_invalid") if staging_raw is not None else None
    forbidden_raw = plan.get("forbidden_source_roots")
    if not isinstance(forbidden_raw, list) or any(not isinstance(item, str) for item in forbidden_raw):
        raise IsolationError("isolation_forbidden_root_invalid")
    forbidden = tuple(_as_path(item, "isolation_forbidden_root_invalid") for item in forbidden_raw)
    mounts = _validate_mounts(plan.get("mounts"), role=str(plan["role"]), staging_root=staging, forbidden_roots=forbidden)
    plan["mounts"] = mounts
    plan["staging_generation_root"] = str(staging) if staging is not None else None
    plan["forbidden_source_roots"] = [str(item) for item in forbidden]
    expected_digest = canonical_plan_digest(plan)
    if plan.get("plan_sha256") != expected_digest:
        raise IsolationError("isolation_plan_digest_invalid")
    plan["plan_sha256"] = expected_digest
    return plan


def _mount_flag(row: Mapping[str, str]) -> str:
    mode = "readonly" if row["mode"] == "ro" else "rw"
    return f"type=bind,source={row['source']},destination={row['target']},{mode}"


def docker_run_command(plan: Mapping[str, Any], *, docker_executable: str = "docker") -> list[str]:
    """Project a validated plan into a fixed Docker CLI argv list.

    There is intentionally no parameter for arbitrary Docker arguments.
    """

    checked = validate_isolation_plan(plan)
    if not isinstance(docker_executable, str) or not docker_executable or "\x00" in docker_executable:
        raise IsolationError("isolation_docker_executable_invalid")
    resources = checked["resource_limits"]
    user = checked["user"]
    command = [
        docker_executable,
        "run",
        "--rm",
        "--network",
        "none",
        "--user",
        f"{user['uid']}:{user['gid']}",
        "--read-only",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--pids-limit",
        str(resources["pids"]),
        "--memory",
        resources["memory"],
        "--cpus",
        resources["cpus"],
        "--workdir",
        checked["workdir"],
    ]
    for row in checked["mounts"]:
        command.extend(("--mount", _mount_flag(row)))
    command.append(checked["image"])
    command.extend(checked["container_argv"])
    return command


def _docker_runtime_options(checked: Mapping[str, Any], *, docker_executable: str, verb: str) -> list[str]:
    """Return the fixed runtime options shared by ``create`` and legacy run."""

    resources = checked["resource_limits"]
    user = checked["user"]
    command = [docker_executable, verb, "--network", "none", "--restart", "no", "--user", f"{user['uid']}:{user['gid']}", "--read-only",
               "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL", "--pids-limit", str(resources["pids"]),
               "--memory", resources["memory"], "--cpus", resources["cpus"], "--workdir", checked["workdir"]]
    for row in checked["mounts"]:
        command.extend(("--mount", _mount_flag(row)))
    command.append(checked["image"])
    command.extend(checked["container_argv"])
    return command


def _docker_executable(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise IsolationError("isolation_docker_executable_invalid")
    return value


def docker_create_command(plan: Mapping[str, Any], *, docker_executable: str = "docker") -> list[str]:
    """Build the only allowed container creation command (never auto-remove)."""

    return _docker_runtime_options(validate_isolation_plan(plan), docker_executable=_docker_executable(docker_executable), verb="create")


def _container_id(value: Any, code: str = "isolation_container_id_invalid") -> str:
    if not isinstance(value, str) or not _CONTAINER_ID_RE.fullmatch(value):
        raise IsolationError(code)
    return value


def docker_start_command(container_id: str, *, docker_executable: str = "docker") -> list[str]:
    return [_docker_executable(docker_executable), "start", _container_id(container_id)]


def docker_container_inspect_command(container_id: str, *, docker_executable: str = "docker") -> list[str]:
    return [_docker_executable(docker_executable), "container", "inspect", _container_id(container_id)]


def docker_image_inspect_command(plan: Mapping[str, Any], *, docker_executable: str = "docker") -> list[str]:
    checked = validate_isolation_plan(plan)
    return [_docker_executable(docker_executable), "image", "inspect", checked["image"]]


def docker_ready_command(container_id: str, *, docker_executable: str = "docker") -> list[str]:
    return [_docker_executable(docker_executable), "exec", _container_id(container_id), "/opt/aerp7/bin/aerp7-ready", "--barrier"]


def docker_canary_command(container_id: str, *, docker_executable: str = "docker") -> list[str]:
    """Fixed same-container probe: no shell, path, or caller arguments."""

    return [_docker_executable(docker_executable), "exec", _container_id(container_id), "/opt/aerp7/bin/aerp7-denial-canary"]


def docker_release_command(
    container_id: str,
    *,
    plan_sha256: str,
    pre_release_gate_sha256: str,
    launch_config_sha256: str | None = None,
    docker_executable: str = "docker",
) -> list[str]:
    """Build the fixed RELEASE command bound to its pre-release gate."""

    command = [
        _docker_executable(docker_executable), "exec", _container_id(container_id), "/opt/aerp7/bin/aerp7-release",
        "--exclusive-create", "/outputs/result/RELEASE", "--plan-sha256", _hex(plan_sha256, "isolation_release_plan_digest_invalid"),
        "--container-id", _container_id(container_id), "--pre-release-gate-sha256",
        _hex(pre_release_gate_sha256, "isolation_release_gate_digest_invalid"),
    ]
    if launch_config_sha256 is not None:
        command.extend(("--launch-config-sha256", _hex(launch_config_sha256, "isolation_launch_config_digest_invalid")))
    return command


def docker_wait_command(container_id: str, *, docker_executable: str = "docker") -> list[str]:
    return [_docker_executable(docker_executable), "wait", _container_id(container_id)]


def docker_remove_command(container_id: str, *, docker_executable: str = "docker") -> list[str]:
    """Force-remove the exact container ID, including post-start failures."""

    return [_docker_executable(docker_executable), "container", "rm", "--force", _container_id(container_id)]


# Descriptive aliases make the seam easy to discover without creating a
# second implementation with subtly different validation rules.
build_docker_command = docker_run_command


def expected_inspect_summary(plan: Mapping[str, Any]) -> dict[str, Any]:
    checked = validate_isolation_plan(plan)
    resources = checked["resource_limits"]
    user = checked["user"]
    argv = checked["container_argv"]
    return {
        "repo_digest": checked["image"],
        "config_image_id": checked["image_config_digest"],
        "configured_image": checked["image"],
        "path": argv[0],
        "args": argv[1:],
        "working_dir": checked["workdir"],
        "network_mode": "none",
        "user": f"{user['uid']}:{user['gid']}",
        "rootfs_read_only": True,
        "no_new_privileges": True,
        "security_options": ["no-new-privileges:true"],
        "restart_policy": {"name": "no", "maximum_retry_count": 0},
        "privileged": False,
        "cap_drop": ["ALL"],
        "resource_limits": {
            "pids": resources["pids"],
            "memory_bytes": _memory_to_bytes(resources["memory"]),
            "nano_cpus": _cpu_to_nano(resources["cpus"]),
        },
        "mounts": [
            {"kind": row["kind"], "source": row["source"], "target": row["target"], "mode": row["mode"]}
            for row in checked["mounts"]
        ],
    }


def validate_inspect_summary(plan: Mapping[str, Any], summary: Mapping[str, Any]) -> dict[str, Any]:
    expected = expected_inspect_summary(plan)
    if not isinstance(summary, Mapping) or set(summary) != _INSPECT_KEYS:
        raise IsolationError("isolation_inspect_policy_mismatch")
    resources = summary.get("resource_limits")
    if not isinstance(resources, Mapping) or set(resources) != _INSPECT_RESOURCE_KEYS:
        raise IsolationError("isolation_inspect_resource_limits_invalid")
    for name in _INSPECT_RESOURCE_KEYS:
        item = resources.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise IsolationError("isolation_inspect_resource_limits_invalid")
    if dict(summary) != expected:
        raise IsolationError("isolation_inspect_policy_mismatch")
    return dict(summary)


def normalize_image_inspect(raw: Mapping[str, Any], *, expected_repo_digest: str) -> dict[str, str]:
    """Normalize *image inspect* evidence only.

    Docker's container inspect does not promise ``RepoDigests``.  The manifest
    identity is therefore consumed from a separate raw ``docker image inspect``
    response, while a container inspect is used only for the container config
    ID and runtime boundary below.
    """

    _validate_image(expected_repo_digest, expected_repo_digest.rsplit("@sha256:", 1)[1])
    if not isinstance(raw, Mapping):
        raise IsolationError("isolation_image_inspect_invalid")
    repo_digests = raw.get("RepoDigests")
    if not isinstance(repo_digests, list) or any(not isinstance(item, str) for item in repo_digests):
        raise IsolationError("isolation_image_inspect_repo_digest_invalid")
    if expected_repo_digest not in repo_digests or any(not _IMAGE_RE.fullmatch(item) for item in repo_digests):
        raise IsolationError("isolation_image_inspect_repo_digest_mismatch")
    config_id = _config_digest(raw.get("Id"), "isolation_image_inspect_config_id_invalid")
    return {"repo_digest": expected_repo_digest, "config_image_id": config_id}


def normalize_container_inspect(raw: Mapping[str, Any], *, expected_container_id: str | None = None) -> dict[str, Any]:
    """Normalize *container inspect* evidence without assuming RepoDigests."""

    if not isinstance(raw, Mapping):
        raise IsolationError("isolation_container_inspect_invalid")
    container_id = raw.get("Id")
    if not isinstance(container_id, str) or not _CONTAINER_ID_RE.fullmatch(container_id):
        raise IsolationError("isolation_container_inspect_id_invalid")
    if expected_container_id is not None and container_id != expected_container_id:
        raise IsolationError("isolation_container_id_mismatch")
    host = raw.get("HostConfig")
    config = raw.get("Config")
    state = raw.get("State")
    if not isinstance(host, Mapping) or not isinstance(config, Mapping) or not isinstance(state, Mapping):
        raise IsolationError("isolation_container_inspect_invalid")
    # Container inspect uses ``Image`` for the immutable config image ID;
    # ``Id`` is the container identity and must never be confused with it.
    config_id = _config_digest(raw.get("Image"), "isolation_inspect_config_image_id_invalid")
    configured_image = config.get("Image")
    path, args, working_dir = raw.get("Path"), raw.get("Args"), config.get("WorkingDir")
    if not isinstance(configured_image, str) or not _IMAGE_RE.fullmatch(configured_image):
        raise IsolationError("isolation_inspect_configured_image_invalid")
    if not isinstance(path, str) or not path.startswith("/") or not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise IsolationError("isolation_inspect_command_identity_invalid")
    if not isinstance(working_dir, str) or not working_dir.startswith("/"):
        raise IsolationError("isolation_inspect_workdir_invalid")

    security = host.get("SecurityOpt")
    # Docker's JSON inspect contract returns this as a list.  Accept a tuple
    # only for injectable test backends, but preserve the exact normalized
    # list in the receipt; a bare NNP among extra options is not sufficient.
    if not isinstance(security, (list, tuple)) or any(not isinstance(item, str) for item in security):
        raise IsolationError("isolation_inspect_security_options_invalid")
    security = list(security)
    if security != ["no-new-privileges:true"]:
        raise IsolationError("isolation_inspect_security_options_invalid")
    cap_drop = host.get("CapDrop") or []
    cap_add = host.get("CapAdd")
    if cap_add not in (None, []):
        raise IsolationError("isolation_inspect_cap_add_invalid")
    restart = host.get("RestartPolicy")
    if not isinstance(restart, Mapping) or restart.get("Name") != "no" or restart.get("MaximumRetryCount") != 0:
        raise IsolationError("isolation_inspect_restart_policy_invalid")
    if host.get("Privileged") is not False:
        raise IsolationError("isolation_inspect_privileged_invalid")
    mounts_raw = raw.get("Mounts")
    if not isinstance(mounts_raw, list):
        raise IsolationError("isolation_inspect_invalid")
    mounts: list[dict[str, str]] = []
    for row in mounts_raw:
        if not isinstance(row, Mapping):
            raise IsolationError("isolation_inspect_invalid")
        if row.get("Type") != "bind":
            raise IsolationError("isolation_inspect_mount_type_invalid")
        destination, source = row.get("Destination"), row.get("Source")
        rw = row.get("RW")
        if not isinstance(destination, str) or not isinstance(source, str) or not isinstance(rw, bool):
            raise IsolationError("isolation_inspect_invalid")
        kind = next((name for name, target in _MOUNT_TARGETS.items() if target == destination), None)
        if kind is None:
            raise IsolationError("isolation_inspect_extra_mount")
        mounts.append({"kind": kind, "source": str(_as_path(source, "isolation_inspect_invalid")), "target": destination, "mode": "rw" if rw else "ro"})
    user = config.get("User")
    if not isinstance(user, str):
        raise IsolationError("isolation_inspect_invalid")
    pids, memory_bytes, nano_cpus = host.get("PidsLimit"), host.get("Memory"), host.get("NanoCpus")
    if any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in (pids, memory_bytes, nano_cpus)):
        raise IsolationError("isolation_inspect_resource_limits_invalid")
    status, running, pid = state.get("Status"), state.get("Running"), state.get("Pid")
    if status not in {"created", "running"} or not isinstance(running, bool) or isinstance(pid, bool) or not isinstance(pid, int) or pid < 0:
        raise IsolationError("isolation_container_state_invalid")
    if (status == "created" and (running or pid != 0)) or (status == "running" and (not running or pid <= 0)):
        raise IsolationError("isolation_container_state_invalid")
    return {
        "container_id": container_id,
        "config_image_id": config_id,
        "configured_image": configured_image,
        "path": path,
        "args": list(args),
        "working_dir": working_dir,
        "network_mode": host.get("NetworkMode"),
        "user": user,
        "rootfs_read_only": host.get("ReadonlyRootfs"),
        "no_new_privileges": True,
        "security_options": security,
        "restart_policy": {"name": "no", "maximum_retry_count": 0},
        "privileged": False,
        "cap_drop": list(cap_drop),
        "resource_limits": {"pids": pids, "memory_bytes": memory_bytes, "nano_cpus": nano_cpus},
        "mounts": sorted(mounts, key=lambda row: row["kind"]),
        "state": status,
        "host_init_pid": pid,
    }


def normalize_docker_inspect(
    raw: Mapping[str, Any], *, image_raw: Mapping[str, Any] | None = None, expected_repo_digest: str | None = None,
    image_digest: str | None = None,
) -> dict[str, Any]:
    """Compatibility wrapper requiring separate image evidence.

    It intentionally rejects the old fabricated model where a container
    inspect was treated as carrying ``RepoDigests``.
    """

    if expected_repo_digest is not None and image_digest is not None and expected_repo_digest != image_digest:
        raise IsolationError("isolation_inspect_repo_digest_ambiguous")
    expected = expected_repo_digest if expected_repo_digest is not None else image_digest
    if expected is None or image_raw is None:
        raise IsolationError("isolation_image_inspect_required")
    image = normalize_image_inspect(image_raw, expected_repo_digest=expected)
    container = normalize_container_inspect(raw)
    return {
        "repo_digest": image["repo_digest"],
        "config_image_id": container["config_image_id"],
        "configured_image": container["configured_image"],
        "path": container["path"], "args": container["args"], "working_dir": container["working_dir"],
        "network_mode": container["network_mode"], "user": container["user"],
        "rootfs_read_only": container["rootfs_read_only"], "no_new_privileges": container["no_new_privileges"],
        "security_options": container["security_options"],
        "restart_policy": container["restart_policy"], "privileged": container["privileged"],
        "cap_drop": container["cap_drop"], "resource_limits": container["resource_limits"], "mounts": container["mounts"],
    }


def _canary_digest() -> str:
    return hashlib.sha256(_CANARY_DENIED_OUTPUT).hexdigest()


def validate_denial_canary(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CANARY_KEYS or value.get("schema") != CANARY_SCHEMA:
        raise IsolationError("isolation_live_denial_canary_required")
    if value.get("mode") != "container_probe" or value.get("target") != _CANARY_TARGET or value.get("attempted") is not True or value.get("denied") is not True or value.get("readable") is not False:
        raise IsolationError("isolation_denial_canary_readable")
    if value.get("output_sha256") != _canary_digest():
        raise IsolationError("isolation_denial_canary_output_digest_invalid")
    return dict(value)


def validate_synthetic_denial_canary(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a plan-only rehearsal result; it contains no live evidence."""

    if not isinstance(value, Mapping) or set(value) != _SYNTHETIC_CANARY_KEYS or value.get("schema") != SYNTHETIC_CANARY_SCHEMA:
        raise IsolationError("isolation_synthetic_denial_canary_invalid")
    if (
        value.get("mode") != "plan_only_rehearsal"
        or value.get("target") != _CANARY_TARGET
        or value.get("live_evidence") is not False
        or value.get("plan_target_unmounted") is not True
    ):
        raise IsolationError("isolation_synthetic_denial_canary_invalid")
    return dict(value)


def run_synthetic_denial_canary(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a plan-only result without claiming a container denial.

    The canary target is intentionally not one of the legal mount targets.  A
    real Docker probe is deliberately not implemented here; only its
    externally normalized live result may enter :func:`validate_denial_canary`.
    """

    checked = validate_isolation_plan(plan)
    if any(row["target"] == _CANARY_TARGET for row in checked["mounts"]):
        raise IsolationError("isolation_denial_canary_readable")
    result = {
        "schema": SYNTHETIC_CANARY_SCHEMA,
        "mode": "plan_only_rehearsal",
        "target": _CANARY_TARGET,
        "live_evidence": False,
        "plan_target_unmounted": True,
    }
    return validate_synthetic_denial_canary(result)


def run_denial_canary(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    """Fail closed: this checkpoint has no live container probe runner."""

    raise IsolationError("isolation_live_denial_canary_runner_unimplemented")


def _attestation_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "attestation_sha256"}


def make_attestation(
    *,
    plan: Mapping[str, Any],
    engine_observed_digest: str,
    inspect_summary: Mapping[str, Any],
    denial_canary: Mapping[str, Any],
) -> dict[str, Any]:
    checked = validate_isolation_plan(plan)
    _hex(engine_observed_digest, "isolation_engine_observed_digest_invalid")
    summary = validate_inspect_summary(checked, inspect_summary)
    canary = validate_denial_canary(denial_canary)
    unsigned = {
        "schema": ATTESTATION_SCHEMA,
        "plan_sha256": checked["plan_sha256"],
        "engine_observed_digest": engine_observed_digest,
        "repo_observed_digest": summary["repo_digest"],
        "config_image_observed_digest": summary["config_image_id"],
        "inspect_summary": summary,
        "inspect_sha256": canonical_sha256(summary),
        "denial_canary": canary,
        "denial_canary_output_sha256": canary["output_sha256"],
    }
    return {**unsigned, "attestation_sha256": canonical_sha256(unsigned)}


attest_isolation = make_attestation


def make_synthetic_rehearsal_attestation(
    *, plan: Mapping[str, Any], denial_canary: Mapping[str, Any]
) -> dict[str, Any]:
    """Create an explicitly non-formal plan-only rehearsal attestation."""

    checked = validate_isolation_plan(plan)
    canary = validate_synthetic_denial_canary(denial_canary)
    unsigned = {
        "schema": SYNTHETIC_ATTESTATION_SCHEMA,
        "synthetic_test_mode": True,
        "formal_eligible": False,
        "plan_sha256": checked["plan_sha256"],
        "denial_canary": canary,
    }
    return {**unsigned, "attestation_sha256": canonical_sha256(unsigned)}


def validate_synthetic_rehearsal_attestation(
    value: Mapping[str, Any], *, plan: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate only the non-formal rehearsal envelope."""

    checked = validate_isolation_plan(plan)
    if not isinstance(value, Mapping) or set(value) != _SYNTHETIC_ATTESTATION_KEYS or value.get("schema") != SYNTHETIC_ATTESTATION_SCHEMA:
        raise IsolationError("isolation_synthetic_attestation_invalid")
    row = dict(value)
    if row.get("synthetic_test_mode") is not True or row.get("formal_eligible") is not False or row.get("plan_sha256") != checked["plan_sha256"]:
        raise IsolationError("isolation_synthetic_attestation_invalid")
    validate_synthetic_denial_canary(row.get("denial_canary"))
    if row.get("attestation_sha256") != canonical_sha256(_attestation_unsigned(row)):
        raise IsolationError("isolation_synthetic_attestation_digest_invalid")
    return row


make_rehearsal_attestation = make_synthetic_rehearsal_attestation
validate_rehearsal_attestation = validate_synthetic_rehearsal_attestation


def validate_attestation(value: Mapping[str, Any], *, plan: Mapping[str, Any]) -> dict[str, Any]:
    checked = validate_isolation_plan(plan)
    if not isinstance(value, Mapping) or set(value) != _ATTESTATION_KEYS or value.get("schema") != ATTESTATION_SCHEMA:
        raise IsolationError("isolation_attestation_invalid")
    row = dict(value)
    if row.get("plan_sha256") != checked["plan_sha256"]:
        raise IsolationError("isolation_attestation_plan_mismatch")
    _hex(row.get("engine_observed_digest"), "isolation_engine_observed_digest_invalid")
    summary = validate_inspect_summary(checked, row.get("inspect_summary"))
    if row.get("repo_observed_digest") != summary["repo_digest"] or row.get("config_image_observed_digest") != summary["config_image_id"]:
        raise IsolationError("isolation_attestation_image_binding_invalid")
    if row.get("inspect_sha256") != canonical_sha256(summary):
        raise IsolationError("isolation_inspect_digest_invalid")
    canary = validate_denial_canary(row.get("denial_canary"))
    if row.get("denial_canary_output_sha256") != canary["output_sha256"]:
        raise IsolationError("isolation_denial_canary_output_digest_invalid")
    expected = canonical_sha256(_attestation_unsigned(row))
    if row.get("attestation_sha256") != expected:
        raise IsolationError("isolation_attestation_digest_invalid")
    return row


def check_docker_readiness(
    plan: Mapping[str, Any],
    *,
    docker_executable: str = "docker",
    runner: Callable[..., Any] | None = None,
) -> bool:
    """Return whether Docker daemon and the exact pinned image are available.

    Any CLI/daemon/image failure returns ``False``.  No caller may interpret
    this as permission to use a normal subprocess; the formal gate remains
    closed and :func:`require_docker_readiness` raises instead.
    """

    checked = validate_isolation_plan(plan)
    if not isinstance(docker_executable, str) or not docker_executable:
        raise IsolationError("isolation_docker_executable_invalid")
    run = runner or subprocess.run
    try:
        daemon = run(
            [docker_executable, "version", "--format", "{{.Server.ID}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if getattr(daemon, "returncode", 1) != 0 or not str(getattr(daemon, "stdout", "")).strip():
            return False
        repo = run(
            [docker_executable, "image", "inspect", checked["image"], "--format", "{{json .RepoDigests}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if getattr(repo, "returncode", 1) != 0:
            return False
        try:
            repo_digests = json.loads(str(getattr(repo, "stdout", "")).strip())
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(repo_digests, list) or not repo_digests or any(not isinstance(item, str) for item in repo_digests):
            return False
        if any(not _IMAGE_RE.fullmatch(item) for item in repo_digests) or checked["image"] not in repo_digests:
            return False

        config = run(
            [docker_executable, "image", "inspect", checked["image"], "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        observed = str(getattr(config, "stdout", "")).strip()
        # Docker's config image ID is normally ``sha256:<hex>``.  Accept
        # exactly one prefix and compare it to the plan's separately committed
        # config digest; this is intentionally not the repo/manifest digest.
        if getattr(config, "returncode", 1) != 0 or not observed.startswith("sha256:"):
            return False
        observed_config_digest = observed[len("sha256:") :]
        if observed_config_digest.startswith("sha256:") or not _DIGEST_RE.fullmatch(observed_config_digest):
            return False
        return observed_config_digest == checked["image_config_digest"]
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False


docker_readiness = check_docker_readiness


def require_docker_readiness(plan: Mapping[str, Any], **kwargs: Any) -> None:
    if not check_docker_readiness(plan, **kwargs):
        raise DockerUnavailable("docker_daemon_or_pinned_image_unavailable")


def require_formal_isolation() -> None:
    if not FORMAL_ISOLATION_ENABLED or SYNTHETIC_ISOLATION_ONLY:
        raise IsolationError("aerp7_formal_isolation_disabled")


def _stdout_bytes(result: Any, *, code: str) -> bytes:
    if getattr(result, "returncode", 1) != 0:
        raise IsolationError(code)
    stdout = getattr(result, "stdout", b"")
    stderr = getattr(result, "stderr", b"")
    if isinstance(stdout, str):
        stdout = stdout.encode("utf-8")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    if not isinstance(stdout, bytes) or not isinstance(stderr, bytes):
        raise IsolationError(code)
    return stdout


def _run_fixed(runner: Callable[..., Any], command: Sequence[str], *, timeout_seconds: int, code: str) -> Any:
    try:
        result = runner(list(command), capture_output=True, text=False, timeout=timeout_seconds, check=False)
    except (OSError, subprocess.SubprocessError, TimeoutError) as exc:
        raise IsolationError(code) from exc
    _stdout_bytes(result, code=code)
    stderr = getattr(result, "stderr", b"")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    if stderr != b"":
        raise IsolationError(code)
    return result


def _json_inspect(result: Any, *, code: str) -> dict[str, Any]:
    stdout = _stdout_bytes(result, code=code)
    try:
        decoded = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolationError(code) from exc
    if isinstance(decoded, list):
        if len(decoded) != 1:
            raise IsolationError(code)
        decoded = decoded[0]
    if not isinstance(decoded, Mapping):
        raise IsolationError(code)
    return dict(decoded)


def _fixed_output(result: Any, expected: bytes, *, code: str) -> str:
    stdout = _stdout_bytes(result, code=code)
    stderr = getattr(result, "stderr", b"")
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8")
    if stderr != b"" or stdout != expected:
        raise IsolationError(code)
    return hashlib.sha256(stdout).hexdigest()


def _validate_output_observation(value: Any, *, before_release: bool) -> dict[str, Any]:
    expected = _OUTPUT_ABSENT_KEYS if before_release else _OUTPUT_PRESENT_KEYS
    if not isinstance(value, Mapping) or set(value) != expected:
        raise IsolationError("isolation_output_validation_invalid")
    if before_release:
        if value.get("release_exists") is not False:
            raise IsolationError("isolation_release_exists_before_attestation")
        if value.get("output_exists") is not False:
            raise IsolationError("isolation_output_exists_before_attestation")
        return {"release_exists": False, "output_exists": False}
    if value.get("release_exists") is not True:
        raise IsolationError("isolation_release_output_missing")
    if value.get("output_exists") is not True:
        raise IsolationError("isolation_final_output_missing")
    release_content = _hex(value.get("release_content_sha256"), "isolation_output_validation_invalid")
    output = _hex(value.get("output_sha256"), "isolation_output_validation_invalid")
    return {"release_exists": True, "output_exists": True, "release_content_sha256": release_content, "output_sha256": output}


def _inspect_summary_from_container(container: Mapping[str, Any], image: Mapping[str, str]) -> dict[str, Any]:
    return {
        "repo_digest": image["repo_digest"], "config_image_id": container["config_image_id"],
        "configured_image": container["configured_image"],
        "path": container["path"], "args": container["args"], "working_dir": container["working_dir"],
        "network_mode": container["network_mode"], "user": container["user"],
        "rootfs_read_only": container["rootfs_read_only"], "no_new_privileges": container["no_new_privileges"],
        "security_options": container["security_options"],
        "restart_policy": container["restart_policy"], "privileged": container["privileged"],
        "cap_drop": container["cap_drop"], "resource_limits": container["resource_limits"], "mounts": container["mounts"],
    }


def _live_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "attestation_sha256"}


def _gate_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "pre_release_gate_sha256"}


def _nullable_digest(value: Any, code: str) -> str | None:
    if value is None:
        return None
    return _hex(value, code)


def make_pre_release_gate(
    *,
    plan: Mapping[str, Any],
    container_id: str,
    host_init_pid: int,
    image_observation: Mapping[str, Any],
    image_inspect_sha256: str,
    created_inspect_summary: Mapping[str, Any],
    created_inspect_sha256: str,
    running_inspect_summary: Mapping[str, Any],
    running_inspect_sha256: str,
    ready_output_sha256: str,
    denial_canary: Mapping[str, Any],
    output_absence: Mapping[str, Any],
    launch_config_sha256: str | None = None,
) -> dict[str, Any]:
    """Mint the immutable gate immediately before the exclusive RELEASE call."""

    checked = validate_isolation_plan(plan)
    _container_id(container_id)
    if isinstance(host_init_pid, bool) or not isinstance(host_init_pid, int) or host_init_pid <= 0:
        raise IsolationError("isolation_pre_release_gate_invalid")
    image = dict(image_observation) if isinstance(image_observation, Mapping) else None
    if image != {"repo_digest": checked["image"], "config_image_id": checked["image_config_digest"]}:
        raise IsolationError("isolation_pre_release_gate_image_invalid")
    for value, code in ((image_inspect_sha256, "isolation_live_image_digest_invalid"), (created_inspect_sha256, "isolation_live_inspect_digest_invalid"), (running_inspect_sha256, "isolation_live_inspect_digest_invalid")):
        _hex(value, code)
    created = validate_inspect_summary(checked, created_inspect_summary)
    running = validate_inspect_summary(checked, running_inspect_summary)
    if ready_output_sha256 != hashlib.sha256(_READY_OUTPUT).hexdigest():
        raise IsolationError("isolation_ready_barrier_invalid")
    canary = validate_denial_canary(denial_canary)
    absence = _validate_output_observation(output_absence, before_release=True)
    launch = _nullable_digest(launch_config_sha256, "isolation_launch_config_digest_invalid")
    unsigned = {
        "schema": PRE_RELEASE_GATE_SCHEMA, "plan_sha256": checked["plan_sha256"], "container_id": container_id,
        "host_init_pid": host_init_pid, "launch_config_sha256": launch, "image_observation": image,
        "image_inspect_sha256": image_inspect_sha256, "created_inspect_summary": created,
        "created_inspect_sha256": created_inspect_sha256, "running_inspect_summary": running,
        "running_inspect_sha256": running_inspect_sha256, "ready_output_sha256": ready_output_sha256,
        "denial_canary": canary, "output_absence": absence, "output_absence_sha256": canonical_sha256(absence),
    }
    return {**unsigned, "pre_release_gate_sha256": canonical_sha256(unsigned)}


def validate_pre_release_gate(value: Mapping[str, Any], *, plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _PRE_RELEASE_GATE_KEYS or value.get("schema") != PRE_RELEASE_GATE_SCHEMA:
        raise IsolationError("isolation_pre_release_gate_invalid")
    row = dict(value)
    # Re-use the constructor's invariant checks, then compare the generated
    # canonical gate byte-for-byte.  This avoids a parallel validation path.
    rebuilt = make_pre_release_gate(
        plan=plan, container_id=row.get("container_id"), host_init_pid=row.get("host_init_pid"),
        image_observation=row.get("image_observation"), image_inspect_sha256=row.get("image_inspect_sha256"),
        created_inspect_summary=row.get("created_inspect_summary"), created_inspect_sha256=row.get("created_inspect_sha256"),
        running_inspect_summary=row.get("running_inspect_summary"), running_inspect_sha256=row.get("running_inspect_sha256"),
        ready_output_sha256=row.get("ready_output_sha256"), denial_canary=row.get("denial_canary"),
        output_absence=row.get("output_absence"), launch_config_sha256=row.get("launch_config_sha256"),
    )
    if row != rebuilt:
        raise IsolationError("isolation_pre_release_gate_digest_invalid")
    return row


def validate_release_receipt(
    value: Mapping[str, Any], *, plan: Mapping[str, Any], pre_release_gate: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate the canonical helper response before it is allowed to publish."""

    checked = validate_isolation_plan(plan)
    gate = validate_pre_release_gate(pre_release_gate, plan=checked)
    if not isinstance(value, Mapping) or set(value) != _RELEASE_RECEIPT_KEYS or value.get("schema") != RELEASE_RECEIPT_SCHEMA:
        raise IsolationError("isolation_release_receipt_invalid")
    row = dict(value)
    if (
        row.get("plan_sha256") != checked["plan_sha256"]
        or row.get("container_id") != gate["container_id"]
        or row.get("pre_release_gate_sha256") != gate["pre_release_gate_sha256"]
        or row.get("launch_config_sha256") != gate["launch_config_sha256"]
    ):
        raise IsolationError("isolation_release_binding_invalid")
    _hex(row.get("release_content_sha256"), "isolation_release_content_digest_invalid")
    return row


def _release_receipt_from_result(result: Any, *, plan: Mapping[str, Any], gate: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Accept only an exact canonical, stderr-free structured helper response."""

    stdout = _stdout_bytes(result, code="isolation_release_failed")
    try:
        parsed = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IsolationError("isolation_release_receipt_invalid") from exc
    if not isinstance(parsed, Mapping) or _bytes(parsed) != stdout:
        raise IsolationError("isolation_release_receipt_invalid")
    receipt = validate_release_receipt(parsed, plan=plan, pre_release_gate=gate)
    return receipt, hashlib.sha256(stdout).hexdigest()


def validate_live_rehearsal_attestation(value: Mapping[str, Any], *, plan: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the v2 live-container receipt, which remains non-formal."""

    checked = validate_isolation_plan(plan)
    if not isinstance(value, Mapping) or set(value) != _LIVE_REHEARSAL_KEYS or value.get("schema") != LIVE_REHEARSAL_SCHEMA:
        raise IsolationError("isolation_live_rehearsal_attestation_invalid")
    row = dict(value)
    if row.get("synthetic_test_mode") is not True or row.get("formal_eligible") is not False or row.get("plan_sha256") != checked["plan_sha256"]:
        raise IsolationError("isolation_live_rehearsal_attestation_invalid")
    container_id = _container_id(row.get("container_id"))
    pid = row.get("host_init_pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise IsolationError("isolation_live_rehearsal_attestation_invalid")
    image = row.get("image_observation")
    if not isinstance(image, Mapping) or set(image) != _IMAGE_OBSERVATION_KEYS:
        raise IsolationError("isolation_live_image_observation_invalid")
    if dict(image) != {"repo_digest": checked["image"], "config_image_id": checked["image_config_digest"]}:
        raise IsolationError("isolation_live_image_observation_invalid")
    _hex(row.get("image_inspect_sha256"), "isolation_live_image_digest_invalid")
    created = validate_inspect_summary(checked, row.get("created_inspect_summary"))
    running = validate_inspect_summary(checked, row.get("running_inspect_summary"))
    _hex(row.get("created_inspect_sha256"), "isolation_live_inspect_digest_invalid")
    _hex(row.get("running_inspect_sha256"), "isolation_live_inspect_digest_invalid")
    if row.get("ready_output_sha256") != hashlib.sha256(_READY_OUTPUT).hexdigest():
        raise IsolationError("isolation_ready_barrier_invalid")
    canary = validate_denial_canary(row.get("denial_canary"))
    gate = validate_pre_release_gate(row.get("pre_release_gate"), plan=checked)
    if row.get("pre_release_gate_sha256") != gate["pre_release_gate_sha256"]:
        raise IsolationError("isolation_pre_release_gate_digest_invalid")
    if (
        gate["container_id"] != container_id or gate["host_init_pid"] != pid
        or gate["image_observation"] != image or gate["denial_canary"] != canary
        or gate["created_inspect_summary"] != created or gate["running_inspect_summary"] != running
        or gate["ready_output_sha256"] != row["ready_output_sha256"]
        or gate["image_inspect_sha256"] != row["image_inspect_sha256"]
        or gate["created_inspect_sha256"] != row["created_inspect_sha256"]
        or gate["running_inspect_sha256"] != row["running_inspect_sha256"]
    ):
        raise IsolationError("isolation_pre_release_gate_binding_invalid")
    release = validate_release_receipt(row.get("release_receipt"), plan=checked, pre_release_gate=gate)
    if row.get("release_receipt_sha256") != canonical_sha256(release) or row.get("release_content_sha256") != release["release_content_sha256"]:
        raise IsolationError("isolation_release_receipt_invalid")
    if row.get("release_output_sha256") != canonical_sha256(release) or row.get("wait_output_sha256") != hashlib.sha256(_WAIT_SUCCESS_OUTPUT).hexdigest():
        raise IsolationError("isolation_release_receipt_invalid")
    output = _validate_output_observation(row.get("output_validation"), before_release=False)
    if output["release_content_sha256"] != release["release_content_sha256"]:
        raise IsolationError("isolation_release_content_binding_invalid")
    if row.get("output_validation_sha256") != canonical_sha256(output):
        raise IsolationError("isolation_output_validation_digest_invalid")
    if row.get("attestation_sha256") != canonical_sha256(_live_unsigned(row)):
        raise IsolationError("isolation_live_rehearsal_attestation_digest_invalid")
    # Keep these locals deliberately evaluated: they make the receipt's key
    # identities explicit in this validator rather than merely schema-shaped.
    _ = (container_id, pid, canary)
    return row


def run_isolated_rehearsal(
    plan: Mapping[str, Any],
    *,
    runner: Callable[..., Any],
    output_validator: Callable[[str, str], Mapping[str, Any]],
    docker_executable: str = "docker",
    timeout_seconds: int = 30,
    issued_container_ids: set[str] | None = None,
    launch_config_sha256: str | None = None,
) -> dict[str, Any]:
    """Exercise the exact Docker lifecycle through an injectable backend.

    This is intentionally a test/rehearsal seam.  It does not fall back to a
    host subprocess, cannot enable formal eligibility, and always removes the
    one exact container ID it created.  ``output_validator`` is called twice:
    ``before_release`` must prove RELEASE absent; ``after_wait`` proves the
    released output exists and supplies its digest.
    """

    checked = validate_isolation_plan(plan)
    if not callable(runner) or not callable(output_validator):
        raise IsolationError("isolation_lifecycle_backend_invalid")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise IsolationError("isolation_lifecycle_timeout_invalid")
    executable = _docker_executable(docker_executable)
    launch = _nullable_digest(launch_config_sha256, "isolation_launch_config_digest_invalid")
    container_id: str | None = None
    primary_error: BaseException | None = None
    try:
        create = _run_fixed(runner, docker_create_command(checked, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_create_failed")
        create_stdout = _stdout_bytes(create, code="isolation_create_failed")
        if getattr(create, "stderr", b"") not in {b"", ""}:
            raise IsolationError("isolation_create_failed")
        try:
            container_id = _container_id(create_stdout.decode("ascii").strip())
        except UnicodeDecodeError as exc:
            raise IsolationError("isolation_container_id_invalid") from exc
        if issued_container_ids is not None:
            if container_id in issued_container_ids:
                raise IsolationError("isolation_container_id_reused")
            issued_container_ids.add(container_id)

        created_result = _run_fixed(runner, docker_container_inspect_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_created_inspect_failed")
        created_raw = _json_inspect(created_result, code="isolation_created_inspect_failed")
        created = normalize_container_inspect(created_raw, expected_container_id=container_id)
        if created["state"] != "created" or created["host_init_pid"] != 0:
            raise IsolationError("isolation_created_state_invalid")

        image_result = _run_fixed(runner, docker_image_inspect_command(checked, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_image_inspect_failed")
        image_raw = _json_inspect(image_result, code="isolation_image_inspect_failed")
        image = normalize_image_inspect(image_raw, expected_repo_digest=checked["image"])
        if image["config_image_id"] != checked["image_config_digest"] or created["config_image_id"] != image["config_image_id"]:
            raise IsolationError("isolation_image_config_binding_invalid")
        created_summary = _inspect_summary_from_container(created, image)
        validate_inspect_summary(checked, created_summary)

        _fixed_output(_run_fixed(runner, docker_start_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_start_failed"), (container_id + "\n").encode("ascii"), code="isolation_start_failed")
        ready_digest = _fixed_output(_run_fixed(runner, docker_ready_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_ready_barrier_invalid"), _READY_OUTPUT, code="isolation_ready_barrier_invalid")
        running_result = _run_fixed(runner, docker_container_inspect_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_running_inspect_failed")
        running_raw = _json_inspect(running_result, code="isolation_running_inspect_failed")
        running = normalize_container_inspect(running_raw, expected_container_id=container_id)
        if running["state"] != "running" or running["host_init_pid"] <= 0 or running["config_image_id"] != image["config_image_id"]:
            raise IsolationError("isolation_running_state_invalid")
        running_summary = _inspect_summary_from_container(running, image)
        validate_inspect_summary(checked, running_summary)

        canary_result = _run_fixed(runner, docker_canary_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_live_denial_canary_failed")
        canary_digest = _fixed_output(canary_result, _CANARY_DENIED_OUTPUT, code="isolation_live_denial_canary_failed")
        canary = validate_denial_canary({"schema": CANARY_SCHEMA, "mode": "container_probe", "target": _CANARY_TARGET, "attempted": True, "denied": True, "readable": False, "output_sha256": canary_digest})
        absence = _validate_output_observation(output_validator(container_id, "before_release"), before_release=True)
        gate = make_pre_release_gate(
            plan=checked, container_id=container_id, host_init_pid=running["host_init_pid"], image_observation=image,
            image_inspect_sha256=canonical_sha256(image_raw), created_inspect_summary=created_summary,
            created_inspect_sha256=canonical_sha256(created_raw), running_inspect_summary=running_summary,
            running_inspect_sha256=canonical_sha256(running_raw), ready_output_sha256=ready_digest,
            denial_canary=canary, output_absence=absence, launch_config_sha256=launch,
        )
        release_result = _run_fixed(
            runner,
            docker_release_command(container_id, plan_sha256=checked["plan_sha256"], pre_release_gate_sha256=gate["pre_release_gate_sha256"], launch_config_sha256=launch, docker_executable=executable),
            timeout_seconds=timeout_seconds, code="isolation_release_failed",
        )
        release, release_digest = _release_receipt_from_result(release_result, plan=checked, gate=gate)
        wait_result = _run_fixed(runner, docker_wait_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_wait_failed")
        wait_digest = _fixed_output(wait_result, _WAIT_SUCCESS_OUTPUT, code="isolation_worker_exit_nonzero")
        output = _validate_output_observation(output_validator(container_id, "after_wait"), before_release=False)
        image_sha = canonical_sha256(image_raw)
        unsigned = {
            "schema": LIVE_REHEARSAL_SCHEMA, "synthetic_test_mode": True, "formal_eligible": False,
            "plan_sha256": checked["plan_sha256"], "container_id": container_id, "host_init_pid": running["host_init_pid"],
            "image_observation": image, "image_inspect_sha256": image_sha,
            "created_inspect_summary": created_summary, "created_inspect_sha256": canonical_sha256(created_raw),
            "running_inspect_summary": running_summary, "running_inspect_sha256": canonical_sha256(running_raw),
            "ready_output_sha256": ready_digest, "denial_canary": canary, "pre_release_gate": gate,
            "pre_release_gate_sha256": gate["pre_release_gate_sha256"], "release_receipt": release,
            "release_receipt_sha256": canonical_sha256(release), "release_content_sha256": release["release_content_sha256"], "release_output_sha256": release_digest,
            "wait_output_sha256": wait_digest, "output_validation": output, "output_validation_sha256": canonical_sha256(output),
        }
        return validate_live_rehearsal_attestation({**unsigned, "attestation_sha256": canonical_sha256(unsigned)}, plan=checked)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if container_id is not None:
            try:
                remove = _run_fixed(runner, docker_remove_command(container_id, docker_executable=executable), timeout_seconds=timeout_seconds, code="isolation_cleanup_failed")
                _fixed_output(remove, (container_id + "\n").encode("ascii"), code="isolation_cleanup_failed")
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                raise IsolationError("isolation_cleanup_failed") from cleanup_error


__all__ = [
    "ATTESTATION_SCHEMA",
    "CANARY_SCHEMA",
    "LIVE_REHEARSAL_SCHEMA",
    "PRE_RELEASE_GATE_SCHEMA",
    "RELEASE_RECEIPT_SCHEMA",
    "SYNTHETIC_ATTESTATION_SCHEMA",
    "SYNTHETIC_CANARY_SCHEMA",
    "DockerUnavailable",
    "FORMAL_ISOLATION_ENABLED",
    "IsolationError",
    "PLAN_SCHEMA",
    "SYNTHETIC_ISOLATION_ONLY",
    "attest_isolation",
    "build_docker_command",
    "build_isolation_plan",
    "canonical_plan_digest",
    "canonical_sha256",
    "check_docker_readiness",
    "docker_readiness",
    "docker_canary_command",
    "docker_container_inspect_command",
    "docker_create_command",
    "docker_image_inspect_command",
    "docker_ready_command",
    "docker_release_command",
    "docker_remove_command",
    "docker_run_command",
    "docker_start_command",
    "docker_wait_command",
    "expected_inspect_summary",
    "expected_worker_argv",
    "make_attestation",
    "make_pre_release_gate",
    "make_rehearsal_attestation",
    "make_synthetic_rehearsal_attestation",
    "normalize_docker_inspect",
    "normalize_container_inspect",
    "normalize_image_inspect",
    "require_docker_readiness",
    "require_formal_isolation",
    "run_denial_canary",
    "run_isolated_rehearsal",
    "run_synthetic_denial_canary",
    "validate_attestation",
    "validate_denial_canary",
    "validate_inspect_summary",
    "validate_isolation_plan",
    "validate_live_rehearsal_attestation",
    "validate_pre_release_gate",
    "validate_release_receipt",
    "validate_rehearsal_attestation",
    "validate_synthetic_denial_canary",
    "validate_synthetic_rehearsal_attestation",
]
