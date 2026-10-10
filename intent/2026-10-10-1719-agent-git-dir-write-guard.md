---
status: draft
issue: 1719
author: olafkfreund
---

# Intent: Guard agent writes to `.git/` paths

## Problem

Build agents can write files inside `.git/`, and nothing stops them.

- The PreToolUse hooks in `apps/backend/core/client.py` guard only `Bash`,
  `WebFetch` and `WebSearch`. The only hooks that see `Write` and `Edit` are
  the catch-all #474/#476 guardrail and mutation hooks, which check no paths.
  Agents run with `bypassPermissions`, so the `Write(./**)` / `Edit(./**)`
  allow list restricts nothing.
- Agents on the Ollama and OpenAI-compatible providers do not go through
  SDK hooks. Their `Write` and `Edit` run in `tools/executor.py`
  `ToolExecutor`, which only checks that the path is inside `working_dir`.
  A `.git` directory, or a worktree's `.git` file, is inside it.
- The Bash hook allows `echo ... >> .git/config`, `printf > .git/config`,
  `tee`, `cp` and `sed -i` onto `.git/config`. It also allows
  `git config core.fsmonitor ...`. Only `git -c <key>=...` is blocked (#321 C4).
- A probe against the real hooks on HEAD c59a0ea6 confirmed every route above
  is allowed, both for a worktree's `.git` pointer file and for the shared
  commondir `config`.

Why it matters: a task worktree shares its `config` and `hooks/` with the
original repo. One write such as `core.fsmonitor=<cmd>` or `core.hooksPath`
runs that command the next time the server runs git in that repo
(`core/worktree.py` uses `_git_env()`, which pins no config keys). The command
runs outside the agent sandbox, with server credentials, and affects every
parallel task. Prompt injection from repo content, a fetched page or an issue
body is enough to trigger it.

The issue describes this as a bypass of #1689's `git config` refusal. That
refusal is not on dev or on this branch; it exists only on
`fix/1689-fsmonitor-git-config-audit`. So today `.git/` is open by every
route, not just bypassable. #1689's plan lists this guard as out of scope.

## Proposed outcome

- An agent that tries to write to the gitdir or commondir of its repo, by
  Write, Edit, MultiEdit or NotebookEdit, gets the call refused with a clear
  reason. This includes a linked worktree's `.git` file and the shared
  commondir reached by absolute path.
- Normal agent work still succeeds: `git add`/`git commit` through Bash, and
  edits to `.gitignore`, `.github/` and other paths that only contain "git".
- The guard runs for every write-capable agent in scope (see open question
  5), locally and in k8s Job pods.

## Affected users and systems

- Every build agent with write tools: spec_writer, planner, coder,
  qa_reviewer, qa_fixer and others (`agents/tools_pkg/models.py`
  `BASE_WRITE_TOOLS` is `Write`, `Edit`, `Bash`; no agent is granted
  `MultiEdit` or `NotebookEdit` today). The insights runner (Read, Glob,
  Grep) and follow-up reviewer (no tools) are not affected.
- Hook registration in `core/client.py`. `core/simple_client.py` builds
  `ClaudeAgentOptions` from the same agent tool config but registers no
  security hooks at all.
- `tools/executor.py` `ToolExecutor`, used by the Ollama and
  OpenAI-compatible agentic providers.
- The external-CLI agentic providers (Codex, Gemini, Copilot, OpenCode,
  Antigravity) run their own file tools, out of reach of both of the above.
- `apps/backend/security/` (hooks, git validators, parser).
- Server-side git in `core/worktree.py` and the original repo's commondir
  shared by all task worktrees.
- In-cluster runs, where the bwrap sandbox is off (#363).

## Constraints

- Hooks are the only enforcement layer while agents run with
  `bypassPermissions`. The guard must fail closed and use the existing
  `{"decision": "block", "reason": ...}` hook shape.
- Must resolve symlinks, `..`, absolute paths and the worktree `.git` file.
  Reuse the existing gitdir/commondir resolution (`core/worktree.py`
  `--git-common-dir`, `gitdir:` parsing in `core/workspace/setup.py` and
  `merge/install_hook.py`); do not add a third parser.
- Must not break legitimate git use or need new env or allow-list forwarding
  to Job pods.
- Overlaps in-flight work: #1689 edits `security/hooks.py`,
  `security/parser.py`, `security/git_validators.py` and `core/worktree.py`;
  #1720 changes `bash -c` unwrapping in the parser; #1717 pins other git
  config keys server-side.
- Security-labelled and multi-file: full intent, spec, plan gates. Security
  test suites (`tests/test_command_sandbox.py`,
  `tests/test_runner_hardening.py`) stay green.

## Open questions

1. Scope: agent-side PreToolUse guard only, a server-side check of
   `.git/config` / `config.worktree` before server git runs, or both? Only
   the server-side check covers scripts.
2. Bash coverage: Write/Edit/MultiEdit/NotebookEdit only, or also
   best-effort checks on redirect, `tee`, `cp` and `sed -i` targets? The
   Bash part stays bypassable through `bash -c` until #1720 lands.
3. Blocked area: all of gitdir and commondir, or only config surfaces
   (`config`, `config.worktree`, `hooks/`, `info/attributes`,
   `modules/*/config`)?
4. Sequencing: wait for #1689 (and maybe #1720) and rebase on them, or
   proceed in parallel and accept conflicts in `security/hooks.py`?
5. Providers: Claude SDK agents only, also `ToolExecutor` (Ollama,
   OpenAI-compatible), or also the external-CLI providers, which can only
   be covered by the server-side check in question 1?
6. On a hit: refuse only, or refuse and write an audit record (as #1689's
   title suggests)?
