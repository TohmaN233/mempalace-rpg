"""Mechanical actor identity preparation; semantic discovery belongs to the caller's model.

Prepared records join the ordinary accepted batch. Preparing never mutates a save,
so a cancelled generation cannot leave an actor or knowledge behind.
"""
import hashlib
import json
import logging

from .play_memory_library import (LIBRARY_POLICY, character_library_projection,
                                 blocked_memory_actors, character_library_statuses,
                                 introduction_eligible,
                                 memory_eligible, normalized_name, resolve_identity_name,
                                 seen_identities, _identity_entry)
from .play_memory_protocol import fail


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def actor_directory(scope, policy):
    records = scope["effective"]
    identity_records = scope.get("records", records)
    identities = {}
    for row in records:
        for key in ("actorId", "fromActorId", "toActorId"):
            if key in row:
                identities.setdefault(row[key], {"actorId": row[key], "displayName": row[key],
                    "aliases": [], "channel": "physical", "present": False})
    for actor in scope["binding"]["actorIds"]:
        identities.setdefault(actor, {"actorId": actor, "displayName": actor,
            "aliases": [], "channel": "physical", "present": False})
    for row in records:
        if row["kind"] == "profile" and row["field"] == "name":
            identity = identities[row["actorId"]]
            identity["displayName"] = row["text"]
            metadata = row.get("metadata", {}).get("actorDirectory", {})
            identity["aliases"] = metadata.get("aliases", [])
            identity["channel"] = metadata.get("channel", "physical")
        if row["kind"] == "state" and row["field"] == "memory.presence":
            presence = json.loads(row["text"])
            identities[row["actorId"]].update(presence)
    rosters = [row for row in records if row["kind"] == "scene"
               and "actorRoster" in row.get("metadata", {})]
    if rosters:
        # A complete accepted scene roster supersedes old individual presence.
        # Absence is Host context, not a new knowledge record granting prose access.
        roster = max(rosters, key=lambda row: (row["order"], row["id"]))["metadata"]["actorRoster"]
        if not isinstance(roster, list):
            fail("Accepted actor roster must be a table", "record-conflict", -32009)
        seen = set()
        for identity in identities.values():
            identity["present"] = False
        for actor in roster:
            if (not isinstance(actor, dict) or set(actor) != {"actorId", "present", "channel"}
                    or not isinstance(actor["actorId"], str) or not actor["actorId"]
                    or type(actor["present"]) is not bool
                    or actor["channel"] not in ("physical", "mental")
                    or actor["actorId"] in seen):
                fail("Accepted actor roster has an invalid or repeated row", "record-conflict", -32009)
            seen.add(actor["actorId"])
            if actor["actorId"] in identities:
                identities[actor["actorId"]].update(present=actor["present"], channel=actor["channel"])
    blocked = blocked_memory_actors(records)
    identities = {actor_id: row for actor_id, row in identities.items() if actor_id not in blocked}
    if scope["binding"]["purpose"] == "actor-knowledge":
        identities = {key: value for key, value in identities.items()
                      if key in scope["binding"]["actorIds"]}
    for actor, identity in identities.items():
        identity["importance"] = policy.classify(records, actor, identity_records=identity_records)
    if scope["binding"].get("characterLibraryPolicy") == LIBRARY_POLICY:
        library = character_library_projection(records, policy, identity_records=identity_records)
        visible = []
        for actor in library["actors"]:
            old = identities.get(actor["actorId"], {})
            visible.append({
                "actorId": actor["actorId"],
                "displayName": actor["displayName"],
                "aliases": actor["aliases"],
                "present": old.get("present", False),
                "channel": old.get("channel", "physical"),
                "importance": policy.classify(records, actor["actorId"], identity_records=identity_records),
            })
        return sorted(visible, key=lambda row: row["actorId"])
    return sorted(identities.values(), key=lambda row: row["actorId"])


def resolve_actors(scope, params, policy):
    if scope["binding"]["purpose"] == "actor-knowledge":
        fail("Actor knowledge readers cannot introduce or rename actors", "forbidden", -32001)
    lifecycle = scope["binding"].get("characterLibraryPolicy") == LIBRARY_POLICY
    directory_rows = {row["actorId"]: row for row in actor_directory(scope, policy)}
    accepted_identities = seen_identities(scope.get("records", scope["effective"]))
    identity_entries = dict(accepted_identities)
    known = dict(directory_rows)
    for actor_id, entry in identity_entries.items():
        row = directory_rows.get(actor_id, {})
        known[actor_id] = {
            "actorId": actor_id,
            "displayName": entry["canonicalName"],
            "aliases": entry["aliases"],
            "present": row.get("present", False),
            "channel": row.get("channel", "physical"),
        }
    for actor_id in scope["binding"]["actorIds"]:
        known.setdefault(actor_id, {"actorId": actor_id, "displayName": actor_id,
                                    "aliases": [], "present": False, "channel": "physical"})
    lookup = dict(identity_entries)
    for actor_id, row in known.items():
        lookup.setdefault(actor_id, {
            "actorId": actor_id, "canonicalName": row["displayName"],
            "aliases": list(row.get("aliases", [])), "legacy": True,
        })
    statuses = character_library_statuses(scope["effective"])
    source, origin = params["source"], params["originId"]
    text, order = source["text"], source["order"]
    version = _hash(text)
    prepared = {}
    request_receipt = params.get("firstAppearance")
    for index, raw in enumerate(params.get("preparedRegistry", [])):
        entry = _identity_entry(raw, f"preparedRegistry[{index}]")
        actor_id = entry["actorId"]
        first = entry["firstAppearance"]
        if (first["basis"] != "panel" or first["requestId"] != origin
                or request_receipt is None or request_receipt["requestId"] != origin):
            fail("prepared identity is not bound to this panel request",
                 "registry-integrity", -32009)
        if actor_id in prepared:
            fail("prepared registry repeats an actor identity", "registry-integrity", -32009)
        if actor_id in accepted_identities:
            fail("prepared registry identity collides with an accepted identity",
                 "registry-integrity", -32009)
        prepared[actor_id] = {**entry, "legacy": False, "pending": True}
        identity_entries[actor_id] = prepared[actor_id]
        known[actor_id] = {
            "actorId": actor_id,
            "displayName": entry["canonicalName"],
            "aliases": list(entry["aliases"]),
            "present": False,
            "channel": "physical",
        }
        lookup[actor_id] = identity_entries[actor_id]
    resolved, records, local_refs, actor_ids, registry_entries = [], [], set(), set(), []

    def final_registry_entry(entry, *, bind_to_current_panel=False):
        result = {key: entry[key] for key in
                  ("actorId", "canonicalName", "aliases", "introduction", "firstAppearance")}
        if bind_to_current_panel:
            result["firstAppearance"] = {**request_receipt, "basis": "panel"}
        return result

    retained_metadata = {r["actorId"]: r.get("metadata", {}).get("actorDirectory", {})
                         for r in scope["effective"] if r["kind"] == "profile" and r["field"] == "name"}
    for proposal in params["proposals"]:
        ref = proposal["localRef"]
        if ref in local_refs:
            fail("Actor localRef is repeated")
        local_refs.add(ref)
        supplied_actor_id = proposal.get("actorId")
        actor_id = supplied_actor_id
        introduction = proposal.get("introduction")
        skip_global_name_lookup = (
            lifecycle and isinstance(introduction, dict)
            and introduction.get("hasProperName") is False
        )
        matches = [] if skip_global_name_lookup else resolve_identity_name(
            proposal["displayName"], lookup, exclude_confirmed_unnamed=lifecycle)
        if actor_id is None:
            if len(matches) > 1:
                fail(f"Actor display name is ambiguous across saved identities: {proposal['displayName']}",
                     "ambiguous-actor-identity", -32009)
            if matches:
                actor_id = matches[0]
            else:
                actor_id = "actor:" + _hash(json.dumps([scope["binding"]["campaignId"],
                    scope["binding"]["branchId"], origin, ref], ensure_ascii=False))
        elif actor_id not in known:
            fail("Existing actorId is not in this save's visible identity scope", "unknown-actor", -32001)
        elif matches and actor_id not in matches:
            fail(f"Actor ID conflicts with canonical name or alias: {proposal['displayName']}",
                 "actor-identity-conflict", -32009)
        is_accepted = actor_id in accepted_identities
        is_pending = actor_id in prepared
        is_new = not is_accepted
        if (is_new and not is_pending and supplied_actor_id is not None
                and actor_id not in scope["binding"]["actorIds"]):
            fail("New explicit actorId must be bound by the Host for this panel",
                 "unknown-actor", -32001)
        if actor_id in actor_ids:
            fail("One actor has multiple rows; merge its aliases in the supplied form")
        actor_ids.add(actor_id)
        previous = known.get(actor_id)
        registered = identity_entries.get(actor_id)
        legacy_reclassification = False
        if registered is not None and not registered.get("legacy"):
            canonical = registered["canonicalName"]
            introduction = proposal.get("introduction") or registered["introduction"]
            if (not is_pending and proposal.get("introduction") is not None
                    and registered["introduction"] != proposal["introduction"]):
                fail(f"First-appearance classification changed for actor {actor_id}",
                     "registry-integrity", -32009)
        elif registered is not None:
            canonical = normalized_name(registered["canonicalName"])
            legacy_reclassification = proposal.get("introduction") is not None
            introduction = None
            if legacy_reclassification:
                logging.getLogger(__name__).info(
                    "actor identity left unclassified actor=%s origin=%s disposition=legacy-unclassified",
                    actor_id, origin)
        else:
            canonical = normalized_name(proposal["displayName"])
            introduction = proposal.get("introduction")

        if is_new and not is_pending and supplied_actor_id is not None and lifecycle:
            if introduction is None:
                fail("New Host-bound World actor requires first-panel introduction tags",
                     "registry-integrity", -32009)
            if request_receipt is None or request_receipt["requestId"] != origin:
                fail("New Host-bound World actor requires its matching firstAppearance receipt",
                     "registry-integrity", -32009)

        if is_new and not is_pending and introduction is not None:
            if request_receipt is None or request_receipt["requestId"] != origin:
                fail("New panel identity requires its exact matching firstAppearance receipt",
                     "registry-integrity", -32009)
            registered = {
                "actorId": actor_id,
                "canonicalName": canonical,
                "aliases": [],
                "introduction": dict(introduction),
                "firstAppearance": {**request_receipt, "basis": "panel"},
                "legacy": False,
                "pending": True,
            }
            identity_entries[actor_id] = registered
            prepared[actor_id] = registered
            lookup[actor_id] = registered

        # Keep the first accepted canonical name. A new displayed name or alias
        # is retained only when it is grounded in this exact source text.
        old_aliases = list((registered or {}).get("aliases", (previous or {}).get("aliases", [])))
        aliases = list(dict.fromkeys(old_aliases))
        for raw_alias in [*proposal["aliases"], proposal["displayName"]]:
            alias = normalized_name(raw_alias)
            if alias == canonical or alias in aliases:
                continue
            if not lifecycle or alias in text:
                aliases.append(alias)
        if registered is not None and not registered.get("legacy"):
            registered["aliases"] = list(aliases)
            if is_pending and proposal.get("introduction") is not None:
                registered["introduction"] = dict(proposal["introduction"])
            identity_entries[actor_id] = registered
            lookup[actor_id] = registered
        known_actor = {**(previous or {}), "actorId": actor_id, "displayName": canonical,
                       "aliases": aliases, "present": proposal["present"],
                       "channel": proposal["channel"]}
        known[actor_id] = known_actor

        eligible = True
        if lifecycle:
            eligible = memory_eligible(actor_id, identity_entries, statuses)
            if (registered is not None and not registered.get("legacy")
                    and actor_id not in statuses):
                eligible = introduction_eligible(registered["introduction"])
        elif registered is not None and not registered.get("legacy"):
            eligible = introduction_eligible(registered["introduction"]) and statuses.get(actor_id, "saved") == "saved"
        else:
            eligible = statuses.get(actor_id, "saved") != "deleted"
        resolved_actor = {"localRef": ref, "actorId": actor_id, "displayName": canonical,
                          "aliases": aliases, "present": proposal["present"],
                          "channel": proposal["channel"], "isNew": is_new}
        if lifecycle or legacy_reclassification:
            resolved_actor["memoryEligible"] = eligible
        if legacy_reclassification:
            resolved_actor["memoryEligible"] = False
            resolved_actor["memoryDisposition"] = "legacy-unclassified"
            resolved.append(resolved_actor)
            continue
        if eligible:
            identity = resolved_actor
            if (not is_pending and previous is not None
                    and previous["displayName"] == identity["displayName"]
                    and previous["aliases"] == aliases
                    and previous["channel"] == identity["channel"]):
                # Already accepted profile information needs no duplicate row.
                resolved.append(resolved_actor)
                continue
        else:
            identity = resolved_actor

        if not eligible:
            # A provisional entry is returned only for identities retained by
            # the current corrected panel. Removed rows are pruned from here.
            if (lifecycle and registered is not None and not registered.get("legacy")
                    and (is_new or is_pending)):
                registry_entries.append(final_registry_entry(registered, bind_to_current_panel=True))
            elif (lifecycle and registered is not None and not registered.get("legacy")
                  and aliases != old_aliases):
                # Excluded/deleted identities have no name-profile update to
                # persist aliases. Append a monotonic registry entry while
                # keeping its original identity and first-appearance provenance.
                registry_entries.append(final_registry_entry(registered))
            resolved.append(resolved_actor)
            continue
        quote = proposal.get("quote", proposal["displayName"])
        offset = text.find(quote)
        context_only = False
        if offset < 0:
            names = [proposal["displayName"], *proposal["aliases"], canonical,
                     *known.get(actor_id, {}).get("aliases", [])]
            literal = next((name for name in names if name and name in text), None)
            if literal is None:
                if not proposal["present"]:
                    logging.getLogger(__name__).warning(
                        "actor identity deferred ref=%s source=%s reason=unanchored-absent-label no-knowledge-grant",
                        ref, source["sourceId"])
                    continue
                # A director's display label may paraphrase an unnamed person's
                # description. It is directory metadata, not a received name or
                # permission to read the scene. Keep context provenance only.
                context_only = True
                quote, offset = text, 0
                logging.getLogger(__name__).warning(
                    "actor identity label ref=%s source=%s basis=semantic-roster no-knowledge-grant",
                    ref, source["sourceId"])
            else:
                quote, offset = literal, text.index(literal)
                logging.getLogger(__name__).info(
                    "actor identity quote rebound ref=%s source=%s basis=exact-literal-name", ref, source["sourceId"])
        # Identity/presence cards authorize only a literal name/label mention,
        # never the surrounding paragraph (which may contain somebody's secret).
        mentions = [name for name in [proposal["displayName"], *proposal["aliases"],
                                     canonical, *known.get(actor_id, {}).get("aliases", [])]
                    if name and name in quote]
        if not mentions:
            context_only = True
            logging.getLogger(__name__).warning(
                "actor identity label ref=%s source=%s basis=descriptive-anchor no-knowledge-grant",
                ref, source["sourceId"])
        mention = quote if context_only else min(mentions, key=len)
        if not context_only:
            offset += quote.index(mention)
        start = len(text[:offset].encode("utf-8"))
        span = {"sourceId": source["sourceId"], "version": version, "startByte": start,
                "endByte": start + len(mention.encode("utf-8")), "sha256": _hash(mention)}
        source_spans = [span]
        if not context_only:
            for alias in aliases:
                if alias == mention:
                    continue
                alias_offset = text.find(alias)
                if alias_offset < 0:
                    continue
                alias_start = len(text[:alias_offset].encode("utf-8"))
                source_spans.append({"sourceId": source["sourceId"], "version": version,
                                     "startByte": alias_start,
                                     "endByte": alias_start + len(alias.encode("utf-8")),
                                     "sha256": _hash(alias)})
        metadata = {"actorDirectory": {**retained_metadata.get(actor_id, {}),
                                       "aliases": aliases, "channel": identity["channel"]}}
        if context_only:
            metadata["actorDirectory"].update(identityLabel=True, sourceContextOnly=True)
        else:
            metadata["actorDirectory"].pop("identityLabel", None)
            metadata["actorDirectory"].pop("sourceContextOnly", None)
        if (previous is None or previous["displayName"] != identity["displayName"]
                or previous["aliases"] != aliases or previous["channel"] != identity["channel"]
                or is_pending):
            records.append({"kind": "profile", "id": f"{source['sourceId']}:actor:{_hash(actor_id)}:name",
                "order": order, "actorId": actor_id, "field": "name", "text": identity["displayName"],
                "knownBy": [] if context_only else [actor_id], "sourceSpans": source_spans, "metadata": metadata})
        resolved.append(resolved_actor)
        # A provisional entry is returned only for identities retained by the
        # current corrected panel. Removed and unanchored identities are pruned.
        if (lifecycle and registered is not None and not registered.get("legacy")
                and (is_new or is_pending)):
            registry_entries.append(final_registry_entry(registered, bind_to_current_panel=True))
        # Participation belongs to the complete accepted scene roster. Do not
        # duplicate it as personal knowledge or an extra embedding document.
    result = {"actors": resolved, "records": records}
    if lifecycle:
        result["characterRegistry"] = registry_entries
    return result
