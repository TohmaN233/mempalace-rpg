import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mempalace_rpg import RpgMemoryKernel, SceneEventInput
from mempalace_rpg.maintenance import backup, delete_after, restore


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
