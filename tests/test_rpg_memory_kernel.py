from mempalace_rpg import RpgMemoryKernel, SceneEventInput
from mempalace_rpg.adapter import RecordingEpisodeAdapter
from mempalace_rpg.budget import budget_for_tier


def test_private_witnessed_scene_is_filtered_before_recall(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"))
    kernel.upsert_character_profile(
        character_id="char_liora",
        display_name="Liora",
        tier="major",
        short_persona="戒备心强的前侦察队长。",
        memory_wing="wing_character_char_liora",
    )
    kernel.upsert_character_profile(
        character_id="char_guard",
        display_name="北门守卫",
        tier="recurring",
        short_persona="普通守卫。",
        memory_wing="wing_character_char_guard",
    )

    kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历1日 黄昏",
        location_id="loc_ash_bridge",
        transcript="玩家在灰烬桥低声向 Liora 承诺：我会救回你弟弟。",
        participants=["player", "char_liora"],
        witnesses=["char_liora"],
        events=[
            SceneEventInput(
                event_type="promise",
                summary="玩家在灰烬桥向 Liora 承诺救回她弟弟。",
                actor_id="player",
                target_id="char_liora",
                truth_status="canonical",
                visibility="witnessed_only",
                witness_set=["char_liora"],
                related_entities=["player", "char_liora"],
                related_quests=["quest_rescue_brother"],
                emotional_weight=0.8,
                importance=0.9,
            )
        ],
    )

    liora_pack = kernel.build_memory_pack(
        actor_id="char_liora",
        actor_type="npc",
        query="玩家承诺过什么？",
        active_quest_ids=["quest_rescue_brother"],
    ).render()
    guard_pack = kernel.build_memory_pack(
        actor_id="char_guard",
        actor_type="npc",
        query="玩家承诺过什么？",
        active_quest_ids=["quest_rescue_brother"],
    ).render()
    gm_pack = kernel.build_memory_pack(
        actor_id="gm",
        actor_type="gm",
        query="玩家承诺过什么？",
        active_quest_ids=["quest_rescue_brother"],
    ).render()

    assert "承诺救回她弟弟" in liora_pack
    assert "承诺救回她弟弟" not in guard_pack
    assert "承诺救回她弟弟" in gm_pack


def test_memory_pack_evidence_includes_time_and_location(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"))
    kernel.commit_scene(
        campaign_id="campaign",
        scene_id="scene_time_anchor",
        in_world_time="复兴纪元488年辉光之月01日07:12",
        location_id="loc_ash_bridge",
        transcript="Liora made an old promise on the ash bridge.",
        participants=["char_liora"],
        witnesses=["char_liora"],
        events=[
            SceneEventInput(
                event_type="promise",
                summary="Liora remembers the ash bridge promise.",
                visibility="witnessed_only",
                witness_set=["char_liora"],
                related_entities=["char_liora"],
            )
        ],
    )

    rendered = kernel.build_memory_pack(
        actor_id="char_liora",
        actor_type="npc",
        query="ash bridge promise",
    ).render()

    assert "复兴纪元488年辉光之月01日07:12" in rendered
    assert "loc_ash_bridge" in rendered
    assert "scene:scene_time_anchor" in rendered
    assert "Liora remembers the ash bridge promise" in rendered


def test_rumor_is_actor_belief_not_world_truth(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"))
    kernel.upsert_entity("char_bard", "character", "酒馆吟游诗人")
    kernel.upsert_entity("char_prince", "character", "黑塔王子")

    kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历2日 夜",
        location_id="loc_tavern",
        transcript="吟游诗人散布传闻：黑塔王子已经死在北境。",
        participants=["player", "char_bard"],
        witnesses=["player", "char_bard"],
        events=[
            SceneEventInput(
                event_type="rumor",
                summary="酒馆传闻称黑塔王子已经死在北境。",
                actor_id="char_bard",
                target_id="char_prince",
                truth_status="rumor",
                visibility="rumor_public",
                witness_set=["player", "char_bard"],
                related_entities=["char_bard", "char_prince"],
                importance=0.6,
            )
        ],
    )

    assert kernel.list_world_facts(subject_id="char_prince") == []
    beliefs = kernel.list_actor_beliefs(actor_id="char_bard", subject_id="char_prince")
    assert len(beliefs) == 1
    assert beliefs[0]["belief_status"] == "rumored"
    assert "黑塔王子已经死" in beliefs[0]["object"]["summary"]


def test_scene_event_projects_to_domain_memories_with_evidence(tmp_path):
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"))
    scene_id = kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历3日 清晨",
        location_id="loc_ash_bridge",
        active_quest_ids=["quest_rescue_brother"],
        transcript="灰烬桥的旧石栏旁，玩家把银坠交给 Liora 作为信物。",
        participants=["player", "char_liora"],
        witnesses=["player", "char_liora"],
        events=[
            SceneEventInput(
                event_type="item_transfer",
                summary="玩家把银坠交给 Liora 作为救援承诺的信物。",
                actor_id="player",
                target_id="char_liora",
                truth_status="canonical",
                visibility="witnessed_only",
                witness_set=["player", "char_liora"],
                related_entities=["player", "char_liora", "item_silver_pendant"],
                related_quests=["quest_rescue_brother"],
                related_locations=["loc_ash_bridge"],
                importance=0.7,
            )
        ],
    )

    quest_items = kernel.list_memory_items(domain="quest", owner_scope="quest_rescue_brother")
    character_items = kernel.list_memory_items(domain="character", owner_scope="char_liora")
    location_items = kernel.list_memory_items(domain="location", owner_scope="loc_ash_bridge")

    assert len(quest_items) == 1
    assert quest_items[0]["source_scene_id"] == scene_id
    assert quest_items[0]["source_event_id"] is not None
    assert "银坠" in quest_items[0]["text"]
    assert len(character_items) == 1
    assert len(location_items) == 1


def test_episode_adapter_receives_verbatim_scene_drawer(tmp_path):
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"), episode_adapter=adapter)

    scene_id = kernel.commit_scene(
        campaign_id="camp_demo",
        in_world_time="星辉历4日 午后",
        location_id="loc_silverport",
        transcript="银帆城午后的海风里，玩家第一次听见蓝光潮的传闻。",
        participants=["player"],
        witnesses=["player"],
        events=[],
    )

    assert len(adapter.drawers) == 1
    drawer = adapter.drawers[0]
    assert drawer.text == "银帆城午后的海风里，玩家第一次听见蓝光潮的传闻。"
    assert drawer.wing == "wing_campaign_canon"
    assert drawer.room == "location_loc_silverport"
    assert drawer.metadata["scene_id"] == scene_id


def test_tier_budgets_keep_minor_npcs_lightweight():
    core = budget_for_tier("core")
    recurring = budget_for_tier("recurring")
    ambient = budget_for_tier("ambient")

    assert core.l2_chars > recurring.l2_chars > ambient.l2_chars
    assert core.allow_deep_recall is True
    assert recurring.allow_deep_recall is False
    assert ambient.l0_chars <= recurring.l0_chars
