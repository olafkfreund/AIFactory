---
status: approved
issue: 1680
author: olafkfreund
---

# Intent: child processes must not carry the web server's secrets

## Problem

The web server holds `DATABASE_URL`, `JWT_SECRET`, `API_TOKEN`, the
trusted-plan HMAC keys (`AIFACTORY_TRUSTED_PLAN_KEY_*`), cloud and Vault
credentials, and the other values the agent scrub (`core/auth.py`,
`get_agent_env_blanks`) removes from agents. 3.8.3 (#1679) made the server
process itself non-dumpable, so same-uid processes can no longer read
`/proc/1/environ`. Its children still get those secrets:

- **About 129 spawn sites** in `apps/web-server/server` start `git`, `gh`,
  `run.py`, provider CLIs and helpers. Most pass no `env=` and inherit
  `os.environ` whole. The 21 that use `make_subprocess_env`
  (`server/utils/subprocess_env.py`) get a copy with only the Anthropic API
  key removed.
- **A child is dumpable again after `execve`.** Any same-uid process in the
  pod, including an agent, can read `/proc/<child>/environ` while the child
  runs. `run.py` lives for the whole build. Its agent is scrubbed, but
  `run.py` itself is not.
- **`git` runs hooks from the repository it operates on.** Several routes
  (`worktree_merge.py`, `git.py`, `pr.py`) run `git` in worktrees the agent
  wrote. A hook planted there runs with the server's full environment, which
  is direct exfiltration, not a `/proc` race.

With `DATABASE_URL` an agent can rewrite the `trusted_contracts` row #1667
relies on, and no isolation stamp would show it (#1667 review). The narrowed
D4 keeps auto-merge safe in production, but the secrets are still exposed.

## Proposed outcome

- No process the web server starts receives a secret it does not need.
  `run.py`, `git`, `gh` and provider CLIs get the server's environment minus
  the agent-scrub set. A child that needs one specific secret gets it by
  name.
- `git` run by the server in an agent-written worktree executes no
  repository hooks.
- A test fails if a spawn site in `apps/web-server/server` passes the
  unscrubbed environment.
- After this lands, a PID-namespaced sandbox can be reconsidered as isolated
  for #1667's D4. That is a separate decision, not part of this task.

## Affected users and systems

- `apps/web-server/server`: every subprocess spawn site, and
  `utils/subprocess_env.py`.
- `apps/backend/core/auth.py`: the deny lists, reused rather than copied.
- `run.py` builds, in-process and sandboxed. Kubejob builds already get an
  explicit environment (`build_backend.py`), so they are unaffected.
- Operators of local and in-process hosts, where this exposure is largest.

## Constraints

- Must not break a child that legitimately needs a credential: `gh` needs
  `GH_TOKEN`/`GITHUB_TOKEN`, `run.py` needs its OAuth token, and git
  askpass needs its token variables. These are passed explicitly by name.
- One scrub definition. Reuse `get_agent_env_blanks` / the `core/auth.py`
  deny lists; no second list to drift.
- No behaviour change for kubejob builds or the production auto-merge path.
- Smallest change: route spawn sites through one helper rather than
  rewriting each call.

## Open questions

Answered (user, 2026-10-09): 1, every git the server runs sets
`core.hooksPath=/dev/null`. 2, deny-list (the `core/auth.py` scrub set), with
needed secrets passed by name. 3, a pytest that scans the spawn sites.

1. **Scope of the hook fix.** Should every server-run `git` set
   `core.hooksPath=/dev/null`, or only `git` in agent-written worktrees? The
   first is simpler. It would also disable hooks a user configured for
   server-side commits, if anyone relies on that.
2. **Deny-list or allow-list.** Should children get the server's environment
   minus the scrub set (deny-list, small change), or only an explicit
   allow-list (stricter, more breakage risk)?
3. **Enforcement.** Is a test that scans the spawn sites enough, or should it
   be a lint rule in CI?
