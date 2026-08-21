"""Run the frozen AERP-1 offline authorization audit from the repository root."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import platform

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aerp1_audit_harness import run_audit


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="path for the atomic JSON audit artifact")
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="fail the audit unless the report is bound to a clean Git worktree",
    )
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="aerp1-audit-") as directory:
            report = run_audit(str(Path(directory) / "aerp1.sqlite3"))
    except Exception as exc:
        report = {
            "schema": "aerp1-offline-audit-report",
            "version": 1,
            "runtime": {"python": sys.version, "platform": platform.platform()},
            "denominators": {"logical_cases": 24, "product_calls": 48, "positive_cases": 12, "negative_cases": 12},
            "calls": [],
            "aggregate": {"verdict": "FAIL", "gate_errors": [f"runner_exception:{type(exc).__name__}:{exc}"], "forbidden_event_id_leaks": [], "forbidden_span_leaks": []},
        }
    if args.require_clean and report.get("runtime", {}).get("git_dirty") is not False:
        report["aggregate"]["gate_errors"].append("checkpoint_requires_clean_git_worktree")
        report["aggregate"]["verdict"] = "FAIL"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, prefix=f".{output.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return 0 if report["aggregate"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
