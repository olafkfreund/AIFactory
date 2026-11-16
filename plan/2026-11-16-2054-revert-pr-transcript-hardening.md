---
status: approved
issue: 2054
spec: spec/2026-11-16-2054-revert-pr-transcript-hardening.md
# coders: none. The steps are small and share one design; the lead implements them
# (a handoff costs more than it saves). Codex is not an implementer this round.
reviewers:
  - codex   # final diff review (agy is off by owner decision, 2026-11-16)
---

# Plan: revert PR after a post-merge regression, and transcript hardening

Approved decisions (see the spec for the reasoning):

- The merge record is `<data_dir>/pipeline_merges/<merge_sha>.json`
  (`server.paths.get_data_dir()`), NOT a file in the project spec dir, because
  project members can write `.aifactory` files and the branch marker
  (`.task_branch`) too. Content: `project_id, spec_id, owner, repo, pr, branch,
  merge_sha, revert_pr_url?`. `run_pr_endgame` gets a new optional
  `project_id` (call site passes `_proj_id`). The route looks the record up by
  the supplied sha and requires its `project_id`/`spec_id` to equal the task's.
  The repo comes from the endgame context (trusted), never from the request.
  At revert time `gh pr view` must still show MERGED, `headRefName ==
  record.branch` and `mergeCommit.oid == record.merge_sha`.
- `open_revert_pr` runs under a process lock, is idempotent (stored URL, then a
  GitHub `gh pr list --state all --limit 100 --search` for the exact phrase
  `Reverts <owner>/<repo>#<pr>`, matched on the returned body so open, closed and
  merged hits all count), creates the PR
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

### Deviation from the approved spec (mechanism only)

The spec stored the marker in the spec dir and checked `recorded_branch`. Codex
review found both are member-writable, so the record moves to the server data
dir keyed by merge sha and bound to project and spec. Everything else is
unchanged.

## Steps

1. `apps/web-server/server/services/revert_pr.py` (new),
   `apps/web-server/tests/test_revert_pr.py` (new): `record_pipeline_merge(
   project_id, spec_id, owner, repo, pr, branch, runner)`,
   `open_revert_pr` as above, with a stub runner. Tests: merge marker written;
   revert created for the right sha (argv has `graphql`, body holds run URL,
   phrase and URIs); wrong sha, wrong branch, unmerged, no marker refused (incl. a record naming another branch/project/spec); second
   call returns `exists` without a second create; GitHub-search hit reconciled
   without create; no argv contains `merge`.
   -> `cd apps/web-server && python -m pytest tests/test_revert_pr.py`
   Also: a closed or merged earlier revert (search hit) prevents a create; two
   threads calling at once create once.
   Traps: runner signature is `runner(argv, cwd) -> CmdResult`; no secrets in logs
   (use `sanitize_log`).
2. `apps/web-server/server/services/pr_endgame.py` (~2268, `run_pr_endgame`) and
   its call in `completion_orchestration.py` (~548, `project_id=_proj_id`):
   add optional `project_id` to `run_pr_endgame` and pass `on_approved_merged` a
   wrapper that calls `record_pipeline_merge` (in its own try) then `re_test`. Test in `tests/test_pr_endgame.py`: marker written
   and `re_test` still called, also when recording raises.
   -> `cd apps/web-server && python -m pytest tests/test_pr_endgame.py`
3. `apps/web-server/server/routes/tasks_regression.py` (new), mounted in
   `routes/tasks.py` after `tasks_logs`; `tests/test_post_merge_regression_route.py`
   (new). Tests: happy path with a patched `open_revert_pr`, bad `run_url`, bad
   task id, unknown project, a viewer (403) and a record for another project/
   spec: each asserts `open_revert_pr`/gh is never called; the worker runs under
   the authorized org's pinned token.
   -> `cd apps/web-server && python -m pytest tests/test_post_merge_regression_route.py`
   Traps: validate `spec_id` with `safe_spec_component` before any path join.
4. `apps/backend/qa/reviewer.py` (`_run_tool_fallback` receive loop): record
   `ToolUseBlock` and `ToolResultBlock` via the task logger; test in
   `apps/backend/qa/test_reviewer_fallback_transcript.py` (new) using a fake
   provider.
   -> `cd apps/backend && python -m pytest qa/test_reviewer_fallback_transcript.py`
5. `apps/backend/task_logger/transcript.py` (`_clean`) and
   `task_logger/test_transcript.py`: `sk-ant-` redaction test.
   -> `cd apps/backend && python -m pytest task_logger/test_transcript.py`
6. File ONE new issue in olafkfreund/AIFactory for the deferred items (run id in
   the key and event, with the project id, since `transcriptUris` may name
   another project's objects on a repeated spec_id; spec-creation Jobs; streaming
   upload; hub `secrets` `sk-ant-` shapes and RFC-0004 status in Factory; the
   TFactory caller of the new endpoint; multi-replica revert idempotency). Link
   it from the PR and comment on #2054.
   -> `gh issue view <new> --repo olafkfreund/AIFactory`

## Tests

The five commands above, then `ruff check` and `ruff format --check` on the
touched files (AIFactory trap: plain names in parenthesised imports), and the
CI workflow's own test jobs on the PR.

## Rollback

Revert the PR. The route and service are new files; the endgame wrapper only
adds a best-effort marker write; no data migration.
