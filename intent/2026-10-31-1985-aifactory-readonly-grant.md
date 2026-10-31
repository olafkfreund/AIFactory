---
status: draft
issue: 1985
author: olafkfreund
---

# Intent: agents get read-only access to the source project's .aifactory/

## Problem

When an agent runs in a task worktree (`<project>/.aifactory/worktrees/tasks/<id>/`)
or a PR-review worktree, `apps/backend/core/client.py` (~766-782) grants it
`Read/Write/Edit/Glob/Grep` on `<project>/.aifactory/**`. That tree holds the
canonical spec dirs (`task_metadata.json`, `review_state.json`,
`task_control.json`, `implementation_plan.json`), which are gate inputs. #1983
made `run.py` read its gates once per process, but in the subprocess backend
the directory persists, so a write in one run is read by the next one (`--qa`,
retry, resume). The grant is also only a statement: the session runs with
`bypassPermissions`, so the allow list does not stop the Write and Edit tools by
itself. Re-verified on origin/dev 8497484b: the grant is unchanged and no hook
refuses those writes.

## Proposed outcome

- The grant on the source `.aifactory/` (and legacy `.worktrees/`) is
  `Read/Glob/Grep` only.
- The Write/Edit/MultiEdit/NotebookEdit PreToolUse hook refuses any file-tool
  write into the source project's `.aifactory/` from a worktree session, except
  inside the session's own worktree (where its localized spec dir lives).
- Reads still work.

## Affected users and systems

Agent sessions in task and PR-review worktrees (subprocess and Job backends).
Spec creation and other sessions whose cwd is the project root are unchanged.

## Constraints

- No legitimate agent-side write may break. Known writers are already
  control-side: the orchestrator syncs plan/progress/QA files back
  (`sync_plan_to_source`, `agent_worktree_sync`), and the worktree has its own
  `project_dir`/`spec_dir` grants.
- Must not change the Bash path; Bash can still write there while
  `Bash(*)` and `AIFACTORY_BASH_SANDBOX=false` hold. That is #1932 (owner
  decision B: agents only in Jobs).

## Open questions

None. Owner decision on #1932 (2026-10-31) lists #1985 as gate hardening step 1.
