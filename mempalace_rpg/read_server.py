"""memory-evidence-v1 JSON-RPC, one request/response per UTF-8 line.

Run with explicit immutable projection and pinned MiniLM manifest paths. Stdout
is exclusively protocol traffic; startup/runtime diagnostics go to stderr.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import logging
import sys
from typing import TextIO

from .evidence_protocol import EvidenceError, parse_rpc, strict_json_loads
from .evidence_store import EvidenceStore
from .runtime_factory import create_encoder

logger = logging.getLogger(__name__)


def serve(store: EvidenceStore, *, input_stream: TextIO, output_stream: TextIO,
          diagnostic_stream: TextIO) -> None:
    try:
        for line in input_stream:
            request_id = None
            try:
                try:
                    value = strict_json_loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise EvidenceError(-32700, "invalid JSON", kind="parse-error") from exc
                if isinstance(value, dict) and type(value.get("id")) in (str, int):
                    request_id = value["id"]
                request_id, method, params = parse_rpc(value)
                # Any third-party print statements are diagnostics, never wire.
                with redirect_stdout(diagnostic_stream):
                    result = store.dispatch(method, params)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
                encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except EvidenceError as exc:
                logger.warning("evidence RPC rejected id=%r kind=%s", request_id, exc.kind)
                response = {"jsonrpc": "2.0", "id": request_id, "error": exc.wire()}
                encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except Exception:
                logger.exception("evidence RPC failed id=%r", request_id)
                error = EvidenceError(-32603, "internal evidence service failure", kind="internal-error")
                response = {"jsonrpc": "2.0", "id": request_id, "error": error.wire()}
                encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            output_stream.write(encoded + "\n")
            output_stream.flush()
    finally:
        with redirect_stdout(diagnostic_stream):
            store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--projection", required=True, help="host-published immutable JSON projection")
    parser.add_argument("--model-dir", required=True, help="complete local all-MiniLM-L6-v2 directory")
    parser.add_argument("--model-tree-sha256", required=True, help="explicit expected model manifest tree SHA-256")
    parser.add_argument("--passage-cache-path", help="absolute host path for the bounded persistent vector cache")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # UTF-8 is explicit on Windows too, independent of the console code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    encoder = None
    try:
        with redirect_stdout(sys.stderr):
            encoder = create_encoder(model_dir=args.model_dir, expected_tree_sha256=args.model_tree_sha256,
                                     passage_cache_path=args.passage_cache_path)
            store = EvidenceStore(args.projection, encoder=encoder)
        serve(store, input_stream=sys.stdin, output_stream=sys.stdout, diagnostic_stream=sys.stderr)
        return 0
    except Exception:
        logger.exception("evidence service failed; no runtime fallback is permitted")
        return 1
    finally:
        if encoder is not None:
            with redirect_stdout(sys.stderr):
                encoder.close()


if __name__ == "__main__":
    raise SystemExit(main())
