# Paper research program

This document is the single source of truth for the paper-oriented research
program.  The program has two independently gated tracks.  Track A must first
establish retrieval quality; Track B then measures whether that memory layer
causes better story generation.  A result in one track cannot rescue a failed
gate in the other.

## Claim boundary

The project does not construct a benchmark whose purpose is to make AERP win.
It freezes adapters, splits, metrics, resource budgets, and thresholds before
confirmation results are read, then asks whether the frozen method wins.  Public
data already inspected during development is engineering evidence, never blind
confirmation.

The intended paper claim is conditional:

1. an authorization-preserving, raw-anchored multi-view retriever improves
   exact evidence recall over MemPalace and strong raw controls without moving
   the adversarial failure elsewhere; and
2. under a fixed story generator, that memory layer reduces forbidden-knowledge
   leakage and continuity errors while improving authorized character-memory
   use.

Until both claims pass their own gates, the repository may report engineering
progress but not a paper-level result.

## Current status

Track A is not complete.  The repository has engineering results on burned
LoCoMo data, a frozen static-P5 primary arm, and now a formal real-1,982
public-product paired result, but it does not yet contain an untouched-dataset
confirmation.  The original repository benchmark candidate's historical result
is not interchangeable with the public-product result.  Track B has a protocol
but no completed controlled story-generation experiment.  No current artifact
is a paper-level result.

The burned-data AERP-6 transition ledger is a read-only mechanism gate over the
frozen staged LoCoMo artifact.  It records per-evidence six-view and cumulative
full-order ranks, Top-10 boundary margins, raw-to-full transitions, the fixed
no-checkpoint and joint-removal diagnostics, and separate identities for the
published raw-fusion comparator versus the full-order raw prefix.  Its only
router-related output is rank-only preparation evidence for the existing
`RawAnchoredP5Policy`; it cannot select `tau`, alter live ranking, or make a
confirmation claim.

### AERP-5 v2 public-product rehearsal

The AERP-5 v2 specification harness binds the exact 1,982 AERP-4 members (and
the four custody exclusions) and consumes one independently byte-pinned,
label-free projection for a public, non-blind product pairing.  Its
intended arms are the pinned original public-product seam, fixed static P5,
and secondary fixed six-view.  The implemented core enforces two frozen
label-free repeats, AERP4 membership/query/input/policy-lineage binding, and a
two-repeat MiniLM-P5 checkpoint before label custody, per-query cold-first latency,
process-tree RSS, and score-after-freeze custody.  The subprocess runner
constructs the pinned original/current product arms from that projection in
fresh temporary backends.  The formal coordinator may not rebuild projection
from, hash, parse, or load the full official LoCoMo bundle: that bundle remains
custodian-only until both the AERP4 BGE-lineage anchor and the two-repeat
MiniLM-P5 checkpoint have passed.  Before the custodian may even hash the
official bundle, it re-binds the score-config projection path and both
projection digests, dataset path and digest, MiniLM tree digest, and clean
original checkout to the canonical manifest; config-local matching hashes cannot
redirect any input.  It also cross-binds the lineage and MiniLM checkpoint's
membership/query-input identities and canonical fixed-P5 semantics.  The label
consumer itself is independently anchored in that manifest by normalized source
digests of scoring, aggregation, bootstrap, replicate-report, and decision
functions plus exact AERP1/original-protocol dependencies; a live receipt and
its score-config copy cannot jointly authorize a scorer change.  The post-label
item/token join, score computation, unresolved-ledger check,
slice aggregation, decision, and scored-result assembly are one pure pinned
score core; its source digest is part of the same contract, while the custodian
shell only performs label loading, postchecks, and publication.  Before any
freeze bytes are hashed or parsed, the custodian also requires every dynamic
freeze to be a unique non-symlink, non-hard-linked regular file named
`{arm}-{repeat}.json` under the coordinator's external work namespace, with no
filesystem alias to projection, dataset, manifest, or scorer inputs.  The
2026-08-23 `retry7` is now the formal public-product result, bound to commit
`c7d737ee217a71ff6a60f1c30313d8e6e1ea3c61` and tree
`65af2cc3b97d6e790ce9e0563380bab3bbb3911b`.  Its formal artifact is
`benchmark-artifacts/aerp5-v2-real-c7d737e-retry7.json` with SHA-256
`afc596566a37b1cacf561bd0971c1cff44064faba1d23ed159960f1d2ebbb916`.
The result covers 1,982 items, five original-product replicates, and two exact
repeats for each of fixed P5 and secondary fixed six-view.  P5 versus the
original public product has question-macro Recall@10 `0.59416156` versus
`0.46623866`, delta `+0.12792290`, with paired 95% CI
`[0.10722680, 0.14660648]`; the conversation-macro delta is `+0.12827231`
with CI `[0.10771112, 0.14653054]`.  The frozen scientific decision is
`GO=true`, with 9 unresolved evidence items.  P5's question-macro deltas by
category are Cat. 1 `+0.06702051`, Cat. 2 `+0.12087227`, Cat. 3
`+0.08960222`, Cat. 4 `+0.13174792`, and Cat. 5 `+0.17219731`.

This remains a burned-LoCoMo, `public_nonblind` engineering result, not a
confirmation claim.  The secondary fixed-six-view overall question-macro
Recall@10 is `0.58868328`; it exceeds P5 in Categories 2 and 5, while Category
1 is `0.04184089` below P5.  `resource_gate_eligible=false` remains in force:
the blockers are mixed-visibility, blind-180, 30k-event, SQLite/drawer
failure-injection, and frozen resource thresholds.  Track A therefore remains
incomplete; the next untouched confirmation datasets are ConvoMem and
MemBench.

The first real execution of commit `9caacc6` failed the pre-registered 2 GiB
RSS cap before any label scoring or formal report publication.  The observed
worker/supervisor peaks were about 2.209/2.209 GB.  Diagnosis showed that the
label-free transport repeated each conversation's sessions once per question,
producing a 405,116,475-byte projection; the ranking phase itself stayed below
the cap and receipt serialization crossed it.  The allowed follow-up is a
representation-only normalization that stores each conversation corpus once,
keeps the 1,982 query membership and frozen rankings unchanged, and retains the
same 2 GiB threshold.  Raising the threshold or using labels during this repair
is forbidden.

The formal coordinator likewise never opens the AERP4 study or custody bundle:
the custody bundle is label-bearing.  It derives allowed membership solely from
the two immutable, label-free train/dev ranking freezes, whose byte digests and
`study_sha256` fields are checked against the canonical manifest; the manifest's
four exclusion tokens are the only exclusion input available before custody.

The later `retry5` at checkpoint
`982252aa85cbcff2fca200a8725fb24b9da7806a` reached all nine completed label-free
workers but stopped before label custody because its then-current gate demanded
byte-identical Top-10 output from the historical AERP4 BGE-fp32 freeze and the
matched-encoder native-MiniLM product arm.  That is a protocol-gate error, not
a quality result: the 1,982 item membership plus every query and input digest
matched, while the encoders are intentionally different.  The corrected v2
protocol keeps the immutable AERP4 freeze as BGE lineage (including its model
pins and fixed-P5 semantics), never as a cross-encoder ranking oracle.  It
instead freezes two exact label-free native-MiniLM P5 repeats as the
`AERP5-MiniLM-P5` checkpoint and binds that checkpoint into the scoring config
before any official evidence labels are read.  A BGE replay, if run, is an
optional lineage check and cannot become the primary comparison.
The immutable external failure receipt is SHA-256
`da7051d79bd8e434d40480c9a6df492beafd2c5d88ea4e4ef69fece1502f221a`.

## Track A: retrieval

### Frozen method and comparators

The frozen primary retrieval method is now static P5: `SixViewRanker` with
`FixedP5Policy`.  AERP-4b closed the low-complexity sparse routing branch as a
controlled train-only null result, so the formal AERP-5 v2 product comparison
has no `tau` or router selection surface.  `RawAnchoredP5Policy` remains a
historical mechanism experiment rather than the method under confirmation.

Every comparison table must distinguish these arms:

- original repository benchmark candidate: code from the pinned MemPalace
  repository that directly constructs its benchmark index;
- original public product: the pinned product path
  `palace.get_collection(...).upsert(...) -> searcher.search_memories(...)`;
- matched-encoder product comparison: original public product and AERP use the
  same MiniLM implementation, corpus bytes, query bytes, and TopK;
- architecture-controlled raw vector comparison: same BGE encoder and direct
  vector scoring, labelled as a control rather than the original product;
- raw BM25+dense, fixed Product Six-View, static P5, and gated AERP-4 ablations.

The original repository benchmark candidate and the original public product are
different experimental arms.  Neither may be renamed or summarized as the
other.

### Data roles

- LoCoMo and LongMemEval-S are burned engineering data.  They may be used for
  adapter rehearsal and diagnosis; the completed AERP-4 train/dev work cannot
  be reopened as a tuning surface.  They cannot support a blind confirmation
  claim.
- ConvoMem is the first retrieval confirmation dataset.  Split by persona, not
  by question or premixed context.  `message_evidences` belongs only to the
  label custodian.  Abstention items are a separate safety endpoint and do not
  enter positive-evidence R@10.
  The AERP-7 prelabel slice is not a confirmation run and must not score: its
  custodian publishes a normalized candidate projection with opaque HMAC IDs
  plus a separately published custody bundle under a different filesystem
  access boundary.  The candidate-ranking process receives only the candidate
  bundle path; it must not receive or access the custody path or binding secret.
  The custodian later receives both paths and the secret.  The custody bundle
  records that evidence span mapping is unresolved/not attempted; it is the
  only place where answers, evidence labels, directory-derived metadata, and
  source locators may exist.
  The protocol implementation is bound to the upstream
  `SalesforceAIResearch/ConvoMem` commit
  `624f582ecf0d336ae1d4539d19186089800774b1` and tree
  `1699a58948e7ac4e3263110a40d06bab457bcf8b`.  ConvoMem has no single official
  retriever serializer: its `MemoryAnswerer` receives ordered structured
  conversations, while the official LongContext baseline renders messages as
  `speaker: text`.  AERP-7 therefore preserves candidate-safe speaker, opaque
  conversation boundaries, conversation/message order, declared context size,
  actual conversation count, and actual message count.  It separately binds
  the official-structure-compatible LongContext serializer, MemPalace's
  text-only public-product serializer, and AERP's structured SixView event
  serializer.  The opaque-ID adaptation is not a byte-exact reproduction of
  the official LongContext prompt, which includes source conversation IDs.
  Before any labels are unsealed, AERP-7 must also freeze exact-only span
  mapping, a strong raw BM25+dense comparator, static-P5 ranking outputs, and a
  persona/context-stratified paired bootstrap.  Positive evidence retrieval and
  abstention are separate endpoints.  Static P5 has no abstain decision, so the
  latter may report only threshold-free confidence separability unless a
  source-disjoint burned calibration set and decision rule are frozen first.
  The streaming SQLite index must use the explicitly approved staging volume.
  On Windows, launch the isolated benchmark process after setting both
  `$env:TMP=$staging` and `$env:TEMP=$staging`, then pass
  `--staging-root $staging`; on Linux launch it with
  `SQLITE_TMPDIR=$staging` and the same flag.  The builder is supported only on
  Windows or Linux with `renameat2(RENAME_NOREPLACE)` and rejects other systems
  before reading official inputs.  Content-digest bundle loaders remain
  portable beyond those builder platforms.  The process verifies the OS temp
  route and `PRAGMA temp_store=FILE` before reading official inputs.
  Exact message-evidence mapping uses normalized `(speaker, text)` only within
  the item's custody-only evidence-conversation set.  Zero matches are
  unmatched and multiple matches are ambiguous; neither case admits a fuzzy
  fallback.  The official primary ConvoMem metric is category-specific
  LLM-judged answer accuracy by conversation-count context.  AERP-7's blinded
  message-level exact-evidence Recall@10/NDCG@10/MRR@10 is an added retrieval
  protocol, not an official ConvoMem metric.  The upstream
  `retrievedRelevantConversations` count is reported separately as a
  conversation-level diagnostic.  Official answer accuracy is outside Track A
  unless an answer model, prompt, category-specific judge, and their source and
  model digests are frozen before the run.
  Before the one permitted formal execution, a fail-closed coordinator binds
  the candidate `READY`/raw/canonical receipts, clean code/model/source
  identity, all arms and serializers, five isolated original builds,
  byte-identical P5 repeat, bootstrap/gates, and pre-frozen resource limits.
  Candidate workers receive only a label-free allowlist.  A separate custodian
  may open labels only after ranking/resource receipts are cross-bound by a
  release authorization; final publication is exclusive/non-replacing and an
  exact-byte retry is the only idempotent retry.  This synthetic-tested gate is
  explicit about P5 accounting: its primary and repeat executions each require
  a separately digested resource receipt; the release HMAC collectively binds
  both receipts, while the current-worker receipt binds their identical
  artifact digest and exactly two P5 executions.
  `benchmarks.aerp7_convomem_executor` now proves the public coordinator, four
  isolated current-arm processes, five original-worker processes, label-free
  candidate copy, a closed environment allowlist, HMAC-and-nonce operator
  authorization, and exclusive publication using synthetic fixtures.  All nine
  worker outputs are built and validated inside a sibling staging generation;
  worker, validation, or publish failure verifies the staging root identity,
  removes that unpublished generation, and leaves the final path absent, while success uses a platform
  no-replace atomic directory move.  A failed consumed authorization requires a
  newly signed authorization, so two worker populations can never be mixed.  The
  exact original-product route is separately wired to the unchanged public
  `upsert -> reset/close -> cold reopen -> search` lifecycle: each worker emits
  only a canonical, digest-bound, non-publishable draft and leaves its palace in
  place; only the coordinator's independent physical-index re-audit can produce
  a publishable replicate.  Native internal embedding calls are not observable
  behind the upstream cached callable, so the resource evidence truthfully uses
  a cross-checked public upsert/search request ledger and preserves that
  limitation out of band.  The custodian authorization binds packet/file/output,
  nonce and expiry; a successful replay authenticates the existing result and
  never opens custody or scores again.
  The executor deliberately retains `FORMAL_EXECUTION_ENABLED = False` and
  `FORMAL_CURRENT_EXECUTION_ENABLED = False`: it does not read, enumerate, or
  hash ConvoMem, and no formal run may flip either constant without a fresh
  review.  The synthetic resource shape uses a test-only encoder and cannot be
  reported as a resource result.  Formal use still requires exact current-arm
  phase measurements, an OS-enforced worker capability boundary, and a Windows
  durable parent-directory publication primitive.  A blank cwd and environment
  allowlist are defense in depth, not that OS boundary; the same boundary is
  required before path-based recursive staging cleanup can be treated as safe
  against an actively hostile same-user process.
  The next pre-formal execution-evidence checkpoint tightens those remaining
  claims without changing either formal switch.  Current-arm ranking now emits
  an out-of-artifact sidecar containing one wall-clock and process-CPU
  measurement per exact projection item/query pair.  Resource receipts replay
  those rows and bind their order, hashes, call/text counts, storage semantics,
  model identity, provider, artifact, and percentile summaries.  Four current
  executions (`strong_raw`, P5 primary, P5 repeat, and Six-View) each emit an
  execution receipt bound to the protocol, projection, canonical worker config,
  method input, code/model before-and-after observations, provider, process,
  artifact file, resource receipt, and supervisor.  P5 still requires two
  distinct process IDs and byte-identical canonical ranking artifacts.

  Synthetic recovery evidence is now structurally incapable of masquerading as
  a formal release.  It uses a separately signed rehearsal-release schema and a
  separate open/audit path; the formal release validator has no synthetic opt-in.
  Likewise, the synthetic isolation canary records only a plan-level rehearsal
  (`live_evidence = false`), while its separate rehearsal attestation records
  `formal_eligible = false`.  Formal isolation accepts only a live
  container-probe canary, and the absent live runner fails closed.
  The isolation plan itself freezes an image repository digest and config image
  ID, non-root identity, read-only root filesystem, no network, no-new-privileges,
  dropped capabilities, resource limits, role-specific command identity, and
  least-privilege mounts, then cross-checks those claims against normalized raw
  Docker inspect output.

  This checkpoint is not a ConvoMem result.  The public MiniLM model has passed
  a direct native-adapter smoke check without changing its model-file tree, but
  the formal current workers are not yet wired through the live isolated runner.
  The pinned original public product exposes wall time but not process-CPU time,
  so its formal resource gate still fails rather than inventing a value.  The
  local Docker daemon/fixed image/live denial probe are also not yet available.
  Therefore formal ConvoMem data remains unopened, unenumerated, and unhashed
  until those live gates pass and a fresh review authorizes the one-shot run.
- MemBench is the second retrieval confirmation dataset.  Split by `tid` because
  a `tid` recurs across task files.  `target_step_id`, answers, choices, and
  ground truth belong only to the label custodian.  Items with more than ten
  evidence targets retain their real denominator.
- LongMemEval-V2 has no public answer-bearing evidence annotations.  It is an
  end-to-end QA/latency/LAFS external-validity dataset, not an official R@10
  confirmation dataset.
- LoCoMo-Plus reuses the burned LoCoMo corpus.  It is a secondary cognitive-cue
  or story-memory diagnostic, not an independent confirmation dataset.

### Information-flow protocol

For every dataset, the candidate producer receives only opaque item/group IDs,
query text, candidate text, and explicitly frozen candidate-safe source metadata
needed by the retrieval adapter, such as ConvoMem speaker and opaque chronology.
The label custodian receives the frozen ranking digest and then attaches exact
evidence IDs.  Prediction and label payloads have separate digests and are joined
only by an opaque crosswalk.  Any plaintext answer, evidence label, category,
split label, source locator, raw conversation ID, or gold-derived feature
observed by a candidate producer invalidates the run.

Splits are group-disjoint and digest-bound.  Confirmation is executed exactly
once after the static P5 method commit, model files, corpus unit, TopK,
tie-break, resource thresholds, and code/input digests are frozen.  No pooled
statistic may rescue a dataset that fails separately.

### Retrieval endpoints

Report question-macro and group/conversation-macro exact evidence Recall@10.
Also report hard, adversarial or abstention, per-category, NDCG@10, and evidence
micro recall as secondary endpoints.  Confidence intervals use paired bootstrap
resampling at the dataset's leakage unit: conversation, persona, or `tid`.
For ConvoMem, `changing_evidence + implicit_connection_evidence` is a declared
derived hard-positive slice, not an upstream hard category; the dataset has no
official adversarial retrieval endpoint.  Its abstention endpoint reports only
threshold-free confidence separability for arms sharing the frozen normalized
top-margin contract.  The original MemPalace public-product arm has no comparable
confidence and is excluded from that secondary comparison with an explicit
reason.

The public, non-blind AERP-5 v2 product gate requires all of:

- static P5 is compared through the current product seam against the exact
  pinned original public-product seam;
- paired conversation-bootstrap 95% confidence-interval lower bounds for both
  question-macro and conversation-macro Recall@10 are greater than zero;
- hard Categories 1/2, adversarial Category 5, and every category are reported
  separately even when they are not part of the primary GO rule;
- authorization, trace replay, exact-span, performance, and failure-injection
  guardrails pass.

### AERP-5 v2 original-product identity amendment

The label-free failure receipt `d90b622e91b27511d69ca3d9fb77214907d7d3e1aa3774a103b3ff1a4a58b2cf`
superseded an initial HNSW-only explanation.  The original LoCoMo adapter had
written ten conversations' locally restarted `dialog_...` IDs into one Chroma
collection, so later upserts overwrote earlier physical records.  The amended
protocol namespaces physical IDs by conversation, asserts the full 5,882-record
collection, cold-reopens it before querying, and maps every returned physical ID
back to its local dialog ID before custody/scoring.  Labels remain sealed.

The original public batch-upsert seam still has observed index-build variation
after that repair.  It is therefore measured as five fresh cold-reopen batch
replicates; each current arm remains two byte-identical repeats.  The primary
original value is the per-query arithmetic mean over those five replicates, and
P5-vs-original CIs use a deterministic hierarchical bootstrap over conversation
clusters and original index-build replicates (seed 20260822; 5,000 resamples).
Every original replicate and its overall, hard Categories 1/2, Category 5, and
per-category variability are reported separately.  This is a public non-blind
engineering amendment, never a confirmation claim.

All three AERP-5 v2 arms use a matched lifecycle: ingest all ten conversations,
close and cold-reopen their backend, then query all 1,982 items.  Current-arm
latency ends on `current_product_rank` return, before benchmark trace/receipt
construction; original-arm latency ends after the namespaced public-search
adapter has validated and mapped the returned physical IDs.  Each original
replicate also carries a coordinator-remeasured physical-ID, float32 embedding,
SQLite HNSW-configuration, and four-file HNSW graph receipt before custody can
unseal labels.

Each untouched confirmation dataset independently requires all of:

- versus the exact pinned original public product, overall point improvement is
  at least 0.01 and the paired group-bootstrap 95% lower bound is greater than
  zero; fixed Product Six-View remains a separately reported secondary arm;
- hard-subset point change is nonnegative and its lower bound is at least -0.01;
- adversarial/abstention change versus the strong raw control has lower bound at
  least -0.01;
- every safety/system guardrail passes.

For ConvoMem specifically, the overall and derived hard-positive retrieval gates
apply to the blinded message-level protocol above.  The abstention non-regression
gate compares static P5 with the strong raw BM25+dense arm on paired
persona-by-declared-context confidence separability; it does not compare against
an invented confidence for the original public product.  Each context stratum
also reports the declared target, actual conversation count, and actual message
count rather than treating those quantities as interchangeable.

### Resource-matched reporting

Every arm binds exact repository HEAD/tree/diff state, dataset bytes, corpus and
query digests, encoder files and semantics, TopK, candidate depth, tie-break,
hardware, and runtime versions.  Report ingest/index time, query latency
distribution, passage/query embedding calls and text counts, index/storage
bytes, peak resident memory, and output digests.  Native-configuration and
equal-budget tables are separate.  Unsupported measurements fail the formal
resource gate rather than being guessed.

Graph or clustering retrieval remains blocked unless frozen cross-dataset error
analysis identifies a candidate-recall gap that the current view pool cannot
cover.  A fixed-fusion regression alone is not evidence for adding a graph.

## Track B: story generation and character memory isolation

Track B starts only after a frozen Track A method exists.  It uses a fixed story
generator, prompt, decoding configuration, context budget, and scenario seed.
The treatment variable is the memory layer.

### Required scenario families

- character-private secret: one character may recall it and another must not;
- witnessed versus unwitnessed event: knowledge follows scene participation and
  explicit grants rather than global transcript access;
- belief versus world truth: a character can act on a false belief without the
  narrator treating it as fact;
- branch and retcon: abandoned or retconned events cannot leak into the active
  timeline;
- long-horizon callback: an authorized detail must be used after distractors;
- knowledge update: later evidence supersedes an earlier state without erasing
  the historical fact that the character once believed it;
- multi-character handoff: independently generated turns preserve distinct
  knowledge states and voices.

Controlled synthetic worlds provide exact authorization and timeline labels.
Public cognitive/agentic datasets may add external validity, but cannot replace
the controlled leakage tests.

### Story endpoints

Hard gates:

- forbidden-span or forbidden-fact disclosure rate is exactly zero;
- every delivered verbatim span descends from an authorized event seed;
- branch, campaign, character-private, belief-owner, and scope isolation pass;
- trace completeness and deterministic replay are 100%.

Quality endpoints:

- authorized-memory precision and recall;
- character-state contradiction rate;
- active-timeline contradiction and retcon-leakage rate;
- long-horizon callback success;
- plot-task completion and required-fact coverage;
- character distinctiveness/voice consistency under a blinded rubric;
- unsupported-memory assertion rate;
- latency, memory-context tokens, storage, and generation cost.

Mechanical labels take precedence over model judges.  Open-ended quality uses
blinded pairwise judging with randomized arm order, multiple judges, agreement
reporting, and a human audit sample.  The judge never sees method names, traces,
or gold policy metadata.

### Story success gate

Compared with both no-memory and original-MemPalace memory under the same story
generator, the frozen AERP treatment must:

- pass every hard isolation gate;
- reduce character/timeline contradictions with a paired 95% lower bound above
  zero for the improvement;
- improve authorized callback or plot-task success with a paired 95% lower bound
  above zero;
- show no material degradation in blinded narrative quality, with a pre-frozen
  noninferiority margin;
- remain within the pre-frozen latency, token, memory, and storage budgets.

## Paper-ready definition

The work is paper-ready only when the following receipts bind one immutable
method checkpoint:

1. an immutable static-P5 method freeze, strong-raw comparator, and label-blind
   ranking receipts; any future operational abstention threshold must be frozen
   separately from burned, source-separated engineering data;
2. two separately passing retrieval confirmation datasets;
3. a direct original-product comparison plus matched-encoder and equal-budget
   comparisons;
4. the blind-180 authorization suite, mixed-visibility product check, exact
   trace replay, 30k-event performance gate, and durable failure/retry gates;
5. the controlled story-generation isolation and quality experiment;
6. one final external-validity run, such as LongMemEval-V2 official QA/latency/
   LAFS, without relabelling it as evidence R@10;
7. exact code, data, model, environment, resource, and output digests sufficient
   for independent reproduction.
