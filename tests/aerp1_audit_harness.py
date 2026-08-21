"""Shared, offline AERP-1 fixture and artifact execution harness."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

from mempalace_rpg import RpgMemoryKernel, SceneEventInput

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("fixtures") / "aerp1_one_seed_manifest.json"
HERO, CAMPAIGN = "hero", "C1"
FROZEN_MANIFEST_SHA256 = "feb887669c9f8a843ce183815a63e46da22c89687a64ef3d994a1a3bc4cf4e22"


@dataclass(frozen=True)
class AuditCase:
    logical_id: str
    category: str
    polarity: str
    query: str
    gold_ids: tuple[str, ...] = ()
    gold_spans: tuple[str, ...] = ()
    forbidden_ids: tuple[str, ...] = ()
    forbidden_spans: tuple[str, ...] = ()
    location_id: str | None = None
    expected_reason: str | None = None


def load_manifest() -> tuple[dict[str, Any], bytes, str]:
    raw = MANIFEST_PATH.read_bytes()
    return json.loads(raw), raw, hashlib.sha256(raw).hexdigest()


def _event(summary: str, span: str, *, visibility: str = "public_world", truth_status: str = "canonical", importance: float = 1.0, **security: Any) -> SceneEventInput:
    return SceneEventInput(
        event_type="evidence", summary=summary, branch_id="main",
        branch_status=truth_status if truth_status in {"retconned", "abandoned"} else "active",
        source_span=span, visibility=visibility, truth_status=truth_status,
        importance=importance, **security,
    )


def _commit(kernel: RpgMemoryKernel, scene_id: str, event: SceneEventInput, *, campaign_id: str = CAMPAIGN, location_id: str | None = None) -> str:
    return kernel.commit_scene(
        campaign_id=campaign_id, scene_id=scene_id, in_world_time=f"time-{scene_id}",
        location_id=location_id or f"loc-{scene_id}", transcript=f"OPEN [{event.source_span}] CLOSE",
        participants=[HERO], witnesses=[HERO], events=[event],
    )


def _terms(value: str) -> set[str]:
    import re
    return set(re.findall(r"[a-z0-9]+", value.lower()))


def _id(kernel: RpgMemoryKernel, scene_id: str, summary: str) -> str:
    row = kernel._conn().execute("SELECT event_id FROM scene_event WHERE scene_id=? AND summary=?", (scene_id, summary)).fetchone()
    assert row
    return str(row["event_id"])


def seed_semantic_matrix(kernel: RpgMemoryKernel) -> list[AuditCase]:
    """One frozen 24-case seed; evaluator labels live only in the test harness."""
    manifest, _raw, _digest = load_manifest()
    cases: list[AuditCase] = []
    definitions = [
        ("exact-public-1", "exact public phrase 1", _event("exact public phrase 1", "EXACT PUBLIC SPAN 1")),
        ("exact-public-2", "exact public phrase 2", _event("exact public phrase 2", "EXACT PUBLIC SPAN 2")),
        ("paraphrase-1", "harbor clearance request", _event("orchid ledger sealed", "PARAPHRASE GOLD SPAN 1")),
        ("paraphrase-2", "midnight rescue appeal", _event("granite compass archived", "PARAPHRASE GOLD SPAN 2")),
        ("promise-1", "promise heirloom 1", _event("promise heirloom 1", "EARLY PROMISE SPAN 1")),
        ("promise-2", "promise heirloom 2", _event("promise heirloom 2", "EARLY PROMISE SPAN 2")),
        ("private-1", "rightful private owner", _event("rightful private owner", "RIGHTFUL PRIVATE SPAN", visibility="character_private", access_owner_id=HERO)),
        ("witness-1", "rightful witnessed companion", _event("rightful witnessed companion", "RIGHTFUL WITNESS SPAN", visibility="witnessed_only", witness_set=[HERO])),
        ("belief-owner-1", "hero belief 1", _event("hero belief 1", "HERO BELIEF SPAN 1", truth_status="belief", actor_id=HERO, belief_owner_id=HERO)),
        ("belief-owner-2", "hero belief 2", _event("hero belief 2", "HERO BELIEF SPAN 2", truth_status="belief", actor_id=HERO, belief_owner_id=HERO)),
        ("gm-only-1", "gm secret plan", _event("gm secret plan", "GM ONLY SPAN", visibility="gm_only")),
        ("private-other-1", "other actor private", _event("other actor private", "WRONG PRIVATE SPAN", visibility="character_private", access_owner_id="other")),
        ("retconned-1", "retconned evidence", _event("retconned evidence", "RETCONNED SPAN", truth_status="retconned")),
        ("abandoned-1", "abandoned evidence", _event("abandoned evidence", "ABANDONED SPAN", truth_status="abandoned")),
        ("faction-outsider-1", "faction outsider secret", _event("faction outsider secret", "FACTION OUTSIDER SPAN", visibility="faction_private", access_scope_id="shared")),
        ("quest-outsider-1", "quest outsider secret", _event("quest outsider secret", "QUEST OUTSIDER SPAN", visibility="quest_participants", access_scope_id="missing-quest")),
        ("belief-other-1", "other belief cannot canonize 1", _event("other belief cannot canonize 1", "OTHER BELIEF SPAN 1", truth_status="belief", actor_id="other", belief_owner_id="other")),
        ("belief-other-2", "other belief cannot canonize 2", _event("other belief cannot canonize 2", "OTHER BELIEF SPAN 2", truth_status="belief", actor_id="other", belief_owner_id="other")),
    ]
    negative_ids = {"gm-only-1", "private-other-1", "retconned-1", "abandoned-1", "faction-outsider-1", "quest-outsider-1", "belief-other-1", "belief-other-2"}
    for logical_id, query, event in definitions:
        scene = f"seed-{logical_id}"
        _commit(kernel, scene, event, location_id=f"loc-{scene}")
        eid = _id(kernel, scene, event.summary)
        is_negative = logical_id in negative_ids
        cases.append(AuditCase(logical_id, next(c["category"] for c in manifest["cases"] if c["id"] == logical_id), "negative" if is_negative else "positive", query, () if is_negative else (eid,), () if is_negative else (str(event.source_span),), (eid,) if is_negative else (), (str(event.source_span),) if is_negative else (), f"loc-{scene}"))
    for n in range(2):
        scene, summary, span, loc = f"seed-temporal-{n}", f"temporal marker {n}", f"TEMPORAL HERE SPAN {n}", f"loc-here-{n}"
        _commit(kernel, scene, _event(summary, span), location_id=loc)
        _commit(kernel, f"seed-temporal-away-{n}", _event(summary, f"TEMPORAL AWAY SPAN {n}"), location_id=f"loc-away-{n}")
        cases.append(AuditCase(f"temporal-{n + 1}", "temporal_location", "positive", summary, (_id(kernel, scene, summary),), (span,), location_id=loc))
    for n in range(2):
        scene, summary, span = f"seed-cross-{n}", f"other campaign public {n}", f"CROSS CAMPAIGN SPAN {n}"
        _commit(kernel, scene, _event(summary, span), campaign_id="C2")
        cases.append(AuditCase(f"cross-campaign-{n + 1}", "cross_campaign", "negative", summary, forbidden_ids=(_id(kernel, scene, summary),), forbidden_spans=(span,)))
    for n in range(2):
        scene, allowed, denied = f"seed-mixed-{n}", f"mixed allowed {n}", f"mixed forbidden {n}"
        allowed_event, denied_event = _event(allowed, f"MIXED ALLOWED SPAN {n}"), _event(denied, f"MIXED FORBIDDEN SPAN {n}", visibility="character_private", access_owner_id="other")
        kernel.commit_scene(campaign_id=CAMPAIGN, scene_id=scene, in_world_time=f"time-{scene}", location_id=f"loc-{scene}", transcript=f"[{allowed_event.source_span}] [{denied_event.source_span}]", participants=[HERO], witnesses=[HERO], events=[allowed_event, denied_event])
        cases.append(AuditCase(f"mixed-span-{n + 1}", "mixed_visibility_forbidden_span", "negative", allowed, (_id(kernel, scene, allowed),), (str(allowed_event.source_span),), (_id(kernel, scene, denied),), (str(denied_event.source_span),)))
    # Low-score authorized noise proves ordinary recall is a ranked budgeted
    # product rather than a replay of every authorized candidate.
    for n in range(25):
        _commit(kernel, f"seed-noise-{n}", _event(f"background evidence {n}", f"BACKGROUND SPAN {n}", importance=0.0))
    kernel.upsert_actor_membership(campaign_id=CAMPAIGN, actor_id=HERO, scope_id="shared", scope_kind="quest")
    reasons = {
        "rightful_private_witness": None,
        "gm_wrong_actor_private": None,
        "retconned_abandoned": "noncanonical_retconned_or_abandoned",
        "faction_quest_outsider": "membership_required",
        "belief_as_canon": "belief_requires_rightful_actor",
    }
    cases = [replace(case, expected_reason=reasons.get(case.category)) for case in cases]
    frozen = {item["id"]: (item["category"], item["polarity"]) for item in manifest["cases"]}
    assert {case.logical_id: (case.category, case.polarity) for case in cases} == frozen
    return cases


def _call_record(case: AuditCase, path: str, product: Any) -> dict[str, Any]:
    if path == "ordinary":
        ids, spans, trace = [item["source_event_id"] for item in product.evidence], [], product.policy_trace
    else:
        ids, trace = [item["source_event_id"] for item in product["evidence"]], product["policy_trace"]
        spans = [{"source_event_id": s["source_event_id"], "source_scene_id": s["source_scene_id"], "text": s["text"], "truncated": bool(s.get("truncated"))} for scene in product["scene_evidence"] for s in scene["authorized_spans"]]
    return {"logical_case_id": case.logical_id, "path": path, "campaign_id": CAMPAIGN, "actor_id": HERO, "query": case.query, "policy_trace": trace, "selected_event_ids": ids, "delivered_spans": spans}


def _trace_errors(record: dict[str, Any], *, expected_spans: list[dict[str, Any]]) -> list[str]:
    trace = record["policy_trace"]
    required = {"policy", "campaign_id", "actor_id", "actor_type", "candidate_generation", "candidates", "deduplication", "authorized_candidate_ids", "selected_evidence_ids", "returned_spans"}
    errors = [] if required <= set(trace) else ["incomplete_policy_trace"]
    if trace.get("selected_evidence_ids") != record["selected_event_ids"]:
        errors.append("selected_ids_do_not_match_product")
    if len(record["selected_event_ids"]) != len(set(record["selected_event_ids"])):
        errors.append("duplicate_selected_event_id")
    summarized = [{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "length": len(span["text"]), "truncated": span["truncated"]} for span in expected_spans]
    if trace.get("returned_spans") != summarized:
        errors.append("returned_spans_do_not_match_product")
    return errors


def _evaluate_invariants(kernel: RpgMemoryKernel, case: AuditCase, ordinary: Any, deep: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, bool]:
    """Check frozen semantic labels against independently seeded facts."""
    ordinary_ids = [item["source_event_id"] for item in ordinary.evidence]
    deep_ids = [item["source_event_id"] for item in deep["evidence"]]
    candidate_ids = {row["source_event_id"] for row in records[0]["policy_trace"]["candidates"]}
    delivered = {span["text"] for span in records[1]["delivered_spans"]}
    rows = kernel._conn().execute("SELECT COUNT(*) FROM scene_event se JOIN scene_record sr ON sr.scene_id=se.scene_id WHERE sr.campaign_id=?", (CAMPAIGN,)).fetchone()[0]
    target_ids = case.gold_ids or case.forbidden_ids
    target = target_ids[0] if target_ids else ""
    candidate = next((row for row in records[0]["policy_trace"]["candidates"] if row["source_event_id"] == target), None)
    results = {
        "candidate_count_matches_campaign_seed": records[0]["policy_trace"]["candidate_generation"]["candidate_count"] == rows,
        "ordinary_deep_selected_parity": ordinary_ids == deep_ids,
        "ordinary_is_ranked_budget_not_all_authorized": len(ordinary_ids) < len(records[0]["policy_trace"]["authorized_candidate_ids"]),
        "forbidden_ids_absent": not bool(set(case.forbidden_ids) & (set(ordinary_ids) | set(deep_ids))),
        "forbidden_spans_absent": not bool(set(case.forbidden_spans) & delivered),
    }
    if case.category == "cross_campaign":
        results["campaign_constrained"] = target not in candidate_ids
    elif case.polarity == "positive":
        results["authorized_candidate"] = candidate is not None and candidate["decision"] == "allow"
        results["gold_selected"] = set(case.gold_ids) <= set(ordinary_ids)
        if case.category in {"exact_public", "temporal_location", "long_range_promise"}:
            results["gold_ranked_first"] = bool(ordinary_ids) and ordinary_ids[0] == target
        if case.category == "paraphrase":
            results["no_lexical_overlap"] = _terms(case.query).isdisjoint(_terms(ordinary.evidence[0]["text"]))
        if case.category == "rightful_private_witness":
            expected = "private_owner_match" if case.logical_id == "private-1" else "witness_match"
            results["rightful_visibility_reason"] = candidate is not None and candidate["reason"] == expected
        if case.category == "actor_belief":
            results["belief_not_world_truth"] = case.query in "\n".join(body for title, body in ordinary.sections if title == "ActorBelief") and case.query not in "\n".join(body for title, body in ordinary.sections if title == "WorldTruth allowed to actor")
    else:
        if case.category == "mixed_visibility_forbidden_span":
            forbidden = next((row for row in records[0]["policy_trace"]["candidates"] if row["source_event_id"] == case.forbidden_ids[0]), None)
            results["mixed_forbidden_reason"] = forbidden is not None and forbidden["decision"] == "deny" and forbidden["reason"] == "private_owner_required"
        else:
            reason = case.expected_reason
            if case.category == "gm_wrong_actor_private":
                reason = "gm_only_or_unsupported_visibility" if case.logical_id == "gm-only-1" else "private_owner_required"
            results["expected_denial"] = candidate is not None and candidate["decision"] == "deny" and candidate["reason"] == reason
    return results


def run_audit(db_path: str) -> dict[str, Any]:
    manifest, raw, digest = load_manifest()
    kernel = RpgMemoryKernel(db_path=db_path)
    try:
        kernel.upsert_character_profile(character_id=HERO, display_name="Hero", tier="core", short_persona="audit actor", memory_wing="wing-audit")
        cases = seed_semantic_matrix(kernel)
        counts = Counter(case.category for case in cases)
        gate_errors: list[str] = []
        if digest != FROZEN_MANIFEST_SHA256:
            gate_errors.append("frozen_manifest_sha256_mismatch")
        if len(cases) != 24 or Counter(c.polarity for c in cases) != {"positive": 12, "negative": 12} or not all(value == 2 for value in counts.values()):
            gate_errors.append("frozen_manifest_shape_mismatch")
        calls, id_leaks, span_leaks = [], [], []
        positive_candidate_ceiling = negative_path_calls = complete_policy_traces = 0
        for case in cases:
            ordinary = kernel.build_memory_pack(campaign_id=CAMPAIGN, actor_id=HERO, actor_type="npc", query=case.query, location_id=case.location_id, max_chars=12000)
            deep = kernel.deep_recall(campaign_id=CAMPAIGN, actor_id=HERO, actor_type="npc", query=case.query, location_id=case.location_id, max_chars=12000, per_scene_chars=12000)
            records = [_call_record(case, "ordinary", ordinary), _call_record(case, "deep", deep)]
            calls.extend(records)
            invariant_results = _evaluate_invariants(kernel, case, ordinary, deep, records)
            for record in records:
                record["invariant_results"] = invariant_results
            failed_invariants = [name for name, passed in invariant_results.items() if not passed]
            gate_errors.extend(f"{case.logical_id}:invariant:{name}" for name in failed_invariants)
            ordinary_spans: list[dict[str, Any]] = []
            deep_spans = records[1]["delivered_spans"]
            for record, expected_spans in ((records[0], ordinary_spans), (records[1], deep_spans)):
                errors = _trace_errors(record, expected_spans=expected_spans)
                if errors:
                    gate_errors.extend(f"{case.logical_id}:{record['path']}:{error}" for error in errors)
                else:
                    complete_policy_traces += 1
            if records[0]["selected_event_ids"] != records[1]["selected_event_ids"]:
                gate_errors.append(f"{case.logical_id}:ordinary_deep_selected_ids_differ")
            selected = set(records[0]["selected_event_ids"]) | set(records[1]["selected_event_ids"]) | {s["source_event_id"] for s in records[1]["delivered_spans"]}
            delivered = {s["text"] for s in records[1]["delivered_spans"]}
            if case.polarity == "positive":
                if set(case.gold_ids) <= set(records[0]["policy_trace"].get("authorized_candidate_ids", [])):
                    positive_candidate_ceiling += 1
                else:
                    gate_errors.append(f"{case.logical_id}:positive_candidate_ceiling")
                if not (set(case.gold_ids) <= selected and (case.category not in {"exact_public", "temporal_location", "long_range_promise"} or set(case.gold_spans) <= delivered)):
                    gate_errors.append(f"{case.logical_id}:positive_product_delivery")
            else:
                negative_path_calls += 2
                id_leaks.extend(sorted(set(case.forbidden_ids) & selected))
                span_leaks.extend(sorted(set(case.forbidden_spans) & delivered))
                rendered = ordinary.render() + deep["rendered"] + "\n".join(span["text"] for span in deep_spans)
                span_leaks.extend(span for span in case.forbidden_spans if span in rendered)
        metrics = {"product_calls": len(calls), "positive_candidate_ceiling": positive_candidate_ceiling, "negative_path_calls": negative_path_calls, "complete_policy_traces": complete_policy_traces}
        if metrics != {"product_calls": 48, "positive_candidate_ceiling": 12, "negative_path_calls": 24, "complete_policy_traces": 48}:
            gate_errors.append("aggregate_denominator_mismatch")
        gate_pass = not gate_errors and not id_leaks and not span_leaks
        return {"schema": "aerp1-offline-audit-report", "version": 1, "manifest": {"schema": manifest["schema"], "version": manifest["version"], "sha256": digest}, "runtime": {"python": sys.version, "platform": platform.platform(), **_git_state()}, "denominators": {"logical_cases": 24, "product_calls": 48, "positive_cases": 12, "negative_cases": 12}, "metrics": metrics, "calls": calls, "aggregate": {"verdict": "PASS" if gate_pass else "FAIL", "gate_errors": gate_errors, "forbidden_event_id_leaks": id_leaks, "forbidden_span_leaks": span_leaks, "policy_trace_trust_boundary": "Denied candidate IDs may appear only in policy telemetry; they never appear in selected evidence, rendered product text, or delivered spans."}}
    finally:
        kernel.close()


def _git_output(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout


def _git_state() -> dict[str, Any]:
    """Bind an artifact to one exact Git object and its parent-to-commit patch."""
    try:
        head = _git_output("rev-parse", "HEAD").decode("ascii").strip()
        tree = _git_output("rev-parse", "HEAD^{tree}").decode("ascii").strip()
        revision = _git_output("rev-list", "--parents", "-n", "1", "HEAD").decode("ascii").split()
        commit_patch = _git_output(
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-color",
            "-p",
            "HEAD",
            "--",
        )
        changed_paths = [
            value
            for value in _git_output(
                "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD", "--"
            ).decode("utf-8").splitlines()
            if value
        ]
        worktree_status = _git_output(
            "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        return {
            "git_head": head,
            "git_tree": tree,
            "git_parents": revision[1:],
            "git_dirty": bool(worktree_status),
            "commit_diff": {
                "algorithm": "sha256",
                "canonicalization": "raw bytes from git diff-tree --root --no-commit-id --binary --full-index --no-ext-diff --no-color -p HEAD --",
                "sha256": hashlib.sha256(commit_patch).hexdigest(),
                "byte_count": len(commit_patch),
                "changed_paths": changed_paths,
            },
            "worktree_status": {
                "algorithm": "sha256",
                "canonicalization": "raw NUL-delimited bytes from git status --porcelain=v1 -z --untracked-files=all",
                "sha256": hashlib.sha256(worktree_status).hexdigest(),
                "byte_count": len(worktree_status),
            },
        }
    except (OSError, subprocess.CalledProcessError):
        return {
            "git_head": None,
            "git_tree": None,
            "git_parents": None,
            "git_dirty": None,
            "commit_diff": None,
            "worktree_status": None,
        }
