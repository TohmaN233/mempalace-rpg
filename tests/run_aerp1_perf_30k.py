"""Run the frozen 30k-event performance gate in isolated child processes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any


TESTS_DIR = Path(__file__).resolve().parent
FREEZE_ROOT = TESTS_DIR.parent


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_harness(target_root: Path):
    os.environ["AERP1_PERF_TARGET_ROOT"] = str(target_root)
    sys.path.insert(0, str(TESTS_DIR))
    import aerp1_perf_30k_harness as harness
    return harness


def _failure_report(message: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema": "aerp1-performance-30k-report",
        "version": 1,
        "runtime": state or {},
        "metrics": {},
        "aggregate": {"verdict": "FAIL", "gate_errors": [message]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=("current", "b0"), default="current")
    parser.add_argument("--target-root", default=str(FREEZE_ROOT))
    parser.add_argument("--b0-report")
    parser.add_argument("--worker-output", help=argparse.SUPPRESS)
    parser.add_argument("--worker-db", help=argparse.SUPPRESS)
    parser.add_argument("--repeat-index", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    target_root = Path(args.target_root).resolve()
    harness = _load_harness(target_root)

    if args.worker_output:
        repeat = harness.run_repeat(args.worker_db, mode=args.mode, repeat_index=args.repeat_index)
        _atomic_json(Path(args.worker_output).resolve(), repeat)
        return 0

    state: dict[str, Any] | None = None
    try:
        manifest, manifest_sha256 = harness.load_manifest()
        state = harness.git_state(target_root)
        harness.validate_target(args.mode, manifest, state)
        if state["git_dirty"]:
            report = _failure_report("checkpoint_requires_clean_git_worktree", state)
        else:
            b0_report = None
            b0_report_sha256 = None
            if args.b0_report:
                import hashlib
                b0_raw = Path(args.b0_report).read_bytes()
                b0_report_sha256 = hashlib.sha256(b0_raw).hexdigest()
                b0_report = json.loads(b0_raw)
            repeats = []
            with tempfile.TemporaryDirectory(prefix="aerp1-perf-30k-") as directory:
                temporary_root = Path(directory)
                for repeat_index in range(manifest["measurement"]["process_repeats"]):
                    repeat_output = temporary_root / f"repeat-{repeat_index}.json"
                    command = [
                        sys.executable, str(Path(__file__).resolve()),
                        "--output", str(output),
                        "--mode", args.mode,
                        "--target-root", str(target_root),
                        "--worker-output", str(repeat_output),
                        "--worker-db", str(temporary_root / f"repeat-{repeat_index}.sqlite3"),
                        "--repeat-index", str(repeat_index),
                    ]
                    subprocess.run(command, cwd=target_root, check=True)
                    repeats.append(json.loads(repeat_output.read_text(encoding="utf-8")))
            report = harness.aggregate_repeats(
                repeats,
                mode=args.mode,
                manifest=manifest,
                manifest_sha256=manifest_sha256,
                state=state,
                b0_report=b0_report,
            )
            if b0_report_sha256 is not None:
                report["baseline_gate"]["artifact_sha256"] = b0_report_sha256
            final_state = harness.git_state(target_root)
            report["runtime_after"] = final_state
            if final_state != state:
                report["aggregate"]["gate_errors"].append("git_state_changed_during_measurement")
                report["aggregate"]["verdict"] = "FAIL"
    except Exception as exc:
        report = _failure_report(f"runner_exception:{type(exc).__name__}:{exc}", state)
    _atomic_json(output, report)
    return 0 if report["aggregate"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
