from __future__ import annotations

import json
import os
import hashlib
import shutil
import stat
import subprocess
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from benchmarks import aerp7_convomem_confirmation as custody
from benchmarks import aerp7_convomem_one_shot as one_shot


SECRET = b"aerp7-synthetic-secret-key-must-be-long"
CONFIG = custody.SelectionConfig(seed=7, persona_quota=1, per_persona_group_quota=1, context_rank_indices=(0, 2))


@pytest.fixture(autouse=True)
def sqlite_temp_file_placement() -> object:
    """The synthetic suite models an isolated process launched on staging."""
    original = custody._verify_sqlite_temp_environment

    def supported(staging_root: Path) -> dict[str, str]:
        return {"os_rule": "test-injected-v1", "resolved_temp_path": str(staging_root)}

    custody._verify_sqlite_temp_environment = supported
    try:
        yield original
    finally:
        custody._verify_sqlite_temp_environment = original


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _staging(tmp_path: Path) -> Path:
    root = tmp_path / "staging"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _custody_bundle(candidate: Path) -> Path:
    return candidate.with_name(candidate.name + "-custody")


def _publish(*, canonical: Path, premix: Path, output: Path, staging: Path, config: custody.SelectionConfig = CONFIG) -> dict[str, object]:
    return custody.build_prelabel_bundle(canonical_root=canonical, premix_root=premix, candidate_output_dir=output, custody_output_dir=_custody_bundle(output), staging_root=staging, secret=SECRET, selection=config)


def _roots(tmp_path: Path, *, mixed: bool = False) -> tuple[Path, Path]:
    canonical, premix = tmp_path / "labels", tmp_path / "premix"
    all_cases = []
    for persona in ("p-a", "p-b"):
        for group in ("group-1", "group-2"):
            conversation = f"{persona}-{group}-conversation"
            suffix = group[-1]
            evidence = {"personId": persona, "question": f"q-{persona}-{suffix}", "answer": f"SECRET-{persona}-{group}", "category": f"category-{group}", "conversations": [{"id": conversation}], "message_evidences": [{"speaker": "secret-speaker", "text": "secret-evidence"}]}
            _write(canonical / "core_benchmark" / "evidence_questions" / group / "tier-1" / f"{persona}.json", {"evidence_items": [evidence]})
            for size in (1, 8, 13):
                embedded = {key: evidence[key] for key in ("personId", "question", "answer", "category", "conversations")}
                all_cases.append({"contextSize": size, "evidenceItems": [embedded], "conversations": [{"id": conversation, "messages": [{"speaker": f"speaker-{persona}", "text": f"candidate-{persona}-{suffix}"}]}]})
    if mixed:
        all_cases.append({"contextSize": 3, "evidenceItems": [{"personId": "p-a", "question": "q-p-a-1", "answer": "SECRET-p-a-group-1", "category": "category-group-1", "conversations": [{"id": "p-a-group-1-conversation"}]}, {"personId": "p-b", "question": "q-p-b-1", "answer": "SECRET-p-b-group-1", "category": "category-group-1", "conversations": [{"id": "p-b-group-1-conversation"}]}], "conversations": [{"id": "p-a-group-1-conversation", "messages": [{"speaker": "speaker-p-a", "text": "candidate-p-a-1"}]}, {"id": "p-b-group-1-conversation", "messages": [{"speaker": "speaker-p-b", "text": "candidate-p-b-1"}]}]})
    _write(premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json", all_cases)
    _write(canonical / "core_benchmark" / "evidence_questions" / "legacy_benchmarks" / "ignored.json", {"evidence_items": []})
    return canonical, premix


def _build(tmp_path: Path, *, name: str = "bundle", mixed: bool = False, config: custody.SelectionConfig = CONFIG) -> Path:
    canonical, premix = _roots(tmp_path, mixed=mixed)
    output = tmp_path / name
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path), config=config)
    return output


def _rewrite_candidate_ready(bundle: Path) -> None:
    ready_path = bundle / "READY.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    raw = (bundle / "projection.json").read_bytes()
    ready["projection"]["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    ready["projection"]["canonical_sha256"] = custody.canonical_sha256(json.loads(raw.decode("utf-8")))
    ready_path.unlink()
    custody._write(ready_path, ready)


def _rewrite_custody_ready(candidate: Path) -> None:
    bundle = _custody_bundle(candidate); ready_path = bundle / "READY.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    raw = (bundle / "sealed-custody.json").read_bytes()
    ready["custody"]["raw_sha256"] = hashlib.sha256(raw).hexdigest()
    ready["custody"]["canonical_sha256"] = custody.canonical_sha256(json.loads(raw.decode("utf-8")))
    ready_path.unlink(); custody._write(ready_path, ready)


def test_multicontext_projection_is_normalized_safe_and_group_selected(tmp_path: Path) -> None:
    output = _build(tmp_path, mixed=True)
    projection = custody.load_candidate_projection(output)
    expanded = custody.materialize_candidate_items(projection)
    sealed = json.loads((_custody_bundle(output) / "sealed-custody.json").read_text(encoding="utf-8"))
    assert len(projection["items"]) == 4  # one persona × two groups × one item × two contexts
    assert len({item["item_id"] for item in projection["items"]}) == 4
    assert len({item["corpus_id"] for item in projection["items"]}) == 4
    assert all(row["candidates"][0]["text"].startswith("candidate-") for row in expanded)
    serialized = json.dumps(projection)
    assert all(token not in serialized for token in ("SECRET-", '"message_evidences"', "category-group-", '"contextSize"'))
    corpus = projection["corpora"][0]
    assert set(corpus) == {"corpus_id", "declared_context_size", "actual_conversation_count", "actual_message_count", "candidates"}
    assert set(corpus["candidates"][0]) == {"message_id", "opaque_conversation_id", "conversation_order", "message_order", "corpus_order", "speaker", "text"}
    assert sealed["mapping_status"]["scoring_permitted"] is False
    assert sealed["selection_receipt"]["exclusion_counts"]["multi_persona_cases"] == 1
    assert all("canonical_item_id" in item for item in sealed["items"])


def test_v3_projection_preserves_nested_order_and_rejects_count_order_tamper(tmp_path: Path) -> None:
    projection = custody.load_candidate_projection(_build(tmp_path))
    corpus = projection["corpora"][0]
    corpus["actual_conversation_count"] = 2
    corpus["actual_message_count"] = 4
    corpus["candidates"] = [
        {
            "message_id": hashlib.sha256(f"message-{index}".encode()).hexdigest(),
            "opaque_conversation_id": hashlib.sha256(f"conversation-{conversation}".encode()).hexdigest(),
            "conversation_order": conversation,
            "message_order": message,
            "corpus_order": index,
            "speaker": f"speaker-{conversation}",
            "text": f"message-{conversation}-{message}",
        }
        for index, (conversation, message) in enumerate(((0, 0), (0, 1), (1, 0), (1, 1)))
    ]
    validated = custody.validate_candidate_projection(projection)
    assert [
        (row["conversation_order"], row["message_order"], row["corpus_order"])
        for row in validated["corpora"][0]["candidates"]
    ] == [(0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3)]

    def tampered() -> dict[str, object]:
        return json.loads(json.dumps(projection))

    bad = tampered(); bad["corpora"][0]["declared_context_size"] = 2.5
    with pytest.raises(custody.CustodyError, match="projection_declared_context_size_invalid"):
        custody.validate_candidate_projection(bad)
    bad = tampered(); bad["corpora"][0]["actual_message_count"] = 3
    with pytest.raises(custody.CustodyError, match="projection_candidates_count_invalid"):
        custody.validate_candidate_projection(bad)
    bad = tampered(); bad["corpora"][0]["candidates"][2]["corpus_order"] = 7
    with pytest.raises(custody.CustodyError, match="projection_corpus_order_invalid"):
        custody.validate_candidate_projection(bad)
    bad = tampered(); bad["corpora"][0]["candidates"][3]["message_order"] = 3
    with pytest.raises(custody.CustodyError, match="projection_message_order_invalid"):
        custody.validate_candidate_projection(bad)
    bad = tampered(); bad["corpora"][0]["candidates"][2]["conversation_order"] = 0
    with pytest.raises(custody.CustodyError, match="projection_conversation_order_invalid|projection_message_order_invalid"):
        custody.validate_candidate_projection(bad)


def test_privileged_scoring_adapter_is_minimal_and_evidence_conversation_bound(tmp_path: Path) -> None:
    output = _build(tmp_path)
    projection = custody.load_candidate_projection(output)
    scoring = custody.load_custody_for_scoring(
        output, _custody_bundle(output), binding_secret=SECRET,
    )
    assert set(scoring) == {"schema", "projection_sha256", "items"}
    assert scoring["projection_sha256"] == custody.canonical_sha256(projection)
    assert all(
        set(row) == {"item_id", "directory_group", "evidence_conversation_ids", "evidence_spans"}
        for row in scoring["items"]
    )
    serialized = json.dumps(scoring)
    assert all(token not in serialized for token in ("SECRET-", "source_locator", "canonical_item_id", "persona_source_id", '"answer"', '"tier"'))

    sealed = custody.load_sealed_custody(output, _custody_bundle(output), binding_secret=SECRET)
    sealed["items"][0]["evidence_conversation_ids"] = [hashlib.sha256(b"unknown-conversation").hexdigest()]
    with pytest.raises(custody.CustodyError, match="custody_evidence_conversation_invalid"):
        custody._validate_custody(sealed, projection, binding_secret=SECRET)


def test_privileged_scoring_adapter_keeps_abstention_source_binding_private(tmp_path: Path) -> None:
    canonical, premix = tmp_path / "labels", tmp_path / "premix"
    conversation = "p-a-abstention-source"
    evidence = {
        "personId": "p-a", "question": "q-abstain", "answer": "no answer",
        "category": "abstention", "conversations": [{"id": conversation}],
        "message_evidences": [],
    }
    _write(
        canonical / "core_benchmark" / "evidence_questions" / "abstention_evidence" / "tier-1" / "p-a.json",
        {"evidence_items": [evidence]},
    )
    embedded = {key: evidence[key] for key in ("personId", "question", "answer", "category", "conversations")}
    _write(
        premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json",
        [{"contextSize": 1, "evidenceItems": [embedded], "conversations": [{"id": conversation, "messages": [{"speaker": "user", "text": "unrelated"}]}]}],
    )
    output = tmp_path / "bundle"
    _publish(
        canonical=canonical, premix=premix, output=output,
        staging=_staging(tmp_path),
        config=custody.SelectionConfig(seed=7, persona_quota=1, per_persona_group_quota=1, context_rank_indices=(0,)),
    )
    sealed = custody.load_sealed_custody(output, _custody_bundle(output), binding_secret=SECRET)
    assert sealed["items"][0]["evidence_conversation_ids"]
    scoring = custody.load_custody_for_scoring(output, _custody_bundle(output), binding_secret=SECRET)
    assert scoring["items"] == [{
        "item_id": sealed["items"][0]["item_id"],
        "directory_group": "abstention_evidence",
        "evidence_conversation_ids": [],
        "evidence_spans": [],
    }]


def test_context_rank_semantics_and_revision_bound_hmac_ids(tmp_path: Path) -> None:
    output = _build(tmp_path / "one", config=custody.SelectionConfig(7, 1, 1, (2,)))
    one = custody.load_candidate_projection(output)
    assert one["selection_receipt"]["context_rank_semantics"] == "zero_based_unique_sorted_values"
    second_root = tmp_path / "two"; output2 = _build(second_root)
    labels = next(path for path in (second_root / "labels" / "core_benchmark" / "evidence_questions").rglob("*.json") if "legacy_benchmarks" not in path.parts)
    labels.write_text(labels.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    canonical, premix = second_root / "labels", second_root / "premix"
    changed = second_root / "changed"
    _publish(canonical=canonical, premix=premix, output=changed, staging=_staging(second_root))
    assert custody.load_candidate_projection(output2)["items"][0]["item_id"] != custody.load_candidate_projection(changed)["items"][0]["item_id"]
    published = (changed / "projection.json").read_bytes() + (_custody_bundle(changed) / "sealed-custody.json").read_bytes()
    assert SECRET not in published


def test_strict_crosswalk_and_schema_fail_closed(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    cases = json.loads((premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json").read_text(encoding="utf-8"))
    cases[0]["evidenceItems"][0]["conversations"] = [{"id": "missing"}]
    _write(premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json", cases)
    with pytest.raises(custody.CrosswalkError, match="premix_embedded_conversation_missing"):
        _publish(canonical=canonical, premix=premix, output=tmp_path / "bundle", staging=_staging(tmp_path))
    projection = {"schema": custody.SCHEMA, "dataset": {}, "selection_receipt": {}, "corpora": [], "items": [], "answer": "leak"}
    with pytest.raises(custody.CustodyError, match="projection_schema_invalid"):
        custody.validate_candidate_projection(projection)


def test_custody_tamper_is_rejected_by_custodian_validator(tmp_path: Path) -> None:
    output = _build(tmp_path)
    projection = custody.load_candidate_projection(output)
    sealed_path = _custody_bundle(output) / "sealed-custody.json"
    sealed = json.loads(sealed_path.read_text(encoding="utf-8")); original_messages = sealed["items"][0]["messages"]; sealed["items"][0]["messages"] = []
    with pytest.raises(custody.CustodyError, match="custody_message_binding_invalid"):
        custody._validate_custody(sealed, projection, binding_secret=SECRET)
    sealed["items"][0]["messages"] = [original_messages[0]] * 2
    with pytest.raises(custody.CustodyError, match="custody_message_binding_invalid"):
        custody._validate_custody(sealed, projection, binding_secret=SECRET)
    sealed_path.write_text(json.dumps(sealed), encoding="utf-8")
    with pytest.raises(custody.CustodyError, match="custody_raw_digest_invalid"):
        custody.load_sealed_custody(output, _custody_bundle(output), binding_secret=SECRET)


def test_ready_last_publication_race_is_nonclobber_and_not_loadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    output = tmp_path / "bundle"; original = custody.os.mkdir

    def race(path: Path, mode: int = 0o777) -> None:
        if Path(path) == output:
            original(path); raise FileExistsError("competitor won")
        original(path)

    monkeypatch.setattr(custody.os, "mkdir", race)
    with pytest.raises(custody.CustodyError, match="output_already_exists"):
        _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path))
    assert not (output / "READY.json").exists()
    with pytest.raises(custody.CustodyError, match="candidate_bundle_not_ready"):
        custody.load_candidate_projection(output)


def test_hardlink_staging_is_rejected_without_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path); output = tmp_path / "bundle"; real_write = custody._write

    def linked(path: Path, value: object) -> None:
        if path.name == "sealed-custody.json": os.link(path.with_name("foreign.json"), path)
        else: real_write(path, value)

    monkeypatch.setattr(custody, "_write", linked)
    with pytest.raises((custody.CustodyError, FileNotFoundError)):
        _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path))
    assert not output.exists()
    monkeypatch.undo()
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path))
    assert custody.load_candidate_projection(output)["items"]


def test_hardlinked_official_source_is_accepted_and_staged_as_a_private_copy(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    canonical_root = custody._subroot(canonical, "evidence_questions")
    source = next(path for path in canonical_root.rglob("*.json") if "legacy_benchmarks" not in path.parts)
    lfs_object = tmp_path / "lfs-object"
    os.link(source, lfs_object)
    assert os.lstat(source).st_nlink == 2

    assert source in custody._files(canonical_root)
    inventory = custody._source_size_inventory(canonical_root, custody._subroot(premix, "pre_mixed_testcases"))
    assert inventory["source_file_count"] == 5

    staged = tmp_path / "stage.json"
    identity, _digest = custody._copy_source_once(source, staged)
    assert identity == (os.lstat(source).st_dev, os.lstat(source).st_ino)
    assert os.lstat(staged).st_nlink == 1

    output = tmp_path / "bundle"
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path))
    assert custody.load_candidate_projection(output)["items"]


def test_official_source_snapshot_rejects_symlink_and_reparse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source.json"
    source.write_text("[]", encoding="utf-8")
    alias = tmp_path / "source-alias.json"
    try:
        os.symlink(source, alias)
    except OSError:
        pass
    else:
        with pytest.raises(custody.CustodyError, match="official_source_invalid"):
            custody._source_snapshot(alias, "official_source_invalid")
    monkeypatch.setattr(custody, "_is_reparse", lambda _metadata: True)
    with pytest.raises(custody.CustodyError, match="official_source_invalid"):
        custody._source_snapshot(source, "official_source_invalid")


@pytest.mark.parametrize("drift", ("identity", "size", "nlink"))
def test_official_source_snapshot_rejects_identity_size_and_link_count_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str) -> None:
    source = tmp_path / "source.json"
    source.write_text("[]", encoding="utf-8")
    lfs_object = tmp_path / "lfs-object"
    os.link(source, lfs_object)
    original_read = custody.os.read
    mutated = False

    if drift == "identity":
        original_lstat = custody.os.lstat
        calls = 0

        def replace_identity(path: str | Path, *args: object, **kwargs: object) -> os.stat_result | SimpleNamespace:
            nonlocal calls
            metadata = original_lstat(path, *args, **kwargs)
            if Path(path) == source:
                calls += 1
                if calls == 2:
                    return SimpleNamespace(st_mode=metadata.st_mode, st_nlink=metadata.st_nlink, st_dev=metadata.st_dev, st_ino=metadata.st_ino + 1, st_size=metadata.st_size)
            return metadata

        monkeypatch.setattr(custody.os, "lstat", replace_identity)

    def mutate(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        if not mutated:
            mutated = True
            if drift == "size":
                with source.open("ab") as stream:
                    stream.write(b"\n")
            elif drift == "nlink":
                lfs_object.unlink()
        return original_read(descriptor, size)

    if drift != "identity":
        monkeypatch.setattr(custody.os, "read", mutate)
    with pytest.raises(custody.CustodyError, match="official_source_drift"):
        custody._source_snapshot(source, "official_source_drift")


def test_pinned_source_drift_and_conflicting_repeated_conversation_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    original_verify = custody._verify_streaming_sources

    def drift(*args: object, **kwargs: object) -> object:
        cases_path.write_text(cases_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(custody, "_verify_streaming_sources", drift)
    with pytest.raises(custody.CustodyError, match="premix_source_drift"):
        _publish(canonical=canonical, premix=premix, output=tmp_path / "drift", staging=_staging(tmp_path))
    monkeypatch.undo()
    canonical, premix = _roots(tmp_path / "conflict")
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8")); cases[3]["conversations"][0]["id"] = cases[0]["conversations"][0]["id"]
    cases[3]["evidenceItems"][0]["conversations"][0]["id"] = cases[0]["conversations"][0]["id"]
    _write(cases_path, cases)
    with pytest.raises(custody.CustodyError, match="premix_conversation_content_conflict"):
        _publish(canonical=canonical, premix=premix, output=tmp_path / "conflict-out", staging=_staging(tmp_path / "conflict"))


def test_cleanup_identity_drift_never_deletes_replaced_target(tmp_path: Path) -> None:
    target = tmp_path / "target"; target.mkdir(); identity = custody._directory_identity(target, "test")
    moved = tmp_path / "moved"; os.rename(target, moved); target.mkdir()
    foreign = target / "foreign.json"; foreign.write_text("foreign", encoding="utf-8")
    assert custody._cleanup_owned_target(target, identity, {}) is False
    assert foreign.exists()


def test_symlink_output_is_rejected_separately(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path); destination = tmp_path / "destination"; destination.mkdir(); alias = tmp_path / "alias"
    try: os.symlink(destination, alias, target_is_directory=True)
    except OSError: pytest.skip("symlink creation unavailable")
    with pytest.raises(custody.CustodyError, match="output_already_exists"):
        _publish(canonical=canonical, premix=premix, output=alias, staging=_staging(tmp_path))


def test_streaming_sqlite_index_uses_staged_incremental_json(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    try:
        connection = __import__("sqlite3").connect(index.database)
        assert connection.execute("SELECT count(*) FROM canonical_items").fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM premix_cases").fetchone()[0] >= 12
        assert len(index.source_receipt) == 5
        connection.close()
        ledger = custody._quarantine_ledger(index.database, index.revision)
        assert custody._validate_quarantine_ledger(ledger, index.revision)["ledger_sha256"] == ledger["ledger_sha256"]
        ledger["reasons"]["unmatched_premix_keys"]["count"] = -1
        with pytest.raises(custody.CustodyError, match="quarantine_ledger_invalid"):
            custody._validate_quarantine_ledger(ledger, index.revision)
    finally:
        directory = index.directory; index.close()
    assert not directory.exists()


def test_official_builder_never_uses_legacy_in_memory_loaders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)

    def legacy(*args: object, **kwargs: object) -> object:
        raise AssertionError("legacy in-memory path was called")

    for name in ("_pinned_sources", "_canonical", "_premix", "_crosswalk", "_select", "_verify_pinned_sources"):
        monkeypatch.setattr(custody, name, legacy)
    output = tmp_path / "stream-only"
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path))
    assert custody.load_candidate_projection(output)["items"]


def test_quarantine_and_context_missing_are_global_and_selected_rows_are_eligible(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    # Create one unmatched premix key and remove the largest context for one
    # canonical key.  The builder must supplement from the other persona,
    # never select either globally ineligible item.
    unknown = dict(cases[0]["evidenceItems"][0]); unknown["personId"] = "unknown"
    cases.append({"contextSize": 1, "evidenceItems": [unknown], "conversations": cases[0]["conversations"]})
    cases[:] = [case for case in cases if not (case["contextSize"] == 13 and case["evidenceItems"][0]["personId"] == "p-a" and case["evidenceItems"][0]["question"].endswith("-1"))]
    _write(cases_path, cases)
    index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    try:
        values = custody._indexed_context_values(index.database)
        ledger = custody._quarantine_ledger(index.database, index.revision, (values[0], values[-1]))
        assert ledger["reasons"]["unmatched_premix_keys"]["count"] == 1
        assert ledger["reasons"]["missing_requested_context_sizes"]["count"] == 1
        assert custody._validate_quarantine_ledger(ledger, index.revision)["ledger_sha256"] == ledger["ledger_sha256"]
    finally:
        index.close()
    # With one selected persona the other complete persona is an explicit
    # eligible supplement; its selected contexts remain complete.
    output = tmp_path / "bundle"
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path))
    receipt = custody.load_candidate_projection(output)["selection_receipt"]
    assert receipt["exclusion_counts"]["missing_requested_context_sizes"] == 1
    assert receipt["exclusion_counts"]["unmatched_premix_keys"] == 1


def test_many_cases_streaming_proxy_keeps_no_staged_json_after_indexing(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    # A synthetic many-case file is a bounded-memory proxy: the index must be
    # able to finish with no staged JSON copies retained.
    cases *= 100
    _write(cases_path, cases)
    index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    try:
        assert not list(index.directory.glob("*.json"))
        assert index.database.exists()
        connection = __import__("sqlite3").connect(index.database)
        try:
            assert connection.execute("SELECT count(*) FROM premix_cases").fetchone()[0] == len(cases)
        finally:
            connection.close()
    finally:
        index.close()


def test_duplicate_outer_row_normalization_preserves_raw_locator_ordinals_and_is_deterministic(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    first = cases[0]["conversations"][0]
    second = json.loads(json.dumps(first))
    second["id"] = "second-conversation"
    second["messages"][0]["text"] = "second-candidate"
    # Retained rows are A@0 and B@2; the remaining three rows are exact
    # duplicates distributed over both ids.
    cases[0]["conversations"] = [
        first,
        json.loads(json.dumps(first)),
        second,
        json.loads(json.dumps(second)),
        json.loads(json.dumps(first)),
    ]
    _write(cases_path, cases)

    nonstream_cases, _excluded = custody._premix(
        [{"locator": "cases.json", "raw": cases_path.read_bytes()}], SECRET, "a" * 64,
    )
    nonstream_messages = nonstream_cases[0]["messages"]
    assert [
        (row["conversation_order"], row["source_locator"]["conversation_id"], row["source_locator"]["conversation_ordinal"])
        for row in nonstream_messages
    ] == [(0, first["id"], 0), (1, second["id"], 2)]

    def indexed_normalization(staging_root: Path) -> tuple[dict[str, object], list[dict[str, object]]]:
        index = custody._streaming_index(
            custody._subroot(canonical, "evidence_questions"),
            custody._subroot(premix, "pre_mixed_testcases"), staging_root,
        )
        try:
            connection = __import__("sqlite3").connect(index.database)
            try:
                assert connection.execute("SELECT count(*) FROM premix_exact_duplicate_normalizations").fetchone()[0] == 3
                locator = custody._bytes({"path": "cases.json", "case_ordinal": 0}).decode()
                messages = json.loads(connection.execute("SELECT messages FROM premix_cases WHERE locator=?", (locator,)).fetchone()[0])
            finally:
                connection.close()
            return index.staging_receipt["premix_exact_duplicate_normalization"], messages
        finally:
            index.close()

    normalization, streaming_messages = indexed_normalization(_staging(tmp_path / "first"))
    normalization_again, _ = indexed_normalization(_staging(tmp_path / "second"))
    assert normalization == normalization_again
    assert [
        (row["conversation_ordinal"], row["source_locator"]["conversation_id"], row["source_locator"]["conversation_ordinal"])
        for row in streaming_messages
    ] == [(0, first["id"], 0), (1, second["id"], 2)]
    assert normalization["schema"] == custody.PREMIX_EXACT_DUPLICATE_NORMALIZATION["schema"]
    assert normalization["duplicate_case_count"] == 1
    assert normalization["duplicate_extra_row_count"] == 3

    output = tmp_path / "duplicate"
    _publish(
        canonical=canonical, premix=premix, output=output,
        staging=_staging(tmp_path / "published"), config=custody.SelectionConfig.census_v1(),
    )
    projection = custody.load_candidate_projection(output)
    sealed = custody.load_sealed_custody(output, _custody_bundle(output), binding_secret=SECRET)
    selected = next(item for item in sealed["items"] if item["source_locator"]["case"] == {"path": "cases.json", "case_ordinal": 0})
    selected_corpus = next(corpus for corpus in projection["corpora"] if corpus["corpus_id"] == selected["corpus_id"])
    assert [
        (candidate["conversation_order"], message["source_locator"]["conversation_id"], message["source_locator"]["conversation_ordinal"])
        for candidate, message in zip(selected_corpus["candidates"], selected["messages"])
    ] == [(0, first["id"], 0), (1, second["id"], 2)]

    cases[0]["conversations"][-1]["messages"][0]["text"] = "conflicting duplicate"
    _write(cases_path, cases)
    with pytest.raises(custody.CrosswalkError, match="premix_outer_conversation_content_conflict") as error:
        _publish(canonical=canonical, premix=premix, output=tmp_path / "conflict-output", staging=_staging(tmp_path / "conflict"))
    assert error.value.receipt["locator"] == "cases.json" and error.value.receipt["case_ordinal"] == 0


def test_streaming_path_normalizes_exact_duplicate_outer_rows_and_rejects_content_conflicts(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases[0]["conversations"].append(json.loads(json.dumps(cases[0]["conversations"][0])))
    _write(cases_path, cases)
    index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    try:
        connection = __import__("sqlite3").connect(index.database)
        try:
            assert connection.execute("SELECT count(*) FROM premix_exact_duplicate_normalizations").fetchone()[0] == 1
        finally:
            connection.close()
        normalization = index.staging_receipt["premix_exact_duplicate_normalization"]
        assert normalization["schema"] == custody.PREMIX_EXACT_DUPLICATE_NORMALIZATION["schema"]
        assert normalization["duplicate_case_count"] == normalization["duplicate_extra_row_count"] == 1
    finally:
        index.close()
    _publish(canonical=canonical, premix=premix, output=tmp_path / "duplicate", staging=_staging(tmp_path))
    cases[0]["conversations"][-1]["messages"][0]["text"] = "conflicting duplicate"
    _write(cases_path, cases)
    with pytest.raises(custody.CrosswalkError, match="premix_outer_conversation_content_conflict") as error:
        _publish(canonical=canonical, premix=premix, output=tmp_path / "conflict", staging=_staging(tmp_path))
    assert error.value.receipt["locator"] == "cases.json" and error.value.receipt["case_ordinal"] == 0
    canonical, premix = _roots(tmp_path / "empty")
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases[0]["conversations"][0]["messages"] = []
    _write(cases_path, cases)
    with pytest.raises(custody.CustodyError, match="premix_case_has_no_messages"):
        _publish(canonical=canonical, premix=premix, output=tmp_path / "empty-out", staging=_staging(tmp_path / "empty"))


@pytest.mark.parametrize("field", ("labels", "persona_source_id", "source_locator", "directory", "messages"))
def test_recomputed_ready_cannot_bypass_keyed_custody_binding(tmp_path: Path, field: str) -> None:
    output = _build(tmp_path)
    sealed_path = _custody_bundle(output) / "sealed-custody.json"
    sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
    row = sealed["items"][0]
    if field == "labels":
        row["labels"]["answer"] = "forged-answer"
    elif field == "persona_source_id":
        row[field] = "forged-persona"
    elif field == "source_locator":
        row[field]["canonical"]["path"] = "forged.json"
    elif field == "directory":
        row[field]["tier"] = "forged-tier"
    else:
        row[field][0]["source_locator"]["message_ordinal"] = 999
    sealed_path.write_text(json.dumps(sealed), encoding="utf-8")
    _rewrite_custody_ready(output)
    # Candidate code receives no binding material and can only validate its own
    # projection/READY generation.  The custodian must reject each sealed swap.
    assert custody.load_candidate_projection(output)["items"]
    with pytest.raises(custody.CustodyError, match="custody_(item_)?binding_invalid"):
        custody.load_sealed_custody(output, _custody_bundle(output), binding_secret=SECRET)


def test_projection_corpus_swap_with_recomputed_ready_fails_keyed_binding(tmp_path: Path) -> None:
    output = _build(tmp_path)
    projection_path, sealed_path = output / "projection.json", _custody_bundle(output) / "sealed-custody.json"
    projection = json.loads(projection_path.read_text(encoding="utf-8")); sealed = json.loads(sealed_path.read_text(encoding="utf-8"))
    first, second = projection["items"][:2]
    first["corpus_id"], second["corpus_id"] = second["corpus_id"], first["corpus_id"]
    for row in sealed["items"]:
        if row["item_id"] == first["item_id"]: row["corpus_id"] = first["corpus_id"]
        if row["item_id"] == second["item_id"]: row["corpus_id"] = second["corpus_id"]
    sealed["projection_sha256"] = custody.canonical_sha256(projection)
    projection_path.write_text(json.dumps(projection), encoding="utf-8"); sealed_path.write_text(json.dumps(sealed), encoding="utf-8")
    _rewrite_candidate_ready(output)
    custody_ready = json.loads((_custody_bundle(output) / "READY.json").read_text(encoding="utf-8"))
    candidate_ready = json.loads((output / "READY.json").read_text(encoding="utf-8"))
    custody_ready["candidate_projection"] = candidate_ready["projection"]
    ( _custody_bundle(output) / "READY.json").unlink(); custody._write(_custody_bundle(output) / "READY.json", custody_ready)
    _rewrite_custody_ready(output)
    assert custody.load_candidate_projection(output)["items"]
    with pytest.raises(custody.CustodyError, match="custody_item_binding_invalid"):
        custody.load_sealed_custody(output, _custody_bundle(output), binding_secret=SECRET)


def test_reader_rejects_ready_generation_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = _build(tmp_path)
    real_snapshot = custody._snapshot; ready_path = output / "READY.json"; changed = False

    def racing(path: Path, code: str, *, retain: bool = True) -> object:
        nonlocal changed
        result = real_snapshot(path, code, retain=retain)
        if Path(path).name == "projection.json" and not changed:
            changed = True
            ready = json.loads(ready_path.read_text(encoding="utf-8")); ready["generation_id"] = "a" * 64
            ready_path.unlink(); custody._write(ready_path, ready)
        return result

    monkeypatch.setattr(custody, "_snapshot", racing)
    with pytest.raises(custody.CustodyError, match="candidate_bundle_generation_drift"):
        custody.load_candidate_projection(output)


def test_staging_root_is_explicit_preflighted_and_returned(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    staging = _staging(tmp_path)
    result = _publish(canonical=canonical, premix=premix, output=tmp_path / "receipt", staging=staging)
    assert result["staging_preflight"]["root"] == str(staging)
    receipt = result["staging_preflight"]
    assert receipt["required_bytes"] == receipt["source_total_bytes"] * custody.SQLITE_INDEX_EXPANSION_FACTOR + receipt["source_max_file_bytes"] + custody.STAGING_HEADROOM_BYTES
    assert receipt["sqlite_temp_policy"] == "launch-environment-verified-file-v1"
    assert receipt["sqlite_temp_placement"]["resolved_temp_path"] == str(staging)


def test_failed_persona_does_not_pollute_variant_receipt(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    # p-a has one eligible item per group, while p-b receives a second.  A
    # quota of two forces an early ranked p-a candidate to fail, then selects
    # p-b.  The receipt must hash only accepted p-b case variants.
    for group in ("group-1", "group-2"):
        labels_path = canonical / "core_benchmark" / "evidence_questions" / group / "tier-1" / "p-b.json"
        labels = json.loads(labels_path.read_text(encoding="utf-8")); conversation = f"p-b-{group}-extra"
        evidence = {"personId": "p-b", "question": f"q-p-b-extra-{group}", "answer": f"answer-extra-{group}", "category": f"category-{group}", "conversations": [{"id": conversation}], "message_evidences": [{"speaker": "s", "text": "e"}]}
        labels["evidence_items"].append(evidence); _write(labels_path, labels)
        cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"; cases = json.loads(cases_path.read_text(encoding="utf-8"))
        for size in (1, 8, 13): cases.append({"contextSize": size, "evidenceItems": [{key: evidence[key] for key in ("personId", "question", "answer", "category", "conversations")}], "conversations": [{"id": conversation, "messages": [{"speaker": "candidate-speaker", "text": "candidate"}]}]})
        _write(cases_path, cases)
    index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    try:
        persona_a = custody._opaque(SECRET, index.revision, "persona", "p-a"); persona_b = custody._opaque(SECRET, index.revision, "persona", "p-b")
        seed = next(seed for seed in range(1000) if custody._rank(SECRET, index.revision, seed, "persona", persona_a) < custody._rank(SECRET, index.revision, seed, "persona", persona_b))
        values = custody._indexed_context_values(index.database); desired = (values[0], values[-1]); ledger = custody._quarantine_ledger(index.database, index.revision, desired)
        selected, receipt = custody._selection_rows_sql(index.database, SECRET, index.revision, custody.SelectionConfig(seed, 1, 2, (0, 2)), desired, ledger)
        assert receipt["item_supplement_count"] == 1
        assert receipt["variant_selection_sha256"] == custody.canonical_sha256([case["case_sha"] for _item, case in selected])
    finally:
        index.close()


def test_staged_parser_bytes_and_sealed_sqlite_bytes_are_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    original_copy = custody._copy_source_once

    def mutate_stage(source: Path, destination: Path, **kwargs: object) -> object:
        result = original_copy(source, destination, **kwargs)
        with destination.open("ab") as stream:
            stream.write(b"\n")  # valid JSON whitespace, different bytes
        return result

    monkeypatch.setattr(custody, "_copy_source_once", mutate_stage)
    with pytest.raises(custody.CustodyError, match="streaming_stage_digest_mismatch"):
        custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    monkeypatch.undo()
    index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    try:
        with index.database.open("r+b") as stream:
            stream.seek(32); original = stream.read(1); stream.seek(32); stream.write(bytes([original[0] ^ 1]))
        with pytest.raises(custody.CustodyError, match="sqlite_index_preselection_drift"):
            custody._verify_index_bytes(index, "sqlite_index_preselection_drift")
    finally:
        index.close()


def test_source_size_preflight_fails_closed_when_free_space_is_insufficient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    monkeypatch.setattr(custody.shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    with pytest.raises(custody.CustodyError, match="staging_free_space_unavailable"):
        _publish(canonical=canonical, premix=premix, output=tmp_path / "out", staging=_staging(tmp_path))


def test_ancestor_reparse_rejection_is_lstat_based(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"; root.mkdir()
    original_lstat = custody.os.lstat

    def linked(path: object) -> object:
        if Path(path) == root:
            return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
        return original_lstat(path)

    monkeypatch.setattr(custody.os, "lstat", linked)
    with pytest.raises(custody.CustodyError, match="official_root_invalid"):
        custody._safe_existing_ancestors(root, "official_root_invalid")


def test_nested_symlink_is_rejected_when_supported(tmp_path: Path) -> None:
    canonical, _premix = _roots(tmp_path)
    root = custody._subroot(canonical, "evidence_questions")
    try:
        os.symlink(root / "group-1", root / "linked-group", target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(custody.CustodyError, match="official_tree_reparse"):
        custody._files(root)


def test_ready_is_portable_after_byte_for_byte_bundle_copy(tmp_path: Path) -> None:
    output = _build(tmp_path)
    copied = tmp_path / "copied"
    shutil.copytree(output, copied)
    assert custody.load_candidate_projection(copied)["items"]
    copied_custody = _custody_bundle(output).with_name("copied-custody")
    shutil.copytree(_custody_bundle(output), copied_custody)
    assert custody.load_sealed_custody(copied, copied_custody, binding_secret=SECRET)["items"]


def test_posix_fsync_boundaries_are_ordered_and_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    calls: list[str] = []

    def recorded(_path: Path, *, code: str) -> bool:
        calls.append(code)
        return True

    monkeypatch.setattr(custody, "_fsync_directory", recorded)
    _publish(canonical=canonical, premix=premix, output=tmp_path / "durable", staging=_staging(tmp_path))
    assert calls == ["publication_target_directory_entry_fsync_failed", "publication_parent_fsync_failed", "publication_payload_directory_fsync_failed", "publication_ready_directory_fsync_failed", "publication_ready_cleanup_directory_fsync_failed"] * 2
    canonical, premix = _roots(tmp_path / "failure")

    def failed(_path: Path, *, code: str) -> bool:
        if code == "publication_payload_directory_fsync_failed":
            raise custody.CustodyError(code)
        return True

    monkeypatch.setattr(custody, "_fsync_directory", failed)
    target = tmp_path / "failure-out"
    with pytest.raises(custody.CustodyError, match="publication_payload_directory_fsync_failed"):
        _publish(canonical=canonical, premix=premix, output=target, staging=_staging(tmp_path / "failure"))
    assert not target.exists()


def test_memory_temp_mode_is_rejected_and_file_placement_is_bound(tmp_path: Path) -> None:
    class Cursor:
        def __init__(self, value: object) -> None: self.value = value
        def fetchone(self) -> object: return self.value

    class MemoryConnection:
        def execute(self, query: str) -> Cursor:
            if query == "PRAGMA temp_store": return Cursor((2,))
            return Cursor(None)

    with pytest.raises(custody.CustodyError, match="sqlite_temp_memory_or_unknown"):
        custody._configure_sqlite_temp(MemoryConnection(), tmp_path)
    canonical, premix = _roots(tmp_path / "receipt")
    result = _publish(canonical=canonical, premix=premix, output=tmp_path / "receipt-out", staging=_staging(tmp_path / "receipt"))
    placement = result["staging_preflight"]["sqlite_temp_placement"]
    assert placement["policy"] == "launch-environment-verified-file-v1"
    assert placement["resolved_temp_path"] == str(_staging(tmp_path / "receipt"))


def test_os_temp_environment_verifier_rejects_mismatch_and_accepts_windows_posix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sqlite_temp_file_placement: object) -> None:
    verifier = sqlite_temp_file_placement
    staging = _staging(tmp_path)
    monkeypatch.setattr(custody.os, "name", "nt")
    monkeypatch.setattr(custody, "_windows_temp_path", lambda: str(tmp_path / "wrong"))
    with pytest.raises(custody.CustodyError, match="sqlite_temp_environment_mismatch"):
        verifier(staging)
    monkeypatch.setattr(custody, "_windows_temp_path", lambda: str(staging))
    assert verifier(staging)["os_rule"] == "windows-gettemppathw-v1"
    monkeypatch.setattr(custody.os, "name", "posix")
    monkeypatch.setattr(custody.sys, "platform", "linux")
    monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    with pytest.raises(custody.CustodyError, match="sqlite_temp_environment_mismatch"):
        verifier(staging)
    monkeypatch.setenv("SQLITE_TMPDIR", str(staging))
    assert verifier(staging)["os_rule"] == "linux-sqlite_tmpdir-v1"


def test_file_temp_config_uses_no_deprecated_temp_directory_pragma(tmp_path: Path) -> None:
    class Cursor:
        def fetchone(self) -> tuple[int]: return (1,)

    class FileConnection:
        def __init__(self) -> None: self.queries: list[str] = []
        def execute(self, query: str) -> Cursor:
            self.queries.append(query)
            return Cursor()

    connection = FileConnection()
    receipt = custody._configure_sqlite_temp(connection, _staging(tmp_path))
    assert receipt["policy"] == "launch-environment-verified-file-v1"
    assert connection.queries == ["PRAGMA temp_store=FILE", "PRAGMA temp_store"]


def test_candidate_and_custody_paths_are_separate_and_candidate_reader_is_capability_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = _build(tmp_path)
    custody_path = _custody_bundle(output)
    assert set(path.name for path in output.iterdir()) == {"projection.json", "READY.json"}
    candidate_bytes = b"".join(path.read_bytes() for path in output.iterdir())
    assert b"SECRET-" not in candidate_bytes and b"sealed-custody" not in candidate_bytes
    real_snapshot = custody._snapshot

    def deny_custody(path: Path, code: str, *, retain: bool = True) -> object:
        if Path(path).parent == custody_path:
            raise AssertionError("candidate loader accessed custody")
        return real_snapshot(path, code, retain=retain)

    monkeypatch.setattr(custody, "_snapshot", deny_custody)
    assert custody.load_candidate_projection(output)["items"]
    canonical, premix = _roots(tmp_path / "separate")
    with pytest.raises(custody.CustodyError, match="output_paths_not_separate"):
        custody.build_prelabel_bundle(canonical_root=canonical, premix_root=premix, candidate_output_dir=tmp_path / "same", custody_output_dir=tmp_path / "same", staging_root=_staging(tmp_path / "separate"), secret=SECRET, selection=CONFIG)


def test_inventory_rejects_source_growth_and_membership_addition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    source = next((premix / "core_benchmark" / "pre_mixed_testcases").glob("*.json"))
    original_copy = custody._copy_source_once

    def grows(path: Path, destination: Path, **kwargs: object) -> object:
        result = original_copy(path, destination, **kwargs)
        if path == source:
            with path.open("ab") as stream:
                stream.write(b"\n")
        return result

    monkeypatch.setattr(custody, "_copy_source_once", grows)
    with pytest.raises(custody.CustodyError, match="source_inventory_identity_drift"):
        custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    monkeypatch.undo()
    canonical, premix = _roots(tmp_path / "added")
    original_files = custody._files; calls = 0

    def adds(root: Path) -> list[Path]:
        nonlocal calls
        rows = original_files(root); calls += 1
        if calls == 4 and root.name == "pre_mixed_testcases":
            _write(root / "late.json", [])
            return original_files(root)
        return rows

    monkeypatch.setattr(custody, "_files", adds)
    with pytest.raises(custody.CustodyError, match="source_inventory_membership_drift"):
        custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path / "added"))


@pytest.mark.skipif(os.name != "nt", reason="Windows VFS smoke")
def test_windows_subprocess_uses_real_tmp_temp_and_cli(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    staging = _staging(tmp_path); candidate = tmp_path / "candidate"; sealed = tmp_path / "custody"; secret = tmp_path / "secret.bin"; secret.write_bytes(SECRET)
    command = [sys.executable, "benchmarks/aerp7_convomem_confirmation.py", "--canonical-root", str(canonical), "--premix-root", str(premix), "--candidate-output-dir", str(candidate), "--custody-output-dir", str(sealed), "--staging-root", str(staging), "--secret-key-file", str(secret), "--seed", "7", "--persona-quota", "1", "--per-persona-group-quota", "1", "--context-rank-index", "0", "--context-rank-index", "2"]
    environment = dict(os.environ); environment["TMP"] = str(staging); environment["TEMP"] = str(staging)
    result = subprocess.run(command, cwd=Path(__file__).parents[1], env=environment, text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    loader = [sys.executable, "-c", "from pathlib import Path; from benchmarks.aerp7_convomem_confirmation import load_candidate_projection,load_sealed_custody; s=Path(r'" + str(secret) + "').read_bytes(); assert load_candidate_projection(Path(r'" + str(candidate) + "'))['items']; assert load_sealed_custody(Path(r'" + str(candidate) + "'),Path(r'" + str(sealed) + "'),binding_secret=s)['items']"]
    checked = subprocess.run(loader, cwd=Path(__file__).parents[1], env=environment, text=True, capture_output=True, timeout=60)
    assert checked.returncode == 0, checked.stdout + checked.stderr


@pytest.mark.parametrize("fault", ("read", "fsync", "source_drift"))
def test_copy_failures_remove_identity_owned_staged_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    source = tmp_path / "source.json"; staged = tmp_path / "stage.json"; source.write_text("[]", encoding="utf-8")
    if fault == "read":
        monkeypatch.setattr(custody.os, "read", lambda *_args: (_ for _ in ()).throw(OSError("read fault")))
        expected = "read fault"
    elif fault == "fsync":
        monkeypatch.setattr(custody.os, "fsync", lambda *_args: (_ for _ in ()).throw(OSError("fsync fault")))
        expected = "fsync fault"
    else:
        def mutate(_identity: tuple[int, int]) -> None:
            with source.open("ab") as stream:
                stream.write(b"\n")
        expected = "official_source_drift"
    with pytest.raises((OSError, custody.CustodyError), match=expected):
        if fault == "source_drift":
            custody._copy_source_once(source, staged, on_staged_created=mutate)
        else:
            custody._copy_source_once(source, staged)
    assert not staged.exists()


def test_streaming_snapshot_failure_removes_registered_staged_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path); staging = _staging(tmp_path); real_snapshot = custody._snapshot

    def fail_stage(path: Path, code: str, *, retain: bool = True) -> object:
        if Path(path).name.startswith(("canonical-", "premix-")):
            raise custody.CustodyError("forced_stage_snapshot_failure")
        return real_snapshot(path, code, retain=retain)

    monkeypatch.setattr(custody, "_snapshot", fail_stage)
    with pytest.raises(custody.CustodyError, match="forced_stage_snapshot_failure"):
        custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), staging)
    assert not list(staging.glob("aerp7-convomem-index-*"))


@pytest.mark.parametrize("publisher", ("custody", "candidate"))
@pytest.mark.parametrize("boundary", ("ready_fsync", "ready_snapshot", "temp_unlink", "temp_cleanup_fsync"))
def test_postlink_faults_leave_no_loadable_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, publisher: str, boundary: str) -> None:
    canonical, premix = _roots(tmp_path); candidate = tmp_path / "candidate"; sealed = _custody_bundle(candidate)
    target = sealed if publisher == "custody" else candidate
    real_fsync, real_snapshot, real_unlink = custody._fsync_directory, custody._snapshot, custody.os.unlink

    if boundary in {"ready_fsync", "temp_cleanup_fsync"}:
        code = "publication_ready_directory_fsync_failed" if boundary == "ready_fsync" else "publication_ready_cleanup_directory_fsync_failed"
        def fail_fsync(path: Path, *, code: str) -> bool:
            if Path(path) == target and code == ("publication_ready_directory_fsync_failed" if boundary == "ready_fsync" else "publication_ready_cleanup_directory_fsync_failed"):
                raise custody.CustodyError(code)
            return real_fsync(path, code=code)
        monkeypatch.setattr(custody, "_fsync_directory", fail_fsync)
    elif boundary == "ready_snapshot":
        def fail_snapshot(path: Path, code: str, *, retain: bool = True) -> object:
            if Path(path) == target / "READY.json":
                raise custody.CustodyError("forced_ready_snapshot_failure")
            return real_snapshot(path, code, retain=retain)
        monkeypatch.setattr(custody, "_snapshot", fail_snapshot)
    else:
        def fail_unlink(path: Path, *args: object, **kwargs: object) -> object:
            if Path(path) == target / ".READY.json.tmp":
                raise OSError("forced_temp_unlink_failure")
            return real_unlink(path, *args, **kwargs)
        monkeypatch.setattr(custody.os, "unlink", fail_unlink)
    with pytest.raises((custody.CustodyError, OSError)):
        _publish(canonical=canonical, premix=premix, output=candidate, staging=_staging(tmp_path))
    with pytest.raises(custody.CustodyError):
        custody.load_candidate_projection(candidate)
    with pytest.raises(custody.CustodyError):
        custody.load_sealed_custody(candidate, sealed, binding_secret=SECRET)


@pytest.mark.parametrize("entry", ("projection.json", "READY.json"))
def test_publication_cleanup_keeps_marker_on_payload_or_ready_unlink_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str) -> None:
    target = tmp_path / "failed"; target.mkdir(); marker = target / ".aerp7-publishing"; payload = target / "projection.json"; ready = target / "READY.json"
    for path in (marker, payload, ready): path.write_text(path.name, encoding="utf-8")
    created = {path: custody._snapshot(path, "test", retain=False)[1] for path in (marker, payload, ready)}
    identity = custody._directory_identity(target, "test"); real_unlink = custody.os.unlink

    def fail(path: Path, *args: object, **kwargs: object) -> object:
        if Path(path).name == entry: raise OSError("forced unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(custody.os, "unlink", fail)
    assert custody._cleanup_owned_target(target, identity, created) is False
    assert marker.exists()


def test_publication_cleanup_keeps_marker_on_ready_identity_drift(tmp_path: Path) -> None:
    target = tmp_path / "drift"; target.mkdir(); marker = target / ".aerp7-publishing"; payload = target / "projection.json"; ready = target / "READY.json"
    for path in (marker, payload, ready): path.write_text(path.name, encoding="utf-8")
    created = {path: custody._snapshot(path, "test", retain=False)[1] for path in (marker, payload, ready)}
    ready.unlink(); ready.write_text("replaced", encoding="utf-8")
    assert custody._cleanup_owned_target(target, custody._directory_identity(target, "test"), created) is False
    assert marker.exists()


@pytest.mark.parametrize("fault", ("unlink", "identity"))
def test_streaming_close_keeps_owned_marker_on_database_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    canonical, premix = _roots(tmp_path); index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    marker = index.directory / ".aerp7-owned"
    if fault == "unlink":
        real_unlink = custody.os.unlink
        def fail(path: Path, *args: object, **kwargs: object) -> object:
            if Path(path) == index.database: raise OSError("forced database unlink failure")
            return real_unlink(path, *args, **kwargs)
        monkeypatch.setattr(custody.os, "unlink", fail)
    else:
        index.database.unlink(); index.database.write_bytes(b"replaced")
    with pytest.raises(custody.CustodyError, match="streaming_index_cleanup_(unlink_failed|identity_drift)"):
        index.close()
    assert marker.exists()


def test_publisher_failure_cleanup_renames_to_nonloadable_tombstone_before_rmdir_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path); candidate = tmp_path / "candidate"; sealed = _custody_bundle(candidate)
    real_fsync, real_rmdir = custody._fsync_directory, custody.Path.rmdir

    def fail_ready(path: Path, *, code: str) -> bool:
        if Path(path) == sealed and code == "publication_ready_directory_fsync_failed":
            raise custody.CustodyError(code)
        return real_fsync(path, code=code)

    def race_rmdir(path: Path) -> None:
        if Path(path).name.startswith(".aerp7-incomplete-"):
            (Path(path) / "foreign.txt").write_text("foreign", encoding="utf-8")
        real_rmdir(path)

    monkeypatch.setattr(custody, "_fsync_directory", fail_ready)
    monkeypatch.setattr(custody.Path, "rmdir", race_rmdir)
    with pytest.raises(custody.CustodyError, match="publication_cleanup_identity_drift"):
        _publish(canonical=canonical, premix=premix, output=candidate, staging=_staging(tmp_path))
    tombstones = list(tmp_path.glob(".aerp7-incomplete-*"))
    assert not sealed.exists() and len(tombstones) == 1 and (tombstones[0] / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    with pytest.raises(custody.CustodyError):
        custody.load_sealed_custody(candidate, sealed, binding_secret=SECRET)


def test_streaming_failure_cleanup_renames_to_tombstone_before_rmdir_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path); index = custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))
    original = index.directory; real_rmdir = custody.Path.rmdir

    def race_rmdir(path: Path) -> None:
        if Path(path).name.startswith(".aerp7-cleaning-"):
            (Path(path) / "foreign.txt").write_text("foreign", encoding="utf-8")
        real_rmdir(path)

    monkeypatch.setattr(custody.Path, "rmdir", race_rmdir)
    with pytest.raises(custody.CustodyError, match="streaming_index_cleanup_rmdir_failed"):
        index.close(failure_cleanup=True)
    tombstones = list(original.parent.glob(".aerp7-cleaning-*"))
    assert not original.exists() and len(tombstones) == 1 and (tombstones[0] / "foreign.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.skipif(os.name != "nt", reason="real Kernel32 MoveFileExW semantics")
def test_windows_noreplace_rename_retries_collision_and_reports_fatal_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"; source.mkdir(); identity = custody._directory_identity(source, "test"); parent_identity = custody._directory_identity(tmp_path, "test")
    collision = tmp_path / ".aerp7-cleaning-collision"; collision.mkdir()
    tokens = iter(("collision", "unique"))
    monkeypatch.setattr(custody.secrets, "token_hex", lambda _length: next(tokens))
    renamed = custody._claim_cleanup_tombstone(source, identity, parent_identity, ".aerp7-cleaning-")
    assert renamed == tmp_path / ".aerp7-cleaning-unique"
    assert not source.exists() and collision.exists() and renamed.exists()
    missing = tmp_path / "missing"
    with pytest.raises(custody.CustodyError, match="cleanup_tombstone_rename_failed") as error:
        custody._rename_noreplace(missing, tmp_path / "destination")
    assert error.value.receipt["platform"] == "windows"
    assert error.value.receipt["winerror"] in {2, 3}


@pytest.mark.parametrize("phase", ("selection", "custody_publish", "candidate_publish"))
def test_build_late_failures_use_cleaning_tombstone_and_preserve_primary_and_cleanup_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    canonical, premix = _roots(tmp_path); candidate = tmp_path / "candidate"; captured: list[object] = []
    real_stream, real_rmdir = custody._streaming_index, custody.Path.rmdir

    def capture_stream(*args: object, **kwargs: object) -> object:
        index = real_stream(*args, **kwargs); captured.append(index); return index

    def race_rmdir(path: Path) -> None:
        if phase == "selection" and Path(path).name.startswith(".aerp7-cleaning-"):
            (Path(path) / "foreign.txt").write_text("foreign", encoding="utf-8")
        real_rmdir(path)

    monkeypatch.setattr(custody, "_streaming_index", capture_stream)
    monkeypatch.setattr(custody.Path, "rmdir", race_rmdir)
    if phase == "selection":
        monkeypatch.setattr(custody, "_selection_rows_sql", lambda *_args, **_kwargs: (_ for _ in ()).throw(custody.CustodyError("forced_selection_failure")))
        primary_code = "forced_selection_failure"
    elif phase == "custody_publish":
        monkeypatch.setattr(custody, "_publish_custody", lambda *_args, **_kwargs: (_ for _ in ()).throw(custody.CustodyError("forced_custody_publish_failure")))
        primary_code = "forced_custody_publish_failure"
    else:
        monkeypatch.setattr(custody, "_publish_candidate", lambda *_args, **_kwargs: (_ for _ in ()).throw(custody.CustodyError("forced_candidate_publish_failure")))
        primary_code = "forced_candidate_publish_failure"
    if phase == "selection":
        with pytest.raises(custody.CustodyError, match="build_primary_and_index_cleanup_failed") as error:
            _publish(canonical=canonical, premix=premix, output=candidate, staging=_staging(tmp_path))
        assert error.value.receipt["primary_error"]["code"] == primary_code
        assert error.value.receipt["cleanup_error"]["code"] == "streaming_index_cleanup_rmdir_failed"
        index = captured[0]
        tombstones = list(index.directory.parent.glob(".aerp7-cleaning-*"))
        assert not index.directory.exists() and len(tombstones) == 1 and (tombstones[0] / "foreign.txt").read_text(encoding="utf-8") == "foreign"
    else:
        with pytest.raises(custody.CustodyError, match=primary_code):
            _publish(canonical=canonical, premix=premix, output=candidate, staging=_staging(tmp_path))
        index = captured[0]
        assert not index.directory.exists()


def test_streaming_rejects_staging_root_rebind_after_preflight_without_index_spill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path); staging = _staging(tmp_path)
    inventory = custody._source_size_inventory(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"))
    staging, preflight = custody._staging_receipt(staging, inventory)
    moved = tmp_path / "approved-root-moved"; real_mkdtemp = custody.tempfile.mkdtemp

    def rebind(*args: object, **kwargs: object) -> str:
        os.rename(staging, moved); staging.mkdir()
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(custody.tempfile, "mkdtemp", rebind)
    with pytest.raises(custody.CustodyError, match="staging_root_identity_drift"):
        custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), staging, staging_preflight=preflight)
    assert not list(staging.glob("aerp7-convomem-index-*")) and not list(moved.glob("aerp7-convomem-index-*"))


def test_success_index_close_failure_blocks_all_ready_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path); candidate = tmp_path / "candidate"; sealed = _custody_bundle(candidate); captured: list[object] = []
    real_stream, real_rmdir = custody._streaming_index, custody.Path.rmdir

    def capture_stream(*args: object, **kwargs: object) -> object:
        index = real_stream(*args, **kwargs); captured.append(index); return index

    def race_rmdir(path: Path) -> None:
        if Path(path).name.startswith(".aerp7-cleaning-"):
            (Path(path) / "foreign.txt").write_text("foreign", encoding="utf-8")
        real_rmdir(path)

    monkeypatch.setattr(custody, "_streaming_index", capture_stream)
    monkeypatch.setattr(custody.Path, "rmdir", race_rmdir)
    with pytest.raises(custody.CustodyError, match="streaming_index_cleanup_rmdir_failed"):
        _publish(canonical=canonical, premix=premix, output=candidate, staging=_staging(tmp_path))
    index = captured[0]; tombstones = list(index.directory.parent.glob(".aerp7-cleaning-*"))
    assert not candidate.exists() and not sealed.exists() and not index.directory.exists()
    assert len(tombstones) == 1 and (tombstones[0] / "foreign.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("platform", ("darwin", "freebsd"))
def test_unsupported_builder_platform_rejects_before_official_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    canonical, premix = _roots(tmp_path)
    monkeypatch.setattr(custody.os, "name", "posix")
    monkeypatch.setattr(custody.sys, "platform", platform)
    monkeypatch.setattr(custody, "_subroot", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("official input was read")))
    with pytest.raises(custody.CustodyError, match="builder_platform_unsupported") as error:
        _publish(canonical=canonical, premix=premix, output=tmp_path / "candidate", staging=_staging(tmp_path))
    assert error.value.receipt["platform"] == platform


def test_linux_without_renameat2_rejects_before_streaming_official_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    canonical, premix = _roots(tmp_path)
    monkeypatch.setattr(custody.os, "name", "posix")
    monkeypatch.setattr(custody.sys, "platform", "linux")
    monkeypatch.setattr(custody, "_linux_renameat2_available", lambda: False)
    monkeypatch.setattr(custody, "_source_size_inventory", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("official input was read")))
    with pytest.raises(custody.CustodyError, match="builder_platform_unsupported"):
        custody._streaming_index(custody._subroot(canonical, "evidence_questions"), custody._subroot(premix, "pre_mixed_testcases"), _staging(tmp_path))


def test_census_v1_selection_is_explicitly_rng_free() -> None:
    selection = custody.SelectionConfig.census_v1()
    selection.validate()
    assert selection.seed is None
    assert selection.persona_quota == "ALL"
    assert selection.per_persona_group_quota == "ALL"
    assert selection.context_rank_indices == "ALL_AVAILABLE_SORTED"


def test_census_v1_publishes_a_projection_that_validates_its_exact_denominators(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    output = tmp_path / "census"
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path), config=custody.SelectionConfig.census_v1())
    projection = custody.load_candidate_projection(output)
    receipt = projection["selection_receipt"]
    assert receipt["algorithm"] == custody.CENSUS_SELECTION_ALGORITHM
    assert receipt["selected_item_ids_sha256"] == custody.canonical_sha256(sorted(item["item_id"] for item in projection["items"]))
    assert receipt["selected_persona_ids_sha256"] == custody.canonical_sha256(sorted({item["persona_id"] for item in projection["items"]}))
    assert sum(row["item_count"] for row in receipt["per_context_denominators"]) == len(projection["items"])
    assert receipt["group_count"] == len({item["selection_group_id"] for item in projection["items"]})
    first = projection["items"][0]["selection_group_id"]
    for item in projection["items"]:
        item["selection_group_id"] = first
    with pytest.raises(custody.CustodyError, match="census_denominator_binding_invalid"):
        custody.validate_candidate_projection(projection)


def test_formal_source_outputs_must_be_new(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    candidate = tmp_path / "candidate"; custody_root = _custody_bundle(candidate)
    _publish(canonical=canonical, premix=premix, output=candidate, staging=_staging(tmp_path), config=custody.SelectionConfig.census_v1())
    staging = tmp_path / "fresh-staging"; staging.mkdir()
    plan = {
        "candidate_output_dir": str(candidate), "custody_output_dir": str(custody_root),
        "output_dir": str(tmp_path / "public"), "staging_root": str(staging),
        **{key: str(tmp_path / (key + ".json")) for key in (
            "protocol_path", "authorization_path", "custodian_public_config_path", "final_output_path",
            "one_shot_receipt_path", "infrastructure_failure_receipt_path", "progress_receipt_path",
        )},
    }
    with pytest.raises(custody.CustodyError, match="formal_output_not_new"):
        one_shot._require_new_formal_targets(plan=plan)


@pytest.mark.parametrize("seal", (
    "group_values_sha256", "tier_values_sha256", "variant_selection_sha256",
    "context_values_sha256", "desired_context_values_sha256", "quarantine_ledger_sha256",
))
def test_census_v1_recomputes_each_public_selection_seal(tmp_path: Path, seal: str) -> None:
    canonical, premix = _roots(tmp_path)
    output = tmp_path / "census-seals"
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path), config=custody.SelectionConfig.census_v1())
    projection = custody.load_candidate_projection(output)
    projection["selection_receipt"][seal] = "0" * 64
    with pytest.raises(custody.CustodyError, match="census_denominator_binding_invalid"):
        custody.validate_candidate_projection(projection)


def test_census_v1_recomputes_each_quarantine_reason_digest(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    output = tmp_path / "census-quarantine"
    _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path), config=custody.SelectionConfig.census_v1())
    projection = custody.load_candidate_projection(output)
    projection["selection_receipt"]["quarantine_reason_digests"]["unmatched_premix_keys"] = "0" * 64
    with pytest.raises(custody.CustodyError, match="census_denominator_binding_invalid"):
        custody.validate_candidate_projection(projection)


def test_census_v1_rejects_any_multi_persona_quarantine_without_publication(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path, mixed=True)
    output = tmp_path / "census"
    with pytest.raises(custody.CrosswalkError, match="census_quarantine_nonempty"):
        _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path), config=custody.SelectionConfig.census_v1())
    assert not output.exists()


def test_census_v1_rejects_unmatched_premix_keys_without_publication(tmp_path: Path) -> None:
    canonical, premix = _roots(tmp_path)
    cases_path = premix / "core_benchmark" / "pre_mixed_testcases" / "cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    cases.append({"contextSize": 1, "evidenceItems": [{"personId": "p-missing", "question": "q-missing", "answer": "SECRET-missing", "category": "category-missing", "conversations": [{"id": "missing-conversation"}]}], "conversations": [{"id": "missing-conversation", "messages": [{"speaker": "speaker", "text": "candidate"}]}]})
    _write(cases_path, cases)
    output = tmp_path / "census"
    with pytest.raises(custody.CrosswalkError, match="census_quarantine_nonempty"):
        _publish(canonical=canonical, premix=premix, output=output, staging=_staging(tmp_path), config=custody.SelectionConfig.census_v1())
    assert not output.exists()
