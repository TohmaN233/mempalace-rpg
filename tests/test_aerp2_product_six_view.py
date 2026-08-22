from __future__ import annotations

import hashlib

import pytest

from mempalace_rpg import RankingResult, RpgMemoryKernel, SceneEventInput, SixViewRanker
from mempalace_rpg.retrieval import AuthorizedRetrievalCandidate, structured_observation


def _event(
    summary: str,
    *,
    visibility: str = "public_world",
    owner: str | None = None,
    payload: dict | None = None,
    actor: str = "hero",
    witness_set: list[str] | None = None,
) -> SceneEventInput:
    return SceneEventInput(
        event_type="memory", summary=summary, branch_id="main", branch_status="active",
        truth_status="canonical", visibility=visibility, access_owner_id=owner,
        source_span=summary, payload=payload or {}, actor_id=actor, target_id="target",
        witness_set=witness_set or [],
        related_entities=["companion"], related_quests=["quest"], related_locations=["town"],
    )


def _seed(kernel: RpgMemoryKernel) -> None:
    kernel.commit_scene(campaign_id="c", scene_id="allowed", in_world_time="1", transcript="ALLOWED TEXT", events=[_event("ALLOWED TEXT")])
    kernel.commit_scene(campaign_id="c", scene_id="denied", in_world_time="2", transcript="FORBIDDEN TEXT", events=[_event("FORBIDDEN TEXT", visibility="character_private", owner="other")])


class _SpyRanker:
    def __init__(self, returned: list[str] | None = None) -> None:
        self.seen: list[AuthorizedRetrievalCandidate] = []
        self.returned = returned
        self.calls = 0

    def rank(self, *, query: str, candidates: list[AuthorizedRetrievalCandidate]) -> RankingResult:
        self.calls += 1
        self.seen = list(candidates)
        ids = self.returned if self.returned is not None else [candidate.source_event_id for candidate in candidates]
        return RankingResult(ids, {identifier: float(index) for index, identifier in enumerate(reversed(ids), start=1)}, {
            "schema": "spy", "input_sha256": "digest", "view_digests": {},
            "selected": [{"source_event_id": item} for item in ids],
        })


def test_ranker_receives_only_authorized_text_and_kernel_rejects_bad_returned_ids(tmp_path):
    spy = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / "rpg.sqlite3"), retrieval_ranker=spy) as kernel:
        _seed(kernel)
        pack = kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="text")

    assert [candidate.source_event_id for candidate in spy.seen] == [pack.evidence[0]["source_event_id"]]
    assert "FORBIDDEN TEXT" not in "\n".join(candidate.raw_text + candidate.observation for candidate in spy.seen)
    assert "character_private" not in spy.seen[0].observation
    assert pack.policy_trace["retrieval_ranking"]["selected"][0]["source_event_id"] == pack.evidence[0]["source_event_id"]
    assert isinstance(pack.evidence[0]["rank_score"], float)

    bad = _SpyRanker(["unknown"])
    with RpgMemoryKernel(db_path=str(tmp_path / "bad.sqlite3"), retrieval_ranker=bad) as kernel:
        _seed(kernel)
        with pytest.raises(PermissionError, match="unauthorized"):
            kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="text")

    duplicate = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / "duplicate.sqlite3"), retrieval_ranker=duplicate) as kernel:
        _seed(kernel)
        event_id = kernel.authorized_evidence(campaign_id="c", actor_id="hero", actor_type="npc", query="text", budget=10).trace["authorized_candidate_ids"][0]
        duplicate.returned = [event_id, event_id]
        with pytest.raises(ValueError, match="duplicate"):
            kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="text")


def test_default_kernel_path_has_no_product_ranker_trace(tmp_path):
    with RpgMemoryKernel(db_path=str(tmp_path / "legacy.sqlite3")) as kernel:
        _seed(kernel)
        pack = kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="allowed")

    assert pack.evidence and pack.evidence[0]["text"] == "ALLOWED TEXT"
    assert "retrieval_ranking" not in pack.policy_trace


def test_empty_authorized_universe_calls_injected_ranker_and_six_view_binds_query_digest(tmp_path):
    spy = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / "empty.sqlite3"), retrieval_ranker=spy) as kernel:
        pack = kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="unseen query")

    assert spy.calls == 1
    assert spy.seen == []
    assert pack.evidence == []
    assert pack.policy_trace["retrieval_ranking"]["schema"] == "spy"
    encoder = _CountingEncoder()
    empty_trace = SixViewRanker(encoder).rank(query="unseen query", candidates=[]).trace
    assert {
        "schema", "encoder_identity", "weights", "rrf_k", "input_sha256",
        "query_sha256", "view_digests", "selected",
    } <= set(empty_trace)
    assert empty_trace["encoder_identity"] == encoder.identity
    assert empty_trace["weights"] == SixViewRanker.weights
    assert empty_trace["rrf_k"] == SixViewRanker.rrf_k
    assert empty_trace["query_sha256"] == hashlib.sha256(b"unseen query").hexdigest()
    assert "unseen query" not in str(empty_trace)
    assert encoder.queries == []
    assert encoder.passage_batches == []


class _CountingEncoder:
    identity = "counting-bge-v1"

    def __init__(self) -> None:
        self.passage_batches: list[list[str]] = []
        self.queries: list[str] = []

    def encode_passages(self, texts):
        self.passage_batches.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    def encode_query(self, query):
        self.queries.append(query)
        return [1.0, 0.0]


def test_forbidden_text_never_reaches_passage_or_query_encoder_and_trace_is_complete(tmp_path):
    encoder = _CountingEncoder()
    with RpgMemoryKernel(db_path=str(tmp_path / "dense.sqlite3"), retrieval_ranker=SixViewRanker(encoder)) as kernel:
        _seed(kernel)
        pack = kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="safe query")

    assert "FORBIDDEN TEXT" not in "\n".join(text for batch in encoder.passage_batches for text in batch)
    assert "FORBIDDEN TEXT" not in encoder.queries
    trace = pack.policy_trace["retrieval_ranking"]
    assert {"input_sha256", "view_digests", "selected", "encoder_identity"} <= set(trace)
    assert set(trace["selected"][0]["contributions"]) == set(SixViewRanker.weights)


def _candidate(
    identifier: str,
    policy: tuple[str | None, ...] = ("canonical", "public_world"),
    *,
    raw: str | None = None,
    observation: str | None = None,
    checkpoint: str = "checkpoint",
    scene_time: int = 1,
    ranking_key: str | None = None,
) -> AuthorizedRetrievalCandidate:
    ranking_key = ranking_key or identifier
    return AuthorizedRetrievalCandidate(
        identifier, "scene", raw or identifier + " raw", observation or identifier + " observation",
        checkpoint, policy, (scene_time, ranking_key), ranking_key,
    )


def test_passage_views_are_cached_but_each_query_is_encoded_once_and_checkpoint_texts_deduplicate():
    encoder = _CountingEncoder()
    ranker = SixViewRanker(encoder)
    same_observation = "summary=same\nevent_type=memory"
    candidates = [
        _candidate("a", ("canonical", "public"), observation=same_observation),
        _candidate("b", ("canonical", "private"), observation=same_observation),
    ]

    ranker.rank(query="first", candidates=candidates)
    first_passage_call_count = len(encoder.passage_batches)
    ranker.rank(query="second", candidates=candidates)

    assert encoder.queries == ["first", "second"]
    assert first_passage_call_count > 0
    assert len(encoder.passage_batches) == first_passage_call_count
    assert [same_observation] in encoder.passage_batches


def test_cjk_tokenization_matches_inside_a_cjk_run_and_rollups_are_chronological_policy_homogeneous():
    encoder = _CountingEncoder()
    ranker = SixViewRanker(encoder)
    result = ranker.rank(
        query="承诺",
        candidates=[
            _candidate("event_b", ("canonical", "public"), raw="无关", observation="summary=second", scene_time=2),
            _candidate("event_a", ("canonical", "private"), raw="玩家承诺永远守护你", observation="summary=first", scene_time=1),
        ],
    )

    assert result.ranked_event_ids[0] == "event_a"
    assert not any("summary=first\nsummary=second" in text for batch in encoder.passage_batches for text in batch)

    same_policy = [
        _candidate("late", raw="late", observation="late observation", scene_time=2),
        _candidate("early", raw="early", observation="early observation", scene_time=1),
    ]
    ranker.rank(query="q", candidates=same_policy)
    assert ["early observation\nlate observation"] in encoder.passage_batches


def test_ranking_key_makes_tied_rankings_and_digests_independent_of_event_uuid():
    first = [
        AuthorizedRetrievalCandidate("uuid-first-a", "scene-a", "alpha", "alpha observation", "session", ("canonical", "public"), (1, "dialog-a"), ranking_key="dialog-a"),
        AuthorizedRetrievalCandidate("uuid-first-b", "scene-b", "beta", "beta observation", "session", ("canonical", "public"), (2, "dialog-b"), ranking_key="dialog-b"),
    ]
    rebuilt = [
        AuthorizedRetrievalCandidate("uuid-rebuilt-b", "scene-b", "beta", "beta observation", "session", ("canonical", "public"), (2, "dialog-b"), ranking_key="dialog-b"),
        AuthorizedRetrievalCandidate("uuid-rebuilt-a", "scene-a", "alpha", "alpha observation", "session", ("canonical", "public"), (1, "dialog-a"), ranking_key="dialog-a"),
    ]

    first_result = SixViewRanker(_CountingEncoder()).rank(query="tie", candidates=first)
    rebuilt_result = SixViewRanker(_CountingEncoder()).rank(query="tie", candidates=rebuilt)
    first_keys = {candidate.source_event_id: candidate.ranking_key for candidate in first}
    rebuilt_keys = {candidate.source_event_id: candidate.ranking_key for candidate in rebuilt}

    assert [first_keys[event_id] for event_id in first_result.ranked_event_ids] == [rebuilt_keys[event_id] for event_id in rebuilt_result.ranked_event_ids] == ["dialog-a", "dialog-b"]
    assert first_result.trace["input_sha256"] == rebuilt_result.trace["input_sha256"]
    assert first_result.trace["view_digests"] == rebuilt_result.trace["view_digests"]
    assert "dialog-a" not in str(first_result.trace["selected"])


def test_duplicate_ranking_key_fails_closed():
    with pytest.raises(ValueError, match="ranking_key"):
        SixViewRanker(_CountingEncoder()).rank(query="q", candidates=[
            AuthorizedRetrievalCandidate("event-a", "scene", "one", "one", "session", ("canonical", "public"), (1, "same"), ranking_key="same"),
            AuthorizedRetrievalCandidate("event-b", "scene", "two", "two", "session", ("canonical", "public"), (2, "same"), ranking_key="same"),
        ])


def test_blank_ranking_key_fails_closed():
    with pytest.raises(ValueError, match="ranking_key"):
        SixViewRanker(_CountingEncoder()).rank(
            query="q",
            candidates=[AuthorizedRetrievalCandidate(
                "event", "scene", "text", "observation", "session",
                ("canonical", "public"), (1, "ignored"), ranking_key=" ",
            )],
        )


def test_kernel_uses_payload_ranking_key_and_falls_back_to_source_event_id(tmp_path):
    spy = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / "ranking-key.sqlite3"), retrieval_ranker=spy) as kernel:
        kernel.commit_scene(campaign_id="c", scene_id="explicit", in_world_time="1", transcript="EXPLICIT", events=[_event("EXPLICIT", payload={"retrieval_ranking_key": "opaque-dialog-1"})])
        kernel.commit_scene(campaign_id="c", scene_id="fallback", in_world_time="2", transcript="FALLBACK", events=[_event("FALLBACK")])
        kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="q")

    candidates_by_text = {candidate.raw_text: candidate for candidate in spy.seen}
    assert candidates_by_text["EXPLICIT"].ranking_key == "opaque-dialog-1"
    assert candidates_by_text["FALLBACK"].ranking_key == candidates_by_text["FALLBACK"].source_event_id
    assert candidates_by_text["FALLBACK"].checkpoint_key == "fallback"


@pytest.mark.parametrize(("field", "value"), [
    ("retrieval_ranking_key", ""),
    ("retrieval_ranking_key", 42),
    ("retrieval_checkpoint_id", " "),
    ("retrieval_checkpoint_id", 42),
])
def test_present_malformed_product_ranking_payload_keys_fail_closed(tmp_path, field, value):
    spy = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / f"{field}-{type(value).__name__}.sqlite3"), retrieval_ranker=spy) as kernel:
        kernel.commit_scene(campaign_id="c", scene_id="scene", in_world_time="1", transcript="EVENT", events=[_event("EVENT", payload={field: value})])
        with pytest.raises(ValueError, match=field):
            kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="q")

    assert spy.calls == 0


def test_kernel_trims_selected_trace_to_packed_evidence(tmp_path):
    spy = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / "trim.sqlite3"), retrieval_ranker=spy) as kernel:
        kernel.commit_scene(campaign_id="c", scene_id="one", in_world_time="1", transcript="FIRST", events=[_event("FIRST")])
        kernel.commit_scene(campaign_id="c", scene_id="two", in_world_time="2", transcript="SECOND", events=[_event("SECOND")])
        pack = kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="q", max_chars=5)

    packed_ids = [item["source_event_id"] for item in pack.evidence]
    assert len(packed_ids) == 1
    assert [item["source_event_id"] for item in pack.policy_trace["retrieval_ranking"]["selected"]] == packed_ids


def test_public_checkpoint_rolls_up_across_speakers_but_security_policy_boundaries_remain_separate(tmp_path):
    encoder = _CountingEncoder()
    with RpgMemoryKernel(db_path=str(tmp_path / "policy.sqlite3"), retrieval_ranker=SixViewRanker(encoder)) as kernel:
        checkpoint = {"retrieval_checkpoint_id": "  session-1  "}
        kernel.commit_scene(campaign_id="c", scene_id="alice", in_world_time="1", transcript="PUBLIC ALICE", events=[_event("PUBLIC ALICE", actor="alice", payload=checkpoint)])
        kernel.commit_scene(campaign_id="c", scene_id="bob", in_world_time="2", transcript="PUBLIC BOB", events=[_event("PUBLIC BOB", actor="bob", payload=checkpoint)])
        kernel.commit_scene(campaign_id="c", scene_id="witness", in_world_time="3", transcript="WITNESSED", events=[_event("WITNESSED", visibility="witnessed_only", actor="guide", witness_set=["hero"], payload=checkpoint)])
        kernel.commit_scene(campaign_id="c", scene_id="private", in_world_time="4", transcript="PRIVATE", events=[_event("PRIVATE", visibility="character_private", owner="hero", actor="guide", payload=checkpoint)])
        kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="session")

    checkpoint_texts = [text for batch in encoder.passage_batches for text in batch]
    assert any("summary=PUBLIC ALICE" in text and "summary=PUBLIC BOB" in text for text in checkpoint_texts)
    assert not any("summary=PUBLIC ALICE" in text and "summary=WITNESSED" in text for text in checkpoint_texts)
    assert not any("summary=PUBLIC ALICE" in text and "summary=PRIVATE" in text for text in checkpoint_texts)


@pytest.mark.parametrize("column", ["payload_json", "related_entities_json"])
def test_product_ranking_rejects_tampered_json_metadata_before_ranking(tmp_path, column):
    spy = _SpyRanker()
    with RpgMemoryKernel(db_path=str(tmp_path / "tampered.sqlite3"), retrieval_ranker=spy) as kernel:
        kernel.commit_scene(campaign_id="c", scene_id="scene", in_world_time="1", transcript="ALLOWED", events=[_event("ALLOWED")])
        kernel._conn().execute(f"UPDATE scene_event SET {column}='not-json'")
        kernel._conn().commit()
        with pytest.raises(ValueError, match=f"malformed product {column}"):
            kernel.build_memory_pack(campaign_id="c", actor_id="hero", actor_type="npc", query="q")

    assert spy.calls == 0


@pytest.mark.parametrize("encoder, message", [
    (type("MissingPassages", (), {"identity": "bad", "encode_query": lambda self, query: [1.0]})(), "encode_passages"),
    (type("BadPassageCount", (), {"identity": "bad", "encode_passages": lambda self, texts: [], "encode_query": lambda self, query: [1.0]})(), "count mismatch"),
    (type("BadPassageDimensions", (), {"identity": "bad", "encode_passages": lambda self, texts: [[1.0], [1.0, 2.0]], "encode_query": lambda self, query: [1.0]})(), "dimensions differ"),
    (type("NonFiniteQuery", (), {"identity": "bad", "encode_passages": lambda self, texts: [[1.0] for _ in texts], "encode_query": lambda self, query: [float("nan")]})(), "non-finite"),
])
def test_malformed_encoder_output_fails_loudly(encoder, message):
    candidates = [_candidate("event")]
    if message == "dimensions differ":
        candidates.append(_candidate("other", ("canonical", "private")))
    with pytest.raises((AttributeError, ValueError), match=message):
        SixViewRanker(encoder).rank(query="q", candidates=candidates)


def test_structured_observation_has_semantics_not_acl_policy_tokens():
    observation = structured_observation(
        summary="arrived", event_type="arrival", actor_id="hero", target_id="guide",
        related_entities=["companion"], related_quests=["quest"], related_locations=["cave"],
        in_world_time="dawn", location_id="cave",
    )

    assert "policy_" not in observation
    assert "public_world" not in observation
    assert "actor_id=hero" in observation
    assert "related_quests=[\"quest\"]" in observation
