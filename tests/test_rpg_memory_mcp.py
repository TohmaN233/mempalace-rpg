import json

import pytest

from mempalace_rpg import mcp_server


_OMITTED = object()


def call(name, arguments=_OMITTED, req_id=1):
    params = {"name": name}
    if arguments is not _OMITTED:
        params["arguments"] = arguments
    return mcp_server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": params,
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
                    "branch_id": "main",
                    "branch_status": "active",
                    "summary": "玩家向 Liora 承诺救回她弟弟。",
                    "actor_id": "player",
                    "target_id": "char_liora",
                    "truth_status": "canonical",
                    "visibility": "witnessed_only",
                    "witness_set": ["char_liora"],
                    "related_entities": ["player", "char_liora"],
                    "related_quests": ["quest_rescue_brother"],
                    "source_span": "玩家向 Liora 承诺救回她弟弟。",
                    "importance": 0.9,
                }
            ],
        },
    )

    visible = call(
        "recall",
            {
                "campaign_id": "camp_demo",
                "actor_id": "char_liora",
            "actor_type": "npc",
            "query": "承诺",
            "active_quest_ids": ["quest_rescue_brother"],
        },
    )
    blocked = call(
        "mempalace_rpg_recall",
            {"campaign_id": "camp_demo", "actor_id": "char_guard", "actor_type": "npc", "query": "承诺"},
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
    assert json.loads(text_from_response(init))["memo_setting"] == str(settings_path)

    call(
        "commit_scene",
        {
            "campaign_id": "camp_demo",
            "in_world_time": "星辉历6日 黎明",
            "transcript": "玩家等级从 1 到 2。玩家答应明日去港口。",
            "participants": ["player"],
            "witnesses": ["player"],
            "events": [
                    {"event_type": "state_patch", "summary": "玩家等级从 1 到 2。", "actor_id": "player", "branch_id": "main", "branch_status": "active", "truth_status": "canonical", "visibility": "gm_only", "source_span": "玩家等级从 1 到 2。"},
                    {"event_type": "promise", "summary": "玩家答应明日去港口。", "actor_id": "player", "branch_id": "main", "branch_status": "active", "truth_status": "canonical", "visibility": "party_only", "access_scope_id": "party_main", "source_span": "玩家答应明日去港口。"},
            ],
        },
    )
    memories = call("list_memories")
    assert memories["error"]["code"] == -32000
    assert "local-admin-only" in memories["error"]["data"]["message"]


def test_rpg_mcp_rejects_unknown_arguments(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)
    response = call("mempalace_rpg_recall", {"actor_id": "gm", "query": "x", "spoof": True})
    assert response["error"]["code"] == -32602
    assert "Unknown parameter 'spoof'" in response["error"]["message"]


def test_rpg_mcp_distinguishes_omitted_from_explicit_nonobject_arguments(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)
    explicit_list = call("status", [])
    assert explicit_list["error"]["code"] == -32602
    assert call("status")["result"]


def test_rpg_mcp_event_branch_is_schema_required_before_handler(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)
    base = {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE SPAN"}
    missing = call("commit_scene", {**base, "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world"}]})
    assert missing["error"]["code"] == -32602
    assert "branch_id" in missing["error"]["message"]
    valid = call("commit_scene", {**base, "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active", "source_span": "SAFE SPAN"}]})
    assert valid["result"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "witnesses": "hero"},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active", "source_span": "SAFE", "witness_set": "hero"}]},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active", "source_span": "SAFE", "witness_set": {"hero": True}}]},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active", "source_span": "SAFE", "witness_set": ["hero", 1]}]},
    ],
)
def test_rpg_mcp_recursively_rejects_malformed_provenance_lists(tmp_path, arguments):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)
    response = call("commit_scene", arguments)
    assert response["error"]["code"] == -32602


@pytest.mark.parametrize(
    "arguments",
    [
        {"campaign_id": " ", "in_world_time": "now", "transcript": "SAFE", "events": []},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "witnesses": [" "], "events": []},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active"}]},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active", "source_span": " "}]},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "character_private", "branch_id": "main", "branch_status": "active", "source_span": "SAFE", "access_owner_id": " "}]},
        {"campaign_id": "C1", "in_world_time": "now", "transcript": "SAFE", "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "witnessed_only", "branch_id": "main", "branch_status": "active", "source_span": "SAFE", "witness_set": [" "]}]},
    ],
)
def test_rpg_mcp_rejects_nonblank_scene_provenance_before_handler(tmp_path, arguments):
    db = tmp_path / "rpg.sqlite3"
    mcp_server.configure(db_path=str(db), palace_path=None)
    response = call("commit_scene", arguments)
    assert response["error"]["code"] == -32602
    assert "non-blank" in response["error"]["message"] or "source_span" in response["error"]["message"]
    assert not db.exists()


def test_rpg_mcp_accepts_open_event_payload_object(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    mcp_server.configure(db_path=str(db), palace_path=None)
    response = call(
        "commit_scene",
        {
            "campaign_id": "C1",
            "in_world_time": "now",
            "transcript": "SAFE",
            "events": [{"event_type": "evidence", "summary": "safe", "truth_status": "canonical", "visibility": "public_world", "branch_id": "main", "branch_status": "active", "source_span": "SAFE", "payload": {"x": 1, "nested": {"ok": True}}}],
        },
    )
    assert response["result"]
    assert db.exists()


def test_rpg_mcp_rejects_party_only_tavern_import_before_kernel_write(tmp_path):
    db = tmp_path / "rpg.sqlite3"
    mcp_server.configure(db_path=str(db), palace_path=None)
    response = call("import_taverndb", {"file_path": str(tmp_path / "missing.json"), "default_visibility": "party_only"})
    assert response["error"]["code"] == -32602
    assert not db.exists()


def test_mcp_scene_mode_and_raw_list_contract(tmp_path):
    mcp_server.configure(db_path=str(tmp_path / "rpg.sqlite3"), palace_path=None)
    listed = mcp_server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
    assert tools["mempalace_rpg_get_scene"]["inputSchema"]["properties"]["mode"]["enum"] == ["snippets"]
    full = call("get_scene", {"campaign_id": "C1", "scene_id": "missing", "actor_id": "hero", "mode": "full"})
    assert full["error"]["code"] == -32602
    assert "Invalid value" in full["error"]["message"]
    for name in ("list_memories", "list_world_facts", "list_actor_beliefs"):
        assert "Disabled MCP surface" in tools[f"mempalace_rpg_{name}"]["description"]
        denied = call(name)
        assert denied["error"]["code"] == -32000
        assert denied["error"]["data"]["message"] == "Raw evidence listing is local-admin-only and is not exposed through MCP"
