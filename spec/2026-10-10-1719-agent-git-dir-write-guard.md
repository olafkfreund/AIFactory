---
status: approved
issue: 1719
intent: intent/2026-10-10-1719-agent-git-dir-write-guard.md
---

# Spec: Guard agent writes to `.git/` paths

## Design

Refuse an agent's file-tool write (Write, Edit, MultiEdit, NotebookEdit) when
the target path has a path component equal to `.git`. One pure helper does the
check. Two places call it: a new Claude SDK PreToolUse hook and the
`ToolExecutor` write handlers. The one `simple_client` agent type that has
write tools loses them. Bash, the external-CLI providers and a server-side
config scan are out of scope (see the answers below).

This branch sits on `dev` at ef25678b. Rebase it on `dev` after PR #1722
(#1689, open, not merged) merges and before implementation starts. That PR
rewrites `security/hooks.py` and `core/worktree.py`.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to accept or change.

1. **Scope.** Agent-side guard only, with no server-side scan of
   `.git/config`. The server side is covered by env pins: `core.hooksPath` is
   pinned today (`core/child_env.py:59`), #1689 (PR #1722) adds
   `core.fsmonitor` and `protocol.ext.allow` and routes `_git_env` through
   `child_env`, and #1717 pins the remaining keys. A pin holds however the bad
   config was written. A scan is a denylist that can be bypassed. File a
   follow-up only if #1717 leaves a key such as `core.sshCommand` or
   `core.pager` unpinned.
2. **Bash.** Not covered: Write, Edit, MultiEdit and NotebookEdit only. A
   check on Bash write targets can be bypassed through `bash -c` or
   `python -c` until #1720 lands, so it would add code without a guarantee.
   Leaving Bash out removes no protection that exists today
   (`security/hooks.py:93` allows these writes now). Follow-up issue after
   #1720.
3. **Blocked area.** All of it, not a list of config surfaces. Any `.git`
   component is refused. A whole-prefix block is less code and stricter, and
   a surface list would miss `modules/*/config`, `info/attributes` and
   `packed-refs`. No agent needs a direct write under `.git`: `git add` and
   `git commit` go through Bash.
4. **Sequencing.** Rebase on #1689 (PR #1722, still open). Do not wait for #1720,
   which matters only for the Bash follow-up.
5. **Providers.** The Claude SDK hooks and `ToolExecutor` (Ollama,
   OpenAI-compatible), plus `simple_client`. `simple_client` registers no
   hooks (`core/simple_client.py:108-118`), and one of its callers,
   `spec/compaction.py:61`, uses `spec_compaction`, whose tools are
   `BASE_READ_TOOLS + BASE_WRITE_TOOLS` (`agents/tools_pkg/models.py:169-170`):
   Write, Edit and Bash, under `bypassPermissions`, on a prompt built from
   untrusted phase output. It only summarises text that is all in the
   prompt, so set its `tools` to `[]` rather than wire hooks into
   `simple_client`. That removes Bash as well, which no hook here would
   cover. The other simple agent types (`commit_message`, `merge_resolver`,
   `insights`, `batch_analysis`, `batch_validation`) have no write tool. The
   external-CLI
   providers (Codex, Gemini, Copilot, OpenCode, Antigravity) run their own
   file tools that no hook reaches. Follow-up issue for them; the server-side
   pins already defuse what they could write.
6. **On a hit.** Refuse with the existing
   `{"decision": "block", "reason": ...}` shape and a `logger.warning`. No new
   audit store; the "audit" in #1689's title means auditing config keys.
7. **Resolving gitdir and commondir.** Not resolved at all; the `.git`
   component check covers both in every layout AIFactory runs:
   - a linked worktree's gitdir is `<orig>/.git/worktrees/<name>`, and its
     commondir is `<orig>/.git`;
   - submodule gitdirs are `<orig>/.git/modules/<x>`;
   - a k8s Job pod clone has `<repo>/.git`;
   - bare repos with worktrees (commondir `repo.git`) cannot happen:
     `WorktreeManager.__init__` calls `_ensure_not_bare()`
     (`core/worktree.py:291`).

   This departs from the proposed `git rev-parse` per client. That would
   cost a subprocess, a scrubbed-env obligation and `git_dirs` threading
   through `create_client`, the hook and the executor, and its only gain is
   the case under Risks. No new parser, which meets the intent's constraint.
8. **Files.** Five backend files, one test file and the CHANGELOG (below).

### Changes

1. **`apps/backend/security/filesystem_validators.py`**: add the pure helper
   `git_metadata_write_refusal(path: str, cwd: str | None) -> str | None`.
   - Make a relative `path` absolute against `cwd` (or `os.getcwd()`), after
     `expanduser()`.
   - Check two forms. The lexical form, `os.path.normpath` of the absolute
     path, catches a `.git` symlink that points out of the tree, which
     `resolve()` would hide. The resolved form, `Path.resolve()`, catches a
     symlink or `..` that leads into `.git`.
   - Refuse if any component of either form, after `casefold()`, equals
     `.git`. `casefold` covers `.GIT/config` on case-insensitive filesystems.
     The match is on a whole component, so `.gitignore`, `.github/`,
     `.gitattributes` and `repo.git/` stay allowed.
   - Fail closed: if `resolve()` raises `OSError`, `RuntimeError` (a symlink
     loop) or `ValueError` (a NUL byte), refuse. Name the exceptions; no
     blind `except`.
   - Mark the ceiling with a `ponytail:` comment (separate git dir, see
     Risks).
   - The module imports neither `os` nor `Path` today; add both.
2. **`apps/backend/security/hooks.py`**: append
   `async def git_dir_write_hook(input_data, tool_use_id=None, context=None)`,
   typed like `web_fetch_security_hook`.
   - Return `{}` unless `tool_name` is in
     `frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})`, so the hook
     is correct whatever the matcher does.
   - Fail closed on bad input: block a non-dict `tool_input`, and block when
     `file_path or notebook_path` is not a non-empty `str`.
   - Call the helper with `input_data.get("cwd")`, the source
     `bash_security_hook` uses (`hooks.py:142`).
   - On a refusal, return `{"decision": "block", "reason": refusal}` and
     log `logger.warning("Blocked agent write into git metadata: tool=%s",
     tool_name)`. The path stays out of the log line (CodeQL
     `py/log-injection`) and reaches the agent in the reason.
3. **`apps/backend/core/client.py`**: add
   `HookMatcher(matcher="Write|Edit|MultiEdit|NotebookEdit",
   hooks=[git_dir_write_hook])` to the PreToolUse list after the WebSearch
   matcher (`:1035`), before the catch-all guardrail matcher. The installed
   SDK documents `|`-joined tool names for `matcher`. Import it as a single
   name, `from security.hooks import git_dir_write_hook`;
   `security/__init__.py` is not touched.
4. **`apps/backend/tools/executor.py`**: in `_exec_write` (`:212`) and
   `_exec_edit` (`:233`), after `_validate_path` (`:217`, `:239`), call the
   helper and on a refusal return `ToolResultBlock(content=refusal,
   is_error=True)`. Pass the raw `file_path` with `working_dir` as `cwd`, so
   the helper sees both the lexical and the resolved form. `_handlers`
   (`:93`) registers no other write tool.
5. **`apps/backend/agents/tools_pkg/models.py`**: `spec_compaction`
   `"tools"` (`:170`) becomes `[]` (Q5).
6. **`tests/test_command_sandbox.py`**: a `TestGitDirWriteGuard` class (see
   Verification).
7. **`CHANGELOG.md`**: an entry under `[Unreleased]`.

No new modules, no new spawn, no new env or allow-list forwarding, so k8s Job
pods work unchanged.

## Alternatives rejected

- **`git rev-parse --absolute-git-dir --git-common-dir` per client, plus a
  prefix check:** it covers only a separate git dir outside any `.git`
  directory, which AIFactory's worktree and pod layouts never produce. It
  costs a subprocess, an env-scrub obligation and argument threading. Add it
  later if that case turns up.
- **A hook factory that closes over `project_dir`:** it guards against a
  wrong `cwd`, but SDK file tools send absolute paths, and the factory adds
  an export and a closure. The `cwd` source matches `bash_security_hook`.
- **A third `gitdir:` parser:** forbidden by the intent.
- **Registering the hooks in `simple_client`:** more code than dropping
  tools a summariser never needs, and it would still leave Bash to the
  Bash hook's gaps (Q5).
- **Allow-listing surfaces (`config`, `hooks/`, ...):** misses cases (Q3).
- **Bash write targets now:** bypassable until #1720 (Q2).
- **A server-side config scan:** a denylist; the pins hold regardless (Q1).
- **Refusing hardlinked targets (`st_nlink > 1`):** false positives on pnpm
  stores, and only Bash can create a hardlink.
- **Moving `target_escapes_worktree` to `resolve()`:** deliberately lexical
  for Bash; out of scope.
- **A new audit store:** speculative (Q6).

## Risks

- **Separate git dir outside `.git`:** a user repo set up with
  `--separate-git-dir`, used directly rather than through a task worktree,
  is not covered. That is the documented ceiling, with rev-parse as the
  upgrade.
- **Check-to-write race:** a symlink swapped between the hook and the write
  is not caught. Only Bash can make that swap, Bash can already write `.git`
  directly (Q2 follow-up), and the server-side pins defuse both.
- **MCP tools that write files** are not covered. Name them in the
  external-provider follow-up.
- **Missing `cwd`:** the hook falls back to `os.getcwd()`, as
  `bash_security_hook` does. SDK file tools send absolute paths.
- **A legitimate `.git` write breaks:** no agent tool config needs one.
- **`spec_compaction` without tools:** its prompt carries the whole phase
  output and it returns text only, so the summary is unchanged. If it ever
  called a tool, the fallback in `spec/compaction.py` returns truncated raw
  output, so the pipeline does not block.
- **Rebase on #1689:** the new hook is appended to `hooks.py`, so a conflict
  resolves as keep-both. The autonomy matrix reads no touched file
  directly, but section C walks import closures through `core.client`, so
  confirm with
  `python scripts/gen_autonomy_matrix.py --check`.

## Verification

- `TestGitDirWriteGuard` in `tests/test_command_sandbox.py`, using a real
  `git init` and `git worktree add` in `tmp_path` with `env=child_env()`:
  - **Blocked:** Write to the worktree's `.git` pointer file; Edit to an
    absolute `<orig>/.git/config`; `hooks/pre-commit`, `info/attributes`,
    `modules/x/config`; `src/../.git/config`; a symlink `link -> <orig>/.git`
    followed by `link/config`; `.GIT/config`; a symlink loop; NotebookEdit's
    `notebook_path` under `.git/`; a non-dict `tool_input`; a missing or
    non-str path.
  - **Allowed:** `.gitignore`, `.github/workflows/x.yml`, `src/git/x.py`,
    `repo.git/readme`, and a non-file tool such as `Read`.
  - **Executor:** `ToolExecutor(working_dir=tmp_path)` returns `is_error` for
    Write and Edit on `.git/config`, and the file is unchanged (pattern:
    `tests/test_ollama_agentic.py:151`).
  - **Matcher:** `re.fullmatch` of the matcher string accepts all four tool
    names.
  - **simple_client:** for each agent type passed to `create_simple_client`
    in `apps/backend` (`commit_message`, `merge_resolver`, `insights`,
    `batch_analysis`, `batch_validation`, `spec_compaction`), the
    `AGENT_CONFIGS` tools include none of Write, Edit, MultiEdit,
    NotebookEdit or Bash. The test lists the types; a new caller is a
    review item.
- Mutation checks, made by editing and re-editing back: a substring match
  instead of a component match fails the `.gitignore`/`.github` cases;
  dropping `resolve()` fails the symlink case; dropping the normpath form
  fails the out-of-tree symlink case; dropping `casefold` fails `.GIT`;
  removing the executor call fails the executor case; restoring
  `spec_compaction`'s write tools fails the simple_client case.
- `pytest tests/test_command_sandbox.py tests/test_runner_hardening.py
  tests/test_ollama_agentic.py tests/test_no_unscrubbed_spawn.py` passes.
- `ruff format --check` and `ruff check` on
  `apps/backend apps/web-server scripts tests`.
- Strict ratchet on the staged backend files, ruff and mypy variants
  (`scripts/cq_ratchet.py --staged`, configs in `standards/`).
- CodeQL: no path in any log call.

### Follow-ups to file

- Bash write targets, after #1720.
- External-CLI providers and MCP write tools.
- Any git config key #1717 does not pin.
