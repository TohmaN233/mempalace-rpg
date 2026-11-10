"""Trusted Host/scoped-model JSONL RPC for the portable gameplay-memory core."""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import logging
import os
import sys
import time

_SERVER_STARTED_NS = time.perf_counter_ns()
_SERVER_TIMING_ENABLED = __name__ == "__main__"


def _timing_marker(event, *, duration_ns=None, method=None, request_bytes=None,
                   response_bytes=None, outcome=None):
    if not _SERVER_TIMING_ENABLED:
        return
    now = time.perf_counter_ns()
    record = {
        "event": event,
        "pid": os.getpid(),
        "elapsedMs": round((now - _SERVER_STARTED_NS) / 1_000_000, 3),
    }
    if duration_ns is not None:
        record["durationMs"] = round(duration_ns / 1_000_000, 3)
    if method is not None:
        record["method"] = method
    if request_bytes is not None:
        record["requestBytes"] = request_bytes
    if response_bytes is not None:
        record["responseBytes"] = response_bytes
    if outcome is not None:
        record["outcome"] = outcome
    sys.stderr.write("play-memory-server-timing " + json.dumps(record, separators=(",", ":")) + "\n")
    sys.stderr.flush()


_CORE_IMPORT_STARTED_NS = time.perf_counter_ns()
_timing_marker("core-import-start")
from .evidence_protocol import EvidenceError, fields, strict_json_loads
from .play_memory import PlayMemoryStore
_timing_marker("core-import-complete", duration_ns=time.perf_counter_ns() - _CORE_IMPORT_STARTED_NS)


def serve(store, *, input_stream, output_stream, diagnostic_stream):
    """One request, one response; no retries and no secret-bearing request logs."""
    first_handler_seen = False
    try:
        for line in input_stream:
            request_id = None
            first_handler_started_ns = None
            request_method = None
            request_bytes = 0
            outcome = "success"
            try:
                request_bytes = len(line.encode("utf-8"))
                if request_bytes > 16_000_000:
                    raise EvidenceError(-32600, "play memory frame exceeds limit", kind="invalid-request")
                try:
                    request = strict_json_loads(line)
                except (ValueError, UnicodeError) as exc:
                    raise EvidenceError(-32700, "invalid play memory JSON", kind="parse-error") from exc
                fields(request, {"jsonrpc", "id", "method", "params"})
                if request["jsonrpc"] != "2.0" or type(request["id"]) not in (str, int) or not isinstance(request["method"], str):
                    raise EvidenceError(-32600, "invalid play memory RPC", kind="invalid-request")
                request_id = request["id"]
                request_method = request["method"]
                if not first_handler_seen:
                    first_handler_seen = True
                    first_handler_started_ns = time.perf_counter_ns()
                    _timing_marker("first-handler-enter", method=request_method, request_bytes=request_bytes)
                with redirect_stdout(diagnostic_stream):
                    value = store.dispatch(request["method"], request["params"])
                # Validate before writing: a bad injected ranker cannot corrupt stdout.
                response = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": value}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except EvidenceError as exc:
                outcome = "rpc-error"
                logging.warning("play memory RPC rejected id=%r kind=%s", request_id, exc.kind)
                response = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": exc.wire()}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            except Exception:
                outcome = "internal-error"
                logging.exception("play memory RPC failed id=%r", request_id)
                error = EvidenceError(-32603, "internal play memory service failure", kind="internal-error")
                response = json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error.wire()}, separators=(",", ":"))
            if first_handler_started_ns is not None:
                completed_ns = time.perf_counter_ns()
                _timing_marker(
                    "first-handler-complete",
                    duration_ns=completed_ns - first_handler_started_ns,
                    method=request_method,
                    request_bytes=request_bytes,
                    response_bytes=len(response.encode("utf-8")),
                    outcome=outcome,
                )
            output_stream.write(response + "\n")
            output_stream.flush()
    finally:
        with redirect_stdout(diagnostic_stream):
            store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--host-token", help="prefer secret environment PLAY_MEMORY_HOST_TOKEN")
    parser.add_argument("--profile", choices=("mechanical-bm25-v1", "qwen-summary768-lexical-weave-v1"), default="mechanical-bm25-v1")
    parser.add_argument("--model-dir")
    parser.add_argument("--model-tree-sha256")
    parser.add_argument("--model-revision")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--dtype", default="bfloat16")
    parser.add_argument("--cuda-cache-policy", choices=("retain", "release-after-encode"), default="retain")
    parser.add_argument("--vector-cache-path", help="optional absolute SQLite path for persistent Qwen passage vectors")
    args = parser.parse_args(argv)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    ranker = None
    try:
        ranker_started_ns = time.perf_counter_ns()
        if args.profile == "qwen-summary768-lexical-weave-v1":
            _timing_marker("ranker-init-start")
        with redirect_stdout(sys.stderr):
            if args.profile == "qwen-summary768-lexical-weave-v1":
                if not all((args.model_dir, args.model_tree_sha256, args.model_revision)):
                    parser.error("Qwen profile requires explicit model directory/tree/revision")
                from .play_memory_qwen import QwenRankerConfig, create_qwen_ranker
                ranker = create_qwen_ranker(QwenRankerConfig(model_directory=args.model_dir,
                                             model_tree_sha256=args.model_tree_sha256,
                                             revision=args.model_revision, device=args.device, dtype=args.dtype,
                                             cuda_cache_policy=args.cuda_cache_policy,
                                             vector_cache_path=args.vector_cache_path))
            elif any((args.model_dir, args.model_tree_sha256, args.model_revision,
                      args.cuda_cache_policy != "retain", args.vector_cache_path)):
                parser.error("mechanical profile does not consume model arguments")
        if args.profile == "qwen-summary768-lexical-weave-v1":
            _timing_marker("ranker-init-complete", duration_ns=time.perf_counter_ns() - ranker_started_ns)
        store_started_ns = time.perf_counter_ns()
        _timing_marker("store-init-start")
        with redirect_stdout(sys.stderr):
            store = PlayMemoryStore(args.db, host_token=args.host_token or os.environ.get("PLAY_MEMORY_HOST_TOKEN"), ranker=ranker)
        _timing_marker("store-init-complete", duration_ns=time.perf_counter_ns() - store_started_ns)
        rpc_output = sys.stdout
        if ranker is not None:
            # This RPC process is started by the Host's background memory lane.
            # Keep Windows Torch/Transformers imports on its main thread.
            started = time.perf_counter_ns()
            _timing_marker("encoder-prewarm-start")
            with redirect_stdout(sys.stderr):
                # Discard the vector; memory and ranking caches stay unchanged.
                ranker.encoder.encode_query("记忆检索预热")
            _timing_marker("encoder-prewarm-complete", duration_ns=time.perf_counter_ns() - started)
        serve(store, input_stream=sys.stdin, output_stream=rpc_output, diagnostic_stream=sys.stderr)
        return 0
    except Exception:
        logging.exception("play memory service stopped")
        return 1
    finally:
        if ranker is not None:
            with redirect_stdout(sys.stderr):
                ranker.close()


if __name__ == "__main__":
    raise SystemExit(main())
