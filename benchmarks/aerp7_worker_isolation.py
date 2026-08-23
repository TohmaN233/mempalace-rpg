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
_INSPECT_KEYS = frozenset(
    {
        "repo_digest",
        "config_image_id",
        "path",
        "args",
        "working_dir",
        "network_mode",
        "user",
        "rootfs_read_only",
        "no_new_privileges",
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
        "path": argv[0],
        "args": argv[1:],
        "working_dir": checked["workdir"],
        "network_mode": "none",
        "user": f"{user['uid']}:{user['gid']}",
        "rootfs_read_only": True,
        "no_new_privileges": True,
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


def normalize_docker_inspect(
    raw: Mapping[str, Any], *, expected_repo_digest: str | None = None, image_digest: str | None = None
) -> dict[str, Any]:
    """Normalize one raw Docker inspect object without trusting caller labels.

    ``expected_repo_digest`` (and the deprecated spelling ``image_digest``) is
    only a commitment to compare against.  The returned repo digest comes from
    the raw ``RepoDigests`` field, while the config image id comes from the raw
    container ``Image`` field.  Path, Args, and WorkingDir are likewise copied
    from inspect and are checked against the plan later.
    """

    if expected_repo_digest is not None and image_digest is not None and expected_repo_digest != image_digest:
        raise IsolationError("isolation_inspect_repo_digest_ambiguous")
    expected = expected_repo_digest if expected_repo_digest is not None else image_digest
    if expected is not None:
        _validate_image(expected, expected.rsplit("@sha256:", 1)[1] if "@sha256:" in expected else None)
    if not isinstance(raw, Mapping):
        raise IsolationError("isolation_inspect_invalid")
    host = raw.get("HostConfig")
    config = raw.get("Config")
    if not isinstance(host, Mapping) or not isinstance(config, Mapping):
        raise IsolationError("isolation_inspect_invalid")

    repo_digests = raw.get("RepoDigests")
    if not isinstance(repo_digests, list) or not repo_digests or any(not isinstance(item, str) for item in repo_digests):
        raise IsolationError("isolation_inspect_repo_digest_invalid")
    valid_repo_digests = [item for item in repo_digests if _IMAGE_RE.fullmatch(item)]
    if len(valid_repo_digests) != len(repo_digests):
        raise IsolationError("isolation_inspect_repo_digest_invalid")
    if expected is not None:
        if expected not in valid_repo_digests:
            raise IsolationError("isolation_inspect_repo_digest_mismatch")
        repo_digest = expected
    elif len(valid_repo_digests) == 1:
        repo_digest = valid_repo_digests[0]
    else:
        raise IsolationError("isolation_inspect_repo_digest_ambiguous")

    # Container inspect uses ``Image`` for the immutable config image ID;
    # ``Id`` is the container identity and must not be confused with it.
    config_id = _config_digest(raw.get("Image"), "isolation_inspect_config_image_id_invalid")
    path, args, working_dir = raw.get("Path"), raw.get("Args"), config.get("WorkingDir")
    if not isinstance(path, str) or not path.startswith("/") or not isinstance(args, list) or any(not isinstance(item, str) for item in args):
        raise IsolationError("isolation_inspect_command_identity_invalid")
    if not isinstance(working_dir, str) or not working_dir.startswith("/"):
        raise IsolationError("isolation_inspect_workdir_invalid")

    security = host.get("SecurityOpt") or []
    cap_drop = host.get("CapDrop") or []
    mounts_raw = raw.get("Mounts")
    if not isinstance(mounts_raw, list):
        raise IsolationError("isolation_inspect_invalid")
    mounts: list[dict[str, str]] = []
    for row in mounts_raw:
        if not isinstance(row, Mapping):
            raise IsolationError("isolation_inspect_invalid")
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
    return {
        "repo_digest": repo_digest,
        "config_image_id": config_id,
        "path": path,
        "args": list(args),
        "working_dir": working_dir,
        "network_mode": host.get("NetworkMode"),
        "user": user,
        "rootfs_read_only": host.get("ReadonlyRootfs"),
        "no_new_privileges": any(item == "no-new-privileges:true" for item in security),
        "cap_drop": list(cap_drop),
        "resource_limits": {"pids": pids, "memory_bytes": memory_bytes, "nano_cpus": nano_cpus},
        "mounts": sorted(mounts, key=lambda row: row["kind"]),
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


__all__ = [
    "ATTESTATION_SCHEMA",
    "CANARY_SCHEMA",
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
    "docker_run_command",
    "expected_inspect_summary",
    "expected_worker_argv",
    "make_attestation",
    "make_rehearsal_attestation",
    "make_synthetic_rehearsal_attestation",
    "normalize_docker_inspect",
    "require_docker_readiness",
    "require_formal_isolation",
    "run_denial_canary",
    "run_synthetic_denial_canary",
    "validate_attestation",
    "validate_denial_canary",
    "validate_inspect_summary",
    "validate_isolation_plan",
    "validate_rehearsal_attestation",
    "validate_synthetic_denial_canary",
    "validate_synthetic_rehearsal_attestation",
]
