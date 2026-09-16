from __future__ import annotations

import importlib
from pathlib import Path

from mempalace_rpg.vendored_mempalace import (
    UPSTREAM_COMMIT,
    UPSTREAM_TREE,
    UPSTREAM_VERSION,
    vendored_mempalace_root,
    vendored_source_state,
)


def test_vendored_mempalace_is_the_pinned_clean_upstream_source() -> None:
    root = vendored_mempalace_root()
    state = vendored_source_state(root)
    package = importlib.import_module("mempalace")

    assert Path(package.__file__).resolve() == root / "mempalace" / "__init__.py"
    assert package.__version__ == UPSTREAM_VERSION == "3.8.0"
    assert state["git_head"] == UPSTREAM_COMMIT
    assert state["git_tree"] == UPSTREAM_TREE
    assert state["git_dirty"] is False
    assert state["worktree_diff_bytes"] == 0


def test_vendored_mempalace_exposes_the_upstream_command_modules() -> None:
    cli = importlib.import_module("mempalace.cli")
    mcp_proxy = importlib.import_module("mempalace.mcp_proxy")

    assert callable(cli.main)
    assert callable(mcp_proxy.main)
