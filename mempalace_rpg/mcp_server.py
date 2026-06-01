"""MCP server for the external RPG memory kernel.

This server is intentionally separate from ``mempalace-mcp``.  Normal
MemPalace remains a coding/project memory tool; ``mempalace-rpg-mcp`` exposes
RPG-specific scene commit and actor recall tools that game agents can call.
"""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Any

__version__ = "0.1.0"

from .adapter import MempalaceEpisodeAdapter, NullEpisodeAdapter
from .kernel import DEFAULT_RPG_MEMORY_DB, RpgMemoryKernel
from .models import SceneEventInput
from .tavern_importer import import_taverndb

logger = logging.getLogger("mempalace_rpg_mcp")

SUPPORTED_PROTOCOL_VERSIONS = [
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
]


@dataclass
class ServerConfig:
    db_path: str = os.environ.get("MEMPALACE_RPG_DB", DEFAULT_RPG_MEMORY_DB)
    palace_path: str | None = os.environ.get("MEMPALACE_RPG_PALACE") or None
    memo_settings_path: str | None = os.environ.get("MEMPALACE_RPG_MEMO_SETTING") or None


_config = ServerConfig()


def configure(
    *,
    db_path: str | None = None,
    palace_path: str | None = None,
    memo_settings_path: str | None = None,
) -> None:
    """Configure the module-level server target.

    Tests and embedders use this directly.  The CLI entry point configures it
    from ``--db`` / ``--palace`` before entering the JSON-RPC loop.
    """

    if db_path is not None:
        _config.db_path = db_path
    _config.palace_path = palace_path
    _config.memo_settings_path = memo_settings_path


def _kernel() -> RpgMemoryKernel:
    adapter = NullEpisodeAdapter()
    if _config.palace_path:
        adapter = MempalaceEpisodeAdapter(_config.palace_path)
    return RpgMemoryKernel(
        db_path=_config.db_path,
        episode_adapter=adapter,
        memo_settings_path=_config.memo_settings_path,
    )


def tool_init() -> dict[str, Any]:
    with _kernel() as kernel:
        return {
            "success": True,
            "db": kernel.db_path,
            "palace": _config.palace_path,
            "memo_setting": _config.memo_settings_path,
        }


def tool_status() -> dict[str, Any]:
    with _kernel() as kernel:
        return kernel.status()


def tool_upsert_profile(
    *,
    character_id: str,
    display_name: str,
    tier: str,
    short_persona: str,
    memory_wing: str,
    public_role: str | None = None,
    private_role: str | None = None,
    speech_style: str | None = None,
    personality_tags: list[str] | None = None,
    core_values: list[str] | None = None,
    current_goal: str | None = None,
    core_fear: str | None = None,
    faction_id: str | None = None,
    home_location_id: str | None = None,
    promotable: bool = True,
) -> dict[str, Any]:
    with _kernel() as kernel:
        cid = kernel.upsert_character_profile(
            character_id=character_id,
            display_name=display_name,
            tier=tier,
            short_persona=short_persona,
            memory_wing=memory_wing,
            public_role=public_role,
            private_role=private_role,
            speech_style=speech_style,
            personality_tags=personality_tags,
            core_values=core_values,
            current_goal=current_goal,
            core_fear=core_fear,
            faction_id=faction_id,
            home_location_id=home_location_id,
            promotable=promotable,
        )
        return {"success": True, "character_id": cid}


def tool_commit_scene(
    *,
    campaign_id: str,
    in_world_time: str,
    transcript: str,
    location_id: str | None = None,
    active_quest_ids: list[str] | None = None,
    participants: list[str] | None = None,
    witnesses: list[str] | None = None,
    events: list[dict[str, Any]] | None = None,
    scene_id: str | None = None,
) -> dict[str, Any]:
    event_inputs = [SceneEventInput(**event) for event in (events or [])]
    with _kernel() as kernel:
        sid = kernel.commit_scene(
            campaign_id=campaign_id,
            in_world_time=in_world_time,
            transcript=transcript,
            location_id=location_id,
            active_quest_ids=active_quest_ids,
            participants=participants,
            witnesses=witnesses,
            events=event_inputs,
            scene_id=scene_id,
        )
        return {"success": True, "scene_id": sid, "events_committed": len(event_inputs)}


def tool_recall(
    *,
    actor_id: str,
    query: str,
    actor_type: str = "npc",
    scene_id: str | None = None,
    location_id: str | None = None,
    active_quest_ids: list[str] | None = None,
    in_world_time: str | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    with _kernel() as kernel:
        pack = kernel.build_memory_pack(
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            scene_id=scene_id,
            location_id=location_id,
            active_quest_ids=active_quest_ids or [],
            in_world_time=in_world_time,
            max_chars=max_chars,
        )
        return {
            "success": True,
            "actor_id": pack.actor_id,
            "actor_type": pack.actor_type,
            "rendered": pack.render(),
            "sections": pack.sections,
            "evidence": pack.evidence,
            "forbidden_guard": pack.forbidden_guard,
        }


def tool_get_scene(
    *,
    scene_id: str,
    actor_id: str,
    actor_type: str = "npc",
    query: str | None = None,
    mode: str = "snippets",
    max_chars: int | None = 4000,
) -> dict[str, Any]:
    with _kernel() as kernel:
        return kernel.get_scene_transcript(
            scene_id=scene_id,
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            mode=mode,
            max_chars=max_chars,
        )


def tool_deep_recall(
    *,
    actor_id: str,
    query: str,
    actor_type: str = "npc",
    scene_id: str | None = None,
    location_id: str | None = None,
    active_quest_ids: list[str] | None = None,
    in_world_time: str | None = None,
    max_chars: int | None = None,
    per_scene_chars: int = 2000,
    scene_limit: int = 3,
) -> dict[str, Any]:
    with _kernel() as kernel:
        return kernel.deep_recall(
            actor_id=actor_id,
            actor_type=actor_type,
            query=query,
            scene_id=scene_id,
            location_id=location_id,
            active_quest_ids=active_quest_ids or [],
            in_world_time=in_world_time,
            max_chars=max_chars,
            per_scene_chars=per_scene_chars,
            scene_limit=scene_limit,
        )


def tool_list_memories(
    *,
    domain: str | None = None,
    owner_scope: str | None = None,
) -> dict[str, Any]:
    with _kernel() as kernel:
        memories = kernel.list_memory_items(domain=domain, owner_scope=owner_scope)
        return {"success": True, "count": len(memories), "memories": memories}


def tool_list_world_facts(*, subject_id: str | None = None) -> dict[str, Any]:
    with _kernel() as kernel:
        facts = kernel.list_world_facts(subject_id=subject_id)
        return {"success": True, "count": len(facts), "facts": facts}


def tool_list_actor_beliefs(
    *,
    actor_id: str | None = None,
    subject_id: str | None = None,
) -> dict[str, Any]:
    with _kernel() as kernel:
        beliefs = kernel.list_actor_beliefs(actor_id=actor_id, subject_id=subject_id)
        return {"success": True, "count": len(beliefs), "beliefs": beliefs}


def tool_import_taverndb(
    *,
    file_path: str,
    campaign_id: str | None = None,
    default_visibility: str = "witnessed_only",
    scene_limit: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    with _kernel() as kernel:
        return import_taverndb(
            kernel,
            file_path,
            campaign_id=campaign_id,
            default_visibility=default_visibility,
            scene_limit=scene_limit,
            dry_run=dry_run,
        )


TOOLS = {
    "mempalace_rpg_init": {
        "description": "Initialize/open the external RPG memory database.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_init,
    },
    "mempalace_rpg_status": {
        "description": "Return counts for profiles, scenes, events, memories, world facts, and actor beliefs.",
        "input_schema": {"type": "object", "properties": {}},
        "handler": tool_status,
    },
    "mempalace_rpg_upsert_profile": {
        "description": "Create/update a character ProfileCard (L0 identity), without writing scene history.",
        "input_schema": {
            "type": "object",
            "properties": {
                "character_id": {"type": "string"},
                "display_name": {"type": "string"},
                "tier": {"type": "string", "enum": ["core", "major", "recurring", "ambient"]},
                "short_persona": {"type": "string"},
                "memory_wing": {"type": "string"},
                "public_role": {"type": "string"},
                "private_role": {"type": "string"},
                "speech_style": {"type": "string"},
                "personality_tags": {"type": "array", "items": {"type": "string"}},
                "core_values": {"type": "array", "items": {"type": "string"}},
                "current_goal": {"type": "string"},
                "core_fear": {"type": "string"},
                "faction_id": {"type": "string"},
                "home_location_id": {"type": "string"},
                "promotable": {"type": "boolean"},
            },
            "required": [
                "character_id",
                "display_name",
                "tier",
                "short_persona",
                "memory_wing",
            ],
        },
        "handler": tool_upsert_profile,
    },
    "mempalace_rpg_commit_scene": {
        "description": "Commit a scene transcript and extracted scene events. This is the write path after a scene/turn ends; ACL metadata is stored before later recall.",
        "input_schema": {
            "type": "object",
            "properties": {
                "campaign_id": {"type": "string"},
                "in_world_time": {"type": "string"},
                "transcript": {"type": "string"},
                "location_id": {"type": "string"},
                "active_quest_ids": {"type": "array", "items": {"type": "string"}},
                "participants": {"type": "array", "items": {"type": "string"}},
                "witnesses": {"type": "array", "items": {"type": "string"}},
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "event_type": {"type": "string"},
                            "summary": {"type": "string"},
                            "actor_id": {"type": "string"},
                            "target_id": {"type": "string"},
                            "truth_status": {"type": "string"},
                            "visibility": {"type": "string"},
                            "witness_set": {"type": "array", "items": {"type": "string"}},
                            "related_entities": {"type": "array", "items": {"type": "string"}},
                            "related_quests": {"type": "array", "items": {"type": "string"}},
                            "related_locations": {"type": "array", "items": {"type": "string"}},
                            "source_span": {"type": "string"},
                            "emotional_weight": {"type": "number"},
                            "importance": {"type": "number"},
                            "payload": {"type": "object"},
                        },
                        "required": ["event_type", "summary"],
                    },
                },
                "scene_id": {"type": "string"},
            },
            "required": ["campaign_id", "in_world_time", "transcript"],
        },
        "handler": tool_commit_scene,
    },
    "mempalace_rpg_recall": {
        "description": "Build an ACL-filtered MemoryPack for one actor. Call before roleplaying an NPC/GM/faction/location narrator, using the actor_id and current player query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_id": {"type": "string"},
                "actor_type": {
                    "type": "string",
                    "enum": ["gm", "npc", "companion", "narrator", "faction_agent", "player"],
                },
                "query": {"type": "string"},
                "scene_id": {"type": "string"},
                "location_id": {"type": "string"},
                "active_quest_ids": {"type": "array", "items": {"type": "string"}},
                "in_world_time": {"type": "string"},
                "max_chars": {"type": "integer"},
            },
            "required": ["actor_id", "query"],
        },
        "handler": tool_recall,
    },
    "mempalace_rpg_get_scene": {
        "description": "Fetch an ACL-checked verbatim scene transcript or exact snippets by scene_id. Use only after normal recall evidence points to a scene and exact wording matters.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scene_id": {"type": "string"},
                "actor_id": {"type": "string"},
                "actor_type": {
                    "type": "string",
                    "enum": ["gm", "npc", "companion", "narrator", "faction_agent", "player"],
                },
                "query": {"type": "string"},
                "mode": {"type": "string", "enum": ["snippets", "full"]},
                "max_chars": {"type": "integer"},
            },
            "required": ["scene_id", "actor_id"],
        },
        "handler": tool_get_scene,
    },
    "mempalace_rpg_deep_recall": {
        "description": "Two-stage recall: build the normal ACL-filtered MemoryPack, then fetch ACL-checked verbatim snippets for the top source scenes. Use when summaries are insufficient and exact old wording/details matter.",
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_id": {"type": "string"},
                "actor_type": {
                    "type": "string",
                    "enum": ["gm", "npc", "companion", "narrator", "faction_agent", "player"],
                },
                "query": {"type": "string"},
                "scene_id": {"type": "string"},
                "location_id": {"type": "string"},
                "active_quest_ids": {"type": "array", "items": {"type": "string"}},
                "in_world_time": {"type": "string"},
                "max_chars": {"type": "integer"},
                "per_scene_chars": {"type": "integer"},
                "scene_limit": {"type": "integer"},
            },
            "required": ["actor_id", "query"],
        },
        "handler": tool_deep_recall,
    },
    "mempalace_rpg_list_memories": {
        "description": "Debug/list memory projections by domain and owner scope.",
        "input_schema": {
            "type": "object",
            "properties": {
                "domain": {"type": "string"},
                "owner_scope": {"type": "string"},
            },
        },
        "handler": tool_list_memories,
    },
    "mempalace_rpg_list_world_facts": {
        "description": "Debug/list canonical world facts. Rumors should not appear here.",
        "input_schema": {
            "type": "object",
            "properties": {"subject_id": {"type": "string"}},
        },
        "handler": tool_list_world_facts,
    },
    "mempalace_rpg_list_actor_beliefs": {
        "description": "Debug/list actor beliefs, including rumors and subjective knowledge.",
        "input_schema": {
            "type": "object",
            "properties": {
                "actor_id": {"type": "string"},
                "subject_id": {"type": "string"},
            },
        },
        "handler": tool_list_actor_beliefs,
    },
    "mempalace_rpg_import_taverndb": {
        "description": "One-time migration: import a SillyTavern ChatSheets/TavernDB JSON export as legacy past-campaign memory. Former protagonists become legacy characters, not the current player.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to TavernDB JSON export."},
                "campaign_id": {"type": "string", "description": "Optional legacy campaign id."},
                "default_visibility": {"type": "string", "description": "witnessed_only|gm_only|public_world|party_only. Default witnessed_only."},
                "scene_limit": {"type": "integer", "description": "Optional max timeline rows to import."},
                "dry_run": {"type": "boolean", "description": "Parse/count without writing."},
            },
            "required": ["file_path"],
        },
        "handler": tool_import_taverndb,
    },
}

# Short aliases make bridged tool names pleasant in clients that already prefix
# them by server name (for example pi-mcp-extension: mcp_rpg_recall instead of
# mcp_rpg_mempalace_rpg_recall).  The long names remain for generic MCP clients
# and backwards compatibility.
_ALIASES = {
    "init": "mempalace_rpg_init",
    "status": "mempalace_rpg_status",
    "upsert_profile": "mempalace_rpg_upsert_profile",
    "commit_scene": "mempalace_rpg_commit_scene",
    "recall": "mempalace_rpg_recall",
    "get_scene": "mempalace_rpg_get_scene",
    "deep_recall": "mempalace_rpg_deep_recall",
    "list_memories": "mempalace_rpg_list_memories",
    "list_world_facts": "mempalace_rpg_list_world_facts",
    "list_actor_beliefs": "mempalace_rpg_list_actor_beliefs",
    "import_taverndb": "mempalace_rpg_import_taverndb",
}
for _alias, _canonical in _ALIASES.items():
    _tool = dict(TOOLS[_canonical])
    _tool["description"] = f"Alias for {_canonical}. " + str(_tool["description"])
    TOOLS[_alias] = _tool


def _internal_tool_error(req_id: Any, tool_name: str, exc: BaseException) -> dict[str, Any]:
    logger.exception("Tool error in %s", tool_name)
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": -32000,
            "message": "Internal tool error",
            "data": {"error_class": type(exc).__name__, "message": str(exc)},
        },
    }


def _validate_and_prepare_args(tool_name: str, tool_args: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(tool_args, dict):
        return None, {"code": -32602, "message": f"Arguments for tool {tool_name} must be an object"}

    schema = TOOLS[tool_name]["input_schema"]
    schema_props = schema.get("properties", {})
    unknown = [k for k in tool_args if k not in schema_props]
    if unknown:
        quoted = ", ".join(f"'{k}'" for k in unknown)
        word = "parameter" if len(unknown) == 1 else "parameters"
        return None, {
            "code": -32602,
            "message": f"Unknown {word} {quoted} for tool {tool_name}",
        }

    required = schema.get("required", [])
    missing = [k for k in required if k not in tool_args]
    if missing:
        quoted = ", ".join(f"'{k}'" for k in missing)
        word = "parameter" if len(missing) == 1 else "parameters"
        return None, {
            "code": -32602,
            "message": f"Missing required {word} {quoted} for tool {tool_name}",
        }

    prepared = {k: v for k, v in tool_args.items() if k in schema_props}
    for key, value in list(prepared.items()):
        declared = schema_props.get(key, {}).get("type")
        try:
            if declared == "integer" and not isinstance(value, int):
                prepared[key] = int(value)
            elif declared == "number" and not isinstance(value, (int, float)):
                prepared[key] = float(value)
        except (TypeError, ValueError):
            return None, {"code": -32602, "message": f"Invalid value for parameter '{key}'"}
    return prepared, None


def handle_request(request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request"},
        }

    method = request.get("method") or ""
    params = request.get("params") or {}
    req_id = request.get("id")

    if method == "initialize":
        client_version = params.get("protocolVersion", SUPPORTED_PROTOCOL_VERSIONS[-1])
        negotiated = (
            client_version
            if client_version in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": negotiated,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mempalace-rpg", "version": __version__},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method.startswith("notifications/"):
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {"name": name, "description": tool["description"], "inputSchema": tool["input_schema"]}
                    for name, tool in TOOLS.items()
                ]
            },
        }
    if method == "tools/call":
        if not isinstance(params, dict) or "name" not in params:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "Invalid params: 'name' is required for tools/call"},
            }
        tool_name = params.get("name")
        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        tool_args, validation_error = _validate_and_prepare_args(
            tool_name,
            params.get("arguments") or {},
        )
        if validation_error is not None:
            return {"jsonrpc": "2.0", "id": req_id, "error": validation_error}
        try:
            handler = TOOLS[tool_name]["handler"]
            if not any(
                p.kind == inspect.Parameter.VAR_KEYWORD
                for p in inspect.signature(handler).parameters.values()
            ):
                result = handler(**tool_args)
            else:
                result = handler(**(tool_args or {}))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(result, indent=2, ensure_ascii=False)}
                    ]
                },
            }
        except TypeError as exc:
            # If the handler itself reports a missing argument, keep the MCP
            # error user-facing; otherwise hide implementation details.
            message = str(exc)
            if re.search(r"missing .*required", message):
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": message},
                }
            return _internal_tool_error(req_id, tool_name, exc)
        except Exception as exc:
            return _internal_tool_error(req_id, tool_name, exc)

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MemPalace RPG Memory MCP Server")
    parser.add_argument(
        "--db",
        default=os.environ.get("MEMPALACE_RPG_DB", DEFAULT_RPG_MEMORY_DB),
        help=f"SQLite RPG memory DB path (default: {DEFAULT_RPG_MEMORY_DB})",
    )
    parser.add_argument(
        "--palace",
        default=os.environ.get("MEMPALACE_RPG_PALACE") or None,
        help="Optional MemPalace palace path for verbatim drawer writes.",
    )
    parser.add_argument(
        "--memo-setting",
        default=os.environ.get("MEMPALACE_RPG_MEMO_SETTING") or None,
        help="Optional memo_setting.json path for host-owned state conflict controls.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    configure(db_path=args.db, palace_path=args.palace, memo_settings_path=args.memo_setting)
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass
    logger.info(
        "MemPalace RPG MCP Server starting (db=%s, palace=%s, memo_setting=%s)",
        _config.db_path,
        _config.palace_path,
        _config.memo_settings_path,
    )
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            response = handle_request(json.loads(line))
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except Exception as exc:  # noqa: BLE001 - MCP loop must stay alive
            logger.error("Server error: %s", exc)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
