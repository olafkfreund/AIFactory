---
status: draft
issue: 1719
spec: spec/2026-10-10-1719-agent-git-dir-write-guard.md
---

# Plan: Guard agent writes to `.git/` paths

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1719`, branch
`fix/1719-agent-git-dir-write-guard`, base `origin/dev`. All paths are
repo-relative; backend paths are under `apps/backend/`.

## Approved decisions (self-contained summary)

**Problem in one line.** An agent's file tools can write into git metadata
(`.git/config`, `.git/hooks/*`, a linked worktree's `.git` pointer file). Git
then reads or runs those files later. This change refuses file-tool writes to
any path that has a `.git` component.

- **D0 Baseline.** HEAD `371f3ab1` is the old base plus 6 docs-only commits. It
  is behind `origin/dev` (5 commits on 2026-10-11; the count grows), where PR
  #1722 (#1689) has merged as `ed266886`. None of the later dev commits touch
  the files below.
  Step 0 rebases onto `origin/dev` before any code. Of the touched files, only
  `security/hooks.py`, `tests/test_command_sandbox.py` and `CHANGELOG.md` differ
  on dev. Line numbers below are **dev** line numbers where they differ (HEAD
  numbers in brackets).
- **D1 Scope.** Agent-side guard only. No server-side scan of `.git/config`.
  The server is protected by env pins: `core.hooksPath` (`core/child_env.py:64`
  on dev), `core.fsmonitor` and `protocol.ext.allow` (#1722), and #1717 for the
  rest.
- **D2 Bash.** Not covered. Only Write, Edit, MultiEdit and NotebookEdit are
  guarded. Follow-up after #1720.
- **D3 Blocked area.** Any path with a component that equals `.git` after
  casefold. The match is on whole components, so `.gitignore`, `.github/`,
  `.gitattributes`, `repo.git/` and `src/git/` stay allowed. There is no
  allow-list.
- **D4 Sequencing.** Rebase on #1689 (merged). Do not wait for #1720.
- **D5 Providers.** Covered: the Claude SDK PreToolUse hook and `ToolExecutor`
  (Ollama, OpenAI-compatible). `simple_client` gets no hooks. Instead,
  `spec_compaction`'s tools become `[]`, which also removes Bash. **That alone
  removes nothing at runtime; see B1 below.** External-CLI
  providers (Codex, Gemini, Copilot, OpenCode, Antigravity) and MCP write tools
  are a follow-up.
- **D6 On a hit.** Return `{"decision": "block", "reason": refusal}` and log
  one warning. The path is never logged. It reaches the agent only through
  `reason`. There is no audit store.
  **Deviation from the spec (approver to confirm):** the spec logs
  `"... tool=%s", tool_name`. `tool_name` comes from the input, and CodeQL
  `py/log-injection` does not treat the frozenset check as a sanitiser. This
  plan logs a constant only:
  `logger.warning("Blocked agent write into git metadata")`.
- **D7 Resolution.** No gitdir or commondir resolution: no `git rev-parse` and
  no `gitdir:` parser. The `.git`-component check covers linked worktrees,
  submodules (`.git/modules/x`) and pod clones. Bare repos cannot occur,
  because `WorktreeManager.__init__` calls `_ensure_not_bare()`
  (`core/worktree.py:291`, defined at `:466`). The known ceiling is
  `--separate-git-dir` to a path outside any `.git`. A `ponytail:` comment
  records it and names the upgrade (`git rev-parse --git-dir/--git-common-dir`).
- **D8 Helper.**
  `git_metadata_write_refusal(path: str, cwd: str | None) -> str | None` in
  `security/filesystem_validators.py`. It runs `expanduser`, then makes the
  path absolute against `cwd or os.getcwd()`. It builds two forms, lexical
  `os.path.normpath` and `Path.resolve()`, and refuses if any casefolded
  component of either form equals `.git`. It fails closed: `resolve()` raising
  `OSError`, `RuntimeError` or `ValueError` is a refusal. The exceptions are
  named; there is no bare `except`.
- **D9 Hook.**
  `async def git_dir_write_hook(input_data: dict[str, Any], _tool_use_id: str | None = None, _context: Any | None = None) -> dict[str, Any]`,
  typed like `web_fetch_security_hook`. The leading underscores keep the strict
  ruff ratchet from counting two new `ARG001`s; the SDK passes all three
  positionally (`claude_agent_sdk/_internal/query.py:446`), so the names do not
  matter to it. It returns `{}` unless `tool_name` is
  in `_GIT_DIR_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})`.
  It blocks a non-dict `tool_input`. It blocks when
  `tool_input.get("file_path") or tool_input.get("notebook_path")` is not a
  non-empty `str`. It takes `cwd` from `input_data.get("cwd")`, the same source
  as `bash_security_hook` (`hooks.py:146` on dev).
- **D10 Registration.**
  `HookMatcher(matcher="Write|Edit|MultiEdit|NotebookEdit", hooks=[git_dir_write_hook])`
  goes after the WebSearch matcher and before the catch-all guardrail matcher.
  It is imported on its own line, `from security.hooks import git_dir_write_hook`.
  `security/__init__.py` is not touched.
- **D11 Executor.** Pass the **raw** `file_path` with
  `cwd=str(self._working_dir)`, so both forms are checked. A refusal returns
  `ToolResultBlock(content=refusal, is_error=True)` through the existing
  `if error:` return (`error = error or <helper>`), because a seventh `return`
  in `_exec_edit` trips `PLR0911` in the strict ratchet. `Write` and `Edit` are
  the only write handlers in `_handlers`.
- **D12 Footprint.** 5 backend files (6 with D5a: `core/simple_client.py`),
  plus `tests/test_command_sandbox.py` and `CHANGELOG.md`. There is no new module, spawn, env var or allow-list
  forwarding, so k8s Job pods are unchanged.
- **D13 Verification.** `TestGitDirWriteGuard` uses a real `git init` and
  `git worktree add`, plus mutation checks, the four related test files, ruff,
  `cq_ratchet --staged` and `gen_autonomy_matrix --check`. See Tests.
- **D14 Follow-ups.** File three: Bash write targets (after #1720);
  external-CLI providers and MCP write tools; any git config key that #1717
  leaves unpinned.

**Plan-level resolutions of spec gaps.**

1. **Symlink loop.** On Python 3.12 (CI), `Path.resolve()` on a loop raises
   `RuntimeError`. On 3.13+ it returns a path and does not raise (checked on
   3.14). The fail-closed branch is tested by monkeypatching `Path.resolve` to
   raise. The real-loop test asserts a block only below 3.13. On 3.13+ it only
   asserts that the hook does not raise. A looped path cannot land in `.git`
   anyway, because the kernel returns ELOOP on the write.
2. **NOTE-A, bashlex skip.** `tests/test_command_sandbox.py` has a module-level
   `pytestmark` that skips the whole file when bashlex is missing (dev
   `:28-30`). The new class stays in that file. `bashlex>=0.18` is a runtime
   dependency (`apps/backend/requirements.txt:46`), so CI never skips it.
3. **NOTE-B, simple_client callers.** Six agent types reach `simple_client`.
   Backend: `spec/compaction.py:61` (`spec_compaction`),
   `commit_message.py:258` (`commit_message`),
   `analysis/insight_extractor.py:375` (`insights`),
   `runners/github/batch_issues.py:155` (`batch_analysis`),
   `runners/github/batch_validator.py:208` (`batch_validation`),
   `merge/ai_resolver/claude_client.py:67` and `core/workspace/merge.py:1440`
   (`merge_resolver`). Web-server: `apps/web-server/server/routes/github.py:181`
   (`batch_analysis`) and
   `apps/web-server/server/services/conflict_service.py:477`, `:611`
   (`merge_resolver`). Only `spec_compaction` lists write tools in its config
   today (`Read, Glob, Grep, Write, Edit, Bash`); at runtime all of them have
   them (B1).
4. **Executor test masking.** `_validate_path` refuses anything outside
   `working_dir`. Executor tests therefore use `working_dir=orig` and the
   relative `.git/config`, so the new guard does the refusing and the boundary
   check does not.

**B1, blocker: spec Q5 does not remove `spec_compaction`'s tools.**
`create_simple_client` (`core/simple_client.py:108-118`) passes the config's
tools as `allowed_tools` and runs under `bypassPermissions`. In the installed
SDK, `allowed_tools` only auto-approves; the available set is `tools`
(`claude_agent_sdk/types.py:1582-1596`), which `simple_client` never passes, so
the CLI gets its default tool set. An empty `allowed_tools` is not even sent
(`_internal/transport/subprocess_cli.py:256`). So every simple agent type,
including `commit_message` and `merge_resolver` with `tools: []` today, already
has Write, Edit and Bash, and the `models.py:170` edit changes nothing at
runtime. Test #10 as specced only reads `AGENT_CONFIGS`, so it passes against
the hole.

**Deviation D5a (approver to confirm; adds a sixth backend file).** In
`core/simple_client.py`, inside `ClaudeAgentOptions(...)` right after
`permission_mode=` (:117), add:

```python
            # #1719: no hooks here, so no write tools; allowed_tools does not restrict.
            disallowed_tools=["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"],
```

Every simple client is text or read-only by its docstring, and after the
`models.py:170` edit no caller's type lists a write tool, so nothing loses a tool it uses. Use a deny list, not
`tools=allowed_tools`: `insights` and `batch_analysis` list
`mcp__aifactory__web_fetch`, which is not a built-in name for `--tools`. Keep
the `models.py:170` edit (the spec's decision; it keeps the config honest).
Test #11 captures the options and proves the deny list (see Tests). Prototyped
in a scratch worktree: ruff and mypy ratchets 0 regressed,
`gen_autonomy_matrix --check` ok, and `tests/test_agent_env_scrub.py`,
`tests/test_outbound_scrub_all_providers.py` and
`tests/test_runner_hardening.py` pass. If the approver rejects D5a, drop the
simple_client edit and test #11, and say in the PR that Q5 is not enforced.

**Model split.** There are 3 or more file-editing steps across 7 files (8
with D5a), so hand off to `coder` (Sonnet). Send step 0 and step 1 to start, then steps 2-4
to the same agent with `SendMessage`. Review with a fresh `opus` agent, given
only this plan path and `git diff origin/dev...HEAD`. The PR says which steps
the coder did.

**Shared files and merge order.**

- **#1720:** shares `security/hooks.py` and `tests/test_command_sandbox.py`.
  #1719 merges first. Both changes only append, so #1720 rebases and keeps
  both.
- **#1717:** touches only `child_env.py` and `test_no_unscrubbed_spawn.py`, so
  there is no file overlap. Its merge decides follow-up 3.
- **#1708, #1624, #1627:** no shared files.
- **#1620, #1631, #1632, #1633, #1692:** share only `CHANGELOG.md`. Keep both
  entries.

## Steps

Run every Python command with
`P=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin/python`
(3.12.13, has pytest and bashlex). The worktree has no venv, and the system
python (3.14) has no pytest. Put that venv's `bin` on `PATH` for ruff, mypy
and the pre-commit hook. Run from the worktree root.

0. **Rebase (no edits except conflict resolution).**
   `git rebase origin/dev`. The 6 local commits are docs-only and should
   replay cleanly.
   → verify by `git rev-list --count HEAD..origin/dev` printing `0` and
   `$P scripts/gen_autonomy_matrix.py --check` passing.
   Traps:
   - From here on, use dev line numbers: `hooks.py` imports at 12-31,
     `web_fetch_security_hook` at 34-73, cwd at :146, EOF at 257;
     `test_command_sandbox.py` `pytestmark` at 28-30, EOF at 213, and `asyncio`
     already imported.
   - If `--check` ever fails, regenerate the matrix rather than hand-merging
     it. The script cites none of the touched files, so no regeneration is
     expected.

1. **`tests/test_command_sandbox.py:213` (EOF): red tests, class
   `TestGitDirWriteGuard`.**
   - Add module imports near the top: `re`, `subprocess`, `sys`,
     `from pathlib import Path`, `from core.child_env import child_env`,
     `from security.hooks import git_dir_write_hook`,
     `from security.filesystem_validators import git_metadata_write_refusal`,
     `from tools.executor import ToolExecutor` and
     `from agents.tools_pkg.models import AGENT_CONFIGS`.
     - Use plain imports with no aliases. Skip any import the dev file already
       has, such as `asyncio`. Do not add `os`: nothing uses it.
     - Run `ruff check --fix` and `ruff format` on the file. The new `from`
       imports must sort into the existing `security.*` block, or default
       `ruff check` fails `I001`.
   - Fixture `repo(tmp_path)` returns `(orig, wt)`. Every `subprocess.run`
     passes `env=child_env()` and `check=True`:
     1. `git init -q <orig>`
     2. `git -C <orig> -c user.name=t -c user.email=t@t commit -q --allow-empty -m init`
     3. `git -C <orig> worktree add -q <wt>`
   - The fixture then creates these filesystem entries:
     - the symlink `wt/link -> orig/.git`
     - the directory `tmp/store/`
     - the symlink `tmp/ext/.git -> tmp/store`
     - the loop `wt/a -> wt/b`, `wt/b -> wt/a`
   - Helper `_hook(tool, ti, cwd)` returns
     `asyncio.run(git_dir_write_hook({"tool_name": tool, "tool_input": ti, "cwd": cwd}))`.
     Paths go in `file_path`, except NotebookEdit, which uses `notebook_path`.
   - The 36 tests (42 with D5a) are listed in Tests below.
   - The `simple_client` test's docstring names the callers from NOTE-B.

   → verify by
   `$P -m pytest -q tests/test_command_sandbox.py`
   failing at collection with `ImportError` on `git_dir_write_hook`. That is
   the accepted red evidence.
   Traps:
   - **Subprocess env.** Every `subprocess` call needs `env=child_env()`, or
     `test_no_unscrubbed_spawn` fails. Pass the identity as `-c` arguments, not
     env vars.
   - **Symlink loop.** Do not assert a block on 3.13+ (resolution 1).
   - **Temp paths.** No `tmp_path`-derived path may contain a `.git` component
     except the ones the test creates on purpose.
   - **bashlex skip.** The module-level skip also covers this class. This is
     accepted (resolution 2).

2. **`security/filesystem_validators.py:8-9, after :170` and
   `security/hooks.py:23, after :257` [HEAD :259]: helper and hook.**
   - In `filesystem_validators.py`:
     - Add `import os` and `from pathlib import Path` to the stdlib block.
     - Append `git_metadata_write_refusal`, per D8:

       ```python
       p = Path(path).expanduser()
       if not p.is_absolute():
           p = Path(cwd or Path.cwd()) / p
       forms = [Path(os.path.normpath(p))]
       try:
           forms.append(p.resolve())
       except (OSError, RuntimeError, ValueError):
           return refusal
       if ".git" in {c.casefold() for f in forms for c in f.parts}:
           return refusal
       return None
       ```

     - Use `Path`, not `os.path.expanduser/isabs/join` or `os.getcwd`. Those
       add `PTH111/117/118/109` and fail the strict ruff ratchet (this file's
       baseline is 3). `os.path.normpath` has no PTH rule and is the only `os`
       use.

     - The refusal string may echo the path, because only the agent sees it.
     - Add the `# ponytail:` comment for the `--separate-git-dir` ceiling (D7).
   - In `hooks.py`:
     - Add `from .filesystem_validators import git_metadata_write_refusal  # noqa: E402`
       to the relative-import block, in alphabetical order before `.parser`.
       The block sits below `logger = ...`; without the `noqa` the strict
       ratchet counts a new `E402`.
     - Add a module-level `_GIT_DIR_WRITE_TOOLS` frozenset.
     - Append `git_dir_write_hook` at EOF, per D9 and D6. It logs only the
       constant message. Name the unused parameters `_tool_use_id` and
       `_context` (D9).
   → verify by
   `$P -m pytest -q tests/test_command_sandbox.py::TestGitDirWriteGuard -k "blocked or allowed or malformed or resolve_error or loop or log_has_no_path"`
   passing, while the executor, matcher and simple_client tests stay red. Then
   run `ruff format apps/backend/security tests/test_command_sandbox.py` (the
   block-reason return lines exceed 88 columns), then
   `ruff format --check apps/backend tests && ruff check apps/backend tests`,
   and both ratchets with the files staged (see Tests).
   Traps:
   - **CodeQL.** Never log the path, `tool_input` or `tool_name`
     (`py/log-injection`). Do not name variables `key`, `token` or `secret`
     near the logger (`py/clear-text-logging`).
   - **Exceptions.** Name them; no bare `except`.
   - **Exports.** Do not touch `security/__init__.py`.
   - **Rebase conflict.** A conflict with #1722 in `hooks.py` resolves as
     keep-both, because this change only appends.
   - **Mutations M1-M11 (see Tests).** Apply each by editing and restore it by
     re-editing, never with `git checkout`.

3. **Wiring: `core/client.py:215, :1035-1036`, `tools/executor.py:35,
   :217, :239`, `agents/tools_pkg/models.py:170`, and with D5a
   `core/simple_client.py:117`.**
   - `core/client.py`:
     - After :215, add `from security.hooks import git_dir_write_hook  # noqa: E402`
       on its own line (same reason as in `hooks.py`; the file already uses
       this `noqa` at :208).
     - Between the WebSearch matcher (:1035) and the #474/#476 comment
       (:1036), insert this, already in `ruff format` shape (on one line it
       runs past 100 columns):

       ```python
                       # #1719: refuse agent writes into git metadata
                       HookMatcher(
                           matcher="Write|Edit|MultiEdit|NotebookEdit",
                           hooks=[git_dir_write_hook],
                       ),
       ```

   - `tools/executor.py`:
     - Next to :35, add
       `from security.filesystem_validators import git_metadata_write_refusal`.
     - In `_exec_write` (after :217) and `_exec_edit` (after :239), insert one
       line between `resolved, error = self._validate_path(file_path)` and
       `if error:`:

       ```python
               error = error or git_metadata_write_refusal(file_path, str(self._working_dir))
       ```

     - Pass the raw `file_path`, not `resolved`. Do not add a separate
       `if refusal: return ...`: that is a seventh `return` in `_exec_edit`,
       and the strict ratchet fails `PLR0911` (D11).
   - `agents/tools_pkg/models.py:170`: change
     `"tools": BASE_READ_TOOLS + BASE_WRITE_TOOLS,` to
     `"tools": [],  # #1719: text-only summariser; simple_client has no hooks`.
   - With D5a only: `core/simple_client.py`, the two lines from B1.
   → verify by
   `$P -m pytest -q tests/test_command_sandbox.py::TestGitDirWriteGuard`
   reporting 36 passed (42 with D5a), then the four-file run, ruff, both
   ratchets and `gen_autonomy_matrix --check` (see Tests).
   Traps:
   - **mypy --strict.** The ratchet is per-file. Keep the existing
     `tool_input: dict` signatures and add no new errors.
   - **Environmental failure.** `tests/test_security.py` GitCommitValidator
     fails while files are staged. That is environmental; ignore it.
   - **Strict ruff ratchet.** Checked in a scratch worktree. Without the two
     `noqa: E402`s, the `_`-prefixed hook parameters, the `Path`-based helper
     and the `error = error or ...` form, the ratchet regresses four files
     (`E402` x2, `ARG001` x2, `PTH109/111/117/118`, `PLR0911`). With them,
     ruff and mypy both report 0 regressed.
   - **Mutations M12-M16.** Apply and restore each one by editing.

4. **`CHANGELOG.md:3` (`## [Unreleased]` > `### Security`), follow-ups and PR.**
   - Add one bullet on dev's version of `CHANGELOG.md`:
     > (#1719) Agent Write/Edit/MultiEdit/NotebookEdit into any `.git` path is
     > now refused, for SDK agents and the Ollama/OpenAI-compatible
     > ToolExecutor. Utility (simple) Claude clients no longer get write
     > tools or Bash (with D5a; without it, say only that `spec_compaction`'s
     > config lists none). Not covered:
     > Bash writes (follow-up after #1720), external-CLI providers and MCP
     > tools.
   - Draft the three D14 follow-up issues. Check follow-up 3 once #1717's
     plan exists.
   - Open the PR to `dev`:
     - Link intent, spec and plan.
     - Say which steps the coder did.
     - Note the D6 log deviation.
     - Use commit scope `fix(security)`, with no `#` in the scope.
   → verify by
   `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests`,
   both ratchets and `$P scripts/gen_autonomy_matrix.py --check`.
   Traps:
   - **CHANGELOG conflicts.** Expect them with #1620, #1631, #1632, #1633 and
     #1692. Keep both entries.

## Tests

All 36 new tests (42 with D5a) are in `TestGitDirWriteGuard` in
`tests/test_command_sandbox.py`. Every snippet, count and mutation below was
run on Python 3.12.13 in a scratch worktree rebased on `origin/dev`
(`4470e4c7`).

| # | Test | Cases | Assertion |
|---|---|---|---|
| 1 | `test_blocked_paths` | 12 | `r["decision"] == "block"` and `r["reason"]` is non-empty |
| 2 | `test_allowed_paths` | 6 | `r == {}` |
| 3 | `test_malformed_input_blocked` | 4 | `r["decision"] == "block"` |
| 4 | `test_resolve_error_refused` | 3 | `git_metadata_write_refusal(str(wt / "ok.txt"), str(wt))` is not None, with `Path.resolve` monkeypatched to raise `OSError`, `RuntimeError` or `ValueError` |
| 5 | `test_symlink_loop` | 1 | Write `wt/a` raises nothing; `decision == "block"` only when `sys.version_info < (3, 13)` |
| 6 | `test_block_log_has_no_path` | 1 | `"Blocked agent write" in caplog.text` and `str(orig) not in caplog.text` |
| 7 | `test_executor_write_refused` | 1 | `ToolExecutor(working_dir=orig)` Write to `.git/config` returns `is_error`, and the config bytes are unchanged |
| 8 | `test_executor_edit_refused` | 1 | Edit of `.git/config` with `old_string="[core]"` returns `is_error`, and the bytes are unchanged |
| 9 | `test_matcher_covers_write_tools` | 1 | see below |
| 10 | `test_simple_client_types_have_no_write_tools` | 6 | the type's `AGENT_CONFIGS` tools share nothing with Write, Edit, MultiEdit, NotebookEdit or Bash |
| 11 | `test_simple_client_options_disallow_write_tools` (D5a only) | 6 | see below |

Case details:

- **#1, 12 blocked ids:**
  - `wt_pointer`: Write `wt/.git`
  - `abs_config`: Edit `orig/.git/config`
  - `hooks`: Write `orig/.git/hooks/pre-commit`
  - `info`: Write `orig/.git/info/attributes`
  - `modules`: Edit `orig/.git/modules/x/config`
  - `dotdot`: Write `"src/../.git/config"` with cwd=orig
  - `symlink_abs`: Edit `wt/link/config`
  - `symlink_rel_cwd`: Write `"link/config"` with cwd=wt
  - `ext_dotgit_symlink`: Write `tmp/ext/.git/config`
  - `upper`: Write `orig/.GIT/config`
  - `multiedit`: MultiEdit `orig/.git/config`
  - `notebook`: NotebookEdit `orig/.git/x.ipynb`
- **#2, 6 allowed ids:**
  - Write `.gitignore`, `.github/workflows/x.yml`, `src/git/x.py` and
    `repo.git/readme`, all relative with cwd=wt.
  - NotebookEdit `wt/nb.ipynb`.
  - Read `orig/.git/config`.
- **#3, 4 malformed ids:** `tool_input="x"`, `{}`, `{"file_path": 3}` and
  `{"file_path": ""}`, all with cwd=wt.
- **#9, matcher:**
  - Read `apps/backend/core/client.py` and match
    `re.search(r'HookMatcher\(\s*matcher="([^"]+)",\s*hooks=\[git_dir_write_hook\]', src)`.
  - Assert that all four tool names `re.fullmatch` it and that `Read` and
    `Bash` do not. This also proves the hook is wired in.
- **#10, simple_client types:** parametrised over `commit_message`,
  `merge_resolver`, `insights`, `batch_analysis`, `batch_validation` and
  `spec_compaction`. Only `spec_compaction` fails before step 3.
- **#11, simple_client options (D5a):** the same six types. Capture the
  options as `tests/test_agent_env_scrub.py:108-125` does: monkeypatch
  `simple_client.require_auth_token`, `ClaudeSDKClient` (a `_capture(**kw)`
  that stores `kw["options"]`) and `wrap_client_outbound_scrub`, and
  `monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok")` first so the
  function's env write is undone. Call
  `create_simple_client(agent_type=t, model="claude-haiku-4-5")` and assert
  the five tool names `<= set(options.disallowed_tools)`. All six fail before
  the simple_client edit.

**Mutation checks.** Apply each by editing, run with
`-k <selector>` on `tests/test_command_sandbox.py::TestGitDirWriteGuard`,
expect exactly the listed failures, then restore by editing.

| ID | Mutation | Selector | Expected failures |
|---|---|---|---|
| M1 | Component match becomes `".git" in s.casefold()` | `allowed` | 3: gitignore, github, repo.git |
| M2 | Drop the `.git` check on the resolved form (keep the try/except) | `blocked` | 2: symlink_abs, symlink_rel_cwd |
| M3 | Drop the normpath form | `blocked` | 1: ext_dotgit_symlink |
| M4 | Drop `casefold` | `blocked` | 1: upper |
| M5 | Ignore `cwd` and always use `os.getcwd()` | `blocked` | 1: symlink_rel_cwd (dotdot is masked, which is expected) |
| M6 | Remove the try/except around `resolve()` | `resolve_error or loop` | 4 on 3.12 (3 plus loop) |
| M7 | Drop the `notebook_path` fallback | `allowed` | 1: the notebook case (the blocked notebook case is masked by the missing-path branch) |
| M8 | Change the non-dict guard's condition to `if False:` | `malformed` | 1 (AttributeError) |
| M9 | Change the non-empty-str condition to `if False:` | `malformed` | 3 |
| M10 | Remove MultiEdit from the frozenset | `blocked` | 1: multiedit |
| M11 | Add the path to the log call | `log_has_no_path` | 1 |
| M12 | Delete the `error = error or ...` line in `_exec_write` | `executor_write` | 1 |
| M13 | Delete the `error = error or ...` line in `_exec_edit` | `executor_edit` | 1 |
| M14 | Remove `\|NotebookEdit` from the matcher, or delete the line | `matcher` | 1 |
| M15 | Restore `BASE_READ_TOOLS + BASE_WRITE_TOOLS` at `models.py:170` | `simple_client_types` | 1: spec_compaction |
| M16 | (D5a) Drop `"Bash"` from `disallowed_tools` in `simple_client.py` | `disallow` | 6 |

`expanduser` has no mutation of its own. A `~/.git/...` path is caught by the
component check in either form, so a mutation there could never fail alone.

**Commands and expected results.** Run from the worktree root with `P` as
above.

1. `$P -m pytest -q tests/test_command_sandbox.py::TestGitDirWriteGuard`
   → 36 passed (42 with D5a). All 16 mutations fail exactly as listed.
2. `$P -m pytest -q tests/test_command_sandbox.py tests/test_runner_hardening.py tests/test_ollama_agentic.py tests/test_no_unscrubbed_spawn.py`
   → 136 passed and 3 skipped (measured), 142 passed with D5a. The skips
   are the live-Ollama tests.
   - With D5a also run `tests/test_agent_env_scrub.py
     tests/test_outbound_scrub_all_providers.py` (they patch
     `simple_client`) → pass.
3. `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests`
   → clean.
4. With the files staged, both must exit 0:
   - `$P scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   - The same command with
     `--tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini`.

   The root `tests/` folder is outside ratchet scope.
5. `$P scripts/gen_autonomy_matrix.py --check` → passes unchanged.

## Rollback

- **Full:** `git revert <merge-sha>`. The change only adds code: one function,
  one hook, one matcher line, two executor guards and one tools-list edit.
  There is no data, schema or env change.
- **Fast partial:**
  - Delete the `HookMatcher(matcher="Write|Edit|MultiEdit|NotebookEdit", ...)`
    line in `core/client.py` to disable the SDK guard.
  - Delete the two `error = error or git_metadata_write_refusal(...)` lines
    in `tools/executor.py` to disable the executor guard.
  - With D5a: delete the `disallowed_tools=` line in `core/simple_client.py`
    and test #11 to give simple clients their default tools back.
  - `spec_compaction` can get `BASE_READ_TOOLS + BASE_WRITE_TOOLS` back on its
    own if compaction turns out to need tools. Test #10 then fails on purpose,
    so update it in the same commit.
- **CHANGELOG conflicts:** keep both entries.
