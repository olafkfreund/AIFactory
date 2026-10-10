---
status: draft
issue: 1690
intent: intent/2026-10-10-1690-lfs-push-hooks-off.md
---

# Spec: push LFS objects while git hooks stay off

## Design

Before each of the 4 web-server pushes, run an explicit `git lfs push` that
can only reach the github.com remote's own LFS endpoint and cannot start a
repo-named transfer program. If git-lfs is missing, skip it. If the upload
fails or is refused, treat it as a push failure. Hooks stay off. The work is
one pure argv helper, one small wrapper in `pr_endgame.py` shared by 3 sites,
and the same sequence inline in `pr.py`. There is no new module, no
Dockerfile change and no new env name.

### Proposed answers to the intent's open questions

These are the defaults this spec proposes. **You confirm them at this spec
gate.** None of them is approved yet.

1. **Q1. git-lfs in the production image:** not in this change. Leave the
   `Dockerfile` alone. Open a follow-up issue for adding git-lfs to the image
   (with a `>=` floor) and for the 2 askpass pushes. Where git-lfs is
   missing, no LFS step runs, so k8s behaves as it does today.
   *Why:* this is the smallest change, adds no image size or package upkeep,
   and creates no new failure mode in k8s. `Dockerfile:175-187` installs git,
   gh and curl but no git-lfs, and prod already commits LFS files as plain
   blobs (intent, Problem para 4).
2. **Q2. Upload failure:** treat it as a push failure. The LFS upload runs
   before `git push`. On a non-zero exit, or when the LFS step is refused
   (point 7 below), the ref push is skipped and the site takes its existing
   push-failure path. Pointers are never pushed after a failed upload.
   *Why:* pushing pointers to objects the server lacks breaks the next
   checkout, and logging that as success is the bug itself. Reusing each
   site's failure path adds no new states (`pr.py:343-347`,
   `completion_orchestration.py:412-416`, `pr_endgame.py:582-584` and
   `:1153-1160`). At `pr_endgame.py:581` the existing path is a warning and
   keep going to `gh pr create`; that stays, but the ref is not pushed.
   Repos without LFS cannot reach this path, because the pinned
   `locksverify=false` (below) makes their upload a no-op with no network
   call that exits 0. Tested here on git-lfs 3.8.0.
3. **Q3. LFS endpoint:** only the GitHub remote's own endpoint,
   `E = https://github.com/<owner>/<repo>.git/info/lfs`, derived from
   origin's push URL and pinned on the command line as both `lfs.url` and
   `lfs.pushurl`. If origin is not exactly one `https://github.com/<owner>/<repo>`
   URL (optional `.git`, optional trailing `/`), skip the LFS step with a
   warning and let the push run as today. Custom LFS servers are not
   supported.
   *Why:* the token only reaches the host the push already authenticates, and
   the `-c` values hold no secret (#1366). A test on the proposal showed
   command-line `lfs.url` plus `lfs.pushurl` override `.lfsconfig` `lfs.url`,
   `.git/config` `lfs.pushurl` and `remote.origin.lfspushurl` together. With
   only `lfs.url` pinned, the repo's pushurl still won, so both are pinned.
   This is the same host rule as `git_credentials.py:37`
   (`GITHUB_HTTPS_PREFIX`), applied as a whole-string match instead of a
   prefix.
4. **Q4. Scope:** only the 4 web-server sites that use the gh credential
   helper: `pr.py:335-355`, `completion_orchestration.py:404-416`,
   `pr_endgame.py:581` and `:1148-1152`. The 2 askpass sites,
   `workspace_fetch.py:135-144` (build Job) and `tfactory_client.py:395-406`,
   go to the follow-up issue.
   *Why:* the Job pod has no git-lfs, so changing `workspace_fetch` now would
   be dead code. Both askpass sites use `authed_push_url`
   (`git_credentials.py:57-99`), which #1688 is about to change, so changing
   them now would mean doing the work twice.
5. **Q5. Trigger:** run the upload whenever `shutil.which("git-lfs")` finds
   the binary. Do not read `.gitattributes`.
   *Why:* the agent controls `.gitattributes`, and it only reflects HEAD, so
   it misses pointers committed under an earlier version of the file.
   `git lfs push` scans the pushed commits itself. `shutil.which` never
   raises.
   **One deviation for you to confirm:** `git remote get-url` runs before
   every push, including on hosts without git-lfs, because the URL is the
   helper's input. That is one local config read with no network. It keeps
   the `runner` call list the same on every host, so fake-runner tests do not
   depend on whether the dev box has git-lfs. In `pr.py` it sits inside the
   existing `try`, so a timeout or `OSError` there (not expected for a local
   read) would surface as a push failure. If you want zero extra spawns in
   k8s, gate get-url on `shutil.which` at each site instead.
6. **Timeout budget (deviation from an intent constraint, needs your
   approval).** Each spawn keeps its site's own timeout: 60s in `pr.py`,
   120s through `_default_runner` (`pr_endgame.py:67-76`). An LFS repo with
   a large upload can therefore take up to about 2x the old push time. That
   does not meet the intent's "LFS uploads stay within the existing push
   timeouts". All 4 sites are web-server PR paths, so kubejob dispatch and
   the grace paths (#1669, #1670, #1677) are not delayed. Repos without LFS
   finish the upload step at once.
   *Why not meet it literally:* one shared deadline across upload and push
   needs a timeout argument on `Runner`, which changes its signature and
   every fake runner. If you want the constraint met as written, say so and
   the plan adds that instead.
7. **Transfer programs (finding, see below):** the LFS step is refused when
   the repo's effective git config defines any `lfs.customtransfer.*` key,
   read with `git config --name-only --get-regexp '^lfs\.customtransfer\.'`
   in the worktree just before the upload. Exit 1 (no match) lets the upload
   run. A match or any other exit refuses it, which is a push failure under
   Q2. This read only runs on hosts with git-lfs and a github.com origin.
   `allowincompletepush=false` is pinned too, so a missing object fails the
   upload instead of warning.
8. **Where the code lives:** the pure helper (git-lfs present and origin URL
   in, `git lfs push` argv or nothing out) goes in
   `apps/backend/core/child_env.py` and is re-exported from
   `server/utils/subprocess_env.py`. The wrapper (get-url, helper,
   transfer-program check, upload, push) goes in `pr_endgame.py` and goes
   through the injectable `runner`, so the intent's "in pr_endgame the LFS
   step goes through `runner`" holds. `completion_orchestration.py` uses that
   wrapper with `_default_runner` in place of its bare `_sp.run` push (same
   120s timeout and env). `pr.py` repeats the sequence inline to keep its 60s
   timeout and its exception mapping at `:349-355`.
   *Why:* the web server already imports `core.child_env` through
   `subprocess_env.py:22-28`, so no new module and no `sys.path` change is
   needed. `child_env.py` cannot import `git_credentials` (that would be
   circular: `git_credentials.py:33` imports `core.child_env`). Every spawn
   reuses `child_env(keep=GITHUB_KEEP)`, so `test_no_unscrubbed_spawn.py`
   still passes and no new `*_KEY`/`*_TOKEN` names reach children.

### Finding: lock verification

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

### Finding: a `-c` pin cannot stop a standalone transfer agent

An earlier draft pinned `-c lfs.standalonetransferagent=` to stop an
agent-named transfer program. Tested here on git-lfs 3.8.0 with a repo-local
`lfs.customtransfer.evil.path` pointing at a script: the global pin holds
against a repo-level `lfs.standalonetransferagent`, but git-lfs also reads the
URL-scoped `lfs.<url>.standalonetransferagent` and prefers the more specific
key. Pinning `lfs.<E>.` as well still lost to repo keys for `<E>/`, `<E>//`
and `<E>/?`, and the script ran each time. The URL variants cannot be
enumerated, so the spec drops the pin and refuses the step instead whenever
any `lfs.customtransfer.*` is defined (Q answer 7): without a definition, no
agent name resolves to a program. `.lfsconfig` cannot define one: the same
keys placed there did not run the script, because git-lfs reads only a safe
list of keys from that file.

### URL source

The helper takes the output of `git remote get-url --push --all origin`.

- `--push` returns the URL the ref actually goes to, with `pushInsteadOf`
  applied. An agent-set `url.https://evil.example/.pushInsteadOf` shows up as
  the evil URL, which the whole-string match rejects.
- `--all` prints several lines when origin has several push URLs. The
  whole-string match rejects that too.
- Userinfo (`https://x:tok@github.com/...`), `?`, `#`, extra path segments,
  `.`/`..` as the repo name, other hosts and ssh URLs are all rejected. A URL
  with a token in it is skipped, so nothing leaks.
- If get-url exits non-zero, there is no LFS step, and the push runs and
  fails on its own path as it does today.
- The ref passed to `lfs push` is the same ref the site pushes (`HEAD` or a
  branch name git has already validated), so it cannot start with `-`.

### Docs and follow-ups

- `CHANGELOG.md:15`: replace "not uploaded on these pushes yet (#1690)" with
  a line saying LFS objects are now uploaded explicitly on the 4 server PR
  pushes where git-lfs is installed, that a failed upload now fails the push,
  and that the build-Job push, the TFactory push and git-lfs in the image
  are still pending (follow-up issue).
- `plan/2026-10-09-1680-child-process-env.md:254`: the same edit.
- Open the follow-up issue: git-lfs in the image with a `>=` floor,
  `workspace_fetch.py:135-144` and `tfactory_client.py:395-406`.
- Add to #1689's list: `lfs.<url>.access`, `lfs.transfer.*`, `http.<url>.*`
  (proxy, extraheader) and `url.*.insteadOf`. The LFS spawn honours these the
  same way the push does. `lfs.customtransfer.*` is handled here (Q answer 7),
  not deferred.

**Scope of change:** 5 code files (`child_env.py`, `subprocess_env.py`,
`pr_endgame.py`, `completion_orchestration.py`, `pr.py`), 2 doc lines and
1 new test file. The `Dockerfile`, `workspace_fetch.py`, `tfactory_client.py`
and `git_credentials.py` are not touched.

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
- **Pin `lfs.standalonetransferagent=` (and URL variants) with `-c`.**
  Bypassed by URL-scoped repo keys (tested, see the finding above).
- **Prefix match with `GITHUB_HTTPS_PREFIX`.** It accepts
  `https://github.com/o/r/../x` and multi-line output, and importing it would
  be circular.
- **`git remote get-url origin` without `--push`.** That is the fetch URL,
  which can differ from where the ref goes, and it skips `pushInsteadOf`.
- **One wrapper for all 4 sites, including `pr.py`.** `pr.py` uses a 60s
  timeout and its own exception mapping. Moving it to `_default_runner`
  (120s) would change its behaviour.
- **Have the helper run get-url itself.** That makes it impure, and
  pr_endgame would lose the `runner` visibility the intent requires.
- **Add git-lfs to the image, or cover the askpass sites now.** Deferred by
  Q1 and Q4: it would be dead code in the Job pod, and #1688 reworks
  `authed_push_url` anyway.

## Risks

1. **Timeout budget.** See Q answer 6. Worst case about 2x60s on the
   `pr.py` route and 2x120s elsewhere, only for LFS repos with large uploads
   on hosts with git-lfs.
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
   objects can't be uploaded used to log success and now fails the push. A
   repo that defines `lfs.customtransfer.*` in its git config now fails the
   push on a host with git-lfs, even with no LFS objects. Q2 intends this,
   and the CHANGELOG says so.
5. **`url.<x>.insteadOf` stays live.** git-lfs applies `insteadOf` to the
   pinned URL. This is no weaker than today: `git push` applies the same
   rewrite, and the gh helper only answers github.com. Recorded under #1689.
   The push itself still honours other agent-set command config
   (`credential.helper`, `core.sshCommand`, ...); that is #1689's scope, not
   widened here.
6. **Warning text at `pr_endgame.py:581`.** If the local ref is missing,
   `lfs push` now fails first ("Invalid ref argument") instead of
   `git push`. Both take the same keep-going path; only the warning text
   changes.
7. **Existing fake runners.** `FakeRunner` (`apps/web-server/tests/test_pr_endgame.py:36-49`)
   and `SeqRunner` (`tests/test_pr_endgame_conflict_loop.py:30-42`) return
   success with empty output for unknown commands. get-url therefore gives
   `""`, no LFS step runs, and one extra call is recorded. Their `"git push"`
   markers do not match `git remote get-url --push` or `git lfs push`. One
   test does use a call index: `test_pr_endgame.py:118-124` finds the first
   call containing `"push"`, which will now be the get-url call
   (`--push`). It still passes because get-url runs after the fetch, but the
   plan should tighten that needle to `"git push"`.

## Verification

- **New unit tests**, with `shutil.which` monkeypatched in `core.child_env`:
  1. The helper returns nothing when git-lfs is missing.
  2. It returns nothing and logs a warning for ssh, a non-github host,
     userinfo, `?`, `..`, an extra path segment, two lines, and an empty
     string.
  3. It returns the exact argv with `E=https://github.com/o/r.git/info/lfs`
     for `.../o/r`, `.../o/r.git` and `.../o/r.git/`, and that argv contains
     no secret.
  4. The wrapper with a fake runner, where get-url gives a github URL and the
     upload fails: no `git push` call happens, and the upload's result is
     returned.
  5. The wrapper where the transfer-program check matches: no upload and no
     `git push` call, and a failure is returned.
  6. The wrapper where all succeed: calls run in the order get-url, config
     check, `lfs push`, `git push`, and the push argv is unchanged.
  7. A real-git-lfs integration case (`skipif` git-lfs missing), in a repo
     whose `.lfsconfig` sets `lfs.url` and whose `.git/config` sets
     `lfs.pushurl`, `allowincompletepush=true` and `locksverify=true`:
     - `git <helper -c args> lfs env` shows `Endpoint=E`.
     - `lfs push origin HEAD` with no objects exits 0 with no credentials,
       so no network call was made.
     - With `lfs.customtransfer.evil.path` and `lfs.<E>/.standalonetransferagent=evil`
       in `.git/config`, the wrapper refuses the step and the script never
       runs.
- **Site tests:**
  - `pr_endgame.py:581` and `:1148`: when the upload fails, `:581` still
    runs `gh pr create` and `:1148` returns `merge_conflict_unresolved`.
  - `pr.py` and `completion_orchestration.py` with `subprocess.run` patched:
    an upload failure gives `success: False` and `False`, and no push argv
    runs after it.
- **Existing suites pass unchanged:** `tests/test_no_unscrubbed_spawn.py`,
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
- **k8s / default installs:** with no git-lfs, no LFS step runs, so pushes
  behave as they do today apart from the one local get-url spawn. On a host
  with git-lfs and a repo without LFS, the upload exits 0 with no network
  call and the push result is unchanged.
