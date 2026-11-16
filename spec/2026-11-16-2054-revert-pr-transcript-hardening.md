---
status: draft
issue: 2054
intent: intent/2026-11-16-2054-revert-pr-transcript-hardening.md
---

# Spec: revert PR after a post-merge regression, and transcript hardening

## Design

1. **Record the merge.** `run_pr_endgame` wraps `re_test` as the
   `on_approved_merged` hook: first `revert_pr.record_pipeline_merge(spec_dir,
   owner, repo, pr, runner)`, then the existing `re_test`. It runs
   `gh pr view <pr> --json id,url,baseRefName,mergeCommit` and writes
   `<spec_dir>/pipeline_merge.json` (`owner, repo, pr, pr_node_id, pr_url, base,
   merge_sha`). Best-effort, never raises, and a failure here must not skip
   `re_test`. The file is the proof that the pipeline merged this commit.
2. **Open the revert.** New `server/services/revert_pr.py::open_revert_pr(
   spec_dir, failing_sha, run_url, transcript_uris, runner)`:
   - refuses (`{"ok": False, "reason": ...}`) if there is no marker, if
     `failing_sha` is not the recorded `merge_sha` (or an unambiguous prefix of at
     least 7 chars), or if the marker already holds `revert_pr_url`
     (idempotent: returns that URL, `reason: "exists"`);
   - otherwise calls GitHub's `revertPullRequest` GraphQL mutation through
     `gh api graphql` (no clone, no local git), with a title
     `Revert "<pr title>"` and a body naming the failing run URL, the merged PR,
     and the transcript URIs labelled "transcripts stored for this task (the
     key has no run id yet, so one may belong to an earlier run)";
   - stores `revert_pr_url` in the marker. No code path merges it; the body says
     a person must approve it.
3. **Trigger.** `POST /api/tasks/{task_id}/post-merge-regression`
   (`routes/tasks_regression.py`, mounted in `routes/tasks.py` like
   `tasks_logs`), `require_task_access("member")`, body `{merge_sha, run_url}`
   (`run_url` must be `https://`, both length-capped). It resolves the spec dir
   the same way `tasks_logs` does, reads transcript URIs with
   `workspace_fetch.transcript_uris(spec_id)` in a thread, runs
   `open_revert_pr` under the org GitHub token (as the endgame does) and
   returns the result. TFactory (or an operator) calls it when a regression run
   fails; TFactory-side wiring is out of scope.
4. **QA fallback capture.** In `qa/reviewer.py::_run_tool_fallback`, the
   receive loop calls `task_logger.transcript_tool_start` on `ToolUseBlock` and
   `transcript_tool_end` on `ToolResultBlock` (as the primary loop does),
   guarded by `if task_logger`.
5. **Local redaction.** `task_logger/transcript.py::_clean` also replaces
   `sk-ant-[A-Za-z0-9_-]{10,}` (before `redact`). The hub table change stays in
   Factory.
6. **Deferred to a new issue:** run id in the key and event, spec-creation Jobs,
   streaming upload, the hub `secrets` table, RFC-0004 status.

## Alternatives rejected

- A clone + `git revert` + push: needs a worktree, credentials and conflict
  handling for no gain over GitHub's own revert mutation.
- Auto-detecting regressions by polling TFactory: no TFactory result feed exists
  in AIFactory today; an explicit authenticated call is the smallest contract.
- Creating the revert as a draft: reviewers cannot approve a draft; the PR is
  normal and simply never merged by us.

## Risks

- `revertPullRequest` fails on a conflicting revert: returned as `ok: False`,
  nothing else changes; a person reverts by hand.
- A stale transcript could be listed: labelled in the body.
- The route trusts `member` role callers; only a pipeline-merged sha qualifies,
  so it cannot revert arbitrary PRs.

## Verification

Stubbed-runner tests (no network): marker written; revert opened for the
recorded sha; wrong sha, no marker, repeat call refused or idempotent; body
holds run URL and transcripts; no `merge` argv ever issued; route happy path and
validation; endgame hook records the marker and still calls `re_test`; QA
fallback records tool start and end; `sk-ant-` redacted. `ruff` and the
web-server and backend test files touched.
