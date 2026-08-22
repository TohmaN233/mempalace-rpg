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
LoCoMo data and a tested seam for a direct original-product comparison, but it
does not yet contain a valid AERP-4 `tau` freeze or a complete paired-product
result.  The original repository benchmark candidate's historical result is not
interchangeable with the public-product result.  Track B has a protocol but no
completed controlled story-generation experiment.  No current artifact is a
paper-level result.

## Track A: retrieval

### Frozen method and comparators

The method under development is AERP-4: `SixViewRanker` with
`RawAnchoredP5Policy`.  Its only selected policy parameter is threshold `tau`.
That threshold still must be frozen on burned, source-separated engineering
data; confirmation runners must accept the resulting immutable value as input
and must never enumerate or select thresholds.

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
  adapter rehearsal, diagnosis, and the one-time train/dev `tau` freeze.  They
  cannot support a blind confirmation claim.
- ConvoMem is the first retrieval confirmation dataset.  Split by persona, not
  by question or premixed context.  `message_evidences` belongs only to the
  label custodian.  Abstention items are a separate safety endpoint and do not
  enter positive-evidence R@10.
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
query text, and candidate text.  The label custodian receives the frozen ranking
digest and then attaches exact evidence IDs.  Prediction and label payloads have
separate digests and are joined only by an opaque crosswalk.  Any plaintext
answer, evidence label, category, split label, or gold-derived feature observed
by a candidate producer invalidates the run.

Splits are group-disjoint and digest-bound.  Confirmation is executed exactly
once after the method commit, `tau`, model files, corpus unit, TopK, tie-break,
resource thresholds, and code/input digests are frozen.  No pooled statistic may
rescue a dataset that fails separately.

### Retrieval endpoints

Report question-macro and group/conversation-macro exact evidence Recall@10.
Also report hard, adversarial or abstention, per-category, NDCG@10, and evidence
micro recall as secondary endpoints.  Confidence intervals use paired bootstrap
resampling at the dataset's leakage unit: conversation, persona, or `tid`.

The calibration dev gate requires all of:

- gated AERP-4 strictly exceeds both static Raw and static P5 for question-macro
  and group-macro Recall@10;
- paired 95% confidence-interval lower bounds versus both static experts are
  greater than zero;
- Raw and P5 each route at least 10% of queries;
- authorization, trace replay, exact-span, performance, and failure-injection
  guardrails pass.

Each untouched confirmation dataset independently requires all of:

- versus frozen Product Six-View, overall point improvement is at least 0.01
  and the paired group-bootstrap 95% lower bound is greater than zero;
- hard-subset point change is nonnegative and its lower bound is at least -0.01;
- adversarial/abstention change versus the strong raw control has lower bound at
  least -0.01;
- both routes remain nondegenerate and every safety/system guardrail passes.

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

1. a calibration `tau` freeze from burned, source-separated engineering data;
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
