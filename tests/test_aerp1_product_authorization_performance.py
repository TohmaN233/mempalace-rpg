"""Regression coverage for AERP-1's single-decision product path.

These tests intentionally exercise the SQL ACL path against the exhaustive
Python authorizer.  The fast path is an optimization only: parity is the
security contract.
"""
from __future__ import annotations

import hashlib
import json

from mempalace_rpg import RpgMemoryKernel, SceneEventInput
from mempalace_rpg.authorization import VALID_TRUTH_STATUSES, VALID_VISIBILITIES


def _event(
    label: str,
    *,
    truth_status: str = "canonical",
    visibility: str = "public_world",
    **extra: object,
) -> SceneEventInput:
    access: dict[str, object] = {}
    if visibility == "character_private":
        access["access_owner_id"] = "hero"
    elif visibility == "witnessed_only":
        access["witness_set"] = ["hero"]
    elif visibility == "faction_private":
        access["access_scope_id"] = "faction_a"
    elif visibility == "quest_participants":
        access["access_scope_id"] = "quest_a"
    elif visibility == "party_only":
        access["access_scope_id"] = "party_a"
    if truth_status == "belief":
        access.update({"actor_id": "hero", "belief_owner_id": "hero"})
    access.update(extra)
    return SceneEventInput(
        event_type="evidence",
        summary=label,
        branch_id="main",
        branch_status=truth_status if truth_status in {"retconned", "abandoned"} else "active",
        truth_status=truth_status,
        visibility=visibility,
        source_span="SPAN " + label,
        **access,
    )


def _ids(decision: object) -> set[str]:
    return set(decision.trace["authorized_candidate_ids"])


def _parity(kernel: RpgMemoryKernel, *, actor_id: str, actor_type: str) -> None:
    detailed = kernel.authorized_evidence(
        campaign_id="C", actor_id=actor_id, actor_type=actor_type, query="q", budget=1000
    )
    fast = kernel.authorized_evidence(
        campaign_id="C",
        actor_id=actor_id,
        actor_type=actor_type,
        query="q",
        budget=1000,
        _compact_product_trace=True,
    )
    assert _ids(fast) == _ids(detailed)
    partition_reason = {
        event_id: partition["reason"]
        for partition in fast.trace["denied_partitions"]
        for event_id in partition["event_ids"]
    }
    detailed_denials = {
        row["source_event_id"]: row["reason"]
        for row in detailed.trace["candidates"]
        if row["decision"] == "deny"
    }
    assert partition_reason == detailed_denials


def test_sql_acl_fast_path_has_parity_for_visibility_truth_branch_malformed_and_membership(tmp_path):
    kernel = RpgMemoryKernel(str(tmp_path / "parity.sqlite3"))
    events = [
        _event(f"{truth}-{visibility}", truth_status=truth, visibility=visibility)
        for truth in sorted(VALID_TRUTH_STATUSES)
        for visibility in sorted(VALID_VISIBILITIES)
    ]
    transcript = "\n".join(event.source_span for event in events)
    kernel.commit_scene(campaign_id="C", scene_id="matrix", in_world_time="now", transcript=transcript, events=events)
    for scope_id, scope_kind in (("faction_a", "faction"), ("quest_a", "quest"), ("party_a", "party")):
        kernel.upsert_actor_membership(campaign_id="C", actor_id="hero", scope_id=scope_id, scope_kind=scope_kind)

    # Deliberately corrupt rows after write validation.  The optimized SQL path
    # must deny the exact same records as the strict JSON/Python path.
    conn = kernel._conn()
    rows = conn.execute("SELECT event_id FROM scene_event ORDER BY event_id LIMIT 3").fetchall()
    conn.execute("UPDATE scene_event SET witness_set_json='not-json' WHERE event_id=?", (rows[0]["event_id"],))
    conn.execute("UPDATE scene_event SET related_quests_json='[\" \" ]' WHERE event_id=?", (rows[1]["event_id"],))
    conn.execute("UPDATE scene_event SET branch_id=NULL WHERE event_id=?", (rows[2]["event_id"],))
    conn.commit()

    _parity(kernel, actor_id="hero", actor_type="npc")
    _parity(kernel, actor_id="outsider", actor_type="npc")
    _parity(kernel, actor_id="gm", actor_type="gm")


def test_product_paths_authorize_once_and_compact_trace_is_bounded(tmp_path, monkeypatch):
    kernel = RpgMemoryKernel(str(tmp_path / "product.sqlite3"))
    events = [_event(f"public-{index:03d}") for index in range(120)]
    kernel.commit_scene(
        campaign_id="C",
        scene_id="bulk",
        in_world_time="now",
        transcript="\n".join(event.source_span for event in events),
        events=events,
    )
    calls: list[dict[str, object]] = []
    original = kernel.authorized_evidence

    def observed(**kwargs: object):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(kernel, "authorized_evidence", observed)
    pack = kernel.build_memory_pack(campaign_id="C", actor_id="hero", actor_type="npc", query="public")
    assert len(calls) == 1
    trace = pack.policy_trace
    selected = trace["selected_evidence_ids"]
    candidate_ids = {row["source_event_id"] for row in trace["candidates"]}
    assert set(selected) <= candidate_ids
    assert len(trace["authorized_candidate_ids"]) == 120
    assert len(trace["candidates"]) == len(selected)  # no redundant unselected allow evaluations
    assert trace["trace_compaction"]["denied_detailed_count"] == 0
    assert trace["trace_compaction"]["omitted_allowed_evaluation_count"] == 120 - len(selected)
    assert len(json.dumps(trace, sort_keys=True)) < 80_000

    calls.clear()
    deep = kernel.deep_recall(campaign_id="C", actor_id="hero", actor_type="npc", query="public")
    assert len(calls) == 1
    assert deep["policy_trace"]["selected_evidence_ids"] == [item["source_event_id"] for item in deep["evidence"]]


def test_compact_trace_partitions_every_denial_with_reason_and_digest(tmp_path):
    kernel = RpgMemoryKernel(str(tmp_path / "partitions.sqlite3"))
    events = [_event(f"public-{index:03d}") for index in range(2)]
    events.extend(_event(f"gm-{index:03d}", visibility="gm_only") for index in range(300))
    kernel.commit_scene(
        campaign_id="C",
        scene_id="partitioned",
        in_world_time="now",
        transcript="\n".join(event.source_span for event in events),
        events=events,
    )

    trace = kernel.build_memory_pack(
        campaign_id="C", actor_id="hero", actor_type="npc", query="public"
    ).policy_trace
    allowed = trace["authorized_candidate_ids"]
    partitions = trace["denied_partitions"]
    denied = [event_id for partition in partitions for event_id in partition["event_ids"]]

    assert len(allowed) == 2
    assert len(denied) == len(set(denied)) == 300
    assert set(allowed).isdisjoint(denied)
    assert len(allowed) + len(denied) == trace["candidate_generation"]["candidate_count"]
    assert {partition["reason"] for partition in partitions} == {
        "gm_only_or_unsupported_visibility"
    }
    for partition in partitions:
        assert partition["count"] == len(partition["event_ids"])
        assert partition["event_ids_sha256"] == hashlib.sha256(
            "\n".join(partition["event_ids"]).encode("utf-8")
        ).hexdigest()
    compaction = trace["trace_compaction"]
    assert compaction["denied_detailed_count"] == 256
    assert compaction["denied_omitted_evaluation_count"] == 44
    assert compaction["denied_partition_count"] == 1
    detailed_denials = [row for row in trace["candidates"] if row["decision"] == "deny"]
    assert len(detailed_denials) == 256
    assert {row["source_event_id"] for row in detailed_denials} <= set(denied)


def test_product_read_caches_invalidate_after_external_sqlite_commit(tmp_path):
    db_path = str(tmp_path / "external-write.sqlite3")
    reader = RpgMemoryKernel(db_path)
    old = _event("old-public")
    reader.commit_scene(
        campaign_id="C", scene_id="old", in_world_time="old",
        transcript=old.source_span, events=[old],
    )
    first = reader.build_memory_pack(
        campaign_id="C", actor_id="hero", actor_type="npc", query="old-public"
    )
    assert len(first.policy_trace["authorized_candidate_ids"]) == 1

    with RpgMemoryKernel(db_path) as writer:
        new = _event("new-public")
        writer.commit_scene(
            campaign_id="C", scene_id="new", in_world_time="new",
            transcript=new.source_span, events=[new],
        )

    second = reader.build_memory_pack(
        campaign_id="C", actor_id="hero", actor_type="npc", query="new-public"
    )
    assert len(second.policy_trace["authorized_candidate_ids"]) == 2
    assert any(item["text"] == "new-public" for item in second.evidence)
