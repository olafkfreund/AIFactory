---
status: draft
issue: 1690
author: olafkfreund
---

# Intent: push LFS objects while git hooks stay off

## Problem

#1680 (PR #1691, merged) adds `core.hooksPath=/dev/null` to every env that
`child_env()` builds (`apps/backend/core/child_env.py:53-57`). git-lfs
uploads objects only from its `pre-push` hook. With hooks off, a push sends
LFS pointers and no objects. The push still exits 0, so AIFactory logs
success.

The PR branch on GitHub, or TFactory's `aifactory/<spec>` branch, then holds
pointers to objects that do not exist on the server. The first person or CI
job to check it out gets a smudge error ("Object does not exist on the
server") or a pointer file. This was reproduced on this checkout with
git-lfs 3.8.0: a normal push uploaded 1 object, and the same push with the
`child_env()` hooksPath setting uploaded 0.

Only projects that track files with `filter=lfs`, and only when the agent
adds or changes such a file, are hit. The #1680 plan
(`plan/2026-10-09-1680-child-process-env.md:253`) and `CHANGELOG.md:14`
record this as a known limit with this issue as the follow-up.

In production the bug does not show up as written, because the image
(`Dockerfile:175-183`) and the build Job (same image) have no git-lfs at
all. There, LFS-tracked files are committed as plain blobs instead. Both
cases mean LFS does not work for AIFactory-built branches in k8s.

## Proposed outcome

- When a project uses LFS, every network push AIFactory makes uploads the
  LFS objects before the ref, so the pushed branch checks out cleanly.
- If the upload fails, the push is reported as failed, not as success.
- Repos without LFS behave exactly as today: no new subprocess, no new log
  lines, no new failure modes.
- Hooks stay off.
- `CHANGELOG.md:14` and the #1680 plan note no longer call this a known
  limit.

## Affected users and systems

- Users whose projects track files with git-lfs.
- The 7 network push sites, all on the `child_env()` env:
  - `routes/pr.py:325-343`
  - `services/completion_orchestration.py:404-411`
  - `services/pr_endgame.py:581` and `:1148-1152`, through the injectable
    `runner`
  - `core/workspace_fetch.py:135-144`, in the build Job
  - `pfactory/tfactory_client.py:395-406`, the TFactory build-branch push
  - the runner env (`utils/subprocess_env.py:52-73`), which only matters
    through the workspace_fetch push
- `apps/backend/core/git_credentials.py` (askpass) and `child_env.py`.
- The production image (`Dockerfile`), used by web-server replicas and
  build Job pods.
- TFactory, which checks out `aifactory/<spec>`.

## Constraints

- Hooks stay off (user decision in #1680). The upload must be an explicit
  call, never a re-enabled `pre-push` hook.
- The repo is written by the agent, so it is untrusted. That covers
  `.gitattributes`, `.lfsconfig`, `lfs.url`/`lfs.pushurl`/`remote.*.lfsurl`,
  and `lfs.customtransfer.*` / `lfs.standalonetransferagent`. The askpass
  script answers any host, so the token must never go to an LFS endpoint
  other than the github.com remote `authed_push_url` checked, and no
  agent-named program may run.
- Every new spawn uses the scrubbed env of the push it belongs to. The
  token goes through askpass, never argv (#1366). New spawns pass
  `tests/test_no_unscrubbed_spawn.py`. No new `*_KEY`/`*_TOKEN` names reach
  children.
- The git-lfs presence check never raises. A missing binary means skip.
- Existing push flags and order stay as they are (`--force-with-lease`,
  `-u`, `HEAD:<branch>`, `--force <sha>:refs/...`).
- In pr_endgame the LFS step goes through `runner`, so tests can see it.
- No shared state across replicas or Job pods. LFS uploads stay within the
  existing push timeouts and do not delay kubejob dispatch or grace paths
  (#1669, #1670, #1677) beyond what the push already takes.
- Any image package follows the Dockerfile `>=` floor rule against the
  pinned Wolfi digest.
- Credential plumbing reuses the helper that #1688 will change. The LFS
  config keys above are either pinned here or recorded under #1689, not
  left in neither.

## Open questions

1. **git-lfs in the production image.** Add it, so LFS works in k8s (a
   bigger image and one more package to maintain)? Or support LFS only where
   git-lfs is already installed, which means none in k8s?
2. **Upload failure.** Fail the push and the PR or handback? Or warn and
   push the pointers anyway, which leaves missing objects on the remote?
3. **LFS endpoint.** Allow only the github.com remote's own LFS endpoint and
   ignore `.lfsconfig`/`lfs.url`? Or honour a custom LFS server, which needs
   its own credential rule?
4. **Scope.** All 7 push sites, including the TFactory build-branch push,
   or only the server-side PR pushes for now?
5. **Trigger.** Run the upload when `.gitattributes` contains `filter=lfs`
   (the issue's proposal, which the agent controls)? Or always when git-lfs
   is installed (simpler, one extra spawn per push)?
