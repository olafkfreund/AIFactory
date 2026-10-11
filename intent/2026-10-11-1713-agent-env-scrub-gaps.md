---
status: draft
issue: 1713
author: olafkfreund
---

# Intent: close the agent env scrub gaps left after #1674

Not fixed on dev. Checked at `e176db99`: all four gaps in the issue are
still there, and no merged PR (#1716, #1734, #1691) closes any of them.

## Problem

Agents should not see host secrets or the server's contract verdict in
their environment. Four gaps remain:

1. **The tool loop is not scrubbed.** The Bash and Grep tools in
   `apps/backend/tools/executor.py` (`:313`, `:418`) spawn with no `env=`,
   so every command the model writes gets the runner's full environment.
   This covers the `ollama`, `ollama-cloud`, `openai-compatible` and
   `github-models` runtimes. A probe running `echo $X` through
   `ToolExecutor` printed `APP_API_TOKEN`, `OLLAMA_API_KEY` and
   `AIFACTORY_TRUSTED_CONTRACT`. Gemini does not use this path, contrary to
   the #1708 review comment.
2. **`OLLAMA_API_KEY` is not denied.** It is forwarded into build Jobs
   (`build_backend.py:300`) but is missing from `_AGENT_ENV_DENY_EXACT`
   (`core/auth.py:118`), so Claude-SDK agents in the Job are not blanked
   for it.
3. **The deny check is case-sensitive.** `is_denied_env_key`
   (`auth.py:170`) denies `APP_API_TOKEN` but not `app_api_token`. The web
   server's pydantic settings (`apps/web-server/server/config.py:248`, not
   the `apps/backend/core/config.py` the issue names) accept either case.
4. **Deny list or allowlist (#363).** Each new secret name is a leak until
   someone adds it. This structural cause has not been decided.

Nothing reports any of this: no error and no log line. A leak only shows if
the model prints env output somewhere.

## Proposed outcome

- Commands run by the Bash and Grep tools cannot read host secrets or
  `AIFACTORY_TRUSTED_CONTRACT`.
- `OLLAMA_API_KEY` is blanked for agents, and Ollama builds still work.
- A lowercase or mixed-case version of a denied name is denied too.
- CI fails if a new unscrubbed spawn is added to the executor.
- The allowlist question has an answer: done here, or moved to its own
  issue.

## Affected users and systems

- Builds on the four tool-loop runtimes (`providers/factory.py:57-81`,
  `ollama_agentic.py:179`, `openai_compatible_agentic.py:141`).
- Deployments that set `OLLAMA_API_KEY`: every build Job, on any runtime.
- Operators who set `APP_*` names in lowercase. This is latent; none was
  found in the repo.
- `core/auth.py`, `core/child_env.py`, `tools/executor.py`,
  `apps/web-server/server/services/build_backend.py`, and the guard test
  `tests/test_no_unscrubbed_spawn.py`, which does not list
  `tools/executor.py` today.

## Constraints

- Reuse `core.child_env.child_env(...)`. Do not write a new scrubber.
- Build Jobs must keep the passthrough keys they need. `OLLAMA_API_KEY`
  stays in `_PASSTHROUGH_BUILD_ENV`, because the provider reads it
  in-process (`providers/_ollama_http.py:26`).
- Keep `_AGENT_ENV_KEEP` and the `ANTHROPIC_API_KEY` opt-in.
- Keep the git pins from #1620/#1627, and from #1717 once it lands.
- Do not touch `_exec_write`/`_exec_edit` (#1719, PR #1749) or the
  `trusted_contract` blank in `auth.py` (#1708, PR #1751).
- Model the executor test on `tests/test_agent_cli_env.py`.
- Check #1741, #1745, #1746 and #1747 for overlap before widening scope.

## Open questions

1. **Allowlist (#363):** switch to an allowlist in #1713, or patch the deny
   list here and move the allowlist to its own issue? An allowlist changes
   every agent spawn, including operator MCP credentials whose names can be
   anything (#1680).
2. **How strict the executor env should be.** Plain `child_env()` also
   drops every `*_KEY`/`*_TOKEN`, including `GITHUB_TOKEN`/`GH_TOKEN` and
   `NPM_TOKEN`, so model commands that run `gh`, push, or install from a
   private registry would break. The options are `child_env()`,
   `child_env(runner=True)`, or `child_env(keep=...)` with a named list.
   All three strip `AIFACTORY_TRUSTED_CONTRACT`.
3. **Lowercase names:** case-fold only in `is_denied_env_key`, which
   changes nothing users can see? Or also set `case_sensitive=True` in the
   web-server settings, which breaks any deployment that sets `APP_*` in
   lowercase?
4. **Guard tests:** ship these in #1713 or as follow-ups? There are three:
   blanks applied before the MCP env merge; no credential on the
   passthrough lists; `tools/executor.py` added to
   `test_no_unscrubbed_spawn.py`.
5. **Ordering:** wait for PRs #1749 and #1751 to merge first, or rebase
   over them?
