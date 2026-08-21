"""AERP-1 branch and frozen offline audit contract tests."""
from __future__ import annotations

import pytest
import sqlite3
import subprocess
import sys

from mempalace_rpg import RpgMemoryKernel, SceneEventInput
from mempalace_rpg.adapter import RecordingEpisodeAdapter
from tests.aerp1_audit_harness import FROZEN_MANIFEST_SHA256, load_manifest, run_audit


def _event(*, branch_id: str = "main", branch_status: str = "active", truth_status: str = "canonical", visibility: str = "public_world", span: str = "SAFE SPAN", **security: object) -> SceneEventInput:
    return SceneEventInput(event_type="evidence", summary="audit event", branch_id=branch_id, branch_status=branch_status, truth_status=truth_status, visibility=visibility, source_span=span, **security)


def test_aerp1_frozen_harness_executes_exact_24_cases_48_calls(tmp_path):
    manifest, raw, digest = load_manifest()
    assert manifest["schema"] == "aerp1-one-seed-manifest" and manifest["version"] == 1
    assert len(manifest["cases"]) == 24 and len(raw) > 0 and digest == FROZEN_MANIFEST_SHA256
    report = run_audit(str(tmp_path / "audit.sqlite3"))
    assert report["manifest"]["sha256"] == digest
    assert report["denominators"] == {"logical_cases": 24, "product_calls": 48, "positive_cases": 12, "negative_cases": 12}
    assert len(report["calls"]) == 48 and report["aggregate"]["verdict"] == "PASS"
    assert report["metrics"] == {"product_calls": 48, "positive_candidate_ceiling": 12, "negative_path_calls": 24, "complete_policy_traces": 48}
    assert report["aggregate"]["gate_errors"] == []
    assert report["aggregate"]["forbidden_event_id_leaks"] == []
    assert report["aggregate"]["forbidden_span_leaks"] == []
    runtime = report["runtime"]
    assert len(runtime["git_head"]) == 40 and len(runtime["git_tree"]) == 40
    assert runtime["commit_diff"]["algorithm"] == "sha256"
    assert len(runtime["commit_diff"]["sha256"]) == 64
    assert runtime["commit_diff"]["byte_count"] > 0
    assert runtime["worktree_status"]["algorithm"] == "sha256"


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (None, "branch_id"),
        (_event(branch_id="", branch_status="active"), "branch_id"),
        (_event(branch_status="unknown"), "branch_status"),
        (_event(branch_status="retconned"), "branch_status must match"),
        (_event(truth_status="retconned", branch_status="active"), "branch_status must match"),
    ],
)
def test_aerp1_branch_write_validation_happens_before_adapter_or_sqlite(tmp_path, event, message):
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "branch.sqlite3"), episode_adapter=adapter)
    if event is None:
        with pytest.raises(TypeError, match=message):
            SceneEventInput(event_type="evidence", summary="missing", truth_status="canonical", visibility="public_world")
        return
    with pytest.raises(ValueError, match=message):
        kernel.commit_scene(campaign_id="C1", in_world_time="now", transcript="SAFE SPAN", events=[event])
    assert adapter.drawers == []
    assert kernel.status()["counts"]["scene_record"] == 0
    assert kernel.status()["counts"]["scene_event"] == 0


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (_event(span=" "), "source_span"),
        (_event(visibility="character_private"), "character_private"),
        (_event(visibility="character_private", access_owner_id="hero", access_scope_id="scope"), "character_private"),
        (_event(visibility="faction_private"), "scoped private"),
        (_event(visibility="public_world", access_owner_id="hero"), "visibility forbids"),
        (_event(truth_status="belief", actor_id="other", belief_owner_id="hero"), "belief requires"),
        (_event(belief_owner_id="hero"), "non-belief"),
    ],
)
def test_aerp1_write_matrix_rejects_before_entity_adapter_or_sqlite(tmp_path, event, message):
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "matrix.sqlite3"), episode_adapter=adapter)
    with pytest.raises(ValueError, match=message):
        kernel.commit_scene(campaign_id="C1", in_world_time="now", transcript="SAFE SPAN", events=[event])
    assert adapter.drawers == []
    assert kernel.status()["counts"]["scene_record"] == 0
    assert kernel.status()["counts"]["entity_registry"] == 0


@pytest.mark.parametrize(
    ("top_level", "event_field", "bad_value"),
    [
        ("witnesses", None, "hero"),
        ("participants", None, {"hero": True}),
        ("active_quest_ids", None, ["quest", 2]),
        (None, "witness_set", "hero"),
        (None, "witness_set", None),
        (None, "related_entities", {"hero": True}),
        (None, "related_quests", ["quest", " "]),
        (None, "related_locations", ("loc",)),
    ],
)
def test_aerp1_list_shapes_fail_before_adapter_or_any_rows(tmp_path, top_level, event_field, bad_value):
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "lists.sqlite3"), episode_adapter=adapter)
    kwargs = {top_level: bad_value} if top_level else {}
    event = _event(**({event_field: bad_value} if event_field else {}))
    with pytest.raises(ValueError, match="list of non-empty strings"):
        kernel.commit_scene(campaign_id="C1", in_world_time="now", transcript="SAFE SPAN", events=[event], **kwargs)
    assert adapter.drawers == []
    assert kernel.status()["counts"]["entity_registry"] == 0
    assert kernel.status()["counts"]["scene_record"] == kernel.status()["counts"]["scene_event"] == 0


@pytest.mark.parametrize("truth_status,branch_status", [("retconned", "retconned"), ("abandoned", "abandoned")])
def test_aerp1_retired_branches_persist_but_fail_closed(tmp_path, truth_status, branch_status):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "retired.sqlite3"))
    kernel.commit_scene(campaign_id="C1", scene_id="retired", in_world_time="now", transcript="RETIRED SPAN", events=[_event(truth_status=truth_status, branch_status=branch_status, span="RETIRED SPAN")])
    decision = kernel.authorized_evidence(campaign_id="C1", actor_id="hero", actor_type="npc", query="retired", budget=10)
    candidate = decision.trace["candidates"][0]
    assert decision.events == [] and candidate["reason"] == "noncanonical_retconned_or_abandoned"
    assert candidate["branch_status"] == branch_status and candidate["evaluation"]["branch"]["consistent"] is True


def test_aerp1_legacy_null_branch_row_is_denied_and_traced(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "legacy.sqlite3"))
    kernel.commit_scene(campaign_id="C1", scene_id="legacy", in_world_time="now", transcript="LEGACY SPAN", events=[_event(span="LEGACY SPAN")])
    kernel._conn().execute("UPDATE scene_event SET branch_id=NULL, branch_status=NULL")
    kernel._conn().commit()
    decision = kernel.authorized_evidence(campaign_id="C1", actor_id="hero", actor_type="npc", query="legacy", budget=10)
    assert decision.events == []
    candidate = decision.trace["candidates"][0]
    assert candidate["reason"] == "invalid_or_missing_branch"
    assert candidate["evaluation"]["branch"]["valid"] is False


def test_aerp1_tampered_security_identities_fail_closed_before_witness_match(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "tampered.sqlite3"))
    kernel.commit_scene(
        campaign_id="C1",
        in_world_time="now",
        transcript="SECRET",
        events=[_event(visibility="witnessed_only", witness_set=["h"], span="SECRET")],
    )
    kernel._conn().execute("UPDATE scene_event SET witness_set_json=?, actor_id=?", ('[" "]', " "))
    kernel._conn().commit()
    with pytest.raises(ValueError, match="campaign_id, actor_id, actor_type"):
        kernel.authorized_evidence(campaign_id="C1", actor_id=" ", actor_type="npc", query="secret", budget=10)
    denied = kernel.authorized_evidence(campaign_id="C1", actor_id="h", actor_type="npc", query="secret", budget=10)
    assert denied.events == []
    assert denied.trace["candidates"][0]["reason"] == "invalid_witness_set_json"
    assert denied.trace["candidates"][0]["reason"] != "witness_match"


def test_aerp1_kernel_allows_direct_none_as_optional_list_omission(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "none-omission.sqlite3"))
    kernel.commit_scene(
        campaign_id="C1",
        in_world_time="now",
        transcript="SAFE SPAN",
        active_quest_ids=None,
        participants=None,
        witnesses=None,
        events=[_event()],
    )
    assert kernel.status()["counts"]["scene_record"] == 1


def test_aerp1_authorized_evidence_direct_spans_are_budgeted_unique_and_traced(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "direct.sqlite3"))
    first = _event(span="UNIQUE SPAN")
    second = SceneEventInput(event_type="evidence", summary="same-span projection", branch_id="main", branch_status="active", truth_status="canonical", visibility="public_world", source_span="UNIQUE SPAN")
    kernel.commit_scene(campaign_id="C1", scene_id="one", in_world_time="now", transcript="UNIQUE SPAN", events=[first, second])
    decision = kernel.authorized_evidence(campaign_id="C1", actor_id="hero", actor_type="npc", query="unique", budget=10)
    assert len(decision.events) == 2 and len(decision.spans) == 1
    span = decision.spans[0]
    assert span["text"] == "UNIQUE SPAN" and span["truncated"] is False
    assert decision.trace["returned_spans"] == [{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "length": len(span["text"]), "truncated": False}]


def test_aerp1_overlap_policy_includes_branch_before_external_call(tmp_path):
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "overlap.sqlite3"), episode_adapter=adapter)
    events = [_event(branch_id="main", span="PRIVATE"), _event(branch_id="alternate", span="PRIVATE SPAN")]
    with pytest.raises(ValueError, match="overlapping source spans"):
        kernel.commit_scene(campaign_id="C1", in_world_time="now", transcript="PRIVATE SPAN", events=events)
    assert adapter.drawers == [] and kernel.status()["counts"]["scene_record"] == 0


@pytest.mark.parametrize(
    ("transcript", "events", "error"),
    [
        ("PUBLIC PRIVATE", [_event(span="PUBLIC PRIVATE"), _event(visibility="character_private", access_owner_id="other", span="PRIVATE")], "overlapping source spans"),
        ("SAME", [_event(span="SAME"), _event(visibility="character_private", access_owner_id="other", span="SAME")], "overlapping source spans"),
        ("ABCD", [_event(span="ABC"), _event(visibility="character_private", access_owner_id="other", span="BCD")], "overlapping source spans"),
        ("REPEAT REPEAT", [_event(span="REPEAT")], "occur exactly once"),
    ],
)
def test_aerp1_overlap_prevalidation_rejects_before_adapter_or_sqlite(tmp_path, transcript, events, error):
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "prevalidate.sqlite3"), episode_adapter=adapter)
    with pytest.raises(ValueError, match=error):
        kernel.commit_scene(campaign_id="C1", in_world_time="now", transcript=transcript, events=events)
    assert adapter.drawers == [] and kernel.status()["counts"]["scene_record"] == 0


def test_aerp1_disjoint_mixed_policy_spans_commit(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "disjoint.sqlite3"))
    kernel.commit_scene(campaign_id="C1", in_world_time="now", transcript="PUBLIC PRIVATE", events=[_event(span="PUBLIC"), _event(visibility="character_private", access_owner_id="other", span="PRIVATE")])
    assert kernel.status()["counts"]["scene_event"] == 2


def test_aerp1_legacy_membership_shape_opens_twice_and_never_authorizes(tmp_path):
    db = tmp_path / "membership.sqlite3"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE actor_membership (campaign_id TEXT, actor_id TEXT, scope_id TEXT, active INTEGER)")
    conn.execute("INSERT INTO actor_membership VALUES ('C1', 'hero', 'shared', 1)")
    conn.commit(); conn.close()
    first = RpgMemoryKernel(db_path=str(db)); first.close()
    kernel = RpgMemoryKernel(db_path=str(db))
    kernel.commit_scene(campaign_id="C1", scene_id="membership", in_world_time="now", transcript="FACTION SPAN", events=[_event(visibility="faction_private", access_scope_id="shared", span="FACTION SPAN")])
    denied = kernel.authorized_evidence(campaign_id="C1", actor_id="hero", actor_type="npc", query="faction", budget=10)
    assert denied.events == []
    kernel.upsert_actor_membership(campaign_id="C1", actor_id="hero", scope_id="shared", scope_kind="faction")
    assert len(kernel.authorized_evidence(campaign_id="C1", actor_id="hero", actor_type="npc", query="faction", budget=10).events) == 1


def test_aerp1_all_enabled_projections_score_before_event_deduplication(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "projections.sqlite3"))
    kernel.commit_scene(campaign_id="C1", scene_id="projection", in_world_time="now", location_id="loc-here", transcript="CLUE SPAN", events=[SceneEventInput(event_type="evidence", summary="clue", branch_id="main", branch_status="active", truth_status="canonical", visibility="public_world", source_span="CLUE SPAN", related_locations=["loc-here"])])
    event_id = kernel._conn().execute("SELECT event_id FROM scene_event").fetchone()[0]
    assert len([item for item in kernel.list_memory_items() if item["source_event_id"] == event_id]) >= 2
    pack = kernel.build_memory_pack(campaign_id="C1", actor_id="hero", actor_type="npc", query="clue", location_id="loc-here")
    assert [item["source_event_id"] for item in pack.evidence].count(event_id) == 1
    assert pack.evidence[0]["domain"] == "location"
    common = {"text": "clue", "importance": 1.0, "emotional_weight": 0.0, "related_quests": [], "related_locations": ["loc-here"]}
    assert kernel._rank_score({**common, "domain": "location"}, query="clue", active_quest_ids=[], location_id="loc-here") == kernel._rank_score({**common, "domain": "canon"}, query="clue", active_quest_ids=[], location_id=None) + 0.85


def test_aerp1_projection_scan_reaches_authorized_event_beyond_old_prefix_limit(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "prefix.sqlite3"))
    kernel.commit_scene(campaign_id="C1", scene_id="old", in_world_time="old", transcript="OLD GOLD SPAN", events=[SceneEventInput(event_type="evidence", summary="old gold", branch_id="main", branch_status="active", truth_status="canonical", visibility="public_world", source_span="OLD GOLD SPAN", importance=10)])
    event_id = kernel._conn().execute("SELECT event_id FROM scene_event WHERE scene_id='old'").fetchone()[0]
    kernel.commit_scene(campaign_id="C1", scene_id="newer", in_world_time="new", transcript="newer", events=[])
    for index in range(1001):
        kernel._insert_memory_item(owner_scope="C1", domain="canon", source_scene_id="newer", source_event_id=f"noise-{index}", memory_type="summary", text="noise", visibility="public_world", known_by=[], related_entities=[], related_quests=[], related_locations=[], importance=0.0, emotional_weight=0.0, vector_id=None, created_at=f"9999-{index:04d}")
    kernel._conn().commit()
    pack = kernel.build_memory_pack(campaign_id="C1", actor_id="hero", actor_type="npc", query="old gold")
    assert pack.evidence[0]["source_event_id"] == event_id


def test_aerp1_deep_default_scene_limit_keeps_b0_top_ranked_older_span(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "deep-order.sqlite3"))
    kernel.commit_scene(campaign_id="C1", scene_id="old", in_world_time="old", transcript="OLD TOP SPAN", events=[SceneEventInput(event_type="evidence", summary="target", branch_id="main", branch_status="active", truth_status="canonical", visibility="public_world", source_span="OLD TOP SPAN", importance=10)])
    for index in range(4):
        kernel.commit_scene(campaign_id="C1", scene_id=f"new-{index}", in_world_time=f"new-{index}", transcript=f"NEW SPAN {index}", events=[SceneEventInput(event_type="evidence", summary="target", branch_id="main", branch_status="active", truth_status="canonical", visibility="public_world", source_span=f"NEW SPAN {index}", importance=1)])
    deep = kernel.deep_recall(campaign_id="C1", actor_id="hero", actor_type="npc", query="target")
    assert deep["evidence"][0]["text"] == "target"
    assert deep["scene_evidence"][0]["authorized_spans"][0]["text"] == "OLD TOP SPAN"


def test_aerp1_runner_repeats_despite_stale_legacy_temp_name(tmp_path):
    output = tmp_path / "aerp1.json"
    (tmp_path / ".aerp1.json.tmp").write_text("stale", encoding="utf-8")
    command = [sys.executable, "tests/run_aerp1_audit.py", "--output", str(output)]
    assert subprocess.run(command, check=False, cwd=".").returncode == 0
    assert subprocess.run(command, check=False, cwd=".").returncode == 0
    assert output.exists()


def test_aerp1_runner_require_clean_rejects_untracked_worktree_state(tmp_path):
    output = tmp_path / "aerp1-dirty.json"
    sentinel = __import__("pathlib").Path(".aerp1-dirty-sentinel")
    sentinel.write_text("dirty", encoding="utf-8")
    try:
        command = [sys.executable, "tests/run_aerp1_audit.py", "--output", str(output), "--require-clean"]
        assert subprocess.run(command, check=False, cwd=".").returncode == 1
    finally:
        sentinel.unlink(missing_ok=True)
    report = __import__("json").loads(output.read_text(encoding="utf-8"))
    assert report["aggregate"]["verdict"] == "FAIL"
    assert "checkpoint_requires_clean_git_worktree" in report["aggregate"]["gate_errors"]
