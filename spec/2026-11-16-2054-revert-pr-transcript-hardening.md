---
status: draft
issue: 2054
intent: intent/2026-11-16-2054-revert-pr-transcript-hardening.md
---

# Spec: revert PR after a post-merge regression, and transcript hardening

## Design

1. **Record the merge.** `run_pr_endgame` wraps `re_test` as the
   `on_approved_merged` hook: first `revert_pr.record_pipeline_merge(spec_dir,
   owner, repo, pr, branch, runner)`, then the existing `re_test`. It writes
   `<spec_dir>/pipeline_merge.json` (`owner, repo, pr, branch`) and never raises;
   a failure must not skip `re_test`. The marker is only a pointer: project
   members can write `.aifactory` files, so it is NOT trusted as proof. Proof is
   re-checked on GitHub at revert time (next point).
2. **Open the revert.** New `server/services/revert_pr.py::open_revert_pr(
   spec_dir, expected_branch, failing_sha, run_url, transcript_uris, runner)`.
   Under a process-wide lock it:
   - refuses (`{"ok": False, "reason": ...}`) without a marker, or when
     `gh pr view <pr> --repo <owner>/<repo> --json id,state,title,headRefName,
     mergeCommit` does not show state MERGED, `headRefName == marker.branch ==
     expected_branch` (the branch the control plane recorded at dispatch, via
     `task_branch.recorded_branch`) and `mergeCommit.oid == failing_sha`
     (full sha, or a prefix of at least 7 chars). The repo is the project's
     own; a forged marker cannot name a PR from another branch;
   - is idempotent: returns the stored `revert_pr_url` (`reason: "exists"`), and
     before creating, reconciles with GitHub by listing PRs whose body holds the
     exact phrase `Reverts <owner>/<repo>#<pr>`, so a crash after creation and
     before the local write does not duplicate. Search indexing lag and several
     control-plane replicas are not covered (stated in the PR);
   - creates the PR with GitHub's `revertPullRequest` GraphQL mutation via
     `gh api graphql` (no clone), title `Revert "<pr title>"`, body: the phrase
     above, the failing run URL, and the transcript URIs under the label
     "transcripts recorded for this task; the key has no run id yet, so one may
     belong to an earlier run";
   - stores `revert_pr_url` in the marker. No code path merges it; the body says
     a person must approve it.
3. **Trigger (AIFactory half).** `POST /api/tasks/{task_id}/post-merge-regression`
   (`routes/tasks_regression.py`, mounted in `routes/tasks.py` like
   `tasks_logs`), `require_task_access("member")`, body `{merge_sha, run_url}`
   (`run_url` must be `https://`, both length-capped). It validates ids the way
   `tasks_logs` does, takes the transcript URIs from `transcriptUris` in the
   task's own `task_metadata.json` (recorded at completion), and runs
   `open_revert_pr` in a thread under
   `github_app.org_github_token(org, pinned=True)` after `warm_org_token`.
   No TFactory result feed exists in AIFactory, so the TFactory-side caller
   (call this endpoint when a post-merge regression run fails) is tracked in the
   follow-up issue; the intent outcome is delivered as the endpoint plus the
   merge record, and the PR says so. `transcriptUris` come from a `spec_id`
   listing, so on a repeated `spec_id` (#1972) the strings may name another
   project's objects (names only, never content); the run/project id in the key
   is in the follow-up issue.
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
- A `member` can call the route, but only a MERGED PR on the task's recorded
  branch whose merge commit equals the supplied sha qualifies.

## Verification

Stubbed-runner tests (no network): marker written; revert opened for the
recorded sha; wrong sha, no marker, repeat call idempotent, a PR from another branch refused, remote-created but local-write-lost case reconciled; body
holds run URL and transcripts; no `merge` argv ever issued; route happy path and
validation; endgame hook records the marker and still calls `re_test`; QA
fallback records tool start and end; `sk-ant-` redacted. `ruff` and the
web-server and backend test files touched.
