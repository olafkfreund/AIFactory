---
status: draft
issue: 1985
intent: intent/2026-10-31-1985-aifactory-readonly-grant.md
---

# Spec: agents get read-only access to the source project's .aifactory/

## Design

Chosen (the Fix in #1985, plus enforcement):

1. `apps/backend/core/client.py` worktree loop (~770): `permission_ops`
   becomes `["Read", "Glob", "Grep"]` for the source `.aifactory/` and the
   legacy `.worktrees/`.
2. `apps/backend/security/filesystem_validators.py`: new
   `source_aifactory_write_refusal(path, cwd)`. When `cwd` is a task worktree
   (`/.aifactory/worktrees/tasks/`) or a PR-review worktree
   (`/.aifactory/github/pr/worktrees/`), it refuses a path whose lexical or
   symlink-resolved form is under `<source>/.aifactory` and not under the
   worktree. Resolution errors fail closed. `protected_write_refusal` calls it,
   so the existing `git_dir_write_hook` (PreToolUse on
   Write|Edit|MultiEdit|NotebookEdit) enforces it with no new wiring. The hook
   is needed because the session runs with `bypassPermissions`, where the allow
   list alone does not stop a write.

Writers of the source `.aifactory/` that ran as the agent: none found.
- Spec, plan, progress, QA and memory files are written to the localized
  spec dir `<worktree>/.aifactory/specs/<id>/` (`core/workspace/setup.py`
  `copy_spec_to_worktree`), which is inside the worktree and stays writable.
  The orchestrator (Python, control side) syncs them back
  (`agents/utils.py` `sync_plan_to_source`, `agent_worktree_sync`).
- PR-review agents (`pr_reviewer`, `pr_orchestrator_parallel`,
  `pr_followup_parallel`) get `spec_dir=<source>/.aifactory/github` but have
  read-only tool sets (`agents/tools_pkg/models.py`), so they never wrote there.
- MCP tools (record_discovery etc.) write from the MCP server process, not via
  agent file tools; unaffected.
So no per-file write grant is added.

## Alternatives rejected

- Allow-list change only: a no-op under `bypassPermissions`; the tests in the
  issue (Write/Edit denied) would not hold.
- A settings `deny` rule on `<source>/.aifactory/**`: deny beats allow, and the
  worktree itself lives under that tree, so the agent could not write its own
  worktree. Narrower deny globs would need to list every sensitive subdir.
- Allow writes under `spec_dir` in the hook: when `spec_dir` is the source spec
  dir, that is exactly the gate-file hole being closed.
- Fixing the Bash leg here: tracked in #1932 (agents only in Jobs).

## Risks

- A session whose cwd is a worktree but whose spec dir was not localized
  (copy skipped because the source spec did not exist) now cannot write the
  source spec dir. Nothing to write there in that case; the error is a clear
  refusal, not silent.
- Bash can still write the files (#1932 caveat). Stated in the PR.

## Verification

- New tests: from a task-worktree cwd, Write and Edit to
  `<source>/.aifactory/specs/<id>/review_state.json` are refused (absolute,
  `../` relative, and via a symlink inside the worktree); writes to the
  worktree's own files and localized spec dir pass; from the project-root cwd
  the source spec dir is still writable; `create_client` in a worktree emits
  `Read/Glob/Grep` but no `Write`/`Edit` for `<source>/.aifactory/**`.
- The tests fail on origin/dev without the fix.
- ruff, mypy ratchet, targeted pytest.
- Not verified: a real end-to-end build (the issue asks for one before merge).
