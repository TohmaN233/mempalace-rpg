import json

from mempalace_rpg import mcp_server


def call(name, arguments=None, req_id=1):
    return mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
    )


def text_from_response(response):
    return response["result"]["content"][0]["text"]


def test_rpg_mcp_lists_tools_and_initializes(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)

    init = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    assert init["result"]["serverInfo"]["name"] == "mempalace-rpg"

    listed = mcp_server.handle_request(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert "mempalace_rpg_commit_scene" in names
    assert "mempalace_rpg_recall" in names
    assert "mempalace_rpg_upsert_profile" in names
    assert "commit_scene" in names
    assert "recall" in names
    assert "import_taverndb" in names


def test_rpg_mcp_commit_scene_and_acl_recall(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)

    assert call("mempalace_rpg_init")["result"]
    call(
        "mempalace_rpg_upsert_profile",
        {
            "character_id": "char_liora",
            "display_name": "Liora",
            "tier": "major",
            "short_persona": "前侦察队长，戒备心强。",
            "memory_wing": "wing_character_char_liora",
        },
    )
    call(
        "mempalace_rpg_commit_scene",
        {
            "campaign_id": "camp_demo",
            "in_world_time": "星辉历6日 黎明",
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
    )

    visible = call(
        "recall",
        {
            "actor_id": "char_liora",
            "actor_type": "npc",
            "query": "承诺",
            "active_quest_ids": ["quest_rescue_brother"],
        },
    )
    blocked = call(
        "mempalace_rpg_recall",
        {"actor_id": "char_guard", "actor_type": "npc", "query": "承诺"},
    )

    assert "承诺救回她弟弟" in text_from_response(visible)
    assert "承诺救回她弟弟" not in text_from_response(blocked)


def test_rpg_mcp_can_use_memo_settings(tmp_path):
    settings_path = tmp_path / "memo_setting.json"
    settings_path.write_text(json.dumps({"write": {"event_types": {"state_patch": False}}}), encoding="utf-8")
    mcp_server.configure(
        db_path=str(tmp_path / "rpg.sqlite3"),
        palace_path=None,
        memo_settings_path=str(settings_path),
    )

    init = call("init")
    assert str(settings_path) in text_from_response(init)

    call(
        "commit_scene",
        {
            "campaign_id": "camp_demo",
            "in_world_time": "星辉历6日 黎明",
            "transcript": "状态变化与承诺。",
            "participants": ["player"],
            "witnesses": ["player"],
            "events": [
                {"event_type": "state_patch", "summary": "玩家等级从 1 到 2。", "actor_id": "player"},
                {"event_type": "promise", "summary": "玩家答应明日去港口。", "actor_id": "player"},
            ],
        },
    )
    memories = call("list_memories")
    text = text_from_response(memories)
    assert "等级从 1 到 2" not in text
    assert "明日去港口" in text


def test_rpg_mcp_rejects_unknown_arguments(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)
    response = call("mempalace_rpg_recall", {"actor_id": "gm", "query": "x", "spoof": True})
    assert response["error"]["code"] == -32602
    assert "Unknown parameter 'spoof'" in response["error"]["message"]
