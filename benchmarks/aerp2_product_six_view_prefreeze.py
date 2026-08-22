"""Publish the ID-free AERP-2 Product Six-View ranking-prefreeze receipt."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks import aerp2_product_six_view_locomo as harness


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--manifest", default=str(harness.MANIFEST_PATH))
    args = parser.parse_args(argv)
    report = harness.run_prefreeze(dataset_path=Path(args.dataset), model_dir=Path(args.model_dir), source_repo=Path(args.source_repo), output=Path(args.output), manifest_path=Path(args.manifest))
    print(json.dumps({"status": report["status"], "product_top10_sha256": report["stream_receipts"]["product_top10_sha256"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
