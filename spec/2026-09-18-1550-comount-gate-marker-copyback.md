---
status: approved
issue: 1550
intent: intent/2026-09-18-1550-comount-gate-marker-copyback.md
---

# Spec: Bring the gate marker home on the co-mount Job path

## Design

Recommended answer to the intent's open question: **hang the copy-back on the completion
path**, where the marker is fetched home on the packed path too, so "bring the gate
evidence home" happens in one place.

1. **Reuse #1249's copier instead of writing a second one.**
   `services/review_redrive_service._sync_cycle_file_from_worktree(main_spec, worktree_spec)`
   already copies one file from the worktree spec dir into main, only when it is newer, and
   never raises. Give it a filename parameter and make it the shared helper:
   `sync_spec_file_from_worktree(main_spec, worktree_spec, name)`. The existing #1249 call
   passes `qa_review_cycle.json`. `_worktree_spec_dir(project_path, spec_id)` becomes public
   alongside it.
2. **Call it for `.trailing_gates_done` in
   `services/completion_orchestration.run_terminal_completion`**, immediately before
   `emit_terminal_completion(...)` (`completion_orchestration.py:~140`). That function
   already has `project_path`, `spec_dir` and `spec_id`, and it is the path kubejob builds
   take (`agent_kubejob.py:236`).
3. **Validation stays with the reader.** A copied marker counts as evidence only if its
   first-line sha matches the task branch tip (#1563) or the worktree HEAD. Copying never
   makes a marker "true".

How the paths interact:
- co-mount Job: the worktree spec dir exists on the shared PVC, so the copy runs;
- packed Job: there is no worktree on the control plane, so the copy is a no-op and #1563's
  object-store fetch inside `emit_terminal_completion` supplies the marker;
- in-pod and subprocess builds: the generic sync already copied it, so the mtime check makes
  this a no-op.

**Dependency:** this builds on #1563 (branch-tip binding). Implement it after #1563 merges,
or rebase onto it.

## Alternatives rejected

- **Hang it on the kubejob reconcile tick, like #1249.** It runs every tick for a file that
  is written once per build, and it splits "marker comes home" across two places.
- **Revive the generic worktree-sync loop under kubejob.** #1249 already rejected that:
  per-tick copies of every file, which is the thing that loop's removal avoided.
- **A second copy helper just for the marker.** It would be a duplicate of #1249's, with the
  same edge cases handled twice.

## Risks

- Renaming the #1249 helper touches `review_redrive_service.py`. The #1249 tests must
  pass unchanged, apart from the call signature.
- If completion runs before the Job's final write (a race), the copy finds an older marker
  or none. That is a false negative, the honest direction, and no worse than today.
  `run_terminal_completion` runs once the Job has terminated, so the file is final.

## Verification

- Unit: a worktree spec dir with a marker and a main spec dir without one →
  after `run_terminal_completion`, main has the marker. When main already has a newer one →
  it is not overwritten. When there is no worktree → no-op, and nothing raises.
- Existing #1249 tests (`review_redrive`) and completion-orchestration tests pass.
- Merger: a copied marker whose sha matches `aifactory/<spec>` → the PR body shows the gate
  line. A copied marker for a different sha → "no verification gates recorded".
