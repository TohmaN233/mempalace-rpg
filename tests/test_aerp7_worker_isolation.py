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
CONTAINER_ID = "c" * 64


def _image_raw(plan):
    return {"Id": "sha256:" + CONFIG_DIGEST, "RepoDigests": [plan["image"]]}


def _container_raw(plan, *, state="running", pid=4242, container_id=CONTAINER_ID):
    return {
        "Id": container_id,
        "Image": "sha256:" + CONFIG_DIGEST,
        "Path": plan["container_argv"][0],
        "Args": plan["container_argv"][1:],
        "Config": {"Image": plan["image"], "User": "65532:65532", "WorkingDir": plan["workdir"]},
        "HostConfig": {
            "NetworkMode": "none", "ReadonlyRootfs": True,
            "SecurityOpt": ["no-new-privileges:true"], "CapDrop": ["ALL"], "CapAdd": [],
            "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0}, "Privileged": False,
            "PidsLimit": 256, "Memory": 1_000_000_000, "NanoCpus": 1_000_000_000,
        },
        "Mounts": [
            {"Type": "bind", "Destination": row["target"], "Source": row["source"], "RW": row["mode"] == "rw"}
            for row in plan["mounts"]
        ],
        "State": {"Status": state, "Running": state == "running", "Pid": pid if state == "running" else 0},
    }


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
    raw = _container_raw(plan)
    assert isolation.normalize_docker_inspect(raw, image_raw=_image_raw(plan), expected_repo_digest=plan["image"]) == expected
    with pytest.raises(isolation.IsolationError, match="resource_limits"):
        isolation.validate_inspect_summary(
            plan,
            {**expected, "resource_limits": {"pids": 256, "memory": "1g", "cpus": "1.0"}},
        )


def test_inspect_identity_is_taken_from_raw_and_bound_to_plan(tmp_path):
    plan = _plan(tmp_path)
    raw = _container_raw(plan)
    other_config = {**raw, "Image": "sha256:" + "e" * 64}
    observed = isolation.normalize_docker_inspect(other_config, image_raw=_image_raw(plan), expected_repo_digest=plan["image"])
    with pytest.raises(isolation.IsolationError, match="policy_mismatch"):
        isolation.validate_inspect_summary(plan, observed)
    with pytest.raises(isolation.IsolationError, match="repo_digest_mismatch"):
        isolation.normalize_docker_inspect(raw, image_raw=_image_raw(plan), expected_repo_digest="ghcr.io/example/other@sha256:" + IMAGE_DIGEST)

    wrong_command = {**raw, "Path": "/opt/aerp7/bin/other-worker"}
    observed = isolation.normalize_docker_inspect(wrong_command, image_raw=_image_raw(plan), expected_repo_digest=plan["image"])
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


class _FakeDocker:
    """Strict command-order fake; it never invokes a real Docker daemon."""

    def __init__(self, plan, overrides=None):
        self.plan = plan
        self.overrides = overrides or {}
        self.commands = []
        self.inspect_count = 0

    @staticmethod
    def _result(stdout=b"", stderr=b"", returncode=0):
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    def _value(self, name, default):
        value = self.overrides.get(name, default)
        if isinstance(value, BaseException):
            raise value
        return value

    def runner(self, argv, **_kwargs):
        self.commands.append(list(argv))
        if argv[1] == "create":
            return self._value("create", self._result((CONTAINER_ID + "\n").encode()))
        if argv[1:3] == ["container", "inspect"]:
            self.inspect_count += 1
            state = "created" if self.inspect_count == 1 else "running"
            raw = self._value("created" if state == "created" else "running", _container_raw(self.plan, state=state))
            return self._value("created_result" if state == "created" else "running_result", self._result(json_bytes([raw])))
        if argv[1:3] == ["image", "inspect"]:
            return self._value("image_result", self._result(json_bytes([self._value("image", _image_raw(self.plan))])))
        if argv[1] == "start":
            return self._value("start", self._result((CONTAINER_ID + "\n").encode()))
        if argv[1] == "exec":
            tool = argv[3]
            if tool.endswith("ready"):
                return self._value("ready", self._result(b"AERP7 READY\n"))
            if tool.endswith("denial-canary"):
                return self._value("canary", self._result(b"aerp7-denial-canary:denied\n"))
            if tool.endswith("release"):
                payload = {
                    "schema": isolation.RELEASE_RECEIPT_SCHEMA,
                    "plan_sha256": argv[argv.index("--plan-sha256") + 1],
                    "container_id": argv[argv.index("--container-id") + 1],
                    "pre_release_gate_sha256": argv[argv.index("--pre-release-gate-sha256") + 1],
                    "launch_config_sha256": argv[argv.index("--launch-config-sha256") + 1] if "--launch-config-sha256" in argv else None,
                    "release_content_sha256": "e" * 64,
                }
                override = self.overrides.get("release_payload", payload)
                payload = override(payload) if callable(override) else override
                return self._value("release", self._result(json_bytes(payload)))
        if argv[1] == "wait":
            return self._value("wait", self._result(b"0\n"))
        if argv[1:3] == ["container", "rm"]:
            return self._value("remove", self._result((CONTAINER_ID + "\n").encode()))
        raise AssertionError(f"unexpected Docker command: {argv}")


def json_bytes(value):
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _output_validator(events):
    def validate(container_id, phase):
        events.append((phase, container_id))
        if phase == "before_release":
            return {"release_exists": False, "output_exists": False}
        assert phase == "after_wait"
        return {"release_exists": True, "output_exists": True, "release_content_sha256": "e" * 64, "output_sha256": "f" * 64}
    return validate


def test_live_rehearsal_observes_raw_image_and_container_separately(tmp_path):
    plan = _plan(tmp_path)
    fake, events, issued = _FakeDocker(plan), [], set()
    receipt = isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=_output_validator(events), issued_container_ids=issued)
    assert receipt["schema"] == isolation.LIVE_REHEARSAL_SCHEMA
    assert receipt["formal_eligible"] is False
    assert receipt["container_id"] == CONTAINER_ID
    assert receipt["host_init_pid"] == 4242
    assert isolation.validate_live_rehearsal_attestation(receipt, plan=plan) == receipt
    assert events == [("before_release", CONTAINER_ID), ("after_wait", CONTAINER_ID)]
    assert [command[1:3] for command in fake.commands] == [
        ["create", "--network"], ["container", "inspect"], ["image", "inspect"], ["start", CONTAINER_ID],
        ["exec", CONTAINER_ID], ["container", "inspect"], ["exec", CONTAINER_ID], ["exec", CONTAINER_ID],
        ["wait", CONTAINER_ID], ["container", "rm"],
    ]
    assert all(command[-1] != "/bin/sh" for command in fake.commands)


@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"image": {"Id": "sha256:" + "e" * 64, "RepoDigests": ["ghcr.io/example/aerp7-worker@sha256:" + IMAGE_DIGEST]}}, "image_config_binding"),
        ({"created": _container_raw, "created_result": None}, "created_inspect"),
        ({"running": lambda plan: {**_container_raw(plan), "Id": "b" * 64}}, "container_id_mismatch"),
        ({"running": lambda plan: {**_container_raw(plan), "State": {"Status": "created", "Running": False, "Pid": 0}}}, "running_state"),
        ({"running": lambda plan: {**_container_raw(plan), "Mounts": []}}, "policy_mismatch"),
        ({"running": lambda plan: {**_container_raw(plan), "Config": {**_container_raw(plan)["Config"], "Image": "ghcr.io/example/aerp7-worker@sha256:" + "b" * 64}}}, "policy_mismatch"),
        ({"running": lambda plan: {**_container_raw(plan), "HostConfig": {**_container_raw(plan)["HostConfig"], "RestartPolicy": {"Name": "always", "MaximumRetryCount": 0}}}}, "restart_policy"),
        ({"running": lambda plan: {**_container_raw(plan), "HostConfig": {**_container_raw(plan)["HostConfig"], "Privileged": True}}}, "privileged"),
        ({"running": lambda plan: {**_container_raw(plan), "HostConfig": {**_container_raw(plan)["HostConfig"], "CapAdd": ["SYS_ADMIN"]}}}, "cap_add"),
        ({"running": lambda plan: {**_container_raw(plan), "Mounts": [{**_container_raw(plan)["Mounts"][0], "Type": "volume"}, *_container_raw(plan)["Mounts"][1:]]}}, "mount_type"),
        ({"running": lambda plan: {**_container_raw(plan), "HostConfig": {**_container_raw(plan)["HostConfig"], "SecurityOpt": ["no-new-privileges:true", "seccomp=unconfined"]}}}, "security_options"),
    ],
)
def test_live_rehearsal_rejects_adversarial_inspect_evidence(tmp_path, override, error):
    plan = _plan(tmp_path)
    prepared = {}
    for key, value in override.items():
        prepared[key] = value(plan) if callable(value) and key not in {"created_result"} else value
    # A malformed raw result is represented as non-JSON command output rather
    # than a Python None passed into the fake's JSON encoder.
    if "created_result" in prepared and prepared["created_result"] is None:
        prepared["created_result"] = _FakeDocker._result(b"not-json")
    fake = _FakeDocker(plan, prepared)
    with pytest.raises(isolation.IsolationError, match=error):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=_output_validator([]))
    assert fake.commands[-1][1:3] == ["container", "rm"]


@pytest.mark.parametrize(
    ("name", "result", "error"),
    [
        ("ready", _FakeDocker._result(b""), "ready_barrier"),
        ("ready", _FakeDocker._result(b"NOT READY\n"), "ready_barrier"),
        ("canary", _FakeDocker._result(b"wrong\n"), "live_denial_canary"),
        ("wait", _FakeDocker._result(b"137\n"), "worker_exit_nonzero"),
        ("release", _FakeDocker._result(b"", returncode=137), "release_failed"),
        ("wait", TimeoutError(), "wait_failed"),
    ],
)
def test_live_rehearsal_rejects_bad_barrier_canary_and_worker_outcomes(tmp_path, name, result, error):
    plan = _plan(tmp_path)
    fake = _FakeDocker(plan, {name: result})
    with pytest.raises(isolation.IsolationError, match=error):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=_output_validator([]))
    assert fake.commands[-1][1:3] == ["container", "rm"]


def test_live_rehearsal_requires_release_absent_before_attestation(tmp_path):
    plan, fake = _plan(tmp_path), None

    def early(_container_id, phase):
        assert phase == "before_release"
        return {"release_exists": True, "output_exists": False}

    fake = _FakeDocker(plan)
    with pytest.raises(isolation.IsolationError, match="release_exists_before_attestation"):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=early)
    assert not any(command[1] == "wait" for command in fake.commands)
    assert fake.commands[-1][1:3] == ["container", "rm"]


def test_live_rehearsal_rejects_final_output_written_before_release(tmp_path):
    plan = _plan(tmp_path)

    def early_output(_container_id, phase):
        assert phase == "before_release"
        return {"release_exists": False, "output_exists": True}

    fake = _FakeDocker(plan)
    with pytest.raises(isolation.IsolationError, match="output_exists_before_attestation"):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=early_output)
    assert not any(command[1] == "wait" for command in fake.commands)
    assert fake.commands[-1] == ["docker", "container", "rm", "--force", CONTAINER_ID]


def test_final_output_release_content_must_match_structured_helper_receipt(tmp_path):
    plan = _plan(tmp_path)

    def mismatch(_container_id, phase):
        if phase == "before_release":
            return {"release_exists": False, "output_exists": False}
        return {"release_exists": True, "output_exists": True, "release_content_sha256": "1" * 64, "output_sha256": "f" * 64}

    fake = _FakeDocker(plan)
    with pytest.raises(isolation.IsolationError, match="release_content_binding"):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=mismatch)
    assert fake.commands[-1] == ["docker", "container", "rm", "--force", CONTAINER_ID]


def test_live_rehearsal_rejects_reused_id_and_cleanup_failure(tmp_path):
    plan = _plan(tmp_path)
    fake = _FakeDocker(plan)
    with pytest.raises(isolation.IsolationError, match="container_id_reused"):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=_output_validator([]), issued_container_ids={CONTAINER_ID})
    assert fake.commands[-1][1:3] == ["container", "rm"]

    bad_cleanup = _FakeDocker(plan, {"remove": _FakeDocker._result(b"", returncode=1)})
    with pytest.raises(isolation.IsolationError, match="cleanup_failed"):
        isolation.run_isolated_rehearsal(plan, runner=bad_cleanup.runner, output_validator=_output_validator([]))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda receipt: {**receipt, "plan_sha256": "1" * 64},
        lambda receipt: {**receipt, "container_id": "2" * 64},
        lambda receipt: {**receipt, "pre_release_gate_sha256": "3" * 64},
    ],
)
def test_release_helper_response_is_bound_to_plan_container_and_pre_release_gate(tmp_path, tamper):
    plan = _plan(tmp_path)
    fake = _FakeDocker(plan, {"release_payload": tamper})
    with pytest.raises(isolation.IsolationError, match="release_binding"):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=_output_validator([]))
    release = next(command for command in fake.commands if command[1] == "exec" and command[3].endswith("release"))
    assert release[release.index("--plan-sha256") + 1] == plan["plan_sha256"]
    assert release[release.index("--container-id") + 1] == CONTAINER_ID
    assert len(release[release.index("--pre-release-gate-sha256") + 1]) == 64
    assert fake.commands[-1] == ["docker", "container", "rm", "--force", CONTAINER_ID]


def test_post_start_failure_force_removes_exact_running_container_and_rejects_stderr(tmp_path):
    plan = _plan(tmp_path)
    fake = _FakeDocker(plan, {"ready": _FakeDocker._result(b"AERP7 READY\n", stderr=b"unexpected")})
    with pytest.raises(isolation.IsolationError, match="ready_barrier"):
        isolation.run_isolated_rehearsal(plan, runner=fake.runner, output_validator=_output_validator([]))
    assert fake.commands[-1] == ["docker", "container", "rm", "--force", CONTAINER_ID]


def test_fixed_lifecycle_command_builders_cannot_take_arbitrary_arguments(tmp_path):
    plan = _plan(tmp_path)
    create = isolation.docker_create_command(plan)
    assert create[:2] == ["docker", "create"] and "--rm" not in create
    assert create[create.index("--restart") + 1] == "no"
    assert isolation.docker_ready_command(CONTAINER_ID)[-2:] == ["/opt/aerp7/bin/aerp7-ready", "--barrier"]
    assert isolation.docker_canary_command(CONTAINER_ID)[-1] == "/opt/aerp7/bin/aerp7-denial-canary"
    release = isolation.docker_release_command(CONTAINER_ID, plan_sha256=plan["plan_sha256"], pre_release_gate_sha256="b" * 64)
    assert release[release.index("--exclusive-create") + 1] == "/outputs/result/RELEASE"
    assert release[release.index("--plan-sha256") + 1] == plan["plan_sha256"]
