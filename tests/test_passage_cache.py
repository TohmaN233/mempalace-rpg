from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys

import pytest

from mempalace_rpg.passage_cache import (
    MAX_DATABASE_BYTES,
    MAX_DISK_ENTRIES,
    VECTOR_FORMAT,
    PassageCacheCorruptionError,
    PassageVectorCache,
    content_sha256,
)
from mempalace_rpg.runtime_factory import LocalMiniLMEncoder


def _namespace(value: str = "test namespace") -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector(text: str) -> list[float]:
    base = float(sum(text.encode("utf-8")))
    return [base + index / 100_000 for index in range(384)]


class FakeNative:
    def __init__(self, *, fail: bool = False, invalid: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail
        self.invalid = invalid

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("injected encoder failure")
        if self.invalid:
            return [[1.0] * 383 for _text in texts]
        return [_vector(text) for text in texts]


def _encoder(
    native: FakeNative,
    *,
    path: Path | None = None,
    namespace: str | None = None,
    max_memory_entries: int = 1024,
    max_disk_entries: int = MAX_DISK_ENTRIES,
) -> LocalMiniLMEncoder:
    cache = PassageVectorCache(
        namespace=namespace or _namespace(),
        path=path,
        max_memory_entries=max_memory_entries,
        max_disk_entries=max_disk_entries,
    )
    return LocalMiniLMEncoder(native, identity="stable-test-identity", passage_cache=cache)


def _cache_rows(path: Path) -> list[tuple[str, str, bytes, str]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT namespace, text_sha256, vector, vector_sha256 FROM passage_vectors ORDER BY rowid"
        ).fetchall()


def _seed_cache(path: Path, text: str = "cache seed", namespace: str | None = None) -> str:
    chosen_namespace = namespace or _namespace()
    encoder = _encoder(FakeNative(), path=path, namespace=chosen_namespace)
    encoder.encode_passages([text])
    encoder.close()
    return chosen_namespace


def test_passages_reuse_hits_and_preserve_duplicate_order() -> None:
    native = FakeNative()
    encoder = _encoder(native)
    try:
        actual = encoder.encode_passages(["first", "second", "first"])
        assert actual == [_vector("first"), _vector("second"), _vector("first")]
        assert native.calls == [["first", "second"]]

        repeated = encoder.encode_passages(["second", "first", "second"])
        assert repeated == [_vector("second"), _vector("first"), _vector("second")]
        assert native.calls == [["first", "second"]]
        assert repeated[0] is not repeated[2]
    finally:
        encoder.close()


def test_disk_cache_reuses_persistent_rows_and_encodes_only_incremental_misses(tmp_path: Path) -> None:
    path = tmp_path / "vectors.sqlite3"
    namespace = _seed_cache(path, "alpha")
    first = _encoder(FakeNative(), path=path, namespace=namespace)
    first.encode_passages(["beta"])
    first.close()

    native = FakeNative()
    second = _encoder(native, path=path, namespace=namespace)
    try:
        actual = second.encode_passages(["alpha", "gamma", "beta", "gamma"])
        assert actual == [_vector("alpha"), _vector("gamma"), _vector("beta"), _vector("gamma")]
        assert native.calls == [["gamma"]]
    finally:
        second.close()

    # A separate interpreter can read the same rows without invoking inference.
    script = "\n".join(
        [
            "import json, sys",
            "from mempalace_rpg.passage_cache import PassageVectorCache",
            "from mempalace_rpg.runtime_factory import LocalMiniLMEncoder",
            "class FailingNative:",
            "    def __call__(self, texts): raise AssertionError('persistent cache missed')",
            "cache = PassageVectorCache(namespace=sys.argv[2], path=sys.argv[1])",
            "encoder = LocalMiniLMEncoder(FailingNative(), identity='stable-test-identity', passage_cache=cache)",
            "print(json.dumps(encoder.encode_passages(['gamma'])[0]))",
            "encoder.close()",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), namespace],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=True,
        text=True,
        timeout=30,
    )
    assert json.loads(result.stdout) == _vector("gamma")


def test_cache_namespace_mismatch_never_reuses_another_namespace(tmp_path: Path) -> None:
    path = tmp_path / "vectors.sqlite3"
    first_namespace = _seed_cache(path, "private text never stored")
    other_namespace = _namespace("different model or implementation")
    native = FakeNative()
    encoder = _encoder(native, path=path, namespace=other_namespace)
    try:
        assert encoder.encode_passages(["private text never stored"]) == [_vector("private text never stored")]
        assert native.calls == [["private text never stored"]]
    finally:
        encoder.close()

    rows = _cache_rows(path)
    assert {row[0] for row in rows} == {first_namespace, other_namespace}
    assert b"private text never stored" not in path.read_bytes()


def test_memory_and_disk_storage_bounds_evict_oldest_entries(tmp_path: Path) -> None:
    path = tmp_path / "bounded.sqlite3"
    native = FakeNative()
    encoder = _encoder(
        native, path=path, max_memory_entries=2, max_disk_entries=2,
    )
    try:
        encoder.encode_passages(["one", "two", "three"])
        assert len(encoder._passage_cache._memory) == 2
        assert len(_cache_rows(path)) == 2
        assert path.stat().st_size <= MAX_DATABASE_BYTES

        encoder.encode_passages(["one"])
        assert native.calls == [["one", "two", "three"], ["one"]]
    finally:
        encoder.close()

    memory_only = PassageVectorCache(namespace=_namespace("memory only"), max_memory_entries=2)
    try:
        memory_only.put_many({content_sha256("a"): _vector("a"), content_sha256("b"): _vector("b")})
        memory_only.put_many({content_sha256("c"): _vector("c")})
        assert len(memory_only._memory) == 2
    finally:
        memory_only.close()


def test_oversized_batch_returns_all_vectors_but_persists_only_bounded_suffix(tmp_path: Path) -> None:
    path = tmp_path / "oversized.sqlite3"
    native = FakeNative()
    encoder = _encoder(
        native, path=path, max_memory_entries=2, max_disk_entries=2,
    )
    try:
        texts = ["first", "second", "third", "fourth"]
        assert encoder.encode_passages(texts) == [_vector(text) for text in texts]
        assert native.calls == [texts]
        assert len(_cache_rows(path)) == 2
        assert [row[1] for row in _cache_rows(path)] == [
            content_sha256("third"), content_sha256("fourth"),
        ]
        assert list(encoder._passage_cache._memory) == [
            content_sha256("third"), content_sha256("fourth"),
        ]
    finally:
        encoder.close()


def test_eviction_finishes_before_each_sqlite_insert(tmp_path: Path) -> None:
    path = tmp_path / "insert-bound.sqlite3"
    namespace = _namespace("insert bound")
    cache = PassageVectorCache(namespace=namespace, path=path, max_disk_entries=2)
    cache.put_many({
        content_sha256("old one"): _vector("old one"),
        content_sha256("old two"): _vector("old two"),
    })
    cache._connection.execute(
        "CREATE TRIGGER enforce_preinsert_row_bound BEFORE INSERT ON passage_vectors "
        "BEGIN SELECT CASE WHEN (SELECT COUNT(*) FROM passage_vectors) >= 2 "
        "THEN RAISE(ABORT, 'preinsert row bound exceeded') END; END"
    )
    try:
        cache.put_many({
            content_sha256("new one"): _vector("new one"),
            content_sha256("new two"): _vector("new two"),
        })
    finally:
        cache.close()
    assert [row[1] for row in _cache_rows(path)] == [
        content_sha256("new one"), content_sha256("new two"),
    ]


def test_oversized_batch_insert_failure_rolls_back_eviction_and_preserves_old_rows(tmp_path: Path) -> None:
    path = tmp_path / "oversized-rollback.sqlite3"
    namespace = _namespace("oversized rollback")
    cache = PassageVectorCache(namespace=namespace, path=path, max_disk_entries=2)
    old_vectors = {
        content_sha256("old one"): _vector("old one"),
        content_sha256("old two"): _vector("old two"),
    }
    cache.put_many(old_vectors)
    failed_digest = content_sha256("fourth")
    cache._connection.execute(
        "CREATE TRIGGER inject_insert_failure BEFORE INSERT ON passage_vectors "
        f"WHEN NEW.text_sha256='{failed_digest}' "
        "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
            cache.put_many({
                content_sha256("first"): _vector("first"),
                content_sha256("second"): _vector("second"),
                content_sha256("third"): _vector("third"),
                failed_digest: _vector("fourth"),
            })
    finally:
        cache.close()
    rows = _cache_rows(path)
    assert [row[1] for row in rows] == list(old_vectors)
    assert [row[2] for row in rows] == [
        struct.pack(VECTOR_FORMAT, *old_vectors[digest]) for digest in old_vectors
    ]


def test_cache_path_requires_absolute_existing_parent_and_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        PassageVectorCache(namespace=_namespace(), path=Path("relative.sqlite3"))
    with pytest.raises(ValueError, match="parent directory"):
        PassageVectorCache(namespace=_namespace(), path=tmp_path / "missing" / "cache.sqlite3")
    with pytest.raises(ValueError, match="regular file"):
        PassageVectorCache(namespace=_namespace(), path=tmp_path)
    with pytest.raises(ValueError, match=str(MAX_DISK_ENTRIES)):
        PassageVectorCache(namespace=_namespace(), max_disk_entries=MAX_DISK_ENTRIES + 1)

    empty_file = tmp_path / "empty.sqlite3"
    empty_file.touch()
    with pytest.raises(PassageCacheCorruptionError, match="empty"):
        PassageVectorCache(namespace=_namespace(), path=empty_file)

    malformed = tmp_path / "not-sqlite.sqlite3"
    malformed.write_bytes(b"this is not a SQLite database")
    with pytest.raises(PassageCacheCorruptionError, match="invalid"):
        PassageVectorCache(namespace=_namespace(), path=malformed)


def test_schema_corruption_is_reported_without_rebuilding(tmp_path: Path) -> None:
    path = tmp_path / "schema.sqlite3"
    _seed_cache(path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=91")

    before = path.read_bytes()
    with pytest.raises(PassageCacheCorruptionError, match="schema identity"):
        PassageVectorCache(namespace=_namespace(), path=path)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("corruption", "message"),
    [
        ("hash", "hash mismatch"),
        ("dimension", "invalid dimension"),
        ("nonfinite", "non-finite"),
    ],
)
def test_corrupt_vector_hash_dimension_and_finite_values_fail_closed(
    tmp_path: Path,
    corruption: str,
    message: str,
) -> None:
    path = tmp_path / f"{corruption}.sqlite3"
    namespace = _seed_cache(path, "corrupt me")
    with sqlite3.connect(path) as connection:
        if corruption == "hash":
            connection.execute(
                "UPDATE passage_vectors SET vector_sha256=?",
                ("0" * 64,),
            )
        elif corruption == "dimension":
            payload = b"short"
            connection.execute(
                "UPDATE passage_vectors SET vector=?, vector_sha256=?",
                (payload, hashlib.sha256(payload).hexdigest()),
            )
        else:
            payload = struct.pack(VECTOR_FORMAT, float("nan"), *([0.0] * 383))
            connection.execute(
                "UPDATE passage_vectors SET vector=?, vector_sha256=?",
                (payload, hashlib.sha256(payload).hexdigest()),
            )

    encoder = _encoder(FakeNative(), path=path, namespace=namespace)
    try:
        with pytest.raises(PassageCacheCorruptionError, match=message):
            encoder.encode_passages(["corrupt me"])
    finally:
        encoder.close()


def test_encoding_failure_does_not_commit_any_new_vectors(tmp_path: Path) -> None:
    path = tmp_path / "atomic.sqlite3"
    namespace = _seed_cache(path, "already cached")
    failing = _encoder(FakeNative(fail=True), path=path, namespace=namespace)
    try:
        with pytest.raises(RuntimeError, match="injected encoder failure"):
            failing.encode_passages(["new one", "new two"])
    finally:
        failing.close()
    assert [row[1] for row in _cache_rows(path)] == [content_sha256("already cached")]

    invalid = _encoder(FakeNative(invalid=True), path=path, namespace=namespace)
    try:
        with pytest.raises(ValueError, match="invalid 384-dimensional"):
            invalid.encode_passages(["new one", "new two"])
    finally:
        invalid.close()
    assert [row[1] for row in _cache_rows(path)] == [content_sha256("already cached")]


def test_queries_are_never_cached_and_logs_contain_no_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    native = FakeNative()
    encoder = _encoder(native)
    secret = "distinctive private query content"
    try:
        encoder.encode_passages([secret])
        encoder.encode_query(secret)
        encoder.encode_query(secret)
        assert native.calls == [[secret], [secret], [secret]]
        assert "input_count=1" in caplog.text
        assert secret not in caplog.text
    finally:
        encoder.close()


def test_closed_encoder_rejects_new_work() -> None:
    encoder = _encoder(FakeNative())
    encoder.close()
    encoder.close()
    with pytest.raises(RuntimeError, match="closed"):
        encoder.encode_query("query")
    with pytest.raises(RuntimeError, match="closed"):
        encoder.encode_passages(["passage"])
