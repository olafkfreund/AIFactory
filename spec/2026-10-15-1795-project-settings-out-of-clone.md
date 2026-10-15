---
status: approved
issue: 1795
intent: intent/2026-10-15-1795-project-settings-out-of-clone.md
---

# Spec: move project settings out of the agent-reachable project clone

Line numbers are from `origin/dev` at `7f7777a4`. This builds on #1790
(`apps/web-server/server/services/project_env.py`, branch
`fix/1745-aifactory-env-allowlist`), which is still an open PR.
**Implementation starts only after #1790 is merged into `dev`.**

## Decisions on the intent's open questions

Each decision was set by the maintainer. The reason is given with it.

1. **Storage: a table in the web-server database.** The web-server always
   has a database: Postgres, or the SQLite fallback
   (`server/database/engine.py:33-45`, `config.py:272`). One table works
   across replicas; a directory needs a shared volume. The table is added
   with the existing Alembic chain (head `0400a6b429b0`,
   `database/alembic/versions/20261009_0400a6b429b0_trusted_contracts.py`).
   **Secret handling deviates from "EncryptedString columns", see D4.**
2. **Migration: lazy import on the first read for each project.** If the
   table has no row for the project and `<project>/.aifactory/.env`
   exists, import only the keys the #1790 allowlist accepts (plus the MCP
   server list, see D5). Then rename the file to
   `.env.migrated-<YYYY-MM-DD>`. Never delete it. From then on the store
   always wins. A file that appears later is ignored for settings, with a
   warning that names its keys but no values. Reason: no startup job and
   no operator step. Each project moves the first time anything needs its
   settings.
3. **Backend CLI with no web-server keeps reading `.aifactory/.env`** as
   a local-only fallback. Only the MCP keys it already reads are used
   (`apps/backend/core/client.py:420-462`). Local mode is single-user and
   trusted, and the docs say so. Reason: the CLI has no database, and on
   one user's machine the agent runs with that user's rights anyway.
4. **Secrets are encrypted at rest in the database.** The plain-text copy
   stays in the renamed file. The settings UI tells the operator to
   delete it. User files are never deleted automatically.
5. **MCP config is delivered by the web-server.** The web-server reads it
   from the store and passes it to the agent process in an environment
   variable holding JSON. A process the web-server starts never reads
   `.aifactory/.env` for MCP config. This closes #1791 for the in-pod
   and Kubernetes paths.
6. **The three UI writers write the store, not the file:**
   `routes/projects.py`, `routes/context.py` and `routes/github.py`.

## Design

### D1. Table `project_settings`

New model in `server/database/models.py`, after `TrustedContract`
(`:1222-1251`), with a migration that revises `0400a6b429b0`:

| Column | Type | Notes |
| --- | --- | --- |
| `project_key` | `String(64)` primary key | sha256 hex of `os.path.abspath(project_path)`, as `spec_key_for_dir` does (`services/trusted_contract_store.py:35-37`). |
| `settings` | `Text`, not null | `seal(json.dumps(dict[str, str]))`. |
| `created_at`, `updated_at` | `DateTime`, server default now | as `TrustedContract`. |

The key is the project path, not the registry id. Every reader has the
path; most don't have the id (`pr_endgame`, `pr_review_service`,
`agent_spec_creation`). The PVC mount path is the same on every replica.

### D2. Secrets: the whole settings blob is sealed with `secret_field.seal`

The maintainer asked for `EncryptedString` on the secret columns. The
spec uses `crypto/secret_field.seal`/`unseal` on one `Text` column
instead, for this reason: `EncryptedString` calls `get_backend()` on
every write (`crypto/encrypted_string.py`), and the default Fernet
backend raises when `KMS_FERNET_KEY` is unset
(`crypto/kms/fernet.py:46-54`). Every laptop install without a key would
then fail to save a token. `seal()` (`crypto/secret_field.py:96-121`) is
the house answer to that case (#1276, #1290):

- it uses the same key material;
- with no KMS configured it stores plaintext and warns once;
- with a configured KMS that fails, it raises, so the save fails loudly
  and never writes plaintext.

Sealing the whole blob is simpler than picking out which keys are
secret. It also covers the values that hold secrets under a non-secret
name, such as HTTP headers inside `CUSTOM_MCP_SERVERS`. On read, a value
that can't be decrypted (`unseal` returns `""`) is treated as an empty
settings dict, and an error is logged.

### D3. Store module `server/services/project_settings_store.py`

The module follows `trusted_contract_store.py`: a session factory can be
passed in, and by default it is imported lazily.

- `STORE_KEYS`: a key is accepted iff `project_env.is_project_env_key(key)`
  or `key == "CUSTOM_MCP_SERVERS"`. The #1790 allowlist itself is not
  widened, so `CUSTOM_MCP_SERVERS` never reaches a subprocess env through
  `load_project_env`. It reaches the agent only through D5.
- `async get_settings(project_path) -> dict[str, str]`:
  - **Row exists:** return it. If `<project>/.aifactory/.env` also
    exists, log a warning once per process per project, naming the keys
    only.
  - **No row:** run the import (D4), insert the row (empty if nothing
    was imported), and return it. The empty row matters: it is what makes
    a file created later "reappeared" rather than "first import".
  - **Two replicas inserting at once:** an `IntegrityError` on insert
    rolls back and re-reads.
- `async put_settings(project_path, settings: dict[str, str]) -> None`:
  full replace (upsert). A key outside `STORE_KEYS` raises `ValueError`.
  That is a programming error in a writer, never user input.
- The snapshot for sync readers (D6): `cached_setting(project_path, key)
  -> str | None` reads an in-process dict. `get_settings` and
  `put_settings` update it.
  `async refresh_all()` loads the project registry and calls
  `get_settings` for each project. That also imports any project not yet
  migrated. A failure is logged and the old snapshot is kept.
- `async load_project_env(project_path, env)` replaces #1790's sync
  file loader with the same contract. It copies the stored keys that pass
  `is_project_env_key` into `env`, never overriding, so
  `CUSTOM_MCP_SERVERS` is never copied. It keeps the name, so #1790's
  call-site tests still describe it. It moves into the store module, or
  `project_env.py` imports from the store; the plan picks one.
- `async mcp_spawn_env(project_path) -> dict[str, str]` returns
  `{"AIFACTORY_PROJECT_MCP_CONFIG": json.dumps(mcp)}`, where `mcp` is
  the stored `CONTEXT7_ENABLED`, `PLAYWRIGHT_MCP_ENABLED`,
  `PUPPETEER_MCP_ENABLED`, `AGENT_MCP_*_(ADD|REMOVE)` and
  `CUSTOM_MCP_SERVERS` values, as strings.

### D4. Import (one time per project)

1. Read `<project>/.aifactory/.env` with #1790's line parser, keeping
   only `STORE_KEYS`. Log the skipped names.
2. **A git-tracked file is not imported:** if `git -C <project> ls-files
   -- .aifactory/.env` prints the path, log a warning and store an empty
   row. The file is left untouched. Content that came with a clone is
   not settings the operator chose. Renaming it would also dirty the
   worktree.
3. Insert the row, then rename the file to `.env.migrated-<UTC date>`.
   If that name exists, append `-1`, `-2` and so on, because `rename`
   would silently overwrite it on POSIX. A rename that fails because
   another replica already did it (`FileNotFoundError`) is ignored. Any
   other `OSError` is logged, and the row still wins.
4. Never log values.

### D5. MCP delivery to the agent process

- **Web-server startup** (`server/main.py`, the `lifespan` before
  `init_db()` at `:138`) sets
  `os.environ.setdefault("AIFACTORY_PROJECT_MCP_CONFIG", "{}")`. Every
  child the web-server starts gets this variable: the
  `make_subprocess_env()` and `child_env()` copies of `os.environ` keep
  it, because the name matches neither `_STRIP_VARS` nor
  `*_KEY|*_TOKEN` (`apps/backend/core/child_env.py:20-60`). So a
  backend process started by the web-server always has the variable and
  never falls back to the file, even on a launch path that doesn't set
  the project's config.
- **The launchers override it with the project's config:**
  - `agent_service._spawn_task_execution`, in-pod (`:1067-1082`);
  - `agent_spec_creation.start_spec_creation` (`:240-255`);
  - `pr_review_service.start_review` (`:275-276`);
  - the Kubernetes Job path (`build_backend.py:1266-1269`). A Job pod
    inherits none of the control-plane env, so the value is put into
    `extra_env` next to `spawn_env`.
- **Backend** `core/client.py:load_project_mcp_config`:
  - The variable is set: parse it as a JSON object of strings and apply
    the same per-key rules as today: the toggles, `AGENT_MCP_*`, and
    `CUSTOM_MCP_SERVERS` checked with `_validate_custom_mcp_server`.
    Invalid JSON logs a warning and returns `{}`, so no custom servers
    run.
  - The variable is unset: this is the CLI. Read the file as today.
  - The per-key logic is split into one helper, used by both paths.

### D6. Synchronous readers in `pr_endgame`

`_project_env` (`pr_endgame.py:121-137`) is reached from sync code that
runs in threads: the merger sweep via `asyncio.to_thread`
(`merger.py:654-661`, `routes/merger.py:95`) and `apply_path_risk_floor`.
There is no sync database driver: only `asyncpg` and `aiosqlite` are
installed. So `_project_env` reads `cached_setting()`, and the async
entry points refresh the snapshot first:

- `merger_loop`, before each `to_thread(sweep_once)`;
- `routes/merger.py`, both sweep routes;
- `completion_orchestration.run_terminal_completion` (`:81`);
- `routes/context.get_project_context` (`:80`), before
  `is_graphiti_enabled` at `:117`.

A missing entry returns `None`. The caller then falls back to the global
env, as it does today for a project with no file. Every flag defaults
OFF. `AIFACTORY_PATH_RISK_FLOOR_ENFORCE` is refused by the #1790
allowlist, so it is never stored. The path floor then follows the global
env only. That is a tightening: today a written file could turn it off.

### D7. Writers

- **`routes/projects.py:update_project_settings`** (`:890-970`): get
  the stored settings, apply the same mapping, then `put_settings`. The
  file read, `mkdir`, write and `chmod` are removed.
- **`routes/context.py:update_project_env`** (`:550-700`): the same.
  It also maps the MCP fields the frontend already sends and the route
  drops today (`ProjectEnvUpdate`, `:42-57`; frontend
  `shared/types/project.ts:363-379`, `components/AgentTools.tsx:674-829`):
  - `mcpServers.context7Enabled` → `CONTEXT7_ENABLED`;
  - `mcpServers.playwrightEnabled` → `PLAYWRIGHT_MCP_ENABLED`;
  - `agentMcpOverrides[agent].add|remove` →
    `AGENT_MCP_<agent>_ADD|REMOVE`, as comma lists. An agent name not
    matching `[A-Za-z0-9_]+` is refused;
  - `customMcpServers` → `CUSTOM_MCP_SERVERS` as JSON.

  Without this, MCP config could not be changed at all once hand-editing
  the file stops working.
- **`routes/context.py:get_project_env`** (`:416-520`): it reads the
  store instead of the file, and maps the MCP keys back to the same
  frontend fields. If `<project>/.aifactory/.env.migrated-*` exists, it
  adds `legacySettingsFile: "<file name>"`. `EnvConfigModal.tsx` shows a
  one-line notice: the file holds plain-text copies and can be deleted.
- **`routes/github.py:_persist_cli_token_to_project`** (`:100-140`)
  becomes `async`, uses `get_settings`/`put_settings`, and its two
  callers (`:535`, `:764`) `await` it.
- **`routes/mcp.py:6`**: the docstring stops pointing at `.aifactory/.env`.

### D8. Docs

- A short section in `docs/docs/configuration-reference.md`, and an
  update to the `.aifactory/.env` lines in
  `docs/docs/concepts/mcp-servers.md`. They cover where settings live now, the
  `.env.migrated-*` file, and that the CLI's file fallback is local-mode
  only, single-user and trusted.
- The frontend type comment in `project.ts:384` changes from
  `.aifactory/.env` to "project settings store".

## Alternatives rejected

- **`EncryptedString` columns:** it fails on keyless local installs. See
  D2.
- **A server-owned directory under `PROJECTS_DATA_DIR`:** multiple
  replicas would need a shared volume, and secrets would need their own
  sealing. The database already exists on every install.
- **Making `pr_endgame`'s flag readers async:** the chain runs through
  sync code in worker threads (`merger._decide`, `apply_path_risk_floor`),
  so each caller would have to be rewritten. There is also no sync
  database driver, and adding `psycopg` for five flags is not worth it.
- **Only a write guard on `.aifactory/.env`:** external-CLI providers
  have no write roots (#1747), and a cloned repo can track the file.
- **Deleting the file after import:** it is the user's file. A rename
  keeps a record, and the UI tells the operator.
- **An explicit migration command, or a startup scan:** the lazy import
  needs no operator step and no boot-time work. `refresh_all()` covers
  the projects that only the merger sweep touches.
- **Adding `AIFACTORY_PROJECT_MCP_CONFIG` to `child_env._STRIP_VARS`:**
  the same helper builds the web-server's own children. Stripping the
  variable there would drop the marker, and backend processes would fall
  back to the file.

## Risks

- **Agents can read the MCP config.** Agent tool shells inherit
  `AIFACTORY_PROJECT_MCP_CONFIG`, so an agent can read the MCP config,
  including custom-server headers. It can already read them from the
  file today. It cannot change the parent's environment, which is the
  write path this issue closes.
- **Stale snapshot on another replica.** Every sync read that matters is
  preceded by an async refresh in the same flow (D6). A sync read with no
  refresh before it returns the value from the last refresh in that
  process.
- **Project path moves.** The key is the path. A moved project has no
  row, its file is already renamed, so it starts empty. The operator
  re-saves its settings. This is rare, and it fails toward defaults.
- **Read-modify-write between two concurrent UI saves.** The last write
  wins, the same as the file today.
- **No KMS key.** The settings are stored in plain text in the database,
  with a warning, as with `claude-profiles.json`. The deprecation of
  plaintext passthrough in `secret_field.unseal` (1.5.0) affects this
  store the same way as the JSON stores.
- **Behaviour changes for operators:**
  - hand edits to `.aifactory/.env` stop working on web-server installs
    (logged);
  - a git-tracked `.aifactory/.env` is no longer used;
  - keys outside the allowlist are dropped at import (logged by name).
- **Hosts:** every web-server install (laptop SQLite, Kubernetes
  Postgres). The new migration must run on Kubernetes before the pods,
  through the Helm `alembic-upgrade` Job.

## Verification

- **Store unit tests** (`apps/web-server/tests/test_project_settings_store.py`,
  temp `aiosqlite` engine plus `Base.metadata.create_all`, as the other
  DB tests do):
  - import keeps only `STORE_KEYS`;
  - the file is renamed and its content kept;
  - a second read doesn't re-import;
  - a reappearing file is ignored and warned about by key name, with no
    values in the log;
  - a tracked file is not imported;
  - the rename doesn't overwrite an existing `.env.migrated-<date>`;
  - `put_settings` refuses a foreign key;
  - the stored column is not plaintext when a key is set.
- **Agent-write regression test** (fails on `dev`, passes after): a
  project whose row exists, plus an agent-written `.aifactory/.env` with
  `CUSTOM_MCP_SERVERS` and `AIFACTORY_AUTO_MERGE=true`. Then:
  - `mcp_spawn_env` and `load_project_env` return the stored values;
  - `pr_endgame.is_auto_merge_enabled` stays off.
- **Backend tests:**
  - `load_project_mcp_config` with the variable set ignores a file in
    `project_dir`;
  - invalid JSON gives `{}`;
  - with the variable unset, it reads the file (the CLI case).
- **Writer tests:**
  - `PATCH /settings`, `PATCH /env` (including the MCP fields) and the
    gh-token persist write the store and leave no `.aifactory/.env`
    behind;
  - `GET /env` reads back from the store and reports
    `legacySettingsFile`.
- **Existing tests updated, not weakened:**
  - `tests/test_pr_endgame.py:450-460` seeds the store instead of
    writing the file;
  - `tests/test_context_memory_status_enabled.py`;
  - #1790's `test_project_env*.py`.
- `alembic upgrade head` then `downgrade -1` on SQLite.
- `ruff check`, `ruff format --check`, the targeted pytest files, and the
  frontend typecheck for `EnvConfigModal.tsx`.
