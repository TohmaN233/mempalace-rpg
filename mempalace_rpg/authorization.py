"""Single fail-closed authorization boundary for evidence source events."""
from __future__ import annotations
import json
import sqlite3
from dataclasses import dataclass
from typing import Any

CANONICAL_TRUTH_STATUSES = {"canonical", "observed", "reported", "rumor", "uncertain"}
VALID_TRUTH_STATUSES = CANONICAL_TRUTH_STATUSES | {"belief", "retconned", "abandoned"}
VALID_VISIBILITIES = {"public_world", "party_only", "witnessed_only", "character_private", "faction_private", "quest_participants", "gm_only", "rumor_public", "retconned"}
VALID_BRANCH_STATUSES = {"active", "retconned", "abandoned"}
_SCOPE_KIND = {"faction_private": "faction", "quest_participants": "quest", "party_only": "party"}

class SecurityMetadataError(ValueError): pass

def _security_list(value: str | None, name: str) -> list[str]:
    try: parsed = json.loads(value) if value is not None else None
    except json.JSONDecodeError as exc: raise SecurityMetadataError(f"invalid_{name}_json") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) and bool(item.strip()) and item == item.strip()
        for item in parsed
    ):
        raise SecurityMetadataError(f"invalid_{name}_json")
    return parsed


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _acl_metadata_valid(event: dict[str, Any]) -> bool:
    owner, scope, belief_owner = event.get("access_owner_id"), event.get("access_scope_id"), event.get("belief_owner_id")
    visibility, truth = event.get("visibility"), event.get("truth_status")
    if visibility == "character_private":
        access_ok = _nonempty(owner) and scope is None
    elif visibility in _SCOPE_KIND:
        access_ok = _nonempty(scope) and owner is None
    else:
        access_ok = owner is None and scope is None
    belief_ok = (_nonempty(belief_owner) and event.get("actor_id") == belief_owner) if truth == "belief" else belief_owner is None
    return access_ok and belief_ok

@dataclass(frozen=True)
class AuthorizedEvidence:
    events: list[dict[str, Any]]
    spans: list[dict[str, Any]]
    trace: dict[str, Any]

class EvidenceAuthorizer:
    def __init__(self, conn: sqlite3.Connection) -> None: self._conn = conn
    def authorize(self, *, campaign_id: str, actor_id: str, actor_type: str, query: str, budget: int, active_quest_ids: list[str], scene_id: str | None = None) -> AuthorizedEvidence:
        if not all(_nonempty(value) for value in (campaign_id, actor_id, actor_type)) or budget < 0:
            raise ValueError("campaign_id, actor_id, actor_type, and non-negative budget are required")
        clauses, params = ["sr.campaign_id=?"], [campaign_id]
        if scene_id: clauses.append("se.scene_id=?"); params.append(scene_id)
        rows = self._conn.execute("SELECT se.*, sr.campaign_id, sr.in_world_time, sr.scene_time_sort, sr.location_id, sr.created_at AS scene_created_at FROM scene_event se JOIN scene_record sr ON sr.scene_id=se.scene_id WHERE " + " AND ".join(clauses) + " ORDER BY se.created_at DESC, se.event_id DESC", params).fetchall()
        events, candidates, seen = [], [], set()
        for row in rows:
            raw, event = dict(row), None
            try: event = self._event(raw); allowed, reason, evaluation = self._allowed(event, campaign_id, actor_id, actor_type)
            except SecurityMetadataError as exc: allowed, reason, evaluation = False, str(exc), {"metadata_integrity": False}
            candidates.append({"source_event_id": raw["event_id"], "source_scene_id": raw["scene_id"], "decision": "allow" if allowed else "deny", "reason": reason, "truth_status": raw["truth_status"], "visibility": raw["visibility"], "branch_id": raw.get("branch_id"), "branch_status": raw.get("branch_status"), "evaluation": evaluation})
            if allowed and raw["event_id"] not in seen: seen.add(raw["event_id"]); events.append(event)
        events = events[:budget]
        spans = []
        seen_spans = set()
        for event in events:
            span = event.get("source_span")
            if not isinstance(span, str) or not span or span in seen_spans:
                continue
            seen_spans.add(span)
            spans.append({"source_event_id": event["source_event_id"], "source_scene_id": event["source_scene_id"], "text": span, "truncated": False})
        trace = {"policy":"AERP-1", "campaign_id":campaign_id, "actor_id":actor_id, "actor_type":actor_type, "query":query, "scene_scope_id":scene_id, "active_quest_ids":list(active_quest_ids), "candidate_generation":{"campaign_constrained":True,"candidate_count":len(rows)}, "candidates":candidates, "deduplication":{"identity":"source_event_id","authorized_unique_count":len(events)}, "authorized_candidate_ids":[e["source_event_id"] for e in events], "selected_evidence_ids":[], "returned_spans":[{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "length": len(span["text"]), "truncated": False} for span in spans]}
        return AuthorizedEvidence(events, spans, trace)
    def _event(self, d: dict[str, Any]) -> dict[str, Any]:
        return {"source_event_id":d["event_id"],"source_scene_id":d["scene_id"],"campaign_id":d["campaign_id"],"summary":d["summary"],"actor_id":d.get("actor_id"),"truth_status":d["truth_status"],"visibility":d["visibility"],"witness_set":_security_list(d["witness_set_json"],"witness_set"),"related_quests":_security_list(d["related_quests_json"],"related_quests"),"source_span":d["source_span"],"access_owner_id":d.get("access_owner_id"),"access_scope_id":d.get("access_scope_id"),"belief_owner_id":d.get("belief_owner_id"),"branch_id":d.get("branch_id"),"branch_status":d.get("branch_status"),"in_world_time":d["in_world_time"],"scene_time_sort":d["scene_time_sort"],"location_id":d["location_id"],"created_at":d["created_at"]}
    def _allowed(self, e: dict[str, Any], campaign_id: str, actor_id: str, actor_type: str) -> tuple[bool,str,dict[str,Any]]:
        truth, visibility = e["truth_status"], e["visibility"]
        branch_id, branch_status = e.get("branch_id"), e.get("branch_status")
        branch_valid = isinstance(branch_id, str) and bool(branch_id.strip()) and branch_status in VALID_BRANCH_STATUSES
        required_status = "active" if truth in CANONICAL_TRUTH_STATUSES | {"belief"} else truth
        branch_consistent = branch_valid and branch_status == required_status
        x={"truth_status_known":truth in VALID_TRUTH_STATUSES,"visibility_known":visibility in VALID_VISIBILITIES,"campaign_matches":e["campaign_id"]==campaign_id,"acl_metadata_valid":_acl_metadata_valid(e),"membership":None,"branch":{"branch_id":branch_id,"branch_status":branch_status,"valid":branch_valid,"required_status":required_status,"consistent":branch_consistent}}
        if not all((x["truth_status_known"],x["visibility_known"],x["campaign_matches"],x["acl_metadata_valid"])): return False,"invalid_security_metadata",x
        if not branch_consistent: return False,"invalid_or_missing_branch",x
        if truth in {"retconned","abandoned"} or visibility=="retconned": return False,"noncanonical_retconned_or_abandoned",x
        if truth=="belief" and e.get("belief_owner_id")!=actor_id: return False,"belief_requires_rightful_actor",x
        if actor_type=="gm" and actor_id=="gm": return True,"gm_authorized_nonretconned",x
        if visibility in {"public_world","rumor_public"}: return True,"public_visibility",x
        if visibility=="witnessed_only": return actor_id in e["witness_set"],("witness_match" if actor_id in e["witness_set"] else "witness_required"),x
        if visibility=="character_private": return e.get("access_owner_id")==actor_id and bool(actor_id),("private_owner_match" if e.get("access_owner_id")==actor_id else "private_owner_required"),x
        kind=_SCOPE_KIND.get(visibility)
        if kind:
            scope=e.get("access_scope_id"); matched=bool(scope) and self._conn.execute("SELECT 1 FROM actor_membership WHERE campaign_id=? AND actor_id=? AND scope_id=? AND scope_kind=? AND active=1",(campaign_id,actor_id,scope,kind)).fetchone() is not None
            x["membership"]={"scope_id":scope,"scope_kind":kind,"matched":matched}; return matched,("membership_match" if matched else "membership_required"),x
        return False,"gm_only_or_unsupported_visibility",x

__all__=["AuthorizedEvidence","EvidenceAuthorizer","SecurityMetadataError","VALID_BRANCH_STATUSES","VALID_TRUTH_STATUSES","VALID_VISIBILITIES"]
