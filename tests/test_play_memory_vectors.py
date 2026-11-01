"""Persistent Qwen vector-cache contracts using fake encoders and real SQLite."""
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from mempalace_rpg.play_memory_encoder import DIMENSIONS, QwenRankerConfig, digest
from mempalace_rpg.play_memory_qwen import create_qwen_ranker
from mempalace_rpg.retrieval import AuthorizedRetrievalCandidate


def unit(index=0):
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


class FakeEncoder:
    def __init__(self, identity="offline-persistent-fake-v1"):
        self.identity = identity
        self.queries = []
        self.passages = []

    def encode_query(self, text):
        self.queries.append(text)
        return unit()

    def encode_passages(self, texts):
        self.passages.append(list(texts))
        return [unit() for _ in texts]


def config(tmp_path, **kwargs):
    return QwenRankerConfig(tmp_path, "a" * 64, "pinned-revision", **kwargs)


def candidate(text, identifier="authorized-event"):
    return AuthorizedRetrievalCandidate(
        source_event_id=identifier,
        source_scene_id="scene",
        raw_text="original remains lexical-only",
        observation=text,
        checkpoint_key="checkpoint",
        policy_tuple=("authorized",),
        chronological_order_key=(1, identifier),
        ranking_key=identifier,
    )


def row_count(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT count(*) FROM passage_vectors").fetchone()[0]


def test_passage_vectors_reopen_without_reencoding_and_queries_stay_ephemeral(tmp_path):
    path = tmp_path / "qwen-vectors.sqlite"
    passage = "private passage contents must not enter SQLite"
    query = "private query contents must not enter SQLite"

    first_encoder = FakeEncoder()
    first_ranker = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=first_encoder)
    first_ranker.prepare(candidates=[candidate(passage)])
    first_ranker.close()
    assert first_encoder.passages == [[passage]]
    assert first_encoder.queries == []
    assert row_count(path) == 1

    second_encoder = FakeEncoder()
    second_ranker = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=second_encoder)
    prepared = second_ranker.prepare(candidates=[candidate(passage)])
    assert prepared["cacheHits"] == 1 and prepared["cacheMisses"] == 0
    assert second_encoder.passages == []

    second_ranker.rank(query=query, candidates=[candidate(passage)])
    assert second_encoder.passages == []
    assert second_encoder.queries == [query]
    second_ranker.close()

    with sqlite3.connect(path) as db:
        rows = db.execute("SELECT role, text_sha256, dimensions, dtype, vector FROM passage_vectors").fetchall()
        metadata = dict(db.execute("SELECT key, value FROM vector_cache_metadata"))
    assert len(rows) == 1
    role, text_sha, dimensions, dtype, vector_blob = rows[0]
    assert (role, text_sha, dimensions, dtype) == ("passage", digest(passage), 768, "float32-le")
    assert len(vector_blob) == 768 * 4
    assert metadata["contents"] == "passage-vectors-only"
    raw_db = path.read_bytes()
    assert passage.encode() not in raw_db
    assert query.encode() not in raw_db


def test_cache_keys_invalidate_on_exact_text_and_semantic_profile_but_not_location(tmp_path):
    path = tmp_path / "qwen-vectors.sqlite"
    other_path = tmp_path / "other-vectors.sqlite"
    cfg = config(tmp_path, vector_cache_path=path)
    same_profile_elsewhere = config(tmp_path, vector_cache_path=other_path)
    first_location = create_qwen_ranker(cfg, encoder=FakeEncoder())
    second_location = create_qwen_ranker(same_profile_elsewhere, encoder=FakeEncoder())
    assert first_location.identity == second_location.identity
    first_location.close()
    second_location.close()

    first_encoder = FakeEncoder()
    first = create_qwen_ranker(cfg, encoder=first_encoder)
    first.prepare(candidates=[candidate("exact summary")])
    first.close()
    assert first_encoder.passages == [["exact summary"]]

    same_encoder = FakeEncoder()
    same = create_qwen_ranker(cfg, encoder=same_encoder)
    assert same.prepare(candidates=[candidate("exact summary")])["cacheHits"] == 1
    same.close()
    assert same_encoder.passages == []

    changed_text_encoder = FakeEncoder()
    changed_text = create_qwen_ranker(cfg, encoder=changed_text_encoder)
    assert changed_text.prepare(candidates=[candidate("exact summary ")])["cacheMisses"] == 1
    changed_text.close()
    assert changed_text_encoder.passages == [["exact summary "]]

    changed_revision_encoder = FakeEncoder()
    changed_revision = create_qwen_ranker(
        replace(cfg, revision="different-revision"), encoder=changed_revision_encoder)
    assert changed_revision.prepare(candidates=[candidate("exact summary")])["cacheMisses"] == 1
    changed_revision.close()
    assert changed_revision_encoder.passages == [["exact summary"]]

    changed_encoder_identity = FakeEncoder("another-encoder-profile")
    changed_encoder = create_qwen_ranker(cfg, encoder=changed_encoder_identity)
    assert changed_encoder.prepare(candidates=[candidate("exact summary")])["cacheMisses"] == 1
    changed_encoder.close()
    assert changed_encoder_identity.passages == [["exact summary"]]


@pytest.mark.parametrize("corruption", ["dimensions", "length", "checksum", "unit", "finite", "role"])
def test_corrupt_persisted_vectors_raise_without_reencoding(tmp_path, corruption):
    path = tmp_path / "qwen-vectors.sqlite"
    passage = "persisted passage"
    writer = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=FakeEncoder())
    writer.prepare(candidates=[candidate(passage)])
    writer.close()

    with sqlite3.connect(path) as db:
        if corruption == "dimensions":
            db.execute("UPDATE passage_vectors SET dimensions = 767")
        elif corruption == "length":
            db.execute("UPDATE passage_vectors SET vector = ?", (b"short",))
        elif corruption == "checksum":
            damaged = np.asarray(unit(1), dtype="<f4").tobytes()
            db.execute("UPDATE passage_vectors SET vector = ?", (damaged,))
        elif corruption == "unit":
            damaged = np.zeros(DIMENSIONS, dtype="<f4").tobytes()
            db.execute("UPDATE passage_vectors SET vector = ?, vector_sha256 = ?",
                       (damaged, hashlib.sha256(damaged).hexdigest()))
        elif corruption == "finite":
            damaged_array = np.zeros(DIMENSIONS, dtype="<f4")
            damaged_array[0] = np.inf
            damaged = damaged_array.tobytes()
            db.execute("UPDATE passage_vectors SET vector = ?, vector_sha256 = ?",
                       (damaged, hashlib.sha256(damaged).hexdigest()))
        else:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE passage_vectors SET role = 'query'")

    reader_encoder = FakeEncoder()
    reader = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=reader_encoder)
    with pytest.raises(ValueError, match="vector cache|finite unit vectors"):
        reader.prepare(candidates=[candidate(passage)])
    assert reader_encoder.passages == []
    reader.close()


def test_invalid_new_batch_is_validated_before_database_or_memory_publication(tmp_path):
    path = tmp_path / "qwen-vectors.sqlite"
    encoder = FakeEncoder()
    encoder.encode_passages = lambda texts: [unit(), [0.0] * DIMENSIONS]
    ranker = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=encoder)

    with pytest.raises(ValueError, match="finite unit vectors"):
        ranker.prepare(candidates=[candidate("valid", "first"), candidate("invalid", "second")])

    assert not ranker._cache
    assert row_count(path) == 0
    ranker.close()


def test_sqlite_batch_failure_rolls_back_without_memory_publication(tmp_path):
    path = tmp_path / "qwen-vectors.sqlite"
    encoder = FakeEncoder()
    ranker = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=encoder)
    blocked_sha = digest("blocked")
    with sqlite3.connect(path) as db:
        db.execute(f"""
            CREATE TRIGGER reject_blocked_passage BEFORE INSERT ON passage_vectors
            WHEN NEW.text_sha256 = '{blocked_sha}'
            BEGIN SELECT RAISE(ABORT, 'injected cache write failure'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="injected cache write failure"):
        ranker.prepare(candidates=[candidate("accepted", "first"), candidate("blocked", "second")])

    assert not ranker._cache
    assert row_count(path) == 0
    ranker.close()


def test_incompatible_cache_metadata_and_sqlite_failures_surface(tmp_path):
    path = tmp_path / "qwen-vectors.sqlite"
    writer = create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=FakeEncoder())
    writer.prepare(candidates=[candidate("summary")])
    writer.close()
    with sqlite3.connect(path) as db:
        db.execute("UPDATE vector_cache_metadata SET value = 'query-vectors-too' WHERE key = 'contents'")
    with pytest.raises(ValueError, match="metadata"):
        create_qwen_ranker(config(tmp_path, vector_cache_path=path), encoder=FakeEncoder())

    broken_path = tmp_path / "broken.sqlite"
    broken_path.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        create_qwen_ranker(config(tmp_path, vector_cache_path=broken_path), encoder=FakeEncoder())


def test_no_path_keeps_legacy_in_memory_behavior_and_config_validation(tmp_path):
    encoder = FakeEncoder()
    ranker = create_qwen_ranker(config(tmp_path), encoder=encoder)
    assert ranker._vector_cache is None
    ranker.prepare(candidates=[candidate("legacy passage")])
    ranker.prepare(candidates=[candidate("legacy passage")])
    assert encoder.passages == [["legacy passage"]]
    ranker.close()

    with pytest.raises(ValueError, match="absolute"):
        replace(config(tmp_path), vector_cache_path="relative.sqlite")
    with pytest.raises(ValueError, match="parent directory"):
        replace(config(tmp_path), vector_cache_path=tmp_path / "missing" / "vectors.sqlite")
    with pytest.raises(ValueError, match="regular file"):
        replace(config(tmp_path), vector_cache_path=tmp_path)


def test_play_memory_server_passes_vector_cache_option_to_qwen_config(monkeypatch, tmp_path):
    from mempalace_rpg import play_memory_qwen, play_memory_server

    captured = {}

    class ClosedRanker:
        def close(self):
            captured["closed"] = True

    def fake_create(config, *, encoder=None):
        captured["config"] = config
        return ClosedRanker()

    monkeypatch.setattr(play_memory_qwen, "create_qwen_ranker", fake_create)
    monkeypatch.setattr(play_memory_server, "PlayMemoryStore", lambda *args, **kwargs: object())
    monkeypatch.setattr(play_memory_server, "serve", lambda *args, **kwargs: None)

    cache_path = tmp_path / "qwen-vectors.sqlite"
    result = play_memory_server.main([
        "--db", str(tmp_path / "play-memory.sqlite"),
        "--profile", "qwen-summary768-lexical-weave-v1",
        "--model-dir", str(tmp_path),
        "--model-tree-sha256", "a" * 64,
        "--model-revision", "pinned-revision",
        "--vector-cache-path", str(cache_path),
    ])

    assert result == 0
    assert Path(captured["config"].vector_cache_path) == cache_path
    assert captured["closed"] is True
