"""CLI owners must pass the host cache path and close it even on startup failure."""
from io import StringIO
from unittest.mock import Mock

import pytest

from mempalace_rpg import read_server, story_read_server


@pytest.mark.parametrize("module", [read_server, story_read_server])
@pytest.mark.parametrize("startup_failure", [False, True])
def test_cli_owns_cache_lifecycle(monkeypatch, tmp_path, module, startup_failure):
    encoder = Mock()
    factory = Mock(return_value=encoder)
    constructor = Mock(side_effect=RuntimeError("store startup failed")) if startup_failure else Mock()
    monkeypatch.setattr(module, "create_encoder", factory)
    monkeypatch.setattr(module, "EvidenceStore" if module is read_server else "StoryStore", constructor)
    monkeypatch.setattr(module.sys, "stdin", StringIO())
    monkeypatch.setattr(module.sys, "stdout", StringIO())
    monkeypatch.setattr(module.sys, "stderr", StringIO())
    cache = str(tmp_path / "vectors.sqlite")
    args = ["--model-dir", "model", "--model-tree-sha256", "a" * 64, "--passage-cache-path", cache]
    if module is read_server:
        args += ["--projection", "projection.json"]
    assert module.main(args) == (1 if startup_failure else 0)
    factory.assert_called_once_with(model_dir="model", expected_tree_sha256="a" * 64, passage_cache_path=cache)
    encoder.close.assert_called_once_with()
    if not startup_failure:
        constructor.return_value.close.assert_called_once_with()


@pytest.mark.parametrize("module", [read_server, story_read_server])
def test_cache_close_failure_is_not_success(monkeypatch, tmp_path, module):
    encoder = Mock()
    encoder.close.side_effect = RuntimeError("cache close failed")
    monkeypatch.setattr(module, "create_encoder", Mock(return_value=encoder))
    monkeypatch.setattr(module, "EvidenceStore" if module is read_server else "StoryStore", Mock())
    monkeypatch.setattr(module.sys, "stdin", StringIO())
    monkeypatch.setattr(module.sys, "stdout", StringIO())
    monkeypatch.setattr(module.sys, "stderr", StringIO())
    args = ["--model-dir", "model", "--model-tree-sha256", "a" * 64,
            "--passage-cache-path", str(tmp_path / "vectors.sqlite")]
    if module is read_server:
        args += ["--projection", "projection.json"]
    with pytest.raises(RuntimeError, match="cache close failed"):
        module.main(args)
