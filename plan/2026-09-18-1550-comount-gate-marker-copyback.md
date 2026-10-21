---
status: approved
issue: 1550
spec: spec/2026-09-18-1550-comount-gate-marker-copyback.md
---

# Plan: Bring the gate marker home on the co-mount Job path

## Decisions (carried from the approved spec)

- The copy-back happens on the **completion path**
  (`services/completion_orchestration.run_terminal_completion`), which kubejob builds
  reach through `agent_kubejob._emit_kubejob_terminal_completion` once the Job is done.
- It **reuses #1249's copier**. `review_redrive_service._sync_cycle_file_from_worktree`
  becomes `sync_spec_file_from_worktree(main_spec, worktree_spec, name)`: it copies only
  when the source is newer and never raises. `_worktree_spec_dir` becomes public beside it.
  There is no second helper and no new sync loop.
- Copying never makes a marker evidence. The reader still binds it to the branch tip or
  worktree HEAD (#1563).
- Placement detail found while planning: the copy runs at the **top** of
  `run_terminal_completion`, before the #1070 evidence gate and outside the
  `.terminal_completion_emitted` guard. The evidence gate checks commits, not the marker,
  so this does not affect it. Running outside the guard means a retried completion still
  copies, and the mtime check makes a repeat a no-op.
- **Dependency:** #1563 (branch-tip binding) must be merged first, or this branch rebased onto it.

## Steps

0. Wait for #1563 to merge, then `git rebase origin/dev`. (If you tell me to go ahead first,
   rebase onto `fix/1550-gate-marker-dir-mismatch` instead.)
   → verify: `trailing_gate_marker_is_current(..., project_dir=...)` exists on the branch.
1. `apps/web-server/server/services/review_redrive_service.py`:
   - rename `_sync_cycle_file_from_worktree(main_spec, worktree_spec)` →
     `sync_spec_file_from_worktree(main_spec, worktree_spec, name: str)`, using `name`
     where it used `_CYCLE_FILE`, with the body otherwise unchanged. Generalise the docstring
     (keep the #1249 history and add the #1550 use);
   - rename `_worktree_spec_dir` → `worktree_spec_dir`;
   - update the #1249 call in `check_review_obligation` to
     `sync_spec_file_from_worktree(main_spec, worktree_spec, _CYCLE_FILE)`.
   → verify: `grep -rn "_sync_cycle_file_from_worktree\|_worktree_spec_dir" apps/web-server`
   returns nothing. `tests/test_review_redrive.py` and `tests/test_kubejob_review_redrive.py`
   pass; update any test that imports the old private names, and nothing else.
2. `apps/web-server/server/services/completion_orchestration.py`, first statement of
   `run_terminal_completion`'s body:
   ```python
   # #1550: on the co-mount Job path the gate marker is written into the task
   # worktree's spec dir and nothing else brings it home (the packed path's
   # object-store fetch is a no-op here). Copy it before anything reads it;
   # newer-only and never raises, so a repeat or a missing worktree is a no-op.
   try:
       from .review_redrive_service import sync_spec_file_from_worktree, worktree_spec_dir
       sync_spec_file_from_worktree(
           spec_dir, worktree_spec_dir(project_path, spec_id), ".trailing_gates_done"
       )
   except Exception:  # noqa: BLE001 - never break the completion path
       logger.debug("gate-marker copy-back failed", exc_info=True)
   ```
   Use a local import only if a module-level import would create a cycle; otherwise import
   at module level (the cq-ratchet flags PLC0415).
   → verify by step 3.
3. New `tests/test_gate_marker_copyback.py` (repo-root `tests/`, beside the #1249 and
   completion tests it mirrors):
   - a worktree spec dir with a marker and a main spec dir without one → after
     `run_terminal_completion(..., is_terminal=True)`, main holds the same bytes;
   - main already has a **newer** marker → it is not overwritten;
   - no worktree dir → no marker appears in main, and nothing raises;
   - `sync_spec_file_from_worktree` patched to raise → `run_terminal_completion` still returns.
   Patch `emit_terminal_completion` and the other side effects the way
   `tests/test_terminal_completion_characterization.py` (repo root) does.
   → verify: the new tests pass. The first one fails if step 2's call is removed.
4. Merger end-to-end: a test in `apps/web-server/tests/test_merger.py`. A marker copied home
   whose sha equals `aifactory/<spec>`'s tip → the PR body contains `Gate evidence:`. A
   marker for another sha → "no verification gates recorded". This reuses #1563's temp-git
   pattern (absolute imports, `# noqa: S603/S607` on the fixed git argv, `ruff format`).
   → verify: both pass.
5. Lint gates before committing, since #1563/#1564 both failed CI on these:
   `ruff format --check apps/backend apps/web-server scripts tests` and
   `python scripts/cq_ratchet.py --base origin/dev --ruff <venv>/ruff --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   → verify: "0 regressed" and the format check is clean.

## Tests

```bash
# repo-root tests (the #1249, completion and new copy-back tests live here)
/mnt/data/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/pytest \
  tests/test_gate_marker_copyback.py tests/test_review_redrive.py \
  tests/test_kubejob_review_redrive.py tests/test_terminal_completion_characterization.py -v
# merger test lives with the web-server suite
(cd apps/web-server && /mnt/data/Source-home/GitHub/AIFactory/apps/web-server/.venv/bin/pytest tests/test_merger.py -v)
```
Expected: all pass. The #1249 tests are unchanged apart from renamed imports.

## Rollback

Revert the implementation commit. The helper rename reverts with it. Markers already copied
into main spec dirs are harmless: they are data, and still validated against the branch tip.

## Deviations (recorded during implementation)

- **Step 4 reuses #1563's positive test.** `test_honest_body_reports_gate_evidence_without_a_worktree`
  (now on `dev`) already covers a marker in the **main** spec dir matching the branch tip,
  which is exactly where the copy-back puts it. So only the negative is new:
  `test_copied_home_marker_for_another_commit_is_no_evidence`, where a copied-home marker for
  another commit reads as "no verification gates recorded".
- **Step 2 uses an absolute module-level import**
  (`server.services.review_redrive_service`): there is no cycle (checked by importing the
  app), and a relative import would count against the TID252 ratchet.
- **Step 3's ordering assertion is explicit:** the merger stub records whether the marker is
  already in the main spec dir when the merger is called (#1566 runs it inside
  `run_terminal_completion`). Mutation-checked: disabling the copy fails it, and removing
  #1249's newer-only guard fails the no-overwrite test.
