"""Mechanical projections for optional episodic-memory lifecycle metadata.

The accepted play-memory rows remain the authoritative append-only history.
This module projects current event versions and stores only derived archive
payloads; expiry never falls back to the accepted record text.
"""
from __future__ import annotations

import copy
import hashlib
import json
import zlib

from .play_memory_protocol import fail


POLICY = "episodic-memory-v1"
MAINTENANCE_POLICY = "episodic-maintenance-v1"
ARCHIVE_FLOORS = 100


def _metadata(record):
    value = record.get("metadata", {})
    if not isinstance(value, dict):
        fail("lifecycle metadata must be an object", "record-conflict", -32009)
    return value


def _lifecycle(record):
    value = _metadata(record).get("memoryLifecycle")
    if value is None:
        return {}
    if (not isinstance(value, dict) or value.get("schema") != POLICY
            or value.get("phase") not in ("draft", "active", "resolved")):
        fail("invalid episodic-memory lifecycle metadata", "record-conflict", -32009)
    parent = value.get("parentEventId")
    if parent is not None and (not isinstance(parent, str) or not parent):
        fail("parentEventId must be a non-empty event ID", "record-conflict", -32009)
    for field in ("archiveEventIds", "restoreEventIds"):
        items = value.get(field, [])
        if (not isinstance(items, list)
                or any(not isinstance(item, str) or not item for item in items)
                or len(items) != len(set(items))):
            fail(f"{field} must be a unique event ID array", "record-conflict", -32009)
    return value


def _has_policy_lifecycle(record):
    metadata = record.get("metadata", {})
    if not isinstance(metadata, dict):
        return False
    value = metadata.get("memoryLifecycle")
    return isinstance(value, dict) and value.get("schema") == POLICY


def _maintenance_archives(record):
    """Return validated Host archive controls attached to a scene/source row."""
    metadata = _metadata(record)
    if "memoryMaintenance" not in metadata:
        return []
    value = metadata["memoryMaintenance"]
    if record["kind"] not in ("scene", "source"):
        fail("memoryMaintenance is only valid on a scene or source record",
             "record-conflict", -32009)
    if record["kind"] == "source" and metadata.get("authored") is not True:
        fail("source memoryMaintenance requires authored=true",
             "record-conflict", -32009)
    if (not isinstance(value, dict) or value.get("schema") != MAINTENANCE_POLICY
            or set(value) != {"schema", "archives"}):
        fail("invalid episodic-memory maintenance metadata", "record-conflict", -32009)
    archives = value.get("archives")
    if not isinstance(archives, list) or not archives:
        fail("memoryMaintenance.archives must be a non-empty array",
             "record-conflict", -32009)
    result = []
    for entry in archives:
        if (not isinstance(entry, dict) or set(entry) - {"eventId", "relatedEventIds"}
                or not isinstance(entry.get("eventId"), str) or not entry["eventId"]):
            fail("memoryMaintenance archive needs an eventId", "record-conflict", -32009)
        related = entry.get("relatedEventIds", [])
        if (not isinstance(related, list)
                or any(not isinstance(event_id, str) or not event_id for event_id in related)
                or len(related) != len(set(related))):
            fail("relatedEventIds must be unique event IDs", "record-conflict", -32009)
        result.append({"eventId": entry["eventId"], "relatedEventIds": list(related)})
    return result


def _managed_event_ids(records):
    """Collect explicitly opted-in components without joining related markers."""
    managed = policy_event_ids(records)
    for row in records:
        if row["kind"] == "event" and _has_policy_lifecycle(row):
            lifecycle = _lifecycle(row)
            managed.update(lifecycle.get("archiveEventIds", []))
            managed.update(lifecycle.get("restoreEventIds", []))
        for archive in _maintenance_archives(row):
            managed.add(archive["eventId"])
            managed.update(archive["relatedEventIds"])
    return managed


def _phase(record):
    if not _has_policy_lifecycle(record):
        return "active"
    return _lifecycle(record)["phase"]


def accepted_floor(records, story_order=None):
    """Return the zero-based ordinal of the last visible distinct scene."""
    scenes = {record["id"] for record in records if record["kind"] == "scene"
              and (story_order is None or record["order"] <= story_order)}
    return max(0, len(scenes) - 1)


def _events(records):
    return {record["id"]: record for record in records if record["kind"] == "event"}


def policy_event_ids(records):
    return {record["id"] for record in records
            if record["kind"] == "event" and _has_policy_lifecycle(record)}


def _acquisitions(records):
    return {record["id"]: record for record in records if record["kind"] == "acquisition"}


def event_families(records, *, managed_event_ids=None):
    """Collapse only opted-in or explicitly targeted continuation components.

    Legacy continuation metadata was previously descriptive and append-only.
    Rows outside lifecycle-managed components therefore retain their own roots
    and representatives, without newly validating their old metadata.
    """
    events = _events(records)
    adjacency = {identifier: set() for identifier in events}
    for identifier, row in events.items():
        continuation = _metadata(row).get("continuationOf")
        if isinstance(continuation, str) and continuation in events:
            adjacency[identifier].add(continuation)
            adjacency[continuation].add(identifier)

    seeds = set(events) if managed_event_ids is None else set(managed_event_ids) & set(events)
    managed = set()
    for start in seeds:
        if start in managed:
            continue
        component, todo = set(), [start]
        while todo:
            identifier = todo.pop()
            if identifier in component:
                continue
            component.add(identifier)
            todo.extend(adjacency[identifier] - component)
        managed.update(component)

    parents, children, roots, members, representatives = {}, {}, {}, {}, {}
    for identifier in events:
        if identifier not in managed:
            roots[identifier] = identifier
            members[identifier] = [identifier]
            representatives[identifier] = identifier

    visited = set()
    for start in sorted(managed):
        if start in visited:
            continue
        component, todo = set(), [start]
        while todo:
            identifier = todo.pop()
            if identifier in component:
                continue
            component.add(identifier)
            todo.extend(adjacency[identifier] - component)
        visited.update(component)
        for identifier in component:
            continuation = _metadata(events[identifier]).get("continuationOf")
            if continuation is None:
                continue
            target = events.get(continuation) if isinstance(continuation, str) else None
            if (target is None or continuation == identifier
                    or target["order"] > events[identifier]["order"]):
                fail("managed event continuation must reference a visible prior event",
                     "reference-integrity", -32009)
            parents[identifier] = continuation
            children.setdefault(continuation, []).append(identifier)
        terminals = [identifier for identifier in component if not children.get(identifier)]
        if not terminals:
            fail("event continuation family has ambiguous current version",
                 "record-conflict", -32009)
        latest_order = max(events[identifier]["order"] for identifier in terminals)
        latest = [identifier for identifier in terminals
                  if events[identifier]["order"] == latest_order]
        if len(latest) > 1:
            # A migration may select one cumulative v1 node where an old
            # append-only history had several same-order branches. Multiple
            # current v1 nodes at that order remain genuinely ambiguous.
            policy_latest = [identifier for identifier in latest
                             if _has_policy_lifecycle(events[identifier])]
            if len(policy_latest) != 1:
                fail("event continuation family has ambiguous current version",
                     "record-conflict", -32009)
            current = policy_latest[0]
        else:
            current = latest[0]
        for identifier in component:
            path = set()
            node = identifier
            while node in parents:
                if node in path:
                    fail("event continuation cycle", "record-conflict", -32009)
                path.add(node)
                node = parents[node]
            if node not in component:
                fail("managed event continuation escaped its component",
                     "record-conflict", -32009)
        root_candidates = [identifier for identifier in component if identifier not in parents]
        if len(root_candidates) != 1:
            fail("event continuation family has ambiguous root", "record-conflict", -32009)
        root = root_candidates[0]
        for identifier in component:
            roots[identifier] = root
        members[root] = sorted(component)
        representatives[root] = current
    return {"events": events, "parents": parents, "children": children,
            "roots": roots, "members": members, "representatives": representatives,
            "managedEventIds": managed}


def validate_lifecycle(records):
    """Validate typed references and ensure lifecycle links remain branch-local.

    ``records`` contains only the accepted scope lineage plus the candidate
    batch, so a sibling-branch identity cannot be resolved here.
    """
    events, acquisitions = _events(records), _acquisitions(records)
    superseders, parent_edges = {}, {}
    managed_event_ids = set()

    for row in records:
        metadata = _metadata(row)
        for archive in _maintenance_archives(row):
            for event_id in {archive["eventId"], *archive["relatedEventIds"]}:
                target = events.get(event_id)
                if target is None or target["order"] > row["order"]:
                    fail("memoryMaintenance references must be visible events at or before control order",
                         "reference-integrity", -32009)
                managed_event_ids.add(event_id)
        if row["kind"] == "event" and _has_policy_lifecycle(row):
            lifecycle = _lifecycle(row)
            managed_event_ids.add(row["id"])
            parent = lifecycle.get("parentEventId")
            if parent is not None:
                target = events.get(parent)
                if target is None or parent == row["id"] or target["order"] > row["order"]:
                    fail("event parent must reference a visible event at or before its order",
                         "reference-integrity", -32009)
                parent_edges[row["id"]] = parent
            for field in ("archiveEventIds", "restoreEventIds"):
                for event_id in lifecycle.get(field, []):
                    target = events.get(event_id)
                    if target is None or target["order"] > row["order"]:
                        fail(f"{field} must reference visible events at or before its order",
                             "reference-integrity", -32009)
                    managed_event_ids.add(event_id)
        elif row["kind"] == "acquisition":
            supersedes = metadata.get("memorySupersedes", [])
            if (not isinstance(supersedes, list)
                    or any(not isinstance(value, str) or not value for value in supersedes)
                    or len(supersedes) != len(set(supersedes))):
                fail("memorySupersedes must be a unique string array", "record-conflict", -32009)
            for old_id in supersedes:
                target = acquisitions.get(old_id)
                if (target is None or target["actorId"] != row["actorId"]
                        or target["order"] > row["order"] or old_id == row["id"]):
                    fail("memorySupersedes must reference a visible prior acquisition owned by the same actor",
                         "reference-integrity", -32009)
                superseders.setdefault(old_id, []).append(row["id"])

    continuation_graph = event_families(records, managed_event_ids=managed_event_ids)
    for identifier in continuation_graph["managedEventIds"]:
        row = events[identifier]
        continuation = _metadata(row).get("continuationOf")
        if continuation is None:
            continue
        target = events.get(continuation) if isinstance(continuation, str) else None
        if (target is None or continuation == row["id"]
                or target["order"] > row["order"]):
            fail("managed event continuation must reference a visible prior event",
                 "reference-integrity", -32009)

    if any(len(nodes) > 1 for nodes in superseders.values()):
        fail("acquisition has multiple superseding successors in one scope", "record-conflict", -32009)

    def check_acyclic(edges, label):
        visiting, visited = set(), set()

        def visit(node):
            if node in visiting:
                fail(f"{label} cycle", "record-conflict", -32009)
            if node in visited:
                return
            visiting.add(node)
            targets = edges.get(node, ())
            if isinstance(targets, str):
                targets = (targets,)
            for target in targets:
                visit(target)
            visiting.remove(node)
            visited.add(node)

        for start in edges:
            visit(start)

    check_acyclic(parent_edges, "event parent")
    check_acyclic(continuation_graph["parents"], "event continuation")
    acquisition_edges = {}
    for row in acquisitions.values():
        supersedes = _metadata(row).get("memorySupersedes", [])
        if supersedes:
            acquisition_edges[row["id"]] = supersedes
    check_acyclic(acquisition_edges, "acquisition supersession")


def _clean_record(record):
    return {key: copy.deepcopy(value) for key, value in record.items() if not key.startswith("_")}


def archive_specs(records, new_records):
    """Build archive payloads from each trigger row's accepted story cutoff."""
    combined = {record["id"]: record for record in records}
    for record in new_records:
        combined[record["id"]] = record
    records = list(combined.values())
    specs = {}

    def add(marker_event_id, root_id, related_event_ids, payload_records, kind, story_order):
        key = (marker_event_id, root_id)
        floor = accepted_floor(records, story_order)
        spec = specs.setdefault(key, {"eventId": marker_event_id, "rootId": root_id,
            "archivedFloor": floor, "expiresAfterFloor": floor + ARCHIVE_FLOORS,
            "storyOrder": story_order,
            "relatedEventIds": set(), "relatedRecordIds": set(), "records": {}, "kind": kind})
        spec["relatedEventIds"].update(related_event_ids)
        for record in payload_records:
            clean = _clean_record(record)
            spec["records"][clean["id"]] = clean
            spec["relatedRecordIds"].add(clean["id"])
        if kind == "event-family":
            spec["kind"] = kind

    def superseded_closure(start_ids, visible_acquisitions):
        found, todo = set(), list(start_ids)
        while todo:
            identifier = todo.pop()
            if identifier in found:
                continue
            row = visible_acquisitions.get(identifier)
            if row is None:
                continue
            found.add(identifier)
            todo.extend(_metadata(row).get("memorySupersedes", []))
        return found

    def archive_family(story_order, target_id, related_event_ids=()):
        # Plan against the complete accepted+candidate timeline but enforce the
        # control row's order cutoff. Related IDs only annotate the archive
        # marker; they do not join families or add rows to the archived payload.
        visible_records = [record for record in records if record["order"] <= story_order]
        managed_ids = _managed_event_ids(visible_records)
        managed_ids.add(target_id)
        managed_ids.update(related_event_ids)
        visible_graph = event_families(visible_records, managed_event_ids=managed_ids)
        visible_events = visible_graph["events"]
        if target_id not in visible_events:
            fail("archive target must be a visible prior event", "reference-integrity", -32009)
        root = visible_graph["roots"][target_id]
        family_ids = set(visible_graph["members"][root])
        representative = visible_graph["representatives"][root]
        visible_acquisitions = _acquisitions(visible_records)
        family_records = [visible_events[identifier] for identifier in sorted(family_ids)]
        related_acquisitions = [record for record in visible_acquisitions.values()
            if family_ids.intersection(record.get("eventIds", []))]
        family_records.extend(related_acquisitions)
        acquisition_ids = {record["id"] for record in related_acquisitions}
        family_records.extend(record for record in visible_records if record["kind"] == "attitude"
                              and record.get("acquisitionId") in acquisition_ids)
        family_records.extend(record for record in visible_records if record["kind"] == "link"
                              and family_ids.intersection(record.get("eventIds", [])))
        add(representative, root, family_ids | set(related_event_ids), family_records,
            "event-family", story_order)

    for row in new_records:
        if row["kind"] == "event":
            lifecycle = _lifecycle(row)
            for target_id in lifecycle.get("archiveEventIds", []):
                archive_family(row["order"], target_id)
        elif row["kind"] in ("scene", "source"):
            for archive in _maintenance_archives(row):
                archive_family(row["order"], archive["eventId"], archive["relatedEventIds"])
        elif row["kind"] == "acquisition":
            old_ids = _metadata(row).get("memorySupersedes", [])
            visible_records = [record for record in records if record["order"] <= row["order"]]
            managed_ids = {record["id"] for record in visible_records
                           if record["kind"] == "event" and _has_policy_lifecycle(record)}
            visible_graph = event_families(visible_records, managed_event_ids=managed_ids)
            visible_events = visible_graph["events"]
            visible_acquisitions = _acquisitions(visible_records)
            for old_id in old_ids:
                old = visible_acquisitions[old_id]
                family_roots = {visible_graph["roots"][event_id]
                                for event_id in old.get("eventIds", [])
                                if event_id in visible_graph["roots"]}
                if not family_roots:
                    family_roots = {visible_graph["roots"][event_id]
                                    for event_id in row.get("eventIds", [])
                                    if event_id in visible_graph["roots"]}
                if not family_roots:
                    continue
                for root in sorted(family_roots):
                    family_ids = set(visible_graph["members"][root])
                    related = [visible_acquisitions[item]
                               for item in sorted(superseded_closure([old_id], visible_acquisitions))
                               if family_ids.intersection(visible_acquisitions[item].get("eventIds", []))]
                    related_ids = {record["id"] for record in related}
                    related.extend(record for record in visible_records if record["kind"] == "attitude"
                                   and record.get("acquisitionId") in related_ids)
                    add(visible_graph["representatives"][root], root, family_ids, related,
                        "acquisition-facet", row["order"])

    return [{**spec, "relatedEventIds": sorted(spec["relatedEventIds"]),
             "relatedRecordIds": sorted(spec["relatedRecordIds"]),
             "records": [spec["records"][identifier] for identifier in sorted(spec["records"])]}
            for _, spec in sorted(specs.items())]


def encode_payload(records):
    raw = json.dumps(records, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8", errors="strict")
    return zlib.compress(raw, level=9), hashlib.sha256(raw).hexdigest()


def decode_payload(blob, expected_hash):
    raw = zlib.decompress(blob)
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        fail("derived archive payload hash differs", "record-integrity", -32009)
    value = json.loads(raw.decode("utf-8", errors="strict"))
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        fail("derived archive payload shape differs", "record-integrity", -32009)
    return value


def project(records, effective, markers, accesses, floor):
    """Return the active record ID set and compact Host-only graph projection."""
    managed_ids = policy_event_ids(records)
    managed_ids.update(event_id for marker in markers if marker.get("kind") == "event-family"
                       for event_id in marker["relatedEventIds"])
    graph = event_families(records, managed_event_ids=managed_ids)
    events, acquisitions = graph["events"], _acquisitions(records)
    marker_by_root = {}
    for marker in markers:
        root = marker["rootId"]
        marker_by_root.setdefault(root, []).append(marker)

    def rank(row):
        return (-row.get("_branchRank", 0), row.get("revision", 0), row.get("sequence", 0))

    latest_marker = {root: max(rows, key=rank) for root, rows in marker_by_root.items()}
    latest_restore = {}
    for access in accesses:
        if access["operation"] == "restore":
            latest_restore[access["rootId"]] = max(
                [latest_restore[access["rootId"]], access] if access["rootId"] in latest_restore else [access],
                key=rank)

    archived_event_ids = {record_id for marker in markers if marker.get("kind") == "event-family"
                          for record_id in marker["relatedRecordIds"]
                          if record_id in events}
    restored_record_ids = set()
    for root, marker in latest_marker.items():
        restore = latest_restore.get(root)
        if restore is not None and rank(restore) > rank(marker):
            restored_record_ids.update(restore.get("restoredRecordIds", []))

    retired_acquisition_ids = set()
    for row in acquisitions.values():
        retired_acquisition_ids.update(_metadata(row).get("memorySupersedes", []))

    restored_roots = {root for root in latest_marker
                       if root in latest_restore and rank(latest_restore[root]) > rank(latest_marker[root])}
    active_ids = {record["id"] for record in effective
                  if record["kind"] not in ("event", "acquisition", "attitude")}
    node_rows = []
    aliases = {}
    for root, family_ids in graph["members"].items():
        representative = graph["representatives"][root]
        for identifier in family_ids:
            if identifier != representative:
                aliases[identifier] = representative
        latest = events[representative]
        phase = _phase(latest)
        family_archived = representative in archived_event_ids
        if ((not family_archived or representative in restored_record_ids)
                and phase != "draft"):
            active_ids.add(representative)
        # This Host-only directory also exposes the current draft identity so
        # the writer can continue it. Its body and linked acquisitions remain
        # outside every ordinary active projection until the phase advances.
        scene_ids = sorted({events[identifier].get("sceneId") for identifier in family_ids
                            if events[identifier].get("sceneId")})
        lifecycle = _lifecycle(latest)
        node = {"eventId": representative, "rootId": root, "phase": phase,
                "sourceSceneIds": scene_ids}
        if "parentEventId" in lifecycle:
            node["parentEventId"] = lifecycle["parentEventId"]
        if not family_archived or representative in restored_record_ids or phase == "draft":
            node_rows.append(node)

    active_acquisition_ids = set()
    for row in effective:
        if row["kind"] != "acquisition":
            continue
        if row["id"] in retired_acquisition_ids and row["id"] not in restored_record_ids:
            continue
        if archived_event_ids.intersection(row.get("eventIds", [])) \
                and row["id"] not in restored_record_ids:
            continue
        linked_roots = {graph["roots"][event_id] for event_id in row.get("eventIds", [])
                        if event_id in graph["roots"]}
        if any(_phase(events[graph["representatives"][root]]) == "draft"
               for root in linked_roots):
            continue
        active_ids.add(row["id"])
        active_acquisition_ids.add(row["id"])

    active_ids.update(record["id"] for record in effective
                      if record["kind"] == "attitude"
                      and record["acquisitionId"] in active_acquisition_ids)
    active_ids.difference_update(record["id"] for record in effective
        if record["kind"] == "link" and any(
            event_id in archived_event_ids
            and event_id not in restored_record_ids
            for event_id in record.get("eventIds", [])))

    archive_markers = []
    for root, marker in sorted(latest_marker.items()):
        expiry = marker["expiresAfterFloor"]
        marker_key = (marker.get("branchId"), marker.get("revision"), marker["eventId"])
        visible_reads = [access for access in accesses if access["rootId"] == root
                         and access["operation"] == "read"
                         and (access.get("markerBranch"), access.get("markerRevision"),
                              access.get("eventId")) == marker_key]
        if visible_reads:
            latest_read = max(visible_reads, key=rank)
            expiry = max(expiry, latest_read["expiresAfterFloor"])
        available = floor < expiry and marker.get("payloadAvailable", False)
        archive_markers.append({"eventId": marker["eventId"], "rootId": root,
            "archivedFloor": marker["archivedFloor"], "expiresAfterFloor": expiry,
            "relatedEventIds": marker["relatedEventIds"], "available": available})

    return {"acceptedFloor": floor, "activeRecordIds": sorted(active_ids),
            "aliases": aliases,
            "nodes": sorted(node_rows, key=lambda row: (row["rootId"], row["eventId"])),
            "archiveMarkers": archive_markers,
            "archivedEventIds": archived_event_ids,
            "restoredRoots": restored_roots,
            "restoredRecordIds": restored_record_ids,
            "retiredAcquisitionIds": retired_acquisition_ids,
            "latestMarkers": latest_marker}
