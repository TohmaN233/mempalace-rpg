"""Fail-closed scorer for frozen, label-blind AERP-7 ranking artifacts."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import tempfile
import unicodedata
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from benchmarks.aerp7_convomem_confirmation import CustodyError, canonical_sha256, validate_candidate_projection
from benchmarks.aerp7_convomem_rank import (
    CONFIDENCE_CONTRACT, CURRENT_ARMS, CURRENT_SERIALIZER, ORIGINAL_MEMPALACE_SERIALIZER,
    CANDIDATE_PROJECTION_REFERENCE_SCHEMA, PROTOCOL_SOURCE, RANKING_ARTIFACT_REFERENCE_SCHEMA,
    CandidateProjectionStore, validate_candidate_projection_reference,
    validate_frozen_ranking, validate_ranking_artifact_reference,
)

MANIFEST_SCHEMA = "aerp7-convomem-endpoint-manifest-v3"
CUSTODY_SCHEMA = "aerp7-convomem-custody-for-scoring-v2"
CUSTODY_REFERENCE_SCHEMA = "aerp7-convomem-custody-reference-v1"
SCHEMA = "aerp7-convomem-scoring-report-v3"
MAPPING_LEDGER_REFERENCE_SCHEMA = "aerp7-convomem-mapping-ledger-reference-v1"
MAPPING_LEDGER_READY_SCHEMA = "aerp7-convomem-mapping-ledger-ready-v1"
MAPPING_LEDGER_FORMAT = "canonical-json-array-v1"
UPSTREAM_GROUPS = {
    "user_evidence": "positive", "assistant_facts_evidence": "positive",
    "changing_evidence": "positive", "preference_evidence": "positive",
    "implicit_connection_evidence": "positive", "abstention_evidence": "abstention",
}
_FORBIDDEN_REPORT_KEYS = frozenset({"text", "speaker", "answer", "source_locator", "query_text", "evidence_spans", "evidence_conversation_ids", "message_id", "ranked_message_ids", "messages"})
_METRIC_KEYS = ("recall_at_10", "hit_at_10", "all_at_10", "ndcg_at_10", "mrr_at_10")
FORMAL_ARMS = ("original_public_product", "strong_raw", "static_p5", "six_view_secondary")
FORMAL_BOOTSTRAP = {"resamples": 10000, "percentile_lower": 0.025, "percentile_upper": 0.975, "percentile_rule": "linear", "original_replicate_rule": "global_build_multiset_per_draw", "seed_derivation": "sha256(protocol_sha256|persona-bootstrap-v1)"}


def formal_bootstrap(protocol_sha256: str) -> dict[str, Any]:
    """Derive the one preregistered formal RNG stream from the sealed protocol."""
    _h(protocol_sha256, "formal_bootstrap_protocol_digest_invalid")
    seed = int.from_bytes(hashlib.sha256((protocol_sha256 + "|persona-bootstrap-v1").encode("utf-8")).digest()[:8], "big")
    return {"seed": seed, **FORMAL_BOOTSTRAP}


def normalize_v1(value: str) -> str:
    if not isinstance(value, str): raise CustodyError("normalization_input_invalid")
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _d(value: Any) -> str: return canonical_sha256(value)


def _o(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping): raise CustodyError(code)
    return dict(value)


def _l(value: Any, code: str) -> list[Any]:
    if not isinstance(value, list): raise CustodyError(code)
    return value


def _h(value: Any, code: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value): raise CustodyError(code)
    return value


def _integer(value: Any, code: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (positive and value <= 0): raise CustodyError(code)
    return value


def _number(value: Any, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)): raise CustodyError(code)
    return float(value)


def artifact_digest(value: Mapping[str, Any]) -> str: return _d({key: item for key, item in value.items() if key != "artifact_sha256"})
def endpoint_manifest_digest(value: Mapping[str, Any]) -> str: return _d({key: item for key, item in value.items() if key != "manifest_sha256"})
def report_digest(value: Mapping[str, Any]) -> str: return _d({key: item for key, item in value.items() if key != "report_sha256"})


def validate_endpoint_manifest(value: Any, *, projection_sha256: str) -> dict[str, Any]:
    row = _o(value, "endpoint_manifest_invalid")
    keys = {"schema", "projection_sha256", "protocol_source", "serializer_contract", "arms", "directory_endpoints", "bootstrap", "synthetic_test_mode", "reference_arm", "manifest_sha256"}
    if set(row) != keys or row.get("schema") != MANIFEST_SCHEMA or _h(row.get("projection_sha256"), "endpoint_manifest_projection_digest_invalid") != projection_sha256:
        raise CustodyError("endpoint_manifest_schema_invalid")
    if row.get("protocol_source") != PROTOCOL_SOURCE or row.get("serializer_contract") != {"current": CURRENT_SERIALIZER, "original_public_product": ORIGINAL_MEMPALACE_SERIALIZER}:
        raise CustodyError("endpoint_manifest_contract_invalid")
    arms: dict[str, dict[str, Any]] = {}
    for arm in _l(row.get("arms"), "endpoint_manifest_arms_invalid"):
        arm = _o(arm, "endpoint_manifest_arm_invalid")
        if set(arm) != {"arm_id", "ranking_artifact_sha256", "confidence_contract"} or not isinstance(arm.get("arm_id"), str) or arm["arm_id"] in arms:
            raise CustodyError("endpoint_manifest_arm_invalid")
        _h(arm.get("ranking_artifact_sha256"), "endpoint_manifest_arm_invalid")
        expected = CONFIDENCE_CONTRACT if arm["arm_id"] in CURRENT_ARMS else None if arm["arm_id"] == "original_public_product" else "__invalid__"
        if arm.get("confidence_contract") != expected: raise CustodyError("endpoint_manifest_arm_contract_invalid")
        arms[arm["arm_id"]] = arm
    if row["synthetic_test_mode"] is False and tuple(arms) != FORMAL_ARMS:
        raise CustodyError("formal_manifest_freeze_invalid")
    if not arms or "original_public_product" not in arms or row.get("reference_arm") not in arms:
        raise CustodyError("endpoint_manifest_arm_registry_invalid")
    endpoints: dict[str, str] = {}
    for endpoint in _l(row.get("directory_endpoints"), "endpoint_manifest_endpoint_invalid"):
        endpoint = _o(endpoint, "endpoint_manifest_endpoint_invalid")
        if set(endpoint) != {"directory_group", "endpoint"} or endpoint["directory_group"] in endpoints or endpoint.get("endpoint") not in {"positive", "abstention"}:
            raise CustodyError("endpoint_manifest_endpoint_invalid")
        endpoints[endpoint["directory_group"]] = endpoint["endpoint"]
    if endpoints != UPSTREAM_GROUPS or not isinstance(row.get("synthetic_test_mode"), bool): raise CustodyError("endpoint_manifest_endpoint_invalid")
    bootstrap = _o(row.get("bootstrap"), "endpoint_manifest_bootstrap_invalid")
    if set(bootstrap) not in ({"seed", "resamples", "percentile_lower", "percentile_upper", "percentile_rule", "original_replicate_rule"}, {"seed", "resamples", "percentile_lower", "percentile_upper", "percentile_rule", "original_replicate_rule", "seed_derivation"}):
        raise CustodyError("endpoint_manifest_bootstrap_invalid")
    _integer(bootstrap.get("seed"), "endpoint_manifest_bootstrap_invalid"); _integer(bootstrap.get("resamples"), "endpoint_manifest_bootstrap_invalid", positive=True)
    low, high = _number(bootstrap.get("percentile_lower"), "endpoint_manifest_bootstrap_invalid"), _number(bootstrap.get("percentile_upper"), "endpoint_manifest_bootstrap_invalid")
    if bootstrap.get("percentile_rule") != "linear" or bootstrap.get("original_replicate_rule") not in {"per_query_arithmetic_mean", "global_build_multiset_per_draw"} or not 0 <= low < high <= 1:
        raise CustodyError("endpoint_manifest_bootstrap_invalid")
    if row["synthetic_test_mode"] is False:
        if tuple(arms) != FORMAL_ARMS or row["reference_arm"] != "six_view_secondary" or set(bootstrap) != {"seed", *FORMAL_BOOTSTRAP} or {key: bootstrap[key] for key in FORMAL_BOOTSTRAP} != FORMAL_BOOTSTRAP:
            raise CustodyError("formal_manifest_freeze_invalid")
    if row.get("manifest_sha256") != endpoint_manifest_digest(row): raise CustodyError("endpoint_manifest_digest_mismatch")
    return row


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _is_custody_store(value: Any) -> bool:
    """Recognize the confirmation-owned streaming custody cursor lazily."""
    try:
        from benchmarks.aerp7_convomem_confirmation import CustodyStore
    except ImportError:  # pragma: no cover - confirmation is a runtime dependency
        return False
    return isinstance(value, CustodyStore)


def _custody_reference(value: Any) -> Mapping[str, Any]:
    if not _is_custody_store(value):
        raise CustodyError("scoring_custody_store_required")
    reference = getattr(value, "reference", None)
    required = {
        "schema", "bundle_path", "candidate_reference", "custody_path", "ready_path",
        "generation_id", "custody_raw_sha256", "custody_canonical_sha256", "dataset",
        "item_count", "evidence_span_count", "ready_sha256",
    }
    if not isinstance(reference, Mapping) or set(reference) != required or reference.get("schema") != CUSTODY_REFERENCE_SCHEMA:
        raise CustodyError("scoring_custody_reference_invalid")
    try:
        validate_candidate_projection_reference(reference["candidate_reference"])
    except CustodyError as exc:
        raise CustodyError("scoring_custody_reference_invalid") from exc
    for key in ("custody_raw_sha256", "custody_canonical_sha256", "ready_sha256"):
        _h(reference.get(key), "scoring_custody_reference_invalid")
    for key in ("item_count", "evidence_span_count"):
        _integer(reference.get(key), "scoring_custody_reference_invalid")
        if reference[key] < 0:
            raise CustodyError("scoring_custody_reference_invalid")
    return reference


class _ProjectionAccess:
    """Small adapter over either the legacy map or a persistent candidate store.

    The formal path receives a :class:`CandidateProjectionStore`; it never
    asks this scorer to load ``projection.json`` into a Python object.  The
    legacy map path is retained for the synthetic fixtures and old callers.
    """

    def __init__(self, projection: Any) -> None:
        self.store: CandidateProjectionStore | None = projection if isinstance(projection, CandidateProjectionStore) else None
        if self.store is not None:
            self.reference = validate_candidate_projection_reference(self.store.reference)
            self.inline: dict[str, Any] | None = None
            self.digest = self.reference["projection_canonical_sha256"]
            self.query_count = self.reference["query_count"]
            self.candidate_text_count = self.reference["candidate_text_count"]
            self._corpora: dict[str, dict[str, Any]] = {}
            self._corpus_cache: dict[str, dict[str, Any]] = {}
            return
        if isinstance(projection, Mapping) and projection.get("schema") == CANDIDATE_PROJECTION_REFERENCE_SCHEMA:
            # A reference is not sufficient to score: the store is the
            # persistent, bounded-memory consumer and must be opened by the
            # custody/coordinator boundary in its configured staging root.
            raise CustodyError("scoring_candidate_store_required")
        self.inline = validate_candidate_projection(projection)
        self.reference = None
        self.digest = _d(self.inline)
        self.query_count = len(self.inline["items"])
        self.candidate_text_count = sum(len(corpus["candidates"]) for corpus in self.inline["corpora"])
        self._items = {item["item_id"]: item for item in self.inline["items"]}
        self._corpora = {corpus["corpus_id"]: corpus for corpus in self.inline["corpora"]}

    def iter_items(self) -> Iterator[dict[str, Any]]:
        if self.store is not None:
            for item in self.store.iter_items():
                yield dict(item)
            return
        assert self.inline is not None
        for item in self.inline["items"]:
            yield dict(item)

    def item(self, item_id: str) -> dict[str, Any] | None:
        if self.store is not None:
            try:
                row = self.store.connection.execute("SELECT payload_json FROM items WHERE item_id=?", (item_id,)).fetchone()
            except sqlite3.Error as exc:
                raise CustodyError("candidate_store_item_invalid") from exc
            if row is None:
                return None
            try:
                value = json.loads(row[0])
            except (TypeError, json.JSONDecodeError) as exc:
                raise CustodyError("candidate_store_item_invalid") from exc
            return value if isinstance(value, dict) else None
        return self._items.get(item_id)

    def corpus(self, corpus_id: str) -> dict[str, Any]:
        if self.store is not None:
            cached = self._corpus_cache.get(corpus_id)
            if cached is not None:
                return cached
            value = self.store.corpus(corpus_id)
            # A one-corpus cache bounds memory by the largest corpus rather
            # than retaining a slice of the 20+GB projection in the scorer.
            if len(self._corpus_cache) >= 1:
                self._corpus_cache.pop(next(iter(self._corpus_cache)))
            self._corpus_cache[corpus_id] = value
            return value
        try:
            return self._corpora[corpus_id]
        except KeyError as exc:
            raise CustodyError("candidate_projection_corpus_missing") from exc

    def corpus_metadata(self, corpus_id: str) -> dict[str, Any]:
        if self.store is None:
            return self.corpus(corpus_id)
        try:
            row = self.store.connection.execute("SELECT payload_json FROM corpora WHERE corpus_id=?", (corpus_id,)).fetchone()
        except sqlite3.Error as exc:
            raise CustodyError("candidate_store_corpus_invalid") from exc
        if row is None:
            raise CustodyError("candidate_projection_corpus_missing")
        try:
            value = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CustodyError("candidate_store_corpus_invalid") from exc
        if not isinstance(value, dict):
            raise CustodyError("candidate_store_corpus_invalid")
        return value


class _ObservedScoringConnection:
    """A deliberately small proxy which observes SQLite *during* mutations.

    Looking at ``score.sqlite3`` after the connection closes misses a rollback
    journal entirely.  The proxy brackets every connection operation and commit
    so the database, ``-journal``, WAL and SHM files are sampled while SQLite
    owns them.  It otherwise preserves the sqlite connection surface used by
    the scorer.
    """

    def __init__(self, connection: sqlite3.Connection, owner: "_ScoringDB") -> None:
        self._connection = connection
        self._owner = owner

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._owner._observe_footprint()
        try:
            return self._connection.execute(*args, **kwargs)
        finally:
            self._owner._observe_footprint()

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._owner._observe_footprint()
        try:
            return self._connection.executemany(*args, **kwargs)
        finally:
            self._owner._observe_footprint()

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        self._owner._observe_footprint()
        try:
            return self._connection.executescript(*args, **kwargs)
        finally:
            self._owner._observe_footprint()

    def commit(self) -> None:
        self._owner._observe_footprint()
        try:
            self._connection.commit()
        finally:
            self._owner._observe_footprint()

    def close(self) -> None:
        self._owner._observe_footprint()
        try:
            self._connection.close()
        finally:
            self._owner._observe_footprint()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


class _ScoringDB:
    """Ephemeral disk spool for custody rows, confidence pairs and ledgers."""

    def __init__(self, *, observe_capacity: bool = False) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="aerp7-scoring-")
        self.path = Path(self._tmp.name) / "score.sqlite3"
        self._observe_capacity = observe_capacity
        self._peak_footprint_bytes = 0
        self._raw_connection = sqlite3.connect(self.path)
        # The formal scorer's normal path deliberately retains sqlite's native
        # connection.  Capacity calibration opts into the bracketed proxy; it
        # is not allowed to add path stats, sampling failures, or call-shape
        # changes to scientific scoring.
        self.connection: Any = (
            _ObservedScoringConnection(self._raw_connection, self)
            if observe_capacity else self._raw_connection
        )
        self.connection.executescript(
            """
            CREATE TABLE projection_items(
                item_id TEXT PRIMARY KEY, persona_id TEXT NOT NULL,
                corpus_id TEXT NOT NULL, declared_context_size INTEGER NOT NULL,
                actual_conversation_count INTEGER NOT NULL,
                actual_message_count INTEGER NOT NULL, query_text TEXT NOT NULL
            );
            CREATE TABLE custody(
                item_id TEXT PRIMARY KEY, directory_group TEXT NOT NULL,
                conversations_json TEXT NOT NULL, mappings_json TEXT NOT NULL
            );
            CREATE TABLE ledger(
                ordinal INTEGER PRIMARY KEY, item_id TEXT NOT NULL,
                evidence_token TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE artifact_seen(
                arm_id TEXT NOT NULL, item_id TEXT NOT NULL,
                PRIMARY KEY(arm_id, item_id)
            );
            CREATE TABLE preflight_rank(
                arm_id TEXT NOT NULL, item_id TEXT NOT NULL,
                query_sha256 TEXT NOT NULL, candidate_input_sha256 TEXT NOT NULL,
                ranked_count INTEGER NOT NULL, ranking_sha256 TEXT NOT NULL,
                PRIMARY KEY(arm_id, item_id)
            );
            CREATE TABLE preflight_trace(
                arm_id TEXT NOT NULL, item_id TEXT NOT NULL,
                PRIMARY KEY(arm_id, item_id)
            );
            CREATE TABLE confidence(
                arm_id TEXT NOT NULL, persona_id TEXT NOT NULL,
                declared_context_size INTEGER NOT NULL,
                confidence REAL NOT NULL, label INTEGER NOT NULL
            );
            CREATE INDEX confidence_stratum
                ON confidence(arm_id, persona_id, declared_context_size, confidence);
            """
        )
        self.connection.commit()

    def _observe_footprint(self) -> int:
        if not self._observe_capacity:
            return 0
        total = 0
        for path in (
            self.path,
            self.path.with_name(self.path.name + "-journal"),
            self.path.with_name(self.path.name + "-wal"),
            self.path.with_name(self.path.name + "-shm"),
        ):
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        self._peak_footprint_bytes = max(self._peak_footprint_bytes, total)
        return total

    @property
    def peak_footprint_bytes(self) -> int:
        # Take one final active-connection observation; the peak itself remains
        # the maximum sampled around every write/commit above.
        self._observe_footprint()
        return self._peak_footprint_bytes

    def close(self) -> None:
        try:
            self.connection.close()
        finally:
            self._tmp.cleanup()

    def __enter__(self) -> "_ScoringDB":
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


class _CustodyIndex:
    def __init__(self, db: _ScoringDB, *, ledger_count: int) -> None:
        self.db = db
        self.ledger_count = ledger_count

    def get(self, item_id: str) -> tuple[str, list[str], list[dict[str, Any]]]:
        row = self.db.connection.execute(
            "SELECT directory_group, conversations_json, mappings_json FROM custody WHERE item_id=?",
            (item_id,),
        ).fetchone()
        if row is None:
            raise CustodyError("scoring_custody_item_coverage_invalid")
        try:
            conversations = json.loads(row[1]); mappings = json.loads(row[2])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CustodyError("scoring_custody_item_invalid") from exc
        if not isinstance(conversations, list) or not isinstance(mappings, list):
            raise CustodyError("scoring_custody_item_invalid")
        return str(row[0]), conversations, mappings

    def iter_ledger(self) -> Iterator[dict[str, Any]]:
        for item_id, token, status in self.db.connection.execute(
            "SELECT item_id, evidence_token, status FROM ledger ORDER BY ordinal"
        ):
            yield {"item_id": item_id, "evidence_token": token, "status": status}

    def legacy_ledger(self) -> list[dict[str, Any]]:
        """Compatibility-only materialization for the small inline fixtures."""
        return list(self.iter_ledger())


def _write_mapping_ledger_reference(
    index: _CustodyIndex, *, path: Path, projection_digest: str,
) -> dict[str, Any]:
    """Publish the label-free mapping ledger without retaining its rows.

    The output is an ordinary persisted report sidecar.  It is written through
    a same-directory temporary file and published only after its count/digest
    are complete; an existing exact sidecar is accepted for an idempotent
    retry, while a different sidecar fails closed.
    """
    # Resolve only after rejecting relative paths.  A relative path would make
    # the report's persisted handle dependent on the caller's future cwd.
    if not isinstance(path, Path) or not path.is_absolute():
        raise CustodyError("scoring_mapping_ledger_output_invalid")
    if path.is_symlink():
        raise CustodyError("scoring_mapping_ledger_output_invalid")
    path = path.resolve()
    ready_path = path.with_name(path.stem + ".READY.json")
    if path.is_symlink() or ready_path.is_symlink() or not path.parent.is_dir():
        raise CustodyError("scoring_mapping_ledger_output_invalid")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{id(index)}")
    ready_temporary = ready_path.with_name(f".{ready_path.name}.tmp-{os.getpid()}-{id(index)}")
    published_payload = False
    published_ready = False
    digest = hashlib.sha256(); digest.update(b"["); first = True; count = 0
    try:
        with temporary.open("xb") as stream:
            stream.write(b"[")
            for row in index.iter_ledger():
                payload = _json(row).encode("utf-8")
                if not first:
                    stream.write(b","); digest.update(b",")
                stream.write(payload); digest.update(payload); first = False; count += 1
            stream.write(b"]"); digest.update(b"]"); stream.flush(); os.fsync(stream.fileno())
        file_digest = digest.hexdigest()
        ready_unsigned = {
            "schema": MAPPING_LEDGER_READY_SCHEMA,
            "format": MAPPING_LEDGER_FORMAT,
            "projection_sha256": projection_digest,
            "count": count,
            "sha256": file_digest,
        }
        ready_digest = _d(ready_unsigned)
        ready_payload = _json({**ready_unsigned, "ready_sha256": ready_digest}).encode("utf-8")
        with ready_temporary.open("xb") as stream:
            stream.write(ready_payload); stream.flush(); os.fsync(stream.fileno())
        if path.exists() or ready_path.exists():
            if (
                path.is_symlink() or not path.is_file() or ready_path.is_symlink() or not ready_path.is_file()
                or _stream_path_sha256(path) != file_digest
                or ready_path.read_bytes() != ready_payload
            ):
                raise CustodyError("scoring_mapping_ledger_output_conflict")
            temporary.unlink()
            ready_temporary.unlink()
        else:
            try:
                os.link(temporary, path)
                published_payload = True
                temporary.unlink()
                os.link(ready_temporary, ready_path)
                published_ready = True
                ready_temporary.unlink()
            except FileExistsError:
                if (
                    path.is_symlink() or not path.is_file() or ready_path.is_symlink() or not ready_path.is_file()
                    or _stream_path_sha256(path) != file_digest
                    or ready_path.read_bytes() != ready_payload
                ):
                    raise CustodyError("scoring_mapping_ledger_output_conflict")
                temporary.unlink()
                ready_temporary.unlink()
        published_payload = False
        published_ready = False
        return {
            "schema": MAPPING_LEDGER_REFERENCE_SCHEMA,
            "format": MAPPING_LEDGER_FORMAT,
            "path": str(path),
            "ready_path": str(ready_path),
            "projection_sha256": projection_digest,
            "count": count,
            "sha256": file_digest,
            "ready_sha256": ready_digest,
        }
    except CustodyError:
        raise
    except OSError as exc:
        raise CustodyError("scoring_mapping_ledger_publish_failed") from exc
    finally:
        if published_ready:
            try:
                ready_path.unlink()
            except FileNotFoundError:
                pass
        if published_payload:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            ready_temporary.unlink()
        except FileNotFoundError:
            pass


def _stream_path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CustodyError("scoring_mapping_ledger_read_failed") from exc
    return digest.hexdigest()


def _validate_ledger_entry(value: Any) -> dict[str, Any]:
    entry = _o(value, "scoring_report_ledger_invalid")
    if set(entry) != {"item_id", "evidence_token", "status"} or entry["status"] not in {"mapped", "unmatched", "ambiguous"}:
        raise CustodyError("scoring_report_ledger_invalid")
    _h(entry["item_id"], "scoring_report_ledger_invalid")
    _h(entry["evidence_token"], "scoring_report_ledger_invalid")
    return entry


def _validate_ledger_entries(entries: Iterable[Any]) -> int:
    """Validate legacy rows through a disk-backed uniqueness index."""
    temporary = tempfile.TemporaryDirectory(prefix="aerp7-ledger-validate-")
    connection = sqlite3.connect(Path(temporary.name) / "seen.sqlite3")
    try:
        connection.execute("CREATE TABLE seen(item_id TEXT NOT NULL, evidence_token TEXT NOT NULL, PRIMARY KEY(item_id, evidence_token))")
        count = 0
        for value in entries:
            entry = _validate_ledger_entry(value)
            try:
                connection.execute("INSERT INTO seen VALUES (?, ?)", (entry["item_id"], entry["evidence_token"]))
            except sqlite3.IntegrityError as exc:
                raise CustodyError("scoring_report_ledger_duplicate") from exc
            count += 1
        connection.commit()
        return count
    finally:
        connection.close(); temporary.cleanup()


def _validate_mapping_ledger_reference(value: Any, *, projection_digest: str) -> dict[str, Any]:
    reference = _o(value, "scoring_report_ledger_reference_invalid")
    required = {"schema", "format", "path", "ready_path", "projection_sha256", "count", "sha256", "ready_sha256"}
    if set(reference) != required or reference.get("schema") != MAPPING_LEDGER_REFERENCE_SCHEMA or reference.get("format") != MAPPING_LEDGER_FORMAT:
        raise CustodyError("scoring_report_ledger_reference_invalid")
    path_value, ready_value = reference.get("path"), reference.get("ready_path")
    if not isinstance(path_value, str) or not isinstance(ready_value, str) or not Path(path_value).is_absolute() or not Path(ready_value).is_absolute():
        raise CustodyError("scoring_report_ledger_reference_invalid")
    path, ready_path = Path(path_value), Path(ready_value)
    if path.parent != ready_path.parent or path.is_symlink() or not path.is_file() or ready_path.is_symlink() or not ready_path.is_file() or reference.get("projection_sha256") != projection_digest:
        raise CustodyError("scoring_report_ledger_reference_invalid")
    _integer(reference.get("count"), "scoring_report_ledger_reference_invalid")
    if reference["count"] < 0:
        raise CustodyError("scoring_report_ledger_reference_invalid")
    _h(reference.get("sha256"), "scoring_report_ledger_reference_invalid")
    _h(reference.get("ready_sha256"), "scoring_report_ledger_reference_invalid")
    if _stream_path_sha256(path) != reference["sha256"]:
        raise CustodyError("scoring_report_ledger_reference_drift")
    try:
        ready_raw = ready_path.read_bytes()
        ready = json.loads(ready_raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CustodyError("scoring_report_ledger_ready_invalid") from exc
    ready_unsigned = {
        "schema": MAPPING_LEDGER_READY_SCHEMA,
        "format": MAPPING_LEDGER_FORMAT,
        "projection_sha256": reference["projection_sha256"],
        "count": reference["count"],
        "sha256": reference["sha256"],
    }
    if ready_raw != _json(ready).encode("utf-8") or ready != {**ready_unsigned, "ready_sha256": reference["ready_sha256"]} or reference["ready_sha256"] != _d(ready_unsigned):
        raise CustodyError("scoring_report_ledger_ready_invalid")
    parser = _ijson(); count = 0
    try:
        with path.open("rb") as stream:
            count = _validate_ledger_entries(parser.items(stream, "item", use_float=True))
    except CustodyError:
        raise
    except (OSError, ValueError) as exc:
        raise CustodyError("scoring_report_ledger_reference_invalid") from exc
    if count != reference["count"]:
        raise CustodyError("scoring_report_ledger_reference_count_invalid")
    return reference


def _prepare_projection_items(access: _ProjectionAccess, db: _ScoringDB) -> None:
    count = 0
    for item in access.iter_items():
        required = ("item_id", "persona_id", "corpus_id")
        if any(not isinstance(item.get(key), str) or not item[key] for key in required):
            raise CustodyError("candidate_projection_item_invalid")
        corpus = access.corpus_metadata(item["corpus_id"])
        values = (
            item["item_id"], item["persona_id"], item["corpus_id"],
            corpus.get("declared_context_size"), corpus.get("actual_conversation_count"),
            corpus.get("actual_message_count"), item.get("query_text"),
        )
        if not isinstance(values[6], str) or not values[6].strip() or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values[3:6]):
            raise CustodyError("candidate_projection_corpus_invalid")
        try:
            db.connection.execute(
                "INSERT INTO projection_items VALUES (?, ?, ?, ?, ?, ?, ?)", values
            )
        except sqlite3.IntegrityError as exc:
            raise CustodyError("candidate_projection_item_duplicate") from exc
        count += 1
    if count != access.query_count:
        raise CustodyError("candidate_projection_query_denominator_invalid")
    db.connection.commit()


def _prepare_custody_rows(
    access: _ProjectionAccess, custody_items: Iterable[Any], secret: bytes, *,
    formal_live: bool, db: _ScoringDB,
) -> _CustodyIndex:
    """Resolve one custody cursor at a time, keeping only compact rows on disk."""
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise CustodyError("scoring_secret_too_short")
    ordinal = 0
    for raw in custody_items:
        item = _o(raw, "scoring_custody_item_invalid")
        required = {"item_id", "directory_group", "evidence_conversation_ids", "evidence_spans"}
        item_id = item.get("item_id")
        if set(item) != required or not isinstance(item_id, str) or item_id == item_id.strip() == "" or item.get("directory_group") not in UPSTREAM_GROUPS:
            raise CustodyError("scoring_custody_item_invalid")
        projection_item = db.connection.execute(
            "SELECT corpus_id FROM projection_items WHERE item_id=?", (item_id,)
        ).fetchone()
        if projection_item is None:
            raise CustodyError("scoring_custody_item_invalid")
        corpus = access.corpus(str(projection_item[0]))
        endpoint = UPSTREAM_GROUPS[item["directory_group"]]
        conversations = _l(item["evidence_conversation_ids"], "scoring_custody_conversations_invalid")
        allowed = {candidate.get("opaque_conversation_id") for candidate in corpus["candidates"]}
        if len(conversations) != len(set(conversations)) or set(conversations) - allowed:
            raise CustodyError("scoring_custody_conversations_invalid")
        spans = _l(item["evidence_spans"], "scoring_custody_evidence_invalid")
        if endpoint == "positive" and not spans:
            raise CustodyError("positive_evidence_span_missing")
        if endpoint == "abstention" and (spans or conversations):
            raise CustodyError("abstention_evidence_must_be_empty")
        mappings: list[dict[str, Any]] = []
        for span_ordinal, raw_span in enumerate(spans):
            span = _o(raw_span, "scoring_custody_evidence_invalid")
            if set(span) != {"speaker", "text"} or not isinstance(span["speaker"], str) or not isinstance(span["text"], str):
                raise CustodyError("scoring_custody_evidence_invalid")
            hits = [
                candidate["message_id"] for candidate in corpus["candidates"]
                if candidate["opaque_conversation_id"] in conversations
                and (normalize_v1(candidate["speaker"]), normalize_v1(candidate["text"])) == (normalize_v1(span["speaker"]), normalize_v1(span["text"]))
            ]
            status = "mapped" if len(hits) == 1 else "unmatched" if not hits else "ambiguous"
            private = {"status": status}
            if status == "mapped":
                private["message_id"] = hits[0]
            mappings.append(private)
            token = hmac.new(secret, f"aerp7-public-ledger/v1/{item_id}/{span_ordinal}".encode("utf-8"), hashlib.sha256).hexdigest()
            db.connection.execute(
                "INSERT INTO ledger VALUES (?, ?, ?, ?)", (ordinal, item_id, token, status)
            )
            ordinal += 1
        try:
            db.connection.execute(
                "INSERT INTO custody VALUES (?, ?, ?, ?)",
                (item_id, item["directory_group"], _json(list(conversations)), _json(mappings)),
            )
        except sqlite3.IntegrityError as exc:
            raise CustodyError("scoring_custody_item_invalid") from exc
        if formal_live and any(mapping["status"] != "mapped" for mapping in mappings):
            raise CustodyError("scoring_exact_evidence_mapping_incomplete")
    count = db.connection.execute("SELECT COUNT(*) FROM custody").fetchone()[0]
    if count != access.query_count:
        raise CustodyError("scoring_custody_item_coverage_invalid")
    db.connection.commit()
    return _CustodyIndex(db, ledger_count=ordinal)


def _prepare_custody(
    access: _ProjectionAccess, custody: Any, secret: bytes, *, formal_live: bool, db: _ScoringDB,
) -> _CustodyIndex:
    """Accept only the small legacy map or the confirmation CustodyStore."""
    if _is_custody_store(custody):
        reference = _custody_reference(custody)
        candidate_reference = reference.get("candidate_reference")
        if not isinstance(candidate_reference, Mapping) or candidate_reference.get("projection_canonical_sha256") != access.digest:
            raise CustodyError("scoring_custody_projection_binding_invalid")
        if access.reference is not None and dict(candidate_reference) != access.reference:
            raise CustodyError("scoring_custody_candidate_reference_invalid")
        if reference.get("item_count") != access.query_count:
            raise CustodyError("scoring_custody_item_coverage_invalid")
        index = _prepare_custody_rows(
            access, custody.iter_scoring_items(), secret, formal_live=formal_live, db=db,
        )
        # The signed custody reference carries the expected label-span
        # denominator.  Bind it to the streamed ledger before ranking rows are
        # consumed so truncation/duplication cannot be hidden by a later
        # report-side count.
        evidence_span_count = reference.get("evidence_span_count")
        if isinstance(evidence_span_count, bool) or not isinstance(evidence_span_count, int) or evidence_span_count < 0 or evidence_span_count != index.ledger_count:
            raise CustodyError("scoring_custody_ledger_count_invalid")
        return index
    row = _o(custody, "scoring_custody_invalid")
    if set(row) != {"schema", "projection_sha256", "items"} or row.get("schema") != CUSTODY_SCHEMA or row.get("projection_sha256") != access.digest:
        raise CustodyError("scoring_custody_schema_invalid")
    custody_items = _l(row["items"], "scoring_custody_items_invalid")
    return _prepare_custody_rows(
        access, custody_items, secret, formal_live=formal_live, db=db,
    )


def _ijson() -> Any:
    try:
        import ijson
    except ImportError as exc:  # pragma: no cover - pinned runtime dependency
        raise CustodyError("scoring_streaming_dependency_missing") from exc
    return ijson


def _stream_json_array(path: Path, prefix: str) -> Iterator[dict[str, Any]]:
    parser = _ijson()
    try:
        with path.open("rb") as handle:
            for value in parser.items(handle, prefix, use_float=True):
                if not isinstance(value, Mapping):
                    raise CustodyError("ranking_artifact_stream_row_invalid")
                yield dict(value)
    except OSError as exc:
        raise CustodyError("ranking_artifact_stream_read_failed") from exc


class _ArtifactReader:
    """Uniform row cursor for inline artifacts and READY-bound references."""

    def __init__(self, value: Any, *, projection_digest: str, projection: Any) -> None:
        self.value = value
        self.inline: dict[str, Any] | None = None
        self.reference: dict[str, Any] | None = None
        self.kind: str
        self._original_refs: list[dict[str, Any]] = []
        # The canonical original lane publishes one small artifact reference
        # whose five replicate references point at durable SQLite stores.  It
        # is deliberately handled before the compatibility wrappers below so
        # the scorer consumes the canonical handle without materializing the
        # published artifact or its rankings.
        if isinstance(value, Mapping) and value.get("schema") == "aerp7-original-product-artifact-reference-v1":
            try:
                from benchmarks import aerp7_original_product as original_product
                checked = original_product.validate_original_public_artifact_reference(value)
            except Exception as exc:
                if isinstance(exc, CustodyError):
                    raise
                raise CustodyError("original_artifact_reference_invalid") from exc
            if checked["candidate_reference"]["projection_canonical_sha256"] != projection_digest:
                raise CustodyError("original_artifact_projection_binding_invalid")
            self._original_refs = [self._check_original_reference(ref) for ref in checked["replicate_references"]]
            self.arm_id = "original_public_product"
            self.artifact_sha256 = checked["artifact_sha256"]
            self.kind = "original"
            return
        if isinstance(value, list) and len(value) == 5 and all(isinstance(item, Mapping) and item.get("schema") == "aerp7-original-product-replicate-reference-v1" for item in value):
            self._original_refs = [self._check_original_reference(item) for item in value]
            self.arm_id = "original_public_product"
            self.artifact_sha256 = _d({"replicates": self._original_refs})
            self.kind = "original"
            return
        if isinstance(value, Mapping) and value.get("schema") == RANKING_ARTIFACT_REFERENCE_SCHEMA:
            self.reference = validate_ranking_artifact_reference(value, expected_projection_sha256=projection_digest)
            self.arm_id = self.reference["arm_id"]
            self.artifact_sha256 = self.reference["artifact_sha256"]
            self.kind = "current"
            return
        if isinstance(value, Mapping) and value.get("schema") == "aerp7-original-product-replicate-reference-v1":
            self._original_refs = [self._check_original_reference(value)]
            self.arm_id = "original_public_product"
            self.artifact_sha256 = self._original_refs[0]["replicate_sha256"]
            self.kind = "original"
            return
        if isinstance(value, Mapping) and value.get("arm_id") == "original_public_product":
            refs = value.get("replicate_references")
            if refs is None:
                refs = value.get("replicate_refs")
            if refs is None and isinstance(value.get("replicates"), list) and all(isinstance(item, Mapping) and item.get("schema") == "aerp7-original-product-replicate-reference-v1" for item in value["replicates"]):
                refs = value["replicates"]
            if refs is not None:
                if not isinstance(refs, list) or len(refs) != 5:
                    raise CustodyError("original_replicate_reference_schema_invalid")
                self._original_refs = [self._check_original_reference(ref) for ref in refs]
                self.arm_id = "original_public_product"
                self.artifact_sha256 = value.get("artifact_sha256") or _d({"replicates": self._original_refs})
                _h(self.artifact_sha256, "ranking_artifact_digest_invalid")
                self.kind = "original"
                return
        self.inline = validate_frozen_ranking(value, projection=projection)
        self.arm_id = self.inline["arm_id"]
        self.artifact_sha256 = self.inline["artifact_sha256"]
        self.kind = "original" if self.arm_id == "original_public_product" else "current"

    def iter_rows(self) -> Iterator[dict[str, Any]]:
        if self.inline is not None:
            if self.kind == "current":
                yield from (dict(row) for row in self.inline["rankings"])
            else:
                yield from _artifact_rows(self.inline)
            return
        if self.kind == "original":
            from benchmarks import aerp7_original_product as original_product
            with ExitStack() as stack:
                stores = [stack.enter_context(original_product.OriginalReplicateStore.open(reference)) for reference in self._original_refs]
                iterators = [store.iter_rankings() for store in stores]
                while True:
                    rows: list[dict[str, Any]] = []
                    exhausted = []
                    for iterator in iterators:
                        try:
                            rows.append(dict(next(iterator)))
                        except StopIteration:
                            exhausted.append(True)
                    if exhausted:
                        if rows:
                            raise CustodyError("original_replicate_query_coverage_invalid")
                        break
                    first = rows[0]
                    if any(row.get("item_id") != first.get("item_id") for row in rows[1:]):
                        raise CustodyError("original_replicate_item_order_invalid")
                    yield {**first, "replicate_ranked_message_ids": [list(row["ranked_message_ids"]) for row in rows], "replicate_retrieved_conversation_ids": [list(row["retrieved_conversation_ids"]) for row in rows]}
            return
        assert self.reference is not None
        yield from _stream_json_array(Path(self.reference["artifact_path"]), "rankings.item")

    def preflight(self, access: _ProjectionAccess, db: _ScoringDB) -> None:
        """Validate streamed public rows before the custody loader is called."""
        seen = 0
        for source in self.iter_rows():
            item_id = source.get("item_id")
            if not isinstance(item_id, str):
                raise CustodyError("ranking_artifact_stream_row_invalid")
            item = access.item(item_id)
            if item is None:
                raise CustodyError("ranking_item_coverage_invalid")
            corpus = access.corpus(str(item["corpus_id"]))
            _validate_stream_row(source, arm_id=self.arm_id, item=item, corpus=corpus)
            if self.kind == "current":
                ids = source["ranked_message_ids"]
                try:
                    db.connection.execute(
                        "INSERT INTO preflight_rank VALUES (?, ?, ?, ?, ?, ?)",
                        (self.arm_id, item_id, source["query_sha256"], source["candidate_input_sha256"], len(ids), _d(ids)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise CustodyError("ranking_item_duplicate") from exc
            if self.kind == "original":
                replica_ids = source.get("replicate_ranked_message_ids")
                replica_conversations = source.get("replicate_retrieved_conversation_ids")
                if not isinstance(replica_ids, list) or len(replica_ids) != 5 or not isinstance(replica_conversations, list) or len(replica_conversations) != 5:
                    raise CustodyError("original_replicate_metric_invalid")
                for replica_ranked, replica_retrieved in zip(replica_ids, replica_conversations, strict=True):
                    _validate_stream_row({**source, "ranked_message_ids": replica_ranked, "retrieved_conversation_ids": replica_retrieved, "confidence": None, "confidence_receipt": None}, arm_id=self.arm_id, item=item, corpus=corpus)
            seen += 1
        if seen != access.query_count:
            raise CustodyError("ranking_item_coverage_invalid")
        if self.kind == "current":
            if self.inline is not None:
                trace_rows: Iterable[Mapping[str, Any]] = (dict(row) for row in self.inline["trace_receipt"])
            else:
                assert self.reference is not None
                trace_rows = _stream_json_array(Path(self.reference["artifact_path"]), "trace_receipt.item")
            trace_count = 0
            trace_digest = hashlib.sha256(); trace_digest.update(b"["); first = True
            for trace in trace_rows:
                if not first:
                    trace_digest.update(b",")
                trace_digest.update(_json(dict(trace)).encode("utf-8")); first = False
                required_trace = {"item_id", "query_sha256", "candidate_input_sha256", "ranked_count", "ranking_sha256", "ranker_trace_sha256", "ranking_trace"}
                if set(trace) != required_trace or not isinstance(trace.get("item_id"), str):
                    raise CustodyError("ranking_trace_schema_invalid")
                matched = db.connection.execute("SELECT query_sha256, candidate_input_sha256, ranked_count, ranking_sha256 FROM preflight_rank WHERE arm_id=? AND item_id=?", (self.arm_id, trace["item_id"])).fetchone()
                if matched is None or tuple(trace[key] for key in ("query_sha256", "candidate_input_sha256", "ranked_count", "ranking_sha256")) != tuple(matched):
                    raise CustodyError("ranking_trace_binding_invalid")
                if trace["ranker_trace_sha256"] != _d(trace["ranking_trace"]):
                    raise CustodyError("ranking_trace_binding_invalid")
                try:
                    db.connection.execute("INSERT INTO preflight_trace VALUES (?, ?)", (self.arm_id, trace["item_id"]))
                except sqlite3.IntegrityError as exc:
                    raise CustodyError("ranking_trace_duplicate") from exc
                trace_count += 1
            trace_digest.update(b"]")
            if trace_count != access.query_count or db.connection.execute("SELECT COUNT(*) FROM preflight_trace WHERE arm_id=?", (self.arm_id,)).fetchone()[0] != access.query_count:
                raise CustodyError("ranking_trace_coverage_invalid")
            if self.reference is not None and trace_digest.hexdigest() != self.reference["trace_sha256"]:
                raise CustodyError("ranking_trace_digest_invalid")

    def original_replicates(self) -> list[Mapping[str, Any]]:
        if self.kind != "original":
            return []
        if self.inline is not None:
            return list(self.inline["replicates"])
        return [{"build_id": reference["build_id"], "index_sha256": reference["index_sha256"]} for reference in self._original_refs]

    @staticmethod
    def _check_original_reference(value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from benchmarks import aerp7_original_product as original_product
            return original_product.validate_original_replicate_reference(value)
        except Exception as exc:
            if isinstance(exc, CustodyError):
                raise
            raise CustodyError("original_replicate_reference_invalid") from exc


def validate_ranking_artifact(value: Any, *, projection: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    artifact = validate_frozen_ranking(value, projection=projection)
    arm = next((item for item in manifest["arms"] if item["arm_id"] == artifact["arm_id"]), None)
    if arm is None or arm["ranking_artifact_sha256"] != artifact["artifact_sha256"]: raise CustodyError("ranking_arm_manifest_binding_invalid")
    expected_confidence = CONFIDENCE_CONTRACT if artifact["arm_id"] in CURRENT_ARMS else None
    if arm["confidence_contract"] != expected_confidence: raise CustodyError("ranking_confidence_manifest_invalid")
    return artifact


def _map(projection: Mapping[str, Any], custody: Any, secret: bytes, *, formal_live: bool):
    if not isinstance(secret, bytes) or len(secret) < 32: raise CustodyError("scoring_secret_too_short")
    row = _o(custody, "scoring_custody_invalid")
    if set(row) != {"schema", "projection_sha256", "items"} or row.get("schema") != CUSTODY_SCHEMA or row.get("projection_sha256") != _d(projection):
        raise CustodyError("scoring_custody_schema_invalid")
    corpora = {item["corpus_id"]: item for item in projection["corpora"]}; items = {item["item_id"]: item for item in projection["items"]}
    mappings: dict[str, list[dict[str, Any]]] = {}; public_ledger: list[dict[str, Any]] = []; groups: dict[str, str] = {}; conversations: dict[str, list[str]] = {}; seen = set()
    for item in _l(row["items"], "scoring_custody_items_invalid"):
        item = _o(item, "scoring_custody_item_invalid")
        if set(item) != {"item_id", "directory_group", "evidence_conversation_ids", "evidence_spans"} or item.get("item_id") not in items or item["item_id"] in seen or item.get("directory_group") not in UPSTREAM_GROUPS:
            raise CustodyError("scoring_custody_item_invalid")
        seen.add(item["item_id"]); group = item["directory_group"]; endpoint = UPSTREAM_GROUPS[group]
        evidence_conversations = _l(item["evidence_conversation_ids"], "scoring_custody_conversations_invalid")
        corpus = corpora[items[item["item_id"]]["corpus_id"]]; allowed_conversations = {candidate["opaque_conversation_id"] for candidate in corpus["candidates"]}
        if len(evidence_conversations) != len(set(evidence_conversations)) or set(evidence_conversations) - allowed_conversations:
            raise CustodyError("scoring_custody_conversations_invalid")
        spans = _l(item["evidence_spans"], "scoring_custody_evidence_invalid")
        if endpoint == "positive" and not spans: raise CustodyError("positive_evidence_span_missing")
        if endpoint == "abstention" and (spans or evidence_conversations): raise CustodyError("abstention_evidence_must_be_empty")
        groups[item["item_id"]] = group; conversations[item["item_id"]] = list(evidence_conversations); resolved = []
        for ordinal, span in enumerate(spans):
            span = _o(span, "scoring_custody_evidence_invalid")
            if set(span) != {"speaker", "text"} or not isinstance(span["speaker"], str) or not isinstance(span["text"], str): raise CustodyError("scoring_custody_evidence_invalid")
            hits = [candidate["message_id"] for candidate in corpus["candidates"] if candidate["opaque_conversation_id"] in evidence_conversations and (normalize_v1(candidate["speaker"]), normalize_v1(candidate["text"])) == (normalize_v1(span["speaker"]), normalize_v1(span["text"]))]
            status = "mapped" if len(hits) == 1 else "unmatched" if not hits else "ambiguous"
            private = {"status": status}
            if status == "mapped": private["message_id"] = hits[0]
            resolved.append(private)
            # Never include mapped message id in a public report.  Token is domain
            # separated and only witnesses cardinality/status for this item.
            public_ledger.append({"item_id": item["item_id"], "evidence_token": hmac.new(secret, f"aerp7-public-ledger/v1/{item['item_id']}/{ordinal}".encode("utf-8"), hashlib.sha256).hexdigest(), "status": status})
        mappings[item["item_id"]] = resolved
    if seen != set(items): raise CustodyError("scoring_custody_item_coverage_invalid")
    if formal_live and any(entry["status"] != "mapped" for entry in public_ledger):
        # Exact-evidence Recall has no defensible denominator when a frozen
        # span maps to zero or multiple candidate messages.  Abort the formal
        # run; do not silently score unresolved labels as misses.  Synthetic
        # rehearsal retains the ledger as a diagnostic, not a formal claim.
        raise CustodyError("scoring_exact_evidence_mapping_incomplete")
    return mappings, public_ledger, groups, conversations


def question_metrics(ids: Sequence[str], spans: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    resolved = [span["message_id"] for span in spans if span["status"] == "mapped"]
    total = len(spans); retrieved = sum(message_id in ids[:10] for message_id in resolved); unique = set(resolved)
    if not total: raise CustodyError("positive_evidence_span_missing")
    first = next((rank for rank, message_id in enumerate(ids[:10], 1) if message_id in unique), None)
    # Recall uses AERP-1's evidence-span multiplicity denominator.  NDCG does
    # not: one retrieved document occupies one rank, so duplicate gold spans
    # are binary relevance at that rank and its ideal uses unique messages.
    dcg = sum(1 / math.log2(rank + 1) for rank, message_id in enumerate(ids[:10], 1) if message_id in unique)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(len(unique), 10) + 1))
    return {"evidence_item_count": total, "resolved_evidence_item_count": len(resolved), "unresolved_evidence_item_count": total - len(resolved), "retrieved_evidence_count_at_10": retrieved, "recall_at_10": retrieved / total, "hit_at_10": float(retrieved > 0), "all_at_10": float(retrieved == total), "ndcg_at_10": 0.0 if ideal == 0 else dcg / ideal, "mrr_at_10": 0.0 if first is None else 1.0 / first}


def _mean(values: Sequence[float]) -> float:
    if not values: raise CustodyError("metric_denominator_zero")
    return sum(values) / len(values)


def _metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows: raise CustodyError("metric_denominator_zero")
    metrics = [row["metrics"] for row in rows]
    result = {"item_count": len(rows), **{key: _mean([float(metric[key]) for metric in metrics]) for key in _METRIC_KEYS}}
    for key in ("evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count"):
        result[key] = sum(int(metric[key]) for metric in metrics)
    result["retrieved_evidence_count_at_10"] = sum(float(metric["retrieved_evidence_count_at_10"]) for metric in metrics)
    if result["evidence_item_count"] != result["resolved_evidence_item_count"] + result["unresolved_evidence_item_count"]: raise CustodyError("metric_denominator_identity_invalid")
    # Explicit secondary endpoint: unlike question-macro Recall@10, every
    # evidence span contributes one unit to this denominator.
    result["evidence_micro_recall_at_10"] = result["retrieved_evidence_count_at_10"] / result["evidence_item_count"]
    return result


def _persona_metric_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows: raise CustodyError("metric_denominator_zero")
    per_persona: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows: per_persona[row["persona_id"]].append(row["metrics"])
    return {"persona_count": len(per_persona), **{key: _mean([_mean([float(metric[key]) for metric in metrics]) for metrics in per_persona.values()]) for key in _METRIC_KEYS}}


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"question_macro": _metric_summary(rows), "persona_macro": _persona_metric_summary(rows)}


def _context_summary(context: int, rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows: raise CustodyError("metric_denominator_zero")
    def distribution(name: str) -> dict[str, float | int]:
        values = [int(row[name]) for row in rows]
        return {"min": min(values), "max": max(values), "mean": _mean(values)}
    return {"declared_context_size": context, "actual_conversation_count": distribution("actual_conversation_count"), "actual_message_count": distribution("actual_message_count"), **_summary(rows)}


def _auroc_ap(pairs: Sequence[tuple[float, int]]) -> tuple[float, float]:
    if not pairs or {label for _, label in pairs} != {0, 1}: raise CustodyError("confidence_stratum_class_missing")
    positives = sum(label for _, label in pairs); negatives = len(pairs) - positives; wins = 0.0
    for positive, label in pairs:
        if label != 1: continue
        for negative, other_label in pairs:
            if other_label == 0: wins += 1.0 if positive > negative else 0.5 if positive == negative else 0.0
    ordered = sorted(pairs, key=lambda pair: -pair[0]); hit = 0; index = 0; ap = 0.0
    while index < len(ordered):
        end = index
        while end < len(ordered) and ordered[end][0] == ordered[index][0]: end += 1
        group_positive = sum(label for _, label in ordered[index:end]); hit += group_positive
        ap += (group_positive / positives) * (hit / end)
        index = end
    return wins / (positives * negatives), ap


def _confidence(rows: Sequence[Mapping[str, Any]], *, available: bool) -> dict[str, Any]:
    if not available: return {"available": False, "reason": "arm_has_no_frozen_comparable_confidence_contract", "by_declared_context": {}}
    by_context: dict[int, list[tuple[float, int]]] = defaultdict(list)
    for row in rows:
        confidence = _number(row["confidence"], "ranking_confidence_invalid")
        if not 0 <= confidence <= 1: raise CustodyError("ranking_confidence_invalid")
        by_context[int(row["declared_context_size"])].append((confidence, 1 if row["endpoint"] == "positive" else 0))
    detail = {}
    for context, pairs in sorted(by_context.items()):
        auroc, ap = _auroc_ap(pairs); detail[str(context)] = {"item_count": len(pairs), "positive_count": sum(label for _, label in pairs), "negative_count": len(pairs) - sum(label for _, label in pairs), "auroc": auroc, "average_precision": ap}
    return {"available": True, "reason": None, "by_declared_context": detail}


class _MetricAccumulator:
    def __init__(self) -> None:
        self.count = 0
        self.metric_sums = {key: 0.0 for key in _METRIC_KEYS}
        self.evidence_item_count = 0
        self.resolved_evidence_item_count = 0
        self.unresolved_evidence_item_count = 0
        self.retrieved_evidence_count_at_10 = 0.0

    def add(self, metrics: Mapping[str, Any]) -> None:
        self.count += 1
        for key in _METRIC_KEYS:
            self.metric_sums[key] += float(metrics[key])
        self.evidence_item_count += int(metrics["evidence_item_count"])
        self.resolved_evidence_item_count += int(metrics["resolved_evidence_item_count"])
        self.unresolved_evidence_item_count += int(metrics["unresolved_evidence_item_count"])
        self.retrieved_evidence_count_at_10 += float(metrics["retrieved_evidence_count_at_10"])

    def metric_summary(self) -> dict[str, Any]:
        if not self.count:
            raise CustodyError("metric_denominator_zero")
        result = {
            "item_count": self.count,
            **{key: self.metric_sums[key] / self.count for key in _METRIC_KEYS},
            "evidence_item_count": self.evidence_item_count,
            "resolved_evidence_item_count": self.resolved_evidence_item_count,
            "unresolved_evidence_item_count": self.unresolved_evidence_item_count,
            "retrieved_evidence_count_at_10": self.retrieved_evidence_count_at_10,
        }
        if result["evidence_item_count"] != result["resolved_evidence_item_count"] + result["unresolved_evidence_item_count"]:
            raise CustodyError("metric_denominator_identity_invalid")
        result["evidence_micro_recall_at_10"] = result["retrieved_evidence_count_at_10"] / result["evidence_item_count"]
        return result


class _SplitAccumulator:
    def __init__(self) -> None:
        self.metrics = _MetricAccumulator()
        self.personas: dict[str, _MetricAccumulator] = {}

    def add(self, row: Mapping[str, Any]) -> None:
        metrics = row["metrics"]
        self.metrics.add(metrics)
        persona = row["persona_id"]
        self.personas.setdefault(persona, _MetricAccumulator()).add(metrics)

    def summary(self) -> dict[str, Any]:
        if not self.metrics.count or not self.personas:
            raise CustodyError("metric_denominator_zero")
        persona_macro = {
            key: _mean([acc.metric_sums[key] / acc.count for acc in self.personas.values()])
            for key in _METRIC_KEYS
        }
        return {"question_macro": self.metrics.metric_summary(), "persona_macro": {"persona_count": len(self.personas), **persona_macro}}


class _ReplicaAccumulator:
    def __init__(self) -> None:
        self.positive = _SplitAccumulator()
        self.exact: dict[str, _SplitAccumulator] = {
            group: _SplitAccumulator() for group, endpoint in UPSTREAM_GROUPS.items() if endpoint == "positive"
        }
        self.derived = _SplitAccumulator()
        self.hits = 0
        self.count = 0

    def add(self, row: Mapping[str, Any]) -> None:
        if row["endpoint"] != "positive":
            return
        self.positive.add(row); self.exact[row["directory_group"]].add(row); self.count += 1
        self.hits += int(bool(row["evidence_conversation_hit"]))
        if row["directory_group"] in {"changing_evidence", "implicit_connection_evidence"}:
            self.derived.add(row)


class _ArmAccumulator:
    """Compact result state; no query rows are retained in Python."""

    def __init__(self, arm_id: str, *, original_replicates: Sequence[Mapping[str, Any]] = ()) -> None:
        self.arm_id = arm_id
        self.original_replicates = list(original_replicates)
        self.positive = _SplitAccumulator()
        self.exact: dict[str, _SplitAccumulator] = {
            group: _SplitAccumulator() for group, endpoint in UPSTREAM_GROUPS.items() if endpoint == "positive"
        }
        self.contexts: dict[int, tuple[_SplitAccumulator, dict[str, int], dict[str, int]]] = {}
        self.derived = _SplitAccumulator()
        self.persona_recall: dict[str, dict[str, list[float | int]]] = {"overall_positive": {}, "derived_hard_changing_and_implicit": {}}
        self.persona_replica_recall: dict[str, dict[str, list[list[float | int]]]] = {"overall_positive": {}, "derived_hard_changing_and_implicit": {}}
        self.replica_accumulators = [_ReplicaAccumulator() for _ in range(5)] if self.arm_id == "original_public_product" else []
        self.positive_item_count = 0
        self.retrieved_relevant_conversation_count = 0

    def _add_persona_recall(self, subset: str, row: Mapping[str, Any]) -> None:
        entry = self.persona_recall[subset].setdefault(row["persona_id"], [0.0, 0])
        entry[0] += float(row["metrics"]["recall_at_10"]); entry[1] += 1
        if self.arm_id == "original_public_product":
            replicas = row.get("replicate_metrics")
            if not isinstance(replicas, list) or len(replicas) != 5:
                raise CustodyError("original_replicate_metric_invalid")
            replica_entries = self.persona_replica_recall[subset].setdefault(row["persona_id"], [[0.0, 0] for _ in range(5)])
            for number, metrics in enumerate(replicas):
                replica_entries[number][0] += float(metrics["recall_at_10"])
                replica_entries[number][1] += 1

    def add(self, row: Mapping[str, Any], *, confidence: float | None, confidence_db: _ScoringDB) -> None:
        if confidence is None:
            confidence_db.connection.execute(
                "INSERT INTO confidence VALUES (?, ?, ?, ?, ?)",
                (self.arm_id, row["persona_id"], int(row["declared_context_size"]), 0.0, 0),
            )
        else:
            confidence_db.connection.execute(
                "INSERT INTO confidence VALUES (?, ?, ?, ?, ?)",
                (self.arm_id, row["persona_id"], int(row["declared_context_size"]), float(confidence), 1 if row["endpoint"] == "positive" else 0),
            )
        if row["endpoint"] != "positive":
            return
        self.positive_item_count += 1
        self.retrieved_relevant_conversation_count += int(bool(row["evidence_conversation_hit"]))
        self.positive.add(row); self.exact[row["directory_group"]].add(row)
        self._add_persona_recall("overall_positive", row)
        context = int(row["declared_context_size"])
        context_acc, conv_dist, msg_dist = self.contexts.setdefault(context, (_SplitAccumulator(), {"min": 10**18, "max": 0, "sum": 0, "count": 0}, {"min": 10**18, "max": 0, "sum": 0, "count": 0}))
        context_acc.add(row)
        for key, name in (("actual_conversation_count", "conv"), ("actual_message_count", "msg")):
            target = conv_dist if name == "conv" else msg_dist
            value = int(row[key]); target["min"] = min(target["min"], value); target["max"] = max(target["max"], value); target["sum"] += value; target["count"] += 1
        if row["directory_group"] in {"changing_evidence", "implicit_connection_evidence"}:
            self.derived.add(row); self._add_persona_recall("derived_hard_changing_and_implicit", row)

    def _distribution(self, values: Mapping[str, int]) -> dict[str, float | int]:
        if values["count"] <= 0: raise CustodyError("metric_denominator_zero")
        return {"min": values["min"], "max": values["max"], "mean": values["sum"] / values["count"]}

    def result(self) -> dict[str, Any]:
        contexts = {}
        for context in sorted(self.contexts):
            acc, conv_dist, msg_dist = self.contexts[context]
            summary = acc.summary()
            contexts[str(context)] = {"declared_context_size": context, "actual_conversation_count": self._distribution(conv_dist), "actual_message_count": self._distribution(msg_dist), **summary}
        diagnostic = {
            "not_official_primary": True,
            "positive_item_count": self.positive_item_count,
            "retrieved_relevant_conversation_count": self.retrieved_relevant_conversation_count,
            "total_relevant_conversation_item_count": self.positive_item_count,
            "recall": self.retrieved_relevant_conversation_count / self.positive_item_count,
        }
        result = {
            "positive": {
                "overall": self.positive.summary(),
                "by_exact_group": {group: self.exact[group].summary() for group in self.exact},
                "by_declared_context": contexts,
                "derived_hard_changing_and_implicit": {"derived": True, **self.derived.summary()},
            },
            "confidence_separability": None,
            "official_style_evidence_conversation_diagnostic": diagnostic,
        }
        if self.arm_id == "original_public_product":
            replica_stats = []
            for number, receipt in enumerate(self.original_replicates):
                # Replica summaries are populated by the per-replica accumulators
                # in ``score_frozen`` and attached after this compact result.
                replica_stats.append({"replicate_index": number, "build_id": receipt["build_id"], "index_sha256": receipt["index_sha256"]})
            result["original_replicates"] = replica_stats
        return result


def _confidence_query(arm_id: str, persona: str | None, context: int, *, order: str = "") -> tuple[str, tuple[Any, ...]]:
    clauses = ["arm_id=?", "declared_context_size=?"]
    params: list[Any] = [arm_id, context]
    if persona is not None:
        clauses.insert(1, "persona_id=?"); params.insert(1, persona)
    return f"SELECT confidence, label FROM confidence WHERE {' AND '.join(clauses)}{order}", tuple(params)


def _stream_auroc_ap(connection: sqlite3.Connection, arm_id: str, persona: str | None, context: int) -> tuple[float, float]:
    query, params = _confidence_query(arm_id, persona, context, order=" ORDER BY confidence ASC")
    rows = connection.execute(query, params)
    total = positives = negatives = 0
    auroc_wins = 0.0
    # Ascending score groups permit exact tie handling with O(1) Python memory.
    pending_score: float | None = None; pending_positive = pending_negative = 0
    negatives_before = 0
    for confidence, label in rows:
        confidence = float(confidence); label = int(label)
        if pending_score is None or confidence == pending_score:
            pending_score = confidence; pending_positive += label; pending_negative += 1 - label
            continue
        auroc_wins += pending_positive * (negatives_before + 0.5 * pending_negative)
        negatives_before += pending_negative; positives += pending_positive; negatives += pending_negative; total += pending_positive + pending_negative
        pending_score = confidence; pending_positive = label; pending_negative = 1 - label
    if pending_score is not None:
        auroc_wins += pending_positive * (negatives_before + 0.5 * pending_negative)
        positives += pending_positive; negatives += pending_negative; total += pending_positive + pending_negative
    if not total or positives <= 0 or negatives <= 0:
        raise CustodyError("confidence_stratum_class_missing")
    # AP follows the legacy tie-group denominator exactly, with descending scores.
    query, params = _confidence_query(arm_id, persona, context, order=" ORDER BY confidence DESC")
    ap_rows = connection.execute(query, params)
    positive_total = sum(int(label) for _confidence, label in ap_rows)
    if positive_total <= 0 or positive_total >= total:
        raise CustodyError("confidence_stratum_class_missing")
    ap_rows = connection.execute(query, params)
    index = hit = 0; ap = 0.0; pending_score = None; group_positive = group_size = 0
    for confidence, label in ap_rows:
        confidence = float(confidence); label = int(label)
        if pending_score is None or confidence == pending_score:
            pending_score = confidence; group_positive += label; group_size += 1
            continue
        index += group_size; hit += group_positive; ap += (group_positive / positive_total) * (hit / index)
        pending_score = confidence; group_positive = label; group_size = 1
    if pending_score is not None:
        index += group_size; hit += group_positive; ap += (group_positive / positive_total) * (hit / index)
    return auroc_wins / (positives * negatives), ap


def _confidence_from_db(connection: sqlite3.Connection, arm_id: str, *, available: bool) -> dict[str, Any]:
    if not available:
        return {"available": False, "reason": "arm_has_no_frozen_comparable_confidence_contract", "by_declared_context": {}}
    strata = connection.execute(
        "SELECT DISTINCT persona_id, declared_context_size FROM confidence WHERE arm_id=? ORDER BY persona_id, declared_context_size",
        (arm_id,),
    )
    detail: dict[str, list[tuple[str, float, float, int, int]]] = defaultdict(list)
    for persona, context in strata:
        auroc, ap = _stream_auroc_ap(connection, arm_id, str(persona), int(context))
        item_count, positive_count = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(label), 0) FROM confidence WHERE arm_id=? AND persona_id=? AND declared_context_size=?",
            (arm_id, persona, context),
        ).fetchone()
        detail[str(context)].append((str(persona), auroc, ap, int(item_count), int(positive_count)))
    output = {}
    for context, entries in sorted(detail.items(), key=lambda row: int(row[0])):
        item_count = sum(entry[3] for entry in entries); positive_count = sum(entry[4] for entry in entries)
        # The legacy report is context-level, pooling all persona/context rows.
        auroc, ap = _stream_auroc_ap(connection, arm_id, None, int(context))
        output[context] = {"item_count": item_count, "positive_count": positive_count, "negative_count": item_count - positive_count, "auroc": auroc, "average_precision": ap}
    return {"available": True, "reason": None, "by_declared_context": output}


def _percentile(values: Sequence[float], point: float) -> float:
    values = sorted(values); position = (len(values) - 1) * point; lower = int(math.floor(position)); upper = int(math.ceil(position))
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def _persona_macro(rows: Sequence[Mapping[str, Any]], personas: Sequence[str]) -> float:
    values = []
    for persona in personas:
        subset = [float(row["metrics"]["recall_at_10"]) for row in rows if row["persona_id"] == persona and row["endpoint"] == "positive"]
        if not subset: raise CustodyError("bootstrap_persona_positive_missing")
        values.append(_mean(subset))
    return _mean(values)


def _bootstrap(arm_rows: Mapping[str, Sequence[Mapping[str, Any]]], manifest: Mapping[str, Any], *, subset: str) -> dict[str, Any]:
    positives = {persona for rows in arm_rows.values() for persona in [row["persona_id"] for row in rows if row["endpoint"] == "positive"]}
    if not positives: raise CustodyError("positive_endpoint_missing")
    personas = sorted(positives); reference = manifest["reference_arm"]; original = "original_public_product"; rng = random.Random(manifest["bootstrap"]["seed"])
    plans = []
    for _ in range(manifest["bootstrap"]["resamples"]):
        sample = [personas[rng.randrange(len(personas))] for _ in personas]
        # Hierarchical baseline: a clustered persona draw is shared across arms;
        # five original indexes are independently resampled with replacement.
        plans.append((sample, [rng.randrange(5) for _ in range(5)]))
    def original_macro(rows: Sequence[Mapping[str, Any]], sample: Sequence[str], replicate_draw: Sequence[int]) -> float:
        values=[]
        for persona in sample:
            values.append(_mean([_mean([float(row["replicate_metrics"][replica]["recall_at_10"]) for replica in replicate_draw]) for row in rows if row["persona_id"] == persona and row["endpoint"] == "positive"]))
        return _mean(values)
    output = {}
    for challenger in sorted(arm_rows):
        if challenger == original: continue
        comparison = {}
        for name, baseline in (("vs_original_public_product", original), ("vs_reference", reference)):
            if challenger == baseline: continue
            deltas = [(_persona_macro(arm_rows[challenger], sample) if challenger != original else original_macro(arm_rows[challenger], sample, replicas)) - (_persona_macro(arm_rows[baseline], sample) if baseline != original else original_macro(arm_rows[baseline], sample, replicas)) for sample, replicas in plans]
            estimate = _persona_macro(arm_rows[challenger], personas) - _persona_macro(arm_rows[baseline], personas)
            comparison[name] = {"estimate": estimate, "ci_lower": _percentile(deltas, manifest["bootstrap"]["percentile_lower"]), "ci_upper": _percentile(deltas, manifest["bootstrap"]["percentile_upper"]), "resample_count": len(deltas)}
        if comparison: output[challenger] = comparison
    plan_digest = _d([{"persona_clusters": sample, "original_replicate_indices": replicas} for sample, replicas in plans])
    return {"subset": subset, "metric": "positive_persona_macro_recall_at_10", "reference_arm": reference, "original_replicate_rule": manifest["bootstrap"]["original_replicate_rule"], "original_replicate_count": 5, "bootstrap_plan_sha256": plan_digest, "resamples": manifest["bootstrap"]["resamples"], "seed": manifest["bootstrap"]["seed"], "percentile_rule": "linear", "paired_deltas": output}


def _state_persona_value(state: _ArmAccumulator, persona: str, subset: str, replicas: Sequence[int]) -> float:
    if state.arm_id == "original_public_product":
        entries = state.persona_replica_recall[subset].get(persona)
        if entries is None:
            raise CustodyError("bootstrap_persona_positive_missing")
        values = [entries[index][0] / entries[index][1] for index in replicas if entries[index][1]]
        if len(values) != len(replicas):
            raise CustodyError("bootstrap_persona_positive_missing")
        return _mean(values)
    entry = state.persona_recall[subset].get(persona)
    if entry is None or not entry[1]:
        raise CustodyError("bootstrap_persona_positive_missing")
    return float(entry[0]) / int(entry[1])


def _bootstrap_states(states: Mapping[str, _ArmAccumulator], manifest: Mapping[str, Any], *, subset: str) -> dict[str, Any]:
    positives = sorted({persona for state in states.values() for persona, entry in state.persona_recall[subset].items() if entry[1]})
    if not positives:
        raise CustodyError("positive_endpoint_missing")
    reference = manifest["reference_arm"]; original = "original_public_product"; rng = random.Random(manifest["bootstrap"]["seed"])
    plans = [([positives[rng.randrange(len(positives))] for _ in positives], [rng.randrange(5) for _ in range(5)]) for _ in range(manifest["bootstrap"]["resamples"])]
    def macro(state: _ArmAccumulator, sample: Sequence[str], replicas: Sequence[int]) -> float:
        return _mean([_state_persona_value(state, persona, subset, replicas if state.arm_id == original else ()) for persona in sample])
    output: dict[str, Any] = {}
    for challenger in sorted(states):
        if challenger == original:
            continue
        comparison: dict[str, Any] = {}
        for name, baseline in (("vs_original_public_product", original), ("vs_reference", reference)):
            if challenger == baseline:
                continue
            deltas = [macro(states[challenger], sample, replicas) - macro(states[baseline], sample, replicas) for sample, replicas in plans]
            replicas = list(range(5))
            estimate = macro(states[challenger], positives, replicas) - macro(states[baseline], positives, replicas)
            comparison[name] = {"estimate": estimate, "ci_lower": _percentile(deltas, manifest["bootstrap"]["percentile_lower"]), "ci_upper": _percentile(deltas, manifest["bootstrap"]["percentile_upper"]), "resample_count": len(deltas)}
        if comparison:
            output[challenger] = comparison
    plan_digest = _d([{"persona_clusters": sample, "original_replicate_indices": replicas} for sample, replicas in plans])
    return {"subset": subset, "metric": "positive_persona_macro_recall_at_10", "reference_arm": reference, "original_replicate_rule": manifest["bootstrap"]["original_replicate_rule"], "original_replicate_count": 5, "bootstrap_plan_sha256": plan_digest, "resamples": manifest["bootstrap"]["resamples"], "seed": manifest["bootstrap"]["seed"], "percentile_rule": "linear", "paired_deltas": output}


def _confidence_nonregression_states(states: Mapping[str, _ArmAccumulator], manifest: Mapping[str, Any], db: _ScoringDB) -> dict[str, Any]:
    raw, p5 = states["strong_raw"], states["static_p5"]
    def strata(arm_id: str) -> dict[tuple[str, int], tuple[float, float]]:
        output: dict[tuple[str, int], tuple[float, float]] = {}
        for persona, context in db.connection.execute("SELECT DISTINCT persona_id, declared_context_size FROM confidence WHERE arm_id=? ORDER BY persona_id, declared_context_size", (arm_id,)):
            output[(str(persona), int(context))] = _stream_auroc_ap(db.connection, arm_id, str(persona), int(context))
        return output
    raw_values, p5_values = strata("strong_raw"), strata("static_p5")
    if set(raw_values) != set(p5_values) or not raw_values:
        raise CustodyError("confidence_pairing_invalid")
    personas = sorted({persona for persona, _context in raw_values}); rng = random.Random(manifest["bootstrap"]["seed"])
    def macro(values: Mapping[tuple[str, int], tuple[float, float]], sampled: Sequence[str], index: int) -> float:
        return _mean([_mean([pair[index] for (persona, _), pair in values.items() if persona == sample]) for sample in sampled])
    result = {}
    for index, name in enumerate(("auroc", "average_precision")):
        draws = [[personas[rng.randrange(len(personas))] for _ in personas] for _ in range(manifest["bootstrap"]["resamples"])]
        deltas = [macro(p5_values, draw, index) - macro(raw_values, draw, index) for draw in draws]
        result[name] = {"pre_registered_scalar": name, "estimate": macro(p5_values, personas, index) - macro(raw_values, personas, index), "ci_lower": _percentile(deltas, manifest["bootstrap"]["percentile_lower"]), "ci_upper": _percentile(deltas, manifest["bootstrap"]["percentile_upper"]), "resample_count": len(deltas)}
    return {"comparison": "static_p5_vs_strong_raw", "unit": "paired_persona_by_declared_context", "metrics": result}


def _confidence_nonregression(arm_rows: Mapping[str, Sequence[Mapping[str, Any]]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    raw, p5 = arm_rows["strong_raw"], arm_rows["static_p5"]
    def strata(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int], tuple[float, float]]:
        grouped: dict[tuple[str, int], list[tuple[float, int]]] = defaultdict(list)
        for row in rows: grouped[(row["persona_id"], int(row["declared_context_size"]))].append((_number(row["confidence"], "ranking_confidence_invalid"), 1 if row["endpoint"] == "positive" else 0))
        return {key: _auroc_ap(pairs) for key, pairs in grouped.items()}
    raw_values, p5_values = strata(raw), strata(p5)
    if set(raw_values) != set(p5_values) or not raw_values: raise CustodyError("confidence_pairing_invalid")
    personas = sorted({persona for persona, _context in raw_values}); rng = random.Random(manifest["bootstrap"]["seed"])
    def macro(values: Mapping[tuple[str, int], tuple[float, float]], sampled: Sequence[str], index: int) -> float:
        return _mean([_mean([pair[index] for (persona, _), pair in values.items() if persona == sample]) for sample in sampled])
    result = {}
    for index, name in enumerate(("auroc", "average_precision")):
        draws = [[personas[rng.randrange(len(personas))] for _ in personas] for _ in range(manifest["bootstrap"]["resamples"])]
        deltas = [macro(p5_values, draw, index) - macro(raw_values, draw, index) for draw in draws]
        result[name] = {"pre_registered_scalar": name, "estimate": macro(p5_values, personas, index) - macro(raw_values, personas, index), "ci_lower": _percentile(deltas, manifest["bootstrap"]["percentile_lower"]), "ci_upper": _percentile(deltas, manifest["bootstrap"]["percentile_upper"]), "resample_count": len(deltas)}
    return {"comparison": "static_p5_vs_strong_raw", "unit": "paired_persona_by_declared_context", "metrics": result}


def _artifact_rows(artifact: Mapping[str, Any]) -> list[dict[str, Any]]:
    if artifact["arm_id"] != "original_public_product": return [dict(row) for row in artifact["rankings"]]
    by_item: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for replicate in artifact["replicates"]:
        for row in replicate["rankings"]: by_item[row["item_id"]].append(row)
    rows = []
    for item_id, replicas in by_item.items():
        # The primary value is defined by the manifest's fixed per-query mean.
        rows.append({**dict(replicas[0]), "replicate_ranked_message_ids": [list(row["ranked_message_ids"]) for row in replicas], "replicate_retrieved_conversation_ids": [list(row["retrieved_conversation_ids"]) for row in replicas]})
    return rows


def _validate_stream_row(row: Mapping[str, Any], *, arm_id: str, item: Mapping[str, Any], corpus: Mapping[str, Any]) -> tuple[list[str], list[str], float | None]:
    required = {"item_id", "query_sha256", "candidate_input_sha256", "ranked_message_ids", "retrieved_conversation_ids", "confidence", "confidence_receipt"}
    if not required <= set(row) or (arm_id != "original_public_product" and set(row) != required) or row.get("item_id") != item.get("item_id"):
        raise CustodyError("ranking_artifact_stream_row_invalid")
    if row.get("query_sha256") != hashlib.sha256(str(item["query_text"]).encode("utf-8")).hexdigest():
        raise CustodyError("ranking_query_digest_invalid")
    expected_serializer = ORIGINAL_MEMPALACE_SERIALIZER if arm_id == "original_public_product" else CURRENT_SERIALIZER
    expected_candidate = _d({"serializer": expected_serializer, "corpus_id": corpus["corpus_id"], "candidates": corpus["candidates"]})
    if row.get("candidate_input_sha256") != expected_candidate:
        raise CustodyError("ranking_candidate_input_digest_invalid")
    ids = row.get("ranked_message_ids"); conversations = row.get("retrieved_conversation_ids")
    if not isinstance(ids, list) or not isinstance(conversations, list) or len(ids) != len(set(ids)) or len(ids) > 10 or len(conversations) != len(set(conversations)):
        raise CustodyError("ranking_top10_invalid")
    allowed = {candidate["message_id"]: candidate["opaque_conversation_id"] for candidate in corpus["candidates"]}
    if any(not isinstance(value, str) or value not in allowed for value in ids):
        raise CustodyError("ranking_top10_invalid")
    expected_conversations = list(dict.fromkeys(allowed[value] for value in ids))
    if conversations != expected_conversations:
        raise CustodyError("ranking_conversations_invalid")
    confidence = row.get("confidence")
    if arm_id == "original_public_product":
        if confidence is not None or row.get("confidence_receipt") is not None:
            raise CustodyError("ranking_confidence_contract_invalid")
        return ids, conversations, None
    if confidence is None or not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise CustodyError("ranking_confidence_invalid")
    receipt = row.get("confidence_receipt")
    if not isinstance(receipt, Mapping) or set(receipt) != {"contract", "top_two_scores"} or receipt.get("contract") != CONFIDENCE_CONTRACT:
        raise CustodyError("ranking_confidence_receipt_invalid")
    return ids, conversations, float(confidence)


def score_frozen(
    *, projection: Any, endpoint_manifest: Any, ranking_artifacts: Sequence[Any],
    evidence_token_secret: bytes, custody_loader: Callable[[], Any] | None = None,
    formal_live: bool | None = None, custody_store: Any = None,
    mapping_ledger_path: Path | str | None = None,
    capacity_observer: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    formal_hint = formal_live is True or (
        formal_live is None and isinstance(endpoint_manifest, Mapping)
        and endpoint_manifest.get("synthetic_test_mode") is False
    )
    if formal_hint and not isinstance(projection, CandidateProjectionStore):
        raise CustodyError("scoring_formal_projection_store_required")
    if custody_loader is not None and custody_store is not None:
        raise CustodyError("scoring_custody_source_ambiguous")
    access = _ProjectionAccess(projection)
    manifest = validate_endpoint_manifest(endpoint_manifest, projection_sha256=access.digest)
    if formal_live is None:
        formal_live = manifest["synthetic_test_mode"] is False
    if not isinstance(formal_live, bool):
        raise CustodyError("scoring_execution_mode_invalid")
    if formal_live and access.store is None:
        raise CustodyError("scoring_formal_projection_store_required")
    if formal_live:
        for artifact in ranking_artifacts:
            if not isinstance(artifact, Mapping) or artifact.get("schema") not in {
                RANKING_ARTIFACT_REFERENCE_SCHEMA,
                "aerp7-original-product-artifact-reference-v1",
            }:
                raise CustodyError("scoring_formal_artifact_reference_required")
    # Validate every public artifact/reference before custody is opened.
    readers = [_ArtifactReader(artifact, projection_digest=access.digest, projection=access.inline) for artifact in ranking_artifacts]
    if len(readers) != len(manifest["arms"]) or {reader.arm_id for reader in readers} != {arm["arm_id"] for arm in manifest["arms"]}:
        raise CustodyError("ranking_arm_set_invalid")
    for reader in readers:
        arm = next(item for item in manifest["arms"] if item["arm_id"] == reader.arm_id)
        if arm["ranking_artifact_sha256"] != reader.artifact_sha256:
            raise CustodyError("ranking_arm_manifest_binding_invalid")
        if access.reference is not None:
            if reader.reference is not None and reader.reference.get("generation_id") != access.reference["generation_id"]:
                raise CustodyError("ranking_artifact_generation_invalid")
            if reader._original_refs and any(ref.get("candidate_reference") != access.reference for ref in reader._original_refs):
                raise CustodyError("original_replicate_candidate_binding_invalid")
        elif reader._original_refs and any(
            ref.get("candidate_reference", {}).get("projection_canonical_sha256") != access.digest
            for ref in reader._original_refs
        ):
            raise CustodyError("original_replicate_projection_binding_invalid")
        expected = CONFIDENCE_CONTRACT if reader.arm_id in CURRENT_ARMS else None
        if arm["confidence_contract"] != expected:
            raise CustodyError("ranking_confidence_manifest_invalid")
    store_active = False
    if access.store is not None:
        access.store.begin_run(); store_active = True
    ledger_sidecar_created = False
    ledger_sidecar_path: Path | None = None
    ledger_sidecar_ready_path: Path | None = None
    try:
        with _ScoringDB(observe_capacity=capacity_observer is not None) as db:
            _prepare_projection_items(access, db)
            for reader in readers:
                reader.preflight(access, db)
            if formal_live and custody_store is None:
                # Formal scoring receives the already-authorized capability
                # explicitly.  A loader callback is retained only for the
                # small legacy fixtures; allowing it in the formal lane would
                # make it too easy to reintroduce the full JSON custody path.
                raise CustodyError("scoring_formal_custody_store_required")
            if formal_live and custody_loader is not None:
                raise CustodyError("scoring_formal_custody_store_required")
            if custody_store is not None:
                custody_source = custody_store
            elif custody_loader is not None:
                custody_source = custody_loader()
            else:
                raise CustodyError("scoring_custody_source_missing")
            if formal_live and not _is_custody_store(custody_source):
                raise CustodyError("scoring_formal_custody_store_required")
            custody_index = _prepare_custody(access, custody_source, evidence_token_secret, formal_live=formal_live, db=db)
            states: dict[str, _ArmAccumulator] = {}
            for reader in readers:
                state = _ArmAccumulator(reader.arm_id, original_replicates=reader.original_replicates())
                seen = 0
                for source in reader.iter_rows():
                    item_id = source.get("item_id")
                    if not isinstance(item_id, str):
                        raise CustodyError("ranking_artifact_stream_row_invalid")
                    try:
                        db.connection.execute("INSERT INTO artifact_seen VALUES (?, ?)", (reader.arm_id, item_id))
                    except sqlite3.IntegrityError as exc:
                        raise CustodyError("ranking_item_duplicate") from exc
                    item_row = db.connection.execute("SELECT persona_id, corpus_id, declared_context_size, actual_conversation_count, actual_message_count, query_text FROM projection_items WHERE item_id=?", (item_id,)).fetchone()
                    if item_row is None:
                        raise CustodyError("ranking_item_coverage_invalid")
                    item = {"item_id": item_id, "persona_id": str(item_row[0]), "corpus_id": str(item_row[1]), "query_text": str(item_row[5])}
                    corpus = access.corpus(str(item_row[1]))
                    group, evidence_conversations, mappings = custody_index.get(item_id)
                    endpoint = UPSTREAM_GROUPS[group]
                    ids, retrieved_conversations, confidence = _validate_stream_row(source, arm_id=reader.arm_id, item=item, corpus=corpus)
                    row = {"item_id": item_id, "persona_id": str(item_row[0]), "declared_context_size": int(item_row[2]), "actual_conversation_count": int(item_row[3]), "actual_message_count": int(item_row[4]), "directory_group": group, "endpoint": endpoint, "confidence": confidence, "evidence_conversation_hit": bool(set(retrieved_conversations) & set(evidence_conversations))}
                    if endpoint == "positive":
                        if reader.arm_id == "original_public_product":
                            replica_ids = source.get("replicate_ranked_message_ids")
                            replica_conversations = source.get("replicate_retrieved_conversation_ids")
                            if not isinstance(replica_ids, list) or len(replica_ids) != 5 or not isinstance(replica_conversations, list) or len(replica_conversations) != 5:
                                raise CustodyError("original_replicate_metric_invalid")
                            for replica_number, (replica_ranked, replica_retrieved) in enumerate(zip(replica_ids, replica_conversations, strict=True)):
                                replica_source = {**source, "ranked_message_ids": replica_ranked, "retrieved_conversation_ids": replica_retrieved, "confidence": None, "confidence_receipt": None}
                                _validate_stream_row(replica_source, arm_id=reader.arm_id, item=item, corpus=corpus)
                            metric_rows = [question_metrics(replica, mappings) for replica in replica_ids]
                            row["metrics"] = {key: _mean([float(metrics[key]) for metrics in metric_rows]) for key in _METRIC_KEYS}
                            for key in ("evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count"):
                                row["metrics"][key] = metric_rows[0][key]
                            row["metrics"]["retrieved_evidence_count_at_10"] = _mean([float(metrics["retrieved_evidence_count_at_10"]) for metrics in metric_rows])
                            row["replicate_metrics"] = metric_rows
                            row["replicate_evidence_conversation_hits"] = [bool(set(conversations) & set(evidence_conversations)) for conversations in replica_conversations]
                            for number, metrics in enumerate(metric_rows):
                                replica_row = {**row, "metrics": metrics, "evidence_conversation_hit": row["replicate_evidence_conversation_hits"][number]}
                                state.replica_accumulators[number].add(replica_row)
                        else:
                            row["metrics"] = question_metrics(ids, mappings)
                    state.add(row, confidence=confidence, confidence_db=db)
                    seen += 1
                count = db.connection.execute("SELECT COUNT(*) FROM artifact_seen WHERE arm_id=?", (reader.arm_id,)).fetchone()[0]
                if count != access.query_count or seen != access.query_count:
                    raise CustodyError("ranking_item_coverage_invalid")
                states[reader.arm_id] = state
            db.connection.commit()
            arms: dict[str, Any] = {}
            for reader in readers:
                state = states[reader.arm_id]
                arm_contract = next(item["confidence_contract"] for item in manifest["arms"] if item["arm_id"] == reader.arm_id)
                arm_result = state.result()
                arm_result["confidence_separability"] = _confidence_from_db(db.connection, reader.arm_id, available=arm_contract == CONFIDENCE_CONTRACT)
                if reader.arm_id == "original_public_product":
                    replica_stats = []
                    for number, receipt in enumerate(state.original_replicates):
                        replica = state.replica_accumulators[number]
                        replica_stats.append({"replicate_index": number, "build_id": receipt["build_id"], "index_sha256": receipt["index_sha256"], "overall_positive": replica.positive.summary(), "by_exact_group": {group: replica.exact[group].summary() for group in replica.exact}, "derived_hard_changing_and_implicit": replica.derived.summary(), "official_style_evidence_conversation_diagnostic": {"positive_item_count": replica.count, "retrieved_relevant_conversation_count": replica.hits, "total_relevant_conversation_item_count": replica.count, "recall": replica.hits / replica.count}})
                    arm_result["original_replicates"] = replica_stats
                    total = sum(replica.count for replica in state.replica_accumulators); hits = sum(replica.hits for replica in state.replica_accumulators)
                    arm_result["official_style_evidence_conversation_diagnostic"] = {"not_official_primary": True, "positive_item_count": total, "retrieved_relevant_conversation_count": hits, "total_relevant_conversation_item_count": total, "recall": hits / total}
                arms[reader.arm_id] = arm_result
            # A CustodyStore opened for a legacy inline/rehearsal projection
            # owns an ephemeral directory which the caller is allowed to
            # close immediately after scoring.  Keeping a report reference to
            # that directory would make the just-produced report unverifiable
            # at the next gate.  Persist a sidecar only for the formal/store
            # projection lane; the small inline compatibility lane retains its
            # legacy in-report ledger.
            external_ledger = formal_live or access.store is not None
            if external_ledger:
                if mapping_ledger_path is not None:
                    requested_ledger_path = Path(mapping_ledger_path)
                    if not requested_ledger_path.is_absolute():
                        raise CustodyError("scoring_mapping_ledger_output_invalid")
                    ledger_sidecar_path = requested_ledger_path.resolve()
                elif access.store is not None:
                    ledger_sidecar_path = access.store.database.with_name(access.store.database.stem + ".mapping-ledger.json")
                else:
                    ledger_sidecar_path = Path(custody_source.directory).resolve() / "mapping-ledger.json"
                existed = ledger_sidecar_path.exists()
                ledger_value = _write_mapping_ledger_reference(
                    custody_index, path=ledger_sidecar_path, projection_digest=access.digest,
                )
                ledger_sidecar_created = not existed
                ledger_sidecar_ready_path = Path(ledger_value["ready_path"])
            else:
                ledger_value = custody_index.legacy_ledger()
            report = {"schema": SCHEMA, "projection_sha256": access.digest, "endpoint_manifest_sha256": manifest["manifest_sha256"], "endpoint_manifest": manifest, "protocol": {"synthetic_test_mode": manifest["synthetic_test_mode"], "arm_registry": [arm["arm_id"] for arm in manifest["arms"]], "reference_arm": manifest["reference_arm"], "bootstrap": manifest["bootstrap"]}, "ranking_artifact_sha256": {reader.arm_id: reader.artifact_sha256 for reader in readers}, "mapping_ledger": ledger_value, "arms": arms, "paired_bootstrap": {"overall_positive": _bootstrap_states(states, manifest, subset="overall_positive"), "derived_hard_changing_and_implicit": _bootstrap_states(states, manifest, subset="derived_hard_changing_and_implicit"), "static_p5_vs_strong_raw_abstention_confidence": _confidence_nonregression_states(states, manifest, db)}}
            report["report_sha256"] = report_digest(report)
            checked_report = validate_report(report)
            if capacity_observer is not None:
                footprint = db.peak_footprint_bytes
                if footprint <= 0:
                    raise CustodyError("scoring_capacity_db_footprint_invalid")
                report_bytes = json.dumps(checked_report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
                ledger_bytes = 0
                if ledger_sidecar_path is not None:
                    if not ledger_sidecar_path.is_file() or ledger_sidecar_path.is_symlink():
                        raise CustodyError("scoring_capacity_mapping_ledger_invalid")
                    ledger_bytes = ledger_sidecar_path.stat().st_size
                if ledger_bytes <= 0:
                    raise CustodyError("scoring_capacity_mapping_ledger_invalid")
                capacity_observer({
                    "schema": "aerp7-convomem-capacity-observer-event-v1", "kind": "scoring",
                    "scoring_db_peak_bytes": footprint, "report_bytes": len(report_bytes),
                    "mapping_ledger_bytes": ledger_bytes,
                })
            return checked_report
    except BaseException:
        if ledger_sidecar_created and ledger_sidecar_path is not None:
            try:
                ledger_sidecar_path.unlink()
            except FileNotFoundError:
                pass
        if ledger_sidecar_created and ledger_sidecar_ready_path is not None:
            try:
                ledger_sidecar_ready_path.unlink()
            except FileNotFoundError:
                pass
        raise
    finally:
        if store_active:
            access.store.end_run()


def validate_report(value: Any) -> dict[str, Any]:
    row = _o(value, "scoring_report_invalid")
    keys = {"schema", "projection_sha256", "endpoint_manifest_sha256", "endpoint_manifest", "protocol", "ranking_artifact_sha256", "mapping_ledger", "arms", "paired_bootstrap", "report_sha256"}
    if set(row) != keys or row.get("schema") != SCHEMA: raise CustodyError("scoring_report_schema_invalid")
    for key in ("projection_sha256", "endpoint_manifest_sha256", "report_sha256"): _h(row.get(key), "scoring_report_digest_invalid")
    if row["report_sha256"] != report_digest(row): raise CustodyError("scoring_report_digest_mismatch")
    def scan(value: Any) -> None:
        if isinstance(value, Mapping):
            if _FORBIDDEN_REPORT_KEYS & set(value): raise CustodyError("scoring_report_leakage")
            for child in value.values(): scan(child)
        elif isinstance(value, list):
            for child in value: scan(child)
        elif isinstance(value, float) and not math.isfinite(value): raise CustodyError("scoring_report_nonfinite")
    scan(row)
    manifest = validate_endpoint_manifest(row["endpoint_manifest"], projection_sha256=row["projection_sha256"])
    if manifest["manifest_sha256"] != row["endpoint_manifest_sha256"]: raise CustodyError("scoring_report_protocol_invalid")
    manifest_artifacts = {arm["arm_id"]: arm["ranking_artifact_sha256"] for arm in manifest["arms"]}
    if row.get("ranking_artifact_sha256") != manifest_artifacts: raise CustodyError("scoring_report_artifact_manifest_binding_invalid")
    protocol = _o(row["protocol"], "scoring_report_protocol_invalid")
    if set(protocol) != {"synthetic_test_mode", "arm_registry", "reference_arm", "bootstrap"} or not isinstance(protocol["synthetic_test_mode"], bool) or not isinstance(protocol["arm_registry"], list) or len(protocol["arm_registry"]) != len(set(protocol["arm_registry"])) or protocol["reference_arm"] not in protocol["arm_registry"]:
        raise CustodyError("scoring_report_protocol_invalid")
    if protocol["synthetic_test_mode"] is False and (tuple(protocol["arm_registry"]) != FORMAL_ARMS or protocol["reference_arm"] != "six_view_secondary" or set(protocol["bootstrap"]) != {"seed", *FORMAL_BOOTSTRAP} or {key: protocol["bootstrap"][key] for key in FORMAL_BOOTSTRAP} != FORMAL_BOOTSTRAP): raise CustodyError("scoring_report_protocol_invalid")
    if protocol != {"synthetic_test_mode": manifest["synthetic_test_mode"], "arm_registry": [arm["arm_id"] for arm in manifest["arms"]], "reference_arm": manifest["reference_arm"], "bootstrap": manifest["bootstrap"]}: raise CustodyError("scoring_report_protocol_invalid")
    if not isinstance(row["ranking_artifact_sha256"], Mapping) or not row["ranking_artifact_sha256"] or not isinstance(row["arms"], Mapping) or set(row["ranking_artifact_sha256"]) != set(row["arms"]) or set(protocol["arm_registry"]) != set(row["arms"]): raise CustodyError("scoring_report_arm_schema_invalid")
    for digest in row["ranking_artifact_sha256"].values(): _h(digest, "scoring_report_digest_invalid")
    ledger = row["mapping_ledger"]
    if isinstance(ledger, Mapping) and ledger.get("schema") == MAPPING_LEDGER_REFERENCE_SCHEMA:
        _validate_mapping_ledger_reference(ledger, projection_digest=row["projection_sha256"])
    elif isinstance(ledger, list):
        _validate_ledger_entries(ledger)
    else:
        raise CustodyError("scoring_report_ledger_invalid")
    metric_keys = {"item_count", "evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count", "retrieved_evidence_count_at_10", "evidence_micro_recall_at_10", *_METRIC_KEYS}
    def metric_summary(summary: Any) -> None:
        summary = _o(summary, "scoring_report_metric_schema_invalid")
        if set(summary) != metric_keys: raise CustodyError("scoring_report_metric_schema_invalid")
        for key in ("item_count", "evidence_item_count", "resolved_evidence_item_count", "unresolved_evidence_item_count"):
            _integer(summary.get(key), "scoring_report_metric_schema_invalid")
            if summary[key] < 0: raise CustodyError("scoring_report_metric_schema_invalid")
        if _number(summary.get("retrieved_evidence_count_at_10"), "scoring_report_metric_schema_invalid") < 0:
            raise CustodyError("scoring_report_metric_schema_invalid")
        if summary["evidence_item_count"] != summary["resolved_evidence_item_count"] + summary["unresolved_evidence_item_count"] or summary["retrieved_evidence_count_at_10"] > summary["evidence_item_count"]:
            raise CustodyError("scoring_report_metric_denominator_invalid")
        for key in _METRIC_KEYS:
            number = _number(summary.get(key), "scoring_report_metric_schema_invalid")
            if not 0 <= number <= 1: raise CustodyError("scoring_report_metric_range_invalid")
        if summary["evidence_item_count"] <= 0 or not 0 <= _number(summary.get("evidence_micro_recall_at_10"), "scoring_report_metric_schema_invalid") <= 1:
            raise CustodyError("scoring_report_metric_range_invalid")
        if not math.isclose(float(summary["evidence_micro_recall_at_10"]), float(summary["retrieved_evidence_count_at_10"]) / int(summary["evidence_item_count"]), rel_tol=0.0, abs_tol=1e-12):
            raise CustodyError("scoring_report_metric_denominator_invalid")
    def persona_summary(summary: Any) -> None:
        summary = _o(summary, "scoring_report_metric_schema_invalid")
        if set(summary) != {"persona_count", *_METRIC_KEYS}: raise CustodyError("scoring_report_metric_schema_invalid")
        _integer(summary.get("persona_count"), "scoring_report_metric_schema_invalid", positive=True)
        for key in _METRIC_KEYS:
            if not 0 <= _number(summary.get(key), "scoring_report_metric_schema_invalid") <= 1: raise CustodyError("scoring_report_metric_range_invalid")
    def split_summary(summary: Any) -> None:
        summary = _o(summary, "scoring_report_metric_schema_invalid")
        if set(summary) != {"question_macro", "persona_macro"}: raise CustodyError("scoring_report_metric_schema_invalid")
        metric_summary(summary["question_macro"]); persona_summary(summary["persona_macro"])
    def diagnostic(value: Any, *, public: bool) -> None:
        value = _o(value, "scoring_report_diagnostic_schema_invalid")
        expected = {"positive_item_count", "retrieved_relevant_conversation_count", "total_relevant_conversation_item_count", "recall"} | ({"not_official_primary"} if public else set())
        if set(value) != expected or public and value.get("not_official_primary") is not True: raise CustodyError("scoring_report_diagnostic_schema_invalid")
        for key in ("positive_item_count", "retrieved_relevant_conversation_count", "total_relevant_conversation_item_count"): _integer(value.get(key), "scoring_report_diagnostic_schema_invalid")
        if value["positive_item_count"] != value["total_relevant_conversation_item_count"] or value["retrieved_relevant_conversation_count"] > value["total_relevant_conversation_item_count"] or not 0 <= _number(value.get("recall"), "scoring_report_diagnostic_schema_invalid") <= 1: raise CustodyError("scoring_report_diagnostic_schema_invalid")
    for arm_id, arm in row["arms"].items():
        if arm_id not in CURRENT_ARMS | {"original_public_product"}: raise CustodyError("scoring_report_arm_schema_invalid")
        arm = _o(arm, "scoring_report_arm_schema_invalid")
        expected_arm = {"positive", "confidence_separability", "official_style_evidence_conversation_diagnostic"} | ({"original_replicates"} if arm_id == "original_public_product" else set())
        if set(arm) != expected_arm: raise CustodyError("scoring_report_arm_schema_invalid")
        positive = _o(arm["positive"], "scoring_report_positive_schema_invalid")
        if set(positive) != {"overall", "by_exact_group", "by_declared_context", "derived_hard_changing_and_implicit"}: raise CustodyError("scoring_report_positive_schema_invalid")
        split_summary(positive["overall"])
        exact = _o(positive["by_exact_group"], "scoring_report_positive_schema_invalid")
        if set(exact) != {group for group, endpoint in UPSTREAM_GROUPS.items() if endpoint == "positive"}: raise CustodyError("scoring_report_positive_schema_invalid")
        for summary in exact.values(): split_summary(summary)
        contexts = _o(positive["by_declared_context"], "scoring_report_positive_schema_invalid")
        if not contexts or any(not isinstance(context, str) or not context.isdigit() or int(context) <= 0 for context in contexts): raise CustodyError("scoring_report_positive_schema_invalid")
        for context, summary in contexts.items():
            summary = _o(summary, "scoring_report_positive_schema_invalid")
            if set(summary) != {"declared_context_size", "actual_conversation_count", "actual_message_count", "question_macro", "persona_macro"} or summary["declared_context_size"] != int(context): raise CustodyError("scoring_report_positive_schema_invalid")
            for name in ("actual_conversation_count", "actual_message_count"):
                distribution = _o(summary[name], "scoring_report_positive_schema_invalid")
                if set(distribution) != {"min", "max", "mean"}: raise CustodyError("scoring_report_positive_schema_invalid")
                minimum, maximum, mean = _integer(distribution["min"], "scoring_report_positive_schema_invalid", positive=True), _integer(distribution["max"], "scoring_report_positive_schema_invalid", positive=True), _number(distribution["mean"], "scoring_report_positive_schema_invalid")
                if minimum > maximum or not minimum <= mean <= maximum: raise CustodyError("scoring_report_positive_schema_invalid")
            split_summary({"question_macro": summary["question_macro"], "persona_macro": summary["persona_macro"]})
        derived = _o(positive["derived_hard_changing_and_implicit"], "scoring_report_positive_schema_invalid")
        if set(derived) != {"derived", "question_macro", "persona_macro"} or derived.get("derived") is not True: raise CustodyError("scoring_report_positive_schema_invalid")
        split_summary({"question_macro": derived["question_macro"], "persona_macro": derived["persona_macro"]})
        confidence = _o(arm["confidence_separability"], "scoring_report_confidence_schema_invalid")
        if set(confidence) != {"available", "reason", "by_declared_context"} or not isinstance(confidence["available"], bool): raise CustodyError("scoring_report_confidence_schema_invalid")
        if confidence["available"] != (arm_id in CURRENT_ARMS) or (confidence["available"] and confidence["reason"] is not None) or (not confidence["available"] and confidence["reason"] != "arm_has_no_frozen_comparable_confidence_contract"):
            raise CustodyError("scoring_report_confidence_schema_invalid")
        conf_contexts = _o(confidence["by_declared_context"], "scoring_report_confidence_schema_invalid")
        if confidence["available"] and not conf_contexts or not confidence["available"] and conf_contexts: raise CustodyError("scoring_report_confidence_schema_invalid")
        for context, stats in conf_contexts.items():
            if not isinstance(context, str) or not context.isdigit() or int(context) <= 0: raise CustodyError("scoring_report_confidence_schema_invalid")
            stats = _o(stats, "scoring_report_confidence_schema_invalid")
            if set(stats) != {"item_count", "positive_count", "negative_count", "auroc", "average_precision"}: raise CustodyError("scoring_report_confidence_schema_invalid")
            for key in ("item_count", "positive_count", "negative_count"): _integer(stats.get(key), "scoring_report_confidence_schema_invalid", positive=True)
            if stats["item_count"] != stats["positive_count"] + stats["negative_count"]: raise CustodyError("scoring_report_confidence_schema_invalid")
            for key in ("auroc", "average_precision"):
                if not 0 <= _number(stats.get(key), "scoring_report_confidence_schema_invalid") <= 1: raise CustodyError("scoring_report_confidence_schema_invalid")
        diagnostic(arm["official_style_evidence_conversation_diagnostic"], public=True)
        if arm_id == "original_public_product":
            replicas = _l(arm["original_replicates"], "scoring_report_replicate_schema_invalid")
            if len(replicas) != 5: raise CustodyError("scoring_report_replicate_schema_invalid")
            replica_diagnostics = []
            for number, replica in enumerate(replicas):
                replica = _o(replica, "scoring_report_replicate_schema_invalid")
                if set(replica) != {"replicate_index", "build_id", "index_sha256", "overall_positive", "by_exact_group", "derived_hard_changing_and_implicit", "official_style_evidence_conversation_diagnostic"} or replica["replicate_index"] != number or not isinstance(replica["build_id"], str) or not replica["build_id"]: raise CustodyError("scoring_report_replicate_schema_invalid")
                _h(replica["index_sha256"], "scoring_report_replicate_schema_invalid")
                split_summary(replica["overall_positive"]); split_summary(replica["derived_hard_changing_and_implicit"]); diagnostic(replica["official_style_evidence_conversation_diagnostic"], public=False)
                replica_diagnostics.append(replica["official_style_evidence_conversation_diagnostic"])
                exact_replica = _o(replica["by_exact_group"], "scoring_report_replicate_schema_invalid")
                if set(exact_replica) != set(exact): raise CustodyError("scoring_report_replicate_schema_invalid")
                for summary in exact_replica.values(): split_summary(summary)
            aggregate = _o(arm["official_style_evidence_conversation_diagnostic"], "scoring_report_replicate_schema_invalid")
            expected_total = sum(item["total_relevant_conversation_item_count"] for item in replica_diagnostics); expected_hit = sum(item["retrieved_relevant_conversation_count"] for item in replica_diagnostics)
            if aggregate["total_relevant_conversation_item_count"] != expected_total or aggregate["retrieved_relevant_conversation_count"] != expected_hit or aggregate["positive_item_count"] != expected_total or not math.isclose(float(aggregate["recall"]), expected_hit / expected_total, rel_tol=0.0, abs_tol=1e-15): raise CustodyError("scoring_report_replicate_aggregate_invalid")
    bootstrap = _o(row["paired_bootstrap"], "scoring_report_bootstrap_schema_invalid")
    if set(bootstrap) != {"overall_positive", "derived_hard_changing_and_implicit", "static_p5_vs_strong_raw_abstention_confidence"}: raise CustodyError("scoring_report_bootstrap_schema_invalid")
    def retrieval_bootstrap(section: Any, expected_subset: str) -> None:
        section = _o(section, "scoring_report_bootstrap_schema_invalid")
        expected = {"subset", "metric", "reference_arm", "original_replicate_rule", "original_replicate_count", "bootstrap_plan_sha256", "resamples", "seed", "percentile_rule", "paired_deltas"}
        if set(section) != expected or section.get("subset") != expected_subset or section.get("metric") != "positive_persona_macro_recall_at_10" or section.get("reference_arm") not in row["arms"] or section.get("original_replicate_rule") not in {"per_query_arithmetic_mean", "global_build_multiset_per_draw"} or section.get("original_replicate_rule") != protocol["bootstrap"].get("original_replicate_rule") or section.get("original_replicate_count") != 5 or section.get("percentile_rule") != "linear": raise CustodyError("scoring_report_bootstrap_schema_invalid")
        _h(section.get("bootstrap_plan_sha256"), "scoring_report_bootstrap_schema_invalid")
        _integer(section.get("resamples"), "scoring_report_bootstrap_schema_invalid", positive=True); _integer(section.get("seed"), "scoring_report_bootstrap_schema_invalid")
        if section["resamples"] != protocol["bootstrap"].get("resamples") or section["seed"] != protocol["bootstrap"].get("seed"): raise CustodyError("scoring_report_bootstrap_schema_invalid")
        paired = _o(section["paired_deltas"], "scoring_report_bootstrap_schema_invalid")
        challengers = set(row["arms"]) - {"original_public_product"}
        if set(paired) != challengers: raise CustodyError("scoring_report_bootstrap_schema_invalid")
        for challenger, comparisons in paired.items():
            comparisons = _o(comparisons, "scoring_report_bootstrap_schema_invalid")
            expected_comparisons = {"vs_original_public_product"} | ({"vs_reference"} if challenger != section["reference_arm"] else set())
            if set(comparisons) != expected_comparisons: raise CustodyError("scoring_report_bootstrap_schema_invalid")
            for comparison in comparisons.values():
                comparison = _o(comparison, "scoring_report_bootstrap_schema_invalid")
                if set(comparison) != {"estimate", "ci_lower", "ci_upper", "resample_count"}: raise CustodyError("scoring_report_bootstrap_schema_invalid")
                _estimate, low, high = (_number(comparison[key], "scoring_report_bootstrap_schema_invalid") for key in ("estimate", "ci_lower", "ci_upper"))
                if low > high or comparison["resample_count"] != section["resamples"]: raise CustodyError("scoring_report_bootstrap_schema_invalid")
                _integer(comparison["resample_count"], "scoring_report_bootstrap_schema_invalid", positive=True)
    retrieval_bootstrap(bootstrap["overall_positive"], "overall_positive")
    retrieval_bootstrap(bootstrap["derived_hard_changing_and_implicit"], "derived_hard_changing_and_implicit")
    confidence_bootstrap = _o(bootstrap["static_p5_vs_strong_raw_abstention_confidence"], "scoring_report_bootstrap_schema_invalid")
    if set(confidence_bootstrap) != {"comparison", "unit", "metrics"} or confidence_bootstrap.get("comparison") != "static_p5_vs_strong_raw" or confidence_bootstrap.get("unit") != "paired_persona_by_declared_context": raise CustodyError("scoring_report_bootstrap_schema_invalid")
    confidence_metrics = _o(confidence_bootstrap["metrics"], "scoring_report_bootstrap_schema_invalid")
    if set(confidence_metrics) != {"auroc", "average_precision"}: raise CustodyError("scoring_report_bootstrap_schema_invalid")
    for name, comparison in confidence_metrics.items():
        comparison = _o(comparison, "scoring_report_bootstrap_schema_invalid")
        if set(comparison) != {"pre_registered_scalar", "estimate", "ci_lower", "ci_upper", "resample_count"} or comparison["pre_registered_scalar"] != name: raise CustodyError("scoring_report_bootstrap_schema_invalid")
        _estimate, low, high = (_number(comparison[key], "scoring_report_bootstrap_schema_invalid") for key in ("estimate", "ci_lower", "ci_upper"))
        if low > high or comparison["resample_count"] != protocol["bootstrap"].get("resamples"): raise CustodyError("scoring_report_bootstrap_schema_invalid")
        _integer(comparison["resample_count"], "scoring_report_bootstrap_schema_invalid", positive=True)
    return row
