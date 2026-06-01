import json

from mempalace_rpg import RpgMemoryKernel, SceneEventInput


def write_settings(tmp_path, data):
    path = tmp_path / "memo_setting.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)


def test_memo_settings_can_disable_conflicting_domain_projection(tmp_path):
    settings_path = write_settings(
        tmp_path,
        {
            "write": {"domains": {"item": False}},
            "recall": {"domains": {"item": False}},
        },
    )
    kernel = RpgMemoryKernel(
        db_path=str(tmp_path / "rpg.sqlite3"),
        memo_settings_path=settings_path,
    )

    kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历7日",
        transcript="玩家把银坠交给 Liora。",
        participants=["player", "char_liora"],
        witnesses=["player", "char_liora"],
        events=[
            SceneEventInput(
                event_type="item_transfer",
                summary="玩家把银坠交给 Liora 作为信物。",
                actor_id="player",
                target_id="char_liora",
                visibility="witnessed_only",
                witness_set=["player", "char_liora"],
                related_entities=["player", "char_liora", "item_silver_pendant"],
                importance=0.8,
            )
        ],
    )

    assert kernel.list_memory_items(domain="item") == []
    assert kernel.list_memory_items(domain="character", owner_scope="char_liora")


def test_memo_settings_can_disable_mechanical_event_types(tmp_path):
    settings_path = write_settings(tmp_path, {"write": {"event_types": {"state_patch": False}}})
    kernel = RpgMemoryKernel(
        db_path=str(tmp_path / "rpg.sqlite3"),
        memo_settings_path=settings_path,
    )

    kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历8日",
        transcript="系统状态更新。",
        participants=["player"],
        witnesses=["player"],
        events=[
            SceneEventInput(
                event_type="state_patch",
                summary="玩家等级从 1 变为 2。",
                actor_id="player",
                visibility="gm_only",
                related_entities=["player"],
            ),
            SceneEventInput(
                event_type="promise",
                summary="玩家答应明天去港口。",
                actor_id="player",
                visibility="party_only",
                related_entities=["player"],
            ),
        ],
    )

    memories = kernel.list_memory_items()
    assert all("等级从 1 变为 2" not in item["text"] for item in memories)
    assert any("明天去港口" in item["text"] for item in memories)


def test_memo_settings_can_disable_recall_sections_that_host_owns(tmp_path):
    settings_path = write_settings(tmp_path, {"recall": {"sections": {"current_state": False}}})
    kernel = RpgMemoryKernel(
        db_path=str(tmp_path / "rpg.sqlite3"),
        memo_settings_path=settings_path,
    )
    kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历9日 清晨",
        location_id="loc_silverport",
        transcript="玩家抵达银帆城。",
        participants=["player"],
        witnesses=["player"],
    )

    rendered = kernel.build_memory_pack(actor_id="gm", actor_type="gm", query="现在在哪").render()
    assert "## Current State" not in rendered
    assert "loc_silverport" not in rendered


def test_status_reports_memo_setting_path_and_host_ownership(tmp_path):
    settings_path = write_settings(
        tmp_path,
        {"host_state_ownership": {"inventory": "host_package"}},
    )
    kernel = RpgMemoryKernel(
        db_path=str(tmp_path / "rpg.sqlite3"),
        memo_settings_path=settings_path,
    )

    status = kernel.status()
    assert status["memo_settings"]["path"] == settings_path
    assert status["memo_settings"]["host_state_ownership"]["inventory"] == "host_package"
