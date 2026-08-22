"""Label-free, production-trace adapter for AERP-4 ranking freezes."""
from __future__ import annotations
import argparse, hashlib, json, math
from pathlib import Path
from typing import Any, Mapping, Sequence
from benchmarks import aerp4_raw_anchored_gate as gate

PREFREEZE_SCHEMA="aerp4-raw-anchored-paired-policy-prefreeze-v1"; TRACE_SCHEMA="aerp2-product-six-view-v1"; ROUTING_SCHEMA="aerp4-raw-anchored-p5-v1"
VIEWS=("raw_bm25","observation_bm25","raw_dense","observation_dense","checkpoint_dense","combo_dense")
RAW_WEIGHTS={"raw_bm25":2.0,"observation_bm25":0.0,"raw_dense":1.0,"observation_dense":0.0,"checkpoint_dense":0.0,"combo_dense":0.0}
P5_WEIGHTS={"raw_bm25":2.0,"observation_bm25":0.5,"raw_dense":1.0,"observation_dense":2.0,"checkpoint_dense":0.0,"combo_dense":1.0}
def _bytes(x: Any)->bytes:return json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=True,allow_nan=False).encode()
def _sha(x: Any)->str:return hashlib.sha256(_bytes(x)).hexdigest()
def _map(x: Any,n:str)->Mapping[str,Any]:
    if not isinstance(x,Mapping):raise ValueError(f"{n} must be object")
    return x
def _list(x: Any,n:str)->list[Any]:
    if not isinstance(x,list):raise ValueError(f"{n} must be list")
    return x
def _exact(x:Mapping[str,Any], keys:set[str], n:str)->None:
    if set(x)!=keys:raise ValueError(f"{n} keys mismatch")
def _finite(x:Any,n:str)->float:
    if isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(float(x)):raise ValueError(f"{n} must be finite")
    return float(x)
def _config(tau:str)->dict[str,Any]:return {"schema":ROUTING_SCHEMA,"policy":"raw_anchored_p5","tau":tau,"raw_weights":RAW_WEIGHTS,"p5_weights":P5_WEIGHTS,"support_top_k":10,"comparator":">=","ordinary_sum_view_order":list(VIEWS),"rrf_k":60}

def _production_trace(value:Any, *, route:str)->dict[str,Any]:
    """Validate an unmodified RankingResult.trace; never emit source IDs."""
    t=_map(value,f"{route} trace"); _exact(t,{"schema","encoder_identity","weights","rrf_k","query_sha256","input_sha256","view_digests","selected","aerp4_raw_anchored_p5"},f"{route} trace")
    if t["schema"]!=TRACE_SCHEMA or not isinstance(t["encoder_identity"],str) or not t["encoder_identity"] or t["rrf_k"]!=60:raise ValueError(f"{route} trace is not production")
    gate._token(t["query_sha256"],"query digest");gate._token(t["input_sha256"],"input digest")
    views=_map(t["view_digests"],"view digests");_exact(views,set(VIEWS),"view digests")
    for x in views.values():gate._token(x,"view digest")
    r=_map(t["aerp4_raw_anchored_p5"],"routing receipt"); _exact(r,{"schema","policy","config","config_sha256","numerator","denominator","A","raw_top10_ranking_sha256","p5_top10_ranking_sha256","route","effective_weights","final_ranking_sha256"},"routing receipt")
    weights,tau=(RAW_WEIGHTS,"+inf") if route=="raw" else (P5_WEIGHTS,"-inf")
    if r["schema"]!=ROUTING_SCHEMA or r["policy"]!="raw_anchored_p5" or r["route"]!=route:raise ValueError(f"{route} routing identity mismatch")
    if r["config"]!=_config(tau) or r["config_sha256"]!=_sha(_config(tau)):raise ValueError(f"{route} config mismatch")
    if r["effective_weights"]!=weights or t["weights"]!=weights:raise ValueError(f"{route} effective weights mismatch")
    num=_finite(r["numerator"],"numerator");den=_finite(r["denominator"],"denominator");a=_finite(r["A"],"A")
    if den<=0 or a!=num/den:raise ValueError(f"{route} A mismatch")
    tokens=[]; id_tokens={}; rank_vectors={view: [] for view in VIEWS}; previous=math.inf
    for i,x in enumerate(_list(t["selected"],"selected")):
        row=_map(x,f"selected[{i}]");_exact(row,{"source_event_id","ranking_key_sha256","final_rrf","component_ranks","contributions"},"selected")
        if not isinstance(row["source_event_id"],str) or not row["source_event_id"]:raise ValueError("selected source id malformed")
        token=gate._token(row["ranking_key_sha256"],"ranking token")
        if token in tokens:raise ValueError("selected ranking tokens duplicate")
        if row["source_event_id"] in id_tokens: raise ValueError("selected source ids duplicate")
        id_tokens[row["source_event_id"]]=token
        final=_finite(row["final_rrf"],"final score"); ranks=_map(row["component_ranks"],"component ranks");contrib=_map(row["contributions"],"contributions");_exact(ranks,set(VIEWS),"component ranks");_exact(contrib,set(VIEWS),"contributions")
        if any(not isinstance(v,int) or isinstance(v,bool) or v<1 for v in ranks.values()):raise ValueError("component ranks malformed")
        expected={view:weights[view]/(60+ranks[view]) for view in VIEWS}
        if any(_finite(contrib[view],"contribution") != expected[view] for view in VIEWS):raise ValueError("contribution replay mismatch")
        if final != sum(expected[view] for view in VIEWS):raise ValueError("final RRF replay mismatch")
        if final >= previous:raise ValueError("selected ordering tie/unverifiable stable order")
        previous=final
        for view in VIEWS:rank_vectors[view].append(ranks[view])
        tokens.append(token)
    if not tokens:raise ValueError("selected must retain full authorized universe")
    expected_ranks=set(range(1,len(tokens)+1))
    if any(set(values)!=expected_ranks for values in rank_vectors.values()):raise ValueError("selected view ranks are not full-universe permutations")
    if r["final_ranking_sha256"]!=_sha(tokens):raise ValueError(f"{route} final ranking digest mismatch")
    if route=="raw" and r["raw_top10_ranking_sha256"]!=_sha(tokens[:10]):raise ValueError("raw selected order does not match raw top10")
    if route=="p5" and r["p5_top10_ranking_sha256"]!=_sha(tokens[:10]):raise ValueError("p5 selected order does not match p5 top10")
    return {"input_sha256":t["input_sha256"],"query_sha256":t["query_sha256"],"view_digests":dict(views),"A":a,"numerator":num,"denominator":den,"tokens":tokens,"id_tokens":id_tokens,"raw_top_digest":r["raw_top10_ranking_sha256"],"p5_top_digest":r["p5_top10_ranking_sha256"],"config_sha256":r["config_sha256"],"encoder_identity":t["encoder_identity"]}

def build_ranking_freeze(study:Mapping[str,Any],partition:str,paired_policy_receipts:Mapping[str,Any])->dict[str,Any]:
    gate._validate_study(study)
    if partition not in {"train","dev"}:raise ValueError("partition must be train or dev")
    s=_map(paired_policy_receipts,"production receipt envelope");_exact(s,{"schema","status","study_sha256","partition","producer","items"},"production receipt envelope")
    if s["schema"]!=PREFREEZE_SCHEMA or s["status"]!="complete" or s["study_sha256"]!=gate._sha(study) or s["partition"]!=partition:raise ValueError("production receipt envelope binding mismatch")
    p=_map(s["producer"],"producer");_exact(p,{"artifact_sha256","git_head","git_tree","retrieval_implementation_sha256"},"producer");gate._token(p["artifact_sha256"],"producer artifact sha");gate._token(p["retrieval_implementation_sha256"],"producer retrieval implementation")
    if any(not isinstance(p[k],str) or len(p[k])!=40 or any(c not in gate._HEX for c in p[k]) for k in ("git_head","git_tree")):raise ValueError("producer commit/tree malformed")
    if dict(p) != dict(_map(study["producer"], "study producer")):raise ValueError("producer receipt does not match frozen study")
    rows=[]
    for x in _list(s["items"],"items"):
        i=_map(x,"item");_exact(i,{"item_token","group_token","campaign_token","raw_trace","p5_trace"},"item")
        raw=_production_trace(i["raw_trace"],route="raw");p5=_production_trace(i["p5_trace"],route="p5")
        retrieval=_map(study["retrieval"], "study retrieval")
        if raw["config_sha256"] != retrieval["raw_config_sha256"] or p5["config_sha256"] != retrieval["p5_config_sha256"]:
            raise ValueError("production config receipt does not match frozen study")
        if raw["encoder_identity"] != p5["encoder_identity"]: raise ValueError("raw/p5 encoder identity mismatch")
        for key in ("input_sha256","query_sha256","view_digests","A","numerator","denominator","raw_top_digest","p5_top_digest"):
            if raw[key]!=p5[key]:raise ValueError(f"raw/p5 production receipt mismatch for {key}")
        if set(raw["tokens"])!=set(p5["tokens"]):raise ValueError("raw/p5 authorized universe mismatch")
        if raw["id_tokens"] != p5["id_tokens"]: raise ValueError("raw/p5 source-to-token mapping mismatch")
        for k in ("item_token","group_token","campaign_token"):gate._token(i[k],k)
        rows.append({"item_token":i["item_token"],"group_token":i["group_token"],"campaign_token":i["campaign_token"],"query_sha256":raw["query_sha256"],"input_sha256":raw["input_sha256"],"A_hex":raw["A"].hex(),"numerator":raw["numerator"],"denominator":raw["denominator"],"authorized_tokens":raw["tokens"],"authorized_tokens_sha256":_sha(raw["tokens"]),"raw_top10":raw["tokens"][:10],"p5_top10":p5["tokens"][:10],"raw_top10_sha256":raw["raw_top_digest"],"p5_top10_sha256":raw["p5_top_digest"]})
    if not rows:raise ValueError("items empty")
    for k,d in (("item_token","item_sha256"),("group_token","group_sha256"),("campaign_token","campaign_sha256")):
        vals=[x[k] for x in rows]
        if k=="item_token" and len(vals)!=len(set(vals)):raise ValueError("duplicate item token")
        if gate._sha(sorted(vals))!=gate._partition_spec(study,partition)[d]:raise ValueError(f"{k} membership mismatch")
    out={"schema":gate.RANKING_FREEZE_SCHEMA,"status":"complete","study_sha256":gate._sha(study),"partition":partition,"producer":dict(p),"paired_input_sha256":_sha(s),"items":rows,"phase_ledger":{"status":"complete","phase":"atomic_publish_ready","terminal_phase":"atomic_publish_ready"}}
    return out

def main(argv:Sequence[str]|None=None)->int:
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--study",required=True);p.add_argument("--study-sha256",required=True);p.add_argument("--partition",choices=("train","dev"),required=True);p.add_argument("--paired-policy-receipts",required=True);p.add_argument("--paired-policy-receipts-sha256",required=True);p.add_argument("--output",required=True);p.add_argument("--repo",required=True);a=p.parse_args(argv)
    study=gate.BoundInput.load(a.study,a.study_sha256); source=gate.BoundInput.load(a.paired_policy_receipts,a.paired_policy_receipts_sha256)
    result=build_ranking_freeze(study.json("study"),a.partition,source.json("production receipts"))
    gate._slot(study.json("study"),stage=f"{a.partition}_prefreeze",partition=a.partition,output=a.output)
    gate.publish_bound_report(report=result,output=a.output,inputs=[study,source],repo=a.repo,implementation_path=Path(__file__));return 0
if __name__=="__main__":raise SystemExit(main())
