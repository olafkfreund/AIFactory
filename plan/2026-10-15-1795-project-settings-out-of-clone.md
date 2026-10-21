---
status: approved
issue: 1795
spec: spec/2026-10-15-1795-project-settings-out-of-clone.md
---

# Plan: move project settings out of the agent-reachable project clone

## Prerequisite

#1790 (`fix/1745-aifactory-env-allowlist`) must be merged into `dev`
first. Then rebase this branch onto `origin/dev`. #1790 adds
`apps/web-server/server/services/project_env.py`, which provides
`PROJECT_ENV_KEYS`, `is_project_env_key()` and the sync
`load_project_env(project_path, env)`. It also wires that loader into
`agent_service.py`, `agent_spec_creation.py` and `pr_review_service.py`.

Line numbers below are from `origin/dev` at `7f7777a4`, before #1790.
After the rebase, find each site by the symbol named, not by the number.

## Decisions carried over from the approved spec

- **Storage.** The settings go in a new table, `project_settings`, in
  the web-server DB (Postgres, or the SQLite fallback).
  - The primary key is `project_key`: the sha256 hex of
    `os.path.abspath(project_path)`.
  - The `settings` column is `Text`. It holds
    `secret_field.seal(json.dumps(dict[str, str]))`.
  - Not `EncryptedString`. That type raises when no KMS key is set.
    `seal()` writes plaintext with a warning when no KMS was chosen, and
    raises when a chosen KMS fails.
  - A sealed value that cannot be decrypted (`unseal()` returns `""`),
    or a row that is not a JSON object, raises
    `ProjectSettingsUnavailableError`. It is never treated as `{}`.
    (Deviation at review; spec D2.) A plaintext row is re-sealed on read
    once a KMS key works.
- **Accepted keys** (`STORE_KEYS`): `is_project_env_key(k)`, or
  `k == "CUSTOM_MCP_SERVERS"`. The #1790 allowlist is not widened.
- **Lazy import on the first read of each project.** If there is no row:
  - read `<project>/.aifactory/.env` and keep only `STORE_KEYS`; log the
    skipped names, never values. If the read fails, raise
    `ProjectSettingsUnavailableError`, store nothing and rename nothing;
  - if git tracks the file (`git -c core.fsmonitor=false -c
    core.hooksPath=/dev/null -C <project> ls-files -- .aifactory/.env`,
    run in `asyncio.to_thread`, prints it), do not import it and do not
    rename it; store an empty row and log a warning. Exit 128 (not a repo)
    means untracked. Any other failure raises, and the import waits;
  - otherwise, insert the row (an empty row if there was no file), then
    rename the file to `.env.migrated-<UTC YYYY-MM-DD>`. Add `-1`, `-2`
    and so on if that name is taken, so nothing is overwritten. Ignore
    `FileNotFoundError` on the rename. Log any other `OSError`. Never
    delete the file;
  - if another replica inserts the row first, the `IntegrityError` rolls
    back and the row is read again.
- **After import the store always wins.** If a row exists and
  `.aifactory/.env` exists too, log a warning once per process per
  project, with key names only. The file is ignored.
- **MCP delivery.**
  - At startup the web-server sets
    `os.environ.setdefault("AIFACTORY_PROJECT_MCP_CONFIG", "{}")`, so
    every child it starts carries the variable.
  - The launchers override it with the project's stored MCP keys, as a
    JSON object of strings.
  - The backend reads the variable when it is set. It falls back to the
    file only when the variable is unset, which is the CLI case.
  - Do not add the variable to `child_env._STRIP_VARS`. That would drop
    the marker from the web-server's own children.
- **Sync readers in `pr_endgame`.** They read an in-process snapshot,
  `cached_setting()`. Async entry points call `refresh_all()` first.
  There is no sync DB driver. A missing value returns `None`, which falls
  back to the global env, and every flag defaults OFF.
- **Writers.** The three writers write the store:
  - `PATCH /settings`;
  - `PATCH /env`, which now also maps the MCP fields the frontend
    already sends;
  - the gh-token persist.

  `GET /env` reads from the store. It also returns
  `legacySettingsFile` when a `.env.migrated-*` file exists. The modal
  shows a notice that the file holds plain-text copies and can be
  deleted.
- **Backend CLI.** With no web-server, it keeps reading `.aifactory/.env`
  for MCP config. This is local mode: one trusted user.

## Steps

Each step ends with its check, then a commit by the lane agent that
names the step: `feat(settings): step N ... (#1795)`.

1. **Model and migration.**
   - `apps/web-server/server/database/models.py`: after `TrustedContract`
     (`:1222-1251`), add `class ProjectSettings(Base)`, with
     `__tablename__ = "project_settings"`. Columns:
     - `project_key: String(64)` primary key;
     - `settings: Text` not null;
     - `created_at` and `updated_at`, the same as `TrustedContract`,
       including `onupdate=func.now()`.

     Give it a docstring with one line of why: settings live outside the
     agent-writable clone (#1795).
   - `apps/web-server/server/database/__init__.py:21,41`: export
     `ProjectSettings` next to `TrustedContract`.
   - New migration
     `apps/web-server/server/database/alembic/versions/20261015_<12hex>_project_settings.py`:
     - `down_revision = "0400a6b429b0"`, in the same shape as
       `20261009_0400a6b429b0_trusted_contracts.py`;
     - `upgrade` creates the table, and `downgrade` drops it;
     - its docstring says the migration is additive.

   → Verify:
   ```
   cd apps/web-server && DATABASE_URL=sqlite+aiosqlite:////tmp/ps.db \
     python -m alembic upgrade head && python -m alembic downgrade -1 && \
     python -m alembic upgrade head
   ```
   (Use a scratch path for the DB.)
   (Deviation: the README migration table stops at its third entry and no later migration updates it, so it is left alone.)

   Traps:
   - check the head first with `python -m alembic heads`. If another
     migration landed on `dev`, revise that one instead;
   - don't reformat `models.py`.

2. **Store module and its unit tests.** New file
   `apps/web-server/server/services/project_settings_store.py`, modelled
   on `services/trusted_contract_store.py`: it takes an optional
   `session_factory` and imports the default one lazily. Contents:
   - `ENV = "AIFACTORY_PROJECT_MCP_CONFIG"`;
   - `project_key(path)`;
   - `is_store_key(k)`;
   - `_parse_env_file(path) -> (dict, skipped_names)`, using the same
     line rules as #1790's loader: strip, skip `#` and lines without
     `=`, split on the first `=`, strip quotes;
   - `_is_tracked(project_path) -> bool`, which runs `git ls-files` with
     `child_env()` from `server.utils.subprocess_env`, a timeout of 10s,
     and `check=False`. Any error means "not tracked";
   - `_rename_legacy(env_file)`;
   - `async get_settings(project_path)`;
   - `async put_settings(project_path, settings)`, which raises
     `ValueError` on a key outside `STORE_KEYS`;
   - the snapshot `_SNAPSHOT: dict[str, dict[str, str]]`, with
     `cached_setting(project_path, key)` and `async refresh_all()`.
     `refresh_all()` calls `server.project_registry.load_projects()`,
     then `get_settings(Path(p["path"]))` for each project, and catches
     and logs each failure;
   - `async load_project_env(project_path, env)`: copy the stored keys
     that pass `is_project_env_key` into `env`, without overriding;
   - `async mcp_spawn_env(project_path) -> {ENV: json}`. The MCP keys are
     `CONTEXT7_ENABLED`, `PLAYWRIGHT_MCP_ENABLED`, `PUPPETEER_MCP_ENABLED`,
     `CUSTOM_MCP_SERVERS`, and the keys matching
     `AGENT_MCP_[A-Za-z0-9_]+_(ADD|REMOVE)`.

   Logs use `sanitize_log` and name keys only.

   New `apps/web-server/tests/test_project_settings_store.py`. It uses a
   temp `aiosqlite` engine, `Base.metadata.create_all` through
   `run_sync`, and `async_sessionmaker`, and passes the factory into the
   store. Cases:
   - the import keeps only `STORE_KEYS`;
   - the file is renamed, keeps its content, and the old name is gone;
   - a second `get_settings` does not import again;
   - a file that reappears is ignored, and the caplog has key names but
     no values;
   - a git-tracked file is not imported and not renamed (use a real
     `git init` in `tmp_path`);
   - an existing `.env.migrated-<date>` is not overwritten;
   - `put_settings` refuses `LD_PRELOAD`;
   - with `KMS_FERNET_KEY` set, the raw column does not contain the
     token text;
   - `load_project_env` never copies `CUSTOM_MCP_SERVERS` and never
     overrides a key already in the env;
   - `mcp_spawn_env` returns only MCP keys.

   → Verify:
   ```
   apps/backend/.venv/bin/pytest apps/web-server/tests/test_project_settings_store.py -q
   ```

   Traps:
   - reset `crypto.kms._INSTANCE` and `_SNAPSHOT` in a fixture;
   - when `encryption_is_required()` is false, `seal()` without a key
     returns plaintext;
   - `secret_field` is at `server.crypto.secret_field`;
   - no `git stash`.

3. **Launchers use the store, and the startup marker.**
   - `services/agent_service.py`, at #1790's `load_project_env(...)` in
     `_spawn_task_execution` (dev `:1067-1082`): replace it with
     `await project_settings_store.load_project_env(project_path, env)`.
     Then, after the backend `.env` load and the token, call
     `env.update(await project_settings_store.mcp_spawn_env(project_path))`.
     It must be an override, so it goes last, next to the existing
     `env.update(await spawn_env(spec_dir))` (dev `:1238-1240`).
   - `services/agent_spec_creation.py` (`start_spec_creation`, async,
     dev `:240-255`): the same two changes.
   - `services/pr_review_service.py` (`start_review`, async, dev
     `:275-276`): the same two changes.
   - `services/build_backend.py:1266-1269`: after
     `extra_env.update(await spawn_env(...))`, add
     `extra_env.update(await project_settings_store.mcp_spawn_env(project_path))`.
   - `services/project_env.py` (#1790): delete the sync
     `load_project_env`, which nothing calls any more. Keep
     `PROJECT_ENV_KEYS` and `is_project_env_key`. In #1790's
     `tests/test_project_env.py`, move the `load_project_env` cases into
     the store test file and drop them here. In
     `tests/test_project_env_call_sites.py`, seed the store instead of
     the file: patch `project_settings_store.get_settings` to return the
     dict. Assert that the child env still has only the allowed key, plus
     `AIFACTORY_PROJECT_MCP_CONFIG`.
   - `server/main.py`, in `lifespan` before `await init_db()` (`:138`):
     add `os.environ.setdefault(project_settings_store.ENV, "{}")`, with
     the comment `# #1795: children never fall back to .aifactory/.env`.

   → Verify:
   ```
   apps/backend/.venv/bin/pytest apps/web-server/tests/test_project_env.py \
     apps/web-server/tests/test_project_env_call_sites.py \
     apps/web-server/tests/test_project_settings_store.py -q
   ```
   Also run the agent-service and build-backend tests that #1790 listed:
   ```
   apps/backend/.venv/bin/pytest apps/web-server/tests -q -k "agent_service or build_backend or spec_creation or pr_review"
   ```

   Deviation (revised at the maintainer's review): when the store cannot be
   read, the module-level `load_project_env` and `mcp_spawn_env` raise
   `ProjectSettingsUnavailableError("project settings unavailable: database
   error")`, so the launch fails instead of starting with no settings. With
   no settings, a task would lose its tokens, `MAIN_BRANCH`, model and
   flags without any sign of it. They never fall back to the file. Tests
   with no DB stub `get_settings`.
   `tests/test_project_env_call_sites.py::test_store_failure_fails_the_launch`
   covers the rule.

   Traps:
   - grep for every caller of `load_project_env` first. After this step
     there must be no sync caller left;
   - the override order matters: MCP config last.

4. **Backend reads MCP config from the variable.**
   `apps/backend/core/client.py:397-470`:
   - Pull the per-key body (`:431-462`) out into
     `_apply_mcp_setting(config, key, value)`.
   - In `load_project_mcp_config`, first read
     `raw = os.environ.get("AIFACTORY_PROJECT_MCP_CONFIG")`.
     - **`raw is not None`:**
       - `json.loads`; if the result is not a dict, warn and return `{}`;
       - for each `k, v` with a `str` value, call `_apply_mcp_setting`;
       - return without touching the file.
     - **`raw is None`:** the existing file loop, now calling the
       helper.
   - Update the docstring to say the file is read only by the CLI.
   - The warning at `:459-461` logs the raw JSON value. Change it to log
     no value, because it can hold header secrets.

   New `tests/test_project_mcp_config_source.py` (repo-root `tests/`,
   which already puts `apps/backend` on `sys.path`). Cases:
   - **(a) the #1791 regression:** the variable is `"{}"` and
     `project_dir/.aifactory/.env` has `CUSTOM_MCP_SERVERS` with a valid
     `npx` server; expect `{}`;
   - **(b)** the variable holds `CONTEXT7_ENABLED=false` and one valid
     custom server; both are returned;
   - **(c)** invalid JSON gives `{}`;
   - **(d)** the variable is unset and the file is read (the CLI case).

   Prove that (a) fails on the old `client.py`: copy the dev version in
   for a moment, or use a detached scratch worktree. Never `git stash`.

   → Verify:
   ```
   apps/backend/.venv/bin/pytest tests/test_project_mcp_config_source.py -q
   ```
   Also run any existing tests that import `load_project_mcp_config`
   (find them with grep).

   Traps:
   - use `monkeypatch.setenv` and `monkeypatch.delenv`;
   - `_validate_custom_mcp_server` (`:255`) still applies.

5. **`pr_endgame` reads the snapshot, and the refresh points.**
   - `services/pr_endgame.py:121-137`: `_project_env(project_path, key)`
     returns `project_settings_store.cached_setting(project_path, key)`,
     or `None` when `project_path` is `None`. Update the docstring.
   - Call `await project_settings_store.refresh_all()` at:
     - `services/merger.py:merger_loop`, inside the `try`, before
       `await asyncio.to_thread(sweep_once)` (`:661`);
     - `routes/merger.py`, before both `asyncio.to_thread(sweep, ...)`
       calls (`report_merger` at `:95`, and `run_merger`);
     - `services/completion_orchestration.py:run_terminal_completion`
       (`:81`): one `get_settings(project_path)` call at the top of the
       body, wrapped so that a failure only logs. (Deviation at review:
       it loads its own project, not `refresh_all()`.)
     - `routes/context.py:get_project_context` (`:80`): use
       `await project_settings_store.get_settings(project_path)` before
       `is_graphiti_enabled(project_path)` at `:117`, which fills the
       snapshot for that project.
   - Tests:
     - `tests/test_pr_endgame.py:450-460` writes `.aifactory/.env`;
       seed the snapshot instead with
       `project_settings_store._SNAPSHOT[project_key(p)] = {...}`;
     - fix `tests/test_context_memory_status_enabled.py` the same way;
     - add the regression test `test_agent_written_env_file_is_ignored`
       to `tests/test_pr_endgame.py`: a stored row (snapshot) with no
       `AIFACTORY_AUTO_MERGE`, and a file with
       `AIFACTORY_AUTO_MERGE=true`. Expect
       `is_auto_merge_enabled(p) is False`.

   → Verify:
   ```
   apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame*.py \
     apps/web-server/tests/test_context_memory_status_enabled.py \
     apps/web-server/tests -q -k "merger or completion_orchestration"
   ```

   Addition (maintainer's review): `is_auto_merge_enabled(project_path)`
   returns `False` when the project's settings have never loaded in this
   process (`project_settings_store.is_loaded`). That happens when the DB has
   been down since start. Auto-merge is then held, not left to the defaults.
   The merge-gate, review-tier and merge-decision tests mark their projects
   as loaded.
   `tests/test_pr_endgame.py::test_auto_merge_held_when_settings_never_loaded`
   covers the rule.

   Traps:
   - `refresh_all` must never raise into the merger loop or the
     completion flow;
   - don't make the `pr_endgame` flag functions async (spec D6).

6. **Writers and read-back.**
   - `routes/projects.py:update_project_settings` (`:904-970`):
     - replace the file read with
       `existing = await project_settings_store.get_settings(project_path)`;
     - keep the mapping code (`:919-962`) as it is;
     - replace the mkdir, write and chmod (`:964-970`) with
       `await project_settings_store.put_settings(project_path, existing)`.
   - `routes/context.py:update_project_env` (`:550-700`):
     - the same swap at `:575-583` and `:690-697`;
     - extend `ProjectEnvUpdate` (`:42-57`) with
       `mcpServers: dict | None = None`,
       `agentMcpOverrides: dict | None = None` and
       `customMcpServers: list | None = None`;
     - map them to keys: `mcpServers.context7Enabled` →
       `CONTEXT7_ENABLED`, and `mcpServers.playwrightEnabled` →
       `PLAYWRIGHT_MCP_ENABLED` (as `"true"`/`"false"`);
     - for `agentMcpOverrides`: first delete every existing
       `AGENT_MCP_*` key, then for each agent set
       `AGENT_MCP_<agent>_ADD` and `_REMOVE` as comma-joined lists,
       skipping empty lists. Return
       `{"success": False, "error": ...}` if an agent name does not
       fullmatch `[A-Za-z0-9_]+`;
     - `customMcpServers` → `CUSTOM_MCP_SERVERS = json.dumps(list)`; an
       empty list deletes the key.
   - `routes/context.py:get_project_env` (`:416-520`):
     - iterate `(await get_settings(project_path)).items()` instead of
       the file lines, with the same key branches;
     - map the MCP keys back to `mcpServers`, `agentMcpOverrides` and
       `customMcpServers`, the inverse of the above;
     - set `legacySettingsFile` to the name of the newest
       `project_path/.aifactory/.env.migrated-*` file, if one exists.
   - `routes/github.py:_persist_cli_token_to_project` (`:100-140`):
     make it `async`, use `get_settings` / `put_settings`, and keep the
     `try/except` returning `False`. Add `await` at both callers (`:535`
     and `:764`; both are already in async routes).
   - `routes/mcp.py:1-9`: the docstring says force-enable and
     force-disable go through the project settings (Settings → MCP), not
     `.aifactory/.env`.
   - Tests, in a new `apps/web-server/tests/test_project_settings_writers.py`.
     Patch the store functions with an in-memory dict, or use the temp
     engine from step 2. Cases:
     - `PATCH /settings` with `autoMerge` stores `AIFACTORY_AUTO_MERGE`
       and creates no `.aifactory/.env`;
     - `PATCH /env` with `customMcpServers` and `agentMcpOverrides`
       round-trips through `GET /env`;
     - `GET /env` reports `legacySettingsFile`;
     - the gh-token persist stores `GITHUB_TOKEN`.

     Update #1790's writer-coverage test if it parses these files.

   → Verify:
   ```
   apps/backend/.venv/bin/pytest apps/web-server/tests/test_project_settings_writers.py \
     apps/web-server/tests/test_project_env.py apps/web-server/tests/test_settings_error_leak.py -q
   ```
   Also run the github route tests (`-k github`).

   Traps:
   - the routes use FastAPI `Path` as `Path` and `pathlib.Path` as
     `FilePath` in `context.py`;
   - do not log token values;
   - keep the projects.json `settings` update in both routes unchanged
     (it is out of scope).

7. **Frontend notice and docs.**
   (Deviation: `EnvConfigModal.tsx` is the global Claude-token modal and never
   loads the project env, so the notice goes on the project settings screen.)
   - `apps/frontend-web/src/components/settings/ProjectSettingsContent.tsx`
     (`ProjectSettingsContentInner`, next to the error banner): when
     `envConfig.legacySettingsFile` is set, show one warning line with the
     i18n key `settings:projectSettings.legacySettingsFile`, which takes the
     interpolation `{{file}}`. Add the key to the `en`, `fr` and `pt-BR`
     `settings.json` files.
   - `apps/frontend-web/src/shared/types/project.ts`: add
     `legacySettingsFile?: string` to `ProjectEnvConfig`, and point the
     `AGENT_MCP_*` comment at the project settings.
   - `docs/docs/configuration-reference.md` and
     `docs/docs/concepts/mcp-servers.md`: say that settings live in the
     web-server DB. Cover the one-time import, the `.env.migrated-*` file,
     and that a git-tracked file is not imported. Say that
     `.aifactory/.env` is read only by the backend CLI in local mode
     (single user, trusted). The MCP editor is the portal's **MCP** view
     (`AgentTools.tsx`).

   → Verify:
   ```
   cd apps/frontend-web && npm run typecheck
   ```
   Runs in CI; the worktree has no `node_modules`.

## Review changes (standing approval, recorded as deviations)

- The migration now revises `5d1c9e3a7b20` (#1794's `handback_receipts`),
  so Alembic keeps a single head.
- `ProjectSettingsUnavailable` is renamed `ProjectSettingsUnavailableError`
  (N818).
- Store reads fail closed: an unreadable legacy file, an undecryptable or
  corrupt row, and a failed tracked-file check all raise
  `ProjectSettingsUnavailableError`, and nothing is stored or renamed. See
  spec D2 and D4.
- The rollback script is `scripts/restore_project_env.py`. It restores with
  `os.link` and then unlinks, so a `.env` created after the check is never
  replaced. `rename()` would silently replace it.
- Re-sealing a plaintext row on read is a conditional `UPDATE ... WHERE
  settings = <raw read>`, so it never overwrites a save committed after the
  read (`test_reseal_does_not_overwrite_a_newer_save`).
- Imports that mix an aliased and a plain name from one package use
  `import server.x.y as alias`. The repo's `ruff.toml` (`combine-as-imports =
  false`) and the ratchet's `standards/ruff.toml` (`true`) disagree on the
  `from ... import` form.
- Runner check: the changelog runner (direct SDK), conflict resolution
  (in-process, `create_simple_client`) and the insights runner
  (`ClaudeSDKClient`) never loaded the project MCP config. Ideation and
  roadmap call `create_client`, but the web-server never starts them; only
  the CLI does. So no extra launcher needs `AIFACTORY_PROJECT_MCP_CONFIG`.

## Who does what

- Steps 2, 3, 4, 5 and 6 go to agy (`agy-implement`), one call per step,
  in the worktree.
- Step 1 (small and mechanical) and step 7 are done by the lane agent,
  unless they grow.
- The lane agent reads each diff against its step, runs the check, and
  commits.

## Tests (whole change)

```
apps/backend/.venv/bin/pytest \
  apps/web-server/tests/test_project_settings_store.py \
  apps/web-server/tests/test_project_settings_writers.py \
  apps/web-server/tests/test_project_env.py \
  apps/web-server/tests/test_project_env_call_sites.py \
  apps/web-server/tests/test_pr_endgame.py \
  apps/web-server/tests/test_context_memory_status_enabled.py \
  tests/test_project_mcp_config_source.py -q
ruff check <touched files> && ruff format --check <touched files>
```

Expected: all pass. `tests/test_project_mcp_config_source.py::(a)`
fails on dev's `client.py`.

## Rollback

- Revert the PR.
- The migration is additive, so `alembic downgrade -1` drops
  `project_settings`.
- Projects that were already imported have their file at
  `.aifactory/.env.migrated-<date>`. To restore the old behaviour, run
  `python scripts/restore_project_env.py`. It is a dry run by default;
  add `--apply`, and optionally list project paths. It renames the newest
  `.env.migrated-*` back to `.env` for every registered project, and never
  overwrites an existing `.env`. (Added at review, with
  `tests/test_restore_project_env.py`.)
- Values saved through the UI after the upgrade exist only in the table.
  Before the downgrade, copy them out with `get_settings`.
