---
status: approved
issue: 1746
issues: [1746, 1747, 1761]
author: olafkfreund
---

# Intent: git metadata write guards (#1746, #1747, #1761)

## Problem

An agent that can write into git metadata (`.git/config`, `.git/info/attributes`)
can make the server run a program the next time the server runs git on that
repo. #1719 closed this for the Claude SDK file tools and the Ollama/OpenAI
`ToolExecutor` only (`apps/backend/security/filesystem_validators.py:175`,
`security/hooks.py:261`, `tools/executor.py:220,243`). Three gaps remain, all
verified on `origin/dev` at 4638e87d:

1. **Bash (#1746).** `bash_security_hook` (`security/hooks.py:124`) checks
   command names and per-command validators. Nothing looks at write targets:
   `echo ... >> .git/config`, `tee`, `cp`, `mv`, `ln`, `sed -i`, and any
   interpreter (`python -c`, a script the agent wrote) can write `.git`.
   `git config merge.x.driver ...` itself is refused
   (`security/git_validators.py:54`, `.driver`), but writing the file is not.
2. **External-CLI providers and MCP tools (#1747).** Codex runs with
   `sandbox: danger-full-access` (`providers/codex_agentic.py:370-371`),
   Antigravity with `--yolo` (`providers/antigravity_agentic.py:260`), Copilot
   with `--allow-all-tools` (`providers/copilot_agentic.py:153`), OpenCode with
   no approval loop. None of our hooks run inside their tool loops. The MCP
   catalogue has no local-filesystem server today
   (`agents/tools_pkg/mcp_catalog.py`), but an operator-added one would not be
   guarded either.
3. **Merge drivers on the server (#1761).** The server's defence is env pins
   (`core/child_env.py:37-48`): hooks, fsmonitor, pager, editor, ssh,
   credentials. A `merge.<name>.driver` cannot be pinned by a fixed key because
   the agent picks `<name>`. Verified with git 2.55: a driver in the repo config
   plus `* merge=<name>` in `.git/info/attributes` runs on
   `git merge-tree --write-tree` and on a real `git merge`; `--attr-source` /
   `attr.tree` and `core.attributesFile=/dev/null` do not stop it, because
   `info/attributes` is always read. Server call sites:
   - `merge-tree`: `apps/web-server/server/routes/worktree_merge.py:224`,
     `apps/backend/core/workspace/merge.py:620`,
     `apps/backend/cli/workspace_commands.py:511` (the last two pass no `env=`,
     so not even the `child_env` pins apply);
   - real merges/rebases (wider than the issue text):
     `worktree_merge.py:656`, `:1948`, `core/worktree.py:1121`,
     `server/routes/pr.py:273`, `server/services/pr_endgame.py:1158`.

   #1761 also asks to widen the #1718 scan (`tests/test_diff_no_ext_textconv.py:22-25`).
   Verified: `git show <commit>` and `git blame` run textconv by default;
   `git show <ref>:<path>`, `show --no-patch` and `format-patch` do not. Every
   `show` on `dev` today is a `ref:path` blob read, so nothing is exploitable
   now; the scan just would not catch a new one.

## Proposed outcome

- No program an agent planted in git metadata runs on the server, whichever
  provider or tool wrote it.
- The fix holds against writes our hooks cannot see (interpreters, external
  CLIs), so it does not depend on a complete Bash parser.
- The #1718 scan also flags `show` with a patch, `stash show -p`, `blame` and
  `range-diff` without the no-driver flags.

## Affected users and systems

- Web server merge preview and merge routes, PR endgame rebase, the backend
  `WorktreeManager` merge, the `workspace` CLI.
- Agent providers: Claude SDK, Ollama/OpenAI ToolExecutor, Codex, Antigravity,
  Copilot, OpenCode.
- Users whose repos define real merge drivers in `.git/config`.

## Constraints

- Real merge drivers the user configured must keep working on real merges.
- Agents must still be able to remove a stale `.git/index.lock`, and `git add`,
  `git commit` and `git worktree` must keep writing `.git`.
- Fail closed: if the server cannot tell whether metadata is safe, it refuses.
- Small, server-side changes first; no new parser.
- Works in k8s Job pods, where bwrap is unavailable (`core/client.py:744-760`).

## Open questions

1. May the merge **preview** treat files covered by a merge driver as
   conflicting (drivers disabled for `merge-tree`), until a trusted-driver
   baseline exists?
2. When repo git metadata changed during a build, should the server refuse the
   merge until a human re-baselines, or blank the new keys and continue?
3. Is an agent-side Bash write guard still wanted as early feedback, knowing it
   cannot be complete, or do #1746/#1747 close once the server-side check lands?
