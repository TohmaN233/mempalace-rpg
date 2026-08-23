"""Synthetic tests for the AERP-7 Docker isolation seam.

No fixture in this file points at, discovers, hashes, or opens the official
ConvoMem tree.  Host paths are empty temporary paths used only as plan
metadata.
"""
from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from benchmarks import aerp7_worker_isolation as isolation


IMAGE_DIGEST = "a" * 64
CONFIG_DIGEST = "d" * 64


def _plan(tmp_path: Path, role: str = "original_product"):
    mounts = [
        {"kind": "protocol", "source": str(tmp_path / "protocol"), "target": "/inputs/protocol", "mode": "ro"},
        {"kind": "candidate", "source": str(tmp_path / "candidate"), "target": "/inputs/candidate", "mode": "ro"},
        {"kind": "config", "source": str(tmp_path / "config"), "target": "/inputs/config", "mode": "ro"},
        {"kind": "model", "source": str(tmp_path / "model"), "target": "/inputs/model", "mode": "ro"},
        {"kind": "source", "source": str(tmp_path / "source"), "target": "/inputs/source", "mode": "ro"},
        {"kind": "output", "source": str(tmp_path / "output"), "target": "/outputs/result", "mode": "rw"},
        {"kind": "palace", "source": str(tmp_path / "palace"), "target": "/outputs/palace", "mode": "rw"},
    ]
    if role not in {"original_product", "original_0", "original_1", "original_2", "original_3", "original_4"}:
        mounts = [row for row in mounts if row["kind"] not in {"source", "palace"}]
    return isolation.build_isolation_plan(
        role=role,
        image=f"ghcr.io/example/aerp7-worker@sha256:{IMAGE_DIGEST}",
        image_config_digest=CONFIG_DIGEST,
        container_argv=isolation.expected_worker_argv(role),
        mounts=mounts,
        staging_generation_root=str(tmp_path / "generation"),
        forbidden_source_roots=[str(tmp_path / "custody"), str(tmp_path / "scorer"), str(tmp_path / "siblings")],
    )


def _rebind(plan, **changes):
    row = dict(plan)
    row.update(changes)
    row["plan_sha256"] = isolation.canonical_plan_digest(row)
    return row


def _live_canary_fixture():
    """Normalized external-probe shape; this test does not run a live probe."""

    return {
        "schema": isolation.CANARY_SCHEMA,
        "mode": "container_probe",
        "target": "/inputs/forbidden-secret",
        "attempted": True,
        "denied": True,
        "readable": False,
        "output_sha256": isolation._canary_digest(),
    }


def test_plan_and_command_are_digest_pinned_and_fixed_shape(tmp_path):
    plan = _plan(tmp_path)
    assert plan["plan_sha256"] == isolation.canonical_plan_digest(plan)
    command = isolation.docker_run_command(plan)
    assert command[:4] == ["docker", "run", "--rm", "--network"]
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == "65532:65532"
    assert "--read-only" in command
    assert command[command.index("--security-opt") + 1] == "no-new-privileges:true"
    assert command[command.index("--cap-drop") + 1] == "ALL"
    assert command[command.index("--pids-limit") + 1] == "256"
    assert command[command.index("--memory") + 1] == "1g"
    assert command[command.index("--cpus") + 1] == "1.0"
    assert command.count("--mount") == 7
    assert "--privileged" not in command
    assert command[-4:] == [plan["image"], "/opt/aerp7/bin/original-worker", "--build-id", "original_product"]


def test_tag_only_or_tag_plus_digest_image_is_rejected(tmp_path):
    with pytest.raises(isolation.IsolationError, match="digest_pinned"):
        isolation.build_isolation_plan(
            role="candidate_ranker",
            image="ghcr.io/example/aerp7-worker:latest",
            image_config_digest=CONFIG_DIGEST,
            container_argv=["/worker"],
            mounts=[],
        )
    plan = _plan(tmp_path)
    with pytest.raises(isolation.IsolationError, match="digest_pinned"):
        isolation.validate_isolation_plan(_rebind(plan, image="ghcr.io/example/aerp7-worker:stable@sha256:" + IMAGE_DIGEST))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("network_mode", "bridge", "runtime_policy"),
        ("rootfs_read_only", False, "runtime_policy"),
        ("no_new_privileges", False, "runtime_policy"),
        ("cap_drop", [], "capability_policy"),
        ("user", {"uid": 0, "gid": 0}, "nonroot"),
        ("resource_limits", {"pids": 0, "memory": "1g", "cpus": "1.0"}, "pids"),
    ],
)
def test_runtime_policy_tampering_fails_closed(tmp_path, field, value, message):
    plan = _plan(tmp_path)
    with pytest.raises(isolation.IsolationError, match=message):
        isolation.validate_isolation_plan(_rebind(plan, **{field: value}))


def test_input_mount_cannot_become_writable_or_extra_mount_visible(tmp_path):
    plan = _plan(tmp_path)
    writable_input = [dict(row) for row in plan["mounts"]]
    writable_input[0]["mode"] = "rw"
    with pytest.raises(isolation.IsolationError, match="mount_policy"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=writable_input))

    extra = [dict(row) for row in plan["mounts"]]
    extra.append({"kind": "secrets", "source": str(tmp_path / "private"), "target": "/private", "mode": "ro"})
    with pytest.raises(isolation.IsolationError, match="mount_invalid|mount_kind"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=extra))


def test_role_specific_mount_least_capability_is_strict(tmp_path):
    current = _plan(tmp_path / "current", role="current")
    assert {row["kind"] for row in current["mounts"]} == {"protocol", "candidate", "config", "model", "output"}
    with pytest.raises(isolation.IsolationError, match="coverage"):
        isolation.validate_isolation_plan(
            _rebind(
                current,
                mounts=current["mounts"]
                + [
                    {"kind": "source", "source": str(tmp_path / "current-source"), "target": "/inputs/source", "mode": "ro"}
                ],
            )
        )
    with pytest.raises(isolation.IsolationError, match="coverage"):
        isolation.validate_isolation_plan(
            _rebind(current, mounts=[row for row in current["mounts"] if row["kind"] != "output"])
        )

    original = _plan(tmp_path / "original", role="original_0")
    assert {row["kind"] for row in original["mounts"]} == {
        "protocol", "candidate", "config", "model", "source", "output", "palace"
    }
    with pytest.raises(isolation.IsolationError, match="coverage"):
        isolation.validate_isolation_plan(
            _rebind(original, mounts=[row for row in original["mounts"] if row["kind"] != "source"])
        )


def test_role_worker_identity_and_entrypoint_are_bound(tmp_path):
    current = _plan(tmp_path / "current", role="current")
    with pytest.raises(isolation.IsolationError, match="worker_identity"):
        isolation.validate_isolation_plan(
            _rebind(current, container_argv=["/opt/aerp7/bin/original-worker", "--build-id", "current"])
        )
    original = _plan(tmp_path / "original", role="original_0")
    with pytest.raises(isolation.IsolationError, match="worker_identity"):
        isolation.validate_isolation_plan(
            _rebind(original, container_argv=["/opt/aerp7/bin/original-worker", "--build-id", "original_1"])
        )


def test_staging_and_forbidden_roots_reject_parent_and_child_overlap(tmp_path):
    plan = _plan(tmp_path)
    broad_staging = [dict(row) for row in plan["mounts"]]
    broad_staging[0]["source"] = str(tmp_path / "generation" / "candidate")
    with pytest.raises(isolation.IsolationError, match="staging_generation"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=broad_staging))

    broad_forbidden = [dict(row) for row in plan["mounts"]]
    broad_forbidden[0]["source"] = str(tmp_path / "safe")
    with pytest.raises(isolation.IsolationError, match="forbidden_mount"):
        isolation.validate_isolation_plan(
            _rebind(plan, mounts=broad_forbidden, forbidden_source_roots=[str(tmp_path)])
        )


def test_filesystem_root_is_not_a_mount_source(tmp_path):
    with pytest.raises(isolation.IsolationError, match="source_invalid"):
        isolation._as_path(str(Path(tmp_path.anchor)), "isolation_mount_source_invalid")


def test_staging_sibling_and_forbidden_mounts_are_rejected(tmp_path):
    plan = _plan(tmp_path)
    broad = [dict(row) for row in plan["mounts"]]
    broad[0]["source"] = str(tmp_path / "generation")
    with pytest.raises(isolation.IsolationError, match="staging_generation"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=broad))

    sibling = [dict(row) for row in plan["mounts"]]
    sibling[0]["source"] = str(tmp_path / "siblings" / "other-output")
    with pytest.raises(isolation.IsolationError, match="forbidden_mount"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=sibling))

    custody = [dict(row) for row in plan["mounts"]]
    custody[0]["source"] = str(tmp_path / "custody" / "bundle")
    with pytest.raises(isolation.IsolationError, match="forbidden_mount"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=custody))


def test_overlapping_mounts_cannot_expose_unlisted_siblings(tmp_path):
    plan = _plan(tmp_path)
    overlapping = [dict(row) for row in plan["mounts"]]
    overlapping[1]["source"] = str(tmp_path / "container-root")
    overlapping[2]["source"] = str(tmp_path / "container-root" / "config")
    with pytest.raises(isolation.IsolationError, match="overlapping"):
        isolation.validate_isolation_plan(_rebind(plan, mounts=overlapping))


def test_denial_canary_is_unreadable_and_output_is_digest_bound(tmp_path):
    plan = _plan(tmp_path)
    canary = isolation.run_synthetic_denial_canary(plan)
    assert canary["schema"] == isolation.SYNTHETIC_CANARY_SCHEMA
    assert canary["live_evidence"] is False
    assert canary["plan_target_unmounted"] is True
    with pytest.raises(isolation.IsolationError, match="live_denial_canary_required"):
        isolation.validate_denial_canary(canary)
    with pytest.raises(isolation.IsolationError, match="runner_unimplemented"):
        isolation.run_denial_canary(plan)
    live = _live_canary_fixture()
    assert isolation.validate_denial_canary(live) == live
    with pytest.raises(isolation.IsolationError, match="readable"):
        isolation.validate_denial_canary({**live, "readable": True})
    with pytest.raises(isolation.IsolationError, match="output_digest"):
        isolation.validate_denial_canary({**live, "output_sha256": "0" * 64})


def test_attestation_recomputes_plan_inspect_and_canary_digests(tmp_path):
    plan = _plan(tmp_path)
    synthetic = isolation.run_synthetic_denial_canary(plan)
    with pytest.raises(isolation.IsolationError, match="live_denial_canary_required"):
        isolation.make_attestation(
            plan=plan,
            engine_observed_digest="b" * 64,
            inspect_summary=isolation.expected_inspect_summary(plan),
            denial_canary=synthetic,
        )
    canary = _live_canary_fixture()
    engine = "b" * 64
    attestation = isolation.make_attestation(
        plan=plan,
        engine_observed_digest=engine,
        inspect_summary=isolation.expected_inspect_summary(plan),
        denial_canary=canary,
    )
    assert isolation.validate_attestation(attestation, plan=plan) == attestation
    with pytest.raises(isolation.IsolationError, match="inspect"):
        isolation.validate_attestation(
            {**attestation, "inspect_sha256": "0" * 64},
            plan=plan,
        )
    with pytest.raises(isolation.IsolationError, match="plan"):
        isolation.validate_attestation({**attestation, "plan_sha256": "0" * 64}, plan=plan)


def test_rehearsal_attestation_is_explicitly_nonformal(tmp_path):
    plan = _plan(tmp_path)
    canary = isolation.run_synthetic_denial_canary(plan)
    rehearsal = isolation.make_synthetic_rehearsal_attestation(plan=plan, denial_canary=canary)
    assert rehearsal["synthetic_test_mode"] is True
    assert rehearsal["formal_eligible"] is False
    assert isolation.validate_synthetic_rehearsal_attestation(rehearsal, plan=plan) == rehearsal
    with pytest.raises(isolation.IsolationError, match="attestation_invalid"):
        isolation.validate_attestation(rehearsal, plan=plan)


def test_inspect_resources_are_normalized_to_bytes_and_nano_cpus(tmp_path):
    plan = _plan(tmp_path)
    expected = isolation.expected_inspect_summary(plan)
    assert expected["resource_limits"] == {"pids": 256, "memory_bytes": 1_000_000_000, "nano_cpus": 1_000_000_000}
    raw = {
        "Image": "sha256:" + CONFIG_DIGEST,
        "RepoDigests": [plan["image"]],
        "Path": plan["container_argv"][0],
        "Args": plan["container_argv"][1:],
        "Config": {"User": "65532:65532", "WorkingDir": plan["workdir"]},
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"],
            "CapDrop": ["ALL"],
            "PidsLimit": 256,
            "Memory": 1_000_000_000,
            "NanoCpus": 1_000_000_000,
        },
        "Mounts": [
            {"Destination": row["target"], "Source": row["source"], "RW": row["mode"] == "rw"}
            for row in plan["mounts"]
        ],
    }
    assert isolation.normalize_docker_inspect(raw, expected_repo_digest=plan["image"]) == expected
    with pytest.raises(isolation.IsolationError, match="resource_limits"):
        isolation.validate_inspect_summary(
            plan,
            {**expected, "resource_limits": {"pids": 256, "memory": "1g", "cpus": "1.0"}},
        )


def test_inspect_identity_is_taken_from_raw_and_bound_to_plan(tmp_path):
    plan = _plan(tmp_path)
    raw = {
        "Image": "sha256:" + CONFIG_DIGEST,
        "RepoDigests": [plan["image"]],
        "Path": plan["container_argv"][0],
        "Args": plan["container_argv"][1:],
        "Config": {"User": "65532:65532", "WorkingDir": plan["workdir"]},
        "HostConfig": {
            "NetworkMode": "none", "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"], "CapDrop": ["ALL"],
            "PidsLimit": 256, "Memory": 1_000_000_000, "NanoCpus": 1_000_000_000,
        },
        "Mounts": [
            {"Destination": row["target"], "Source": row["source"], "RW": row["mode"] == "rw"}
            for row in plan["mounts"]
        ],
    }
    other_config = {**raw, "Image": "sha256:" + "e" * 64}
    observed = isolation.normalize_docker_inspect(other_config, expected_repo_digest=plan["image"])
    with pytest.raises(isolation.IsolationError, match="policy_mismatch"):
        isolation.validate_inspect_summary(plan, observed)
    with pytest.raises(isolation.IsolationError, match="repo_digest_mismatch"):
        isolation.normalize_docker_inspect(raw, expected_repo_digest="ghcr.io/example/other@sha256:" + IMAGE_DIGEST)

    wrong_command = {**raw, "Path": "/opt/aerp7/bin/other-worker"}
    observed = isolation.normalize_docker_inspect(wrong_command, expected_repo_digest=plan["image"])
    with pytest.raises(isolation.IsolationError, match="policy_mismatch"):
        isolation.validate_inspect_summary(plan, observed)


def test_readiness_fails_closed_when_daemon_or_image_unavailable(tmp_path):
    plan = _plan(tmp_path)
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args[0])
        raise FileNotFoundError("docker")

    assert isolation.check_docker_readiness(plan, runner=unavailable) is False
    assert len(calls) == 1
    with pytest.raises(isolation.DockerUnavailable, match="unavailable"):
        isolation.require_docker_readiness(plan, runner=unavailable)


def test_readiness_requires_exact_pinned_image(tmp_path):
    plan = _plan(tmp_path)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="docker-server"),
            SimpleNamespace(returncode=0, stdout='["' + plan["image"] + '"]'),
            SimpleNamespace(returncode=0, stdout="sha256:" + "c" * 64),
        ]
    )

    def runner(*args, **kwargs):
        return next(responses)

    assert isolation.check_docker_readiness(plan, runner=runner) is False


@pytest.mark.parametrize("observed", [IMAGE_DIGEST, "sha256:sha256:" + IMAGE_DIGEST, "sha256:" + "a" * 63])
def test_readiness_rejects_noncanonical_image_id_prefix(tmp_path, observed):
    plan = _plan(tmp_path)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="docker-server"),
            SimpleNamespace(returncode=0, stdout='["' + plan["image"] + '"]'),
            SimpleNamespace(returncode=0, stdout=observed),
        ]
    )

    def runner(*args, **kwargs):
        return next(responses)

    assert isolation.check_docker_readiness(plan, runner=runner) is False


def test_readiness_accepts_exactly_one_sha256_prefix(tmp_path):
    plan = _plan(tmp_path)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout="docker-server"),
            SimpleNamespace(returncode=0, stdout='["' + plan["image"] + '"]'),
            SimpleNamespace(returncode=0, stdout="sha256:" + CONFIG_DIGEST),
        ]
    )

    def runner(*args, **kwargs):
        return next(responses)

    assert isolation.check_docker_readiness(plan, runner=runner) is True


def test_formal_gate_never_claims_ready(tmp_path):
    with pytest.raises(isolation.IsolationError, match="formal_isolation_disabled"):
        isolation.require_formal_isolation()
