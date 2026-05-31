import json
import subprocess
import sys


def run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "mempalace_rpg.cli", *args],
        text=True,
        capture_output=True,
        check=True,
    )


def test_cli_commit_scene_and_recall_are_external_processes(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    profile_file = tmp_path / "liora.json"
    scene_file = tmp_path / "scene.json"

    profile_file.write_text(
        json.dumps(
            {
                "character_id": "char_liora",
                "display_name": "Liora",
                "tier": "major",
                "short_persona": "前侦察队长，谨慎而讽刺。",
                "memory_wing": "wing_character_char_liora",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    scene_file.write_text(
        json.dumps(
            {
                "campaign_id": "camp_demo",
                "in_world_time": "星辉历5日 夜",
                "location_id": "loc_ash_bridge",
                "active_quest_ids": ["quest_rescue_brother"],
                "transcript": "玩家向 Liora 承诺救回她弟弟。",
                "participants": ["player", "char_liora"],
                "witnesses": ["char_liora"],
                "events": [
                    {
                        "event_type": "promise",
                        "summary": "玩家向 Liora 承诺救回她弟弟。",
                        "actor_id": "player",
                        "target_id": "char_liora",
                        "truth_status": "canonical",
                        "visibility": "witnessed_only",
                        "witness_set": ["char_liora"],
                        "related_entities": ["player", "char_liora"],
                        "related_quests": ["quest_rescue_brother"],
                        "importance": 0.9,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    init = run_cli("--db", str(db), "init")
    assert json.loads(init.stdout)["ok"] is True

    profile = run_cli("--db", str(db), "upsert-profile", str(profile_file))
    assert json.loads(profile.stdout)["character_id"] == "char_liora"

    committed = run_cli("--db", str(db), "commit-scene", str(scene_file))
    assert json.loads(committed.stdout)["scene_id"].startswith("scene_")

    recall = run_cli(
        "--db",
        str(db),
        "recall",
        "--actor-id",
        "char_liora",
        "--actor-type",
        "npc",
        "--query",
        "承诺",
        "--active-quest",
        "quest_rescue_brother",
    )
    assert "承诺救回她弟弟" in recall.stdout

    blocked = run_cli(
        "--db",
        str(db),
        "recall",
        "--actor-id",
        "char_guard",
        "--actor-type",
        "npc",
        "--query",
        "承诺",
    )
    assert "承诺救回她弟弟" not in blocked.stdout
