---
status: approved
issue: 1973
spec: spec/2026-11-14-1973-agent-github-token.md
coders:
  1: codex   # security-sensitive logic and its tests in one hand: token selection, call sites, loaders, Job env
  2: coder   # chart value, Helm test and docs: plain edits
reviewers:
  - codex    # independent read of the diff against this plan
  - agy      # second independent read; it found the tokenEnv alias and .env loader paths in the spec review
---

# Plan: keep the control plane's GitHub token out of agent children

## Approved decisions (from the spec)

- Scope: GitHub token names in the child env only. No other env scrubbing
  (#1931, team tiso), no change to `_externalize_secret_env`.
- "GitHub names" = `GITHUB_TOKEN`, `GH_TOKEN`,
  `GITHUB_PERSONAL_ACCESS_TOKEN`, plus the operator's MCP alias: the env
  name under `github.tokenEnv` in `~/.aifactory/mcp-credentials.json`
  (`core.mcp_credentials._load_operator_config()`), when set.
- `make_subprocess_env(..., github=False)`: every GitHub name is removed
  from the result in both modes; with `github=True` the result of
  `runner_github_env()` is then applied. `RUNNER_KEEP` loses `GITHUB_KEEP`.
- `AIFACTORY_AGENT_GITHUB_TOKEN` (optional, read by the web server only):
  when set, `runner_github_env()` returns it under every GitHub name, the
  alias included (deviation recorded 2026-11-14 after the diff review: the
  runners call `load_dotenv` themselves, so an absent alias could be
  restored from `apps/backend/.env` and win in the MCP probe); when unset it returns every GitHub name that has a
  non-empty value in `os.environ` (today's behaviour); when present but
  blank (`""`, as an empty Secret key gives, or whitespace) it logs one
  WARNING, which says that runner children fall back to the control plane's
  GitHub token, and behaves as unset.
- The variable itself never reaches a child: it goes on
  `_AGENT_ENV_DENY_EXACT`.
- `.env` loaders that run after the scrub skip the agent variable always,
  and every GitHub name while the agent token is set
  (`dotenv_key_blocked`).
- Build Job env: gets only `GITHUB_TOKEN` and `GH_TOKEN` from
  `runner_github_env()`.
- The web server's own GitHub calls (`github_env(child_env())`: PR create,
  review request, merge, clone) keep the control plane's token. Untouched.
- Callers: `github=True` for `agent_service` (run.py), `agent_spec_creation`
  (spec_runner.py), `pr_review_service` (runners/github/runner.py);
  default `False` for `changelog_service`, `clarification_service`,
  `insights_service`.
- Chart: `agentGithubToken.secretName` / `secretKey` → env
  `AIFACTORY_AGENT_GITHUB_TOKEN`, off by default, placed outside the
  `mcpCredentials.enabled` condition.

## Steps

1. All Python: token selection, call sites, loaders, Job env, tests
   (coder: codex).

   a. `apps/backend/core/auth.py:151-152`: add
      `"AIFACTORY_AGENT_GITHUB_TOKEN",` to `_AGENT_ENV_DENY_EXACT` next to
      `"GITHUB_TOKEN"`, with a one-line comment (`# #1973: read by the web
      server only; children get it as GITHUB_TOKEN.`).

   b. `apps/web-server/server/utils/subprocess_env.py`:
      - `:53-62` remove `*GITHUB_KEEP,` from `RUNNER_KEEP`. Keep the
        `GITHUB_KEEP = _core.GITHUB_KEEP` re-export at `:33` and in
        `__all__`.
      - add `AGENT_GITHUB_TOKEN_VAR = "AIFACTORY_AGENT_GITHUB_TOKEN"`;
        `github_names() -> tuple[str, ...]` (the three fixed names plus the
        alias from `_load_operator_config()`, imported lazily from
        `core.mcp_credentials` the way `services/github_app.py:152-154`
        does; skip an alias that is empty or already one of the three);
        `agent_github_token() -> str` (the stripped value; when the
        variable is in `os.environ` but blank it logs the WARNING at most
        once per process through a module-level flag, names only);
        `runner_github_env() -> dict[str, str]`;
        `dotenv_key_blocked(key) -> bool`. Add them to `__all__`.
      - `make_subprocess_env` (`:78-95`): new keyword
        `github: bool = False`; after `child_env(...)`, pop every name in
        `github_names()`; if `github`, `env.update(runner_github_env())`.
        Update the docstring (it says "Keeps RUNNER_KEEP (GitHub and
        provider keys)").

   c. Call sites: `make_subprocess_env(github=True)` at
      `apps/web-server/server/services/agent_service.py:1110`,
      `agent_spec_creation.py:223` and `pr_review_service.py:258`. The
      other three callers (`changelog_service.py:216`,
      `clarification_service.py:195`, `insights_service.py:531`) stay as
      they are.

   d. `.env` loaders skip a key when `dotenv_key_blocked(key)`:
      - `agent_service.py:151` (`_load_backend_env`): add to the existing
        condition;
      - `pr_review_service.py:88` (`_load_env_file`): add to the condition;
      - `agent_spec_creation.py:238-252`: move the inline loop into a
        module-level function `_load_spec_env(path: Path, env: dict[str,
        str]) -> None` in the same file (same parsing, same "do not
        override", plus the guard) and call it from where the loop was,
        keeping the existing `try/except` and log line. This makes the
        guard testable; change nothing else in that method.

   e. `apps/web-server/server/services/build_backend.py`: remove
      `"GITHUB_TOKEN",` and `"GH_TOKEN",` from `_PASSTHROUGH_BUILD_ENV`
      (`:308-309`); in `build_job_env` (`:350-371`) after the passthrough
      loop merge only the `GITHUB_KEEP` names from `runner_github_env()`;
      correct the comments at `:234-236` and `:266-268`: the Job uses the
      token for the base-branch fetch, the packed-workspace branch push and
      the GitHub MCP server; the PR endgame runs in the web server.

   f. Tests: everything under Tests below. Existing tests to adjust:
      - `tests/test_child_process_env.py:220-226`
        (`test_make_subprocess_env_keep_and_drop`): `GITHUB_TOKEN` is now
        absent by default. `:550-567`: read what those assert about the
        runner env and keep them true.
      - `tests/test_env_passthrough_credentials.py:38-57`: remove
        `GITHUB_TOKEN` and `GH_TOKEN` from the `RUNNER_KEEP` and
        `_PASSTHROUGH_BUILD_ENV` exemption sets.
      - `tests/test_github_env.py:273-292`
        (`test_web_server_has_no_env_token_path`): `*GITHUB_KEEP` no longer
        appears in `subprocess_env.py`; drop that allowed entry and the
        #1723 D1 comment. Add the needle `runner_github_env(` and put the
        exact stripped lines that contain it (after `ruff format`) into
        `allowed`: they may only be in `utils/subprocess_env.py` and
        `services/build_backend.py`.
      - `tests/test_build_backend_kubejob.py:327-345`: the cleanup loop
        over `bb._PASSTHROUGH_BUILD_ENV` no longer clears the GitHub names.
        In every `build_job_env` test in that file that relies on the loop,
        also `delenv` `GITHUB_TOKEN`, `GH_TOKEN`,
        `GITHUB_PERSONAL_ACCESS_TOKEN` and `AIFACTORY_AGENT_GITHUB_TOKEN`
        and monkeypatch the operator config to `{}` (one small fixture or
        helper). Fix the comment at `:332`.

   → verify by
   `apps/backend/.venv/bin/pytest tests/test_child_process_env.py tests/test_github_env.py tests/test_env_passthrough_credentials.py tests/test_build_backend_kubejob.py tests/test_agent_env_scrub.py tests/test_agent_sdk_env_blanks.py tests/test_mcp_credentials.py tests/test_tracing.py -q`
   and
   `apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_github_app.py -q -o asyncio_mode=auto`
   Traps: `apps/backend` and `apps/web-server` are separate `sys.path`
   roots; `subprocess_env.py` already inserts the backend dir (`:22-24`).
   `_load_operator_config` reads a file under `Path.home()`: every new test
   monkeypatches `core.mcp_credentials._load_operator_config` (it is looked
   up at call time through the lazy import) and never touches the real
   home. Tests must also `delenv` the four GitHub-related variables they do
   not set, because the developer shell and CI may export `GH_TOKEN`. The
   blank-token warning has a once-per-process flag: tests reset it. Never
   log a token value; test sentinels that look like tokens need
   `# gitleaks:allow` (see `tests/test_github_env.py:27`). `build_backend.py`
   uses absolute `server.` imports (TID252); extend its existing
   `server.utils.subprocess_env` import. Open PR #2015 also edits
   `_PASSTHROUGH_BUILD_ENV`: touch only the two GitHub lines. The cq
   ratchet counts ruff and mypy findings per touched file against
   `origin/dev`. Do not stage or commit.

2. Chart and docs (coder: coder).
   - `charts/aifactory/values.yaml` after the `githubApp` block (`:767-772`):
     ```yaml
     # Narrow GitHub token for runner children and build Jobs (#1973). When
     # set, the control plane's own GitHub token never enters them.
     # See docs/docs/concepts/github-app.md.
     agentGithubToken:
       secretName: ""
       secretKey: token
     ```
   - `charts/aifactory/templates/deployment.yaml` after the `githubApp`
     block's `{{- end }}` (`:683`), at the same indentation as the other
     env entries: `{{- if .Values.agentGithubToken.secretName }}` … env
     `AIFACTORY_AGENT_GITHUB_TOKEN` with `valueFrom.secretKeyRef` (`name`
     quoted, `key` from `secretKey`) … `{{- end }}`.
   - New `tests/helm/test_agent_github_token.py`, modelled on
     `tests/helm/test_github_app_toggle.py`: absent by default; present
     with the right `secretKeyRef` when `secretName` is set; also present
     with `githubApp.enabled=true` (use that file's `_ON` values) and with
     `mcpCredentials.enabled=false`.
   - `docs/docs/concepts/github-app.md`: new section "Token for agent
     children" before "Limits" (`:119`): what the children use GitHub for
     (base fetch, packed-workspace branch push, PR review reads, GitHub MCP
     tools); how to set it (chart values and the Secret); the fine-grained
     PAT to create: only the repos AIFactory builds, Contents read and
     write, Pull requests read, Issues read, Metadata read; that without it
     the children that need GitHub share the control plane's token; that an
     empty Secret value also means "not set" and is logged as a warning;
     that the agent can read the children's token whenever the GitHub MCP
     server is on, so it must stay narrow; that `github.tokenEnv` must not
     point at this variable (the App start refuses it as a PAT); that
     GitHub tokens should not be put in `apps/backend/.env`. Replace the
     Limits bullet "Claude agents never see the token. Non-Claude runners
     and the GitHub MCP server do." with: "An agent can read the GitHub
     token of its runner whenever the GitHub MCP server is enabled (it is
     passed to the agent CLI as `GITHUB_PERSONAL_ACCESS_TOKEN`). Set a
     token for agent children to keep that narrow."
   → verify by `apps/backend/.venv/bin/pytest tests/helm/test_agent_github_token.py tests/helm/test_github_app_toggle.py -q`
   (needs `helm` on PATH; if it is missing say so in the handback).
   Traps: Helm whitespace control (`{{-`) must not join lines; render and
   read the Deployment env. Do not stage or commit.

## Tests

All written in step 1 unless marked. Fixture for the env cases:
`GITHUB_TOKEN=cp-a`, `GH_TOKEN=cp-b`, `GITHUB_PERSONAL_ACCESS_TOKEN=cp-c`,
`GITHUB_MCP_TOKEN=cp-d` with the operator config monkeypatched to
`{"github": {"tokenEnv": "GITHUB_MCP_TOKEN"}}`.

`tests/test_child_process_env.py`:

- `make_subprocess_env()`: none of the four names present; the same with
  `AIFACTORY_AGENT_GITHUB_TOKEN=agent-x` set, and then `agent-x` appears in
  no env value either.
- `make_subprocess_env(github=True)`, no agent token: all four values kept.
- with `AIFACTORY_AGENT_GITHUB_TOKEN=agent-x`: the three fixed names and
  the alias equal `agent-x`; `AIFACTORY_AGENT_GITHUB_TOKEN` is absent; none
  of `cp-a..cp-d` appears in any env value.
- agent variable `""` and `"  "` (two tests, flag reset): exactly one
  WARNING whose text mentions the control plane's token; the result equals
  the unset case; the variable unset → no warning.
- `child_env()` and `child_env(agent=True)` never contain
  `AIFACTORY_AGENT_GITHUB_TOKEN`; `is_denied_env_key` is true for it.
- `dotenv_key_blocked`: the agent variable → True always; `GITHUB_TOKEN`,
  `GITHUB_PERSONAL_ACCESS_TOKEN` and the alias → True only while the agent
  token is set; `OPENAI_API_KEY` → False.
- loaders, behaviourally, for each of `_load_backend_env`
  (`agent_service`), `_load_env_file` (`pr_review_service`) and
  `_load_spec_env` (`agent_spec_creation`): a `.env` file with
  `GITHUB_TOKEN=env-1`, `GITHUB_PERSONAL_ACCESS_TOKEN=env-2`,
  `GITHUB_MCP_TOKEN=env-3`, `AIFACTORY_AGENT_GITHUB_TOKEN=env-4` and
  `FOO=bar`, loaded into `make_subprocess_env(github=True)` built with the
  agent token set: `FOO` is added, and none of `env-1..env-4` appears in
  any env value. Without the agent token, `GITHUB_MCP_TOKEN=env-3` is
  loaded as before and `AIFACTORY_AGENT_GITHUB_TOKEN` still is not.
- call sites, by AST over every `.py` under `apps/web-server/server`
  (reuse `_non_test_sources` or the walk in `tests/test_github_env.py`):
  each call to `make_subprocess_env` passes `github=True` exactly in
  `agent_service.py`, `agent_spec_creation.py` and `pr_review_service.py`,
  and no `github` keyword anywhere else; the set of files that call it is
  exactly the six in the decisions.

`tests/test_mcp_credentials.py`: with the fixture and the agent token set,
replace `os.environ` content with `make_subprocess_env(github=True)`
(`monkeypatch.setattr(os, "environ", ...)` or set/del per key) and assert
`_probe_github()` reports `agent-x`; with `make_subprocess_env()` it
reports no credential. Clear the probe's caches if it has any.

`tests/test_build_backend_kubejob.py`: `build_job_env` with no token (no
GitHub key at all, no empty placeholder), ambient token (`GITHUB_TOKEN`
kept, `GH_TOKEN` absent when unset, as today), agent token
(`GITHUB_TOKEN == GH_TOKEN == agent-x`, no `GITHUB_PERSONAL_ACCESS_TOKEN`,
no control-plane value in any env value).

`tests/test_github_env.py`: with `GITHUB_TOKEN=cp-a` and the agent token
set, `github_env(child_env())` writes `cp-a` into `hosts.yml`, and the env
has neither `agent-x` nor the agent variable (PR creation keeps the control
plane's token).

`apps/web-server/tests/test_github_app.py`: `start()` succeeds with the
agent variable set next to the three App variables (mint mocked the way the
file's other `start()` tests do).

Step 2: `tests/helm/test_agent_github_token.py` as described there.

Full run (lead, after both steps):
`apps/backend/.venv/bin/pytest tests/test_child_process_env.py tests/test_github_env.py tests/test_env_passthrough_credentials.py tests/test_build_backend_kubejob.py tests/test_agent_env_scrub.py tests/test_agent_sdk_env_blanks.py tests/test_mcp_credentials.py tests/test_tracing.py tests/helm/test_agent_github_token.py tests/helm/test_github_app_toggle.py -q`
and
`apps/backend/.venv/bin/pytest apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_github_app.py -q -o asyncio_mode=auto`;
all pass. Ratchet as CI runs it (`.github/workflows/cq-ratchet.yml:109-135`)
with `--base origin/dev`: no regression.

Not tested: a live build with a real fine-grained PAT (owner action).

## Rollback

Revert the PR. Unsetting `AIFACTORY_AGENT_GITHUB_TOKEN` (chart:
`agentGithubToken.secretName=""`) restores the old token for the children
that use GitHub without a revert; the three children that lose the token
get it back only with the revert.
