---
status: approved
issue: 1745
spec: spec/2026-10-15-1745-aifactory-env-allowlist.md
---

# Plan: `.aifactory/.env` may only set the keys the factory owns

Self-contained. You do not need to open the intent or the spec.

**Problem.** Three web-server launchers copy every key from
`<project>/.aifactory/.env` into a child process's env (gh, git, the Claude
CLI), so loader, shell and exec variables and factory security knobs get
through. Agents can write that file.

**Decisions (approved in the spec).**

- One shared loader: new module `apps/web-server/server/services/project_env.py`.
  - `load_project_env(project_path: Path, env: dict[str, str]) -> None`.
  - It reads `project_path / ".aifactory" / ".env"`. A missing file is a no-op.
  - Parse each line: strip it; skip blank lines and `#` lines; skip a line with no `=`; split on the first `=`; strip the key; strip the value and its surrounding `"`/`'`. This is the same as `pr_review_service._load_env_file`.
  - Copy only when `is_project_env_key(key)` is true and the key is not already in `env` (never override).
  - Collect the skipped keys. If there are any, emit one `logger.warning` naming them (sorted, comma-joined, through `sanitize_log` from `factory_common.logsafe`) and the file path. Never log a value.
  - An `OSError`/`UnicodeDecodeError` on read logs a warning and loads nothing.
- `is_project_env_key(key: str) -> bool` is true iff
  `(key in PROJECT_ENV_KEYS or _AGENT_MCP_KEY.fullmatch(key))` and
  `(key in _FACTORY_GIT_KEYS or not _REFUSED_KEY.fullmatch(key))`.
  - `_AGENT_MCP_KEY = re.compile(r"AGENT_MCP_[A-Za-z0-9_]+_(?:ADD|REMOVE)")`
  - `_REFUSED_KEY = re.compile(r"LD_.*|DYLD_.*|NODE_OPTIONS|BASH_ENV|ENV|PYTHON.*|GIT_.*|PATH|HOME|SHELL|.*_PROXY", re.IGNORECASE)`
  - `_FACTORY_GIT_KEYS = frozenset({"GIT_PROVIDER", "GIT_TOKEN", "GIT_BASE_URL", "GIT_ORG", "GIT_PROJECT", "GIT_REPO"})`
- `PROJECT_ENV_KEYS` is a frozenset. Group it with a one-line comment per source:
  - `routes/projects.py` settings: AI_FACTORY_MODEL, MEMORY_BACKEND, GRAPHITI_MCP_URL, MAIN_BRANCH, GIT_PROVIDER, GIT_TOKEN, GIT_BASE_URL, GIT_ORG, GIT_PROJECT, GIT_REPO, GITHUB_TOKEN, GITHUB_REPO, AIFACTORY_PR_REVIEWER, GRAPHITI_ENABLED, USE_CLAUDE_MD, REMOTE_CONTROL_BY_DEFAULT, DELEGATE_BY_DEFAULT, AIFACTORY_AUTO_PR, AIFACTORY_AUTO_MERGE
  - `routes/context.py` env: CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_AUTH_TOKEN, ENABLE_FANCY_UI, GRAPHITI_EMBEDDER_PROVIDER, OLLAMA_BASE_URL, OLLAMA_EMBEDDING_MODEL, OLLAMA_EMBEDDING_DIM, OPENAI_API_KEY, VOYAGE_API_KEY, VOYAGE_EMBEDDING_MODEL, GOOGLE_API_KEY, AZURE_OPENAI_API_KEY, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_EMBEDDING_DEPLOYMENT, GRAPHITI_DATABASE, GRAPHITI_DB_PATH
  - `core/client.py` MCP toggles: CONTEXT7_ENABLED, PLAYWRIGHT_MCP_ENABLED, PUPPETEER_MCP_ENABLED
- Deliberately NOT allowed:
  - CUSTOM_MCP_SERVERS: read from the file directly, not from the env.
  - Any other AIFACTORY_* name: operator security knobs.
  - ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY.
- `pr_review_service._load_env_file` stays: it still loads the operator's
  backend `.env` at `:273`.

## Steps

1. **New module and unit tests.**
   - Create `apps/web-server/server/services/project_env.py` as above.
   - Create `apps/web-server/tests/test_project_env.py` with these tests:
     - (a) a file with `USE_CLAUDE_MD=true`, `LD_PRELOAD=x`, `NODE_OPTIONS=x` and `BASH_ENV=x` loads only `USE_CLAUDE_MD`;
     - (b) parametrized over `LD_PRELOAD, LD_LIBRARY_PATH, DYLD_INSERT_LIBRARIES, NODE_OPTIONS, BASH_ENV, ENV, PYTHONPATH, PYTHONSTARTUP, GIT_SSH_COMMAND, GIT_CONFIG_PARAMETERS, GIT_EXEC_PATH, PATH, HOME, SHELL, HTTPS_PROXY, http_proxy`: `is_project_env_key` is False, and still False after `monkeypatch.setattr(project_env, "PROJECT_ENV_KEYS", PROJECT_ENV_KEYS | {name})`;
     - (c) `AGENT_MCP_coder_ADD` and `AGENT_MCP_qa_reviewer_REMOVE` are allowed; `AGENT_MCP_`, `AGENT_MCP_coder` and `AGENT_MCP_coder_ADDX` are not;
     - (d) an existing env key is not overridden, and quotes are stripped;
     - (e) the `caplog` warning names `LD_PRELOAD` and does not contain the value (use a distinctive value such as `sentinel-value-1745`);
     - (f) writer coverage: for `server.routes.projects.update_project_settings` and `server.routes.context.update_project_env`, take `inspect.getsource(...)` and pull every `"[A-Z][A-Z0-9_]{2,}"` string literal; each one must pass `is_project_env_key` (unwrap a decorator with `inspect.unwrap` if needed);
     - (g) a missing file is a no-op.
   - Verify: `cd apps/web-server && ../backend/.venv/bin/python -m pytest tests/test_project_env.py -q`.
   - Traps: the decorators `@honest_status`/router may wrap the functions. If (f) collects a literal that is not an env key (e.g. a log string), filter it to literals used as mapping values or `existing[...]` subscripts rather than widening the allowlist. Do not add to the allowlist anything outside the decision list without updating this plan.

2. **Call-site regression tests (they fail before step 3).**
   - Create `apps/web-server/tests/test_project_env_call_sites.py` with one async test per site.
   - Each test writes `<tmp project>/.aifactory/.env` with `USE_CLAUDE_MD=true`, `LD_PRELOAD=x`, `NODE_OPTIONS=x` and `BASH_ENV=x`. It removes those four names from the process env with `monkeypatch.delenv(..., raising=False)`.
   - It then patches `asyncio.create_subprocess_exec` in the target module, i.e. `monkeypatch.setattr(<module>.asyncio, "create_subprocess_exec", fake)`. `fake` records `kwargs["env"]` and raises a sentinel exception, so nothing runs.
   - Each test calls the entry point and catches the sentinel (the function may also catch it and return; either way assert on the recorded env).
   - Assert `env["USE_CLAUDE_MD"] == "true"` and that none of the three refused keys is present.
   - Sites:
     - `PRReviewService.start_review` (`server/services/pr_review_service.py:207`): it needs `get_settings().BACKEND_PATH` pointing at a tmp dir that contains `runners/github/runner.py`. Patch `get_settings` in that module.
     - `AgentService._spawn_task_execution` (`server/services/agent_service.py:932`, spawn at `:1242`): stub `_write_skill_context` and `_resolve_claude_token_pooled` (return `(None, None, None)`), and any other helper that touches the network or registry before the spawn.
     - `AgentService.start_spec_creation` (`server/services/agent_spec_creation.py:66`): same stubs.
   - Build the service objects the way `tests/test_agent_service_admission.py` does.
   - Verify: run the file. The three tests FAIL now (refused keys present). Record that.
   - Traps: never spawn a real process. If a site cannot be reached without heavy mocking, stub more helpers (the shared loader is not wired yet, so patching it proves nothing). Do not change production code in this step.

3. **Wire the three sites to the shared loader.**
   - `pr_review_service.py:275-276`: replace `_load_env_file(project_path / ".aifactory" / ".env", env)` with `load_project_env(project_path, env)`, and add the import.
   - `agent_service.py:1066-1082` and `agent_spec_creation.py:239-255`: replace the whole `project_env_file` block (comment, `if exists`, `try/except`) with one comment line plus `load_project_env(project_path, env)`, and add the import.
   - Verify: `../backend/.venv/bin/python -m pytest tests/test_project_env.py tests/test_project_env_call_sites.py tests/test_agent_service_admission.py tests/test_pr_endgame.py -q` all pass.
   - Traps: keep the backend `.env` loaders as they are. Use the import style of each file: the neighbouring imports are relative, so `from .project_env import load_project_env` (deviation: the draft said the absolute `server.services` form). Do not reformat untouched code.

## Tests

From `apps/web-server`:
- `../backend/.venv/bin/python -m pytest tests/test_project_env.py tests/test_project_env_call_sites.py -q` passes.
- The existing agent-service, pr-review and pr-endgame tests still pass.

From the repo root, on the touched files:
- `ruff check`
- `ruff format --check`

## Rollback

Revert the PR. The three sites go back to copying every key, which
re-opens the injection.
