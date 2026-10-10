---
status: approved
issue: 1674
spec: spec/2026-10-10-1674-agent-env-host-vars.md
---

# Plan: deny six unscrubbed host keys in the agent env

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1674`, branch
`fix/1674-agent-env-host-vars`. Every line number below was checked against
HEAD `d19f4305` (the spec approval commit). The spec was written against
`445e7dc7`; every cited line still matches except the keep-restore in
`child_env.py`, which is line 48, not 49.

## Approved decisions (self-contained summary)

1. **D1, exact names only.** Add six names to `_AGENT_ENV_DENY_EXACT` in
   `apps/backend/core/auth.py` as one group, with a comment that cites #1674:
   `APP_CFACTORY_READ_KEY`, `CONTEXT7_KEY`, `RAPIDAPI_KEY`,
   `LANGCHAIN_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, `S3_ACCESS_KEY`. Add no
   suffix or other pattern, and leave `_AGENT_ENV_DENY_PATTERN` unchanged
   (`SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|KMS|PASSPHRASE|TRUSTED_PLAN_KEY`).
   A pattern such as `_KEY$` would strip #1680 operator MCP credentials from
   the runner. None of the six names matches the pattern, so only the exact
   entries deny them.
2. **D2, S3.** Deny `S3_ACCESS_KEY` everywhere and leave `RUNNER_KEEP`
   unchanged. The in-pod runner loses it, just as it lost `S3_SECRET_KEY`
   after #1691. Add no S3 name to `RUNNER_KEEP`.
3. **D3, Context7.** Scrub only. Add no `env` block to
   `mcp_servers['context7']` (`client.py:855-859`) and do not wire in
   `CONTEXT7_API_KEY`. The Context7 MCP now runs without a key.
4. **D4, #363 allowlist.** Out of scope; do not reopen #363. File one new
   follow-up issue linking #363 and #1674 (content in Step 3).
5. **D5, kubejob.** Rely on `run.py`'s agent scrub inside the Job. Leave
   `build_backend._PASSTHROUGH_BUILD_ENV` unchanged: it still forwards
   `OPENAI_COMPATIBLE_API_KEY` (`build_backend.py:295`) and `S3_ACCESS_KEY`
   (`:318`) into the Job. File no kubejob issue; narrowing dispatch belongs
   to #1669, #1670 and #1677.
6. **D6, OpenAI-compatible runner exception.** It needs no code change.
   `OPENAI_COMPATIBLE_API_KEY` is in `RUNNER_KEEP`
   (`apps/web-server/server/utils/subprocess_env.py:35`). `child_env`
   (`apps/backend/core/child_env.py`) drops denied names in the comprehension
   at `:41-47` (deny filter at `:44`) and then restores keep names at `:48`.
   A new test assertion pins this.
7. **D7, no other code changes.** `is_denied_env_key`,
   `get_agent_env_blanks`, the merge sites `client.py:614` and
   `simple_client.py:89`, `_AGENT_ENV_KEEP`, `_STRIP_VARS`, the
   `AIFACTORY_ALLOW_API_KEY` opt-in, `child_env.py`, `subprocess_env.py` and
   `_externalize_secret_env` (`client.py:914`, applied after the blanks) all
   stay as they are. `child_env.py:24` `_CREDENTIAL_NAME` already drops all
   six for `runner=False` children.
8. **D8, rejected.** No agent-only scrub helper, no case-folding, no
   ordering or passthrough guard tests (these go to the follow-up issue), no
   change to the Codex or Antigravity agents (#1692).
9. **D9, PR text.** The PR must state the MCP risk and its workaround: an
   operator MCP server that reads `RAPIDAPI_KEY`, `LANGCHAIN_API_KEY` or
   `CONTEXT7_KEY` from the inherited env must declare it in the catalog
   `env`, which wins at `client.py:914`. The PR must not claim that in-pod
   gate packing is fixed, that #1692 is fixed, or that OpenAI-compatible or
   Ollama tool-loop phases are scrubbed.
10. **D10, verification.** See Tests.

Not touched, verified: `tests/test_openai_compat_litellm_routing.py` also
sets `OPENAI_COMPATIBLE_API_KEY` but does not go through the agent scrub, so
it needs no change. `tests/test_build_backend_kubejob.py:345-373` is a
verification anchor that must keep passing unchanged.

Handoff: three steps edit files (five files in total), so per the model split
this plan goes to one `coder` (Step 1 first, Steps 2 and 3 by `SendMessage`
to the same agent). A fresh `opus` agent reviews with only this plan path and
`git diff`.

Environment for every command: run from the worktree root with
`export PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`.

## Steps

1. **Red tests (tests only, no production code).**

   a. `tests/test_agent_env_scrub.py:48-49`: in the `SECRETS` dict, after
      `"AIFACTORY_TOKEN": "x",` (48) and before `}` (49), insert:

      ```python
          # Host keys that matched neither list (#1674)
          "APP_CFACTORY_READ_KEY": "x",
          "CONTEXT7_KEY": "x",
          "RAPIDAPI_KEY": "x",
          "LANGCHAIN_API_KEY": "x",
          "OPENAI_COMPATIBLE_API_KEY": "x",
          "S3_ACCESS_KEY": "x",
      ```

      No new test function: `test_all_secrets_are_blanked` (:73-77, the
      `client.py:614` path) and `test_simple_client_env_is_scrubbed`
      (:101-118, the `simple_client.py:89` path) iterate `SECRETS`.
      `test_empty_when_no_secrets` does not use `SECRETS`.

   b. `tests/test_child_process_env.py:42`: in the `secret_env` fixture
      (:36-45), after `monkeypatch.setenv("OPENAI_API_KEY", "oa")`, add
      `monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "oc")`. No test in the
      file compares the whole env, so the extra key is safe.

   c. `tests/test_child_process_env.py:89`: in
      `test_make_subprocess_env_keep_and_drop` (:87-92), after
      `assert env["OPENAI_API_KEY"] == "oa"`, add
      `assert env["OPENAI_COMPATIBLE_API_KEY"] == "oc"`. This pins D6: it
      passes before and after Step 2.

   d. `tests/test_child_process_env.py:207`: in
      `test_credential_names_dropped_for_tools_kept_for_runner` (:184-208),
      replace `assert runner["CONTEXT7_KEY"] == "c"` with:

      ```python
          # undenied credential-shaped name survives in the runner (#1680)
          assert runner["OLLAMA_API_KEY"] == "c"
          assert "CONTEXT7_KEY" not in runner  # #1674
          assert "S3_ACCESS_KEY" not in runner  # #1674
      ```

      Keep lines 187-204 (the tools half) and 206 unchanged. The setup loop
      at :187-193 already sets `CONTEXT7_KEY`, `S3_ACCESS_KEY` and
      `OLLAMA_API_KEY` to `"c"`.

   → verify by
   `python -m pytest tests/test_agent_env_scrub.py tests/test_child_process_env.py -q`.
   Expected red: `test_all_secrets_are_blanked`,
   `test_simple_client_env_is_scrubbed` (the six names) and
   `test_credential_names_dropped_for_tools_kept_for_runner` (`CONTEXT7_KEY`,
   `S3_ACCESS_KEY`). Expected green: `test_make_subprocess_env_keep_and_drop`.

   Traps:
   - Lint with the repo default config (CI's), not `standards/ruff.toml`:
     `ruff format --check tests` and `ruff check tests`. The formatter does
     not wrap comments and formats at 88, so keep every comment line at 88
     characters or fewer (hence the comment on its own line in d).
   - No new imports are needed.
   - Commit `test(security): pin agent scrub of six host keys (#1674)`. The
     scope must not contain `#`.
   - #1671 and #1673 also edit `tests/test_child_process_env.py`; expect a
     rebase conflict near :36-45 and :184-208.

2. **The deny list (the only production edit).**

   `apps/backend/core/auth.py:150-151`: in `_AGENT_ENV_DENY_EXACT`
   (:118-151), after `"GH_TOKEN",` (150) and before `}` (151), insert:

   ```python
       # Host keys that matched neither list (#1674). Their readers keep them: web
       # server (own env), runner via RUNNER_KEEP, kubejob run.py via Job spec.
       "APP_CFACTORY_READ_KEY",
       "CONTEXT7_KEY",
       "RAPIDAPI_KEY",
       "LANGCHAIN_API_KEY",
       "OPENAI_COMPATIBLE_API_KEY",
       "S3_ACCESS_KEY",
   ```

   Leave `_AGENT_ENV_DENY_PATTERN`, `RUNNER_KEEP`, `child_env.py`,
   `client.py`, `simple_client.py` and `build_backend.py` unchanged (D1, D2,
   D3, D5, D7).

   → verify by
   `python -m pytest tests/test_agent_env_scrub.py tests/test_child_process_env.py tests/test_build_backend_kubejob.py tests/test_mcp_secret_env_599.py tests/test_no_unscrubbed_spawn.py -q`
   (all pass, 100 tests), then `git stash push apps/backend/core/auth.py`,
   rerun and see Step 1's assertions fail, then `git stash pop`.

   Traps:
   - Lint with the default config: `ruff format --check apps/backend` and
     `ruff check apps/backend`. Each comment line must be 88 characters or
     fewer.
   - Strict ratchet with `auth.py` staged:
     `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py'`
     and
     `python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py'`.
   - The edit shifts `auth.py` by 8 lines. The autonomy matrix
     (`docs/docs/compliance/autonomy-matrix.md`, `.json`) does not cite
     `auth.py` at all, so nothing needs regenerating; still run
     `python scripts/gen_autonomy_matrix.py --check` (baseline prints
     `ok: tiers=10 overlay=12 val=8 paths=28 gates=3 controls=13`). If it
     fails, run it without `--check` and commit the output in this step. The
     `::_AGENT_ENV_DENY_EXACT` citation lives in
     `docs/docs/environment-reference.md` (Step 3a), by name, not line.
   - `tests/test_no_unscrubbed_spawn.py` needs nothing: no spawn is added.
   - Commit `fix(security): deny six host keys in the agent env (#1674)`.
   - #1671 also touches `child_env.py` and `RUNNER_KEEP`. This branch leaves
     both alone; if #1671 changes `RUNNER_KEEP`'s shape, rerun mutation M3.

3. **Docs, CHANGELOG, follow-up issue, PR.**

   a. `docs/docs/environment-reference.md:155-160` (the table under the
      "recognized / scrubbed credentials" paragraph that cites
      `core/auth.py::_AGENT_ENV_DENY_EXACT`): add one row:
      `| APP_CFACTORY_READ_KEY, CONTEXT7_KEY, RAPIDAPI_KEY, LANGCHAIN_API_KEY, OPENAI_COMPATIBLE_API_KEY, S3_ACCESS_KEY | Host keys scrubbed from agents (#1674). OPENAI_COMPATIBLE_API_KEY is restored for the runner via RUNNER_KEEP; S3_ACCESS_KEY is denied to agents and the in-pod runner (kubejob Jobs still receive both for run.py). |`
      (wrap each name in backticks like the existing rows).

   b. `CHANGELOG.md`, under `## [Unreleased]` → `### Security` (line 3),
      next to the #1680 entry, add a bullet: agent sessions no longer
      inherit `APP_CFACTORY_READ_KEY`, `CONTEXT7_KEY`, `RAPIDAPI_KEY`,
      `LANGCHAIN_API_KEY`, `OPENAI_COMPATIBLE_API_KEY` or `S3_ACCESS_KEY`
      (#1674). Also say: the Context7 MCP runs without a key; the runner
      keeps `OPENAI_COMPATIBLE_API_KEY`; kubejob still forwards both
      `OPENAI_COMPATIBLE_API_KEY` and `S3_ACCESS_KEY` to the Job, where
      `run.py` blanks them for the agent.

   c. Follow-up issue (D4), linking #363 and #1674, with this evidence:
      - the six names the deny list missed;
      - `OLLAMA_API_KEY` is forwarded into build Jobs and is not denied;
      - denial is case-sensitive while pydantic `env_prefix="APP_"`
        (`apps/backend/core/config.py:248`) is case-insensitive;
      - `tools/executor.py:313` runs Bash for the OpenAI-compatible and
        Ollama agentic providers with the unscrubbed runner env;
      - the allowlist question (#363);
      - deferred work: the blank-then-MCP ordering guard test, the
        passthrough credential guard test, and case-folding.
      File no kubejob issue (D5). Filing needs the user's go-ahead; the
      coder drafts the text and the session model files it.

   d. PR against `dev` (the repo default; `origin/HEAD -> origin/dev`): link `intent/`, `spec/` and `plan/`
      `2026-10-10-1674-agent-env-host-vars.md` and the follow-up issue. It
      must state the MCP risk and workaround from D9, and say which steps
      the coder did. It must not claim in-pod gate packing is fixed, #1692
      is fixed, or OpenAI-compatible / Ollama tool-loop phases are scrubbed.

   → verify by `python -m pytest tests -q` (same pass/skip counts as the
   baseline on main) and a read of the rendered table row.

   Traps:
   - Commit `docs(security): changelog and env reference for agent scrub (#1674)`.
   - Doc-only edits outside the autonomy matrix sources need no
     regeneration.
   - #1669/#1670 (`agent_kubejob.py`) and #1672 (`pr_endgame.py`) touch
     none of these files; narrowing dispatch is their work, not this PR's.
   - Do not push or open the PR without the user's go-ahead.

## Tests

No new test function or test file; three existing tests in two files are
extended (Step 1).

1. Spec verification suite:
   `python -m pytest tests/test_agent_env_scrub.py tests/test_child_process_env.py tests/test_build_backend_kubejob.py tests/test_mcp_secret_env_599.py tests/test_no_unscrubbed_spawn.py -q`
   → **100 passed** (5 + 16 + 74 + 3 + 2), the same count as baseline.
   `test_build_backend_kubejob.py:345-373` still asserts
   `OPENAI_COMPATIBLE_API_KEY` (:350) and `S3_ACCESS_KEY` (:373) reach the
   Job env.
2. Other deny-list consumers:
   `python -m pytest tests/test_sandbox_escape_corpus.py tests/test_tracing.py tests/test_job_tracing.py -q`
   → **47 passed, 2 skipped**.
3. Full root suite: `python -m pytest tests -q` → same pass/skip counts as
   baseline. Web server suite is optional (no test there touches these
   names): `cd apps/web-server && python -m pytest tests -q -o asyncio_mode=auto`.
4. One-liner, from `apps/backend`:
   `APP_CFACTORY_READ_KEY=x CONTEXT7_KEY=x RAPIDAPI_KEY=x LANGCHAIN_API_KEY=x OPENAI_COMPATIBLE_API_KEY=x S3_ACCESS_KEY=x python -c "from core.auth import get_agent_env_blanks; print(sorted(get_agent_env_blanks()))"`
   → all six names in the output.
5. Mutations (traced by hand, must be run and the result recorded here):

   | # | Mutation | Must fail |
   |---|---|---|
   | M1 | Revert the `auth.py` hunk (`git stash push apps/backend/core/auth.py`) | `test_all_secrets_are_blanked`, `test_simple_client_env_is_scrubbed`, `test_credential_names_dropped_for_tools_kept_for_runner` |
   | M2 | Delete one name (repeat for each of the six) from `_AGENT_ENV_DENY_EXACT` | the two scrub tests; for `CONTEXT7_KEY`/`S3_ACCESS_KEY` also the runner test |
   | M3 | With the change applied, delete `OPENAI_COMPATIBLE_API_KEY` from `RUNNER_KEEP` (`subprocess_env.py:35`) | `test_make_subprocess_env_keep_and_drop` (KeyError) |
   | M4 | Add `_KEY$` to `_AGENT_ENV_DENY_PATTERN` | `test_credential_names_dropped_for_tools_kept_for_runner` on the `OLLAMA_API_KEY` assertion. (Not `runner=False`: that fails first on `CLAUDE_CODE_OAUTH_TOKEN` at :206 and never reaches the new assertion.) |
   | M5 | Filter `build_job_env` through `is_denied_env_key` | `test_build_job_env_propagates_non_claude_provider_env` (first fails at :347 on the already-denied `OPENAI_API_KEY`, so :350 is not reached) and `test_build_job_env_propagates_artifact_store_s3_env` at :373. This guards the unchanged passthrough (D5), not the new names. |

   Undo each with `git stash pop` or `git checkout <file>`.
6. Lint as CI runs it: `ruff format --check apps/backend apps/web-server scripts tests`
   and `ruff check apps/backend apps/web-server scripts tests`.
7. `python scripts/gen_autonomy_matrix.py --check` → `ok`.
8. After deploy: from an agent Bash tool, in the pod and in a kubejob build,
   `env | grep -E 'APP_CFACTORY_READ_KEY|CONTEXT7_KEY|RAPIDAPI_KEY|LANGCHAIN_API_KEY|OPENAI_COMPATIBLE_API_KEY|S3_ACCESS_KEY'`
   → each absent or blank. Then confirm an OpenAI-compatible phase completes
   and the web server's `/search` still works.

## Rollback

No data, schema or config migration.

1. `git revert` the squash-merge commit on `dev` (it carries Steps 1-3
   together) and open the revert PR. Do not revert the `auth.py` hunk alone:
   Step 1's #1674 assertions would then fail in CI (M1). After the full
   revert, test suite 1 passes at 100.
2. Redeploy the previous image. Agents and the runner get the six keys back.
3. If only the runner breaks (an OpenAI-compatible phase fails), the targeted
   fix is to add the missing name to `RUNNER_KEEP` rather than revert the
   agent scrub. An operator MCP that lost a key declares it in its catalog
   `env` (wins at `client.py:914`).

## Deviations

- Step 2: the autonomy matrix does cite `core.auth` (the `pr_review_service`
  row, `:313` → `:321`), so the regenerated matrix files are in step 2's
  commit. M1 is covered by step 1's red run (same assertions fail without the
  `auth.py` hunk).
- Step 3: mutation M5 fails four tests, not the two listed: also
  `test_build_job_env_propagates_present_provider_env` and
  `test_manifest_carries_oauth_env_in_container_not_argv`.
