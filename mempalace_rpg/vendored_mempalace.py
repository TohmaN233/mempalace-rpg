"""Verify the official MemPalace source snapshot shipped with this package."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


MANIFEST_NAME = "_upstream_source.json"
UPSTREAM_LICENSE_NAME = "LICENSE.upstream"
MANIFEST_SCHEMA = "mempalace-rpg-vendored-upstream-v1"
UPSTREAM_COMMIT = "87e6f38377b4bee0666374b05df6e14ffd154245"
UPSTREAM_TREE = "639b2a849816fd4853072920405822824464e9c6"
UPSTREAM_VERSION = "3.8.0"
_VENDOR_METADATA = frozenset({MANIFEST_NAME, UPSTREAM_LICENSE_NAME})


def vendored_mempalace_root() -> Path:
    """Return the directory containing the bundled top-level ``mempalace`` package."""

    return Path(__file__).resolve().parents[1]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _package_tree_receipt(package: Path) -> dict[str, Any]:
    files = []
    for path in sorted(item for item in package.rglob("*") if item.is_file()):
        relative = path.relative_to(package).as_posix()
        if relative in _VENDOR_METADATA or "__pycache__" in path.parts:
            continue
        payload = path.read_bytes()
        files.append(
            {
                "relative_path": relative,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {
        "file_count": len(files),
        "bytes": sum(row["bytes"] for row in files),
        "package_tree_sha256": hashlib.sha256(_canonical_bytes(files)).hexdigest(),
    }


def vendored_source_state(root: Path | None = None) -> dict[str, Any]:
    """Return a git-state-shaped receipt for the bundled immutable source tree.

    Historical benchmark schemas call these fields ``git_*``.  For a bundled
    snapshot they bind the upstream commit/tree declared by the checked-in
    manifest, while ``git_dirty`` reports byte drift in the vendored package.
    """

    source_root = (root or vendored_mempalace_root()).resolve()
    package = source_root / "mempalace"
    manifest_path = package / MANIFEST_NAME
    license_path = package / UPSTREAM_LICENSE_NAME
    if not package.is_dir() or not manifest_path.is_file() or not license_path.is_file():
        raise ValueError("vendored MemPalace source is incomplete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    required = {
        "schema",
        "repository",
        "version",
        "commit",
        "tree",
        "package_tree_sha256",
        "file_count",
        "bytes",
        "license_file",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("commit") != UPSTREAM_COMMIT
        or manifest.get("tree") != UPSTREAM_TREE
        or manifest.get("version") != UPSTREAM_VERSION
        or manifest.get("license_file") != UPSTREAM_LICENSE_NAME
    ):
        raise ValueError("vendored MemPalace manifest is invalid")
    actual = _package_tree_receipt(package)
    expected = {key: manifest[key] for key in ("file_count", "bytes", "package_tree_sha256")}
    drift = actual != expected
    drift_payload = b"" if not drift else _canonical_bytes({"expected": expected, "actual": actual})
    return {
        "path": str(source_root),
        "git_head": manifest["commit"],
        "git_tree": manifest["tree"],
        "git_dirty": drift,
        "worktree_status_sha256": hashlib.sha256(drift_payload).hexdigest(),
        "worktree_diff_sha256": hashlib.sha256(drift_payload).hexdigest(),
        "worktree_diff_bytes": len(drift_payload),
    }


__all__ = [
    "UPSTREAM_COMMIT",
    "UPSTREAM_TREE",
    "UPSTREAM_VERSION",
    "vendored_mempalace_root",
    "vendored_source_state",
]
