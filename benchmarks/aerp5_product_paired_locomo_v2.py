"""AERP-5 v2: formal public-nonblind paired product runner for LoCoMo.

The implementation deliberately separates a label-free ranking worker from the
coordinator that is later allowed to score frozen output.  It is a public-data
engineering confirmation, therefore ``confirmation_claim`` is permanently
false; it is nevertheless fail-closed enough to be a reproducible product
comparison.  Resource-gate eligibility remains a separate, later system audit.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from benchmarks import aerp1_locomo_three_way as aerp1
from benchmarks import aerp2_product_six_view_locomo as aerp2
from benchmarks import aerp4_locomo_paired_receipts as a4paired
from benchmarks import aerp5_product_paired_locomo as v1
from mempalace_rpg import FixedP5Policy, FixedSixViewPolicy, RpgMemoryKernel, SixViewRanker
from mempalace_rpg.retrieval import SIX_VIEW_WEIGHTS


ROOT = Path(__file__).resolve().parents[1]
TOP_K = 10
REPEATS = 2
RSS_CAP_BYTES = 2_147_483_648
SCHEMA = "aerp5-product-paired-locomo-v2"
EXPECTED_DEFAULT_MANIFEST_SHA256 = "daebd38962d62074ec70312572fdc49bfbbe943f024610037a012180d6398d1e"
EXPECTED_SCIENTIFIC_GATES = {
    "projected_member_count": 1982,
    "strict_top_k": 10,
    "p5_matches_aerp4_freeze": True,
    "rerun_projection_digest_equal": True,
    "score_only_after_freeze": True,
    "p5_vs_original_question_ci_lower_strictly_gt": 0.0,
    "p5_vs_original_conversation_ci_lower_strictly_gt": 0.0,
    "hard_cat1_2_and_cat5_report_required": True,
    "public_nonblind": True,
    "confirmation_claim": False,
    "resource_gate_after_engineering_only": True,
}
ARM_ORIGINAL = "original_public_product_minilm"
ARM_P5 = "current_fixed_p5_product"
ARM_SIX_VIEW = "current_fixed_six_view_product_secondary"
ALL_ARMS = (ARM_ORIGINAL, ARM_P5, ARM_SIX_VIEW)
# This asserts implementation availability, never experimental completion.
END_TO_END_PRODUCT_WORKER_IMPLEMENTED = True


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _publish_nonreplace(path: Path, payload: bytes) -> None:
    """Publish without the replace race: hard-link creation is exclusive."""
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with open(temporary, "xb") as handle: handle.write(payload); handle.flush(); os.fsync(handle.fileno())
        os.link(temporary, path)  # fails if another coordinator published first
    except FileExistsError:
        raise FileExistsError("refusing to clobber existing formal output")
    finally:
        temporary.unlink(missing_ok=True)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


@dataclass(frozen=True)
class PinnedJson:
    path: Path
    sha256: str

    @classmethod
    def pin(cls, path: Path | str, expected: str) -> "PinnedJson":
        target = Path(path).resolve()
        if not target.is_file() or sha256_file(target) != expected:
            raise ValueError("pinned JSON digest mismatch")
        return cls(target, expected)

    def load(self) -> dict[str, Any]:
        if sha256_file(self.path) != self.sha256:
            raise RuntimeError("pinned JSON drifted during run")
        return _json(self.path)


def load_manifest(path: Path | None = None) -> dict[str, Any]:
    manifest_path = path or Path(__file__).with_name("aerp5_product_paired_locomo_v2_manifest.json")
    if sha256_file(manifest_path) != EXPECTED_DEFAULT_MANIFEST_SHA256:
        raise ValueError("AERP5 v2 canonical manifest bytes drifted")
    manifest = _json(manifest_path)
    required = {"schema", "dataset", "original", "aerp4", "run", "arms", "scientific_gates"}
    if set(manifest) != required or manifest["schema"] != SCHEMA:
        raise ValueError("AERP5 v2 manifest schema is malformed")
    if manifest["run"].get("repeats") != REPEATS or manifest["run"].get("top_k") != TOP_K:
        raise ValueError("AERP5 v2 repeat/TopK contract drifted")
    if manifest["run"].get("rss_cap_bytes") != RSS_CAP_BYTES:
        raise ValueError("AERP5 v2 RSS contract drifted")
    if set(manifest["arms"]) != set(ALL_ARMS) or manifest["arms"].get(ARM_P5, {}).get("primary") is not True:
        raise ValueError("AERP5 v2 arms contract is malformed")
    if manifest.get("scientific_gates") != EXPECTED_SCIENTIFIC_GATES:
        raise ValueError("AERP5 v2 scientific gates differ from the canonical zero-threshold contract")
    if "tau" in _canonical(manifest).decode("ascii") or "router" in _canonical(manifest).decode("ascii"):
        raise ValueError("AERP5 v2 must not admit tunable tau/router inputs")
    return manifest


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{label} must be a SHA-256 token")
    return value


def _freeze_items(value: Mapping[str, Any], partition: str) -> list[dict[str, Any]]:
    if value.get("partition") != partition or value.get("status") != "complete":
        raise ValueError(f"AERP4 {partition} ranking freeze is malformed")
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("AERP4 ranking freeze items are missing")
    result: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            raise ValueError("AERP4 ranking freeze item is malformed")
        _token(row.get("item_token"), "item token")
        for field in ("raw_top10", "p5_top10"):
            ranked = row.get(field)
            if not isinstance(ranked, list) or len(ranked) != TOP_K or len(set(ranked)) != TOP_K:
                raise ValueError(f"AERP4 {field} must be strict Top-10")
        result.append(row)
    return result


def _pinned_value(value: PinnedJson | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    if isinstance(value, PinnedJson):
        return value.load(), value.sha256
    if not isinstance(value, Mapping):
        raise TypeError("AERP4 input must be a pinned JSON receipt or object")
    copied = dict(value)
    return copied, canonical_sha256(copied)


def validate_aerp4_membership(
    *, study: PinnedJson | Mapping[str, Any], custody: PinnedJson | Mapping[str, Any], train_freeze: PinnedJson | Mapping[str, Any], dev_freeze: PinnedJson | Mapping[str, Any], manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate exact AERP4 1,982 membership without loading labels."""
    pins = manifest["aerp4"]
    study, study_sha = _pinned_value(study); custody, custody_sha = _pinned_value(custody)
    train_freeze, train_sha = _pinned_value(train_freeze); dev_freeze, dev_sha = _pinned_value(dev_freeze)
    if study_sha != pins["study_sha256"]:
        raise ValueError("AERP4 study digest mismatch")
    if custody_sha != pins["custody_bundle_sha256"]:
        raise ValueError("AERP4 custody digest mismatch")
    if train_sha != pins["train_ranking_freeze_sha256"]:
        raise ValueError("AERP4 train ranking freeze digest mismatch")
    if dev_sha != pins["dev_ranking_freeze_sha256"]:
        raise ValueError("AERP4 dev ranking freeze digest mismatch")
    if custody.get("study_sha256") != pins["study_sha256"]:
        raise ValueError("AERP4 custody does not bind study")
    exclusion = custody.get("label_custody", {}).get("exclusion_receipt", {})
    excluded = exclusion.get("excluded_item_tokens")
    if not isinstance(excluded, list) or sorted(excluded) != sorted(pins["excluded_item_tokens"]):
        raise ValueError("AERP4 four-exclusion receipt mismatch")
    if len(excluded) != 4 or len(set(excluded)) != 4:
        raise ValueError("AERP4 exclusion count must equal four")
    train = _freeze_items(train_freeze, "train")
    dev = _freeze_items(dev_freeze, "dev")
    tokens = [row["item_token"] for row in train + dev]
    if len(tokens) != 1982 or len(set(tokens)) != 1982 or set(tokens) & set(excluded):
        raise ValueError("AERP4 frozen membership is not exactly 1,982 allowed items")
    return {"item_tokens": tuple(sorted(tokens)), "excluded_item_tokens": tuple(sorted(excluded)), "count": len(tokens)}


def project_public_items(
    *, public_items: Iterable[Mapping[str, Any]], allowed_item_tokens: Iterable[str]
) -> list[dict[str, Any]]:
    """Project official public inputs by opaque AERP4 item token only.

    Callers must supply no scorer object or label payload.  The projection is
    intentionally strict so a public replay cannot silently expand to 1,986.
    """
    allowed = set(allowed_item_tokens)
    projected: list[dict[str, Any]] = []
    for source in public_items:
        token = _token(source.get("item_token"), "public item token")
        if token not in allowed:
            continue
        fields = {key: source.get(key) for key in ("item_token", "item_id", "conversation_id", "conversation_token", "query", "sessions")}
        if not isinstance(fields["query"], str) or not isinstance(fields["sessions"], list) or not isinstance(fields["item_id"], str) or not isinstance(fields["conversation_id"], str):
            raise ValueError("public item lacks query/session projection")
        projected.append(fields)
    if len(projected) != len(allowed) or {row["item_token"] for row in projected} != allowed:
        raise ValueError("official bundle projection does not exactly match AERP4 membership")
    return sorted(projected, key=lambda row: row["item_token"])


def build_public_projection(*, dataset: Path, original_root: Path, allowed_item_tokens: Iterable[str]) -> list[dict[str, Any]]:
    """Build the sole worker input from official query/session bytes, no scorer."""
    _palace, _searcher, protocol, _state = v1.load_original_product(original_root)
    loaded = protocol.load_official_locomo10(dataset)
    allowed = set(allowed_item_tokens); result = []; ordinal = 0
    for conversation_index, sample in enumerate(loaded.records):
        conversation_id = f"conversation_{conversation_index:06d}"
        sessions, _mapping = protocol._sanitize_conversation(sample)
        for qa in sample["qa"]:
            item_id = f"item_{ordinal:06d}"; ordinal += 1
            token = a4paired._token("aerp4:item", item_id)
            if token in allowed:
                result.append({"item_token": token, "item_id": item_id, "conversation_id": conversation_id, "conversation_token": a4paired._token("aerp4:group", conversation_id), "query": qa["question"], "sessions": sessions})
    if ordinal != 1986:
        raise ValueError("official LoCoMo source denominator drifted")
    return project_public_items(public_items=result, allowed_item_tokens=allowed)


def _evidence_tokens(dialog_ids: Sequence[str]) -> list[str]:
    return [a4paired.evidence_token_from_ranking_key_sha256(hashlib.sha256(dialog.encode("utf-8")).hexdigest()) for dialog in dialog_ids]


def actual_onnx_session_providers(native_embedding: Any) -> list[str]:
    """Read the provider from the live ONNX session after an encode occurred."""
    seen: set[int] = set(); queue = [native_embedding]
    while queue:
        value = queue.pop(0)
        if id(value) in seen: continue
        seen.add(id(value))
        getter = getattr(value, "get_providers", None)
        if callable(getter):
            providers = getter()
            if not isinstance(providers, list) or providers != ["CPUExecutionProvider"]: raise RuntimeError(f"live ONNX providers escaped CPU freeze: {providers!r}")
            return providers
        for name in ("_session", "session", "_model", "model", "_ort_session"):
            child = getattr(value, name, None)
            if child is not None: queue.append(child)
    raise RuntimeError("native Chroma embedding did not expose a live ONNX session")


def policy_receipt_from_ranking_trace(
    *, arm: str, ranking: Mapping[str, Any], selected_ranking_keys: Sequence[str]
) -> tuple[dict[str, Any], str | None]:
    """Extract a frozen policy receipt from the trace fields the ranker really emits."""
    selected_digest = canonical_sha256(list(selected_ranking_keys))
    if arm == ARM_P5:
        fixed = ranking.get("aerp5_fixed_p5")
        expected = _expected_policy_receipt(arm)
        if (
            not isinstance(fixed, Mapping)
            or fixed.get("schema") != FixedP5Policy.schema
            or fixed.get("policy") != FixedP5Policy.policy
            or fixed.get("config") != FixedP5Policy._config(60)
            or fixed.get("config_sha256") != expected["config_sha256"]
            or fixed.get("effective_weights") != expected["effective_weights"]
            or ranking.get("weights") != expected["effective_weights"]
        ):
            raise RuntimeError("fixed P5 policy receipt missing or inconsistent")
        final_digest = _token(fixed.get("final_ranking_sha256"), "fixed P5 final ranking digest")
        return {
            **expected,
            "final_ranking_sha256": final_digest,
            "selected_top10_ranking_sha256": selected_digest,
        }, final_digest
    if arm == ARM_SIX_VIEW:
        expected = _expected_policy_receipt(arm)
        if ranking.get("weights") != expected["effective_weights"] or "aerp5_fixed_p5" in ranking:
            raise RuntimeError("fixed six-view policy receipt missing or inconsistent")
        return {**expected, "selected_top10_ranking_sha256": selected_digest}, None
    raise ValueError("policy receipts exist only for current-product arms")


def stable_product_trace_receipt(
    trace: Mapping[str, Any], *, event_to_dialog: Mapping[str, str]
) -> dict[str, Any]:
    """Project a complete product trace onto backend-independent ranking identities."""
    audit = aerp1.audit_product_trace(dict(trace))
    if audit.get("complete") is not True:
        raise RuntimeError("current product authorization trace is incomplete")
    ranking = trace.get("retrieval_ranking")
    if not isinstance(ranking, Mapping):
        raise RuntimeError("current product retrieval ranking trace is missing")
    authorized_ids = [str(value) for value in trace["authorized_candidate_ids"]]
    selected_ids = [str(value) for value in trace["selected_evidence_ids"]]
    try:
        authorized_dialogs = [event_to_dialog[value] for value in authorized_ids]
        selected_dialogs = [event_to_dialog[value] for value in selected_ids]
    except KeyError as exc:
        raise RuntimeError("authorization trace contains an unmapped backend event") from exc
    selected = ranking.get("selected")
    if not isinstance(selected, list):
        raise RuntimeError("current product selected ranking trace is missing")
    stable_selected = []
    for entry in selected:
        if not isinstance(entry, Mapping):
            raise RuntimeError("current product selected ranking trace is malformed")
        stable_selected.append({
            key: entry.get(key)
            for key in ("ranking_key_sha256", "final_rrf", "component_ranks", "contributions")
        })
    retrieval = {
        key: ranking.get(key)
        for key in ("schema", "encoder_identity", "weights", "rrf_k", "query_sha256", "input_sha256", "view_digests")
    }
    retrieval["selected"] = stable_selected
    for name in ("aerp5_fixed_p5",):
        if name in ranking:
            retrieval[name] = ranking[name]
    return {
        "schema": "aerp5-stable-product-trace-v1",
        "authorization_audit": audit,
        "authorized_dialog_universe_sha256": canonical_sha256(sorted(authorized_dialogs)),
        "selected_dialog_order_sha256": canonical_sha256(selected_dialogs),
        "retrieval_ranking": retrieval,
    }


def worker_run(config: Mapping[str, Any]) -> dict[str, Any]:
    """Real, label-free product worker for one arm/repeat with a fresh backend."""
    arm = config.get("arm"); projection_path = Path(str(config.get("projection"))); output = Path(str(config.get("output"))); backend = Path(str(config.get("temporary_backend")))
    if arm not in ALL_ARMS or output.exists() or backend.exists(): raise ValueError("invalid worker arm/output/fresh backend")
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    if not isinstance(projection, list) or len(projection) != 1982: raise ValueError("worker projection must contain exact 1,982 public items")
    if any("label" in key.casefold() or "scorer" in key.casefold() for row in projection for key in row): raise ValueError("worker projection contains forbidden label/scorer field")
    backend.mkdir(parents=True); model = Path(str(config["model_dir"])); original_root = Path(str(config["original_root"]));
    started = time.monotonic(); by_conversation: dict[str, list[dict[str, Any]]] = {}
    for row in projection: by_conversation.setdefault(str(row["conversation_id"]), []).append(dict(row))
    rankings: dict[str, dict[str, list[str]]] = {}; traces: dict[str, dict[str, Any]] = {}; policies: dict[str, dict[str, Any]] = {}; query_ns: list[int] = []; ingest_ns: list[int] = []
    with RssMonitor(os.getpid()) as monitor, v1.pinned_original_environment():
        palace, searcher, _protocol, _state = v1.load_original_product(original_root); encoder = v1.native_minilm_adapter(model)
        original_palace = backend / "original-palace"; db_path = backend / "current.sqlite3"
        original_configuration = v1.assert_original_product_configuration(palace, palace_path=original_palace, encoder=encoder)
        ranker = None if arm == ARM_ORIGINAL else SixViewRanker(encoder, diagnostic_ledger=True, routing_policy=FixedP5Policy() if arm == ARM_P5 else FixedSixViewPolicy())
        with RpgMemoryKernel(db_path=str(db_path), retrieval_ranker=ranker) if ranker else _NullContext() as kernel:
            for conversation_id, rows in sorted(by_conversation.items()):
                payload = {"sessions": rows[0]["sessions"]}; dialogs = v1.raw_dialogs(payload)
                start = time.perf_counter_ns()
                if arm == ARM_ORIGINAL: v1.original_product_ingest(palace=palace, palace_path=original_palace, conversation_id=conversation_id, dialogs=dialogs)
                else: event_map, _ = aerp2.seed_sanitized_conversation(kernel, payload, conversation_id=conversation_id)
                ingest_ns.append(time.perf_counter_ns() - start)
                for row in rows:
                    start = time.perf_counter_ns()
                    if arm == ARM_ORIGINAL:
                        dialog_top10 = v1.original_product_query(searcher=searcher, palace_path=original_palace, conversation_id=conversation_id, corpus_ids=[d["id"] for d in dialogs], query=row["query"], item_id=row["item_id"])
                    else:
                        dialog_top10, _trace = v1.current_product_rank(kernel, conversation_id=conversation_id, query=row["query"], event_to_dialog=event_map, item_id=row["item_id"])
                        if not v1.TRACE_REQUIRED <= set(_trace): raise RuntimeError("current product compact trace incomplete")
                        ranking = _trace.get("retrieval_ranking", {})
                        selected = ranking.get("selected")
                        if not isinstance(selected, list) or len(selected) != TOP_K: raise RuntimeError("current ranking selected trace is not strict Top-10")
                        selected_evidence = [a4paired.evidence_token_from_ranking_key_sha256(str(entry.get("ranking_key_sha256"))) for entry in selected if isinstance(entry, Mapping)]
                        if len(selected_evidence) != TOP_K: raise RuntimeError("current ranking selected trace is malformed")
                        selected_ranking_keys = [str(entry["ranking_key_sha256"]) for entry in selected]
                        stable_trace = stable_product_trace_receipt(_trace, event_to_dialog=event_map)
                        trace_item = {
                            "trace_sha256": canonical_sha256(stable_trace),
                            "stable_trace": stable_trace,
                            "selected_count": len(_trace["selected_evidence_ids"]),
                            "selected_evidence_top10": selected_evidence,
                            "selected_ranking_key_sha256": selected_ranking_keys,
                            "selected_ranking_keys_sha256": canonical_sha256(selected_ranking_keys),
                            "complete": True,
                        }
                        policy_receipt, final_digest = policy_receipt_from_ranking_trace(
                            arm=arm,
                            ranking=ranking,
                            selected_ranking_keys=selected_ranking_keys,
                        )
                        if arm == ARM_P5:
                            if final_digest is None:
                                raise RuntimeError("fixed P5 final ranking receipt disappeared")
                            trace_item["policy_final_ranking_sha256"] = final_digest
                        policies[row["item_token"]] = policy_receipt
                        traces[row["item_token"]] = trace_item
                    evidence_top10 = _evidence_tokens(dialog_top10)
                    if arm != ARM_ORIGINAL and evidence_top10 != selected_evidence: raise RuntimeError("current output evidence tokens do not bind selected ranking trace")
                    query_ns.append(time.perf_counter_ns() - start); rankings[row["item_token"]] = {"dialog_top10": dialog_top10, "evidence_top10": evidence_top10}
        provider = actual_onnx_session_providers(encoder._function)
        cleanup = v1.reset_original_product_backends(original_palace)
    if len(rankings) != 1982: raise RuntimeError("worker did not rank every projected item")
    trace_receipt = {"supported": arm != ARM_ORIGINAL, "complete_count": len(traces) if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "expected_count": 1982 if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "trace_sha256": canonical_sha256(traces) if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "items": traces if arm != ARM_ORIGINAL else {}}
    if arm != ARM_ORIGINAL and len(traces) != 1982: raise RuntimeError("current trace completeness is not 100%")
    with RssMonitor(os.getpid()) as publish_monitor:
        if arm != ARM_ORIGINAL and len(policies) != 1982: raise RuntimeError("policy receipt completeness is not 100%")
        report = {"schema": SCHEMA + "-worker", "arm": arm, "items": rankings, "input_projection_sha256": sha256_file(projection_path), "input_projection_content_sha256": canonical_sha256(projection), "dialog_ranking_sha256": canonical_sha256({key: row["dialog_top10"] for key, row in rankings.items()}), "evidence_ranking_sha256": canonical_sha256({key: row["evidence_top10"] for key, row in rankings.items()}), "projection_sha256": canonical_sha256(rankings), "latency": latency_receipt(query_ns), "conversation_ingest": distribution_receipt(ingest_ns, expected_count=10, label="conversation ingest"), "trace_receipt": trace_receipt, "policy_receipts": policies if arm != ARM_ORIGINAL else {}, "policy_receipts_sha256": canonical_sha256(policies) if arm != ARM_ORIGINAL else "unsupported_through_original_public_interface", "original_product_configuration": original_configuration, "worker_self_rss_diagnostic": {"authoritative": False, "reason": "coordinator supervisor owns spawn-through-exit peak", "load_through_ranking_peak_bytes": monitor.peak_bytes, "receipt_serialize_publish_peak_bytes": publish_monitor.peak_bytes}, "onnx_providers": provider, "model_file_tree_sha256": v1.file_tree_receipt(model)["sha256"], "cleanup": cleanup, "elapsed_seconds": time.monotonic() - started}
        _publish_nonreplace(output, _canonical(report))
    return report


class _NullContext:
    def __enter__(self): return None
    def __exit__(self, *_: Any) -> None: return None


def validate_p5_against_aerp4(
    produced: Mapping[str, Sequence[str]], frozen_rows: Iterable[Mapping[str, Any]]
) -> None:
    expected = {str(row["item_token"]): list(row["p5_top10"]) for row in frozen_rows}
    if set(produced) != set(expected):
        raise RuntimeError("current P5 production output membership differs from AERP4 freeze")
    for token, ranking in produced.items():
        if list(ranking) != expected[token]:
            raise RuntimeError(f"current P5 Top-10 differs from frozen AERP4 P5 row: {token}")


def _expected_policy_receipt(arm: str) -> dict[str, Any]:
    if arm == ARM_P5:
        config = FixedP5Policy._config(60)
        return {
            "route": "p5",
            "config_sha256": canonical_sha256(config),
            "effective_weights": config["p5_weights"],
        }
    if arm == ARM_SIX_VIEW:
        effective_weights = dict(SIX_VIEW_WEIGHTS)
        return {
            "route": "six_view",
            "config_sha256": canonical_sha256(
                {"route": "six_view", "effective_weights": effective_weights}
            ),
            "effective_weights": effective_weights,
        }
    raise ValueError("the original arm has no current-product policy receipt")


def parse_worker_freeze(
    value: Mapping[str, Any],
    *,
    arm: str,
    projection_sha256: str,
    projection_content_sha256: str,
    item_tokens: Iterable[str],
    model_sha256: str,
) -> dict[str, Any]:
    """Strictly validate one completed worker receipt before custody sees it."""
    if (
        value.get("schema") != SCHEMA + "-worker"
        or value.get("arm") != arm
        or value.get("input_projection_sha256") != projection_sha256
        or value.get("input_projection_content_sha256") != projection_content_sha256
    ):
        raise ValueError("worker freeze identity/projection mismatch")
    if value.get("model_file_tree_sha256") != model_sha256 or value.get("onnx_providers") != ["CPUExecutionProvider"]:
        raise ValueError("worker freeze model/provider mismatch")
    items = value.get("items")
    expected_tokens = set(item_tokens)
    if not isinstance(items, Mapping) or len(items) != 1982 or set(items) != expected_tokens:
        raise ValueError("worker freeze membership is not 1,982")
    dialogs = {}; evidence = {}
    for token, row in items.items():
        _token(token, "worker item token")
        if not isinstance(row, Mapping): raise ValueError("worker item receipt malformed")
        for name, target in (("dialog_top10", dialogs), ("evidence_top10", evidence)):
            ranking = row.get(name)
            if not isinstance(ranking, list) or len(ranking) != TOP_K or len(set(ranking)) != TOP_K: raise ValueError("worker ranking is not strict Top-10")
            target[token] = ranking
        if row["evidence_top10"] != _evidence_tokens(row["dialog_top10"]):
            raise ValueError("worker dialog/evidence Top-10 linkage mismatch")
    if value.get("dialog_ranking_sha256") != canonical_sha256(dialogs) or value.get("evidence_ranking_sha256") != canonical_sha256(evidence) or value.get("projection_sha256") != canonical_sha256(items):
        raise ValueError("worker ranking digest mismatch")
    latency = value.get("latency")
    if not isinstance(latency, Mapping) or not isinstance(latency.get("samples_ns"), list) or dict(latency) != latency_receipt(latency["samples_ns"]):
        raise ValueError("worker latency receipt is not replayable")
    ingest = value.get("conversation_ingest")
    if not isinstance(ingest, Mapping) or not isinstance(ingest.get("samples_ns"), list) or dict(ingest) != distribution_receipt(ingest["samples_ns"], expected_count=10, label="conversation ingest"):
        raise ValueError("worker conversation-ingest receipt is not replayable")
    configuration = value.get("original_product_configuration")
    if not isinstance(configuration, Mapping) or (
        configuration.get("backend") != "chroma"
        or configuration.get("collection") != v1.ORIGINAL_COLLECTION
        or configuration.get("embedding_model") != "minilm"
        or configuration.get("embedding_device") != "cpu"
        or configuration.get("providers") != ["CPUExecutionProvider"]
        or configuration.get("model_file_tree_sha256") != model_sha256
        or configuration.get("same_cached_embedding_object_for_both_arms") is not True
    ):
        raise ValueError("original product resolved configuration mismatch")
    trace = value.get("trace_receipt", {})
    if arm == ARM_ORIGINAL:
        unsupported = "unsupported_through_original_public_interface"
        if trace != {
            "supported": False,
            "complete_count": unsupported,
            "expected_count": unsupported,
            "trace_sha256": unsupported,
            "items": {},
        }:
            raise ValueError("original worker trace support receipt is malformed")
        if value.get("policy_receipts") != {} or value.get("policy_receipts_sha256") != unsupported:
            raise ValueError("original worker must not claim a current-product policy receipt")
    else:
        trace_items = trace.get("items")
        if (
            trace.get("supported") is not True
            or trace.get("complete_count") != 1982
            or trace.get("expected_count") != 1982
            or not isinstance(trace_items, Mapping)
            or set(trace_items) != expected_tokens
            or trace.get("trace_sha256") != canonical_sha256(trace_items)
        ):
            raise ValueError("current worker trace receipt incomplete")
        for token, receipt in trace_items.items():
            if not isinstance(receipt, Mapping) or receipt.get("complete") is not True:
                raise ValueError("current worker trace item is malformed")
            expected_trace_fields = {
                "trace_sha256", "stable_trace", "selected_count", "selected_evidence_top10",
                "selected_ranking_key_sha256", "selected_ranking_keys_sha256", "complete",
            } | ({"policy_final_ranking_sha256"} if arm == ARM_P5 else set())
            if set(receipt) != expected_trace_fields:
                raise ValueError("current worker trace item fields are not canonical")
            _token(receipt.get("trace_sha256"), "current trace digest")
            stable_trace = receipt.get("stable_trace")
            if (
                not isinstance(stable_trace, Mapping)
                or stable_trace.get("schema") != "aerp5-stable-product-trace-v1"
                or stable_trace.get("authorization_audit", {}).get("complete") is not True
                or receipt["trace_sha256"] != canonical_sha256(stable_trace)
                or stable_trace.get("selected_dialog_order_sha256") != canonical_sha256(dialogs[token])
            ):
                raise ValueError("current worker stable trace receipt is malformed")
            _token(stable_trace.get("authorized_dialog_universe_sha256"), "authorized dialog universe digest")
            selected_evidence = receipt.get("selected_evidence_top10")
            selected_keys = receipt.get("selected_ranking_key_sha256")
            if (
                selected_evidence != evidence[token]
                or receipt.get("selected_count") != TOP_K
                or not isinstance(selected_keys, list)
                or len(selected_keys) != TOP_K
                or len(set(selected_keys)) != TOP_K
                or any(_token(key, "selected ranking key digest") != key for key in selected_keys)
                or [a4paired.evidence_token_from_ranking_key_sha256(key) for key in selected_keys] != selected_evidence
                or receipt.get("selected_ranking_keys_sha256") != canonical_sha256(selected_keys)
                or [entry.get("ranking_key_sha256") for entry in stable_trace.get("retrieval_ranking", {}).get("selected", [])] != selected_keys
            ):
                raise ValueError("current worker trace selection does not bind the frozen Top-10")
    if arm != ARM_ORIGINAL:
        policies = value.get("policy_receipts")
        if not isinstance(policies, Mapping) or set(policies) != set(items) or value.get("policy_receipts_sha256") != canonical_sha256(policies): raise ValueError("worker policy receipt digest/membership mismatch")
        expected = _expected_policy_receipt(arm)
        for token, receipt in policies.items():
            if not isinstance(receipt, Mapping):
                raise ValueError("worker fixed policy receipt differs from frozen configuration")
            static = {key: receipt.get(key) for key in expected}
            if static != expected or receipt.get("selected_top10_ranking_sha256") != trace_items[token].get("selected_ranking_keys_sha256"):
                raise ValueError("worker fixed policy receipt differs from frozen configuration")
            if arm == ARM_P5:
                if set(receipt) != {*expected, "selected_top10_ranking_sha256", "final_ranking_sha256"}:
                    raise ValueError("fixed P5 policy receipt has unexpected fields")
                if _token(receipt.get("final_ranking_sha256"), "fixed P5 final ranking digest") != trace_items[token].get("policy_final_ranking_sha256"):
                    raise ValueError("fixed P5 final ranking receipt is not trace-bound")
            elif set(receipt) != {*expected, "selected_top10_ranking_sha256"}:
                raise ValueError("fixed six-view policy receipt has unexpected fields")
    return dict(value)


def validate_repeat_identity(arm: str, repeats: Sequence[Mapping[str, Any]]) -> None:
    if len(repeats) != REPEATS:
        raise RuntimeError(f"{arm} requires exactly two repeats")
    scalar_fields = (
        "input_projection_sha256",
        "input_projection_content_sha256",
        "projection_sha256",
        "dialog_ranking_sha256",
        "evidence_ranking_sha256",
        "policy_receipts_sha256",
    )
    if any(repeats[0].get(field) != repeats[1].get(field) for field in scalar_fields):
        raise RuntimeError(f"{arm} repeat identity drift")
    if repeats[0].get("trace_receipt", {}).get("trace_sha256") != repeats[1].get(
        "trace_receipt", {}
    ).get("trace_sha256"):
        raise RuntimeError(f"{arm} repeat trace drift")
    if canonical_sha256(repeats[0].get("original_product_configuration")) != canonical_sha256(
        repeats[1].get("original_product_configuration")
    ):
        raise RuntimeError(f"{arm} repeat original-product configuration drift")


def supervise_worker(command: Sequence[str], *, sidecar: Path, freeze_path: Path) -> dict[str, Any]:
    """Coordinator-owned process-tree RSS authority for a worker lifetime."""
    if sidecar.exists(): raise FileExistsError("worker supervisor sidecar already exists")
    process = subprocess.Popen(list(command), cwd=ROOT)
    with RssMonitor(process.pid) as monitor:
        code = process.wait()
    if code: raise subprocess.CalledProcessError(code, list(command))
    if not freeze_path.is_file(): raise RuntimeError("worker succeeded without a frozen receipt")
    if monitor.peak_bytes <= 0: raise RuntimeError("worker supervisor obtained no RSS sample")
    receipt = {"pid": process.pid, "exit_code": code, "command_sha256": canonical_sha256(list(command)), "freeze_path": str(freeze_path), "freeze_sha256": sha256_file(freeze_path), "observed_process_tree_peak_rss_bytes": monitor.peak_bytes, "rss_cap_bytes": RSS_CAP_BYTES}
    _publish_nonreplace(sidecar, _canonical(receipt))
    return receipt


def latency_receipt(samples_ns: Sequence[int]) -> dict[str, Any]:
    raw_values = [int(value) for value in samples_ns]
    values = sorted(raw_values)
    if len(values) != 1982 or any(value <= 0 for value in values):
        raise ValueError("AERP5 requires exactly 1,982 positive per-query latency samples")
    def pct(fraction: float) -> int:
        return values[math.ceil((len(values) - 1) * fraction)]
    return {"count": len(values), "cold_first_ns": raw_values[0], "p50_ns": pct(.50), "p95_ns": pct(.95), "p99_ns": pct(.99), "mean_ns": statistics.fmean(values), "max_ns": values[-1], "samples_ns": raw_values, "samples_sha256": canonical_sha256(raw_values), "cold_first_query_included": True}


def distribution_receipt(samples_ns: Sequence[int], *, expected_count: int, label: str) -> dict[str, Any]:
    raw_values = [int(value) for value in samples_ns]
    values = sorted(raw_values)
    if len(values) != expected_count or any(value <= 0 for value in values): raise ValueError(f"{label} sample count/value mismatch")
    def pct(value: float) -> int: return values[math.ceil((len(values) - 1) * value)]
    return {"count": len(values), "p50_ns": pct(.50), "p95_ns": pct(.95), "p99_ns": pct(.99), "mean_ns": statistics.fmean(values), "max_ns": values[-1], "samples_ns": raw_values, "samples_sha256": canonical_sha256(raw_values)}


class RssMonitor:
    """Fail-closed process-tree RSS sampler, spanning load through serialization."""
    def __init__(self, root_pid: int, cap_bytes: int = RSS_CAP_BYTES) -> None:
        self.root_pid, self.cap_bytes, self.peak_bytes = root_pid, cap_bytes, 0
        self.error: str | None = None; self._stop = threading.Event(); self._thread: threading.Thread | None = None

    def _sample_once(self) -> None:
        import psutil
        try: root = psutil.Process(self.root_pid); processes = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess: return
        total = 0
        for process in processes:
            try:
                if process.is_running():
                    total += process.memory_info().rss
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        self.peak_bytes = max(self.peak_bytes, total)
        if total > self.cap_bytes: self.error = f"RSS cap exceeded: {total} > {self.cap_bytes}"

    def _sample(self) -> None:
        try:
            while not self._stop.wait(.02):
                self._sample_once()
                if self.error: return
        except BaseException as exc:  # measurement unavailable is a formal failure
            self.error = f"RSS monitor failed: {type(exc).__name__}: {exc}"

    def __enter__(self) -> "RssMonitor":
        try: self._sample_once()
        except BaseException as exc: self.error = f"RSS monitor failed: {type(exc).__name__}: {exc}"
        if self.error: raise RuntimeError(self.error)
        self._thread = threading.Thread(target=self._sample, daemon=True); self._thread.start(); return self

    def __exit__(self, *_: Any) -> None:
        self._stop.set()
        if self._thread is not None: self._thread.join()
        if self.error is None:
            try: self._sample_once()
            except BaseException as exc: self.error = f"RSS monitor failed: {type(exc).__name__}: {exc}"
        if self.error: raise RuntimeError(self.error)


def coordinator_run(*, dataset: Path, original_root: Path, model_dir: Path, study: PinnedJson, custody: PinnedJson, train: PinnedJson, dev: PinnedJson, work: Path, output: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Create projection, launch 3x2 disposable workers, and validate freezes."""
    canonical_manifest = load_manifest()
    if dict(manifest) != canonical_manifest:
        raise ValueError("coordinator requires the canonical AERP5 v2 manifest")
    v1.require_external_output(work, ROOT, original_root); v1.require_external_output(output, ROOT, original_root)
    before_current, before_original = v1.git_state(ROOT), v1.require_clean_pinned_original(original_root)
    dataset_before, model_before = sha256_file(dataset), v1.file_tree_receipt(model_dir)
    membership = validate_aerp4_membership(study=study, custody=custody, train_freeze=train, dev_freeze=dev, manifest=manifest)
    receipt = environment_receipt(dataset=dataset, model_dir=model_dir, original_root=original_root); validate_environment_receipt(receipt, manifest)
    if work.exists() or output.exists(): raise FileExistsError("AERP5 coordinator refuses to clobber work/output")
    work.mkdir(parents=True); projection = build_public_projection(dataset=dataset, original_root=original_root, allowed_item_tokens=membership["item_tokens"])
    projection_path = work / "projection.json"; projection_path.write_bytes(_canonical(projection))
    projection_file_sha256 = sha256_file(projection_path)
    projection_content_sha256 = canonical_sha256(projection)
    projection_tokens = tuple(row["item_token"] for row in projection)
    outputs: dict[str, list[dict[str, Any]]] = {arm: [] for arm in ALL_ARMS}
    try:
        for arm in ALL_ARMS:
            for repeat in range(REPEATS):
                path = work / f"{arm}-{repeat}.json"; backend = work / f"{arm}-{repeat}-backend"
                config = {"arm": arm, "projection": str(projection_path), "output": str(path), "temporary_backend": str(backend), "original_root": str(original_root), "model_dir": str(model_dir)}
                config_path = work / f"{arm}-{repeat}-worker.json"; config_path.write_bytes(_canonical(config))
                sidecar = work / f"{arm}-{repeat}-supervisor.json"
                supervisor = supervise_worker([sys.executable, "-m", "benchmarks.aerp5_product_paired_locomo_v2", "--worker-config", str(config_path)], sidecar=sidecar, freeze_path=path)
                parsed = parse_worker_freeze(
                    _json(path),
                    arm=arm,
                    projection_sha256=projection_file_sha256,
                    projection_content_sha256=projection_content_sha256,
                    item_tokens=projection_tokens,
                    model_sha256=manifest["run"]["model"]["file_tree_sha256"],
                )
                parsed["supervisor"] = supervisor; outputs[arm].append(parsed)
        for arm, repeats in outputs.items():
            validate_repeat_identity(arm, repeats)
        frozen_rows = [*_freeze_items(train.load(), "train"), *_freeze_items(dev.load(), "dev")]
        validate_p5_against_aerp4({key: row["evidence_top10"] for key, row in outputs[ARM_P5][0]["items"].items()}, frozen_rows)
        freeze_paths = {arm: [str(work / f"{arm}-{repeat}.json") for repeat in range(REPEATS)] for arm in ALL_ARMS}; freeze_hashes = {arm: [sha256_file(Path(path)) for path in paths] for arm, paths in freeze_paths.items()}
        scorer_receipt = scorer_implementation_receipt(original_root)
        score_path = work / "custodian-score.json"; score_config = {"projection": str(projection_path), "expected_projection_sha256": projection_file_sha256, "expected_projection_content_sha256": projection_content_sha256, "expected_model_sha256": manifest["run"]["model"]["file_tree_sha256"], "freezes": freeze_paths, "expected_freeze_sha256": freeze_hashes, "dataset": str(dataset), "expected_dataset_sha256": manifest["dataset"]["sha256"], "scorer_implementation_receipt": scorer_receipt, "original_root": str(original_root), "scientific_gates": manifest["scientific_gates"], "expected_scientific_gates_sha256": canonical_sha256(manifest["scientific_gates"]), "manifest_sha256": canonical_sha256(manifest), "output": str(score_path)}
        score_config_path = work / "custodian-config.json"; score_config_path.write_bytes(_canonical(score_config))
        subprocess.run([sys.executable, "-m", "benchmarks.aerp5_product_paired_locomo_v2", "--score-config", str(score_config_path)], cwd=ROOT, check=True)
        after_current, after_original = v1.git_state(ROOT), v1.git_state(original_root)
        if after_current != before_current or after_original != before_original: raise RuntimeError("measured repository state drifted during formal run")
        if sha256_file(dataset) != dataset_before or v1.file_tree_receipt(model_dir) != model_before: raise RuntimeError("dataset/model file tree drifted during formal run")
        score = _json(score_path)
        expected_score_receipts = {"dataset_sha256": manifest["dataset"]["sha256"], "projection_sha256": projection_file_sha256, "projection_content_sha256": projection_content_sha256, "freeze_sha256": freeze_hashes, "scientific_gates_sha256": canonical_sha256(manifest["scientific_gates"]), "manifest_sha256": canonical_sha256(manifest)}
        if score.get("input_receipts") != expected_score_receipts: raise RuntimeError("custodian score receipts do not match pre-score inputs")
        if score.get("scorer_implementation_receipt") != scorer_receipt: raise RuntimeError("custodian scorer implementation receipt drifted")
        report = {"engineering_gates_passed": True, "environment": receipt, "repository_state_before": {"current": before_current, "original": before_original}, "repository_state_after": {"current": after_current, "original": after_original}, "projection_sha256": sha256_file(projection_path), "freezes": outputs, "score": score, "freeze_file_sha256": freeze_hashes}
        publish_report(output=output, manifest=manifest, report=report); return report
    except BaseException:
        # No partial formal report is published; work is retained for diagnosis.
        raise


def environment_receipt(*, dataset: Path, model_dir: Path, original_root: Path) -> dict[str, Any]:
    package_names = ("chromadb", "onnxruntime", "psutil", "mempalace-rpg")
    packages = {}
    for name in package_names:
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = "not-installed"
    try:
        import psutil; hardware = {"machine": platform.machine(), "processor": platform.processor(), "logical_cores": os.cpu_count(), "physical_cores": psutil.cpu_count(logical=False), "total_ram_bytes": psutil.virtual_memory().total}
    except ImportError: raise RuntimeError("psutil is required for formal hardware receipt")
    try:
        import numpy; numpy_version = numpy.__version__
    except ImportError: numpy_version = "not-installed"
    return {"python": sys.version, "executable": sys.executable, "platform": platform.platform(), "hardware": hardware, "packages": packages | {"numpy": numpy_version, "sqlite": sqlite3.sqlite_version}, "dataset_sha256": sha256_file(dataset), "model": v1.file_tree_receipt(model_dir), "original": v1.git_state(original_root), "latest": v1.git_state(ROOT), "lockfiles": {path.name: sha256_file(path) for path in sorted(ROOT.glob("*lock*")) if path.is_file()}}


def validate_environment_receipt(receipt: Mapping[str, Any], manifest: Mapping[str, Any]) -> None:
    """Reject unpinned corpus/model/provider/repository environments."""
    if receipt.get("dataset_sha256") != manifest["dataset"]["sha256"]:
        raise ValueError("official LoCoMo dataset digest mismatch")
    model = receipt.get("model", {})
    if model.get("sha256") != manifest["run"]["model"]["file_tree_sha256"]:
        raise ValueError("MiniLM file tree digest mismatch")
    original = receipt.get("original", {})
    if original.get("git_head") != manifest["original"]["commit"] or original.get("git_tree") != manifest["original"]["tree"] or original.get("git_dirty"):
        raise ValueError("original public-product checkout is not the clean pinned source")
    if receipt.get("latest", {}).get("git_dirty"):
        raise ValueError("current product checkout must be clean for a formal AERP5 run")


def paired_cluster_bootstrap(rows: list[dict[str, Any]], *, current: str, original: str, estimand: str) -> dict[str, Any]:
    return v1.paired_group_bootstrap(rows, current=current, original=original, estimand=estimand)


def scorer_implementation_receipt(original_root: Path) -> dict[str, Any]:
    protocol = original_root / "benchmarks" / "locomo_story_protocol.py"
    return {"files": {"aerp5_runner": sha256_file(Path(__file__)), "aerp1_scorer": sha256_file(Path(aerp1.__file__)), "original_locomo_protocol": sha256_file(protocol)}, "current_git": v1.git_state(ROOT), "original_git": v1.git_state(original_root)}


def score_after_all_freezes(*, scorer: Any, freezes: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Only legal scoring entry point; requires both identical repeats for all arms."""
    if set(freezes) != set(ALL_ARMS): raise RuntimeError("all arms must freeze before scoring")
    rankings: dict[str, dict[str, list[str]]] = {}
    for arm, repeats in freezes.items():
        if len(repeats) != REPEATS or repeats[0]["projection_sha256"] != repeats[1]["projection_sha256"]:
            raise RuntimeError("all arm reruns must complete with stable projection digests")
        rankings[arm] = dict(repeats[0]["items"])
    # The custodian API is intentionally explicit; no label object appears in rank/freeze APIs.
    rows: list[dict[str, Any]] = []
    for token, item in scorer.scorer_items.items():
        official = item.official_exact
        metrics = {arm: aerp1.question_metrics(ranking[token]["dialog_top10"], official.resolved_opaque_dialog_ids, evidence_item_count=official.source_evidence_item_count, unresolved_evidence_item_count=official.unresolved_evidence_item_count, top_k=TOP_K) for arm, ranking in rankings.items()}
        if not all(metric["scored"] for metric in metrics.values()): raise ValueError("AERP4 membership must not contain a zero-evidence score row")
        recall = {arm: float(metric["recall_at_10"]) for arm, metric in metrics.items()}
        rows.append({"conversation_id": item.opaque_conversation_id, "item_token": token, "category": item.category, "unresolved": official.unresolved_evidence_item_count, "official_metrics": metrics, "recall": recall})
    if len(rows) != 1982: raise RuntimeError("scoring denominator must remain 1,982")
    return {"question_macro": {arm: statistics.fmean(row["recall"][arm] for row in rows) for arm in ALL_ARMS}, "paired_cluster_bootstrap": {"p5_vs_original_question": paired_cluster_bootstrap(rows, current=ARM_P5, original=ARM_ORIGINAL, estimand="question_macro_cluster_bootstrap"), "p5_vs_original_conversation": paired_cluster_bootstrap(rows, current=ARM_P5, original=ARM_ORIGINAL, estimand="conversation_macro")}}, rows


def summarize_scored_rows(subset: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate every frozen official endpoint for one named report slice."""
    if not subset:
        return {"count": 0}
    conversations = sorted({str(row["conversation_id"]) for row in subset})
    arm_metrics: dict[str, dict[str, Any]] = {}
    for arm in ALL_ARMS:
        metrics = [row["official_metrics"][arm] for row in subset]
        denominator = sum(metric["evidence_item_count"] for metric in metrics)
        if denominator <= 0:
            raise ValueError("scored report slice has no evidence denominator")
        arm_metrics[arm] = {
            "question_macro_recall_at_10": statistics.fmean(float(metric["recall_at_10"]) for metric in metrics),
            "conversation_macro_recall_at_10": statistics.fmean(
                statistics.fmean(float(row["recall"][arm]) for row in subset if row["conversation_id"] == conversation)
                for conversation in conversations
            ),
            "evidence_micro_recall_at_10": sum(metric["retrieved_evidence_count_at_10"] for metric in metrics) / denominator,
            "hit_at_10": statistics.fmean(float(metric["hit_at_10"]) for metric in metrics),
            "all_at_10": statistics.fmean(float(metric["all_at_10"]) for metric in metrics),
            "ndcg_at_10": statistics.fmean(float(metric["ndcg_at_10"]) for metric in metrics),
            "evidence_item_count": denominator,
            "resolved_evidence_item_count": sum(metric["resolved_evidence_item_count"] for metric in metrics),
            "unresolved_evidence_item_count": sum(metric["unresolved_evidence_item_count"] for metric in metrics),
        }
    return {
        "count": len(subset),
        "arms": arm_metrics,
        "paired": {
            "question": paired_cluster_bootstrap(list(subset), current=ARM_P5, original=ARM_ORIGINAL, estimand="question_macro_cluster_bootstrap"),
            "conversation": paired_cluster_bootstrap(list(subset), current=ARM_P5, original=ARM_ORIGINAL, estimand="conversation_macro"),
        },
    }


def custodian_score_run(config: Mapping[str, Any]) -> dict[str, Any]:
    """The only stage that reads official evidence labels, after all freezes."""
    projection_path = Path(str(config["projection"])); dataset_path = Path(str(config["dataset"])); freeze_paths = config.get("freezes")
    gates = config.get("scientific_gates")
    if not isinstance(gates, Mapping) or dict(gates) != EXPECTED_SCIENTIFIC_GATES:
        raise ValueError("custodian needs the canonical frozen scientific gates")
    if canonical_sha256(gates) != config.get("expected_scientific_gates_sha256"):
        raise RuntimeError("custodian scientific gates drifted")
    if config.get("manifest_sha256") != canonical_sha256(load_manifest()):
        raise RuntimeError("custodian canonical manifest receipt drifted")
    scorer_receipt = scorer_implementation_receipt(Path(str(config["original_root"])))
    if scorer_receipt != config.get("scorer_implementation_receipt"): raise RuntimeError("scorer implementation receipt drift before scoring")
    if sha256_file(projection_path) != config.get("expected_projection_sha256") or sha256_file(dataset_path) != config.get("expected_dataset_sha256"): raise RuntimeError("custodian input drift before scoring")
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    if not isinstance(projection, list) or len(projection) != 1982 or not isinstance(freeze_paths, Mapping) or set(freeze_paths) != set(ALL_ARMS): raise ValueError("custodian input contract is malformed")
    projection_content_sha256 = canonical_sha256(projection)
    if projection_content_sha256 != config.get("expected_projection_content_sha256"):
        raise RuntimeError("custodian projection content drift before scoring")
    projection_tokens = tuple(row["item_token"] for row in projection)
    freezes: dict[str, list[dict[str, Any]]] = {}
    for arm, paths in freeze_paths.items():
        if not isinstance(paths, list) or len(paths) != 2: raise ValueError("custodian requires two freezes per arm")
        if [sha256_file(Path(path)) for path in paths] != config.get("expected_freeze_sha256", {}).get(arm): raise RuntimeError("freeze drift before scoring")
        repeats = [parse_worker_freeze(_json(Path(path)), arm=arm, projection_sha256=config["expected_projection_sha256"], projection_content_sha256=projection_content_sha256, item_tokens=projection_tokens, model_sha256=config["expected_model_sha256"]) for path in paths]
        validate_repeat_identity(arm, repeats)
        freezes[arm] = repeats
    # This is deliberately after complete-rerun validation.
    _palace, _searcher, protocol, _state = v1.load_original_product(Path(str(config["original_root"])))
    dataset = protocol.load_official_locomo10(dataset_path)
    _retrieval, scorer = protocol.prepare_hard_story_track(
        dataset, candidate_pool_size=TOP_K, require_official_counts=True
    )
    item_to_token = {row["item_id"]: row["item_token"] for row in projection}
    scorer_view = type("ScorerView", (), {"scorer_items": {item_to_token[key]: value for key, value in scorer.scorer_items.items() if key in item_to_token}})()
    aggregate, rows = score_after_all_freezes(scorer=scorer_view, freezes=freezes)
    if sum(row["unresolved"] for row in rows) != sum(scorer.scorer_items[key].official_exact.unresolved_evidence_item_count for key in item_to_token): raise RuntimeError("unresolved evidence disappeared from score ledger")
    categories = {str(category): summarize_scored_rows([row for row in rows if row["category"] == category]) for category in sorted({row["category"] for row in rows})}
    q = aggregate["paired_cluster_bootstrap"]["p5_vs_original_question"]; c = aggregate["paired_cluster_bootstrap"]["p5_vs_original_conversation"]
    decision = {"rule": "both P5-vs-original overall paired CI lower bounds strictly exceed frozen zero", "question_ci_lower": q["lower_95"], "conversation_ci_lower": c["lower_95"], "go": q["lower_95"] > gates["p5_vs_original_question_ci_lower_strictly_gt"] and c["lower_95"] > gates["p5_vs_original_conversation_ci_lower_strictly_gt"]}
    if scorer_implementation_receipt(Path(str(config["original_root"]))) != scorer_receipt: raise RuntimeError("scorer implementation receipt drift after scoring")
    result = {"schema": SCHEMA + "-custodian-score", "scorer_implementation_receipt": scorer_receipt, "input_receipts": {"dataset_sha256": sha256_file(dataset_path), "projection_sha256": sha256_file(projection_path), "projection_content_sha256": projection_content_sha256, "freeze_sha256": {arm: [sha256_file(Path(path)) for path in paths] for arm, paths in freeze_paths.items()}, "scientific_gates_sha256": canonical_sha256(gates), "manifest_sha256": config.get("manifest_sha256")}, "aggregate": aggregate, "overall": summarize_scored_rows(rows), "hard_cat1_2": summarize_scored_rows([row for row in rows if row["category"] in {1, 2}]), "cat5": summarize_scored_rows([row for row in rows if row["category"] == 5]), "per_category": categories, "scientific_decision": decision, "unresolved_evidence_item_count": sum(row["unresolved"] for row in rows), "freeze_digests": {arm: [sha256_file(Path(path)) for path in paths] for arm, paths in freeze_paths.items()}}
    if sha256_file(projection_path) != config.get("expected_projection_sha256") or sha256_file(dataset_path) != config.get("expected_dataset_sha256") or any([sha256_file(Path(path)) for path in paths] != config["expected_freeze_sha256"][arm] for arm, paths in freeze_paths.items()): raise RuntimeError("custodian input drift after scoring")
    _publish_nonreplace(Path(str(config["output"])), _canonical(result)); return result


def publish_report(*, output: Path, manifest: Mapping[str, Any], report: Mapping[str, Any]) -> None:
    if output.exists(): raise FileExistsError("refusing to clobber formal AERP5 report")
    temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
    if temporary.exists(): raise FileExistsError("stale temporary report exists")
    payload = {"schema": SCHEMA + "-report", "manifest_sha256": canonical_sha256(manifest), "public_nonblind": True, "confirmation_claim": False, "product_comparison_complete": bool(report.get("engineering_gates_passed")), "resource_gate_eligible": False, "resource_gate_blockers": ["mixed-visibility", "blind-180", "30k-event", "SQLite/drawer failure-injection", "frozen resource thresholds"], "report": dict(report)}
    temporary.unlink(missing_ok=True)
    _publish_nonreplace(output, _canonical(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path(__file__).with_name("aerp5_product_paired_locomo_v2_manifest.json"))
    parser.add_argument("--tau", help=argparse.SUPPRESS)
    parser.add_argument("--router", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker-config", type=Path)
    parser.add_argument("--score-config", type=Path)
    parser.add_argument("--dataset", type=Path); parser.add_argument("--original-root", type=Path); parser.add_argument("--model-dir", type=Path); parser.add_argument("--study", type=Path); parser.add_argument("--custody", type=Path); parser.add_argument("--train-freeze", type=Path); parser.add_argument("--dev-freeze", type=Path); parser.add_argument("--work", type=Path)
    args = parser.parse_args(argv)
    if args.tau is not None or args.router is not None: raise ValueError("AERP5 v2 rejects tau/router inputs")
    load_manifest(args.manifest)
    if args.worker_config is not None:
        worker_run(_json(args.worker_config)); return 0
    if args.score_config is not None:
        custodian_score_run(_json(args.score_config)); return 0
    required = (args.dataset, args.original_root, args.model_dir, args.study, args.custody, args.train_freeze, args.dev_freeze, args.work, args.output)
    if any(value is None for value in required): parser.error("coordinator requires dataset/original/model/AERP4 pins/work/output")
    coordinator_run(dataset=args.dataset, original_root=args.original_root, model_dir=args.model_dir, study=PinnedJson.pin(args.study, load_manifest(args.manifest)["aerp4"]["study_sha256"]), custody=PinnedJson.pin(args.custody, load_manifest(args.manifest)["aerp4"]["custody_bundle_sha256"]), train=PinnedJson.pin(args.train_freeze, load_manifest(args.manifest)["aerp4"]["train_ranking_freeze_sha256"]), dev=PinnedJson.pin(args.dev_freeze, load_manifest(args.manifest)["aerp4"]["dev_ranking_freeze_sha256"]), work=args.work, output=args.output, manifest=load_manifest(args.manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
