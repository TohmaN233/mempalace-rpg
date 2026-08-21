"""Frozen 180-query AERP-1 blind authorization and B0 non-regression harness."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from mempalace_rpg import RpgMemoryKernel, SceneEventInput

FIXTURE_DIR = Path(__file__).with_name("fixtures")
SPEC_PATH = FIXTURE_DIR / "aerp1_blind_180_spec.json"
QUERY_PATH = FIXTURE_DIR / "aerp1_blind_180_queries.json"
ORACLE_PATH = FIXTURE_DIR / "aerp1_blind_180_oracle.json"
HERO, CAMPAIGN = "blind-hero", "blind-campaign"
EXPECTED_SPEC_SHA256 = "0c00e6bd606532ec4efe06ac59aafcb2034d839f36720485988cf480d13c81d8"
EXPECTED_QUERY_SHA256 = "3aaeb9e83fc2d9bc749c4d66f18460f34b7fd5cb7067effadefb3047dc011a3b"
EXPECTED_ORACLE_SHA256 = "02daad616c828ff270f8cc9adc53cdcc4d9e7e365bd8ac5bb5ccdd34f2a80c54"
EXPECTED_B0_COMMIT = "8dca38c3d23c0e7c9f36fff10944bb86e71c3819"
EXPECTED_B0_RANKER_SHA256 = "7f8b702bfc1923d8a59c4569e436d43a185dcef39cab31eb7b0b8c0d3073d1d2"
POSITIVE = {"exact_public", "paraphrase", "long_range_promise", "temporal_location", "rightful_private_witness", "actor_belief"}
NEUTRAL = {"exact_public", "paraphrase", "long_range_promise", "temporal_location"}
ALL_CATEGORIES = POSITIVE | {"gm_wrong_actor_private", "retconned_abandoned", "faction_quest_outsider", "belief_as_canon", "cross_campaign", "mixed_visibility_forbidden_span"}


@dataclass(frozen=True)
class SeedFact:
    gold_event_id: str | None = None
    forbidden_event_ids: tuple[str, ...] = ()
    forbidden_spans: tuple[str, ...] = ()
    location_id: str | None = None


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _file_sha256(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def _load_spec() -> tuple[dict[str, Any], str]:
    raw, digest = _file_sha256(SPEC_PATH)
    if digest != EXPECTED_SPEC_SHA256:
        raise ValueError("frozen blind spec sha256 mismatch")
    return json.loads(raw), digest


def load_query_bundle() -> tuple[dict[str, Any], dict[str, Any]]:
    """Execution phase opens spec + queries only; it never opens the oracle file."""
    spec, spec_digest = _load_spec()
    raw, digest = _file_sha256(QUERY_PATH)
    query_spec = spec["fixtures"]["queries"]
    if (
        digest != EXPECTED_QUERY_SHA256
        or digest != query_spec["sha256"]
        or query_spec["path"] != QUERY_PATH.name
    ):
        raise ValueError("frozen query bundle sha256 mismatch")
    return {
        "baseline": spec["baseline"],
        "phase_contract": spec["phase_contract"],
        "spec_sha256": spec_digest,
        "query_bundle_sha256": digest,
    }, json.loads(raw)


def load_evaluator_oracle() -> dict[str, Any]:
    """Open the independent oracle file only after the product-output boundary."""
    spec, _spec_digest = _load_spec()
    raw, digest = _file_sha256(ORACLE_PATH)
    oracle_spec = spec["fixtures"]["oracle"]
    if (
        digest != EXPECTED_ORACLE_SHA256
        or digest != oracle_spec["sha256"]
        or oracle_spec["path"] != ORACLE_PATH.name
    ):
        raise ValueError("frozen evaluator oracle sha256 mismatch")
    return json.loads(raw)


def validate_freeze_spec(metadata: dict[str, Any], bundle: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    baseline = metadata["baseline"]
    if baseline.get("commit") != EXPECTED_B0_COMMIT:
        errors.append("b0_commit_mismatch")
    if baseline.get("ranker_source", {}).get("sha256") != EXPECTED_B0_RANKER_SHA256:
        errors.append("b0_ranker_source_digest_mismatch")
    if not baseline.get("contract", {}).get("score"):
        errors.append("b0_ranker_contract_missing")
    cases, counts = bundle.get("cases", []), Counter(item.get("category") for item in bundle.get("cases", []))
    if len(cases) != 180 or len(counts) != 12 or any(count != 15 for count in counts.values()):
        errors.append("blind_query_bundle_shape_mismatch")
    if len({item.get("id") for item in cases}) != 180:
        errors.append("blind_query_ids_not_unique")
    if set(counts) != ALL_CATEGORIES or set(bundle.get("categories", [])) != ALL_CATEGORIES:
        errors.append("blind_query_category_contract_mismatch")
    return errors


def _event(summary: str, span: str, *, visibility: str = "public_world", truth_status: str = "canonical", importance: float = 50.0, **security: Any) -> SceneEventInput:
    return SceneEventInput(
        event_type="blind_evidence", summary=summary, branch_id="main",
        branch_status=truth_status if truth_status in {"retconned", "abandoned"} else "active",
        source_span=span, visibility=visibility, truth_status=truth_status,
        importance=importance, **security,
    )


def _commit(kernel: RpgMemoryKernel, *, scene_id: str, event: SceneEventInput, campaign_id: str = CAMPAIGN, location_id: str | None = None) -> str:
    kernel.commit_scene(
        campaign_id=campaign_id, scene_id=scene_id, in_world_time=f"blind-time-{scene_id}",
        location_id=location_id or f"blind-location-{scene_id}",
        transcript=f"BEGIN [{event.source_span}] END", participants=[HERO], witnesses=[HERO], events=[event],
    )
    row = kernel._conn().execute("SELECT event_id FROM scene_event WHERE scene_id=? AND summary=?", (scene_id, event.summary)).fetchone()
    assert row is not None
    return str(row["event_id"])


def _suffix(case: dict[str, Any]) -> str:
    return str(case["id"]).rsplit("-", 1)[1]


def _seed_case(kernel: RpgMemoryKernel, case: dict[str, Any]) -> SeedFact:
    category, suffix = str(case["category"]), _suffix(case)
    scene = f"blind-{case['id']}"
    if category == "exact_public":
        event = _event(f"exact public archive {suffix}", f"BLIND EXACT GOLD {suffix}")
        return SeedFact(gold_event_id=_commit(kernel, scene_id=scene, event=event))
    if category == "paraphrase":
        event = _event(f"orchid ledger sealed {suffix}", f"BLIND PARAPHRASE GOLD {suffix}")
        return SeedFact(gold_event_id=_commit(kernel, scene_id=scene, event=event))
    if category == "long_range_promise":
        event = _event(f"long promise heirloom {suffix}", f"BLIND LONG RANGE GOLD {suffix}", importance=100.0)
        return SeedFact(gold_event_id=_commit(kernel, scene_id=scene, event=event))
    if category == "temporal_location":
        location = f"blind-temporal-location-{suffix}"
        target = _event(f"temporal beacon {suffix}", f"BLIND TEMPORAL GOLD {suffix}", related_locations=[location])
        target_id = _commit(kernel, scene_id=scene, event=target, location_id=location)
        decoy = _event(f"temporal beacon {suffix}", f"BLIND TEMPORAL DECOY {suffix}", importance=0.0, related_locations=[f"blind-temporal-away-{suffix}"])
        _commit(kernel, scene_id=f"{scene}-away", event=decoy, location_id=f"blind-temporal-away-{suffix}")
        return SeedFact(gold_event_id=target_id, location_id=location)
    if category == "rightful_private_witness":
        if int(suffix) % 2:
            event = _event(f"rightful private witness {suffix}", f"BLIND RIGHTFUL PRIVATE GOLD {suffix}", visibility="character_private", access_owner_id=HERO)
        else:
            event = _event(f"rightful private witness {suffix}", f"BLIND RIGHTFUL WITNESS GOLD {suffix}", visibility="witnessed_only", witness_set=[HERO])
        return SeedFact(gold_event_id=_commit(kernel, scene_id=scene, event=event))
    if category == "actor_belief":
        event = _event(f"hero belief {suffix}", f"BLIND HERO BELIEF GOLD {suffix}", truth_status="belief", actor_id=HERO, belief_owner_id=HERO)
        return SeedFact(gold_event_id=_commit(kernel, scene_id=scene, event=event))
    if category == "gm_wrong_actor_private":
        event = _event(f"gm or private secret {suffix}", f"BLIND GM FORBIDDEN {suffix}", visibility="gm_only") if int(suffix) % 2 else _event(f"gm or private secret {suffix}", f"BLIND PRIVATE FORBIDDEN {suffix}", visibility="character_private", access_owner_id="blind-other")
    elif category == "retconned_abandoned":
        truth = "retconned" if int(suffix) % 2 else "abandoned"
        event = _event(f"retired branch secret {suffix}", f"BLIND {truth.upper()} FORBIDDEN {suffix}", truth_status=truth)
    elif category == "faction_quest_outsider":
        event = _event(f"scoped outsider secret {suffix}", f"BLIND FACTION FORBIDDEN {suffix}", visibility="faction_private", access_scope_id=f"blind-faction-{suffix}") if int(suffix) % 2 else _event(f"scoped outsider secret {suffix}", f"BLIND QUEST FORBIDDEN {suffix}", visibility="quest_participants", access_scope_id=f"blind-quest-{suffix}")
    elif category == "belief_as_canon":
        event = _event(f"other actor belief {suffix}", f"BLIND OTHER BELIEF FORBIDDEN {suffix}", truth_status="belief", actor_id="blind-other", belief_owner_id="blind-other")
    elif category == "cross_campaign":
        event = _event(f"other campaign secret {suffix}", f"BLIND CROSS CAMPAIGN FORBIDDEN {suffix}")
        event_id = _commit(kernel, scene_id=scene, event=event, campaign_id="blind-other-campaign")
        return SeedFact(forbidden_event_ids=(event_id,), forbidden_spans=(str(event.source_span),))
    elif category == "mixed_visibility_forbidden_span":
        allowed = _event(f"mixed allowed evidence {suffix}", f"BLIND MIXED ALLOWED {suffix}")
        forbidden = _event(f"mixed forbidden evidence {suffix}", f"BLIND MIXED FORBIDDEN {suffix}", visibility="character_private", access_owner_id="blind-other")
        kernel.commit_scene(campaign_id=CAMPAIGN, scene_id=scene, in_world_time=f"blind-time-{scene}", location_id=f"blind-location-{scene}", transcript=f"SAFE [{allowed.source_span}] SECRET [{forbidden.source_span}]", participants=[HERO], witnesses=[HERO], events=[allowed, forbidden])
        rows = kernel._conn().execute("SELECT event_id, summary FROM scene_event WHERE scene_id=?", (scene,)).fetchall()
        ids = {str(row["summary"]): str(row["event_id"]) for row in rows}
        return SeedFact(forbidden_event_ids=(ids[forbidden.summary],), forbidden_spans=(str(forbidden.source_span),))
    else:
        raise ValueError(f"unknown frozen blind category: {category}")
    event_id = _commit(kernel, scene_id=scene, event=event)
    return SeedFact(forbidden_event_ids=(event_id,), forbidden_spans=(str(event.source_span),))


def seed_blind_suite(kernel: RpgMemoryKernel, bundle: dict[str, Any]) -> tuple[dict[str, SeedFact], dict[str, int]]:
    kernel.upsert_character_profile(character_id=HERO, display_name="Blind Hero", tier="core", short_persona="blind audit actor", memory_wing="wing-blind-audit")
    cases = list(bundle["cases"])
    early = next(case for case in cases if case["id"] == "exact_public-01")
    facts = {str(early["id"]): _seed_case(kernel, early)}
    early_sort = kernel._conn().execute("SELECT scene_time_sort FROM scene_record WHERE scene_id=?", ("blind-exact_public-01",)).fetchone()[0]
    for index in range(1001):
        _commit(kernel, scene_id=f"blind-ceiling-noise-{index:04d}", event=_event(f"blind ceiling noise {index:04d}", f"BLIND CEILING NOISE {index:04d}", importance=0.0))
    noise_count = int(kernel._conn().execute("SELECT COUNT(*) FROM scene_event WHERE scene_id LIKE 'blind-ceiling-noise-%'").fetchone()[0])
    first_noise_sort = kernel._conn().execute("SELECT MIN(scene_time_sort) FROM scene_record WHERE scene_id LIKE 'blind-ceiling-noise-%'").fetchone()[0]
    for case in cases:
        if case["id"] != early["id"]:
            facts[str(case["id"])] = _seed_case(kernel, case)
    return facts, {"real_scene_event_noise_count": noise_count, "early_gold_scene_time_sort": int(early_sort), "first_noise_scene_time_sort": int(first_noise_sort)}


def _delivered_spans(deep: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "text": span["text"], "truncated": bool(span.get("truncated"))} for scene in deep["scene_evidence"] for span in scene["authorized_spans"]]


def _capture_call(kernel: RpgMemoryKernel, case: dict[str, Any], fact: SeedFact) -> list[dict[str, Any]]:
    shared = {"campaign_id": CAMPAIGN, "actor_id": HERO, "actor_type": "npc", "query": case["query"], "location_id": fact.location_id, "max_chars": 12000}
    ordinary = kernel.build_memory_pack(**shared)
    deep = kernel.deep_recall(**shared, per_scene_chars=12000)
    return [
        {"case_id": case["id"], "path": "ordinary", "selected_event_ids": [item["source_event_id"] for item in ordinary.evidence], "delivered_spans": [], "rendered": ordinary.render(), "policy_trace": ordinary.policy_trace},
        {"case_id": case["id"], "path": "deep", "selected_event_ids": [item["source_event_id"] for item in deep["evidence"]], "delivered_spans": _delivered_spans(deep), "rendered": deep["rendered"], "policy_trace": deep["policy_trace"]},
    ]


def _trace_errors(record: dict[str, Any]) -> list[str]:
    trace = record["policy_trace"]
    required = {"policy", "campaign_id", "actor_id", "actor_type", "candidate_generation", "candidates", "deduplication", "authorized_candidate_ids", "selected_evidence_ids", "returned_spans"}
    errors = [] if required <= set(trace) else ["incomplete_policy_trace"]
    if trace.get("selected_evidence_ids") != record["selected_event_ids"]:
        errors.append("selected_ids_do_not_match_product")
    expected_spans = [{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "length": len(span["text"]), "truncated": span["truncated"]} for span in record["delivered_spans"]]
    if trace.get("returned_spans") != expected_spans:
        errors.append("returned_spans_do_not_match_product")
    return errors


def _evaluate(calls: list[dict[str, Any]], facts: dict[str, SeedFact], oracle: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str], list[str], list[str]]:
    records: dict[str, dict[str, dict[str, Any]]] = {}
    for call in calls:
        records.setdefault(str(call["case_id"]), {})[str(call["path"])] = call
    errors: list[str] = []
    id_leaks: list[str] = []; span_leaks: list[str] = []; rendered_leaks: list[str] = []
    coverage = complete = neutral_rank_ok = 0
    recall: list[float] = []; ndcg: list[float] = []; b0_recall: list[float] = []; b0_ndcg: list[float] = []
    expected_cases = oracle.get("cases", [])
    if len(expected_cases) != 180:
        errors.append("blind_oracle_denominator_mismatch")
    for expected in expected_cases:
        case_id = str(expected["case_id"]); pair = records.get(case_id, {})
        if set(pair) != {"ordinary", "deep"}:
            errors.append(f"{case_id}:missing_product_call"); continue
        ordinary, deep, fact = pair["ordinary"], pair["deep"], facts[case_id]
        for record in (ordinary, deep):
            trace_errors = _trace_errors(record)
            if trace_errors: errors.extend(f"{case_id}:{record['path']}:{item}" for item in trace_errors)
            else: complete += 1
        selected = set(ordinary["selected_event_ids"]) | set(deep["selected_event_ids"])
        delivered = {str(span["text"]) for span in deep["delivered_spans"]}
        rendered = f"{ordinary['rendered']}\n{deep['rendered']}"
        if expected["polarity"] == "positive":
            if fact.gold_event_id in set(ordinary["policy_trace"]["authorized_candidate_ids"]): coverage += 1
            else: errors.append(f"{case_id}:positive_authorized_candidate_missing")
        else:
            bad_ids = sorted(set(fact.forbidden_event_ids) & selected)
            bad_spans = sorted(set(fact.forbidden_spans) & delivered)
            bad_rendered = sorted(span for span in fact.forbidden_spans if span in rendered)
            id_leaks.extend(bad_ids); span_leaks.extend(bad_spans); rendered_leaks.extend(bad_rendered)
            if bad_ids or bad_spans or bad_rendered: errors.append(f"{case_id}:negative_nontelemetry_leak")
        if expected.get("authorization_neutral"):
            rank = ordinary["selected_event_ids"].index(fact.gold_event_id) + 1 if fact.gold_event_id in ordinary["selected_event_ids"] else None
            b0 = expected["b0"]
            b0_rank = b0["gold_rank"]
            if b0_rank is None or (rank is not None and rank <= int(b0_rank)): neutral_rank_ok += 1
            else: errors.append(f"{case_id}:neutral_gold_rank_regressed")
            recall.append(1.0 if rank is not None and rank <= 24 else 0.0)
            ndcg.append(1.0 / math.log2(rank + 1) if rank is not None and rank <= 24 else 0.0)
            b0_recall.append(float(b0["recall_at_24"])); b0_ndcg.append(float(b0["ndcg_at_24"]))
    metrics = {"product_calls": len(calls), "positive_cases": sum(item["polarity"] == "positive" for item in expected_cases), "negative_cases": sum(item["polarity"] == "negative" for item in expected_cases), "positive_authorized_candidate_coverage": coverage, "negative_nontelemetry_event_id_leaks": len(id_leaks), "negative_nontelemetry_span_leaks": len(span_leaks), "negative_nontelemetry_rendered_leaks": len(rendered_leaks), "complete_policy_traces": complete, "neutral_cases": len(recall), "neutral_gold_rank_not_worse": neutral_rank_ok, "neutral_recall_at_24": sum(recall) / len(recall), "neutral_ndcg_at_24": sum(ndcg) / len(ndcg), "b0_neutral_recall_at_24": sum(b0_recall) / len(b0_recall), "b0_neutral_ndcg_at_24": sum(b0_ndcg) / len(b0_ndcg)}
    gates = {"product_calls": 360, "positive_cases": 90, "negative_cases": 90, "positive_authorized_candidate_coverage": 90, "negative_nontelemetry_event_id_leaks": 0, "negative_nontelemetry_span_leaks": 0, "negative_nontelemetry_rendered_leaks": 0, "complete_policy_traces": 360, "neutral_cases": 60, "neutral_gold_rank_not_worse": 60}
    for name, required in gates.items():
        if metrics[name] != required: errors.append(f"hard_gate:{name}:{metrics[name]}!={required}")
    if metrics["neutral_recall_at_24"] < metrics["b0_neutral_recall_at_24"]: errors.append("hard_gate:neutral_recall_below_b0")
    if metrics["neutral_ndcg_at_24"] < metrics["b0_neutral_ndcg_at_24"]: errors.append("hard_gate:neutral_ndcg_below_b0")
    return metrics, errors, id_leaks, span_leaks, rendered_leaks


def run_blind_180(db_path: str) -> dict[str, Any]:
    """Run 360 product calls before reading evaluator labels."""
    metadata, bundle = load_query_bundle()
    gate_errors = validate_freeze_spec(metadata, bundle)
    kernel = RpgMemoryKernel(db_path=db_path)
    try:
        facts, seed_evidence = seed_blind_suite(kernel, bundle)
        if seed_evidence["real_scene_event_noise_count"] < 1001: gate_errors.append("ceiling_probe_has_fewer_than_1001_real_scene_events")
        if seed_evidence["early_gold_scene_time_sort"] >= seed_evidence["first_noise_scene_time_sort"]: gate_errors.append("ceiling_probe_gold_not_earlier_than_noise")
        calls: list[dict[str, Any]] = []
        for case in bundle["cases"]:
            calls.extend(_capture_call(kernel, case, facts[str(case["id"])]))
        frozen_output_bytes = _canonical_bytes(calls)
        frozen_calls = json.loads(frozen_output_bytes)
        oracle = load_evaluator_oracle()
        metrics, errors, id_leaks, span_leaks, rendered_leaks = _evaluate(frozen_calls, facts, oracle)
        gate_errors.extend(errors)
        return {"schema": "aerp1-blind-180-report", "version": 1, "baseline": metadata["baseline"], "input_freeze": {"spec_sha256": metadata["spec_sha256"], "query_bundle_sha256": metadata["query_bundle_sha256"], "oracle_sha256": EXPECTED_ORACLE_SHA256, "frozen_product_outputs_sha256": hashlib.sha256(frozen_output_bytes).hexdigest(), "phase_order": ["open_spec_and_queries_only", "seed_and_run_360_product_calls", "freeze_product_outputs", "open_independent_oracle", "evaluate"]}, "denominators": {"logical_cases": 180, "product_calls": 360, "positive_cases": 90, "negative_cases": 90, "authorization_neutral_cases": 60}, "seed_evidence": seed_evidence, "metrics": metrics, "calls": frozen_calls, "aggregate": {"verdict": "PASS" if not gate_errors else "FAIL", "gate_errors": gate_errors, "forbidden_event_id_leaks": id_leaks, "forbidden_span_leaks": span_leaks, "forbidden_rendered_leaks": rendered_leaks, "policy_trace_trust_boundary": "Denied IDs may appear only in policy telemetry; selected evidence, delivered spans, and rendered product text are independently checked."}}
    finally:
        kernel.close()
