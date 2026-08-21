import json
import subprocess
import sys

import pytest


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
                        "branch_id": "main",
                        "branch_status": "active",
                        "summary": "玩家向 Liora 承诺救回她弟弟。",
                        "source_span": "玩家向 Liora 承诺救回她弟弟。",
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
            "--campaign-id",
            "camp_demo",
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
        "--campaign-id",
        "camp_demo",
        "--actor-id",
        "char_guard",
        "--actor-type",
        "npc",
        "--query",
        "承诺",
    )
    assert "承诺救回她弟弟" not in blocked.stdout


def test_cli_deep_spans_and_restricted_product_surfaces(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    payload = {"campaign_id": "c", "in_world_time": "early", "transcript": "EXACT SPAN", "events": [{"event_type": "promise", "summary": "a promise", "branch_id": "main", "branch_status": "active", "truth_status": "canonical", "visibility": "public_world", "source_span": "EXACT SPAN"}]}
    source = tmp_path / "scene.json"; source.write_text(json.dumps(payload), encoding="utf-8")
    run_cli("--db", str(db), "commit-scene", str(source))
    deep = run_cli("--db", str(db), "deep-recall", "--campaign-id", "c", "--actor-id", "gm", "--actor-type", "gm", "--query", "promise")
    assert "event:" in deep.stdout and "EXACT SPAN" in deep.stdout


def test_cli_rejects_malformed_scene_list_and_party_only_import_before_writes(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    scene = tmp_path / "bad-scene.json"
    scene.write_text(json.dumps({"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "witnesses": "hero", "events": []}), encoding="utf-8")
    bad_scene = subprocess.run([sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "commit-scene", str(scene)], text=True, capture_output=True)
    assert bad_scene.returncode != 0 and not db.exists()
    source = tmp_path / "empty-tavern.json"
    source.write_text("{}", encoding="utf-8")
    bad_import = subprocess.run([sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "import-taverndb", str(source), "--default-visibility", "party_only"], text=True, capture_output=True)
    assert bad_import.returncode != 0 and not db.exists()
    full = subprocess.run([sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "get-scene", "--campaign-id", "c", "--scene-id", "scene_missing", "--actor-id", "gm", "--mode", "full"], text=True, capture_output=True)
    assert full.returncode == 2 and "invalid choice" in full.stderr
    for kind in ("facts", "beliefs", "memories"):
        raw_list = subprocess.run([sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "list", kind], text=True, capture_output=True)
        assert raw_list.returncode != 0 and "Raw evidence listing is local-admin-only and is not exposed through the CLI" in raw_list.stderr


@pytest.mark.parametrize("field", ["active_quest_ids", "participants", "witnesses"])
def test_cli_explicit_null_optional_scene_lists_fail_before_database_creation(tmp_path, field):
    db = tmp_path / "rpg.sqlite3"
    source = tmp_path / f"null-{field}.json"
    source.write_text(json.dumps({"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", field: None, "events": []}), encoding="utf-8")
    result = subprocess.run([sys.executable, "-m", "mempalace_rpg.cli", "--db", str(db), "commit-scene", str(source)], text=True, capture_output=True)
    assert result.returncode != 0
    assert not db.exists()
