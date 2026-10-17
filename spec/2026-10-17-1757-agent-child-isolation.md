---
status: draft
issue: 1757
intent: intent/2026-10-17-1757-agent-child-isolation.md
---

# Spec: Agent child isolation (env, HOME credentials, /proc, host_isolated)

Issues: #1757, #1732, #1762 (b), #1724, #1760. Verified open on `origin/dev`
at `4638e87d`; none is fixed there. #1810 closed #1762 (a) and (c) only.

## Facts the options rest on

- **The agent can always read its own CLI's credential.** The CLI holds it in
  memory or env and runs the model's commands as its own children, same uid.
  No option below hides `CLAUDE_CODE_OAUTH_TOKEN` from a claude agent. The goal
  is that it reads nothing else.
- **The SDK cannot unset a variable.** It builds the CLI env as
  `{**os.environ, **options.env}` (`apps/backend/core/auth.py:95-101`), so an
  allowlist on that path is expressed as "blank every inherited key not on the
  list" (`core/client.py:632`, `core/simple_client.py:88`).
- **Where each CLI keeps its login.** claude: `CLAUDE_CODE_OAUTH_TOKEN` env, or
  `$CLAUDE_CONFIG_DIR` (default `~/.claude`)`/.credentials.json` for Remote
  Control (`apps/web-server/server/services/agent_service.py:1116-1160`).
  codex: `$CODEX_HOME` (default `~/.codex`)`/auth.json`. copilot:
  `~/.config/github-copilot/apps.json` or `COPILOT_GITHUB_TOKEN`. gemini /
  antigravity: `~/.gemini/oauth_creds.json` or `GEMINI_API_KEY`. opencode:
  `$XDG_DATA_HOME/opencode/auth.json` plus a model cache under
  `$XDG_CACHE_HOME` (`apps/backend/providers/opencode_agentic.py:116-140`).
  The build Job seeds the same files (`build_backend.py:468-475`).
- **Refresh tokens rotate.** codex and claude rewrite their file when they
  refresh. A copy that is not written back logs the user out on the next run.
- **Custom command MCP servers inherit the claude CLI env**
  (`core/client.py:937-955`: no `env` key), so an operator credential with an
  arbitrary name reaches them only by inheritance today. Catalog servers
  already pass theirs explicitly through `_externalize_secret_env()`
  (`core/client.py:380-394`).
- **No uid switch and no PID namespace in-pod.** The chart runs uid 65532 with
  all capabilities dropped (`charts/aifactory/values.yaml:50-61`); bwrap's
  `--unshare-pid` needs privilege (`server/services/sandbox.py:22-28`).
- **The bwrap sandbox binds only system dirs and the worktree**
  (`sandbox.py:46,135-159`): no `/home`, no `/nix`, no data root. It hides
  `HOME` today, but a backend under `/home/projects/...` or a Nix store cannot
  run inside it, so it is unlikely to be on in any deployment.

## Options

### Option A (recommended): per-CLI private HOME + env allowlist + quiesce before push

Smallest change that closes all four holes on every backend without new
privilege.

A1. **Private HOME per agent spawn** (#1732, #1762 b). New helper
`core/agent_home.py: agent_home(cli) -> contextmanager[dict[str,str]]`:
- makes a 0700 dir under `$TMPDIR` (`aif-home-*`);
- copies in only that CLI's credential file(s) from the real `HOME` (table
  above), 0600, plus `~/.gitconfig` with no `credential.*`/`include*` keys
  (identity only);
- yields `HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`,
  `XDG_STATE_HOME`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` pointing into it;
- on exit, writes each credential file back to the real path when its content
  changed (atomic rename, under an `fcntl` lock on the real file), then removes
  the dir;
- fails closed: an error building the dir raises; the spawn never falls back
  to the real `HOME`.
Callers: `core/client.py:632` (claude SDK), `core/simple_client.py:88`,
`providers/codex.py:193`, `providers/codex_agentic.py:246`,
`providers/copilot_agentic.py:195`, `providers/antigravity.py:285`,
`providers/antigravity_agentic.py:197`, `providers/opencode_agentic.py:358`,
`tools/executor.py:322,428` (tool-loop Bash gets an empty private HOME), the
three `get_agent_env_blanks()` runners (`runners/insights_runner.py:197`,
`runners/ai_analyzer/claude_client.py:134`,
`runners/github/services/followup_reviewer.py:705`). The opencode cache
pre-warm keeps reading the real cache (it runs in run.py, not the agent).

A2. **Env allowlist** (#1757). Replace the deny list in the agent paths with
`AGENT_ENV_ALLOW` in `core/auth.py`: `PATH`, `LANG`, `LC_*`, `TZ`, `TERM`,
`TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `PYTHONUNBUFFERED`, `PYTHONIOENCODING`,
`CI`, `CLAUDE_CODE_*`, `NIX_*` / `NIX_PATH` (Nix gates), `SSL_CERT_FILE`,
`NODE_EXTRA_CA_CERTS`, proxy vars, `TRACEPARENT`, plus `_AGENT_ENV_KEEP`, plus
the per-CLI `keep` names the providers already pass, plus the A1 HOME vars.
- SDK path: `get_agent_env_blanks()` returns `""` for every `os.environ` key
  not allowed (a blank `SSH_AUTH_SOCK` is treated as unset by ssh).
- `child_env()` gains `agent=True`, used only by the agent call sites in A1;
  git/gh/server callers keep today's deny list, so nothing else changes.
- MCP (#1680 constraint): a custom server entry gets an optional
  `env_keys: [..]`; those host vars are read in run.py and passed with
  `_externalize_secret_env()` like catalog servers. Plus a project-level
  escape hatch `AIFACTORY_AGENT_ENV_EXTRA` (comma list) for anything else.
- `agent_service.py:1047-1060`: the backend `.env` load drops keys
  `is_denied_env_key()` matches, same as `make_subprocess_env()`.
- `SSH_AUTH_SOCK` is not on the list: dropped.

A3. **Quiesce before the token is used in a build Job** (#1724). In the Job,
before `maybe_push_workspace_branch()` (`core/workspace_fetch.py:95-160`) and
before any other `github_env()` use after the agent started, run.py SIGKILLs
every process of its uid that is not itself or an ancestor (walk `/proc`),
then checks none is left and refuses to push if one is. Only in a build Job
(`JOB_ID` and `FACTORY_SERVICE` both set, as the Job spec sets them); on a
local runner that would kill the user's session, so there the window stays
open and is documented. Also: `GITHUB_TOKEN`/`GH_TOKEN` are blanked for the
agent (already true) and the agent's private `/tmp` is a fresh `TMPDIR` under
A1's dir, so `aif-gh-*` is not in a dir the agent lists by default (defence
in depth only; `/tmp` stays reachable).

A4. **host_isolated stays narrow** (#1760). Do not widen. Reasons: on a
sandbox-pidns host run.py still loads the backend `.env` unscrubbed until A2
lands, the token holder and the agent share uid and the same pidns inside the
sandbox, and the bwrap layout cannot run the backend from `/home` or `/nix`.
Update the comment on `trusted_contract.py:35-39` and the test docstring
`test_pid_namespaced_sandbox_host_is_not_isolated` to say why, and close
#1760 as "decided: not now". Widening becomes a follow-up once A2 and A3 are
in and a sandbox layout that runs the backend exists.

**What breaks.** Agents lose `~/.ssh` and the SSH agent (ssh git remotes,
signed commits inside a build). Tools that read config from the real home
(`~/.npmrc` registry auth, `~/.m2/settings.xml`, `~/.cargo/credentials`,
`~/.docker/config.json`) stop seeing it, so private registries need the
credential named per project. A custom MCP server that relied on an inherited
env var breaks until its entry lists `env_keys`. Agent-side caches under
`~/.cache` start cold per task (slower first `npm`/`pip`).

**Migration.** Release note listing the dropped vars and files; one-time log
line per spawn naming each inherited key that was blanked and is not on the
list (names only), for one release, so operators can find what to add;
`env_keys` documented next to `CUSTOM_MCP_SERVERS`; the build Job keeps the
`seed-creds` initContainer unchanged (A1 copies from its seeded `HOME`).

**Test plan.**
- `tests/test_agent_home.py`: the dir holds only the CLI's files; another
  CLI's file and a fake `~/.ssh/id_ed25519` in a temp real-home are absent; a
  changed credential is written back, an unchanged one is not touched; a
  failure to create the dir raises.
- `tests/test_child_process_env.py`: an unknown `FOO_SECRETISH=x` is absent
  with `agent=True` and present without; `SSH_AUTH_SOCK` absent;
  `get_agent_env_blanks()` blanks a non-allowed key and keeps `PATH`.
- MCP: a custom server with `env_keys=["MY_MCP_KEY"]` gets the value in the
  claude env and not in argv.
- `agent_service`: a denied key in the backend `.env` is not in run.py's env.
- Quiesce: a test that starts a `setsid sleep` child, runs the quiesce, and
  asserts it is gone and that the push is refused when a kill fails (mocked).
- Live: one claude, one codex and one opencode build on the local runner and
  one kubejob build, with a codex token refresh across two builds.

### Option B: env allowlist only, keep HOME, drop SSH_AUTH_SOCK

A2 alone plus `SSH_AUTH_SOCK` on the strip list.
- Closes #1757 and the SSH-agent half of #1732.
- Leaves every on-disk login under `HOME` readable (#1732, #1762 b), leaves
  the shared Job `HOME` with all four logins, and does nothing for #1724.
- **Breaks:** same env breakage as A2; no HOME breakage.
- **Migration:** as A2. **Tests:** the A2 tests.
Smaller and safer, but does not close the holes the issues are about.

### Option C: separate uid for the agent

Run the agent CLI (and its children) as a second uid; run.py, git push and
the token stay with the first uid. Closes all four issues fully, including
`/proc` on the local runner, because `/proc/<pid>/environ`, `/proc/<pid>/root`
and 0600 files are unreadable across uids.
- **Breaks:** needs `CAP_SETUID`/`CAP_SETGID` or root in the runner. The chart
  forbids both (`values.yaml:50-61`); the Job would need a split into two
  containers with an RPC between run.py and the agent, or a privileged init
  that `setpriv`s. On a local runner it needs a system user, sudoers or a
  setuid helper, and worktree group permissions so both uids can write.
- **Migration:** new chart values and pod security exception, a NixOS/host
  setup step for the agent user, file ownership fix-up of existing worktrees.
- **Tests:** a privileged CI job; cross-uid read tests for `/proc` and `$HOME`.
The strongest boundary but a large change to every deployment. Better as the
#363 end state after A.

## Alternatives rejected

- **Widen host_isolated to sandbox-pidns now (#1760).** The sandbox shares uid
  and pidns between run.py and the agent, run.py still gets the backend `.env`
  unscrubbed, and the sandbox cannot run the backend from `/home` or `/nix`.
  Widening would mark tasks verified on a host that does not isolate them.
- **bwrap around each agent CLI as the HOME fix.** It would hide `HOME` by
  bind layout, but needs unprivileged user namespaces on every node
  (`sandbox.py:59-91` degrades to unwrapped when they are off), must bind
  `/nix` and `/home/projects` for the CLIs to run, and is passthrough when
  disabled. A1 works everywhere and does not depend on node sysctls.
- **Symlink or bind-mount only the cred file into the private HOME.** A
  symlink is replaced, not followed, by the CLIs' atomic rewrite on refresh,
  so the real file goes stale; bind mounts need a mount namespace. Copy in +
  write back covers both.
- **Pass the GitHub token to git through a pipe/fd instead of hosts.yml
  (#1724).** Same uid can open `/proc/<git pid>/fd/N` of a dumpable process,
  so it only moves the window.
- **Do not forward GITHUB_TOKEN to the Job; push from the control plane.**
  Closes #1724 in Jobs cleanly but moves the packed-workspace push
  (`workspace_fetch.py:95`) and the in-build PR endgame to the server, a
  larger change to the RFC-0017 flow. Worth revisiting with C.

## Risks

- Token write-back races: two concurrent builds on one codex login both
  refresh; last writer wins and the other refresh token may already be
  revoked. Today they share the live file and see the same race in place. The
  lock narrows it; it does not remove it.
- A CLI that stores its login somewhere the table does not list (a new CLI
  version) loses it: the spawn fails with an auth error, not silently. Each
  provider's hard-failure detection (`providers/codex_agentic.py:79-110`)
  surfaces it.
- An allowlist that misses a var a real toolchain needs (e.g. `JAVA_HOME`,
  `GOPATH`, `CARGO_HOME` from a devenv) breaks builds. Mitigation: Nix gates
  run in `nix develop` which sets its own env; the escape hatch and the
  one-release log line.
- A3 kills by uid. A wrong Job detection on a local runner would kill the
  user's processes. Gate on `JOB_ID` and `FACTORY_SERVICE` both set, and test
  that it is a no-op without them.

## Verification

Done when, on `dev` after the plan is implemented:
- the tests listed under Option A pass, and `ruff check`, `ruff format
  --check` and the existing `tests/test_child_process_env.py`,
  `tests/test_runner_hardening.py`,
  `apps/web-server/tests/test_trusted_contract_merge.py` pass;
- a local-runner build with codex and one with claude (OAuth token and
  Remote Control) succeed, and `ls ~/.ssh`, `cat ~/.config/gh/hosts.yml`,
  `echo $SSH_AUTH_SOCK` run by the agent return nothing;
- a kubejob build succeeds and its agent cannot read
  `~/.codex/auth.json` when routed to claude;
- #1760 is closed with the decision, #1724 notes the local-runner limit.
