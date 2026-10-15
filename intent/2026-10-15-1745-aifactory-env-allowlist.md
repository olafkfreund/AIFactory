---
status: draft
issue: 1745
author: olafkfreund
---

# Intent: `.aifactory/.env` may only set the keys the factory owns

Scope: the `.aifactory/.env` part of #1745 only. The git-config pinning parts
of #1745 stay open.

## Problem

Three subprocess launchers in the web server copy every `KEY=value` line from
`<project>/.aifactory/.env` into the child environment, for any key not
already set:

- `apps/web-server/server/services/pr_review_service.py:75-90` (`_load_env_file`), called at `:276`
- `apps/web-server/server/services/agent_service.py:1067-1082`
- `apps/web-server/server/services/agent_spec_creation.py:240-255`

The children run gh, git and the Claude CLI. Loader, shell and exec variables
(the dynamic-loader family, Node and Python start-up hooks, the shell start-up
file, git's ssh and config overrides) therefore reach those programs. A task
agent can write the file: task worktrees live under `<project>/.aifactory/`
and no write guard covers it. A repo that tracks `.aifactory/.env` also brings
the file in on clone. Factory security knobs read from the environment
(`AIFACTORY_EGRESS_POLICY`, `AIFACTORY_SANDBOX_*`, `AIFACTORY_ALLOW_API_KEY`,
`AIFACTORY_EXTRA_ALLOWED_COMMANDS`, ...) can be set the same way.

## Proposed outcome

All three launchers load `.aifactory/.env` through one shared loader that
copies only an explicit allowlist of the keys the factory itself writes or
reads in that file. Any other key is skipped and logged by name only (never
the value). Every setting the Settings UI writes keeps working.

## Affected users and systems

- web-server: task execution, spec creation and PR review subprocesses.
- Operators who hand-added keys outside the allowlist to `.aifactory/.env`:
  those keys stop reaching the child (they are logged as skipped).

## Constraints

- Must not break any key the factory writes (`routes/context.py`,
  `routes/projects.py`, `routes/github.py`) or reads from that file.
- Loader/shell/exec keys are refused even if a writer someday emits one.
- No value is ever logged.
- No working exploit in commits, PRs or comments.
- The agent's ability to WRITE the file is out of scope (#1746/#1747).

## Open questions

None. Conservative choices are recorded in the spec.
