---
status: approved
issue: 1924
spec: spec/2026-10-27-1924-approval-hash-content.md
---

# Plan: hash the reviewed plan content, not the plan file bytes

Approved decisions: the approval hash covers `spec.md` raw plus the plan JSON with a
denylist of runtime progress keys removed (plan level: `status`, `planStatus`,
`reviewReason`, `recoveryNote`, `qa_signoff`, `qa_iteration_history`, `qa_stats`,
`last_updated`, `updated_at`, `phase`; subtask level in `subtasks`/`chunks`:
`status`, `notes`, `updated_at`, `started_at`, `completed_at`, `actual_output`,
`critique_result`). Unknown keys stay hashed. Unparseable plans hash raw. The approve
route and the #1926 gate are unchanged. Two steps, implemented by the session model
(below the coder handoff threshold).

## Steps

1. `apps/backend/review/state.py:30-38`: add the two key sets and
   `_compute_plan_hash(plan_file)`; `_compute_spec_hash` uses it for the plan.
   → verify by `cd apps/backend && .venv/bin/ruff check review/state.py`
   Traps: keep `_compute_file_hash` exported (review/__init__.py); guard non-dict
   phases/subtasks.
2. `tests/test_review_validation.py`: add the regression tests from the spec; switch
   `test_spec_change_detection_accuracy` Test 2 to a description change.
   → verify by `cd apps/backend && PYTHONPATH=. .venv/bin/pytest ../../tests/test_review_validation.py ../../tests/test_review_approval.py`
   Traps: the new "still valid" test must fail on dev.

## Tests

- `PYTHONPATH=. .venv/bin/pytest ../../tests/test_review_*.py` (from apps/backend): green.
- `cd apps/web-server && .venv/bin/pytest tests/test_plan_approval_resume.py`: green.

## Rollback

Revert the commit; stored hashes computed with the new scheme then read as stale
(fail closed) until re-approved.

## Deviations

- Step 2 also edits `tests/test_review_integration.py:230`: it held a second copy of
  the "subtask status change invalidates" assertion; it now changes the subtask
  description, as in `test_review_validation.py`.
- Review (lead, Opus): the wave engine (`agents/utils.record_subtask_started`,
  `record_subtask_completion`) and `parallel_integration.reset_subtask_status_pending`
  round-trip the plan through `ImplementationPlan.load()/save()`, which drops keys the
  model does not carry (phase `id`, unknown keys), writes both `subtasks` and
  `chunks`, and adds `created_at`/`spec_file`/`services_involved`/`final_acceptance`.
  That invalidated the approval. Step 1 now canonicalises with
  `ImplementationPlan.from_dict(plan).to_dict()` before stripping progress keys, and
  falls back to the raw hash when the plan does not parse. Chosen over making the
  helpers write only status fields: `save()` also recomputes plan status, and every
  other `load()/save()` caller would need the same change. Trade-off: a key the model
  drops is no longer bound by the approval; it is also gone after the first save.
  Denylist now: plan `status`, `planStatus`, `reviewReason`, `recoveryNote`,
  `contractHold` (written by `core/contract_trust.record_hold` at build start),
  `qa_signoff`, `created_at`, `updated_at`; subtask `status`, `started_at`,
  `completed_at`, `actual_output`, `critique_result` (keys the model drops need no
  entry). Step 2 adds `test_wave_engine_round_trip_keeps_approval_valid` (fails on
  the previous head) and swaps the phase-`status` invalidation case, which the model
  drops, for a phase `name` change.
- Re-review (lead, Opus): hashing the plan model's view was a regression against
  dev. Keys the model drops were no longer bound but still drive execution:
  `security/plan_commands._collect_strings` grants `required_commands`/`command`/
  `run` from the raw plan at any depth, and the coder reads the raw file. The
  lead's suggestion (make the model round-trip lossless, then hash raw) still
  goes stale on the first save, because `to_dict` adds defaults
  (`services_involved`, `final_acceptance`, `spec_file`, `created_at`, phase
  `name`/`type`, `feature` from `title`). It would also change every `save()`
  output repo-wide.
  Chosen instead, proposed to the lead: the approval stores two hashes, of the
  raw plan as approved (`spec_hash`) and of `ImplementationPlan.from_dict(raw).to_dict()`
  (`saved_spec_hash`). Both are taken after stripping progress keys, at the
  levels they live, with `chunks` ignored when `subtasks` is present.
  `is_approval_valid` hashes the raw current file and accepts either. Any
  post-approval edit at any depth matches neither. A wave round-trip yields
  exactly the saved form, so it stays valid. No model change.
  Denylist back to the writers found on dev: plan `status`, `planStatus`,
  `reviewReason`, `recoveryNote`, `contractHold`, `qa_signoff`,
  `qa_iteration_history`, `qa_stats`, `last_updated`, `created_at`,
  `updated_at`, `phase`; subtask `status`, `notes`, `updated_at`, `started_at`,
  `completed_at`, `actual_output`, `critique_result`. `notes` stays progress
  because `update_subtask_status` and `session.py` write it on every update;
  binding it would bring back the mid-build invalidation this task fixes
  (flagged to the lead). `get_review_status_summary.spec_changed` now uses the
  same validity check.
  Tests: `test_content_edits_invalidate` covers 12 edits, each on the approved
  file and after a wave save: subtask `required_commands`, `steps`, `files`,
  description, verification `run`, new subtask, phase `id`/`name`/`description`,
  `feature`, plan `required_commands`, unknown plan key. The `required_commands`,
  `steps`, phase `id`/`description` and unknown-key cases fail on the previous
  head. Also `test_wave_engine_round_trip_keeps_approval_valid` (two saves) and
  `test_chunks_copy_ignored_when_subtasks_present`.
