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

## Verification

Run `python -m pytest -q`, including `tests/test_aerp1_authorized_evidence_audit.py`
for the deterministic 24-query / 48-call AERP-1 gate, then `git diff --check`.

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
