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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="atomic JSON report target")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
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
