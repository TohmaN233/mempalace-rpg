"""Lossless character projection and explicit, deterministic importance policy.

Inputs are already lineage/time scoped. Host qualification controls and explicit
roles classify importance; neither grants actor knowledge or original-source access.
"""
from dataclasses import dataclass
import json

from .play_memory_protocol import fail
from .play_memory_library import character_registry, introduction_eligible


DETAIL_POLICY = "character-importance-v1"
IMPORTANCE_POLICY = "character-importance-v2"
QUALIFICATION_SCHEMA = "character-qualification-v1"
QUALIFICATION_PREFIX = "character.qualification:"
TIERS = ("core", "major", "recurring", "ambient")


@dataclass(frozen=True)
class CharacterImportancePolicy:
    policy_id: str = IMPORTANCE_POLICY

    def __post_init__(self):
        if not isinstance(self.policy_id, str) or not self.policy_id:
            raise ValueError("importance policy requires an identity")

    def classify(self, records, actor, *, identity_records=None):
        fields = [r for r in records if r["kind"] in ("profile", "state")
                  and r["actorId"] == actor and actor in r["knownBy"]]
        overrides = sorted((r for r in fields if r["field"] == "memory.importance"),
                           key=lambda r: (r["order"], r["id"]))
        if len(overrides) > 1 and overrides[-1]["order"] == overrides[-2]["order"]:
            fail("ambiguous importance override order", "record-conflict", -32009)
        override = overrides[-1]["text"] if overrides else "auto"
        evidence = [r["id"] for r in overrides[-1:]]
        acquisitions = [r for r in records if r["kind"] == "acquisition" and r["actorId"] == actor]
        events = {event for r in acquisitions for event in r.get("eventIds", [])}
        event_rows = {r["id"]: r for r in records if r["kind"] == "event"}
        interactions = set()
        for event_id in events:
            event = event_rows.get(event_id, {})
            group = event.get("metadata", {}).get("interactionGroup")
            if group is not None:
                if not isinstance(group, str) or not group or group != event.get("sceneId"):
                    fail("interaction group must identify its accepted scene", "record-conflict", -32009)
                interactions.add(("scene", group))
            else:
                interactions.add(("event", event_id))
        count = len(interactions)
        reasons = [f"distinct-source-backed-interactions:{count}", "interaction-count-is-telemetry-only"]
        if override != "auto":
            tier = override
            reasons.append("host-importance-override")
        else:
            roles = [r for r in fields if r["field"] == "memory.role"]
            players = [r for r in roles if r["text"] == "player"]
            player_ids = {r["actorId"] for r in records if r["kind"] in ("profile", "state")
                          and r["field"] == "memory.role" and r["text"] == "player"
                          and r["actorId"] in r["knownBy"]}
            selected_core = []
            for row in records if identity_records is None else identity_records:
                if row["kind"] != "profile" or row["field"] != "name" or row["actorId"] != actor:
                    continue
                binding = row.get("metadata", {}).get("actorDirectory", {}).get("coreBinding")
                if binding is None:
                    continue
                required = {"kind", "definitionHash", "selectionHash", "ownerActorId", "coreId", "source"}
                if (not isinstance(binding, dict) or set(binding) != required
                        or binding["kind"] != "fated-selected-core-v1"
                        or any(not isinstance(binding[key], str) or not binding[key]
                               for key in required - {"source"})
                        or not isinstance(binding["source"], dict) or not binding["source"]):
                    fail("selected core binding has incomplete identity", "record-conflict", -32009)
                if binding["ownerActorId"] in player_ids and row.get("sourceSpans"):
                    selected_core.append(row)
            if len({json.dumps(row["metadata"]["actorDirectory"]["coreBinding"], sort_keys=True)
                    for row in selected_core}) > 1:
                fail("selected core has conflicting accepted identity anchors", "record-conflict", -32009)
            selected_core = sorted(selected_core, key=lambda row: (row["order"], row["id"]))[:1]
            qualified = {"core": [], "major": [], "recurring": []}
            qualification_rows = [r for r in records if r["kind"] == "state" and r["actorId"] == actor
                                  and r["field"].startswith(QUALIFICATION_PREFIX)]
            for row in qualification_rows:
                try:
                    value = json.loads(row["text"])
                except (ValueError, TypeError):
                    fail("character qualification must be structured JSON", "record-conflict", -32009)
                if (not isinstance(value, dict) or set(value) != {
                        "schemaVersion", "kind", "status", "counterpartActorId"}
                        or value["schemaVersion"] != QUALIFICATION_SCHEMA
                        or value["kind"] not in ("long-term-companion", "spouse", "plot-important", "recurring")
                        or value["status"] not in ("established", "temporary", "intended", "reported", "ended")
                        or not isinstance(value["counterpartActorId"], str) or not value["counterpartActorId"]
                        or row["field"] != f"{QUALIFICATION_PREFIX}{value['kind']}:{value['counterpartActorId']}"):
                    fail("character qualification has invalid identity or classification", "record-conflict", -32009)
                if (value["status"] == "established" and value["counterpartActorId"] in player_ids
                        and row.get("sourceSpans")):
                    target = {"long-term-companion": "core", "spouse": "core",
                              "plot-important": "major", "recurring": "recurring"}[value["kind"]]
                    qualified[target].append(row)
            major = [r for r in roles if r["text"] in ("major", "critical", "companion", "antagonist", "quest-giver")]
            major += [r for r in fields if r["kind"] == "profile" and r["field"] == "name"
                      and r.get("metadata", {}).get("actorDirectory", {}).get("role") in ("critical", "companion")]
            introduction = character_registry(records).get(actor)
            important_panels = []
            if (introduction is not None and introduction_eligible(introduction["introduction"])
                    and introduction["introduction"]["plotImportant"] is True):
                important_panels = [r for r in records if r["kind"] in ("source", "scene")
                    and any(entry["actorId"] == actor for entry in r.get("metadata", {}).get(
                        "characterRegistry", {}).get("entries", []))]
            if players or selected_core or qualified["core"]:
                tier = "core"
                evidence += [r["id"] for r in players + selected_core + qualified["core"]]
                reasons += (["explicit-player-role"] if players else [])
                reasons += (["source-bound-selected-core"] if selected_core else [])
                reasons += (["established-core-relationship"] if qualified["core"] else [])
            elif major or qualified["major"] or important_panels:
                tier = "major"
                evidence += [r["id"] for r in major + qualified["major"] + important_panels]
                if major or qualified["major"]:
                    reasons.append("explicit-important-role-or-plot-qualification")
                if important_panels:
                    reasons.append("accepted-plot-important-introduction")
            elif qualified["recurring"]:
                tier = "recurring"
                evidence += [r["id"] for r in qualified["recurring"]]
                reasons.append("established-recurring-qualification")
            else:
                tier = "ambient"
                reasons.append("insufficient-major-or-recurring-evidence")
        return {"tier": tier, "policyId": self.policy_id, "override": override,
                "reasons": reasons, "evidenceIds": sorted(set(evidence)), "interactionCount": count}


def detail_records(dossier, references):
    """Select current basic cards, without duplicating historical knowledge.

    Dossiers retain the other categories; reference knowledge and date cues use
    their existing dedicated delivery paths at every importance tier.
    """
    all_records = dossier["profile"] + dossier["state"] + dossier["relationships"]
    tier = dossier["importance"]["tier"]
    refs = set(references)
    if tier in ("core", "major"):
        return all_records
    if tier == "recurring":
        return [r for r in all_records if r["kind"] == "state" or r["id"] in refs
                or (r["kind"] == "profile" and r["field"] in ("name", "identity", "appearance"))]
    return [r for r in dossier["profile"] if r["field"] in ("name", "identity", "appearance")]
