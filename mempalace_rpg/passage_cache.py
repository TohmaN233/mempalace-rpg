"""Bounded, content-addressed cache for MiniLM passage vectors.

Persistent cache rows contain only namespace and content/vector digests plus the
binary vector itself. Source passages, evidence identifiers and access metadata
never enter this store.
"""
from __future__ import annotations

from collections import OrderedDict
from collections import deque
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
import struct
import threading
from typing import Mapping, Sequence


VECTOR_DIMENSIONS = 384
VECTOR_FORMAT = "<384d"
VECTOR_BYTES = struct.calcsize(VECTOR_FORMAT)
SCHEMA_VERSION = 1
SCHEMA_APPLICATION_ID = 0x4D504743  # ASCII "MPGC"
CACHE_ALGORITHM_ID = "minilm-exact-text-sha256-bounded-v1"
VECTOR_SERIALIZATION_ID = "ieee754-binary64-little-endian-384-v1"
MAX_MEMORY_ENTRIES = 1024
MAX_DISK_ENTRIES = 24_576
MAX_DATABASE_BYTES = 128 * 1024 * 1024
_SHA256_RE = re.compile(r"[a-f0-9]{64}\Z")


class PassageCacheCorruptionError(RuntimeError):
    """The persistent passage cache is malformed or contains an invalid row."""


def content_sha256(text: str) -> str:
    if not isinstance(text, str):
        raise TypeError("passage cache inputs must be strings")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pack_vector(vector: Sequence[float]) -> tuple[bytes, str]:
    if len(vector) != VECTOR_DIMENSIONS:
        raise ValueError("passage cache vectors must be 384-dimensional")
    values = tuple(float(value) for value in vector)
    if any(not math.isfinite(value) for value in values):
        raise ValueError("passage cache vectors must contain only finite values")
    payload = struct.pack(VECTOR_FORMAT, *values)
    return payload, hashlib.sha256(payload).hexdigest()


def _unpack_vector(payload: object, vector_sha256: object) -> list[float]:
    if not isinstance(payload, bytes) or len(payload) != VECTOR_BYTES:
        raise PassageCacheCorruptionError("cached passage vector has an invalid dimension")
    if not isinstance(vector_sha256, str) or not _SHA256_RE.fullmatch(vector_sha256):
        raise PassageCacheCorruptionError("cached passage vector hash is malformed")
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 != vector_sha256:
        raise PassageCacheCorruptionError("cached passage vector hash mismatch")
    try:
        vector = list(struct.unpack(VECTOR_FORMAT, payload))
    except struct.error as exc:
        raise PassageCacheCorruptionError("cached passage vector serialization is malformed") from exc
    if any(not math.isfinite(value) for value in vector):
        raise PassageCacheCorruptionError("cached passage vector contains a non-finite value")
    return vector


def _validated_cache_path(path: str | Path | None) -> Path | None:
    if path is None:
        return None
    if not isinstance(path, (str, Path)):
        raise TypeError("passage_cache_path must be a string, Path, or None")
    raw = os.fspath(path)
    if not raw or "\x00" in raw:
        raise ValueError("passage_cache_path must be a non-empty filesystem path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("passage_cache_path must be absolute")
    if candidate.is_symlink():
        raise ValueError("passage_cache_path must not be a symbolic link")
    if candidate.exists() and not candidate.is_file():
        raise ValueError("passage_cache_path must name a regular file")
    parent = candidate.parent
    if not parent.exists() or not parent.is_dir():
        raise ValueError("passage cache parent directory must already exist")
    if parent.is_symlink():
        raise ValueError("passage cache parent directory must not be a symbolic link")
    resolved_parent = parent.resolve(strict=True)
    return resolved_parent / candidate.name


def validate_passage_cache_path(path: str | Path | None) -> Path | None:
    """Validate a cache path before expensive model initialization begins."""
    return _validated_cache_path(path)


def _table_shape(connection: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return [
        (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
        for row in rows
    ]


class PassageVectorCache:
    """A bounded memory cache optionally backed by a strict SQLite file."""

    def __init__(
        self,
        *,
        namespace: str,
        path: str | Path | None = None,
        max_memory_entries: int = MAX_MEMORY_ENTRIES,
        max_disk_entries: int = MAX_DISK_ENTRIES,
    ) -> None:
        if not isinstance(namespace, str) or not _SHA256_RE.fullmatch(namespace):
            raise ValueError("passage cache namespace must be a lowercase SHA-256 digest")
        if type(max_memory_entries) is not int or max_memory_entries <= 0:
            raise ValueError("max_memory_entries must be a positive integer")
        if type(max_disk_entries) is not int or not 0 < max_disk_entries <= MAX_DISK_ENTRIES:
            raise ValueError(f"max_disk_entries must be between 1 and {MAX_DISK_ENTRIES}")
        self.namespace = namespace
        self.max_memory_entries = max_memory_entries
        self.max_disk_entries = max_disk_entries
        self.path = _validated_cache_path(path)
        self._memory: OrderedDict[str, tuple[float, ...]] = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False
        self._connection: sqlite3.Connection | None = None
        if self.path is not None:
            self._connection = self._open_database(self.path)

    def _open_database(self, path: Path) -> sqlite3.Connection:
        existed = path.exists()
        if existed:
            if path.is_symlink() or not path.is_file():
                raise ValueError("passage_cache_path must name a regular, unlinked file")
            if path.stat().st_size == 0:
                raise PassageCacheCorruptionError("existing passage cache file is empty")
            if path.stat().st_size > MAX_DATABASE_BYTES:
                raise PassageCacheCorruptionError("passage cache database exceeds its storage bound")

        connection = sqlite3.connect(
            str(path), timeout=30.0, isolation_level=None, check_same_thread=False,
        )
        try:
            connection.execute("PRAGMA busy_timeout=30000")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN EXCLUSIVE")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            app_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if not existed and not tables and app_id == 0 and user_version == 0:
                self._create_schema(connection)
            else:
                self._validate_schema(connection, tables, app_id, user_version)
            connection.commit()

            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if journal_mode != "delete":
                raise PassageCacheCorruptionError("passage cache uses an unsupported journal mode")
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            max_pages = MAX_DATABASE_BYTES // page_size
            if page_count > max_pages:
                raise PassageCacheCorruptionError("passage cache database exceeds its storage bound")
            configured_max = int(connection.execute(f"PRAGMA max_page_count={max_pages}").fetchone()[0])
            if configured_max > max_pages:
                raise PassageCacheCorruptionError("SQLite did not enforce the passage cache size bound")
            row_count = int(connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0])
            if row_count > self.max_disk_entries:
                raise PassageCacheCorruptionError("passage cache row count exceeds its storage bound")
            if path.stat().st_size > MAX_DATABASE_BYTES:
                raise PassageCacheCorruptionError("passage cache database exceeds its storage bound")
            return connection
        except BaseException as exc:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
            if isinstance(exc, sqlite3.DatabaseError):
                raise PassageCacheCorruptionError("persistent passage cache database is invalid") from exc
            raise

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE passage_cache_metadata ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "schema_version INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE passage_vectors ("
            "namespace TEXT NOT NULL, "
            "text_sha256 TEXT NOT NULL, "
            "vector BLOB NOT NULL, "
            "vector_sha256 TEXT NOT NULL, "
            "PRIMARY KEY(namespace, text_sha256))"
        )
        connection.execute(
            "INSERT INTO passage_cache_metadata(singleton, schema_version) VALUES(1, ?)",
            (SCHEMA_VERSION,),
        )
        connection.execute(f"PRAGMA application_id={SCHEMA_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _validate_schema(
        connection: sqlite3.Connection,
        tables: set[str],
        app_id: int,
        user_version: int,
    ) -> None:
        if app_id != SCHEMA_APPLICATION_ID or user_version != SCHEMA_VERSION:
            raise PassageCacheCorruptionError("passage cache schema identity is unsupported")
        if tables != {"passage_cache_metadata", "passage_vectors"}:
            raise PassageCacheCorruptionError("passage cache schema tables are invalid")
        objects = connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        expected_objects = [
            ("table", "passage_cache_metadata"),
            ("table", "passage_vectors"),
        ]
        if objects != expected_objects:
            raise PassageCacheCorruptionError("passage cache schema objects are invalid")

        expected_metadata = [
            ("singleton", "INTEGER", 0, None, 1),
            ("schema_version", "INTEGER", 1, None, 0),
        ]
        expected_vectors = [
            ("namespace", "TEXT", 1, None, 1),
            ("text_sha256", "TEXT", 1, None, 2),
            ("vector", "BLOB", 1, None, 0),
            ("vector_sha256", "TEXT", 1, None, 0),
        ]
        if _table_shape(connection, "passage_cache_metadata") != expected_metadata:
            raise PassageCacheCorruptionError("passage cache metadata table shape is invalid")
        if _table_shape(connection, "passage_vectors") != expected_vectors:
            raise PassageCacheCorruptionError("passage vector table shape is invalid")

        metadata_rows = connection.execute(
            "SELECT singleton, schema_version FROM passage_cache_metadata ORDER BY singleton"
        ).fetchall()
        if metadata_rows != [(1, SCHEMA_VERSION)]:
            raise PassageCacheCorruptionError("passage cache metadata row is invalid")

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("passage cache is closed")

    def _remember(self, text_sha256: str, vector: Sequence[float]) -> None:
        copied = tuple(float(value) for value in vector)
        self._memory[text_sha256] = copied
        self._memory.move_to_end(text_sha256)
        while len(self._memory) > self.max_memory_entries:
            self._memory.popitem(last=False)

    def get_many(self, text_sha256s: Sequence[str]) -> dict[str, list[float]]:
        with self._lock:
            self._ensure_open()
            requested = list(dict.fromkeys(text_sha256s))
            if any(not isinstance(value, str) or not _SHA256_RE.fullmatch(value) for value in requested):
                raise ValueError("passage cache content keys must be lowercase SHA-256 digests")
            found: dict[str, list[float]] = {}
            disk_misses: list[str] = []
            for digest in requested:
                vector = self._memory.get(digest)
                if vector is None:
                    disk_misses.append(digest)
                else:
                    self._memory.move_to_end(digest)
                    found[digest] = list(vector)

            if self._connection is not None and disk_misses:
                try:
                    for start in range(0, len(disk_misses), 400):
                        chunk = disk_misses[start:start + 400]
                        placeholders = ",".join("?" for _ in chunk)
                        rows = self._connection.execute(
                            "SELECT namespace, text_sha256, vector, vector_sha256 "
                            f"FROM passage_vectors WHERE namespace=? AND text_sha256 IN ({placeholders})",
                            (self.namespace, *chunk),
                        ).fetchall()
                        for row_namespace, row_digest, payload, vector_digest in rows:
                            if row_namespace != self.namespace:
                                raise PassageCacheCorruptionError("cached passage namespace mismatch")
                            if not isinstance(row_digest, str) or not _SHA256_RE.fullmatch(row_digest):
                                raise PassageCacheCorruptionError("cached passage content hash is malformed")
                            if row_digest not in chunk:
                                raise PassageCacheCorruptionError("cached passage content hash mismatch")
                            vector = _unpack_vector(payload, vector_digest)
                            self._remember(row_digest, vector)
                            found[row_digest] = vector
                except sqlite3.DatabaseError as exc:
                    raise PassageCacheCorruptionError("persistent passage cache read failed") from exc
            return found

    def put_many(self, vectors_by_text_sha256: Mapping[str, Sequence[float]]) -> None:
        with self._lock:
            self._ensure_open()
            disk_prepared: deque[tuple[str, bytes, str]] = deque(maxlen=self.max_disk_entries)
            memory_prepared: deque[tuple[str, tuple[float, ...]]] = deque(maxlen=self.max_memory_entries)
            prepared_count = 0
            for text_digest, vector in vectors_by_text_sha256.items():
                if not isinstance(text_digest, str) or not _SHA256_RE.fullmatch(text_digest):
                    raise ValueError("passage cache content keys must be lowercase SHA-256 digests")
                payload, vector_digest = _pack_vector(vector)
                vector_values = tuple(struct.unpack(VECTOR_FORMAT, payload))
                if self._connection is not None:
                    disk_prepared.append((text_digest, payload, vector_digest))
                memory_prepared.append((text_digest, vector_values))
                prepared_count += 1

            if self._connection is not None and prepared_count:
                connection = self._connection
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    if prepared_count <= self.max_disk_entries:
                        keys = [text_digest for text_digest, _payload, _digest in disk_prepared]
                        for start in range(0, len(keys), 400):
                            chunk = keys[start:start + 400]
                            placeholders = ",".join("?" for _ in chunk)
                            connection.execute(
                                "DELETE FROM passage_vectors WHERE namespace=? "
                                f"AND text_sha256 IN ({placeholders})",
                                (self.namespace, *chunk),
                            )

                    row_count = int(connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0])
                    existing_limit = self.max_disk_entries - len(disk_prepared)
                    overflow = row_count - existing_limit
                    if overflow > 0:
                        connection.execute(
                            "DELETE FROM passage_vectors WHERE rowid IN "
                            "(SELECT rowid FROM passage_vectors ORDER BY rowid LIMIT ?)",
                            (overflow,),
                        )
                    # All eviction completes before executemany starts, so a full
                    # database never needs temporary space for old and new rows.
                    connection.executemany(
                        "INSERT OR REPLACE INTO passage_vectors "
                        "(namespace, text_sha256, vector, vector_sha256) VALUES(?, ?, ?, ?)",
                        [
                            (self.namespace, text_digest, payload, vector_digest)
                            for text_digest, payload, vector_digest in disk_prepared
                        ],
                    )
                    final_count = int(connection.execute("SELECT COUNT(*) FROM passage_vectors").fetchone()[0])
                    if final_count > self.max_disk_entries:
                        raise RuntimeError("passage cache row bound was exceeded during insertion")
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise

            if prepared_count >= self.max_memory_entries:
                self._memory.clear()
            for text_digest, vector in memory_prepared:
                self._remember(text_digest, vector)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            self._closed = True

    def __enter__(self) -> "PassageVectorCache":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()
