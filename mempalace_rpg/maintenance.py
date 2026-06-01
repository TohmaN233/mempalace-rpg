"""Backup, restore, and time-based rollback helpers for RPG memory."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _backup_path(path: str | Path, backup_dir: str | Path | None, stamp: str) -> Path:
    source = Path(path)
    root = Path(backup_dir) if backup_dir else source.parent / "backups"
    return root / f"{source.name}.bak-{stamp}"


def backup(db_path: str, *, palace_path: str | None = None, backup_dir: str | None = None) -> dict[str, Any]:
    stamp = _stamp()
    db = Path(db_path)
    if not db.exists():
        raise FileNotFoundError(f"RPG memory DB not found: {db}")
    db_target = _backup_path(db, backup_dir, stamp)
    db_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db, db_target)

    palace_target = None
    if palace_path:
        palace = Path(palace_path)
        if palace.exists():
            palace_target = _backup_path(palace, backup_dir, stamp)
            if palace_target.exists():
                shutil.rmtree(palace_target)
            shutil.copytree(palace, palace_target)

    return {
        "success": True,
        "stamp": stamp,
        "db_backup": str(db_target),
        "palace_backup": str(palace_target) if palace_target else None,
    }


def restore(
    db_path: str,
    *,
    db_backup: str,
    palace_path: str | None = None,
    palace_backup: str | None = None,
    pre_backup: bool = True,
) -> dict[str, Any]:
    before = backup(db_path, palace_path=palace_path) if pre_backup and Path(db_path).exists() else None
    db_source = Path(db_backup)
    if not db_source.exists():
        raise FileNotFoundError(f"DB backup not found: {db_source}")
    db_target = Path(db_path)
    db_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_source, db_target)

    restored_palace = None
    if palace_path and palace_backup:
        palace_source = Path(palace_backup)
        if not palace_source.exists():
            raise FileNotFoundError(f"Palace backup not found: {palace_source}")
        palace_target = Path(palace_path)
        if palace_target.exists():
            shutil.rmtree(palace_target)
        shutil.copytree(palace_source, palace_target)
        restored_palace = str(palace_target)

    return {
        "success": True,
        "db_restored_from": str(db_source),
        "db_restored_to": str(db_target),
        "palace_restored_to": restored_palace,
        "pre_restore_backup": before,
    }


def preview_delete_after(db_path: str, cutoff: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    try:
        scene_rows = conn.execute(
            "SELECT scene_id, created_at, in_world_time, substr(transcript, 1, 160) FROM scene_record WHERE created_at > ? ORDER BY created_at",
            (cutoff,),
        ).fetchall()
        scene_ids = [row[0] for row in scene_rows]
        event_ids: list[str] = []
        if scene_ids:
            q = ",".join("?" for _ in scene_ids)
            event_ids = [
                row[0]
                for row in conn.execute(
                    f"SELECT event_id FROM scene_event WHERE scene_id IN ({q})",
                    scene_ids,
                ).fetchall()
            ]
        counts = {"scene_record": len(scene_ids), "scene_event": len(event_ids)}
        if scene_ids:
            q = ",".join("?" for _ in scene_ids)
            counts["memory_item"] = conn.execute(
                f"SELECT COUNT(*) FROM memory_item WHERE source_scene_id IN ({q})",
                scene_ids,
            ).fetchone()[0]
        else:
            counts["memory_item"] = 0
        if event_ids:
            q = ",".join("?" for _ in event_ids)
            counts["world_fact"] = conn.execute(
                f"SELECT COUNT(*) FROM world_fact WHERE source_event_id IN ({q})",
                event_ids,
            ).fetchone()[0]
            counts["actor_belief"] = conn.execute(
                f"SELECT COUNT(*) FROM actor_belief WHERE source_event_id IN ({q})",
                event_ids,
            ).fetchone()[0]
        else:
            counts["world_fact"] = 0
            counts["actor_belief"] = 0
        return {
            "success": True,
            "cutoff": cutoff,
            "counts": counts,
            "scenes": [
                {
                    "scene_id": row[0],
                    "created_at": row[1],
                    "in_world_time": row[2],
                    "transcript_excerpt": row[3],
                }
                for row in scene_rows[:50]
            ],
            "scene_count_shown": min(len(scene_rows), 50),
            "scene_count_total": len(scene_rows),
        }
    finally:
        conn.close()


def _delete_scene_ids(
    db_path: str,
    scene_ids: list[str],
    *,
    palace_path: str | None = None,
) -> dict[str, Any]:
    if not scene_ids:
        return {
            "scene_record": 0,
            "scene_event": 0,
            "memory_item": 0,
            "world_fact": 0,
            "actor_belief": 0,
            "relationship_state": 0,
            "palace_drawers_deleted": [],
        }

    conn = sqlite3.connect(db_path)
    counts = {
        "scene_record": len(scene_ids),
        "scene_event": 0,
        "memory_item": 0,
        "world_fact": 0,
        "actor_belief": 0,
        "relationship_state": 0,
    }
    palace_drawer_ids = [f"rpg_scene_{scene_id}" for scene_id in scene_ids]
    try:
        q_scenes = ",".join("?" for _ in scene_ids)
        event_ids = [
            row[0]
            for row in conn.execute(
                f"SELECT event_id FROM scene_event WHERE scene_id IN ({q_scenes})",
                scene_ids,
            ).fetchall()
        ]
        counts["scene_event"] = len(event_ids)
        with conn:
            if event_ids:
                q_events = ",".join("?" for _ in event_ids)
                counts["actor_belief"] = conn.execute(
                    f"SELECT COUNT(*) FROM actor_belief WHERE source_event_id IN ({q_events})",
                    event_ids,
                ).fetchone()[0]
                counts["world_fact"] = conn.execute(
                    f"SELECT COUNT(*) FROM world_fact WHERE source_event_id IN ({q_events})",
                    event_ids,
                ).fetchone()[0]
                counts["relationship_state"] = conn.execute(
                    f"SELECT COUNT(*) FROM relationship_state WHERE evidence_event_id IN ({q_events})",
                    event_ids,
                ).fetchone()[0]
                counts["memory_item"] = conn.execute(
                    f"SELECT COUNT(*) FROM memory_item WHERE source_event_id IN ({q_events}) OR source_scene_id IN ({q_scenes})",
                    [*event_ids, *scene_ids],
                ).fetchone()[0]
                conn.execute(f"DELETE FROM actor_belief WHERE source_event_id IN ({q_events})", event_ids)
                conn.execute(f"DELETE FROM world_fact WHERE source_event_id IN ({q_events})", event_ids)
                conn.execute(f"DELETE FROM relationship_state WHERE evidence_event_id IN ({q_events})", event_ids)
                conn.execute(
                    f"DELETE FROM memory_item WHERE source_event_id IN ({q_events}) OR source_scene_id IN ({q_scenes})",
                    [*event_ids, *scene_ids],
                )
                conn.execute(f"DELETE FROM scene_event WHERE event_id IN ({q_events})", event_ids)
            else:
                counts["memory_item"] = conn.execute(
                    f"SELECT COUNT(*) FROM memory_item WHERE source_scene_id IN ({q_scenes})",
                    scene_ids,
                ).fetchone()[0]
                conn.execute(f"DELETE FROM memory_item WHERE source_scene_id IN ({q_scenes})", scene_ids)
            conn.execute(f"DELETE FROM scene_record WHERE scene_id IN ({q_scenes})", scene_ids)
    finally:
        conn.close()

    palace_deleted: list[str] = []
    if palace_path and palace_drawer_ids and Path(palace_path).exists():
        from mempalace.palace import get_collection

        col = get_collection(palace_path, create=False)
        found = col.get(ids=palace_drawer_ids).get("ids", [])
        if found:
            col.delete(ids=found)
        palace_deleted = list(found)
    counts["palace_drawers_deleted"] = palace_deleted
    return counts


def delete_scenes(
    db_path: str,
    scene_ids: list[str],
    *,
    palace_path: str | None = None,
    dry_run: bool = False,
    backup_first: bool = True,
) -> dict[str, Any]:
    scene_ids = sorted({scene_id for scene_id in scene_ids if scene_id})
    if dry_run:
        preview = preview_scene_ids(db_path, scene_ids)
        preview["dry_run"] = True
        return preview
    before = backup(db_path, palace_path=palace_path) if backup_first else None
    deleted = _delete_scene_ids(db_path, scene_ids, palace_path=palace_path)
    return {"success": True, "scene_ids": scene_ids, "deleted": deleted, "backup": before}


def preview_scene_ids(db_path: str, scene_ids: list[str]) -> dict[str, Any]:
    scene_ids = sorted({scene_id for scene_id in scene_ids if scene_id})
    if not scene_ids:
        return {"success": True, "scene_ids": [], "counts": {"scene_record": 0}, "scenes": []}
    conn = sqlite3.connect(db_path)
    try:
        q = ",".join("?" for _ in scene_ids)
        rows = conn.execute(
            f"SELECT scene_id, created_at, in_world_time, substr(transcript, 1, 160) FROM scene_record WHERE scene_id IN ({q}) ORDER BY created_at",
            scene_ids,
        ).fetchall()
        found = [row[0] for row in rows]
        missing = [scene_id for scene_id in scene_ids if scene_id not in set(found)]
        return {
            "success": True,
            "scene_ids": found,
            "missing_scene_ids": missing,
            "counts": {"scene_record": len(found)},
            "scenes": [
                {
                    "scene_id": row[0],
                    "created_at": row[1],
                    "in_world_time": row[2],
                    "transcript_excerpt": row[3],
                }
                for row in rows
            ],
        }
    finally:
        conn.close()


def _auto_commit_scene_ids(
    db_path: str,
    campaign_id: str | None = None,
    branch_scope_id: str | None = None,
) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        params: list[str] = []
        where = "WHERE (se.payload_json LIKE '%\"auto_commit\": true%' OR se.event_type='scene_transcript')"
        if campaign_id:
            where += " AND sr.campaign_id=?"
            params.append(campaign_id)
        if branch_scope_id:
            where += " AND se.payload_json LIKE ?"
            params.append(f'%"branch_scope_id": "{branch_scope_id}"%')
        rows = conn.execute(
            f"""
            SELECT DISTINCT sr.scene_id
            FROM scene_record sr
            JOIN scene_event se ON se.scene_id = sr.scene_id
            {where}
            ORDER BY sr.created_at
            """,
            params,
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()


def sync_branch(
    db_path: str,
    *,
    keep_scene_ids: list[str],
    campaign_id: str | None = None,
    branch_scope_id: str | None = None,
    palace_path: str | None = None,
    dry_run: bool = False,
    backup_first: bool = True,
) -> dict[str, Any]:
    keep = {scene_id for scene_id in keep_scene_ids if scene_id}
    existing = _auto_commit_scene_ids(db_path, campaign_id=campaign_id, branch_scope_id=branch_scope_id)
    to_delete = [scene_id for scene_id in existing if scene_id not in keep]
    if dry_run:
        preview = preview_scene_ids(db_path, to_delete)
        preview.update(
            {
                "dry_run": True,
                "campaign_id": campaign_id,
                "branch_scope_id": branch_scope_id,
                "kept_scene_count": len(keep),
                "auto_commit_scene_count": len(existing),
                "delete_scene_count": len(to_delete),
            }
        )
        return preview
    before = backup(db_path, palace_path=palace_path) if backup_first and to_delete else None
    deleted = _delete_scene_ids(db_path, to_delete, palace_path=palace_path)
    return {
        "success": True,
        "campaign_id": campaign_id,
        "branch_scope_id": branch_scope_id,
        "kept_scene_count": len(keep),
        "auto_commit_scene_count": len(existing),
        "delete_scene_count": len(to_delete),
        "scene_ids_deleted": to_delete,
        "deleted": deleted,
        "backup": before,
    }


def delete_after(
    db_path: str,
    cutoff: str,
    *,
    palace_path: str | None = None,
    dry_run: bool = False,
    backup_first: bool = True,
) -> dict[str, Any]:
    preview = preview_delete_after(db_path, cutoff)
    if dry_run:
        preview["dry_run"] = True
        return preview

    before = backup(db_path, palace_path=palace_path) if backup_first else None
    conn = sqlite3.connect(db_path)
    try:
        scene_ids = [
            row[0]
            for row in conn.execute(
                "SELECT scene_id FROM scene_record WHERE created_at > ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        ]
    finally:
        conn.close()
    if not scene_ids:
        return {"success": True, "cutoff": cutoff, "deleted": preview["counts"], "backup": before}
    deleted = _delete_scene_ids(db_path, scene_ids, palace_path=palace_path)
    return {
        "success": True,
        "cutoff": cutoff,
        "deleted": deleted,
        "backup": before,
    }


def dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
