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

## Verification

Run `python -m pytest -q`, including `tests/test_aerp1_authorized_evidence_audit.py`
for the deterministic 24-query / 48-call AERP-1 gate and
`tests/test_aerp2_product_six_view.py` for the authorization-after-ranking
contract. The release-quality retrieval gate is
`python -m benchmarks.aerp2_product_six_view_locomo` from a clean worktree; its
JSON output must remain outside both repositories, and both `p1_pass` and
`p2_pass` (therefore `release_pass`) must be true. Then run `git diff --check`.

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
