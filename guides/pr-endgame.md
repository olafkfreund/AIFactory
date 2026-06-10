# PR Endgame — auto-PR → Copilot review → merge → re-test

> Status: **opt-in, default OFF** · Added in v3.6.15 (#71 Phase 4) · Verified live 2026-06-10 (PR #10 on `aifactory-test`)

The PR endgame is the finish line of the closed PARR loop. On a **clean,
QA-passed build** AIFactory can open a pull request, request a GitHub Copilot
review, and — only on approval — merge it and re-run the tests. It is **off by
default** and degrades safely to a human-stop at every uncertain step.

## Feature flags

Both are environment variables on the AIFactory web-server (set in
`factory-gitops/apps/aifactory/manifests/manifests.yaml`). Both default OFF.

| Flag | Default | Effect when `true` |
|---|---|---|
| `AIFACTORY_AUTO_PR` | off | On a `COMPLETED` build: push the worktree branch, open a PR, request a Copilot review. Then **stop** for a human. |
| `AIFACTORY_AUTO_MERGE` | off | *Additionally*: when Copilot's review is **APPROVED**, squash-merge the PR and re-run TFactory against the merged result. |

`AIFACTORY_AUTO_MERGE` has no effect unless `AIFACTORY_AUTO_PR` is also on.

## Flow

```
build COMPLETED (clean)
  └─ gather_pr_context  (worktree branch + repo; skip if missing)
     └─ create_pr        gh auth setup-git → git push → gh pr create
        └─ request_copilot_review   (best-effort)
           └─ watch_and_finish  (poll the review verdict, bounded ~20 min)
              ├─ APPROVED + AIFACTORY_AUTO_MERGE → merge (squash) → re-test (TFactory)
              ├─ CHANGES_REQUESTED        → human-stop (PR left open)
              ├─ review timeout           → human-stop (PR left open)
              └─ merge conflict / no repo → human-stop (PR left open)
```

Code: `apps/web-server/server/services/pr_endgame.py`, wired into the completion
hook in `agent_service.py` (the terminal `COMPLETED` branch), guarded by a
fire-once `.terminal_side_effects_done` marker in the spec dir.

## Safety properties

- **Default OFF** — inert until a flag is explicitly set.
- **Human-stop is the default outcome** — `CHANGES_REQUESTED`, a review timeout,
  a merge conflict, or a missing repo all leave the PR open for a person.
  `CHANGES_REQUESTED` dominates `APPROVED`.
- **Never force-merges** — `gh pr merge` is only called on a clean `APPROVED`
  verdict and only when `AIFACTORY_AUTO_MERGE` is on.
- **Fire-once** — the `.terminal_side_effects_done` marker prevents duplicate
  PRs across the two completion call paths.
- **Best-effort** — any failure is logged and never blocks task completion.
- Unit-tested in `apps/web-server/tests/test_pr_endgame.py` (16 tests: flag
  gating, verdict parsing, merge-only-on-approved, timeout/human-stop, full
  chain, repo resolution).

## Prerequisites

1. **A pushable GitHub repo** for the project (resolved from `requirements.json`
   `githubIssue.repo`/`github_repo`, else the worktree's `origin` remote).
2. **gh authenticated in the pod** (`GITHUB_TOKEN`). `create_pr` runs
   `gh auth setup-git` so the raw `git push` can authenticate.
3. **GitHub Copilot _code review_ enabled for the repo/org** — see the known
   limitation below.

## Known limitation — Copilot code review must be enabled

`request_copilot_review` issues the documented request and GitHub accepts it
(HTTP 200), but **Copilot only posts a review if Copilot *code review* is enabled
for that repository/organization** (a GitHub plan + setting, not controlled by
AIFactory). When it is not active, no review is produced; the watcher then polls
`pending` until it times out and hands the PR to a human (the safe fallback).

This means the **review → auto-merge** leg cannot run end-to-end until Copilot
code review is actually active on the target repo. The auto-PR + human-stop legs
work regardless. Verified live: task 014 opened PR #10 on `aifactory-test`
(open, not merged) — but no Copilot review appeared, confirming this dependency.

To enable Copilot code review: GitHub repo/org **Settings → Copilot → Code
review** (requires a qualifying Copilot plan). Once active, re-run a clean build
with `AIFACTORY_AUTO_PR=true` (and `AIFACTORY_AUTO_MERGE=true` to close the loop).

## Operational note — deploy downtime

AIFactory currently runs as a **single replica with a `Recreate` deploy
strategy**, so each deploy has a ~1-2 minute window where the portal returns 502
while the new pod pulls its image and runs init containers. Toggling these flags
is a deploy and incurs that window. Switching to `RollingUpdate` with
`maxUnavailable: 0` (and ≥2 replicas) would make it zero-downtime.
