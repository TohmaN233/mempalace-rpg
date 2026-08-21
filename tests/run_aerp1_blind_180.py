"""Execute the frozen blind AERP-1 suite and atomically publish its JSON report."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aerp1_blind_180_harness import run_blind_180
from aerp1_audit_harness import _git_state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="atomic JSON report target")
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="reject before execution unless Git is clean, and reject if it changes during execution",
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    before = _git_state()
    try:
        if args.require_clean and before.get("git_dirty") is not False:
            report = {
                "schema": "aerp1-blind-180-report",
                "version": 1,
                "denominators": {
                    "logical_cases": 180,
                    "product_calls": 360,
                    "positive_cases": 90,
                    "negative_cases": 90,
                    "authorization_neutral_cases": 60,
                },
                "calls": [],
                "aggregate": {
                    "verdict": "FAIL",
                    "gate_errors": ["checkpoint_requires_clean_git_worktree"],
                    "forbidden_event_id_leaks": [],
                    "forbidden_span_leaks": [],
                    "forbidden_rendered_leaks": [],
                },
            }
        else:
            with tempfile.TemporaryDirectory(prefix="aerp1-blind-180-") as directory:
                report = run_blind_180(str(Path(directory) / "blind-180.sqlite3"))
    except Exception as exc:
        report = {
            "schema": "aerp1-blind-180-report",
            "version": 1,
            "runtime": {"python": sys.version, "platform": platform.platform()},
            "denominators": {
                "logical_cases": 180,
                "product_calls": 360,
                "positive_cases": 90,
                "negative_cases": 90,
                "authorization_neutral_cases": 60,
            },
            "calls": [],
            "aggregate": {
                "verdict": "FAIL",
                "gate_errors": [f"runner_exception:{type(exc).__name__}:{exc}"],
                "forbidden_event_id_leaks": [],
                "forbidden_span_leaks": [],
                "forbidden_rendered_leaks": [],
            },
        }
    after = _git_state()
    report["runtime"] = {"python": sys.version, "platform": platform.platform(), **after}
    if args.require_clean and before != after:
        report["aggregate"]["gate_errors"].append("git_state_changed_during_execution")
        report["aggregate"]["verdict"] = "FAIL"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 0 if report["aggregate"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
