---
status: draft
issue: 1690
intent: intent/2026-10-10-1690-lfs-push-hooks-off.md
---

# Spec: push LFS objects while git hooks stay off

## Design

Before each of the 4 web-server pushes, run an explicit `git lfs push` that
can only reach the github.com remote's own LFS endpoint. If git-lfs is
missing, skip it. If the upload fails, treat it as a push failure. Hooks stay
off. This adds one pure helper, one small wrapper in `pr_endgame.py` used at
3 sites, and about 8 inline lines in `pr.py`. There is no new module, no
Dockerfile change and no new env name.

### Proposed answers to the intent's open questions

These are the defaults this spec proposes. **You confirm them at this spec
gate.** None of them is approved yet.

1. **Q1. git-lfs in the production image:** not in this change. Leave the
   `Dockerfile` alone. Open a follow-up issue for adding git-lfs to the image
   (with a `>=` floor) and for the build-Job push. Where git-lfs is missing,
   the helper returns `None`, so k8s behaves as it does today.
   *Why:* this is the smallest change, adds no image size or package upkeep,
   and creates no new failure mode in k8s. `Dockerfile:175-187` installs git,
   gh and curl but no git-lfs, and prod already commits LFS files as plain
   blobs (intent, Problem para 4).
2. **Q2. Upload failure:** treat it as a push failure. Run `git lfs push`
   before `git push`. On a non-zero exit, skip the ref push and take that
   site's existing push-failure path. Pointers are never pushed after a
   failed upload.
   *Why:* pushing pointers to objects the server lacks breaks the next
   checkout, and logging that as success is the bug itself. Reusing each
   site's failure path adds no new states (`pr.py:343-347`,
   `completion_orchestration.py:412-416`, `pr_endgame.py:582-584` and
   `:1153-1160`). Repos without LFS cannot reach this path, because the
   pinned `locksverify=false` below makes their upload a no-op with no
   network call that exits 0. This was tested here on git-lfs 3.8.0.
3. **Q3. LFS endpoint:** only the GitHub remote's own endpoint,
   `E = https://github.com/<owner>/<repo>.git/info/lfs`, built from origin's
   push URL and pinned with `-c` (the argv is below). If origin is not exactly
   one `https://github.com/<owner>/<repo>` URL, skip the LFS step and log a
   warning. Custom LFS servers are not supported.
   *Why:* the token only reaches the host the push already authenticates, and
   the `-c` values hold no secret (#1366). A test on the proposal showed
   `-c lfs.url` plus `-c lfs.pushurl` override `.lfsconfig` `lfs.url`,
   `.git/config` `lfs.pushurl` and `remote.origin.lfspushurl` together. With
   only `lfs.url` pinned, the repo's pushurl still won, so both are pinned.
   This is the same host rule as `git_credentials.py:37`
   (`GITHUB_HTTPS_PREFIX`).
4. **Q4. Scope:** only the 4 web-server sites that use the gh credential
   helper: `pr.py:334-347`, `completion_orchestration.py:404-416`,
   `pr_endgame.py:581` and `:1148-1152`. The 2 askpass sites,
   `workspace_fetch.py:135-144` (build Job) and `tfactory_client.py:395-406`,
   go to the follow-up issue.
   *Why:* the Job pod has no git-lfs, so changing `workspace_fetch` now would
   be dead code. Both askpass sites use `authed_push_url`
   (`git_credentials.py:56-99`), which #1688 is about to change, so changing
   them now would mean doing the work twice.
5. **Q5. Trigger:** run the upload whenever `shutil.which("git-lfs")` finds
   the binary. Do not read `.gitattributes`.
   *Why:* the agent controls `.gitattributes`, and it only reflects HEAD, so
   it misses pointers committed under an earlier version of the file.
   `git lfs push` scans the pushed commits itself. `shutil.which` never
   raises.
   **One deviation for you to confirm:** `git remote get-url` runs before
   every push, including on hosts without git-lfs, because the URL is the
   helper's input. That is one local config read with no network, and it
   cannot fail the push. It also keeps the `runner` call list the same on
   every host, so `FakeRunner` tests do not depend on whether the dev box has
   git-lfs. If you want zero extra spawns in k8s, gate get-url on
   `shutil.which` at each site instead.
6. **How it is built:** one pure helper, `lfs_push_argv(remote_url, ref)`,
   lives in `apps/backend/core/child_env.py` and is re-exported from
   `server/utils/subprocess_env.py`. A 9-line `push_with_lfs` wrapper in
   `pr_endgame.py` covers the three sites that can use `_default_runner`.
   `pr.py` keeps its own 60s timeout and exception mapping inline.
   *Why:* the web server already imports `core.child_env` through
   `subprocess_env.py:22-28`, so no new module and no `sys.path` change is
   needed. Every spawn reuses `child_env(keep=GITHUB_KEEP)`
   (`pr_endgame.py:67-75`), so `test_no_unscrubbed_spawn.py` still passes and
   no new `*_KEY`/`*_TOKEN` names reach children.

### Finding that the design depends on: lock verification

Without extra config, `git lfs push origin HEAD` calls GitHub's
`/locks/verify` API even when the repo has no LFS objects. Here, with
git-lfs 3.8.0, hooks off, no credential helper and an
`https://github.com/o/r.git` origin, the unpinned command did not finish in
30s (rc=124). An earlier test with no credentials failed with "Authentication
required ... verify locks" (rc=2). With `-c lfs.<E>.locksverify=false` the
same command exited 0 with no credentials, so it made no request.

So pinning `locksverify=false` is what lets repos without LFS keep "no new
failure modes": an auth hiccup, rate limit or LFS outage cannot block their
push. It matches today, because hooks are already off and no lock check runs
now.

### 1. `apps/backend/core/child_env.py`: the helper

```python
# ponytail: whole-URL match, same host rule as git_credentials.GITHUB_HTTPS_PREFIX
_GITHUB_REPO = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+?)(?:\.git)?/?")

def lfs_push_argv(remote_url: str, ref: str) -> list[str] | None:
    """`git lfs push` pinned to origin's own GitHub LFS endpoint, or None (#1690)."""
    if shutil.which("git-lfs") is None:
        return None  # k8s today
    m = _GITHUB_REPO.fullmatch(remote_url.strip())
    if m is None or m[2] in {".", ".."}:
        logger.warning("git-lfs present but origin is not one https://github.com/ repo; LFS upload skipped")
        return None
    e = f"https://github.com/{m[1]}/{m[2]}.git/info/lfs"
    return ["git",
            "-c", f"lfs.url={e}", "-c", f"lfs.pushurl={e}",  # beat .lfsconfig and repo config
            "-c", "lfs.standalonetransferagent=",           # no agent-named transfer program
            "-c", "lfs.allowincompletepush=false",          # a missing object fails, never rc=0
            "-c", f"lfs.{e}.locksverify=false",             # no /locks/verify call
            "lfs", "push", "origin", ref]
```

- The match is on the whole URL, not a prefix. It rejects userinfo
  (`https://x:tok@github.com/...`), `?`, `#`, extra path segments, `..`,
  other hosts, ssh URLs and multi-line input. A URL with a token in it is
  skipped, so nothing leaks.
- It does not import `git_credentials`, which would be circular
  (`git_credentials.py:33` imports `core.child_env`).
- `child_env.py` gains `import logging, shutil` and a module logger.
  `re` is already imported.
- `ref` is always `HEAD` or a branch name that git has already validated, so
  it cannot start with `-`.

`apps/web-server/server/utils/subprocess_env.py:28-30`: add
`lfs_push_argv = _core.lfs_push_argv` and list it in `__all__`.

### 2. URL source: `git remote get-url --push --all origin`

- `--push` returns the URL the ref actually goes to, with `pushInsteadOf`
  applied. An agent-set `url.https://evil.example/.pushInsteadOf` shows up as
  the evil URL, which the regex rejects.
- `--all` prints several lines when origin has several push URLs. The
  whole-string match rejects that, with no extra code.
- If get-url exits non-zero, there is no LFS step, and the push runs and
  fails on its own path as it does today.

### 3. `apps/web-server/server/services/pr_endgame.py`: one wrapper, 3 callers

```python
def push_with_lfs(runner: Runner, cwd: str, push_argv: list[str], ref: str) -> CmdResult:
    """Upload LFS objects first; a failed upload is the push's failure (#1690)."""
    url = runner(["git", "remote", "get-url", "--push", "--all", "origin"], cwd)
    lfs = lfs_push_argv(url.out, ref) if url.ok else None
    if lfs:
        up = runner(lfs, cwd)
        if not up.ok:
            return up  # never push pointers to objects the server lacks
    return runner(push_argv, cwd)
```

- **`:581`:** `push = push_with_lfs(runner, str(worktree), ["git", "push", "-u", "origin", branch], branch)`.
  The warn-and-keep-going path at `:582-584` stays as it is.
- **`:1148-1152`:** `push = await asyncio.to_thread(push_with_lfs, runner, worktree, ["git", "push", "--force-with-lease", "origin", "HEAD"], "HEAD")`.
  A failure still goes to the warning and human-stop at `:1153-1160`.
- **`completion_orchestration.py:404-416`:** replace the bare `_sp.run` push
  with `push_with_lfs(_default_runner, str(_wt), ["git", "push", "origin", "HEAD"], "HEAD")`.
  - `_default_runner` has the same timeout (120), env, cwd and text capture
    as the current call.
  - Import it with the file's existing lazy `from .pr_endgame import` style
    (`:221`, `:274`, `:318`).
  - The check becomes `not push.ok` / `push.err`. The warning and
    `return False` stay.
  - This removes one direct spawn rather than adding one.

### 4. `apps/web-server/server/routes/pr.py:334-347`

Inside the existing `try`, before `result = subprocess.run(push_cmd, ...)`:

```python
origin = subprocess.run(["git", "remote", "get-url", "--push", "--all", "origin"],
                        cwd=worktree_path, capture_output=True, text=True,
                        timeout=60, env=child_env(keep=GITHUB_KEEP))
lfs = lfs_push_argv(origin.stdout, worktree_branch) if origin.returncode == 0 else None
if lfs:
    up = subprocess.run(lfs, cwd=worktree_path, capture_output=True, text=True,
                        timeout=60, env=child_env(keep=GITHUB_KEEP))
    if up.returncode != 0:
        return {"success": False, "error": f"Failed to push LFS objects: {up.stderr.strip()}"}
```

A timeout or `OSError` falls into the existing `except` clauses at `:348-355`
("Push timed out" or `client_error`). The push flags at `:324-333` do not
change.

### 5. Docs and follow-ups

- `CHANGELOG.md:15`: replace "not uploaded on these pushes yet (#1690)" with
  this: LFS objects are now uploaded explicitly on the 4 server PR pushes
  where git-lfs is installed, and a failed upload now fails the push. The
  build-Job push and the TFactory push are still pending, along with git-lfs
  in the image (follow-up #NNNN).
- `plan/2026-10-09-1680-child-process-env.md:254`: make the same edit.
- Open the follow-up issue covering git-lfs in the image with a `>=` floor,
  `workspace_fetch.py:135-144` and `tfactory_client.py:395-406`.
- Add to #1689's list: `lfs.customtransfer.*`, `lfs.<url>.access`,
  `lfs.transfer.*`, `http.<url>.*` (proxy, extraheader) and
  `url.*.insteadOf`. The LFS spawn honours these the same way the push does.

**Files:** 5 code files (about 45 added lines), 2 doc lines and 1 test file.
The `Dockerfile`, `workspace_fetch.py`, `tfactory_client.py` and
`git_credentials.py` are not touched.

## Alternatives rejected

- **Turn `pre-push` back on with a scoped hooksPath, or pipe stdin to
  `git lfs pre-push`.** This breaks the #1680 "hooks stay off" decision, and
  the agent can override hook config.
- **Use the endpoint from `.lfsconfig` or `lfs.url`.** The agent controls
  them, and the token would follow them to that host. This breaks #1366 and
  Q3.
- **Gate on `.gitattributes` `filter=lfs`.** The agent controls the file, and
  it only reflects HEAD (Q5).
- **Leave `locksverify` unpinned.** Every push on a host with git-lfs would
  make a network call, and could hang or fail, even in repos that use no LFS
  (tested: rc=124 here, rc=2 without credentials).
- **Pin `allowincompletepush=true` so a missing object only warns.** That is
  the bug again: pointers on the remote with no objects, logged as success.
- **Prefix match with `GITHUB_HTTPS_PREFIX.startswith`.** It accepts
  `https://github.com/o/r/../x` and multi-line output. A full match costs
  nothing more and avoids the circular import.
- **`git remote get-url origin` without `--push`.** That is the fetch URL,
  which can differ from where the ref goes, and it skips `pushInsteadOf`.
- **One wrapper for all 4 sites, including `pr.py`.** pr.py uses a 60s
  timeout and its own exception mapping. Moving it to `_default_runner`
  (120s) would change its behaviour.
- **Have `lfs_push_argv` run get-url itself.** That makes it impure, and
  pr_endgame would lose the `runner` visibility the intent requires.
- **Add git-lfs to the image, or cover the askpass sites now.** Deferred by
  Q1 and Q4: it would be dead code in the Job pod, and #1688 reworks
  `authed_push_url` anyway.

## Risks

1. **Timeout budget (your call).** The worst case runs get-url, then lfs, then
   push, each with its own timeout: up to about 2x60s on the pr.py route and
   about 2x120s elsewhere. That exceeds the intent's "within existing push
   timeouts" constraint. Only LFS repos with large uploads come near it, and
   only on hosts with git-lfs, so the kubejob and grace paths (#1669, #1670,
   #1677) are unaffected. The choice is to accept it and document it, or to
   shrink the push timeout.
2. **Scan width when remote-tracking refs are missing (not verified).** If the
   earlier fetch failed, `lfs push` scans more history. The batch API skips
   objects the server already has. We have not checked whether old pointers
   whose objects are absent locally fail under `allowincompletepush=false`
   even when the server has them. **Test this before the plan is
   approved.** If they do fail, a PR on a repo with LFS history and a failed
   fetch would be blocked.
3. **Lock protection.** With `locksverify` pinned off, AIFactory never honours
   GitHub LFS locks. This is the same as today, but it is now a stated
   choice.
4. **Pushes that used to "succeed" can now fail.** An LFS project whose
   objects can't be uploaded used to log success and now fails the push. Q2
   intends this, and the CHANGELOG says so.
5. **`url.<x>.insteadOf` and `lfs.customtransfer.*` stay live.** git-lfs
   applies `insteadOf` to the pinned URL. This is no weaker than today:
   `git push` applies the same rewrite, the gh helper only answers
   github.com, and github.com only offers the `basic` transfer. Both are
   recorded under #1689.
6. **Warning text at `pr_endgame:581`.** If the local ref is missing,
   `lfs push` now fails first ("Invalid ref argument") instead of `git push`.
   Both take the same keep-going path; only the warning text changes.
7. **Existing fake runners.** `test_pr_endgame.py:43-49` and
   `test_pr_endgame_conflict_loop.py:30-38` return `CmdResult(0, "", "")` for
   unknown commands. get-url therefore gives `""`, the helper returns `None`,
   and only one extra call is recorded. No test asserts exact `calls ==` or
   call indexes, and the needle "git push" does not match "lfs push".

## Verification

- **New `tests/test_lfs_push_argv.py`**, with `shutil.which` monkeypatched in
  `core.child_env`:
  1. It returns `None` when git-lfs is missing.
  2. It returns `None` and logs a warning for ssh, a non-github host,
     userinfo, `?`, `..`, an extra path segment, two lines, and an empty
     string.
  3. It returns the exact argv with `E=https://github.com/o/r.git/info/lfs`
     for `.../o/r`, `.../o/r.git` and `.../o/r.git/`.
  4. `push_with_lfs` with a fake runner, where get-url gives a github URL and
     lfs fails: no "git push" call happens, and the lfs result is returned.
  5. `push_with_lfs` where lfs succeeds: the calls run in the order get-url,
     lfs push, git push, and the push argv is unchanged.
  6. A real-git-lfs integration case (`skipif` git-lfs missing), in a repo
     whose `.lfsconfig` sets `lfs.url` and whose `.git/config` sets
     `lfs.pushurl`, `allowincompletepush=true` and `locksverify=true`:
     - `git <helper -c args> lfs env` shows `Endpoint=E`.
     - `lfs push origin HEAD` with no objects exits 0 with no credentials,
       so no network call was made.
- **Site tests:**
  - pr_endgame `:581` and `:1148`: when lfs fails, `:581` still runs
    `gh pr create` and `:1148` returns `merge_conflict_unresolved`.
  - `pr.py` and `completion_orchestration` with `subprocess.run` patched: an
    lfs failure gives `success: False` and `False`, and no push argv runs
    after it.
- **Existing suites:** `tests/test_no_unscrubbed_spawn.py`,
  `apps/web-server/tests/test_pr_endgame*.py`,
  `tests/test_pr_endgame_conflict_loop.py` and
  `tests/test_git_push_credential_helper.py`.
- **Manual check before merge:** on a host with git-lfs, push to a scratch
  github.com repo with one LFS-tracked `.bin`.
  - `GIT_TRACE=1` shows the request going to
    `github.com/<o>/<r>.git/info/lfs`, even when `.lfsconfig` and
    `.git/config` name another endpoint.
  - A fresh clone smudges the file cleanly.
  - Run the Risk 2 check on the same repo.
- **k8s:** with no git-lfs, `lfs_push_argv` returns `None`, so pushes behave
  as they do today apart from the one get-url spawn.
