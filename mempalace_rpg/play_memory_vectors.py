"""Local SQLite persistence for validated Qwen passage vectors.

The cache stores only profile and exact-text digests plus float32 passage
vectors. Candidate membership, permissions, queries and source text stay with
the caller and are never read or reconstructed here.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import sqlite3
import threading

DIMENSIONS = 768
DTYPE = "float32-le"
VECTOR_BYTES = DIMENSIONS * 4
_SCHEMA_VERSION = "1"
_METADATA = {
    "schema_version": _SCHEMA_VERSION,
    "dimensions": str(DIMENSIONS),
    "dtype": DTYPE,
    "encoding": "little-endian-c-contiguous",
    "contents": "passage-vectors-only",
}


def validate_vector(values):
    """Return a read-only little-endian float32 unit vector or raise."""
    import numpy as np

    array = np.asarray(values)
    if array.dtype.kind not in "fiu" or array.shape != (DIMENSIONS,):
        raise ValueError("Qwen vector must contain exactly 768 numeric values")
    array = array.astype(np.dtype("<f4"), copy=True)
    norm = float(np.linalg.norm(array.astype(np.float64)))
    if not np.isfinite(array).all() or not math.isfinite(norm) or abs(norm - 1.0) > 1e-5:
        raise ValueError("Qwen vector must be finite unit vectors with L2 norm 1")
    array.flags.writeable = False
    return array


def _require_sha256(value, label):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{label} must be a lowercase SHA-256")


class PassageVectorCache:
    """Durable, profile-namespaced passage vectors backed by SQLite."""

    def __init__(self, path: str | Path, *, profile_identity: str):
        if not isinstance(path, (str, Path)) or not Path(path).is_absolute():
            raise ValueError("Qwen vector cache path must be absolute")
        self.path = Path(path)
        if not self.path.parent.is_dir():
            raise ValueError("Qwen vector cache parent directory must already exist")
        if self.path.is_symlink():
            raise ValueError("Qwen vector cache path must not be a symbolic link")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("Qwen vector cache path must be a regular file")
        if not isinstance(profile_identity, str) or not profile_identity.strip():
            raise ValueError("Qwen vector cache requires a semantic profile identity")

        self.namespace = hashlib.sha256(profile_identity.encode("utf-8", errors="strict")).hexdigest()
        self._lock = threading.RLock()
        self._closed = False
        self._db = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._initialize()
        except BaseException as error:
            try:
                self._db.close()
            except BaseException as close_error:
                raise error from close_error
            raise

    def _initialize(self):
        with self._lock:
            expected_tables = {"vector_cache_metadata", "passage_vectors"}
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # Recheck after taking the write lock so simultaneous first opens
                # cannot both try to create the same schema.
                existing = {row[0] for row in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
                if not existing:
                    self._db.execute("""
                        CREATE TABLE vector_cache_metadata (
                            key TEXT PRIMARY KEY,
                            value TEXT NOT NULL
                        ) WITHOUT ROWID
                    """)
                    self._db.execute("""
                        CREATE TABLE passage_vectors (
                            profile_sha256 TEXT NOT NULL CHECK(length(profile_sha256) = 64),
                            role TEXT NOT NULL CHECK(role = 'passage'),
                            text_sha256 TEXT NOT NULL CHECK(length(text_sha256) = 64),
                            dimensions INTEGER NOT NULL,
                            dtype TEXT NOT NULL,
                            vector BLOB NOT NULL,
                            vector_sha256 TEXT NOT NULL CHECK(length(vector_sha256) = 64),
                            PRIMARY KEY(profile_sha256, role, text_sha256)
                        ) WITHOUT ROWID
                    """)
                    self._db.executemany(
                        "INSERT INTO vector_cache_metadata(key, value) VALUES (?, ?)",
                        sorted(_METADATA.items()),
                    )
                elif existing != expected_tables:
                    raise ValueError("incompatible Qwen vector cache database tables")
                self._db.execute("COMMIT")
            except BaseException as error:
                self._rollback(error)
                raise

            rows = self._db.execute("SELECT key, value FROM vector_cache_metadata").fetchall()
            metadata = {row["key"]: row["value"] for row in rows}
            if len(rows) != len(metadata) or metadata != _METADATA:
                raise ValueError("incompatible Qwen vector cache metadata")

            # Force SQLite to validate the exact columns and primary-key shape.
            self._db.execute("""
                SELECT profile_sha256, role, text_sha256, dimensions, dtype, vector, vector_sha256
                FROM passage_vectors LIMIT 0
            """).fetchall()

    def _require_open(self):
        if self._closed:
            raise RuntimeError("Qwen vector cache is closed")

    def _rollback(self, primary_error):
        try:
            self._db.execute("ROLLBACK")
        except BaseException as rollback_error:
            raise primary_error from rollback_error

    @staticmethod
    def _decode_row(row):
        import numpy as np

        if row["role"] != "passage":
            raise ValueError("Qwen vector cache contains a non-passage vector")
        if type(row["dimensions"]) is not int or row["dimensions"] != DIMENSIONS:
            raise ValueError("Qwen vector cache row has incompatible dimensions")
        if row["dtype"] != DTYPE:
            raise ValueError("Qwen vector cache row has incompatible float32 encoding")
        blob = row["vector"]
        if type(blob) is not bytes or len(blob) != VECTOR_BYTES:
            raise ValueError("Qwen vector cache row has malformed float32 bytes")
        checksum = row["vector_sha256"]
        _require_sha256(checksum, "stored vector checksum")
        if hashlib.sha256(blob).hexdigest() != checksum:
            raise ValueError("Qwen vector cache row checksum mismatch")
        return validate_vector(np.frombuffer(blob, dtype=np.dtype("<f4"), count=DIMENSIONS))

    def _read_rows(self, text_sha256_values):
        result = {}
        # Stay below SQLite's conservative bound on bind parameters.
        for offset in range(0, len(text_sha256_values), 400):
            batch = text_sha256_values[offset:offset + 400]
            placeholders = ",".join("?" for _ in batch)
            rows = self._db.execute(
                "SELECT profile_sha256, role, text_sha256, dimensions, dtype, vector, vector_sha256 "
                "FROM passage_vectors WHERE profile_sha256 = ? "
                f"AND text_sha256 IN ({placeholders})",
                [self.namespace, *batch],
            ).fetchall()
            for row in rows:
                if row["profile_sha256"] != self.namespace:
                    raise ValueError("Qwen vector cache row has incompatible profile metadata")
                _require_sha256(row["text_sha256"], "stored text digest")
                if row["text_sha256"] in result:
                    raise ValueError("Qwen vector cache has duplicate role rows for a passage")
                result[row["text_sha256"]] = self._decode_row(row)
        return result

    def get_many(self, text_sha256_values):
        requested = list(dict.fromkeys(text_sha256_values))
        for value in requested:
            _require_sha256(value, "passage text digest")
        if not requested:
            return {}
        with self._lock:
            self._require_open()
            return self._read_rows(requested)

    def put_many(self, vectors_by_text_sha256):
        """Validate first, then publish the complete batch in one transaction."""
        prepared = []
        seen = set()
        for text_sha256, values in vectors_by_text_sha256.items():
            _require_sha256(text_sha256, "passage text digest")
            if text_sha256 in seen:
                raise ValueError("Qwen vector cache write batch contains duplicate passage digests")
            seen.add(text_sha256)
            vector = validate_vector(values)
            blob = vector.tobytes(order="C")
            if len(blob) != VECTOR_BYTES:
                raise ValueError("Qwen vector cache encoding produced an invalid byte length")
            prepared.append((self.namespace, "passage", text_sha256, DIMENSIONS, DTYPE,
                             blob, hashlib.sha256(blob).hexdigest()))
        if not prepared:
            return {}

        with self._lock:
            self._require_open()
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.executemany("""
                    INSERT INTO passage_vectors(
                        profile_sha256, role, text_sha256, dimensions, dtype, vector, vector_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(profile_sha256, role, text_sha256) DO NOTHING
                """, prepared)
                committed_vectors = self._read_rows([row[2] for row in prepared])
                if len(committed_vectors) != len(prepared):
                    raise ValueError("Qwen vector cache transaction did not publish its complete batch")
                self._db.execute("COMMIT")
                return committed_vectors
            except BaseException as error:
                self._rollback(error)
                raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True
