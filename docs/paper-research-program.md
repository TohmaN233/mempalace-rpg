# Paper research program

This is the single source of truth for paper-oriented work. Track A establishes
retrieval effectiveness; Track B tests story generation and character-memory
isolation. Neither track can rescue a failed gate in the other.

## Claim boundary and current status

The retrieval method-selection phase is complete as a formal project decision
milestone; see [the Phase 1 evidence report](phase-1-retrieval-evidence-report.zh-CN.md).
Across burned LoCoMo, a fixed-seed ConvoMem observed-pair subset, and a
fixed-seed same-scale MemBench study, Static P5 improved the within-study
primary Recall@10 aggregation over the corresponding original-compatible arm.
This freezes Static P5 as the preferred method for the next phase, with
Six-View and Strong Raw retained as controls.

The three completed studies are formal evidence for the scoped Phase 1
question—method feasibility and selection for RPG memory retrieval. Both
sampled studies used one declared fixed seed rather than a result-selected seed:
ConvoMem records `selection_seed=20260826`; MemBench records
`seed=membench-same-scale-v1-20260902`. The receipt field
`formal_evidence_eligible=false` has the narrower meaning of ineligibility for
the AERP one-shot full official-benchmark release protocol. It does not make
the fixed-design comparison informal or block the RPG phase.

The external publication boundary remains explicit. LoCoMo is burned and
non-blind; ConvoMem is not the full census; MemBench is not the missing paper
`data2test` sample and its original arm is compatibility-patched rather than
exact-unpatched. These facts prohibit relabelling the runs as complete official
benchmark reproductions, but that claim is not the objective or completion
gate of Phase 1. The full ConvoMem census is not suitable for this laptop and
is not a planned local run. Track B/RPG memory-retrieval work may now begin.

ConvoMem's official primary endpoint is category-specific LLM-judged answer
accuracy by conversation-count context. The blinded exact-evidence
Recall@10/NDCG@10/MRR@10 protocol is an added AERP retrieval endpoint, not an
official ConvoMem metric. LongMemEval-V2 is external QA/latency/LAFS evidence,
not an official evidence-R@10 confirmation dataset.

## Implemented formal common path

AERP-7 provides the ConvoMem trusted-host formal path: candidate/custody
separation, independent current and original worker processes, durable
no-replace publication, HMAC/nonce authorization, exact-byte retry, and an
external AERP-8 checkpoint revalidation before public work and before custody
opens.  Its source selector is `census-v1`: no RNG, all candidate-visible
persona/group/context/query units in sorted context order, and any duplicate,
missing rank, or candidate/custody crosswalk defect invalidates the entire run.
The checkpoint has an explicit two-layer boundary: AERP-8 revalidates immutable
driver-source/interpreter bytes and the live pinned-original policy, while the
later AERP-7 orchestration commit is separately clean-sealed in the formal
protocol. Thus an orchestration-only AERP-7 commit does not silently weaken or
require rewriting the immutable AERP-8 receipt.
Its formal data path remains unused; synthetic fixtures and smoke checks are
implementation evidence only.

The formal one-shot path deliberately fails closed on Windows and on hosts
without Linux directory-`fsync` publication semantics.  It therefore makes no
durability or exact-retry claim for this Windows development machine; synthetic
rehearsals remain diagnostic only.  A formal run requires a supported Linux
staging filesystem and records its publication boundaries in the READY and
progress receipts.  Formal source output directories must be fresh: the
protocol deliberately disables pre-custody source-generation resume until a
plan/checkpoint/census-authenticated generation seal is implemented.  Exact-byte
retry therefore covers only a fully validated published final packet whose
immutable consumed marker preserves its original file digest.  A crash after
authorization consumption but before public freeze, any consumed authorization
whose final packet/marker is absent, or any incomplete source generation is a
durable terminal infrastructure failure requiring a new signed plan; the
protocol never mints a replacement authorization or reopens custody.
The Linux formal host must first mint a fresh Linux-compatible AERP-8 external
checkpoint; the Windows development checkpoint is not transferable evidence for
that execution identity.

Any future AERP-7 Pro review packet is a transitive, isolated artifact rather
than a diff excerpt.  It must include `aerp7_convomem_rank.py`,
`aerp7_original_product.py`, the imported AERP-5 runtime receipt helpers,
`aerp8_membench.py`, all direct tests and a canonical file manifest.  Its
receipt must record the packet-isolated collection/execution command, Python
and environment identity, manifest digest, collection count and result.  A
synthetic test receipt is implementation evidence, not a benchmark result.

AERP-8 implements the corresponding MemBench path:

- An isolated formal source-builder accepts only a frozen manifest, exact file
  capabilities, an operator-HMAC authorization, and an opacity-secret
  capability; it emits a label-free candidate projection plus sealed custody
  and byte-bound READY receipts.
- Four current workers execute strong raw, static-P5 primary, static-P5 repeat,
  and fixed Six-View in independent processes; the two P5 artifact bytes must
  match.
- Five fresh exact-original workers use the public `upsert -> cold reopen ->
  search` lifecycle and a coordinator physical-index re-audit.
- Before any original worker starts, an external checkpoint with schema
  `aerp8-membench-current-checkpoint-v2` freezes an
  `aerp8-membench-original-execution-policy-v1` digest. A shared no-data probe
  executes the frozen original interpreter under a scrubbed environment and
  verifies exact root/interpreter bytes, `sys.executable`, `sys.version`,
  `sys._base_executable` (or equivalent base identity) and bytes, and the real
  imported `mempalace.__file__` (only the pinned
  `mempalace/__init__.py` is accepted), alongside model and Git identity.
  The same policy digest binds all five worker configs/runtimes/receipts/READY
  files, the original artifact, public packet, release, formal-public check,
  and custodian pre-custody validation.
- A formal custodian validates public/release/READY bytes before opening
  custody; a completed retry does not stat, read, or score custody.
- Formal scoring uses the four fixed MemBench source roles, HMAC `(role, tid)`
  groups pooled across the frozen context profiles, 5,000 stratified bootstrap
  draws with a complete-original-build layer, and
  the frozen overall and reflective gates. Adversarial and abstention are N/A
  for the official MemBench protocol.

Synthetic helpers use separate schemas and cannot authorize formal release or
custody scoring. This path trusts frozen benchmark code and does not claim
hostile same-user containment.

## Pins and smoke evidence

The formal primary common-base pin is official MemPalace v3.8.0: commit
`87e6f38377b4bee0666374b05df6e14ffd154245`, tree
`639b2a849816fd4853072920405822824464e9c6`, and model-tree SHA-256
`76217893f057779cee29c903aa24444154ad0da7645853f1041fd970cca275a0`.
The former 3.6.0 pin, commit `72ccd2f3653ab902e419d15bb542c88045342b04`
and tree `5e4ad9cf1d6387cebe16dd03b6da8355d899f70c`, is historical-secondary
only and is not a formal primary comparator.
The formal path also binds interpreter/source import paths, clean Git state,
driver code receipts, and model receipts. Current-worker provenance binds the
platform-native virtualenv interpreter (`.venv/Scripts/python.exe` on Windows,
`.venv/bin/python` on POSIX) and `.venv/pyvenv.cfg` paths plus byte hashes.
The source tree deliberately leaves local `.venv` untracked: the final clean
checkpoint therefore uses an external runtime/checkpoint receipt with exact paths,
byte hashes, and live revalidation evidence, while the review closure carries both
files only for isolated reproduction.

Small synthetic live smoke evidence is not a dataset result. The five-original
smoke artifact SHA-256 is
`21917673215412ba42cbc2e1fabd4ea019bb8fcb6ed47b17418211c705e4e30d`.
The current-four smoke artifact SHA-256 values are strong raw
`2389a5e51955fa448d1a6e4b65477772fa70fd882a1fe63c89be257d9f4d945b`,
static P5 primary/repeat
`23f008fa4bf7f1a61b3c341d49fa07a2bfa8806e9b1290bb4202a173cda80a7d`,
and Six-View
`330fee44e051cbf126aca6534719372dbfc9a05876f6685fc6c386b5e792b7c3`.

The v2 regression set keeps checkpoint/commit/tree/model/Git at A while ordinary
runtime/worker/artifact/READY/public digests are resealed. It rejects an in-root
ordinary text file substituted as `original_python`, a different real interpreter,
and a different pinned-tree file substituted as `mempalace` import origin. The
freeze/formal-public/release chain fails before an acceptable release can exist;
the custodian neither stats nor reads custody and never invokes scoring.

Independent root verification on 2026-08-23 used fresh E-drive basetemps with
`-p no:cacheprovider`. `python -m pytest tests/test_aerp8_membench.py -q
--basetemp E:\MemPalaceWorkspace\repos\.pytest-root-aerp8-pro6-3c71
-p no:cacheprovider` returned `48 passed in 276.81s (0:04:36)`. `python -m
pytest -q --basetemp E:\MemPalaceWorkspace\repos\.pytest-root-aerp8-pro6-full-73d2
-p no:cacheprovider` returned `716 passed, 4 skipped in 433.42s (0:07:13)`.
`py_compile` and `git diff --check` also passed. This remains pre-formal-checkpoint
implementation evidence, not a MemBench or ConvoMem result.

## Required review and execution order

MemBench `selected_profiles` is frozen as exactly `['0', '100']`, corresponding
to the official paper-sampled `0-10k` and `100k` labels. The source inventory is
the four exact `data/data2test/*_multiple_{0|100}.json` paths per profile; it
does not permit discovery, wildcard expansion, sampling, or post-result drops.
Because these external payloads are not Git/LFS objects in the pinned official
tree, acquisition records the external archive/file byte digests before parsing,
while a clean pinned checkout proves the official generator/code provenance.

The remaining order is:

1. Pro review and a clean checkpoint.
2. Freeze ConvoMem and MemBench source manifests/profiles and acquire authorized
   inputs without reopening a tuning surface.
3. Run each dataset one time through its source-builder, current-four,
   original-five, and custodian path.
4. Apply each dataset's gates separately; do not pool a failure away.
5. After the scoped Phase 1 method-selection gate passes, run the frozen Track
   B/RPG experiment. A separate full official-benchmark reproduction may be
   added later but does not block Track B.

Docker/cgroup work is not a Track A efficacy blocker. `resource_comparability`
may be `unavailable`; no efficiency, matched-resource, or systems-security
claim may be made without the separately required evidence.

## Dataset roles and information flow

LoCoMo and LongMemEval-S are burned engineering data and cannot become blind
confirmation data. ConvoMem is the first confirmation dataset and is split by
persona. `message_evidences`, answers, source locators, and exact evidence
crosswalks are custody-only. Positive retrieval and abstention are separate
endpoints. ConvoMem is bound to upstream commit
`624f582ecf0d336ae1d4539d19186089800774b1` and tree
`1699a58948e7ac4e3263110a40d06bab457bcf8b`.

ConvoMem preserves ordered structured speaker/text messages, opaque
conversation boundaries, declared and actual context sizes, and message order.
The official-structure LongContext serializer, MemPalace text-only public
product serializer, and AERP structured Six-View serializer are distinct
receipts. Exact evidence mapping is normalized `(speaker, text)` within the
custody-only evidence conversation set; in a formal run zero or multiple
matches fail closed without fuzzy fallback.  Synthetic rehearsals retain those
states in the private/public mapping ledger solely as diagnostics and cannot
make a formal endpoint claim.

MemBench is the second confirmation dataset. Its four formal source roles are
`participation_reflective`, `participation_factual`,
`observation_reflective`, and `observation_factual`. The leakage unit is the
HMAC of `(source_role, tid)`; a repeated `tid` across profiles is pooled, while
the same `tid` across source roles is not pooled. `target_step_id`, choices,
ground truth, raw `tid`, profile, and
source locator remain custody-only. More than ten gold targets retain their
actual denominator.

For every dataset, ranking receives only opaque item/group IDs, query text,
candidate text, and pre-frozen candidate-safe metadata. Labels attach only
after public artifact/READY/release validation. Prediction and label payloads
have separate digests and join only through opaque custody crosswalks. Any
candidate-visible answer, label, source locator, raw identifier, category, or
gold-derived feature invalidates the run.

## Retrieval endpoints and gates

Report question-macro and leakage-unit macro exact-evidence Recall@10; NDCG@10,
MRR@10, evidence micro recall, and declared slices are secondary endpoints.
Intervals use paired bootstrap at the dataset leakage unit. For ConvoMem,
`changing_evidence + implicit_connection_evidence` is a declared derived hard
positive slice, not an upstream official hard category. Its abstention endpoint
is threshold-free confidence separability unless a separate source-disjoint
calibration and decision rule are frozen.

Each untouched confirmation dataset must pass separately against the exact
pinned original public product.  ConvoMem's primary current arm is the frozen
`six_view_secondary` configuration (the existing Six-View product arm), with
persona-macro exact-evidence Recall@10 point improvement at least `0.01` and a
95% paired-bootstrap lower bound greater than zero.  It uses 10,000 paired
persona-cluster draws; each draw uses one global five-original-build multiset
for every sampled persona, and its seed is domain-separated from the sealed
protocol digest. P5/raw, question-macro, evidence-micro, NDCG@10, MRR@10,
context slices, and abstention are secondary diagnostics and cannot rescue a
failed primary gate. All integrity,
authorization, physical-audit, and failure/retry guardrails. MemBench reports
the four cross roles plus factual/reflective and participation/observation
aggregates, and reports `0-10k` and `100k` profile slices as predeclared
secondary results. Its primary point is P5 versus the five-original mean; each
bootstrap draw samples five complete original builds with replacement and applies
the same sampled build multiset to every row. It has no official
adversarial or abstention endpoint, so both are N/A rather than fabricated
gates.

No pooled statistic may rescue a dataset that fails. Original-product confidence or confidence-margin claims
must not be invented where the original method has no comparable confidence.

## AERP-5 public-product engineering amendment

The burned AERP-5 v2 LoCoMo result remains non-blind engineering evidence. The
original adapter repair namespaces physical IDs by conversation, asserts the
full collection, cold-reopens before querying, and maps returned physical IDs
back to local IDs. Five fresh cold-reopen original builds represent index-build
variation; current P5 and Six-View use byte-identical repeats. Labels remain
sealed until the label-free receipts, original physical audit, and release
binding pass.

Its historical result must not be relabelled as a confirmation result or used to
choose a new method. The AERP-4 train/dev work is closed to tuning. The exact
original public-product identity is the public lifecycle, not the original
repository's direct benchmark candidate.

## Resource and systems reporting

Every report binds code head/tree/diff state, data and projection digests,
model files and semantics, TopK, candidate depth, tie break, hardware, runtime,
and output digests. When available, report ingest/index time, query latency,
embedding calls/text counts, storage bytes, and memory under a matched contract.
Unsupported measurements are unavailable, never guessed.

Docker, cgroup-v2 accounting, and hostile same-user containment belong to an
optional systems-security or strict-resource appendix. Host PID/RSS polling
cannot prove short-lived-child absence; Docker Desktop PID is VM/daemon evidence
on Windows. A future efficiency claim requires matched descendant-inclusive
cgroup accounting for current and original workers. These restrictions do not
block an efficacy-only Track A result with `resource_comparability=unavailable`.

Graph or clustering retrieval remains blocked unless frozen cross-dataset error
analysis shows a candidate-recall gap that the existing view pool cannot cover.
A fixed-fusion regression alone is not that evidence.

## Track B: story generation and character memory isolation

Track B starts only after a frozen Track A method. It fixes the generator,
prompt, decoding configuration, context budget, and scenario seed; the memory
layer is the treatment. Required controlled scenario families are
character-private secret, witnessed versus unwitnessed event, belief versus
world truth, branch/retcon, long-horizon callback, knowledge update, and
multi-character handoff.

Hard gates are zero forbidden-span/fact disclosure, authorization ancestry for
every delivered span, campaign/branch/private/belief isolation, and complete
deterministic replay. Quality endpoints include authorized-memory precision and
recall, contradiction and retcon-leakage rates, callback success, plot/fact
coverage, voice consistency, unsupported-memory assertions, and cost. Mechanical
labels take precedence; open-ended quality uses blinded randomized pairwise
judging with multiple judges, agreement reporting, and human audit.

Compared with no-memory and original-MemPalace memory under the same generator,
the frozen treatment must pass all hard gates, improve contradiction and
authorized callback/plot success with paired lower bounds above zero, and show
no material blinded-quality regression under a pre-frozen noninferiority margin.

## Milestones

Before data acquisition: Pro review freezes manifests, profile choices, source
roles, metrics, bootstrap units, model files, code checkpoint, budgets, and
authorization operators. During one-shot execution: preserve external artifacts
and no-replace receipts, then do not tune. After both Track A datasets pass:
freeze Track B and run it once. For the current scoped program, the three-study
Phase 1 feasibility result satisfies this transition; full official benchmark
reproduction is an optional later publication track. LongMemEval-V2 may then provide an external QA/
latency/LAFS run without being relabelled as evidence R@10.

## Paper-ready definition

Paper readiness requires one immutable method checkpoint, two separately
passing untouched retrieval datasets, exact original-product and matched-budget
comparisons, authorization/trace/failure-retry gates, the controlled Track B
experiment, one external-validity QA/latency run, and reproducible code/data/
model/environment/output receipts.
