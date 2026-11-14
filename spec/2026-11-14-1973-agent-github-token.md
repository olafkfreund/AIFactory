---
status: draft
issue: 1973
intent: intent/2026-11-14-1973-agent-github-token.md
---

# Spec: keep the control plane's GitHub token out of agent children

## Design

Two changes, both in how the web server builds a child's env. "GitHub names"
below means `GITHUB_TOKEN`, `GH_TOKEN` and `GITHUB_PERSONAL_ACCESS_TOKEN`.

### 1. Children that do no GitHub work get no GitHub names

`make_subprocess_env` (`apps/web-server/server/utils/subprocess_env.py:78`)
gains a keyword `github: bool = False`.

- `RUNNER_KEEP` no longer contains `GITHUB_KEEP`.
- With `github=False` the result has none of the GitHub names.
  `GITHUB_PERSONAL_ACCESS_TOKEN` needs an explicit drop: the runner scrub
  (`child_env(runner=True)`) does not apply the `*_TOKEN` rule and the name
  is not on the deny list.
- With `github=True` the GitHub names are filled as in section 2.

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
the data class only).

### 2. An optional token for the children: `AIFACTORY_AGENT_GITHUB_TOKEN`

New helper in `subprocess_env.py`:

```python
AGENT_GITHUB_TOKEN_VAR = "AIFACTORY_AGENT_GITHUB_TOKEN"

def runner_github_env() -> dict[str, str]:
    """GitHub token vars for a runner child or build Job (#1973)."""
```

- If `AIFACTORY_AGENT_GITHUB_TOKEN` is set (non-blank): return
  `{"GITHUB_TOKEN": t, "GH_TOKEN": t}`. Nothing from the control plane's own
  GitHub names is returned, so the child cannot see the control plane's
  token under any of the three names. The GitHub MCP probe in the child then
  resolves the agent token through `GITHUB_TOKEN`
  (`apps/backend/core/mcp_credentials.py:187`).
- Otherwise: return the GitHub names that are set in `os.environ`, unchanged.
  This is today's behaviour (plus `GITHUB_PERSONAL_ACCESS_TOKEN`, which
  passes through today as well).

Used in two places:

- `make_subprocess_env(github=True)` applies it on top of the scrubbed env.
- `build_job_env` (`services/build_backend.py:350`): `GITHUB_TOKEN` and
  `GH_TOKEN` leave `_PASSTHROUGH_BUILD_ENV`; the function merges
  `runner_github_env()` instead (dropping `GITHUB_PERSONAL_ACCESS_TOKEN`,
  which the Job never received). The stale comments at `build_backend.py:234`
  and `:267` ("for run.py's PR endgame") are corrected: the Job uses the
  token for the base fetch, the packed-workspace branch push and the GitHub
  MCP server; the PR endgame runs in the web server.

The variable itself must never reach a child: add
`AIFACTORY_AGENT_GITHUB_TOKEN` to `_AGENT_ENV_DENY_EXACT`
(`apps/backend/core/auth.py:118`). `child_env` drops denied names for every
caller, and `_load_backend_env` skips them.

Loaders that run after the scrub in the `github=True` callers
(`_load_backend_env`, the `.env` loops in `agent_spec_creation.py:240` and
`pr_review_service.py:274`, `load_project_env`) only add keys that are
absent. When the agent token is set, `GITHUB_TOKEN` and `GH_TOKEN` are
present, so none of them can replace it.

### 3. What does not change

- The web server's own GitHub calls: `github_env(child_env())` reads the
  token from `os.environ` (`apps/backend/core/git_credentials.py:42`). PR
  create, review request and merge (`services/pr_endgame.py:79`,
  `routes/pr.py`), project clone and the control plane's worktree fetch keep
  the control plane's token.
- With the GitHub App configured, the ambient token is the installation
  token. `github_app.start()` does not refuse an agent token: it is not one
  of the names the web server or `gh` read. If both are set, children use
  the agent token and the web server acts as the App.
- `_externalize_secret_env` and the agent env allowlist: untouched (#1931).
  The agent can still read the token its MCP server gets; after this change
  that is the narrow token when the operator sets one.

### 4. Chart and docs

- `charts/aifactory/values.yaml`: `agentGithubToken: {secretName: "",
  secretKey: token}`. `templates/deployment.yaml`: when `secretName` is set,
  add env `AIFACTORY_AGENT_GITHUB_TOKEN` from that Secret key. Off by
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

- `tests/test_child_process_env.py`: `make_subprocess_env()` has no GitHub
  name; `make_subprocess_env(github=True)` keeps the ambient token when no
  agent token is set; with `AIFACTORY_AGENT_GITHUB_TOKEN` set, `GITHUB_TOKEN`
  and `GH_TOKEN` equal it, the control plane's value appears in no env
  value, and neither `GITHUB_PERSONAL_ACCESS_TOKEN` nor the variable itself
  is present.
- `build_job_env`: same three cases (`tests/test_build_backend_kubejob.py`).
- PR creation unaffected: with both tokens set, `github_env(child_env())`
  writes the control plane's token to `hosts.yml`, not the agent token
  (`tests/test_github_env.py`), and `apps/web-server/tests/test_pr_endgame.py`
  still passes.
- A source test that each `make_subprocess_env(` call site passes the
  `github` value in the table above.
- `tests/test_env_passthrough_credentials.py` exemption sets updated.
- Helm: `tests/helm/` renders the env only when `secretName` is set.
- `pytest tests/test_child_process_env.py tests/test_github_env.py
  tests/test_env_passthrough_credentials.py tests/test_build_backend_kubejob.py
  tests/test_agent_env_scrub.py tests/helm apps/web-server/tests/test_pr_endgame.py`
  and `scripts/cq_ratchet.py` on the touched files.
- Not verified: a live build with a real fine-grained PAT (owner action).
