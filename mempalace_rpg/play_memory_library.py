"""Host-owned saved-character identity and lifecycle projections.

The accepted scene/source metadata is an identity ledger, not a knowledge grant.
Library status is a separate non-text record so it cannot become retrieval text
or an actor dossier fact.
"""
from __future__ import annotations

import re
import unicodedata

from .play_memory_protocol import fail


REGISTRY_SCHEMA_VERSION = "play-memory-character-registry-v1"
LIBRARY_SCHEMA_VERSION = "play-memory-character-library-v1"
LIBRARY_POLICY = "panel-named-intelligent-v1"


def normalized_name(value):
    return unicodedata.normalize("NFC", value.strip())


def introduction_eligible(introduction):
    return (introduction["entityKind"] == "character"
            and introduction["hasProperName"] is True
            and introduction["intelligent"] is True)


def _identity_entry(value, path):
    if not isinstance(value, dict) or set(value) != {
            "actorId", "canonicalName", "aliases", "introduction", "firstAppearance"}:
        fail(f"{path} must have the exact character registry entry shape", "registry-integrity", -32009)
    actor_id, canonical_name = value["actorId"], value["canonicalName"]
    if not isinstance(actor_id, str) or not actor_id.strip():
        fail(f"{path}.actorId must be nonempty", "registry-integrity", -32009)
    if not isinstance(canonical_name, str) or not canonical_name.strip():
        fail(f"{path}.canonicalName must be nonempty", "registry-integrity", -32009)
    if normalized_name(canonical_name) != canonical_name:
        fail(f"{path}.canonicalName must be NFC-normalized and trimmed", "registry-integrity", -32009)
    aliases = value["aliases"]
    if (not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip()
            or normalized_name(alias) != alias for alias in aliases)
            or len(set(aliases)) != len(aliases) or canonical_name in aliases):
        fail(f"{path}.aliases must be distinct normalized names", "registry-integrity", -32009)
    introduction = value["introduction"]
    if not isinstance(introduction, dict) or set(introduction) != {
            "entityKind", "hasProperName", "intelligent", "plotImportant"}:
        fail(f"{path}.introduction has the wrong shape", "registry-integrity", -32009)
    if introduction["entityKind"] not in ("character", "monster"):
        fail(f"{path}.introduction.entityKind is invalid", "registry-integrity", -32009)
    if any(type(introduction[key]) is not bool
           for key in ("hasProperName", "intelligent", "plotImportant")):
        fail(f"{path}.introduction flags must be booleans", "registry-integrity", -32009)
    first = value["firstAppearance"]
    if not isinstance(first, dict) or set(first) - {"requestId", "panelHash", "basis"}:
        fail(f"{path}.firstAppearance has the wrong shape", "registry-integrity", -32009)
    if not isinstance(first.get("requestId"), str) or not first["requestId"].strip():
        fail(f"{path}.firstAppearance.requestId must be nonempty", "registry-integrity", -32009)
    if not isinstance(first.get("panelHash"), str) or re.fullmatch(r"[0-9a-f]{64}", first["panelHash"]) is None:
        fail(f"{path}.firstAppearance.panelHash must be a SHA-256 digest", "registry-integrity", -32009)
    basis = first.get("basis", "panel")
    if basis not in ("panel", "legacy-name-profile", "static-profile"):
        fail(f"{path}.firstAppearance.basis is invalid", "registry-integrity", -32009)
    return {
        "actorId": actor_id,
        "canonicalName": canonical_name,
        "aliases": list(aliases),
        "introduction": dict(introduction),
        "firstAppearance": {"requestId": first["requestId"], "panelHash": first["panelHash"], "basis": basis},
    }


def character_registry(records):
    """Read and validate the effective Host identity ledger without exposing it."""
    found = {}
    for record in records:
        metadata = record.get("metadata", {})
        if not isinstance(metadata, dict) or "characterRegistry" not in metadata:
            continue
        if record["kind"] not in ("scene", "source"):
            fail("characterRegistry is only valid on accepted scene or authored source records",
                 "registry-integrity", -32009)
        if record["kind"] == "source":
            if metadata.get("authored") is not True or metadata.get("characterRegistryImport") is not True:
                fail("source characterRegistry requires explicit Host-authored import metadata",
                     "registry-integrity", -32009)
        registry = metadata["characterRegistry"]
        if not isinstance(registry, dict) or set(registry) != {"schemaVersion", "entries"}:
            fail("characterRegistry has the wrong shape", "registry-integrity", -32009)
        if registry["schemaVersion"] != REGISTRY_SCHEMA_VERSION or not isinstance(registry["entries"], list):
            fail("characterRegistry schema version or entries are invalid", "registry-integrity", -32009)
        local = set()
        for index, raw in enumerate(registry["entries"]):
            entry = _identity_entry(raw, f"characterRegistry.entries[{index}]")
            actor_id = entry["actorId"]
            if actor_id in local:
                fail("characterRegistry repeats an actor identity", "registry-integrity", -32009)
            local.add(actor_id)
            basis = entry["firstAppearance"]["basis"]
            previous = found.get(actor_id)
            if record["kind"] == "scene" and basis != "panel" and previous is None:
                fail("scene may repeat non-panel provenance only for an accepted identity",
                     "registry-integrity", -32009)
            if record["kind"] == "source" and basis == "panel":
                fail("authored source imports must identify their non-panel provenance", "registry-integrity", -32009)
            if previous is not None:
                stable_fields = ("actorId", "canonicalName", "introduction", "firstAppearance")
                if any(previous[key] != entry[key] for key in stable_fields):
                    fail(f"character identity {actor_id} has conflicting first-appearance entries",
                         "registry-integrity", -32009)
                if not set(previous["aliases"]).issubset(entry["aliases"]):
                    fail(f"character identity {actor_id} aliases may only be added",
                         "registry-integrity", -32009)
                entry["aliases"] = list(dict.fromkeys([*previous["aliases"], *entry["aliases"]]))
            found[actor_id] = entry
    return found


def character_library_statuses(records):
    latest = {}
    for record in records:
        if record["kind"] != "character-library":
            continue
        previous = latest.get(record["actorId"])
        if previous is not None and previous["order"] == record["order"] and previous["id"] != record["id"]:
            fail("ambiguous character-library status order", "registry-integrity", -32009)
        if previous is None or previous["order"] < record["order"]:
            latest[record["actorId"]] = record
    return {actor_id: record["status"] for actor_id, record in latest.items()}


def _legacy_identities(records):
    identities = {}
    for record in records:
        if record["kind"] != "profile" or record["field"] != "name":
            continue
        actor_id = record["actorId"]
        current = identities.get(actor_id)
        if current is not None and current["order"] > record["order"]:
            continue
        metadata = record.get("metadata", {}).get("actorDirectory", {})
        aliases = metadata.get("aliases", []) if isinstance(metadata, dict) else []
        identities[actor_id] = {
            "actorId": actor_id,
            "canonicalName": record["text"],
            "aliases": list(dict.fromkeys(aliases)),
            "order": record["order"],
            "legacy": True,
        }
    return identities


def seen_identities(records):
    """Private resolver view. Legacy profiles are identities, never auto-classified."""
    result = _legacy_identities(records)
    for actor_id, entry in character_registry(records).items():
        legacy = result.get(actor_id, {})
        aliases = list(dict.fromkeys([
            *entry["aliases"],
            *(alias for alias in legacy.get("aliases", []) if alias != entry["canonicalName"]),
            *(name for name in [legacy.get("canonicalName")] if name and name != entry["canonicalName"]),
        ]))
        result[actor_id] = {**entry, "aliases": aliases, "legacy": False}
    return result


def memory_eligible(actor_id, identities, statuses):
    entry = identities.get(actor_id)
    if entry is None or entry.get("legacy"):
        return False
    if not introduction_eligible(entry["introduction"]):
        return False
    return statuses.get(actor_id, "saved") == "saved"


def blocked_memory_actors(records):
    identities = seen_identities(records)
    statuses = character_library_statuses(records)
    return {
        actor_id for actor_id, entry in identities.items()
        if statuses.get(actor_id) == "deleted"
        or (not entry.get("legacy") and not introduction_eligible(entry["introduction"]))
    }


def resolve_identity_name(name, identities, *, exclude_confirmed_unnamed=False):
    target = normalized_name(name)
    matches = []
    for actor_id, entry in identities.items():
        introduction = entry.get("introduction")
        if (exclude_confirmed_unnamed and isinstance(introduction, dict)
                and introduction.get("hasProperName") is False):
            continue
        if (target == normalized_name(entry["canonicalName"])
                or target in {normalized_name(alias) for alias in entry["aliases"]}):
            matches.append(actor_id)
    matches.sort()
    return matches


def validate_library_records(records, existing_records, lifecycle_policy=None):
    """Reject lifecycle writes that would revive hidden memory or bypass tagging."""
    combined = [*existing_records, *records]
    identities = seen_identities(combined)
    statuses = character_library_statuses(combined)
    for row in records:
        if row["kind"] == "character-library":
            actor_id = row["actorId"]
            entry = identities.get(actor_id)
            if entry is None:
                fail("character-library status references an unseen actor", "registry-integrity", -32009)
            if row["status"] == "saved" and (entry.get("legacy") or not introduction_eligible(entry["introduction"])):
                fail("only a tagged eligible actor can be saved in the character library",
                     "registry-integrity", -32009)
    for row in records:
        kind = row["kind"]
        if kind == "character-library":
            continue
        owners = []
        if kind in ("profile", "state", "acquisition", "attitude", "link", "exception"):
            if isinstance(row.get("actorId"), str):
                owners.append(row["actorId"])
        elif kind == "relationship" and isinstance(row.get("fromActorId"), str):
            owners.append(row["fromActorId"])
        for actor_id in owners:
            entry = identities.get(actor_id)
            if (entry is not None and (statuses.get(actor_id) == "deleted"
                    or not (entry.get("legacy") and lifecycle_policy is None)
                    and not memory_eligible(actor_id, identities, statuses))):
                fail(f"actor-owned memory update is not allowed for unsaved or excluded actor {actor_id}",
                     "character-memory-ineligible", -32009)
            if entry is None and lifecycle_policy == LIBRARY_POLICY:
                fail(f"actor-owned memory update has no tagged first appearance for {actor_id}",
                     "character-memory-unclassified", -32009)
        if "knownBy" in row:
            for actor_id in row["knownBy"]:
                entry = identities.get(actor_id)
                if (entry is not None and (statuses.get(actor_id) == "deleted"
                        or not (entry.get("legacy") and lifecycle_policy is None)
                        and not memory_eligible(actor_id, identities, statuses))):
                    fail(f"new memory grant targets unsaved or excluded actor {actor_id}",
                         "character-memory-ineligible", -32009)
                if entry is None and lifecycle_policy == LIBRARY_POLICY:
                    fail(f"new memory grant targets unclassified actor {actor_id}",
                         "character-memory-unclassified", -32009)


def character_library_projection(records, importance_policy, *, identity_records=None):
    identities = seen_identities(records)
    statuses = character_library_statuses(records)
    actors = []
    for actor_id, entry in sorted(identities.items()):
        if not memory_eligible(actor_id, identities, statuses):
            continue
        importance = importance_policy.classify(records, actor_id, identity_records=identity_records)
        actors.append({
            "actorId": actor_id,
            "displayName": entry["canonicalName"],
            "aliases": sorted(set(entry["aliases"])),
            "tier": importance["tier"],
            "tierOverride": importance["override"],
        })
    return {"schemaVersion": LIBRARY_SCHEMA_VERSION, "actors": actors}
