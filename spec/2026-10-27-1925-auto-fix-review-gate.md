---
status: approved
issue: 1925
intent: intent/2026-10-27-1925-auto-fix-review-gate.md
---

# Spec: manual auto-fix start must not force-run an unreviewed plan

## Design

1. **Force only fresh specs.** In `services/auto_fix_service.py`
   `start_auto_fix`, remember whether the spec dir was created by this call.
   Pass `force=True` to `start_task_execution` only then (fresh spec: no plan
   yet, the planner runs and the post-planner gate applies — today's
   behaviour). When an existing spec dir is reused, pass `force=False`.
   `should_pass_force(spec_dir, False)` still sends `--force` when the spec
   does not require review, and otherwise lets run.py's pre-flight decide:
   valid approval → builds; no or stale approval → saves pending review and
   exits (task paused in human_review). The web-server needs no copy of the
   approval-validity logic.
2. **Project authz.** Every route in `routes/auto_fix.py` gets
   `Depends(require_project_access(...))`: `viewer` for `GET queue`,
   `member` for `GET/PUT config` (the config carries `botToken`),
   `check-new` and `{N}/start` (matches `routes/execution.py` task-start).
   The dependency reads a `project_id` param, so the path placeholder
   `{projectId}` becomes `{project_id}`. URLs are unchanged.
3. **Wire `requireHumanApproval`.** `start_auto_fix` reads it from
   `get_config(project_id)` and passes it to `_write_spec_dir`, which sets
   `requirements.json` `metadata.requireReviewBeforeCoding = true` (merged
   with any delegation metadata). `sync_require_review_metadata` already
   carries that to `task_metadata.json`. Fresh specs still start with
   `force=True`; the pause happens at the post-planner gate. Applies to the
   background poll too, which is the point of the setting; with the setting
   off (default) the poll is byte-for-byte unchanged.

## Alternatives rejected

- Refuse to restart a spec in `human_review`: misses specs whose status is
  something else but still lack approval; run.py's pre-flight is the single
  authority.
- Check `is_approval_valid` in the web-server: duplicates backend logic in
  `review/state.py` (another lane is editing it) and can drift.
- Delete `requireHumanApproval`: wiring it is a few lines and the UI exposes it.

## Risks

- A reused spec that requires review and was never approved now pauses
  instead of coding. That is the fix.
- Viewers and users outside the project's org lose access to auto-fix
  routes; with auth disabled (dev) the dependency allows all, as elsewhere.
- With `requireHumanApproval` on, fresh specs rely on the post-planner gate
  (#1922) to pause.

## Verification

Regression tests in `apps/web-server/tests`: non-member → 403 on start;
reused spec → `force=False` (pre-flight decides; `should_pass_force` false
when review required); fresh spec → `force=True`; `requireHumanApproval`
→ metadata flag written. Plus ruff and the existing auto-fix tests.
