---
status: draft
issue: 2109
spec: spec/2026-11-17-2109-plan-slug-trailer.md
coders:
  3: codex   # contained workflow edit with a shell snippet; lead re-verifies
reviewers:
  - codex    # independent read-only review of each draft and the final diff
---

# Plan: Stamp a Plan-Slug trailer on factory commits

Decisions (from the approved spec): trailer `Plan-Slug: <correlation_key>`;
one format `PLAN_SLUG_RE = ^[A-Za-z0-9._/#:-]{1,128}$` shared by writer, trailer
and deploy step; written to `requirements.json["plan_slug"]` only by the
trusted-plan install, outside the numeric-only guard; same
`AIFACTORY_AGENT_COMMIT_TRAILERS` flag; deploy step copies the trailer of the
resolved ref's HEAD onto the gitops bump. Plan-less tasks get nothing.

## Steps

1. `apps/backend/core/commit_trailers.py`: add `PLAN_SLUG_RE`; in
   `commit_trailer_args` append `("Plan-Slug", plan_slug)` last, accepted only when
   `fullmatch(PLAN_SLUG_RE)` (keep other fields' `isprintable` rule).
   → `pytest tests/test_commit_trailers.py`
   Traps: use plain names in imports (ruff vs standards/ruff.toml).
2. `apps/web-server/server/routes/execution.py` ~L1479: after the numeric-guard
   block, separate try: if `corr` is a str/int and `fullmatch(PLAN_SLUG_RE)`, set
   `reqs["plan_slug"]`. Existing `issue_number` logic unchanged.
   → `pytest tests/test_trusted_plan.py` plus new test
3. `.github/workflows/deploy.yml`: build-push "Compute tags" step also emits
   `plan_slug` output (HEAD message, `git interpret-trailers --parse`, first
   `Plan-Slug:`, regex-checked, empty if none); job `outputs.plan_slug`; the gitops
   commit step takes it via `env: PLAN_SLUG` and appends `Plan-Slug: $PLAN_SLUG`
   to BODY when non-empty. No `${{ }}` inside the script body for it.
   → `python -c yaml.safe_load` and a local run of the snippet on a sample commit
4. `tests/test_commit_trailers.py` and `tests/test_trusted_plan.py`: well-formed,
   absent when no plan, bad value omitted, flag off, not duplicated in
   `commit_message_args`/`trailer_block`; ingest stamps `18` and `owner/Repo#18`,
   rejects `a b`, and does not stamp when plan has no correlation_key.

## Tests

`pytest tests/test_commit_trailers.py tests/test_trusted_plan.py`; `ruff check`
and `ruff format --check` on touched files.

## Rollback

Revert the PR; unset flag already disables all trailers.
