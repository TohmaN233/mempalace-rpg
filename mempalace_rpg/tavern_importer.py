"""Import SillyTavern ChatSheets/TavernDB exports as legacy RPG memory.

The importer treats old chat history as *past campaign canon*, not as the new
player's current state.  Former protagonists are imported as legacy characters;
scene summaries are written as witnessed memories for participants and GM-only
facts for directors.  Mechanical state remains owned by the host game.
"""

from __future__ import annotations

import json
import re
import unicodedata
from hashlib import sha1
from pathlib import Path
from typing import Any

from .kernel import RpgMemoryKernel
from .models import SceneEventInput


def _hash(text: str, n: int = 8) -> str:
    return sha1(text.encode("utf-8")).hexdigest()[:n]


def _clean_name(value: Any) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[（(]已死亡[）)]", "", text)
    return text.strip()


def _id_part(text: str, limit: int = 32) -> str:
    normalized = unicodedata.normalize("NFKC", text).strip()
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"[^\w\u4e00-\u9fff.-]+", "", normalized, flags=re.UNICODE)
    normalized = normalized.strip("_")
    if not normalized:
        normalized = "entity"
    return normalized[:limit]


def _stable_id(prefix: str, name: str) -> str:
    return f"{prefix}_{_id_part(name)}_{_hash(name)}"


def _rows(sheet: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not sheet:
        return []
    content = sheet.get("content") or []
    if not isinstance(content, list) or not content:
        return []
    header = [str(h or "") for h in content[0]]
    out: list[dict[str, Any]] = []
    for row in content[1:]:
        if not isinstance(row, list):
            continue
        padded = [*row, *([None] * max(0, len(header) - len(row)))]
        record = {key: padded[i] for i, key in enumerate(header) if key}
        if any(str(v or "").strip() for v in record.values()):
            out.append(record)
    return out


def _sheet_by_name(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    for value in data.values():
        if isinstance(value, dict) and value.get("name") == name:
            return value
    return None


def _split_people(value: Any) -> list[str]:
    text = str(value or "")
    text = text.replace("、", ",").replace("，", ",").replace(";", ",").replace("；", ",")
    # Promise rows often use A与B / A和B.
    text = re.sub(r"\s*(?:与|和)\s*", ",", text)
    people = [_clean_name(part) for part in text.split(",")]
    return [p for p in people if p]


def _tier_from_level(text: Any) -> str:
    raw = str(text or "")
    numbers = [int(n) for n in re.findall(r"(?:Lv\.?\s*|/|^)(\d{1,2})(?:级)?", raw, flags=re.I)]
    level = max(numbers) if numbers else 1
    if level >= 21:
        return "core"
    if level >= 10:
        return "major"
    return "recurring"


def _importance(value: Any) -> float:
    text = str(value or "").strip().lower()
    return {
        "critical": 1.0,
        "high": 0.9,
        "medium": 0.6,
        "mid": 0.6,
        "low": 0.3,
        "高": 0.9,
        "中": 0.6,
        "低": 0.3,
    }.get(text, 0.65)


def _scene_exists(kernel: RpgMemoryKernel, scene_id: str) -> bool:
    row = kernel._conn().execute(  # package-internal importer; use existing connection.
        "SELECT 1 FROM scene_record WHERE scene_id=?",
        (scene_id,),
    ).fetchone()
    return bool(row)


def _campaign_id_from_path(path: str) -> str:
    stem = Path(path).stem
    return f"legacy_tavern_{_id_part(stem, 48)}_{_hash(stem)}"


def _upsert_profile(
    kernel: RpgMemoryKernel,
    *,
    character_id: str,
    display_name: str,
    tier: str = "major",
    short_persona: str,
    speech_style: str | None = None,
    current_goal: str | None = None,
) -> None:
    kernel.upsert_character_profile(
        character_id=character_id,
        display_name=display_name,
        tier=tier,
        short_persona=short_persona or f"旧战役角色：{display_name}",
        speech_style=speech_style,
        current_goal=current_goal,
        memory_wing=f"wing_character_{character_id}",
    )


def import_taverndb(  # noqa: C901 - one import pipeline keeps row counters and id maps together
    kernel: RpgMemoryKernel,
    path: str,
    *,
    campaign_id: str | None = None,
    default_visibility: str = "witnessed_only",
    scene_limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import TavernDB ChatSheets JSON into the RPG memory kernel."""

    source_path = str(Path(path).expanduser())
    with open(source_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("TavernDB export root must be an object")

    campaign_id = campaign_id or _campaign_id_from_path(source_path)
    pc_rows = _rows(_sheet_by_name(data, "主角信息表"))
    character_rows = _rows(_sheet_by_name(data, "重要角色表"))
    guide_rows = _rows(_sheet_by_name(data, "角色扮演指南"))
    foreshadow_rows = _rows(_sheet_by_name(data, "伏笔表"))
    promise_rows = _rows(_sheet_by_name(data, "约定表"))
    scene_rows = _rows(_sheet_by_name(data, "纪要表"))
    if scene_limit is not None:
        scene_rows = scene_rows[: max(0, scene_limit)]

    speech_by_name = {
        _clean_name(row.get("角色姓名")): str(row.get("语言特征") or "").strip()
        for row in guide_rows
        if _clean_name(row.get("角色姓名"))
    }

    character_ids: dict[str, str] = {}
    counts = {
        "characters": 0,
        "guides": len(speech_by_name),
        "scenes": 0,
        "promises": 0,
        "foreshadowing": 0,
        "protagonists": 0,
        "skipped_existing_scenes": 0,
    }

    def char_id(name: str) -> str:
        clean = _clean_name(name)
        if clean not in character_ids:
            character_ids[clean] = _stable_id("char", clean)
        return character_ids[clean]

    # Former protagonist(s) become legacy characters, never the new player.
    for row in pc_rows:
        name = _clean_name(row.get("人物名称"))
        if not name:
            continue
        cid = _stable_id("char_legacy_pc", name)
        character_ids[name] = cid
        persona = "；".join(
            part
            for part in [
                "旧战役主角，不是当前新主角。",
                f"性别/年龄：{row.get('性别/年龄') or ''}",
                f"外貌：{row.get('外貌特征') or ''}",
                f"身份：{row.get('职业/身份') or ''}",
                f"性格：{row.get('性格特点') or ''}",
                f"旧经历：{row.get('过往经历') or ''}",
            ]
            if str(part).strip("；: ")
        )
        counts["protagonists"] += 1
        counts["characters"] += 1
        if not dry_run:
            _upsert_profile(
                kernel,
                character_id=cid,
                display_name=f"{name}（旧主角）",
                tier="core",
                short_persona=persona,
            )
            scene_id = f"legacy_profile_{_hash(campaign_id + cid)}"
            if _scene_exists(kernel, scene_id):
                counts["skipped_existing_scenes"] += 1
            else:
                kernel.commit_scene(
                    scene_id=scene_id,
                    campaign_id=campaign_id,
                    in_world_time="旧战役角色档案",
                    transcript=persona,
                    participants=[cid],
                    witnesses=[cid],
                    events=[
                        SceneEventInput(
                            event_type="legacy_protagonist_profile",
                            summary=persona,
                            target_id=cid,
                            truth_status="canonical",
                            visibility="gm_only",
                            related_entities=[cid],
                            importance=0.8,
                        )
                    ],
                )

    # Important characters + speech guides.
    for row in character_rows:
        name = _clean_name(row.get("姓名"))
        if not name:
            continue
        cid = char_id(name)
        persona = "；".join(
            part
            for part in [
                "旧战役重要角色。",
                f"性别/年龄/等级：{row.get('性别/年龄/等级') or ''}",
                f"重要日期：{row.get('重要日期') or ''}",
                f"关系：{row.get('角色间关系') or ''}",
                f"关键记忆：{row.get('关键记忆') or ''}",
                f"目前经历：{row.get('目前经历') or ''}",
            ]
            if str(part).strip("；: ")
        )
        counts["characters"] += 1
        if dry_run:
            continue
        _upsert_profile(
            kernel,
            character_id=cid,
            display_name=name,
            tier=_tier_from_level(row.get("性别/年龄/等级")),
            short_persona=persona,
            speech_style=speech_by_name.get(name),
        )
        scene_id = f"legacy_profile_{_hash(campaign_id + cid)}"
        if _scene_exists(kernel, scene_id):
            counts["skipped_existing_scenes"] += 1
            continue
        kernel.commit_scene(
            scene_id=scene_id,
            campaign_id=campaign_id,
            in_world_time="旧战役角色档案",
            transcript=persona,
            participants=[cid],
            witnesses=[cid],
            events=[
                SceneEventInput(
                    event_type="legacy_character_profile",
                    summary=f"{name} 的旧战役档案：{persona}",
                    target_id=cid,
                    truth_status="canonical",
                    visibility="character_private",
                    witness_set=[cid],
                    related_entities=[cid],
                    importance=0.75,
                    emotional_weight=0.3,
                )
            ],
        )

    def participant_ids(names: list[str]) -> list[str]:
        ids: list[str] = []
        for name in names:
            cid = char_id(name)
            ids.append(cid)
            if not dry_run and not kernel._profile(cid):
                _upsert_profile(
                    kernel,
                    character_id=cid,
                    display_name=name,
                    tier="recurring",
                    short_persona=f"旧战役出现角色：{name}",
                    speech_style=speech_by_name.get(name),
                )
        return ids

    # Timeline summaries become past plot scenes. Participants witnessed them;
    # current new player does not automatically know them.
    for row in scene_rows:
        code = str(row.get("编码索引") or row.get("row_id") or _hash(json.dumps(row, ensure_ascii=False))).strip()
        scene_id = f"legacy_scene_{_id_part(code, 24)}_{_hash(campaign_id + code)}"
        if dry_run:
            counts["scenes"] += 1
            continue
        if _scene_exists(kernel, scene_id):
            counts["skipped_existing_scenes"] += 1
            continue
        names = _split_people(row.get("参与人员"))
        participants = participant_ids(names)
        location_name = str(row.get("地点") or "旧战役地点未标注").strip()
        location_id = _stable_id("loc", location_name)
        kernel.upsert_entity(location_id, "location", location_name)
        transcript = "\n".join(
            part
            for part in [
                f"编码：{code}",
                f"时间：{row.get('时间跨度') or ''}",
                f"地点：{location_name}",
                f"纪要：{row.get('纪要') or ''}",
                f"概览：{row.get('概览') or ''}",
                f"参与人员：{row.get('参与人员') or ''}",
                "说明：这是旧主角时代的过去剧情，不是当前新主角的亲历记忆。",
            ]
            if str(part).strip()
        )
        kernel.commit_scene(
            scene_id=scene_id,
            campaign_id=campaign_id,
            in_world_time=str(row.get("时间跨度") or "旧战役时间未标注"),
            location_id=location_id,
            transcript=transcript,
            participants=participants,
            witnesses=participants,
            events=[
                SceneEventInput(
                    event_type="legacy_past_plot",
                    summary=str(row.get("概览") or row.get("纪要") or code),
                    truth_status="canonical",
                    visibility=default_visibility,
                    witness_set=participants,
                    related_entities=participants,
                    related_locations=[location_id],
                    source_span=code,
                    importance=0.65,
                    emotional_weight=0.2,
                    payload={"legacy_code": code, "source": "TavernDB 纪要表"},
                )
            ],
        )
        counts["scenes"] += 1

    # Promises/agreements.
    for row in promise_rows:
        archive_id = str(row.get("档案ID") or row.get("row_id") or _hash(json.dumps(row, ensure_ascii=False)))
        scene_id = f"legacy_promise_{_id_part(archive_id, 24)}_{_hash(campaign_id + archive_id)}"
        if dry_run:
            counts["promises"] += 1
            continue
        if _scene_exists(kernel, scene_id):
            counts["skipped_existing_scenes"] += 1
            continue
        names = _split_people(row.get("约定双方"))
        participants = participant_ids(names)
        transcript = "\n".join(
            part
            for part in [
                f"档案ID：{archive_id}",
                f"关联编码：{row.get('关联编码') or ''}",
                f"约定主题：{row.get('约定主题') or ''}",
                f"约定双方：{row.get('约定双方') or ''}",
                f"详细内容：{row.get('详细内容') or ''}",
                f"状态：{row.get('状态') or ''}",
                "说明：这是旧战役遗留约定，不是当前新主角亲自作出的承诺。",
            ]
            if str(part).strip()
        )
        kernel.commit_scene(
            scene_id=scene_id,
            campaign_id=campaign_id,
            in_world_time="旧战役约定档案",
            transcript=transcript,
            participants=participants,
            witnesses=participants,
            events=[
                SceneEventInput(
                    event_type="promise",
                    summary=f"旧战役约定：{row.get('约定主题') or ''}。{row.get('详细内容') or ''}",
                    truth_status="canonical",
                    visibility=default_visibility,
                    witness_set=participants,
                    related_entities=participants,
                    source_span=archive_id,
                    importance=_importance(row.get("重要度")),
                    emotional_weight=0.5,
                    payload={"status": row.get("状态"), "source": row.get("信息来源")},
                )
            ],
        )
        counts["promises"] += 1

    # Foreshadowing is GM-only by default.
    for row in foreshadow_rows:
        archive_id = str(row.get("档案ID") or row.get("row_id") or _hash(json.dumps(row, ensure_ascii=False)))
        scene_id = f"legacy_foreshadow_{_id_part(archive_id, 24)}_{_hash(campaign_id + archive_id)}"
        if dry_run:
            counts["foreshadowing"] += 1
            continue
        if _scene_exists(kernel, scene_id):
            counts["skipped_existing_scenes"] += 1
            continue
        transcript = "\n".join(
            part
            for part in [
                f"档案ID：{archive_id}",
                f"关联编码：{row.get('关联编码') or ''}",
                f"伏笔主题：{row.get('伏笔主题') or ''}",
                f"详细内容：{row.get('详细内容') or ''}",
                f"状态：{row.get('状态') or ''}",
                "说明：旧战役伏笔，默认仅 GM 可见。",
            ]
            if str(part).strip()
        )
        kernel.commit_scene(
            scene_id=scene_id,
            campaign_id=campaign_id,
            in_world_time="旧战役伏笔档案",
            transcript=transcript,
            participants=[],
            witnesses=[],
            events=[
                SceneEventInput(
                    event_type="legacy_foreshadowing",
                    summary=f"旧战役伏笔：{row.get('伏笔主题') or ''}。{row.get('详细内容') or ''}",
                    truth_status="canonical",
                    visibility="gm_only",
                    source_span=archive_id,
                    importance=_importance(row.get("重要度")),
                    emotional_weight=0.4,
                    payload={"status": row.get("状态"), "source": row.get("信息来源")},
                )
            ],
        )
        counts["foreshadowing"] += 1

    status = kernel.status() if not dry_run else {"counts": {}}
    if not dry_run:
        counts["scene_records_total"] = status["counts"]["scene_record"]
        counts["memory_items_total"] = status["counts"]["memory_item"]
    return {
        "success": True,
        "source": source_path,
        "campaign_id": campaign_id,
        "dry_run": dry_run,
        "counts": counts,
        "entities": {"characters": character_ids},
    }
