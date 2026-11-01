"""Explicit local Qwen/stdio engineering smoke; synthetic facts, no LLM or paid API.

Run with -m mempalace_rpg.play_memory_smoke. Output must be a new directory.
This is an optional operator command, never part of ordinary unit tests.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

from .play_memory_protocol import batch_hash


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--model-tree-sha256", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output)
    if not output.is_absolute():
        parser.error("output must be an absolute new directory")
    output.mkdir(parents=True, exist_ok=False)
    token = secrets.token_urlsafe(32)
    environment = {**os.environ, "PLAY_MEMORY_HOST_TOKEN": token,
                   "PYTHONUTF8": "1", "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    report = {"schema": "play-memory-real-qwen-smoke-v1", "status": "running",
              "paidCalls": 0, "fixture": "synthetic-three-fragments-no-benchmark-gold",
              "modelDirectory": args.model_dir, "modelTreeSha256": args.model_tree_sha256,
              "revision": args.revision, "device": "cuda", "dtype": "bfloat16",
              "semanticAcceptance": "not-measured", "calls": []}
    child = None
    with (output / "stderr.txt").open("w", encoding="utf-8") as diagnostic:
        try:
            child = subprocess.Popen([sys.executable, "-m", "mempalace_rpg.play_memory_server",
                                      "--db", str(output / "memory.sqlite"),
                                      "--profile", "qwen-summary768-lexical-weave-v1",
                                      "--model-dir", args.model_dir,
                                      "--model-tree-sha256", args.model_tree_sha256,
                                      "--model-revision", args.revision],
                                     cwd=Path(__file__).resolve().parent.parent, env=environment,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostic,
                                     text=True, encoding="utf-8")
            request_id = 0

            def call(method, params):
                nonlocal request_id
                request_id += 1
                if method in {"apply_batch", "validate_batch", "open_scope", "close_scope"}:
                    params = {**params, "hostToken": token}
                start = time.perf_counter()
                child.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                              "method": method, "params": params}) + "\n")
                child.stdin.flush()
                raw = child.stdout.readline()
                if not raw:
                    raise RuntimeError(f"Qwen smoke process exited during {method}; see stderr.txt")
                response = json.loads(raw)
                if response.get("id") != request_id or response.get("jsonrpc") != "2.0" or "error" in response:
                    raise RuntimeError(f"Invalid/failed {method} response: {response}")
                report["calls"].append({"method": method, "elapsedMs": (time.perf_counter() - start) * 1000})
                return response["result"]

            capability = call("capabilities", {})
            text = "Alice hides the key in the locked north drawer. Bob hears the meeting is at the bridge. Bob carries a blue book."
            source_hash = hashlib.sha256(text.encode()).hexdigest()

            def span(fragment):
                start = text.encode().index(fragment.encode())
                return [{"sourceId": "source", "version": "v1", "startByte": start,
                         "endByte": start + len(fragment.encode()), "sha256": hashlib.sha256(fragment.encode()).hexdigest()}]

            records = [{"kind": "source", "id": "source", "order": 1, "sourceId": "source",
                        "version": "v1", "text": text, "sha256": source_hash}]
            for identifier, actor, fragment in [
                ("alice-private", "alice", "Alice hides the key in the locked north drawer."),
                ("bob-meeting", "bob", "Bob hears the meeting is at the bridge."),
                ("bob-book", "bob", "Bob carries a blue book.")]:
                records.append({"kind": "event", "id": identifier + "-event", "order": 1,
                                "text": fragment, "sourceSpans": span(fragment)})
                records.append({"kind": "acquisition", "id": identifier, "order": 1,
                                "actorId": actor, "text": fragment, "beliefKind": "fact",
                                "method": "witnessed", "eventIds": [identifier + "-event"],
                                "basisIds": [identifier + "-event"], "sourceSpans": span(fragment)})
            batch = {"schemaVersion": "play-memory-v1", "campaignId": "smoke", "batchId": "batch-1",
                     "predecessorBatchId": None, "previousWorldRevision": 0,
                     "branchId": "main", "epoch": 0, "worldRevision": 1, "records": records}
            batch["payloadHash"] = batch_hash(batch)
            call("validate_batch", {"batch": batch})
            receipt = call("apply_batch", {"batch": batch})
            scope = call("open_scope", {"binding": {
                "campaignId": "smoke", "branchId": "main", "epoch": 0, "worldRevision": 1,
                "storyOrder": 1, "checkpointId": "main:1", "materialRevision": "synthetic-v1",
                "configHash": "0" * 64, "purpose": "actor-knowledge", "actorIds": ["bob"],
                "grants": [], "lineage": [{"branchId": "main", "throughRevision": 1}],
                "maxReadBytes": 10000, "maxReadCalls": 10, "requiredBatchId": "batch-1"}})
            searches = [call("search", {"scopeHandle": scope["scopeHandle"],
                                         "query": "Where is the meeting?", "limit": 10}) for _ in range(2)]
            expected = {"bob-meeting", "bob-book"}
            for result in searches:
                if {row["id"] for row in result["candidates"]} != expected or result["trace"]["authorizedCount"] != 2:
                    raise AssertionError("Real Qwen ranking received an unauthorized/incomplete actor universe")
            if searches[1]["trace"]["ranker"]["cacheMisses"] != 0:
                raise AssertionError("Identical warm Qwen query did not reuse its bounded encoding cache")
            read = call("read", {"scopeHandle": scope["scopeHandle"],
                                 "handle": searches[0]["candidates"][0]["handle"], "representation": "original"})
            if "Alice" in read["text"] or "north drawer" in read["text"] or read["id"] not in expected:
                raise AssertionError("Actor original read escaped its acquired fragment")
            call("close_scope", {"scopeHandle": scope["scopeHandle"]})
            child.stdin.close()
            if child.wait(timeout=30) != 0:
                raise RuntimeError("Qwen service did not close successfully")
            report.update(status="passed", capabilities=capability, receipt=receipt,
                          searchTraces=[r["trace"] for r in searches],
                          rankedIds=[[c["id"] for c in r["candidates"]] for r in searches],
                          read={k: v for k, v in read.items() if k != "text"})
        except BaseException as error:
            report.update(status="failed", error={"type": type(error).__name__, "message": str(error)})
            raise
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            with (output / "receipt.json").open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": report["status"], "receipt": str(output / "receipt.json"), "paidCalls": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
