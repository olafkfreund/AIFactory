---
status: approved
issue: 1757
author: olafkfreund
---

# Intent: Agent child isolation (env, HOME credentials, /proc, host_isolated)

Covers #1757, #1732, #1762 part (b), #1724 and #1760. Part (a) of #1762
(runners fail closed when prctl fails) landed in #1810. This is a design
task: the user picks an option in the spec before any plan is written.

## Problem

An agent CLI (claude, codex, copilot, gemini/antigravity, opencode) and every
command it runs share the uid, the `HOME` and most of the environment of the
process that spawned it. Four holes follow from that.

1. **Env is a deny list (#1757).** `get_agent_env_blanks()`
   (`apps/backend/core/auth.py:180-198`) blanks only names in
   `_AGENT_ENV_DENY_EXACT` (`auth.py:118-163`) or matching
   `_AGENT_ENV_DENY_PATTERN` (`auth.py:167-170`). The Claude SDK merges the whole
   `os.environ` into the CLI (`auth.py:95-101`), so any secret with a name the
   list does not know reaches the agent. `child_env()`
   (`apps/backend/core/child_env.py:93-100`) has the same shape for the other
   CLIs and the tool-loop Bash (`apps/backend/tools/executor.py:322`). On top,
   `agent_service` copies every key of the backend `.env` into run.py's env
   without any scrub (`apps/web-server/server/services/agent_service.py:1047-1060`).
2. **HOME credentials and the SSH agent (#1732, #1762 b).** `HOME`, `XDG_*` and
   `SSH_AUTH_SOCK` pass through `child_env()` because their names are not
   credential-shaped. On a local runner that is the user's real home:
   `~/.ssh`, `~/.config/gh/hosts.yml`, `~/.git-credentials`, `~/.npmrc`,
   `~/.aws`, `~/.kube`, `~/.claude/.credentials.json`, `~/.codex/auth.json`.
   In a build Job the `seed-creds` initContainer copies ALL four CLI logins
   into one shared `HOME` (`apps/web-server/server/services/build_backend.py:465-475`,
   `532-...`), so a build routed to claude can read the codex, copilot and
   gemini logins too.
3. **The per-call GitHub token is readable through /proc (#1724).**
   `github_env()` writes the token to a 0600 `hosts.yml` in
   `$TMPDIR/aif-gh-*` (`apps/backend/core/git_credentials.py:47,53-93`). The
   docstring admits it is readable via `/proc/<child>` (`:59`): `gh`/`git` are
   dumpable after `exec`, run as the same uid, and the dir is in a shared
   `/tmp`. In a build Job, run.py pushes the branch after the agent ran
   (`apps/backend/core/workspace_fetch.py:95-160`), so any process the agent
   left behind (`setsid`, `nohup`) can wait for the dir and read the token.
4. **host_isolated is narrower than its stamps (#1760).** `host_isolated()`
   (`apps/web-server/server/services/trusted_contract.py:30-39`) counts only
   kubejob with the durable store, while `_ISOLATED_STAMPS` (`:27`) and the
   spawn stamp (`agent_service.py:1211-1222`) already know `sandbox-pidns`.
   Its comment says "revisit when #1680 lands"; it has landed.

## Proposed outcome

- An agent CLI child gets only the env it needs, named on an allowlist; a new
  secret on the host does not reach it by default.
- Each agent CLI sees a private `HOME` that holds only its own login. It cannot
  read another CLI's login, the user's SSH keys or cloud configs, and has no
  `SSH_AUTH_SOCK`.
- claude, codex, copilot, gemini and opencode logins keep working, including
  token refresh, on the local runner and in build Jobs.
- In a build Job the GitHub token is never on disk or in a process while an
  agent-started process is alive.
- `host_isolated()` reflects a deliberate decision, with its test updated.

## Affected users and systems

- Local runner (`run.py` under the web server, `AIFACTORY_BUILD_BACKEND=subprocess`)
  and the user's real `HOME` on that machine.
- Kubernetes build Jobs (`kubejob` backend) and the `factory-cli-creds` secret.
- Operators with custom command MCP servers that read host env vars with
  arbitrary names (#1680 constraint).
- The trusted-plan merge gate, TFactory handoff and path floor (#1760).

## Constraints

- claude/codex/gemini CLI logins must keep working: they read creds from
  `HOME` / `CLAUDE_CONFIG_DIR` / `CODEX_HOME`. Remote Control needs the
  full-scope `~/.claude/.credentials.json` (`agent_service.py:1116-1160`).
- Operator MCP credentials with arbitrary names keep working (#1680).
- Unprivileged pods: `runAsNonRoot`, uid 65532, all capabilities dropped
  (`charts/aifactory/values.yaml:50-61`); no PID namespace there
  (`apps/web-server/server/services/sandbox.py:22-28`).
- Fail closed: a spawn that cannot build its private `HOME` does not fall back
  to the real one.
- Never touch or run the user's real credential helpers in tests.

## Open questions

1. Which option in the spec (recommended: A).
2. Is losing `SSH_AUTH_SOCK` (ssh remotes, signed commits from agents)
   acceptable, or does some project need it?
3. For #1760: keep `host_isolated()` narrow, or widen it to `sandbox-pidns`
   after the preconditions in the spec hold?
