---
status: draft
issue: 1713
intent: intent/2026-10-11-1713-agent-env-scrub-gaps.md
---

# Spec: close the agent env scrub gaps left after #1674

Paths are relative to `apps/backend/` unless they start with `tests/` or
`apps/web-server/`. Line numbers are at `faf72a24` (dev with #1749 and #1751
merged, so the ordering decision is already met).

## Design

Four small edits and the tests that pin them. No new helper. Each item maps
to one approver decision in the intent.

### 1. Tool-loop spawns get `env=child_env()` (gap 1)

`tools/executor.py` spawns twice with no `env=`:

- `_exec_bash`, `asyncio.create_subprocess_shell(...)` at `:316`
- `_exec_grep`, `asyncio.create_subprocess_exec(...)` at `:421`

Both get `env=child_env()`, imported as `from core.child_env import child_env`.
Plain `child_env()` (no `keep`, no `runner`) per the approver decision. It is
built inside each call, so it reflects `os.environ` at spawn time.

What `child_env()` (`core/child_env.py:35-82`) gives these commands:

- Dropped: every `is_denied_env_key` name (now including `OLLAMA_API_KEY`
  and lowercase variants, items 2 and 3), `_STRIP_VARS` (which includes
  `AIFACTORY_TRUSTED_CONTRACT`), and every `*_KEY`/`*_TOKEN` name, so also
  `GITHUB_TOKEN`, `GH_TOKEN`, `NPM_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`.
- Kept: everything else, so `HOME`, `PATH`, `LANG`, `*_BASE_URL`, proxies.
- Added: the six git pins (hooks off, fsmonitor off, `ext::` off, ssh
  `ssh`, pager `cat`, editor `true`).

This covers all four tool-loop runtimes (`ollama`, `ollama-cloud`,
`openai-compatible`, `github-models`), because they all reach the executor
through `providers/ollama_agentic.py:179` or
`providers/openai_compatible_agentic.py:141`. The providers themselves read
their keys in-process (`providers/_ollama_http.py:92`,
`phase_config.py:995`), which this does not touch.

`_exec_write`/`_exec_edit` (#1719) are not touched.

### 2. Deny `OLLAMA_API_KEY` (gap 2)

- `core/auth.py:118` `_AGENT_ENV_DENY_EXACT`: add `"OLLAMA_API_KEY"` to the
  `#1674` block. Claude-SDK agents now get it blanked via
  `get_agent_env_blanks()` (`core/client.py:615`, `core/simple_client.py:89`).
- `apps/web-server/server/utils/subprocess_env.py:47` `RUNNER_KEEP`: add
  `"OLLAMA_API_KEY"`, so the locally spawned runner (`make_subprocess_env`)
  keeps it. Without this, denying it would also drop it from the local
  runner and break local Ollama builds. This is the same pattern #1674 used
  for `OPENAI_COMPATIBLE_API_KEY`.
- `apps/web-server/server/services/build_backend.py:300`
  `_PASSTHROUGH_BUILD_ENV` is unchanged. The Job still gets the key, and the
  provider still reads it in-process.

### 3. Case-insensitive exact match (gap 3)

`core/auth.py:172` `is_denied_env_key` becomes:

```python
return name.upper() in _AGENT_ENV_DENY_EXACT or bool(_AGENT_ENV_DENY_PATTERN.search(name))
```

The pattern is already `re.I`. All entries in `_AGENT_ENV_DENY_EXACT` are
upper case today; a test asserts this so a lowercase entry cannot slip in
and silently never match. The web-server settings
(`apps/web-server/server/config.py:248`) are unchanged: no
`case_sensitive=True`, per the approver decision.

Both callers get it for free: `child_env` (`core/child_env.py:56`) and
`get_agent_env_blanks` (`core/auth.py:193`).

### 4. Executor joins the spawn guard

`tests/test_no_unscrubbed_spawn.py:61` `_BACKEND_FILES`: add
`_BACKEND / "tools" / "executor.py"`, with a one-line comment that it runs
model-written commands (#1713) and is not in-process like the others. That
file then fails CI on any new spawn without a scrubbed `env=`, and on any
whole-`os.environ` copy (`test_no_environ_copy_outside_helper`). The
executor has no `os.environ` use today.

### 5. Tests

- New `tests/test_tool_executor_env.py`, modelled on
  `tests/test_agent_cli_env.py`: stub `security.hooks.bash_security_hook` to
  allow, replace `asyncio.create_subprocess_shell` and `_exec` with a fake
  that records `env` and raises, then drive `ToolExecutor.execute("Bash", ...)`
  and `execute("Grep", ...)`. Secrets are set after the executor is built.
  Assert absent: `APP_API_TOKEN`, `app_api_token`, `OLLAMA_API_KEY`,
  `AIFACTORY_TRUSTED_CONTRACT`, `GITHUB_TOKEN`, `NPM_TOKEN`. Assert present:
  `HOME`, `PATH`, `OLLAMA_BASE_URL`, and a `core.hooksPath` pin.
- `tests/test_agent_env_scrub.py`: lowercase and mixed-case denied names
  (`app_api_token`, `Ollama_Api_Key`) are blanked; `OLLAMA_API_KEY` is
  blanked; every `_AGENT_ENV_DENY_EXACT` entry is upper case.
- `tests/test_child_process_env.py:372`: the assertion that the runner keeps
  `OLLAMA_API_KEY` still holds (now via `RUNNER_KEEP`); fix the comment
  "undenied credential-shaped name" to say it is kept by `RUNNER_KEEP`.

### 6. Follow-ups (not in this change)

Filed as new issues before the PR, per the approver decisions:

- Allowlist instead of deny list (#363 scope, with the #1680 MCP
  credential constraint).
- Guard test: blanks are applied before the MCP env merge.
- Guard test: no credential name on the passthrough lists.

## Alternatives rejected

- **Allowlist now (#363).** Changes every agent spawn and must handle
  operator MCP credentials with arbitrary names (#1680). Approver moved it to
  its own issue.
- **`child_env(runner=True)` or `child_env(keep=...)` in the executor.** Would
  let `*_KEY`/`*_TOKEN` names such as `NPM_TOKEN` through to model commands.
  Approver chose the strictest option; agents do not push, the server does.
- **Blanking via `get_agent_env_blanks()` merged over `os.environ`.** Leaves
  the non-denied `*_TOKEN` names and is a whole-environ copy the guard
  forbids. `child_env()` already exists for exactly this.
- **`case_sensitive=True` in web-server settings.** Breaks any deployment
  that sets `APP_*` in lower case. Approver chose case-folding only.
- **Removing `OLLAMA_API_KEY` from `_PASSTHROUGH_BUILD_ENV`.** Breaks Ollama
  builds in Jobs; the provider reads it in-process.
- **Case-folding the set at import (`{k.upper() for k in ...}`).** Same
  effect as an upper-case assertion test but hides a typo instead of
  failing on it.

## Risks

- **Model commands lose tokens.** `gh`, `git push`, `npm install` from a
  private registry, or any tool that needs a `*_TOKEN`/`*_KEY` in a tool-loop
  build will now fail with an auth error. Accepted by the approver. Affects
  only the four tool-loop runtimes, on any host or Job.
- **Git pins now apply to model git commands.** A model-run `git commit` in
  the tool loop skips the repo's own hooks (pre-commit) and uses `true` as
  editor. This matches how the server runs git since #1680; the Claude-SDK
  path is unchanged (it only gets blanks, not the pins).
- **Local runner and `OLLAMA_API_KEY`.** If the `RUNNER_KEEP` edit is missed,
  local (non-Job) Ollama Cloud builds lose the key.
  `tests/test_child_process_env.py:372` catches this.
- **Case-folding is stricter for blanks.** A lowercase `anthropic_api_key`
  is now blanked even with API-key auth opted in, because the opt-in check
  at `core/auth.py:191` is an exact match. Nothing reads the lowercase name,
  so nothing breaks.
- **Not covered:** a lowercase `anthropic_api_key_file` is not in the deny
  list or `_STRIP_VARS` and is still passed through. Latent, same as today.
- **Overlap.** #1746 (refuse Bash writes into git metadata) will also edit
  `_exec_bash`, but in the command validation, not the spawn call; a rebase
  conflict at most. #1741, #1745 and #1747 do not touch these lines.

## Verification

- `pytest tests/test_tool_executor_env.py tests/test_no_unscrubbed_spawn.py
  tests/test_agent_env_scrub.py tests/test_child_process_env.py
  tests/test_agent_cli_env.py tests/test_ollama_agentic.py
  tests/test_ollama_endpoint_resolution.py tests/test_build_backend_kubejob.py`
  passes.
- Guard proof: remove `env=` from one executor spawn locally and
  `tests/test_no_unscrubbed_spawn.py` fails naming
  `apps/backend/tools/executor.py:<line>`.
- Re-run the intent's probe: with `APP_API_TOKEN`, `OLLAMA_API_KEY` and
  `AIFACTORY_TRUSTED_CONTRACT` set, `ToolExecutor.execute("Bash",
  {"command": "echo $APP_API_TOKEN $OLLAMA_API_KEY $AIFACTORY_TRUSTED_CONTRACT"})`
  prints none of the values.
- `ruff check` and `ruff format --check` on the touched files.
- Runtime: one Ollama-routed build (local runner and Job) still reaches the
  provider and completes a tool-loop turn.
