"""Bounded-memory, custody-separated front half of the AERP-4 LoCoMo study.

The public module has one small interface: ``sanitize``, ``custody`` and
``paired`` CLI stages.  Internally it owns streaming pinning, JSONL shards and
non-clobbering publication so callers cannot accidentally reconnect labels to
the ranking producer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from benchmarks import aerp4_locomo_custody as custody
from benchmarks import aerp4_locomo_paired_receipts as paired
from benchmarks import aerp4_raw_anchored_gate as gate
from benchmarks import aerp4_raw_anchored_gate_prefreeze as prefreeze
from mempalace_rpg.retrieval import RawAnchoredP5Policy


MANIFEST_SCHEMA = "aerp4-locomo-stream-rank-manifest-v1"
CUSTODY_SCHEMA = "aerp4-locomo-stream-custody-bundle-v1"
_VIEWS = tuple(prefreeze.VIEWS)
_BLOCK = 1024 * 1024
FROZEN_MAX_RSS_BYTES = 2_147_483_648


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _stream_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as handle:
        while block := handle.read(_BLOCK):
            digest.update(block); size += len(block)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class StreamInput:
    path: Path
    sha256: str
    byte_count: int
    receipt_path: Path | None = None

    @classmethod
    def pin(cls, path: Path | str, expected_sha256: str) -> "StreamInput":
        target = Path(path).resolve()
        digest = gate._token(expected_sha256, "expected input SHA-256")
        actual, size = _stream_digest(target)
        if actual != digest:
            raise ValueError("input SHA-256 mismatch")
        return cls(target, digest, size, target)

    def verify_unchanged(self) -> None:
        actual, size = _stream_digest(self.path)
        if actual != self.sha256 or size != self.byte_count:
            raise RuntimeError(f"TOCTOU input drift: {self.path}")

    def json(self, *, maximum_bytes: int = 16 * 1024 * 1024) -> Mapping[str, Any]:
        if self.byte_count > maximum_bytes:
            raise ValueError("refusing to materialize oversized JSON input")
        raw = self.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise RuntimeError(f"TOCTOU input drift: {self.path}")
        return gate._mapping(json.loads(raw), "pipeline JSON input")


def _ijson() -> Any:
    try:
        import ijson
    except ImportError as exc:
        raise RuntimeError("AERP-4 streaming pipeline requires optional dependency ijson>=3.2,<4") from exc
    return ijson


def _external_new(path: Path | str, repo: Path) -> Path:
    target = Path(path).resolve()
    if target.exists():
        raise FileExistsError("refusing to clobber existing output")
    if repo == target or repo in target.parents:
        raise ValueError("output must be outside repository")
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _atomic_bytes(target: Path, raw: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError:
        raise FileExistsError("refusing to clobber existing output") from None
    finally:
        if temporary.exists():
            temporary.unlink()


def _publication(report: Mapping[str, Any], *, inputs: Iterable[StreamInput], repo: Path, implementation: Path) -> dict[str, Any]:
    before = gate._git_state(repo)
    body = dict(report)
    body["publication"] = {
        "input_receipts": [{"path_sha256": gate._sha(str(item.receipt_path or item.path)), "sha256": item.sha256} for item in inputs],
        "analyzer_git_state": before,
        "implementation_sha256": hashlib.sha256(implementation.read_bytes()).hexdigest(),
    }
    return body


def _publish(report: Mapping[str, Any], *, output: Path, inputs: list[StreamInput], repo: Path) -> dict[str, Any]:
    body = _publication(report, inputs=inputs, repo=repo, implementation=Path(__file__))
    for item in inputs:
        item.verify_unchanged()
    if gate._git_state(repo) != body["publication"]["analyzer_git_state"]:
        raise RuntimeError("git state drift before publication")
    _atomic_bytes(output, _canonical(body))
    return body


def _rss_bytes() -> int | None:
    try:
        import psutil
        return int(psutil.Process().memory_info().rss)
    except ImportError:
        if os.name != "nt":
            return None
        try:
            import ctypes
            class Counters(ctypes.Structure):
                _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong), ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            counters = Counters(); counters.cb = ctypes.sizeof(Counters)
            if ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.WorkingSetSize)
        except Exception:
            return None
    return None


class _RssSampler:
    """Small best-effort process RSS sampler with a frozen fail-closed cap."""
    def __init__(self, limit_bytes: int) -> None:
        if not isinstance(limit_bytes, int) or limit_bytes < 1:
            raise ValueError("memory threshold must be a positive integer")
        self.limit_bytes = limit_bytes; self.start = _rss_bytes(); self.peak = self.start; self._stop = threading.Event(); self._started = False; self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="aerp4-rss", daemon=True)
    def _run(self) -> None:
        try:
            while not self._stop.wait(.025):
                value = _rss_bytes()
                if value is not None: self.peak = max(self.peak or value, value)
        except BaseException as exc:
            self._error = exc
            self._stop.set()
    def __enter__(self) -> "_RssSampler":
        if self.start is None:
            raise RuntimeError("RSS observation unavailable")
        if self.start > self.limit_bytes:
            raise RuntimeError("frozen memory threshold exceeded")
        self._thread.start(); self._started = True
        return self
    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set(); self._thread.join(timeout=1)
        if self._thread.is_alive():
            raise RuntimeError("RSS sampler thread did not terminate")
        self._started = False
        if self._error is not None:
            raise RuntimeError("RSS sampler failed") from self._error
        value = _rss_bytes()
        if value is not None: self.peak = max(self.peak or value, value)
    def assert_within_limit(self) -> None:
        if self.peak is None:
            raise RuntimeError("RSS observation unavailable")
        if self.peak > self.limit_bytes:
            raise RuntimeError("frozen memory threshold exceeded")
    def __exit__(self, *_args: Any) -> None:
        self.stop()
        self.assert_within_limit()


def _stats(start: float, rss_start: int | None, output_paths: Iterable[Path], *, sampler: _RssSampler | None = None) -> dict[str, Any]:
    rss_end = _rss_bytes()
    return {
        "elapsed_seconds": time.monotonic() - start,
        "rss_bytes_start": rss_start,
        "rss_bytes_end": rss_end,
        "rss_bytes_peak_observed": sampler.peak if sampler is not None else max(value for value in (rss_start, rss_end) if value is not None) if rss_start is not None or rss_end is not None else None,
        "rss_semantics": "sampled_process_working_set" if sampler is not None else "start_end_best_effort_process_working_set",
        "memory_threshold_bytes": sampler.limit_bytes if sampler is not None else None,
        "output_bytes": sum(path.stat().st_size for path in output_paths if path.exists()),
    }


def _cleanup_temporary_directory(path: Path) -> None:
    """Remove only a pipeline-owned temporary directory after any failed stage."""
    if path.exists():
        shutil.rmtree(path)


def _compact_row(question: Mapping[str, Any], trace: Mapping[str, Any]) -> dict[str, Any]:
    """Per-question adapter: FCD validation is complete before raw IDs vanish."""
    source = paired._sanitized_item(question, trace)
    rank = source["rank_source"]
    orders = rank["view_token_orders"]
    evidence = rank["evidence_token_by_ranking_token"]
    ranks_by_view = {view: {token: index for index, token in enumerate(orders[view], start=1)} for view in _VIEWS}
    candidates = []
    for token in orders["raw_bm25"]:
        candidates.append({
            "ranking_token": token,
            "evidence_token": evidence[token],
            "ranks": {view: ranks_by_view[view][token] for view in _VIEWS},
        })
    return {
        "item_token": source["item_token"], "group_token": source["group_token"], "campaign_token": source["campaign_token"],
        "query_sha256": rank["query_sha256"], "input_sha256": rank["input_sha256"], "encoder_identity": rank["encoder_identity"],
        "view_digests": rank["view_digests"], "candidates": candidates,
    }


def _source_git_receipts(artifact: StreamInput) -> dict[str, Mapping[str, Any]]:
    ijson = _ijson(); receipts: dict[str, Mapping[str, Any]] = {}
    for name in ("git_state_before", "git_state_after"):
        with artifact.path.open("rb") as handle:
            values = list(ijson.items(handle, name, use_float=True))
        if len(values) != 1 or not isinstance(values[0], Mapping): raise ValueError(f"source artifact {name} receipt is missing")
        value = values[0]
        expected = {"git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes"}
        if set(value) != expected or value.get("git_dirty") is not False: raise ValueError(f"source artifact {name} receipt malformed")
        for field in ("worktree_status_sha256", "commit_diff_sha256"): gate._token(value.get(field), f"source artifact {name} {field}")
        for field in ("git_head", "git_tree"):
            if not isinstance(value.get(field), str) or len(value[field]) != 40: raise ValueError(f"source artifact {name} Git object malformed")
        receipts[name] = dict(value)
    return receipts


def sanitize_stream(*, artifact: StreamInput, output: Path, repo: Path, memory_threshold_bytes: int = FROZEN_MAX_RSS_BYTES) -> dict[str, Any]:
    """Stream identity then product traces into two compact JSONL shards."""
    if memory_threshold_bytes != FROZEN_MAX_RSS_BYTES:
        raise ValueError("memory threshold differs from frozen pipeline limit")
    stage_dir = output.resolve()
    if stage_dir.exists():
        raise FileExistsError("refusing to clobber existing shard directory")
    temporary_dir: Path | None = None
    with _RssSampler(memory_threshold_bytes) as sampler:
        try:
            ijson = _ijson(); started = time.monotonic(); rss_start = _rss_bytes(); state = gate._git_state(repo)
            source_git = _source_git_receipts(artifact)
            identities: dict[str, tuple[str, str]] = {}
            with artifact.path.open("rb") as handle:
                for question in ijson.items(handle, "questions.item", use_float=True):
                    if not isinstance(question, Mapping) or not isinstance(question.get("item_id"), str) or not isinstance(question.get("conversation_id"), str):
                        raise ValueError("staged question identity is malformed")
                    if question["item_id"] in identities:
                        raise ValueError("staged question identities repeat")
                    identities[question["item_id"]] = (question["item_id"], question["conversation_id"])
            if len(identities) != 1986:
                raise ValueError("frozen LoCoMo question denominator drift")
            groups = sorted({paired._token("aerp4:group", conversation) for _item, conversation in identities.values()})
            if len(groups) != 10:
                raise ValueError("frozen LoCoMo conversation denominator drift")
            train_groups = set(groups[:5])
            temporary_dir = Path(tempfile.mkdtemp(prefix=f".{stage_dir.name}.", dir=stage_dir.parent))
            paths = {name: temporary_dir / f"{name}.jsonl" for name in ("train", "dev")}
            counts = {name: {"items": 0, "candidates": 0} for name in paths}; crosswalk_rows = {name: [] for name in paths}
            with paths["train"].open("x", encoding="utf-8", newline="\n") as train_handle, paths["dev"].open("x", encoding="utf-8", newline="\n") as dev_handle:
                handles = {"train": train_handle, "dev": dev_handle}
                with artifact.path.open("rb") as handle:
                    for item_id, trace in ijson.kvitems(handle, "product_traces", use_float=True):
                        if item_id not in identities or not isinstance(trace, Mapping):
                            raise ValueError("product trace identity differs from questions")
                        raw_item, conversation = identities.pop(item_id)
                        row = _compact_row({"item_id": raw_item, "conversation_id": conversation}, trace)
                        partition = "train" if row["group_token"] in train_groups else "dev"
                        handles[partition].write(json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n")
                        counts[partition]["items"] += 1; counts[partition]["candidates"] += len(row["candidates"])
                        crosswalk_rows[partition].append({key: row[key] for key in ("item_token", "group_token", "campaign_token", "query_sha256", "input_sha256")})
                for handle in handles.values():
                    handle.flush(); os.fsync(handle.fileno())
            if identities or sum(value["items"] for value in counts.values()) != 1986:
                raise ValueError("product trace denominator differs from questions")
            if gate._git_state(repo) != state:
                raise RuntimeError("git state drift during sanitize")
            shard_rows = []
            for name in ("train", "dev"):
                digest, byte_count = _stream_digest(paths[name]); relative = f"{name}.jsonl"
                shard_rows.append({"partition": name, "path": relative, "path_sha256": gate._sha(relative), "sha256": digest, "bytes": byte_count, "item_count": counts[name]["items"], "candidate_count": counts[name]["candidates"], "crosswalk_sha256": gate._crosswalk(crosswalk_rows[name])})
            manifest_path = temporary_dir / "manifest.json"
            report = {"schema": MANIFEST_SCHEMA, "status": "complete", "artifact_sha256": artifact.sha256, "artifact_bytes": artifact.byte_count, "source_artifact_git_receipts": source_git, "question_random_split": False, "groups": {"train": sorted(train_groups), "dev": sorted(set(groups) - train_groups)}, "shards": shard_rows, "metrics": _stats(started, rss_start, [*paths.values(), manifest_path], sampler=sampler)}
            result = _publish(report, output=manifest_path, inputs=[artifact], repo=repo)
            sampler.stop(); sampler.assert_within_limit()
            os.rename(temporary_dir, stage_dir); temporary_dir = None
            return result
        finally:
            if temporary_dir is not None:
                _cleanup_temporary_directory(temporary_dir)


def _manifest_rows(manifest_input: StreamInput) -> tuple[Mapping[str, Any], dict[str, StreamInput]]:
    manifest = manifest_input.json()
    if set(manifest) != {"schema", "status", "artifact_sha256", "artifact_bytes", "source_artifact_git_receipts", "question_random_split", "groups", "shards", "metrics", "publication"} or manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("status") != "complete" or manifest.get("question_random_split") is not False:
        raise ValueError("sanitized manifest contract mismatch")
    gate._token(manifest.get("artifact_sha256"), "source artifact SHA-256")
    if not isinstance(manifest.get("artifact_bytes"), int) or manifest["artifact_bytes"] < 1: raise ValueError("source artifact byte receipt malformed")
    gate._validate_publication(manifest["publication"])
    receipts = manifest.get("source_artifact_git_receipts")
    if not isinstance(receipts, Mapping) or set(receipts) != {"git_state_before", "git_state_after"}:
        raise ValueError("source artifact provenance receipt mismatch")
    groups = manifest.get("groups")
    if not isinstance(groups, Mapping) or set(groups) != {"train", "dev"} or any(not isinstance(groups[name], list) or len(groups[name]) != 5 for name in ("train", "dev")):
        raise ValueError("sanitized manifest group split malformed")
    train_groups = {gate._token(value, "train group") for value in groups["train"]}; dev_groups = {gate._token(value, "dev group") for value in groups["dev"]}
    if len(train_groups) != 5 or len(dev_groups) != 5 or train_groups & dev_groups: raise ValueError("sanitized manifest group split overlaps")
    rows = manifest.get("shards")
    if not isinstance(rows, list) or {row.get("partition") for row in rows if isinstance(row, Mapping)} != {"train", "dev"}:
        raise ValueError("sanitized manifest shard set mismatch")
    pinned: dict[str, StreamInput] = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"partition", "path", "path_sha256", "sha256", "bytes", "item_count", "candidate_count", "crosswalk_sha256"}: raise ValueError("sanitized manifest shard malformed")
        name = row["partition"]; relative = row.get("path"); path = (manifest_input.path.parent / str(relative)).resolve(); digest = gate._token(row.get("sha256"), "shard SHA-256")
        if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts or gate._sha(relative) != row.get("path_sha256") or not isinstance(row.get("bytes"), int) or row["bytes"] < 0:
            raise ValueError("sanitized manifest shard receipt malformed")
        actual, size = _stream_digest(path)
        if actual != digest or size != row["bytes"]:
            raise ValueError("sanitized shard receipt drift")
        item_count = 0; candidate_count = 0; crosswalk = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                value = json.loads(line)
                if not isinstance(value, Mapping) or not isinstance(value.get("candidates"), list): raise ValueError("sanitized shard JSONL row malformed")
                item_count += 1; candidate_count += len(value["candidates"])
                crosswalk.append({key: value.get(key) for key in ("item_token", "group_token", "campaign_token", "query_sha256", "input_sha256")})
        if item_count != row["item_count"] or candidate_count != row["candidate_count"] or gate._crosswalk(crosswalk) != row["crosswalk_sha256"]:
            raise ValueError("sanitized shard count or crosswalk receipt drift")
        pinned[name] = StreamInput(path, digest, size, path)
    if sum(row["item_count"] for row in rows) != 1986:
        raise ValueError("sanitized manifest item denominator drift")
    return manifest, pinned


def _source_from_shards(shards: Mapping[str, StreamInput]) -> dict[str, Any]:
    partitions: dict[str, Any] = {}
    for name in ("train", "dev"):
        rows = []
        with shards[name].path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = json.loads(line); candidates = raw.pop("candidates", None)
                if not isinstance(candidates, list): raise ValueError("compact shard candidate rows missing")
                rows.append({key: raw[key] for key in ("item_token", "group_token", "campaign_token")} | {"rank_source": {key: raw[key] for key in ("query_sha256", "input_sha256")}})
        partitions[name] = {"items": rows, "crosswalk_sha256": gate._crosswalk([{**{key: row[key] for key in ("item_token", "group_token", "campaign_token")}, **row["rank_source"]} for row in rows])}
    return {"schema": paired.SCHEMA, "status": "complete", "question_random_split": False, "partitions": partitions}


def _stream_labels(labels: StreamInput) -> dict[str, Any]:
    ijson = _ijson(); rows = []
    with labels.path.open("rb") as handle:
        for question in ijson.items(handle, "questions.item", use_float=True):
            if not isinstance(question, Mapping): raise ValueError("staged label question malformed")
            item = question.get("item_id"); conversation = question.get("conversation_id")
            official = question.get("official_exact", question.get("gold", {}).get("official_exact") if isinstance(question.get("gold"), Mapping) else None)
            if not isinstance(item, str) or not isinstance(conversation, str) or not isinstance(official, Mapping):
                raise ValueError("staged label identity or official evidence is malformed")
            # The custody process sees labels, but not arbitrary question text
            # or product traces. Retain only its join keys and official count.
            rows.append({"item_id": item, "conversation_id": conversation, "official_exact": {key: official.get(key) for key in ("resolved_dialog_ids", "resolved_ids", "unresolved_evidence_item_count", "unresolved", "evidence_item_count", "count") if key in official}})
    if len(rows) != 1986:
        raise ValueError("staged label denominator drift")
    return {"questions": rows}


def custody_stream(*, manifest_input: StreamInput, labels_input: StreamInput, guardrail_input: StreamInput, output: Path, repo: Path, output_slots: Mapping[str, Path | str], memory_threshold_bytes: int = FROZEN_MAX_RSS_BYTES) -> dict[str, Any]:
    """Build custody/study from JSONL ranks and a label-only artifact scan."""
    if memory_threshold_bytes != FROZEN_MAX_RSS_BYTES: raise ValueError("memory threshold differs from frozen pipeline limit")
    with _RssSampler(memory_threshold_bytes) as sampler:
     started = time.monotonic(); rss_start = _rss_bytes(); state = gate._git_state(repo)
     manifest, shards = _manifest_rows(manifest_input); source = _source_from_shards(shards); labels = _stream_labels(labels_input)
     guardrail = guardrail_input.json(); custody._validate_guardrail_manifest(guardrail)
     implementation = hashlib.sha256(Path(__file__).read_bytes()).hexdigest(); retrieval_impl = hashlib.sha256((Path(__file__).parents[1] / "mempalace_rpg" / "retrieval.py").read_bytes()).hexdigest()
     label_custody = custody.build_label_custody(source, labels, source_artifact_sha256=labels_input.sha256, producer_sha256=implementation)
     receipt = gate._git_state(repo)
     root = Path(__file__).resolve().parents[1]
     analyzers = {
        "gate": {**receipt, "implementation_sha256": hashlib.sha256((root / "benchmarks" / "aerp4_raw_anchored_gate.py").read_bytes()).hexdigest()},
        "prefreeze": {**receipt, "implementation_sha256": implementation},
        "label": {**receipt, "implementation_sha256": hashlib.sha256((root / "benchmarks" / "aerp4_locomo_custody.py").read_bytes()).hexdigest()},
        "guardrail": {**receipt, "implementation_sha256": hashlib.sha256((root / "benchmarks" / "aerp4_locomo_custody.py").read_bytes()).hexdigest()},
     }
     study = custody.build_study(
        sanitized_source=source, label_custody=label_custody, dataset_sha256=labels_input.sha256,
        producer={"artifact_sha256": manifest_input.sha256, "git_head": receipt["git_head"], "git_tree": receipt["git_tree"], "retrieval_implementation_sha256": retrieval_impl},
        retrieval={"implementation_sha256": retrieval_impl, "raw_config_sha256": prefreeze._sha(prefreeze._config("+inf")), "p5_config_sha256": prefreeze._sha(prefreeze._config("-inf"))},
        output_slots=output_slots, analyzers=analyzers, guardrail_source=guardrail,
        guardrail_source_artifact_sha256=guardrail_input.sha256,
     )
     for pinned in [manifest_input, labels_input, guardrail_input, *shards.values()]: pinned.verify_unchanged()
     if gate._git_state(repo) != state: raise RuntimeError("git state drift during custody")
     if output.exists(): raise FileExistsError("refusing to clobber existing custody directory")
     temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
     try:
        study_output = temporary / "study.json"; bundle_output = temporary / "manifest.json"
        _atomic_bytes(study_output, _canonical(study))
        formal_labels = {name: custody.build_label_artifact(study=study, label_custody=label_custody, partition=name) for name in ("train", "dev")}
        formal_guardrail = custody.build_guardrail_evidence(study=study, guardrail_source=guardrail, guardrail_source_artifact_sha256=guardrail_input.sha256)
        def final_publication(value: Mapping[str, Any], *, analyzer_name: str, source_path: Path, source_sha: str) -> None:
            analyzer = analyzers[analyzer_name]
            value["publication"] = {"input_receipts": [{"path_sha256": gate._sha(str(output / "study.json")), "sha256": gate._sha(study)}, {"path_sha256": gate._sha(str(source_path)), "sha256": source_sha}], "analyzer_git_state": {key: analyzer[key] for key in ("git_head", "git_tree", "git_dirty", "worktree_status_sha256", "commit_diff_sha256", "commit_diff_bytes")}, "implementation_sha256": analyzer["implementation_sha256"]}
        for value in formal_labels.values(): final_publication(value, analyzer_name="label", source_path=labels_input.receipt_path or labels_input.path, source_sha=labels_input.sha256)
        final_publication(formal_guardrail, analyzer_name="guardrail", source_path=guardrail_input.receipt_path or guardrail_input.path, source_sha=guardrail_input.sha256)
        for name, value in formal_labels.items(): _atomic_bytes(temporary / f"labels-{name}.json", _canonical(value))
        _atomic_bytes(temporary / "guardrail-evidence.json", _canonical(formal_guardrail))
        report = {"schema": CUSTODY_SCHEMA, "status": "complete", "sanitized_manifest_sha256": manifest_input.sha256, "label_artifact_sha256": labels_input.sha256, "guardrail_manifest_sha256": guardrail_input.sha256, "study_sha256": gate._sha(study), "study_path": "study.json", "study_path_sha256": gate._sha("study.json"), "formal_artifacts": {"train_labels": "labels-train.json", "dev_labels": "labels-dev.json", "guardrail_evidence": "guardrail-evidence.json"}, "label_custody": label_custody, "metrics": _stats(started, rss_start, [bundle_output, study_output], sampler=sampler)}
        result = _publish(report, output=bundle_output, inputs=[manifest_input, labels_input, guardrail_input], repo=repo)
        sampler.stop(); sampler.assert_within_limit()
        os.rename(temporary, output)
        return result
     except BaseException:
        _cleanup_temporary_directory(temporary)
        raise


def _compact_to_paired_item(row: Mapping[str, Any]) -> dict[str, Any]:
    candidates = row.get("candidates")
    if not isinstance(candidates, list) or not candidates: raise ValueError("compact candidate rows are malformed")
    tokens = []; evidence: dict[str, str] = {}; ranks = {view: {} for view in _VIEWS}
    for candidate in candidates:
        if not isinstance(candidate, Mapping): raise ValueError("compact candidate is malformed")
        token = gate._token(candidate.get("ranking_token"), "ranking token"); evidence_token = gate._token(candidate.get("evidence_token"), "evidence token")
        if evidence_token != hashlib.sha256(token.encode("utf-8")).hexdigest(): raise ValueError("compact evidence token does not replay ranking token")
        evidence[token] = evidence_token
        values = candidate.get("ranks")
        if not isinstance(values, Mapping) or set(values) != set(_VIEWS): raise ValueError("compact ranks are malformed")
        tokens.append(token)
        for view in _VIEWS:
            rank = values[view]
            if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1: raise ValueError("compact rank is malformed")
            ranks[view][token] = rank
    if len(tokens) != len(set(tokens)) or any(set(values.values()) != set(range(1, len(tokens) + 1)) for values in ranks.values()):
        raise ValueError("compact ranks are not view permutations")
    encoder = row.get("encoder_identity"); digests = row.get("view_digests")
    if not isinstance(encoder, str) or not encoder or not isinstance(digests, Mapping) or set(digests) != set(_VIEWS): raise ValueError("compact encoder/view receipt is malformed")
    for digest in digests.values(): gate._token(digest, "view digest")
    return {"item_token": gate._token(row.get("item_token"), "item token"), "group_token": gate._token(row.get("group_token"), "group token"), "campaign_token": gate._token(row.get("campaign_token"), "campaign token"), "rank_source": {"query_sha256": gate._token(row.get("query_sha256"), "query digest"), "input_sha256": gate._token(row.get("input_sha256"), "input digest"), "encoder_identity": encoder, "view_digests": dict(digests), "view_token_orders": {view: [token for token, _rank in sorted(ranks[view].items(), key=lambda pair: pair[1])] for view in _VIEWS}, "evidence_token_by_ranking_token": evidence}}


def paired_stream(*, manifest_input: StreamInput, custody_input: StreamInput, study_input: StreamInput, partition: str, output: Path, repo: Path, memory_threshold_bytes: int = FROZEN_MAX_RSS_BYTES) -> dict[str, Any]:
    """Read one compact shard and produce gate rows without retaining traces."""
    if partition not in {"train", "dev"}: raise ValueError("partition is invalid")
    if memory_threshold_bytes != FROZEN_MAX_RSS_BYTES: raise ValueError("memory threshold differs from frozen pipeline limit")
    with _RssSampler(memory_threshold_bytes) as sampler:
     started = time.monotonic(); rss_start = _rss_bytes(); state = gate._git_state(repo)
     manifest, shards = _manifest_rows(manifest_input); custody_bundle = custody_input.json(); study = study_input.json()
     if custody_bundle.get("schema") != CUSTODY_SCHEMA or custody_bundle.get("study_sha256") != gate._sha(study) or custody_bundle.get("sanitized_manifest_sha256") != manifest_input.sha256:
        raise ValueError("custody/study/manifest binding mismatch")
     gate._validate_study(study)
     gate._slot(study, stage=f"{partition}_prefreeze", partition=partition, output=output / "ranking-freeze.json")
     membership = custody_bundle.get("label_custody", {}).get("exclusion_receipt")
     if not isinstance(membership, Mapping): raise ValueError("custody membership receipt is missing")
     excluded = set(membership.get("excluded_item_tokens", [])); rows = []; candidates = 0; strict_questions = 0
     with shards[partition].path.open("r", encoding="utf-8") as handle:
        for line in handle:
            item = _compact_to_paired_item(json.loads(line)); candidates += len(item["rank_source"]["view_token_orders"]["raw_bm25"])
            if item["item_token"] in excluded: continue
            raw = paired._trace(item, policy=RawAnchoredP5Policy(float("inf")), route="raw")
            p5 = paired._trace(item, policy=RawAnchoredP5Policy(float("-inf")), route="p5")
            parsed_raw = prefreeze._production_trace(raw, route="raw"); parsed_p5 = prefreeze._production_trace(p5, route="p5")
            if parsed_raw["A"] != parsed_p5["A"] or set(parsed_raw["tokens"]) != set(parsed_p5["tokens"]): raise ValueError("paired compact replay mismatch")
            if len(parsed_raw["tokens"]) > 10: strict_questions += 1
            rows.append({"item_token": item["item_token"], "group_token": item["group_token"], "campaign_token": item["campaign_token"], "query_sha256": parsed_raw["query_sha256"], "input_sha256": parsed_raw["input_sha256"], "A_hex": parsed_raw["A"].hex(), "numerator": parsed_raw["numerator"], "denominator": parsed_raw["denominator"], "authorized_tokens": parsed_raw["tokens"], "authorized_tokens_sha256": prefreeze._sha(parsed_raw["tokens"]), "raw_top10": parsed_raw["tokens"][:10], "p5_top10": parsed_p5["tokens"][:10], "raw_top10_sha256": parsed_raw["raw_top_digest"], "p5_top10_sha256": parsed_p5["p5_top_digest"]})
     expected = gate._partition_spec(study, partition)
     for key, digest in (("item_token", "item_sha256"), ("group_token", "group_sha256"), ("campaign_token", "campaign_sha256")):
        if gate._sha(sorted(row[key] for row in rows)) != expected[digest]: raise ValueError("paired membership digest mismatch")
     if gate._crosswalk(rows) != expected["crosswalk_sha256"]: raise ValueError("paired crosswalk mismatch")
     if output.exists(): raise FileExistsError("refusing to clobber existing paired directory")
     temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
     try:
        paired_input = {"schema": "aerp4-locomo-paired-input-v1", "manifest_sha256": manifest_input.sha256, "custody_sha256": custody_input.sha256, "partition": partition, "shard_sha256": shards[partition].sha256, "membership_sha256": gate._sha(membership), "metrics": {**_stats(started, rss_start, [], sampler=sampler), "candidate_count": candidates, "strict_top10_question_count": strict_questions}}
        paired_path = temporary / "paired-input.json"; freeze_path = temporary / "ranking-freeze.json"; _atomic_bytes(paired_path, _canonical(paired_input))
        report = {"schema": gate.RANKING_FREEZE_SCHEMA, "status": "complete", "study_sha256": gate._sha(study), "partition": partition, "producer": dict(study["producer"]), "paired_input_sha256": _sha(paired_input), "items": rows, "phase_ledger": {"status": "complete", "phase": "atomic_publish_ready", "terminal_phase": "atomic_publish_ready"}}
        # The formal freeze binds the separately addressable frozen study and
        # paired-input manifest, never a manufactured receipt.
        paired_pin = StreamInput(paired_path, _sha(paired_input), paired_path.stat().st_size, output / "paired-input.json")
        result = _publish(report, output=freeze_path, inputs=[study_input, paired_pin], repo=repo)
        gate._parse_ranking_freeze(result, study, partition)
        for pin in [manifest_input, custody_input, study_input, *shards.values()]: pin.verify_unchanged()
        if gate._git_state(repo) != state: raise RuntimeError("git state drift during paired")
        sampler.stop(); sampler.assert_within_limit()
        os.rename(temporary, output); return result
     except BaseException:
        _cleanup_temporary_directory(temporary)
        raise


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); commands = parser.add_subparsers(dest="stage", required=True)
    sanitize = commands.add_parser("sanitize"); sanitize.add_argument("--artifact", required=True); sanitize.add_argument("--artifact-sha256", required=True); sanitize.add_argument("--output", required=True); sanitize.add_argument("--repo", required=True)
    custody_parser = commands.add_parser("custody")
    for name in ("manifest", "labels", "guardrail-manifest"):
        custody_parser.add_argument("--" + name, required=True); custody_parser.add_argument("--" + name + "-sha256", required=True)
    custody_parser.add_argument("--output", required=True); custody_parser.add_argument("--repo", required=True)
    for stage in ("train-prefreeze", "dev-prefreeze", "tau-select", "dev-eval"):
        custody_parser.add_argument("--" + stage + "-output", required=True)
    paired_parser = commands.add_parser("paired")
    for name in ("manifest", "custody", "study"):
        paired_parser.add_argument("--" + name, required=True); paired_parser.add_argument("--" + name + "-sha256", required=True)
    paired_parser.add_argument("--partition", choices=("train", "dev"), required=True); paired_parser.add_argument("--output", required=True); paired_parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv); repo = Path(args.repo).resolve(); gate._git_state(repo)
    if args.stage == "sanitize":
        output = _external_new(args.output, repo); artifact = StreamInput.pin(args.artifact, args.artifact_sha256)
        sanitize_stream(artifact=artifact, output=output, repo=repo)
    elif args.stage == "custody":
        output = _external_new(args.output, repo)
        manifest = StreamInput.pin(args.manifest, args.manifest_sha256); labels = StreamInput.pin(args.labels, args.labels_sha256); guardrail = StreamInput.pin(args.guardrail_manifest, args.guardrail_manifest_sha256)
        slots = {"train_prefreeze": args.train_prefreeze_output, "dev_prefreeze": args.dev_prefreeze_output, "tau_select": args.tau_select_output, "dev_eval": args.dev_eval_output}
        custody_stream(manifest_input=manifest, labels_input=labels, guardrail_input=guardrail, output=output, repo=repo, output_slots=slots)
    elif args.stage == "paired":
        output = _external_new(args.output, repo)
        manifest = StreamInput.pin(args.manifest, args.manifest_sha256); bundle = StreamInput.pin(args.custody, args.custody_sha256); study = StreamInput.pin(args.study, args.study_sha256)
        paired_stream(manifest_input=manifest, custody_input=bundle, study_input=study, partition=args.partition, output=output, repo=repo)
    return 0


def main(argv: list[str] | None = None) -> int:
    return _main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
