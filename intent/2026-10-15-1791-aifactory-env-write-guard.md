---
status: draft
issue: 1791
author: olafkfreund
---

# Intent: task agents on the Claude SDK path must not write `.aifactory/.env`

## Problem

Still open on dev at `7f7777a4`.

`<project>/.aifactory/.env` holds project settings that the backend reads in
process: `CUSTOM_MCP_SERVERS` and the per-agent MCP toggles
(`apps/backend/core/client.py:414-460`), plus the keys #1790 lets into
subprocess envs (tokens, provider endpoints). A Claude SDK task agent can
write that file today:

- when the agent runs in a task worktree, `core/client.py:696-723` adds
  `Read/Write/Edit/Glob/Grep(<main clone>/.aifactory/**)` to its allow list;
- when it runs in the main clone (spec creation, ideation, roadmap), the
  project allow list `Write(<project>/**)` already covers the file;
- the permission mode is `bypassPermissions` (`client.py:744`, `:1060`);
- the only PreToolUse write hook, `git_dir_write_hook`
  (`security/hooks.py:261-283`), refuses `.git` paths only.

So one agent session can change the MCP server command or the credentials
the next session starts with.

## Proposed outcome

On the Claude SDK path, a Write, Edit, MultiEdit or NotebookEdit whose target
resolves (after `..` and symlinks) to a file named `.aifactory/.env` is
refused, wherever the agent runs. Worktree agents keep write access to the
`.aifactory` subpaths they need and lose the blanket write grant on the rest
of the main clone's `.aifactory/`.

## Affected users and systems

- `apps/backend/security/filesystem_validators.py`, `security/hooks.py`,
  `core/client.py`; the in-process tool executor (`tools/executor.py`, used by
  the Ollama and OpenAI-compatible agentic providers) shares the `.git` guard
  and gets the same check.
- Every Claude SDK agent session (coder, planner, QA, spec, PR review).

## Constraints

- Must not break a working agent phase: never remove a write path a phase
  needs; keep Read/Glob/Grep on the main clone's `.aifactory/**`.
- Same shape as the `.git` guard: fail closed on resolution errors, log no
  paths.
- No exploit detail in commits, PRs or comments.
- Out of scope: external CLI providers (#1747); moving settings out of the
  clone (#1795); the operator UI still writes the file through the backend,
  not through an agent tool, and is unaffected.

## Open questions

None. Bash coverage is limited to what the `.git` guard does (none in the
hook; see the spec).
