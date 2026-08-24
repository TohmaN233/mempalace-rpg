# RPG Memory Architecture Notes

## AERP-1 evidence boundary

`RpgMemoryKernel.authorized_evidence` is the single fail-closed authorization
boundary for ordinary recall, deep recall, and scene spans. Every retrieval call
requires `campaign_id`; candidates are campaign constrained before ranking and
are deduplicated by `source_event_id` before the budget ceiling.

Private faction, quest, and party evidence requires an explicit active row in
`actor_membership` for the same campaign and scope. Character-private evidence
requires `access_owner_id`; actor beliefs require `belief_owner_id`. Missing or
contradictory security metadata denies. Retconned material is never returned.

Verbatim access returns only `source_span` values that descend from authorized
event seeds. A participant or witness is not granted the whole transcript.

## AERP-2 product retrieval boundary

`RpgMemoryKernel` may receive an `AuthorizedEventRanker`, but authorization
remains the only candidate-universe boundary. The kernel passes that ranker
only ACL-approved source events and rejects unknown IDs, duplicate IDs,
missing/non-finite scores, malformed trace rows, and corrupt product metadata.
Without an injected ranker, the AERP-1 ranking path and product response remain
unchanged.

`SixViewRanker` is the frozen annotation-free product ranker. It uses raw and
structured-observation BM25, raw and structured-observation dense retrieval,
policy-homogeneous checkpoint dense retrieval, and raw-plus-observation dense
retrieval with weights `2.0/0.5/1.0/2.0/2.0/1.0` and weighted RRF `k=60`.
Checkpoint roll-ups are chronological and may cross actors only when their
security-policy identity is the same. A caller may provide
`payload.retrieval_checkpoint_id`; otherwise the source scene is the checkpoint.
For rebuild-stable product ranking, callers should provide a non-empty unique
`payload.retrieval_ranking_key` (LoCoMo uses its opaque dialog ID). The kernel
falls back to `source_event_id` for ordinary compatibility, but UUID fallbacks
cannot make cross-rebuild ranking ties or digests stable.

Dense runtimes are injected through separate `encode_query` and
`encode_passages` methods plus a stable encoder identity. Passage vectors are
cached by encoder identity, view, and content digest; query vectors are encoded
once per request and never cached. Retrieval trace payloads contain algorithm
identity, digests, ranks, scores/contributions, and only the evidence actually
packed. Do not put transcript text in the ranking trace. Do not add graph or
clustering retrieval until the frozen annotation-free evaluation identifies a
remaining recall gap.

## AERP-3 / FCD research boundary

Checkpoint `e8ca3a7` is a frozen engineering result, not a release-qualified or
paper-level retrieval claim. Product Six-View improves the strongest raw control
overall and on the hard subset, but it misses the frozen P1/P2 gates and regresses
on the adversarial category. Do not describe fixed RRF as the established cause.
Correlated product projections, checkpoint tie semantics, representation mismatch,
candidate-depth differences, and inherited historical weights remain competing
explanations.

The ordered program is Frozen Causal Decomposition (FCD). It must remain diagnostic
until the evidence selects a mechanism:

1. FCD-0 may decompose the frozen Product top-10 against the frozen raw BM25+dense
   RRF top-10 and raw top-50 union. Its `fusion_promotion` and `fusion_demotion`
   labels describe output set relations only; they do not establish Product
   view-pool coverage or fusion causality.
2. FCD-1 must capture a benchmark-only ranking ledger before kernel trace
   compaction and before scorer-label access: every Product view's top-50 IDs and
   scores, fused top-50, checkpoint tie groups, raw-control top-50, and authorization
   digest. `SixViewRanker.diagnostic_ledger` is strict-bool and defaults off, so
   production traces and production ranking cost remain unchanged. The frozen
   benchmark explicitly enables it and records each view's full authorized ID order
   only to prove top-50 prefixes and every fused component rank; it must never record
   query, transcript, observation, checkpoint, policy, or ranking-key plaintext.
   Checkpoint/policy receipts must bind to the independent pre-ranking seed ledger.
   The Product top-10 stream must remain exactly
   `64007282069621bb3e603598938993ebe0907e8e84ebaa65394741ab618e5441`
   across all 1,986 outputs.
3. FCD-2 must run, in order, raw-component parity, the six-view source-pool oracle,
   fusion-semantics parity, then fixed add-view/leave-one-view-out diagnostics. It
   must produce exactly one frozen verdict: `FUSION_SUPPORTED`,
   `REPRESENTATION_SUPPORTED`, or `COMPONENT_PARITY_FAILED`. No tuning is allowed.
4. Only `FUSION_SUPPORTED` permits one pre-registered annotation-free fusion
   candidate, developed on a separate development corpus. Otherwise the next work
   is projection research. Graph or clustering remains blocked until current
   Product views miss recoverable evidence on multiple datasets.

A retrieval-paper claim additionally requires a frozen implementation, one blind
execution with no subsequent tuning, and at least two untouched confirmatory
datasets. P1/P2, ACL leakage, trace completeness, exact-span replay, performance,
and failure-injection measurements remain unchanged, but production-grade OS
isolation is not a prerequisite for the Track A efficacy result. The minimal
formal benchmark threat model trusts frozen benchmark code while separating
ranking and custody into independent subprocesses with explicit file
capabilities. A systems-paper path is separate and would require a formal threat
model, explicit invariants, Docker/cgroup or equivalent enforcement, and a
security evaluation.
Paper statistics must report question-macro and conversation-weighted estimands
separately; the existing conversation bootstrap must not be presented as an
interval for a differently weighted point estimate.

## Paper research program

The paper program is split into independently gated retrieval and story-generation
tracks.  The authoritative protocol, data roles, original-product comparison
semantics, success thresholds, and paper-ready definition live in
`docs/paper-research-program.md`.  Do not call the original repository's direct
Chroma benchmark candidate the MemPalace public product: the product comparison
must execute `palace.get_collection(...).upsert(...)` followed by
`searcher.search_memories(...)`.  Do not use LongMemEval-V2 as an official
evidence-R@10 dataset because its public package omits answer-bearing evidence
annotations.

Current formal-path status is implementation evidence, not a paper result.
AERP-7 supplies the trusted-host candidate/custody, worker, authorization, and
durable publication common path. AERP-8 supplies an isolated manifest-bound
MemBench source-builder, four current workers, five exact-original public-product
workers with physical re-audit, and a custody-after-public/release scorer. Formal
source bytes for both ConvoMem and MemBench remain unopened, unenumerated,
unhashed, undownloaded, and unrun. MemBench `selected_profiles` is frozen as
exactly `['0', '100']` (the official `0-10k` and `100k` labels), with all four
fixed `data2test` role paths per profile. Those payloads are external to the
pinned Git tree: an authorized acquisition receipt must bind archive/file byte
digests before JSON parsing. The implementation never discovers, samples,
shuffles, or drops profiles/files.
ConvoMem's signed `census-v1` pre-parse manifest is likewise frozen as the
official upstream commit/tree plus exactly 2,168 files, 27,458,421,811 bytes,
and its inventory digest; those source bytes remain unopened until the approved
formal run.  The current Windows workspace cannot produce that formal receipt:
the one-shot path fails closed without Linux directory-fsync semantics, and a
future Linux run requires a freshly compatible AERP-8 external checkpoint rather
than reusing this Windows development receipt.
Before any original worker starts, the external AERP-8 checkpoint is now schema
`aerp8-membench-current-checkpoint-v2` and binds an
`aerp8-membench-original-execution-policy-v1` digest. The policy is independently
re-probed with the frozen original interpreter and scrubbed environment: exact
original root/interpreter bytes, `sys.executable`, `sys.version`,
`sys._base_executable` (or equivalent base identity) and its bytes, the actual
`mempalace.__file__` (which must be the pinned root's `mempalace/__init__.py`),
model tree, and Git capability must all match. Its digest is carried through
each original worker config/runtime/receipt/READY, the original artifact, public
packet, capability release, `_formal_public`, and custodian pre-custody checks.
The current worker identity is likewise bound to the reviewed `.venv/Scripts/python.exe`
and `.venv/pyvenv.cfg` paths and SHA-256 values. Because the source tree deliberately
does not track the local `.venv`, the final clean checkpoint's external runtime/checkpoint
receipt must preserve exact paths, byte hashes, and live revalidation evidence instead;
the review closure carries both files only for isolated reproduction.
The pinned original is commit
`72ccd2f3653ab902e419d15bb542c88045342b04`, tree
`5e4ad9cf1d6387cebe16dd03b6da8355d899f70c`, model tree
`76217893f057779cee29c903aa24444154ad0da7645853f1041fd970cca275a0`.
Synthetic live smoke artifacts are not data results: original
`21917673215412ba42cbc2e1fabd4ea019bb8fcb6ed47b17418211c705e4e30d`;
current raw `2389a5e51955fa448d1a6e4b65477772fa70fd882a1fe63c89be257d9f4d945b`,
P5 `23f008fa4bf7f1a61b3c341d49fa07a2bfa8806e9b1290bb4202a173cda80a7d`,
and Six-View `330fee44e051cbf126aca6534719372dbfc9a05876f6685fc6c386b5e792b7c3`.
The AERP-8 v2 same-checkpoint attack regressions keep checkpoint/commit/tree/model/Git
at A while ordinary visible receipts are resealed; they reject (1) an in-root
ordinary text file as `original_python`, (2) another real interpreter, and (3)
another pinned-tree file as `mempalace` import origin. Rejection occurs at public
freeze or formal-public/release before a valid release can be minted; the custodian
does not stat/read custody or invoke the scorer.
Independent root verification on 2026-08-23 used fresh E-drive basetemps and
`-p no:cacheprovider`: `python -m pytest tests/test_aerp8_membench.py -q --basetemp
E:\MemPalaceWorkspace\repos\.pytest-root-aerp8-pro6-3c71 -p no:cacheprovider`
returned `48 passed in 276.81s (0:04:36)`; `python -m pytest -q --basetemp
E:\MemPalaceWorkspace\repos\.pytest-root-aerp8-pro6-full-73d2 -p no:cacheprovider`
returned `716 passed, 4 skipped in 433.42s (0:07:13)`. `py_compile` and
`git diff --check` also passed. These are implementation/pre-formal-checkpoint
receipts, not benchmark results.
Docker/cgroup work does not block Track A
efficacy, but
`resource_comparability=unavailable` cannot support an efficiency claim. The
remaining order is Pro review, clean checkpoint, source-manifest/profile freeze
and acquisition, one-shot per-dataset execution/gates, then Track B.

ConvoMem's official primary metric is category-specific LLM-judged answer
accuracy by conversation-count context; message-level exact-evidence Recall@10,
NDCG@10, and MRR@10 are this project's blinded retrieval protocol and must not
be described as official ConvoMem metrics.  Bind protocol claims to upstream
commit `624f582ecf0d336ae1d4539d19186089800774b1`.  Preserve ordered structured
speaker/text messages and opaque conversation boundaries in candidate-safe
projections, but keep the original MemPalace public-product document serializer
text-only.  Exact evidence mapping is `(speaker, text)` within the custody-only
evidence-conversation set; ambiguity or absence fails closed without fuzzy
fallback.

Docker/cgroup evidence belongs to the optional systems-security or strict
resource-accounting appendix and must not block the two-dataset Track A efficacy
run. If such a descendant-inclusive resource claim is reported, current and
original workers must use the same Linux cgroup-v2 CPU accounting; host PID/RSS
polling cannot prove that a short-lived child did not run. `memory.current` is a
query-boundary charged-memory snapshot and `memory.peak` is a container-lifetime
charged-memory peak, never per-query peak RSS. Docker `State.Pid` on Windows
Docker Desktop is daemon/VM evidence, not a Windows host PID.

## Verification

Run `python -m pytest -q`, including `tests/test_aerp1_authorized_evidence_audit.py`
for the deterministic 24-query / 48-call AERP-1 gate and
`tests/test_aerp2_product_six_view.py` for the authorization-after-ranking
contract. The release-quality retrieval gate is
`python -m benchmarks.aerp2_product_six_view_locomo` from a clean worktree; its
JSON output must remain outside both repositories, and both `p1_pass` and
`p2_pass` (therefore `release_pass`) must be true. Then run `git diff --check`.

The non-causal FCD-0 checkpoint diagnostic is
`python -m benchmarks.aerp2_fusion_error_decomposition --artifact <quality-json>
--expected-artifact-sha256 <sha256> --expected-git-head <commit>
--output <outside-repo-json>`. It must fail closed on receipt or denominator drift,
must not rerank, and must keep generated reports outside the repository.

## AERP-1 branch and artifact boundary

`SceneEventInput` requires explicit `branch_id` and `branch_status`; missing
metadata never inherits `active`. `active` is only valid for non-retired truth,
while retconned and abandoned truth require their matching branch status.
Imported TavernDB history uses `legacy:<campaign_id>` deterministically.
Before any entity creation, drawer call, or SQLite write, every event also needs
a non-empty event type, summary, and exact unique source span. ACL metadata
must match visibility exactly; a belief owner must equal its actor.

The repeatable offline artifact command is
`python tests/run_aerp1_audit.py --output <outside-repo-path>`. The report's
policy trace is audit telemetry: denied candidate IDs may be recorded there but
must never cross into selected evidence, rendered output, or delivered spans.
Do not commit generated reports.

For a release checkpoint, add `--require-clean`. The report binds the exact
`HEAD` commit and tree plus the SHA-256 of Git's binary/full-index parent patch.
A dirty or untracked worktree fails this mode; generated reports remain outside
the repository so the binding is not self-referential.
