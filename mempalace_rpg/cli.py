"""Command-line interface for the RPG memory kernel.

The CLI is the lowest-friction integration point for host projects: they can
write scene/event JSON, call ``mempalace-rpg commit-scene``, then call
``mempalace-rpg recall`` when building an actor prompt.  No game package needs
to import or vendor this kernel.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .adapter import MempalaceEpisodeAdapter, NullEpisodeAdapter
from .kernel import DEFAULT_RPG_MEMORY_DB, RpgMemoryKernel
from .maintenance import backup, delete_after, delete_scenes, restore, sync_branch
from .models import SceneEventInput
from .tavern_importer import import_taverndb


def _read_json(path: str) -> Any:
    if path == "-":
        return json.loads(sys.stdin.read())
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _kernel(args: argparse.Namespace) -> RpgMemoryKernel:
    adapter = NullEpisodeAdapter()
    palace = getattr(args, "palace", None)
    if palace:
        adapter = MempalaceEpisodeAdapter(palace_path=palace)
    return RpgMemoryKernel(
        db_path=args.db,
        episode_adapter=adapter,
        memo_settings_path=getattr(args, "memo_setting", None),
    )


def cmd_init(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        _print_json({"ok": True, "db": kernel.db_path})
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        _print_json(kernel.status())
    return 0


def cmd_upsert_profile(args: argparse.Namespace) -> int:
    payload = _read_json(args.file)
    with _kernel(args) as kernel:
        character_id = kernel.upsert_character_profile(**payload)
        _print_json({"ok": True, "character_id": character_id})
    return 0


def cmd_commit_scene(args: argparse.Namespace) -> int:
    payload = _read_json(args.file)
    events = [SceneEventInput(**event) for event in payload.pop("events", [])]
    with _kernel(args) as kernel:
        scene_id = kernel.commit_scene(events=events, **payload)
        _print_json({"ok": True, "scene_id": scene_id})
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        pack = kernel.build_memory_pack(
            actor_id=args.actor_id,
            actor_type=args.actor_type,
            query=args.query,
            scene_id=args.scene_id,
            location_id=args.location_id,
            active_quest_ids=args.active_quest,
            in_world_time=args.in_world_time,
            max_chars=args.max_chars,
        )
        if args.json:
            _print_json(
                {
                    "actor_id": pack.actor_id,
                    "actor_type": pack.actor_type,
                    "sections": pack.sections,
                    "evidence": pack.evidence,
                    "forbidden_guard": pack.forbidden_guard,
                }
            )
        else:
            print(pack.render())
    return 0


def cmd_get_scene(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        result = kernel.get_scene_transcript(
            scene_id=args.scene_id,
            actor_id=args.actor_id,
            actor_type=args.actor_type,
            query=args.query,
            mode=args.mode,
            max_chars=args.max_chars,
        )
        if args.json:
            _print_json(result)
        elif result.get("success"):
            if result.get("transcript"):
                print(result["transcript"])
            elif result.get("transcript_excerpt"):
                print(result["transcript_excerpt"])
            else:
                _print_json(result)
        else:
            _print_json(result)
    return 0


def cmd_deep_recall(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        result = kernel.deep_recall(
            actor_id=args.actor_id,
            actor_type=args.actor_type,
            query=args.query,
            scene_id=args.scene_id,
            location_id=args.location_id,
            active_quest_ids=args.active_quest,
            in_world_time=args.in_world_time,
            max_chars=args.max_chars,
            per_scene_chars=args.per_scene_chars,
            scene_limit=args.scene_limit,
        )
        _print_json(result) if args.json else print(result["rendered"])
        if not args.json and result.get("scene_evidence"):
            print("\n## Verbatim Scene Evidence")
            for scene in result["scene_evidence"]:
                if scene.get("transcript_excerpt"):
                    print(f"\n### {scene.get('scene_id')}")
                    print(scene["transcript_excerpt"])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        if args.kind == "facts":
            _print_json(kernel.list_world_facts(subject_id=args.subject_id))
        elif args.kind == "beliefs":
            _print_json(kernel.list_actor_beliefs(actor_id=args.actor_id, subject_id=args.subject_id))
        else:
            _print_json(kernel.list_memory_items(domain=args.domain, owner_scope=args.owner_scope))
    return 0


def cmd_import_taverndb(args: argparse.Namespace) -> int:
    with _kernel(args) as kernel:
        _print_json(
            import_taverndb(
                kernel,
                args.file,
                campaign_id=args.campaign_id,
                default_visibility=args.default_visibility,
                scene_limit=args.scene_limit,
                dry_run=args.dry_run,
            )
        )
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    _print_json(backup(args.db, palace_path=args.palace, backup_dir=args.backup_dir))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    _print_json(
        restore(
            args.db,
            db_backup=args.db_backup,
            palace_path=args.palace,
            palace_backup=args.palace_backup,
            pre_backup=not args.no_pre_backup,
        )
    )
    return 0


def cmd_delete_after(args: argparse.Namespace) -> int:
    _print_json(
        delete_after(
            args.db,
            args.cutoff,
            palace_path=args.palace,
            dry_run=args.dry_run,
            backup_first=not args.no_backup,
        )
    )
    return 0


def _scene_ids_from_args(args: argparse.Namespace) -> list[str]:
    scene_ids = list(args.scene_id or [])
    if args.file:
        payload = _read_json(args.file)
        if isinstance(payload, list):
            scene_ids.extend(str(item) for item in payload)
        elif isinstance(payload, dict):
            raw = payload.get("scene_ids") or payload.get("keep_scene_ids") or []
            scene_ids.extend(str(item) for item in raw)
    return scene_ids


def cmd_delete_scenes(args: argparse.Namespace) -> int:
    _print_json(
        delete_scenes(
            args.db,
            _scene_ids_from_args(args),
            palace_path=args.palace,
            dry_run=args.dry_run,
            backup_first=not args.no_backup,
        )
    )
    return 0


def cmd_sync_branch(args: argparse.Namespace) -> int:
    _print_json(
        sync_branch(
            args.db,
            keep_scene_ids=_scene_ids_from_args(args),
            campaign_id=args.campaign_id,
            branch_scope_id=args.branch_scope_id,
            palace_path=args.palace,
            dry_run=args.dry_run,
            backup_first=not args.no_backup,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mempalace-rpg",
        description="External RPG narrative memory kernel backed by MemPalace-style drawers and SQLite ACL state.",
    )
    parser.add_argument(
        "--db",
        default=DEFAULT_RPG_MEMORY_DB,
        help=f"SQLite RPG memory DB path (default: {DEFAULT_RPG_MEMORY_DB})",
    )
    parser.add_argument(
        "--palace",
        help="Optional MemPalace palace path. When set, verbatim scenes are also written as drawers.",
    )
    parser.add_argument(
        "--memo-setting",
        help="Optional memo_setting.json path for host-owned state conflict controls.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Create/open the RPG memory database.")
    init.set_defaults(func=cmd_init)

    status = sub.add_parser("status", help="Show RPG memory database status and table counts.")
    status.set_defaults(func=cmd_status)

    profile = sub.add_parser("upsert-profile", help="Upsert a character ProfileCard from JSON.")
    profile.add_argument("file", help="JSON file or '-' for stdin.")
    profile.set_defaults(func=cmd_upsert_profile)

    commit = sub.add_parser("commit-scene", help="Commit a scene record and extracted scene events from JSON.")
    commit.add_argument("file", help="JSON file or '-' for stdin.")
    commit.set_defaults(func=cmd_commit_scene)

    recall = sub.add_parser("recall", help="Build an ACL-filtered MemoryPack for an actor.")
    recall.add_argument("--actor-id", required=True)
    recall.add_argument("--actor-type", default="npc", choices=["gm", "npc", "companion", "narrator", "faction_agent", "player"])
    recall.add_argument("--query", required=True)
    recall.add_argument("--scene-id")
    recall.add_argument("--location-id")
    recall.add_argument("--active-quest", action="append", default=[])
    recall.add_argument("--in-world-time")
    recall.add_argument("--max-chars", type=int)
    recall.add_argument("--json", action="store_true", help="Emit structured JSON instead of rendered prompt text.")
    recall.set_defaults(func=cmd_recall)

    get_scene = sub.add_parser("get-scene", help="Fetch an ACL-checked verbatim scene transcript or exact snippets.")
    get_scene.add_argument("--scene-id", required=True)
    get_scene.add_argument("--actor-id", required=True)
    get_scene.add_argument("--actor-type", default="npc", choices=["gm", "npc", "companion", "narrator", "faction_agent", "player"])
    get_scene.add_argument("--query")
    get_scene.add_argument("--mode", default="snippets", choices=["snippets", "full"])
    get_scene.add_argument("--max-chars", type=int, default=4000)
    get_scene.add_argument("--json", action="store_true")
    get_scene.set_defaults(func=cmd_get_scene)

    deep = sub.add_parser("deep-recall", help="Normal MemoryPack plus ACL-checked verbatim snippets for top source scenes.")
    deep.add_argument("--actor-id", required=True)
    deep.add_argument("--actor-type", default="npc", choices=["gm", "npc", "companion", "narrator", "faction_agent", "player"])
    deep.add_argument("--query", required=True)
    deep.add_argument("--scene-id")
    deep.add_argument("--location-id")
    deep.add_argument("--active-quest", action="append", default=[])
    deep.add_argument("--in-world-time")
    deep.add_argument("--max-chars", type=int)
    deep.add_argument("--per-scene-chars", type=int, default=2000)
    deep.add_argument("--scene-limit", type=int, default=3)
    deep.add_argument("--json", action="store_true")
    deep.set_defaults(func=cmd_deep_recall)

    list_cmd = sub.add_parser("list", help="List facts, beliefs, or memory items.")
    list_cmd.add_argument("kind", choices=["facts", "beliefs", "memories"])
    list_cmd.add_argument("--actor-id")
    list_cmd.add_argument("--subject-id")
    list_cmd.add_argument("--domain")
    list_cmd.add_argument("--owner-scope")
    list_cmd.set_defaults(func=cmd_list)

    tavern = sub.add_parser("import-taverndb", help="Import SillyTavern ChatSheets/TavernDB JSON as legacy past-campaign memory.")
    tavern.add_argument("file", help="TavernDB JSON file.")
    tavern.add_argument("--campaign-id", help="Legacy campaign id. Default derived from filename.")
    tavern.add_argument("--default-visibility", default="witnessed_only", choices=["witnessed_only", "gm_only", "public_world", "party_only"], help="Visibility for imported timeline/promise memories. Default: witnessed_only.")
    tavern.add_argument("--scene-limit", type=int, help="Import at most N timeline rows (useful for testing).")
    tavern.add_argument("--dry-run", action="store_true", help="Parse and count without writing.")
    tavern.set_defaults(func=cmd_import_taverndb)

    backup_cmd = sub.add_parser("backup", help="Copy SQLite DB and optional palace directory to a timestamped backup.")
    backup_cmd.add_argument("--backup-dir", help="Directory for backups. Default: <db-dir>/backups.")
    backup_cmd.set_defaults(func=cmd_backup)

    restore_cmd = sub.add_parser("restore", help="Restore SQLite DB and optionally palace directory from backups.")
    restore_cmd.add_argument("--db-backup", required=True, help="SQLite backup file to restore from.")
    restore_cmd.add_argument("--palace-backup", help="Palace backup directory to restore from. Requires --palace target.")
    restore_cmd.add_argument("--no-pre-backup", action="store_true", help="Do not create a safety backup before restoring.")
    restore_cmd.set_defaults(func=cmd_restore)

    delete_after_cmd = sub.add_parser("delete-after", help="Rollback by deleting all RPG memory created after a system timestamp.")
    delete_after_cmd.add_argument("cutoff", help="System timestamp cutoff, e.g. 2026-05-30T20:29:55+00:00. Deletes rows with created_at > cutoff.")
    delete_after_cmd.add_argument("--dry-run", action="store_true", help="Preview what would be deleted without changing data.")
    delete_after_cmd.add_argument("--no-backup", action="store_true", help="Do not create a safety backup before deleting.")
    delete_after_cmd.set_defaults(func=cmd_delete_after)

    delete_scenes_cmd = sub.add_parser("delete-scenes", help="Delete exact scene ids from SQLite and optional Palace drawers.")
    delete_scenes_cmd.add_argument("file", nargs="?", help="JSON list of scene ids, object with scene_ids, or '-' for stdin.")
    delete_scenes_cmd.add_argument("--scene-id", action="append", default=[], help="Scene id to delete. May be repeated.")
    delete_scenes_cmd.add_argument("--dry-run", action="store_true")
    delete_scenes_cmd.add_argument("--no-backup", action="store_true")
    delete_scenes_cmd.set_defaults(func=cmd_delete_scenes)

    sync_branch_cmd = sub.add_parser("sync-branch", help="Delete auto-commit scenes not present in the current pi branch ledger.")
    sync_branch_cmd.add_argument("file", nargs="?", help="JSON list of keep scene ids, object with keep_scene_ids/scene_ids, or '-' for stdin.")
    sync_branch_cmd.add_argument("--scene-id", action="append", default=[], help="Scene id to keep. May be repeated.")
    sync_branch_cmd.add_argument("--campaign-id", help="Only sync auto-commit scenes from this campaign.")
    sync_branch_cmd.add_argument("--branch-scope-id", help="Only sync scenes written by this pi session/scope id.")
    sync_branch_cmd.add_argument("--dry-run", action="store_true")
    sync_branch_cmd.add_argument("--no-backup", action="store_true")
    sync_branch_cmd.set_defaults(func=cmd_sync_branch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
