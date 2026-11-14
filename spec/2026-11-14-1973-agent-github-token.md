---
status: approved
issue: 1973
intent: intent/2026-11-14-1973-agent-github-token.md
---

# Spec: keep the control plane's GitHub token out of agent children

## Design

Two changes, both in how the web server builds a child's env.

"GitHub names" below means `GITHUB_TOKEN`, `GH_TOKEN`,
`GITHUB_PERSONAL_ACCESS_TOKEN`, and the operator's MCP alias when one is
configured: the env name in `github.tokenEnv` of
`~/.aifactory/mcp-credentials.json`, which the GitHub MCP probe reads before
any other name (`apps/backend/core/mcp_credentials.py:188-190`). A helper
`github_names()` in `apps/web-server/server/utils/subprocess_env.py` returns
them (the same lookup as `services/github_app.py:152-154`).

### 1. Children that do no GitHub work get no GitHub names

`make_subprocess_env` (`subprocess_env.py:78`) gains a keyword
`github: bool = False`.

- `RUNNER_KEEP` no longer contains `GITHUB_KEEP`.
- After the scrub, every GitHub name is removed from the result, in both
  modes. The explicit removal is needed because the runner scrub
  (`child_env(runner=True)`) does not apply the `*_TOKEN` rule, and
  `GITHUB_PERSONAL_ACCESS_TOKEN` and an alias are not on the deny list.
- With `github=True` the result of `runner_github_env()` (section 2) is then
  applied.

Callers:

| Caller | `github` | Why |
| --- | --- | --- |
| `services/agent_service.py:1110` (`run.py`) | True | base fetch, GitHub MCP |
| `services/agent_spec_creation.py:223` (`spec_runner.py`) | True | GitHub MCP; chains into `run.py` on the subprocess backend |
| `services/pr_review_service.py:258` (`runners/github/runner.py`) | True | `gh` reads |
| `services/changelog_service.py:216` | False | stdlib-only script |
| `services/clarification_service.py:195` (`claude --print`) | False | no GitHub use |
| `services/insights_service.py:531` (`claude --print`) | False | no GitHub use |

The three `github=False` callers add nothing to the env afterwards that could
bring a GitHub name back (checked: they set `PYTHON*`, the OAuth token and
the data class only; none loads a `.env` file or project settings).

### 2. An optional token for the children: `AIFACTORY_AGENT_GITHUB_TOKEN`

New in `subprocess_env.py`:

```python
AGENT_GITHUB_TOKEN_VAR = "AIFACTORY_AGENT_GITHUB_TOKEN"

def agent_github_token() -> str:
    """The operator's token for runner children, stripped; "" when unset."""

def runner_github_env() -> dict[str, str]:
    """GitHub token vars for a runner child or build Job (#1973)."""
```

`runner_github_env()`:

- If the agent token is set: return it under every GitHub name, the alias
  included. Since section 1 removed every GitHub name first, the child
  cannot see the control plane's token under any of them. Setting them all
  also means nothing that only adds absent keys can bring in another value:
  the loaders below, and the runners' own `load_dotenv` calls
  (`apps/backend/cli/utils.py:48`, `runners/spec_runner.py:91`,
  `runners/github/runner.py:64`), which do not override. (Changed after the
  diff review, 2026-11-14: the first version left the alias absent, and a
  value for it in `apps/backend/.env` came back inside the child.)
- Otherwise: return every GitHub name that has a non-empty value in
  `os.environ`, unchanged. This is today's behaviour.
- If the variable is present but blank (for example a Secret key with no
  value), log one WARNING that the children fall back to the control plane's
  token, then behave as unset.

Used in two places:

- `make_subprocess_env(github=True)`.
- `build_job_env` (`services/build_backend.py:350`): `GITHUB_TOKEN` and
  `GH_TOKEN` leave `_PASSTHROUGH_BUILD_ENV`; the function merges only those
  two names from `runner_github_env()`, so the Job receives exactly the
  names it receives today. The stale comments at `build_backend.py:234` and
  `:267` ("for run.py's PR endgame") are corrected: the Job uses the token
  for the base fetch, the packed-workspace branch push and the GitHub MCP
  server; the PR endgame runs in the web server.

The variable itself must never reach a child: add
`AIFACTORY_AGENT_GITHUB_TOKEN` to `_AGENT_ENV_DENY_EXACT`
(`apps/backend/core/auth.py:118`). `child_env` drops denied names for every
caller.

Loaders that run after the scrub in the `github=True` callers copy keys from
`apps/backend/.env` when they are absent: `_load_backend_env`
(`agent_service.py:144`, already skips denied names), the inline loop in
`agent_spec_creation.py:240-250` and `_load_env_file`
(`pr_review_service.py:76`), neither of which checks anything. A new helper
`dotenv_key_blocked(key) -> bool` in `subprocess_env.py` is true for the
agent variable always, and for any GitHub name while the agent token is
set; the three loaders skip such keys. So a `.env` file can neither hand
the agent variable to a child nor add an alias next to the agent token.
`load_project_env` adds only allowlisted project keys that are absent, and
`mcp_spawn_env` returns one fixed key with the project's MCP toggles
(`project_settings_store.py:442-448`); neither can replace the token.

Without the agent token the loaders behave as today.

### 3. What does not change

- The web server's own GitHub calls: `github_env(child_env())` reads the
  token from `os.environ` (`apps/backend/core/git_credentials.py:42`). PR
  create, review request and merge (`services/pr_endgame.py:79`,
  `routes/pr.py`), project clone and the control plane's worktree fetch keep
  the control plane's token.
- With the GitHub App configured, the ambient token is the installation
  token. `github_app.start()` does not refuse an agent token: it is not one
  of the names the web server or `gh` read. If both are set, children use
  the agent token and the web server acts as the App. The operator must not
  point `github.tokenEnv` at the agent variable: `start()` treats that alias
  as a PAT and refuses to boot (`github_app.py:152-158`); the docs say so.
- `_externalize_secret_env` and the agent env allowlist: untouched (#1931).
  The agent can still read the token its MCP server gets; after this change
  that is the narrow token when the operator sets one.

### 4. Chart and docs

- `charts/aifactory/values.yaml`: `agentGithubToken: {secretName: "",
  secretKey: token}`. `templates/deployment.yaml`: when `secretName` is set,
  add env `AIFACTORY_AGENT_GITHUB_TOKEN` from that Secret key. The block
  goes after the `githubApp` block (`:683`), outside the
  `mcpCredentials.enabled` condition, so it works with either. Off by
  default.
- `docs/docs/concepts/github-app.md`: new section "Token for agent
  children": what the children use GitHub for, the fine-grained PAT
  permissions to grant (Contents read and write, Pull requests read, Issues
  read, Metadata read; only the repos AIFactory builds), that without the
  setting the children share the control plane's token, and the accepted
  residual from the intent. Update the "Limits" bullet "Claude agents never
  see the token", which is wrong when the GitHub MCP server is on.

## Alternatives rejected

- **Drop the token from every child and push from the control plane only.**
  The packed-workspace Job has the only copy of the build tree; it must push.
  The base fetch and the PR review reads also run in the child.
- **Mint a down-scoped App installation token per child** (GitHub allows
  `repositories` and `permissions` on the mint call). The right end state,
  but it needs the App and per-call minting (#1700). The helper is the one
  place to add it.
- **Separate read-only and read-write child tokens.** Two more secrets and a
  per-caller choice; spec creation becomes the build on the subprocess
  backend, so it cannot be read-only there.
- **Stop handing the MCP credential to the agent CLI env.** That is the
  agent-env isolation of #1931 (team tiso), and the argv alternative leaks
  the token in `ps` (#599).
- **Refuse to start without an agent token.** Breaks every existing
  deployment on upgrade.

## Risks

- A child marked `github=False` that does use GitHub would lose access. The
  three were traced (stdlib-only changelog script; two bare `claude --print`
  calls). If the `claude` CLI itself used `GITHUB_TOKEN` for something, that
  use stops; none is known.
- An agent token that is too narrow breaks the base fetch (private repos) or
  the Job's branch push. The docs list the permissions; failure shows as a
  git auth error in the build log.
- `tests/test_github_env.py:273` pins `*GITHUB_KEEP,` in `subprocess_env.py`
  by approver decision #1723 D1 (the runner keeps the token). This change
  keeps that decision for the children that need GitHub and narrows it for
  the others; the pin is updated to the new line.
- Open PR #2015 also edits `_PASSTHROUGH_BUILD_ENV`; a textual conflict is
  possible, not a logical one.

## Verification

- `tests/test_child_process_env.py`, with three distinct control-plane
  values in `GITHUB_TOKEN`, `GH_TOKEN` and `GITHUB_PERSONAL_ACCESS_TOKEN`
  and a fourth under an operator alias:
  - `make_subprocess_env()` has none of the four names;
  - `make_subprocess_env(github=True)` without an agent token keeps all
    four values;
  - with `AIFACTORY_AGENT_GITHUB_TOKEN` set, the three fixed names equal it,
    the alias is absent, no control-plane value appears in any env value,
    and the variable itself is absent;
  - a blank agent variable logs the warning and behaves as unset.
- The MCP probe on the child env: `_probe_github()` run under
  `make_subprocess_env(github=True)` with the agent token and an alias set
  returns the agent token; under `make_subprocess_env()` it reports no
  credential (`tests/test_mcp_credentials.py`).
- Final spawn env: for the spec-creation and PR-review loaders, a backend
  `.env` that defines `GITHUB_TOKEN`, `GITHUB_PERSONAL_ACCESS_TOKEN`, the
  alias and `AIFACTORY_AGENT_GITHUB_TOKEN` leaves the env unchanged for
  those keys when the agent token is set; `dotenv_key_blocked` unit test.
- `build_job_env`: no token, ambient token, agent token; never an empty
  placeholder; never `GITHUB_PERSONAL_ACCESS_TOKEN`
  (`tests/test_build_backend_kubejob.py`).
- PR creation unaffected: with both tokens set, `github_env(child_env())`
  writes the control plane's token to `hosts.yml`, not the agent token, and
  the agent variable is not in that env (`tests/test_github_env.py`);
  `apps/web-server/tests/test_pr_endgame.py` still passes.
- `github_app.start()` succeeds with the agent variable set next to the App
  variables (`apps/web-server/tests/test_github_app.py`).
- Source tests: each `make_subprocess_env(` call site passes the `github`
  value in the table; `runner_github_env` is referenced only in
  `subprocess_env.py` and `build_backend.py`; the `*GITHUB_KEEP,` pin in
  `tests/test_github_env.py:273` is replaced by the new allowed lines.
- `tests/test_env_passthrough_credentials.py` exemption sets updated;
  `is_denied_env_key("AIFACTORY_AGENT_GITHUB_TOKEN")` is true.
- Helm: `tests/helm/` renders the env only when `secretName` is set, with
  and without `githubApp.enabled`.
- `pytest tests/test_child_process_env.py tests/test_github_env.py
  tests/test_env_passthrough_credentials.py tests/test_build_backend_kubejob.py
  tests/test_agent_env_scrub.py tests/test_mcp_credentials.py tests/helm
  apps/web-server/tests/test_pr_endgame.py apps/web-server/tests/test_github_app.py`
  and `scripts/cq_ratchet.py` on the touched files.
- Not verified: a live build with a real fine-grained PAT (owner action).
