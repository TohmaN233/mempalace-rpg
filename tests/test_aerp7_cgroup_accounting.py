from __future__ import annotations

from types import SimpleNamespace

import pytest

from benchmarks import aerp7_cgroup_accounting as accounting


H = "a" * 64
Q = "b" * 64
C = "c" * 64
P = "d" * 64
BASE = "/cg/team"


class FakeCgroup:
    def __init__(self):
        self.files = {
            "/proc/self/cgroup": "0::/team\n",
            "/proc/1/cgroup": "0::/team\n",
            "/proc/self/mountinfo": "29 23 0:27 / /cg rw,nosuid,nodev - cgroup2 cgroup rw\n",
            f"{BASE}/cgroup.type": "domain\n",
            f"{BASE}/cgroup.controllers": "cpu memory pids\n",
            f"{BASE}/cpu.stat": "usage_usec 100\nuser_usec 70\nsystem_usec 30\n",
            f"{BASE}/memory.current": "50\n",
            f"{BASE}/memory.peak": "100\n",
            f"{BASE}/memory.events": "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n",
        }
        self.stat = SimpleNamespace(st_dev=1, st_ino=2)

    def read(self, path):
        return self.files[path]

    def stat_reader(self, path):
        assert path == BASE
        return self.stat

    def advance(self, *, usage=3, user=2, system=1, current=60, peak=120, max_event=0):
        self.files[f"{BASE}/cpu.stat"] = f"usage_usec {100 + usage}\nuser_usec {70 + user}\nsystem_usec {30 + system}\n"
        self.files[f"{BASE}/memory.current"] = f"{current}\n"
        self.files[f"{BASE}/memory.peak"] = f"{peak}\n"
        self.files[f"{BASE}/memory.events"] = f"low 0\nhigh 0\nmax {max_event}\noom 0\noom_kill 0\noom_group_kill 0\n"


def meter(fake):
    return accounting.CgroupV2QueryMeter.synthetic(reader=fake.read, stat_reader=fake.stat_reader)


@pytest.mark.parametrize("raw", ["1:name=/x\n", "0::/x\n0::/x\n", "0::/x/../y\n", "0::/x\x00\n"])
def test_unified_cgroup_parser_is_single_v2_and_no_traversal(raw):
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_unified_cgroup(raw)


def test_mountinfo_unescapes_and_containment_is_strict():
    root, point = accounting.parse_cgroup2_mountinfo("1 2 0:1 /team /cg\\040root rw - cgroup2 cgroup rw\n")
    assert (root, point) == ("/team", "/cg root")
    assert accounting._resolve_cgroup_dir(mount_root=root, mount_point=point, relative_path="/team/a") == "/cg root/a"
    with pytest.raises(accounting.CgroupAccountingError, match="outside_mount_root"):
        accounting._resolve_cgroup_dir(mount_root="/team", mount_point="/cg", relative_path="/other")
    with pytest.raises(accounting.CgroupAccountingError, match="not_unique"):
        accounting.parse_cgroup2_mountinfo("1 2 0:1 / /cg rw - cgroup2 cgroup rw\n2 3 0:2 / /cg2 rw - cgroup2 cgroup rw\n")
    with pytest.raises(accounting.CgroupAccountingError, match="escape_invalid"):
        accounting.parse_cgroup2_mountinfo("1 2 0:1 /team /cg\\057root rw - cgroup2 cgroup rw\n")


@pytest.mark.parametrize("field", ["usage_usec", "user_usec", "system_usec"])
def test_cpu_stat_requires_all_unique_nonnegative_fields(field):
    raw = "usage_usec 1\nuser_usec 1\nsystem_usec 1\n"
    bad = "\n".join(line for line in raw.splitlines() if not line.startswith(field)) + "\n"
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_cpu_stat(bad)
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_cpu_stat(raw + "usage_usec 2\n")
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_cpu_stat("usage_usec -1\nuser_usec 1\nsystem_usec 1\n")


def test_memory_parsers_reject_missing_duplicate_or_invalid_values():
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_memory_value("10", label="memory_current")
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_memory_events("low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.parse_memory_events("low 0\nlow 1\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n")


def test_snapshot_rejects_cpu_components_outside_rounding_tolerance_before_a_row_can_publish():
    fake = FakeCgroup()
    fake.files[f"{BASE}/cpu.stat"] = "usage_usec 100\nuser_usec 71\nsystem_usec 32\n"
    with pytest.raises(accounting.CgroupAccountingError, match="cpu_stat_components_mismatch"):
        meter(fake)


def test_identity_receipt_binds_raw_mount_inode_and_revalidates_every_snapshot():
    fake = FakeCgroup(); observed = meter(fake).identity_receipt
    assert observed["required_file_set"] == list(accounting._REQUIRED_FILES)
    assert observed["identity_sha256"] == accounting.canonical_sha256({key: value for key, value in observed.items() if key != "identity_sha256"})
    m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    fake.stat = SimpleNamespace(st_dev=1, st_ino=99)
    with pytest.raises(accounting.CgroupAccountingError, match="identity_drift"):
        m.end()


def test_query_state_machine_includes_descendant_cumulative_cpu_and_not_memory_peak_as_rss():
    fake = FakeCgroup(); failed = meter(fake)
    with pytest.raises(accounting.CgroupAccountingError, match="without_begin"):
        failed.end()
    with pytest.raises(accounting.CgroupAccountingError, match="meter_failed"):
        failed.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    m = meter(fake)
    m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    with pytest.raises(accounting.CgroupAccountingError, match="nested"):
        m.begin(item_id="two", query_sha256=H, container_id=C, plan_sha256=P)
    with pytest.raises(accounting.CgroupAccountingError, match="meter_failed"):
        m.end()
    m = meter(fake)
    m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    # A fake cumulative counter models both a short-lived descendant and the
    # worker: no PID polling can see the child, but cgroup usage does.
    fake.advance(usage=13, user=8, system=5, current=61, peak=150)
    row = m.end()
    assert row["cpu_ns"] == 13_000
    assert row["user_cpu_ns"] == 8_000
    assert row["system_cpu_ns"] == 5_000
    assert row["memory_current_before_bytes"] == 50
    assert row["memory_current_after_bytes"] == 61
    assert "memory_peak" not in row
    receipt = m.final_receipt(container_id=C, plan_sha256=P)
    assert receipt["container_memory_peak_bytes"] == 150
    assert receipt["memory_metric"] == "cgroup_v2_charged_memory"
    assert receipt["per_query_memory_peak_available"] is False
    assert receipt["query_cpu_accounting"] == "cgroup_v2_descendant_inclusive"
    assert accounting.validate_final_receipt(receipt, rows=[row]) == receipt


@pytest.mark.parametrize("kind", ["cpu", "events", "oom", "peak"])
def test_counter_regression_and_memory_pressure_fail_closed_without_row(kind):
    fake = FakeCgroup()
    if kind == "events":
        fake.files[f"{BASE}/memory.events"] = "low 1\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
    m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    if kind == "cpu":
        fake.files[f"{BASE}/cpu.stat"] = "usage_usec 99\nuser_usec 70\nsystem_usec 30\n"
    elif kind == "events":
        fake.files[f"{BASE}/memory.events"] = "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\noom_group_kill 0\n"
    elif kind == "peak":
        fake.files[f"{BASE}/memory.peak"] = "99\n"
    else:
        fake.advance(max_event=1)
    with pytest.raises(accounting.CgroupAccountingError):
        m.end()
    assert m.rows == ()
    with pytest.raises(accounting.CgroupAccountingError, match="meter_failed"):
        m.begin(item_id="two", query_sha256=H, container_id=C, plan_sha256=P)


@pytest.mark.parametrize("key", ["item_id", "query_sha256", "container_id", "plan_sha256"])
def test_row_and_final_receipt_reject_swaps_and_tamper(key):
    fake = FakeCgroup(); m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); row = m.end(); receipt = m.final_receipt(container_id=C, plan_sha256=P)
    swapped = {**row, key: ("two" if key == "item_id" else H)}
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.validate_final_receipt(receipt, rows=[swapped])
    wrong_unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    wrong = {**wrong_unsigned, "memory_metric": "rss"}
    wrong = {**wrong, "receipt_sha256": accounting.canonical_sha256(wrong)}
    with pytest.raises(accounting.CgroupAccountingError):
        accounting.validate_final_receipt(wrong, rows=[row])


def test_formal_factory_rejects_injection_and_non_linux_without_reading_real_machine(monkeypatch):
    with pytest.raises(accounting.CgroupAccountingError, match="injected_reader_forbidden"):
        accounting.CgroupV2QueryMeter.formal(reader=lambda _path: "")
    with pytest.raises(accounting.CgroupAccountingError, match="accounting_disabled"):
        accounting.CgroupV2QueryMeter.formal()
    monkeypatch.setattr(accounting.platform, "system", lambda: "Windows")
    monkeypatch.setattr(accounting, "FORMAL_CGROUP_ACCOUNTING_ENABLED", True)
    monkeypatch.setattr(accounting, "SYNTHETIC_CGROUP_ACCOUNTING_ONLY", False)
    with pytest.raises(accounting.CgroupAccountingError, match="linux_required"):
        accounting.CgroupV2QueryMeter.formal()
    fake = FakeCgroup()
    with pytest.raises(accounting.CgroupAccountingError, match="linux_required"):
        accounting.CgroupV2QueryMeter.synthetic(reader=fake.read, stat_reader=fake.stat_reader, platform_name="Windows")


def test_begin_failure_does_not_leave_an_active_query_or_publish_a_row():
    fake = FakeCgroup(); m = meter(fake)
    del fake.files[f"{BASE}/cpu.stat"]
    with pytest.raises(accounting.CgroupAccountingError, match="cpu_stat_read_failed"):
        m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    assert m.rows == ()
    with pytest.raises(accounting.CgroupAccountingError, match="meter_failed"):
        m.end()


def test_final_receipt_requires_nonempty_coverage_positive_peak_and_post_query_event_quiescence():
    fake = FakeCgroup(); m = meter(fake)
    with pytest.raises(accounting.CgroupAccountingError, match="coverage_empty"):
        m.final_receipt(container_id=C, plan_sha256=P)

    fake = FakeCgroup(); m = meter(fake)
    m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); row = m.end()
    fake.files[f"{BASE}/memory.peak"] = "0\n"
    with pytest.raises(accounting.CgroupAccountingError, match="peak_not_positive"):
        m.final_receipt(container_id=C, plan_sha256=P)

    fake = FakeCgroup(); m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); row = m.end()
    fake.files[f"{BASE}/memory.events"] = "low 0\nhigh 0\nmax 1\noom 0\noom_kill 0\noom_group_kill 0\n"
    with pytest.raises(accounting.CgroupAccountingError, match="final_memory_oom_or_max_event"):
        m.final_receipt(container_id=C, plan_sha256=P)
    assert row["cpu_ns"] > 0


def test_final_receipt_validator_rejects_zero_peak_even_with_recomputed_digest():
    fake = FakeCgroup(); m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); row = m.end(); receipt = m.final_receipt(container_id=C, plan_sha256=P)
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    unsigned["container_memory_peak_bytes"] = 0
    invalid = {**unsigned, "receipt_sha256": accounting.canonical_sha256(unsigned)}
    with pytest.raises(accounting.CgroupAccountingError, match="memory_peak_invalid"):
        accounting.validate_final_receipt(invalid, rows=[row])


def _resign_row(row, **changes):
    unsigned = {key: value for key, value in row.items() if key != "query_row_sha256"}
    unsigned.update(changes)
    return {**unsigned, "query_row_sha256": accounting.canonical_sha256(unsigned)}


def _resign_receipt(receipt, **changes):
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    unsigned.update(changes)
    return {**unsigned, "receipt_sha256": accounting.canonical_sha256(unsigned)}


def test_initial_critical_memory_events_and_between_query_failures_are_permanent():
    initial = FakeCgroup()
    initial.files[f"{BASE}/memory.events"] = "low 0\nhigh 0\nmax 1\noom 0\noom_kill 0\noom_group_kill 0\n"
    with pytest.raises(accounting.CgroupAccountingError, match="initial_memory_oom_or_max"):
        meter(initial)

    gap_oom = FakeCgroup(); m = meter(gap_oom)
    gap_oom.files[f"{BASE}/memory.events"] = "low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\noom_group_kill 0\n"
    with pytest.raises(accounting.CgroupAccountingError, match="begin_memory_oom_or_max"):
        m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    with pytest.raises(accounting.CgroupAccountingError, match="meter_failed"):
        m.begin(item_id="two", query_sha256=H, container_id=C, plan_sha256=P)

    gap_regression = FakeCgroup(); m = meter(gap_regression)
    gap_regression.files[f"{BASE}/memory.peak"] = "99\n"
    with pytest.raises(accounting.CgroupAccountingError, match="begin_memory_peak_counter_regressed"):
        m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P)
    with pytest.raises(accounting.CgroupAccountingError, match="meter_failed"):
        m.begin(item_id="two", query_sha256=H, container_id=C, plan_sha256=P)


def test_meter_and_external_validator_reject_duplicate_items_and_resigned_bad_semantics():
    fake = FakeCgroup(); m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); m.end()
    with pytest.raises(accounting.CgroupAccountingError, match="duplicate_item"):
        m.begin(item_id="one", query_sha256=H, container_id=C, plan_sha256=P)
    with pytest.raises(accounting.CgroupAccountingError, match="meter_incomplete"):
        m.final_receipt(container_id=C, plan_sha256=P)

    fake = FakeCgroup(); m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); row = m.end(); receipt = m.final_receipt(container_id=C, plan_sha256=P)
    critical = _resign_row(row, memory_events_delta={"low": 0, "high": 0, "max": 1, "oom": 0, "oom_kill": 0, "oom_group_kill": 0})
    with pytest.raises(accounting.CgroupAccountingError, match="row_memory_oom_or_max"):
        accounting.validate_final_receipt(receipt, rows=[critical])
    bad_units = _resign_row(row, cpu_ns=row["cpu_ns"] + 1)
    with pytest.raises(accounting.CgroupAccountingError, match="cpu_unit"):
        accounting.validate_final_receipt(receipt, rows=[bad_units])
    bad_components = _resign_row(row, user_cpu_ns=row["user_cpu_ns"] + 3000)
    with pytest.raises(accounting.CgroupAccountingError, match="cpu_components"):
        accounting.validate_final_receipt(receipt, rows=[bad_components])
    duplicate = _resign_row(row, query_sha256=H)
    duplicate_receipt = _resign_receipt(receipt, query_count=2, query_rows_sha256=accounting.canonical_sha256([row, duplicate]))
    order = [{"item_id": value["item_id"], "query_sha256": value["query_sha256"], "query_row_sha256": value["query_row_sha256"]} for value in [row, duplicate]]
    duplicate_receipt = _resign_receipt(duplicate_receipt, query_coverage_order_sha256=accounting.canonical_sha256(order))
    with pytest.raises(accounting.CgroupAccountingError, match="duplicate_item"):
        accounting.validate_final_receipt(duplicate_receipt, rows=[row, duplicate])
    too_low_peak = _resign_receipt(receipt, container_memory_peak_bytes=row["memory_current_after_bytes"] - 1)
    with pytest.raises(accounting.CgroupAccountingError, match="current_exceeds_peak"):
        accounting.validate_final_receipt(too_low_peak, rows=[row])

    components = row["user_cpu_ns"] + row["system_cpu_ns"]
    for residual in (-2000, -1000, 0, 1000, 2000):
        assert accounting.CgroupV2QueryMeter._validate_row(_resign_row(row, cpu_ns=components + residual))["cpu_ns"] == components + residual
    with pytest.raises(accounting.CgroupAccountingError, match="cpu_components"):
        accounting.CgroupV2QueryMeter._validate_row(_resign_row(row, cpu_ns=components + 3000))


def test_final_receipt_validator_checks_final_snapshot_and_final_event_delta_after_resigning():
    fake = FakeCgroup(); m = meter(fake); m.begin(item_id="one", query_sha256=Q, container_id=C, plan_sha256=P); fake.advance(); row = m.end(); receipt = m.final_receipt(container_id=C, plan_sha256=P)
    missing_snapshot = _resign_receipt(receipt, final_snapshot_sha256="not-a-digest")
    with pytest.raises(accounting.CgroupAccountingError, match="snapshot_invalid"):
        accounting.validate_final_receipt(missing_snapshot, rows=[row])
    final_oom = _resign_receipt(receipt, final_memory_events_delta={"low": 0, "high": 0, "max": 0, "oom": 1, "oom_kill": 1, "oom_group_kill": 0})
    with pytest.raises(accounting.CgroupAccountingError, match="receipt_memory_oom_or_max"):
        accounting.validate_final_receipt(final_oom, rows=[row])
