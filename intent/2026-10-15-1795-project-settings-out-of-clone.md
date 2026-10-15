---
status: draft
issue: 1795
author: olafkfreund
---

# Intent: move project settings out of the agent-reachable project clone

Follow-up to #1745, #1790 and #1791. Line numbers are from `origin/dev`
at `7f7777a4`. #1790 is still an open PR, so its loader is not on `dev` yet.

## Problem

Per-project settings live in `<project>/.aifactory/.env`, inside the
project clone. Task worktrees live in the same directory, at
`<project>/.aifactory/worktrees/tasks/<spec>`
(`apps/backend/core/workspace/git_utils.py:231`,
`apps/backend/core/workspace_fetch.py:115`). An agent that can write
files near its worktree can therefore edit a file the factory later
trusts.

The file holds three kinds of value:

- **Behaviour:** auto-PR and auto-merge, the PR reviewer, memory backend,
  `USE_CLAUDE_MD` and similar.
- **MCP server selection:** `CUSTOM_MCP_SERVERS` and the per-agent
  `AGENT_MCP_<agent>_(ADD|REMOVE)` toggles. These choose which command an
  agent session starts (#1791).
- **Credentials and endpoints:** `GITHUB_TOKEN`, provider tokens,
  `OLLAMA_BASE_URL` and others. These are stored in plain text today.

**Writers** (all in the web-server, all plain-text writes into the clone):

- `PATCH /api/projects/{id}/settings`: `apps/web-server/server/routes/projects.py:890`,
  write at `:967`, `chmod 0600` at `:970`.
- `update_project_env`, the env settings UI: `apps/web-server/server/routes/context.py:550`,
  path at `:573`, write after `:697`.
- gh CLI token persist: `_persist_cli_token_to_project`
  (`apps/web-server/server/routes/github.py:100`, path at `:120`,
  `GITHUB_TOKEN` at `:131`), reached from `POST /persist-token` (`:757`)
  and `GET /auto-detect` (`:503`).

**Readers:**

- Subprocess env for task runs: `agent_service._spawn_task_execution`
  (`apps/web-server/server/services/agent_service.py:1068`).
- Subprocess env for spec creation:
  `apps/web-server/server/services/agent_spec_creation.py:241`.
- Subprocess env for PR review:
  `apps/web-server/server/services/pr_review_service.py:276`.
- These three sites are what #1790 moves onto one allowlisted loader,
  `services/project_env.py:load_project_env()`. #1790 limits which keys
  are copied. It does not change where they are read from.
- MCP config, read in-process by the backend:
  `load_project_mcp_config` (`apps/backend/core/client.py:397`, path at
  `:414`, called from `create_client` at `:655`). It runs inside the
  agent subprocess, from the project directory, not in the web-server.
- PR endgame settings: `_project_env`
  (`apps/web-server/server/services/pr_endgame.py:121`, used at `:144`
  and `:615`).
- The settings UI reads back through `get_project_env`
  (`apps/web-server/server/routes/context.py:416`, path at `:425`).
- The MCP route says force-enable and force-disable still go through the
  file (`apps/web-server/server/routes/mcp.py:6`).

**Why a write guard alone does not close this:** the `.git` write hook
(`apps/backend/security/hooks.py:261`, wired at
`apps/backend/core/client.py:1031`) covers Claude SDK agents only. The
external-CLI providers have no write roots and run their own tool loops:
codex, opencode, copilot and antigravity (`apps/backend/providers/`).
MCP servers with write tools aren't covered either. Both gaps are
tracked in #1747. A guard extended to `.aifactory/.env` would leave the
same gaps. A cloned repo that tracks `.aifactory/.env` also supplies
settings without any agent writing the file at all.

**Where the server already keeps its own state:** `PROJECTS_DATA_DIR`
(`apps/web-server/server/config.py:170`, defaulting to the data dir from
`apps/web-server/server/paths.py:30`). It holds `projects.json` and,
when `DATABASE_URL` is unset, the SQLite database
(`apps/web-server/server/config.py:272`). Production sets `DATABASE_URL`
to Postgres. Credential columns already have an encrypted-at-rest type,
`EncryptedString` (`apps/web-server/server/crypto/`, used at
`apps/web-server/server/database/models.py:455`).

**Deployment shapes:**

- **Laptop / local:** web-server plus SQLite, with projects anywhere on
  disk.
- **Backend CLI** (`apps/backend/cli/`): runs with no web-server and no
  database. It reads `.aifactory/.env` directly through
  `load_project_mcp_config`.
- **Kubernetes:** cloned workspaces are on a PVC under
  `PROJECT_WORKSPACE_ROOT` (`charts/aifactory/values.yaml:545-582`,
  `ReadWriteOnce`). The deployment allows `replicaCount` > 1 when rmux is
  off (`charts/aifactory/templates/deployment.yaml:12`), with an HPA of
  up to 5 (`charts/aifactory/values.yaml:194`). The chart notes that
  replicas > 1 need an RWX workspace volume. A server-owned directory has
  the same problem across replicas. The database does not.

## Proposed outcome

- Project settings are stored where no task worktree, agent process or
  cloned repo content can write them.
- The Settings UI reads and writes them there.
- Every reader listed above gets its values from that store, not from
  the clone:
  - the three subprocess launchers;
  - MCP config;
  - PR endgame;
  - the settings read-back.
- Existing `<project>/.aifactory/.env` files are imported once, and then
  no longer read for settings.
- An agent that edits or creates `<project>/.aifactory/.env` changes
  nothing the factory does on the next run.

## Affected users and systems

- `apps/web-server`:
  - the settings, context and github routes;
  - `agent_service`, `agent_spec_creation`, `pr_review_service` and
    `pr_endgame`;
  - the #1790 loader;
  - database models and an Alembic migration if the store is the DB.
- `apps/backend`: `core/client.py` MCP config loading. The agent
  subprocess would need its settings passed in, because it can no longer
  read them from the clone.
- `apps/frontend-web`: the env and MCP settings screens
  (`EnvConfigModal.tsx`, `project-settings/MCPServersTab.tsx`). Their API
  shape should not need to change.
- Operators:
  - local installs, which hand-edit `.aifactory/.env` today;
  - Kubernetes installs, single and multi-replica;
  - backend CLI users.

## Constraints

- **Must:** no agent-reachable path can change a setting the factory
  trusts. This includes the external-CLI providers that have no write
  guard.
- **Must:** credentials are not stored in plain text in any new location.
- **Must:** the migration is one-way and runs once. It must not delete a
  user's file without a record, and it must not log setting values. Key
  names only, as #1790 does.
- **Must:** it works with Postgres and with the SQLite default, and with
  more than one web-server replica.
- **Must not:** break the backend CLI, which has no web-server or
  database.
- **Must not:** widen #1790's allowlist. The loader's refusals still
  apply to whatever the new store returns.
- **Must not:** touch the backend `.env` (`apps/backend/.env`) or the
  global settings file in `PROJECTS_DATA_DIR`. They are outside the clone
  and out of scope.
- Lands after #1790, which it builds on.

## Open questions

1. **Storage.** Choose one:
   - **A DB table** keyed by project. It works across replicas, can use
     `EncryptedString` for credentials, and follows the existing
     migrations.
   - **A server-owned directory** under `PROJECTS_DATA_DIR`. It is
     simpler and works without a database, but replicas > 1 need a shared
     volume, and credentials would need their own encryption.
   - Or a split: the DB for the web-server, a directory for the CLI.
2. **Migration and cutover.**
   - When is each file imported: at startup, on first read for each
     project, or by an explicit command?
   - After import, is the file deleted, renamed, or left alone and
     ignored?
   - Is there a release with a warning where the file is still read if
     the store is empty? Or does reading stop in the release that adds
     the store?
   - If a file and a stored value differ after import, does the store
     always win? Is the difference reported?
3. **Backend CLI with no database.**
   - Where does `apps/backend/cli` get project settings: a server-owned
     user config directory, environment variables or flags?
   - Should the CLI keep reading `.aifactory/.env` as a documented
     local-only fallback, accepting that its agents can write it?
4. **Secrets at rest.** Tokens (`GITHUB_TOKEN`, provider tokens) are in
   this file today.
   - Do they move into the same store, encrypted with `EncryptedString`
     and the KMS backend?
   - Or are they split out to the existing credential storage, with only
     non-secret settings in the new store?
   - In both cases, what happens to the plain-text copies left in clones
     after the import?
5. **Delivery to the agent subprocess.** `load_project_mcp_config` runs
   inside the agent process. Is the MCP config passed in by the
   web-server (argument, env or a file outside the clone)? Or does the
   backend query the store itself?
