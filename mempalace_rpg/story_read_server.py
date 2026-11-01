"""Additive bounded story protocol using the unchanged pinned P5 runtime."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import logging
import sys

from .evidence_protocol import EvidenceError, fields, strict_json_loads
from .runtime_factory import create_encoder
from .story_store import StoryStore


def serve(store, *, input_stream, output_stream, diagnostic_stream):
    """Dedicated method namespace; the legacy parser/service remain unchanged."""
    try:
        for line in input_stream:
            request_id = None
            try:
                if len(line.encode("utf-8")) > 64_000_000:
                    raise EvidenceError(-32600, "story frame too large", kind="invalid-request")
                try:
                    request = strict_json_loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise EvidenceError(-32700, "invalid story JSON", kind="parse-error") from exc
                fields(request, {"jsonrpc", "id", "method", "params"})
                if request["jsonrpc"] != "2.0" or type(request["id"]) not in (str, int) or not isinstance(request["method"], str) or not isinstance(request["params"], dict):
                    raise EvidenceError(-32600, "invalid story RPC", kind="invalid-request")
                request_id = request["id"]
                with redirect_stdout(diagnostic_stream):
                    value = store.dispatch(request["method"], request["params"])
                response = {"jsonrpc": "2.0", "id": request_id, "result": value}
            except EvidenceError as exc:
                logging.warning("story RPC rejected id=%r kind=%s", request_id, exc.kind)
                response = {"jsonrpc": "2.0", "id": request_id, "error": exc.wire()}
            except Exception:
                logging.exception("story RPC failed id=%r", request_id)
                response = {"jsonrpc": "2.0", "id": request_id, "error": EvidenceError(-32603, "internal story service failure", kind="internal-error").wire()}
            output_stream.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            output_stream.flush()
    finally:
        with redirect_stdout(diagnostic_stream):
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--model-tree-sha256", required=True)
    parser.add_argument("--passage-cache-path", help="absolute host path for the bounded persistent vector cache")
    args = parser.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    encoder = None
    try:
        with redirect_stdout(sys.stderr):
            encoder = create_encoder(model_dir=args.model_dir, expected_tree_sha256=args.model_tree_sha256,
                                     passage_cache_path=args.passage_cache_path)
            store = StoryStore(encoder)
        serve(store, input_stream=sys.stdin, output_stream=sys.stdout, diagnostic_stream=sys.stderr)
        return 0
    except Exception:
        logging.exception("story service failed; no encoder fallback permitted")
        return 1
    finally:
        if encoder is not None:
            with redirect_stdout(sys.stderr):
                encoder.close()


if __name__ == "__main__":
    raise SystemExit(main())
