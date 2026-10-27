---
status: approved
issue: 1921
spec: spec/2026-10-27-1921-sync-allowlist.md
---

# Plan: allowlist the worktree extra-files sync

Approved decisions: the extra-files loop in `_sync_worktree_files` copies only
`token_usage.json`, `injection_scan.json`, `guardrail_halt.json`,
`handback_received.json`; `files_to_sync` is unchanged; the Factory#1960
`never_sync` set is removed as redundant. No `*.md` mirroring. Two steps, two
files: implemented directly (below the coder hand-off threshold).

## Steps

1. `apps/web-server/server/services/agent_worktree_sync.py` (the `never_sync`
   set and the "Sync any additional files" loop): replace with an allowlist
   tuple and a loop over it → verify by `ruff check` + `ruff format --check` on
   the file.
   Traps: keep the per-file `try/except` + warning; module-level name only if
   tests need it (they do not).
2. `tests/test_task_control_plane.py`: add
   `test_agent_sync_mirrors_only_allowlisted_extra_files` → verify by
   `pytest tests/test_task_control_plane.py tests/test_corrupt_plan_status.py
   tests/test_live_usage_emit.py tests/test_live_usage_marker.py
   tests/test_memory_sync_merges.py tests/test_agent_worktree_sync_mixin.py`,
   and by checking the new test fails with step 1 reverted.
   Traps: `git add <paths>` only; never stage the `.venv` symlink.

## Tests

The pytest command in step 2: all pass.

## Rollback

Revert the PR commit; the generic mirror returns.
