import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mempalace_rpg import RpgMemoryKernel, SceneEventInput
from mempalace_rpg.maintenance import backup, delete_after, delete_scenes, restore, sync_branch


def test_backup_restore_and_delete_after(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    kernel = RpgMemoryKernel(str(db))
    old_scene = kernel.commit_scene(
        scene_id="old_scene",
        campaign_id="c",
        in_world_time="old",
        transcript="old transcript",
        participants=["npc_a"],
        witnesses=["npc_a"],
        events=[SceneEventInput(event_type="old", summary="old event", visibility="witnessed_only")],
    )
    backup_result = backup(str(db))
    assert Path(backup_result["db_backup"]).exists()

    cutoff = datetime.now(timezone.utc).isoformat()
    new_scene = kernel.commit_scene(
        scene_id="new_scene",
        campaign_id="c",
        in_world_time="new",
        transcript="new transcript",
        participants=["npc_a"],
        witnesses=["npc_a"],
        events=[SceneEventInput(event_type="new", summary="new event", visibility="witnessed_only")],
    )
    assert old_scene == "old_scene"
    assert new_scene == "new_scene"

    dry = delete_after(str(db), cutoff, dry_run=True)
    assert dry["counts"]["scene_record"] == 1
    assert dry["scenes"][0]["scene_id"] == "new_scene"

    deleted = delete_after(str(db), cutoff)
    assert deleted["deleted"]["scene_record"] == 1
    assert kernel.status()["counts"]["scene_record"] == 1
    pack = kernel.build_memory_pack(actor_id="gm", actor_type="gm", query="new old").render()
    assert "old event" in pack
    assert "new event" not in pack

    restore(str(db), db_backup=backup_result["db_backup"], pre_backup=False)
    assert kernel.status()["counts"]["scene_record"] == 1


def test_delete_scenes_and_sync_branch(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    kernel = RpgMemoryKernel(str(db))
    keep = kernel.commit_scene(
        scene_id="keep_auto",
        campaign_id="c",
        in_world_time="keep",
        transcript="keep transcript",
        events=[SceneEventInput(event_type="scene_transcript", summary="keep", payload={"auto_commit": True, "branch_scope_id": "s1"})],
    )
    drop = kernel.commit_scene(
        scene_id="drop_auto",
        campaign_id="c",
        in_world_time="drop",
        transcript="drop transcript",
        events=[SceneEventInput(event_type="scene_transcript", summary="drop", payload={"auto_commit": True, "branch_scope_id": "s1"})],
    )
    manual = kernel.commit_scene(
        scene_id="manual_scene",
        campaign_id="c",
        in_world_time="manual",
        transcript="manual transcript",
        events=[SceneEventInput(event_type="note", summary="manual")],
    )

    other = kernel.commit_scene(
        scene_id="other_session_auto",
        campaign_id="c",
        in_world_time="other",
        transcript="other transcript",
        events=[SceneEventInput(event_type="scene_transcript", summary="other", payload={"auto_commit": True, "branch_scope_id": "s2"})],
    )

    preview = sync_branch(str(db), keep_scene_ids=[keep], campaign_id="c", branch_scope_id="s1", dry_run=True)
    assert preview["delete_scene_count"] == 1
    assert preview["scene_ids"] == [drop]

    synced = sync_branch(str(db), keep_scene_ids=[keep], campaign_id="c", branch_scope_id="s1")
    assert synced["scene_ids_deleted"] == [drop]
    assert other == "other_session_auto"
    assert kernel.status()["counts"]["scene_record"] == 3

    deleted = delete_scenes(str(db), [manual])
    assert deleted["deleted"]["scene_record"] == 1
    assert kernel.status()["counts"]["scene_record"] == 2


def test_cli_delete_after_dry_run(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    payload = {
        "scene_id": "scene_cli",
        "campaign_id": "c",
        "in_world_time": "now",
        "transcript": "hello",
        "events": [{"event_type": "note", "summary": "hello", "visibility": "public_world"}],
    }
    scene_file = tmp_path / "scene.json"
    scene_file.write_text(json.dumps(payload), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "commit-scene", str(scene_file)],
        check=True,
        capture_output=True,
        text=True,
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    result = subprocess.run(
        [sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "delete-after", cutoff, "--dry-run"],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(result.stdout)
    assert data["dry_run"] is True
    assert data["counts"]["scene_record"] == 1
