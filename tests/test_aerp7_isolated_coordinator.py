from __future__ import annotations

import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest

import benchmarks.aerp7_isolated_coordinator as coordinator
from benchmarks.aerp7_isolated_coordinator import (
    FORMAL_ELIGIBLE,
    WORKER_LAUNCH_SPECS,
    build_nine_worker_manifest,
    canonical_launch_config_digest,
    canonical_manifest_digest,
    make_host_output_observer,
    validate_nine_worker_manifest,
)
from benchmarks.aerp7_worker_isolation import IsolationError, canonical_plan_digest


IMAGE = "registry.example/aerp7-worker@sha256:" + "a" * 64
CONFIG_ID = "b" * 64
HELPER = "c" * 64
CONTAINER = "d" * 64
OTHER_CONTAINER = "e" * 64


def _directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _inputs(tmp_path: Path) -> dict[str, object]:
    root = _directory(tmp_path / "safe")
    capability = _directory(root / "capability")
    for spec in WORKER_LAUNCH_SPECS:
        role = _directory(capability / spec.supervisor_key)
        _directory(role / "config")
        _directory(role / "output")
        if spec.build_id:
            _directory(role / "palace")
    return {
        "image": IMAGE,
        "image_config_digest": CONFIG_ID,
        "protocol_root": str(_directory(root / "protocol")),
        "candidate_root": str(_directory(root / "candidate")),
        "model_root": str(_directory(root / "model")),
        "source_root": str(_directory(root / "source")),
        "capability_root": str(capability),
        "staging_generation_root": str(_directory(tmp_path / "staging")),
        "private_forbidden_roots": [str(_directory(tmp_path / "private"))],
        "resource_limits": {"pids": 64, "memory": "1g", "cpus": "1.0"},
        "helper_contract_sha256": HELPER,
    }


def _manifest(tmp_path: Path) -> dict:
    return build_nine_worker_manifest(**_inputs(tmp_path))


def _mark_lstat_leaf_as_symlink(monkeypatch, target: Path) -> None:
    """Exercise the Windows-safe lstat seam without requiring symlink privilege."""

    original_lstat = os.lstat
    expected = os.path.normcase(os.path.abspath(target))

    def fake_lstat(path):
        result = original_lstat(path)
        if os.path.normcase(os.path.abspath(path)) == expected:
            row = list(result)
            row[stat.ST_MODE] = stat.S_IFLNK | 0o777
            return os.stat_result(row)
        return result

    monkeypatch.setattr(coordinator.os, "lstat", fake_lstat)


def test_exact_nine_roles_p5_double_run_and_five_originals(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    assert len(WORKER_LAUNCH_SPECS) == len(manifest["workers"]) == 9
    assert manifest["formal_eligible"] is FORMAL_ELIGIBLE is False
    assert [row["supervisor_key"] for row in manifest["workers"]] == [spec.supervisor_key for spec in WORKER_LAUNCH_SPECS]
    plans = {row["plan"]["role"] for row in manifest["workers"]}
    assert {"current_p5_primary", "current_p5_repeat"} <= plans
    assert {f"original_{index}" for index in range(5)} <= plans
    for row in manifest["workers"]:
        config = row["launch_config"]
        assert config["synthetic_test_mode"] is True
        assert config["formal_eligible"] is False
        assert config["plan_sha256"] == row["plan"]["plan_sha256"]


def test_manifest_is_deterministic_and_validates(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    first = build_nine_worker_manifest(**inputs)
    second = build_nine_worker_manifest(**inputs)
    assert first == second
    assert validate_nine_worker_manifest(first) == first


def test_manifest_rejects_single_plan_runtime_policy_tamper_even_after_rehashing(tmp_path: Path) -> None:
    value = deepcopy(_manifest(tmp_path))
    plan = value["workers"][0]["plan"]
    plan["resource_limits"]["pids"] = 63
    plan["plan_sha256"] = canonical_plan_digest(plan)
    config = value["workers"][0]["launch_config"]
    config["plan_sha256"] = plan["plan_sha256"]
    config["launch_config_sha256"] = canonical_launch_config_digest(config)
    key = value["workers"][0]["supervisor_key"]
    value["plan_digests"][key] = plan["plan_sha256"]
    value["launch_config_digests"][key] = config["launch_config_sha256"]
    value["manifest_sha256"] = canonical_manifest_digest(value)
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)


def test_manifest_rejects_launch_helper_tamper_even_after_rehashing(tmp_path: Path) -> None:
    value = deepcopy(_manifest(tmp_path))
    config = value["workers"][0]["launch_config"]
    config["helper_contract_sha256"] = "f" * 64
    config["launch_config_sha256"] = canonical_launch_config_digest(config)
    key = value["workers"][0]["supervisor_key"]
    value["launch_config_digests"][key] = config["launch_config_sha256"]
    value["manifest_sha256"] = canonical_manifest_digest(value)
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("image", IMAGE.replace("a", "f")),
        lambda value: value.__setitem__("image_config_digest", "f" * 64),
        lambda value: value.__setitem__("helper_contract_sha256", "f" * 64),
        lambda value: value["workers"][0]["launch_config"]["container_paths"].__setitem__("output", "/tmp/out"),
        lambda value: value["workers"][0]["launch_config"].__setitem__("supervisor_key", "current-six"),
        lambda value: value["workers"][0]["plan"].__setitem__("image_config_digest", "f" * 64),
    ],
)
def test_manifest_rejects_image_config_helper_role_and_path_tamper(tmp_path: Path, mutate) -> None:
    value = deepcopy(_manifest(tmp_path))
    mutate(value)
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)


def test_manifest_rejects_cross_role_mount_and_duplicate_root(tmp_path: Path) -> None:
    value = deepcopy(_manifest(tmp_path))
    first_output = next(row for row in value["workers"][0]["plan"]["mounts"] if row["kind"] == "output")
    second_output = next(row for row in value["workers"][1]["plan"]["mounts"] if row["kind"] == "output")
    first_output["source"] = second_output["source"]
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)
    duplicate = deepcopy(_manifest(tmp_path))
    second_config = next(row for row in duplicate["workers"][1]["plan"]["mounts"] if row["kind"] == "config")
    first_config = next(row for row in duplicate["workers"][0]["plan"]["mounts"] if row["kind"] == "config")
    second_config["source"] = first_config["source"]
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(duplicate)


def test_manifest_rejects_private_root_and_private_field(tmp_path: Path) -> None:
    value = deepcopy(_manifest(tmp_path))
    value["workers"][0]["launch_config"]["container_paths"]["output"] = value["private_forbidden_roots"][0]
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)
    value = deepcopy(_manifest(tmp_path))
    value["workers"][0]["launch_config"]["evidence"] = "canary"
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)
    for field in ("binding_secret", "message_evidences"):
        value = deepcopy(_manifest(tmp_path))
        value["workers"][0]["launch_config"][field] = "private-canary"
        with pytest.raises(IsolationError):
            validate_nine_worker_manifest(value)


def test_builder_rejects_private_or_sibling_layouts(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["capability_root"] = inputs["private_forbidden_roots"][0]
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)


def test_builder_and_validator_reject_overlapping_input_roots(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["candidate_root"] = inputs["protocol_root"]
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)
    inputs = _inputs(tmp_path / "ancestor")
    inputs["source_root"] = str(Path(inputs["protocol_root"]).parent)
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)
    value = deepcopy(_manifest(tmp_path / "receipt"))
    value["source_root"] = value["protocol_root"]
    value["manifest_sha256"] = canonical_manifest_digest(value)
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)


def test_builder_and_validator_reject_input_inside_capability_root(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs["candidate_root"] = inputs["capability_root"]
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)
    inputs = _inputs(tmp_path / "unused")
    unused = _directory(Path(inputs["capability_root"]) / "unused")
    inputs["model_root"] = str(unused)
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)

    value = deepcopy(_manifest(tmp_path / "receipt"))
    unused = _directory(Path(value["capability_root"]) / "unused")
    value["source_root"] = str(unused)
    # Model the stronger receipt-forgery attempt: make the five original
    # plans agree with the changed top-level source, then recompute every
    # downstream plan/config/map digest before asking the validator to catch
    # the capability-root overlap itself.
    for worker in value["workers"]:
        plan = worker["plan"]
        if plan["role"].startswith("original_"):
            next(mount for mount in plan["mounts"] if mount["kind"] == "source")["source"] = str(unused)
            plan["plan_sha256"] = canonical_plan_digest(plan)
            config = worker["launch_config"]
            config["plan_sha256"] = plan["plan_sha256"]
            config["launch_config_sha256"] = canonical_launch_config_digest(config)
            value["plan_digests"][worker["supervisor_key"]] = plan["plan_sha256"]
            value["launch_config_digests"][worker["supervisor_key"]] = config["launch_config_sha256"]
    value["manifest_sha256"] = canonical_manifest_digest(value)
    with pytest.raises(IsolationError):
        validate_nine_worker_manifest(value)


def test_builder_validator_and_observer_reject_supplied_link_or_reparse_paths(tmp_path: Path, monkeypatch) -> None:
    inputs = _inputs(tmp_path / "input")
    _mark_lstat_leaf_as_symlink(monkeypatch, Path(inputs["protocol_root"]))
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)

    monkeypatch.undo()
    inputs = _inputs(tmp_path / "input-parent")
    _mark_lstat_leaf_as_symlink(monkeypatch, Path(inputs["protocol_root"]).parent)
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)

    monkeypatch.undo()
    inputs = _inputs(tmp_path / "role")
    role_output = Path(inputs["capability_root"]) / "current-raw" / "output"
    _mark_lstat_leaf_as_symlink(monkeypatch, role_output)
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)

    monkeypatch.undo()
    root = _directory(tmp_path / "observer-output")
    _mark_lstat_leaf_as_symlink(monkeypatch, root)
    with pytest.raises(IsolationError):
        make_host_output_observer(str(root), expected_container_id=CONTAINER)
    inputs = _inputs(tmp_path / "two")
    capability = Path(inputs["capability_root"])
    # A role-owned output root may not be a regular file in place of a
    # directory.  The observer test below injects an lstat seam for the
    # symlink case because Windows CI commonly lacks symlink privilege.
    second = capability / "current-p5_primary" / "output"
    second.rmdir()
    second.write_bytes(b"not a directory")
    with pytest.raises(IsolationError):
        build_nine_worker_manifest(**inputs)


def test_observer_before_release_and_after_wait_exact_files(tmp_path: Path) -> None:
    root = _directory(tmp_path / "output")
    observer = make_host_output_observer(str(root), expected_container_id=CONTAINER)
    assert observer(CONTAINER, "before_release") == {"release_exists": False, "output_exists": False}
    (root / "RELEASE").write_bytes(b"release")
    (root / "packet.json").write_bytes(b"packet")
    result = observer(CONTAINER, "after_wait")
    assert result["release_exists"] is True and result["output_exists"] is True
    assert len(result["release_content_sha256"]) == len(result["output_sha256"]) == 64


@pytest.mark.parametrize("name,content", [("RELEASE", b"early"), ("packet.json", b"early"), ("extra", b"x")])
def test_observer_rejects_early_or_extra_artifacts(tmp_path: Path, name: str, content: bytes) -> None:
    root = _directory(tmp_path / "output")
    (root / name).write_bytes(content)
    with pytest.raises(IsolationError):
        make_host_output_observer(str(root), expected_container_id=CONTAINER)(CONTAINER, "before_release")


@pytest.mark.parametrize("name", ["RELEASE", "packet.json"])
def test_observer_rejects_missing_or_empty_files(tmp_path: Path, name: str) -> None:
    root = _directory(tmp_path / "output")
    (root / "RELEASE").write_bytes(b"release")
    (root / "packet.json").write_bytes(b"packet")
    (root / name).write_bytes(b"")
    with pytest.raises(IsolationError):
        make_host_output_observer(str(root), expected_container_id=CONTAINER)(CONTAINER, "after_wait")


def test_observer_rejects_missing_required_file(tmp_path: Path) -> None:
    root = _directory(tmp_path / "output")
    (root / "RELEASE").write_bytes(b"release")
    with pytest.raises(IsolationError):
        make_host_output_observer(str(root), expected_container_id=CONTAINER)(CONTAINER, "after_wait")


def test_observer_rejects_symlink_hardlink_and_container_swap(tmp_path: Path) -> None:
    root = _directory(tmp_path / "output")
    (root / "RELEASE").write_bytes(b"release")
    (root / "packet.json").write_bytes(b"packet")

    def symlink_lstat(path: Path):
        result = os.lstat(path)
        if path.name == "RELEASE":
            row = list(result)
            row[stat.ST_MODE] = stat.S_IFLNK | 0o777
            return os.stat_result(row)
        return result

    with pytest.raises(IsolationError):
        make_host_output_observer(str(root), expected_container_id=CONTAINER, stat_fn=symlink_lstat)(CONTAINER, "after_wait")
    (root / "packet.json").unlink()
    os.link(root / "RELEASE", root / "packet.json")
    with pytest.raises(IsolationError):
        make_host_output_observer(str(root), expected_container_id=CONTAINER)(CONTAINER, "after_wait")
    root2 = _directory(tmp_path / "output2")
    observer = make_host_output_observer(str(root2), expected_container_id=CONTAINER)
    with pytest.raises(IsolationError):
        observer(OTHER_CONTAINER, "before_release")


def test_observer_rejects_read_race(tmp_path: Path) -> None:
    root = _directory(tmp_path / "output")
    (root / "RELEASE").write_bytes(b"release")
    (root / "packet.json").write_bytes(b"packet")

    def racing_reader(path: Path) -> bytes:
        content = path.read_bytes()
        if path.name == "RELEASE":
            path.write_bytes(content + b"!")
        return content

    observer = make_host_output_observer(str(root), expected_container_id=CONTAINER, reader=racing_reader)
    with pytest.raises(IsolationError):
        observer(CONTAINER, "after_wait")


def test_observer_rechecks_first_file_after_second_read(tmp_path: Path) -> None:
    root = _directory(tmp_path / "output")
    release = root / "RELEASE"
    release.write_bytes(b"release")
    (root / "packet.json").write_bytes(b"packet")

    def late_racing_reader(path: Path) -> bytes:
        content = path.read_bytes()
        if path.name == "packet.json":
            release.write_bytes(b"replacement")
        return content

    observer = make_host_output_observer(str(root), expected_container_id=CONTAINER, reader=late_racing_reader)
    with pytest.raises(IsolationError):
        observer(CONTAINER, "after_wait")


def test_observer_rechecks_ctime_and_link_count_after_transient_hardlink(tmp_path: Path) -> None:
    root = _directory(tmp_path / "output")
    release = root / "RELEASE"
    release.write_bytes(b"release")
    (root / "packet.json").write_bytes(b"packet")
    changed = False

    def hardlink_reader(path: Path) -> bytes:
        nonlocal changed
        content = path.read_bytes()
        if path.name == "packet.json":
            link = root / "transient-link"
            os.link(release, link)
            link.unlink()
            changed = True
        return content

    def ctime_stat(path: Path):
        result = os.lstat(path)
        if changed and path.name == "RELEASE":
            row = list(result)
            row[stat.ST_CTIME] = row[stat.ST_CTIME] + 1
            return os.stat_result(row)
        return result

    observer = make_host_output_observer(
        str(root), expected_container_id=CONTAINER, reader=hardlink_reader, stat_fn=ctime_stat
    )
    with pytest.raises(IsolationError):
        observer(CONTAINER, "after_wait")
