# PR Endgame — auto-PR → Copilot review → merge → re-test

> Status: **opt-in, default OFF** · Added in v3.6.15 (#71 Phase 4) · Verified live 2026-06-10 (PR #10 on `aifactory-test`)

The PR endgame is the finish line of the closed PARR loop. On a **clean,
QA-passed build** AIFactory can open a pull request, request a GitHub Copilot
review, and — only on approval — merge it and re-run the tests. It is **off by
default** and degrades safely to a human-stop at every uncertain step.

## Feature flags

Toggle these **per project** from the Settings UI (Project Settings → General →
*Auto-open a PR* / *Auto-merge after Copilot approves*), or globally via env on
the web-server (`factory-gitops/apps/aifactory/manifests/manifests.yaml`). The
**per-project setting wins**; the env var is the fallback default. Both OFF by
default.

| Setting (UI) | Env / project `.aifactory/.env` | Default | Effect when on |
|---|---|---|---|
| Auto-open a PR | `AIFACTORY_AUTO_PR` | off | On a `COMPLETED` build: push the worktree branch, open a PR, request a Copilot review. Then **stop** for a human. |
| Auto-merge after Copilot approves | `AIFACTORY_AUTO_MERGE` | off | *Additionally*: merge + re-test **only after GitHub Copilot posts an APPROVED review**. |

`AIFACTORY_AUTO_MERGE` has no effect unless `AIFACTORY_AUTO_PR` is also on. The
UI toggle resolution is `is_auto_pr_enabled(project_path)` /
`is_auto_merge_enabled(project_path)` in `pr_endgame.py` (reads the project's
`.aifactory/.env`, then the global env).

### Configurable reviewer (`AIFACTORY_PR_REVIEWER`)

The merge gate's reviewer is selectable per-project (Settings → General → *Pre-merge
reviewer*), or via env. Values:

| Reviewer | How it gates | Credits |
|---|---|---|
| `aifactory` (default) | AIFactory's **own** review engine reviews the diff with the project's provider (Claude/Ollama) and the merge is gated on **its verdict** (read from `review_{pr}.json`). GitHub forbids self-approving the PR AIFactory opened, so we gate on the engine verdict, not a GitHub review event. | none beyond your Claude/Ollama usage |
| `copilot` | GitHub Copilot's review must be `APPROVED` (see below). | GitHub Copilot **code review** credits |
| `any` | Any `APPROVED` GitHub review (human or bot). | none |

`aifactory` is the default precisely because it needs no Copilot credits and rides
the same provider you build with (Claude now, Ollama later).

### Auto-feedback loop (changes-requested → fix → re-review → merge)

When the reviewer requests changes, the endgame doesn't just stop — it runs a
**bounded auto-fix loop** (`fix_fn`, ≤2 cycles): the findings are routed to the
QA-fixer (`apply_correction`), the fix is pushed to the PR branch, and the PR is
re-reviewed. It merges only once the re-review passes; after the cycle budget it
hands to a human (`needs_human_after_fixes`). A fixer failure stops immediately
(`fix_failed`). With no `fix_fn` wired, changes-requested is an immediate
human-stop. (The AIFactory reviewer's verdict vocabulary is `MergeVerdict`:
`ready_to_merge` ⇒ approve; `merge_with_changes`/`needs_revision`/`blocked` ⇒
changes; a non-empty `blockers` list also forces changes.)

### Copilot's review is a hard gate (it is not bypassed)

Auto-merge requires GitHub Copilot to have **actually reviewed and APPROVED** the
PR (`require_copilot=True`, the default). Copilot finds real problems, so its
verdict gates the merge:
- **Copilot APPROVED** → merge + re-test (when auto-merge is on).
- **Copilot CHANGES_REQUESTED** (or any reviewer) → human-stop, PR left open.
- **Copilot has not reviewed yet** → keep waiting; on timeout, human-stop. Never
  a blind merge, and never a merge on a *human-only* approval when
  `require_copilot` is set. A human approval alone does not satisfy the gate.

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

## The merger: landing work the endgame never reached

**User story.** *As the person reviewing the factory's output, I want every
finished build to reach me as a pull request, and each task's board status to
say what happened to that PR, so I never have to go looking in worktrees for
work that was done and then forgotten.*

The endgame above runs only on a clean, QA-approved build. Anything else used
to stop at "branch in a worktree". On the co-mount path the build commits
locally and never pushes, so the work was invisible. In September 2026 five
tasks sat for a week holding 2-10 real commits each (Factory#2586).

The merger (`apps/web-server/server/services/merger.py`) is the landing path for everything
else. For each task it:

1. **Looks up the branch's PR in every state.** An open PR is left alone. A
   merged or closed PR is a decision already made, so no second PR is opened.
2. **Measures the work where it actually is**, whichever of the local and
   origin branch contains the other. If the two have diverged, the task is
   skipped with `diverged` and nothing is force-pushed. If the branch cannot
   be found or fetched, the result is `unmeasurable`, never "empty".
3. **Pushes and opens a PR** if there is unmerged work and `AIFACTORY_AUTO_PR`
   is on. The PR body states what was and was not verified: it is a review
   surface, not a certificate.
4. **Makes the task's status follow the PR**, only for tasks in
   `human_review`. A status a person set is never overwritten.

| PR state | Task becomes |
|---|---|
| open (new or existing) | `human_review` / `awaiting_merge` ("PR Open") |
| merged | `done` |
| closed without merging | `human_review` / `pr_closed` ("PR Closed") |
| no work to land | `human_review` / `no_work` ("No Work Produced") |
| could not measure | unchanged |

**It never merges.** `AIFACTORY_AUTO_MERGE` is not read by the merger.

### When it runs

- **At the end of every completed build**, after the endgame. After an endgame
  PR it just records `awaiting_merge`. Failed builds are left to the sweep.
- **On a periodic sweep** (optional), which catches failed builds with real
  work, builds from before 3.6.82, and a build-end call that errored.
- **On demand:** `POST /api/maintenance/merger/run?dry_run=false` (member role).
  `GET /api/maintenance/merger` reports what it would do and opens nothing.

### Options

| Variable | Default (unset) | Effect |
|---|---|---|
| `AIFACTORY_AUTO_PR` | off | Unset: no PR is opened. A task that would need one is skipped as `auto_pr_disabled`, with no status write. Tasks that already have a PR (open, merged or closed) still get their status synced. |
| `AIFACTORY_MERGER_SWEEP` | off | Unset: no periodic sweep. Build-end landing still happens. |
| `AIFACTORY_MERGER_SWEEP_DRY_RUN` | `true` | Anything except exactly `false` is report-only: the tick is logged, and nothing is pushed, opened or written. |
| `AIFACTORY_MERGER_SWEEP_INTERVAL_S` | `900` | Seconds between ticks. Zero or non-numeric falls back to 900. |

Recommended rollout: enable the sweep report-only, read one `merger-sweep {...}`
log line, and flip `DRY_RUN` to `false` only when the report is what you
expect.

### Is the sweep alive?

The loop runs inside the web-server pod, not as a CronJob: the spec tree is on
a ReadWriteOnce volume, so a separate pod could see nothing and still report
success. `job-watchdog` therefore cannot see it. Check
`GET /api/maintenance/merger`: `last_tick_at` advances once per interval, and
`null` means it has never ticked.

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
