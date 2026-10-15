---
status: approved
issue: 1745
intent: intent/2026-10-15-1745-aifactory-env-allowlist.md
---

# Spec: `.aifactory/.env` may only set the keys the factory owns

## Design

New module `apps/web-server/server/services/project_env.py` with one public
function, `load_project_env(project_path: Path, env: dict[str, str]) -> None`.
It reads `<project_path>/.aifactory/.env` and parses it the way
`pr_review_service._load_env_file` does today: skip blanks and `#` lines,
split on the first `=`, strip the key, strip the value and its surrounding
quotes, and do not override a key already in `env`. A key is copied only if
`is_project_env_key(key)` is true. Every other key is skipped. One warning per
call lists the skipped key names, passed through `sanitize_log`. Values are
never logged. A read error logs a warning and loads nothing, as today.

`is_project_env_key(key)` is true when both of these hold:

1. `key` is in `PROJECT_ENV_KEYS` (exact, case-sensitive) or fully matches
   `^AGENT_MCP_[A-Za-z0-9_]+_(ADD|REMOVE)$`;
2. `key` does not match the refused pattern (case-insensitive):
   `LD_*`, `DYLD_*`, `NODE_OPTIONS`, `BASH_ENV`, `ENV`, `PYTHON*`, `GIT_*`,
   `PATH`, `HOME`, `SHELL`, `*_PROXY`. The one exception is the six exact
   `GIT_*` names below, which the factory writes. They are factory settings,
   not variables that git reads.

So a loader or exec key can never get in, even if a later edit adds one to
the allowlist by mistake.

The three call sites replace their own parsing with this call:

| Site | Today | After |
| --- | --- | --- |
| `pr_review_service.py:276` | `_load_env_file(project_path / ".aifactory" / ".env", env)` | `load_project_env(project_path, env)` (`_load_env_file` stays for the backend `.env` at `:273`) |
| `agent_service.py:1066-1082` | inline loop | `load_project_env(project_path, env)` |
| `agent_spec_creation.py:239-255` | inline loop | `load_project_env(project_path, env)` |

### Allowlist (`PROJECT_ENV_KEYS`) and source of each key

Settings writer `PATCH /api/projects/{id}/settings`, `routes/projects.py:918-960`:
`AI_FACTORY_MODEL`, `MEMORY_BACKEND`, `GRAPHITI_MCP_URL`, `MAIN_BRANCH`,
`GIT_PROVIDER`, `GIT_TOKEN`, `GIT_BASE_URL`, `GIT_ORG`, `GIT_PROJECT`,
`GIT_REPO`, `GITHUB_TOKEN`, `GITHUB_REPO`, `AIFACTORY_PR_REVIEWER`,
`GRAPHITI_ENABLED`, `USE_CLAUDE_MD`, `REMOTE_CONTROL_BY_DEFAULT`,
`DELEGATE_BY_DEFAULT`, `AIFACTORY_AUTO_PR`, `AIFACTORY_AUTO_MERGE`.
(`pr_endgame.py:121-141` reads the last three AIFACTORY keys and the PR reviewer.)

Env writer `PATCH /api/projects/{id}/env`, `routes/context.py:571-700`:
`GITHUB_TOKEN`, `GIT_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `GITHUB_REPO`,
`GIT_REPO`, `GIT_PROVIDER`, `GIT_BASE_URL`, `GIT_ORG`, `GIT_PROJECT`,
`GRAPHITI_ENABLED`, `ENABLE_FANCY_UI`, `GRAPHITI_EMBEDDER_PROVIDER`,
`OLLAMA_BASE_URL`, `OLLAMA_EMBEDDING_MODEL`, `OLLAMA_EMBEDDING_DIM`,
`OPENAI_API_KEY`, `VOYAGE_API_KEY`, `VOYAGE_EMBEDDING_MODEL`,
`GOOGLE_API_KEY`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `GRAPHITI_DATABASE`, `GRAPHITI_DB_PATH`.

Env reader `GET /api/projects/{id}/env`, `routes/context.py:423-540`: the
same keys plus `ANTHROPIC_AUTH_TOKEN` (read as a Claude auth source).

Token writer `routes/github.py:101-138`: `GITHUB_TOKEN`.

MCP toggles, `apps/backend/core/client.py:414-436`: `CONTEXT7_ENABLED`,
`PLAYWRIGHT_MCP_ENABLED`, `PUPPETEER_MCP_ENABLED`, plus the open-ended family
`AGENT_MCP_<agent>_ADD` / `AGENT_MCP_<agent>_REMOVE`
(`agents/tools_pkg/models.py:479-482`), allowed by the strict pattern above.
The backend reads these from the file itself. Copying them into the env is
harmless and keeps the current behaviour.

The six `GIT_*` exceptions are `GIT_PROVIDER`, `GIT_TOKEN`, `GIT_BASE_URL`,
`GIT_ORG`, `GIT_PROJECT` and `GIT_REPO`. git reads none of these names.

### Deliberately left out

- `CUSTOM_MCP_SERVERS`: the backend reads it from the file directly
  (`core/client.py:439`), not from the env. It describes commands to run, so
  it has no business in the env either.
- Every other `AIFACTORY_*` name (`AIFACTORY_EGRESS_POLICY`,
  `AIFACTORY_SANDBOX_*`, `AIFACTORY_ALLOW_API_KEY`,
  `AIFACTORY_EXTRA_ALLOWED_COMMANDS`, ...): these are deployment security
  knobs that only the operator may set. Only the three names the Settings UI
  writes are allowed.
- `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`: not written by any project
  writer. The base URL would redirect the CLI's credential.

Behaviour change: the two `agent_*` sites did not strip quotes before.
Writers never quote, so only hand-written quoted values differ, and they now
match what `pr_review_service` and `core/client.py` already do.

## Alternatives rejected

- **Denylist of dangerous keys**: open-ended, and a new loader variable
  would get through. The brief asks for an allowlist.
- **Prefix allowlist (`AIFACTORY_*`, `GIT_*`)**: lets in the security knobs
  and git's own variables.
- **Dropping the project `.env` from the env entirely**: breaks
  `USE_CLAUDE_MD`, the Graphiti settings and the git provider settings that
  the runners read from the environment.
- **Guarding the file against agent writes**: needed too, but tracked in
  #1746/#1747. A tracked file in a cloned repo would still bypass it.

## Risks

- An operator who hand-put an unlisted key into a project `.env` loses it in
  the child. The skip is logged by name, so it is visible.
- The allowlist must grow when a new project setting is added. A test pins
  the writers' keys, so a new writer key without an allowlist entry fails CI.

## Verification

- Unit tests for `project_env`: an allowed key plus loader, shell and exec keys
  gives only the allowed key; every refused name is refused even if added to
  the set; `AGENT_MCP_coder_ADD` is allowed and `AGENT_MCP_` with no suffix is
  not; existing env keys are not overridden; quotes are stripped; the warning
  names the skipped keys and contains no value.
- Writer-coverage test: every key in the writer mappings in
  `routes/projects.py` and `routes/context.py` passes `is_project_env_key`.
- Regression test per call site (`start_review`, `_spawn_task_execution`,
  `start_spec_creation`): with a project `.env` holding `USE_CLAUDE_MD=true`
  plus `LD_PRELOAD`, `NODE_OPTIONS` and `BASH_ENV`, the env handed to
  `asyncio.create_subprocess_exec` holds `USE_CLAUDE_MD` and none of the three
  others. It fails on `dev` and passes after the fix.
- `ruff check`, `ruff format --check` on touched files; the web-server pytest
  files for these services.
