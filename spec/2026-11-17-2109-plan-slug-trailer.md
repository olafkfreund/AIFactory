---
status: draft
issue: 2109
intent: intent/2026-11-17-2109-plan-slug-trailer.md
---

# Spec: Stamp a Plan-Slug trailer on factory commits

## Design

Trailer format, matching PFactory `plan.governance_cli`: `Plan-Slug: <correlation_key>`
(the PFactory plan's `correlation_key`, e.g. `18` or `owner/Repo#18`).

1. Writer. In `execution.py` trusted-plan install (the block that already reads
   `request.plan["correlation_key"]`), also write `requirements["plan_slug"] =
   str(corr)` when `corr` matches ONE shared format, `PLAN_SLUG_RE =
   ^[A-Za-z0-9._/#:-]{1,128}$` (defined in `core/commit_trailers.py`, imported by
   the ingest and used by the deploy step). The stamp sits OUTSIDE the existing
   numeric-only guard (that guard stays for `issue_number`), so `owner/Repo#18`
   and `18` both work. Plan-less (from-issue) tasks never get the field.
2. Trailer. `commit_trailer_args` adds `("Plan-Slug", data.get("plan_slug"))` to
   its existing tuple, last, under the same flag, accepted only if it
   `fullmatch`es `PLAN_SLUG_RE`. Every framework commit path already goes through it (agents/utils.py,
   core/worktree.py, build_backend.py, approval.py, pr_endgame.py squash body),
   so none changes. The trailer set is rebuilt per commit from one dict, so a
   trailer cannot be duplicated.
3. Deploy join. `deploy.yml` build-push reads the `Plan-Slug` trailer from HEAD of
   the resolved deploy ref (`git interpret-trailers --parse`, first match,
   validated against the same regex), exposes it as a job output,
   and the gitops commit step appends `Plan-Slug: <v>` to the commit body when set.
   Passed through `env:`, never interpolated into the script.

## Alternatives rejected

- Derive `repo#issue` from `githubIssue`/`provenance` at read time: stamps plan-less
  from-issue tasks, contradicting "omit when no plan".
- Stamp the `af-<task>` synthetic key: not a plan, PFactory cannot resolve it.
- Patch the coder prompt's direct `git commit`: agent-controlled text, out of scope.

## Risks

Squash bodies of already-open PRs lack the trailer (new tasks only). A plan key
outside the format is silently omitted, like the other trailers.
Deploy workflow edit is unrun until the next main deploy; the shell is checked
locally with a sample commit.

## Verification

Unit tests in `tests/test_commit_trailers.py`: present and well-formed, absent
when no plan, bad values omitted, flag off, no duplicate in `commit_message_args`
/ `trailer_block`. Test for the ingest stamp with `18` and `owner/Repo#18`, and a rejected key. A local `git interpret-trailers`
run of the workflow snippet. `pytest` on touched tests and `ruff`.
