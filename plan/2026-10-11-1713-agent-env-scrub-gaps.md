---
status: draft
issue: 1713
spec: spec/2026-10-11-1713-agent-env-scrub-gaps.md
---

# Plan: close the agent env scrub gaps left after #1674

Paths are relative to the repo root. Line numbers were checked against the
branch at `d5a9c08b` (spec approved). Run every command from the worktree root
`/mnt/code/Source-home/GitHub/AIFactory-1713` unless a step says otherwise.
`V=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin` (the worktree
has no venv of its own).

## Approved decisions (carried from the spec)

1. **Tool-loop spawns get plain `child_env()`.** The two spawns in
   `apps/backend/tools/executor.py` (`_exec_bash`, `_exec_grep`) pass
   `env=child_env()`. No `keep=`, no `runner=True`: model-run commands lose every
   `*_KEY`/`*_TOKEN` name too (`GITHUB_TOKEN`, `GH_TOKEN`, `NPM_TOKEN`,
   `CLAUDE_CODE_OAUTH_TOKEN`). Build it inside each call, never cache it.
   `HOME`, `PATH`, `LANG`, `*_BASE_URL` and proxies stay. This covers all four
   tool-loop runtimes (`ollama`, `ollama-cloud`, `openai-compatible`,
   `github-models`). `_exec_write`/`_exec_edit` are not touched.
2. **Deny `OLLAMA_API_KEY`** in `_AGENT_ENV_DENY_EXACT` (`apps/backend/core/auth.py`),
   and **keep it for the local runner** by adding it to `RUNNER_KEEP`
   (`apps/web-server/server/utils/subprocess_env.py`). `_PASSTHROUGH_BUILD_ENV`
   in `apps/web-server/server/services/build_backend.py:300` is NOT changed (Jobs
   still get it; the provider reads it in-process).
3. **Case-insensitive exact match:** `is_denied_env_key` compares
   `name.upper()` against `_AGENT_ENV_DENY_EXACT`. Do NOT case-fold the set at
   import; instead a test asserts every entry is upper case. Do NOT set
   `case_sensitive=True` in `apps/web-server/server/config.py`.
4. **Executor joins the spawn guard:** add `tools/executor.py` to
   `_BACKEND_FILES` in `tests/test_no_unscrubbed_spawn.py`.
5. **Accepted consequences (approver, 2026-10-13):** model-run git in the tool
   loop skips repo hooks and uses `true` as editor (from `child_env()`'s git
   pins); a lowercase `anthropic_api_key` is blanked even when API-key auth is
   opted in. Model commands needing a token (`gh`, `git push`, private `npm
   install`) will fail with an auth error: accepted.
6. **Out of scope, filed as follow-up issues before the PR:** allowlist instead
   of deny list (#363 scope, with the #1680 MCP-credential constraint); a guard
   test that blanks are applied before the MCP env merge; a guard test that no
   credential name is on the passthrough lists.

Rejected, do not implement: allowlist now; `child_env(runner=True)` or
`keep=` in the executor; merging `get_agent_env_blanks()` over `os.environ`;
`case_sensitive=True`; removing `OLLAMA_API_KEY` from `_PASSTHROUGH_BUILD_ENV`;
`{k.upper() for k in ...}` at import.

## Steps

1. `apps/backend/tools/executor.py`:
   - `:34-36` (imports block, `from providers.types ...` to
     `from security.tool_input_validator ...`): add
     `from core.child_env import child_env` in sorted position (before
     `from providers.types import ToolResultBlock`).
   - `:316-321` `_exec_bash`, `asyncio.create_subprocess_shell(command, ...,
     cwd=str(self._working_dir),)`: add `env=child_env(),` after `cwd=`.
   - `:421-426` `_exec_grep`, `asyncio.create_subprocess_exec(*cmd, ...,
     cwd=str(self._working_dir),)`: add `env=child_env(),` after `cwd=`.

   → verify by `cd apps/backend && $V/python -c "import tools.executor"` (no
   circular import; already checked to work) and
   `grep -n "env=child_env()" apps/backend/tools/executor.py` shows 2 hits.
   Traps: do not touch `_exec_write`/`_exec_edit` (#1719) or the Bash security
   hook block above `:316` (#1746 edits that area; keep the diff to the spawn
   call). No `os.environ` use may be added to this file (step 4 makes the guard
   forbid it).

2. `apps/backend/core/auth.py`:
   - `:151-158` the `#1674` block of `_AGENT_ENV_DENY_EXACT` (set starts at
     `:118`): add `"OLLAMA_API_KEY",` after `"S3_ACCESS_KEY",` (`:158`). Its
     reader keeps it via `RUNNER_KEEP` (step 3) and the Job passthrough; the
     existing block comment at `:151-152` already says this, leave it.
   - `:174` in `is_denied_env_key`: change
     `return name in _AGENT_ENV_DENY_EXACT or bool(...)` to
     `return name.upper() in _AGENT_ENV_DENY_EXACT or bool(_AGENT_ENV_DENY_PATTERN.search(name))`.
     The pattern at `:164-168` is already `re.I`; leave it.

   → verify by
   `cd apps/backend && $V/python -c "from core.auth import is_denied_env_key as d; assert d('app_api_token') and d('Ollama_Api_Key') and d('OLLAMA_API_KEY') and not d('HOME')"`.
   Traps: do not change `_AGENT_ENV_KEEP` or the `ANTHROPIC_API_KEY` opt-in
   check at `:191` (exact match is intended, decision 5). Line length: ruff
   uses `standards/ruff.toml`; if the return line exceeds the limit, let
   `ruff format` wrap it.

3. `apps/web-server/server/utils/subprocess_env.py:47-55` `RUNNER_KEEP`: add
   `"OLLAMA_API_KEY",` after `"VOYAGE_API_KEY",` (`:54`).
   → verify by `$V/pytest tests/test_child_process_env.py -q` (the assertion at
   `:372` that the runner keeps `OLLAMA_API_KEY` must pass after step 2; it
   fails if this step is skipped).
   Traps: without this step, local (non-Job) Ollama Cloud builds lose the key.

4. `tests/test_no_unscrubbed_spawn.py:60-68` `_BACKEND_FILES`: add
   `_BACKEND / "tools" / "executor.py",` after `_BACKEND / "pfactory" / "tfactory_client.py",`
   with a one-line comment above it:
   `# Not in-process: runs model-written commands in the tool loop (#1713).`
   → verify by `$V/pytest tests/test_no_unscrubbed_spawn.py -q` passes. Guard
   proof (do locally, do not commit): delete `env=child_env(),` from one spawn
   in step 1, rerun, expect a failure naming `apps/backend/tools/executor.py:<line>`,
   then restore.
   Traps: none.

5. New file `tests/test_tool_executor_env.py`, modelled on
   `tests/test_agent_cli_env.py` (`_SpawnedError`, fake spawn that records
   `env` then raises). Content:
   - Module docstring: `"""Tool-loop Bash/Grep spawns get a scrubbed env (#1713)."""`
   - `from __future__ import annotations`; imports `asyncio`, `pytest`,
     `from pathlib import Path`, `from tools.executor import ToolExecutor`
     (`tests/conftest.py:92` already puts `apps/backend` on `sys.path`).
   - Fixture: `monkeypatch.setattr("security.hooks.bash_security_hook", <async fn returning {}>)`
     (the executor imports it inside `_exec_bash`, so patching the module
     attribute works). Build `ToolExecutor(tmp_path)` FIRST, then `setenv` the
     secrets, so the test proves the env is read at spawn time.
   - Secrets to set: `APP_API_TOKEN`, `app_api_token`, `OLLAMA_API_KEY`,
     `AIFACTORY_TRUSTED_CONTRACT`, `GITHUB_TOKEN`, `NPM_TOKEN`. Pass-through to
     set: `HOME`, `PATH`, `OLLAMA_BASE_URL`.
   - Fake: `async def _fake(*a, **kw): captured.update(kw["env"]); raise _SpawnedError`.
     Patch both `asyncio.create_subprocess_shell` and
     `asyncio.create_subprocess_exec` via
     `monkeypatch.setattr(tools.executor.asyncio, ...)`.
   - Parametrize over `("Bash", {"command": "echo hi"})` and
     `("Grep", {"pattern": "x"})`. `await executor.execute(name, inp)`.
     **`execute()` catches every exception and returns an error block** (see
     `executor.py:126-134`), so do NOT use `pytest.raises`; assert on
     `captured` instead, and first assert `captured` is non-empty (the fake
     was reached).
   - Assert each secret is absent; `HOME`, `PATH`, `OLLAMA_BASE_URL` present;
     some `GIT_CONFIG_KEY_<n>` has value `core.hooksPath`.
   → verify by `$V/pytest tests/test_tool_executor_env.py -q`: 2 passed. Also
   confirm it fails with step 1 reverted (stash the executor change).
   Traps: pytest runs `asyncio_mode = auto`, so plain `async def test_...`
   works with no marker. Grep needs `rg` or `grep` on `PATH`; the fake replaces
   the spawn, so `shutil.which("rg")` result does not matter, but set `PATH`
   to a real value (e.g. keep `os.environ["PATH"]`).

6. `tests/test_agent_env_scrub.py`:
   - `:50-55` `SECRETS` dict, `#1674` block: add `"OLLAMA_API_KEY": "x",`.
   - Add tests after `test_empty_when_no_secrets` (`:102-107`):
     - `test_lowercase_and_mixed_case_denied_names_are_blanked(clean_env, monkeypatch)`:
       `setenv("app_api_token", "x")`, `setenv("Ollama_Api_Key", "x")`; both in
       `get_agent_env_blanks()` with value `""`.
     - `test_deny_exact_entries_are_upper_case()`: import
       `_AGENT_ENV_DENY_EXACT` from `core.auth`; assert
       `all(k == k.upper() for k in _AGENT_ENV_DENY_EXACT)`.
   → verify by `$V/pytest tests/test_agent_env_scrub.py -q` passes.
   Traps: the existing `test_all_secrets_are_blanked` iterates `SECRETS`, so
   adding `OLLAMA_API_KEY` there is the "OLLAMA_API_KEY is blanked" test.

7. `tests/test_child_process_env.py:371`: change the comment
   `# undenied credential-shaped name survives in the runner (#1680)` to
   `# denied, but RUNNER_KEEP restores it for the runner (#1713)`. The
   assertion at `:372` stays.
   → verify by `$V/pytest tests/test_child_process_env.py -q` passes.
   Traps: none.

8. `CHANGELOG.md`, under `## [Unreleased]` → `### Security` (`:3`), add as the
   first bullet:
   ```
   - **Tool-loop Bash and Grep commands no longer see host secrets (#1713).**
     The Ollama, Ollama Cloud, OpenAI-compatible and GitHub Models runtimes now
     spawn model-written commands with `child_env()`: every denied secret and
     every `*_KEY`/`*_TOKEN` name is dropped, and git hooks are off. Commands
     that need a token (`gh`, `git push`, a private `npm install`) now fail
     with an auth error. `OLLAMA_API_KEY` is denied to agents (the local runner
     and Jobs still get it), and the deny list now matches names in any case.
   ```
   → verify by `head -15 CHANGELOG.md`.
   Traps: `standards/coding-standards.md` is vendored; do not edit it or any
   other file under `standards/`.

9. Session model only (not the coder), before opening the PR: file the three
   follow-up issues from decision 6 with `gh issue create`, each linking #1713,
   and list their numbers in the PR description.
   → verify by `gh issue list --search "1713 in:body" --state open`.
   Traps: none.

Commit after each step or group (steps 1-4 code, 5-7 tests, 8 changelog),
staging only the touched files, and run the ratchets on what is staged (see
Tests). Cite the step number in each commit message.

## Tests

From the worktree root, with `V` as above:

- `$V/pytest tests/test_tool_executor_env.py tests/test_no_unscrubbed_spawn.py tests/test_agent_env_scrub.py tests/test_child_process_env.py tests/test_agent_cli_env.py tests/test_ollama_agentic.py tests/test_ollama_endpoint_resolution.py tests/test_build_backend_kubejob.py -q`
  → all pass, 0 failures.
- Guard proof (step 4): removing `env=` from one executor spawn makes
  `tests/test_no_unscrubbed_spawn.py` fail naming `apps/backend/tools/executor.py:<line>`.
- Probe: with `APP_API_TOKEN`, `OLLAMA_API_KEY` and `AIFACTORY_TRUSTED_CONTRACT`
  exported, from `apps/backend`:
  `$V/python -c "import asyncio; from pathlib import Path; from tools.executor import ToolExecutor; print(asyncio.run(ToolExecutor(Path('.')).execute('Bash', {'command': 'echo [\$APP_API_TOKEN][\$OLLAMA_API_KEY][\$AIFACTORY_TRUSTED_CONTRACT]'})).content)"`
  → prints `[][][]` (or a security-hook refusal, in which case rely on
  `tests/test_tool_executor_env.py`).
- Lint, with the touched files staged (`git add` them first):
  `$V/python scripts/cq_ratchet.py --tool ruff --staged --ruff $V/ruff --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  and
  `$V/python scripts/cq_ratchet.py --tool mypy --staged --mypy $V/mypy --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
  → exit 0. Plus `$V/ruff format --check <touched .py files>` and
  `$V/ruff check --config standards/ruff.toml tests/test_tool_executor_env.py` → clean.
- Autonomy matrix: not affected (it is generated from `merge.merge_policy`,
  which this change does not touch). Confirm with
  `$V/python scripts/gen_autonomy_matrix.py --check` → `ok: ...`, exit 0. If it
  ever reports stale, regenerate with `$V/python scripts/gen_autonomy_matrix.py`
  and commit the outputs.
- Runtime (session model, after merge to a test host): one Ollama-routed build
  on the local runner and one as a Job each reach the provider and complete a
  tool-loop turn.

## Rollback

Revert the implementation commits (`git revert <sha>...`) and redeploy the
backend and web-server. No data, schema or config migration is involved. To
undo only one part: dropping the two `env=child_env()` arguments restores the
old tool-loop env (and step 4's guard entry must go with it, or the guard
fails); removing `OLLAMA_API_KEY` from both `_AGENT_ENV_DENY_EXACT` and
`RUNNER_KEEP` together restores the old key handling; reverting the
`name.upper()` change restores case-sensitive matching (then drop the two new
tests in `tests/test_agent_env_scrub.py`).
