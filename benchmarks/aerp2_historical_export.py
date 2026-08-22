"""One-pass, byte-pinned AERP-2 historical four-arm rerun exporter.

The source is recovered from Git blobs, never from a potentially line-ending
converted worktree.  It deliberately records the two historical environment
snapshot checks that must be waived for a byte-pinned R0 rerun on a new host;
the raw-BM25 and six-view rank streams must then match the published artifact
exactly or the export fails.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from benchmarks.aerp2_six_view_replay import _canonical_sha256, _load_json, run_replay


HISTORICAL_COMMIT = "429e11ced3529a3409509026a62fb3bb5ec43c77"
HISTORICAL_FILES = {
    "benchmarks/locomo_bge_encoder.py": "ffbf915209c83135b34a6f1e4071b746006c671597ba6b8b6cf4842902f1ad51",
    "benchmarks/locomo_dense_story_candidate.py": "7378564770f898ee9ad93ac3ec49322cb6f7e85754100692cfab49bc05bfd3e9",
    "benchmarks/locomo_story_candidate.py": "2709c3aa6c8a0220f409320f013651370eaea759ddfde01f7b041428f173a97a",
    "benchmarks/locomo_story_experiment.py": "2b7355a0fb66d0e8c3c0f9c5e282fd6f794ad805bf5597fc4d5a75f0c9e218bf",
    "benchmarks/locomo_story_protocol.py": "f0e5b3ec3045b83149d36347435b65c1ff7ee92e9597e74cdf4c0cd636394341",
    "benchmarks/locomo_story_artifact_pins.py": "e066864df8550b1fb170172b8deaa5ff50d61f46c1a04625e33e01421fa87592",
    "benchmarks/locomo_story_selection_freeze_pin.py": "b57d6002a8b11d6bf196dfb1685496d096c888da3ec8f209e19ad351ca7f5e51",
    "benchmarks/evaluate_locomo_story_gate.py": "7a7ebcc7cd6cb6ff35f5713627ef27fc48471c307efbd73e7f255735daa3376a",
    "benchmark-runs/generate_locomo_story_dense_v2_dev_weight_grid.py": "50b04e1cc30116dcd9252403ea13507fbec743a2f4d2b21766ef461b36b7f71d",
}
_MODULES = tuple(path.stem for path in map(Path, HISTORICAL_FILES) if path.suffix == ".py" and path.parent.name == "benchmarks")


def _git_blob(repo: Path, relative: str) -> bytes:
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo.as_posix()}", "-C", str(repo), "show", f"{HISTORICAL_COMMIT}:{relative}"],
        check=True, capture_output=True,
    ).stdout


def _extract_historical_source(repo: Path, target: Path) -> dict[str, str]:
    observed: dict[str, str] = {}
    for relative, expected in HISTORICAL_FILES.items():
        raw = _git_blob(repo, relative)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected:
            raise RuntimeError(f"historical source blob digest mismatch: {relative}")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        observed[relative] = digest
    return observed


def _arm_digest(report: dict[str, Any], name: str) -> str:
    return _canonical_sha256([
        (question["opaque_conversation_id"], question["opaque_question_id"], question["methods"][name]["ranking"])
        for question in report["questions"]
    ])


def _r0_compare(published: dict[str, Any], rerun: dict[str, Any]) -> None:
    expected = {(row["opaque_conversation_id"], row["opaque_question_id"]): row for row in published["questions"]}
    actual = {(row["opaque_conversation_id"], row["opaque_question_id"]): row for row in rerun["questions"]}
    if set(expected) != set(actual):
        raise RuntimeError("R0 question identities differ from published historical artifact")
    for key in sorted(expected):
        for method in ("raw_dialog_bm25", "six_view_story_dense_rrf_v2"):
            if expected[key]["methods"][method]["ranking"] != actual[key]["methods"][method]["ranking"]:
                raise RuntimeError(f"R0 ranking mismatch for {method}:{key[0]}:{key[1]}")


def _derive_denominators(published_replay: dict[str, Any], rerun: dict[str, Any]) -> dict[str, int]:
    expected = published_replay["denominators"]
    questions = rerun["questions"]
    observed = {
        "questions": len(questions),
        "hard_questions": sum(question["scorer"]["category"] in {1, 2} for question in questions),
        "top_k": 10,
    }
    if observed != expected:
        raise RuntimeError(f"historical rerun denominators differ from published replay: {observed}")
    return observed


def _build_export_report(
    published_replay: dict[str, Any], published: dict[str, Any], rerun: dict[str, Any], source_digests: dict[str, str],
) -> dict[str, Any]:
    _r0_compare(published, rerun)
    denominators = _derive_denominators(published_replay, rerun)
    arms = {
        name: {
            "status": "complete",
            "ranking_stream_sha256": _arm_digest(rerun, name),
            "official_exact": rerun["aggregate_metrics"][name]["official_exact"],
        }
        for name in ("six_view_story_dense_rrf_v2", "raw_dialog_bm25", "raw_dense", "raw_bm25_plus_raw_dense")
    }
    return {
        "schema": "aerp2-six-view-historical-export",
        "version": 1,
        "status": "complete",
        "input_freeze": published_replay["input_freeze"],
        "r0": {"raw_bm25_and_six_view_rankings_exact": True, "published_replay_status": published_replay["status"]},
        "environment_waivers": {
            "bypassed_historical_checks": [
                {
                    "name": "selection_runtime_fingerprint",
                    "scope": "_selection_runtime_payload",
                    "reason": "the frozen value identifies the original host runtime rather than ranking inputs",
                },
                {
                    "name": "selection_provenance_validation",
                    "scope": "entire _validate_selection_provenance gate",
                    "reason": "the gate compares original-host filesystem snapshots, including device, inode, and modified time",
                },
            ],
            "independent_hard_gates": [
                "published artifact and replay manifest SHA-256",
                "historical Git source blob SHA-256",
                "historical dataset, model, configuration, selection-freeze, and scorer digests",
                "exact R0 equality for raw BM25 and six-view ranking streams",
            ],
        },
        "historical_source": {"commit": HISTORICAL_COMMIT, "files": source_digests},
        "denominators": denominators,
        "arms": arms,
        "claim_boundary": {"annotation_free_productization": "not evaluated", "improvement_claim": "not made by this export"},
    }


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _patch_four_arm_freeze(experiment: Any) -> None:
    original_methods = experiment.METHODS
    original_candidate = experiment._candidate_method_rankings
    original_freeze = experiment._freeze_question_rankings

    def candidate_methods(lexical_result: Any, dense_result: Any) -> dict[str, Any]:
        methods = dict(original_candidate(lexical_result, dense_result))
        raw_dense = tuple({"opaque_dialog_id": hit.opaque_dialog_id, "score": hit.score} for hit in dense_result.method_hits(experiment.dense_candidate.RAW_DENSE))
        methods["raw_dense"] = raw_dense
        scores: dict[str, float] = {}
        for weight, ranking in ((2.0, methods["raw_dialog_bm25"]), (1.0, raw_dense)):
            for rank, row in enumerate(ranking, start=1):
                dialog_id = row["opaque_dialog_id"]
                scores[dialog_id] = scores.get(dialog_id, 0.0) + weight / (60.0 + rank)
        methods["raw_bm25_plus_raw_dense"] = tuple(
            {"opaque_dialog_id": dialog_id, "score": score}
            for dialog_id, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:50]
        )
        return methods

    def freeze_after_selection(prepared: Any, track: Any, index_stage: Any) -> Any:
        experiment.METHODS = original_methods + ("raw_dense", "raw_bm25_plus_raw_dense")
        return original_freeze(prepared, track, index_stage)

    experiment._candidate_method_rankings = candidate_methods
    experiment._freeze_question_rankings = freeze_after_selection


def run_export(
    *, artifact_path: Path | str, dataset_path: Path | str, model_dir: Path | str,
    source_repo: Path | str, selection_freeze_path: Path | str, manifest_path: Path | str,
) -> dict[str, Any]:
    """Rerun the exact historical benchmark and emit the four requested arms."""
    published_replay = run_replay(artifact_path, manifest_path)
    published, _ = _load_json(Path(artifact_path), "published artifact")
    source_repo = Path(source_repo).resolve()
    with tempfile.TemporaryDirectory(prefix="aerp2-historical-") as directory:
        root = Path(directory)
        source_digests = _extract_historical_source(source_repo, root)
        saved = {name: sys.modules.pop(name, None) for name in _MODULES}
        sys.path.insert(0, str(root / "benchmarks"))
        try:
            experiment = importlib.import_module("locomo_story_experiment")
            selection = json.loads(Path(selection_freeze_path).read_text(encoding="utf-8"))
            # The dataset/model/code byte pins remain enforced.  These two checks
            # compare host-specific runtime and filesystem metadata, so R0 below
            # is the stronger cross-host reproducibility proof.
            experiment._selection_runtime_payload = lambda: selection["selection_freeze_payload"]["runtime"]
            experiment._validate_selection_provenance = lambda *args, **kwargs: None
            _patch_four_arm_freeze(experiment)
            rerun = experiment.build_experiment_report(
                str(Path(dataset_path).resolve()), str(Path(model_dir).resolve()),
                selection_freeze_path=str(Path(selection_freeze_path).resolve()),
            )
        finally:
            sys.path.pop(0)
            for name in _MODULES:
                sys.modules.pop(name, None)
            for name, module in saved.items():
                if module is not None:
                    sys.modules[name] = module
    return _build_export_report(published_replay, published, rerun, source_digests)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--source-repo", required=True)
    parser.add_argument("--selection-freeze", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = run_export(
        artifact_path=args.artifact,
        dataset_path=args.dataset,
        model_dir=args.model_dir,
        source_repo=args.source_repo,
        selection_freeze_path=args.selection_freeze,
        manifest_path=args.manifest,
    )
    _atomic_json(Path(args.output), report)
    print(json.dumps({"status": report["status"], "denominators": report["denominators"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
