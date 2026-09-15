import copy
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time
import tracemalloc
from pathlib import Path

import pytest

from benchmarks import aerp7_convomem_rank as rank
from benchmarks import aerp7_convomem_scoring as score
from benchmarks.aerp7_convomem_confirmation import CustodyError, CustodyStore, canonical_sha256


def h(value): return hashlib.sha256(value.encode()).hexdigest()


class Encoder:
    identity = "synthetic-encoder"
    def encode_passages(self, texts): return [[float(len(text) + index + 1), 1.0] for index, text in enumerate(texts)]
    def encode_query(self, text): return [float(len(text) + 1), 1.0]


def receipts(): return ({"encoder_identity": "synthetic-encoder", "encoder_semantics": "deterministic", "files": [{"path_role": "weights", "sha256": h("weights"), "bytes": 2}]}, {"head": h("head"), "tree": h("tree"), "diff_digest": h("diff"), "dirty_policy": "clean_required"})


def projection():
    selection = {"algorithm": "hmac-sha256-revision-bound-persona-group-tier-context-v1", "seed": 1, "persona_quota": 1, "per_persona_group_quota": 1, "context_rank_indices": [0], "context_rank_semantics": "zero_based_unique_sorted_values", "selected_persona_ids_sha256": h("selected"), "holdout_persona_set_sha256": h("holdout"), "group_values_sha256": h("groups"), "tier_values_sha256": h("tiers"), "context_values_sha256": h("contexts"), "desired_context_values_sha256": h("desired"), "variant_selection_sha256": h("variants"), "selected_item_context_count": 12, "item_supplement_count": 0, "exclusion_counts": {"multi_persona_cases": 0, "missing_crosswalk": 0, "ambiguous_canonical_keys": 0, "unmatched_premix_keys": 0, "multiple_logical_matches_or_variants": 0, "missing_requested_context_sizes": 0}, "quarantine_reason_digests": {name: h(name) for name in ("multi_persona_cases", "canonical_zero_logical_matches", "ambiguous_canonical_keys", "unmatched_premix_keys", "multiple_logical_matches_or_variants", "missing_requested_context_sizes")}, "quarantine_ledger_sha256": h("ledger")}
    corpora=[]; items=[]; groups=list(score.UPSTREAM_GROUPS)
    for person in range(2):
        corpus=h("corpus"+str(person)); conversation=h("conversation"+str(person)); candidates=[{"message_id":h(f"m{person}-{n}"),"opaque_conversation_id":conversation,"conversation_order":0,"message_order":n,"corpus_order":n,"speaker":"user" if n==0 else "assistant","text":"target" if n==0 else f"other {n}"} for n in range(11)]
        corpora.append({"corpus_id":corpus,"declared_context_size":2,"actual_conversation_count":1,"actual_message_count":len(candidates),"candidates":candidates})
        items.extend({"item_id":h(f"item-{person}-{group}"),"persona_id":h(f"persona-{person}"),"query_text":f"query {person} {group}","corpus_id":corpus} for group in groups)
    return {"schema":"aerp7-convomem-candidate-projection-v3","dataset":{key:h(key) for key in ("canonical_sha256","premix_sha256","revision_sha256","source_inventory_sha256")},"selection_receipt":selection,"corpora":corpora,"items":items}


def original_replicates(p):
    model, code=receipts(); source=rank.rank_projection(projection=p,encoder=Encoder(),arm_id="strong_raw",model_receipt=model,code_receipt=code); corpora={row["corpus_id"]:row for row in p["corpora"]}; items={row["item_id"]:row for row in p["items"]}; result=[]
    for number in range(5):
        rows=[]; traces=[]
        for row in source["rankings"]:
            corpus=corpora[items[row["item_id"]]["corpus_id"]]; candidate_digest=rank._candidate_input(corpus,rank.ORIGINAL_MEMPALACE_SERIALIZER)
            rows.append({**{key:value for key,value in row.items() if key not in {"confidence","confidence_receipt"}},"candidate_input_sha256":candidate_digest,"confidence":None,"confidence_receipt":None})
        for trace in source["trace_receipt"]:
            corpus=corpora[items[trace["item_id"]]["corpus_id"]]
            traces.append({"item_id":trace["item_id"],"query_sha256":trace["query_sha256"],"candidate_input_sha256":rank._candidate_input(corpus,rank.ORIGINAL_MEMPALACE_SERIALIZER),"ranked_count":trace["ranked_count"],"ranking_sha256":trace["ranking_sha256"]})
        input_receipt=rank._input_receipt(p,rank.ORIGINAL_MEMPALACE_SERIALIZER); physical_ids=[f"{corpus['corpus_id']}::aerp7::{candidate['message_id']}" for corpus in p["corpora"] for candidate in corpus["candidates"]]; physical={"physical_count":len(physical_ids),"physical_ids_sha256":rank._digest(sorted(physical_ids)),"embedding":{"count":len(physical_ids),"dimension":384,"dtype":"float32","float32_sha256":h(f"embed-{number}")},"hnsw_config":rank.ORIGINAL_HNSW_CONFIG,"graph_files":[{"name":name,"path":f"segment/{name}","bytes":1,"sha256":h(f"graph-{number}-{name}")} for name in rank.ORIGINAL_GRAPH_NAMES],"immutable_backend_sha256":h(f"backend-{number}"),"immutable_non_length_backend_sha256":h(f"non-length-backend-{number}"),"immutable_residual_backend_sha256":h(f"residual-backend-{number}"),"sqlite_semantic_sha256":h(f"sqlite-{number}"),"operational_delta":rank.ORIGINAL_OPERATIONAL_DELTA,"direct_read_normalization_delta":{"schema":rank.DIRECT_READ_NORMALIZATION_SCHEMA,"status":"none","path":None,"bytes":None,"before_sha256":None,"after_sha256":None}}
        index_receipt={"build_id":f"build-{number}","fresh_build":True,"collection_identity":f"collection-{number}","index_identity_sha256":"","cold_reopen":True,"call_contract":rank.ORIGINAL_CALL_CONTRACT,"input_coverage_sha256":rank._digest(input_receipt["item_corpora"]),"query_coverage_sha256":rank._digest([{"item_id":item["item_id"],"query_sha256":rank._query_digest(item["query_text"])} for item in sorted(p["items"],key=lambda item:item["item_id"])]),"output_coverage_sha256":rank._digest([{"item_id":trace["item_id"],"ranking_sha256":trace["ranking_sha256"]} for trace in sorted(traces,key=lambda trace:trace["item_id"])]),"worker_physical_receipt":physical,"coordinator_physical_receipt":copy.deepcopy(physical)}; index_receipt["index_identity_sha256"]=rank._digest({"collection_identity":index_receipt["collection_identity"],"physical":physical})
        result.append({"build_id":f"build-{number}","input_receipt":input_receipt,"input_sha256":rank._digest(input_receipt),"index_receipt":index_receipt,"index_sha256":rank._digest(index_receipt),"trace_receipt":traces,"trace_sha256":rank._digest(traces),"rankings":rows})
    return result


def artifacts(p):
    model, code=receipts(); current=rank.freeze_current_rankings(projection=p,encoder=Encoder(),model_receipt=model,code_receipt=code)
    return [rank.wrap_original_public_rankings(projection=p,replicates=original_replicates(p),model_receipt=model,code_receipt=code),*current]


def manifest(p, arts):
    row={"schema":score.MANIFEST_SCHEMA,"projection_sha256":canonical_sha256(p),"protocol_source":rank.PROTOCOL_SOURCE,"serializer_contract":{"current":rank.CURRENT_SERIALIZER,"original_public_product":rank.ORIGINAL_MEMPALACE_SERIALIZER},"arms":[{"arm_id":art["arm_id"],"ranking_artifact_sha256":art["artifact_sha256"],"confidence_contract":rank.CONFIDENCE_CONTRACT if art["arm_id"] in rank.CURRENT_ARMS else None} for art in arts],"directory_endpoints":[{"directory_group":group,"endpoint":endpoint} for group,endpoint in score.UPSTREAM_GROUPS.items()],"bootstrap":{"seed":17,"resamples":19,"percentile_lower":.025,"percentile_upper":.975,"percentile_rule":"linear","original_replicate_rule":"per_query_arithmetic_mean"},"synthetic_test_mode":True,"reference_arm":"strong_raw"}
    row["manifest_sha256"]=score.endpoint_manifest_digest(row); return row


def custody(p):
    corpora={row["corpus_id"]:row for row in p["corpora"]}; result=[]
    for item in p["items"]:
        group=item["query_text"].split()[-1]; corpus=corpora[item["corpus_id"]]; endpoint=score.UPSTREAM_GROUPS[group]
        result.append({"item_id":item["item_id"],"directory_group":group,"evidence_conversation_ids":[] if endpoint=="abstention" else [corpus["candidates"][0]["opaque_conversation_id"]],"evidence_spans":[] if endpoint=="abstention" else [{"speaker":"user","text":"target"}]})
    return {"schema":score.CUSTODY_SCHEMA,"projection_sha256":canonical_sha256(p),"items":result}


def run():
    p=projection(); arts=artifacts(p); return p, arts, manifest(p,arts), custody(p)


def test_scoring_db_capacity_peak_includes_active_rollback_journal() -> None:
    with score._ScoringDB(observe_capacity=True) as db:
        db.connection.execute("BEGIN IMMEDIATE")
        db.connection.execute(
            "INSERT INTO projection_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("item", "persona", "corpus", 1, 1, 1, "x" * 1_000_000),
        )
        active = sum(
            path.stat().st_size
            for path in (db.path, db.path.with_name(db.path.name + "-journal"), db.path.with_name(db.path.name + "-wal"), db.path.with_name(db.path.name + "-shm"))
            if path.is_file() and not path.is_symlink()
        )
        assert db.path.with_name(db.path.name + "-journal").is_file()
        assert db.peak_footprint_bytes >= active > 0
        db.connection.commit()
        assert db.peak_footprint_bytes >= active


def test_scoring_db_default_path_has_no_capacity_stat_proxy(monkeypatch) -> None:
    monkeypatch.setattr(score._ScoringDB, "_observe_footprint", lambda _self: (_ for _ in ()).throw(AssertionError("capacity stat")))
    with score._ScoringDB() as db:
        db.connection.execute("SELECT 1").fetchone()
        db.connection.commit()


def test_scoring_db_uses_validated_sqlite_staging_and_rejects_invalid_paths(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SQLITE_TMPDIR", raising=False)
    with score._ScoringDB() as db:
        assert db.path.parent.name.startswith("aerp7-scoring-")
    staging = tmp_path / "run-root" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("SQLITE_TMPDIR", str(staging))
    with score._ScoringDB() as db:
        assert db.path.parent.parent == staging.resolve()
        assert Path(db.connection.execute("PRAGMA database_list").fetchone()[2]) == db.path
        db.connection.execute("PRAGMA temp_store=FILE")
        db.connection.execute("CREATE TEMP TABLE temp_probe(value INTEGER)")
        db.connection.execute("INSERT INTO temp_probe VALUES (1)")
        assert db.connection.execute("SELECT value FROM temp_probe").fetchone() == (1,)
    assert not list(staging.glob("aerp7-scoring-*"))
    monkeypatch.setenv("SQLITE_TMPDIR", "relative-staging")
    with pytest.raises(CustodyError, match="scoring_sqlite_tmpdir_invalid"):
        score._ScoringDB()
    monkeypatch.setenv("SQLITE_TMPDIR", str(tmp_path / "missing-staging"))
    with pytest.raises(CustodyError, match="scoring_sqlite_tmpdir_invalid"):
        score._ScoringDB()
    nul_environment = dict(os.environ)
    nul_environment["SQLITE_TMPDIR"] = "staging\x00path"
    monkeypatch.setattr(score.os, "environ", nul_environment)
    with pytest.raises(CustodyError, match="scoring_sqlite_tmpdir_invalid"):
        score._ScoringDB()


def test_legacy_ledger_validation_uses_validated_sqlite_staging_and_cleans_up(tmp_path, monkeypatch) -> None:
    staging = tmp_path / "run-root" / "staging"
    staging.mkdir(parents=True)
    monkeypatch.setenv("SQLITE_TMPDIR", str(staging))
    first = h("legacy-ledger-first")
    second = h("legacy-ledger-second")
    completed: dict[str, object] = {}

    def entries():
        yield {"item_id": first, "evidence_token": second, "status": "mapped"}
        time.sleep(0.05)
        yield {"item_id": second, "evidence_token": first, "status": "unmatched"}

    def validate() -> None:
        try:
            completed["count"] = score._validate_ledger_entries(entries())
        except BaseException as exc:
            completed["error"] = exc

    worker = threading.Thread(target=validate)
    worker.start()
    observed_ledger_spools: set[Path] = set()
    deadline = time.monotonic() + 5.0
    while worker.is_alive():
        observed_ledger_spools.update(staging.glob("aerp7-ledger-validate-*"))
        if time.monotonic() >= deadline:
            pytest.fail("legacy_ledger_staging_observation_timeout")
        time.sleep(0.001)
    worker.join()
    if "error" in completed:
        raise completed["error"]
    assert completed["count"] == 2
    assert observed_ledger_spools
    assert all(path.parent == staging.resolve() for path in observed_ledger_spools)
    assert not list(staging.glob("aerp7-ledger-validate-*"))


class _CursorProjectionStore(rank.CandidateProjectionStore):
    """Candidate-only cursor seam used to exercise the formal consumer shape."""

    def __init__(self, p, tmp_path):
        self.reference = {
            "schema": rank.CANDIDATE_PROJECTION_REFERENCE_SCHEMA,
            "bundle_path": str((tmp_path / "candidate").resolve()),
            "projection_path": "projection.json", "ready_path": "READY.json",
            "generation_id": h("generation"),
            "projection_raw_sha256": h("projection-raw"),
            "projection_canonical_sha256": canonical_sha256(p),
            "dataset": dict(p["dataset"]), "query_count": len(p["items"]),
            "candidate_text_count": sum(len(row["candidates"]) for row in p["corpora"]),
        }
        self.database = tmp_path / "candidate.sqlite3"
        self.connection = sqlite3.connect(self.database)
        self.connection.executescript("CREATE TABLE items(item_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL); CREATE TABLE corpora(corpus_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);")
        self.connection.executemany("INSERT INTO items VALUES (?, ?)", [(row["item_id"], json.dumps(row)) for row in p["items"]])
        self.connection.executemany("INSERT INTO corpora VALUES (?, ?)", [(row["corpus_id"], json.dumps(row)) for row in p["corpora"]])
        self.connection.commit()
        self._items = list(p["items"]); self._corpora = {row["corpus_id"]: row for row in p["corpora"]}
        self._run_active = False; self._closed = False

    def iter_items(self):
        yield from self._items

    def corpus(self, corpus_id):
        return self._corpora[corpus_id]

    def begin_run(self):
        self._run_active = True

    def end_run(self):
        self._run_active = False


class _CursorCustodyStore(CustodyStore):
    """Custody capability exposing only the scorer's label-free cursor."""

    def __init__(self, reference, rows, directory):
        self.reference = reference; self.directory = directory; self.connection = None; self._rows = rows

    def iter_scoring_items(self):
        yield from self._rows

    def close(self):
        if self.directory.exists():
            shutil.rmtree(self.directory)


def _cursor_custody(p, projection_store, tmp_path):
    rows = custody(p)["items"]
    reference = {
        "schema": score.CUSTODY_REFERENCE_SCHEMA,
        "bundle_path": str((tmp_path / "candidate-custody").resolve()),
        "candidate_reference": dict(projection_store.reference),
        "custody_path": str((tmp_path / "sealed-custody.json").resolve()),
        "ready_path": str((tmp_path / "custody.READY.json").resolve()),
        "generation_id": projection_store.reference["generation_id"],
        "custody_raw_sha256": h("custody-raw"), "custody_canonical_sha256": h("custody-canonical"),
        "dataset": dict(p["dataset"]), "item_count": len(rows),
        "evidence_span_count": sum(len(row["evidence_spans"]) for row in rows),
        "ready_sha256": h("custody-ready"),
    }
    return _CursorCustodyStore(reference, rows, tmp_path / "custody-store"), rows


class _LargeCursorProjectionStore(rank.CandidateProjectionStore):
    """Disk-backed candidate cursor for the full score_frozen memory seam."""

    def __init__(self, query_count, tmp_path):
        self.query_count = query_count
        self.corpus_id = h("large-corpus")
        self.message_id = h("large-message")
        self.conversation_id = h("large-conversation")
        self.reference = {
            "schema": rank.CANDIDATE_PROJECTION_REFERENCE_SCHEMA,
            "bundle_path": str((tmp_path / "candidate").resolve()),
            "projection_path": "projection.json", "ready_path": "READY.json",
            "generation_id": h(f"large-generation-{query_count}"),
            "projection_raw_sha256": h(f"large-projection-raw-{query_count}"),
            "projection_canonical_sha256": h(f"large-projection-{query_count}"),
            "dataset": {key: h("large-" + key) for key in ("canonical_sha256", "premix_sha256", "revision_sha256", "source_inventory_sha256")},
            "query_count": query_count, "candidate_text_count": 1,
        }
        self.database = tmp_path / "candidate.sqlite3"
        self.connection = sqlite3.connect(self.database)
        self.connection.executescript("CREATE TABLE items(item_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL); CREATE TABLE corpora(corpus_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);")
        corpus = {
            "corpus_id": self.corpus_id, "declared_context_size": 2,
            "actual_conversation_count": 1, "actual_message_count": 1,
            "candidates": [{"message_id": self.message_id, "opaque_conversation_id": self.conversation_id, "conversation_order": 0, "message_order": 0, "corpus_order": 0, "speaker": "user", "text": "target"}],
        }
        self.connection.execute("INSERT INTO corpora VALUES (?, ?)", (self.corpus_id, json.dumps(corpus, separators=(",", ":"))))
        for index in range(query_count):
            item = {"item_id": h(f"large-item-{index}"), "persona_id": h("large-persona"), "query_text": f"large query {index}", "corpus_id": self.corpus_id}
            self.connection.execute("INSERT INTO items VALUES (?, ?)", (item["item_id"], json.dumps(item, separators=(",", ":"))))
        self.connection.commit()
        self._corpus = corpus
        self._run_active = False; self._closed = False

    def iter_items(self):
        for index in range(self.query_count):
            yield {"item_id": h(f"large-item-{index}"), "persona_id": h("large-persona"), "query_text": f"large query {index}", "corpus_id": self.corpus_id}

    def corpus(self, corpus_id):
        assert corpus_id == self.corpus_id
        return self._corpus

    def begin_run(self):
        self._run_active = True

    def end_run(self):
        self._run_active = False


class _LargeCursorCustodyStore(CustodyStore):
    def __init__(self, projection_store, tmp_path):
        self.projection_store = projection_store
        self.directory = tmp_path / "custody-store"
        self.directory.mkdir()
        self.connection = None
        positive_count = projection_store.query_count - projection_store.query_count // 6
        self.reference = {
            "schema": score.CUSTODY_REFERENCE_SCHEMA,
            "bundle_path": str((tmp_path / "candidate-custody").resolve()),
            "candidate_reference": dict(projection_store.reference),
            "custody_path": str((tmp_path / "sealed-custody.json").resolve()),
            "ready_path": str((tmp_path / "custody.READY.json").resolve()),
            "generation_id": projection_store.reference["generation_id"],
            "custody_raw_sha256": h("large-custody-raw"), "custody_canonical_sha256": h("large-custody-canonical"),
            "dataset": dict(projection_store.reference["dataset"]),
            "item_count": projection_store.query_count, "evidence_span_count": positive_count,
            "ready_sha256": h("large-custody-ready"),
        }

    def iter_scoring_items(self):
        groups = tuple(score.UPSTREAM_GROUPS)
        for index in range(self.projection_store.query_count):
            group = groups[index % len(groups)]
            positive = score.UPSTREAM_GROUPS[group] == "positive"
            yield {
                "item_id": h(f"large-item-{index}"), "directory_group": group,
                "evidence_conversation_ids": [self.projection_store.conversation_id] if positive else [],
                "evidence_spans": [{"speaker": "user", "text": "target"}] if positive else [],
            }


class _LargeArtifactReader:
    def __init__(self, value, *, projection_digest, projection):
        self.arm_id = value["arm_id"]; self.artifact_sha256 = value["artifact_sha256"]
        self._query_count = value["query_count"]
        self.reference = None; self._original_refs = []
        if self.arm_id == "original_public_product":
            self._original_refs = [{"build_id": f"large-build-{number}", "index_sha256": h(f"large-index-{number}"), "candidate_reference": value["candidate_reference"]} for number in range(5)]

    def preflight(self, access, db):
        return None

    def original_replicates(self):
        return self._original_refs

    def iter_rows(self):
        corpus = {"corpus_id": h("large-corpus"), "candidates": [{"message_id": h("large-message"), "opaque_conversation_id": h("large-conversation"), "conversation_order": 0, "message_order": 0, "corpus_order": 0, "speaker": "user", "text": "target"}]}
        for index in range(self._query_count):
            item_id = h(f"large-item-{index}"); query = f"large query {index}"
            ids = [h("large-message")]; conversations = [h("large-conversation")]
            common = {"item_id": item_id, "query_sha256": h(query), "candidate_input_sha256": rank._candidate_input(corpus, rank.ORIGINAL_MEMPALACE_SERIALIZER if self.arm_id == "original_public_product" else rank.CURRENT_SERIALIZER), "ranked_message_ids": ids, "retrieved_conversation_ids": conversations}
            if self.arm_id == "original_public_product":
                yield {**common, "confidence": None, "confidence_receipt": None, "replicate_ranked_message_ids": [ids] * 5, "replicate_retrieved_conversation_ids": [conversations] * 5}
            else:
                yield {**common, "confidence": 0.5, "confidence_receipt": {"contract": score.CONFIDENCE_CONTRACT, "top_two_scores": [1.0, 0.0]}}


def _large_manifest(projection_digest, artifact_values):
    row = {
        "schema": score.MANIFEST_SCHEMA, "projection_sha256": projection_digest,
        "protocol_source": rank.PROTOCOL_SOURCE,
        "serializer_contract": {"current": rank.CURRENT_SERIALIZER, "original_public_product": rank.ORIGINAL_MEMPALACE_SERIALIZER},
        "arms": [{"arm_id": value["arm_id"], "ranking_artifact_sha256": value["artifact_sha256"], "confidence_contract": rank.CONFIDENCE_CONTRACT if value["arm_id"] in score.CURRENT_ARMS else None} for value in artifact_values],
        "directory_endpoints": [{"directory_group": group, "endpoint": endpoint} for group, endpoint in score.UPSTREAM_GROUPS.items()],
        "bootstrap": {"seed": 17, "resamples": 2, "percentile_lower": .025, "percentile_upper": .975, "percentile_rule": "linear", "original_replicate_rule": "per_query_arithmetic_mean"},
        "synthetic_test_mode": True, "reference_arm": "strong_raw",
    }
    row["manifest_sha256"] = score.endpoint_manifest_digest(row)
    return row


def _large_score(query_count, tmp_path, monkeypatch):
    tmp_path.mkdir(parents=True, exist_ok=True)
    projection_store = _LargeCursorProjectionStore(query_count, tmp_path)
    custody_store = _LargeCursorCustodyStore(projection_store, tmp_path)
    artifact_values = [{"arm_id": arm_id, "artifact_sha256": h(f"large-artifact-{query_count}-{arm_id}"), "query_count": query_count, "candidate_reference": projection_store.reference} for arm_id in ("original_public_product", *sorted(score.CURRENT_ARMS))]
    endpoint_manifest = _large_manifest(projection_store.reference["projection_canonical_sha256"], artifact_values)
    monkeypatch.setattr(score, "_ArtifactReader", _LargeArtifactReader)
    ledger_path = tmp_path / "mapping-ledger.json"
    tracemalloc.start()
    try:
        report = score.score_frozen(
            projection=projection_store, endpoint_manifest=endpoint_manifest,
            ranking_artifacts=artifact_values, evidence_token_secret=b"x" * 32,
            custody_store=custody_store, formal_live=False, mapping_ledger_path=ledger_path,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        projection_store.connection.close()
    assert report["mapping_ledger"]["count"] == custody_store.reference["evidence_span_count"]
    assert ledger_path.is_file() and ledger_path.with_name("mapping-ledger.READY.json").is_file()
    return peak



def test_full_synthetic_scoring_is_leak_free_and_bootstrap_is_deterministic():
    p, arts, m, c=run(); first=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32); second=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    assert first==second and first["paired_bootstrap"]["overall_positive"]["paired_deltas"]
    assert first["arms"]["original_public_product"]["confidence_separability"]["available"] is False
    assert first["arms"]["strong_raw"]["confidence_separability"]["by_declared_context"]["2"]["average_precision"] >= 0
    assert all("message_id" not in row for row in first["mapping_ledger"])
    assert score.validate_report(first)["report_sha256"]==first["report_sha256"]


def test_cursor_projection_and_custody_consumer_never_calls_legacy_loaders(tmp_path, monkeypatch):
    p, arts, m, c = run()
    projection_store = _CursorProjectionStore(p, tmp_path)
    custody_store, rows = _cursor_custody(p, projection_store, tmp_path)
    from benchmarks import aerp7_convomem_confirmation as confirmation
    def forbidden(*_args, **_kwargs):
        raise AssertionError("legacy custody loader must not be called")
    monkeypatch.setattr(confirmation, "load_sealed_custody", forbidden)
    monkeypatch.setattr(confirmation, "load_custody_for_scoring", forbidden)
    legacy_reader = score._ArtifactReader
    class StoreFixtureReader(legacy_reader):
        def __init__(self, value, *, projection_digest, projection):
            super().__init__(value, projection_digest=projection_digest, projection=p)
    monkeypatch.setattr(score, "_ArtifactReader", StoreFixtureReader)
    ledger_path = tmp_path / "mapping-ledger.json"
    try:
        streamed = score.score_frozen(
            projection=projection_store, endpoint_manifest=m, ranking_artifacts=arts,
            evidence_token_secret=b"x" * 32, custody_store=custody_store,
            formal_live=False, mapping_ledger_path=ledger_path,
        )
        legacy = score.score_frozen(
            projection=p, endpoint_manifest=m, ranking_artifacts=arts,
            evidence_token_secret=b"x" * 32, custody_loader=lambda: c,
        )
        assert isinstance(streamed["mapping_ledger"], dict)
        assert streamed["mapping_ledger"]["schema"] == score.MAPPING_LEDGER_REFERENCE_SCHEMA
        assert streamed["mapping_ledger"]["count"] == sum(len(row["evidence_spans"]) for row in rows)
        assert streamed["arms"] == legacy["arms"]
        assert streamed["paired_bootstrap"] == legacy["paired_bootstrap"]
        assert streamed["ranking_artifact_sha256"] == legacy["ranking_artifact_sha256"]
        assert ledger_path.is_file() and ledger_path.with_name("mapping-ledger.READY.json").is_file()
        assert score.validate_report(streamed)["report_sha256"] == streamed["report_sha256"]
    finally:
        custody_store.connection = None
        projection_store.connection.close()


def test_inline_rehearsal_custody_store_does_not_leave_report_ledger_in_ephemeral_directory(tmp_path):
    p, arts, m, c = run()
    projection_store = _CursorProjectionStore(p, tmp_path)
    custody_store, _rows = _cursor_custody(p, projection_store, tmp_path)
    custody_store.directory.mkdir(parents=True, exist_ok=True)
    try:
        report = score.score_frozen(
            projection=p, endpoint_manifest=m, ranking_artifacts=arts,
            evidence_token_secret=b"x" * 32, custody_loader=lambda: custody_store,
            formal_live=False,
        )
        assert isinstance(report["mapping_ledger"], list)
        custody_store.close()
        # The caller may close the ephemeral CustodyStore before the scientific
        # gate revalidates the report.  The compatibility report must remain
        # self-contained rather than point at the deleted store directory.
        assert score.validate_report(report)["report_sha256"] == report["report_sha256"]
    finally:
        projection_store.connection.close()


def test_evidence_micro_recall_at_10_is_an_explicit_secondary_denominator():
    rows = [
        {"metrics": {"evidence_item_count": 2, "resolved_evidence_item_count": 2, "unresolved_evidence_item_count": 0, "retrieved_evidence_count_at_10": 1, "recall_at_10": .5, "hit_at_10": 1., "all_at_10": 0., "ndcg_at_10": .5, "mrr_at_10": 1.}},
        {"metrics": {"evidence_item_count": 1, "resolved_evidence_item_count": 1, "unresolved_evidence_item_count": 0, "retrieved_evidence_count_at_10": 1, "recall_at_10": 1., "hit_at_10": 1., "all_at_10": 1., "ndcg_at_10": 1., "mrr_at_10": 1.}},
    ]
    summary = score._metric_summary(rows)
    assert summary["evidence_micro_recall_at_10"] == pytest.approx(2 / 3)
    assert summary["recall_at_10"] == pytest.approx(.75)


def test_scoring_crosswalk_maps_unique_case_normalized_annotated_subspan_at_public_call_seam():
    p, _arts, _m, c = run()
    candidate = p["corpora"][0]["candidates"][0]
    candidate["speaker"] = "user"
    candidate["text"] = "Yes. target"
    c["items"][0]["evidence_spans"] = [{"speaker": "User", "text": "target"}]
    c["projection_sha256"] = canonical_sha256(p)
    arts = artifacts(p)
    m = manifest(p, arts)

    report = score.score_frozen(
        projection=p,
        endpoint_manifest=m,
        ranking_artifacts=arts,
        custody_loader=lambda: c,
        evidence_token_secret=b"x" * 32,
    )

    assert report["mapping_ledger"][0]["status"] == "mapped"


def test_formal_crosswalk_uses_upstream_tiers_and_span_to_conversation_position():
    p, _arts, _m, c = run()
    p["items"] = [p["items"][0]]
    p["selection_receipt"]["selected_item_context_count"] = 1
    corpus = p["corpora"][0]
    first = corpus["candidates"][0]
    first["text"] = "exact-" + "a" * 100
    second = copy.deepcopy(first)
    second.update({
        "message_id": h("reverse-partial"), "opaque_conversation_id": h("reverse-partial-conversation"),
        "conversation_order": 1, "message_order": 0, "corpus_order": 11,
        "text": "reverse-" + "b" * 100,
    })
    third = copy.deepcopy(first)
    third.update({
        "message_id": h("fuzzy"), "opaque_conversation_id": h("fuzzy-conversation"),
        "conversation_order": 2, "message_order": 0, "corpus_order": 12,
        "text": "fuzzy-" + "c" * 100,
    })
    corpus["candidates"].extend((second, third))
    corpus["actual_conversation_count"] = 3
    corpus["actual_message_count"] += 2
    c["items"][0]["evidence_conversation_ids"] = [
        first["opaque_conversation_id"], second["opaque_conversation_id"], third["opaque_conversation_id"],
    ]
    c["items"][0]["evidence_spans"] = [
        {"speaker": "User", "text": first["text"]},
        {"speaker": "User", "text": second["text"] + " extension"},
        {"speaker": "User", "text": third["text"][:-1] + "d"},
    ]
    access = score._ProjectionAccess(p)
    with score._ScoringDB() as db:
        score._prepare_projection_items(access, db)
        index = score._prepare_custody_rows(
            access, [c["items"][0]], b"x" * 32, formal_live=True, db=db,
        )
        _group, _conversations, mappings = index.get(c["items"][0]["item_id"])

    assert [(row["status"], row["tier"]) for row in mappings] == [
        ("mapped", "exact_substring"),
        ("mapped", "reverse_partial_80pct"),
        ("mapped", "levenshtein_15pct"),
    ]


def test_crosswalk_leaves_tier_exhaustion_unresolved_and_rejects_wrong_position_formally():
    p, _arts, _m, c = run()
    c["items"][0]["evidence_spans"] = [{"speaker": "User", "text": "no official threshold match"}]
    access = score._ProjectionAccess(p)
    with score._ScoringDB() as db:
        score._prepare_projection_items(access, db)
        index = score._prepare_custody_rows(
            access, c["items"], b"x" * 32, formal_live=False, db=db,
        )
        _group, _conversations, mappings = index.get(c["items"][0]["item_id"])
    assert mappings == [{"status": "unmatched", "reason": "tier_exhausted"}]

    p, _arts, _m, c = run()
    corpus = p["corpora"][0]
    wrong_position = copy.deepcopy(corpus["candidates"][0])
    wrong_position.update({
        "message_id": h("wrong-position"), "opaque_conversation_id": h("wrong-position-conversation"),
        "conversation_order": 1, "message_order": 0, "corpus_order": 11,
        "text": "other",
    })
    corpus["candidates"].append(wrong_position)
    corpus["actual_conversation_count"] = 2
    corpus["actual_message_count"] += 1
    c["items"][0]["evidence_conversation_ids"] = [
        wrong_position["opaque_conversation_id"], corpus["candidates"][0]["opaque_conversation_id"],
    ]
    c["items"][0]["evidence_spans"] = [
        {"speaker": "User", "text": "target"}, {"speaker": "User", "text": "other"},
    ]
    access = score._ProjectionAccess(p)
    with score._ScoringDB() as db:
        score._prepare_projection_items(access, db)
        with pytest.raises(CustodyError, match="scoring_exact_evidence_mapping_incomplete"):
            score._prepare_custody_rows(
                access, c["items"], b"x" * 32, formal_live=True, db=db,
            )


def test_crosswalk_requires_one_ordered_evidence_conversation_per_positive_span():
    p, _arts, _m, c = run()
    c["items"][0]["evidence_spans"].append({"speaker": "User", "text": "target"})
    access = score._ProjectionAccess(p)
    with score._ScoringDB() as db:
        score._prepare_projection_items(access, db)
        with pytest.raises(CustodyError, match="scoring_evidence_conversation_alignment_invalid"):
            score._prepare_custody_rows(
                access, c["items"], b"x" * 32, formal_live=False, db=db,
            )


def test_formal_crosswalk_keeps_unresolved_and_ambiguous_annotated_spans_fail_closed():
    def assert_formal_mapping_rejected(p, rows):
        access = score._ProjectionAccess(p)
        with score._ScoringDB() as db:
            score._prepare_projection_items(access, db)
            with pytest.raises(CustodyError, match="scoring_exact_evidence_mapping_incomplete"):
                score._prepare_custody_rows(
                    access, rows, b"x" * 32, formal_live=True, db=db,
                )

    p, _arts, _m, c = run()
    unresolved = copy.deepcopy(c)["items"]
    unresolved[0]["evidence_spans"] = [{"speaker": "User", "text": "unrelated-evidence-" + "z" * 100}]
    assert_formal_mapping_rejected(p, unresolved)

    p, _arts, _m, c = run()
    duplicate = copy.deepcopy(p["corpora"][0]["candidates"][0])
    duplicate.update({"message_id": h("duplicate-target"), "message_order": 11, "corpus_order": 11})
    p["corpora"][0]["candidates"].append(duplicate)
    p["corpora"][0]["actual_message_count"] += 1
    assert_formal_mapping_rejected(p, c["items"])


def test_public_failures_precede_custody_and_mapping_is_exact_speaker_text_with_cardinality_gates():
    p, arts, m, c=run(); bad=copy.deepcopy(arts); bad[1]["trace_receipt"][0]["ranking_sha256"]=h("tamper")
    with pytest.raises(CustodyError): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=bad,custody_loader=lambda:(_ for _ in ()).throw(AssertionError("must not open custody")),evidence_token_secret=b"x"*32)
    ambiguous=copy.deepcopy(p); ambiguous["corpora"][0]["candidates"][1].update({"speaker":"user","text":"target"})
    # Projection is altered before all public receipts, which still must fail before custody.
    with pytest.raises(CustodyError): score.score_frozen(projection=ambiguous,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:(_ for _ in ()).throw(AssertionError()),evidence_token_secret=b"x"*32)
    c["items"][0]["evidence_spans"]=[]
    with pytest.raises(CustodyError,match="positive_evidence_span_missing"): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    c=custody(p); c["items"][5]["evidence_conversation_ids"]=[p["corpora"][0]["candidates"][0]["opaque_conversation_id"]]
    with pytest.raises(CustodyError,match="abstention_evidence_must_be_empty"): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    ambiguous=copy.deepcopy(p); candidate=copy.deepcopy(ambiguous["corpora"][0]["candidates"][0]); candidate["message_id"]=h("ambiguous-message"); candidate["message_order"]=11; candidate["corpus_order"]=11; ambiguous["corpora"][0]["candidates"].append(candidate); ambiguous["corpora"][0]["actual_message_count"]+=1
    ambiguous_arts=artifacts(ambiguous)
    with pytest.raises(CustodyError, match="scoring_formal_projection_store_required"):
        score.score_frozen(projection=ambiguous,endpoint_manifest=manifest(ambiguous,ambiguous_arts),ranking_artifacts=ambiguous_arts,custody_loader=lambda:custody(ambiguous),evidence_token_secret=b"x"*32, formal_live=True)


def test_ap_ties_missing_stratum_secret_and_report_leak_are_fail_closed():
    assert score._auroc_ap([(0.5,1),(0.5,0)])==(0.5,0.5)
    with pytest.raises(CustodyError): score._auroc_ap([(0.5,1)])
    p,arts,m,c=run()
    with pytest.raises(CustodyError,match="scoring_secret_too_short"): score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"short")
    report=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32); report["mapping_ledger"][0]["message_id"]=h("leak"); report["report_sha256"]=score.report_digest(report)
    with pytest.raises(CustodyError,match="scoring_report_leakage"): score.validate_report(report)


def test_formal_freeze_report_completeness_and_duplicate_span_ndcg_are_fail_closed():
    p,arts,m,c=run(); formal=copy.deepcopy(m); formal["synthetic_test_mode"]=False; formal["bootstrap"]=score.formal_bootstrap(h("sealed-protocol")); formal["reference_arm"]="six_view_secondary"; formal["manifest_sha256"]=score.endpoint_manifest_digest(formal)
    assert score.validate_endpoint_manifest(formal,projection_sha256=canonical_sha256(p))["reference_arm"]=="six_view_secondary"
    for mutate in (
        lambda value: value["arms"].pop(),
        lambda value: value["bootstrap"].__setitem__("seed_derivation","manual"),
        lambda value: value.__setitem__("reference_arm","static_p5"),
    ):
        bad=copy.deepcopy(formal); mutate(bad); bad["manifest_sha256"]=score.endpoint_manifest_digest(bad)
        with pytest.raises(CustodyError,match="formal_manifest_freeze_invalid"): score.validate_endpoint_manifest(bad,projection_sha256=canonical_sha256(p))
    report=score.score_frozen(projection=p,endpoint_manifest=m,ranking_artifacts=arts,custody_loader=lambda:c,evidence_token_secret=b"x"*32)
    for mutate in (
        lambda value: value["arms"]["strong_raw"]["positive"].pop("derived_hard_changing_and_implicit"),
        lambda value: value["paired_bootstrap"].pop("static_p5_vs_strong_raw_abstention_confidence"),
        lambda value: value["arms"]["original_public_product"].pop("original_replicates"),
    ):
        bad=copy.deepcopy(report); mutate(bad); bad["report_sha256"]=score.report_digest(bad)
        with pytest.raises(CustodyError): score.validate_report(bad)
    metrics=score.question_metrics(["gold"],[{"status":"mapped","message_id":"gold"},{"status":"mapped","message_id":"gold"}])
    assert metrics["recall_at_10"]==1.0 and metrics["ndcg_at_10"]==1.0
    bad=copy.deepcopy(report); bad["ranking_artifact_sha256"]["strong_raw"]=h("different-valid-digest"); bad["report_sha256"]=score.report_digest(bad)
    with pytest.raises(CustodyError,match="scoring_report_artifact_manifest_binding_invalid"): score.validate_report(bad)


def test_streaming_confidence_cursor_is_numerically_equal_to_legacy_pair_scorer():
    pairs = [(0.2, 1), (0.2, 0), (0.7, 1), (0.1, 0), (0.7, 0), (0.9, 1)]
    with score._ScoringDB() as spool:
        spool.connection.executemany("INSERT INTO confidence VALUES (?, ?, ?, ?, ?)", [("strong_raw", "p", 2, confidence, label) for confidence, label in pairs])
        spool.connection.commit()
        assert score._stream_auroc_ap(spool.connection, "strong_raw", "p", 2) == score._auroc_ap(pairs)


def test_ready_bound_ranking_reader_streams_without_retaining_payload_rows(tmp_path):
    """Exercise the persisted-artifact seam rather than the inline legacy path."""
    rows = [{"item_id": h(f"stream-item-{index}"), "padding": "x" * 1024} for index in range(20_000)]
    artifact_path = tmp_path / "ranking.json"
    ready_path = tmp_path / "ranking.READY.json"
    projection_digest = h("projection")
    serializer_receipt = rank.CURRENT_SERIALIZER
    serializer_digest = rank._digest(serializer_receipt)
    input_rows = [
        {"item_id": item_id, "corpus_id": h("corpus"), "candidate_input_sha256": h(f"candidate-{item_id}")}
        for item_id in sorted(row["item_id"] for row in rows)
    ]
    input_path = tmp_path / "input-receipt-items.json"
    input_path.write_bytes(rank._bytes(input_rows))
    input_payload_digest = hashlib.sha256(input_path.read_bytes()).hexdigest()
    input_legacy_digest = rank._input_receipt_legacy_fields(
        projection_sha256=projection_digest,
        serializer_sha256=serializer_digest,
        item_corpus_set_sha256=input_payload_digest,
        item_corpora_path=input_path,
    )
    input_receipt = {
        "schema": rank.INPUT_RECEIPT_REFERENCE_SCHEMA,
        "item_corpora_path": input_path.name,
        "item_corpus_set_sha256": input_payload_digest,
        "item_count": len(input_rows),
        "projection_sha256": projection_digest,
        "serializer_sha256": serializer_digest,
        "legacy_input_sha256": input_legacy_digest,
    }
    model_receipt = {
        "encoder_identity": "synthetic-encoder", "encoder_semantics": "deterministic",
        "files": [{"path_role": "weights", "sha256": h("weights"), "bytes": 2}],
    }
    code_receipt = {"head": h("head")[:40], "tree": h("tree")[:40], "diff_digest": h("diff"), "dirty_policy": "clean_required"}
    artifact_path.write_bytes(json.dumps({"rankings": rows}, separators=(",", ":")).encode("utf-8"))
    artifact_digest = h("artifact")
    ready = {
        "schema": rank.RANKING_ARTIFACT_READY_SCHEMA,
        "arm_id": "strong_raw",
        "generation_id": "generation",
        "projection_sha256": projection_digest,
        "payload_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "artifact_sha256": artifact_digest,
        "ready_sha256": "",
    }
    ready["ready_sha256"] = canonical_sha256({key: value for key, value in ready.items() if key != "ready_sha256"})
    ready_path.write_bytes(json.dumps(ready, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    reference = {
        "schema": rank.RANKING_ARTIFACT_REFERENCE_SCHEMA,
        "artifact_path": str(artifact_path.resolve()),
        "ready_path": str(ready_path.resolve()),
        "arm_id": "strong_raw",
        "projection_sha256": projection_digest,
        "generation_id": "generation",
        "payload_sha256": ready["payload_sha256"],
        "artifact_sha256": artifact_digest,
        "ready_sha256": ready["ready_sha256"],
        "method_receipt": rank._arm_method("strong_raw"), "serializer_receipt": serializer_receipt,
        "input_receipt": input_receipt, "input_sha256": input_legacy_digest,
        "model_receipt": model_receipt, "model_sha256": rank._digest(model_receipt),
        "source_receipt": rank.PROTOCOL_SOURCE, "source_commit_sha256": rank._digest(rank.PROTOCOL_SOURCE),
        "serializer_sha256": serializer_digest, "code_receipt": code_receipt, "code_sha256": rank._digest(code_receipt), "trace_sha256": h("trace"),
    }
    reader = score._ArtifactReader(reference, projection_digest=projection_digest, projection=None)
    tracemalloc.start()
    try:
        count = sum(1 for _ in reader.iter_rows())
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert count == len(rows)
    assert peak < 8_000_000


@pytest.mark.performance
def test_full_score_frozen_cursor_memory_does_not_scale_with_100k_to_200k_queries(tmp_path, monkeypatch):
    """The bounded-memory assertion crosses the complete scorer seam."""
    # Keep this stress lane focused on the complete consumer loop while the
    # small-fixture tests cover the full four-arm registry.  Both confidence
    # arms remain present because the preregistered non-regression gate needs
    # the raw/P5 pair.
    monkeypatch.setattr(score, "CURRENT_ARMS", {"strong_raw", "static_p5"})
    peak_100k = _large_score(100_000, tmp_path / "one", monkeypatch)
    peak_200k = _large_score(200_000, tmp_path / "two", monkeypatch)
    assert peak_200k <= peak_100k + 16 * 1024 * 1024, (
        f"score_frozen_peak_100k={peak_100k} score_frozen_peak_200k={peak_200k}"
    )
