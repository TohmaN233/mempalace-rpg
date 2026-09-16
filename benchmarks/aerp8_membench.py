"""Fail-closed AERP-8 MemBench harness; only isolated formal source-builder accepts source paths."""
from __future__ import annotations
import argparse, hashlib, hmac, importlib, json, math, os, random, shutil, subprocess, sys, time, uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from benchmarks.aerp7_convomem_rank import FixedRawPolicy
from benchmarks import aerp7_original_core as original_core
from benchmarks import aerp7_original_product as original_product
from mempalace_rpg.retrieval import AuthorizedRetrievalCandidate, FixedP5Policy, SixViewRanker, structured_observation

SOURCE_COMMIT="f66d8d1028d3f68627d00f77a967b93fbb8694b6"; SOURCE_TREE="2944102605501f03327b80d1e3471af1e18e2fd7"
SOURCE_RECEIPT={"dataset":"MemBench","official_commit":SOURCE_COMMIT,"official_tree":SOURCE_TREE,"source_shape":"data2test.question_type.scenario.trajectory.v1"}
ORIGINAL_COMMIT="87e6f38377b4bee0666374b05df6e14ffd154245"; ORIGINAL_TREE="639b2a849816fd4853072920405822824464e9c6"; ORIGINAL_MODEL_TREE="76217893f057779cee29c903aa24444154ad0da7645853f1041fd970cca275a0"
BUNDLED_ORIGINAL_ROOT=Path(__file__).resolve().parents[1]
CANDIDATE_SCHEMA="aerp8-membench-candidate-projection-v2"; CUSTODY_SCHEMA="aerp8-membench-sealed-custody-v2"; FORMAL_CUSTODY_SCHEMA="aerp8-membench-sealed-custody-v3"; NORMALIZATION_SCHEMA="aerp8-membench-normalized-retrieval-v1"; ARTIFACT_SCHEMA="aerp8-membench-ranking-artifact-v2"; PUBLIC_RESULTS_SCHEMA="aerp8-membench-public-results-v2"; SYNTHETIC_PUBLIC_RESULTS_SCHEMA="aerp8-membench-public-results-synthetic-v1"; RELEASE_SCHEMA="aerp8-membench-release-v3"; SYNTHETIC_RELEASE_SCHEMA="aerp8-membench-synthetic-release-v1"; REPORT_SCHEMA="aerp8-membench-retrieval-report-v3"; SYNTHETIC_REPORT_SCHEMA="aerp8-membench-retrieval-report-synthetic-v1"; READY_SCHEMA="aerp8-membench-ready-v1"; FORMAL_PREFLIGHT_SCHEMA="aerp8-membench-formal-preflight-v1"; ORIGINAL_WORKER_CONFIG_SCHEMA="aerp8-membench-original-worker-config-v2"; CURRENT_WORKER_CONFIG_SCHEMA="aerp8-membench-current-worker-config-v1"; THRESHOLD_PROTOCOL_SCHEMA="aerp8-membench-threshold-protocol-v1"; CUSTODIAN_CONFIG_SCHEMA="aerp8-membench-formal-custodian-config-v1"; SOURCE_MANIFEST_SCHEMA="aerp8-membench-formal-source-manifest-v2"; SOURCE_RECEIPT_SCHEMA="aerp8-membench-formal-source-receipt-v2"; SOURCE_BUILDER_CONFIG_SCHEMA="aerp8-membench-formal-source-builder-config-v2"; SOURCE_BUILDER_AUTH_SCHEMA="aerp8-membench-formal-source-builder-authorization-v1"; CURRENT_CHECKPOINT_SCHEMA="aerp8-membench-current-checkpoint-v2"; ORIGINAL_EXECUTION_POLICY_SCHEMA="aerp8-membench-original-execution-policy-v1"
ARMS=("strong_raw","static_p5","six_view_secondary","original_public_product"); CURRENT_ARMS=ARMS[:3]
CURRENT_ROLES={"raw":"strong_raw","p5_primary":"static_p5","p5_repeat":"static_p5","six":"six_view_secondary"}
FORMAL_SOURCE_ROLES=frozenset({"participation_reflective","participation_factual","observation_reflective","observation_factual"})
# This reviewed inventory is the only formal data2test namespace.  It is not a
# discovery rule and cannot be extended by a manifest or acquisition receipt.
FROZEN_PROFILE_INVENTORY: Mapping[str, Mapping[str, Any]] = {
    "0": {"context_label": "0-10k", "paths": {
        "participation_reflective": "data/data2test/FirstAgentDataHighLevel_multiple_0.json",
        "participation_factual": "data/data2test/FirstAgentDataLowLevel_multiple_0.json",
        "observation_reflective": "data/data2test/ThirdAgentDataHighLevel_multiple_0.json",
        "observation_factual": "data/data2test/ThirdAgentDataLowLevel_multiple_0.json",
    }},
    "100": {"context_label": "100k", "paths": {
        "participation_reflective": "data/data2test/FirstAgentDataHighLevel_multiple_100.json",
        "participation_factual": "data/data2test/FirstAgentDataLowLevel_multiple_100.json",
        "observation_reflective": "data/data2test/ThirdAgentDataHighLevel_multiple_100.json",
        "observation_factual": "data/data2test/ThirdAgentDataLowLevel_multiple_100.json",
    }},
}
_FORBIDDEN=frozenset({"target_step_id","ground_truth","choices","question_type","scenario","source_path","source_file_role","raw_tid","tid","crosswalk","strata"})
class MemBenchError(RuntimeError): pass
def _bytes(v:Any)->bytes: return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
def digest(v:Any)->str: return hashlib.sha256(_bytes(v)).hexdigest()
def _opaque(secret:bytes,v:Any)->str:
    if not isinstance(secret,bytes) or len(secret)<32: raise MemBenchError("membench_opacity_secret_invalid")
    return hmac.new(secret,_bytes(v),hashlib.sha256).hexdigest()
def _text(v:Any,code:str)->str:
    if not isinstance(v,str) or not v.strip(): raise MemBenchError(code)
    return v
def _formal_source_receipt(value:Any)->dict[str,Any]:
    required={"schema","dataset","official_commit","official_tree","source_shape","source_manifest_sha256","selected_profiles_sha256","source_file_bytes_aggregate_sha256","acquisition_sha256","profile_role_file_map_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=SOURCE_RECEIPT_SCHEMA or value.get("dataset")!="MemBench" or value.get("official_commit")!=SOURCE_COMMIT or value.get("official_tree")!=SOURCE_TREE or value.get("source_shape")!=SOURCE_RECEIPT["source_shape"] or any(not isinstance(value.get(key),str) or len(value[key])!=64 or any(char not in "0123456789abcdef" for char in value[key]) for key in ("source_manifest_sha256","selected_profiles_sha256","source_file_bytes_aggregate_sha256","acquisition_sha256","profile_role_file_map_sha256")): raise MemBenchError("membench_formal_source_receipt_invalid")
    return dict(value)
def _source_receipt(value:Any,*,formal_only:bool=False)->dict[str,Any]:
    if value==SOURCE_RECEIPT:
        if formal_only: raise MemBenchError("membench_formal_synthetic_source_receipt")
        return dict(SOURCE_RECEIPT)
    return _formal_source_receipt(value)
def _messages(v:Any)->list[str]:
    # Official data2test/list loader presents this exact list[str] shape.
    if not isinstance(v,list) or not v or not all(isinstance(x,str) and x.strip() for x in v): raise MemBenchError("membench_message_list_invalid")
    return list(v)
def _targets(v:Any,count:int)->list[int]:
    rows=v if isinstance(v,list) else [v]
    if not rows or any(isinstance(x,bool) or not isinstance(x,int) or x<0 or x>=count for x in rows): raise MemBenchError("membench_target_step_invalid")
    return sorted(set(rows))
def _walk(v:Any)->None:
    if isinstance(v,Mapping):
        if _FORBIDDEN & set(v): raise MemBenchError("membench_candidate_label_leakage")
        for x in v.values(): _walk(x)
    elif isinstance(v,list):
        for x in v: _walk(x)

def validate_candidate_projection(value:Any)->dict[str,Any]:
    if not isinstance(value,Mapping) or set(value)!={"schema","source_receipt","items","projection_sha256"}: raise MemBenchError("membench_candidate_projection_invalid")
    row=dict(value)
    if row["schema"]!=CANDIDATE_SCHEMA or not isinstance(row["items"],list) or not row["items"]: raise MemBenchError("membench_candidate_projection_invalid")
    _source_receipt(row["source_receipt"])
    _walk({"items":row["items"]}); seen=set()
    formal=isinstance(row["source_receipt"],Mapping) and row["source_receipt"].get("schema")==SOURCE_RECEIPT_SCHEMA
    for item in row["items"]:
        expected={"item_id","group_id","query_text","query_time","candidates"} | ({"profile_binding_id"} if formal else set())
        if not isinstance(item,Mapping) or set(item)!=expected: raise MemBenchError("membench_candidate_item_invalid")
        for key in ("item_id","group_id","query_text"): _text(item[key],"membench_candidate_item_invalid")
        if formal: _text(item["profile_binding_id"],"membench_candidate_item_invalid")
        if item["item_id"] in seen: raise MemBenchError("membench_candidate_item_duplicate")
        seen.add(item["item_id"])
        if item["query_time"] is not None and not isinstance(item["query_time"],str): raise MemBenchError("membench_candidate_item_invalid")
        candidates=item["candidates"]
        if not isinstance(candidates,list) or not candidates or [x.get("order") if isinstance(x,Mapping) else None for x in candidates]!=list(range(len(candidates))): raise MemBenchError("membench_candidate_order_invalid")
        if any(not isinstance(x,Mapping) or set(x)!={"candidate_id","order","text"} or not isinstance(x["candidate_id"],str) or not x["candidate_id"] or not isinstance(x["text"],str) or not x["text"].strip() for x in candidates): raise MemBenchError("membench_candidate_item_invalid")
    if row["projection_sha256"]!=digest({k:v for k,v in row.items() if k!="projection_sha256"}): raise MemBenchError("membench_candidate_projection_digest_invalid")
    return row
def validate_custody(value:Any)->dict[str,Any]:
    if not isinstance(value,Mapping) or set(value)!={"schema","source_receipt","records","custody_sha256"}: raise MemBenchError("membench_custody_invalid")
    row=dict(value); required={"item_id","group_id","source_file_role","raw_tid","question_type","scenario","target_step_id","ground_truth","choices","strata","source_locator","crosswalk","gold_candidate_ids"}
    formal=row.get("schema")==FORMAL_CUSTODY_SCHEMA
    if formal: required|={"profile_id","profile_binding_id"}
    if row["schema"] not in {CUSTODY_SCHEMA,FORMAL_CUSTODY_SCHEMA} or not isinstance(row["records"],list) or not row["records"] or any(not isinstance(x,Mapping) or set(x)!=required or not isinstance(x["gold_candidate_ids"],list) or not x["gold_candidate_ids"] for x in row["records"]): raise MemBenchError("membench_custody_invalid")
    _source_receipt(row["source_receipt"],formal_only=formal)
    if row["custody_sha256"]!=digest({k:v for k,v in row.items() if k!="custody_sha256"}): raise MemBenchError("membench_custody_digest_invalid")
    return row

def build_bundles(*,source_role:str,source:Mapping[str,Any],opacity_secret:bytes)->tuple[dict[str,Any],dict[str,Any]]:
    role=_text(source_role,"membench_source_role_invalid")
    if not isinstance(source,Mapping) or not source: raise MemBenchError("membench_source_shape_invalid")
    items=[]; records=[]; expected={"question","time","choices","ground_truth","target_step_id"}
    for question_type,scenarios in sorted(source.items()):
      _text(question_type,"membench_source_shape_invalid")
      if not isinstance(scenarios,Mapping): raise MemBenchError("membench_source_shape_invalid")
      for scenario,trajectories in sorted(scenarios.items()):
       _text(scenario,"membench_source_shape_invalid")
       if not isinstance(trajectories,list): raise MemBenchError("membench_source_shape_invalid")
       for trajectory in trajectories:
        if not isinstance(trajectory,Mapping) or set(trajectory)!={"tid","message_list","QA"}: raise MemBenchError("membench_trajectory_invalid")
        tid=_text(trajectory["tid"],"membench_trajectory_invalid"); messages=_messages(trajectory["message_list"]); qa=trajectory["QA"]
        if not isinstance(qa,Mapping) or set(qa)!=expected: raise MemBenchError("membench_qa_invalid")
        targets=_targets(qa["target_step_id"],len(messages)); group=_opaque(opacity_secret,{"source_file_role":role,"tid":tid})
        candidates=[{"candidate_id":_opaque(opacity_secret,{"group_id":group,"step":i}),"order":i,"text":text} for i,text in enumerate(messages)]
        item_id=_opaque(opacity_secret,{"group_id":group,"qa_index":0})
        items.append({"item_id":item_id,"group_id":group,"query_text":_text(qa["question"],"membench_qa_invalid"),"query_time":qa["time"] if isinstance(qa["time"],str) else None,"candidates":candidates})
        records.append({"item_id":item_id,"group_id":group,"source_file_role":role,"raw_tid":tid,"question_type":question_type,"scenario":scenario,"target_step_id":qa["target_step_id"],"ground_truth":qa["ground_truth"],"choices":qa["choices"],"strata":{"question_type":question_type,"scenario":scenario},"source_locator":{"source_file_role":role,"tid":tid,"qa_index":0},"crosswalk":{"candidate_ids_by_step":[x["candidate_id"] for x in candidates]},"gold_candidate_ids":[candidates[i]["candidate_id"] for i in targets]})
    candidate={"schema":CANDIDATE_SCHEMA,"source_receipt":SOURCE_RECEIPT,"items":items}; candidate["projection_sha256"]=digest(candidate)
    custody={"schema":CUSTODY_SCHEMA,"source_receipt":SOURCE_RECEIPT,"records":records}; custody["custody_sha256"]=digest(custody)
    return validate_candidate_projection(candidate),validate_custody(custody)

def _hex256(value:Any)->bool:
    return isinstance(value,str) and len(value)==64 and all(char in "0123456789abcdef" for char in value)

def validate_source_manifest(value:Any)->dict[str,Any]:
    """Validate the pre-acquisition inventory, never caller-selected file paths.

    MemBench's data2test files are external payloads: they are not Git blobs (or
    LFS objects) in the pinned code tree.  Therefore their byte identities live
    in the separately authorized acquisition receipt, while this manifest pins
    only the official checkout and exact published inventory-relative names.
    """
    required={"schema","official_commit","official_tree","selected_profiles","profiles","manifest_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=SOURCE_MANIFEST_SCHEMA or value.get("official_commit")!=SOURCE_COMMIT or value.get("official_tree")!=SOURCE_TREE or value.get("selected_profiles")!=["0","100"] or not isinstance(value.get("profiles"),list) or value.get("manifest_sha256")!=digest({key:child for key,child in value.items() if key!="manifest_sha256"}): raise MemBenchError("membench_source_manifest_invalid")
    required_profile={"profile_id","context_label","source_role","inventory_relative_path"}; pairs=set()
    for profile in value["profiles"]:
        if not isinstance(profile,Mapping) or set(profile)!=required_profile: raise MemBenchError("membench_source_manifest_invalid")
        profile_id=profile.get("profile_id"); role=profile.get("source_role")
        expected=FROZEN_PROFILE_INVENTORY.get(profile_id) if isinstance(profile_id,str) else None
        if expected is None or role not in FORMAL_SOURCE_ROLES or profile.get("context_label")!=expected["context_label"] or profile.get("inventory_relative_path")!=expected["paths"][role] or Path(str(profile["inventory_relative_path"])).is_absolute() or ".." in Path(str(profile["inventory_relative_path"])).parts or (profile_id,role) in pairs: raise MemBenchError("membench_source_manifest_invalid")
        pairs.add((profile_id,role))
    expected_pairs={(profile_id,role) for profile_id in ("0","100") for role in FORMAL_SOURCE_ROLES}
    if pairs!=expected_pairs: raise MemBenchError("membench_source_manifest_coverage_invalid")
    return dict(value)

def _git_state(root:Path)->Mapping[str,Any]:
    try: return original_product.v1.git_state(root)
    except (OSError, subprocess.SubprocessError) as exc: raise MemBenchError("membench_source_checkout_receipt_invalid") from exc

def validate_acquisition_receipt(value:Any,*,manifest:Mapping[str,Any],operator_secret:bytes)->dict[str,Any]:
    """Bind external, non-Git payload bytes before *any* JSON is parsed."""
    required={"schema","manifest_sha256","source_checkout","data_root","files","authorization_nonce","acquisition_sha256","acquisition_hmac"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!="aerp8-membench-external-acquisition-v1" or value.get("manifest_sha256")!=manifest["manifest_sha256"] or not isinstance(value.get("source_checkout"),str) or not isinstance(value.get("data_root"),str) or not isinstance(value.get("authorization_nonce"),str) or len(value["authorization_nonce"])<32 or not isinstance(value.get("files"),list): raise MemBenchError("membench_acquisition_receipt_invalid")
    checkout=Path(value["source_checkout"]); data_root=Path(value["data_root"])
    if not checkout.is_absolute() or checkout.is_symlink() or not checkout.is_dir() or not data_root.is_absolute() or data_root.is_symlink() or not data_root.is_dir(): raise MemBenchError("membench_acquisition_receipt_invalid")
    state=_git_state(checkout)
    if state.get("git_dirty") is not False or state.get("git_head")!=SOURCE_COMMIT or state.get("git_tree")!=SOURCE_TREE: raise MemBenchError("membench_source_checkout_pin_drift")
    required_file={"profile_id","source_role","inventory_relative_path","source_file_sha256","archive_sha256"}; expected={(row["profile_id"],row["source_role"]):row for row in manifest["profiles"]}; seen=set()
    for row in value["files"]:
        if not isinstance(row,Mapping) or set(row)!=required_file or (row.get("profile_id"),row.get("source_role")) not in expected or (row["profile_id"],row["source_role"]) in seen or row.get("inventory_relative_path")!=expected[(row["profile_id"],row["source_role"])]["inventory_relative_path"] or not _hex256(row.get("source_file_sha256")) or not _hex256(row.get("archive_sha256")): raise MemBenchError("membench_acquisition_receipt_invalid")
        resolved=_under(data_root/str(row["inventory_relative_path"]),data_root,"membench_acquisition_path_escape")
        if resolved.is_symlink() or not resolved.is_file(): raise MemBenchError("membench_acquisition_file_missing")
        seen.add((row["profile_id"],row["source_role"]))
    if seen!=set(expected): raise MemBenchError("membench_acquisition_coverage_invalid")
    unsigned={key:child for key,child in value.items() if key not in {"acquisition_sha256","acquisition_hmac"}}
    if value.get("acquisition_sha256")!=digest(unsigned) or not isinstance(value.get("acquisition_hmac"),str) or not hmac.compare_digest(value["acquisition_hmac"],_opaque(operator_secret,{**unsigned,"acquisition_sha256":value["acquisition_sha256"]})): raise MemBenchError("membench_acquisition_receipt_invalid")
    return dict(value)

def _profile_role_file_map_sha256(files:Sequence[Mapping[str,Any]])->str:
    rows=[{"profile_id":row["profile_id"],"source_role":row["source_role"],"inventory_relative_path":row["inventory_relative_path"],"source_file_sha256":row["source_file_sha256"]} for row in files]
    return digest(sorted(rows,key=lambda row:(row["profile_id"],row["source_role"])))

def _formal_source_receipt_from_manifest(*,manifest:Mapping[str,Any],source_bytes:Mapping[tuple[str,str],bytes],acquisition:Mapping[str,Any])->dict[str,Any]:
    files=[]
    for profile in manifest["profiles"]:
        key=(profile["profile_id"],profile["source_role"]); raw=source_bytes.get(key)
        if raw is None: raise MemBenchError("membench_source_file_digest_invalid")
        files.append({"profile_id":profile["profile_id"],"source_role":profile["source_role"],"source_file_sha256":hashlib.sha256(raw).hexdigest(),"byte_count":len(raw)})
    return _formal_source_receipt({"schema":SOURCE_RECEIPT_SCHEMA,"dataset":"MemBench","official_commit":SOURCE_COMMIT,"official_tree":SOURCE_TREE,"source_shape":SOURCE_RECEIPT["source_shape"],"source_manifest_sha256":manifest["manifest_sha256"],"selected_profiles_sha256":digest(manifest["selected_profiles"]),"source_file_bytes_aggregate_sha256":digest(sorted(files,key=lambda row:(row["source_file_sha256"],row["byte_count"]))),"acquisition_sha256":acquisition["acquisition_sha256"],"profile_role_file_map_sha256":_profile_role_file_map_sha256(acquisition["files"])})

def _build_formal_bundles(*,manifest:Mapping[str,Any],source_bytes:Mapping[tuple[str,str],bytes],acquisition:Mapping[str,Any],opacity_secret:bytes)->tuple[dict[str,Any],dict[str,Any]]:
    frozen=validate_source_manifest(manifest); receipt=_formal_source_receipt_from_manifest(manifest=frozen,source_bytes=source_bytes,acquisition=acquisition); items=[]; records=[]
    for profile in frozen["profiles"]:
        profile_id=profile["profile_id"]; role=profile["source_role"]; raw=source_bytes[(profile_id,role)]
        try: source=json.loads(raw)
        except json.JSONDecodeError as exc: raise MemBenchError("membench_source_file_json_invalid") from exc
        if not isinstance(source,Mapping) or not source: raise MemBenchError("membench_source_shape_invalid")
        for question_type,scenarios in sorted(source.items()):
            _text(question_type,"membench_source_shape_invalid")
            if not isinstance(scenarios,Mapping): raise MemBenchError("membench_source_shape_invalid")
            for scenario,trajectories in sorted(scenarios.items()):
                _text(scenario,"membench_source_shape_invalid")
                if not isinstance(trajectories,list): raise MemBenchError("membench_source_shape_invalid")
                for trajectory in trajectories:
                    if not isinstance(trajectory,Mapping) or set(trajectory)!={"tid","message_list","QA"}: raise MemBenchError("membench_trajectory_invalid")
                    tid=_text(trajectory["tid"],"membench_trajectory_invalid"); messages=_messages(trajectory["message_list"]); qa=trajectory["QA"]; expected={"question","time","choices","ground_truth","target_step_id"}
                    if not isinstance(qa,Mapping) or set(qa)!=expected: raise MemBenchError("membench_qa_invalid")
                    targets=_targets(qa["target_step_id"],len(messages)); group=_opaque(opacity_secret,{"source_file_role":role,"tid":tid}); item_id=_opaque(opacity_secret,{"group_id":group,"profile_id":profile_id,"qa_index":0})
                    candidates=[{"candidate_id":_opaque(opacity_secret,{"item_id":item_id,"step":number}),"order":number,"text":text} for number,text in enumerate(messages)]
                    # This commitment deliberately has no plaintext profile in
                    # the candidate.  The custodian can nevertheless recompute
                    # it from custody-only profile/role/tid metadata, so a
                    # two-way profile-label swap cannot preserve the denominator.
                    profile_binding_id=digest({"profile_id":profile_id,"source_file_role":role,"tid":tid})
                    items.append({"item_id":item_id,"group_id":group,"profile_binding_id":profile_binding_id,"query_text":_text(qa["question"],"membench_qa_invalid"),"query_time":qa["time"] if isinstance(qa["time"],str) else None,"candidates":candidates})
                    records.append({"item_id":item_id,"group_id":group,"profile_id":profile_id,"profile_binding_id":profile_binding_id,"source_file_role":role,"raw_tid":tid,"question_type":question_type,"scenario":scenario,"target_step_id":qa["target_step_id"],"ground_truth":qa["ground_truth"],"choices":qa["choices"],"strata":{"question_type":question_type,"scenario":scenario},"source_locator":{"inventory_relative_path":profile["inventory_relative_path"],"profile_id":profile_id,"source_file_role":role,"tid":tid,"qa_index":0},"crosswalk":{"candidate_ids_by_step":[row["candidate_id"] for row in candidates]},"gold_candidate_ids":[candidates[number]["candidate_id"] for number in targets]})
    candidate={"schema":CANDIDATE_SCHEMA,"source_receipt":receipt,"items":items}; candidate["projection_sha256"]=digest(candidate)
    custody={"schema":FORMAL_CUSTODY_SCHEMA,"source_receipt":receipt,"records":records}; custody["custody_sha256"]=digest(custody)
    return validate_candidate_projection(candidate),validate_custody(custody)

def _normalization(p:Mapping[str,Any])->dict[str,Any]:
    value={"schema":NORMALIZATION_SCHEMA,"source_receipt":p["source_receipt"],"projection_sha256":p["projection_sha256"],"items":[{"item_id":x["item_id"],"group_id":x["group_id"],"candidate_ids":[c["candidate_id"] for c in x["candidates"]]} for x in p["items"]]}; value["normalization_sha256"]=digest(value); return value
def original_normalized_projection(candidate:Mapping[str,Any])->dict[str,Any]:
    """Label-free bridge from MemBench candidates to the generic original core."""
    p=validate_candidate_projection(candidate)
    value={"schema":original_core.NORMALIZED_PROJECTION_SCHEMA,"corpora":[{"corpus_id":item["item_id"],"candidates":[{"candidate_id":row["candidate_id"],"order":row["order"],"text":row["text"]} for row in item["candidates"]]} for item in p["items"]],"items":[{"item_id":item["item_id"],"corpus_id":item["item_id"],"query_text":item["query_text"]} for item in p["items"]]}
    return original_core.validate_normalized_projection(value)
def original_lifecycle_adapter(candidate:Mapping[str,Any])->original_core.LifecycleAdapter:
    """AERP-8 adapter: opaque candidate IDs are the only original-worker IDs."""
    # The formal worker receives only this normalized representation, while the
    # coordinator may start from the richer (still label-free) candidate
    # projection.  Both forms deliberately lead to the same lifecycle.
    if isinstance(candidate, Mapping) and candidate.get("schema") == CANDIDATE_SCHEMA:
        normalized=original_normalized_projection(candidate)
    else:
        normalized=original_core.validate_normalized_projection(candidate)
    def namespace(projection:Mapping[str,Any])->Mapping[str,Any]:
        # The public product's direct physical audit owns this separator.  The
        # AERP-8 adapter changes only opaque logical IDs, never that exact
        # persisted-index contract.
        generic=original_core.identity_namespace(projection,separator=original_product.PHYSICAL_SEPARATOR,schema="aerp8-original-identity-namespace-v1")
        rows=[{"corpus_id":row["corpus_id"],"message_id":row["candidate_id"],"physical_id":row["physical_id"]} for row in generic["rows"]]
        return {**generic,"rows":rows,"mapping_sha256":original_core.canonical_sha256(rows)}
    def receipt(projection:Mapping[str,Any])->Mapping[str,Any]:
        frozen=original_core.validate_normalized_projection(projection)
        return {"schema":"aerp8-original-input-receipt-v1","projection_sha256":original_core.canonical_sha256(frozen),"item_corpora":[{"item_id":row["item_id"],"corpus_id":row["corpus_id"]} for row in sorted(frozen["items"],key=lambda row:row["item_id"])]}
    def format_row(*,item:Mapping[str,Any],ranked_candidate_ids:list[str],trace:Mapping[str,Any],product_row:Mapping[str,Any])->Mapping[str,Any]:
        return {"item_id":item["item_id"],"ranked_candidate_ids":list(ranked_candidate_ids),"rank_trace_sha256":original_core.canonical_sha256(trace)}
    def completed(projection:Mapping[str,Any],replicate:Mapping[str,Any])->None:
        if not isinstance(replicate,Mapping) or not {"build_id","index_sha256","rankings"} <= set(replicate): raise merror("membench_original_reaudit_invalid")
        expected={row["item_id"]: {candidate["candidate_id"] for corpus in normalized["corpora"] if corpus["corpus_id"] == row["corpus_id"] for candidate in corpus["candidates"]} for row in normalized["items"]}
        rows=replicate["rankings"]
        if not isinstance(rows,list) or len(rows)!=len(expected): raise merror("membench_original_reaudit_invalid")
        seen=set()
        for row in rows:
            if not isinstance(row,Mapping) or set(row)!={"item_id","ranked_candidate_ids","rank_trace_sha256"} or row.get("item_id") not in expected or row["item_id"] in seen: raise merror("membench_original_reaudit_invalid")
            ranked=row.get("ranked_candidate_ids"); allowed=expected[row["item_id"]]
            if not isinstance(ranked,list) or len(ranked)!=min(10,len(allowed)) or len(ranked)!=len(set(ranked)) or not set(ranked)<=allowed: raise merror("membench_original_reaudit_invalid")
            seen.add(row["item_id"])
        if seen!=set(expected): raise merror("membench_original_reaudit_invalid")
    def runtime(projection:Mapping[str,Any])->Mapping[str,Any]:
        # Public-product fields only: opaque IDs, text, and a fixed metadata
        # value.  No role/tid/label can enter this execution representation.
        frozen=original_core.validate_normalized_projection(projection)
        return {"corpora":[{"corpus_id":corpus["corpus_id"],"candidates":[{"message_id":row["candidate_id"],"opaque_conversation_id":corpus["corpus_id"],"conversation_order":0,"message_order":row["order"],"corpus_order":row["order"],"speaker":"","text":row["text"]} for row in corpus["candidates"]]} for corpus in frozen["corpora"]],"items":[{"item_id":row["item_id"],"corpus_id":row["corpus_id"],"query_text":row["query_text"]} for row in frozen["items"]]}
    return original_core.LifecycleAdapter(validate_projection=original_core.validate_normalized_projection,identity_namespace=namespace,input_receipt=receipt,format_row=format_row,validate_completed_replicate=completed,runtime_projection=runtime,adapter_id="aerp8-membench-original-lifecycle-v1")
def merror(code:str)->MemBenchError: return MemBenchError(code)
def _ranker(encoder:Any,arm:str)->SixViewRanker:
    if encoder is None: raise MemBenchError("membench_encoder_required")
    if arm=="strong_raw": return SixViewRanker(encoder,diagnostic_ledger=True,routing_policy=FixedRawPolicy())
    if arm=="static_p5": return SixViewRanker(encoder,diagnostic_ledger=True,routing_policy=FixedP5Policy())
    if arm=="six_view_secondary": return SixViewRanker(encoder,diagnostic_ledger=True)
    raise MemBenchError("membench_current_arm_invalid")
def _authorized(item:Mapping[str,Any])->list[AuthorizedRetrievalCandidate]:
    return [AuthorizedRetrievalCandidate(source_event_id=x["candidate_id"],source_scene_id=item["group_id"],raw_text=x["text"],observation=structured_observation(summary=x["text"],event_type="membench_message",actor_id=None,target_id=None,related_entities=None,related_quests=None,related_locations=None,in_world_time=item["query_time"],location_id=None),checkpoint_key=item["group_id"],policy_tuple=(None,)*8,chronological_order_key=(x["order"],x["candidate_id"]),ranking_key=x["candidate_id"]) for x in item["candidates"]]
def _method(arm:str)->dict[str,str]: return {"implementation":"mempalace_rpg.retrieval.SixViewRanker","routing_policy":{"strong_raw":"FixedRawPolicy","static_p5":"FixedP5Policy","six_view_secondary":"FixedSixViewPolicy"}[arm],"fixed_policy":"true"}
def rank_current(*,candidate:Mapping[str,Any],arm_id:str,encoder:Any|None=None)->dict[str,Any]:
    p=validate_candidate_projection(candidate); ranker=_ranker(encoder,arm_id); norm=_normalization(p); rows=[]
    for item in p["items"]:
      result=ranker.rank(query=item["query_text"],candidates=_authorized(item)); rows.append({"item_id":item["item_id"],"ranked_candidate_ids":result.ranked_event_ids[:10],"rank_trace_sha256":digest(result.trace)})
    artifact={"schema":ARTIFACT_SCHEMA,"source_receipt":p["source_receipt"],"arm_id":arm_id,"method":_method(arm_id),"projection_sha256":p["projection_sha256"],"normalization_sha256":norm["normalization_sha256"],"rankings":rows}; artifact["artifact_sha256"]=digest(artifact); return artifact
def run_candidate_arms(*,candidate:Mapping[str,Any],encoder:Any|None=None)->dict[str,dict[str,Any]]:
    rows={arm:rank_current(candidate=candidate,arm_id=arm,encoder=encoder) for arm in CURRENT_ARMS}; repeat=rank_current(candidate=candidate,arm_id="static_p5",encoder=encoder)
    if _bytes(rows["static_p5"])!=_bytes(repeat): raise MemBenchError("membench_static_p5_not_byte_identical")
    return {**rows,"static_p5_repeat":repeat}
def _validate_rankings(rows:Any,p:Mapping[str,Any])->list[dict[str,Any]]:
    if not isinstance(rows,list) or len(rows)!=len(p["items"]): raise MemBenchError("membench_ranking_coverage_invalid")
    expected={x["item_id"]:x for x in p["items"]}; seen=set()
    for row in rows:
      if not isinstance(row,Mapping) or set(row)-{"item_id","ranked_candidate_ids","rank_trace_sha256"} or not isinstance(row.get("item_id"),str) or not isinstance(row.get("ranked_candidate_ids"),list): raise MemBenchError("membench_ranking_invalid")
      item=row["item_id"]; ids=row["ranked_candidate_ids"]
      if item in seen or item not in expected: raise MemBenchError("membench_ranking_coverage_invalid")
      seen.add(item); allowed={x["candidate_id"] for x in expected[item]["candidates"]}
      if len(ids)>min(10,len(allowed)) or len(ids)!=len(set(ids)) or not set(ids)<=allowed: raise MemBenchError("membench_ranking_invalid")
    if seen!=set(expected): raise MemBenchError("membench_ranking_coverage_invalid")
    return [dict(x) for x in rows]
def run_synthetic_original_five(*,candidate:Mapping[str,Any],original_runner:Callable[[Mapping[str,Any],str],Mapping[str,Any]],coordinator_reaudit:Callable[[Mapping[str,Any]],Mapping[str,Any]])->dict[str,Any]:
    """Synthetic-test helper only; formal execution must use worker processes."""
    p=validate_candidate_projection(candidate); normalized=original_normalized_projection(p); reps=[]
    for n in range(5):
      checked=coordinator_reaudit(original_runner(p,f"membench-original-{n}"))
      if not isinstance(checked,Mapping) or set(checked)!={"build_id","index_sha256","draft_sha256","rankings"} or not all(isinstance(checked[x],str) and checked[x] for x in ("build_id","index_sha256","draft_sha256")): raise MemBenchError("membench_original_reaudit_invalid")
      row=dict(checked); row["rankings"]=_validate_rankings(row["rankings"],p); reps.append(row)
    if any(len({x[key] for x in reps})!=5 for key in ("build_id","index_sha256","draft_sha256")): raise MemBenchError("membench_original_build_not_fresh")
    artifact={"schema":ARTIFACT_SCHEMA,"source_receipt":p["source_receipt"],"arm_id":"original_public_product","method":{"implementation":"exact_pinned_original_public_product_worker","five_fresh_builds":True},"projection_sha256":p["projection_sha256"],"normalization_sha256":_normalization(p)["normalization_sha256"],"original_normalized_projection_sha256":original_core.canonical_sha256(normalized),"replicates":reps}; artifact["artifact_sha256"]=digest(artifact); return artifact
def _validate_artifact(a:Any,p:Mapping[str,Any],arm:str)->dict[str,Any]:
    if not isinstance(a,Mapping) or a.get("schema")!=ARTIFACT_SCHEMA or a.get("source_receipt")!=p["source_receipt"] or a.get("arm_id")!=arm or a.get("projection_sha256")!=p["projection_sha256"] or a.get("normalization_sha256")!=_normalization(p)["normalization_sha256"] or a.get("artifact_sha256")!=digest({k:v for k,v in a.items() if k!="artifact_sha256"}): raise MemBenchError("membench_artifact_invalid")
    if arm in CURRENT_ARMS:
      if set(a)!={"schema","source_receipt","arm_id","method","projection_sha256","normalization_sha256","rankings","artifact_sha256"}: raise MemBenchError("membench_artifact_invalid")
      _validate_rankings(a["rankings"],p)
    else:
      allowed={"schema","source_receipt","arm_id","method","projection_sha256","normalization_sha256","original_normalized_projection_sha256","replicates","artifact_sha256","runtime_receipts","worker_receipts","coordinator_code_receipt","checkpoint_sha256","original_execution_policy_sha256"}
      if set(a)-allowed or a.get("original_normalized_projection_sha256")!=original_core.canonical_sha256(original_normalized_projection(p)) or not isinstance(a["replicates"],list) or len(a["replicates"])!=5: raise MemBenchError("membench_artifact_invalid")
      formal_fields={"runtime_receipts","worker_receipts","coordinator_code_receipt","checkpoint_sha256","original_execution_policy_sha256"}
      if bool(formal_fields & set(a)) != (formal_fields <= set(a)): raise MemBenchError("membench_artifact_invalid")
      if formal_fields <= set(a):
        if not isinstance(a["runtime_receipts"],list) or not isinstance(a["worker_receipts"],list) or len(a["runtime_receipts"])!=5 or len(a["worker_receipts"])!=5: raise MemBenchError("membench_artifact_invalid")
        _validate_driver_code_receipt(a["coordinator_code_receipt"])
        if not isinstance(a["checkpoint_sha256"],str) or len(a["checkpoint_sha256"])!=64 or not _hex256(a["original_execution_policy_sha256"]): raise MemBenchError("membench_artifact_invalid")
      for key in ("build_id","index_sha256","draft_sha256"):
        if len({x.get(key) for x in a["replicates"] if isinstance(x,Mapping)})!=5: raise MemBenchError("membench_original_build_not_fresh")
      for x in a["replicates"]:
        if not isinstance(x,Mapping) or set(x)!={"build_id","index_sha256","draft_sha256","rankings"}: raise MemBenchError("membench_artifact_invalid")
        _validate_rankings(x["rankings"],p)
    return dict(a)
def freeze_synthetic_public_results(*,candidate:Mapping[str,Any],current:Mapping[str,Mapping[str,Any]],original:Mapping[str,Any],ready_receipts:Mapping[str,Mapping[str,Any]],model_receipt:Mapping[str,Any],code_receipt:Mapping[str,Any])->dict[str,Any]:
    p=validate_candidate_projection(candidate)
    if set(current)!={"strong_raw","static_p5","six_view_secondary","static_p5_repeat"} or set(ready_receipts)!={"strong_raw","static_p5","six_view_secondary","static_p5_repeat","original_public_product"}: raise MemBenchError("membench_public_results_invalid")
    for arm in CURRENT_ARMS: _validate_artifact(current[arm],p,arm)
    _validate_artifact(current["static_p5_repeat"],p,"static_p5")
    if _bytes(current["static_p5"])!=_bytes(current["static_p5_repeat"]): raise MemBenchError("membench_static_p5_not_byte_identical")
    original=_validate_artifact(original,p,"original_public_product")
    for arm,ready in ready_receipts.items():
      if not isinstance(ready,Mapping) or ready.get("schema")!=READY_SCHEMA or ready.get("arm_id")!=arm or not isinstance(ready.get("payload_sha256"),str): raise MemBenchError("membench_ready_invalid")
    result={"schema":SYNTHETIC_PUBLIC_RESULTS_SCHEMA,"candidate_projection_sha256":p["projection_sha256"],"current_artifact_sha256":{arm:current[arm]["artifact_sha256"] for arm in CURRENT_ARMS},"static_p5_repeat_sha256":current["static_p5_repeat"]["artifact_sha256"],"original_artifact_sha256":original["artifact_sha256"],"original_replicate_receipts":[{key:x[key] for key in ("build_id","index_sha256","draft_sha256")} for x in original["replicates"]],"ready_receipts":dict(ready_receipts),"model_receipt":dict(model_receipt),"code_receipt":dict(code_receipt)}; result["public_results_sha256"]=digest(result); return result

def _load_public_artifact_file(*,capability:Mapping[str,Any],candidate:Mapping[str,Any],arm:str,role:str|None)->tuple[dict[str,Any],dict[str,Any]]:
    required={"artifact_path","ready_path"} | ({"execution_role"} if role is not None else set())
    if not isinstance(capability,Mapping) or set(capability)!=required or (role is not None and capability.get("execution_role")!=role): raise MemBenchError("membench_public_file_capability_invalid")
    artifact_path=Path(str(capability["artifact_path"])); ready_path=Path(str(capability["ready_path"]))
    if not artifact_path.is_absolute() or not ready_path.is_absolute() or artifact_path.is_symlink() or ready_path.is_symlink() or not artifact_path.is_file() or not ready_path.is_file(): raise MemBenchError("membench_public_file_capability_invalid")
    payload=artifact_path.read_bytes(); ready_bytes=ready_path.read_bytes()
    try: artifact=json.loads(payload); ready=json.loads(ready_bytes)
    except json.JSONDecodeError as exc: raise MemBenchError("membench_public_file_payload_invalid") from exc
    if _bytes(artifact)!=payload or _bytes(ready)!=ready_bytes or not isinstance(ready,Mapping) or ready.get("schema")!=READY_SCHEMA or ready.get("arm_id")!=arm or ready.get("payload_sha256")!=hashlib.sha256(payload).hexdigest() or ready.get("ready_sha256")!=digest({key:child for key,child in ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_public_ready_invalid")
    if role is not None and (ready.get("execution_role")!=role or ready.get("artifact_sha256")!=artifact.get("artifact_sha256")): raise MemBenchError("membench_public_ready_invalid")
    return _validate_artifact(artifact,candidate,arm),dict(ready)

def validate_threshold_protocol(value:Any)->dict[str,Any]:
    required={"schema","source_roles","seed","resamples","overall_delta_min","overall_ci_lower_min","hard_delta_min","hard_ci_lower_min","threshold_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=THRESHOLD_PROTOCOL_SCHEMA or value.get("source_roles")!=sorted(FORMAL_SOURCE_ROLES) or value.get("seed")!=20260823 or value.get("resamples")!=5000: raise MemBenchError("membench_threshold_protocol_invalid")
    for key in ("overall_delta_min","overall_ci_lower_min","hard_delta_min","hard_ci_lower_min"):
        if isinstance(value[key],bool) or not isinstance(value[key],(int,float)) or not math.isfinite(float(value[key])): raise MemBenchError("membench_threshold_protocol_invalid")
    if value["overall_delta_min"]!=.01 or value["overall_ci_lower_min"]!=0 or value["hard_delta_min"]!=0 or value["hard_ci_lower_min"]!=-.01 or value["threshold_sha256"]!=digest({key:child for key,child in value.items() if key!="threshold_sha256"}): raise MemBenchError("membench_threshold_protocol_invalid")
    return dict(value)

def _formal_custody(value:Any)->dict[str,Any]:
    custody=validate_custody(value)
    if custody["schema"]!=FORMAL_CUSTODY_SCHEMA or custody["source_receipt"]==SOURCE_RECEIPT or any(record["source_file_role"] not in FORMAL_SOURCE_ROLES or not isinstance(record.get("profile_id"),str) or not isinstance(record.get("strata"),Mapping) or set(record["strata"])!={"question_type","scenario"} for record in custody["records"]): raise MemBenchError("membench_formal_custody_role_invalid")
    roles={record["source_file_role"] for record in custody["records"]}
    if roles!=FORMAL_SOURCE_ROLES: raise MemBenchError("membench_formal_custody_role_coverage_invalid")
    return custody

def _cross_bind_candidate_custody(*,candidate:Mapping[str,Any],custody:Mapping[str,Any])->None:
    """Make the sealed denominator an exact projection crosswalk, not a role check."""
    p=validate_candidate_projection(candidate); c=_formal_custody(custody)
    if p["source_receipt"]!=c["source_receipt"]:
        raise MemBenchError("membench_candidate_custody_source_receipt_mismatch")
    items={row["item_id"]:row for row in p["items"]}; records={}
    for record in c["records"]:
        item_id=record["item_id"]
        if item_id in records: raise MemBenchError("membench_candidate_custody_record_duplicate")
        records[item_id]=record
    if set(items)!=set(records): raise MemBenchError("membench_candidate_custody_denominator_mismatch")
    profile_roles={profile:{role:0 for role in FORMAL_SOURCE_ROLES} for profile in ("0","100")}
    for item_id,item in items.items():
        record=records[item_id]
        expected_binding=digest({"profile_id":record["profile_id"],"source_file_role":record["source_file_role"],"tid":record["raw_tid"]})
        if record["group_id"]!=item["group_id"] or record["profile_id"] not in profile_roles or record["profile_binding_id"]!=expected_binding or item["profile_binding_id"]!=expected_binding:
            raise MemBenchError("membench_candidate_custody_crosswalk_mismatch")
        profile_roles[record["profile_id"]][record["source_file_role"]]+=1
        candidate_ids=[row["candidate_id"] for row in item["candidates"]]
        gold=record["gold_candidate_ids"]
        if len(gold)!=len(set(gold)) or not gold or not set(gold)<=set(candidate_ids) or record.get("crosswalk",{}).get("candidate_ids_by_step")!=candidate_ids:
            raise MemBenchError("membench_candidate_custody_gold_crosswalk_invalid")
    if any(any(count<=0 for count in roles.values()) for roles in profile_roles.values()):
        raise MemBenchError("membench_candidate_custody_profile_coverage_invalid")

def _checkpoint_binding(value:Any,*,code_receipt:Mapping[str,Any])->dict[str,Any]:
    required={"checkpoint_sha256","driver_code_receipt","original_execution_policy","original_execution_policy_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or not isinstance(value.get("checkpoint_sha256"),str) or len(value["checkpoint_sha256"])!=64 or value.get("driver_code_receipt")!=code_receipt:
        raise MemBenchError("membench_checkpoint_binding_invalid")
    policy=_validate_original_execution_policy(value.get("original_execution_policy"))
    if value.get("original_execution_policy_sha256")!=policy["policy_sha256"]:
        raise MemBenchError("membench_checkpoint_binding_invalid")
    return dict(value)

def _code_source(code:Mapping[str,Any],module:str)->dict[str,Any]:
    _validate_driver_code_receipt(code)
    rows=[row for row in code["sources"] if row["module"]==module]
    if len(rows)!=1: raise MemBenchError("membench_runtime_code_receipt_invalid")
    return dict(rows[0])

def _validate_current_runtime(runtime:Any,*,checkpoint_sha256:str,code_receipt:Mapping[str,Any],artifact_path:Path,ready_path:Path)->dict[str,Any]:
    required={"schema","rpg_root","rpg_python","rpg_python_sha256","driver_file","driver_file_sha256","model_dir","model_file_tree_sha256","git_capability","worker_home_path","checkpoint_sha256","code_receipt_sha256","cpu_provider_policy","runtime_sha256"}
    if not isinstance(runtime,Mapping) or set(runtime)!=required or runtime.get("schema")!="aerp8-pinned-current-runtime-v1" or runtime.get("runtime_sha256")!=digest({key:value for key,value in runtime.items() if key!="runtime_sha256"}) or runtime.get("checkpoint_sha256")!=checkpoint_sha256 or runtime.get("code_receipt_sha256")!=code_receipt.get("code_sha256") or runtime.get("model_file_tree_sha256")!=ORIGINAL_MODEL_TREE or runtime.get("git_capability")!=code_receipt.get("git_capability") or runtime.get("cpu_provider_policy")!={"device":"cpu","providers":["CPUExecutionProvider"]}: raise MemBenchError("membench_current_runtime_receipt_invalid")
    root=Path(str(runtime["rpg_root"])); python=Path(str(runtime["rpg_python"])); driver=Path(str(runtime["driver_file"])); home=Path(str(runtime["worker_home_path"]))
    model=Path(str(runtime["model_dir"]))
    config=Path(str(code_receipt["venv_pyvenv_cfg"]))
    if not root.is_absolute() or root.resolve()!=Path(str(code_receipt["rpg_root"])).resolve() or not python.is_absolute() or python.resolve()!=Path(str(code_receipt["python"])).resolve() or not python.is_file() or python.is_symlink() or _sha256_file(python)!=runtime.get("rpg_python_sha256") or runtime.get("rpg_python_sha256")!=code_receipt.get("python_sha256") or not config.is_file() or config.is_symlink() or config.resolve()!=(root/".venv"/"pyvenv.cfg").resolve() or _sha256_file(config)!=code_receipt.get("venv_pyvenv_cfg_sha256") or not str(python.resolve()).startswith(str((root/".venv").resolve())) or not driver.is_absolute() or driver.resolve()!=Path(_code_source(code_receipt,"benchmarks.aerp8_membench")["path"]).resolve() or not driver.is_file() or driver.is_symlink() or _sha256_file(driver)!=runtime.get("driver_file_sha256") or runtime.get("driver_file_sha256")!=_code_source(code_receipt,"benchmarks.aerp8_membench")["sha256"] or not model.is_dir() or model.is_symlink() or original_product.v1.file_tree_receipt(model).get("sha256")!=ORIGINAL_MODEL_TREE or not home.is_dir() or home.is_symlink() or artifact_path.parent.resolve()!=home.resolve() or ready_path.parent.resolve()!=home.resolve(): raise MemBenchError("membench_current_runtime_receipt_invalid")
    git=runtime["git_capability"]; _verify_git_capability(executable=Path(git["executable"]),sha256=git["sha256"],version=git["version"],system32_required=git["system32_required"])
    return dict(runtime)

def _validate_original_runtime(runtime:Any,*,checkpoint_sha256:str,execution_policy:Mapping[str,Any],code_receipt:Mapping[str,Any],draft_path:Path,ready_path:Path)->dict[str,Any]:
    required={"schema","original_commit","original_tree","original_git_state","original_root","original_python","original_python_sha256","mempalace_file","mempalace_file_sha256","driver_file","driver_file_sha256","model_dir","model_file_tree_sha256","git_capability","worker_home_path","checkpoint_sha256","code_receipt_sha256","original_execution_policy_sha256","runtime_sha256"}
    policy=_validate_original_execution_policy(execution_policy)
    if not isinstance(runtime,Mapping) or set(runtime)!=required or runtime.get("schema")!="aerp8-pinned-original-runtime-v2" or runtime.get("runtime_sha256")!=digest({key:value for key,value in runtime.items() if key!="runtime_sha256"}) or runtime.get("checkpoint_sha256")!=checkpoint_sha256 or runtime.get("code_receipt_sha256")!=code_receipt.get("code_sha256") or runtime.get("original_execution_policy_sha256")!=policy["policy_sha256"] or runtime.get("original_commit")!=ORIGINAL_COMMIT or runtime.get("original_tree")!=ORIGINAL_TREE or runtime.get("model_file_tree_sha256")!=ORIGINAL_MODEL_TREE or runtime.get("git_capability")!=code_receipt.get("git_capability"): raise MemBenchError("membench_original_runtime_receipt_invalid")
    state=runtime.get("original_git_state")
    if not isinstance(state,Mapping) or state.get("git_dirty") is not False or state.get("git_head")!=ORIGINAL_COMMIT or state.get("git_tree")!=ORIGINAL_TREE: raise MemBenchError("membench_original_runtime_receipt_invalid")
    for key in ("original_root","original_python","mempalace_file","driver_file","model_dir","worker_home_path"):
        if not isinstance(runtime.get(key),str) or not Path(runtime[key]).is_absolute(): raise MemBenchError("membench_original_runtime_receipt_invalid")
    root=Path(runtime["original_root"]); python=Path(runtime["original_python"]); package=Path(runtime["mempalace_file"]); model=Path(runtime["model_dir"]); home=Path(runtime["worker_home_path"]); driver=Path(runtime["driver_file"]); source=_code_source(code_receipt,"benchmarks.aerp8_membench")
    try: state=original_product.v1.original_source_state(root)
    except (OSError,subprocess.SubprocessError) as exc: raise MemBenchError("membench_original_runtime_receipt_invalid") from exc
    if not root.is_dir() or root.is_symlink() or state.get("git_dirty") is not False or state.get("git_head")!=ORIGINAL_COMMIT or state.get("git_tree")!=ORIGINAL_TREE or not python.is_file() or python.is_symlink() or _under(python,root,"membench_original_runtime_receipt_invalid")!=python.resolve() or _sha256_file(python)!=runtime.get("original_python_sha256") or not package.is_file() or package.is_symlink() or _under(package,root,"membench_original_runtime_receipt_invalid")!=package.resolve() or _sha256_file(package)!=runtime.get("mempalace_file_sha256") or driver.resolve()!=Path(source["path"]).resolve() or not driver.is_file() or driver.is_symlink() or _sha256_file(driver)!=runtime.get("driver_file_sha256") or runtime.get("driver_file_sha256")!=source["sha256"] or not model.is_dir() or model.is_symlink() or original_product.v1.file_tree_receipt(model).get("sha256")!=ORIGINAL_MODEL_TREE or not home.is_dir() or home.is_symlink() or draft_path.parent.resolve()!=home.resolve() or ready_path.parent.resolve()!=home.resolve(): raise MemBenchError("membench_original_runtime_receipt_invalid")
    for key in ("original_root","original_python","original_python_sha256","mempalace_file","mempalace_file_sha256","model_dir","model_file_tree_sha256","git_capability"):
        if runtime.get(key)!=policy.get(key): raise MemBenchError("membench_original_runtime_receipt_invalid")
    git=runtime["git_capability"]; _verify_git_capability(executable=Path(git["executable"]),sha256=git["sha256"],version=git["version"],system32_required=git["system32_required"])
    return dict(runtime)

def _validate_formal_original_provenance(artifact:Mapping[str,Any],*,code_receipt:Mapping[str,Any],checkpoint_sha256:str,execution_policy:Mapping[str,Any])->None:
    runtimes=artifact.get("runtime_receipts"); receipts=artifact.get("worker_receipts")
    policy=_validate_original_execution_policy(execution_policy)
    if artifact.get("checkpoint_sha256")!=checkpoint_sha256 or artifact.get("original_execution_policy_sha256")!=policy["policy_sha256"] or artifact.get("coordinator_code_receipt")!=code_receipt or not isinstance(runtimes,list) or not isinstance(receipts,list) or len(runtimes)!=5 or len(receipts)!=5: raise MemBenchError("membench_original_runtime_receipt_invalid")
    builds={row["build_id"]:row for row in artifact["replicates"]}
    if len(builds)!=5: raise MemBenchError("membench_original_runtime_receipt_invalid")
    seen=set()
    for runtime,receipt in zip(runtimes,receipts):
        if not isinstance(receipt,Mapping): raise MemBenchError("membench_original_runtime_receipt_invalid")
        checked=_validate_original_runtime(runtime,checkpoint_sha256=checkpoint_sha256,execution_policy=policy,code_receipt=code_receipt,draft_path=Path(str(receipt.get("draft_path",""))),ready_path=Path(str(receipt.get("ready_path",""))))
        if not isinstance(receipt,Mapping) or set(receipt)!={"schema","build_id","draft_path","ready_path","draft_bytes_sha256","ready_sha256","checkpoint_sha256","original_execution_policy_sha256","runtime_receipt"} or receipt.get("schema")!="aerp8-original-worker-receipt-v1" or receipt.get("checkpoint_sha256")!=checkpoint_sha256 or receipt.get("original_execution_policy_sha256")!=policy["policy_sha256"] or receipt.get("runtime_receipt")!=checked or receipt.get("build_id") not in builds or receipt["build_id"] in seen or receipt.get("draft_bytes_sha256")!=builds[receipt["build_id"]]["draft_sha256"]: raise MemBenchError("membench_original_runtime_receipt_invalid")
        seen.add(receipt["build_id"])
    if seen!=set(builds): raise MemBenchError("membench_original_runtime_receipt_invalid")

def _public_current_worker_receipts(*,receipts:Any,files:Mapping[str,Mapping[str,Any]],loaded:Mapping[str,tuple[dict[str,Any],dict[str,Any]]],checkpoint_sha256:str,code_receipt:Mapping[str,Any])->dict[str,Any]:
    if not isinstance(receipts,Mapping) or set(receipts)!=set(CURRENT_ROLES): raise MemBenchError("membench_public_worker_receipt_invalid")
    checked={}
    required={"schema","execution_role","arm_id","artifact_path","ready_path","payload_sha256","artifact_sha256","ready_sha256","checkpoint_sha256","runtime_receipt"}
    for role,arm in CURRENT_ROLES.items():
        receipt=receipts[role]; artifact,ready=loaded[role]
        if not isinstance(receipt,Mapping) or set(receipt)!=required or receipt.get("schema")!="aerp8-current-worker-receipt-v1" or receipt.get("execution_role")!=role or receipt.get("arm_id")!=arm or receipt.get("artifact_path")!=files[role]["artifact_path"] or receipt.get("ready_path")!=files[role]["ready_path"] or receipt.get("payload_sha256")!=ready.get("payload_sha256") or receipt.get("artifact_sha256")!=artifact.get("artifact_sha256") or receipt.get("ready_sha256")!=ready.get("ready_sha256") or receipt.get("checkpoint_sha256")!=checkpoint_sha256:
            raise MemBenchError("membench_public_worker_receipt_invalid")
        _validate_current_runtime(receipt.get("runtime_receipt"),checkpoint_sha256=checkpoint_sha256,code_receipt=code_receipt,artifact_path=Path(str(receipt["artifact_path"])),ready_path=Path(str(receipt["ready_path"])))
        if receipt["runtime_receipt"]["runtime_sha256"]!=ready.get("runtime_sha256"):
            raise MemBenchError("membench_public_worker_receipt_invalid")
        checked[role]=dict(receipt)
    return checked

def freeze_public_results(*,candidate:Mapping[str,Any],current_files:Mapping[str,Mapping[str,Any]],current_worker_receipts:Mapping[str,Any],original_file:Mapping[str,Any],sealed_custody_sha256:str,threshold_protocol:Mapping[str,Any],model_receipt:Mapping[str,Any],code_receipt:Mapping[str,Any],checkpoint:Mapping[str,Any])->dict[str,Any]:
    """Formal freezer: read exact artifact/READY bytes from file capabilities."""
    p=validate_candidate_projection(candidate); _source_receipt(p["source_receipt"],formal_only=True); threshold=validate_threshold_protocol(threshold_protocol)
    if not isinstance(sealed_custody_sha256,str) or len(sealed_custody_sha256)!=64: raise MemBenchError("membench_public_custody_receipt_invalid")
    if not isinstance(model_receipt,Mapping) or model_receipt.get("sha256")!=ORIGINAL_MODEL_TREE or _validate_driver_code_receipt(code_receipt)!=(code_receipt): raise MemBenchError("membench_public_runtime_receipt_invalid")
    checkpoint=_checkpoint_binding(checkpoint,code_receipt=code_receipt)
    if set(current_files)!=set(CURRENT_ROLES): raise MemBenchError("membench_public_file_coverage_invalid")
    loaded={role:_load_public_artifact_file(capability=current_files[role],candidate=p,arm=arm,role=role) for role,arm in CURRENT_ROLES.items()}
    worker_receipts=_public_current_worker_receipts(receipts=current_worker_receipts,files=current_files,loaded=loaded,checkpoint_sha256=checkpoint["checkpoint_sha256"],code_receipt=code_receipt)
    original,original_ready=_load_public_artifact_file(capability=original_file,candidate=p,arm="original_public_product",role=None)
    if not isinstance(original.get("runtime_receipts"),list) or len(original["runtime_receipts"])!=5 or original.get("coordinator_code_receipt")!=code_receipt or original.get("checkpoint_sha256")!=checkpoint["checkpoint_sha256"]: raise MemBenchError("membench_public_runtime_receipt_invalid")
    _validate_formal_original_provenance(original,code_receipt=code_receipt,checkpoint_sha256=checkpoint["checkpoint_sha256"],execution_policy=checkpoint["original_execution_policy"])
    if original_ready.get("original_execution_policy_sha256")!=checkpoint["original_execution_policy_sha256"]:
        raise MemBenchError("membench_public_runtime_receipt_invalid")
    primary,repeat=loaded["p5_primary"][0],loaded["p5_repeat"][0]
    if _bytes(primary)!=_bytes(repeat): raise MemBenchError("membench_static_p5_not_byte_identical")
    result={"schema":PUBLIC_RESULTS_SCHEMA,"source_receipt":p["source_receipt"],"candidate_projection_sha256":p["projection_sha256"],"normalization_sha256":_normalization(p)["normalization_sha256"],"sealed_custody_sha256":sealed_custody_sha256,"threshold_protocol_sha256":threshold["threshold_sha256"],"checkpoint_sha256":checkpoint["checkpoint_sha256"],"original_execution_policy":checkpoint["original_execution_policy"],"original_execution_policy_sha256":checkpoint["original_execution_policy_sha256"],"artifact_files":{**{role:dict(current_files[role]) for role in CURRENT_ROLES},"original_public_product":dict(original_file)},"current_worker_receipts":worker_receipts,"current_artifact_sha256":{"strong_raw":loaded["raw"][0]["artifact_sha256"],"static_p5":primary["artifact_sha256"],"six_view_secondary":loaded["six"][0]["artifact_sha256"]},"static_p5_repeat_sha256":repeat["artifact_sha256"],"original_artifact_sha256":original["artifact_sha256"],"original_replicate_receipts":[{key:x[key] for key in ("build_id","index_sha256","draft_sha256")} for x in original["replicates"]],"ready_receipts":{**{role:ready for role,(_artifact,ready) in loaded.items()},"original_public_product":original_ready},"model_receipt":dict(model_receipt),"code_receipt":dict(code_receipt)}
    result["public_results_sha256"]=digest(result); return result
def mint_synthetic_release(*,candidate:Mapping[str,Any],custody:Mapping[str,Any],public_results:Mapping[str,Any],capability_secret:bytes)->dict[str,Any]:
    p=validate_candidate_projection(candidate); c=validate_custody(custody)
    if p["source_receipt"]!=SOURCE_RECEIPT or c["schema"]!=CUSTODY_SCHEMA or not isinstance(public_results,Mapping) or public_results.get("schema")!=SYNTHETIC_PUBLIC_RESULTS_SCHEMA or public_results.get("candidate_projection_sha256")!=p["projection_sha256"] or public_results.get("public_results_sha256")!=digest({k:v for k,v in public_results.items() if k!="public_results_sha256"}): raise MemBenchError("membench_public_results_invalid")
    row={"schema":SYNTHETIC_RELEASE_SCHEMA,"candidate_projection_sha256":p["projection_sha256"],"custody_sha256":c["custody_sha256"],"public_results_sha256":public_results["public_results_sha256"]}; row["release_sha256"]=digest(row); row["capability_hmac"]=_opaque(capability_secret,row); return row
def open_synthetic_custody_after_release(*,release:Mapping[str,Any],candidate:Mapping[str,Any],custody:Mapping[str,Any],public_results:Mapping[str,Any],capability_secret:bytes)->dict[str,Any]:
    p=validate_candidate_projection(candidate); c=validate_custody(custody); required={"schema","candidate_projection_sha256","custody_sha256","public_results_sha256","release_sha256","capability_hmac"}
    if p["source_receipt"]!=SOURCE_RECEIPT or c["schema"]!=CUSTODY_SCHEMA: raise MemBenchError("membench_synthetic_input_required")
    if not isinstance(release,Mapping) or set(release)!=required or release.get("schema")!=SYNTHETIC_RELEASE_SCHEMA: raise MemBenchError("membench_release_invalid")
    unsigned={k:v for k,v in release.items() if k not in {"release_sha256","capability_hmac"}}
    if release["release_sha256"]!=digest(unsigned) or release["capability_hmac"]!=_opaque(capability_secret,{**unsigned,"release_sha256":release["release_sha256"]}): raise MemBenchError("membench_release_invalid")
    if release["candidate_projection_sha256"]!=p["projection_sha256"] or release["custody_sha256"]!=c["custody_sha256"] or not isinstance(public_results,Mapping) or release["public_results_sha256"]!=public_results.get("public_results_sha256"): raise MemBenchError("membench_release_binding_invalid")
    return c
def consume_synthetic_release(*,marker_path:Path,release:Mapping[str,Any])->dict[str,Any]:
    if not isinstance(release,Mapping) or release.get("schema")!=SYNTHETIC_RELEASE_SCHEMA or not isinstance(release.get("release_sha256"),str): raise MemBenchError("membench_release_invalid")
    return publish_nonreplace(path=marker_path,payload=_bytes({"schema":"aerp8-membench-consumed-synthetic-release-v1","release_sha256":release["release_sha256"]}))
def formal_preflight(value:Any)->dict[str,Any]:
    """Validate public receipts before a coordinator gets a source capability."""
    required={"schema","candidate","public_results","release","source_receipt","current_commit","original_commit","model_receipt","code_receipt"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=FORMAL_PREFLIGHT_SCHEMA: raise MemBenchError("membench_formal_preflight_invalid")
    candidate=validate_candidate_projection(value["candidate"]); public=value["public_results"]
    if not isinstance(public,Mapping) or public.get("schema")!=PUBLIC_RESULTS_SCHEMA or public.get("candidate_projection_sha256")!=candidate["projection_sha256"] or public.get("normalization_sha256")!=_normalization(candidate)["normalization_sha256"] or public.get("public_results_sha256")!=digest({k:v for k,v in public.items() if k!="public_results_sha256"}): raise MemBenchError("membench_formal_preflight_public_results_invalid")
    if candidate["source_receipt"]==SOURCE_RECEIPT or public.get("source_receipt")!=candidate["source_receipt"] or not all(isinstance(value[key],Mapping) and value[key].get("clean") is True for key in ("current_commit","original_commit")) or public.get("model_receipt")!=value["model_receipt"] or public.get("code_receipt")!=value["code_receipt"]: raise MemBenchError("membench_formal_preflight_receipt_invalid")
    release=value["release"]
    if not isinstance(release,Mapping) or release.get("schema")!=RELEASE_SCHEMA or release.get("candidate_projection_sha256")!=candidate["projection_sha256"] or release.get("public_results_sha256")!=public["public_results_sha256"] or release.get("custody_bytes_sha256")!=public.get("sealed_custody_sha256") or release.get("threshold_protocol_sha256")!=public.get("threshold_protocol_sha256") or release.get("checkpoint_sha256")!=public.get("checkpoint_sha256") or release.get("original_execution_policy_sha256")!=public.get("original_execution_policy_sha256"): raise MemBenchError("membench_formal_preflight_release_invalid")
    return {"schema":FORMAL_PREFLIGHT_SCHEMA,"candidate_projection_sha256":candidate["projection_sha256"],"public_results_sha256":public["public_results_sha256"],"release_sha256":release.get("release_sha256")}
def _metrics(ranked:Sequence[str],gold:Sequence[str])->dict[str,float]:
    top=list(ranked[:10]); targets=set(gold); hits=[i for i,x in enumerate(top,1) if x in targets]; ideal=math.fsum(1/math.log2(i+1) for i in range(1,min(10,len(targets))+1))
    return {"recall_at_10":len(hits)/len(targets),"ndcg_at_10":math.fsum(1/math.log2(i+1) for i in hits)/ideal,"mrr_at_10":1/hits[0] if hits else 0.0,"gold_hits_at_10":len(hits),"gold_denominator":len(targets)}

def _complete_build_delta(rows:Sequence[Mapping[str,Any]],rng:random.Random)->tuple[list[int],float]:
    """One complete-build draw shared by every sampled question row."""
    if not rows or any(not isinstance(row.get("original_replicates"),list) or len(row["original_replicates"])!=5 for row in rows): raise MemBenchError("membench_bootstrap_invalid")
    build_draw=[rng.randrange(5) for _ in range(5)]
    value=math.fsum(row["metrics"]["static_p5"]["recall_at_10"]-math.fsum(row["original_replicates"][index]["recall_at_10"] for index in build_draw)/len(build_draw) for row in rows)/len(rows)
    return build_draw,value
def score_synthetic_frozen(*,artifacts:Sequence[Mapping[str,Any]],candidate:Mapping[str,Any],custody:Mapping[str,Any],thresholds_by_dataset:Mapping[str,Mapping[str,float]],seed:int=20260823,resamples:int=5000)->dict[str,Any]:
    p=validate_candidate_projection(candidate); c=validate_custody(custody); arms={x.get("arm_id"):x for x in artifacts}
    if p["source_receipt"]!=SOURCE_RECEIPT or c["schema"]!=CUSTODY_SCHEMA: raise MemBenchError("membench_synthetic_input_required")
    if set(arms)!=set(ARMS): raise MemBenchError("membench_artifact_coverage_invalid")
    for arm in ARMS: _validate_artifact(arms[arm],p,arm)
    current={arm:{x["item_id"]:x for x in arms[arm]["rankings"]} for arm in CURRENT_ARMS}; originals=[{x["item_id"]:x for x in rep["rankings"]} for rep in arms["original_public_product"]["replicates"]]; datasets={}
    for record in c["records"]:
      original=[_metrics(x[record["item_id"]]["ranked_candidate_ids"],record["gold_candidate_ids"]) for x in originals]; metrics={arm:_metrics(current[arm][record["item_id"]]["ranked_candidate_ids"],record["gold_candidate_ids"]) for arm in CURRENT_ARMS}; metrics["original_public_product"]={key:math.fsum(x[key] for x in original)/5 for key in original[0]}
      datasets.setdefault(record["source_file_role"],[]).append({"item_id":record["item_id"],"group_id":record["group_id"],"strata":record["strata"],"metrics":metrics,"original_replicates":original})
    reports={}
    for dataset,rows in datasets.items():
      if dataset not in thresholds_by_dataset: raise MemBenchError("membench_dataset_threshold_missing")
      grouped={}
      for row in rows: grouped.setdefault(row["group_id"],[]).append(row)
      keys=sorted(grouped); rng=random.Random(seed); draws=[]
      for _ in range(resamples):
       delta=[]
       build_draw=[rng.randrange(5) for _ in range(5)]
       for group in [grouped[rng.choice(keys)] for _ in keys]:
        for row in group: delta.append(row["metrics"]["static_p5"]["recall_at_10"]-math.fsum(row["original_replicates"][index]["recall_at_10"] for index in build_draw)/len(build_draw))
       draws.append(math.fsum(delta)/len(delta))
      point=math.fsum(row["metrics"]["static_p5"]["recall_at_10"]-row["metrics"]["original_public_product"]["recall_at_10"] for row in rows)/len(rows); draws.sort(); lower=draws[int(.025*(resamples-1))]; hard=[row for row in rows if str(row["strata"].get("question_type"," ")).casefold()=="reflective"]; hard_point=None if not hard else math.fsum(row["metrics"]["static_p5"]["recall_at_10"]-row["metrics"]["original_public_product"]["recall_at_10"] for row in hard)/len(hard); threshold=thresholds_by_dataset[dataset]
      reports[dataset]={"rows":rows,"bootstrap":{"grouping":"opaque_source_role_tid","original_replicate_layer":True,"point_estimate":point,"ci_lower":lower,"ci_upper":draws[int(.975*(resamples-1))],"seed":seed,"resamples":resamples},"strata":{"factual_reflective_x_participation_observation":"reported_from_custody_at_formal_run"},"adversarial":"N/A:not_official_MemBench_protocol","abstention":"N/A:not_official_MemBench_protocol","gate":{"overall_delta_min":threshold["overall_delta_min"],"point_estimate":point,"ci_lower":lower,"reflective_point_estimate":hard_point,"outcome":"PASS" if point>=threshold["overall_delta_min"] and lower>0 and (hard_point is None or hard_point>=0) else "FAIL"}}
    report={"schema":SYNTHETIC_REPORT_SCHEMA,"source_receipt":p["source_receipt"],"dataset_reports":reports}; report["report_sha256"]=digest(report); return report

def _formal_public(value:Any,*,candidate:Mapping[str,Any],custody_sha256:str,threshold:Mapping[str,Any])->dict[str,Any]:
    required={"schema","source_receipt","candidate_projection_sha256","normalization_sha256","sealed_custody_sha256","threshold_protocol_sha256","checkpoint_sha256","original_execution_policy","original_execution_policy_sha256","artifact_files","current_worker_receipts","current_artifact_sha256","static_p5_repeat_sha256","original_artifact_sha256","original_replicate_receipts","ready_receipts","model_receipt","code_receipt","public_results_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=PUBLIC_RESULTS_SCHEMA or value.get("source_receipt")!=candidate["source_receipt"] or candidate["source_receipt"]==SOURCE_RECEIPT or value.get("candidate_projection_sha256")!=candidate["projection_sha256"] or value.get("normalization_sha256")!=_normalization(candidate)["normalization_sha256"] or value.get("sealed_custody_sha256")!=custody_sha256 or value.get("threshold_protocol_sha256")!=threshold["threshold_sha256"] or value.get("public_results_sha256")!=digest({key:child for key,child in value.items() if key!="public_results_sha256"}): raise MemBenchError("membench_formal_public_results_invalid")
    policy=_validate_original_execution_policy(value.get("original_execution_policy"))
    if not isinstance(value.get("checkpoint_sha256"),str) or len(value["checkpoint_sha256"])!=64 or value.get("original_execution_policy_sha256")!=policy["policy_sha256"] or not isinstance(value.get("model_receipt"),Mapping) or value["model_receipt"].get("sha256")!=ORIGINAL_MODEL_TREE or _validate_driver_code_receipt(value.get("code_receipt"))!=value.get("code_receipt"): raise MemBenchError("membench_formal_public_results_invalid")
    files=value.get("artifact_files")
    if not isinstance(files,Mapping) or set(files)!={*CURRENT_ROLES,"original_public_product"}: raise MemBenchError("membench_formal_public_results_invalid")
    loaded={role:_load_public_artifact_file(capability=files[role],candidate=candidate,arm=arm,role=role) for role,arm in CURRENT_ROLES.items()}
    _public_current_worker_receipts(receipts=value.get("current_worker_receipts"),files=files,loaded=loaded,checkpoint_sha256=value["checkpoint_sha256"],code_receipt=value["code_receipt"])
    original,original_ready=_load_public_artifact_file(capability=files["original_public_product"],candidate=candidate,arm="original_public_product",role=None)
    if not isinstance(original.get("runtime_receipts"),list) or len(original["runtime_receipts"])!=5 or original.get("coordinator_code_receipt")!=value["code_receipt"] or original.get("checkpoint_sha256")!=value["checkpoint_sha256"]: raise MemBenchError("membench_formal_public_results_invalid")
    _validate_formal_original_provenance(original,code_receipt=value["code_receipt"],checkpoint_sha256=value["checkpoint_sha256"],execution_policy=policy)
    if original_ready.get("original_execution_policy_sha256")!=policy["policy_sha256"]:
        raise MemBenchError("membench_formal_public_results_invalid")
    if _bytes(loaded["p5_primary"][0])!=_bytes(loaded["p5_repeat"][0]): raise MemBenchError("membench_static_p5_not_byte_identical")
    expected={"strong_raw":loaded["raw"][0]["artifact_sha256"],"static_p5":loaded["p5_primary"][0]["artifact_sha256"],"six_view_secondary":loaded["six"][0]["artifact_sha256"]}
    expected_ready={**{role:ready for role,(_artifact,ready) in loaded.items()},"original_public_product":original_ready}
    expected_replicates=[{key:item[key] for key in ("build_id","index_sha256","draft_sha256")} for item in original["replicates"]]
    if value.get("current_artifact_sha256")!=expected or value.get("static_p5_repeat_sha256")!=loaded["p5_repeat"][0]["artifact_sha256"] or value.get("original_artifact_sha256")!=original["artifact_sha256"] or value.get("ready_receipts")!=expected_ready or value.get("original_replicate_receipts")!=expected_replicates: raise MemBenchError("membench_formal_public_results_invalid")
    return {"public":dict(value),"artifacts":{**{arm:loaded[role][0] for role,arm in CURRENT_ROLES.items() if role!="p5_repeat"},"original_public_product":original},"original_replicates":original["replicates"]}

def _sealed_custody_ready_bytes_sha(path:Path)->str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file(): raise MemBenchError("membench_custody_ready_capability_invalid")
    ready=_load_canonical(path,"membench_custody_ready_invalid")
    if not isinstance(ready,Mapping) or ready.get("schema")!=READY_SCHEMA or ready.get("arm_id")!="formal_sealed_custody" or not isinstance(ready.get("payload_sha256"),str) or len(ready["payload_sha256"])!=64 or not isinstance(ready.get("custody_sha256"),str) or len(ready["custody_sha256"])!=64 or ready.get("ready_sha256")!=digest({key:child for key,child in ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_custody_ready_invalid")
    return ready["payload_sha256"]

def _verify_source_generation_receipts(*,candidate:Mapping[str,Any],candidate_ready_path:Path,custody_ready_path:Path,marker_path:Path,operator_secret:bytes,checkpoint:Mapping[str,Any],code_receipt:Mapping[str,Any])->str:
    candidate_bytes=_bytes(candidate); candidate_ready=_load_canonical(candidate_ready_path,"membench_candidate_ready_invalid"); custody_ready=_load_canonical(custody_ready_path,"membench_custody_ready_invalid"); marker=_load_canonical(marker_path,"membench_source_builder_marker_invalid")
    marker_sha=hashlib.sha256(_bytes(marker)).hexdigest(); source_sha=digest(candidate["source_receipt"])
    required={"schema","authorization_sha256","candidate_payload_sha256","custody_payload_sha256","candidate_projection_sha256","custody_sha256","source_receipt_sha256","acquisition_sha256","profile_role_file_map_sha256","checkpoint_sha256","code_receipt_sha256","generation_hmac"}
    unsigned={key:child for key,child in marker.items() if key!="generation_hmac"} if isinstance(marker,Mapping) else {}
    if not isinstance(marker,Mapping) or set(marker)!=required or marker.get("schema")!="aerp8-membench-source-builder-consumed-v1" or not hmac.compare_digest(str(marker.get("generation_hmac","")),_opaque(operator_secret,unsigned)) or marker.get("candidate_payload_sha256")!=hashlib.sha256(candidate_bytes).hexdigest() or marker.get("candidate_projection_sha256")!=candidate["projection_sha256"] or marker.get("source_receipt_sha256")!=source_sha or marker.get("acquisition_sha256")!=candidate["source_receipt"]["acquisition_sha256"] or marker.get("profile_role_file_map_sha256")!=candidate["source_receipt"]["profile_role_file_map_sha256"] or marker.get("checkpoint_sha256")!=checkpoint.get("checkpoint_sha256") or marker.get("code_receipt_sha256")!=code_receipt.get("code_sha256"): raise MemBenchError("membench_source_generation_receipt_invalid")
    if not isinstance(candidate_ready,Mapping) or candidate_ready.get("schema")!=READY_SCHEMA or candidate_ready.get("arm_id")!="formal_source_candidate" or candidate_ready.get("payload_sha256")!=hashlib.sha256(candidate_bytes).hexdigest() or candidate_ready.get("projection_sha256")!=candidate["projection_sha256"] or candidate_ready.get("source_receipt_sha256")!=source_sha or candidate_ready.get("source_builder_marker_sha256")!=marker_sha or candidate_ready.get("ready_sha256")!=digest({key:child for key,child in candidate_ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_source_generation_receipt_invalid")
    if not isinstance(custody_ready,Mapping) or custody_ready.get("schema")!=READY_SCHEMA or custody_ready.get("arm_id")!="formal_sealed_custody" or custody_ready.get("custody_sha256")!=marker.get("custody_sha256") or custody_ready.get("payload_sha256")!=marker.get("custody_payload_sha256") or custody_ready.get("source_receipt_sha256")!=source_sha or custody_ready.get("source_builder_marker_sha256")!=marker_sha or custody_ready.get("ready_sha256")!=digest({key:child for key,child in custody_ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_source_generation_receipt_invalid")
    return str(marker["custody_payload_sha256"])

def mint_formal_release(*,candidate_path:Path,custody_ready_path:Path,public_results_path:Path,threshold_protocol_path:Path,capability_secret_path:Path)->dict[str,Any]:
    paths=(candidate_path,custody_ready_path,public_results_path,threshold_protocol_path,capability_secret_path)
    if any(not path.is_absolute() or path.is_symlink() or not path.is_file() for path in paths): raise MemBenchError("membench_formal_release_capability_invalid")
    candidate_bytes=candidate_path.read_bytes(); public_bytes=public_results_path.read_bytes(); threshold_bytes=threshold_protocol_path.read_bytes(); secret=capability_secret_path.read_bytes(); custody_sha=_sealed_custody_ready_bytes_sha(custody_ready_path)
    try: candidate=validate_candidate_projection(json.loads(candidate_bytes)); public=json.loads(public_bytes); threshold=validate_threshold_protocol(json.loads(threshold_bytes))
    except (json.JSONDecodeError,MemBenchError) as exc: raise MemBenchError("membench_formal_release_input_invalid") from exc
    if _bytes(candidate)!=candidate_bytes or _bytes(public)!=public_bytes or _bytes(threshold)!=threshold_bytes: raise MemBenchError("membench_formal_release_input_invalid")
    _formal_public(public,candidate=candidate,custody_sha256=custody_sha,threshold=threshold)
    row={"schema":RELEASE_SCHEMA,"candidate_projection_sha256":candidate["projection_sha256"],"custody_bytes_sha256":custody_sha,"public_results_sha256":public["public_results_sha256"],"threshold_protocol_sha256":threshold["threshold_sha256"],"checkpoint_sha256":public["checkpoint_sha256"],"original_execution_policy_sha256":public["original_execution_policy_sha256"]}
    row["release_sha256"]=digest(row); row["capability_hmac"]=_opaque(secret,row); return row

def _formal_release(value:Any,*,candidate:Mapping[str,Any],custody_sha256:str,public:Mapping[str,Any],threshold:Mapping[str,Any],secret:bytes)->dict[str,Any]:
    required={"schema","candidate_projection_sha256","custody_bytes_sha256","public_results_sha256","threshold_protocol_sha256","checkpoint_sha256","original_execution_policy_sha256","release_sha256","capability_hmac"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=RELEASE_SCHEMA: raise MemBenchError("membench_formal_release_invalid")
    unsigned={key:child for key,child in value.items() if key not in {"release_sha256","capability_hmac"}}
    if value.get("release_sha256")!=digest(unsigned) or value.get("capability_hmac")!=_opaque(secret,{**unsigned,"release_sha256":value["release_sha256"]}) or value.get("candidate_projection_sha256")!=candidate["projection_sha256"] or value.get("custody_bytes_sha256")!=custody_sha256 or value.get("public_results_sha256")!=public.get("public_results_sha256") or value.get("threshold_protocol_sha256")!=threshold["threshold_sha256"] or value.get("checkpoint_sha256")!=public.get("checkpoint_sha256") or value.get("original_execution_policy_sha256")!=public.get("original_execution_policy_sha256"): raise MemBenchError("membench_formal_release_binding_invalid")
    return dict(value)

def _formal_rows(*,artifacts:Mapping[str,Any],custody:Mapping[str,Any],threshold:Mapping[str,Any])->dict[str,Any]:
    current={arm:{row["item_id"]:row for row in artifacts[arm]["rankings"]} for arm in CURRENT_ARMS}; originals=[{row["item_id"]:row for row in rep["rankings"]} for rep in artifacts["original_public_product"]["replicates"]]
    rows=[]
    for record in custody["records"]:
        role=record["source_file_role"]
        if role not in FORMAL_SOURCE_ROLES: raise MemBenchError("membench_formal_custody_role_invalid")
        metric={arm:_metrics(current[arm][record["item_id"]]["ranked_candidate_ids"],record["gold_candidate_ids"]) for arm in CURRENT_ARMS}
        original=[_metrics(rep[record["item_id"]]["ranked_candidate_ids"],record["gold_candidate_ids"]) for rep in originals]
        metric["original_public_product"]={key:math.fsum(item[key] for item in original)/5 for key in original[0]}
        rows.append({"group_id":record["group_id"],"source_role":role,"profile_id":record["profile_id"],"item_id":record["item_id"],"metrics":metric,"original_replicates":original,"question_type":record["question_type"],"scenario":record["scenario"]})
    def summary(selected:list[dict[str,Any]], *, strata:bool)->dict[str,Any]:
        if not selected: raise MemBenchError("membench_required_stratum_empty")
        grouped={}
        for row in selected: grouped.setdefault(row["source_role"] if strata else "all",{}).setdefault(row["group_id"],[]).append(row)
        if strata and set(grouped)!={row["source_role"] for row in selected}: raise MemBenchError("membench_bootstrap_invalid")
        rng=random.Random(threshold["seed"]); draws=[]
        for _ in range(threshold["resamples"]):
            draw=[]
            for role in sorted(grouped):
                groups=grouped[role]
                keys=sorted(groups)
                for _number in keys: draw.extend(groups[rng.choice(keys)])
            # Each sampled original index is a complete fresh index build and is
            # shared by all rows in this draw.  Never manufacture a row-wise
            # mixture of builds that did not exist in the experiment.
            _build_draw,delta=_complete_build_delta(draw,rng)
            draws.append(delta)
        draws.sort(); point=math.fsum(row["metrics"]["static_p5"]["recall_at_10"]-row["metrics"]["original_public_product"]["recall_at_10"] for row in selected)/len(selected)
        metric_keys=("recall_at_10","ndcg_at_10","mrr_at_10","gold_hits_at_10","gold_denominator")
        metrics={arm:{key:math.fsum(row["metrics"][arm][key] for row in selected)/len(selected) for key in metric_keys} for arm in ARMS}
        units={}
        for row in selected: units.setdefault(row["group_id"],[]).append(row)
        unit_metrics={arm:{key:math.fsum(math.fsum(row["metrics"][arm][key] for row in unit)/len(unit) for unit in units.values())/len(units) for key in metric_keys} for arm in ARMS}
        evidence_micro={arm:math.fsum(row["metrics"][arm]["gold_hits_at_10"] for row in selected)/math.fsum(row["metrics"][arm]["gold_denominator"] for row in selected) for arm in ARMS}
        return {"n_rows":len(selected),"n_groups":len(units),"metrics":metrics,"question_macro":metrics,"leakage_unit_macro":unit_metrics,"evidence_micro_recall_at_10":evidence_micro,"p5_minus_original_recall_at_10":{"point":point,"ci_lower":draws[int(.025*(len(draws)-1))],"ci_upper":draws[int(.975*(len(draws)-1))],"seed":threshold["seed"],"resamples":threshold["resamples"],"original_replicate_layer":"complete_build_draw"}}
    role_reports={role:summary([row for row in rows if row["source_role"]==role],strata=False) for role in sorted(FORMAL_SOURCE_ROLES)}
    factual=summary([row for row in rows if row["source_role"].endswith("factual")],strata=True); reflective=summary([row for row in rows if row["source_role"].endswith("reflective")],strata=True); participation=summary([row for row in rows if row["source_role"].startswith("participation")],strata=True); observation=summary([row for row in rows if row["source_role"].startswith("observation")],strata=True); overall=summary(rows,strata=True)
    delta=overall["p5_minus_original_recall_at_10"]; hard=reflective["p5_minus_original_recall_at_10"]
    profiles={profile:summary([row for row in rows if row["profile_id"]==profile],strata=True) for profile in ("0","100")}
    return {"schema":REPORT_SCHEMA,"source_receipt":custody["source_receipt"],"dataset":"MemBench","cross_source_roles":role_reports,"profile_secondary":profiles,"aggregates":{"factual":factual,"reflective":reflective,"participation":participation,"observation":observation,"overall":overall},"gate":{"primary":{"point":delta["point"],"ci_lower":delta["ci_lower"],"outcome":"PASS" if delta["point"]>=threshold["overall_delta_min"] and delta["ci_lower"]>threshold["overall_ci_lower_min"] else "FAIL"},"hard_reflective":{"point":hard["point"],"ci_lower":hard["ci_lower"],"outcome":"PASS" if hard["point"]>=threshold["hard_delta_min"] and hard["ci_lower"]>=threshold["hard_ci_lower_min"] else "FAIL"},"adversarial":"N/A:not_official_MemBench_protocol","abstention":"N/A:not_official_MemBench_protocol"}}
def publish_nonreplace(*,path:Path,payload:bytes,before_publish:Callable[[],None]|None=None)->dict[str,Any]:
    if before_publish: before_publish()
    try: fd=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
      if not path.is_file() or path.is_symlink() or path.read_bytes()!=payload: raise MemBenchError("membench_publish_conflict")
      return {"published":False,"retry_idempotent":True,"sha256":hashlib.sha256(payload).hexdigest()}
    try:
      with os.fdopen(fd,"wb") as handle: handle.write(payload); handle.flush(); os.fsync(handle.fileno())
    except BaseException:
      try:path.unlink()
      except OSError:pass
      raise
    return {"published":True,"retry_idempotent":False,"sha256":hashlib.sha256(payload).hexdigest()}
def publish_ready_payload(*,output_dir:Path,arm_id:str,payload:Mapping[str,Any])->dict[str,Any]:
    if arm_id not in {*CURRENT_ARMS,"static_p5_repeat","original_public_product"}: raise MemBenchError("membench_ready_invalid")
    output_dir.mkdir(parents=True,exist_ok=True); packet=_bytes(payload); publish_nonreplace(path=output_dir/f"{arm_id}.json",payload=packet); ready={"schema":READY_SCHEMA,"arm_id":arm_id,"payload_sha256":hashlib.sha256(packet).hexdigest()}; ready["ready_sha256"]=digest(ready); publish_nonreplace(path=output_dir/f"{arm_id}.READY.json",payload=_bytes(ready)); return ready

def validate_formal_custodian_config(value:Any)->dict[str,Any]:
    required={"schema","candidate_path","candidate_ready_path","source_builder_marker_path","custody_path","custody_ready_path","public_results_path","release_path","threshold_protocol_path","capability_secret_path","operator_capability_secret_path","expected_checkpoint_path","report_path","ready_path","consumed_marker_path"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=CUSTODIAN_CONFIG_SCHEMA: raise MemBenchError("membench_custodian_config_invalid")
    row=dict(value)
    for key in required-{"schema"}:
        if not isinstance(row[key],str): raise MemBenchError("membench_custodian_path_invalid")
        path=Path(row[key])
        if not path.is_absolute(): raise MemBenchError("membench_custodian_path_invalid")
        # A completed retry must not even stat the sealed custody capability.
        # Its leaf validation is therefore deferred until after the completed
        # receipt has been authenticated; every other capability is resolved now.
        row[key]=str(path) if key=="custody_path" else str(path.resolve())
        if key!="custody_path" and path.is_symlink(): raise MemBenchError("membench_custodian_path_invalid")
    inputs=("candidate_path","candidate_ready_path","source_builder_marker_path","custody_ready_path","public_results_path","release_path","threshold_protocol_path","capability_secret_path","operator_capability_secret_path","expected_checkpoint_path")
    if any(not Path(row[key]).is_file() for key in inputs): raise MemBenchError("membench_custodian_input_missing")
    outputs=("report_path","ready_path","consumed_marker_path")
    parent=Path(row["report_path"]).parent
    if not parent.is_dir() or parent.is_symlink() or any(Path(row[key]).parent!=parent for key in outputs) or len({row[key] for key in outputs})!=len(outputs) or set(row[key] for key in outputs)&set(row[key] for key in inputs): raise MemBenchError("membench_custodian_output_invalid")
    if any(Path(row[key]).exists() and (not Path(row[key]).is_file() or Path(row[key]).is_symlink()) for key in outputs): raise MemBenchError("membench_custodian_output_invalid")
    return row

def _load_canonical(path:Path,code:str)->Any:
    data=path.read_bytes()
    try: value=json.loads(data)
    except json.JSONDecodeError as exc: raise MemBenchError(code) from exc
    if _bytes(value)!=data: raise MemBenchError(code)
    return value

def validate_formal_source_builder_config(value:Any)->dict[str,Any]:
    required={"schema","manifest_path","acquisition_receipt_path","opacity_secret_path","operator_capability_secret_path","authorization_path","candidate_path","candidate_ready_path","custody_path","custody_ready_path","consumed_marker_path","expected_checkpoint_path","code_receipt"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=SOURCE_BUILDER_CONFIG_SCHEMA: raise MemBenchError("membench_source_builder_config_invalid")
    row=dict(value)
    for key in required-{"schema","code_receipt"}:
        if not isinstance(row[key],str): raise MemBenchError("membench_source_builder_path_invalid")
        path=Path(row[key])
        if not path.is_absolute() or path.is_symlink(): raise MemBenchError("membench_source_builder_path_invalid")
        row[key]=str(path.resolve())
    _validate_driver_code_receipt(row["code_receipt"])
    if row["code_receipt"]!=_driver_code_receipt(): raise MemBenchError("membench_source_builder_code_drift")
    inputs=("manifest_path","acquisition_receipt_path","opacity_secret_path","operator_capability_secret_path","authorization_path","expected_checkpoint_path")
    if any(not Path(row[key]).is_file() for key in inputs): raise MemBenchError("membench_source_builder_input_missing")
    outputs=("candidate_path","candidate_ready_path","custody_path","custody_ready_path","consumed_marker_path")
    parent=Path(row["candidate_path"]).parent
    if not parent.is_dir() or parent.is_symlink() or any(Path(row[key]).parent!=parent for key in outputs) or len({row[key] for key in outputs})!=len(outputs) or set(row[key] for key in outputs)&set(row[key] for key in inputs) or any(Path(row[key]).exists() and (not Path(row[key]).is_file() or Path(row[key]).is_symlink()) for key in outputs): raise MemBenchError("membench_source_builder_output_invalid")
    return row

def _source_builder_authorization(value:Any,*,manifest:Mapping[str,Any],config:Mapping[str,Any],operator_secret:bytes)->dict[str,Any]:
    outputs=("candidate_path","candidate_ready_path","custody_path","custody_ready_path","consumed_marker_path"); required={"schema","manifest_sha256","code_sha256","authorization_nonce","expires_at_unix","output_absent","authorization_sha256","authorization_hmac",*outputs}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=SOURCE_BUILDER_AUTH_SCHEMA or value.get("manifest_sha256")!=manifest["manifest_sha256"] or value.get("code_sha256")!=config["code_receipt"].get("code_sha256") or any(value.get(key)!=config[key] for key in outputs) or not isinstance(value.get("authorization_nonce"),str) or len(value["authorization_nonce"])<32 or value.get("output_absent") is not True or isinstance(value.get("expires_at_unix"),bool) or not isinstance(value.get("expires_at_unix"),int) or value["expires_at_unix"]<=int(time.time()): raise MemBenchError("membench_source_builder_authorization_invalid")
    unsigned={key:child for key,child in value.items() if key not in {"authorization_sha256","authorization_hmac"}}
    expected_sha=digest(unsigned)
    expected_hmac=_opaque(operator_secret,{**unsigned,"authorization_sha256":expected_sha})
    if not isinstance(value.get("authorization_sha256"),str) or not isinstance(value.get("authorization_hmac"),str) or not hmac.compare_digest(value["authorization_sha256"],expected_sha) or not hmac.compare_digest(value["authorization_hmac"],expected_hmac): raise MemBenchError("membench_source_builder_authorization_invalid")
    return dict(value)

def _verify_completed_source_builder(*,config:Mapping[str,Any],authorization:Mapping[str,Any]|None=None,operator_secret:bytes,checkpoint:Mapping[str,Any])->dict[str,Any]|None:
    paths=tuple(Path(config[key]) for key in ("candidate_path","candidate_ready_path","custody_path","custody_ready_path","consumed_marker_path")); present=tuple(path.exists() for path in paths)
    if not any(present): return None
    if not all(present): raise MemBenchError("membench_source_builder_partial_output")
    candidate=_load_canonical(Path(config["candidate_path"]),"membench_source_builder_candidate_invalid"); custody=_load_canonical(Path(config["custody_path"]),"membench_source_builder_custody_invalid"); candidate=validate_candidate_projection(candidate); custody=validate_custody(custody)
    if candidate["source_receipt"]!=custody["source_receipt"] or candidate["source_receipt"]==SOURCE_RECEIPT: raise MemBenchError("membench_source_builder_completed_invalid")
    _cross_bind_candidate_custody(candidate=candidate,custody=custody)
    candidate_bytes=_bytes(candidate); custody_bytes=_bytes(custody); candidate_ready=_load_canonical(Path(config["candidate_ready_path"]),"membench_source_builder_ready_invalid"); custody_ready=_load_canonical(Path(config["custody_ready_path"]),"membench_source_builder_ready_invalid"); marker=_load_canonical(Path(config["consumed_marker_path"]),"membench_source_builder_marker_invalid")
    marker_sha=hashlib.sha256(_bytes(marker)).hexdigest()
    if not isinstance(candidate_ready,Mapping) or candidate_ready.get("schema")!=READY_SCHEMA or candidate_ready.get("arm_id")!="formal_source_candidate" or candidate_ready.get("payload_sha256")!=hashlib.sha256(candidate_bytes).hexdigest() or candidate_ready.get("projection_sha256")!=candidate["projection_sha256"] or candidate_ready.get("source_receipt_sha256")!=digest(candidate["source_receipt"]) or candidate_ready.get("source_builder_marker_sha256")!=marker_sha or candidate_ready.get("ready_sha256")!=digest({key:child for key,child in candidate_ready.items() if key!="ready_sha256"}) or not isinstance(custody_ready,Mapping) or custody_ready.get("schema")!=READY_SCHEMA or custody_ready.get("arm_id")!="formal_sealed_custody" or custody_ready.get("payload_sha256")!=hashlib.sha256(custody_bytes).hexdigest() or custody_ready.get("custody_sha256")!=custody["custody_sha256"] or custody_ready.get("source_receipt_sha256")!=digest(custody["source_receipt"]) or custody_ready.get("source_builder_marker_sha256")!=marker_sha or custody_ready.get("ready_sha256")!=digest({key:child for key,child in custody_ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_source_builder_completed_invalid")
    required={"schema","authorization_sha256","candidate_payload_sha256","custody_payload_sha256","candidate_projection_sha256","custody_sha256","source_receipt_sha256","acquisition_sha256","profile_role_file_map_sha256","checkpoint_sha256","code_receipt_sha256","generation_hmac"}
    unsigned={key:child for key,child in marker.items() if key!="generation_hmac"} if isinstance(marker,Mapping) else {}
    if not isinstance(marker,Mapping) or set(marker)!=required or marker.get("schema")!="aerp8-membench-source-builder-consumed-v1" or not hmac.compare_digest(str(marker.get("generation_hmac","")),_opaque(operator_secret,unsigned)) or marker.get("candidate_payload_sha256")!=hashlib.sha256(candidate_bytes).hexdigest() or marker.get("custody_payload_sha256")!=hashlib.sha256(custody_bytes).hexdigest() or marker.get("candidate_projection_sha256")!=candidate["projection_sha256"] or marker.get("custody_sha256")!=custody["custody_sha256"] or marker.get("source_receipt_sha256")!=digest(candidate["source_receipt"]) or marker.get("acquisition_sha256")!=candidate["source_receipt"]["acquisition_sha256"] or marker.get("profile_role_file_map_sha256")!=candidate["source_receipt"]["profile_role_file_map_sha256"] or marker.get("checkpoint_sha256")!=checkpoint.get("checkpoint_sha256") or marker.get("code_receipt_sha256")!=config["code_receipt"]["code_sha256"] or marker.get("authorization_sha256")!=(authorization or {}).get("authorization_sha256"): raise MemBenchError("membench_source_builder_marker_invalid")
    return {"candidate":candidate,"custody_ready":dict(custody_ready),"retry_idempotent":True,"read_source_files":False}

def run_formal_source_builder(config:Mapping[str,Any])->dict[str,Any]:
    """Build opaque ranking input and sealed custody from one frozen source manifest."""
    row=validate_formal_source_builder_config(config)
    operator_secret=Path(row["operator_capability_secret_path"]).read_bytes()
    checkpoint=_require_external_current_checkpoint(Path(row["expected_checkpoint_path"]))
    manifest=_load_canonical(Path(row["manifest_path"]),"membench_source_manifest_invalid"); manifest=validate_source_manifest(manifest)
    acquisition=_load_canonical(Path(row["acquisition_receipt_path"]),"membench_acquisition_receipt_invalid"); acquisition=validate_acquisition_receipt(acquisition,manifest=manifest,operator_secret=operator_secret)
    authorization=_load_canonical(Path(row["authorization_path"]),"membench_source_builder_authorization_invalid"); authorization=_source_builder_authorization(authorization,manifest=manifest,config=row,operator_secret=operator_secret)
    completed=_verify_completed_source_builder(config=row,authorization=authorization,operator_secret=operator_secret,checkpoint=checkpoint)
    if completed is not None: return completed
    secret=Path(row["opacity_secret_path"]).read_bytes()
    source_bytes={}
    acquisition_files={(item["profile_id"],item["source_role"]):item for item in acquisition["files"]}
    data_root=Path(acquisition["data_root"])
    for profile in manifest["profiles"]:
        acquisition_file=acquisition_files[(profile["profile_id"],profile["source_role"])]
        path=_under(data_root/profile["inventory_relative_path"],data_root,"membench_acquisition_path_escape")
        if not path.is_file() or path.is_symlink(): raise MemBenchError("membench_source_file_missing")
        raw=path.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=acquisition_file["source_file_sha256"]: raise MemBenchError("membench_source_file_digest_invalid")
        source_bytes[(profile["profile_id"],profile["source_role"])]=raw
    candidate,custody=_build_formal_bundles(manifest=manifest,source_bytes=source_bytes,acquisition=acquisition,opacity_secret=secret); candidate_bytes=_bytes(candidate); custody_bytes=_bytes(custody)
    staging={key:Path(row[key]).with_name(Path(row[key]).name+".staging") for key in ("candidate_path","custody_path")}
    if any(path.exists() for path in staging.values()): raise MemBenchError("membench_source_builder_staging_present")
    identities={key:None for key in ("candidate_staging","candidate","candidate_ready","custody_staging","custody","custody_ready","marker")}
    def publish_once(*,path:Path,payload:bytes,key:str)->None:
        result=publish_nonreplace(path=path,payload=payload)
        if not result["published"]: raise MemBenchError("membench_source_builder_output_race")
        stat=path.stat(); identities[key]=(stat.st_dev,stat.st_ino)
    try:
        publish_once(path=staging["candidate_path"],payload=candidate_bytes,key="candidate_staging")
        if staging["candidate_path"].read_bytes()!=candidate_bytes: raise MemBenchError("membench_source_builder_staging_tamper")
        publish_once(path=Path(row["candidate_path"]),payload=candidate_bytes,key="candidate"); _remove_owned(staging["candidate_path"],identities["candidate_staging"]); identities["candidate_staging"]=None
        marker={"schema":"aerp8-membench-source-builder-consumed-v1","authorization_sha256":authorization["authorization_sha256"],"candidate_payload_sha256":hashlib.sha256(candidate_bytes).hexdigest(),"custody_payload_sha256":hashlib.sha256(custody_bytes).hexdigest(),"candidate_projection_sha256":candidate["projection_sha256"],"custody_sha256":custody["custody_sha256"],"source_receipt_sha256":digest(candidate["source_receipt"]),"acquisition_sha256":candidate["source_receipt"]["acquisition_sha256"],"profile_role_file_map_sha256":candidate["source_receipt"]["profile_role_file_map_sha256"],"checkpoint_sha256":checkpoint["checkpoint_sha256"],"code_receipt_sha256":row["code_receipt"]["code_sha256"]}; marker["generation_hmac"]=_opaque(operator_secret,marker)
        marker_sha=hashlib.sha256(_bytes(marker)).hexdigest()
        candidate_ready={"schema":READY_SCHEMA,"arm_id":"formal_source_candidate","payload_sha256":hashlib.sha256(candidate_bytes).hexdigest(),"projection_sha256":candidate["projection_sha256"],"source_receipt_sha256":digest(candidate["source_receipt"]),"source_builder_marker_sha256":marker_sha}; candidate_ready["ready_sha256"]=digest(candidate_ready); publish_once(path=Path(row["candidate_ready_path"]),payload=_bytes(candidate_ready),key="candidate_ready")
        publish_once(path=staging["custody_path"],payload=custody_bytes,key="custody_staging")
        if staging["custody_path"].read_bytes()!=custody_bytes: raise MemBenchError("membench_source_builder_staging_tamper")
        publish_once(path=Path(row["custody_path"]),payload=custody_bytes,key="custody"); _remove_owned(staging["custody_path"],identities["custody_staging"]); identities["custody_staging"]=None
        custody_ready={"schema":READY_SCHEMA,"arm_id":"formal_sealed_custody","payload_sha256":hashlib.sha256(custody_bytes).hexdigest(),"custody_sha256":custody["custody_sha256"],"source_receipt_sha256":digest(custody["source_receipt"]),"source_builder_marker_sha256":marker_sha}; custody_ready["ready_sha256"]=digest(custody_ready); publish_once(path=Path(row["custody_ready_path"]),payload=_bytes(custody_ready),key="custody_ready")
        publish_once(path=Path(row["consumed_marker_path"]),payload=_bytes(marker),key="marker")
    except BaseException:
        for key,path in (("marker",Path(row["consumed_marker_path"])),("custody_ready",Path(row["custody_ready_path"])),("custody",Path(row["custody_path"])),("custody_staging",staging["custody_path"]),("candidate_ready",Path(row["candidate_ready_path"])),("candidate",Path(row["candidate_path"])),("candidate_staging",staging["candidate_path"])): _remove_owned(path,identities[key])
        raise
    return {"candidate":candidate,"custody_ready":custody_ready,"retry_idempotent":False,"read_source_files":True}

def _verify_completed_custody_run(*,config:Mapping[str,Any],release:Mapping[str,Any],candidate:Mapping[str,Any],public:Mapping[str,Any],threshold:Mapping[str,Any],custody_sha256:str,secret:bytes)->dict[str,Any]|None:
    marker_path=Path(config["consumed_marker_path"])
    paths=tuple(Path(config[key]) for key in ("report_path","ready_path","consumed_marker_path"))
    present=tuple(path.exists() for path in paths)
    if not any(present): return None
    if not all(present): raise MemBenchError("membench_custodian_partial_output")
    marker=_load_canonical(marker_path,"membench_custodian_marker_invalid")
    required={"schema","release_sha256","report_sha256"}
    if not isinstance(marker,Mapping) or set(marker)!=required or marker.get("schema")!="aerp8-membench-consumed-release-v2" or marker.get("release_sha256")!=release.get("release_sha256"): raise MemBenchError("membench_custodian_marker_invalid")
    report=_load_canonical(Path(config["report_path"]),"membench_custodian_report_invalid"); ready=_load_canonical(Path(config["ready_path"]),"membench_custodian_ready_invalid")
    report_bytes=_bytes(report)
    required_report={"schema","source_receipt","dataset","cross_source_roles","profile_secondary","aggregates","gate","candidate_projection_sha256","custody_bytes_sha256","public_results_sha256","release_sha256","threshold_protocol_sha256","report_sha256","report_hmac"}
    unsigned={key:child for key,child in report.items() if key not in {"report_sha256","report_hmac"}} if isinstance(report,Mapping) else {}
    if not isinstance(report,Mapping) or set(report)!=required_report or report.get("schema")!=REPORT_SCHEMA or report.get("report_sha256")!=digest(unsigned) or not hmac.compare_digest(str(report.get("report_hmac","")),_opaque(secret,{**unsigned,"report_sha256":report.get("report_sha256")})) or report.get("candidate_projection_sha256")!=candidate["projection_sha256"] or report.get("custody_bytes_sha256")!=custody_sha256 or report.get("public_results_sha256")!=public["public_results_sha256"] or report.get("release_sha256")!=release["release_sha256"] or report.get("threshold_protocol_sha256")!=threshold["threshold_sha256"] or report.get("source_receipt")!=candidate["source_receipt"]: raise MemBenchError("membench_custodian_report_invalid")
    if marker["report_sha256"]!=hashlib.sha256(report_bytes).hexdigest() or not isinstance(ready,Mapping) or ready.get("schema")!=READY_SCHEMA or ready.get("arm_id")!="formal_custody_report" or ready.get("payload_sha256")!=hashlib.sha256(report_bytes).hexdigest() or ready.get("report_sha256")!=report["report_sha256"] or ready.get("release_sha256")!=release["release_sha256"] or ready.get("ready_sha256")!=digest({key:child for key,child in ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_custodian_marker_invalid")
    return {"report":dict(report),"retry_idempotent":True,"opened_custody":False}

def run_formal_custodian(config:Mapping[str,Any])->dict[str,Any]:
    """The sole formal scorer: validates public/release bytes before custody opens."""
    row=validate_formal_custodian_config(config)
    checkpoint=_require_external_current_checkpoint(Path(row["expected_checkpoint_path"]))
    candidate=_load_canonical(Path(row["candidate_path"]),"membench_custodian_candidate_invalid"); candidate=validate_candidate_projection(candidate)
    threshold=_load_canonical(Path(row["threshold_protocol_path"]),"membench_custodian_threshold_invalid"); threshold=validate_threshold_protocol(threshold)
    public=_load_canonical(Path(row["public_results_path"]),"membench_custodian_public_invalid")
    if not isinstance(public,Mapping) or public.get("code_receipt")!=checkpoint.get("driver_code_receipt") or public.get("checkpoint_sha256")!=checkpoint.get("checkpoint_sha256") or public.get("original_execution_policy")!=checkpoint.get("original_execution_policy") or public.get("original_execution_policy_sha256")!=checkpoint.get("original_execution_policy_sha256"):
        raise MemBenchError("membench_custodian_checkpoint_public_mismatch")
    custody_path=Path(row["custody_path"]); operator_secret=Path(row["operator_capability_secret_path"]).read_bytes(); custody_sha=_verify_source_generation_receipts(candidate=candidate,candidate_ready_path=Path(row["candidate_ready_path"]),custody_ready_path=Path(row["custody_ready_path"]),marker_path=Path(row["source_builder_marker_path"]),operator_secret=operator_secret,checkpoint=checkpoint,code_receipt=checkpoint["driver_code_receipt"]); secret=Path(row["capability_secret_path"]).read_bytes(); release=_load_canonical(Path(row["release_path"]),"membench_custodian_release_invalid")
    checked=_formal_public(public,candidate=candidate,custody_sha256=custody_sha,threshold=threshold); release=_formal_release(release,candidate=candidate,custody_sha256=custody_sha,public=checked["public"],threshold=threshold,secret=secret)
    completed=_verify_completed_custody_run(config=row,release=release,candidate=candidate,public=checked["public"],threshold=threshold,custody_sha256=custody_sha,secret=secret)
    if completed is not None: return completed
    staging=Path(row["report_path"]).with_name(Path(row["report_path"]).name+".staging")
    if staging.exists(): raise MemBenchError("membench_custodian_staging_present")
    if custody_path.is_symlink() or not custody_path.is_file(): raise MemBenchError("membench_custodian_custody_missing")
    custody_bytes=custody_path.read_bytes()
    try: custody=json.loads(custody_bytes)
    except json.JSONDecodeError as exc: raise MemBenchError("membench_custodian_custody_invalid") from exc
    if _bytes(custody)!=custody_bytes: raise MemBenchError("membench_custodian_custody_invalid")
    custody=_formal_custody(custody)
    if custody["custody_sha256"]!=digest({key:child for key,child in custody.items() if key!="custody_sha256"}) or hashlib.sha256(custody_bytes).hexdigest()!=custody_sha: raise MemBenchError("membench_custodian_custody_invalid")
    _cross_bind_candidate_custody(candidate=candidate,custody=custody)
    report=_formal_rows(artifacts=checked["artifacts"],custody=custody,threshold=threshold); report.update({"candidate_projection_sha256":candidate["projection_sha256"],"custody_bytes_sha256":custody_sha,"public_results_sha256":checked["public"]["public_results_sha256"],"release_sha256":release["release_sha256"],"threshold_protocol_sha256":threshold["threshold_sha256"]}); report["report_sha256"]=digest(report); report["report_hmac"]=_opaque(secret,{key:child for key,child in report.items() if key!="report_hmac"}); report_bytes=_bytes(report)
    identities={"staging":None,"report":None,"ready":None,"marker":None}
    def publish_owned_once(*,path:Path,payload:bytes,key:str)->None:
        published=publish_nonreplace(path=path,payload=payload)
        if not published["published"]: raise MemBenchError("membench_custodian_output_race")
        stat=path.stat(); identities[key]=(stat.st_dev,stat.st_ino)
    try:
        publish_owned_once(path=staging,payload=report_bytes,key="staging")
        if staging.read_bytes()!=report_bytes: raise MemBenchError("membench_custodian_staging_tamper")
        publish_owned_once(path=Path(row["report_path"]),payload=report_bytes,key="report")
        _remove_owned(staging,identities["staging"]); identities["staging"]=None
        ready={"schema":READY_SCHEMA,"arm_id":"formal_custody_report","payload_sha256":hashlib.sha256(report_bytes).hexdigest(),"report_sha256":report["report_sha256"],"release_sha256":release["release_sha256"]}; ready["ready_sha256"]=digest(ready); publish_owned_once(path=Path(row["ready_path"]),payload=_bytes(ready),key="ready")
        marker={"schema":"aerp8-membench-consumed-release-v2","release_sha256":release["release_sha256"],"report_sha256":hashlib.sha256(report_bytes).hexdigest()}; publish_owned_once(path=Path(row["consumed_marker_path"]),payload=_bytes(marker),key="marker")
    except BaseException:
        for key,path in (("marker",Path(row["consumed_marker_path"])),("ready",Path(row["ready_path"])),("report",Path(row["report_path"])),("staging",staging)): _remove_owned(path,identities[key])
        raise
    return {"report":report,"retry_idempotent":False,"opened_custody":True}
def run_synthetic_candidate_worker_subprocess(*,role:str,candidate:Mapping[str,Any],encoder_spec:Mapping[str,Any],python:str=sys.executable)->Mapping[str,Any]:
    """Explicit synthetic-only test helper; never routes to formal CLI."""
    p=validate_candidate_projection(candidate)
    request={"schema":"aerp8-membench-synthetic-current-worker-request-v1","role":role,"candidate":p,"encoder_spec":dict(encoder_spec)}
    result=subprocess.run([python,"-m","benchmarks.aerp8_membench","--synthetic-candidate-worker"],input=_bytes(request),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if result.returncode: raise MemBenchError("membench_synthetic_candidate_worker_failed:"+result.stderr.decode("utf-8","replace")[:200])
    try:return json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise MemBenchError("membench_synthetic_candidate_worker_output_invalid") from exc

def run_worker_subprocess(**_forbidden:Any)->Mapping[str,Any]:
    raise MemBenchError("membench_formal_current_requires_isolated_worker_contract")
def _sha256_file(path:Path)->str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def _under(path:Path, root:Path, code:str)->Path:
    resolved=path.resolve()
    try: resolved.relative_to(root.resolve())
    except ValueError as exc: raise MemBenchError(code) from exc
    return resolved

def _git_capability()->dict[str,Any]:
    found=shutil.which("git")
    if not found: raise MemBenchError("membench_git_capability_missing")
    executable=Path(found).resolve()
    if not executable.is_file() or executable.is_symlink(): raise MemBenchError("membench_git_capability_invalid")
    version=subprocess.run([str(executable),"--version"],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if version.returncode or not version.stdout.strip(): raise MemBenchError("membench_git_capability_invalid")
    # First prove whether the Git parent alone works.  Add Windows System32 only
    # when this particular executable demonstrably needs it.
    minimal={"PATH":str(executable.parent)}
    for key in ("SYSTEMROOT","WINDIR"):
        if os.environ.get(key): minimal[key]=str(os.environ[key])
    probe=subprocess.run([str(executable),"--version"],env=minimal,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    system32_required=False
    if probe.returncode:
        system_root=os.environ.get("SYSTEMROOT")
        system32=Path(system_root,"System32") if system_root else None
        if system32 is None or not system32.is_dir(): raise MemBenchError("membench_git_capability_isolation_failed")
        minimal["PATH"]=os.pathsep.join((str(executable.parent),str(system32)))
        probe=subprocess.run([str(executable),"--version"],env=minimal,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
        if probe.returncode: raise MemBenchError("membench_git_capability_isolation_failed")
        system32_required=True
    value={"executable":str(executable),"sha256":_sha256_file(executable),"version":version.stdout.decode("utf-8","strict").strip(),"system32_required":system32_required}
    return value

def _verify_git_capability(*, executable:Path, sha256:str, version:str, system32_required:bool)->dict[str,Any]:
    if not executable.is_absolute() or executable.is_symlink() or not executable.is_file() or not isinstance(sha256,str) or not isinstance(version,str) or not isinstance(system32_required,bool): raise MemBenchError("membench_git_capability_invalid")
    if _sha256_file(executable)!=sha256: raise MemBenchError("membench_git_capability_drift")
    env=_scrubbed_original_env(original_python=Path(sys.executable),rpg_root=Path(__file__).resolve().parents[1],git_executable=executable,git_system32_required=system32_required,home_dir=Path(__file__).resolve().parents[1])
    found=shutil.which("git",path=env["PATH"])
    if found is None or Path(found).resolve()!=executable.resolve(): raise MemBenchError("membench_git_capability_path_drift")
    result=subprocess.run([str(executable),"--version"],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if result.returncode or result.stdout.decode("utf-8","strict").strip()!=version: raise MemBenchError("membench_git_capability_drift")
    return {"executable":str(executable.resolve()),"sha256":sha256,"version":version,"system32_required":system32_required}

def _scrubbed_original_env(*, original_python:Path, rpg_root:Path, git_executable:Path, git_system32_required:bool, home_dir:Path)->dict[str,str]:
    """The child gets import/root capabilities only, never inherited benchmark state."""
    git=git_executable.resolve()
    home=home_dir.resolve()
    if not git.is_file() or git.is_symlink() or not isinstance(git_system32_required,bool): raise MemBenchError("membench_git_capability_invalid")
    if not home_dir.is_absolute() or not home.is_dir() or home_dir.is_symlink(): raise MemBenchError("membench_worker_home_invalid")
    path_entries=[str(original_python.resolve().parent),str(git.parent)]
    if git_system32_required:
        system_root=os.environ.get("SYSTEMROOT")
        system32=Path(system_root,"System32") if system_root else None
        if system32 is None or not system32.is_dir(): raise MemBenchError("membench_git_capability_isolation_failed")
        path_entries.append(str(system32.resolve()))
    env={"PYTHONPATH":str(rpg_root.resolve()),"PATH":os.pathsep.join(dict.fromkeys(path_entries)),"GIT_EXECUTABLE":str(git),"HOME":str(home),"USERPROFILE":str(home)}
    for key in ("SYSTEMROOT","WINDIR","TEMP","TMP"):
        value=os.environ.get(key)
        if value: env[key]=value
    return env

def _venv_python(root:Path,*,os_name:str|None=None)->Path:
    """Return the conventional virtualenv interpreter for the host family."""
    name=os.name if os_name is None else os_name
    if name=="nt": return root/".venv"/"Scripts"/"python.exe"
    if name=="posix": return root/".venv"/"bin"/"python"
    raise MemBenchError("membench_driver_worker_python_invalid")


def _driver_code_receipt()->dict[str,Any]:
    root=Path(__file__).resolve().parents[1]
    worker_python=_venv_python(root)
    venv_config=root/".venv"/"pyvenv.cfg"
    if not worker_python.is_file() or worker_python.is_symlink() or not venv_config.is_file() or venv_config.is_symlink(): raise MemBenchError("membench_driver_worker_python_invalid")
    module_names=("benchmarks.aerp8_membench","benchmarks.aerp7_convomem_rank","mempalace_rpg.retrieval","benchmarks.aerp7_original_core","benchmarks.aerp7_original_product")
    sources=[]
    for module_name in module_names:
        module=sys.modules.get(module_name) if module_name==__name__ else importlib.import_module(module_name)
        raw_path=getattr(module,"__file__",None)
        if not isinstance(raw_path,str): raise MemBenchError("membench_driver_import_origin_invalid")
        path=Path(raw_path).resolve()
        _under(path,root,"membench_driver_import_origin_outside_root")
        if not path.is_file() or path.suffix!=".py": raise MemBenchError("membench_driver_import_origin_invalid")
        sources.append({"module":module_name,"path":str(path),"sha256":_sha256_file(path)})
    try: state=original_product.v1.git_state(root)
    except (OSError, subprocess.SubprocessError) as exc: raise MemBenchError("membench_driver_code_receipt_invalid") from exc
    value={"schema":"aerp8-driver-code-receipt-v2","rpg_root":str(root),"python":str(worker_python.resolve()),"python_sha256":_sha256_file(worker_python),"venv_pyvenv_cfg":str(venv_config.resolve()),"venv_pyvenv_cfg_sha256":_sha256_file(venv_config),"git":state,"git_capability":_git_capability(),"sources":sources}
    value["code_sha256"]=digest(value)
    return value

def _validate_driver_code_receipt(value:Any)->dict[str,Any]:
    required={"schema","rpg_root","python","python_sha256","venv_pyvenv_cfg","venv_pyvenv_cfg_sha256","git","git_capability","sources","code_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!="aerp8-driver-code-receipt-v2" or value.get("code_sha256")!=digest({key:child for key,child in value.items() if key!="code_sha256"}) or not isinstance(value.get("sources"),list) or [row.get("module") if isinstance(row,Mapping) else None for row in value["sources"]] != ["benchmarks.aerp8_membench","benchmarks.aerp7_convomem_rank","mempalace_rpg.retrieval","benchmarks.aerp7_original_core","benchmarks.aerp7_original_product"]: raise MemBenchError("membench_driver_code_receipt_invalid")
    return dict(value)

def _require_external_current_checkpoint(path:Path)->dict[str,Any]:
    """Require a post-review, externally written receipt; never mint one inline."""
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise MemBenchError("membench_expected_checkpoint_missing")
    value=_load_canonical(path,"membench_expected_checkpoint_invalid")
    required={"schema","driver_code_receipt","original_execution_policy","original_execution_policy_sha256","checkpoint_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=CURRENT_CHECKPOINT_SCHEMA:
        raise MemBenchError("membench_expected_checkpoint_invalid")
    expected=_validate_driver_code_receipt(value.get("driver_code_receipt"))
    policy=_validate_original_execution_policy(value.get("original_execution_policy"))
    git=expected.get("git")
    if not isinstance(git,Mapping) or git.get("git_dirty") is not False or not isinstance(git.get("git_head"),str) or not isinstance(git.get("git_tree"),str) or value.get("original_execution_policy_sha256")!=policy["policy_sha256"] or value.get("checkpoint_sha256")!=digest({key:child for key,child in value.items() if key!="checkpoint_sha256"}):
        raise MemBenchError("membench_expected_checkpoint_invalid")
    actual=_driver_code_receipt()
    if actual!=expected:
        raise MemBenchError("membench_expected_checkpoint_drift")
    return dict(value)

def _probe_original_execution_policy(*,original_root:Path,original_python:Path,git_executable:Path,git_system32_required:bool)->dict[str,Any]:
    """Run the one shared no-data interpreter/import probe in a scrubbed environment."""
    root=original_root.resolve(); python=original_python.resolve()
    if not root.is_dir() or root.is_symlink() or not python.is_file() or python.is_symlink() or _under(python,root,"membench_original_execution_policy_invalid")!=python:
        raise MemBenchError("membench_original_execution_policy_invalid")
    code=("import json,sys,mempalace;print(json.dumps({'sys_executable':sys.executable,"
          "'sys_version':sys.version,'base_executable':getattr(sys,'_base_executable',sys.executable),"
          "'mempalace_file':mempalace.__file__},sort_keys=True,separators=(',',':')))" )
    result=subprocess.run([str(python),"-c",code],cwd=str(root),env=_scrubbed_original_env(original_python=python,rpg_root=Path(__file__).resolve().parents[1],git_executable=git_executable,git_system32_required=git_system32_required,home_dir=root),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if result.returncode:
        raise MemBenchError("membench_original_execution_policy_probe_failed:"+result.stderr.decode("utf-8","replace")[:300])
    try: probe=json.loads(result.stdout)
    except json.JSONDecodeError as exc: raise MemBenchError("membench_original_execution_policy_probe_invalid") from exc
    if not isinstance(probe,Mapping): raise MemBenchError("membench_original_execution_policy_probe_invalid")
    executable=Path(str(probe.get("sys_executable",""))).resolve(); base=Path(str(probe.get("base_executable",""))).resolve(); package=Path(str(probe.get("mempalace_file",""))).resolve()
    expected_package=(root/"mempalace"/"__init__.py").resolve()
    if executable!=python or not base.is_absolute() or not base.is_file() or base.is_symlink() or package!=expected_package or not package.is_file() or package.is_symlink() or not isinstance(probe.get("sys_version"),str) or not probe["sys_version"]:
        raise MemBenchError("membench_original_execution_policy_probe_invalid")
    return {"sys_executable":str(executable),"sys_version":probe["sys_version"],"base_executable":str(base),"base_executable_sha256":_sha256_file(base),"mempalace_file":str(package),"mempalace_file_sha256":_sha256_file(package)}

def capture_original_execution_policy(*,original_root:Path,original_python:Path,model_dir:Path,git_capability:Mapping[str,Any]|None=None)->dict[str,Any]:
    """Capture the bundled original execution policy before any worker starts."""
    root=original_root.resolve(); python=original_python.resolve(); model=model_dir.resolve(); git=dict(git_capability or _git_capability())
    checked_git=_verify_git_capability(executable=Path(str(git.get("executable",""))),sha256=str(git.get("sha256","")),version=str(git.get("version","")),system32_required=git.get("system32_required"))
    try: state=original_product.v1.original_source_state(root); model_receipt=original_product.v1.file_tree_receipt(model)
    except (OSError,subprocess.SubprocessError,ValueError) as exc: raise MemBenchError("membench_original_execution_policy_invalid") from exc
    if not root.is_dir() or root.is_symlink() or not python.is_file() or python.is_symlink() or _under(python,root,"membench_original_execution_policy_invalid")!=python or not model.is_dir() or model.is_symlink() or state.get("git_dirty") is not False or state.get("git_head")!=ORIGINAL_COMMIT or state.get("git_tree")!=ORIGINAL_TREE or model_receipt.get("sha256")!=ORIGINAL_MODEL_TREE:
        raise MemBenchError("membench_original_execution_policy_invalid")
    probe=_probe_original_execution_policy(original_root=root,original_python=python,git_executable=Path(checked_git["executable"]),git_system32_required=checked_git["system32_required"])
    value={"schema":ORIGINAL_EXECUTION_POLICY_SCHEMA,"original_commit":ORIGINAL_COMMIT,"original_tree":ORIGINAL_TREE,"original_root":str(root),"original_python":str(python),"original_python_sha256":_sha256_file(python),"sys_executable":probe["sys_executable"],"sys_version":probe["sys_version"],"base_executable":probe["base_executable"],"base_executable_sha256":probe["base_executable_sha256"],"mempalace_file":probe["mempalace_file"],"mempalace_file_sha256":probe["mempalace_file_sha256"],"model_dir":str(model),"model_file_tree_sha256":ORIGINAL_MODEL_TREE,"git_capability":checked_git}
    value["policy_sha256"]=digest(value); return value

def _validate_original_execution_policy(value:Any)->dict[str,Any]:
    required={"schema","original_commit","original_tree","original_root","original_python","original_python_sha256","sys_executable","sys_version","base_executable","base_executable_sha256","mempalace_file","mempalace_file_sha256","model_dir","model_file_tree_sha256","git_capability","policy_sha256"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=ORIGINAL_EXECUTION_POLICY_SCHEMA or value.get("policy_sha256")!=digest({key:child for key,child in value.items() if key!="policy_sha256"}) or value.get("original_commit")!=ORIGINAL_COMMIT or value.get("original_tree")!=ORIGINAL_TREE or value.get("model_file_tree_sha256")!=ORIGINAL_MODEL_TREE:
        raise MemBenchError("membench_original_execution_policy_invalid")
    for key in ("original_root","original_python","sys_executable","sys_version","base_executable","base_executable_sha256","mempalace_file","mempalace_file_sha256","model_dir","original_python_sha256"):
        if not isinstance(value.get(key),str) or not value[key]: raise MemBenchError("membench_original_execution_policy_invalid")
    live=capture_original_execution_policy(original_root=Path(value["original_root"]),original_python=Path(value["original_python"]),model_dir=Path(value["model_dir"]),git_capability=value["git_capability"])
    if live!=dict(value): raise MemBenchError("membench_original_execution_policy_drift")
    return dict(value)

def pinned_original_runtime_preflight(*,original_root:Path,original_python:Path,rpg_root:Path,model_dir:Path,git_executable:Path,git_sha256:str,git_version:str,git_system32_required:bool,worker_home_path:Path,execution_policy:Mapping[str,Any],checkpoint_sha256:str|None=None,code_receipt_sha256:str|None=None)->dict[str,Any]:
    """Prove the exact original interpreter, import source, commit, and model tree."""
    policy=_validate_original_execution_policy(execution_policy)
    root=original_root.resolve(); python=original_python.resolve(); driver=rpg_root.resolve(); model=model_dir.resolve()
    if not python.is_file() or not root.is_dir() or not driver.is_dir() or not model.is_dir() or original_python.is_symlink() or model_dir.is_symlink(): raise MemBenchError("membench_original_runtime_path_invalid")
    _under(python,root,"membench_original_runtime_python_outside_root")
    git_capability=_verify_git_capability(executable=git_executable,sha256=git_sha256,version=git_version,system32_required=git_system32_required)
    try:
        state=original_product.v1.original_source_state(root); model_receipt=original_product.v1.file_tree_receipt(model)
    except (OSError, subprocess.SubprocessError, ValueError) as exc: raise MemBenchError("membench_original_runtime_receipt_invalid") from exc
    if state.get("git_dirty") is not False or state.get("git_head")!=ORIGINAL_COMMIT or state.get("git_tree")!=ORIGINAL_TREE: raise MemBenchError("membench_original_runtime_pin_drift")
    if model_receipt.get("sha256")!=ORIGINAL_MODEL_TREE: raise MemBenchError("membench_original_runtime_model_drift")
    # Importing ``benchmarks.aerp8_membench`` here is unsafe: the original
    # repository also has a top-level ``benchmarks`` package and CWD wins for
    # ``python -c``.  This process has already loaded the driver; bind its
    # source path below while this probe verifies only the original package.
    if any(policy[key]!=str(value) for key,value in (("original_root",root),("original_python",python),("model_dir",model))) or policy["git_capability"]!=git_capability:
        raise MemBenchError("membench_original_runtime_execution_policy_mismatch")
    probe=_probe_original_execution_policy(original_root=root,original_python=python,git_executable=git_executable,git_system32_required=git_system32_required)
    if any(probe[key]!=policy[key] for key in probe): raise MemBenchError("membench_original_runtime_execution_policy_mismatch")
    loaded=Path(policy["mempalace_file"]); loaded_driver=(driver/"benchmarks"/"aerp8_membench.py").resolve()
    _under(loaded,root,"membench_original_runtime_not_source_root"); _under(loaded_driver,driver,"membench_original_runtime_driver_not_rpg_root")
    if checkpoint_sha256 is not None and (not isinstance(checkpoint_sha256,str) or len(checkpoint_sha256)!=64) or code_receipt_sha256 is not None and (not isinstance(code_receipt_sha256,str) or len(code_receipt_sha256)!=64): raise MemBenchError("membench_original_runtime_checkpoint_invalid")
    value={"schema":"aerp8-pinned-original-runtime-v2","original_commit":ORIGINAL_COMMIT,"original_tree":ORIGINAL_TREE,"original_git_state":state,"original_root":str(root),"original_python":str(python),"original_python_sha256":_sha256_file(python),"mempalace_file":str(loaded),"mempalace_file_sha256":_sha256_file(loaded),"driver_file":str(loaded_driver),"driver_file_sha256":_sha256_file(loaded_driver),"model_dir":str(model),"model_file_tree_sha256":ORIGINAL_MODEL_TREE,"git_capability":git_capability,"worker_home_path":str(worker_home_path.resolve()),"original_execution_policy_sha256":policy["policy_sha256"]}
    if checkpoint_sha256 is not None: value["checkpoint_sha256"]=checkpoint_sha256
    if code_receipt_sha256 is not None: value["code_receipt_sha256"]=code_receipt_sha256
    value["runtime_sha256"]=digest(value)
    return value
def validate_original_worker_config(value:Any)->dict[str,Any]:
    required={"schema","adapter_id","projection_path","projection_sha256","original_root","original_commit","original_tree","original_python","model_dir","model_tree_sha256","git_executable","git_sha256","git_version","git_system32_required","worker_home_path","palace_path","build_id","collection_identity","draft_path","ready_path","driver_code_receipt","checkpoint_sha256","original_execution_policy","original_execution_policy_sha256","resource_comparability"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=ORIGINAL_WORKER_CONFIG_SCHEMA or value.get("adapter_id")!="aerp8-membench-original-lifecycle-v1": raise MemBenchError("membench_original_worker_config_invalid")
    row=dict(value)
    _walk(row)
    for key in ("projection_path","original_root","original_python","model_dir","git_executable","worker_home_path","palace_path","draft_path","ready_path"):
        if not isinstance(row[key],str): raise MemBenchError("membench_original_worker_path_invalid")
        path=Path(row[key]);
        if not path.is_absolute() or path.is_symlink(): raise MemBenchError("membench_original_worker_path_invalid")
        row[key]=str(path.resolve())
    if row["original_commit"]!=ORIGINAL_COMMIT or row["original_tree"]!=ORIGINAL_TREE or row["model_tree_sha256"]!=ORIGINAL_MODEL_TREE or row["resource_comparability"] not in {"available","unavailable"} or not isinstance(row["checkpoint_sha256"],str) or len(row["checkpoint_sha256"])!=64 or not isinstance(row["build_id"],str) or not row["build_id"] or not isinstance(row["collection_identity"],str) or not row["collection_identity"] or row["build_id"]==row["collection_identity"]: raise MemBenchError("membench_original_worker_receipt_invalid")
    _validate_driver_code_receipt(row["driver_code_receipt"])
    policy=_validate_original_execution_policy(row["original_execution_policy"])
    if row["original_execution_policy_sha256"]!=policy["policy_sha256"] or any(row[key]!=policy[key] for key in ("original_root","original_python","model_dir")):
        raise MemBenchError("membench_original_worker_execution_policy_invalid")
    _verify_git_capability(executable=Path(row["git_executable"]),sha256=row["git_sha256"],version=row["git_version"],system32_required=row["git_system32_required"])
    path=Path(row["projection_path"])
    if not path.is_file(): raise MemBenchError("membench_original_worker_projection_missing")
    try: normalized=original_core.validate_normalized_projection(json.loads(path.read_bytes()))
    except (json.JSONDecodeError, original_core.OriginalCoreError) as exc: raise MemBenchError("membench_original_worker_projection_invalid") from exc
    if row["projection_sha256"]!=original_core.canonical_sha256(normalized): raise MemBenchError("membench_original_worker_projection_digest_invalid")
    if any(Path(row[key]).exists() for key in ("palace_path","draft_path","ready_path")): raise MemBenchError("membench_original_worker_output_present")
    worker_root=Path(row["palace_path"]).parent
    if not worker_root.is_dir() or worker_root.is_symlink() or Path(row["worker_home_path"])!=worker_root or Path(row["draft_path"]).parent!=worker_root or Path(row["ready_path"]).parent!=worker_root: raise MemBenchError("membench_original_worker_root_invalid")
    return row

def _remove_owned(path:Path, identity:tuple[int,int]|None)->None:
    if identity is None or not path.exists() or path.is_symlink(): return
    stat=path.stat()
    if (stat.st_dev,stat.st_ino)!=identity: return
    if path.is_dir(): shutil.rmtree(path)
    else: path.unlink()

def _worker_output_identities(config:Mapping[str,Any])->dict[str,tuple[int,int]|None]:
    return {key: None for key in ("palace_path","draft_path","ready_path")}

def _clean_worker_outputs(config:Mapping[str,Any], identities:Mapping[str,tuple[int,int]|None])->None:
    for key in ("ready_path","draft_path","palace_path"): _remove_owned(Path(str(config[key])),identities.get(key))

def _publish_owned(*, path:Path, payload:bytes, identities:dict[str,tuple[int,int]|None], key:str)->None:
    publish_nonreplace(path=path,payload=payload)
    stat=path.stat(); identities[key]=(stat.st_dev,stat.st_ino)

def run_original_worker(config:Mapping[str,Any])->dict[str,Any]:
    """Formal, non-injectable AERP-8 worker entrypoint for exactly one build."""
    row=validate_original_worker_config(config)
    if _validate_driver_code_receipt(row["driver_code_receipt"])!=_driver_code_receipt(): raise MemBenchError("membench_driver_code_drift")
    rpg_root=Path(__file__).resolve().parents[1]
    runtime=pinned_original_runtime_preflight(original_root=Path(row["original_root"]),original_python=Path(row["original_python"]),rpg_root=rpg_root,model_dir=Path(row["model_dir"]),git_executable=Path(row["git_executable"]),git_sha256=row["git_sha256"],git_version=row["git_version"],git_system32_required=row["git_system32_required"],worker_home_path=Path(row["worker_home_path"]),execution_policy=row["original_execution_policy"],checkpoint_sha256=row["checkpoint_sha256"],code_receipt_sha256=row["driver_code_receipt"]["code_sha256"])
    try: normalized=original_core.validate_normalized_projection(json.loads(Path(row["projection_path"]).read_bytes()))
    except (json.JSONDecodeError, original_core.OriginalCoreError) as exc: raise MemBenchError("membench_original_worker_projection_invalid") from exc
    if original_core.canonical_sha256(normalized)!=row["projection_sha256"]: raise MemBenchError("membench_original_worker_projection_drift")
    adapter=original_lifecycle_adapter(normalized); identities=_worker_output_identities(row); palace_path=Path(row["palace_path"])
    try:
        denominators={"query_count":len(normalized["items"]),"candidate_text_count":sum(len(corpus["candidates"]) for corpus in normalized["corpora"])}
        with original_product.pinned_live_original_product(original_root=Path(row["original_root"]),model_dir=Path(row["model_dir"]),palace_path=palace_path) as (seams,live_receipt):
            provider={key:seams.encoder.runtime_identity[key] for key in ("model","device","providers")}
            observer=original_product.LiveOriginalObserver(palace_path=palace_path,provider=provider,denominators=denominators,corpus_count=len(normalized["corpora"]))
            draft=original_product.run_original_public_replicate(projection=normalized,build_id=row["build_id"],collection_identity=row["collection_identity"],palace_path=palace_path,observer=observer,seams=seams,live_receipt=live_receipt,formal=True,resource_sink=lambda _telemetry: None,lifecycle_adapter=adapter)
        if not palace_path.is_dir() or palace_path.is_symlink(): raise MemBenchError("membench_original_worker_palace_missing")
        stat=palace_path.stat(); identities["palace_path"]=(stat.st_dev,stat.st_ino)
        payload=original_product.serialize_generic_worker_draft(draft=draft,adapter_id=adapter.adapter_id)
        draft_sha=hashlib.sha256(payload).hexdigest(); _publish_owned(path=Path(row["draft_path"]),payload=payload,identities=identities,key="draft_path")
        ready={"schema":READY_SCHEMA,"arm_id":"original_public_product","adapter_id":adapter.adapter_id,"build_id":row["build_id"],"draft_bytes_sha256":draft_sha,"checkpoint_sha256":row["checkpoint_sha256"],"original_execution_policy_sha256":row["original_execution_policy_sha256"]}
        ready["ready_sha256"]=digest(ready); _publish_owned(path=Path(row["ready_path"]),payload=_bytes(ready),identities=identities,key="ready_path")
        if _validate_driver_code_receipt(row["driver_code_receipt"])!=_driver_code_receipt(): raise MemBenchError("membench_driver_code_drift")
        return {"schema":"aerp8-original-worker-receipt-v1","build_id":row["build_id"],"draft_path":row["draft_path"],"ready_path":row["ready_path"],"draft_bytes_sha256":draft_sha,"ready_sha256":ready["ready_sha256"],"checkpoint_sha256":row["checkpoint_sha256"],"original_execution_policy_sha256":row["original_execution_policy_sha256"],"runtime_receipt":runtime}
    except BaseException:
        if identities["palace_path"] is None and palace_path.exists() and palace_path.is_dir() and not palace_path.is_symlink():
            stat=palace_path.stat(); identities["palace_path"]=(stat.st_dev,stat.st_ino)
        _clean_worker_outputs(row,identities)
        raise

def _read_worker_receipt(value:bytes, config:Mapping[str,Any])->dict[str,Any]:
    try: row=json.loads(value)
    except json.JSONDecodeError as exc: raise MemBenchError("membench_original_worker_receipt_invalid") from exc
    required={"schema","build_id","draft_path","ready_path","draft_bytes_sha256","ready_sha256","checkpoint_sha256","original_execution_policy_sha256","runtime_receipt"}
    if not isinstance(row,Mapping) or set(row)!=required or row.get("schema")!="aerp8-original-worker-receipt-v1" or row.get("build_id")!=config["build_id"] or row.get("draft_path")!=config["draft_path"] or row.get("ready_path")!=config["ready_path"]: raise MemBenchError("membench_original_worker_receipt_invalid")
    if not isinstance(row.get("draft_bytes_sha256"),str) or not isinstance(row.get("ready_sha256"),str) or not isinstance(row.get("runtime_receipt"),Mapping): raise MemBenchError("membench_original_worker_receipt_invalid")
    runtime=dict(row["runtime_receipt"])
    if row.get("checkpoint_sha256")!=config["checkpoint_sha256"] or row.get("original_execution_policy_sha256")!=config["original_execution_policy_sha256"] or runtime.get("schema")!="aerp8-pinned-original-runtime-v2" or runtime.get("original_commit")!=ORIGINAL_COMMIT or runtime.get("original_tree")!=ORIGINAL_TREE or runtime.get("model_file_tree_sha256")!=ORIGINAL_MODEL_TREE or runtime.get("original_execution_policy_sha256")!=config["original_execution_policy_sha256"] or runtime.get("checkpoint_sha256")!=config["checkpoint_sha256"] or runtime.get("runtime_sha256")!=digest({key:child for key,child in runtime.items() if key!="runtime_sha256"}): raise MemBenchError("membench_original_worker_runtime_invalid")
    if runtime.get("original_root")!=config["original_root"] or runtime.get("original_python")!=config["original_python"] or runtime.get("model_dir")!=config["model_dir"] or not isinstance(runtime.get("original_git_state"),Mapping) or runtime["original_git_state"].get("git_dirty") is not False or runtime["original_git_state"].get("git_head")!=ORIGINAL_COMMIT or runtime["original_git_state"].get("git_tree")!=ORIGINAL_TREE: raise MemBenchError("membench_original_worker_runtime_invalid")
    if runtime.get("git_capability")!={"executable":config["git_executable"],"sha256":config["git_sha256"],"version":config["git_version"],"system32_required":config["git_system32_required"]}: raise MemBenchError("membench_original_worker_runtime_invalid")
    if runtime.get("worker_home_path")!=config["worker_home_path"]: raise MemBenchError("membench_original_worker_runtime_invalid")
    return dict(row)

def run_original_five(*,candidate:Mapping[str,Any],original_root:Path|None=None,original_python:Path|None=None,model_dir:Path|None=None,output_root:Path|None=None,expected_checkpoint_path:Path|None=None,resource_comparability:str="unavailable",**forbidden:Any)->dict[str,Any]:
    """Launch and independently re-audit five exact original-product builds."""
    if forbidden: raise MemBenchError("membench_formal_original_requires_isolated_worker_contract")
    if original_root is None or original_python is None or model_dir is None or output_root is None or expected_checkpoint_path is None: raise MemBenchError("membench_original_worker_contract_invalid")
    checkpoint=_require_external_current_checkpoint(expected_checkpoint_path)
    p=validate_candidate_projection(candidate); root=output_root.resolve()
    if not output_root.is_absolute() or root.exists() or root.is_symlink(): raise MemBenchError("membench_original_output_root_invalid")
    if resource_comparability not in {"available","unavailable"}: raise MemBenchError("membench_original_resource_comparability_invalid")
    root.mkdir(parents=True); normalized=original_normalized_projection(p); projection_path=root/"normalized-projection.json"; publish_nonreplace(path=projection_path,payload=original_product._canonical_bytes(normalized))
    code_receipt=_driver_code_receipt()
    if checkpoint["driver_code_receipt"]!=code_receipt: raise MemBenchError("membench_original_checkpoint_code_mismatch")
    policy=checkpoint["original_execution_policy"]
    if any(str(path.resolve())!=policy[key] for key,path in (("original_root",original_root),("original_python",original_python),("model_dir",model_dir))): raise MemBenchError("membench_original_execution_policy_mismatch")
    git_capability=dict(code_receipt["git_capability"]); configs=[]; worker_receipts=[]; replicates=[]
    try:
        for number in range(5):
            worker_root=root/f"original-worker-{number+1}"; worker_root.mkdir()
            build_id=f"aerp8-original-{number+1}-{uuid.uuid4().hex}"; collection_identity=f"aerp8-collection-{number+1}-{uuid.uuid4().hex}"
            config={"schema":ORIGINAL_WORKER_CONFIG_SCHEMA,"adapter_id":"aerp8-membench-original-lifecycle-v1","projection_path":str(projection_path),"projection_sha256":original_core.canonical_sha256(normalized),"original_root":str(original_root.resolve()),"original_commit":ORIGINAL_COMMIT,"original_tree":ORIGINAL_TREE,"original_python":str(original_python.resolve()),"model_dir":str(model_dir.resolve()),"model_tree_sha256":ORIGINAL_MODEL_TREE,"git_executable":git_capability["executable"],"git_sha256":git_capability["sha256"],"git_version":git_capability["version"],"git_system32_required":git_capability["system32_required"],"worker_home_path":str(worker_root),"palace_path":str(worker_root/"palace"),"build_id":build_id,"collection_identity":collection_identity,"draft_path":str(worker_root/"draft.json"),"ready_path":str(worker_root/"draft.READY.json"),"driver_code_receipt":code_receipt,"checkpoint_sha256":checkpoint["checkpoint_sha256"],"original_execution_policy":policy,"original_execution_policy_sha256":checkpoint["original_execution_policy_sha256"],"resource_comparability":resource_comparability}
            config=validate_original_worker_config(config); configs.append(config); publish_nonreplace(path=worker_root/"worker-config.json",payload=_bytes(config))
            # Execute the reviewed driver by its exact path.  ``-m benchmarks``
            # from the original checkout may resolve that checkout's unrelated
            # package before the RPG driver on PYTHONPATH.
            result=subprocess.run([config["original_python"],str(Path(__file__).resolve()),"--original-worker"],input=_bytes(config),cwd=config["original_root"],env=_scrubbed_original_env(original_python=Path(config["original_python"]),rpg_root=Path(__file__).resolve().parents[1],git_executable=Path(config["git_executable"]),git_system32_required=config["git_system32_required"],home_dir=Path(config["worker_home_path"])),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
            if result.returncode: raise MemBenchError("membench_original_worker_failed:"+result.stderr.decode("utf-8","replace")[:4000])
            receipt=_read_worker_receipt(result.stdout,config); worker_receipts.append(receipt)
            payload=Path(config["draft_path"]).read_bytes(); ready=json.loads(Path(config["ready_path"]).read_bytes())
            if hashlib.sha256(payload).hexdigest()!=receipt["draft_bytes_sha256"] or not isinstance(ready,Mapping) or ready.get("schema")!=READY_SCHEMA or ready.get("adapter_id")!=config["adapter_id"] or ready.get("build_id")!=config["build_id"] or ready.get("draft_bytes_sha256")!=receipt["draft_bytes_sha256"] or ready.get("checkpoint_sha256")!=checkpoint["checkpoint_sha256"] or ready.get("original_execution_policy_sha256")!=checkpoint["original_execution_policy_sha256"] or ready.get("ready_sha256")!=receipt["ready_sha256"] or ready.get("ready_sha256")!=digest({key:child for key,child in ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_original_ready_or_draft_tamper")
            draft=original_product.load_generic_worker_draft(payload=payload,adapter_id=config["adapter_id"])
            audited=original_product.coordinator_reaudit_replicate(draft=draft,palace_path=Path(config["palace_path"]),projection=normalized,lifecycle_adapter=original_lifecycle_adapter(normalized))
            checked={"build_id":audited["build_id"],"index_sha256":audited["index_sha256"],"draft_sha256":receipt["draft_bytes_sha256"],"rankings":audited["rankings"]}; checked["rankings"]=_validate_rankings(checked["rankings"],p); replicates.append(checked)
        if any(len({row[key] for row in replicates})!=5 for key in ("build_id","index_sha256","draft_sha256")): raise MemBenchError("membench_original_build_not_fresh")
        if _driver_code_receipt()!=code_receipt: raise MemBenchError("membench_driver_code_drift")
        artifact={"schema":ARTIFACT_SCHEMA,"source_receipt":p["source_receipt"],"arm_id":"original_public_product","method":{"implementation":"exact_pinned_original_public_product_worker","five_fresh_builds":True,"original_commit":ORIGINAL_COMMIT,"original_tree":ORIGINAL_TREE,"model_tree_sha256":ORIGINAL_MODEL_TREE},"projection_sha256":p["projection_sha256"],"normalization_sha256":_normalization(p)["normalization_sha256"],"original_normalized_projection_sha256":original_core.canonical_sha256(normalized),"replicates":replicates,"runtime_receipts":[receipt["runtime_receipt"] for receipt in worker_receipts],"worker_receipts":worker_receipts,"coordinator_code_receipt":code_receipt,"checkpoint_sha256":checkpoint["checkpoint_sha256"],"original_execution_policy_sha256":checkpoint["original_execution_policy_sha256"]}; artifact["artifact_sha256"]=digest(artifact); return artifact
    except BaseException:
        for config in configs: _clean_worker_outputs(config,{key:(Path(str(config[key])).stat().st_dev,Path(str(config[key])).stat().st_ino) if Path(str(config[key])).exists() and not Path(str(config[key])).is_symlink() else None for key in ("palace_path","draft_path","ready_path")})
        raise

def validate_current_worker_config(value:Any)->dict[str,Any]:
    required={"schema","execution_role","projection_path","projection_sha256","rpg_root","rpg_python","model_dir","model_tree_sha256","git_executable","git_sha256","git_version","git_system32_required","worker_home_path","artifact_path","ready_path","driver_code_receipt","checkpoint_sha256","resource_comparability"}
    if not isinstance(value,Mapping) or set(value)!=required or value.get("schema")!=CURRENT_WORKER_CONFIG_SCHEMA or value.get("execution_role") not in CURRENT_ROLES: raise MemBenchError("membench_current_worker_config_invalid")
    row=dict(value); _walk(row)
    for key in ("projection_path","rpg_root","rpg_python","model_dir","git_executable","worker_home_path","artifact_path","ready_path"):
        if not isinstance(row[key],str): raise MemBenchError("membench_current_worker_path_invalid")
        path=Path(row[key])
        if not path.is_absolute() or path.is_symlink(): raise MemBenchError("membench_current_worker_path_invalid")
        row[key]=str(path.resolve())
    root=Path(row["rpg_root"]); python=Path(row["rpg_python"]); worker_root=Path(row["worker_home_path"])
    if root!=Path(__file__).resolve().parents[1] or not root.is_dir() or not worker_root.is_dir() or worker_root.is_symlink() or Path(row["artifact_path"]).parent!=worker_root or Path(row["ready_path"]).parent!=worker_root: raise MemBenchError("membench_current_worker_root_invalid")
    _under(python,root/".venv","membench_current_worker_python_outside_venv")
    if row["model_tree_sha256"]!=ORIGINAL_MODEL_TREE or row["resource_comparability"] not in {"available","unavailable"} or not isinstance(row["checkpoint_sha256"],str) or len(row["checkpoint_sha256"])!=64: raise MemBenchError("membench_current_worker_receipt_invalid")
    _validate_driver_code_receipt(row["driver_code_receipt"]); _verify_git_capability(executable=Path(row["git_executable"]),sha256=row["git_sha256"],version=row["git_version"],system32_required=row["git_system32_required"])
    path=Path(row["projection_path"])
    if not path.is_file(): raise MemBenchError("membench_current_worker_projection_missing")
    try: candidate=validate_candidate_projection(json.loads(path.read_bytes()))
    except (json.JSONDecodeError, MemBenchError) as exc: raise MemBenchError("membench_current_worker_projection_invalid") from exc
    if candidate["projection_sha256"]!=row["projection_sha256"]: raise MemBenchError("membench_current_worker_projection_digest_invalid")
    if Path(row["artifact_path"]).exists() or Path(row["ready_path"]).exists(): raise MemBenchError("membench_current_worker_output_present")
    return row

def _current_runtime_preflight(*,rpg_root:Path,rpg_python:Path,model_dir:Path,git_executable:Path,git_sha256:str,git_version:str,git_system32_required:bool,worker_home_path:Path,checkpoint_sha256:str)->dict[str,Any]:
    root=rpg_root.resolve(); python=rpg_python.resolve(); model=model_dir.resolve()
    if root!=Path(__file__).resolve().parents[1] or not python.is_file() or not model.is_dir() or model_dir.is_symlink(): raise MemBenchError("membench_current_runtime_path_invalid")
    _under(python,root/".venv","membench_current_runtime_python_outside_venv")
    git=_verify_git_capability(executable=git_executable,sha256=git_sha256,version=git_version,system32_required=git_system32_required)
    try: model_receipt=original_product.v1.file_tree_receipt(model)
    except (OSError,ValueError) as exc: raise MemBenchError("membench_current_runtime_model_invalid") from exc
    if model_receipt.get("sha256")!=ORIGINAL_MODEL_TREE: raise MemBenchError("membench_current_runtime_model_drift")
    if not isinstance(checkpoint_sha256,str) or len(checkpoint_sha256)!=64: raise MemBenchError("membench_current_runtime_checkpoint_invalid")
    code=_driver_code_receipt()
    value={"schema":"aerp8-pinned-current-runtime-v1","rpg_root":str(root),"rpg_python":str(python),"rpg_python_sha256":_sha256_file(python),"driver_file":str(Path(__file__).resolve()),"driver_file_sha256":_sha256_file(Path(__file__).resolve()),"model_dir":str(model),"model_file_tree_sha256":ORIGINAL_MODEL_TREE,"git_capability":git,"worker_home_path":str(worker_home_path.resolve()),"checkpoint_sha256":checkpoint_sha256,"code_receipt_sha256":code["code_sha256"],"cpu_provider_policy":{"device":"cpu","providers":["CPUExecutionProvider"]}}
    value["runtime_sha256"]=digest(value); return value

def run_current_worker(config:Mapping[str,Any])->dict[str,Any]:
    """Formal native-ONNX current-product worker; it has no injectable seams."""
    row=validate_current_worker_config(config)
    if _validate_driver_code_receipt(row["driver_code_receipt"])!=_driver_code_receipt(): raise MemBenchError("membench_driver_code_drift")
    runtime=_current_runtime_preflight(rpg_root=Path(row["rpg_root"]),rpg_python=Path(row["rpg_python"]),model_dir=Path(row["model_dir"]),git_executable=Path(row["git_executable"]),git_sha256=row["git_sha256"],git_version=row["git_version"],git_system32_required=row["git_system32_required"],worker_home_path=Path(row["worker_home_path"]),checkpoint_sha256=row["checkpoint_sha256"])
    identities={"artifact_path":None,"ready_path":None}
    try:
        candidate=validate_candidate_projection(json.loads(Path(row["projection_path"]).read_bytes()))
        encoder=original_product.v1.native_minilm_adapter(Path(row["model_dir"]))
        identity=getattr(encoder,"runtime_identity",None)
        if not isinstance(identity,Mapping) or identity.get("device")!="cpu" or identity.get("providers")!=["CPUExecutionProvider"] or identity.get("model_file_tree_sha256")!=ORIGINAL_MODEL_TREE: raise MemBenchError("membench_current_worker_native_runtime_invalid")
        artifact=rank_current(candidate=candidate,arm_id=CURRENT_ROLES[row["execution_role"]],encoder=encoder); payload=_bytes(artifact); artifact_sha=hashlib.sha256(payload).hexdigest()
        _publish_owned(path=Path(row["artifact_path"]),payload=payload,identities=identities,key="artifact_path")
        ready={"schema":READY_SCHEMA,"arm_id":artifact["arm_id"],"execution_role":row["execution_role"],"payload_sha256":artifact_sha,"artifact_sha256":artifact["artifact_sha256"],"runtime_sha256":runtime["runtime_sha256"],"checkpoint_sha256":row["checkpoint_sha256"]}; ready["ready_sha256"]=digest(ready)
        _publish_owned(path=Path(row["ready_path"]),payload=_bytes(ready),identities=identities,key="ready_path")
        if _validate_driver_code_receipt(row["driver_code_receipt"])!=_driver_code_receipt(): raise MemBenchError("membench_driver_code_drift")
        return {"schema":"aerp8-current-worker-receipt-v1","execution_role":row["execution_role"],"arm_id":artifact["arm_id"],"artifact_path":row["artifact_path"],"ready_path":row["ready_path"],"payload_sha256":artifact_sha,"artifact_sha256":artifact["artifact_sha256"],"ready_sha256":ready["ready_sha256"],"checkpoint_sha256":row["checkpoint_sha256"],"runtime_receipt":runtime}
    except BaseException:
        for key in ("ready_path","artifact_path"):
            path=Path(row[key])
            if identities[key] is None and path.exists() and path.is_file() and not path.is_symlink():
                stat=path.stat(); identities[key]=(stat.st_dev,stat.st_ino)
            _remove_owned(path,identities[key])
        raise

def _read_current_worker_receipt(value:bytes,config:Mapping[str,Any])->dict[str,Any]:
    try: row=json.loads(value)
    except json.JSONDecodeError as exc: raise MemBenchError("membench_current_worker_receipt_invalid") from exc
    required={"schema","execution_role","arm_id","artifact_path","ready_path","payload_sha256","artifact_sha256","ready_sha256","checkpoint_sha256","runtime_receipt"}
    if not isinstance(row,Mapping) or set(row)!=required or row.get("schema")!="aerp8-current-worker-receipt-v1" or row.get("execution_role")!=config["execution_role"] or row.get("arm_id")!=CURRENT_ROLES[config["execution_role"]] or row.get("artifact_path")!=config["artifact_path"] or row.get("ready_path")!=config["ready_path"]: raise MemBenchError("membench_current_worker_receipt_invalid")
    runtime=row.get("runtime_receipt")
    if not isinstance(runtime,Mapping) or row.get("checkpoint_sha256")!=config["checkpoint_sha256"] or runtime.get("schema")!="aerp8-pinned-current-runtime-v1" or runtime.get("runtime_sha256")!=digest({key:child for key,child in runtime.items() if key!="runtime_sha256"}) or runtime.get("rpg_root")!=config["rpg_root"] or runtime.get("rpg_python")!=config["rpg_python"] or runtime.get("model_dir")!=config["model_dir"] or runtime.get("model_file_tree_sha256")!=ORIGINAL_MODEL_TREE or runtime.get("worker_home_path")!=config["worker_home_path"] or runtime.get("checkpoint_sha256")!=config["checkpoint_sha256"] or runtime.get("git_capability")!={"executable":config["git_executable"],"sha256":config["git_sha256"],"version":config["git_version"],"system32_required":config["git_system32_required"]}: raise MemBenchError("membench_current_worker_runtime_invalid")
    return dict(row)

def run_current_four(*,candidate:Mapping[str,Any],rpg_root:Path,rpg_python:Path,model_dir:Path,output_root:Path,expected_checkpoint_path:Path,resource_comparability:str="unavailable")->dict[str,Any]:
    checkpoint=_require_external_current_checkpoint(expected_checkpoint_path)
    p=validate_candidate_projection(candidate); root=output_root.resolve()
    if not output_root.is_absolute() or root.exists() or root.is_symlink() or resource_comparability not in {"available","unavailable"}: raise MemBenchError("membench_current_worker_contract_invalid")
    root.mkdir(parents=True); projection_path=root/"candidate-projection.json"; publish_nonreplace(path=projection_path,payload=_bytes(p))
    code=_driver_code_receipt()
    if checkpoint["driver_code_receipt"]!=code: raise MemBenchError("membench_current_checkpoint_code_mismatch")
    git=dict(code["git_capability"]); configs=[]; artifacts={}; files={}; receipts={}
    try:
        for role,arm in CURRENT_ROLES.items():
            worker_root=root/f"current-worker-{role}"; worker_root.mkdir()
            config={"schema":CURRENT_WORKER_CONFIG_SCHEMA,"execution_role":role,"projection_path":str(projection_path),"projection_sha256":p["projection_sha256"],"rpg_root":str(rpg_root.resolve()),"rpg_python":str(rpg_python.resolve()),"model_dir":str(model_dir.resolve()),"model_tree_sha256":ORIGINAL_MODEL_TREE,"git_executable":git["executable"],"git_sha256":git["sha256"],"git_version":git["version"],"git_system32_required":git["system32_required"],"worker_home_path":str(worker_root),"artifact_path":str(worker_root/"artifact.json"),"ready_path":str(worker_root/"artifact.READY.json"),"driver_code_receipt":code,"checkpoint_sha256":checkpoint["checkpoint_sha256"],"resource_comparability":resource_comparability}
            config=validate_current_worker_config(config); configs.append(config); publish_nonreplace(path=worker_root/"worker-config.json",payload=_bytes(config))
            result=subprocess.run([config["rpg_python"],"-m","benchmarks.aerp8_membench","--candidate-worker"],input=_bytes(config),cwd=config["rpg_root"],env=_scrubbed_original_env(original_python=Path(config["rpg_python"]),rpg_root=Path(config["rpg_root"]),git_executable=Path(config["git_executable"]),git_system32_required=config["git_system32_required"],home_dir=Path(config["worker_home_path"])),stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
            if result.returncode: raise MemBenchError("membench_current_worker_failed:"+result.stderr.decode("utf-8","replace")[:2000])
            receipt=_read_current_worker_receipt(result.stdout,config); payload=Path(config["artifact_path"]).read_bytes(); ready=json.loads(Path(config["ready_path"]).read_bytes())
            if hashlib.sha256(payload).hexdigest()!=receipt["payload_sha256"] or not isinstance(ready,Mapping) or ready.get("schema")!=READY_SCHEMA or ready.get("execution_role")!=role or ready.get("arm_id")!=arm or ready.get("payload_sha256")!=receipt["payload_sha256"] or ready.get("artifact_sha256")!=receipt["artifact_sha256"] or ready.get("checkpoint_sha256")!=checkpoint["checkpoint_sha256"] or ready.get("ready_sha256")!=receipt["ready_sha256"] or ready.get("ready_sha256")!=digest({key:child for key,child in ready.items() if key!="ready_sha256"}): raise MemBenchError("membench_current_ready_or_artifact_tamper")
            artifact=json.loads(payload); artifact=_validate_artifact(artifact,p,arm)
            if _bytes(artifact)!=payload or artifact["artifact_sha256"]!=receipt["artifact_sha256"]: raise MemBenchError("membench_current_artifact_bytes_invalid")
            artifacts[role]=artifact; files[role]={"artifact_path":config["artifact_path"],"ready_path":config["ready_path"],"execution_role":role}; receipts[role]=receipt
        if _bytes(artifacts["p5_primary"])!=_bytes(artifacts["p5_repeat"]): raise MemBenchError("membench_static_p5_not_byte_identical")
        if _driver_code_receipt()!=code: raise MemBenchError("membench_driver_code_drift")
        return {"schema":"aerp8-current-four-workers-v1","artifacts":artifacts,"files":files,"worker_receipts":receipts,"code_receipt":code,"checkpoint_sha256":checkpoint["checkpoint_sha256"]}
    except BaseException:
        for config in configs:
            for key in ("ready_path","artifact_path"):
                path=Path(config[key])
                if path.exists() and path.is_file() and not path.is_symlink(): path.unlink()
        raise
def _main()->int:
    # Both protocols are deliberately stdin-only: no candidate/custody data can
    # leak through argv and the original worker accepts no injectable seams.
    if sys.argv[1:]==["--candidate-worker"]:
      request=json.loads(sys.stdin.buffer.read())
      if not isinstance(request,Mapping): raise MemBenchError("membench_current_worker_config_invalid")
      print(_bytes(run_current_worker(request)).decode("utf-8")); return 0
    if sys.argv[1:]==["--synthetic-candidate-worker"]:
      request=json.loads(sys.stdin.buffer.read())
      if not isinstance(request,Mapping) or request.get("schema")!="aerp8-membench-synthetic-current-worker-request-v1" or request.get("encoder_spec")!={"resolver":"external_frozen"}: raise MemBenchError("membench_external_frozen_encoder_required")
      raise MemBenchError("membench_synthetic_worker_has_no_formal_encoder")
    if sys.argv[1:]==["--original-worker"]:
      request=json.loads(sys.stdin.buffer.read())
      if not isinstance(request,Mapping): raise MemBenchError("membench_original_worker_config_invalid")
      print(_bytes(run_original_worker(request)).decode("utf-8")); return 0
    if sys.argv[1:]==["--formal-custodian"]:
      request=json.loads(sys.stdin.buffer.read())
      if not isinstance(request,Mapping): raise MemBenchError("membench_custodian_config_invalid")
      print(_bytes(run_formal_custodian(request)).decode("utf-8")); return 0
    if sys.argv[1:]==["--formal-source-builder"]:
      request=json.loads(sys.stdin.buffer.read())
      if not isinstance(request,Mapping): raise MemBenchError("membench_source_builder_config_invalid")
      print(_bytes(run_formal_source_builder(request)).decode("utf-8")); return 0
    raise MemBenchError("membench_cli_invalid")
if __name__=="__main__":
    try: raise SystemExit(_main())
    except MemBenchError as exc: print(str(exc),file=sys.stderr); raise SystemExit(2)
