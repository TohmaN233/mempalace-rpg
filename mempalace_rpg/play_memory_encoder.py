"""Opt-in, local-only Qwen3 embedding backend for portable gameplay memory.

Preprocessing is extracted from the frozen Foundation qwen768-mechanical encoder:
query instruction only, left padding, last valid token, prefix768 then L2. This
is a new profile: dynamic authorized summaries, bounded in-memory caches and an
explicit model tree replace that experiment's fixed corpus and GPU identity.
Importing this module never imports torch/transformers or starts a model.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import logging
from pathlib import Path
import re
import threading
import time

DIMENSIONS = 768
MAX_TOKENS = 32768
INSTRUCTION = "Given a story-memory query, retrieve relevant historical passages that answer the query"
logger = logging.getLogger(__name__)


def digest(text):
    return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()


def canonical_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False))


@dataclass(frozen=True)
class QwenRankerConfig:
    model_directory: str | Path
    model_tree_sha256: str
    revision: str
    device: str = "cuda"
    dtype: str = "bfloat16"
    cache_capacity: int = 1024
    batch_size: int = 8
    cuda_cache_policy: str = "retain"
    vector_cache_path: str | Path | None = None

    def __post_init__(self):
        if not isinstance(self.model_directory, (str, Path)) or not Path(self.model_directory).is_absolute():
            raise ValueError("Qwen model_directory must be an absolute local path")
        if not isinstance(self.model_tree_sha256, str) or not re.fullmatch("[a-f0-9]{64}", self.model_tree_sha256):
            raise ValueError("Qwen model_tree_sha256 must be a lowercase SHA-256")
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("Qwen revision must be explicit nonblank text")
        if not isinstance(self.device, str) or not re.fullmatch(r"cpu|cuda(?::[0-9]+)?", self.device):
            raise ValueError("Qwen device must be explicit cpu, cuda or cuda:N")
        if self.dtype not in ("float32", "bfloat16"):
            raise ValueError("Qwen dtype must be float32 or bfloat16")
        if type(self.cache_capacity) is not int or not 1 <= self.cache_capacity <= 24576:
            raise ValueError("Qwen cache_capacity must be between 1 and 24576")
        if type(self.batch_size) is not int or not 1 <= self.batch_size <= 16:
            raise ValueError("Qwen batch_size must be between 1 and 16")
        if self.cuda_cache_policy not in ("retain", "release-after-encode"):
            raise ValueError("Qwen CUDA cache policy must be explicit retain or release-after-encode")
        if self.vector_cache_path is not None:
            if not isinstance(self.vector_cache_path, (str, Path)):
                raise ValueError("Qwen vector_cache_path must be an absolute local file path")
            vector_cache_path = Path(self.vector_cache_path)
            if not vector_cache_path.is_absolute():
                raise ValueError("Qwen vector_cache_path must be an absolute local file path")
            if not vector_cache_path.parent.is_dir():
                raise ValueError("Qwen vector_cache_path parent directory must already exist")
            if vector_cache_path.is_symlink():
                raise ValueError("Qwen vector_cache_path must not be a symbolic link")
            if vector_cache_path.exists() and not vector_cache_path.is_file():
                raise ValueError("Qwen vector_cache_path must be a regular file")


def model_tree_hash(directory: str | Path) -> str:
    """Relative-path/size/content SHA tree encoding used by runtime_factory.

    Links are rejected, including linked ancestors, so a manifest cannot authorize
    a different model tree through a path alias. Hashes are streamed, not loaded.
    """
    path = Path(directory)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Qwen model directory must be absolute and must not contain links")
    root = path.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Qwen model directory must be a directory")
    files = []
    for entry in sorted(root.rglob("*")):
        if entry.is_symlink():
            raise ValueError("Qwen model tree must not contain links")
        if entry.is_file():
            hasher = hashlib.sha256()
            with entry.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(chunk)
            files.append({"relative_path": entry.relative_to(root).as_posix(),
                          "bytes": entry.stat().st_size, "sha256": hasher.hexdigest()})
    if not files:
        raise ValueError("Qwen model tree must not be empty")
    return canonical_hash(files)


def render_input(text: str, *, query: bool) -> str:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Qwen embedding input must be nonblank text")
    text.encode("utf-8", errors="strict")
    return f"Instruct: {INSTRUCTION}\nQuery:{text}" if query else text


def token_ids(tokenizer, text: str, *, query: bool):
    rendered = render_input(text, query=query)
    ids = tokenizer(rendered, truncation=False, add_special_tokens=True)["input_ids"]
    if not isinstance(ids, list) or not 0 < len(ids) <= MAX_TOKENS or any(type(i) is not int or i < 0 for i in ids):
        raise ValueError("Qwen token IDs invalid or exceed 32768 tokens; clipping is forbidden")
    return ids


def normalized_prefix(hidden, torch):
    """Slice before L2; accepts the explicit torch runtime for offline fakes."""
    if hidden.ndim != 2 or hidden.shape[1] < DIMENSIONS:
        raise ValueError("Invalid Qwen full embedding shape")
    prefix = hidden[:, :DIMENSIONS].float()
    if not torch.isfinite(prefix).all() or (prefix.norm(dim=1) == 0).any():
        raise ValueError("Nonfinite or zero Qwen embedding")
    return torch.nn.functional.normalize(prefix, p=2, dim=1)


def _dependency_versions():
    # Metadata lookup neither imports nor initializes either optional library.
    return {name: importlib.metadata.version(name) for name in ("torch", "transformers", "numpy")}


class LocalQwenEncoder:
    """Explicit local backend. Factory checks bytes; first encode starts inference.

    Optional torch/transformers must already be installed by the operator. All
    failures propagate. There is no package install, model download, alternate
    model, automatic device change, truncation or lexical-only recovery.
    """
    def __init__(self, config: QwenRankerConfig):
        self.config = config
        self.model_directory = Path(config.model_directory).resolve(strict=True)
        actual = model_tree_hash(config.model_directory)
        if actual != config.model_tree_sha256:
            raise ValueError(f"Qwen model tree mismatch: expected {config.model_tree_sha256}, actual {actual}")
        for name in ("config.json", "tokenizer.json", "tokenizer_config.json"):
            if not (self.model_directory / name).is_file():
                raise ValueError(f"Qwen local model missing {name}")
        if not list(self.model_directory.glob("*.safetensors")):
            raise ValueError("Qwen local model missing safetensors weights")
        self.profile = {"schema": "play-memory-qwen768-encoder-v1", "repository": "Qwen/Qwen3-Embedding-0.6B",
                        "modelTreeSha256": actual, "revision": config.revision,
                        "device": config.device, "dtype": config.dtype, "attention": "sdpa",
                        "pooling": "last-valid-token", "normalization": "L2-after-prefix768",
                        "dimensions": DIMENSIONS, "maxTokens": MAX_TOKENS, "truncation": False,
                        "queryInstruction": INSTRUCTION, "documentInstruction": None,
                        "batchSize": config.batch_size, "batchTokenBudget": 8192, "dependencies": _dependency_versions(),
                        "cudaCachePolicy": config.cuda_cache_policy,
                        "implementationSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        self.identity = "qwen3-embedding-0.6b:summary768:" + canonical_hash(self.profile)
        self._model = self._tokenizer = self._torch = None
        self._closed = False
        self._lock = threading.RLock()

    def _load(self):
        if self._closed:
            raise RuntimeError("Qwen encoder is closed")
        if self._model is not None:
            return
        # Recheck immediately before loading to reject edits after factory creation.
        if model_tree_hash(self.model_directory) != self.config.model_tree_sha256:
            raise ValueError("Qwen model tree changed before model loading")
        import torch
        from transformers import AutoModel, AutoTokenizer

        if self.config.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError("Selected Qwen CUDA device is unavailable; fallback is forbidden")
        tokenizer = AutoTokenizer.from_pretrained(str(self.model_directory), local_files_only=True,
                                                  padding_side="left", trust_remote_code=False)
        model = AutoModel.from_pretrained(str(self.model_directory), local_files_only=True,
                                         trust_remote_code=False, dtype=getattr(torch, self.config.dtype),
                                         attn_implementation="sdpa").to(self.config.device).eval()
        if model.config.max_position_embeddings != MAX_TOKENS or model.config.model_type != "qwen3":
            raise ValueError("Selected local Qwen model architecture/context differs from profile")
        self._tokenizer, self._model, self._torch = tokenizer, model, torch
        logger.info("Qwen model loaded profile=%s device=%s", self.identity, self.config.device)

    def _encode(self, texts, *, query):
        with self._lock:
            started = time.perf_counter()
            self._load()
            try:
                result = self._encode_loaded(texts, query=query)
            except Exception as encode_error:
                try:
                    self._release_cuda_cache(started)
                except Exception as cleanup_error:
                    # Both failures remain visible; the original inference failure
                    # stays primary rather than being replaced by allocator cleanup.
                    raise encode_error from cleanup_error
                raise
            self._release_cuda_cache(started)
            return result

    def _release_cuda_cache(self, started):
        # Successful returns drop the helper's batch/hidden views. On an exception,
        # traceback-owned tensors remain live. Only unused blocks are released.
        if self.config.device.startswith("cuda") and self.config.cuda_cache_policy == "release-after-encode":
            before = self._cuda_memory()
            self._torch.cuda.synchronize(self.config.device)
            self._torch.cuda.empty_cache()
            logger.info("Qwen allocator release device=%s before=%s after=%s totalSeconds=%.6f",
                        self.config.device, before, self._cuda_memory(), time.perf_counter() - started)

    def _cuda_memory(self):
        if not self.config.device.startswith("cuda"):
            return None
        return {"allocatedBytes": self._torch.cuda.memory_allocated(self.config.device),
                "reservedBytes": self._torch.cuda.memory_reserved(self.config.device)}

    def _encode_loaded(self, texts, *, query):
        tokenize_started = time.perf_counter()
        rows = [token_ids(self._tokenizer, text, query=query) for text in texts]
        logger.info("Qwen tokenize role=%s rows=%d tokens=%d maxTokens=%d seconds=%.6f",
                    "query" if query else "passage", len(rows), sum(map(len, rows)), max(map(len, rows), default=0),
                    time.perf_counter() - tokenize_started)
        output = [None] * len(rows)
        batches, pending, maximum = [], [], 0
        for index in sorted(range(len(rows)), key=lambda i: len(rows[i])):
            length = len(rows[index])
            if pending and (len(pending) >= self.config.batch_size or max(maximum, length) * (len(pending) + 1) > 8192):
                batches.append(pending)
                pending, maximum = [], 0
            pending.append(index)
            maximum = max(maximum, length)
        if pending:
            batches.append(pending)
        for indices in batches:
            batch_started = time.perf_counter()
            logger.info("Qwen batch start role=%s rows=%d maxTokens=%d memory=%s",
                        "query" if query else "passage", len(indices), max(len(rows[i]) for i in indices),
                        self._cuda_memory())
            batch = self._tokenizer.pad({"input_ids": [rows[i] for i in indices]},
                                        padding=True, return_tensors="pt").to(self.config.device)
            if not batch["attention_mask"][:, -1].all():
                raise ValueError("Qwen last-token pooling requires left padding")
            with self._torch.inference_mode():
                if self.config.device.startswith("cuda"):
                    self._torch.cuda.synchronize(self.config.device)
                forward_started = time.perf_counter()
                hidden = self._model(**batch, use_cache=False).last_hidden_state[:, -1]
                if self.config.device.startswith("cuda"):
                    self._torch.cuda.synchronize(self.config.device)
                forward_seconds = time.perf_counter() - forward_started
                normalize_started = time.perf_counter()
                values = normalized_prefix(hidden, self._torch).cpu().tolist()
            logger.info("Qwen batch completed role=%s rows=%d forwardSeconds=%.6f normalizeCopySeconds=%.6f totalSeconds=%.6f memory=%s",
                        "query" if query else "passage", len(indices), forward_seconds,
                        time.perf_counter() - normalize_started, time.perf_counter() - batch_started, self._cuda_memory())
            for index, vector in zip(indices, values):
                output[index] = vector
        return output

    def encode_query(self, query):
        return self._encode([query], query=True)[0]

    def encode_passages(self, texts):
        return self._encode(texts, query=False)

    def close(self):
        with self._lock:
            self._closed = True
            self._model = self._tokenizer = None
            if self._torch is not None and self.config.device.startswith("cuda"):
                self._torch.cuda.empty_cache()
            self._torch = None
