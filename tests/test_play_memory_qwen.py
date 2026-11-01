"""Offline Qwen engineering contracts; every inference object is a local fake."""
from contextlib import nullcontext
from dataclasses import replace
import ast
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from mempalace_rpg import play_memory_encoder as backend
from mempalace_rpg.play_memory_encoder import (
    DIMENSIONS, INSTRUCTION, LocalQwenEncoder, QwenRankerConfig, model_tree_hash,
    normalized_prefix, render_input, token_ids,
)
from mempalace_rpg.play_memory_qwen import create_qwen_ranker, vector_cache_key, weave_owners
from mempalace_rpg.retrieval import AuthorizedRetrievalCandidate


def unit(index=0):
    vector = [0.0] * DIMENSIONS
    vector[index] = 1.0
    return vector


class FakeEncoder:
    identity = "offline-fake-summary768-v1"

    def __init__(self):
        self.queries, self.passages = [], []

    def encode_query(self, text):
        self.queries.append(text)
        return unit()

    def encode_passages(self, texts):
        self.passages.append(list(texts))
        return [unit(1 if text == "dense-low" else 0) for text in texts]


def config(tmp_path, **kwargs):
    return QwenRankerConfig(tmp_path, "a" * 64, "pinned-revision", **kwargs)


def candidate(identifier, summary="dense-high", original="", **kwargs):
    values = dict(source_event_id=identifier, source_scene_id="scene", raw_text=original,
                  observation=summary, checkpoint_key="checkpoint", policy_tuple=("authorized",),
                  chronological_order_key=(1, identifier), ranking_key=identifier)
    return AuthorizedRetrievalCandidate(**{**values, **kwargs})


def test_only_authorized_summary_embeddings_and_original_lexical_weave(tmp_path):
    encoder = FakeEncoder()
    ranker = create_qwen_ranker(config(tmp_path), encoder=encoder)
    rows = [candidate("semantic", "dense-high"), candidate("lexical", "dense-low", "needle needle"),
            candidate("same-summary", "dense-high")]
    result = ranker.rank(query="needle", candidates=rows)
    assert result.ranked_event_ids[0] == "lexical"
    assert set(result.ranked_event_ids) == {row.source_event_id for row in rows}
    assert result.trace["lexicalOrder"] == ["lexical"]
    assert result.trace["semanticOrder"] == ["same-summary", "semantic", "lexical"]
    assert encoder.passages == [["dense-high", "dense-low"]]
    assert encoder.queries == ["needle"]
    assert result.scores == {key: 1 / (i + 1) for i, key in enumerate(result.ranked_event_ids)}
    assert result.trace["navigationOnly"] is True
    # A new scope must be ranked only from its supplied allowed set, even when
    # cache contents were originally created by a wider scope.
    narrowed = ranker.rank(query="needle", candidates=[rows[0]])
    assert narrowed.ranked_event_ids == ["semantic"]
    assert set(narrowed.trace["denseScores"]) == {"semantic"}
    assert encoder.passages == [["dense-high", "dense-low"]]


def test_exact_cache_text_roles_profile_changes_and_source_identity(tmp_path):
    encoder = FakeEncoder()
    cfg = config(tmp_path)
    ranker = create_qwen_ranker(cfg, encoder=encoder)
    first = candidate("one", "needle")
    ranker.rank(query="needle", candidates=[first])
    repeated = ranker.rank(query="needle", candidates=[replace(first, source_event_id="two", raw_text="new original")])
    assert repeated.ranked_event_ids == ["two"]
    assert repeated.trace["cacheMisses"] == 0
    assert encoder.queries == ["needle"] and encoder.passages == [["needle"]]
    ranker.rank(query=" needle", candidates=[replace(first, observation="needle ")])
    assert encoder.queries[-1] == " needle" and encoder.passages[-1] == ["needle "]
    assert vector_cache_key(ranker.identity, "needle", query=False) != vector_cache_key(ranker.identity, "needle", query=True)
    assert vector_cache_key("other-profile", "needle", query=False) != vector_cache_key(ranker.identity, "needle", query=False)
    assert create_qwen_ranker(replace(cfg, revision="another-revision"), encoder=FakeEncoder()).identity != ranker.identity
    assert create_qwen_ranker(replace(cfg, model_tree_sha256="b" * 64), encoder=FakeEncoder()).identity != ranker.identity
    encoder.identity = "changed"
    with pytest.raises(ValueError, match="identity changed"):
        ranker.rank(query="needle", candidates=[first])


def test_prepare_warms_only_authorized_passages_for_later_search(tmp_path):
    encoder = FakeEncoder()
    ranker = create_qwen_ranker(config(tmp_path), encoder=encoder)
    rows = [candidate("first", "shared scene summary"),
            candidate("second", "shared scene summary")]

    warmed = ranker.prepare(candidates=rows)

    assert warmed["profile"] == ranker.identity
    assert warmed["candidateCount"] == 2
    assert warmed["cacheHits"] == 1
    assert warmed["cacheMisses"] == 1
    assert encoder.passages == [["shared scene summary"]]
    assert encoder.queries == []

    result = ranker.rank(query="a later player query", candidates=rows)
    assert set(result.ranked_event_ids) == {"first", "second"}
    assert encoder.passages == [["shared scene summary"]]
    assert encoder.queries == ["a later player query"]
    # The later search encodes its query, but reuses both authorized passage
    # vectors prepared above.
    assert result.trace["cacheHits"] == 2
    assert result.trace["cacheMisses"] == 1


def test_bounded_cache_does_not_lose_large_request(tmp_path):
    encoder = FakeEncoder()
    ranker = create_qwen_ranker(config(tmp_path, cache_capacity=1), encoder=encoder)
    rows = [candidate(str(i), f"summary-{i}") for i in range(7)]
    result = ranker.rank(query="query", candidates=rows)
    assert len(result.ranked_event_ids) == 7
    assert result.trace["cacheSize"] == 1
    ranker.rank(query="other query", candidates=[rows[0]])
    assert len(encoder.passages) == 2


@pytest.mark.parametrize("bad", [[0.0] * 768, unit()[:-1], [float("nan")] * 768,
                                 [float("inf")] * 768, ["bad"] * 768, [[1.0]] * 768])
def test_invalid_encoder_vectors_fail_without_lexical_recovery(tmp_path, bad):
    encoder = FakeEncoder()
    encoder.encode_passages = lambda texts: [bad]
    ranker = create_qwen_ranker(config(tmp_path), encoder=encoder)
    with pytest.raises(ValueError):
        ranker.rank(query="needle", candidates=[candidate("authorized", original="needle")])
    assert not ranker._cache


def test_failures_propagate_no_partial_batch_cache(tmp_path):
    encoder = FakeEncoder()
    encoder.encode_passages = lambda texts: [unit(), [0.0] * 768]
    ranker = create_qwen_ranker(config(tmp_path), encoder=encoder)
    with pytest.raises(ValueError, match="unit vectors"):
        ranker.rank(query="needle", candidates=[candidate("one", "one"), candidate("two", "two")])
    assert not ranker._cache
    encoder.encode_passages = lambda texts: []
    with pytest.raises(ValueError, match="count mismatch"):
        ranker.rank(query="needle", candidates=[candidate("one")])
    encoder.encode_passages = lambda texts: (_ for _ in ()).throw(RuntimeError("device failure"))
    with pytest.raises(RuntimeError, match="device failure"):
        ranker.rank(query="needle", candidates=[candidate("one", original="needle")])


def test_input_rejections_empty_and_close_do_not_encode(tmp_path):
    encoder = FakeEncoder()
    ranker = create_qwen_ranker(config(tmp_path), encoder=encoder)
    assert ranker.rank(query="query", candidates=[]).ranked_event_ids == []
    for rows in ([candidate("same"), candidate("same")], [candidate("", "summary")], [candidate("one", " ")]):
        with pytest.raises(ValueError):
            ranker.rank(query="query", candidates=rows)
    with pytest.raises(ValueError):
        ranker.rank(query=" ", candidates=[])
    ranker.close()
    with pytest.raises(RuntimeError, match="closed"):
        ranker.rank(query="query", candidates=[])
    assert not encoder.queries and not encoder.passages


@pytest.mark.parametrize("overrides", [{"model_directory": "relative"}, {"model_tree_sha256": "invalid"},
    {"revision": ""}, {"device": "auto"}, {"dtype": "auto"}, {"cache_capacity": 0},
    {"cache_capacity": True}, {"batch_size": 0}])
def test_configuration_is_explicit(tmp_path, overrides):
    with pytest.raises(ValueError):
        replace(config(tmp_path), **overrides)


def test_production_factory_checks_local_bytes_before_optional_import(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "_dependency_versions", lambda: {"torch": "fake", "transformers": "fake", "numpy": np.__version__})
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors"):
        (tmp_path / name).write_text("offline test bytes", encoding="utf-8")
    cfg = replace(config(tmp_path), model_tree_sha256=model_tree_hash(tmp_path))
    before = {name: sys.modules.get(name) for name in ("torch", "transformers")}
    ranker = create_qwen_ranker(cfg)
    assert ranker.encoder._model is None
    assert {name: sys.modules.get(name) for name in before} == before
    ranker.close()
    (tmp_path / "config.json").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="tree mismatch"):
        create_qwen_ranker(cfg)
    with pytest.raises(FileNotFoundError):
        create_qwen_ranker(replace(cfg, model_directory=tmp_path / "absent"))


def test_exact_query_rendering_no_document_instruction_no_clipping():
    calls = []
    def tokenizer(text, **kwargs):
        calls.append((text, kwargs))
        return {"input_ids": [1, 2]}
    assert token_ids(tokenizer, "  query\n", query=True) == [1, 2]
    assert calls[-1] == (f"Instruct: {INSTRUCTION}\nQuery:  query\n", {"truncation": False, "add_special_tokens": True})
    assert render_input("  document\n", query=False) == "  document\n"
    for ids in ([], [1] * 32769, [True]):
        with pytest.raises(ValueError, match="clipping is forbidden"):
            token_ids(lambda *args, **kwargs: {"input_ids": ids}, "query", query=True)


class Tensor(np.ndarray):
    def float(self):
        return self.astype(np.float32)

    def norm(self, dim):
        return np.linalg.norm(self, axis=dim)

    def cpu(self):
        return self


def tensor(value):
    return np.asarray(value).view(Tensor)


FakeTorch = SimpleNamespace(isfinite=np.isfinite, inference_mode=nullcontext,
    nn=SimpleNamespace(functional=SimpleNamespace(normalize=lambda array, p, dim: array / np.linalg.norm(array, axis=dim, keepdims=True))))


def test_prefix_before_l2_and_nonfinite_rejection():
    hidden = tensor([[3.0, 4.0] + [0.0] * 766 + [10000.0]])
    result = normalized_prefix(hidden, FakeTorch)
    assert result.shape == (1, 768) and result.dtype == np.float32
    assert result[0, :2].tolist() == pytest.approx([0.6, 0.8])
    for bad in (tensor([[0.0] * 768]), tensor([[float("nan")] * 768]), tensor([[1.0] * 767])):
        with pytest.raises(ValueError):
            normalized_prefix(bad, FakeTorch)


def test_backend_last_token_pooling_restore_order_and_left_padding(tmp_path):
    class Batch(dict):
        def to(self, device):
            assert device == "cpu"
            return self
    class Tokenizer:
        def __call__(self, text, **kwargs):
            assert kwargs == {"truncation": False, "add_special_tokens": True}
            return {"input_ids": [1] * len(text)}
        def pad(self, value, **kwargs):
            assert kwargs == {"padding": True, "return_tensors": "pt"}
            rows = value["input_ids"]
            return Batch(attention_mask=tensor([[0, 1]] * len(rows)), lengths=[len(row) for row in rows])
    def model(**batch):
        assert batch["use_cache"] is False
        return SimpleNamespace(last_hidden_state=tensor([[[10000] + [0] * 768,
            [length, 1] + [0] * 767] for length in batch["lengths"]]))
    encoder = object.__new__(LocalQwenEncoder)
    encoder.config = config(tmp_path, device="cpu", batch_size=1)
    encoder._closed = False
    import threading
    encoder._lock = threading.RLock()
    encoder._model, encoder._tokenizer, encoder._torch = model, Tokenizer(), FakeTorch
    values = encoder.encode_passages(["longer", "a"])
    assert [value[:2] for value in values] == pytest.approx(np.asarray([[6, 1], [1, 1]]) / np.sqrt([[37], [2]]))
    assert all(value[2:] == [0] * 766 for value in values)
    encoder._tokenizer.pad = lambda value, **kwargs: Batch(attention_mask=tensor([[1, 0]]), lengths=[1])
    with pytest.raises(ValueError, match="left padding"):
        encoder.encode_passages(["a"])


def test_explicit_cuda_cache_release_after_cpu_result_and_on_failure():
    import threading
    calls = []
    encoder = object.__new__(LocalQwenEncoder)
    encoder.config = SimpleNamespace(device="cuda", cuda_cache_policy="release-after-encode")
    encoder._lock = threading.RLock()
    encoder._load = lambda: None
    encoder._torch = SimpleNamespace(cuda=SimpleNamespace(
        synchronize=lambda device: calls.append(("sync", device)),
        empty_cache=lambda: calls.append("release-unused"),
        memory_allocated=lambda device: 100,
        memory_reserved=lambda device: 200,
    ))
    def completed(texts, *, query):
        calls.append("cpu-result-ready")
        return [[1.0]]
    encoder._encode_loaded = completed
    assert encoder.encode_query("query") == [1.0]
    assert calls == ["cpu-result-ready", ("sync", "cuda"), "release-unused"]
    calls.clear()
    def failed(texts, *, query):
        raise RuntimeError("original forward failure")
    encoder._encode_loaded = failed
    with pytest.raises(RuntimeError, match="original forward failure"):
        encoder.encode_query("query")
    assert calls == [("sync", "cuda"), "release-unused"]
    cleanup_error = RuntimeError("CUDA cleanup failure")
    def cleanup_failed():
        raise cleanup_error
    encoder._torch.cuda.empty_cache = cleanup_failed
    with pytest.raises(RuntimeError, match="original forward failure") as failure:
        encoder.encode_query("query")
    assert failure.value.__cause__ is cleanup_error
    calls.clear()
    encoder.config.cuda_cache_policy = "retain"
    encoder._encode_loaded = completed
    assert encoder.encode_query("query") == [1.0]
    assert calls == ["cpu-result-ready"]


def test_backend_loader_is_local_only_explicit_device_and_no_fallback(tmp_path, monkeypatch):
    for name in ("config.json", "tokenizer.json", "tokenizer_config.json", "model.safetensors"):
        (tmp_path / name).write_text("fake local bytes", encoding="utf-8")
    monkeypatch.setattr(backend, "_dependency_versions", lambda: {})
    cfg = replace(config(tmp_path), model_tree_sha256=model_tree_hash(tmp_path))
    calls = []
    model = SimpleNamespace(config=SimpleNamespace(max_position_embeddings=32768, model_type="qwen3"))
    def to(device):
        calls.append(("device", device))
        return model
    model.to, model.eval = to, lambda: model
    def loader(kind, value):
        def load(path, **kwargs):
            calls.append((kind, path, kwargs))
            return value
        return SimpleNamespace(from_pretrained=load)
    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None), bfloat16="explicit-bfloat16")
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoModel=loader("model", model), AutoTokenizer=loader("tokenizer", object())))
    encoder = LocalQwenEncoder(cfg)
    with pytest.raises(RuntimeError, match="fallback is forbidden"):
        encoder._load()
    assert not calls
    torch.cuda.is_available = lambda: True
    encoder._load()
    assert calls == [
        ("tokenizer", str(tmp_path), {"local_files_only": True, "padding_side": "left", "trust_remote_code": False}),
        ("model", str(tmp_path), {"local_files_only": True, "trust_remote_code": False,
                                "dtype": "explicit-bfloat16", "attn_implementation": "sdpa"}),
        ("device", "cuda"),
    ]
    encoder.close()
    changed = LocalQwenEncoder(cfg)
    (tmp_path / "config.json").write_text("mutated before load", encoding="utf-8")
    with pytest.raises(ValueError, match="changed before model loading"):
        changed._load()


def test_weave_duplicate_skipping_and_authorized_remainder():
    assert weave_owners(["b", "a"], ["a", "b", "c", "d"]) == ["b", "a", "c", "d"]
    with pytest.raises(ValueError):
        weave_owners(["b", "b"], ["a"])


def test_dense_math_against_actual_frozen_ranking_when_checkout_available(tmp_path):
    # Execute only the frozen pure NumPy helper. No corpus, gold, encoder,
    # provider or experiment entrypoint is loaded; unavailable checkout skips.
    frozen = Path(__file__).resolve().parents[2] / "rp-foundation-lazy-memory-20261013/experiments/qwen-semantic-search-service/ranking.py"
    if not frozen.is_file():
        pytest.skip("Adjacent frozen pure ranking source is unavailable")
    namespace = {}
    # Compile source directly so verification cannot write bytecode into the
    # frozen experiment directory.
    exec(compile(frozen.read_text(encoding="utf-8"), str(frozen), "exec"), namespace)
    module = SimpleNamespace(**namespace)
    encoder = FakeEncoder()
    rows = [candidate("a", "dense-high"), candidate("b", "dense-low"), candidate("c", "other")]
    result = create_qwen_ranker(config(tmp_path), encoder=encoder).rank(query="no lexical hit", candidates=rows)
    texts = {module.sha256_text(row.observation): row.observation for row in rows}
    keys = list(texts)
    vectors = np.asarray([unit(1 if texts[key] == "dense-low" else 0) for key in keys], dtype=np.float32)
    frozen_rows = module.rank_summaries(np.asarray(unit(), dtype=np.float32), keys, texts, vectors)
    expected_keys = [row["key"] for row in frozen_rows]
    assert [module.sha256_text(next(row.observation for row in rows if row.source_event_id == i)) for i in result.ranked_event_ids] == expected_keys
    assert [result.trace["denseScores"][i] for i in result.ranked_event_ids] == [row["score"] for row in frozen_rows]


def test_preprocessing_math_against_actual_frozen_encoder_without_imports():
    frozen = Path(__file__).resolve().parents[2] / "rp-foundation-lazy-memory-20261013/experiments/qwen768-mechanical/encoder.py"
    if not frozen.is_file():
        pytest.skip("Adjacent frozen pure transformation source is unavailable")
    source = ast.parse(frozen.read_text(encoding="utf-8"))
    function = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "normalized_prefix")
    namespace = {"torch": FakeTorch, "DIMENSIONS": DIMENSIONS}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(frozen), "exec"), namespace)
    hidden = tensor(np.random.default_rng(42).normal(size=(5, 1024)))
    expected = namespace["normalized_prefix"](hidden)
    actual = normalized_prefix(hidden, FakeTorch)
    np.testing.assert_array_equal(actual, expected)
