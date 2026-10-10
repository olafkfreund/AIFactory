---
status: draft
issue: 1688
intent: intent/2026-10-10-1688-github-token-per-call.md
---

# Spec: the GitHub token must not sit in the environment of `gh` and `git` children

## Design

`gh` and `git` stop getting the token through the environment. For each call
that needs GitHub auth, the server writes the token to a 0600 `hosts.yml` in a
fresh 0700 temp dir and points `GH_CONFIG_DIR` at it. `gh` reads it directly,
and `git` gets it through `gh auth git-credential`, scoped to
`https://github.com`. The dir is removed when the call ends. This narrows the
exposure from "readable in `environ` for every call" to "readable through the
child's `/proc` while the call runs". It does not close it. See Risks 1.

### Proposed answers to the intent's open questions

These are proposed defaults for the approver to confirm or change at this
gate. They are not yet approved.

1. **Threat bar: narrow now, close fully in a follow-up.** Each call gets a
   `mkdtemp` 0700 dir under `/tmp` holding `GH_CONFIG_DIR/hosts.yml` (0600). It
   is created in a context manager and removed in `finally`, so it is also
   removed on `TimeoutExpired` or an exception. No child env carries
   `GITHUB_TOKEN`, `GH_TOKEN` or `GIT_PASS`. One sweep at startup removes
   leftover `aif-gh-*` dirs, so a SIGKILLed server leaves no token on disk.
   Without the sweep this change would be weaker than today. Agents in the
   default sandbox already get a private tmpfs `/tmp`
   (`charts/aifactory/values.yaml:93-94`), so they cannot read the file by
   plain path, only through a dumpable child's `/proc`. Full closure needs
   `pidNamespace`, which the chart says an unprivileged pod cannot use
   (`values.yaml:101-103`, `deployment.yaml:189-192`). A follow-up issue
   covers PID isolation or a separate agent uid.
2. **Scope: web-server children only.** That means the 18
   `child_env(keep=GITHUB_KEEP)` sites, plus `GIT_PASS` in
   `project_workspace_service`'s askpass, because that is a web-server child
   too. One follow-up issue covers everything else:
   - `RUNNER_KEEP` (`utils/subprocess_env.py:32-39`, overlaps #1692);
   - `core/worktree.py:416-432`;
   - `core/git_credentials.authed_push_url` (`GIT_PASS` at `:94`), used by
     `workspace_fetch.py:135` and `tfactory_client.py:397`. Most of its
     callers run in kubejob pods, which have their own `/proc`;
   - the token that gh's web login stores in the default `GH_CONFIG_DIR`.

   `GITHUB_KEEP` stays, because `RUNNER_KEEP` still uses it.
3. **`gh auth setup-git`: remove it at the three web-server sites**
   (`routes/pr.py:285-292`, `pr_endgame.py:558`,
   `completion_orchestration.py:398-403`). Its global helper depends on a
   token in the env, which is the problem. The per-call env adds a
   github.com-scoped helper instead (section 1). `core/worktree.py:421` is left
   for the follow-up. The global helper it installs reads the same
   `GH_CONFIG_DIR`, and the per-call reset (section 1) stops it from running in
   the server's children anyway.
4. **#1692: do not wait for it.** The two run in parallel. #1692 changes the
   runner path (`subprocess_env.py:55-71`), and this change does not touch
   `make_subprocess_env` or `RUNNER_KEEP`. The files do not overlap, so
   waiting would only leave this exposure open longer.
5. **#1671: neither wait for it nor include it.** The token is resolved on
   every call through the existing `github_token()`
   (`core/git_credentials.py:52-54`), which reads `os.environ` each time. That
   function is the one place #1671 later changes to mint installation tokens.
   Resolving per call already meets the intent's constraint that the token can
   change at runtime (`.env` rewrites, lazy load in `conflict_service.py`).

Two more decisions the intent did not ask about, also proposed:

- **`gh auth login --web` (`routes/github.py:622-636`)** runs with
  `child_env()` and no `keep`. When `github_token()` is set, it first returns
  early with "already authenticated via GITHUB_TOKEN". Today a token in the env
  makes gh refuse to log in, so the result is the same, but the token stays out
  of the login child's env. The login is a long-running background process,
  so it gets no temp dir.
- **No token set (local dev).** `github_env` passes `base_env` through
  unchanged, with no `GH_CONFIG_DIR` and no helper entries. A developer's own
  gh login and git helpers behave exactly as today. Nothing is shared between
  replicas: each call has its own dir in the pod's `/tmp` emptyDir
  (`deployment.yaml:680-683`), and `replicaCount` is 1 (`values.yaml:32`).

### 1. One context manager in `apps/backend/core/git_credentials.py`

It sits next to `github_token()` and `authed_push_url`, and follows the same
`try/finally` cleanup pattern (`:97-99`).

```python
GH_DIR_PREFIX = "aif-gh-"
_GH_HELPER_KEY = "credential.https://github.com.helper"

@contextlib.contextmanager
def github_env(base_env: dict[str, str]) -> Iterator[dict[str, str]]:
    """base_env plus a per-call GH_CONFIG_DIR; the token is never in the env (#1688)."""
    token = github_token()                         # per call; #1671 hook point
    if not token:
        yield base_env                             # dev's own gh login untouched
        return
    d = tempfile.mkdtemp(prefix=GH_DIR_PREFIX)     # 0700, unpredictable name
    try:
        fd = os.open(Path(d, "hosts.yml"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:              # JSON is valid YAML: no quoting issues
            json.dump({"github.com": {"oauth_token": token, "user": USERNAME,
                                      "git_protocol": "https"}}, f)
        env = {k: v for k, v in base_env.items()
               if k not in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_PASS")}
        n = int(env.get("GIT_CONFIG_COUNT", "0"))  # after child_env's hooksPath entry
        # The empty value resets the helper list first, so a global `store` or
        # setup-git helper can neither run nor save the token.
        for i, v in enumerate(("", "!gh auth git-credential")):
            env[f"GIT_CONFIG_KEY_{n + i}"] = _GH_HELPER_KEY
            env[f"GIT_CONFIG_VALUE_{n + i}"] = v
        env |= {"GIT_CONFIG_COUNT": str(n + 2), "GH_CONFIG_DIR": d}
        if any(token in v for v in env.values()):  # fail closed; not `assert` (-O strips it)
            raise RuntimeError("GitHub token in child env")
        yield env
    finally:
        shutil.rmtree(d, ignore_errors=True)       # also on TimeoutExpired / exceptions

def sweep_github_dirs() -> None:
    """Remove dirs a SIGKILLed server left behind. Call once at startup."""
    for p in Path(tempfile.gettempdir()).glob(f"{GH_DIR_PREFIX}*"):
        shutil.rmtree(p, ignore_errors=True)       # refuses symlinks; error ignored
```

How the token reaches each tool:

- **`gh`** reads `hosts.yml` from `GH_CONFIG_DIR`. gh 2.102.0 was checked
  locally: it read a JSON `hosts.yml` from a temp `GH_CONFIG_DIR`, and
  `gh auth token` and `gh auth git-credential get` both returned the token. It
  did not migrate or rewrite the file. Writing `user` stops gh from calling the
  API to look up a username.
- **`git`** gets the credential from `gh auth git-credential`, which inherits
  `GH_CONFIG_DIR` from git's env. The config value contains no secret, so
  nothing reaches argv or `/proc/<pid>/cmdline`.
- **Scope.** The helper only answers for `https://github.com`. That meets the
  intent's constraint that the token is offered only to github.com.
- **The helper reset.** An empty helper value clears the helper list built
  from the config files. This covers the `Dockerfile:438` global helper,
  `setup-git` leftovers, `core/worktree.py:421`, and a developer's
  `credential.helper=store`. Without the reset, a `store` helper would write
  the token to `~/.git-credentials` after a successful push. This was checked
  locally with git 2.55.0.
- **Fails closed.** If `mkdtemp` or the write fails, the `OSError` reaches the
  caller. Every site already turns that into an error result (for example
  `gh.py:48`, `git.py:69`). The code never falls back to putting the token in
  the env.
- **Cleanup after a timeout.** `subprocess.run` kills and reaps the child on
  `TimeoutExpired` before the exception leaves the `with`, so `rmtree` runs
  after the child is dead.
- **#1689 and #1690.** The entries are appended after the existing
  `GIT_CONFIG_COUNT`, the same pattern as `core/child_env.py:51-57`. git-lfs
  uses git's credential helper, so #1690 gets the token the same way.

### 2. Startup sweep

`apps/web-server/server/main.py` `lifespan`: call `sweep_github_dirs()` right
after `_make_non_dumpable()` (`:104`). It runs once per process start, before
any request is served, so it never removes a dir that is in use in this
process.

### 3. Call sites

Each site changes from `env=child_env(keep=GITHUB_KEEP)` to
`with github_env(child_env()) as env:`. The web server's `child_env`
(`utils/subprocess_env.py:43`) keeps adding `TRACEPARENT`.

| File (`apps/web-server/server/`) | Lines | Change |
|---|---|---|
| `routes/pr.py` | 207, 311, 342 | wrap |
| `routes/pr.py` | 285-292 | delete the setup-git block |
| `routes/git.py` | 63, 1451 | wrap |
| `routes/github.py` | 622-636 | login guard, `child_env()` with no keep |
| `services/gh.py` | 39 | wrap |
| `services/task_branch.py` | 50 | wrap |
| `services/pr_data_service.py` | 68 | wrap |
| `services/copilot_dispatch_service.py` | 78, 126, 157 | wrap |
| `services/build_backend.py` | 953 | `github_env(child_env(extra={"GIT_TERMINAL_PROMPT": "0"}))` |
| `services/pr_endgame.py` | 66-75 | wrap once inside `_default_runner`; every endgame call goes through it |
| `services/pr_endgame.py` | 558 | delete `runner(["gh", "auth", "setup-git"], None)` |
| `services/completion_orchestration.py` | 398-403 | delete setup-git |
| `services/completion_orchestration.py` | 410 | wrap the push |
| `services/project_workspace_service.py` | 466-470 | async: the `with` covers `create_subprocess_exec` through `communicate()` and the timeout `kill()` |

Every git call at these sites is wrapped, including local-only ones. One
`mkdtemp`, one write and one `rmtree` take well under a millisecond, and
sorting git subcommands into "network" and "local" would be fragile.

### 4. `project_workspace_service._git_askpass_env` (`:141-177`)

This credential comes from the `git_credentials` table and can be for any
host, so gh's helper cannot serve it. The askpass stays, but the password
moves from the env to a file:

- The script line becomes `*) cat "$GIT_PASS_FILE" ;;`.
- One `mkdtemp(prefix=GH_DIR_PREFIX)` dir holds the script (0700) and a `pass`
  file (0600, `O_EXCL`). Because it shares the prefix, the startup sweep also
  covers it.
- It yields `GIT_PASS_FILE` and never `GIT_PASS`. `GIT_USER` stays, since a
  username is not a secret. `rmtree` runs in `finally`.

### 5. Left alone

- `child_env()` itself, `GITHUB_KEEP`, `make_subprocess_env` and
  `RUNNER_KEEP`.
- `authed_push_url` and `core/worktree.py`. They go to the scope follow-up
  (answer 2).

### Follow-up issues to open

1. Scope: `RUNNER_KEEP` (overlaps #1692), `core/worktree.py:416-432`,
   `authed_push_url`'s `GIT_PASS`, and the token gh's web login stores.
2. Close the window: PID isolation or a separate uid for agents.

## Alternatives rejected

- **Keep `GH_TOKEN` for `gh` and use askpass only for `git`.** gh has no
  askpass, so the token stays in gh's `environ`, and the outcome fails.
- **One shared `hosts.yml` written at startup.** The token would sit on disk
  for the server's whole life and would go stale when `.env` is rewritten.
  That breaks the per-call constraint.
- **Pass the config over a pipe, an fd, `memfd` or `O_TMPFILE`.**
  `GH_CONFIG_DIR` must be a real directory path, and `/proc/<pid>/fd` exposes
  an fd just like a file.
- **The `git credential-cache` daemon.** Any same-uid process can query its
  socket at any time, which is a wider window than one call.
- **Keep `gh auth setup-git`.** Its helper depends on a token in the env. It
  also rewrites global config on every call, and those writes race.
- **A flag on `child_env()`.** `child_env` returns a dict and has no scope in
  which to clean up.
- **A `try/finally` at each site.** That is 18 copies of the same logic.
- **A pid in the dir name, so the sweep skips another live server's dirs.**
  This matters only for two servers sharing one `/tmp`, such as local dev
  worktrees. There the worst case is one failed in-flight call on the other
  server. Add it if that happens.
- **`atexit` or a background reaper.** Neither runs on SIGKILL. The startup
  sweep covers that case.
- **Rewriting `authed_push_url` and `worktree.py` now.** Their callers are
  mostly kubejob pods. This is out of scope under answer 2 and goes to the
  follow-up.
- **A sandbox PID namespace now.** It is the full fix, but the chart rules it
  out for unprivileged pods (`values.yaml:101-103`,
  `deployment.yaml:189-192`). It is follow-up 2.

## Risks

1. **What stays open.** While a call runs, a process with the same uid can
   still read the token in three ways:
   - through `/proc/<child>/root/tmp/aif-gh-*/hosts.yml` or
     `/proc/<child>/fd`;
   - from gh or git memory, if ptrace is allowed;
   - by plain path, on local or co-mounted installs where agents do not have
     the bwrap tmpfs `/tmp`.

   This change closes the `environ` path for these 18 sites and nothing more.
   The runner env and the backend sites stay exposed until follow-up 1.
2. **gh format drift.** A future gh could change how it reads `hosts.yml`, and
   the version in the Wolfi image may differ from the 2.102.0 checked locally.
   The real-gh test catches that in CI. Verification step 5 checks the image.
3. **Tests that assert today's behavior.**
   - `apps/web-server/tests/test_pr_endgame.py:93` asserts
     `saw("auth setup-git")`; invert it.
   - The docstring and assertion in
     `tests/test_create_pr_fetches_branch.py:119` need an update.
   - The stub at `apps/web-server/tests/test_merger.py:313` becomes unused.
4. **Developer gh config.** When a token is set, the server's children no
   longer see the developer's gh config (aliases, extensions). These children
   already authenticate through `GITHUB_TOKEN` today and use none of that.
5. **The askpass change** applies to every host, including GitLab `oauth2`
   credentials. Only the transport changes, not the behavior.
6. **The sweep** assumes one server process per `/tmp`. That holds in the pod
   (the emptyDir at `deployment.yaml:680-683`, `replicaCount: 1`). On a dev box
   with several servers it is the case covered under Alternatives rejected.
7. **Blocking `subprocess.run` in async routes** keeps blocking as before. The
   `with` only scopes cleanup and does not change timing.

## Verification

Automated (`apps/backend/tests/test_github_env.py` plus a web-server guard):

1. `github_env` with a fake token:
   - No `GITHUB_TOKEN`, `GH_TOKEN` or `GIT_PASS` in the env, and no value
     contains the token.
   - The dir is 0700 and `hosts.yml` is 0600.
   - The dir is gone after a normal exit, after an exception and after
     `TimeoutExpired`.
   - The two helper entries come after `core.hooksPath`, and an existing
     `GIT_CONFIG_*` entry survives.
   - A token passed in through `extra` raises.
   - With no token, `base_env` comes back unchanged.
2. **Helper reset.** Use a fake `HOME` whose `.gitconfig` sets
   `credential.helper=store`, and run `git credential fill`, then `approve`,
   with the yielded env. The token comes back and no credentials file is
   written. Skipped if git is absent.
3. **Real gh.** `gh auth token` with the yielded env prints the fake token.
   Skipped if gh is absent.
4. **`sweep_github_dirs`** removes leftover dirs and does not follow a planted
   `aif-gh-*` symlink.
5. **Askpass.** `GIT_PASS` is absent, and the script prints the contents of
   `GIT_PASS_FILE`.
6. **Grep guard over `apps/web-server/server`.** It fails if any of
   `keep=GITHUB_KEEP`, `"setup-git"` or `GIT_PASS"` appears.
7. The #1362 argv tests (`tests/test_git_argv_credential_backend.py`) and
   `tests/test_child_process_env.py` pass unchanged.

Manual, on this checkout and in a pod:

1. Start the server with `GITHUB_TOKEN` set, then run a slow `gh pr view` or a
   `git fetch` of a large repo.
2. While it runs, `tr '\0' '\n' </proc/<child>/environ | grep -c <token>`
   prints 0. This is the intent's repro, which finds the token today.
3. A PR create, the endgame push and fix push, and a workspace clone of a
   private repo all succeed.
4. With no token set, a developer's own gh login still works for `gh` and
   `git push`.
5. In the image, `gh --version` matches the tested format, and step 2 above
   passes.
6. `kill -9` the server during a call and restart it. `ls /tmp/aif-gh-*`
   finds nothing.
