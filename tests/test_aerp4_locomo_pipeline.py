from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from benchmarks import aerp4_locomo_custody as custody
from benchmarks import aerp4_locomo_pipeline as pipeline
from benchmarks import aerp4_locomo_paired_receipts as paired
from benchmarks import aerp4_raw_anchored_gate as gate
from mempalace_rpg.retrieval import AuthorizedRetrievalCandidate, RawAnchoredP5Policy, SixViewRanker


def test_stream_input_rehashes_and_rejects_drift_without_read_bytes(tmp_path) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(b'{"x":1}')
    pinned = pipeline.StreamInput.pin(path, hashlib.sha256(path.read_bytes()).hexdigest())
    path.write_bytes(b'{"x":2}')
    with pytest.raises(RuntimeError, match="TOCTOU"):
        pinned.verify_unchanged()


def test_stream_input_refuses_to_materialize_large_json(tmp_path) -> None:
    path = tmp_path / "large.json"
    path.write_bytes(b"{" + b'"x":"' + b"a" * 64 + b'"}')
    pinned = pipeline.StreamInput.pin(path, hashlib.sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError, match="oversized"):
        pinned.json(maximum_bytes=8)


def test_streaming_dependency_fails_explicitly_when_not_installed() -> None:
    try:
        pipeline._ijson()
    except RuntimeError as exc:
        assert "ijson>=3.2,<4" in str(exc)
    else:
        pytest.importorskip("ijson")


def test_external_output_refuses_clobber_and_repository_target(tmp_path) -> None:
    existing = tmp_path / "already.json"
    existing.write_text("x", encoding="utf-8")
    with pytest.raises(FileExistsError):
        pipeline._external_new(existing, pipeline.Path.cwd())
    with pytest.raises(ValueError, match="outside repository"):
        pipeline._external_new(pipeline.Path.cwd() / "not-allowed.json", pipeline.Path.cwd())


def test_rss_observability_fields_are_honest_and_named() -> None:
    stats = pipeline._stats(0.0, pipeline._rss_bytes(), [])
    assert stats["rss_semantics"] == "start_end_best_effort_process_working_set"
    assert "rss_bytes_peak_observed" in stats


def test_rss_sampler_cap_is_fail_closed_and_joins_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "_rss_bytes", lambda: pipeline.FROZEN_MAX_RSS_BYTES + 1)
    sampler = pipeline._RssSampler(pipeline.FROZEN_MAX_RSS_BYTES)
    with pytest.raises(RuntimeError, match="frozen memory threshold"):
        with sampler:
            pass
    assert not sampler._thread.is_alive()


def test_rss_sampler_background_failure_is_synchronous_and_leaves_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    values = iter((1, RuntimeError("probe failed"), 1))
    def broken_probe():
        value = next(values)
        if isinstance(value, BaseException): raise value
        return value
    monkeypatch.setattr(pipeline, "_rss_bytes", broken_probe)
    sampler = pipeline._RssSampler(pipeline.FROZEN_MAX_RSS_BYTES)
    with pytest.raises(RuntimeError, match="RSS sampler failed"):
        with sampler:
            sampler._stop.wait(.05)
    assert not sampler._thread.is_alive()


def test_starting_rss_cap_fails_before_sanitize_scans_or_creates_temp(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact = tmp_path / "artifact.json"; artifact.write_bytes(b"{}")
    pin = pipeline.StreamInput.pin(artifact, hashlib.sha256(artifact.read_bytes()).hexdigest())
    output = tmp_path / "blocked"
    monkeypatch.setattr(pipeline, "_rss_bytes", lambda: pipeline.FROZEN_MAX_RSS_BYTES + 1)
    monkeypatch.setattr(pipeline, "_source_git_receipts", lambda *_args: (_ for _ in ()).throw(AssertionError("artifact scan happened")))
    monkeypatch.setattr(pipeline.tempfile, "mkdtemp", lambda **_kwargs: (_ for _ in ()).throw(AssertionError("temporary directory created")))
    with pytest.raises(RuntimeError, match="frozen memory threshold"):
        pipeline.sanitize_stream(artifact=pin, output=output, repo=pipeline.Path.cwd())
    assert not output.exists() and not list(tmp_path.glob(".blocked.*"))


def test_compact_row_accepts_real_ijson_fcd_trace_from_production_ranker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Encoder:
        identity = "aerp4-test-encoder"
        def encode_query(self, _query): return [1.0, .5]
        def encode_passages(self, texts):
            return [[float((sum(map(ord, text)) % 17) + 1), float((len(text) % 7) + 1)] for text in texts]

    monkeypatch.setattr(paired.fcd2, "EXPECTED_POOL", 12)
    candidates = [
        AuthorizedRetrievalCandidate(
            source_event_id=f"event-{index}", source_scene_id="scene", raw_text=f"raw {index}", observation=f"observation {index}",
            checkpoint_key=f"checkpoint-{index // 3}", policy_tuple=("canonical", "public", "main", "active", None, None, None, "[]"),
            chronological_order_key=(index, f"event-{index}"), ranking_key=f"dialog-{index}",
        )
        for index in range(12)
    ]
    ranked = SixViewRanker(Encoder(), diagnostic_ledger=True).rank(query="real ijson float trace", candidates=candidates)
    source = tmp_path / "trace.json"
    source.write_bytes(pipeline._canonical({"traces": [{"retrieval_ranking": ranked.trace, "numeric_probe": .25}]}))
    with source.open("rb") as handle:
        parsed = next(pipeline._ijson().items(handle, "traces.item", use_float=True))
    assert type(parsed["numeric_probe"]) is float
    row = pipeline._compact_row({"item_id": "item", "conversation_id": "conversation"}, parsed)
    assert len(row["candidates"]) == 12
    assert all(set(candidate["ranks"]) == set(pipeline._VIEWS) for candidate in row["candidates"])


def test_streaming_pipeline_e2e_and_prepublication_failures_leave_no_final_dir(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def token(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
    git = {"git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": token("status"), "commit_diff_sha256": token("diff"), "commit_diff_bytes": 0}
    monkeypatch.setattr(pipeline.gate, "_git_state", lambda _repo: dict(git))
    monkeypatch.setattr(pipeline.custody, "_validate_guardrail_manifest", lambda _value: token("guardrail"))

    def compact(question, _trace):
        item = question["item_id"]; conversation = question["conversation_id"]
        candidates = []
        for rank in range(1, 12):
            ranking = token(f"ranking:{item}:{rank}")
            candidates.append({"ranking_token": ranking, "evidence_token": hashlib.sha256(ranking.encode()).hexdigest(), "ranks": {view: rank for view in pipeline._VIEWS}})
        return {"item_token": paired._token("aerp4:item", item), "group_token": paired._token("aerp4:group", conversation), "campaign_token": paired._token("aerp4:campaign", conversation), "query_sha256": token("query:" + item), "input_sha256": token("input:" + item), "encoder_identity": "synthetic", "view_digests": {view: token(view + item) for view in pipeline._VIEWS}, "candidates": candidates}
    monkeypatch.setattr(pipeline, "_compact_row", compact)

    questions = []; traces = {}
    for index in range(1986):
        item = f"item-{index}"; conversation = f"conversation-{index % 10}"
        questions.append({"item_id": item, "conversation_id": conversation, "official_exact": {"resolved_dialog_ids": [] if index < 4 else [f"evidence-{index}"], "unresolved_evidence_item_count": 0, "evidence_item_count": 0 if index < 4 else 1}, "query": "must-not-be-copied"})
        traces[item] = {"irrelevant": True}
    artifact = tmp_path / "artifact.json"; artifact.write_text(json.dumps({"questions": questions, "product_traces": traces, "git_state_before": git, "git_state_after": git}), encoding="utf-8")
    labels = tmp_path / "labels.json"; labels.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    guardrail = tmp_path / "guardrail.json"; guardrail.write_text("{}", encoding="utf-8")
    pin = lambda path: pipeline.StreamInput.pin(path, hashlib.sha256(path.read_bytes()).hexdigest())
    repo = pipeline.Path.cwd(); sanitize_dir = tmp_path / "sanitize"
    pipeline.sanitize_stream(artifact=pin(artifact), output=sanitize_dir, repo=repo)
    manifest = sanitize_dir / "manifest.json"; manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    assert sum(row["item_count"] for row in manifest_value["shards"]) == 1986
    assert all((sanitize_dir / row["path"]).exists() for row in manifest_value["shards"])
    assert set(manifest_value["source_artifact_git_receipts"]) == {"git_state_before", "git_state_after"}
    for shard in manifest_value["shards"]:
        shard_rows = [json.loads(line) for line in (sanitize_dir / shard["path"]).read_text(encoding="utf-8").splitlines()]
        crosswalk = [{key: row[key] for key in ("item_token", "group_token", "campaign_token", "query_sha256", "input_sha256")} for row in shard_rows]
        assert hashlib.sha256((sanitize_dir / shard["path"]).read_bytes()).hexdigest() == shard["sha256"]
        assert pipeline.gate._crosswalk(crosswalk) == shard["crosswalk_sha256"]
    slots = {"train_prefreeze": tmp_path / "paired-train" / "ranking-freeze.json", "dev_prefreeze": tmp_path / "paired-dev" / "ranking-freeze.json", "tau_select": tmp_path / "tau-select.json", "dev_eval": tmp_path / "dev-eval.json"}
    custody_dir = tmp_path / "custody"
    pipeline.custody_stream(manifest_input=pin(manifest), labels_input=pin(labels), guardrail_input=pin(guardrail), output=custody_dir, repo=repo, output_slots=slots)
    bundle = custody_dir / "manifest.json"; study = custody_dir / "study.json"
    for partition in ("train", "dev"):
        paired_dir = tmp_path / ("paired-" + partition)
        pipeline.paired_stream(manifest_input=pin(manifest), custody_input=pin(bundle), study_input=pin(study), partition=partition, output=paired_dir, repo=repo)
        freeze = json.loads((paired_dir / "ranking-freeze.json").read_text(encoding="utf-8"))
        assert freeze["publication"]["input_receipts"][1]["path_sha256"] == pipeline.gate._sha(str(paired_dir / "paired-input.json"))

    for stage, call in (("sanitize", lambda out: pipeline.sanitize_stream(artifact=pin(artifact), output=out, repo=repo)), ("custody", lambda out: pipeline.custody_stream(manifest_input=pin(manifest), labels_input=pin(labels), guardrail_input=pin(guardrail), output=out, repo=repo, output_slots={name: tmp_path / ("failure-" + name) for name in slots})), ("paired", lambda out: pipeline.paired_stream(manifest_input=pin(manifest), custody_input=pin(bundle), study_input=pin(study), partition="train", output=out, repo=repo))):
        original = pipeline._publish
        monkeypatch.setattr(pipeline, "_publish", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected publish failure")))
        if stage == "paired":
            monkeypatch.setattr(pipeline.gate, "_slot", lambda *_args, **_kwargs: None)
        failed = tmp_path / ("failed-" + stage)
        with pytest.raises(RuntimeError, match="injected"):
            call(failed)
        assert not failed.exists()
        monkeypatch.setattr(pipeline, "_publish", original)

    # Every sanitizer failure point is outside the final directory from the
    # instant its temporary shard directory is created.
    original_open, original_compact, original_dumps = pipeline.Path.open, pipeline._compact_row, pipeline.json.dumps
    for fault in ("handle-open", "compact", "json", "publish"):
        failed = tmp_path / ("sanitize-" + fault)
        with monkeypatch.context() as scoped:
            if fault == "handle-open":
                def fail_train_open(path, *args, **kwargs):
                    if path.name == "train.jsonl" and "x" in args:
                        raise RuntimeError("injected handle-open failure")
                    return original_open(path, *args, **kwargs)
                scoped.setattr(pipeline.Path, "open", fail_train_open)
            elif fault == "compact":
                scoped.setattr(pipeline, "_compact_row", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected compact failure")))
            elif fault == "json":
                def fail_row_json(value, *args, **kwargs):
                    if isinstance(value, dict) and "item_token" in value:
                        raise RuntimeError("injected JSON failure")
                    return original_dumps(value, *args, **kwargs)
                scoped.setattr(pipeline.json, "dumps", fail_row_json)
            else:
                scoped.setattr(pipeline, "_publish", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected publish failure")))
            with pytest.raises(RuntimeError, match="injected"):
                pipeline.sanitize_stream(artifact=pin(artifact), output=failed, repo=repo)
        assert not failed.exists()
        assert not list(tmp_path.glob("." + failed.name + ".*"))

    # The cap remains live through report canonicalization and atomic publish;
    # a peak observed in that window must prevent the final rename.
    crossed = threading.Event(); original_canonical = pipeline._canonical
    with monkeypatch.context() as scoped:
        def cap_during_report(value):
            if isinstance(value, dict) and value.get("schema") == pipeline.MANIFEST_SCHEMA:
                crossed.set(); time.sleep(.06)
            return original_canonical(value)
        scoped.setattr(pipeline, "_canonical", cap_during_report)
        scoped.setattr(pipeline, "_rss_bytes", lambda: pipeline.FROZEN_MAX_RSS_BYTES + 1 if crossed.is_set() else 1)
        final = tmp_path / "serialize-cap"
        with pytest.raises(RuntimeError, match="frozen memory threshold"):
            pipeline.sanitize_stream(artifact=pin(artifact), output=final, repo=repo)
        assert not final.exists() and not list(tmp_path.glob("." + final.name + ".*"))

    for schema, final, call in (
        (pipeline.CUSTODY_SCHEMA, tmp_path / "custody-serialize-cap", lambda: pipeline.custody_stream(manifest_input=pin(manifest), labels_input=pin(labels), guardrail_input=pin(guardrail), output=tmp_path / "custody-serialize-cap", repo=repo, output_slots={name: tmp_path / ("cap-slot-" + name) for name in slots})),
        (gate.RANKING_FREEZE_SCHEMA, tmp_path / "paired-serialize-cap", lambda: pipeline.paired_stream(manifest_input=pin(manifest), custody_input=pin(bundle), study_input=pin(study), partition="train", output=tmp_path / "paired-serialize-cap", repo=repo)),
    ):
        crossed = threading.Event()
        with monkeypatch.context() as scoped:
            def cap_during_stage_report(value, *, expected=schema):
                if isinstance(value, dict) and value.get("schema") == expected:
                    crossed.set(); time.sleep(.06)
                return original_canonical(value)
            scoped.setattr(pipeline, "_canonical", cap_during_stage_report)
            scoped.setattr(pipeline, "_rss_bytes", lambda: pipeline.FROZEN_MAX_RSS_BYTES + 1 if crossed.is_set() else 1)
            if schema == gate.RANKING_FREEZE_SCHEMA:
                scoped.setattr(pipeline.gate, "_slot", lambda *_args, **_kwargs: None)
            with pytest.raises(RuntimeError, match="frozen memory threshold"):
                call()
        assert not final.exists() and not list(tmp_path.glob("." + final.name + ".*"))


def test_formal_stream_chain_selects_non_degenerate_tau_and_publishes_go(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The four registered slots admit one complete custody-to-dev proof."""
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    git = {"git_head": "a" * 40, "git_tree": "b" * 40, "git_dirty": False, "worktree_status_sha256": digest("status"), "commit_diff_sha256": digest("diff"), "commit_diff_bytes": 0}
    monkeypatch.setattr(gate, "_git_state", lambda _repo: dict(git))

    def compact(question, _trace):
        index = int(question["item_id"].rsplit("-", 1)[1]); style = (index // 10) % 2
        candidates = []
        for position in range(12):
            dialog = f"dialog-{index}-{position}"
            ranking = paired._token("aerp4:ranking", digest(dialog))
            reverse = 12 - position
            ranks = {
                "raw_bm25": position + 1,
                "raw_dense": position + 1 if style == 0 else reverse,
                "observation_bm25": reverse,
                "observation_dense": reverse,
                "checkpoint_dense": position + 1,
                "combo_dense": reverse,
            }
            candidates.append({"ranking_token": ranking, "evidence_token": hashlib.sha256(ranking.encode()).hexdigest(), "ranks": ranks, "dialog": dialog})
        row = {"item_token": paired._token("aerp4:item", question["item_id"]), "group_token": paired._token("aerp4:group", question["conversation_id"]), "campaign_token": paired._token("aerp4:campaign", question["conversation_id"]), "query_sha256": digest("query:" + question["item_id"]), "input_sha256": digest("input:" + question["item_id"]), "encoder_identity": "synthetic", "view_digests": {view: digest(view + question["item_id"]) for view in pipeline._VIEWS}, "candidates": [{key: value for key, value in candidate.items() if key != "dialog"} for candidate in candidates]}
        return row

    # Establish the two arm-specific label targets using the same product
    # adapter that paired_stream replays, not a copied score formula.
    choices: dict[str, str] = {}
    for style, probe_index in ((0, 0), (1, 10)):
        probe = compact({"item_id": f"probe-{probe_index}", "conversation_id": "probe"}, {})
        item = pipeline._compact_to_paired_item(probe)
        raw = paired._trace(item, policy=RawAnchoredP5Policy(float("inf")), route="raw")
        p5 = paired._trace(item, policy=RawAnchoredP5Policy(float("-inf")), route="p5")
        raw_ids = [entry["source_event_id"] for entry in raw["selected"][:10]]
        p5_ids = [entry["source_event_id"] for entry in p5["selected"][:10]]
        assert raw_ids != p5_ids, (style, raw_ids, p5_ids)
        raw_only = next(token for token in raw_ids if token not in p5_ids)
        p5_only = next(token for token in p5_ids if token not in raw_ids)
        # Both styles must have an arm distinction.  The eventual label below
        # alternates them so train selection has a meaningful threshold choice.
        choices[str(style)] = raw_ids.index(raw_only) if style == 0 else p5_ids.index(p5_only)

    monkeypatch.setattr(pipeline, "_compact_row", compact)
    questions, traces = [], {}
    for index in range(1986):
        item = f"item-{index}"; conversation = f"conversation-{index % 10}"; style = (index // 10) % 2
        sample = compact({"item_id": item, "conversation_id": conversation}, {})
        ranked = pipeline._compact_to_paired_item(sample)
        arm = paired._trace(ranked, policy=RawAnchoredP5Policy(float("inf")), route="raw") if style == 0 else paired._trace(ranked, policy=RawAnchoredP5Policy(float("-inf")), route="p5")
        selected_token = arm["selected"][choices[str(style)]]["source_event_id"]
        dialog_by_token = {candidate["ranking_token"]: f"dialog-{index}-{position}" for position, candidate in enumerate(sample["candidates"])}
        questions.append({"item_id": item, "conversation_id": conversation, "official_exact": {"resolved_dialog_ids": [] if index < 4 else [dialog_by_token[selected_token]], "unresolved_evidence_item_count": 0, "evidence_item_count": 0 if index < 4 else 1}})
        traces[item] = {"synthetic": True}
    artifact = tmp_path / "artifact.json"; artifact.write_text(json.dumps({"questions": questions, "product_traces": traces, "git_state_before": git, "git_state_after": git}), encoding="utf-8")
    labels = tmp_path / "labels.json"; labels.write_text(json.dumps({"questions": questions}), encoding="utf-8")
    guardrail = tmp_path / "guardrail.json"; guardrail.write_bytes(pipeline._canonical(custody.build_guardrail_source_manifest()))
    pin = lambda path: pipeline.StreamInput.pin(path, hashlib.sha256(path.read_bytes()).hexdigest())
    slots = {"train_prefreeze": tmp_path / "paired-train" / "ranking-freeze.json", "dev_prefreeze": tmp_path / "paired-dev" / "ranking-freeze.json", "tau_select": tmp_path / "tau-select.json", "dev_eval": tmp_path / "dev-eval.json"}
    sanitized = tmp_path / "sanitize"; pipeline.sanitize_stream(artifact=pin(artifact), output=sanitized, repo=pipeline.Path.cwd())
    manifest = sanitized / "manifest.json"; custody_dir = tmp_path / "custody"
    pipeline.custody_stream(manifest_input=pin(manifest), labels_input=pin(labels), guardrail_input=pin(guardrail), output=custody_dir, repo=pipeline.Path.cwd(), output_slots=slots)
    bundle, study_path = custody_dir / "manifest.json", custody_dir / "study.json"
    frozen_study = json.loads(study_path.read_text(encoding="utf-8"))
    formal_guardrail = json.loads((custody_dir / "guardrail-evidence.json").read_text(encoding="utf-8"))
    assert frozen_study["guardrail_custodian"]["source_artifact_sha256"] == hashlib.sha256(guardrail.read_bytes()).hexdigest()
    assert formal_guardrail["publication"]["input_receipts"][1] == {"path_sha256": gate._sha(str(guardrail)), "sha256": hashlib.sha256(guardrail.read_bytes()).hexdigest()}
    for partition in ("train", "dev"):
        pipeline.paired_stream(manifest_input=pin(manifest), custody_input=pin(bundle), study_input=pin(study_path), partition=partition, output=tmp_path / ("paired-" + partition), repo=pipeline.Path.cwd())
    train_rankings, dev_rankings = tmp_path / "paired-train" / "ranking-freeze.json", tmp_path / "paired-dev" / "ranking-freeze.json"
    train_labels, dev_labels, guardrails = custody_dir / "labels-train.json", custody_dir / "labels-dev.json", custody_dir / "guardrail-evidence.json"
    study = gate.BoundInput.load(study_path, hashlib.sha256(study_path.read_bytes()).hexdigest())
    train_ranking = gate.BoundInput.load(train_rankings, hashlib.sha256(train_rankings.read_bytes()).hexdigest()); train_label = gate.BoundInput.load(train_labels, hashlib.sha256(train_labels.read_bytes()).hexdigest())
    selected = gate.select_and_freeze(study.json("study"), train_ranking.json("train rankings"), train_label.json("train labels"))
    assert selected["status"] == "complete" and selected["selected_tau"] not in {float("inf").hex(), float("-inf").hex()}
    gate.publish_bound_report(report=selected, output=slots["tau_select"], inputs=[study, train_ranking, train_label], repo=pipeline.Path.cwd())
    policy = gate.BoundInput.load(slots["tau_select"], hashlib.sha256(slots["tau_select"].read_bytes()).hexdigest())
    dev_ranking = gate.BoundInput.load(dev_rankings, hashlib.sha256(dev_rankings.read_bytes()).hexdigest()); dev_label = gate.BoundInput.load(dev_labels, hashlib.sha256(dev_labels.read_bytes()).hexdigest()); guardrail_evidence = gate.BoundInput.load(guardrails, hashlib.sha256(guardrails.read_bytes()).hexdigest())
    evaluated = gate.evaluate_dev_once(study.json("study"), dev_ranking.json("dev rankings"), dev_label.json("dev labels"), policy.json("policy"), guardrail_evidence.json("guardrails"))
    assert evaluated["go"] is True, json.dumps({"metrics": evaluated["metrics"], "gated_minus": evaluated["gated_minus"], "routes": evaluated["routes"], "go_gates": evaluated["go_gates"]}, indent=2)
    gate.publish_bound_report(report=evaluated, output=slots["dev_eval"], inputs=[study, dev_ranking, dev_label, policy, guardrail_evidence], repo=pipeline.Path.cwd())
    assert all(path.exists() for path in slots.values())
    assert json.loads(manifest.read_text(encoding="utf-8"))["metrics"]["memory_threshold_bytes"] == pipeline.FROZEN_MAX_RSS_BYTES
    assert json.loads(bundle.read_text(encoding="utf-8"))["metrics"]["memory_threshold_bytes"] == pipeline.FROZEN_MAX_RSS_BYTES
    for partition in ("train", "dev"):
        paired_input = tmp_path / ("paired-" + partition) / "paired-input.json"
        assert json.loads(paired_input.read_text(encoding="utf-8"))["metrics"]["memory_threshold_bytes"] == pipeline.FROZEN_MAX_RSS_BYTES

    # The immutable cap is enforced before each stage can create a final path.
    monkeypatch.setattr(pipeline, "_rss_bytes", lambda: pipeline.FROZEN_MAX_RSS_BYTES + 1)
    cap_slots = {name: tmp_path / ("cap-" + name) for name in slots}
    for final, call in (
        (tmp_path / "cap-sanitize", lambda: pipeline.sanitize_stream(artifact=pin(artifact), output=tmp_path / "cap-sanitize", repo=pipeline.Path.cwd())),
        (tmp_path / "cap-custody", lambda: pipeline.custody_stream(manifest_input=pin(manifest), labels_input=pin(labels), guardrail_input=pin(guardrail), output=tmp_path / "cap-custody", repo=pipeline.Path.cwd(), output_slots=cap_slots)),
        (tmp_path / "cap-paired", lambda: pipeline.paired_stream(manifest_input=pin(manifest), custody_input=pin(bundle), study_input=pin(study_path), partition="train", output=tmp_path / "cap-paired", repo=pipeline.Path.cwd())),
    ):
        with pytest.raises(RuntimeError, match="frozen memory threshold"):
            call()
        assert not final.exists()
