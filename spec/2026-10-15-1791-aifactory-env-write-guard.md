---
status: draft
issue: 1791
intent: intent/2026-10-15-1791-aifactory-env-write-guard.md
---

# Spec: refuse agent writes to `.aifactory/.env` on the Claude SDK path

## Design

1. **Refusal function.** Add `aifactory_env_write_refusal(path, cwd)` to
   `apps/backend/security/filesystem_validators.py`, after
   `git_metadata_write_refusal` (`:175-198`) and built the same way: make
   `path` absolute against `cwd`, take the lexical form
   (`os.path.normpath`) and the symlink-resolved form (`Path.resolve()`),
   and refuse when either form's last two components are exactly
   `.aifactory` and `.env` (case-sensitive). Fail closed on
   `OSError`/`RuntimeError`/`ValueError`. Return a fixed refusal string;
   never echo the path.
2. **Wiring.** `git_dir_write_hook` (`security/hooks.py:261-283`) already
   runs on `Write|Edit|MultiEdit|NotebookEdit` (`core/client.py:1036-1041`)
   and checks every path key. In its loop, use
   `git_metadata_write_refusal(...) or aifactory_env_write_refusal(...)`
   and log "Blocked agent write into a protected path" (no path). The hook
   name and the matcher stay as they are, so the existing matcher test still
   holds. The in-process `ToolExecutor` (`tools/executor.py:220`, `:243`)
   chains the same check after the `.git` one.
3. **Narrow the worktree grant** (`core/client.py:696-723`). Today a worktree
   agent gets Read/Write/Edit/Glob/Grep on all of `<main>/.aifactory/**` and
   `<main>/.worktrees/**`. What agents write there:
   - task agents: their spec dir is copied into the worktree
     (`core/workspace/setup.py:278-282`, "spec files INSIDE the worktree")
     and is allowed by the `spec_path_str` rules (`client.py:758-761`);
   - PR review agents: `spec_dir` is `<main>/.aifactory/github`
     (`runners/github/services/pr_review_engine.py:242-243` and siblings),
     also allowed by the `spec_path_str` rules;
   - prompts name only `.aifactory/specs/` (`prompts/planner.md:928`,
     `qa_reviewer.md:512`), and say the orchestrator syncs it.
   I cannot prove no agent writes `<main>/.aifactory/specs/**` (the
   fallback when no spec copy is made, setup.py:301), nor the legacy
   `<main>/.worktrees/**`. So: keep Read/Glob/Grep on both dirs; keep
   Write/Edit on `<main>/.aifactory/specs/**` and `<main>/.worktrees/**`;
   drop Write/Edit on the rest of `<main>/.aifactory/**`.

   Note: with `bypassPermissions` the allow list does not gate tool calls;
   it feeds the bash sandbox's writable paths. The hook (1-2) is the primary
   control; this step is defence in depth.

## Bash

The `.git` guard has no Bash coverage: `git_dir_write_hook` matches file
tools only, and `bash_security_hook` has no write-target check for
redirects, `cp`, `mv`, `tee` or `sed -i`. Per the brief, no new Bash
heuristic is added. Residual risk: a Bash write to the file is not refused
by a hook; it is limited only by the command allowlist and, when enabled,
the OS sandbox write set (which step 3 narrows). Tracked for #1747/#1795.

## Alternatives rejected

- A separate sibling hook with the same matcher: two hooks doing the same
  path walk; one loop is shorter and keeps one matcher test.
- Blocking any `.env` file: agents legitimately edit an app's `.env` or
  `.env.example` in the repo they work on.
- Case-insensitive match: the loader reads the exact name on Linux; the
  brief asks for case-sensitive.
- A `permissions.deny` rule: path-rule semantics under bypass mode are not
  testable here; the hook is.
- Moving settings out of the clone: #1795.

## Risks

- An agent that wrote `<main>/.aifactory/<other>` (not specs) through the
  allow list loses the bash sandbox write grant there. Nothing found that
  does; the Write/Edit tools are still not blocked by the allow list under
  bypass mode, so file-tool writes there keep working.
- Hardlinks to the file are not caught (same as the `.git` guard).

## Verification

- New tests in `tests/test_command_sandbox.py`: Write, Edit, MultiEdit and
  NotebookEdit to `<main>/.aifactory/.env` refused, also via a `..` path,
  a relative path and a symlink in the worktree; worktree files,
  `<main>/.aifactory/specs/x`, `.env.example`, `app/.env` and
  `.AIFACTORY/.env` pass; executor Write refused.
- New test in the same file: `create_client` from a worktree path writes
  settings whose allow list has no `Write(<main>/.aifactory/**)` but has
  `Write(<main>/.aifactory/specs/**)` and `Read(<main>/.aifactory/**)`.
- The refusal tests fail on `origin/dev`.
- `ruff check`, `ruff format --check` on touched files; the full
  `tests/test_command_sandbox.py` passes.
