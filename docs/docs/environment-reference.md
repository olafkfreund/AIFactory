---
title: Environment Variable Reference
sidebar_position: 5
---

# Environment Variable Reference

The complete, exhaustive list of every environment variable AIFactory reads —
required vars, config, feature flags, tuning knobs, and credentials. If a fresh
install "works here but breaks there", the cause is almost always a variable
set in one environment and missing in the other. This page is the single source
of truth for finding them.

For a shorter, opinionated tour of the feature flags that matter most, see the
[Configuration Reference](./configuration-reference). This page is the full
enumeration; that page is the highlights.

## How variables are read

AIFactory reads configuration from four distinct surfaces:

| Surface | Naming | Where it lives | Read by |
|---------|--------|----------------|---------|
| **Pydantic `Settings`** | `APP_`-prefixed | `apps/web-server/.env` or pod/Helm env | `apps/web-server/server/config.py` |
| **Direct `os.environ`** | unprefixed (e.g. `AIFACTORY_*`, provider keys) | process env, `apps/backend/.env`, `apps/web-server/.env` (loaded into `os.environ` at startup by `server/env_bootstrap.py`), or Helm env | modules across `apps/backend/` and `apps/web-server/server/` |
| **Per-project `.aifactory/.env`** | `AIFACTORY_*` / `DELEGATE_*` | the target repo being built | PR-endgame + deploy flags; override the host per project |
| **Frontend build env** | `VITE_`-prefixed | `apps/frontend-web` build (Vite) | baked into the static bundle at build time |

A field named `FOO` on the pydantic `Settings` class is set with `APP_FOO`.
Some variables (e.g. `DATABASE_URL`, `DEBUG`) are read **both** as an `APP_`
pydantic field and directly from `os.environ` elsewhere — set the unprefixed
form and, for the pydantic field, the `APP_`-prefixed form; both are noted below.

Booleans are truthy on `1` / `true` / `yes` / `on` (case-insensitive) unless
noted. Empty / unset means off / default.

:::note Authoritative source
Defaults and behaviour are verified against the code at time of writing. The
code is always the final authority — file paths are given for every variable so
you can confirm.
:::

## Control plane (web server)

Set via `APP_`-prefixed env (pydantic `Settings`). All read in
`apps/web-server/server/config.py` unless noted.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `APP_HOST` | `0.0.0.0` | no | Bind address for the web server. |
| `APP_PORT` | `3101` | no | Listen port. |
| `APP_DEBUG` | `false` | no (on=debug) | Debug mode; also relaxes the `APP_DISABLE_AUTH` / wildcard-CORS boot guards to warn instead of refuse. |
| `APP_API_TOKEN` | auto-generated | no | Admin bearer token. Auto-generated to `~/.aifactory/.token` (0600) on first run if unset. Also read directly as `APP_API_TOKEN` by intake/handoff clients. |
| `APP_DISABLE_AUTH` | `false` | no (on=bypass) | Bypass auth — every request treated as admin. Local dev only; the server refuses to bind a non-loopback host with this on unless `APP_DEBUG`. |
| `APP_JWT_SECRET` | auto-generated | no | HMAC secret for JWTs. Persisted to `~/.aifactory/.jwt_secret` if unset so tokens survive restarts. |
| `APP_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | no | Access-token lifetime. |
| `APP_JWT_REFRESH_TOKEN_EXPIRE_DAYS` | `7` | no | Refresh-token lifetime. |
| `APP_JWT_ALGORITHM` | `HS256` | no | JWT signing algorithm. |
| `APP_SSL_ENABLED` | `false` | no | Serve HTTPS; generates a self-signed cert if no paths given. |
| `APP_SSL_CERTFILE` | `""` | no | Path to SSL certificate (with `APP_SSL_ENABLED`). |
| `APP_SSL_KEYFILE` | `""` | no | Path to SSL private key (with `APP_SSL_ENABLED`). |
| `DATABASE_URL` / `APP_DATABASE_URL` | `sqlite+aiosqlite:///<data>/data.db` | no | Primary database URL. Set Postgres here for multi-replica. Read directly in `server/database/engine.py`, `database/alembic/env.py`, `services/tenant_teardown.py`, `services/job_state_store.py`, `crypto/__main__.py`. |
| `APP_MIGRATIONS_AUTO_APPLY` | `true` | no | On boot run `alembic upgrade head`. Set false in K8s where a Helm Job migrates out-of-band. |
| `APP_PROJECTS_DATA_DIR` / `APP_PROJECTS_DATA_DIR` | data dir | no | Directory for project metadata + SQLite. Also read directly by `routes/copilot_mcp.py`. |
| `APP_BACKEND_PATH` | `../backend` | no | Path to `apps/backend`. Also the build-Job backend path (`build_backend.py`). |
| `APP_CORS_ORIGINS` | localhost:3000/3100/3101 | no | Allowed CORS origins (comma-list or JSON array). A wildcard `*` disables credentialed CORS (refuses boot unless `APP_DEBUG`). |
| `APP_DEFAULT_SHELL` | `/bin/bash` | no | Shell for the built-in terminal. |
| `APP_MAX_TERMINALS` | `20` | no | Max concurrent terminal sessions. |
| `APP_MAX_CONCURRENT_TASKS` | `5` | no | Global cap on concurrently running builds (RFC-0016 admission control); `<= 0` = unlimited. |
| `APP_AGENT_MONITOR_SYNC_INTERVAL` | `3.0` | no | Seconds between agent-monitor poll/sync ticks. |
| `APP_AGENT_MAX_CONTINUATION_ROUNDS` | `10` | no | Max auto-continue rounds for a finished-but-incomplete build. |
| `APP_REDIS_URL` | `""` | no | Redis for cross-replica WebSocket fan-out. Empty = in-process only (single replica). |
| `APP_REDIS_CHANNEL` | `aifactory:events` | no | Redis pub/sub channel (override for shared Redis). |
| `APP_WORKSPACE_S3_URI_BASE` | `""` | no | fsspec URI base (`s3://`, `gs://`, `azure://`) enabling workspace snapshot/restore. Empty = local PVC only. |
| `APP_CFACTORY_SEARCH_URL` | `http://cfactory.factory.svc.cluster.local:3111` | no | Cockpit base for federated command-palette (Cmd-K) search proxy (#149). |
| `APP_CFACTORY_READ_KEY` | `""` | no | Read-scoped cockpit key for federated search. Empty = feature off. |
| `APP_SKILLS_PATH` | (built-in) | no | Override path to the agent skills directory. Read in `services/skills_service.py`. |
| `APP_FILE_BROWSE_ROOTS` | `""` | no | Extra `os.pathsep`-separated roots the file browser may serve. Read in `routes/files.py`. |
| `APP_PROJECTS_DATA_DIR` | (data dir) | no | Copilot-MCP project data root fallback (`routes/copilot_mcp.py`). |

## Authentication & provider credentials

AIFactory is **OAuth-only by default** for Claude: any `ANTHROPIC_API_KEY` in
the environment is scrubbed from agents to prevent silent API billing. Read in
`apps/backend/core/auth.py`, `core/client.py`, `services/claude_token_pool.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `CLAUDE_CODE_OAUTH_TOKEN` | (none) | **yes** (Claude) | Claude Code OAuth token — the primary credential. Set via `claude setup-token`, the portal OAuth flow, or explicitly. Without it a build dies `No OAuth token found`. |
| `CLAUDE_CODE_OAUTH_TOKEN_POOL` | (none) | no | Newline/comma list of OAuth tokens for rotation/failover across replicas (`claude_token_pool.py`). |
| `ANTHROPIC_AUTH_TOKEN` | (none) | conditional | CCR/proxy bearer token for enterprise gateway setups. |
| `ANTHROPIC_BASE_URL` | Anthropic API | no | Override the Anthropic endpoint (local proxy / gateway / self-hosted). Read in `cli/utils.py`, `core/auth.py`. |
| `AIFACTORY_ALLOW_API_KEY` | off | no | Opt into direct `ANTHROPIC_API_KEY` auth (accepts the direct-billing path). Default OAuth-only. |
| `AIFACTORY_HEADLESS_PREFER_API_KEY` | off | no | In headless mode prefer an API key over OAuth when both present. |
| `ANTHROPIC_API_KEY` | (none) | conditional | Direct Anthropic key. Only honoured with `AIFACTORY_ALLOW_API_KEY`; also used by the Batch API helper (`core/batch.py`) and Graphiti. Otherwise scrubbed from agents. |
| `ANTHROPIC_MODEL` | (SDK default) | no | Model-name override passed through to the agent SDK. |
| `ANTHROPIC_DEFAULT_HAIKU_MODEL` | (SDK default) | no | Override the concrete model the `haiku` alias resolves to (`phase_config.py`). |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | (SDK default) | no | Override the `sonnet` alias model ID. |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` | (SDK default) | no | Override the `opus` / `opus-1m` alias model ID. |
| `NO_PROXY` | (none) | no | Standard no-proxy list; SDK passthrough (usually with `ANTHROPIC_BASE_URL`). |
| `DISABLE_TELEMETRY` | (none) | no | Disable Claude SDK telemetry; SDK passthrough. |
| `DISABLE_COST_WARNINGS` | (none) | no | Suppress SDK cost warnings; SDK passthrough. |
| `API_TIMEOUT_MS` | (SDK default) | no | Agent SDK request timeout (ms); SDK passthrough. |

### Non-Claude provider credentials

Resolved by phase routing / `byo_llm` and forwarded to build Jobs only when
present. Read mainly in `apps/backend/phase_config.py`, `providers/*`,
`integrations/graphiti/config.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `OPENAI_API_KEY` | (none) | conditional | OpenAI key (QA/planner routing, Graphiti, changelog fallback). |
| `OPENAI_MODEL` | `gpt-5-mini` (Graphiti) | no | OpenAI model name (Graphiti LLM / OpenAI-compatible). |
| `OPENAI_EMBEDDING_MODEL` | `text-embedding-3-small` | no | OpenAI embedding model (Graphiti). |
| `OPENAI_COMPATIBLE_API_KEY` | (none) | conditional | Key for a generic OpenAI-compatible endpoint. |
| `OPENAI_COMPATIBLE_BASE_URL` | (none) | conditional | Base URL for the OpenAI-compatible endpoint. |
| `OPENAI_COMPATIBLE_MAX_TOKENS` | (provider default) | no | Max-tokens override for the OpenAI-compatible agent (`providers/openai_compatible_agentic.py`). |
| `OPENAI_COMPATIBLE_REASONING_EFFORT` | (provider default) | no | Reasoning-effort override for the OpenAI-compatible agent. |
| `AZURE_OPENAI_API_KEY` | (none) | conditional | Azure OpenAI key (Graphiti). |
| `AZURE_OPENAI_BASE_URL` | (none) | conditional | Azure OpenAI endpoint. |
| `AZURE_OPENAI_LLM_DEPLOYMENT` | `""` | no | Azure OpenAI LLM deployment name. |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `""` | no | Azure OpenAI embedding deployment name. |
| `GEMINI_API_KEY` | (none) | conditional | Google Gemini key (phase routing, CLI accounts). |
| `GEMINI_CLI_TRUST_WORKSPACE` | (none) | no | Gemini CLI workspace-trust passthrough for build Jobs. |
| `GOOGLE_API_KEY` | (none) | conditional | Google AI key (Graphiti, phase routing). |
| `GOOGLE_LLM_MODEL` | `gemini-2.0-flash` | no | Google LLM model (Graphiti). |
| `GOOGLE_EMBEDDING_MODEL` | `text-embedding-004` | no | Google embedding model (Graphiti). |
| `OPENROUTER_API_KEY` | (none) | conditional | OpenRouter aggregator key (Graphiti). |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | no | OpenRouter base URL. |
| `OPENROUTER_LLM_MODEL` | `anthropic/claude-3.5-sonnet` | no | OpenRouter LLM model. |
| `OPENROUTER_EMBEDDING_MODEL` | `openai/text-embedding-3-small` | no | OpenRouter embedding model. |
| `VOYAGE_API_KEY` | (none) | conditional | Voyage AI embeddings key (Graphiti). |
| `VOYAGE_EMBEDDING_MODEL` | `voyage-3` | no | Voyage embedding model. |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | no | Ollama server URL (local/offline models). |
| `OLLAMA_API_URL` | (fallback of `OLLAMA_BASE_URL`) | no | Alternate Ollama URL var (`pfactory/tiers.py`). |
| `OLLAMA_API_KEY` | (none) | no | Ollama Cloud key; build-Job passthrough. |
| `OLLAMA_CLOUD_BASE_URL` | (none) | no | Ollama Cloud base URL; build-Job passthrough. |
| `OLLAMA_LLM_MODEL` | `""` | conditional | Ollama LLM model (required for Ollama LLM). |
| `OLLAMA_EMBEDDING_MODEL` | `""` | conditional | Ollama embedding model (required for Ollama embeddings). |
| `OLLAMA_EMBEDDING_DIM` | `0` | conditional | Ollama embedding dimension; must match the model. |
| `OPENCODE_DEFAULT_MODEL` | `""` | conditional | Model for the OpenCode agentic provider (`providers/opencode_agentic.py`). |
| `GH_TOKEN` / `GITHUB_TOKEN` | (none) | conditional | GitHub token for PR endgame, workspace fetch, testing (`providers/factory.py`, `runners/github/runner.py`). Required to open/merge PRs. |
| `GITHUB_BOT_TOKEN` | (none) | no | Separate bot identity for GitHub actions (`runners/github/runner.py`). |
| `GITHUB_REPO` | (none) | conditional | Target repo for the GitHub runner CLI. |

### Recognized / scrubbed credentials

These are recognized as secrets and **blanked from the agent environment**
(`core/auth.py::_AGENT_ENV_DENY_EXACT`) so a prompt-injected build can't
exfiltrate them. Set them on the control plane only if a component genuinely
needs them (cloud/KMS SDKs); they never reach agents.

| Variable | Purpose |
|----------|---------|
| `GROQ_API_KEY`, `TOGETHER_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `LINEAR_API_KEY` | Additional provider/integration keys; recognized and scrubbed. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` | AWS credentials (boto3 / workspace store / deploy); scrubbed from agents. |
| `GOOGLE_APPLICATION_CREDENTIALS` | GCP service-account path; scrubbed from agents. |
| `AZURE_CLIENT_SECRET` | Azure app secret; scrubbed from agents. |

Any host var whose name matches `SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|_KMS|PASSPHRASE`
is also blanked generically.

## LiteLLM gateway & audit

Read in `providers/_gateway.py`, `phase_config.py`, `core/enforcement.py`,
`services/litellm_admin_client.py`, `services/llm_audit_hook.py`,
`providers/openai_compatible.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `LITELLM_GATEWAY_URL` | `""` | conditional | Route model calls through a LiteLLM gateway. Empty = direct providers. |
| `LITELLM_API_KEY` | `""` | conditional | Auth key for the LiteLLM gateway. |
| `LITELLM_MASTER_KEY` | `""` | conditional | Master key for LiteLLM admin operations. |
| `LITELLM_MASTER_KEY_WRAPPED` | `""` | no | KMS-wrapped master key (base64) unwrapped at runtime. |
| `LITELLM_AUDIT_SCRUB_OUTBOUND` | on | no | Scrub high-precision PII (SSN, email, phone, Luhn-checked CC) from the prompt BEFORE it is sent to the LLM provider (#320). On by default; kill-switch — set `false`/`0`/`no`/`off` to disable and restore audit-row-only redaction. |
| `LITELLM_AUDIT_EXTRA_PATTERNS` | `""` | no | Extra comma-separated regex patterns for the audit scrubber. |

## Model, agent & routing tuning

Read across `apps/backend/` (`routing_policy.py`, `core/model_config.py`,
`agents/*`, `prompts_pkg/prompts.py`, `spec/pipeline/agent_runner.py`,
`solo_mode.py`, `integrations/bmad/session_config.py`, `core/enforcement.py`).

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_ROUTING_POLICY` | `""` | no | Named per-phase model routing policy (`routing_policy.py`). |
| `AIFACTORY_RUNTIMES` | `""` (only `claude`) | no (spend) | RFC-0014 operator allowlist of non-default runtimes — comma-separated (`codex`, `antigravity`, `ollama`, `ollama-cloud`), or `all`. `claude` is always on; the fan-out runtimes `claude-subagents` / `dynamic-workflow` multiply spend and must be named explicitly (never implied by `all`). A runtime also needs the contract's `execution.runtime` opt-in (`core/runtime_gating.py`). |
| `AIFACTORY_PROVIDER_FAILOVER` | (built-in chain) | no | Comma-separated provider failover order tried when the primary provider errors (`core/provider_failover.py`). Empty = default chain. |
| `AIFACTORY_PROVIDER_DEADLINE_S` | `900` | no | Total wall-clock budget (seconds) for a phase across all failover attempts (`core/provider_failover.py`). |
| `AUTO_BUILD_MODEL` | (built-in) | no | Default model for CLI auto-builds (`cli/main.py`). |
| `UTILITY_MODEL_ID` | (built-in) | no | Model for small utility completions (`core/model_config.py`). |
| `UTILITY_THINKING_BUDGET` | `""` | no | Thinking-token budget for the utility model. |
| `QA_LLM_PROVIDER` | `""` | no | Force the QA phase to a specific provider (`phase_config.py`). |
| `INSIGHT_EXTRACTION_ENABLED` | `true` | no (off disables) | Enable session-insight extraction (`analysis/insight_extractor.py`). |
| `INSIGHT_EXTRACTOR_MODEL` | (built-in) | no | Model used for insight extraction. |
| `AIFACTORY_SOLO_MODE` | off | no | Single self-directed agent for small jobs (token-saving). |
| `QUICK_MODE` | off | no | Shorter prompts / quick path (`prompts_pkg/prompts.py`, `spec/pipeline/agent_runner.py`). |
| `BMAD_SESSION_SEGMENTATION` | off | no | Per-story session segmentation for large tasks. |
| `USE_CLAUDE_MD` | off | no | Inject the repo `CLAUDE.md` into the agent context (`core/client.py`). |
| `DELEGATE_BY_DEFAULT` | off | no | Delegate the coder phase by default (per-project `.aifactory/.env`). |
| `AIFACTORY_AGENT_STALL_TIMEOUT` | `600` | no | Seconds before an agent with no output is considered stalled. |
| `AIFACTORY_FIRST_TOKEN_TIMEOUT` | `120` | no | Seconds to wait for the agent's first token (`agents/session.py`). |
| `AIFACTORY_MAX_RATE_LIMIT_WAIT_SECONDS` | `3300` | no | Cap on backoff waiting out a provider rate limit (`agents/base.py`). |
| `AIFACTORY_GUARDRAIL_REPEAT_FAIL` | `5` | no | Repeated-failure count that trips the guardrail (`agents/guardrails.py`). |
| `AIFACTORY_GUARDRAIL_TOOL_FAIL_HALT` | `8` | no | Tool-failure count that halts the agent. |
| `AIFACTORY_GUARDRAIL_NOPROGRESS` | `5` | no | No-progress count that trips the guardrail. |
| `AIFACTORY_ACT_GUARDRAIL` | off | no | Enable the act-loop guardrail hook (`agents/act_loop_hooks.py`). |
| `AIFACTORY_CONTEXT_SUMMARY` | off | no | Enable rolling context summarization (`agents/context_summary.py`). |
| `AIFACTORY_MUTATION_LEDGER` | off | no | Record agent file mutations to a ledger (`agents/mutation_ledger.py`). |
| `AIFACTORY_TEST_EVIDENCE_GATE` | on (set to `off`/`0`/`false`/`no` to disable) | no | Honesty gate (`agents/test_evidence.py`). Two checks, both at `update_subtask_status(... "completed")`: (1) #851 — a test/verification subtask needs a real, non-failing test command recorded this build; (2) #1111 — a subtask naming an HTTP path needs at least one Python test mentioning that path which reaches the shipped app (imports an application entrypoint without building its own, or calls a live server), so a suite asserting against a `FastAPI()` built inside the test file no longer counts. Inert when the subtask names no HTTP path or no Python test mentions it. (3) #1123 — one post-merge gate (`agents/route_wiring.py`), run once at the trailing-gate step when every wave has merged, that imports the application entrypoint the repo's own tests name and asserts every HTTP path the plan promised resolves on the real app. No gate at all when the plan promises no HTTP path or no test names an entrypoint; reported `skipped` (never `passed`) when the app cannot be imported. |
| `AIFACTORY_SELF_HEAL` | off | no | Enable the pre-merge security self-heal gate (`agents/self_heal_integration.py`, `core/worktree.py`). |
| `AIFACTORY_CLAUDE_ENFORCEMENT_ENABLED` | off | no | Enable per-org Claude enforcement (`core/enforcement.py`). |
| `AIFACTORY_CLAUDE_ENFORCEMENT_FAILURE_MODE` | `open` | no | `open` (fail-open) or `closed` (fail-closed) when enforcement can't decide. |
| `AIFACTORY_GRAPHIFY_ENABLED` | off | no | Opt into the Graphify code-graph MCP tool for the coder (`core/client.py`); forwarded to build Jobs. |
| `AIFACTORY_TEST_AGENT_CMD` | `""` | no | Override the agent command (test hook, `services/agent_service.py`). |
| `AIFACTORY_GIT_LOCK_TIMEOUT` | `120` | no | Seconds to wait for the per-repo git lock (`core/worktree.py`). |
| `DEFAULT_BRANCH` | auto-detect | no | Base branch for worktree creation (`core/worktree.py`, `cli/workspace_commands.py`). |
| `PROJECT_WORKSPACE_ROOT` | (data dir) | no | Root for project workspaces (`services/project_workspace_service.py`). |
| `CI` | runtime-detected | no | `true` selects non-interactive / CI reviewer behaviour (`cli/spec_commands.py`, `review/reviewer.py`). Also set in the fixed build-Job env. |
| `ENABLE_FANCY_UI` | `true` | no | Rich terminal UI vs plain text (`ui/capabilities.py`). |
| `FACTORY_SERVICE_NAME` | `the Factory` | no | Service name string in GitLab-provider messages. |
| `EMAIL_OAUTH_REDIRECT_URI` | (derived) | no | Override the email-integration OAuth redirect (`routes/email.py`). |

## Build backend & job-native scheduling

How `run.py` executes and what lets a build schedule on any node. Read in
`apps/web-server/server/services/build_backend.py`, `core/nix_env.py`,
`core/workspace_fetch.py`, `agents/gate_runner.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_BUILD_BACKEND` | `subprocess` | no | `subprocess` (in-pod asyncio) or `kubejob` (each build as its own k8s Job). Unknown values fall back to `subprocess`. |
| `AIFACTORY_IMAGE` | (built-in) | no | Running image; fallback build-Job image when `AIFACTORY_BUILD_IMAGE` unset. |
| `AIFACTORY_BUILD_IMAGE` | (unset) | no | Override the image for the `kubejob` build Job only. Point at a `:sha-<short>-nix` tag for in-image Nix. |
| `AIFACTORY_PACK_WORKSPACE` | off | no | Pack `/work` to object storage and unpack in the Job (removes the workspace node-pin). |
| `AIFACTORY_PACKED_NIX_IN_IMAGE` | off | no | Resolve `/nix` from the build image instead of the Nix-store PVC (removes the last node-pin). |
| `WORKSPACE_URI` | `""` | conditional | Object-storage URI the Job unpacks `/work` from (set by the pack path; `core/workspace_fetch.py`). |
| `AIFACTORY_DATA_ROOT` | `/home/nonroot/.aifactory` | no | Data PVC mount root; used to compute worktree subPaths and sandbox mounts. |
| `AIFACTORY_SANDBOX_NAMESPACE` | `factory` | no | K8s namespace the build Job runs in. |
| `AIFACTORY_BUILD_SA` | `aifactory-sandbox` | no | ServiceAccount for the build Job. |
| `AIFACTORY_BUILD_DEADLINE_SECONDS` | `21600` (6h) | no | Active-deadline for the build Job. |
| `AIFACTORY_SANDBOX_REPO_PVC` | `aifactory-data` | no | PVC holding the repo/worktree (co-mounted into the Job). |
| `AIFACTORY_NIX_STORE_PVC` | `""` | no | Warm Nix-store PVC to mount into the Job. Empty on the in-image-Nix path. |
| `AIFACTORY_CLI_CREDS_SECRET` | `""` | no | K8s Secret name holding CLI credentials mounted into the build Job. |
| `APP_BACKEND_PATH` | `/home/projects/MagesticAI/apps/backend` | no | Backend path inside the build Job. |

### Build-Job env allowlist (`_PASSTHROUGH_BUILD_ENV`)

A `kubejob` build runs in a fresh pod. `build_job_env()` seeds it with a
**fixed** non-interactive env plus a **passthrough allowlist** — control-plane
vars are forwarded **only when present**, in env (never argv). Anything not on
the allowlist (notably `ANTHROPIC_API_KEY` and control-plane secrets) never
reaches the Job.

Fixed env always set: `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8`,
`CLAUDE_CODE_ENTRYPOINT=cli`, `CI=true`, plus `CLAUDE_CODE_OAUTH_TOKEN` (the
pooled token).

Forwarded when present: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`,
`ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `NO_PROXY`,
`DISABLE_TELEMETRY`, `DISABLE_COST_WARNINGS`, `API_TIMEOUT_MS`, `GITHUB_TOKEN`,
`GH_TOKEN`, `OPENAI_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`,
`OPENAI_COMPATIBLE_BASE_URL`, `GEMINI_API_KEY`, `GEMINI_CLI_TRUST_WORKSPACE`,
`GOOGLE_API_KEY`, `OLLAMA_API_KEY`, `OLLAMA_CLOUD_BASE_URL`, `S3_ENDPOINT`,
`S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_REGION`,
`AIFACTORY_GRAPHIFY_ENABLED`.

The `S3_*` namespace is what the Job-side `core/artifact_store` reads to
reconstitute a packed workspace (distinct from the chart's fsspec
`AIFACTORY_S3_*` / `AWS_*` `WorkspaceStore` vars below). All forwarded only when
non-empty.

## Execution mode & parallelism

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_INTAKE_PARALLEL` | off | no | Fleet default: unlabelled issue-intake builds run parallel waves. Per-issue labels win. |
| `AIFACTORY_INTAKE_WORKERS` | (unset) | no | Worker cap for parallel intake builds; unset uses the coder default (3). |

`run.py --parallel` / `--workers N` are CLI flags, not env vars. Per-issue
labels (`factory:parallel`, `factory:serial`, `factory:workers=N`) are covered
in the [Configuration Reference](./configuration-reference#parallel-execution-for-intake-builds).

## Intake poller

Read in `apps/web-server/server/services/intake_poller.py`,
`apps/backend/intake/*`, `routes/from_issue.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_INTAKE_POLLER` | off | no | Enable the background issue-intake poller. |
| `AIFACTORY_INTAKE_REPOS` | `""` | conditional | Comma-separated repos the poller watches. |
| `AIFACTORY_INTAKE_INTERVAL_S` | `30` | no | Poll interval (min 5s). |
| `AIFACTORY_INTAKE_REQUEUE_AFTER_S` | `600` | no | Requeue a stuck in-flight issue after N seconds (min 60). |
| `AIFACTORY_INTAKE_DB` | (data dir) | no | Override the processed-issue store path (`intake/processed_store.py`). |
| `AIFACTORY_INTAKE_AUTO_HANDOFF` | off | no | Auto-hand off to the next stage after an intake build (`routes/from_issue.py`). |
| `AIFACTORY_TOKEN` | (none) | conditional | AIFactory API token for the poller's own calls (falls back to `APP_API_TOKEN`). |
| `AIFACTORY_URL` | `""` | conditional | AIFactory base URL for poller callbacks. |
| `PFACTORY_TOKEN` | (none) | no | PFactory token for ingest (falls back to `APP_API_TOKEN`). |
| `PFACTORY_INGEST_URL` | `""` | no | PFactory ingest endpoint the poller posts to. |

## PR endgame, deploy & Copilot

Read in `apps/web-server/server/services/pr_endgame.py`,
`services/copilot_dispatch_service.py`, `routes/github.py`. These are commonly
set per-project in `.aifactory/.env`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_AUTO_PR` | off | no | Auto-open a PR on a clean build. |
| `AIFACTORY_AUTO_MERGE` | off | no | Auto-merge after the reviewer approves (requires `AIFACTORY_AUTO_PR`). |
| `AIFACTORY_PR_REVIEWER` | `aifactory` | no | Which review gates merge: `aifactory`, `copilot`, or `any`. |
| `AIFACTORY_AUTO_DEPLOY` | off | no | Deploy-then-verify to AWS App Runner on a clean build, then tear down. |
| `AIFACTORY_COPILOT_DISPATCH_ENABLED` | off | no | Enable GitHub Copilot dispatch for reviews. |

## Completion events & outbox

RFC-0001 PARR spine. Read in `apps/web-server/server/services/completion.py`,
`services/outbox.py`. Best-effort — a missing target never breaks a build.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_COMPLETION_WEBHOOK` | (none) | no | URL to POST normalized completion events (cockpit ingress `:3111/api/events`). |
| `AIFACTORY_COMPLETION_WEBHOOK_TIMEOUT` | `5` | no | Timeout (seconds) for the completion webhook. |
| `AIFACTORY_COMPLETION_SENTINEL` | off | no | Also write `<spec_dir>/COMPLETED.json` on completion. |
| `AIFACTORY_COMPLETION_OUTBOX` | off | no | Enable the durable completion outbox (retry queue). |
| `AIFACTORY_COMPLETION_OUTBOX_DB` | (data dir) | no | Override the outbox DB path. |
| `AIFACTORY_EVENT_SOURCE` | `/aifactory` | no | CloudEvents `source` for emitted events. |
| `AIFACTORY_WORKER_PROGRESS_INTERVAL_S` | (built-in) | no | Interval for worker progress heartbeat events. |

## MCP control plane

Read in `apps/web-server/server/mcp_remote/*`, `routes/copilot_mcp.py`,
`apps/backend/mcp_server/*`, `agents/tools_pkg/http_client.py`,
`core/mcp_credentials.py`, `agents/tools_pkg/mcp_catalog.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_MCP_REMOTE_ENABLED` | off | no | Expose the HTTP+SSE MCP server at `/api/mcp-remote/sse`. |
| `AIFACTORY_MCP_LOOPBACK_URL` | `http://localhost:3101` | no | Loopback URL MCP tools call back into. |
| `AIFACTORY_MCP_SECRET` | `""` | conditional | Shared secret for the Copilot MCP endpoint. |
| `AIFACTORY_MCP_KEY` | `""` | no | Scoped MCP key (`acw_`) the agent HTTP client presents. |
| `AIFACTORY_API_URL` | `http://localhost:3101` | no | Base URL the agent tools call the web server on. |
| `AIFACTORY_API_TOKEN_FILE` | `~/.aifactory/.token` | no | File the agent tools read the API token from. |
| `AIFACTORY_SPEC_DIR` | (none) | no | Spec directory the MCP server serves (`mcp_server/aifactory_server.py`). |
| `AIFACTORY_PROJECT_DIR` | (cwd) | no | Project directory for the MCP server (falls back to `CLAUDE_PROJECT_DIR`). |
| `GCP_MCP_ENDPOINT` | (built-in default) | no | Endpoint for the GCP MCP catalog tool. |
| `GRAPHITI_MCP_URL` | (none) | conditional | Enables the Graphiti MCP memory tool when set (`agents/tools_pkg/models.py`, `core/client.py`). |

## Live Console (rmux)

Read in `apps/web-server/server/rmux/*`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_RMUX_ENABLED` / `APP_RMUX_ENABLED` | off | no | Enable the streaming rmux Live Agent Console. |
| `AIFACTORY_RMUX_PANES_DIR` | (data dir) | no | Writable directory for rmux FIFO panes. |

## Security & sandbox

Read in `apps/web-server/server/services/sandbox.py`,
`apps/backend/agents/gate_runner.py`, `agents/coder.py`, `core/client.py`,
`runners/ai_analyzer/claude_client.py`, `project/models.py`, `security/egress.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_BASH_SANDBOX` | on | no | Gate the bubblewrap syscall sandbox; set off where bwrap can't mount `/proc`. |
| `AIFACTORY_AGENT_SANDBOX` | `off` | no | OS-level agent sandbox mode (`services/sandbox.py`). |
| `AIFACTORY_AGENT_SANDBOX_PIDNS` | off | no | Opt into a private PID namespace for the sandbox. |
| `AIFACTORY_SANDBOX_BACKEND` | `docker` | no | Gate-runner sandbox backend (`docker` / `nixjob`). |
| `AIFACTORY_SANDBOX_GATES` | off | no | Run agent gates inside the sandbox. |
| `AIFACTORY_SANDBOX_IMAGE` | `""` | no | Image for sandboxed gate runs. |
| `AIFACTORY_SANDBOX_NETWORK` | `none` | no | Network mode for the sandbox. |
| `AIFACTORY_SANDBOX_REPO_PVC` | `aifactory-data` | no | Repo PVC for sandboxed gate runs. |
| `AIFACTORY_EXTRA_ALLOWED_COMMANDS` | `""` | no | Extend the dynamic command allowlist (`project/models.py`). |
| `AIFACTORY_EGRESS_POLICY` | `off` | no | Agent egress policy mode (`security/egress.py`). |
| `AIFACTORY_EGRESS_ALLOWED_HOSTS` | `""` | no | Allowed egress hosts when the egress policy is on. |
| `AIFACTORY_INJECTION_SCAN` | `on` | no (fail-closed) | Prompt-injection content-scan gate mode: `on` / `warn` / `off`. Unrecognised values resolve to `on` so a typo can't silently disable it (`security/content_scan.py`). |
| `AIFACTORY_AUTH_PREFLIGHT` | `warn` | no | Provider-credential pre-flight check before a run: `off` / `warn` / `enforce` (`core/auth_preflight.py`). |
| `METRICS_SCRAPE_TOKEN` | `""` | no | Bearer token gating the `/metrics` scrape endpoint (`observability/metrics.py`). |

## Identity providers (OIDC / SAML / SCIM / email OAuth)

Read in `apps/web-server/server/oidc/*`, `saml/*`, `scim/*`,
`identity_providers.py`, `routes/oidc_routes.py`, `_get_email_oauth_credentials.py`.

### OIDC

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `APP_OIDC_ENABLED` | off | no | Enable OIDC login. |
| `APP_OIDC_ISSUER_URL` | (none) | conditional | OIDC issuer URL (required when enabled). |
| `APP_OIDC_CLIENT_ID` | (none) | conditional | OIDC client ID. |
| `APP_OIDC_CLIENT_SECRET` | (none) | conditional | OIDC client secret. |
| `APP_OIDC_PROVIDER` | `keycloak` | no | Provider preset (`oidc/presets.py`). |
| `APP_OIDC_SCOPE` | preset default | no | OIDC scopes. |
| `APP_OIDC_REDIRECT_URI` | (derived) | no | Override the OIDC redirect URI. |
| `APP_OIDC_POST_LOGIN_REDIRECT` | `/` | no | Post-login redirect path. |
| `APP_OIDC_POST_LOGOUT_REDIRECT` | `/` | no | Post-logout redirect path. |
| `APP_OIDC_DEFAULT_ROLE` | `member` | no | Role assigned to new OIDC users. |
| `APP_OIDC_GROUP_TO_ROLE` | `""` | no | JSON map of IdP groups to roles. |
| `APP_OIDC_DEFAULT_ORG_SLUG` | `default` | no | Default org slug for provisioned users. |
| `APP_OIDC_DEFAULT_ORG_NAME` | `Default Organization` | no | Default org display name. |
| `APP_OIDC_USERINFO_CACHE_TTL_S` | (built-in) | no | TTL for cached OIDC userinfo. |
| `OIDC_DISPLAY_NAME` | `OIDC (default)` | no | Login-button label (`identity_providers.py`). |

### SAML

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `SAML_ENABLED` | off | no | Enable SAML SSO. |
| `SAML_IDP_NAME` | `saml` | no | Internal IdP identifier. |
| `SAML_IDP_DISPLAY_NAME` | (= name) | no | Login-button label. |
| `SAML_SP_ENTITY_ID` | `""` | conditional | Service-provider entity ID. |
| `SAML_ACS_URL` | `""` | conditional | Assertion Consumer Service URL. |
| `SAML_IDP_METADATA_URL` | (none) | conditional | IdP metadata URL (or use the file form). |
| `SAML_IDP_METADATA_FILE` | (none) | conditional | Path to IdP metadata XML. |
| `SAML_SP_CERT_FILE` | (none) | no | SP certificate file path. |
| `SAML_SP_KEY_FILE` | (none) | no | SP private-key file path. |
| `SAML_SP_CERT_PREVIOUS_FILE` | (none) | no | Previous SP cert (rotation). |
| `SAML_REQUIRE_ENCRYPTED_ASSERTION` | off | no | Require encrypted assertions. |
| `SAML_IDP_INIT_DEFAULT_RETURN_TO` | `""` | no | Default return-to for IdP-initiated login. |
| `SAML_SLO_ENABLED` | off | no | Enable Single Logout. |
| `SAML_SLO_URL` | (none) | no | SP SLO endpoint. |
| `SAML_IDP_SLO_URL` | (none) | no | IdP SLO endpoint. |

### SCIM & email OAuth

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `SCIM_ENABLED` | off | no | Enable the SCIM provisioning endpoint (`main.py`). |
| `SCIM_BEARER_TOKEN` | `""` | conditional | Bearer token gating SCIM (`scim/auth.py`). |
| `APP_EMAIL_GOOGLE_CLIENT_ID` | (none) | conditional | Google email-integration OAuth client ID. |
| `APP_EMAIL_GOOGLE_CLIENT_SECRET` | (none) | conditional | Google email-integration OAuth secret. |
| `APP_EMAIL_MICROSOFT_CLIENT_ID` | (none) | conditional | Microsoft email-integration OAuth client ID. |
| `APP_EMAIL_MICROSOFT_CLIENT_SECRET` | (none) | conditional | Microsoft email-integration OAuth secret. |

## Multi-tenant, crypto (KMS) & audit anchor

Read in `apps/web-server/server/tenancy.py`, `services/tenant_reconciler.py`,
`services/tenant_teardown.py`, `crypto/kms/*`, `services/audit_anchor.py`,
`jobs/audit_anchor_cron.py`, `services/vault_client.py`, `crypto/kms/vault.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_MULTI_TENANT` | off | no | Enable multi-tenant isolation (`tenancy.py`). |
| `TENANT_ISOLATION_ENABLED` | off | no | Gate tenant-teardown isolation logic. |
| `TENANT_NAMESPACE_PREFIX` | `aifactory-tenant` | no | Namespace prefix for per-tenant namespaces. |
| `TENANT_DELETION_GRACE_DAYS` | `30` | no | Grace period before tenant deletion. |
| `TENANT_TEARDOWN_DRY_RUN_HOURS` | `24` | no | Dry-run window before teardown executes. |
| `TENANT_CLOUD_BACKEND` | `aws` | no | Cloud backend for tenant reconciliation. |
| `TENANT_CNI_BACKEND` | `auto` | no | CNI backend for tenant network policy. |
| `TENANT_S3_BUCKET` | `""` | no | S3 bucket for per-tenant prefixes (empty = skip). |
| `TENANT_OIDC_PROVIDER_ARN` | `""` | no | IRSA OIDC provider ARN (empty = skip IAM role). |
| `TENANT_ALLOWED_FQDNS` | `api.anthropic.com` | no | Allowed-FQDN egress list for tenants. |
| `TENANT_QUOTA_PODS` | `50` | no | Per-tenant pod quota. |
| `TENANT_QUOTA_PVCS` | `10` | no | Per-tenant PVC quota. |
| `TENANT_LIMIT_CPU` | `500m` | no | Default per-tenant CPU limit. |
| `TENANT_LIMIT_MEMORY` | `512Mi` | no | Default per-tenant memory limit. |
| `KMS_BACKEND` / `APP_KMS_BACKEND` | `fernet` | no | Envelope-encryption backend: `fernet`, `aws`, `gcp`, `azure`, `vault`. |
| `KMS_FERNET_KEY` / `APP_KMS_FERNET_KEY` | (none) | conditional | Fernet key for the `fernet` KMS backend. |
| `AWS_KMS_KEY_ID` | (none) | conditional | KMS key ID (AWS backend). |
| `AWS_REGION` | (none) | no | AWS region for KMS/services. |
| `AWS_ENDPOINT_URL` | (none) | no | Custom AWS endpoint (e.g. LocalStack). |
| `GCP_KMS_KEY_NAME` | (none) | conditional | KMS key name (GCP backend). |
| `AZURE_KEYVAULT_URL` | (none) | conditional | Key Vault URL (Azure backend). |
| `AZURE_KEYVAULT_KEY` | (none) | conditional | Key Vault key name (Azure backend). |
| `VAULT_ADDR` | (none) | conditional | HashiCorp Vault address (Vault backend / vault client). |
| `VAULT_TOKEN` | (none) | conditional | Vault token. |
| `VAULT_NAMESPACE` | (none) | no | Vault namespace (Enterprise). |
| `VAULT_TRANSIT_KEY` | (default) | no | Vault transit key name. |
| `VAULT_TRANSIT_MOUNT` | (default) | no | Vault transit mount point. |
| `AUDIT_ANCHOR_PER_TENANT` | `false` | no | Anchor the audit chain per tenant. |
| `AUDIT_ANCHOR_BATCH_SIZE` | `100` | no | Rows per anchor batch. |
| `AUDIT_ANCHOR_KEY_CACHE_SIZE` | `1000` | no | Per-tenant anchor-key cache size. |
| `AUDIT_ANCHOR_GENESIS` | `GENESIS` | no | Genesis sentinel for an empty audit chain. |

## Graphiti memory

Enable/disable and provider selection for the persistent memory layer. Read in
`apps/backend/integrations/graphiti/config.py`, `query_memory.py`. Provider
credential/model vars (`OPENAI_*`, `AZURE_OPENAI_*`, `VOYAGE_*`, `GOOGLE_*`,
`OPENROUTER_*`, `OLLAMA_*`, `ANTHROPIC_API_KEY`, `GRAPHITI_ANTHROPIC_MODEL`) are
listed under [provider credentials](#non-claude-provider-credentials).

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `GRAPHITI_ENABLED` | `true` | no (off disables) | Enable the Graphiti memory layer. |
| `GRAPHITI_LLM_PROVIDER` | `openai` | no | LLM provider: `openai`/`anthropic`/`azure_openai`/`ollama`/`google`/`openrouter`. |
| `GRAPHITI_EMBEDDER_PROVIDER` | `openai` | no | Embedder provider: `openai`/`voyage`/`azure_openai`/`ollama`/`google`/`openrouter`. |
| `GRAPHITI_DATABASE` | `magestic_ai_memory` | no | LadybugDB database name. |
| `GRAPHITI_DB_PATH` | `~/.magestic-ai/memories` | no | LadybugDB storage path. |
| `GRAPHITI_ANTHROPIC_MODEL` | `claude-sonnet-4-5` | no | Anthropic model when Graphiti LLM = anthropic. |

## Observability & tracing

Read in `apps/web-server/server/observability/*`,
`apps/backend/core/job_tracing.py`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | no | OTLP endpoint; set enables OpenTelemetry export. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | no | OTLP protocol (`grpc`/`http`). |
| `OTEL_SERVICE_NAME` | `aifactory-web` | no | Service name for web-server traces/metrics. |
| `OTEL_SERVICE_NAME_AGENT` | `aifactory-agent` | no | Service name for agent traces. |

## TFactory / PFactory handoff

Read in `apps/backend/pfactory/tfactory_client.py`, `pfactory/tiers.py`,
`services/agent_service.py`. (Poller-side `PFACTORY_*` / `AIFACTORY_URL` vars
are under [Intake poller](#intake-poller).)

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `TFACTORY_BASE_URL` | `""` | conditional | TFactory base URL; unset disables the handoff (a local marker is still written). |
| `TFACTORY_TOKEN` | (none) | no | Bearer token for TFactory (falls back to `APP_API_TOKEN`). |
| `TFACTORY_HANDOFF_PATH` | `/api/handoff` | no | TFactory handoff endpoint path. |
| `TFACTORY_PROJECT_ID` | (derived) | no | TFactory project ID for the handoff (`tfactory_client.py`). |

## Workspace object storage (fsspec `WorkspaceStore`)

Read in `apps/web-server/server/services/workspace_store.py`. Distinct from the
Job-side `S3_*` namespace in the [build allowlist](#build-job-env-allowlist-_passthrough_build_env).

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_S3_ENDPOINT_URL` | (none) | no | Custom S3 endpoint for the fsspec workspace store (MinIO etc.). |
| `AIFACTORY_S3_ADDRESSING_STYLE` | (provider default) | no | S3 addressing style (`path`/`virtual`). |

## Batch API (Anthropic Message Batches)

Read in `apps/backend/analysis/insight_extractor.py` (helper: `core/batch.py`).

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_BATCH_MIN_JOBS` | `2` | no | Below this many concurrent completions, skip the batch path. |
| `AIFACTORY_BATCH_TIMEOUT` | `120` | no | Hard timeout (seconds) for the batch poll loop. |
| `AIFACTORY_BATCH_DISABLE` | off | no | Kill-switch forcing the sequential path. |

## Frontend build (Vite)

Baked into the static bundle at build time. Read in `apps/frontend-web/src/*`.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `VITE_API_URL` | (same-origin) | no | Backend base URL for integration settings. |
| `VITE_API_BASE_URL` | `/api` | no | API base path for the API client. |
| `VITE_WS_BASE_URL` | (derived) | no | WebSocket base URL. |
| `VITE_DEBUG` | off | no | Enable verbose frontend debug logging (with `DEV`). |
| `VITE_PFACTORY_URL` | `https://pfactory.freundcloud.org.uk` | no | Plan-portal link in the portal switcher. |
| `VITE_AIFACTORY_URL` | `https://aifactory.freundcloud.org.uk` | no | Build-portal link in the portal switcher. |
| `VITE_TFACTORY_URL` | `https://tfactory.freundcloud.org.uk` | no | Test-portal link in the portal switcher. |
| `VITE_CFACTORY_URL` | `https://cfactory.freundcloud.org.uk` | no | Cockpit link in the portal switcher. |

## Trusted-plan ingest

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>` | (none) | no | HMAC key verifying a signed Task Contract v2 from an upstream authority (e.g. `AIFACTORY_TRUSTED_PLAN_KEY_PFACTORY`). Legacy single-key entry; matches envelopes with no `kid`. See [Task Contract](./task-contract). Read in `apps/backend/trusted_plan.py`. |
| `AIFACTORY_TRUSTED_PLAN_KEY_<AUTHORITY>__<KID>` | (none) | no | Keyed rotation entry: registers one verification key under `authority/kid` (kid case-insensitive). Multiple kids can be active at once for zero-downtime rotation. See [Trusted-plan Key Rotation](./compliance/trusted-plan-key-rotation). |
| `AIFACTORY_TRUSTED_PLAN_RETIRED_KIDS` | (none) | no | Comma-separated `authority/kid` (or bare `kid`) to revoke; a retired kid is rejected at verify time even if its key material is still configured. |
| `AIFACTORY_BASELINE_DRIFT` | `blast-radius` | no | Baseline-freshness policy on the trusted-plan path (#1109). `blast-radius` rejects with 422 only when a file in `baseline.blast_radius.files` changed between the approved commit and the target repo's HEAD, and records unrelated drift as a warning; `reject` rejects any divergence from `baseline.commit`; `warn` never rejects but still records the verdict on the spec's provenance; `off` skips the check. An unrecognised value falls back to `blast-radius` — a typo must not disable a supply-chain control. Read in `apps/backend/trusted_plan.py`. |

### Baseline freshness (#1109)

**User story.** As a reviewer who approved a plan against the repo as it stood
last Tuesday, I want the build to refuse — or at minimum say so on the record —
when the code the plan is about has moved since, so my approval does not silently
attach to a tree nobody reviewed.

The signature gate answers *did anyone edit the instructions*. Baseline
freshness answers *are the instructions still about this codebase*. A contract
carries the approved commit twice (`provenance.baseline_commit` and
`baseline.commit`), both inside the signed bytes, so both are already
tamper-evident; this gate is what reads them back.

Verdicts recorded on `requirements.json` under `provenance.baseline_drift`:

| Verdict | Meaning | Rejected under… |
|---------|---------|-----------------|
| `absent` | The contract carries no baseline (greenfield, or a v1 plan). Nothing recorded. | never |
| `current` | HEAD is the approved commit (a short SHA still counts as the same commit). | never |
| `unknown` | HEAD could not be resolved, or the approved commit is not in this clone (e.g. a shallow clone). | never |
| `drifted` | The tree moved, but no blast-radius file was touched. | `reject` |
| `blast_radius_changed` | A file the plan intends to touch changed since approval. | `reject`, `blast-radius` |

`unknown` never rejects, deliberately: a control that cannot see must say so
rather than guess. Guessing "current" would hide the very drift the gate exists
to catch; guessing "drifted" would turn every shallow clone into an outage.
Every non-`absent` verdict is written to the spec's provenance even when it does
not reject — "we proceeded" is a decision, and an auditor should not have to
reconstruct it from a log line.

## Approved-model registry

Read in `apps/backend/model_registry.py`. See [Approved-model Registry](./compliance/model-registry).

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `AIFACTORY_MODEL_REGISTRY_ENFORCE` | `warn` | no | Registry assertion mode: `warn` (log unregistered/mis-staged model, never block), `deny` (raise and fail the stage), `off` (skip). Default is advisory — routing behaviour is unchanged. |
| `AIFACTORY_MODEL_REGISTRY` | (built-in) | no | Override the approved-model allowlist: inline JSON or a path to a JSON file. Invalid/unreadable value fails safe to the built-in `DEFAULT_REGISTRY`. |

## Debug helpers

Read in `apps/backend/core/debug.py`, `ui/status.py`, and various runners.

| Variable | Default | Required | Purpose |
|----------|---------|----------|---------|
| `DEBUG` / `APP_DEBUG` | off | no | Enable debug logging across backend + web server. |
| `DEBUG_LEVEL` | `1` | no | Debug verbosity: 1=basic, 2=detailed, 3=verbose. |
| `DEBUG_LOG_FILE` | (stdout) | no | Write debug logs to a file instead of stdout. |

## Intentionally excluded (incidental)

These are read by the code but are pure OS / runtime / toolchain noise an
operator does not configure for AIFactory. They are listed here so the
reconciliation below accounts for every grepped name.

- **OS / shell / terminal**: `PATH`, `HOME`, `PWD`, `TERM`, `SHELL`, `EDITOR`,
  `VISUAL`, `NO_COLOR`, `FORCE_COLOR`, `XDG_RUNTIME_DIR`, `XDG_CACHE_HOME`.
- **Python / Node runtime**: `PYTHONPATH`, `PYTHONUNBUFFERED`, `PYTHONIOENCODING`,
  `NODE_ENV`, `DEV` (Vite build-mode flag).
- **Injected by the Claude Code CLI / k8s, not set by operators**:
  `CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_PROJECT_DIR`, `KUBECONFIG`, `TRACEPARENT`
  (W3C trace-context propagation).
- **Analyzer example strings / test fixtures** (not AIFactory config — they are
  literals inside code that scans *other* repos, or pytest fixtures):
  `VAR`, `VAR_NAME`, `API_KEY`, `QUOTED`, `ALREADY_SET`, and the bare `PORT`
  example in `analysis/analyzers/port_detector.py` (the real port is `APP_PORT`).

## Completeness

This reference was reconciled against an exhaustive grep of the codebase
(`os.environ`, `os.getenv`, `getenv(`, `env.get(`, `process.env`,
`import.meta.env`, every `APP_`-prefixed pydantic `Settings` field, the
`_PASSTHROUGH_BUILD_ENV` allowlist, and every indirected `_ENV_*` constant).

- **303** unique variable names found in code.
- **288** documented above (including the `_PASSTHROUGH_BUILD_ENV` allowlist and
  recognized/scrubbed credentials).
- **15** intentionally excluded as incidental (OS/runtime noise, CLI-injected
  vars, the analyzer `PORT` example) — a subset of the fuller excluded list
  above, which also names OS vars read only via `env.get`/fixtures.
- **0** unaccounted for.
