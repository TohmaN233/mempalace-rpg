# Paper research program

This is the single source of truth for paper-oriented work. Track A establishes
retrieval effectiveness; Track B tests story generation and character-memory
isolation. Neither track can rescue a failed gate in the other.

## Claim boundary and current status

Track A is incomplete. Burned LoCoMo/AERP-5 results are engineering evidence,
not confirmation. No ConvoMem or MemBench formal source has been opened,
enumerated, hashed, downloaded, or run. Therefore there is no current paper
result and no current per-dataset gate result. Track B has a protocol but no
completed controlled experiment.

ConvoMem's official primary endpoint is category-specific LLM-judged answer
accuracy by conversation-count context. The blinded exact-evidence
Recall@10/NDCG@10/MRR@10 protocol is an added AERP retrieval endpoint, not an
official ConvoMem metric. LongMemEval-V2 is external QA/latency/LAFS evidence,
not an official evidence-R@10 confirmation dataset.

## Implemented formal common path

AERP-7 provides the common trusted-host formal path: candidate/custody
separation, independent current and original worker processes, durable
no-replace publication, HMAC/nonce authorization, and exact-byte retry. Its
formal data path remains unused; its synthetic fixtures and smoke checks are
implementation evidence only.

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

The exact original pin is commit
`72ccd2f3653ab902e419d15bb542c88045342b04`, tree
`5e4ad9cf1d6387cebe16dd03b6da8355d899f70c`, and model-tree SHA-256
`76217893f057779cee29c903aa24444154ad0da7645853f1041fd970cca275a0`.
The formal path also binds interpreter/source import paths, clean Git state,
driver code receipts, and model receipts. Current-worker provenance binds the
reviewed `.venv/Scripts/python.exe` and `.venv/pyvenv.cfg` paths plus byte hashes.
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
5. Only after Track A, run the frozen Track B experiment.

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
custody-only evidence conversation set; zero or multiple matches fail closed
without fuzzy fallback.

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
pinned original public product: overall P5-minus-original point improvement at
least `0.01` and 95% paired-bootstrap lower bound greater than zero; hard-slice
point nonnegative and lower bound at least `-0.01`; all integrity,
authorization, physical-audit, and failure/retry guardrails. MemBench reports
the four cross roles plus factual/reflective and participation/observation
aggregates, and reports `0-10k` and `100k` profile slices as predeclared
secondary results. Its primary point is P5 versus the five-original mean; each
bootstrap draw samples five complete original builds with replacement and applies
the same sampled build multiset to every row. It has no official
adversarial or abstention endpoint, so both are N/A rather than fabricated
gates.

No pooled statistic may rescue a dataset that fails. Static Six-View is a
separate secondary arm. Original-product confidence or confidence-margin claims
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
freeze Track B and run it once. LongMemEval-V2 may then provide an external QA/
latency/LAFS run without being relabelled as evidence R@10.

## Paper-ready definition

Paper readiness requires one immutable method checkpoint, two separately
passing untouched retrieval datasets, exact original-product and matched-budget
comparisons, authorization/trace/failure-retry gates, the controlled Track B
experiment, one external-validity QA/latency run, and reproducible code/data/
model/environment/output receipts.
