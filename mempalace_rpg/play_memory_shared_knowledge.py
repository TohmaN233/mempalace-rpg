"""Mechanical, actor-scoped pooling compatibility for accepted knowledge units.

Raw batch rows remain the authenticated storage. Only read projections are hydrated.
"""
from __future__ import annotations

from .play_memory_protocol import fail


def hydrate_shared_knowledge(records):
    events = {row["id"]: row for row in records if row["kind"] == "event"}
    units_by_event = {}
    for event_id, event in events.items():
        units = event.get("metadata", {}).get("knowledgeUnits")
        if units is None:
            continue
        if not isinstance(units, list):
            fail("shared knowledge units must be an array", "knowledge-unit-integrity", -32009)
        indexed = {}
        for unit in units:
            if (not isinstance(unit, dict) or set(unit) - {"key", "summary", "useEventSummary", "actorIds", "beliefKind", "method", "sourceSpans", "grants"}
                    or not isinstance(unit.get("key"), str) or not unit["key"] or unit["key"] in indexed
                    or not isinstance(unit.get("actorIds"), list) or not unit["actorIds"]
                    or any(not isinstance(actor, str) or not actor for actor in unit["actorIds"])
                    or len(set(unit["actorIds"])) != len(unit["actorIds"])
                    or unit.get("beliefKind") not in ("fact", "hearsay", "inference", "mistaken-belief")
                    or not isinstance(unit.get("method"), str) or not unit["method"]):
                fail("invalid shared knowledge unit", "knowledge-unit-integrity", -32009)
            grants = unit.get("grants")
            if grants is None:
                if not isinstance(unit.get("sourceSpans"), list) or not unit["sourceSpans"]:
                    fail("invalid shared knowledge evidence", "knowledge-unit-integrity", -32009)
            elif (not isinstance(grants, list) or not grants
                    or any(not isinstance(grant, dict) or set(grant) != {"recordId", "actorId", "sourceSpans"}
                           or not isinstance(grant["recordId"], str) or not grant["recordId"]
                           or grant["actorId"] not in unit["actorIds"]
                           or not isinstance(grant["sourceSpans"], list) or not grant["sourceSpans"] for grant in grants)
                    or len({grant["recordId"] for grant in grants}) != len(grants)):
                fail("invalid shared knowledge grants", "knowledge-unit-integrity", -32009)
            if unit.get("useEventSummary") is True:
                if "summary" in unit:
                    fail("shared knowledge unit has conflicting content", "knowledge-unit-integrity", -32009)
                text = event.get("text")
            else:
                if "useEventSummary" in unit:
                    fail("unsupported shared knowledge content selector", "knowledge-unit-integrity", -32009)
                text = unit.get("summary")
            if not isinstance(text, str) or not text.strip():
                fail("shared knowledge unit has no readable summary", "knowledge-unit-integrity", -32009)
            indexed[unit["key"]] = (unit, text)
        units_by_event[event_id] = indexed
    result = []
    for row in records:
        reference = row.get("metadata", {}).get("knowledgeUnitRef") if row["kind"] == "acquisition" else None
        if reference is None:
            result.append(row)
            continue
        if (not isinstance(reference, dict) or set(reference) != {"eventId", "key"}
                or not isinstance(reference["eventId"], str) or not isinstance(reference["key"], str)):
            fail("invalid shared knowledge reference", "knowledge-unit-integrity", -32009)
        event = events.get(reference["eventId"])
        selected = units_by_event.get(reference["eventId"], {}).get(reference["key"])
        if event is None or selected is None:
            fail("shared knowledge reference cannot be resolved", "knowledge-unit-integrity", -32009)
        unit, text = selected
        grants = unit.get("grants")
        matching_evidence = (any(grant["recordId"] == row["id"] and grant["actorId"] == row["actorId"]
                                 and grant["sourceSpans"] == row.get("sourceSpans") for grant in grants)
                             if grants is not None else row.get("sourceSpans") == unit["sourceSpans"])
        if (row["actorId"] not in unit["actorIds"] or row["beliefKind"] != unit["beliefKind"]
                or row["method"] != unit["method"] or event["id"] not in row.get("eventIds", [])
                or event["order"] > row["order"] or not matching_evidence):
            fail("shared knowledge reference exceeds actor, belief, event or evidence scope", "knowledge-unit-integrity", -32009)
        if row.get("text") not in ("", text):
            fail("shared knowledge conflicts with actor delta", "knowledge-unit-integrity", -32009)
        result.append({**row, "text": text})
    return result
