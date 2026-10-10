---
status: draft
issue: 1692
spec: spec/2026-10-10-1692-non-claude-agent-env.md
---

# Plan: non-Claude agent CLIs must not inherit the runner's credentials

Worktree `/mnt/code/Source-home/GitHub/AIFactory-1692`, branch
`fix/1692-non-claude-agent-env`, base `cdc65ab0` (spec approved). The PR
targets `dev` and is typed `fix(security)`. All paths are repo-relative;
provider paths are under `apps/backend/`.

Every line cited by the spec (written at `13a5de36`) was re-read at
`cdc65ab0` and still matches. Only `core/contract_trust.py` and
`core/migration_mapper.py` changed in between. Two small shifts in
`providers/opencode_agentic.py` change nothing: `_strip_opencode_prefix` is
:171-182 and the class starts at :184 (spec says :186), and
`_build_subprocess_env` is defined at :298 (spec says :301). Lines :326, :327,
:406 and :413-420 match exactly.

## Approved decisions (self-contained summary)

**Problem in one line.** Six non-Claude agent CLI spawns pass the runner's
whole `os.environ` (or inherit it by omitting `env=`) to a child that runs
shell commands without approval, so every env-borne credential on the runner
(GH_TOKEN, GITHUB_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_API_KEY,
CONTEXT7_API_KEY, other vendors' keys) reaches the agent.

- **D1 Reuse `core.child_env.child_env(keep=..., extra=...)`** with
  `runner=False` (the default) at every site. No new helper, table,
  `agent_cli_env()` or second deny list. Do not edit `core/child_env.py` or
  `core/auth.py`. Build the env inside the spawn method so it reflects
  `os.environ` at spawn time.
  - `child_env` behaviour at HEAD (reference only): the deny list,
    `_STRIP_VARS` (:16) and the `*_KEY`/`*_TOKEN` regex (:24, :46) drop names;
    `keep` restores names and overrides every drop (:48); `extra` applies
    (:49-50); the hooks-off pin `core.hooksPath=/dev/null` is appended at index
    `GIT_CONFIG_COUNT` (:51-57).
- **D2 Scope:** all six spawn sites in one PR: `codex.py`, `codex_agentic.py`,
  `antigravity.py`, `antigravity_agentic.py`, `copilot_agentic.py`,
  `opencode_agentic.py`.
- **D3 Codex** (text-only and agentic): `keep=("OPENAI_API_KEY",
  "CODEX_API_KEY")`, no `extra`. Agentic Codex builds the env once per
  `__aenter__` (the MCP child lives for the session). `OPENAI_BASE_URL` passes
  through (no `_KEY`/`_TOKEN` suffix).
- **D4 Antigravity:** `_GEMINI_KEEP: tuple[str, ...] = ("GEMINI_API_KEY",
  "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")` defined once in
  `providers/antigravity.py`; `antigravity_agentic.py` imports it.
  `GOOGLE_APPLICATION_CREDENTIALS` must be in it: `core/auth.py:144` denies it
  and kubejob mounts Vertex ADC through it.
- **D5 Text-only `antigravity.py`:** `keep=_GEMINI_KEEP`, **no `extra`**, so
  no `GEMINI_CLI_TRUST_WORKSPACE`. Agentic antigravity keeps
  `extra={"GEMINI_CLI_TRUST_WORKSPACE": "true"}`, the local name `env` and the
  `env=env` kwarg (`tests/test_gemini_trust_workspace.py:22-29` greps for both).
- **D6 Copilot:** `keep=("COPILOT_GITHUB_TOKEN",)`,
  `extra={"COPILOT_ALLOW_ALL": "true"}`. GH_TOKEN and GITHUB_TOKEN are dropped.
  Auth comes from the on-disk `~/.copilot` login (HOME and XDG survive) or an
  operator-set `COPILOT_GITHUB_TOKEN`. No GitHub identity name enters any keep
  list (no conflict with #1688 or #1671).
- **D7 OpenCode:** `env = child_env(keep=_opencode_keep(self._model))` with a
  new module-level function, verbatim from the spec:

  ```python
  def _opencode_keep(model: str) -> tuple[str, ...]:
      # ponytail: env name derived by convention (<PROVIDER>_API_KEY); a provider
      # with an unusual name (amazon-bedrock's AWS_*, google-vertex ADC, azure)
      # falls back to `opencode auth login`. Follow-up issue if one shows up.
      provider, sep, _ = model.partition("/")
      if not sep or not provider:
          return ()
      name = f"{provider.upper().replace('-', '_')}_API_KEY"
      if name.startswith("ANTHROPIC"):  # keep would restore it (child_env.py:48)
          return ()
      if provider == "google":
          return (name, "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY")
      return (name,)
  ```

  The Anthropic guard is required: `keep` overrides `_STRIP_VARS`, so without
  it `anthropic/*` would put ANTHROPIC_API_KEY back.
- **D8** `OPENCODE_DISABLE_AUTOUPDATE` stays `env.setdefault(...)` (not
  `extra=`), so an operator value wins. The XDG pre-warm in
  `_build_subprocess_env` is unchanged.
- **D9 Imports:** add `from core.child_env import child_env` to all six files.
  Remove `import os` from `antigravity_agentic.py` and `copilot_agentic.py`
  (the replaced line is its only use; ruff F401). `opencode_agentic.py` keeps
  `os` (used at :228). No cycle: `core.child_env` -> `core.auth` -> stdlib +
  `factory_common.logsafe`.
- **D10** Accept the GIT_CONFIG_* hooks-off pin. No variant without it. The PR
  says repository hooks do not run inside commits the agent makes.
- **D11** `tools/executor.py` tool-loop scrub (OpenAI-compatible and Ollama
  agents) is out of scope; follow-up issue. The PR must not claim it.
- **D12** Do not add the six files to `_BACKEND_FILES` in
  `tests/test_no_unscrubbed_spawn.py:59`.
- **D13/D14 Tests:** one new file `tests/test_agent_cli_env.py`, clean under
  `mypy --strict`. Do not edit `tests/test_child_process_env.py` (pending
  edits on #1671 and #1673). Assert on key membership only; never put env
  values in messages or logs (CodeQL).
- **D15** These stay green: `test_gemini_trust_workspace.py`,
  `test_opencode_provider.py` (:316-430 use only HOME/XDG_*; :440-469 needs the
  `setdefault`), `test_copilot_provider.py`, `test_codex_stderr_drain.py`,
  `test_outbound_scrub_all_providers.py`, `test_qa_providers.py`,
  `test_no_unscrubbed_spawn.py`. Baseline at HEAD: 195 passed.
- **D16 CI:** default ruff, strict ruff + mypy ratchet (zero new violations),
  CodeQL (no new logging), and `scripts/gen_autonomy_matrix.py --check`
  (`providers` is a model-client root, :462); regenerate and commit the two
  matrix files if it drifts.
- **D17** No Helm or Job-spec changes. No keys are added on any deployed path;
  credentials are only removed from the CLI children on both the subprocess
  and kubejob backends. The runner's own env is unchanged.
- **D18 Migration notes (PR body + CHANGELOG):** Copilot users relying on
  GH_TOKEN/GITHUB_TOKEN set COPILOT_GITHUB_TOKEN or use the on-disk login.
  OpenCode `anthropic/*` now needs `opencode auth login`. OpenCode providers
  whose key is not `<P>_API_KEY` (bedrock AWS_*, google-vertex, azure) fall
  back to on-disk auth. A Codex custom `env_key` (e.g. AZURE_OPENAI_API_KEY) is
  lost; use `codex login` or OPENAI_API_KEY. The OpenCode model prefix chooses
  which `*_API_KEY` survives. Git hooks are off in agent commits. The PR says
  it removes "env-borne credentials": HOME on-disk credentials and
  SSH_AUTH_SOCK remain.
- **D19 Follow-up issues:** (a) `tools/executor.py` scrub; (b) residual
  exposure via HOME on-disk credentials and SSH_AUTH_SOCK (spec Risk 7).

**Handoff.** 5 steps editing 8 files: goes to the `coder` agent. Start one
coder with this plan path and step 1; send steps 2-5 to the same agent with
SendMessage. Review with a fresh opus agent given only this plan and
`git diff`.

## Steps

1. `tests/test_agent_cli_env.py` (new file): write the failing tests →
   verify by `cd tests && python -m pytest -q test_agent_cli_env.py` being
   **RED** (all spawn rows fail; `_opencode_keep` rows fail with
   AttributeError), then
   `MYPYPATH=apps/backend mypy --strict --config-file standards/mypy.ini --follow-imports=silent tests/test_agent_cli_env.py`
   and `ruff format --check tests/test_agent_cli_env.py && ruff check tests/test_agent_cli_env.py`
   both clean.
   - Fake spawn: `class _Spawned(Exception)`. Every spawn site catches only
     `TimeoutError`, so it propagates. `async def _fake(*_a: object, **kw: object) -> NoReturn`
     records `kw.get("env")` and raises `_Spawned`. Patch with
     `monkeypatch.setattr(site.mod.asyncio, "create_subprocess_exec", _fake)`.
   - Patch `shutil.which` to return `"/fake/bin"`, and
     `get_antigravity_binary` on `providers.antigravity` and
     `providers.antigravity_agentic` (both import it by name; guard with
     `hasattr(site.mod, ...)`).
   - Delete `XDG_CACHE_HOME`, `GIT_CONFIG_COUNT`, `OPENCODE_DEFAULT_MODEL`.
     Construct the provider **first**, then `setenv` every secret
     (`_FOREIGN` + own keys + `COPILOT_GITHUB_TOKEN`) plus `OPENAI_BASE_URL`
     and `GOOGLE_CLOUD_PROJECT` to `"fake"`, and `HOME` to `tmp_path`. This
     folds the "set after construction appears at spawn" edge into every row.
   - Drive: `CodexAgenticProvider` with `await p.__aenter__()`; the others
     with `await p.query("x")` then `await p.receive_response().__anext__()`,
     inside `pytest.raises(_Spawned)`.
   - Rows (`_Site` frozen dataclass: mod, make, drive, own, flags, no_flags):
     `codex` (own = OPENAI/CODEX keys), `codex_agentic` (same),
     `antigravity` (own = `_GEMINI`, `no_flags=("GEMINI_CLI_TRUST_WORKSPACE",)`),
     `antigravity_agentic` (own = `_GEMINI`, flag GEMINI_CLI_TRUST_WORKSPACE),
     `copilot_agentic` (own = COPILOT_GITHUB_TOKEN, flag COPILOT_ALLOW_ALL),
     `opencode_openrouter` (`model="opencode:openrouter/x"`, own =
     OPENROUTER_API_KEY, flag OPENCODE_DISABLE_AUTOUPDATE),
     `opencode_anthropic` (`model="opencode:anthropic/x"`, own = (), same
     flag). The seventh row extends D13's six; it is the main Anthropic-guard
     regression check.
   - Each row asserts: every secret not in `own` is absent; `own + flags +
     ("HOME", "PATH", "OPENAI_BASE_URL", "GOOGLE_CLOUD_PROJECT")` present;
     `no_flags` absent; `"core.hooksPath"` is among the values of
     `GIT_CONFIG_KEY_*` (constant, not a secret).
   - `test_opencode_keep` parametrised: `anthropic/x` -> `()`,
     `anthropic-vertex/x` -> `()`, `google/x` -> `("GOOGLE_API_KEY",
     "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_API_KEY")`, `x-y/m` ->
     `("X_Y_API_KEY",)`, `openrouter/x` -> `("OPENROUTER_API_KEY",)`, `""`,
     `gpt-4o`, `/m` -> `()`.
   Traps: membership only; loop variable named `var` (not `key`/`token`),
   assert messages are constant names, no print/logging (CodeQL
   py/clear-text-logging-sensitive-data). `tests/pytest.ini` already sets
   `asyncio_mode = auto` and ignores DeprecationWarning (Antigravity sunset
   warning). `tests/conftest.py:92` puts `apps/backend` on `sys.path`. Use
   module imports (`from providers import codex`), no `Any` returns. Do NOT
   edit `tests/test_child_process_env.py` or `tests/test_no_unscrubbed_spawn.py`.
   The coder cannot use `git stash`/`checkout`/`restore`, so prove RED before
   any provider edit.

2. Four single-line providers → verify by
   `cd tests && python -m pytest -q test_agent_cli_env.py -k "not opencode"`
   GREEN, `python -m pytest -q test_gemini_trust_workspace.py test_copilot_provider.py test_codex_stderr_drain.py`
   GREEN, and `ruff check apps/backend/providers` clean.
   - `providers/codex.py:53-54`: add `from core.child_env import child_env`
     next to `from providers import BaseLLMProvider`.
     `:186-192`: add `env=child_env(keep=("OPENAI_API_KEY", "CODEX_API_KEY"))`
     to `create_subprocess_exec(...)` (no `env=` today).
   - `providers/codex_agentic.py:40-41`: add the import. `:240-245` inside
     `__aenter__` (:225): add the same `env=` to the
     `self._proc = await asyncio.create_subprocess_exec(...)` call.
   - `providers/antigravity.py:58-62`: add the import; after
     `logger = logging.getLogger(__name__)` (:62) define `_GEMINI_KEEP`.
     `:272-278`: add `env=child_env(keep=_GEMINI_KEEP)`, **no `extra`**.
   - `providers/antigravity_agentic.py:41`: delete `import os`. `:48-51`: add
     the import; `:50` becomes
     `from providers.antigravity import _GEMINI_KEEP, _emit_sunset_warning  # Issue #22`.
     `:197` becomes
     `env = child_env(keep=_GEMINI_KEEP, extra={"GEMINI_CLI_TRUST_WORKSPACE": "true"})`.
     Keep `env=env,` at :204 and the comment at :187-196.
   - `providers/copilot_agentic.py:40`: delete `import os`. `:47-48`: add the
     import. `:195` becomes
     `env = child_env(keep=("COPILOT_GITHUB_TOKEN",), extra={"COPILOT_ALLOW_ALL": "true"})`.
     Keep the :194 comment and `env=env` at :202.
   Traps: F401 if `import os` stays (verified: :197 and :195 are the only
   uses). `test_gemini_trust_workspace.py:22-29` greps source for
   `GEMINI_CLI_TRUST_WORKSPACE` and `env=env`. Ruff isort places
   `core.child_env` in the first-party block.

3. `providers/opencode_agentic.py` → verify by
   `cd tests && python -m pytest -q test_agent_cli_env.py test_opencode_provider.py`
   GREEN.
   - `:73-74`: add `from core.child_env import child_env`. Keep `import os`
     (:66, used at :228).
   - Between `_strip_opencode_prefix` (ends :182) and
     `class OpenCodeAgenticProvider` (:184): add `_opencode_keep` exactly as in
     D7, including the `ponytail:` comment.
   - `:326`: `env = os.environ.copy()` becomes
     `env = child_env(keep=_opencode_keep(self._model))`. `:327`
     `env.setdefault(_DISABLE_AUTOUPDATE_ENV_VAR, "1")` stays; XDG pre-warm
     unchanged. The spawn at :413-420 already passes `env=env` (built at :406).
   Traps: the Anthropic guard is required (keep overrides `_STRIP_VARS`).
   `self._model` already has `opencode:` stripped by the constructor. Two blank
   lines around the new top-level function (ruff format).

4. Full gates, no new edits unless the matrix drifts → verify by the commands
   in **Tests** (items 1-9).
   - If `python scripts/gen_autonomy_matrix.py --check` reports drift, run
     `python scripts/gen_autonomy_matrix.py`, re-run `--check`, and include
     `docs/docs/compliance/autonomy-matrix.md` and
     `docs/static/compliance/autonomy-matrix.json` in the commit.
   Traps: use the backend venv
   (`PATH=/mnt/code/Source-home/GitHub/AIFactory/apps/backend/.venv/bin:$PATH`;
   system python lacks `yaml`). `tests/test_security.py` GitCommitValidator
   fails while files are staged; environmental, ignore it and run the ratchet
   first. On a rebase, regenerate the matrix rather than hand-merging.

5. `CHANGELOG.md` under `## [Unreleased]`, commit, PR → verify by
   `git diff dev --stat` showing only the 6 providers,
   `tests/test_agent_cli_env.py`, `CHANGELOG.md` and (if drifted) the 2 matrix
   files.
   - CHANGELOG: a Security entry for #1692 with the D18 notes. On conflict
     with another in-flight PR, keep both entries.
   - Commit: `fix(security): scrub env-borne credentials from non-Claude agent CLIs (#1692)`
     with the Co-Authored-By and Claude-Session trailers.
   - PR to `dev`: links intent, spec and plan; says which steps the coder did;
     covers the six sites only and does NOT claim `tools/executor.py`; no Helm
     or Job-spec changes; states no keys are added on any deployed path
     (D17); includes the git-hooks-off note and the D18 migration notes.
   - Draft the two D19 follow-up issues (executor scrub; HOME on-disk
     credentials and SSH_AUTH_SOCK).
   Traps: commit scope may not contain `#`. Plan deviations go into this file
   in the same commit as the code.

## Tests

Run with the backend venv on PATH, from the worktree root unless noted.

1. `cd tests && pytest -q test_agent_cli_env.py` → **15 passed** (7 spawn rows
   + 8 keep rows).
2. `cd tests && pytest -q test_agent_cli_env.py test_gemini_trust_workspace.py test_opencode_provider.py test_copilot_provider.py test_codex_stderr_drain.py test_outbound_scrub_all_providers.py test_qa_providers.py test_no_unscrubbed_spawn.py`
   → **210 passed** (195 baseline + 15).
3. `ruff format --check apps/backend apps/web-server scripts tests && ruff check apps/backend apps/web-server scripts tests` → clean.
4. `ruff check --config standards/ruff.toml tests/test_agent_cli_env.py` → 0.
5. `MYPYPATH=apps/backend mypy --strict --config-file standards/mypy.ini --follow-imports=silent tests/test_agent_cli_env.py` → Success.
6. After `git add`:
   `python scripts/cq_ratchet.py --staged --ruff "$(command -v ruff)" --config standards/ruff.toml --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   and
   `python scripts/cq_ratchet.py --staged --tool mypy --mypy "$(command -v mypy)" --config standards/mypy.ini --paths 'apps/backend/*.py' 'apps/web-server/*.py' 'scripts/*.py'`
   → no per-file count rises.
7. `grep -n -A8 create_subprocess_exec apps/backend/providers/{codex,codex_agentic,antigravity,antigravity_agentic,copilot_agentic,opencode_agentic}.py | grep -c "env="` → 6.
8. `python scripts/gen_autonomy_matrix.py --check` → clean (or regenerated per step 4).
9. `git diff dev | grep -nE "^\+.*(logger\.|print\()"` → no new lines.

Mutation checks (edit, run item 1, edit back; `git diff <file>` must then
show only the planned change):

| # | Mutation | Expected failures |
|---|---|---|
| M1 | `codex.py`: drop the `env=` kwarg | `[codex]` |
| M2 | `codex_agentic.py`: `keep=("CODEX_API_KEY",)` | `[codex_agentic]` |
| M3 | `antigravity.py`: add `extra={"GEMINI_CLI_TRUST_WORKSPACE": "true"}` | `[antigravity]` |
| M4 | `_GEMINI_KEEP`: remove GOOGLE_APPLICATION_CREDENTIALS | both antigravity rows |
| M5 | `antigravity_agentic.py`: drop `extra=` | `[antigravity_agentic]` |
| M6 | `copilot_agentic.py`: add `"GH_TOKEN"` to keep | `[copilot_agentic]` |
| M7 | copilot: filter out `GIT_CONFIG_*` | `[copilot_agentic]` (hooksPath) |
| M8 | copilot: build env in `__init__` | `[copilot_agentic]` |
| M9 | opencode: `env = os.environ.copy()` | both opencode rows |
| M10 | opencode: delete the `setdefault` | both opencode rows + 1 in `test_opencode_provider.py` |
| M11 | `_opencode_keep`: delete the ANTHROPIC guard | `[opencode_anthropic]`, keep `anthropic/x`, keep `anthropic-vertex/x` |
| M12 | drop `.replace('-', '_')` | keep `x-y/m` |
| M13 | google returns `(name,)` | keep `google/x` |
| M14 | no-slash returns `(f"{model.upper()}_API_KEY",)` | keep `gpt-4o` |

## Rollback

- Before merge: edit the touched lines back; `git diff --stat` shows only the
  planned files.
- After merge: `git revert <merge-sha>` in one PR. No migration, no persisted
  or deploy state. The revert restores the `os.environ` pass-through at each
  site and, if regenerated, the matrix docs; confirm with
  `python scripts/gen_autonomy_matrix.py --check`.
