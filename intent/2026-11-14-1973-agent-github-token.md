---
status: draft
issue: 1973
author: olafkfreund
---

# Intent: keep the control plane's GitHub token out of agent children

## Problem

The aifactory pod holds one GitHub token (`GITHUB_TOKEN` from
`factory-secrets`, today a PAT with `admin` and `delete_repo` scopes). The web
server needs it to push branches, open PRs and merge them. But the same token
is also copied into every runner subprocess and every build Job
(`RUNNER_KEEP` in `apps/web-server/server/utils/subprocess_env.py:53`,
`_PASSTHROUGH_BUILD_ENV` in `services/build_backend.py:308`). Traced from the
code on 2026-11-14:

| Child | Uses GitHub for |
| --- | --- |
| build runner `run.py` (subprocess or Job) | fetch the base branch; push the build branch (Job only); GitHub MCP tools |
| spec creation `spec_runner.py` | GitHub MCP tools only |
| PR review `runners/github/runner.py` | read PRs and diffs with `gh` |
| changelog runner, clarification and insights (`claude --print`) | nothing |

Two consequences:

- Three children get a token they never use.
- The agent itself does not get `GITHUB_TOKEN` in its env (it is blanked,
  `core/auth.py:151`), but the GitHub MCP server the agent drives is started
  with the same token (`core/mcp_credentials.py:187`), and the runner process
  the agent runs under holds it too. Spec creation reads untrusted issue
  text, so a prompt-injected agent can use the full scope of that token
  through MCP tools, far beyond pushing its own branch.

The owner's fix (2026-10-31) is a GitHub App with narrow permissions, but the
App does not exist yet, and even with it the children would hold the same
token the control plane merges with.

## Proposed outcome

- A child that does no GitHub work gets no GitHub token.
- An operator can give the children their own, narrower token. When that is
  set, the control plane's token never enters a runner subprocess or a build
  Job; the web server keeps using its own token for PRs and merges.
- Without that setting nothing changes for the children that need GitHub, so
  no deployment breaks on upgrade.
- PR creation and merge still work: they run in the web server, which is
  untouched. A test proves it.

## Affected users and systems

- AIFactory web-server (runner spawns, build Job env), the Helm chart, docs.
- Operators: one new optional secret.
- Team tiso owns general agent child-env isolation (#1930, #1931, #1932).
  This change touches only the GitHub token names in the child env.

## Constraints

- Limited to the GitHub token. No other env scrubbing changes (#1931).
- No secret writes, no GitHub settings changes, no deploys: those are owner
  actions listed in the PR.
- Must not break the base-branch fetch, the Job's branch push, PR review
  reads, or the PR endgame.
- The new token must never reach the agent's own env or logs.
- Same-uid reads of the web server's `/proc/<pid>/environ` are #1931, not
  this issue.

## Open questions

None. Revoking the PAT's admin scopes and creating the App stay with the
owner (issue comment 2026-10-31); this issue stays open until then.
