---
status: draft
issue: 2054
spec: spec/2026-11-16-2054-revert-pr-transcript-hardening.md
# coders: none. The steps are small and share one design; the lead implements them
# (a handoff costs more than it saves). Codex is not an implementer this round.
reviewers:
  - codex   # final diff review (agy is off by owner decision, 2026-11-16)
---

# Plan: revert PR after a post-merge regression, and transcript hardening

Approved decisions (see the spec for the reasoning):

- The merge record `<spec_dir>/pipeline_merge.json` (`owner, repo, pr, branch`)
  is a pointer only. Proof is re-checked on GitHub at revert time: PR MERGED,
  `headRefName == marker.branch == recorded_branch(project, spec)`,
  `mergeCommit.oid == failing_sha` (>= 7 chars prefix allowed).
- `open_revert_pr` runs under a process lock, is idempotent (stored URL, then a
  GitHub search for `Reverts <owner>/<repo>#<pr>` in PR bodies), creates the PR
  with the `revertPullRequest` GraphQL mutation via `gh api graphql`, and never
  merges. Body: that phrase, failing run URL, transcript URIs with the
  "may belong to an earlier run" label.
- Route `POST /api/tasks/{task_id}/post-merge-regression`, role `member`, body
  `{merge_sha, run_url}`; URIs from `transcriptUris` in the task's
  `task_metadata.json`; blocking work in a thread under
  `org_github_token(org, pinned=True)` after `warm_org_token`.
- QA fallback loop records tool start/end; `transcript._clean` redacts
  `sk-ant-[A-Za-z0-9_-]{10,}` before `redact`.
- Deferred to a new issue: run id in key and event, spec-creation Jobs,
  streaming upload, hub `secrets` table, RFC-0004 status, the TFactory caller.

## Steps

1. `apps/web-server/server/services/revert_pr.py` (new),
   `apps/web-server/tests/test_revert_pr.py` (new): `record_pipeline_merge`,
   `open_revert_pr` as above, with a stub runner. Tests: merge marker written;
   revert created for the right sha (argv has `graphql`, body holds run URL,
   phrase and URIs); wrong sha, wrong branch, unmerged, no marker refused; second
   call returns `exists` without a second create; GitHub-search hit reconciled
   without create; no argv contains `merge`.
   -> `cd apps/web-server && python -m pytest tests/test_revert_pr.py`
   Traps: runner signature is `runner(argv, cwd) -> CmdResult`; no secrets in logs
   (use `sanitize_log`).
2. `apps/web-server/server/services/pr_endgame.py` (~2268, `run_pr_endgame`):
   pass `on_approved_merged` a wrapper that calls `record_pipeline_merge` (in its
   own try) then `re_test`. Test in `tests/test_pr_endgame.py`: marker written
   and `re_test` still called, also when recording raises.
   -> `python -m pytest tests/test_pr_endgame.py -k merged`
3. `apps/web-server/server/routes/tasks_regression.py` (new), mounted in
   `routes/tasks.py` after `tasks_logs`; `tests/test_post_merge_regression_route.py`
   (new). Tests: happy path with a patched `open_revert_pr`, bad `run_url`, bad
   task id, unknown project.
   -> `python -m pytest tests/test_post_merge_regression_route.py`
   Traps: validate `spec_id` with `safe_spec_component` before any path join.
4. `apps/backend/qa/reviewer.py` (`_run_tool_fallback` receive loop): record
   `ToolUseBlock` and `ToolResultBlock` via the task logger; test in
   `apps/backend/qa/test_reviewer_fallback_transcript.py` (new) using a fake
   provider.
   -> `cd apps/backend && python -m pytest qa/test_reviewer_fallback_transcript.py`
5. `apps/backend/task_logger/transcript.py` (`_clean`) and
   `task_logger/test_transcript.py`: `sk-ant-` redaction test.
   -> `python -m pytest task_logger/test_transcript.py`

## Tests

The five commands above, then `ruff check` and `ruff format --check` on the
touched files (AIFactory trap: plain names in parenthesised imports), and the
CI workflow's own test jobs on the PR.

## Rollback

Revert the PR. The route and service are new files; the endgame wrapper only
adds a best-effort marker write; no data migration.
