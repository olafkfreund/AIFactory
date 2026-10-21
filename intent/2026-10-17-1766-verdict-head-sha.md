---
status: approved
issue: 1766
author: olafkfreund
---

# Intent: bind the TFactory verdict to the head SHA, bound the watcher in time, require the branch's required checks

## Problem

Three follow-ups from the #1695 review of the merge-time gate
(`apps/web-server/server/services/pr_endgame.py`, `_build_merge_gate` at
`:1655`):

1. **The TFactory verdict is not tied to a commit.** `tfactory_verdict`
   (`apps/backend/pfactory/tfactory_client.py:901-950`) trusts any `triaged`
   task with accept/flag verdicts. It never sees the head the gate is about
   to merge (`pr_endgame.py:1687` calls it without the head). If anyone
   outside the endgame pushes to the PR branch after TFactory triaged, CI
   goes green on the new head and the old "pass" is applied to it. TFactory
   already records the commit it tested (`source_sha` in
   `context/source.json`, TFactory `task_control.py:535`) but its task GET
   (TFactory `apps/web-server/server/routes/tfactory_tasks.py:266-289`) does
   not return it.
2. **The watcher's "20 minutes" is counted in polls, not time.**
   `watch_and_finish` runs `max_minutes*60 // poll_interval` polls
   (`pr_endgame.py:1293`). Each gate evaluation can block on three `gh api`
   calls (`:969-1030`, 120 s timeout each) and two 10 s TFactory GETs, so a
   hung GitHub or TFactory stretches the human-stop to hours.
3. **Late-posting required checks.** `host_ci_green` (`:969`) only sees the
   checks that have posted. A required context that has not posted yet on a
   repo with late external CI is invisible, so the gate can call a partial
   head green.

## Proposed outcome

- The gate passes TFactory only when TFactory's recorded `source_sha` equals
  the PR head being merged. A missing `source_sha` (older TFactory, target
  mode, failed checkout) is "no trusted result": the merge holds.
- `watch_and_finish` ends at a `time.monotonic()` deadline of
  `max_minutes`, however long each poll takes (at most one poll past it).
- Before merging, the gate reads the base branch's required status checks
  (classic branch protection and rulesets). Each must be present and green on
  the head. If the token cannot read them, the merge holds and the log says
  why.
- The PR endgame guide says how to make GitHub itself enforce this: list the
  CI checks (and TFactory's check, if it posts one) as required checks.

## Affected users and systems

- AIFactory web-server PR endgame (auto-merge path for non-legacy tasks only;
  legacy tasks have no merge gate, `pr_endgame.py:1676`).
- TFactory web-server task GET route (one new response field).
- Projects with `AIFACTORY_AUTO_MERGE` on.

## Constraints

- Fail closed everywhere: unknown is never a pass.
- TFactory ships first; until it does, every gated auto-merge holds.
- No change to live branch protection or rulesets; code and docs only.
- No new dependency; reuse `gh api` through the existing `Runner`.

## Open questions

None. The user chose all three parts (see the spec's decisions).
