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
| build runner `run.py` (subprocess or Job) | fetch the base branch; push the build branch (packed-workspace Job only, `core/workspace_fetch.py:114`); GitHub MCP tools |
| spec creation `spec_runner.py` | GitHub MCP tools; on the subprocess backend it then becomes `run.py` in the same process and env (`agent_spec_creation.py:179`), so it needs what the build needs |
| PR review `runners/github/runner.py` | read PRs and diffs with `gh` |
| changelog runner (a Python script) | nothing |
| clarification and insights (`claude --print`) | nothing |

How the token reaches an agent today:

- Clarification and insights start the `claude` CLI directly with this env,
  so that agent has `GITHUB_TOKEN` and `GH_TOKEN` in its own environment.
- For SDK agents `GITHUB_TOKEN` and `GH_TOKEN` are blanked
  (`core/auth.py:151`), but when the GitHub MCP server is enabled its
  credential is moved into the agent CLI's env as
  `GITHUB_PERSONAL_ACCESS_TOKEN` (`_externalize_secret_env`,
  `core/client.py:408`, fed by `core/mcp_credentials.py:187`). The agent's
  Bash tool can read it. The MCP server is configured read-only, but the
  token itself is not.
- The runner process the agent runs under holds the token as well.

Spec creation reads untrusted issue text. A prompt-injected agent can
therefore use whatever the token allows: with today's PAT that includes
deleting repositories and changing settings.

The owner's fix (2026-10-31) is a GitHub App with narrow permissions, but the
owner has not created it yet, and even with it the children would hold the
same token the control plane merges with.

## Proposed outcome

- A child that does no GitHub work gets no GitHub token, under any of the
  names the code reads (`GITHUB_TOKEN`, `GH_TOKEN`,
  `GITHUB_PERSONAL_ACCESS_TOKEN`), in the final env it is started with.
- An operator can give the children that do need GitHub their own, narrower
  token. When that is set, the control plane's token never enters a runner
  subprocess or a build Job under any of those names; the web server keeps
  using its own token for PRs and merges.
- Without that setting, the children that need GitHub get what they get
  today, so no deployment breaks on upgrade. The exposure for those children
  then stays as it is until the operator sets the token or the App replaces
  the PAT; the docs say so.
- PR creation and merge still work: they run in the web server, which is
  untouched. A test proves it.

Accepted residual: the children's token is readable by the agent whenever the
GitHub MCP server is on, and it must allow pushing a branch (the Job pushes
the build branch). So a prompt-injected agent can still read the repos and
push branches the token covers. It can no longer administer or delete them,
and branch protection keeps it off protected branches. One token is shared by
all children that need GitHub; separate read-only and write tokens are not
part of this.

## Affected users and systems

- AIFactory web-server (runner spawns, build Job env), `apps/backend/core`
  (the env deny list, for the new variable name only), the Helm chart, docs.
- Operators: one new optional secret.
- Team tiso owns general agent child-env isolation (#1930, #1931, #1932).
  This change touches only GitHub token names in the child env.

## Constraints

- Limited to GitHub token names. No other env scrubbing changes (#1931), and
  no change to how MCP credentials are handed to the agent CLI.
- No secret writes, no GitHub settings changes, no deploys: those are owner
  actions listed in the PR.
- Must not break the base-branch fetch, the Job's branch push, PR review
  reads, the chained spec-then-build run, or the PR endgame.
- The control plane's token and the new variable itself must not reach a
  child or a log once the children's token is set.
- Same-uid reads of the web server's `/proc/<pid>/environ` are #1931, not
  this issue.

## Open questions

None. Revoking the PAT's admin scopes and creating the App stay with the
owner (issue comment 2026-10-31); this issue stays open until then.
