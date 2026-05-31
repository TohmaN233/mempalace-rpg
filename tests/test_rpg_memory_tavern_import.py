import json

from mempalace_rpg import RpgMemoryKernel
from mempalace_rpg.adapter import RecordingEpisodeAdapter
from mempalace_rpg.tavern_importer import import_taverndb


def write_taverndb(tmp_path):
    data = {
        "mate": {"type": "chatSheets", "version": 2},
        "sheet_pc": {
            "name": "主角信息表",
            "content": [
                ["row_id", "人物名称", "性别/年龄", "外貌特征", "职业/身份", "过往经历", "性格特点"],
                ["1", "旧主角", "男/28", "黑发", "旧日勇者", "旧主角完成了旧战役。", "谨慎"],
            ],
        },
        "sheet_chars": {
            "name": "重要角色表",
            "content": [
                ["row_id", "姓名", "性别/年龄/等级", "初夜状态", "重要日期", "角色间关系", "关键记忆", "目前经历"],
                ["1", "Liora", "女/21岁/21级", "", "生日", "旧主角的盟友", "被旧主角救过", "守在灰烬桥"],
            ],
        },
        "sheet_guides": {
            "name": "角色扮演指南",
            "content": [
                ["row_id", "角色姓名", "语言特征", "动态对话示例", "互动态度字典"],
                ["1", "Liora", "说话短促", "别再许空话。", "旧主角:信任"],
            ],
        },
        "sheet_promises": {
            "name": "约定表",
            "content": [
                ["row_id", "档案ID", "关联编码", "约定主题", "约定双方", "详细内容", "重要度", "状态", "信息来源"],
                ["1", "CV001", "AM0001", "救援承诺", "旧主角与Liora", "旧主角答应救出 Liora 的弟弟。", "high", "进行中", "口头约定"],
            ],
        },
        "sheet_notes": {
            "name": "纪要表",
            "content": [
                ["row_id", "编码索引", "时间跨度", "地点", "纪要", "概览", "参与人员"],
                ["1", "AM0001", "复兴纪元1日 07:00-08:00", "灰烬桥", "旧主角与 Liora 在灰烬桥谈及救援。", "旧主角向 Liora 作出救援承诺。", "旧主角, Liora"],
            ],
        },
    }
    path = tmp_path / "tavern.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_import_taverndb_writes_legacy_characters_and_past_plot(tmp_path):
    path = write_taverndb(tmp_path)
    adapter = RecordingEpisodeAdapter()
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"), episode_adapter=adapter)

    result = import_taverndb(kernel, str(path), campaign_id="legacy_campaign")

    assert result["success"] is True
    assert result["counts"]["characters"] == 2  # old protagonist + Liora
    assert result["counts"]["scenes"] == 1
    assert result["counts"]["promises"] == 1
    assert len(adapter.drawers) >= 1
    assert any("旧主角与 Liora" in drawer.text for drawer in adapter.drawers)

    gm_pack = kernel.build_memory_pack(actor_id="gm", actor_type="gm", query="救援承诺").render()
    assert "旧主角向 Liora 作出救援承诺" in gm_pack
    assert "旧主角答应救出 Liora 的弟弟" in gm_pack

    liora_id = result["entities"]["characters"]["Liora"]
    liora_pack = kernel.build_memory_pack(actor_id=liora_id, actor_type="npc", query="旧主角承诺").render()
    assert "救援承诺" in liora_pack

    new_player_pack = kernel.build_memory_pack(actor_id="player", actor_type="player", query="救援承诺").render()
    assert "救援承诺" not in new_player_pack


def test_import_taverndb_is_idempotent_for_deterministic_scene_ids(tmp_path):
    path = write_taverndb(tmp_path)
    kernel = RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"))

    first = import_taverndb(kernel, str(path), campaign_id="legacy_campaign")
    second = import_taverndb(kernel, str(path), campaign_id="legacy_campaign")

    assert first["counts"]["scenes"] == 1
    assert second["counts"]["skipped_existing_scenes"] >= 1
    assert kernel.status()["counts"]["scene_record"] == first["counts"]["scene_records_total"]
