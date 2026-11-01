"""Offline protocol/authorization fault fixtures; NOT semantic experiments.

The small injected encoder below only exercises SixViewRanker's real fixed P5
execution path. Its scores must never be reported as MiniLM retrieval results.
"""
from copy import deepcopy
import hashlib
from io import StringIO
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from mempalace_rpg.evidence_protocol import EvidenceError, strict_json_loads, timestamp
from mempalace_rpg.evidence_store import EvidenceStore
from mempalace_rpg.read_server import serve
from mempalace_rpg.retrieval import FixedP5Policy, RankingResult, SixViewRanker
from mempalace_rpg.runtime_factory import MODEL_FILES, create_encoder, model_tree_hash

NOW = "2026-10-01T12:00:00Z"
PAST = "2026-09-30T12:00:00Z"
FUTURE = "2026-10-02T12:00:00Z"
BOUND = {"campaignId": "campaign", "actorId": "alice", "branchId": "leaf",
         "checkpointId": "checkpoint", "storyTime": NOW, "asOf": NOW, "epoch": 2,
         "configHash": "a" * 64, "sourceIndexVersion": "immutable-index-2"}
BRANCHES = [{"branchId": "root", "parentId": None, "forkSourceCommit": None},
            {"branchId": "child", "parentId": "root", "forkSourceCommit": 10},
            {"branchId": "leaf", "parentId": "child", "forkSourceCommit": 4},
            {"branchId": "sibling", "parentId": "root", "forkSourceCommit": 10}]


class OfflineFaultEncoder:
    identity = "offline-fault-fixture-not-semantic-results"

    def __init__(self):
        self.query_calls = 0
        self.passage_texts = []

    def encode_query(self, query):
        self.query_calls += 1
        return [1.0, 0.0, 0.0, 0.0]

    def encode_passages(self, texts):
        self.passage_texts.extend(texts)
        return [[1.0, 0.0, 0.0, 0.0] for text in texts]


def grant(**changes):
    result = {"actorId": "alice", "acquiredAt": PAST, "revealedAt": None,
              "originBranch": "root", "sourceCommit": 1, "reviewStatus": "approved"}
    result.update(changes)
    return result


def event(identifier="event", text="The gate is open, unless the alarm is armed.", **changes):
    result = {"campaignId": "campaign", "sourceId": identifier, "sourceVersion": "v1",
              "originBranch": "root", "sourceCommit": 1, "sceneId": "scene-1",
              "occurredAt": PAST, "text": text,
              "rawSha256": hashlib.sha256(text.encode()).hexdigest(), "grants": [grant()]}
    result.update(changes)
    return result


def publish(path, events, **changes):
    payload = {"schema": "memory-evidence-projection-v1", "watermark": deepcopy(BOUND),
               "branches": deepcopy(BRANCHES), "events": deepcopy(events)}
    payload.update(changes)
    payload["watermark"]["sourceIndexVersion"] = hashlib.sha256(json.dumps(
        {"branches": payload["branches"], "events": payload["events"]},
        ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def store(tmp_path, events):
    encoder = OfflineFaultEncoder()
    path = publish(tmp_path / "evidence.json", events)
    return EvidenceStore(path, encoder=encoder), encoder, path


def open_store(value):
    return value.open_snapshot({"binding": deepcopy(value._watermark)})["snapshotHandle"]


def search(value, handle, **changes):
    return value.search({"snapshotHandle": handle, "query": "gate open alarm", **changes})


def test_actual_fixed_p5_ranks_full_authorized_universe_before_scope(tmp_path):
    events = [event(f"event-{index:02}", sceneId=f"scene-{index:02}") for index in range(15)]
    service, encoder, _path = store(tmp_path, events)
    handle = open_store(service)
    seen = []
    original_rank = service._ranker.rank

    def spy(**kwargs):
        seen.append(kwargs["candidates"])
        return original_rank(**kwargs)

    service._ranker.rank = spy
    full = search(service, handle)
    scoped = search(service, handle, scopeSceneIds=["scene-03", "scene-13"])
    assert isinstance(service._ranker, SixViewRanker)
    assert isinstance(service._ranker.routing_policy, FixedP5Policy)
    assert len(seen) == 2 and all(len(candidates) == 15 for candidates in seen)
    assert len(full["candidates"]) == 10
    assert [(row["sourceId"], row["nativeRank"]) for row in scoped["candidates"]] == [("event-03", 4)]
    assert full["trace"] == scoped["trace"]
    assert encoder.query_calls == 2


@pytest.mark.parametrize("changes", [
    {"grants": []}, {"grants": [grant(actorId="bob")]},
    {"grants": [grant(reviewStatus="needs-review")]},
    {"grants": [grant(reviewStatus="rejected")]},
    {"grants": [grant(acquiredAt=FUTURE)]}, {"grants": [grant(revealedAt=FUTURE)]},
    {"grants": [grant(originBranch="sibling")]},
    {"grants": [grant(sourceCommit=5)]},
    {"occurredAt": FUTURE}, {"originBranch": "sibling"}, {"sourceCommit": 5},
])
def test_denied_events_never_reach_encoder_search_or_catalogue(tmp_path, changes):
    service, encoder, _path = store(tmp_path, [event("allowed"), event("denied", text="SECRET denied", sceneId="secret-scene", **changes)])
    handle = open_store(service)
    result = search(service, handle)
    assert [row["sourceId"] for row in result["candidates"]] == ["allowed"]
    assert result["trace"]["rankedTop10SourceIds"] == ["allowed"]
    assert all("SECRET denied" not in text for text in encoder.passage_texts)
    assert [node["sceneId"] for node in service.catalogue({"snapshotHandle": handle})["nodes"]] == ["scene-1"]


def test_empty_universe_is_explicit_no_history_and_never_ranks(tmp_path):
    service, encoder, _path = store(tmp_path, [event(grants=[grant(actorId="bob")])])
    handle = open_store(service)
    service._ranker.rank = lambda **_kwargs: pytest.fail("empty U must not rank")
    result = search(service, handle)
    assert result["status"] == "no-history" and result["candidates"] == []
    assert result["trace"]["inputHash"] is None
    assert encoder.query_calls == 0 and not encoder.passage_texts
    assert service.catalogue({"snapshotHandle": handle}) == {"nodes": [], "nextCursor": None}


@pytest.mark.parametrize("key,new_value", [("actorId", "unknown"), ("campaignId", "other"),
    ("branchId", "root"), ("checkpointId", "other"), ("storyTime", PAST), ("asOf", PAST),
    ("epoch", 3), ("configHash", "b" * 64), ("sourceIndexVersion", "stale")])
def test_every_binding_field_matches_exact_watermark(tmp_path, key, new_value):
    service, _encoder, _path = store(tmp_path, [event()])
    bound = {**service._watermark, key: new_value}
    with pytest.raises(EvidenceError, match="binding") as error:
        service.open_snapshot({"binding": bound})
    assert error.value.kind == "binding-mismatch"


def test_full_utf8_read_preserves_tail_conditions_and_only_current_approved_grants(tmp_path):
    text = "门🚪e\u0301\r\n" + "The gate is open. " * 30 + "But it will NOT open unless the alarm is disabled."
    service, _encoder, _path = store(tmp_path, [event(text=text, grants=[grant(), grant(actorId="bob", acquiredAt=FUTURE), grant(reviewStatus="rejected")])])
    handle = open_store(service)
    candidate = search(service, handle)["candidates"][0]
    assert "NOT open" not in candidate["preview"]
    result = service.read_event({"snapshotHandle": handle, "eventHandle": candidate["eventHandle"]})
    assert result["span"]["text"] == text
    assert result["span"]["startByte"] == 0
    assert result["span"]["endByteExclusive"] == len(text.encode("utf-8"))
    assert result["span"]["rawSha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert result["projection"]["grants"] == [grant()]
    assert "retrieval" not in result["projection"]


def test_leases_and_handles_are_opaque_scoped_and_close_revokes(tmp_path):
    service, _encoder, _path = store(tmp_path, [event()])
    first, second = open_store(service), open_store(service)
    candidate = search(service, first)["candidates"][0]
    assert candidate["eventHandle"] != "event"
    assert first != second
    with pytest.raises(EvidenceError, match="handle"):
        service.read_event({"snapshotHandle": second, "eventHandle": candidate["eventHandle"]})
    with pytest.raises(EvidenceError, match="handle"):
        service.read_event({"snapshotHandle": first, "eventHandle": "event"})
    assert service.close_snapshot({"snapshotHandle": first}) == {"closed": True}
    with pytest.raises(EvidenceError, match="snapshot"):
        search(service, first)
    assert search(service, second)["candidates"]


def test_scene_window_has_three_scenes_and_individual_authorization_and_no_chaining(tmp_path):
    # 12 sources ensures two new neighbor handles lie beyond the native top10.
    events = [event(f"event-{index:02}", sceneId=f"scene-{index:02}", occurredAt=f"2026-09-{index + 1:02}T12:00:00Z") for index in range(12)]
    events.append(event("private", text="SECRET neighbor", sceneId="scene-10", grants=[grant(actorId="bob")]))
    service, _encoder, _path = store(tmp_path, events)
    handle = open_store(service)
    candidates = search(service, handle)["candidates"]
    seed = candidates[-1]
    first_read = service.read_event({"snapshotHandle": handle, "eventHandle": seed["eventHandle"]})
    result = service.read_scene_window({"snapshotHandle": handle, "eventHandle": seed["eventHandle"]})
    assert len(result["scenes"]) == 3
    assert "SECRET" not in json.dumps(result)
    seed_in_window = next(row for scene in result["scenes"] for row in scene["events"]
                          if row["span"]["eventHandle"] == seed["eventHandle"])
    assert seed_in_window == first_read
    new_handle = result["scenes"][-1]["events"][0]["span"]["eventHandle"]
    assert new_handle not in {candidate["eventHandle"] for candidate in candidates}
    with pytest.raises(EvidenceError, match="search seed"):
        service.read_scene_window({"snapshotHandle": handle, "eventHandle": new_handle})
    assert service.read_event({"snapshotHandle": handle, "eventHandle": new_handle})["span"]["text"]


def test_catalogue_paging_is_mechanical_authorized_and_cursor_is_lease_scoped(tmp_path):
    events = [event(f"event-{index}", sceneId=f"scene-{index}") for index in range(15)]
    service, encoder, _path = store(tmp_path, events)
    first, second = open_store(service), open_store(service)
    page = service.catalogue({"snapshotHandle": first, "limit": 12})
    assert len(page["nodes"]) == 12
    assert page["nextCursor"]
    assert len(service.catalogue({"snapshotHandle": first, "cursor": page["nextCursor"]})["nodes"]) == 3
    with pytest.raises(EvidenceError, match="cursor"):
        service.catalogue({"snapshotHandle": second, "cursor": page["nextCursor"]})
    assert encoder.query_calls == 0 and not encoder.passage_texts


def test_projection_is_frozen_on_load_and_service_never_writes_source(tmp_path):
    service, _encoder, path = store(tmp_path, [event()])
    before = path.read_bytes()
    first = open_store(service)
    search(service, first)
    assert path.read_bytes() == before
    publish(path, [event(text="Changed in later publication.")])
    second = open_store(service)
    for handle in (first, second):
        candidate = search(service, handle)["candidates"][0]
        assert candidate["preview"].startswith("The gate")


def test_read_budget_charges_utf8_once_for_span_projection_and_repeated_reads_again(tmp_path):
    text = "门" * 4000  # 12000 exact UTF-8 bytes, not 4000 character units.
    service, _encoder, _path = store(tmp_path, [event(text=text)])
    handle = open_store(service)
    candidate = search(service, handle)["candidates"][0]
    params = {"snapshotHandle": handle, "eventHandle": candidate["eventHandle"]}
    first = service.read_event(params)
    second = service.read_event(params)
    assert first["span"]["spanHandle"] == second["span"]["spanHandle"]
    assert service._leases[handle].read_bytes == 24000
    with pytest.raises(EvidenceError) as error:
        service.read_event(params)
    assert error.value.code == -32010 and error.value.kind == "read-budget-exceeded"
    assert service._leases[handle].read_bytes == 24000


def test_oversized_first_event_fails_before_plaintext_response(tmp_path):
    service, _encoder, _path = store(tmp_path, [event(text="x" * 24001)])
    handle = open_store(service)
    candidate = search(service, handle)["candidates"][0]
    with pytest.raises(EvidenceError) as error:
        service.read_event({"snapshotHandle": handle, "eventHandle": candidate["eventHandle"]})
    assert error.value.kind == "read-budget-exceeded"
    assert service._leases[handle].read_bytes == 0
    assert not service._leases[handle].read_events


def test_window_budget_rejects_whole_group_without_new_handles_or_charges(tmp_path):
    events = [event(f"event-{index:02}", text="x" * 9000, sceneId=f"scene-{index:02}") for index in range(11)]
    service, _encoder, _path = store(tmp_path, events)
    handle = open_store(service)
    seed = search(service, handle)["candidates"][-1]
    lease = service._leases[handle]
    before_handles = dict(lease.handles)
    with pytest.raises(EvidenceError) as error:
        service.read_scene_window({"snapshotHandle": handle, "eventHandle": seed["eventHandle"]})
    assert error.value.kind == "read-budget-exceeded"
    assert lease.handles == before_handles
    assert lease.read_bytes == 0 and not lease.read_events


def test_window_distinct_budget_is_twelve_and_rejection_is_atomic(tmp_path):
    # Thirteen individually authorized events in one scene exceed the lease's
    # distinct-source ceiling even though their total text is well under 24KB.
    service, _encoder, _path = store(tmp_path, [event(f"event-{index:02}") for index in range(13)])
    handle = open_store(service)
    seed = search(service, handle)["candidates"][0]
    with pytest.raises(EvidenceError) as error:
        service.read_scene_window({"snapshotHandle": handle, "eventHandle": seed["eventHandle"]})
    assert error.value.kind == "read-budget-exceeded"
    assert service._leases[handle].read_bytes == 0
    assert not service._leases[handle].read_events


def test_native_metadata_mapping_uses_original_text_and_supplied_kernel_views(tmp_path):
    item = event(text="Exact original event: do not omit the negation.")
    item["retrieval"] = {"observation": "summary=a short observation", "checkpointKey": "checkpoint-native",
                         "policyTuple": [None] * 8, "chronologicalOrderKey": [2, "opaque-rank-key"], "rankingKey": "opaque-rank-key"}
    service, _encoder, _path = store(tmp_path, [item])
    handle = open_store(service)
    lease = service._leases[handle]
    candidate = service._ranking_candidate(lease, lease.events["event"])
    assert candidate.raw_text == item["text"]
    assert candidate.observation == item["retrieval"]["observation"]
    assert candidate.checkpoint_key == "checkpoint-native"
    assert candidate.chronological_order_key == (2, "opaque-rank-key")


@pytest.mark.parametrize("mutate", [
    lambda payload: payload.update(extra=True),
    lambda payload: payload["watermark"].update(epoch=True),
    lambda payload: payload["events"][0].update(rawSha256="0" * 64),
    lambda payload: payload["events"][0].update(campaignId="other"),
    lambda payload: payload["events"][0].update(text="\ud800"),
    lambda payload: payload["events"][0].update(extra=True),
    lambda payload: payload["events"][0]["grants"][0].update(reviewStatus="unknown"),
    lambda payload: payload["branches"][0].update(parentId="leaf", forkSourceCommit=1),
])
def test_malformed_projection_fails_fast(tmp_path, mutate):
    path = publish(tmp_path / "bad.json", [event()])
    payload = json.loads(path.read_text(encoding="utf-8"))
    mutate(payload)
    path.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    with pytest.raises(EvidenceError):
        EvidenceStore(path, encoder=OfflineFaultEncoder())


def test_resealed_source_text_with_unchanged_source_index_is_rejected(tmp_path):
    path = publish(tmp_path / "attacked.json", [event()])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][0]["text"] = "Attacker changed the original source."
    payload["events"][0]["rawSha256"] = hashlib.sha256(payload["events"][0]["text"].encode()).hexdigest()
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(EvidenceError) as error:
        EvidenceStore(path, encoder=OfflineFaultEncoder())
    assert error.value.kind == "source-index-mismatch"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for JS/Python wire hash parity fixture")
def test_source_index_hash_matches_js_stringify_with_unicode_controls_and_safe_integer(tmp_path):
    text = "门🚪e\u0301\u2028\u2029\x00\b\f\n\r\t\x1b quote\" slash/ backslash\\"
    path = publish(tmp_path / "cross-language.json", [event(text=text, sourceCommit=9007199254740991)])
    payload = path.read_text(encoding="utf-8")
    script = "const fs=require('node:fs');const c=require('node:crypto');const p=JSON.parse(fs.readFileSync(0,'utf8'));process.stdout.write(c.createHash('sha256').update(JSON.stringify({branches:p.branches,events:p.events}),'utf8').digest('hex'));"
    result = subprocess.run([shutil.which("node"), "-e", script], input=payload,
                            text=True, capture_output=True, encoding="utf-8", timeout=30, check=True)
    expected = json.loads(payload)["watermark"]["sourceIndexVersion"]
    assert result.stdout == expected
    EvidenceStore(path, encoder=OfflineFaultEncoder())


@pytest.mark.parametrize("method,params", [
    ("capabilities", {"unexpected": True}), ("search", {"snapshotHandle": "x", "query": "q", "topK": 50}),
    ("catalogue", {"snapshotHandle": "x", "limit": True}),
    ("read_event", {"snapshotHandle": "x", "eventHandle": "e", "startByte": 1}),
])
def test_unknown_or_bad_rpc_params_are_explicit_errors(tmp_path, method, params):
    service, _encoder, _path = store(tmp_path, [event()])
    with pytest.raises(EvidenceError):
        service.dispatch(method, params)


def test_ranker_corruption_never_crosses_service_boundary(tmp_path):
    service, _encoder, _path = store(tmp_path, [event()])
    handle = open_store(service)
    service._ranker.rank = lambda **kwargs: RankingResult(["not-authorized"], {"not-authorized": 1.0}, {})
    with pytest.raises(RuntimeError, match="unauthorized"):
        search(service, handle)


def test_stdio_stdout_is_only_one_line_json_rpc_and_errors_are_observable(tmp_path, caplog):
    service, _encoder, _path = store(tmp_path, [event()])
    source = StringIO('not-json\n{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}\n'
                      '{"jsonrpc":"2.0","id":2,"method":"missing","params":{}}\n'
                      '{"jsonrpc":"2.0","id":3,"method":"capabilities","params":{"extra":1}}\n')
    output, diagnostics = StringIO(), StringIO()
    original_dispatch = service.dispatch

    def noisy(method, params):
        print("dependency diagnostic")
        return original_dispatch(method, params)

    service.dispatch = noisy
    serve(service, input_stream=source, output_stream=output, diagnostic_stream=diagnostics)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(responses) == 4
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"]["retrieval"]["weights"]["checkpointDense"] == 0
    assert responses[2]["error"]["code"] == -32601
    assert responses[3]["error"]["code"] == -32602
    assert "dependency diagnostic" in diagnostics.getvalue()
    assert "rejected" in caplog.text
    assert not service._leases


def test_internal_stdio_failure_is_logged_and_returned_not_swallowed(tmp_path, caplog):
    service, _encoder, _path = store(tmp_path, [event()])
    service.dispatch = lambda *args: (_ for _ in ()).throw(RuntimeError("injected failure"))
    output = StringIO()
    serve(service, input_stream=StringIO('{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}\n'),
          output_stream=output, diagnostic_stream=StringIO())
    assert json.loads(output.getvalue())["error"]["code"] == -32603
    assert "injected failure" in caplog.text


def test_invalid_response_serialization_returns_jsonrpc_error(tmp_path, caplog):
    service, _encoder, _path = store(tmp_path, [event()])
    service.dispatch = lambda *args: {"invalid": object()}
    output = StringIO()
    serve(service, input_stream=StringIO('{"jsonrpc":"2.0","id":1,"method":"capabilities","params":{}}\n'),
          output_stream=output, diagnostic_stream=StringIO())
    assert json.loads(output.getvalue())["error"]["code"] == -32603
    assert "not JSON serializable" in caplog.text


def test_close_failure_is_propagated_not_swallowed(tmp_path):
    service, _encoder, _path = store(tmp_path, [event()])
    service.close = lambda: (_ for _ in ()).throw(RuntimeError("close failed"))
    with pytest.raises(RuntimeError, match="close failed"):
        serve(service, input_stream=StringIO(), output_stream=StringIO(), diagnostic_stream=StringIO())


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'])
def test_json_duplicates_and_nonfinite_constants_fail(raw):
    with pytest.raises(ValueError):
        strict_json_loads(raw)


@pytest.mark.parametrize("value", ["2026-02-30T12:00:00Z", "2026-10-01T12:00:00", "2026-10-01T12:00:00+01:99"])
def test_invalid_or_naive_times_fail(value):
    with pytest.raises(EvidenceError):
        timestamp(value, "fixture")


def test_runtime_requires_complete_local_tree_and_explicit_matching_manifest(tmp_path):
    model = tmp_path / "model"
    (model / "onnx").mkdir(parents=True)
    with pytest.raises(ValueError, match="missing"):
        create_encoder(model_dir=model, expected_tree_sha256="a" * 64)
    for filename in MODEL_FILES:
        (model / "onnx" / filename).write_bytes(b"offline-invalid-model")
    digest = model_tree_hash(model)
    assert len(digest) == 64
    with pytest.raises(ValueError, match="mismatch"):
        create_encoder(model_dir=model, expected_tree_sha256="a" * 64)
    with pytest.raises(ValueError, match="explicit"):
        create_encoder(model_dir=model, expected_tree_sha256="")


def test_real_cli_missing_model_fails_without_stdout_or_network_fallback(tmp_path):
    projection = publish(tmp_path / "evidence.json", [event()])
    result = subprocess.run([sys.executable, "-m", "mempalace_rpg.read_server", "--projection", str(projection),
                             "--model-dir", str(tmp_path / "missing-model"), "--model-tree-sha256", "a" * 64],
                            text=True, capture_output=True, encoding="utf-8", timeout=30)
    assert result.returncode == 1 and result.stdout == ""
    assert "no runtime fallback is permitted" in result.stderr
