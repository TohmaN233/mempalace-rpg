"""Single fail-closed authorization boundary for evidence source events."""
from __future__ import annotations
import json
import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

CANONICAL_TRUTH_STATUSES = {"canonical", "observed", "reported", "rumor", "uncertain"}
VALID_TRUTH_STATUSES = CANONICAL_TRUTH_STATUSES | {"belief", "retconned", "abandoned"}
VALID_VISIBILITIES = {"public_world", "party_only", "witnessed_only", "character_private", "faction_private", "quest_participants", "gm_only", "rumor_public", "retconned"}
VALID_BRANCH_STATUSES = {"active", "retconned", "abandoned"}
_SCOPE_KIND = {"faction_private": "faction", "quest_participants": "quest", "party_only": "party"}
_PRODUCT_DENIED_DETAIL_LIMIT = 256

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
    def authorize(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        query: str,
        budget: int,
        active_quest_ids: list[str],
        scene_id: str | None = None,
        compact_product_trace: bool = False,
    ) -> AuthorizedEvidence:
        if not all(_nonempty(value) for value in (campaign_id, actor_id, actor_type)) or budget < 0:
            raise ValueError("campaign_id, actor_id, actor_type, and non-negative budget are required")
        if compact_product_trace:
            # This is deliberately an opt-in product path.  Direct authorization
            # and the scene-snippet surface keep the exhaustive trace below.
            return self._authorize_compact_product(
                campaign_id=campaign_id,
                actor_id=actor_id,
                actor_type=actor_type,
                query=query,
                active_quest_ids=active_quest_ids,
                scene_id=scene_id,
            )
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
        # Authorization produces the complete ACL-approved candidate universe.
        # The product ranker applies its own hit/character budget after this
        # boundary; truncating here by recency would make an older authorized
        # event impossible to rank or use as deep evidence.  Keep ``budget``
        # for the direct span materialization below, where it remains a
        # response-size guard for this low-level API.
        direct_events = events[:budget]
        spans = []
        seen_spans = set()
        for event in direct_events:
            span = event.get("source_span")
            if not isinstance(span, str) or not span or span in seen_spans:
                continue
            seen_spans.add(span)
            spans.append({"source_event_id": event["source_event_id"], "source_scene_id": event["source_scene_id"], "text": span, "truncated": False})
        trace = {"policy":"AERP-1", "campaign_id":campaign_id, "actor_id":actor_id, "actor_type":actor_type, "query":query, "scene_scope_id":scene_id, "active_quest_ids":list(active_quest_ids), "candidate_generation":{"campaign_constrained":True,"candidate_count":len(rows)}, "candidates":candidates, "deduplication":{"identity":"source_event_id","authorized_unique_count":len(events)}, "authorized_candidate_ids":[e["source_event_id"] for e in events], "selected_evidence_ids":[], "returned_spans":[{"source_event_id": span["source_event_id"], "source_scene_id": span["source_scene_id"], "length": len(span["text"]), "truncated": False} for span in spans]}
        return AuthorizedEvidence(events, spans, trace)

    def _authorize_compact_product(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        query: str,
        active_quest_ids: list[str],
        scene_id: str | None,
    ) -> AuthorizedEvidence:
        """Authorize product recall without materializing every event payload.

        The SQL predicate is intentionally a *complete* ACL predicate, not a
        heuristic pre-filter: it has parity tests against ``_allowed``.  If its
        JSON functions or schema assumptions fail, the call fails rather than
        widening access.  Full rows are loaded only for denied audit records and
        later for selected evidence/span records.
        """
        all_ids, allowed_ids, denied_by_reason = self._candidate_authorization(
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
            scene_id=scene_id,
        )
        allowed_set = set(allowed_ids)
        denied_ids = [event_id for event_id in all_ids if event_id not in allowed_set]
        partitioned_denied_ids = [
            event_id for event_ids in denied_by_reason.values() for event_id in event_ids
        ]
        if set(partitioned_denied_ids) != set(denied_ids) or len(partitioned_denied_ids) != len(denied_ids):
            raise SecurityMetadataError("fast_path_denied_partition_mismatch")
        detailed_denied_ids = denied_ids[:_PRODUCT_DENIED_DETAIL_LIMIT]
        denied = self._candidate_rows(
            detailed_denied_ids,
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        if any(candidate["decision"] != "deny" for candidate in denied):
            # The SQL ACL and the detailed policy evaluator must describe the
            # same snapshot.  A disagreement is a security fault, never a
            # reason to return a partially trusted product response.
            raise SecurityMetadataError("fast_path_denied_candidate_mismatch")
        digest = hashlib.sha256("\n".join(allowed_ids).encode("utf-8")).hexdigest()
        trace = {
            "policy": "AERP-1",
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "query": query,
            "scene_scope_id": scene_id,
            "active_quest_ids": list(active_quest_ids),
            "candidate_generation": {
                "campaign_constrained": True,
                "candidate_count": len(all_ids),
                "authorization_mode": "sqlite_acl_parity_v1",
            },
            # Detailed rows are bounded telemetry.  The two complete ID lists
            # below are the auditable partition of the candidate universe.
            "candidates": denied,
            "deduplication": {"identity": "source_event_id", "authorized_unique_count": len(allowed_ids)},
            "authorized_candidate_ids": allowed_ids,
            "denied_partitions": [
                {
                    "reason": reason,
                    "key_evaluation": {"failed_check": reason},
                    "event_ids": event_ids,
                    "event_ids_sha256": hashlib.sha256("\n".join(event_ids).encode("utf-8")).hexdigest(),
                    "count": len(event_ids),
                }
                for reason, event_ids in denied_by_reason.items()
            ],
            "selected_evidence_ids": [],
            "returned_spans": [],
            "trace_compaction": {
                "mode": "full_id_partitions_selected_allow_v2",
                "denied_detailed_count": len(denied),
                "denied_omitted_evaluation_count": len(denied_ids) - len(denied),
                "selected_allowed_detailed_count": 0,
                "omitted_allowed_evaluation_count": len(allowed_ids),
                "authorized_candidate_ids_sha256": digest,
                "denied_candidate_ids_sha256": hashlib.sha256("\n".join(denied_ids).encode("utf-8")).hexdigest(),
                "denied_partition_count": len(denied_by_reason),
                "semantic_contract": "authorized_candidate_ids and every denied_partitions.event_ids list form the complete candidate partition; candidates contains bounded denial detail plus every selected allow.",
            },
        }
        # Product ranking works from the authorized ID universe.  It must not
        # accidentally make every source span/text resident for an ordinary call.
        return AuthorizedEvidence([], [], trace)

    def add_selected_candidates(
        self,
        decision: AuthorizedEvidence,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        selected_event_ids: list[str],
    ) -> None:
        """Attach detailed allow telemetry only for evidence actually returned."""
        compaction = decision.trace.get("trace_compaction")
        if not isinstance(compaction, dict):
            return
        selected = list(dict.fromkeys(str(event_id) for event_id in selected_event_ids))
        rows = self._candidate_rows(
            selected,
            campaign_id=campaign_id,
            actor_id=actor_id,
            actor_type=actor_type,
        )
        by_id = {row["source_event_id"]: row for row in rows}
        missing = [event_id for event_id in selected if event_id not in by_id or by_id[event_id]["decision"] != "allow"]
        if missing:
            # A fast-path mismatch is a security fault.  Fail closed instead of
            # silently returning a selected item with unverifiable policy state.
            raise SecurityMetadataError("fast_path_selected_candidate_mismatch")
        selected_rows = [by_id[event_id] for event_id in selected]
        decision.trace["candidates"].extend(selected_rows)
        compaction["selected_allowed_detailed_count"] = len(selected_rows)
        compaction["omitted_allowed_evaluation_count"] = max(
            0,
            int(decision.trace["deduplication"]["authorized_unique_count"]) - len(selected_rows),
        )

    @staticmethod
    def _valid_json_string_array(column: str) -> str:
        # CASE forces json_type/json_each to run only after json_valid.  This is
        # important for legacy/tampered rows: malformed security metadata is a
        # deny, not a SQLite exception or an implicit allow.
        return (
            "CASE WHEN json_valid(" + column + ") THEN "
            "CASE WHEN json_type(" + column + ")='array' "
            "AND NOT EXISTS (SELECT 1 FROM json_each(" + column + ") j "
            "WHERE j.type<>'text' OR j.value='' OR trim(j.value)<>j.value) "
            "THEN 1 ELSE 0 END ELSE 0 END"
        )

    @staticmethod
    def _nonempty_text(column: str) -> str:
        return "typeof(" + column + ")='text' AND trim(" + column + ")<>''"

    def _candidate_authorization(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
        scene_id: str | None,
    ) -> tuple[list[str], list[str], dict[str, list[str]]]:
        witness_valid = self._valid_json_string_array("se.witness_set_json")
        quests_valid = self._valid_json_string_array("se.related_quests_json")
        owner = self._nonempty_text("se.access_owner_id")
        scope = self._nonempty_text("se.access_scope_id")
        belief_owner = self._nonempty_text("se.belief_owner_id")
        branch = self._nonempty_text("se.branch_id")
        static_acl = (
            "CASE "
            "WHEN se.visibility='character_private' THEN (" + owner + " AND se.access_scope_id IS NULL) "
            "WHEN se.visibility IN ('faction_private','quest_participants','party_only') THEN (" + scope + " AND se.access_owner_id IS NULL) "
            "ELSE (se.access_owner_id IS NULL AND se.access_scope_id IS NULL) END"
        )
        belief_acl = "CASE WHEN se.truth_status='belief' THEN (" + belief_owner + " AND se.actor_id=se.belief_owner_id) ELSE se.belief_owner_id IS NULL END"
        metadata_clauses = [
            "se.truth_status IN ('canonical','observed','reported','rumor','uncertain','belief','retconned','abandoned')",
            "se.visibility IN ('public_world','party_only','witnessed_only','character_private','faction_private','quest_participants','gm_only','rumor_public','retconned')",
            "(" + static_acl + ")",
            "(" + belief_acl + ")",
        ]
        branch_clauses = [
            "(" + branch + ")",
            "se.branch_status IN ('active','retconned','abandoned')",
            "se.branch_status=CASE WHEN se.truth_status IN ('canonical','observed','reported','rumor','uncertain','belief') THEN 'active' ELSE se.truth_status END",
        ]
        witness_match = "EXISTS (SELECT 1 FROM json_each(CASE WHEN json_valid(se.witness_set_json) THEN se.witness_set_json ELSE '[]' END) witness WHERE witness.value=:actor_id)"
        faction_match = "EXISTS (SELECT 1 FROM actor_membership am WHERE am.campaign_id=sr.campaign_id AND am.actor_id=:actor_id AND am.scope_id=se.access_scope_id AND am.scope_kind='faction' AND am.active=1)"
        quest_match = "EXISTS (SELECT 1 FROM actor_membership am WHERE am.campaign_id=sr.campaign_id AND am.actor_id=:actor_id AND am.scope_id=se.access_scope_id AND am.scope_kind='quest' AND am.active=1)"
        party_match = "EXISTS (SELECT 1 FROM actor_membership am WHERE am.campaign_id=sr.campaign_id AND am.actor_id=:actor_id AND am.scope_id=se.access_scope_id AND am.scope_kind='party' AND am.active=1)"
        denial_reason = (
            "CASE "
            "WHEN NOT ((" + witness_valid + ")=1) THEN 'invalid_witness_set_json' "
            "WHEN NOT ((" + quests_valid + ")=1) THEN 'invalid_related_quests_json' "
            "WHEN NOT (" + " AND ".join(metadata_clauses) + ") THEN 'invalid_security_metadata' "
            "WHEN NOT (" + " AND ".join(branch_clauses) + ") THEN 'invalid_or_missing_branch' "
            "WHEN se.truth_status IN ('retconned','abandoned') OR se.visibility='retconned' THEN 'noncanonical_retconned_or_abandoned' "
            "WHEN se.truth_status='belief' AND se.belief_owner_id<>:actor_id THEN 'belief_requires_rightful_actor' "
            "WHEN :actor_id='gm' AND :actor_type='gm' THEN NULL "
            "WHEN se.visibility IN ('public_world','rumor_public') THEN NULL "
            "WHEN se.visibility='witnessed_only' THEN CASE WHEN " + witness_match + " THEN NULL ELSE 'witness_required' END "
            "WHEN se.visibility='character_private' THEN CASE WHEN se.access_owner_id=:actor_id THEN NULL ELSE 'private_owner_required' END "
            "WHEN se.visibility='faction_private' THEN CASE WHEN " + faction_match + " THEN NULL ELSE 'membership_required' END "
            "WHEN se.visibility='quest_participants' THEN CASE WHEN " + quest_match + " THEN NULL ELSE 'membership_required' END "
            "WHEN se.visibility='party_only' THEN CASE WHEN " + party_match + " THEN NULL ELSE 'membership_required' END "
            "ELSE 'gm_only_or_unsupported_visibility' END"
        )
        candidate_clauses = ["sr.campaign_id=:campaign_id"]
        params: dict[str, Any] = {
            "campaign_id": campaign_id,
            "actor_id": actor_id,
            "actor_type": actor_type,
        }
        if scene_id:
            candidate_clauses.append("se.scene_id=:scene_id")
            params["scene_id"] = scene_id
        rows = self._conn.execute(
            "SELECT se.event_id, " + denial_reason + " AS denial_reason "
            "FROM scene_event se JOIN scene_record sr ON sr.scene_id=se.scene_id WHERE "
            + " AND ".join(candidate_clauses)
            + " ORDER BY se.created_at DESC, se.event_id DESC",
            params,
        ).fetchall()
        all_ids = [str(row["event_id"]) for row in rows]
        allowed_ids = [str(row["event_id"]) for row in rows if row["denial_reason"] is None]
        denied_by_reason: dict[str, list[str]] = {}
        for row in rows:
            if row["denial_reason"] is not None:
                denied_by_reason.setdefault(str(row["denial_reason"]), []).append(str(row["event_id"]))
        return all_ids, allowed_ids, denied_by_reason

    def _candidate_rows(
        self,
        event_ids: list[str],
        *,
        campaign_id: str,
        actor_id: str,
        actor_type: str,
    ) -> list[dict[str, Any]]:
        if not event_ids:
            return []
        # SQLite has a conservative variable cap, so bulk telemetry is chunked.
        raw_by_id: dict[str, dict[str, Any]] = {}
        for start in range(0, len(event_ids), 900):
            batch = event_ids[start:start + 900]
            placeholders = ",".join("?" for _ in batch)
            rows = self._conn.execute(
                "SELECT se.*, sr.campaign_id, sr.in_world_time, sr.scene_time_sort, sr.location_id, sr.created_at AS scene_created_at "
                "FROM scene_event se JOIN scene_record sr ON sr.scene_id=se.scene_id "
                "WHERE sr.campaign_id=? AND se.event_id IN (" + placeholders + ")",
                [campaign_id, *batch],
            ).fetchall()
            raw_by_id.update({str(row["event_id"]): dict(row) for row in rows})
        candidates: list[dict[str, Any]] = []
        for event_id in event_ids:
            raw = raw_by_id.get(event_id)
            if raw is None:
                raise SecurityMetadataError("candidate_row_missing")
            try:
                event = self._event(raw)
                allowed, reason, evaluation = self._allowed(event, campaign_id, actor_id, actor_type)
            except SecurityMetadataError as exc:
                allowed, reason, evaluation = False, str(exc), {"metadata_integrity": False}
            candidates.append({"source_event_id": raw["event_id"], "source_scene_id": raw["scene_id"], "decision": "allow" if allowed else "deny", "reason": reason, "truth_status": raw["truth_status"], "visibility": raw["visibility"], "branch_id": raw.get("branch_id"), "branch_status": raw.get("branch_status"), "evaluation": evaluation})
        return candidates
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
